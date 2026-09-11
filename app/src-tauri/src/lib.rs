//! Lens — a glass, mouse-first Finder over the repo_index `INDEX.sqlite`.
//!
//! This file is the THIN INTEGRATION LAYER (the "Integrate / build-green" stage). It owns ONLY the
//! Tauri plumbing; all substantive logic lives in two modules:
//!   * [`db`]      — the read-only data layer (Row mapping, the search-grammar parser → FTS5, the
//!                   queries, the crosslinks adjacency, the path/clipboard helpers). Every op is a
//!                   plain `pub fn` taking a borrowed `&rusqlite::Connection`.
//!   * [`preview`] — the `repoindex://` byte producers (img passthrough / comrak+ammonia markdown).
//!
//! What this file does, per CONTRACT.md:
//!   1. Registers the `repoindex://` custom scheme → [`preview::handle_repoindex`] (raw bytes,
//!      NEVER base64).
//!   2. In `.setup()`: opens the ONE read-only `Connection` ([`db::Db::open_at`]) and loads the
//!      crosslinks adjacency once ([`db::Crosslinks::load`]) into `tauri::State`; applies the PROVEN
//!      transparent-window `apply_vibrancy` glass (window-vibrancy 0.7.1, `macos-private-api`); and
//!      runs a DEV STARTUP SELF-CHECK that logs the entries row count + a sample `search("umap")`
//!      hit count to stderr (so verification can grep proof the data plane works in the RUNNING app).
//!   3. Registers EVERY contract command in `invoke_handler` as a thin shim that briefly locks the
//!      `Mutex<Connection>` and delegates to the matching `db::` function — the lock guard is dropped
//!      the instant the call returns (never held across an `.await`).
//!
//! The DTOs (`Row`/`SearchResult`/`EntryDetail`/`Facets`) and all query/grammar logic are defined
//! ONCE in `db.rs`; this file does not re-declare them. The frozen IPC field names/types therefore
//! have a single source of truth.

mod db;
mod helper;
mod pathkey;
mod preview;
mod reconcile;
mod tree;
mod watcher;
mod writer;

// The op-journal + self-event engine (§5) is built + fully unit-tested in Phase 1 but stays
// INTERNAL/test-only + the startup `recover` path — `apply_op` gets NO `#[tauri::command]` until
// Phase 4 wires the file ops. So its API reads as "never used" in a non-test build BY DESIGN; the
// allow documents that (the tests + `ops::recover` at startup keep it exercised).
#[allow(dead_code)]
mod defer;
#[allow(dead_code)]
mod journal;
#[allow(dead_code)]
mod ops;

use std::sync::{Arc, Mutex};

use db::{Crosslinks, Db, EntryDetail, Facets, Project, Projects, ReindexReport, Row, SearchResult};
use defer::DeferralRegistry;
use helper::PyMetaSource;
use journal::OpLog;
use reconcile::ReconcileCtx;
use watcher::WatcherHandle;
use writer::IndexWriter;
// `Manager` → `app.state()`; `Emitter` → `app.emit(...)` (the live index-progress broadcast).
use tauri::{Emitter, Manager, State};

/// The live-index engine's managed state (Phase 0–1). `IndexWriter` is the SOLE `INDEX.sqlite`
/// writer; `ReconcileCtx` carries the walk policy + Python meta helper the watcher uses; the
/// `WatcherHandle` is behind `Mutex<Option<…>>` so a project switch / quit can stop+replace it; the
/// `OpLog` + `DeferralRegistry` are the durable op-journal + self-event registry (built + recovered
/// at startup; the FS-mutating `apply_op` stays internal/test-only in Phase 1).
type WriterState = Arc<IndexWriter>; // the sole writer, shared with the watcher thread
type WatcherState = Mutex<Option<WatcherHandle>>; // swappable on project switch / stoppable on quit
type OpLogState = Mutex<OpLog>; // durable op-journal (per-project; repointed on switch)
type RegistryState = Mutex<DeferralRegistry>; // self-event registry (cleared on switch)

/// Payload for the `index-progress` event — one indexer phase line, tagged with the project root it
/// belongs to (the frontend filters by `root` so a stale overlay never shows another project's log).
/// Must be `Clone + Serialize` for `Emitter::emit`.
#[derive(Clone, serde::Serialize)]
struct IndexProgress {
    root: String,
    line: String,
}

/// Payload for the `index-changed` event — emitted after each live reconcile flush with the changed
/// directory keys (the frontend may refetch affected `list_children`, or ignore it in Phase 1).
#[derive(Clone, serde::Serialize)]
struct IndexChanged {
    root: String,
    dirs: Vec<String>,
}

#[cfg(target_os = "macos")]
use window_vibrancy::{apply_vibrancy, NSVisualEffectMaterial, NSVisualEffectState};

// ───────────────────────────────────────────────────────────────────────────────────────────
// #[tauri::command] shims — the FROZEN IPC surface. Each is a one-liner: lock the read-only
// `Db` briefly via `Db::with`, delegate to the matching substantive `db::` function, drop the
// guard. No logic lives here (it is all in `db.rs`); these only bridge `State` → `&Connection`.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// `list_page(offset, limit, sort)` → one page of rows ordered by a whitelisted column. NEVER
/// parses `meta` (see `db::list_page`).
#[tauri::command]
fn list_page(db: State<'_, Db>, offset: u32, limit: u32, sort: String) -> Result<Vec<Row>, String> {
    db.with(|conn| db::list_page(conn, offset, limit, &sort))
}

/// `list_all()` → the ENTIRE index as path-ordered rows — the browse tree's single bulk load.
/// Distinct from `list_page` (page-bounded at `MAX_PAGE_LIMIT` for the search view): the browse
/// tree builds + virtualizes the whole index client-side, so it needs every row, unclamped. The
/// 200k-row request the frontend used to pass to `list_page` was silently capped at 500 — this is
/// the fix (see `db::list_all`).
#[tauri::command]
fn list_all(db: State<'_, Db>) -> Result<Vec<Row>, String> {
    db.with(db::list_all)
}

/// `search(query, offset, limit)` → a PAGE of matching rows (capped at `MAX_PAGE_LIMIT`) + the total
/// match count. Parses the search grammar — free tokens → LIKE substring over the reconstructed
/// haystack (path+category+ext+extractor+tags+meta); `cat:`/`type:`/`path:`/`dir:`/`obs:`/`obsm:` →
/// scoped LIKE; `ext:` → exact equality (multiple `ext:` UNION via `IN`). The real implementation is
/// `db::search`. NOTE: the grouped-search UI uses `search_all` (uncapped) for losslessness; this
/// paged command stays for windowed callers (e.g. the lineage `path:"…"` lookup).
#[tauri::command]
fn search(
    db: State<'_, Db>,
    query: String,
    offset: u32,
    limit: u32,
    include_figure_text: Option<bool>,
) -> Result<SearchResult, String> {
    let fig = include_figure_text.unwrap_or(false);
    db.with(|conn| db::search(conn, &query, offset, limit, fig))
}

/// `search_all(query)` → EVERY matching row + total, NOT page-capped — the grouped-search view's
/// lossless source. Mirrors the `list_page`→`list_all` pair: same predicate as `search`, but bounded
/// only by `BROWSE_ROW_HARD_CAP` so the whole match set reaches the client (the paged `search`
/// silently dropped the alphabetical tail past row 500). The real implementation is `db::search_all`.
#[tauri::command]
fn search_all(
    db: State<'_, Db>,
    query: String,
    include_figure_text: Option<bool>,
) -> Result<SearchResult, String> {
    let fig = include_figure_text.unwrap_or(false);
    db.with(|conn| db::search_all(conn, &query, fig))
}

/// `search_ids(tokens, exts, cats)` → `[{id, path}]` for the top-hits band's tier 3.
/// IDS + PATHS ONLY, never Rows: the payload is ~52x smaller (88 KB vs 4,551 KB for `figure`) and
/// saves 4.4 ms of JSON.parse per reply; the SQL costs the same either way. The frontend joins on
/// PATH (uniquely indexed), because row ids are rowids and churn across a reconcile.
///
/// `tokens` arrive ALREADY split / NFD-normalized / ASCII-folded from `src/query.ts` — the ONE
/// tokenizer in the app. This command parses nothing, so the two sides cannot disagree about what
/// a token is. The real implementation is `db::search_ids`.
#[tauri::command]
fn search_ids(
    db: State<'_, Db>,
    tokens: Vec<String>,
    exts: Vec<String>,
    cats: Vec<String>,
    include_figure_text: Option<bool>,
) -> Result<Vec<db::IdPath>, String> {
    let fig = include_figure_text.unwrap_or(false);
    db.with(|conn| db::search_ids(conn, &tokens, &exts, &cats, fig))
}

/// `get_entry(id)` → `EntryDetail{row, meta, refs, ref_by}`. The ONLY command that parses the FULL
/// `meta` JSON (lazily, for one entry) and resolves lineage from the crosslinks adjacency.
#[tauri::command]
fn get_entry(
    db: State<'_, Db>,
    crosslinks: State<'_, Mutex<Crosslinks>>,
    id: i64,
) -> Result<EntryDetail, String> {
    // Crosslinks is now SWAPPABLE (reloaded on a project switch / reindex), so it is managed behind
    // a `Mutex`. Lock it briefly to borrow the current adjacency; the guard drops with the call.
    let cl = crosslinks.lock().map_err(|e| format!("crosslinks mutex poisoned: {e}"))?;
    db.with(|conn| db::get_entry(conn, &cl, id))
}

/// `facets()` → the filter-chip counts (`GROUP BY category` / `GROUP BY ext`). NEVER parses `meta`.
#[tauri::command]
fn facets(db: State<'_, Db>) -> Result<Facets, String> {
    db.with(db::facets)
}

/// `reveal_in_finder(path)` → reveal the absolute path in Finder via `open -R`. No DB access; the
/// frontend passes the entry's stored (repo-relative POSIX) path, resolved under the ACTIVE
/// project's root (read from the managed `Projects` state — the only new wiring vs. the old shim).
#[tauri::command]
fn reveal_in_finder(projects: State<'_, Projects>, path: String) -> Result<(), String> {
    db::reveal_in_finder(&projects.active_root(), &path)
}

/// `copy_path(path, kind)` → the requested string form (`abs|rel|posix|file_uri`). No DB access;
/// the frontend writes the returned string to the clipboard. `abs`/`file_uri` resolve under the
/// ACTIVE project's root (from the managed `Projects` state).
#[tauri::command]
fn copy_path(projects: State<'_, Projects>, path: String, kind: String) -> String {
    db::copy_path(&projects.active_root(), &path, &kind)
}

/// `open_file(path)` → open the file in its default app via `open` (no `-R`). No DB access; the
/// frontend passes the entry's stored (repo-relative POSIX) path, resolved under the ACTIVE
/// project's root (from the managed `Projects` state).
#[tauri::command]
fn open_file(projects: State<'_, Projects>, path: String) -> Result<(), String> {
    db::open_file(&projects.active_root(), &path)
}

/// `reindex()` → the in-app Rebuild (§3.6 Path A). Rust is the sole `INDEX.sqlite` writer now, so a
/// rebuild is: pause the watcher → run the Python cold walk (emits the frozen `INDEX.json`/`.jsonl`
/// manifest ONLY, no sqlite — so no orphaned-WAL hazard, §0.5D) → the Rust writer INGESTS that fresh
/// JSONL into the live index in ONE `BEGIN IMMEDIATE … COMMIT` (WAL snapshot isolation → readers see
/// old-or-new atomically, NO inode swap, NO `reopen_at`, §3.3) → resume with a full rescan. Also
/// regenerates the committed manifest as a side effect. Errors in read-only degrade mode (another
/// instance holds the writer lock). `crosslinks.json` is live-stale until a cold rebuild (§4.6).
#[tauri::command]
async fn reindex(app: tauri::AppHandle) -> Result<ReindexReport, String> {
    let (root, index_path) = {
        let proj = app.state::<Projects>().active_project()?;
        (proj.root.clone(), proj.index_path())
    };
    let writer = app
        .try_state::<WriterState>()
        .map(|w| w.inner().clone())
        .ok_or("reindex: index is read-only (another Lens instance holds the writer lock)")?;

    // Pause the watcher during the bulk rebuild (scoped so no State guard crosses the .await).
    if let Some(w) = app.try_state::<WatcherState>() {
        if let Some(h) = w.lock().map_err(|e| format!("watcher mutex poisoned: {e}"))?.as_ref() {
            h.pause();
        }
    }

    // The out dir is the index's own directory (`<root>/_repo_index`).
    let out_dir = std::path::Path::new(&index_path)
        .parent()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|| format!("{root}/_repo_index"));
    let app_emit = app.clone();
    let root_for_index = root.clone();
    let root_for_payload = root.clone();
    let out_for_task = out_dir.clone();
    let res: Result<tree::IngestStats, String> =
        tauri::async_runtime::spawn_blocking(move || {
            // 1. Python cold walk → fresh INDEX.jsonl (+ INDEX.json), NO sqlite.
            db::run_manifest_streamed(&root_for_index, &out_for_task, |line| {
                let _ = app_emit.emit(
                    "index-progress",
                    IndexProgress { root: root_for_payload.clone(), line: line.to_string() },
                );
            })?;
            // 2. Rust ingests the fresh JSONL into the live index in one BEGIN IMMEDIATE … COMMIT.
            let jsonl = std::fs::read_to_string(format!("{out_for_task}/INDEX.jsonl"))
                .map_err(|e| format!("rebuild: reading INDEX.jsonl: {e}"))?;
            let entries = tree::parse_jsonl(&jsonl)?;
            let now = journal::now_iso();
            writer.ingest(&entries, &now, 0)
        })
        .await
        .map_err(|e| format!("reindex task failed to join: {e}"))?;

    // Resume the watcher (full rescan reconverges) regardless of the ingest result.
    if let Some(w) = app.try_state::<WatcherState>() {
        if let Some(h) = w.lock().map_err(|e| format!("watcher mutex poisoned: {e}"))?.as_ref() {
            h.resume_with_full_rescan();
        }
    }
    res?;

    let db = app.state::<Db>();
    let entries = db.with(db::file_count)?;
    Ok(ReindexReport { entries })
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Multi-project commands (Phase-1 backend plumbing — no UI yet). The invariant: EXACTLY ONE
// project resident at a time. `switch_project` REPOINTS the single `Db` connection in place
// (`Db::reopen_at`) + reloads the swappable `Crosslinks`; it never opens a second connection. The
// indexer always runs as a one-at-a-time SUBPROCESS (`index_project`), never in-process.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// `list_projects()` → every registered project (name + root).
#[tauri::command]
fn list_projects(projects: State<'_, Projects>) -> Result<Vec<Project>, String> {
    projects.list()
}

/// `current_project()` → the active project (the one the resident `Db`/`Crosslinks` point at).
#[tauri::command]
fn current_project(projects: State<'_, Projects>) -> Result<Project, String> {
    projects.active_project()
}

/// `add_project(root, name?)` → register a new project (validating `root` is a directory) and
/// persist. Does NOT index or switch — the caller indexes (`index_project`) then switches
/// (`switch_project`). Idempotent: a root already registered is returned unchanged.
#[tauri::command]
fn add_project(
    projects: State<'_, Projects>,
    root: String,
    name: Option<String>,
) -> Result<Project, String> {
    projects.add(root, name)
}

/// `switch_project(root)` → make `root` the active project. Requires the project's index file to
/// EXIST (else a clear `Err` the frontend can act on — e.g. prompt to `index_project` first). On
/// success: repoint the SINGLE `Db` connection at the new index, reload its crosslinks, then commit
/// + persist the active selection. The previous project's connection/rows are released by the swap
/// — only ONE project is ever resident.
#[tauri::command]
fn switch_project(
    app: tauri::AppHandle,
    db: State<'_, Db>,
    crosslinks: State<'_, Mutex<Crosslinks>>,
    projects: State<'_, Projects>,
    root: String,
) -> Result<(), String> {
    let proj = projects.get(&root)?;
    let index_path = proj.index_path();
    if !proj.index_reachable() {
        return Err(format!(
            "switch_project: no index for \"{}\" — run index_project first ({index_path})",
            proj.name
        ));
    }
    // #7: no-op if already active — never tear down the live engine or re-acquire the SAME project's
    // writer flock on a second fd (which self-conflicts on a volume where flock works). Just refresh
    // the swappable crosslinks and return.
    //
    // The reader must ALSO already be on this project's index. It is not enough to compare roots:
    // when the active project was unreachable at boot, `.setup()` degraded the pool onto the
    // placeholder index while `active_root` kept naming the absent project. Re-picking that same
    // project after plugging its volume back in would then hit this branch and silently leave the
    // user staring at the empty placeholder.
    if projects.active_root() == root && db.current_index_path() == index_path {
        *crosslinks.lock().map_err(|e| format!("crosslinks mutex poisoned: {e}"))? =
            Crosslinks::load(&proj.crosslinks_path());
        return Ok(());
    }

    // Helper: replace the managed watcher handle (stopping any prior one happens via `take`).
    // RECOVER from a poisoned WatcherState mutex (`into_inner`) rather than silently skipping — else
    // a poison would drop the freshly-spawned `wh` (its Drop stops it) and STRAND the engine, breaking
    // the "always respawn before returning" invariant (the re-review's Q5 gap).
    let set_watcher = |wh: Option<WatcherHandle>| {
        if let Some(w) = app.try_state::<WatcherState>() {
            let mut g = w.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            if let Some(old) = g.take() {
                old.stop();
            }
            *g = wh;
        }
    };

    let Some(writer) = app.try_state::<WriterState>() else {
        // Read-only degrade mode (no writer): just repoint the reader + crosslinks.
        db.reopen_at(&index_path)?;
        *crosslinks.lock().map_err(|e| format!("crosslinks mutex poisoned: {e}"))? =
            Crosslinks::load(&proj.crosslinks_path());
        projects.set_active(&root)?;
        return Ok(());
    };

    // Stop the old watcher BEFORE repointing the writer (it must not flush OLD-root paths into the
    // NEW index). We ALWAYS respawn a watcher before returning — a failure never strands it (#6).
    set_watcher(None);

    // The one genuinely-likely-to-fail step: acquiring the NEW project's writer lock. `writer.switch`
    // acquires the new lock BEFORE mutating, so on failure the writer is UNTOUCHED → fully roll back
    // to the old project (respawn its watcher) and surface the error.
    if let Err(e) = writer.switch(&root, &index_path) {
        set_watcher(spawn_watcher(&app, &projects.active_root(), writer.inner().clone()));
        return Err(e);
    }

    // Writer now on the new project. Respawn the watcher on the new root IMMEDIATELY — so NO later
    // fallible step (a poisoned crosslinks mutex, an app_config_dir error, an oplog open) can
    // return early and strand a stopped watcher (the re-review's residual concern). The watcher uses
    // the WRITER (already switched), not the reader, so it's safe to start before db.reopen.
    set_watcher(spawn_watcher(&app, &root, writer.inner().clone()));

    // Repoint the rest BEST-EFFORT — no `?` that could return before the watcher is back up.
    let _ = db.reopen_at(&index_path);
    if let (Some(oplog_state), Some(reg)) =
        (app.try_state::<OpLogState>(), app.try_state::<RegistryState>())
    {
        if let Ok(path) = oplog_path_for(&app, &root) {
            if let Ok(new_oplog) = OpLog::open_at(&path) {
                let ctx = ReconcileCtx::new(&root, Arc::new(PyMetaSource::new(&root)));
                let _ = ops::recover(&new_oplog, &writer, &ctx);
                if let Ok(mut g) = oplog_state.lock() {
                    *g = new_oplog;
                }
            }
        }
        if let Ok(mut g) = reg.lock() {
            g.clear(); // a switch abandons the old tree's in-flight tokens (§4.8)
        }
    }
    if let Ok(mut cl) = crosslinks.lock() {
        *cl = Crosslinks::load(&proj.crosslinks_path());
    }
    // The watcher/writer/reader are all on the new project now; committing the active selection is
    // the last step (a persist failure surfaces as Err but no longer strands the live engine).
    projects.set_active(&root)?;
    Ok(())
}

/// `remove_project(root)` → deregister a project and persist. Refuses to remove the ACTIVE project
/// (switch away first) so the resident connection never points at a deregistered root.
#[tauri::command]
fn remove_project(projects: State<'_, Projects>, root: String) -> Result<(), String> {
    projects.remove(&root)
}

/// `index_project(root)` → run the canonical indexer subprocess for an ARBITRARY project root
/// (reused by an add-then-index flow), returning its post-index entry count. If `root` is the
/// ACTIVE project, the resident `Db` is repointed at the freshly written index (and its crosslinks
/// reloaded), mirroring `reindex`; otherwise the count is read via a TRANSIENT connection that is
/// dropped immediately (the app still holds exactly ONE resident connection). The indexer runs on
/// the blocking pool as a separate process — never in-process, never two at once for a given root.
#[tauri::command]
async fn index_project(app: tauri::AppHandle, root: String) -> Result<ReindexReport, String> {
    // ACTIVE project → the in-app Rust Rebuild (the live writer holds the lock, so the Python DR
    // build would REFUSE, §3.6). Non-active → the flock is free, so run the Python DR `.sqlite` build.
    if app.state::<Projects>().active_root() == root {
        return reindex(app).await;
    }

    let index_path = format!("{root}/_repo_index/INDEX.sqlite");
    let app_for_emit = app.clone();
    let root_for_index = root.clone();
    let root_for_payload = root.clone();
    tauri::async_runtime::spawn_blocking(move || {
        db::run_reindex_streamed(&root_for_index, |line| {
            let _ = app_for_emit.emit(
                "index-progress",
                IndexProgress { root: root_for_payload.clone(), line: line.to_string() },
            );
        })
    })
    .await
    .map_err(|e| format!("index_project task failed to join: {e}"))??;

    // NON-active → count via a transient connection (never retained).
    let entries = db::count_entries_at(&index_path)?;
    Ok(ReindexReport { entries })
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Dev startup self-check — logs to STDERR at launch so verification can grep proof that the data
// plane works END-TO-END inside the RUNNING app (not just in unit tests): the `entries` row count
// and a sample `search("umap")` hit count. Read-only; degrades to a logged warning on any error
// (never fails startup). The distinctive `[lens][selfcheck]` prefix is the grep anchor.
// ───────────────────────────────────────────────────────────────────────────────────────────

fn dev_startup_self_check(db: &Db) {
    let report = db.with(|conn| {
        // file-only count (§2.8): synthesized dir rows never inflate the user-facing "N files".
        let entries = db::file_count(conn)?;
        let umap_hits = db::search(conn, "umap", 0, 1, false)
            .map(|r| r.total)
            .map_err(|e| format!("search(\"umap\") failed: {e}"))?;
        Ok((entries, umap_hits))
    });
    match report {
        Ok((entries, umap_hits)) => {
            eprintln!("[lens][selfcheck] entries row count = {entries}");
            eprintln!("[lens][selfcheck] search(\"umap\") total hits = {umap_hits}");
            eprintln!("[lens][selfcheck] data plane OK (read-only INDEX.sqlite reachable in running app)");
        }
        Err(e) => {
            eprintln!("[lens][selfcheck] WARNING: data plane self-check failed: {e}");
        }
    }
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Live-index engine commands (§2.7, §4.8): the v2 tree queries + the watcher controls.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// `list_children(parent_key, limit, offset)` → the direct children of a directory (§2.7). The ONE
/// command that surfaces dir rows; `parent_key = ""` is the repo root.
#[tauri::command]
fn list_children(
    db: State<'_, Db>,
    parent_key: String,
    limit: u32,
    offset: u32,
) -> Result<Vec<Row>, String> {
    db.with(|conn| db::list_children(conn, &parent_key, limit, offset))
}

/// `count_children(parent_key)` → the number of direct children (live callers use this, §2.9).
#[tauri::command]
fn count_children(db: State<'_, Db>, parent_key: String) -> Result<i64, String> {
    db.with(|conn| db::count_children(conn, &parent_key))
}

/// `force_rescan()` → push a whole-tree rescan into the reconciler via its control channel (never
/// writes on this IPC thread — that would interleave with the reconciler's flush, §4.4).
#[tauri::command]
fn force_rescan(watcher: State<'_, WatcherState>) -> Result<(), String> {
    if let Some(h) = watcher.lock().map_err(|e| format!("watcher mutex poisoned: {e}"))?.as_ref() {
        h.force_rescan();
    }
    Ok(())
}

#[derive(Clone, serde::Serialize)]
struct WatchStatus {
    watching: bool,
    root: String,
}

/// `watch_status()` → whether the live watcher is running + the active root. Uses `Projects` (always
/// managed) so it still answers in read-only degrade mode (no writer/watcher).
#[tauri::command]
fn watch_status(
    watcher: State<'_, WatcherState>,
    projects: State<'_, Projects>,
) -> Result<WatchStatus, String> {
    let watching = watcher.lock().map_err(|e| format!("watcher mutex poisoned: {e}"))?.is_some();
    Ok(WatchStatus { watching, root: projects.active_root() })
}

// ── engine bootstrap helpers ────────────────────────────────────────────────────────────────

/// The op-journal lives on INTERNAL APFS (§5.2), never the exFAT data drive: under Tauri's app
/// config dir, namespaced per project root (a coarse but stable per-drive-ish key).
fn oplog_path_for(app: &tauri::AppHandle, root: &str) -> Result<String, String> {
    let base = app.path().app_config_dir().map_err(|e| format!("app_config_dir: {e}"))?;
    let key: String = root.chars().map(|c| if c.is_alphanumeric() { c } else { '_' }).collect();
    Ok(base.join("oplog").join(format!("{key}.sqlite")).to_string_lossy().into_owned())
}

/// Open the writer (acquires the single-writer lock + migrates v1→v2) and the op-journal, then run
/// crash recovery. Returns `Err` if another live Lens instance holds the writer lock (the caller
/// degrades to read-only mode, §3.9).
fn build_engine(
    app: &tauri::AppHandle,
    root: &str,
    index_path: &str,
) -> Result<(WriterState, OpLog), String> {
    let writer: WriterState = Arc::new(IndexWriter::open(root, index_path)?);
    let oplog = OpLog::open_at(&oplog_path_for(app, root)?)?;
    // Crash recovery (§5.7): reconverge the index + op state from FS evidence + durable intent.
    let ctx = ReconcileCtx::new(root, Arc::new(PyMetaSource::new(root)));
    ops::recover(&oplog, &writer, &ctx)?;
    Ok((writer, oplog))
}

/// An EMPTY, valid v2 index inside the app's own config dir — the last-resort reader target when
/// the active project's index cannot be opened (its volume is unplugged, renamed, or unreadable).
///
/// This exists so `.setup()` never has to fail. Every `#[tauri::command]` resolves `State<Db>`
/// infallibly, so "no DB at all" is not representable without touching the frozen IPC surface;
/// pointing the pool at an empty index instead keeps that invariant, opens the window, and leaves
/// the user able to pick a project (or plug the drive back in). Always on the INTERNAL disk, which
/// is the whole point — every registered root lives under `/Volumes/…` and can vanish.
///
/// `connect_writer` is `READ_WRITE|CREATE` and runs `migrate_to_v2` on open, so this both creates
/// the file on first use and guarantees the v2 schema the `query_only` reader pool needs. The
/// connection is dropped immediately — the placeholder gets no resident writer, no watcher.
fn placeholder_index_path(app: &tauri::AppHandle) -> Result<String, String> {
    let dir = app
        .path()
        .app_config_dir()
        .map_err(|e| format!("placeholder: app_config_dir: {e}"))?
        .join("placeholder")
        .join("_repo_index");
    std::fs::create_dir_all(&dir)
        .map_err(|e| format!("placeholder: create {}: {e}", dir.display()))?;
    let path = dir.join("INDEX.sqlite").to_string_lossy().into_owned();
    drop(writer::connect_writer(&path)?);
    Ok(path)
}

/// Build a fresh reconcile context for `root` (the watcher owns its own; `reindex` builds one on
/// demand). The Python `extract-batch` helper supplies heavy meta; config gates come from
/// `config_used` in the committed `INDEX.json` (§6.5).
fn make_ctx(root: &str) -> Arc<ReconcileCtx> {
    Arc::new(ReconcileCtx::new(root, Arc::new(PyMetaSource::new(root))))
}

/// Spawn the live watcher on `root`, emitting `index-changed` after each flush.
fn spawn_watcher(app: &tauri::AppHandle, root: &str, writer: WriterState) -> Option<WatcherHandle> {
    let app_emit = app.clone();
    let root_owned = root.to_string();
    WatcherHandle::spawn(
        root,
        writer,
        make_ctx(root),
        Box::new(move |dirs| {
            let _ = app_emit.emit("index-changed", IndexChanged { root: root_owned.clone(), dirs });
        }),
    )
    .ok()
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// App bootstrap — register the repoindex:// scheme, open the DB read-only + load crosslinks in
// `.setup()`, apply the proven transparent-window vibrancy, run the self-check, wire the commands.
// ───────────────────────────────────────────────────────────────────────────────────────────

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // The native folder picker for "Add folder…" (open({ directory: true })). Only the dialog
        // plugin's `open` command is used (capability: dialog:default).
        .plugin(tauri_plugin_dialog::init())
        // Native file drag-out: the frontend's startDrag() begins an OS drag session so a row can be
        // dropped onto Finder / Inkscape / any app (capability: drag:default). No commands of ours.
        .plugin(tauri_plugin_drag::init())
        // repoindex://<kind>/<id> → raw bytes (image passthrough / markdown HTML), handled by the
        // synchronous + panic-safe `preview::handle_repoindex`. Registered exactly the way
        // figpolish registers `figpolish://` (the proven byte-server pattern).
        .register_uri_scheme_protocol("repoindex", |ctx, request| {
            preview::handle_repoindex(ctx.app_handle(), request)
        })
        .setup(|app| {
            // Resolve the persistent registry file in Tauri's app config dir
            // (~/Library/Application Support/com.declan.lens/projects.json on macOS). `app_config_dir`
            // only COMPUTES the path; `Projects::load_or_seed` creates the dir on its first write.
            let config_path = app
                .path()
                .app_config_dir()?
                .join("projects.json")
                .to_string_lossy()
                .into_owned();

            // Load the project registry, or seed it on first run with a single default project
            // pointing at the legacy SCRNA root — so the FIRST boot opens today's project identically
            // (zero observable change), and later boots reopen whatever was last active.
            let projects = Projects::load_or_seed(config_path);
            let active = projects.active_project()?;

            // ── live-index engine (§4.8): open the WRITER FIRST — it acquires the single-writer lock,
            //    CREATES a missing INDEX.sqlite (cold first run), and migrates v1→v2 — so the reader
            //    pool below always opens an EXISTING v2 file (no cold-start hard-abort, no v1 read).
            //    Then the op-journal (+ crash recovery), then the watcher. Degrade to READ-ONLY mode
            //    (§3.9) if the writer can't open (another live instance holds the lock, disk error);
            //    the reader pool stays live, just no reconciler.
            let handle = app.handle().clone();
            let engine = build_engine(&handle, &active.root, &active.index_path());

            // Open the ACTIVE project's index reader POOL (query_only WAL, statement-level read-only).
            // NEVER `?` HERE. This open depends on a removable volume, and an error propagating out
            // of `.setup()` is not a graceful failure: Tauri panics, the panic happens inside
            // `did_finish_launching` (an ObjC callback = a non-unwinding boundary), so it becomes
            // `abort()` → SIGABRT with NO window ever created. That bricked the app for 3 days in
            // Aug 2026 — with no UI, there was no way to select a different project. Degrade onto
            // the empty placeholder index instead; the window opens and the user can recover.
            let db = match Db::open_at(&active.index_path()) {
                Ok(db) => db,
                Err(e) => {
                    eprintln!(
                        "[lens] active project \"{}\" is unavailable ({e}) — opening the EMPTY \
                         placeholder index; plug the volume in and re-pick the project",
                        active.root
                    );
                    Db::open_at(&placeholder_index_path(&handle)?)?
                }
            };

            // Load the active project's crosslinks adjacency. A missing/garbled file degrades to
            // empty adjacency (lineage just blank), never a startup failure. Managed behind a `Mutex`
            // so a project switch / reindex can swap in the new project's adjacency in place.
            let crosslinks = Crosslinks::load(&active.crosslinks_path());

            app.manage(db);
            app.manage(Mutex::new(crosslinks));

            match engine {
                Ok((writer, oplog)) => {
                    let watcher = spawn_watcher(&handle, &active.root, writer.clone());
                    app.manage::<WriterState>(writer);
                    app.manage::<OpLogState>(Mutex::new(oplog));
                    app.manage::<RegistryState>(Mutex::new(DeferralRegistry::new()));
                    app.manage::<WatcherState>(Mutex::new(watcher));
                }
                Err(e) => {
                    eprintln!("[lens] live-index engine unavailable — read-only mode: {e}");
                    app.manage::<WatcherState>(Mutex::new(None));
                }
            }

            app.manage(projects);

            // DEV STARTUP SELF-CHECK → stderr: prove the data plane works in the running app.
            // Borrow the just-managed State back out to run it against the live (active) Connection.
            dev_startup_self_check(&app.state::<Db>());

            // The PROVEN transparent-window + NSVisualEffectView vibrancy (window-vibrancy 0.7.1,
            // macos-private-api) — verbatim from the spike. Window label "main" is set in
            // tauri.conf.json; `UnderWindowBackground` + `FollowsWindowActiveState` is the locked look.
            let win = app
                .get_webview_window("main")
                .expect("window label \"main\" must exist (tauri.conf.json)");

            #[cfg(target_os = "macos")]
            apply_vibrancy(
                &win,
                NSVisualEffectMaterial::UnderWindowBackground,
                Some(NSVisualEffectState::FollowsWindowActiveState),
                None,
            )
            .expect("apply_vibrancy failed (macOS + macos-private-api)");

            #[cfg(not(target_os = "macos"))]
            let _ = win;

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            list_page,
            list_all,
            search,
            search_all,
            search_ids,
            get_entry,
            facets,
            reveal_in_finder,
            copy_path,
            open_file,
            reindex,
            list_projects,
            current_project,
            add_project,
            switch_project,
            remove_project,
            index_project,
            list_children,
            count_children,
            force_rescan,
            watch_status,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // Graceful quit (§4.8, §0.5G): stop the watcher FIRST (no more writes), then the writer
            // does the final `wal_checkpoint(TRUNCATE)` — readers-first-writer-last. The reader pool
            // drops with the process. Idempotent across ExitRequested + Exit.
            if matches!(event, tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit) {
                if let Some(w) = app_handle.try_state::<WatcherState>() {
                    if let Ok(mut guard) = w.lock() {
                        if let Some(h) = guard.take() {
                            h.stop();
                        }
                    }
                }
                if let Some(writer) = app_handle.try_state::<WriterState>() {
                    let _ = writer.checkpoint_truncate();
                }
            }
        });
}
