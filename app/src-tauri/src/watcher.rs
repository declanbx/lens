//! `watcher` — the FSEvents watcher + debounced, stat-is-truth reconciler (PHASE_0_1_SPEC.md §4.1–§4.3).
//! Turns the read-only viewer into a self-updating index: `⟳` becomes automatic and the tree never
//! lags disk by more than ~2 s.
//!
//! Design (§0.5G): use `notify`'s RAW watcher (the reconciler owns debounce, NOT notify) behind a
//! `Watcher` trait so a later swap to raw `fsevent-sys` (for MarkSelf/OwnEvent, Phase 4) is a cheap
//! backend change. The callback does ZERO work → it pushes onto a channel; a dedicated `std::thread`
//! owns the dirty-set + debounce and is the ONLY caller of the reconcile authority. Events are HINTS,
//! never facts — the reconciler `lstat`s and believes only the filesystem.
//!
//! Phase 1 processes FOREIGN changes (the deferral/drain self-event machinery in ops.rs is built +
//! tested but dormant in the app, since `apply_op` is internal/test-only — no FS-mutating command).

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::time::{Duration, Instant};

use notify::{RecursiveMode, Watcher};

use crate::reconcile::{reconcile_paths, reconcile_tree, Change, ReconcileCtx, WalkConfig};
use crate::writer::IndexWriter;

/// Flush this long after the LAST event (§4.3).
const QUIESCENCE: Duration = Duration::from_millis(300);
/// A sustained storm still flushes within this cap.
const MAX_LATENCY: Duration = Duration::from_secs(2);
/// Storm backstop: past this many point paths, collapse to a whole-tree rescan.
const DIRTY_HARD_CAP: usize = 10_000;
/// The recv timeout when there's nothing to flush (idle, or paused): effectively block on the channel.
const IDLE_TIMEOUT: Duration = Duration::from_secs(3600);

/// Callback invoked after each committed flush with the changed directory keys (the app emits the
/// `index-changed` Tauri event; tests use it to observe progress).
pub type OnChange = Box<dyn Fn(Vec<String>) + Send>;

enum Msg {
    Event(notify::Result<notify::Event>),
    Control(Control),
}

enum Control {
    Rescan,
    Pause,
    Resume,
    Stop,
}

/// Drop an event BEFORE it enters the dirty-set (§4.1): the index's own churn (`_repo_index/` +
/// `.repo_index/` — both spellings until §3.8 unifies), `._*` AppleDouble sidecars, `.DS_Store`, and
/// the prune-dir set (`.git`, `node_modules`, …). Keeping the index's WAL churn out of the reconciler
/// is the feedback-loop guard.
pub fn is_watch_excluded(rel: &str, config: &WalkConfig) -> bool {
    for comp in rel.split('/') {
        if comp == "_repo_index" || comp == ".repo_index" {
            return true;
        }
        if comp.starts_with("._") || comp == ".DS_Store" {
            return true;
        }
        if config.prune_dirs.contains(comp) {
            return true;
        }
    }
    false
}

/// The dirty-set: point paths (stat one) + recursive subtrees (rescan). A recursive entry subsumes
/// everything beneath it; a point under a recursive ancestor is dropped (§4.3). Keyed by RAW rel
/// (reconcile is idempotent, so any residual case-redundancy is harmless).
#[derive(Default)]
struct DirtySet {
    points: HashSet<String>,
    recursive: HashSet<String>,
    first_dirty: Option<Instant>,
    last_event: Option<Instant>,
}

fn is_descendant(child: &str, ancestor: &str) -> bool {
    if ancestor.is_empty() {
        return true;
    }
    child == ancestor
        || (child.len() > ancestor.len()
            && child.starts_with(ancestor)
            && child.as_bytes()[ancestor.len()] == b'/')
}

impl DirtySet {
    fn is_empty(&self) -> bool {
        self.points.is_empty() && self.recursive.is_empty()
    }

    fn touch(&mut self, now: Instant) {
        self.first_dirty.get_or_insert(now);
        self.last_event = Some(now);
    }

    fn mark_point(&mut self, rel: String, now: Instant) {
        self.touch(now);
        // covered by a recursive ancestor? then it's already going to be rescanned.
        if self.recursive.iter().any(|r| is_descendant(&rel, r)) {
            return;
        }
        self.points.insert(rel);
        if self.points.len() > DIRTY_HARD_CAP {
            self.mark_recursive(String::new(), now); // storm → whole-tree rescan
        }
    }

    fn mark_recursive(&mut self, rel: String, now: Instant) {
        self.touch(now);
        if rel.is_empty() {
            self.points.clear();
            self.recursive.clear();
            self.recursive.insert(String::new());
            return;
        }
        self.points.retain(|p| !is_descendant(p, &rel));
        self.recursive.retain(|r| !is_descendant(r, &rel));
        // if an ancestor recursive already covers rel, nothing to add
        if self.recursive.iter().any(|r| is_descendant(&rel, r)) {
            return;
        }
        self.recursive.insert(rel);
    }

    /// Is the debounce satisfied (quiescence since the last event, or the max-latency cap reached)?
    fn ready(&self, now: Instant) -> bool {
        if self.is_empty() {
            return false;
        }
        let quiet = self.last_event.map_or(true, |t| now.saturating_duration_since(t) >= QUIESCENCE);
        let capped =
            self.first_dirty.map_or(false, |t| now.saturating_duration_since(t) >= MAX_LATENCY);
        quiet || capped
    }

    /// The recv_timeout to use: when idle, block long; when dirty, wake at the nearer of the
    /// quiescence / max-latency deadlines.
    fn timeout(&self, now: Instant) -> Duration {
        if self.is_empty() {
            return Duration::from_secs(3600);
        }
        let quiet_at = self.last_event.map(|t| t + QUIESCENCE);
        let cap_at = self.first_dirty.map(|t| t + MAX_LATENCY);
        let next = [quiet_at, cap_at].into_iter().flatten().min().unwrap_or(now);
        next.saturating_duration_since(now)
    }

    fn drain(&mut self) -> (Vec<String>, Vec<String>) {
        let recs: Vec<String> = self.recursive.drain().collect();
        let pts: Vec<String> = self.points.drain().collect();
        self.first_dirty = None;
        self.last_event = None;
        (recs, pts)
    }
}

/// A running watcher + its reconciler thread. Dropping (or `stop`) tears both down cleanly.
pub struct WatcherHandle {
    _watcher: notify::RecommendedWatcher, // kept alive; dropping it stops FSEvents delivery
    control: Sender<Msg>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl WatcherHandle {
    /// Watch-then-scan (§4.8): construct the `notify` watcher FIRST (events buffer into the channel),
    /// THEN the reconciler thread runs the initial `reconcile_tree("")` and begins draining — so an
    /// event during startup is never lost (buffered events replay harmlessly, idempotent).
    pub fn spawn(
        root: &str,
        writer: Arc<IndexWriter>,
        ctx: Arc<ReconcileCtx>,
        on_change: OnChange,
    ) -> Result<WatcherHandle, String> {
        let (tx, rx) = mpsc::channel::<Msg>();

        let ev_tx = tx.clone();
        let mut watcher = notify::recommended_watcher(move |res| {
            let _ = ev_tx.send(Msg::Event(res)); // callback does ZERO work
        })
        .map_err(|e| format!("notify watcher: {e}"))?;
        watcher
            .watch(Path::new(root), RecursiveMode::Recursive)
            .map_err(|e| format!("notify watch {root}: {e}"))?;

        let root_owned = root.to_string();
        let thread = std::thread::spawn(move || {
            reconciler_loop(rx, root_owned, writer, ctx, on_change);
        });

        Ok(WatcherHandle { _watcher: watcher, control: tx, thread: Some(thread) })
    }

    /// Push a whole-tree rescan into the reconciler (the `force_rescan` command routes here — writing
    /// on the IPC thread would interleave with the reconciler's flush, §4.4).
    pub fn force_rescan(&self) {
        let _ = self.control.send(Msg::Control(Control::Rescan));
    }

    /// Stop flushing (buffer events) — used by the in-app Rebuild before the JSONL re-ingest (§3.6).
    pub fn pause(&self) {
        let _ = self.control.send(Msg::Control(Control::Pause));
    }

    /// Resume flushing and force a full rescan to reconverge Rust↔disk (§3.6).
    pub fn resume_with_full_rescan(&self) {
        let _ = self.control.send(Msg::Control(Control::Resume));
    }

    pub fn stop(mut self) {
        self.shutdown();
    }

    fn shutdown(&mut self) {
        let _ = self.control.send(Msg::Control(Control::Stop));
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

impl Drop for WatcherHandle {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn reconciler_loop(
    rx: mpsc::Receiver<Msg>,
    root: String,
    writer: Arc<IndexWriter>,
    ctx: Arc<ReconcileCtx>,
    on_change: OnChange,
) {
    // Initial full reconcile (startup) — buffered startup events replay harmlessly after.
    let changes = reconcile_tree(&writer, &ctx, "").unwrap_or_default();
    emit(&on_change, &changes);

    let mut dirty = DirtySet::default();
    let mut paused = false;
    loop {
        let now = Instant::now();
        // When PAUSED, no flush can happen until Resume — so ignore the debounce deadlines (a
        // non-empty dirty set would make `timeout()` return ~0, and `recv_timeout(0)` would return
        // immediately, spinning the loop at 100% CPU). Block on the channel instead (§4.3 guard).
        let wait = if paused { IDLE_TIMEOUT } else { dirty.timeout(now) };
        match rx.recv_timeout(wait) {
            Ok(Msg::Event(Ok(ev))) => ingest(&mut dirty, &ev, &root, &ctx.config),
            Ok(Msg::Event(Err(_))) => dirty.mark_recursive(String::new(), Instant::now()), // notify error → rescan
            Ok(Msg::Control(Control::Rescan)) => dirty.mark_recursive(String::new(), Instant::now()),
            Ok(Msg::Control(Control::Pause)) => paused = true,
            Ok(Msg::Control(Control::Resume)) => {
                paused = false;
                dirty.mark_recursive(String::new(), Instant::now());
            }
            Ok(Msg::Control(Control::Stop)) => break,
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        if !paused && dirty.ready(Instant::now()) {
            let (recs, pts) = dirty.drain();
            let changes = flush(&writer, &ctx, &root, recs, pts);
            emit(&on_change, &changes);
        }
    }
}

/// Interpret an event ONLY as "needs a rescan?" + "which paths" — never branch on create/modify/
/// remove flags (that decision is deferred to `stat`, §4.3). Excluded paths are dropped here.
fn ingest(dirty: &mut DirtySet, ev: &notify::Event, root: &str, config: &WalkConfig) {
    let now = Instant::now();
    if ev.need_rescan() {
        // MustScanSubDirs / dropped → rescan the shallowest reported dir (else the whole tree).
        let shallow = ev
            .paths
            .iter()
            .filter_map(|p| rel_of(p, root))
            .filter(|r| !is_watch_excluded(r, config))
            .min_by_key(|r| r.matches('/').count());
        dirty.mark_recursive(shallow.unwrap_or_default(), now);
        return;
    }
    for p in &ev.paths {
        if let Some(rel) = rel_of(p, root) {
            if rel.is_empty() || is_watch_excluded(&rel, config) {
                continue;
            }
            dirty.mark_point(rel, now);
        }
    }
}

fn flush(
    writer: &IndexWriter,
    ctx: &ReconcileCtx,
    root: &str,
    recs: Vec<String>,
    pts: Vec<String>,
) -> Vec<Change> {
    let mut changes = Vec::new();
    // A whole-tree rescan subsumes everything.
    if recs.iter().any(|r| r.is_empty()) {
        return reconcile_tree(writer, ctx, "").unwrap_or_default();
    }
    for r in &recs {
        changes.extend(reconcile_tree(writer, ctx, r).unwrap_or_default());
    }
    // Point paths not covered by a recursive entry → one batched reconcile_paths.
    let abs: Vec<PathBuf> = pts
        .iter()
        .filter(|p| !recs.iter().any(|r| is_descendant(p, r)))
        .map(|p| Path::new(root).join(p))
        .collect();
    if !abs.is_empty() {
        changes.extend(reconcile_paths(writer, ctx, &abs).unwrap_or_default());
    }
    changes
}

fn emit(on_change: &OnChange, changes: &[Change]) {
    if changes.is_empty() {
        return;
    }
    let dirs: Vec<String> = changes
        .iter()
        .map(|c| match c {
            Change::Upserted { key, .. } => key.clone(),
            Change::Deleted { key } => key.clone(),
        })
        .collect();
    on_change(dirs);
}

fn rel_of(p: &Path, root: &str) -> Option<String> {
    p.strip_prefix(root).ok().map(|r| r.to_string_lossy().replace('\\', "/"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reconcile::GenericMetaSource;

    #[test]
    fn is_watch_excluded_drops_index_churn_and_noise() {
        let cfg = WalkConfig::default();
        assert!(is_watch_excluded("_repo_index/INDEX.sqlite-wal", &cfg));
        assert!(is_watch_excluded(".repo_index/INDEX.json", &cfg));
        assert!(is_watch_excluded("a/._sidecar.csv", &cfg));
        assert!(is_watch_excluded("a/.DS_Store", &cfg));
        assert!(is_watch_excluded("node_modules/x/y.js", &cfg));
        assert!(!is_watch_excluded("a/b/real.csv", &cfg));
    }

    #[test]
    fn dirty_set_collapses_points_under_a_recursive_ancestor() {
        let now = Instant::now();
        let mut d = DirtySet::default();
        d.mark_point("a/b/c.csv".into(), now);
        d.mark_point("a/b/d.csv".into(), now);
        d.mark_recursive("a".into(), now); // subsumes the two points
        let (recs, pts) = d.drain();
        assert_eq!(recs, vec!["a".to_string()]);
        assert!(pts.is_empty(), "points under 'a' collapsed into the recursive rescan");
    }

    #[test]
    fn live_watcher_indexes_a_touched_file_within_two_seconds() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("_watcher_scratch");
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("_repo_index")).unwrap();
        let writer = Arc::new(
            IndexWriter::open(dir.to_str().unwrap(), dir.join("_repo_index/INDEX.sqlite").to_str().unwrap())
                .unwrap(),
        );
        let ctx = Arc::new(ReconcileCtx::new(dir.to_str().unwrap(), Arc::new(GenericMetaSource)));
        let handle =
            WatcherHandle::spawn(dir.to_str().unwrap(), writer.clone(), ctx, Box::new(|_| {})).unwrap();

        // give the watcher a beat to arm + finish the initial scan, then create a file
        std::thread::sleep(Duration::from_millis(200));
        std::fs::write(dir.join("live.csv"), b"hello").unwrap();

        // poll up to ~3s for the row to appear (debounce is ~300ms)
        let mut seen = false;
        for _ in 0..30 {
            std::thread::sleep(Duration::from_millis(100));
            let n: i64 = writer
                .with_conn(|c| {
                    c.query_row("SELECT COUNT(*) FROM entries WHERE path='live.csv'", [], |r| r.get(0))
                        .map_err(|e| e.to_string())
                })
                .unwrap();
            if n == 1 {
                seen = true;
                break;
            }
        }
        handle.stop();
        drop(writer);
        let _ = std::fs::remove_dir_all(&dir);
        assert!(seen, "the watcher must index a touched file within ~2s (⟳ automatic)");
    }
}
