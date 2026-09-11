//! `ops` — the single self-mutation authority `apply_op` (PHASE_0_1_SPEC.md §5.6) + the drain driver
//! + crash recovery (§5.7). Phase 1 exercises only `rename`/`mkdir` behind an INTERNAL `apply_op`
//! (no `#[tauri::command]`, no frontend change); the state machine + journal are built in full so
//! Phase 4's file ops drop onto a proven foundation.
//!
//! Three layers, correctness never depending on an optimization (§5.1):
//!   * Layer 0 (floor): write-through — own syscall → `stat` → the SAME `reconcile_paths` the watcher
//!     uses, so the index is correct the instant `apply_op` returns, independent of any FSEvent.
//!   * Layer 1 (lossless dedup): a deferral token parks matching FSEvents, drained ONCE on a
//!     fail-open lease — a foreign change coalesced into a self-event still survives (drain re-stats).
//!   * Layer 2 (fast-path, Phase-4 only): MarkSelf / OwnEvent.

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Instant;

use crate::defer::{
    sidecar_of, DeferDecision, DeferralRegistry, DeferralToken, DRAIN_MARGIN, LEASE_TTL,
};
use crate::journal::{OpIntent, OpLog, OpRow, OpState, OpType};
use crate::pathkey::NormPath;
use crate::reconcile::{reconcile_paths, reconcile_tree, Change, ReconcileCtx};
use crate::writer::IndexWriter;

/// A planned self-mutation. Phase 1: rename + mkdir. Paths are RAW root-relative POSIX.
#[derive(Debug, Clone)]
pub enum OpPlan {
    Rename { src: String, dst: String },
    Mkdir { path: String },
}

impl OpPlan {
    fn intent(&self) -> OpIntent {
        match self {
            OpPlan::Rename { src, dst } => OpIntent {
                op_type: OpType::Rename,
                src: Some(src.clone()),
                dst: Some(dst.clone()),
                temp_path: None,
            },
            OpPlan::Mkdir { path } => OpIntent {
                op_type: OpType::Mkdir,
                src: None,
                dst: Some(path.clone()),
                temp_path: None,
            },
        }
    }
}

/// The context every self-mutation shares (the writer, the reconcile authority, the durable oplog,
/// the in-memory deferral registry, and the current batch id).
pub struct OpCtx<'a> {
    pub reconcile: &'a ReconcileCtx,
    pub writer: &'a IndexWriter,
    pub oplog: &'a OpLog,
    pub registry: &'a Mutex<DeferralRegistry>,
    pub batch_id: String,
}

impl OpCtx<'_> {
    fn root(&self) -> &str {
        &self.reconcile.root
    }
}

fn poisoned(_e: impl std::fmt::Display) -> String {
    "deferral registry mutex poisoned".to_string()
}

/// The single self-mutation authority (§5.6). Records durable intent, registers a deferral token,
/// runs the syscall, then Layer-0 write-through reconciles via the WATCHER's `reconcile_paths` — so
/// the index is correct BEFORE any FSEvent. Opens the short trailing drain window on commit.
pub fn apply_op(ctx: &OpCtx, plan: OpPlan) -> Result<i64, String> {
    let root = ctx.root();

    // 1) INTENT (durable) + register the deferral token (footprint incl. `._` sidecars + subtree
    //    prefixes; lease_until = now + LEASE_TTL). Token ACTIVE.
    let op_id = ctx.oplog.begin(&plan.intent(), &ctx.batch_id)?;
    let (footprint, subtree_prefixes, subtree_roots_raw) = footprint_for(root, &plan);
    ctx.registry.lock().map_err(poisoned)?.register(DeferralToken {
        op_id,
        batch_id: ctx.batch_id.clone(),
        footprint,
        subtree_prefixes,
        subtree_roots_raw,
        lease_until: Instant::now() + LEASE_TTL,
        drain_at: None,
    });
    maybe_crash("after_intent");

    // 2) → executing
    ctx.oplog.transition(op_id, OpState::Executing, None)?;
    maybe_crash("after_executing");

    // 3) SYSCALL (Phase 1: rename / mkdir)
    let abs = footprint_abs(root, &plan);
    match do_syscall(root, &plan) {
        Err(e) => {
            ctx.oplog.transition(op_id, OpState::Failed, Some(&e))?;
            let _ = reconcile_paths(ctx.writer, ctx.reconcile, &abs); // index → ACTUAL state
            ctx.registry.lock().map_err(poisoned)?.begin_drain(op_id, Instant::now());
            return Err(e);
        }
        Ok(Some(url)) => ctx.oplog.set_trashed_url(op_id, &url)?, // Phase 4
        Ok(None) => {}
    }
    maybe_crash("after_syscall");

    // 4) LAYER-0 WRITE-THROUGH: reconcile via the watcher's fn — entries correct before any FSEvent.
    reconcile_paths(ctx.writer, ctx.reconcile, &abs)?;
    maybe_crash("after_writethrough");

    // 5) → committed; open the short trailing drain window (§5.6).
    ctx.oplog.transition(op_id, OpState::Committed, None)?;
    ctx.registry
        .lock()
        .map_err(poisoned)?
        .begin_drain(op_id, Instant::now() + DRAIN_MARGIN);
    maybe_crash("after_commit");

    // 6) the drain DRIVER ([`drain_tick`]) later reconciles any parked foreign paths ONCE and
    //    transitions committed → settled.
    Ok(op_id)
}

fn do_syscall(root: &str, plan: &OpPlan) -> Result<Option<String>, String> {
    let base = Path::new(root);
    match plan {
        OpPlan::Rename { src, dst } => {
            if let Some(parent) = base.join(dst).parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            std::fs::rename(base.join(src), base.join(dst))
                .map_err(|e| format!("rename {src} -> {dst}: {e}"))?;
            Ok(None)
        }
        OpPlan::Mkdir { path } => {
            std::fs::create_dir_all(base.join(path)).map_err(|e| format!("mkdir {path}: {e}"))?;
            Ok(None)
        }
    }
}

/// The EXACT normalized-key footprint (src/dst/temp + `._` sidecars) AND the `subtree_prefixes` (for
/// a dir rename whose descendants are unenumerable, §0.5G) for DEFERRAL matching.
pub fn footprint_for(
    root: &str,
    plan: &OpPlan,
) -> (HashSet<NormPath>, HashSet<NormPath>, Vec<PathBuf>) {
    let mut footprint = HashSet::new();
    let mut prefixes = HashSet::new();
    let mut roots_raw: Vec<PathBuf> = Vec::new();
    match plan {
        OpPlan::Rename { src, dst } => {
            for p in [src, dst] {
                footprint.insert(NormPath::of(p));
                footprint.insert(NormPath::of(&sidecar_of(p)));
            }
            // Subtree prefixes ONLY for a REAL DIRECTORY rename (an unenumerable descendant set,
            // §0.5G). Determined by lstat'ing `src` NOW, while it still exists (apply_op calls this
            // pre-syscall). Use `symlink_metadata` (NOT `Path::is_dir`, which FOLLOWS the link): a
            // symlink-TO-a-dir is record-not-traverse — it must NOT carry a subtree prefix, or the
            // drain would `reconcile_tree` the symlink path and walk INTO the target. A plain FILE
            // rename likewise gets no prefix (else the drain stamps a bogus dir row over the file).
            let src_is_real_dir = std::fs::symlink_metadata(Path::new(root).join(src))
                .map(|m| m.is_dir())
                .unwrap_or(false);
            if src_is_real_dir {
                prefixes.insert(NormPath::of(src));
                prefixes.insert(NormPath::of(dst));
                // RAW roots for the drain's reconcile_tree (true casing, not the folded key).
                roots_raw.push(Path::new(root).join(src));
                roots_raw.push(Path::new(root).join(dst));
            }
        }
        OpPlan::Mkdir { path } => {
            footprint.insert(NormPath::of(path));
        }
    }
    (footprint, prefixes, roots_raw)
}

/// RAW `root.join(rel)` absolute paths (sidecars excluded) for `reconcile_paths` input.
pub fn footprint_abs(root: &str, plan: &OpPlan) -> Vec<PathBuf> {
    let base = Path::new(root);
    match plan {
        OpPlan::Rename { src, dst } => vec![base.join(src), base.join(dst)],
        OpPlan::Mkdir { path } => vec![base.join(path)],
    }
}

/// The same, reconstructed from a durable op-journal row (for recovery).
pub fn footprint_abs_from_row(root: &str, row: &OpRow) -> Vec<PathBuf> {
    let base = Path::new(root);
    let mut v = Vec::new();
    if let Some(s) = &row.src {
        v.push(base.join(s));
    }
    if let Some(d) = &row.dst {
        v.push(base.join(d));
    }
    v
}

/// Crash recovery (§5.7). For every non-terminal op: reconcile the index to FS truth (idempotent),
/// then BRANCH on state — `planned`/`executing` → `failed` (interrupted before commit); `committed`
/// → `settled` (the syscall AND the write-through already completed — marking it `failed` would make
/// a fully-completed move un-undoable in Phase 4). The FILESYSTEM is the arbiter, so no torn state
/// misleads the index and cross-DB atomicity is unnecessary. Roll-forward from FS evidence (§0.5G).
pub fn recover(oplog: &OpLog, writer: &IndexWriter, reconcile: &ReconcileCtx) -> Result<(), String> {
    let root = &reconcile.root;
    for row in oplog.select_non_terminal()? {
        let abs = footprint_abs_from_row(root, &row);
        let _ = reconcile_paths(writer, reconcile, &abs); // index → truth (idempotent)
        match row.state {
            OpState::Planned | OpState::Executing => oplog.transition(
                row.op_id,
                OpState::Failed,
                Some("recovered: interrupted before commit"),
            )?,
            OpState::Committed => oplog.transition(row.op_id, OpState::Settled, None)?,
            // Phase-4 mid-states: resume/rollback TODO — leave as-is (FS + intent are sufficient).
            OpState::Copied | OpState::Verified | OpState::Finalized | OpState::SourceRemoved => {}
            _ => {}
        }
    }
    Ok(())
}

/// The drain DRIVER (§5.5): retire expired tokens, reconcile their drained point-paths AND subtree
/// roots (a dir op's coalesced foreign descendants are caught by re-stating the whole subtree), then
/// transition each retired `committed` op → `settled`. Without this, a lone rename with no subsequent
/// FSEvent would never leave `committed`. Both the drained paths and the subtree roots are RAW abs
/// paths (true on-disk casing), so `reconcile_paths`/`reconcile_tree` store the correct display path.
pub fn drain_tick(ctx: &OpCtx) -> Result<Vec<Change>, String> {
    let result = ctx.registry.lock().map_err(poisoned)?.retire_expired(Instant::now());
    let mut changes = Vec::new();
    if !result.drained.is_empty() {
        changes.extend(reconcile_paths(ctx.writer, ctx.reconcile, &result.drained).unwrap_or_default());
    }
    for raw_root in &result.subtree_roots {
        if let Some(rel) = raw_root
            .strip_prefix(&ctx.reconcile.root)
            .ok()
            .map(|p| p.to_string_lossy().replace('\\', "/"))
        {
            changes.extend(reconcile_tree(ctx.writer, ctx.reconcile, &rel).unwrap_or_default());
        }
    }
    for op_id in result.retired_ops {
        if let Some(row) = ctx.oplog.get(op_id)? {
            if row.state == OpState::Committed {
                ctx.oplog.transition(op_id, OpState::Settled, None)?;
            }
        }
    }
    Ok(changes)
}

/// The watcher's per-event dispatch (§5.5). Order per tick: (1) ALWAYS reconcile drained (drain
/// first), then (2) park a matched self-event OR reconcile an external/unmatched one. `maybe_defer`
/// does NOT retire internally — the dispatcher owns the drain, so a drained path is never discarded.
pub fn dispatch_event(
    ctx: &OpCtx,
    ev_key: &NormPath,
    ev_abs: &Path,
    own_event: bool,
) -> Result<Vec<Change>, String> {
    let mut changes = drain_tick(ctx)?;
    let decision = ctx.registry.lock().map_err(poisoned)?.maybe_defer(ev_key, ev_abs, own_event);
    if decision == DeferDecision::Reconcile {
        changes.extend(reconcile_paths(ctx.writer, ctx.reconcile, &[ev_abs.to_path_buf()])?);
    }
    Ok(changes)
}

/// Test-only fault injection (§8 gate 9): `abort()` at `point` iff `LENS_CRASH_AT` matches it. A no-op
/// in production (the env var is never set). `SIGABRT` ≈ `kill -9` for our purposes — no stack
/// unwinding, no `Drop`, no flush — so recovery must reconverge from durable state + FS evidence alone.
#[inline]
fn maybe_crash(point: &str) {
    if std::env::var("LENS_CRASH_AT").ok().as_deref() == Some(point) {
        std::process::abort();
    }
}

/// Build an [`OpPlan`] key for a raw rel path (used by callers/tests).
pub fn key_of(rel: &str) -> NormPath {
    NormPath::from_key(crate::pathkey::norm_key(rel))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reconcile::GenericMetaSource;
    use rusqlite::OptionalExtension;
    use std::sync::Arc;
    use std::time::Duration;

    struct Harness {
        dir: PathBuf,
        writer: IndexWriter,
        oplog: OpLog,
        reconcile: ReconcileCtx,
        registry: Mutex<DeferralRegistry>,
    }

    fn harness(tag: &str) -> Harness {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("_ops_scratch_{tag}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("_repo_index")).unwrap();
        let writer = IndexWriter::open(
            dir.to_str().unwrap(),
            dir.join("_repo_index/INDEX.sqlite").to_str().unwrap(),
        )
        .unwrap();
        let oplog = OpLog::open_at(dir.join("_repo_index/oplog.sqlite").to_str().unwrap()).unwrap();
        let reconcile = ReconcileCtx::new(dir.to_str().unwrap(), Arc::new(GenericMetaSource));
        Harness { dir, writer, oplog, reconcile, registry: Mutex::new(DeferralRegistry::new()) }
    }

    impl Harness {
        fn ctx(&self, batch: &str) -> OpCtx<'_> {
            OpCtx {
                reconcile: &self.reconcile,
                writer: &self.writer,
                oplog: &self.oplog,
                registry: &self.registry,
                batch_id: batch.to_string(),
            }
        }
        fn write(&self, rel: &str, content: &[u8]) {
            let p = self.dir.join(rel);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(p, content).unwrap();
        }
        fn has(&self, rel: &str) -> bool {
            self.writer
                .with_conn(|c| {
                    c.query_row("SELECT 1 FROM entries WHERE path=?1", rusqlite::params![rel], |_| Ok(()))
                        .optional()
                        .map(|o| o.is_some())
                        .map_err(|e| e.to_string())
                })
                .unwrap()
        }
        fn size_of(&self, rel: &str) -> Option<i64> {
            self.writer
                .with_conn(|c| {
                    c.query_row("SELECT size_bytes FROM entries WHERE path=?1", rusqlite::params![rel], |r| r.get(0))
                        .optional()
                        .map_err(|e| e.to_string())
                })
                .unwrap()
        }
        fn cleanup(self) {
            drop(self.writer);
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    #[test]
    fn apply_op_rename_is_written_through_before_any_fsevent() {
        let h = harness("rename");
        h.write("a/src.csv", b"hello");
        let ctx = h.ctx("b1");
        let id = apply_op(&ctx, OpPlan::Rename { src: "a/src.csv".into(), dst: "a/dst.csv".into() }).unwrap();

        // Index reflects the move with NO watcher/FSEvent — Layer 0.
        assert!(h.has("a/dst.csv"), "dst indexed by write-through");
        assert!(!h.has("a/src.csv"), "src removed by write-through");
        assert_eq!(h.oplog.get(id).unwrap().unwrap().state, OpState::Committed);
        h.cleanup();
    }

    #[test]
    fn apply_op_mkdir_inserts_a_dir_row() {
        let h = harness("mkdir");
        let ctx = h.ctx("b1");
        apply_op(&ctx, OpPlan::Mkdir { path: "fresh/dir".into() }).unwrap();
        let is_dir: Option<i64> = h
            .writer
            .with_conn(|c| {
                c.query_row("SELECT is_dir FROM entries WHERE path='fresh/dir'", [], |r| r.get(0))
                    .optional()
                    .map_err(|e| e.to_string())
            })
            .unwrap();
        assert_eq!(is_dir, Some(1), "live mkdir inserts a visible is_dir=1 row");
        h.cleanup();
    }

    #[test]
    fn deferral_losslessness_coalesced_foreign_change_survives() {
        // THE regression that distinguishes DEFER from SUPPRESS (§8 gate 8), driving the PRODUCTION
        // dispatch path (not a direct retire_expired call).
        let h = harness("lossless");
        h.write("src.csv", b"aaaa"); // 4 bytes
        let ctx = h.ctx("b1");
        apply_op(&ctx, OpPlan::Rename { src: "src.csv".into(), dst: "dst.csv".into() }).unwrap();
        assert_eq!(h.size_of("dst.csv"), Some(4), "write-through indexed dst at 4 bytes");

        // A FOREIGN process modifies dst (coalesced into the SAME FSEvent as our rename).
        std::fs::write(h.dir.join("dst.csv"), b"bbbbbbbb").unwrap(); // now 8 bytes

        // The (coalesced) event for dst arrives → the registry PARKS it (a suppression window would
        // DROP it, losing the foreign change). Index still shows the old size at this instant.
        let dst_key = key_of("dst.csv");
        dispatch_event(&ctx, &dst_key, &h.dir.join("dst.csv"), false).unwrap();
        assert_eq!(h.size_of("dst.csv"), Some(4), "parked, not yet reconciled (defer, not eager)");

        // After the drain window elapses, the drain reconciles dst ONCE → the foreign change survives.
        std::thread::sleep(DRAIN_MARGIN + Duration::from_millis(50));
        drain_tick(&ctx).unwrap();
        assert_eq!(h.size_of("dst.csv"), Some(8), "coalesced foreign change survived the deferral");
        h.cleanup();
    }

    #[test]
    fn drain_preserves_raw_casing_of_a_self_event() {
        // Regression for the drain-reconciles-the-folded-key bug: the parked raw path must be
        // reconciled (true on-disk casing), NOT root.join(folded_key), or the row's `path` column
        // gets overwritten with the lowercase/NFC-folded spelling.
        let h = harness("case");
        h.write("src.csv", b"aaaa");
        let ctx = h.ctx("b1");
        apply_op(&ctx, OpPlan::Rename { src: "src.csv".into(), dst: "Mixed.CSV".into() }).unwrap();
        assert!(h.has("Mixed.CSV"), "write-through indexed the raw casing");

        std::fs::write(h.dir.join("Mixed.CSV"), b"bbbbbbbb").unwrap(); // coalesced foreign modify
        let key = key_of("Mixed.CSV");
        dispatch_event(&ctx, &key, &h.dir.join("Mixed.CSV"), false).unwrap(); // parks the RAW path
        std::thread::sleep(DRAIN_MARGIN + Duration::from_millis(50));
        drain_tick(&ctx).unwrap();

        let path: Option<String> = h
            .writer
            .with_conn(|c| {
                c.query_row(
                    "SELECT path FROM entries WHERE path_key=?1 AND is_dir=0",
                    rusqlite::params![crate::pathkey::norm_key("Mixed.CSV")],
                    |r| r.get(0),
                )
                .optional()
                .map_err(|e| e.to_string())
            })
            .unwrap();
        assert_eq!(path.as_deref(), Some("Mixed.CSV"), "drain kept the RAW casing, not the folded key");
        assert_eq!(h.size_of("Mixed.CSV"), Some(8), "and the coalesced foreign change survived");
        h.cleanup();
    }

    #[test]
    fn drain_settles_a_committed_op_with_no_subsequent_event() {
        let h = harness("settle");
        h.write("x.csv", b"x");
        let ctx = h.ctx("b1");
        let id = apply_op(&ctx, OpPlan::Rename { src: "x.csv".into(), dst: "y.csv".into() }).unwrap();
        assert_eq!(h.oplog.get(id).unwrap().unwrap().state, OpState::Committed);
        std::thread::sleep(DRAIN_MARGIN + Duration::from_millis(50));
        drain_tick(&ctx).unwrap();
        assert_eq!(h.oplog.get(id).unwrap().unwrap().state, OpState::Settled, "drain driver settles it");
        h.cleanup();
    }

    // ── §8 gate 9: kill -9 (SIGABRT) crash-injection harness ──────────────────────────────────────
    // `crash_child` is a subprocess entry point (a no-op under a normal `cargo test` run). The parent
    // spawns the test binary to run ONLY this test, with LENS_CRASH_ROOT + LENS_CRASH_AT set, so
    // `apply_op` aborts at the chosen point — SIGABRT ≈ kill -9 (no unwinding, no Drop, no flush).

    #[test]
    fn crash_child() {
        let root = match std::env::var("LENS_CRASH_ROOT") {
            Ok(r) => r,
            Err(_) => return, // normal test run: no-op
        };
        std::fs::create_dir_all(Path::new(&root).join("_repo_index")).unwrap();
        let writer = IndexWriter::open(&root, &format!("{root}/_repo_index/INDEX.sqlite")).unwrap();
        let oplog = OpLog::open_at(&format!("{root}/_repo_index/oplog.sqlite")).unwrap();
        let reconcile = ReconcileCtx::new(root.clone(), Arc::new(GenericMetaSource));
        let registry = Mutex::new(DeferralRegistry::new());
        let ctx = OpCtx {
            reconcile: &reconcile,
            writer: &writer,
            oplog: &oplog,
            registry: &registry,
            batch_id: "crash".into(),
        };
        // src.csv was written on disk by the parent before the spawn.
        let _ = apply_op(&ctx, OpPlan::Rename { src: "src.csv".into(), dst: "dst.csv".into() });
        // reached only if no crash point matched — exit cleanly (parent asserts an abort otherwise)
    }

    #[test]
    fn crash_injection_recover_reconverges_at_every_point() {
        let exe = std::env::current_exe().unwrap();
        for point in ["after_intent", "after_executing", "after_syscall", "after_writethrough", "after_commit"] {
            let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("_crash_{point}"));
            let _ = std::fs::remove_dir_all(&dir);
            std::fs::create_dir_all(dir.join("_repo_index")).unwrap();
            std::fs::write(dir.join("src.csv"), b"hello").unwrap();

            let status = std::process::Command::new(&exe)
                .args(["ops::tests::crash_child", "--exact", "--test-threads=1"])
                .env("LENS_CRASH_ROOT", dir.to_str().unwrap())
                .env("LENS_CRASH_AT", point)
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status()
                .unwrap();
            assert!(!status.success(), "child must have aborted (SIGABRT) at {point}");

            // Recover IN-PROCESS from the crashed on-disk state (the child's flock died with it).
            let writer = IndexWriter::open(
                dir.to_str().unwrap(),
                dir.join("_repo_index/INDEX.sqlite").to_str().unwrap(),
            )
            .unwrap();
            let oplog = OpLog::open_at(dir.join("_repo_index/oplog.sqlite").to_str().unwrap()).unwrap();
            let reconcile = ReconcileCtx::new(dir.to_str().unwrap(), Arc::new(GenericMetaSource));
            recover(&oplog, &writer, &reconcile).unwrap();

            // (1) every op is now terminal — nothing lingers planned/executing/committed
            assert!(
                oplog.select_non_terminal().unwrap().is_empty(),
                "recover resolves all ops at {point}"
            );
            // (2) the index matches the FILESYSTEM (the arbiter): exactly the file that exists on disk
            let indexed = |rel: &str| -> bool {
                writer
                    .with_conn(|c| {
                        c.query_row("SELECT 1 FROM entries WHERE path=?1", rusqlite::params![rel], |_| Ok(()))
                            .optional()
                            .map(|o| o.is_some())
                            .map_err(|e| e.to_string())
                    })
                    .unwrap()
            };
            assert_eq!(indexed("src.csv"), dir.join("src.csv").exists(), "src index==disk at {point}");
            assert_eq!(indexed("dst.csv"), dir.join("dst.csv").exists(), "dst index==disk at {point}");

            drop(writer);
            let _ = std::fs::remove_dir_all(&dir);
        }
    }

    #[test]
    fn recover_branches_and_reconverges_from_fs_evidence() {
        // Simulate a crash: seed oplog rows in mid-states with matching FS states, then recover.
        let h = harness("recover");

        // (a) 'planned' rename that never ran: src still on disk, dst absent.
        h.write("p_src.csv", b"p");
        let planned = h
            .oplog
            .begin(&OpIntent { op_type: OpType::Rename, src: Some("p_src.csv".into()), dst: Some("p_dst.csv".into()), temp_path: None }, "b1")
            .unwrap();

        // (b) 'committed' rename that DID run: c_src gone, c_dst on disk.
        h.write("c_dst.csv", b"cc");
        let committed = h
            .oplog
            .begin(&OpIntent { op_type: OpType::Rename, src: Some("c_src.csv".into()), dst: Some("c_dst.csv".into()), temp_path: None }, "b2")
            .unwrap();
        h.oplog.transition(committed, OpState::Executing, None).unwrap();
        h.oplog.transition(committed, OpState::Committed, None).unwrap();

        recover(&h.oplog, &h.writer, &h.reconcile).unwrap();

        // planned → failed; index reconverged (p_src present, p_dst absent)
        assert_eq!(h.oplog.get(planned).unwrap().unwrap().state, OpState::Failed);
        assert!(h.has("p_src.csv"));
        assert!(!h.has("p_dst.csv"));
        // committed → settled; index reconverged (c_dst present)
        assert_eq!(h.oplog.get(committed).unwrap().unwrap().state, OpState::Settled);
        assert!(h.has("c_dst.csv"));
        h.cleanup();
    }
}
