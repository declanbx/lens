//! `reconcile` — the stat-is-truth reconciler (PHASE_0_1_SPEC.md §4.4, §4.9). An FSEvent is a HINT
//! that a path MIGHT have changed; the FILESYSTEM is the only authority. Never write from event
//! flags — `lstat` the path and believe only that. This is the ONE writer path shared by startup
//! reconcile, live events (§4), self-mutations (`apply_op`, §5), and crash recovery.
//!
//! Two entry points, both routing through the SAME `build_row`/`should_index`:
//!   * [`reconcile_paths`] — the stat authority for a SET of specific paths (point events). Idempotent.
//!   * [`reconcile_tree`]  — a full sync of a subtree (upsert present + delete vanished). The recovery
//!     primitive for startup / overflow / `need_rescan()` / unmount→remount.
//!
//! Heavy per-file metadata (h5py/pyarrow) comes from a pluggable [`MetaSource`] (the Python
//! `extract-batch` helper in production, §6; a stub in tests) — Rust owns only the cheap STAT fields
//! + the walk membership decision. The `should_index` / `ext_of` / `category_for` / `iso_mtime` ports
//! MUST stay byte-faithful to `walker.py`/`config.py`/`base.py` (§4.9 drift hazard); a parity gate
//! (§8 gate 7) locks them.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use rusqlite::Connection;
use serde_json::value::RawValue;

use crate::pathkey::{basename_of, norm_key, parent_of};
use crate::tree::{self, ManifestEntry, RowValues};
use crate::writer::IndexWriter;

/// A committed reconcile outcome (for the `index-changed` event + tests).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Change {
    Upserted { key: String, is_dir: bool },
    Deleted { key: String },
}

// ── walk policy (ported from config.py / walker.py — §4.9) ────────────────────────────────────────

/// The default directory names the walk prunes entirely (`config.py:40-45` `prune_dirs`). Exact
/// basename match, case-sensitive. Includes BOTH `_repo_index` (the OUT dir) and `.repo_index`.
const DEFAULT_PRUNE_DIRS: &[&str] = &[
    "_vendor",
    "__pycache__",
    ".pytest_cache",
    ".git",
    "site-packages",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ipynb_checkpoints",
    ".ruff_cache",
    ".eggs",
    "build",
    "dist",
    ".repo_index",
    "_repo_index",
    ".cache",
];

/// The compound extension keys the walker recognizes (the dotted keys of the default `ext_to_category`
/// / the extractor `REGISTRY`, `base.py:_compound_keys`). Only `csv.gz`/`tsv.gz` in the default config.
const COMPOUND_EXTS: &[&str] = &["csv.gz", "tsv.gz"];

/// Resolved walk configuration (the subset `should_index` + cheap fields need). Defaults mirror the
/// shipped `config.py` (`prune_dirs` above; `skip_prefixes=["._"]`; empty include/exclude globs;
/// `racy_window_seconds=2`).
#[derive(Clone)]
pub struct WalkConfig {
    pub prune_dirs: HashSet<String>,
    pub skip_prefixes: Vec<String>,
    pub exclude_globs: Vec<String>,
    pub include_globs: Vec<String>,
    pub racy_window_seconds: f64,
}

impl Default for WalkConfig {
    fn default() -> Self {
        WalkConfig {
            prune_dirs: DEFAULT_PRUNE_DIRS.iter().map(|s| s.to_string()).collect(),
            skip_prefixes: vec!["._".to_string()],
            exclude_globs: Vec::new(),
            include_globs: Vec::new(),
            racy_window_seconds: 2.0,
        }
    }
}

impl WalkConfig {
    /// Membership: should a file at root-relative POSIX `rel` be indexed? Mirrors the walk's
    /// combined gate: no path component is pruned or starts with a skip prefix (`._`), and `rel`
    /// passes the exclude-then-include glob test (exclude wins; empty include = keep all).
    pub fn should_index(&self, rel: &str) -> bool {
        if rel.is_empty() {
            return false;
        }
        let comps: Vec<&str> = rel.split('/').collect();
        let last = comps.len() - 1;
        for (i, comp) in comps.iter().enumerate() {
            // `._` skip-prefix applies to files AND dirs (walker parity).
            if self.skip_prefixes.iter().any(|p| comp.starts_with(p.as_str())) {
                return false;
            }
            // prune_dirs applies ONLY to ANCESTOR directory components — the Python walk prunes dir
            // names during DESCENT, never the file basename. A FILE literally named `build`/`dist`/
            // `venv`/… IS indexed by Python, so pruning it here would be a parity divergence.
            if i < last && self.prune_dirs.contains(*comp) {
                return false;
            }
        }
        if !self.exclude_globs.is_empty() && self.exclude_globs.iter().any(|g| glob_match(g, rel)) {
            return false; // exclude wins
        }
        self.include_globs.is_empty() || self.include_globs.iter().any(|g| glob_match(g, rel))
    }

    /// Should the walk descend into a directory named `name` (basename)? False for pruned dirs and
    /// `._`-prefixed names (the walker prunes these before any stat).
    fn allow_descend(&self, name: &str) -> bool {
        !self.prune_dirs.contains(name)
            && !self.skip_prefixes.iter().any(|p| name.starts_with(p.as_str()))
    }
}

/// fnmatch-style glob match (`*` matches ANY run incl. `/`, `?` matches one char). Character classes
/// `[...]` are NOT supported (the shipped config has EMPTY globs, so this is latent; a non-default
/// config using classes would need extending). Recursive; fine for short patterns/paths.
fn glob_match(pat: &str, s: &str) -> bool {
    fn m(p: &[u8], t: &[u8]) -> bool {
        match p.first() {
            None => t.is_empty(),
            Some(b'*') => m(&p[1..], t) || (!t.is_empty() && m(p, &t[1..])),
            Some(b'?') => !t.is_empty() && m(&p[1..], &t[1..]),
            Some(&c) => !t.is_empty() && t[0] == c && m(&p[1..], &t[1..]),
        }
    }
    m(pat.as_bytes(), s.as_bytes())
}

/// The compound-aware, lowercased extension KEY (`base.py:ext_of`). `foo`→`""`; `.gitignore`→`""`;
/// `data.csv.gz`→`csv.gz` (only if the last-two join is a known compound); else the last segment.
pub fn ext_of(name: &str) -> String {
    let parts: Vec<&str> = name.split('.').collect();
    if parts.len() == 1 {
        return String::new();
    }
    if name.starts_with('.') && parts.len() == 2 {
        return String::new();
    }
    if parts.len() >= 3 || (parts.len() == 2 && !name.starts_with('.')) {
        let compound = parts[parts.len() - 2..].join(".").to_lowercase();
        if COMPOUND_EXTS.contains(&compound.as_str()) {
            return compound;
        }
    }
    parts[parts.len() - 1].to_lowercase()
}

/// ext → category (`config.py:ext_to_category` + `category_for`). Unknown/empty ext → `"other"`.
/// NOTE `pdf` → `"figure_pdf"` (not `figure`), matching the shipped map exactly.
pub fn category_for(ext: &str) -> &'static str {
    match ext {
        "py" | "r" | "sh" | "cpp" | "c" => "code",
        "h5ad" | "h5" | "npy" | "npz" | "loom" => "data_matrix",
        "csv" | "tsv" | "csv.gz" | "tsv.gz" | "parquet" | "xlsx" => "data_table",
        "yaml" | "yml" | "toml" | "json" | "ini" => "config",
        "md" | "txt" | "rst" => "doc",
        "ipynb" => "notebook",
        "png" | "svg" | "jpg" | "jpeg" => "figure",
        "pdf" => "figure_pdf",
        "pkl" | "pt" | "pth" | "joblib" | "model" | "rds" | "onnx" => "model",
        "log" | "out" | "err" => "log",
        "gz" | "tgz" | "zip" | "tar" => "archive",
        _ => "other",
    }
}

/// Format a POSIX mtime (whole seconds, TRUNCATED toward epoch) as `%Y-%m-%dT%H:%M:%SZ` in UTC —
/// byte-identical to `walker.py:iso_mtime` (`.replace(microsecond=0)` + trailing `Z`, not `+00:00`).
pub fn iso_mtime(unix_secs: i64) -> String {
    let days = unix_secs.div_euclid(86_400);
    let sod = unix_secs.rem_euclid(86_400);
    let (y, mo, d) = civil_from_days(days);
    let (hh, mm, ss) = (sod / 3600, (sod % 3600) / 60, sod % 60);
    format!("{y:04}-{mo:02}-{d:02}T{hh:02}:{mm:02}:{ss:02}Z")
}

/// Howard Hinnant's days-from-civil inverse: days-since-1970-01-01 → (year, month, day).
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as i64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

// ── heavy-metadata source (Python helper in production; stub in tests) ────────────────────────────

/// The heavy per-file metadata for one path (from the Python `extract-batch` helper, §6). Rust owns
/// the cheap stat fields + ext/category/tags; this supplies extractor + meta (+ error), the
/// digest-critical fields (`content_digest` = path,size,extractor,meta — §6.1).
#[derive(Clone, Debug)]
pub struct Extracted {
    pub extractor: String,
    pub meta: String, // raw JSON, stored verbatim (§4.7)
    pub error: Option<String>,
}

impl Default for Extracted {
    fn default() -> Self {
        Extracted { extractor: "generic".into(), meta: "{}".into(), error: None }
    }
}

/// Supplies [`Extracted`] for a batch of absolute file paths, keyed back by the abs path. In
/// production this shells to `python -m repo_index extract-batch` (§6.8); in tests, a stub.
pub trait MetaSource: Send + Sync {
    fn extract(&self, root: &str, abs_paths: &[String]) -> Result<HashMap<String, Extracted>, String>;
}

/// A do-nothing meta source: every path resolves to the generic (`extractor="generic"`, `{}`) result
/// — used when heavy extraction is unavailable (and as the test default).
pub struct GenericMetaSource;
impl MetaSource for GenericMetaSource {
    fn extract(&self, _root: &str, abs: &[String]) -> Result<HashMap<String, Extracted>, String> {
        Ok(abs.iter().map(|p| (p.clone(), Extracted::default())).collect())
    }
}

/// The reconcile context: the project root, the walk policy, and the heavy-meta source.
pub struct ReconcileCtx {
    pub root: String,
    pub config: WalkConfig,
    pub meta: std::sync::Arc<dyn MetaSource>,
}

impl ReconcileCtx {
    pub fn new(root: impl Into<String>, meta: std::sync::Arc<dyn MetaSource>) -> Self {
        ReconcileCtx { root: root.into(), config: WalkConfig::default(), meta }
    }

    fn abs_of(&self, rel: &str) -> PathBuf {
        Path::new(&self.root).join(rel)
    }
    fn rel_of(&self, abs: &Path) -> Option<String> {
        abs.strip_prefix(&self.root)
            .ok()
            .map(|p| p.to_string_lossy().replace('\\', "/"))
    }
}

// ── snapshot (Phase A) ────────────────────────────────────────────────────────────────────────────

/// A prior row's fields the reuse gate + move-in detection need.
struct SnapRow {
    size_bytes: i64,
    mtime_iso: String,
    extractor: String,
    meta: String,
    has_error: bool,
}

/// Snapshot the subtree rooted at `dir_key` ('' = whole tree). Keyed by `path_key`.
fn snapshot_subtree(conn: &Connection, dir_key: &str) -> Result<HashMap<String, SnapRow>, String> {
    let (sql, params): (&str, Vec<rusqlite::types::Value>) = if dir_key.is_empty() {
        ("SELECT path_key,size_bytes,mtime_iso,extractor,meta,error FROM entries WHERE is_dir=0", vec![])
    } else {
        (
            "SELECT path_key,size_bytes,mtime_iso,extractor,meta,error FROM entries \
             WHERE is_dir=0 AND (path_key = ?1 OR (path_key >= ?1 || '/' AND path_key < ?1 || '0'))",
            vec![rusqlite::types::Value::Text(dir_key.to_string())],
        )
    };
    let mut st = conn.prepare(sql).map_err(|e| e.to_string())?;
    let rows = st
        .query_map(rusqlite::params_from_iter(params.iter()), |r| {
            Ok((
                r.get::<_, String>(0)?,
                SnapRow {
                    size_bytes: r.get::<_, Option<i64>>(1)?.unwrap_or(0),
                    mtime_iso: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    extractor: r.get::<_, Option<String>>(3)?.unwrap_or_default(),
                    meta: r.get::<_, Option<String>>(4)?.unwrap_or_else(|| "{}".into()),
                    has_error: r.get::<_, Option<String>>(5)?.is_some(),
                },
            ))
        })
        .map_err(|e| e.to_string())?;
    let mut map = HashMap::new();
    for r in rows {
        let (k, v) = r.map_err(|e| e.to_string())?;
        map.insert(k, v);
    }
    Ok(map)
}

// ── cheap stat-field derivation + build_row ───────────────────────────────────────────────────────

/// The cheap STAT fields Rust owns for one present path (never follows a symlink for size/mtime).
struct Stat {
    size_bytes: i64,
    mtime_iso: String,
    mtime_secs: i64,
    is_symlink: bool,
    is_dir: bool, // a REAL directory (symlink-to-dir is is_symlink=true, is_dir=false)
    symlink_target: Option<String>,
    symlink_ok: Option<bool>,
}

fn stat_of(abs: &Path) -> std::io::Result<Stat> {
    let md = std::fs::symlink_metadata(abs)?; // lstat — never follows
    let is_symlink = md.file_type().is_symlink();
    let is_dir = md.is_dir(); // false for a symlink (symlink_metadata doesn't follow)
    let mtime_secs = md
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let (symlink_target, symlink_ok) = if is_symlink {
        let target = std::fs::read_link(abs).ok().map(|p| p.to_string_lossy().into_owned());
        // exists() FOLLOWS the link: Some(true) resolves, Some(false) broken.
        let ok = Some(abs.exists());
        (target, ok)
    } else {
        (None, None)
    };
    Ok(Stat {
        size_bytes: md.len() as i64,
        mtime_iso: iso_mtime(mtime_secs),
        mtime_secs,
        is_symlink,
        is_dir,
        symlink_target,
        symlink_ok,
    })
}

/// Build a v2 FILE row from cheap stat fields + heavy [`Extracted`] meta, reusing the SAME derivation
/// (keys, n_obs/n_vars denorm) as the JSONL ingest (via `tree::RowValues::from_entry`).
fn build_row(rel: &str, st: &Stat, ex: &Extracted, now_iso: &str) -> Result<RowValues, String> {
    let ext = ext_of(basename_of(rel));
    let category = category_for(&ext).to_string();
    let meta_raw = RawValue::from_string(ex.meta.clone())
        .map_err(|e| format!("bad meta json for {rel}: {e}"))?;
    let entry = ManifestEntry {
        path: rel.to_string(),
        category,
        ext,
        size_bytes: st.size_bytes,
        mtime_iso: st.mtime_iso.clone(),
        is_symlink: st.is_symlink,
        symlink_target: st.symlink_target.clone(),
        symlink_ok: st.symlink_ok,
        extractor: ex.extractor.clone(),
        tags: Vec::new(), // shipped config has zero ontology tags → always []
        meta: meta_raw,
        error: ex.error.clone(),
    };
    Ok(RowValues::from_entry(&entry, now_iso))
}

// ── the write batch (Phase C helpers) ─────────────────────────────────────────────────────────────

/// Upsert missing ancestor directory rows for `rel` top-down, so a leaf's `parent_key` never dangles
/// (§4.4). Idempotent. Adds every ensured dir key to `seen` so the subtree sweep won't delete them.
fn ensure_dir_chain(
    up: &mut rusqlite::Statement<'_>,
    rel: &str,
    now_iso: &str,
    ensured: &mut HashSet<String>,
    seen: &mut Option<&mut HashSet<String>>,
) -> Result<(), String> {
    // Collect ancestors bottom-up, then upsert top-down (parent before child).
    let mut chain: Vec<&str> = Vec::new();
    let mut anc = parent_of(rel);
    while !anc.is_empty() {
        chain.push(anc);
        anc = parent_of(anc);
    }
    for dir in chain.into_iter().rev() {
        let dk = norm_key(dir);
        if let Some(s) = seen.as_deref_mut() {
            s.insert(dk.clone());
        }
        if ensured.insert(dk) {
            RowValues::dir(dir, now_iso).bind_exec(up)?;
        }
    }
    Ok(())
}

/// The subtree-range DELETE on an absent path (§4.4): removes the key AND every descendant at any
/// depth via the binary-collated `path_key` range (prefix-safe for any character — no GLOB metachar
/// escaping needed; `'/'`(0x2F) successor byte is `'0'`(0x30)). Harmless for a file (matches nothing
/// under it), correct for a directory.
fn delete_subtree(conn: &Connection, path_key: &str) -> Result<usize, String> {
    conn.execute(
        "DELETE FROM entries WHERE path_key = ?1 OR (path_key >= ?1 || '/' AND path_key < ?1 || '0')",
        rusqlite::params![path_key],
    )
    .map_err(|e| format!("delete_subtree {path_key}: {e}"))
}

/// Bump the persisted `index_generation` (the monotonic freshness counter, §4.5) and stamp both
/// `last_index_time` (ISO) and `last_index_epoch` (unix secs — the reference for the NEXT tree
/// pass's racy gate) inside the same write transaction.
fn bump_generation(conn: &Connection, now_iso: &str, now_epoch: i64) -> Result<(), String> {
    conn.execute(
        "UPDATE schema_meta SET value = CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='index_generation'",
        [],
    )
    .map_err(|e| e.to_string())?;
    let mut st = conn
        .prepare("INSERT INTO schema_meta(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        .map_err(|e| e.to_string())?;
    st.execute(rusqlite::params!["last_index_time", now_iso]).map_err(|e| e.to_string())?;
    st.execute(rusqlite::params!["last_index_epoch", now_epoch.to_string()]).map_err(|e| e.to_string())?;
    Ok(())
}

// ── reconcile_paths (point events) ────────────────────────────────────────────────────────────────

/// Stat-is-truth reconcile of a SET of specific paths (point events, `apply_op` write-through, drain).
/// Idempotent, safe to call twice. Present files/symlinks → upsert; absent → subtree-range delete;
/// a NEW present directory → promoted to a [`reconcile_tree`] of its subtree (its pre-existing
/// children may have been coalesced into the parent event, §4.4). Point events ALWAYS re-extract
/// (heavy extraction is sub-second; avoids the ±window same-tick miss).
pub fn reconcile_paths(
    writer: &IndexWriter,
    ctx: &ReconcileCtx,
    abs_paths: &[PathBuf],
) -> Result<Vec<Change>, String> {
    let (now_iso, now_epoch) = now_stamp();
    // Phase A: snapshot the affected keys (to detect a directory move-in).
    let keys: Vec<String> = abs_paths
        .iter()
        .filter_map(|p| ctx.rel_of(p))
        .map(|r| norm_key(&r))
        .collect();
    let known: HashSet<String> =
        writer.with_conn(|c| Ok(known_keys(c, &keys)))?;

    // Phase B: stat + plan (NO LOCK). Collect file rows to upsert, keys to delete, dirs to recurse.
    let mut to_extract: Vec<(String, String)> = Vec::new(); // (rel, abs) regular files needing meta
    let mut deletes: Vec<String> = Vec::new();
    let mut dir_recurse: Vec<String> = Vec::new();
    let mut file_stats: HashMap<String, Stat> = HashMap::new(); // rel -> stat
    for abs in abs_paths {
        let rel = match ctx.rel_of(abs) {
            Some(r) if !r.is_empty() => r,
            _ => continue,
        };
        match stat_of(abs) {
            Err(e) if is_absent(&e) => deletes.push(norm_key(&rel)),
            Err(_) => { /* transient error → keep prior row */ }
            Ok(st) => {
                if !ctx.config.should_index(&rel) {
                    deletes.push(norm_key(&rel)); // now-excluded (pruned ancestor / ._ / glob)
                } else if st.is_dir {
                    // For a real DIRECTORY, additionally apply the DESCENT gate to its own basename:
                    // `should_index` deliberately does NOT prune a basename (a FILE named `build` is
                    // valid), but a real dir named `node_modules`/`.git`/… must NOT be recursed into.
                    if !ctx.config.allow_descend(basename_of(&rel)) {
                        deletes.push(norm_key(&rel));
                    } else if !known.contains(&norm_key(&rel)) {
                        // A NEWLY-appeared directory (no prior row) → promote to a subtree walk: its
                        // pre-existing children may have been coalesced into this parent event (§4.4).
                        // A known dir's own event is a no-op — children get their own file events.
                        dir_recurse.push(rel);
                    }
                } else {
                    if !st.is_symlink {
                        to_extract.push((rel.clone(), abs.to_string_lossy().into_owned()));
                    }
                    file_stats.insert(rel, st);
                }
            }
        }
    }

    // Extract heavy meta for regular files (batched, NO LOCK). Symlinks are never extracted.
    let extracted = extract_batch(ctx, &to_extract)?;

    // Phase C: write batch (writer lock, one BEGIN IMMEDIATE).
    let mut changes = Vec::new();
    let dels = deletes.clone();
    writer.with_conn(|c| {
        tree::immediate_txn(c, |c| {
            let mut up = c.prepare(tree::UPSERT_SQL).map_err(|e| e.to_string())?;
            let mut ensured = HashSet::new();
            for (rel, st) in &file_stats {
                let ex = if st.is_symlink {
                    Extracted::default()
                } else {
                    extracted.get(rel).cloned().unwrap_or_default()
                };
                ensure_dir_chain(&mut up, rel, &now_iso, &mut ensured, &mut None)?;
                let rv = build_row(rel, st, &ex, &now_iso)?;
                changes.push(Change::Upserted { key: rv.path_key().to_string(), is_dir: false });
                rv.bind_exec(&mut up)?;
            }
            drop(up);
            for k in &dels {
                let n = delete_subtree(c, k)?;
                if n > 0 {
                    changes.push(Change::Deleted { key: k.clone() });
                }
            }
            bump_generation(c, &now_iso, now_epoch)?;
            Ok(())
        })
    })?;

    // A newly-appeared directory → full subtree sync (idempotent).
    for rel in dir_recurse {
        changes.extend(reconcile_tree(writer, ctx, &rel)?);
    }
    Ok(changes)
}

/// Which of `keys` currently exist as rows (for directory move-in detection).
fn known_keys(conn: &Connection, keys: &[String]) -> HashSet<String> {
    let mut out = HashSet::new();
    let Ok(mut st) = conn.prepare("SELECT 1 FROM entries WHERE path_key = ?1") else {
        return out;
    };
    for k in keys {
        if st.exists(rusqlite::params![k]).unwrap_or(false) {
            out.insert(k.clone());
        }
    }
    out
}

// ── reconcile_tree (subtree full sync) ────────────────────────────────────────────────────────────

/// Full sync of a subtree (`rel_dir` = "" → whole tree): upsert every present file + its ancestor dir
/// chain, then DELETE every db row under the subtree not seen this pass (the recovery primitive for
/// startup / overflow / need_rescan / remount). Applies the racy-window reuse gate (mirrors
/// `walker.py`) so bulk re-stat over ~38k unchanged files doesn't re-extract everything.
pub fn reconcile_tree(
    writer: &IndexWriter,
    ctx: &ReconcileCtx,
    rel_dir: &str,
) -> Result<Vec<Change>, String> {
    let (now_iso, now_epoch) = now_stamp();
    let dir_key = norm_key(rel_dir);

    // Walk the subtree (NO LOCK): every present file leaf (regular / symlink / broken symlink).
    let mut files: Vec<(String, Stat)> = Vec::new();
    walk_subtree(ctx, rel_dir, &mut files)?;

    // Phase A: snapshot the subtree for the reuse gate + the racy reference time.
    let (snap, ref_epoch) = writer.with_conn(|c| {
        Ok((snapshot_subtree(c, &dir_key)?, last_index_epoch(c)))
    })?;

    // Phase B: decide reuse vs extract (NO LOCK); batch-extract the non-reusable regular files.
    let mut to_extract: Vec<(String, String)> = Vec::new();
    let mut reused: HashMap<String, Extracted> = HashMap::new();
    for (rel, st) in &files {
        if st.is_symlink {
            continue; // symlinks are generic, never extracted
        }
        let pk = norm_key(rel);
        if let Some(prior) = snap.get(&pk) {
            if !is_racy(st.mtime_secs, ref_epoch, ctx.config.racy_window_seconds)
                && prior.size_bytes == st.size_bytes
                && prior.mtime_iso == st.mtime_iso
                && !prior.extractor.is_empty()
                && !prior.has_error
            {
                reused.insert(
                    rel.clone(),
                    Extracted { extractor: prior.extractor.clone(), meta: prior.meta.clone(), error: None },
                );
                continue;
            }
        }
        to_extract.push((rel.clone(), ctx.abs_of(rel).to_string_lossy().into_owned()));
    }
    let extracted = extract_batch(ctx, &to_extract)?;

    // Phase C: one BEGIN IMMEDIATE — upsert every present file + dir chain into `seen`, then sweep.
    let mut changes = Vec::new();
    writer.with_conn(|c| {
        tree::immediate_txn(c, |c| {
            c.execute_batch("CREATE TEMP TABLE IF NOT EXISTS seen(k TEXT PRIMARY KEY); DELETE FROM seen;")
                .map_err(|e| e.to_string())?;
            let mut seen_set: HashSet<String> = HashSet::new();
            {
                let mut up = c.prepare(tree::UPSERT_SQL).map_err(|e| e.to_string())?;
                let mut ensured = HashSet::new();
                // Ensure + KEEP the subtree-root dir row (and its ancestors) so reconcile_tree of a
                // specific dir never sweeps its own root, and an EMPTY dir (a live `mkdir`, §5.6)
                // still gets a visible is_dir=1 row even though it has no files to derive it from.
                // GUARDED by is_dir() on disk: an ABSENT rel_dir (a renamed-away / deleted subtree)
                // must NOT be recreated — it falls through to the sweep, which removes it + descendants.
                if !rel_dir.is_empty() && Path::new(&ctx.root).join(rel_dir).is_dir() {
                    let mut chain: Vec<&str> = Vec::new();
                    let mut d = rel_dir;
                    while !d.is_empty() {
                        chain.push(d);
                        d = parent_of(d);
                    }
                    for dir in chain.into_iter().rev() {
                        let dk = norm_key(dir);
                        seen_set.insert(dk.clone());
                        if ensured.insert(dk) {
                            RowValues::dir(dir, &now_iso).bind_exec(&mut up)?;
                        }
                    }
                }
                for (rel, st) in &files {
                    let ex = if st.is_symlink {
                        Extracted::default()
                    } else if let Some(r) = reused.get(rel) {
                        r.clone()
                    } else {
                        extracted.get(rel).cloned().unwrap_or_default()
                    };
                    let mut seen_ref = Some(&mut seen_set);
                    ensure_dir_chain(&mut up, rel, &now_iso, &mut ensured, &mut seen_ref)?;
                    let rv = build_row(rel, st, &ex, &now_iso)?;
                    seen_set.insert(rv.path_key().to_string());
                    changes.push(Change::Upserted { key: rv.path_key().to_string(), is_dir: false });
                    rv.bind_exec(&mut up)?;
                }
            }
            // Load seen keys into the temp table (batched INSERT — avoids the 999-var IN() limit).
            {
                let mut ins = c.prepare("INSERT OR IGNORE INTO seen(k) VALUES (?1)").map_err(|e| e.to_string())?;
                for k in &seen_set {
                    ins.execute(rusqlite::params![k]).map_err(|e| e.to_string())?;
                }
            }
            // Vanished-row sweep within the subtree range.
            let deleted = c
                .execute(
                    "DELETE FROM entries
                     WHERE ( ?1 = '' OR path_key = ?1 OR (path_key >= ?1 || '/' AND path_key < ?1 || '0') )
                       AND path_key NOT IN (SELECT k FROM seen)",
                    rusqlite::params![dir_key],
                )
                .map_err(|e| format!("sweep: {e}"))?;
            if deleted > 0 {
                changes.push(Change::Deleted { key: format!("<swept {deleted} under '{dir_key}'>") });
            }
            c.execute_batch("DROP TABLE seen;").ok();
            bump_generation(c, &now_iso, now_epoch)?;
            Ok(())
        })
    })?;
    Ok(changes)
}

/// Recursively walk `root/rel_dir`, appending every present FILE leaf (regular / symlink / broken)
/// that passes the walk policy. Real directories are descended (not emitted); symlink-to-dir is
/// emitted as a leaf and NOT descended (record-not-traverse). Pruned / `._` / excluded are skipped.
fn walk_subtree(ctx: &ReconcileCtx, rel_dir: &str, out: &mut Vec<(String, Stat)>) -> Result<(), String> {
    let abs_dir = if rel_dir.is_empty() { PathBuf::from(&ctx.root) } else { ctx.abs_of(rel_dir) };
    let entries = match std::fs::read_dir(&abs_dir) {
        Ok(e) => e,
        // A vanished non-root SUBTREE (ENOENT/ENOTDIR) is a GENUINE deletion under stat-is-truth →
        // return an empty listing so the caller's sweep removes its rows (a real `rm -rf`). Any OTHER
        // error — permission denied, and crucially the EIO/ENXIO a USB/Thunderbolt DISCONNECT of this
        // external mount produces (ErrorKind::Other, NOT is_absent) — ABORTs, so a transient read
        // failure never makes the sweep see an empty tree and wipe the index. The ROOT (rel_dir="")
        // aborts on ANY error (incl. ENOENT), since an empty root there would sweep EVERYTHING.
        // DELIBERATE, ACCEPTED edge: if some driver ever returned ENOENT (not EIO) for a *transient*
        // nested unreachability, that subtree would be wrongly swept then re-added on the next rescan
        // (self-healing). Treating nested ENOENT as an abort instead would REGRESS real deletions
        // (they'd never be swept → permanent stale rows) — a worse, definite bug for an unconfirmed one.
        Err(e) if !rel_dir.is_empty() && is_absent(&e) => return Ok(()),
        Err(e) => return Err(format!("walk_subtree: read_dir {abs_dir:?} failed: {e}")),
    };
    for ent in entries {
        // Do NOT `.flatten()` a per-entry Err away: a mid-iteration read failure (I/O error) would
        // silently DROP entries, so the sweep would delete their still-present rows (a partial
        // index wipe — the same data-loss class as the read_dir-open blocker). Abort the walk
        // instead → reconcile_tree returns Err → the sweep never runs.
        let ent = ent.map_err(|e| format!("walk_subtree: read entry in {abs_dir:?} failed: {e}"))?;
        let name = ent.file_name().to_string_lossy().into_owned();
        // MANDATORY ._ skip BEFORE any stat — files AND dirs (walker parity).
        if ctx.config.skip_prefixes.iter().any(|p| name.starts_with(p.as_str())) {
            continue;
        }
        let rel = if rel_dir.is_empty() { name.clone() } else { format!("{rel_dir}/{name}") };
        let abs = ent.path();
        let st = match stat_of(&abs) {
            Ok(s) => s,
            // Genuinely vanished between readdir and lstat (a race) → skip; it's gone, so letting the
            // sweep drop any stale row is CORRECT.
            Err(e) if is_absent(&e) => continue,
            // A NON-absent lstat error (EIO / EACCES on the parent / drive hiccup) does NOT mean the
            // entry is gone — silently skipping it would drop a PRESENT file/subtree from `seen` and
            // the sweep would delete it (a partial wipe, the same data-loss class as the read_dir
            // blocker). ABORT the walk instead → reconcile_tree returns Err → the sweep never runs.
            Err(e) => return Err(format!("walk_subtree: lstat {abs:?} failed: {e}")),
        };
        if st.is_dir {
            // real directory: prune / exclude gate, else descend (not emitted).
            if !ctx.config.allow_descend(&name) {
                continue;
            }
            if !ctx.config.exclude_globs.is_empty() && ctx.config.exclude_globs.iter().any(|g| glob_match(g, &rel)) {
                continue;
            }
            walk_subtree(ctx, &rel, out)?;
        } else {
            // file / symlink-to-file / symlink-to-dir / broken symlink → emit iff membership passes.
            if ctx.config.should_index(&rel) {
                out.push((rel, st));
            }
        }
    }
    Ok(())
}

// ── small helpers ─────────────────────────────────────────────────────────────────────────────────

fn extract_batch(ctx: &ReconcileCtx, work: &[(String, String)]) -> Result<HashMap<String, Extracted>, String> {
    if work.is_empty() {
        return Ok(HashMap::new());
    }
    let abs: Vec<String> = work.iter().map(|(_, a)| a.clone()).collect();
    let by_abs = ctx.meta.extract(&ctx.root, &abs)?;
    // Re-key by rel (build_row keys by rel).
    let mut out = HashMap::new();
    for (rel, a) in work {
        if let Some(e) = by_abs.get(a) {
            out.insert(rel.clone(), e.clone());
        }
    }
    Ok(out)
}

fn is_absent(e: &std::io::Error) -> bool {
    use std::io::ErrorKind;
    matches!(e.kind(), ErrorKind::NotFound | ErrorKind::NotADirectory)
}

/// The racy check (`walker.py:_is_racy`): a file whose mtime is within `window` seconds of the last
/// index time is treated as possibly-changed (re-extract), even on a (size,mtime) match.
fn is_racy(file_mtime_secs: i64, reference_epoch: Option<i64>, window: f64) -> bool {
    match reference_epoch {
        None => false,
        Some(reference) => (file_mtime_secs as f64) > (reference as f64) - window,
    }
}

fn last_index_epoch(conn: &Connection) -> Option<i64> {
    conn.query_row("SELECT value FROM schema_meta WHERE key='last_index_epoch'", [], |r| {
        r.get::<_, String>(0)
    })
    .ok()
    .and_then(|s| s.parse().ok())
}

/// Current UTC time as (ISO string, unix epoch secs). The epoch is persisted by `bump_generation`
/// as `last_index_epoch` — the reference for the next tree pass's racy gate.
fn now_stamp() -> (String, i64) {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    (iso_mtime(secs), secs)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::sync::Arc;

    fn scratch(tag: &str) -> PathBuf {
        let d = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("_reconcile_scratch_{tag}"));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn writer_at(dir: &Path) -> IndexWriter {
        let idx = dir.join("_repo_index");
        std::fs::create_dir_all(&idx).unwrap();
        IndexWriter::open(dir.to_str().unwrap(), idx.join("INDEX.sqlite").to_str().unwrap()).unwrap()
    }

    fn write_file(dir: &Path, rel: &str, content: &[u8]) {
        let p = dir.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::File::create(&p).unwrap().write_all(content).unwrap();
    }

    /// A stub meta source that reports n_obs for a specific basename (to exercise the denorm path).
    struct StubMeta;
    impl MetaSource for StubMeta {
        fn extract(&self, _root: &str, abs: &[String]) -> Result<HashMap<String, Extracted>, String> {
            Ok(abs
                .iter()
                .map(|a| {
                    let ex = if a.ends_with("atlas.h5ad") {
                        Extracted { extractor: "h5ad".into(), meta: r#"{"n_obs":2500,"n_vars":5}"#.into(), error: None }
                    } else {
                        Extracted::default()
                    };
                    (a.clone(), ex)
                })
                .collect())
        }
    }

    fn ctx_at(dir: &Path) -> ReconcileCtx {
        ReconcileCtx::new(dir.to_str().unwrap(), Arc::new(StubMeta))
    }

    fn count_files(w: &IndexWriter) -> i64 {
        w.with_conn(|c| c.query_row("SELECT COUNT(*) FROM entries WHERE is_dir=0", [], |r| r.get(0)).map_err(|e| e.to_string())).unwrap()
    }

    #[test]
    fn ext_and_category_parity() {
        assert_eq!(ext_of("a.csv"), "csv");
        assert_eq!(ext_of("data.csv.gz"), "csv.gz");
        assert_eq!(ext_of("weird.tar.gz"), "gz"); // tar.gz not a compound key → bare gz
        assert_eq!(ext_of(".gitignore"), "");
        assert_eq!(ext_of("Makefile"), "");
        assert_eq!(ext_of("X.H5AD"), "h5ad");
        assert_eq!(category_for("pdf"), "figure_pdf");
        assert_eq!(category_for("h5ad"), "data_matrix");
        assert_eq!(category_for("zzz"), "other");
    }

    #[test]
    fn iso_mtime_truncates_and_zulu() {
        // 2026-04-20T07:21:29Z == 1776669689
        assert_eq!(iso_mtime(1_776_669_689), "2026-04-20T07:21:29Z");
        assert_eq!(iso_mtime(0), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn should_index_prunes_and_skips() {
        let cfg = WalkConfig::default();
        assert!(cfg.should_index("a/b/atlas.h5ad"));
        assert!(!cfg.should_index("node_modules/pkg/x.js"));
        assert!(!cfg.should_index("_repo_index/INDEX.sqlite"));
        assert!(!cfg.should_index(".git/config"));
        assert!(!cfg.should_index("a/._hidden.csv")); // ._ sidecar
        assert!(!cfg.should_index("a/.git/x")); // pruned ANCESTOR
        // A FILE whose basename equals a prune-dir name IS indexed (Python prunes dir names during
        // descent, never the file basename) — the parity divergence the review caught.
        assert!(cfg.should_index("scripts/build"));
        assert!(cfg.should_index("dist"));
        assert!(cfg.should_index("a/node_modules")); // a FILE named node_modules under dir `a`
    }

    #[test]
    fn reconcile_tree_creates_files_dirs_and_sweeps_deletions() {
        let dir = scratch("tree");
        let w = writer_at(&dir);
        let ctx = ctx_at(&dir);
        write_file(&dir, "a/b/atlas.h5ad", b"x");
        write_file(&dir, "a/readme.md", b"# hi");
        write_file(&dir, "top.csv", b"c");
        // an AppleDouble sidecar + a pruned dir must NOT be indexed
        write_file(&dir, "a/._atlas.h5ad", b"junk");
        write_file(&dir, "node_modules/pkg/x.js", b"j");

        reconcile_tree(&w, &ctx, "").unwrap();
        assert_eq!(count_files(&w), 3, "3 real files; ._ sidecar + node_modules skipped");

        // dir rows synthesized; n_obs denormalized from the stub extractor
        let (ndir, nobs): (i64, Option<i64>) = w
            .with_conn(|c| {
                let d: i64 = c.query_row("SELECT COUNT(*) FROM entries WHERE is_dir=1", [], |r| r.get(0)).unwrap();
                let n: Option<i64> = c.query_row("SELECT n_obs FROM entries WHERE path='a/b/atlas.h5ad'", [], |r| r.get(0)).unwrap();
                Ok((d, n))
            })
            .unwrap();
        assert_eq!(ndir, 2, "a and a/b");
        assert_eq!(nobs, Some(2500));

        // delete a file on disk → a re-tree sweeps the row AND its now-empty dir
        std::fs::remove_file(dir.join("a/b/atlas.h5ad")).unwrap();
        reconcile_tree(&w, &ctx, "").unwrap();
        assert_eq!(count_files(&w), 2);
        let gone: i64 = w.with_conn(|c| c.query_row("SELECT COUNT(*) FROM entries WHERE path='a/b'", [], |r| r.get(0)).map_err(|e| e.to_string())).unwrap();
        assert_eq!(gone, 0, "empty dir a/b swept");

        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn reconcile_tree_aborts_on_unreadable_root_instead_of_wiping_the_index() {
        // Regression for the blocker: a transient/unreadable reconcile ROOT (unmount, EIO, permission)
        // must ABORT reconcile_tree — never let an empty walk make the sweep DELETE the whole index.
        let dir = scratch("wipeguard");
        let w = writer_at(&dir);
        let ctx = ctx_at(&dir);
        write_file(&dir, "a/x.csv", b"x");
        write_file(&dir, "b/y.csv", b"y");
        reconcile_tree(&w, &ctx, "").unwrap();
        assert_eq!(count_files(&w), 2);

        // A ctx whose root does NOT exist (simulating an unmounted/unreadable drive), sharing the
        // SAME writer/DB. If the guard were missing, the empty walk + `?1=''` sweep would wipe all rows.
        let bad_ctx = ReconcileCtx::new(
            dir.join("__unmounted__").to_str().unwrap(),
            Arc::new(GenericMetaSource),
        );
        let res = reconcile_tree(&w, &bad_ctx, "");
        assert!(res.is_err(), "an unreadable root must ABORT the reconcile");
        assert_eq!(count_files(&w), 2, "index PRESERVED — the sweep never ran");

        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn reconcile_paths_create_modify_delete_and_subtree_delete() {
        let dir = scratch("paths");
        let w = writer_at(&dir);
        let ctx = ctx_at(&dir);

        // create via point events
        write_file(&dir, "d/x.csv", b"aaaa");
        write_file(&dir, "d/e/y.csv", b"bbbb");
        reconcile_paths(&w, &ctx, &[dir.join("d/x.csv"), dir.join("d/e/y.csv")]).unwrap();
        assert_eq!(count_files(&w), 2);
        let sz: i64 = w.with_conn(|c| c.query_row("SELECT size_bytes FROM entries WHERE path='d/x.csv'", [], |r| r.get(0)).map_err(|e| e.to_string())).unwrap();
        assert_eq!(sz, 4);

        // modify → size updates
        write_file(&dir, "d/x.csv", b"aaaaaaaa");
        reconcile_paths(&w, &ctx, &[dir.join("d/x.csv")]).unwrap();
        let sz2: i64 = w.with_conn(|c| c.query_row("SELECT size_bytes FROM entries WHERE path='d/x.csv'", [], |r| r.get(0)).map_err(|e| e.to_string())).unwrap();
        assert_eq!(sz2, 8);

        // delete a file → subtree-range delete removes just that row
        std::fs::remove_file(dir.join("d/x.csv")).unwrap();
        reconcile_paths(&w, &ctx, &[dir.join("d/x.csv")]).unwrap();
        assert_eq!(count_files(&w), 1);

        // rm -rf the 'd/e' subtree, fire a point event on the DIR → all descendants swept
        std::fs::remove_dir_all(dir.join("d/e")).unwrap();
        reconcile_paths(&w, &ctx, &[dir.join("d/e")]).unwrap();
        let under_e: i64 = w.with_conn(|c| c.query_row("SELECT COUNT(*) FROM entries WHERE path LIKE 'd/e%'", [], |r| r.get(0)).map_err(|e| e.to_string())).unwrap();
        assert_eq!(under_e, 0, "subtree-range delete removed d/e and descendants");

        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn reuse_gate_skips_reextraction_of_unchanged_files() {
        // A counting meta source proves the reuse gate avoids a second extraction.
        struct Counter(std::sync::atomic::AtomicUsize);
        impl MetaSource for Counter {
            fn extract(&self, _r: &str, abs: &[String]) -> Result<HashMap<String, Extracted>, String> {
                self.0.fetch_add(abs.len(), std::sync::atomic::Ordering::SeqCst);
                Ok(abs.iter().map(|a| (a.clone(), Extracted::default())).collect())
            }
        }
        let dir = scratch("reuse");
        let w = writer_at(&dir);
        let counter = Arc::new(Counter(std::sync::atomic::AtomicUsize::new(0)));
        let mut ctx = ReconcileCtx::new(dir.to_str().unwrap(), counter.clone());
        // racy_window=0 makes reuse deterministic: files are written BEFORE the first reconcile, so
        // their mtime <= the last_index_epoch that pass persists → never racy on the second pass.
        ctx.config.racy_window_seconds = 0.0;

        write_file(&dir, "data/a.csv", b"hello");
        write_file(&dir, "data/b.csv", b"world");

        reconcile_tree(&w, &ctx, "").unwrap();
        let after_first = counter.0.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(after_first, 2, "both files extracted on first pass");

        // second pass, files unchanged → reuse gate skips re-extraction
        reconcile_tree(&w, &ctx, "").unwrap();
        let after_second = counter.0.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(after_second, 2, "no re-extraction of unchanged files (reuse gate)");

        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── §8 gate 7: Rust reconcile ↔ Python cold-manifest parity, under a NON-default config ─────────

    fn python_ok() -> bool {
        let python = std::env::var("LENS_PYTHON")
            .unwrap_or_else(|_| crate::db::PYTHON_BIN_DEFAULT.to_string());
        std::process::Command::new(&python)
            .args(["-c", "import repo_index"])
            .env("PYTHONPATH", crate::db::REPO_INDEX_PKG_PARENT)
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    /// Run the REAL Python `walk` over `root` under `index_columns=False` (the `--no-columns`
    /// non-default config, §6.5) and return path → (size_bytes, extractor, parsed meta).
    fn python_manifest(root: &str) -> HashMap<String, (i64, String, serde_json::Value)> {
        let python =
            std::env::var("LENS_PYTHON").unwrap_or_else(|_| crate::db::PYTHON_BIN_DEFAULT.to_string());
        let script = r#"
import json, sys
from pathlib import Path
from repo_index.config import load_config
from repo_index.walker import walk
root = sys.argv[1]
cfg = load_config(overrides={"index_columns": False})
out = []
for e in walk(Path(root), cfg):
    out.append({"path": e.get("path"), "size_bytes": e.get("size_bytes"),
                "extractor": e.get("extractor"), "meta": e.get("meta")})
json.dump(out, sys.stdout)
"#;
        let output = std::process::Command::new(&python)
            .args(["-c", script, root])
            .current_dir(root)
            .env("PYTHONPATH", crate::db::REPO_INDEX_PKG_PARENT)
            .output()
            .expect("spawn python walk");
        assert!(
            output.status.success(),
            "python walk failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let entries: Vec<serde_json::Value> =
            serde_json::from_slice(&output.stdout).expect("parse python manifest");
        let mut map = HashMap::new();
        for e in entries {
            let path = e["path"].as_str().unwrap().to_string();
            let size = e["size_bytes"].as_i64().unwrap_or(0);
            let extractor = e["extractor"].as_str().unwrap_or("").to_string();
            map.insert(path, (size, extractor, e["meta"].clone()));
        }
        map
    }

    #[test]
    fn rust_reconcile_matches_python_cold_manifest_nondefault_config() {
        if !python_ok() {
            eprintln!("SKIP: python cannot import repo_index");
            return;
        }
        let dir = scratch("parity");
        // A fixture exercising membership edges + several extractor types.
        write_file(&dir, "a/data.csv", b"col1,col2,col3\n1,2,3\n4,5,6\n");
        write_file(&dir, "a/b/nested.tsv", b"x\ty\n1\t2\n");
        write_file(&dir, "script.py", b"import os\ndef main():\n    pass\n");
        write_file(&dir, "README.md", b"# Title\n\nbody\n");
        write_file(&dir, "conf.json", b"{\"k\": 1}\n");
        write_file(&dir, "a/._sidecar.csv", b"junk"); // ._ → excluded by BOTH
        write_file(&dir, "node_modules/pkg/x.js", b"j"); // pruned by BOTH

        let py = python_manifest(dir.to_str().unwrap());

        // Rust side: reconcile with the SAME --no-columns config gate.
        let w = writer_at(&dir);
        let ctx = ReconcileCtx::new(
            dir.to_str().unwrap(),
            Arc::new(crate::helper::PyMetaSource { cfg_flags: vec!["--no-columns".into()] }),
        );
        reconcile_tree(&w, &ctx, "").unwrap();
        let rust: HashMap<String, (i64, String, serde_json::Value)> = w
            .with_conn(|c| {
                let mut st = c.prepare("SELECT path,size_bytes,extractor,meta FROM entries WHERE is_dir=0").unwrap();
                let rows = st
                    .query_map([], |r| {
                        let meta: String = r.get::<_, Option<String>>(3)?.unwrap_or_else(|| "{}".into());
                        Ok((
                            r.get::<_, String>(0)?,
                            (
                                r.get::<_, Option<i64>>(1)?.unwrap_or(0),
                                r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                                serde_json::from_str(&meta).unwrap_or(serde_json::Value::Null),
                            ),
                        ))
                    })
                    .unwrap()
                    .map(|r| r.unwrap())
                    .collect();
                Ok(rows)
            })
            .unwrap();

        // (1) same indexed file SET (membership parity: ._ + node_modules excluded by both)
        let py_paths: std::collections::BTreeSet<_> = py.keys().cloned().collect();
        let rust_paths: std::collections::BTreeSet<_> = rust.keys().cloned().collect();
        assert_eq!(rust_paths, py_paths, "indexed file sets diverge (should_index parity)");
        assert!(!rust_paths.contains("a/._sidecar.csv"));
        assert!(!rust_paths.iter().any(|p| p.starts_with("node_modules/")));

        // (2) per-file (size, extractor, parsed meta) parity — the content_digest fields (§6.1)
        for (path, (psize, pextr, pmeta)) in &py {
            let (rsize, rextr, rmeta) = rust.get(path).unwrap_or_else(|| panic!("rust missing {path}"));
            assert_eq!(rsize, psize, "size mismatch for {path}");
            assert_eq!(rextr, pextr, "extractor mismatch for {path}");
            assert_eq!(rmeta, pmeta, "parsed meta mismatch for {path}\n rust={rmeta}\n  py={pmeta}");
        }

        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
