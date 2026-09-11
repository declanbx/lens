//! Lens backend — the read-only data layer over the EXISTING `_repo_index/INDEX.sqlite`.
//!
//! This module OWNS the substantive backend (the row mapping, the search-grammar parser, the
//! read-only queries, the crosslinks adjacency, the path helpers). It is deliberately decoupled
//! from Tauri's `State`/`Mutex` plumbing: every operation is a plain `pub fn` that takes an
//! already-borrowed `&rusqlite::Connection` (and `&Crosslinks` where lineage is needed), so:
//!   * the locking discipline (lock the `Mutex<Connection>` briefly, never across an `.await`)
//!     lives in the thin `#[tauri::command]` shims in `lib.rs` (the Integrate stage owns that
//!     wiring) — this module never touches `State`;
//!   * the query logic is unit-testable with a bare in-memory `Connection`.
//!
//! INVARIANTS honored here (CONTRACT.md):
//!   * The DB is opened READ-ONLY elsewhere; nothing in this file writes.
//!   * `list_page` / `search` / `facets` NEVER parse `meta` — they read the denormalized columns
//!     (`n_obs`, `n_vars`, …) only. The FULL `meta` JSON is parsed lazily in EXACTLY one place:
//!     `get_entry`.
//!   * Every row-returning query selects the SAME ordered column list (`ROW_COLS`) and maps it
//!     through the SINGLE `row_from` function — the one source of truth for column→`Row`.
//!   * All SQL is prepared; user input never interpolates into SQL text (the `sort` arg is
//!     whitelisted; every grammar value binds as a `?` parameter). Page size is bounded.

use std::collections::HashMap;
use std::sync::Mutex;

use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use serde_json::Value;

// ───────────────────────────────────────────────────────────────────────────────────────────
// Canonical on-disk locations + caps (the app's only data inputs).
// ───────────────────────────────────────────────────────────────────────────────────────────

// THERE IS NO DEFAULT PROJECT ROOT, NO DEFAULT INTERPRETER AND NO HARDCODED CRAWLER PATH.
//
// Three `const` absolute paths used to live here — a seed project root on one external volume, one
// user's anaconda python3, and that volume's copy of the `repo_index` package. Every one of them
// resolved on the machine they were written for and on no other, which is what made the app
// unusable for anyone else: the window opened, "Add folder…" opened a picker, and indexing then
// failed spawning an interpreter that did not exist.
//
//   * the interpreter and the crawler are now RESOLVED AT RUNTIME — see [`crate::runtime`];
//   * the seed project is gone entirely: a registry with zero projects is a representable state
//     (`list = []`, `active_root = ""`), which is what a genuine first run looks like.
//
/// Hard ceiling on a single page's row count, regardless of the `limit` the frontend asks for.
/// Keeps any one IPC payload bounded (memory invariant) even if a caller passes a huge `limit`.
pub const MAX_PAGE_LIMIT: u32 = 500;

/// Defensive backstop for the browse bulk-load (`list_all`) — NOT a real limit. The browse tree
/// needs the WHOLE index (not a 500-row page), so `list_all` is intentionally NOT clamped to
/// `MAX_PAGE_LIMIT`; this only stops a pathological index from OOMing the single bulk payload. Far
/// above any real file count (the index is bounded by the filesystem — ~38k entries today).
pub const BROWSE_ROW_HARD_CAP: u32 = 5_000_000;

// ───────────────────────────────────────────────────────────────────────────────────────────
// Managed state (constructed once at startup; held by `lib.rs` in `tauri::State`).
// ───────────────────────────────────────────────────────────────────────────────────────────

/// The single read-only DB handle — the `State`-managed type (`Mutex<Connection>`, matching the
/// CONTRACT's "State<Mutex<Connection>>"). Opened ONCE at startup via [`Db::open_at`], never per
/// call. The substantive query functions below take a bare `&Connection` (lock-agnostic +
/// unit-testable); the thin `#[tauri::command]` shims in `lib.rs` lock `db.0` and call them. The
/// [`Db::with`] helper makes those shims one-liners and centralizes the poison-error message.
///
/// The number of resident READER connections (§0.5G): a small pool so concurrent UI commands don't
/// serialize on a single handle. Each pool member is READ_WRITE + `PRAGMA query_only=ON` (WAL-
/// readable — a strictly `READ_ONLY` handle can't join the `-shm` handshake — but write-rejecting).
const READER_POOL_SIZE: usize = 3;

/// The resident reader: a POOL of `query_only` WAL connections at the ACTIVE project's index. ALL
/// writes go through the separate [`IndexWriter`](crate::writer::IndexWriter); under WAL a writer
/// commit becomes visible to each pool connection at the START of its next read transaction, with
/// NO reopen (§3.3) — every `with` call opens a fresh read txn. The read-only *guarantee* now lives
/// at the statement level (`query_only`), not in the open flags (CONTRACT.md:8 renegotiated, §8).
pub struct Db {
    pool: Vec<Mutex<Connection>>,
    next: std::sync::atomic::AtomicUsize,
    index_path: Mutex<String>,
}

impl Db {
    /// Open a strictly-`READ_ONLY` connection (never creates `-shm`). Used only by
    /// [`count_entries_at`]'s RO-first probe against a NON-active project (§3.1) — cheap, and needs
    /// no write permission on that project's directory.
    fn connect_ro(index_path: &str) -> Result<Connection, String> {
        Connection::open_with_flags(
            index_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|e| format!("could not open {index_path} read-only: {e}"))
    }

    /// Open the reader POOL at `index_path` for `app.manage`. Best-effort v2 migration first (§2.9)
    /// so the `query_only` readers (which cannot migrate) never face a v1 schema.
    pub fn open_at(index_path: &str) -> Result<Db, String> {
        crate::writer::ensure_v2_if_writable(index_path);
        let mut pool = Vec::with_capacity(READER_POOL_SIZE);
        for _ in 0..READER_POOL_SIZE {
            // Prefer READ_WRITE + query_only (full WAL participation). Fall back to strictly
            // READ_ONLY on a read-only-mounted index (where creating the `-shm` fails) so the app
            // still opens read-only instead of hard-aborting startup (§3.9 degrade).
            let conn = crate::writer::connect_reader(index_path)
                .or_else(|_| Self::connect_ro(index_path))?;
            pool.push(Mutex::new(conn));
        }
        Ok(Db {
            pool,
            next: std::sync::atomic::AtomicUsize::new(0),
            index_path: Mutex::new(index_path.to_string()),
        })
    }

    /// The floor beneath the placeholder: a pool of EMPTY, v2, IN-MEMORY indexes, touching no disk
    /// at all.
    ///
    /// The placeholder index is already the fallback for "the active project's index will not
    /// open", but it is itself a file on disk (under the app config dir) and so can itself fail —
    /// a full disk, a permissions problem, a config dir that cannot be created. That was the LAST
    /// `?` in `.setup()`, and a `?` there is not an error, it is `abort()` with no window (Tauri
    /// panics inside `did_finish_launching`, which cannot unwind). This makes the window
    /// unconditional: every command still resolves `State<Db>`, every query answers "nothing", and
    /// the user can still reach the folder picker.
    ///
    /// Each pool connection is its own private database — they never see each other's writes, which
    /// is fine precisely because nothing ever writes here.
    pub fn open_in_memory() -> Result<Db, String> {
        let mut pool = Vec::with_capacity(READER_POOL_SIZE);
        for _ in 0..READER_POOL_SIZE {
            let conn = Connection::open_in_memory()
                .map_err(|e| format!("in-memory index: {e}"))?;
            // The v2 schema has to be created here: `ensure_v2_if_writable` works through a
            // separate transient connection, which for `:memory:` would migrate a DIFFERENT
            // database and leave this one without tables.
            crate::tree::migrate_to_v2(&conn)?;
            conn.execute_batch("PRAGMA query_only=ON;")
                .map_err(|e| format!("in-memory index: query_only: {e}"))?;
            pool.push(Mutex::new(conn));
        }
        Ok(Db {
            pool,
            next: std::sync::atomic::AtomicUsize::new(0),
            index_path: Mutex::new(String::new()),
        })
    }

    /// Repoint EVERY pool connection at `index_path` — used on a PROJECT SWITCH and after a Python DR
    /// whole-file replace (both swap the inode). The common Rust JSONL ingest does NOT swap the inode
    /// (§3.3), so it needs no reopen. Best-effort v2 migration first (§2.9).
    pub fn reopen_at(&self, index_path: &str) -> Result<(), String> {
        crate::writer::ensure_v2_if_writable(index_path);
        for slot in &self.pool {
            let conn = crate::writer::connect_reader(index_path)?;
            *slot.lock().map_err(|e| format!("db mutex poisoned: {e}"))? = conn;
        }
        *self.index_path.lock().map_err(|e| format!("db mutex poisoned: {e}"))? =
            index_path.to_string();
        Ok(())
    }

    /// The index this pool is CURRENTLY pointing at — which is **not** always the active project's
    /// own index. When the active project is unreachable at boot, `.setup()` degrades onto the
    /// placeholder index while `active_root` still names the absent project, so `switch_project`
    /// must compare against this (not just the active root) to know whether a switch is genuinely
    /// a no-op. A poisoned mutex yields `""`, which reads as "not what you asked for" → repoint.
    pub fn current_index_path(&self) -> String {
        self.index_path.lock().map(|g| g.clone()).unwrap_or_default()
    }

    /// Check out a pool connection (round-robin) and run `f` against it. The guard is dropped the
    /// instant `f` returns — never held across an `.await`, and never kept open beyond one call (a
    /// perpetually-open read txn pins the WAL and starves the writer's checkpoint, §3.3).
    pub fn with<T>(&self, f: impl FnOnce(&Connection) -> Result<T, String>) -> Result<T, String> {
        let i = self.next.fetch_add(1, std::sync::atomic::Ordering::Relaxed) % self.pool.len();
        let conn = self.pool[i].lock().map_err(|e| format!("db mutex poisoned: {e}"))?;
        f(&conn)
    }
}

/// Crosslinks adjacency, loaded ONCE from `crosslinks.json` at startup. The graph keys nodes by
/// PATH (not entry id): `edges` is `[{src, dst, kind}]`. For a given entry path `P`:
///   * `refs`   (P references these) = the `dst` of every edge whose `src == P`
///   * `ref_by` (these reference P)  = the `src` of every edge whose `dst == P`
#[derive(Default)]
pub struct Crosslinks {
    /// path → outgoing edge targets (the entry's own references).
    refs: HashMap<String, Vec<String>>,
    /// path → incoming edge sources (who references the entry).
    ref_by: HashMap<String, Vec<String>>,
}

impl Crosslinks {
    /// Parse `crosslinks.json` (`{nodes, edges:[{src,dst,kind}], dangling_refs}`) into the two
    /// path-keyed adjacency maps. A missing / garbled file degrades to EMPTY adjacency (the app
    /// still runs; lineage is just blank) rather than failing startup — lineage is auxiliary.
    pub fn load(path: &str) -> Self {
        let mut cl = Crosslinks::default();
        let text = match std::fs::read_to_string(path) {
            Ok(t) => t,
            Err(e) => {
                eprintln!("[lens] crosslinks: could not read {path}: {e} — lineage disabled");
                return cl;
            }
        };
        let doc: Value = match serde_json::from_str(&text) {
            Ok(v) => v,
            Err(e) => {
                eprintln!("[lens] crosslinks: bad JSON in {path}: {e} — lineage disabled");
                return cl;
            }
        };
        if let Some(edges) = doc.get("edges").and_then(|e| e.as_array()) {
            for edge in edges {
                let src = edge.get("src").and_then(|s| s.as_str());
                let dst = edge.get("dst").and_then(|d| d.as_str());
                if let (Some(src), Some(dst)) = (src, dst) {
                    cl.refs.entry(src.to_string()).or_default().push(dst.to_string());
                    cl.ref_by.entry(dst.to_string()).or_default().push(src.to_string());
                }
            }
        }
        cl
    }

    /// The outgoing lineage for an entry path (this entry references these paths). Empty if none.
    pub fn refs_of(&self, path: &str) -> Vec<String> {
        self.refs.get(path).cloned().unwrap_or_default()
    }

    /// The incoming lineage for an entry path (these paths reference this entry). Empty if none.
    pub fn ref_by_of(&self, path: &str) -> Vec<String> {
        self.ref_by.get(path).cloned().unwrap_or_default()
    }
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Multi-project model + registry (Phase-1 backend plumbing). The app holds EXACTLY ONE project
// resident at a time (one `Db` connection, one `Crosslinks`, one active root); switching repoints
// the single connection in place. Nothing here caches a second project's index/connection/rows.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// A registered project: a human `name` + its filesystem `root` (the parent of `_repo_index/`).
/// Every per-project on-disk location DERIVES from `root` via the indexer's fixed convention
/// (`repo_index export-sqlite --root <root>` writes `<root>/_repo_index/{INDEX.sqlite,crosslinks.json}`),
/// so a project is fully described by its root — the index/crosslinks paths are never stored.
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct Project {
    pub name: String,
    pub root: String,
}

impl Project {
    /// The read-only index DB for this project: `<root>/_repo_index/INDEX.sqlite`.
    pub fn index_path(&self) -> String {
        format!("{}/_repo_index/INDEX.sqlite", self.root)
    }

    /// The crosslinks adjacency source for this project: `<root>/_repo_index/crosslinks.json`.
    pub fn crosslinks_path(&self) -> String {
        format!("{}/_repo_index/crosslinks.json", self.root)
    }

    /// Whether this project's index is present ON DISK **right now**. Registration is not
    /// reachability: every root here lives under `/Volumes/…`, so an unplugged (or, after an
    /// unclean eject, a RENAMED — `FieldDrive` → `FieldDrive 1`) external volume leaves a
    /// perfectly-registered project whose index cannot be opened. The single definition of
    /// "usable project", shared by [`Projects::load_or_seed`]'s boot-time selection and the
    /// `switch_project` command's precondition, so the two can never disagree.
    pub fn index_reachable(&self) -> bool {
        std::path::Path::new(&self.index_path()).exists()
    }
}

/// One registered project plus everything the UI needs to tell a WORKING row from a broken one.
/// Registration is not reachability (see [`Project::index_reachable`]) and the switcher used to
/// render both identically: a folder that had been moved, renamed or unplugged looked exactly like
/// a folder that was fine, right up until picking it failed.
///
/// Deliberately reports rather than fails — an entry whose root is unreadable comes back
/// `folder_exists: false, has_index: false, index_bytes: 0`, never an error, because one bad row
/// must not cost the user the whole list.
#[derive(Serialize, Clone, Debug)]
pub struct ProjectStatus {
    pub name: String,
    /// The registry KEY — this is what every other project command takes.
    pub root: String,
    pub is_active: bool,
    /// The project folder itself is on disk right now.
    pub folder_exists: bool,
    /// `<root>/_repo_index/INDEX.sqlite` exists — the same test `switch_project` preconditions on.
    pub has_index: bool,
    /// Total size of `<root>/_repo_index/`, 0 when absent — what the "also delete its index files"
    /// confirmation shows, so the user knows what they are reclaiming before they agree to it.
    pub index_bytes: u64,
}

/// What `remove_project` reports back. `now_active` is the whole point: after removing the ACTIVE
/// project the app has moved somewhere, and the caller must be told WHERE — a root to display, or
/// `None` for "nothing is open, show the welcome screen".
#[derive(Serialize, Clone, Debug)]
pub struct RemoveReport {
    /// The root that was removed (the key that was passed in), echoed back.
    pub removed: String,
    pub index_deleted: bool,
    pub bytes_freed: u64,
    pub now_active: Option<String>,
}

/// On-disk persistence shape for `projects.json` (the registry + the last-active selection). A
/// missing / garbled file degrades to a fresh seed (see [`Projects::load_or_seed`]), never a fail.
#[derive(Serialize, Deserialize, Default)]
struct ProjectsFile {
    projects: Vec<Project>,
    last_active: String,
}

/// The mutable runtime registry guarded by [`Projects`]: the active project's root, the full list,
/// and the absolute path of the `projects.json` it persists to.
struct ProjectsState {
    active_root: String,
    list: Vec<Project>,
    config_path: String,
}

/// The `State`-managed project registry — a `Mutex` over [`ProjectsState`]. Holds the ACTIVE root
/// (which the path-action commands resolve relative paths against) plus the registered list, and
/// persists every mutation to `projects.json`. Switching projects updates `active_root` here and is
/// paired (in the `lib.rs` command) with a single `Db::reopen_at` + a `Crosslinks` reload — never a
/// second resident connection.
pub struct Projects(Mutex<ProjectsState>);

/// Serialize the registry + active selection to `projects.json`, creating the config dir first
/// (Tauri's `app_config_dir()` only COMPUTES the path — it does not create the directory, so the
/// first write would ENOENT without this). Pretty-printed for hand-inspectability.
fn write_projects_file(config_path: &str, list: &[Project], active_root: &str) -> Result<(), String> {
    if let Some(parent) = std::path::Path::new(config_path).parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("projects: could not create config dir {}: {e}", parent.display()))?;
    }
    let doc = ProjectsFile { projects: list.to_vec(), last_active: active_root.to_string() };
    let text = serde_json::to_string_pretty(&doc)
        .map_err(|e| format!("projects: serialize failed: {e}"))?;
    std::fs::write(config_path, text)
        .map_err(|e| format!("projects: write {config_path} failed: {e}"))
}

impl Projects {
    /// Load the registry from `config_path`. A missing, empty or garbled file yields a registry
    /// with NO projects and NO active root — the honest description of a first run.
    ///
    /// **This used to seed a project pointing at one specific external volume, and persist it.**
    /// The zero-project state was not representable (an empty `projects` array was treated as "no
    /// file" and re-seeded), so a first run on any other Mac wrote a registry whose only entry
    /// named a volume that would never exist there. `""` is now a legal `active_root`; `.setup()`
    /// opens the placeholder index and the frontend shows a welcome screen.
    ///
    /// When the file DOES list projects, `active_root` resolves to `last_active` if that root is
    /// registered AND [reachable](Project::index_reachable), else the first project that is.
    ///
    /// **Reachability is checked here because an unreachable selection used to be FATAL.** Opening
    /// the active project's index is the first thing `.setup()` does; its error propagates out of
    /// the setup hook, and Tauri turns that into a panic inside `did_finish_launching` — an ObjC
    /// callback, so the panic cannot unwind and becomes `abort()`. The app died on SIGABRT *before
    /// any window existed*, leaving no UI to switch project from: a stale `last_active` on a
    /// detached volume bricked every subsequent launch until `projects.json` was hand-edited.
    /// (Observed 2026-07-31 → 08-03: `last_active` = `/Volumes/DUAL DRIVE`, 10 identical crashes.)
    ///
    /// The fallback is deliberately **not persisted** (`needs_write` stays false): `last_active`
    /// keeps naming the absent project, so simply replugging the volume restores it next launch.
    pub fn load_or_seed(config_path: String) -> Self {
        // NOTE: no `.filter(|f| !f.projects.is_empty())` here any more. That filter made an
        // empty-but-valid registry indistinguishable from a missing one, which is precisely what
        // forced the re-seed — the user who removed their last folder got it silently handed back.
        let parsed = std::fs::read_to_string(&config_path)
            .ok()
            .and_then(|t| serde_json::from_str::<ProjectsFile>(&t).ok());

        let (list, active_root) = match parsed {
            Some(f) if f.projects.is_empty() => {
                // A registry that lists nothing: zero projects, nothing active. Not an error.
                (Vec::new(), String::new())
            }
            Some(f) => {
                let is_registered = f.projects.iter().any(|p| p.root == f.last_active);
                let last_active_usable =
                    f.projects.iter().any(|p| p.root == f.last_active && p.index_reachable());

                let active = if last_active_usable {
                    // The normal path: the remembered project is registered and its index is there.
                    f.last_active.clone()
                } else if let Some(p) = f.projects.iter().find(|p| p.index_reachable()) {
                    // Remembered project is gone (volume detached / renamed) — boot into the first
                    // one that IS reachable rather than aborting on the one that isn't.
                    eprintln!(
                        "[lens] projects: last-active \"{}\" is unreachable — booting \"{}\" \
                         instead (last_active left unchanged, so replugging restores it)",
                        f.last_active, p.root
                    );
                    p.root.clone()
                } else {
                    // NOTHING is reachable (e.g. the one external drive holding every project is
                    // unplugged). Keep the old resolution and let `.setup()` degrade onto its
                    // placeholder index — this is no longer a fatal condition.
                    eprintln!(
                        "[lens] projects: NO registered project is reachable — starting with an \
                         empty placeholder index"
                    );
                    if is_registered {
                        f.last_active.clone()
                    } else {
                        f.projects[0].root.clone()
                    }
                };
                (f.projects, active)
            }
            // No file at all (or unparseable JSON) — a genuine first run. Nothing is written: the
            // registry only ever reaches disk as the result of a real user action (add / remove /
            // switch), so a fresh install leaves no file until the user chooses a folder.
            None => (Vec::new(), String::new()),
        };

        Projects(Mutex::new(ProjectsState { active_root, list, config_path }))
    }

    /// The active project's root — what `abs_of` resolves stored relative paths against. `""` when
    /// no project is open, and ALSO the degraded answer on a poisoned mutex (it used to be one
    /// specific machine's volume, which quietly resolved paths against a folder the user had never
    /// opened). `abs_of` refuses to build a path from an empty root, so the caller fails visibly.
    pub fn active_root(&self) -> String {
        self.0.lock().map(|s| s.active_root.clone()).unwrap_or_default()
    }

    /// The full active [`Project`] record (root + name). `Err` when nothing is active — the callers
    /// that genuinely need a project (the in-app Rebuild) should say so.
    pub fn active_project(&self) -> Result<Project, String> {
        let s = self.0.lock().map_err(|e| format!("projects mutex poisoned: {e}"))?;
        if s.active_root.is_empty() {
            return Err("no project is open".to_string());
        }
        s.list
            .iter()
            .find(|p| p.root == s.active_root)
            .cloned()
            .ok_or_else(|| format!("active project not in registry: {}", s.active_root))
    }

    /// The active project as an `Option` — "nothing is open" is an ordinary answer, not a failure.
    ///
    /// `.setup()` uses THIS one and never `?`s on it. The `Result` sibling's error propagating out
    /// of the setup hook is what bricked the app for three days in Aug 2026: Tauri turns it into a
    /// panic inside `did_finish_launching`, an ObjC callback, so it cannot unwind and becomes
    /// `abort()` — the process died before any window existed, leaving no UI to recover from.
    pub fn active_project_opt(&self) -> Option<Project> {
        let s = self.0.lock().ok()?;
        if s.active_root.is_empty() {
            return None;
        }
        s.list.iter().find(|p| p.root == s.active_root).cloned()
    }

    /// Every registered project with its live on-disk state, for the switcher. NEVER fails — a
    /// poisoned mutex yields an empty list rather than an error the UI would have to render.
    pub fn status_list(&self) -> Vec<ProjectStatus> {
        let Ok(s) = self.0.lock() else {
            return Vec::new();
        };
        s.list
            .iter()
            .map(|p| {
                let index_dir = format!("{}/_repo_index", p.root);
                ProjectStatus {
                    name: p.name.clone(),
                    root: p.root.clone(),
                    is_active: p.root == s.active_root,
                    folder_exists: std::path::Path::new(&p.root).is_dir(),
                    has_index: p.index_reachable(),
                    index_bytes: dir_size(std::path::Path::new(&index_dir)),
                }
            })
            .collect()
    }

    /// A snapshot of every registered project.
    pub fn list(&self) -> Result<Vec<Project>, String> {
        let s = self.0.lock().map_err(|e| format!("projects mutex poisoned: {e}"))?;
        Ok(s.list.clone())
    }

    /// Look up a registered project by root (the lookup key); `Err` if not registered.
    pub fn get(&self, root: &str) -> Result<Project, String> {
        let s = self.0.lock().map_err(|e| format!("projects mutex poisoned: {e}"))?;
        s.list
            .iter()
            .find(|p| p.root == root)
            .cloned()
            .ok_or_else(|| format!("project not registered: {root}"))
    }

    /// Register a new project (validating its `root` IS a directory) and persist. Idempotent: a root
    /// already registered returns the existing record unchanged. Does NOT switch active or index —
    /// the caller indexes (`index_project`) then switches (`switch_project`) explicitly.
    pub fn add(&self, root: String, name: Option<String>) -> Result<Project, String> {
        if !std::path::Path::new(&root).is_dir() {
            return Err(format!("add_project: not a directory: {root}"));
        }
        let mut s = self.0.lock().map_err(|e| format!("projects mutex poisoned: {e}"))?;
        if let Some(existing) = s.list.iter().find(|p| p.root == root) {
            return Ok(existing.clone());
        }
        let proj = Project { name: name.unwrap_or_else(|| basename(&root)), root };
        s.list.push(proj.clone());
        write_projects_file(&s.config_path, &s.list, &s.active_root)?;
        Ok(proj)
    }

    /// Drop a registered project and persist.
    ///
    /// **This used to refuse the ACTIVE project outright**, which meant the only way to forget the
    /// folder you were looking at was to hand-edit `projects.json`. The refusal was guarding a real
    /// hazard — the live engine would have kept its writer lock and kept flushing into a folder the
    /// app no longer listed — so the guard did not disappear, it MOVED to the caller: the
    /// `remove_project` command switches the engine away first (§1.3) and only then calls this.
    ///
    /// The one thing handled here is the persisted pointer: if the removed root was still the
    /// active one, `active_root` is cleared rather than left naming a deregistered project (which
    /// the next boot would have had to guess its way out of).
    pub fn remove(&self, root: &str) -> Result<(), String> {
        let mut s = self.0.lock().map_err(|e| format!("projects mutex poisoned: {e}"))?;
        let before = s.list.len();
        s.list.retain(|p| p.root != root);
        if s.list.len() == before {
            return Err(format!("remove_project: not registered: {root}"));
        }
        if s.active_root == root {
            s.active_root = String::new();
        }
        write_projects_file(&s.config_path, &s.list, &s.active_root)?;
        Ok(())
    }

    /// Park the registry on "no project is open" and persist. Used when the active project is
    /// removed and there is nothing reachable to fall back to — the reader goes to the placeholder
    /// index, the writer lock is released, and this records that there is nothing to reopen.
    pub fn clear_active(&self) -> Result<(), String> {
        let mut s = self.0.lock().map_err(|e| format!("projects mutex poisoned: {e}"))?;
        s.active_root = String::new();
        write_projects_file(&s.config_path, &s.list, &s.active_root)
    }

    /// The registered project to fall back on when the active one is removed: the most recently
    /// added project (registry order is add order) that is NOT `excluding` and whose index is
    /// actually there. `None` ⇒ park on the placeholder.
    pub fn fallback_after_removing(&self, excluding: &str) -> Option<Project> {
        let s = self.0.lock().ok()?;
        s.list.iter().rev().find(|p| p.root != excluding && p.index_reachable()).cloned()
    }

    /// Commit `root` as the active project and persist `last_active`. Validates the root is
    /// registered. Called by `switch_project` AFTER the single `Db` has been repointed + crosslinks
    /// reloaded, so the persisted active selection is only advanced once the swap actually succeeded.
    pub fn set_active(&self, root: &str) -> Result<Project, String> {
        let mut s = self.0.lock().map_err(|e| format!("projects mutex poisoned: {e}"))?;
        let proj = s
            .list
            .iter()
            .find(|p| p.root == root)
            .cloned()
            .ok_or_else(|| format!("project not registered: {root}"))?;
        s.active_root = root.to_string();
        write_projects_file(&s.config_path, &s.list, &s.active_root)?;
        Ok(proj)
    }
}

/// Total bytes of the files under `dir` (0 if it is absent or unreadable). Uses
/// `symlink_metadata`, so a symlink is counted as the link it is and NEVER followed — the number
/// reported to the user must describe the bytes that would actually be freed, and following a link
/// out of the tree would both inflate it and invite the deletion path to wander.
fn dir_size(dir: &std::path::Path) -> u64 {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return 0;
    };
    let mut total = 0u64;
    for entry in entries.flatten() {
        let Ok(meta) = entry.path().symlink_metadata() else {
            continue;
        };
        if meta.is_dir() {
            total = total.saturating_add(dir_size(&entry.path()));
        } else {
            total = total.saturating_add(meta.len());
        }
    }
    total
}

/// Delete `<root>/_repo_index/` and report the bytes freed.
///
/// This is the only code in the app that removes a directory tree the user did not name directly,
/// so the preconditions are stated in full and checked BEFORE anything is touched. All of them must
/// hold, or nothing is deleted:
///
///   * the path ends in exactly one component, named `_repo_index`;
///   * its parent is exactly the `root` that was asked for;
///   * it is a REAL directory — `symlink_metadata`, never followed, so `_repo_index` symlinked to
///     somewhere else deletes nothing;
///   * `root` is not `/`, not `$HOME`, and names at least two path components.
///
/// The last one is the blunt instrument on purpose: a registry entry that has been corrupted down
/// to `/` or a bare home directory must not be able to recruit this function.
pub fn delete_index_dir(root: &str) -> Result<u64, String> {
    use std::path::{Component, Path};

    let root_path = Path::new(root);
    if root.is_empty() || !root_path.is_absolute() {
        return Err(format!("delete index: refusing a non-absolute root: {root}"));
    }
    let comps: Vec<_> =
        root_path.components().filter(|c| matches!(c, Component::Normal(_))).collect();
    if comps.len() < 2 {
        return Err(format!(
            "delete index: refusing a root with fewer than two path components: {root}"
        ));
    }
    if let Ok(home) = std::env::var("HOME") {
        if !home.is_empty() && root_path == Path::new(&home) {
            return Err("delete index: refusing to touch the home directory".to_string());
        }
    }

    let dir = root_path.join("_repo_index");
    if dir.file_name().map(|n| n != "_repo_index").unwrap_or(true) {
        return Err(format!("delete index: not an _repo_index directory: {}", dir.display()));
    }
    if dir.parent() != Some(root_path) {
        return Err(format!("delete index: {} is not directly under {root}", dir.display()));
    }
    let meta = std::fs::symlink_metadata(&dir)
        .map_err(|e| format!("delete index: {} is not readable: {e}", dir.display()))?;
    if meta.file_type().is_symlink() {
        return Err(format!("delete index: {} is a symlink — refusing", dir.display()));
    }
    if !meta.is_dir() {
        return Err(format!("delete index: {} is not a directory", dir.display()));
    }

    // Size FIRST: after `remove_dir_all` there is nothing left to measure, and the number is what
    // the user is told they reclaimed.
    let bytes = dir_size(&dir);
    std::fs::remove_dir_all(&dir)
        .map_err(|e| format!("delete index: could not remove {}: {e}", dir.display()))?;
    Ok(bytes)
}

/// Count `entries` in the index at `index_path` via a TRANSIENT read-only connection that is opened,
/// read, and dropped within this call — it is NEVER retained, so the app still holds exactly ONE
/// RESIDENT connection (the managed [`Db`], which points at the ACTIVE project). Used to report
/// `index_project`'s result when the indexed root is NOT the active project (whose managed `Db` must
/// not be repointed). Reuses the same strictly-read-only open as [`Db::open_at`].
pub fn count_entries_at(index_path: &str) -> Result<i64, String> {
    // RO-first (§3.1): counting a NON-active project needs no write permission. Only if the WAL
    // `-shm` handshake fails on a strictly-read-only handle do we fall back to `connect_reader`
    // (READ_WRITE + query_only), which DOES require write permission on that project's dir.
    if let Ok(conn) = Db::connect_ro(index_path) {
        if let Ok(n) = file_count(&conn) {
            return Ok(n);
        }
    }
    let conn = crate::writer::connect_reader(index_path)?;
    file_count(&conn).map_err(|e| format!("count_entries_at({index_path}): {e}"))
}

/// Count FILE rows (the user-facing "N files"). VERSION-AWARE (§2.9 degrade path): a v2 db counts
/// `WHERE is_dir = 0` (excluding synthesized dir rows); a not-yet-migrated v1 db (all rows are
/// files) counts plainly — so counting a non-active project's stale v1 index never raises
/// `no such column: is_dir`. Used by `count_entries_at` and the four `lib.rs` COUNT sites (§2.8).
pub fn file_count(conn: &Connection) -> Result<i64, String> {
    // Key on the COLUMN, not on `user_version >= USER_VERSION`: the version constant advances
    // with every schema change (v3 added `figure_text`), so a version comparison would send an
    // already-migrated v2 db down the legacy branch and count DIRECTORY rows as files.
    let sql = if crate::tree::column_exists(conn, "entries", "is_dir")? {
        "SELECT COUNT(*) FROM entries WHERE is_dir = 0"
    } else {
        "SELECT COUNT(*) FROM entries"
    };
    conn.query_row(sql, [], |r| r.get::<_, i64>(0))
        .map_err(|e| e.to_string())
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Contract DTOs (serde → the frontend). Field names + types are the FROZEN IPC contract.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// One result row — the `entries` table columns mapped 1:1 (see CONTRACT.md §"column → Row"):
///   id←id · path←path · name←basename(path) · category←category · ext←ext ·
///   size_bytes←size_bytes · mtime←mtime_iso · n_obs←n_obs · n_vars←n_vars ·
///   extractor←extractor · error←error · symlink_ok←symlink_ok (NULL→false) ·
///   is_dir←is_dir (v2: real directory rows exist; `list_children` surfaces them) ·
///   child_count←child_count (populated ONLY by `list_children`; `None` elsewhere).
#[derive(Serialize, Clone, Debug)]
pub struct Row {
    pub id: i64,
    pub path: String,
    pub name: String,
    pub category: String,
    pub ext: String,
    pub size_bytes: i64,
    pub mtime: String,
    pub n_obs: Option<i64>,
    pub n_vars: Option<i64>,
    pub extractor: String,
    pub error: Option<String>,
    pub symlink_ok: bool,
    pub is_dir: bool,
    /// Direct-child count of a directory row — set ONLY by [`list_children`] (a bespoke mapper);
    /// `None` for every other query. Additive serde field (the frontend ignores unknown fields).
    #[serde(default)]
    pub child_count: Option<i64>,
}

/// A page of search results plus the TOTAL match count (for paging UI) — `total` is the count of
/// ALL rows the query matches, independent of `offset`/`limit`.
#[derive(Serialize, Clone, Debug)]
pub struct SearchResult {
    pub rows: Vec<Row>,
    pub total: u32,
}

/// The single-entry inspector payload: the row, the FULL parsed `meta` JSON (the ONLY place
/// `meta` is read), and the resolved lineage (`refs`/`ref_by` from the crosslinks adjacency).
///
/// `dangling_refs` / `dangling_ref_by` are the SUBSET of `refs` / `ref_by` whose target path is
/// NOT an entry in `INDEX.sqlite` (an archived / deleted / out-of-tree file the crosslinks graph
/// still records). The crosslinks adjacency has ~14% of edge targets that are non-entries; the
/// frontend renders these as the disabled `.is-dangling` xref (non-navigable) instead of a live
/// clickable link that would navigate to a 0-match search. Every path in `dangling_refs` also
/// appears in `refs` (likewise `dangling_ref_by ⊆ ref_by`) — the dangling lists are membership
/// markers, not a separate set, so the contract's `refs`/`ref_by` shape is unchanged.
#[derive(Serialize, Clone, Debug)]
pub struct EntryDetail {
    pub row: Row,
    pub meta: Value,
    pub refs: Vec<String>,
    pub ref_by: Vec<String>,
    /// Subset of `refs` whose target path is not an entry (dangling outgoing reference).
    pub dangling_refs: Vec<String>,
    /// Subset of `ref_by` whose source path is not an entry (dangling incoming reference).
    pub dangling_ref_by: Vec<String>,
}

/// One filter-chip facet: a key (a `category` or `ext` value) and how many entries carry it.
#[derive(Serialize, Clone, Debug)]
pub struct Facet {
    pub key: String,
    pub count: u32,
}

/// The two facet families the left-rail filter chips render.
#[derive(Serialize, Clone, Debug)]
pub struct Facets {
    pub categories: Vec<Facet>,
    pub exts: Vec<Facet>,
}

/// Returned by the `reindex` command: the `entries` row count AFTER the index was refreshed (the
/// frontend shows it as the confirmation, e.g. "index refreshed · 38,288 files").
#[derive(Serialize, Clone, Debug)]
pub struct ReindexReport {
    pub entries: i64,
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Row mapping — the ONE place the entries-table columns become a `Row`.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// The exact, ordered SELECT column list every row-returning query uses, so `row_from` can read
/// by fixed index. NOTE: the time column is `mtime_iso` (surfaced as the contract's `mtime` field)
/// and there is NO `name` column (derived in `row_from`). `is_dir` is the LAST column (index 11).
/// Any query that appends columns AFTER this block (e.g. `get_entry`'s `meta`, `list_children`'s
/// `child_count`) starts at index 12.
pub const ROW_COLS: &str =
    "id, path, category, ext, size_bytes, mtime_iso, n_obs, n_vars, extractor, error, symlink_ok, is_dir";

/// `ROW_COLS` with every column table-qualified by `alias` (e.g. `e.id, e.path, …`). REQUIRED for
/// any query that JOINs `entries` with `fts`: both tables expose a `path` column, so an unqualified
/// `SELECT path …` is "ambiguous column name". The plain `ROW_COLS` is fine for single-table
/// queries (`list_page`/`get_entry`/`facets`); the JOINed `search` uses this qualified form.
fn row_cols_qualified(alias: &str) -> String {
    ROW_COLS
        .split(", ")
        .map(|c| format!("{alias}.{c}"))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Derive a display `name` from a path: the final `/`-separated segment (the index stores POSIX
/// relative paths). Falls back to the whole path when there is no separator.
pub fn basename(path: &str) -> String {
    path.rsplit('/').next().unwrap_or(path).to_string()
}

/// Build a `Row` from a SQLite row whose columns are exactly `ROW_COLS` in order. The SINGLE
/// source of truth for the column→`Row` mapping the whole app relies on. Reads by fixed index.
pub fn row_from(r: &rusqlite::Row<'_>) -> rusqlite::Result<Row> {
    let path: String = r.get(1)?;
    Ok(Row {
        id: r.get(0)?,
        name: basename(&path),
        path,
        category: r.get::<_, Option<String>>(2)?.unwrap_or_default(),
        ext: r.get::<_, Option<String>>(3)?.unwrap_or_default(),
        size_bytes: r.get::<_, Option<i64>>(4)?.unwrap_or(0),
        mtime: r.get::<_, Option<String>>(5)?.unwrap_or_default(),
        n_obs: r.get::<_, Option<i64>>(6)?,
        n_vars: r.get::<_, Option<i64>>(7)?,
        extractor: r.get::<_, Option<String>>(8)?.unwrap_or_default(),
        error: r.get::<_, Option<String>>(9)?,
        // symlink_ok is a nullable INTEGER (NULL when not a symlink): NULL/0 → false, 1 → true.
        symlink_ok: r.get::<_, Option<i64>>(10)?.unwrap_or(0) != 0,
        // v2: real directory rows exist. is_dir is the last ROW_COLS column (index 11).
        is_dir: r.get::<_, Option<i64>>(11)?.unwrap_or(0) != 0,
        // Populated ONLY by list_children's bespoke mapper; None for every other query.
        child_count: None,
    })
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// list_page — a pure index scan ordered by a WHITELISTED column. NEVER parses `meta`.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// Map the frontend `sort` token to a whitelisted `ORDER BY` clause. Anything unrecognized falls
/// back to `path ASC` (the stable default). Never interpolates raw input into SQL.
fn order_by_for(sort: &str) -> &'static str {
    match sort {
        "size" => "size_bytes DESC",
        "mtime" => "mtime_iso DESC",
        _ => "path ASC",
    }
}

/// `list_page(offset, limit, sort)` → one page of rows, ordered by the whitelisted `sort`.
/// `limit` is clamped to `MAX_PAGE_LIMIT` so a single payload stays bounded. NO `meta` parse.
pub fn list_page(
    conn: &Connection,
    offset: u32,
    limit: u32,
    sort: &str,
) -> Result<Vec<Row>, String> {
    let order = order_by_for(sort);
    let limit = limit.min(MAX_PAGE_LIMIT);
    // WHERE is_dir = 0: synthesized directory rows never surface through the flat list (§2.8) — only
    // list_children does. Keeps Phase-0 read behavior byte-identical (the frontend buildTree is
    // unchanged; it still receives file-only rows).
    let sql = format!("SELECT {ROW_COLS} FROM entries WHERE is_dir = 0 ORDER BY {order} LIMIT ?1 OFFSET ?2");
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([limit as i64, offset as i64], row_from)
        .map_err(|e| e.to_string())?
        .collect::<rusqlite::Result<Vec<Row>>>()
        .map_err(|e| e.to_string())?;
    Ok(rows)
}

/// `list_all()` → EVERY row, ordered by path — the browse tree's single bulk load. The browse view
/// builds the whole tree client-side from this and renders it lazily (virtualized), so it needs the
/// FULL index, not a page. Deliberately NOT clamped to `MAX_PAGE_LIMIT` (that cap is for the paged
/// `search` view); `BROWSE_ROW_HARD_CAP` is only a pathological-index backstop. `Row` excludes
/// `meta`, so even the full set stays a lean payload. NO `meta` parse.
pub fn list_all(conn: &Connection) -> Result<Vec<Row>, String> {
    // WHERE is_dir = 0 (§2.8): file-only, so the client-side buildTree is unchanged.
    let sql = format!(
        "SELECT {ROW_COLS} FROM entries WHERE is_dir = 0 ORDER BY path ASC LIMIT {BROWSE_ROW_HARD_CAP}"
    );
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], row_from)
        .map_err(|e| e.to_string())?
        .collect::<rusqlite::Result<Vec<Row>>>()
        .map_err(|e| e.to_string())?;
    Ok(rows)
}

/// `list_children(parent_key, limit, offset)` → the v2 lazy tree query (§2.7): the direct children
/// of the directory whose `path_key == parent_key` (`""` = the repo root). Folders first, then
/// case-insensitive by name (`name_key`), with a stable `path` tiebreak. Index-backed by
/// `idx_entries_children(parent_key, is_dir DESC, name_key)`. The ONE query that surfaces dir rows;
/// the ONLY caller that populates `Row.child_count` (from the extra selected column). Bounded by
/// `BROWSE_ROW_HARD_CAP` as a payload backstop (a keyset window is the Phase-2 escape hatch).
pub fn list_children(
    conn: &Connection,
    parent_key: &str,
    limit: u32,
    offset: u32,
) -> Result<Vec<Row>, String> {
    let limit = (limit as i64).min(BROWSE_ROW_HARD_CAP as i64);
    let sql = format!(
        "SELECT {ROW_COLS}, child_count FROM entries WHERE parent_key = ?1
         ORDER BY is_dir DESC, name_key ASC, path ASC LIMIT ?2 OFFSET ?3"
    );
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(rusqlite::params![parent_key, limit, offset as i64], |r| {
            let mut row = row_from(r)?;
            // `child_count` is the column AFTER the 12-wide ROW_COLS block → index 12.
            row.child_count = r.get::<_, Option<i64>>(12)?;
            Ok(row)
        })
        .map_err(|e| e.to_string())?
        .collect::<rusqlite::Result<Vec<Row>>>()
        .map_err(|e| e.to_string())?;
    Ok(rows)
}

/// `count_children(parent_key)` → the number of direct children. Live callers use THIS, not the
/// rebuild-only `child_count` hint column, which incremental writes do not keep consistent (§2.9).
pub fn count_children(conn: &Connection, parent_key: &str) -> Result<i64, String> {
    conn.query_row(
        "SELECT COUNT(*) FROM entries WHERE parent_key = ?1",
        rusqlite::params![parent_key],
        |r| r.get(0),
    )
    .map_err(|e| format!("count_children({parent_key}): {e}"))
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// search — the grammar parser → SUBSTRING (LIKE) match (free / obs: / obsm: / path: / dir:) +
// substring category filter (cat: / type:) + exact ext: filter. They compose with `AND`.
//
// FAITHFUL TO THE FOUNDATION (render_html.py searchFilter): a free token matches iff it is a
// SUBSTRING of the entry's lowercased haystack (`hay.indexOf(tok) !== -1`) — MID-WORD, not just
// token-start. FTS5 MATCH (even `*`-prefixed) only matches whole/leading tokens, so it can't
// reproduce that. We match with SQL LIKE over the SAME haystack the FTS `searchtext` is built from
// (export_sqlite._searchtext): the readable `entries` columns path+category+ext+extractor+tags+
// meta. cat:/type: match category by SUBSTRING too (foundation: `cat:figure`→figure+figure_pdf).
//
// GRAMMAR (CONTRACT §RUST COMMANDS):
//   bare token        → LIKE substring across the whole haystack (path+category+ext+extractor+tags+meta).
//   path:VALUE        → LIKE substring scoped to the `path` column.
//   dir:VALUE         → LIKE substring scoped to the `path` column (the foundation's parent-dir op;
//                       a path-substring is the closest index-free approximation — may also match a
//                       basename, a rare false positive far better than the prior free-text fallback).
//   obs:VALUE         → LIKE substring scoped to the `meta` JSON (obs column *names* live in meta).
//   obsm:VALUE        → same as obs: (obsm keys likewise live in the meta JSON).
//   cat:VALUE / type: → SUBSTRING `LIKE` on the category column (type: is the foundation alias).
//   ext:VALUE         → structured `WHERE entries.ext = VALUE` (exact, like the foundation).
//
// IMPLEMENTATION SHAPE: build (a) a list of `(sql_expr, value)` LIKE predicates (sql_expr is a
// `&'static str` SQL fragment with ONE `?`) and (b) `(column, value)` exact filters from ext:. Then
// every predicate → `AND <sql_expr> LIKE ? ESCAPE '\\'` (ext: → `AND e.ext = ?`), values bound
// positionally; empty query → no predicate → COUNT(*) + scan. No FTS join (LIKE reads `entries`).
// Values bind as `?` params (LIKE wildcards `%`/`_`/`\\` escaped INTO the value → LITERAL match);
// `total` is a COUNT(*) over the SAME predicate (offset/limit-independent).
// ───────────────────────────────────────────────────────────────────────────────────────────

/// The free-token haystack: the SAME surface `export_sqlite._searchtext` (and the foundation's
/// `hayOf`) flatten — path + category + ext + extractor + tags + meta, lowercased. A `&'static`
/// literal (never user input); the user token binds as the LIKE `?`.
///
/// Module-scope (was a `parse_query`-local `const`) so the token-level [`search_ids`] can compose
/// the IDENTICAL predicate without re-parsing a query string. Keep it BYTE-IDENTICAL for both
/// callers: the moment the two haystacks diverge, the top-hits band's tier 3 silently loses recall
/// that `search_all` still has (`search_ids_matches_search_all_ids` asserts set equality).
pub(crate) const HAYSTACK: &str = "LOWER(e.path||' '||COALESCE(e.category,'')||' '||COALESCE(e.ext,'')||' '||COALESCE(e.extractor,'')||' '||COALESCE(e.tags,'')||' '||COALESCE(e.meta,''))";

/// The `cat:`/`type:` scope expression — a SUBSTRING `LIKE` over the category column. Hoisted
/// alongside [`HAYSTACK`] for the same reason, and likewise byte-identical to what `parse_query`
/// pushes (the existing `parse_prefixes_route_correctly` / `parse_type_aliases_cat_and_dir_scopes_path`
/// tests pin the exact string, which is what keeps the two predicate builders honest).
pub(crate) const CAT_EXPR: &str = "LOWER(e.category)";

/// The free-token haystack WITH figure text folded in — used only when the caller opts in
/// (the Lens "figure text" checkbox). Identical to [`HAYSTACK`] plus `e.figure_text`, so the
/// opt-in is a strict SUPERSET: every default hit is still a hit.
///
/// Figure text lives in its OWN column precisely so this is a deliberate choice rather than an
/// ambient cost — `export_sqlite._split_figure_text` keeps it out of `meta`, which is why
/// [`HAYSTACK`] cannot see it and the opt-out path stays byte-identical to the pre-feature build.
pub(crate) const HAYSTACK_WITH_FIGURE_TEXT: &str = "LOWER(e.path||' '||COALESCE(e.category,'')||' '||COALESCE(e.ext,'')||' '||COALESCE(e.extractor,'')||' '||COALESCE(e.tags,'')||' '||COALESCE(e.meta,'')||' '||COALESCE(e.figure_text,''))";

/// The `fig:` scope expression — a SUBSTRING `LIKE` over the figure-text column ALONE. Available
/// regardless of the checkbox, so an agent (or a user who knows the grammar) can ask the precise
/// question "which figure has this rendered in it?" without widening the whole query.
pub(crate) const FIG_EXPR: &str = "LOWER(COALESCE(e.figure_text,''))";

/// A parsed search query: the LIKE-substring predicates (free tokens / path: / dir: / obs: / obsm:
/// / cat: / type:) plus the structured equality filter drawn from `ext:`.
#[derive(Default, Debug, PartialEq)]
struct ParsedQuery {
    /// `(sql_expr, value)` substring predicates. `sql_expr` is a whitelisted `&'static str` SQL
    /// fragment (a `LOWER(...)` over `entries` columns) spliced in front of `LIKE ? ESCAPE '\\'`;
    /// the (already lowercased + LIKE-escaped) `value` binds as the `?`. Sourced from free tokens
    /// (whole haystack), `path:`/`dir:` (path), `obs:`/`obsm:` (meta) and `cat:`/`type:` (category).
    like_filters: Vec<(&'static str, String)>,
    /// `(column, value)` exact-match filters on `entries` (column is a whitelisted literal; the
    /// value binds as a parameter). Sourced from `ext:` (ext — an exact match, like the foundation).
    eq_filters: Vec<(&'static str, String)>,
}

/// Build the bound value for a `… LIKE ? ESCAPE '\\'` substring match of `token`: lowercase it (the
/// haystack columns are matched via `LOWER(...)`), escape the three LIKE-special chars (`\\` first,
/// then `%` and `_`) with a backslash so they match LITERALLY (the foundation does a literal
/// `indexOf` — an unescaped `_` would otherwise act as a single-char wildcard, e.g. `cell_type`
/// falsely matching `cellXtype`), then wrap in `%…%` so the match is a SUBSTRING anywhere in the
/// column. The result binds as a `?` parameter, never spliced into SQL text — injection-safe.
///
/// The fold is `to_ascii_lowercase`, NOT full-Unicode `to_lowercase`, ON PURPOSE: SQLite's built-in
/// `LOWER()` (no ICU) folds ASCII A–Z only, so a full-Unicode fold here would case-fold the bind
/// asymmetrically vs the column (`δ` from `to_lowercase("Δ")` could never match a column whose `Δ`
/// SQLite leaves uppercase). ASCII-folding both sides keeps the two consistent (no silent misses).
fn like_substring(token: &str) -> String {
    let escaped = token
        .to_ascii_lowercase()
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_");
    format!("%{escaped}%")
}

/// Split a raw query into whitespace-separated terms, but keep a `prefix:"quoted value"` or
/// `"quoted value"` group intact (so a user can search a phrase containing spaces). Returns the
/// list of raw terms (prefix still attached, surrounding quotes on the *value* stripped).
fn split_terms(query: &str) -> Vec<String> {
    let mut terms: Vec<String> = Vec::new();
    let mut cur = String::new();
    let mut in_quotes = false;
    for ch in query.chars() {
        match ch {
            '"' => {
                in_quotes = !in_quotes; // a quote toggles "inside a phrase"; the quote itself is
                                        // dropped (the value is rebuilt without quote chars).
            }
            c if c.is_whitespace() && !in_quotes => {
                if !cur.is_empty() {
                    terms.push(std::mem::take(&mut cur));
                }
            }
            c => cur.push(c),
        }
    }
    if !cur.is_empty() {
        terms.push(cur);
    }
    terms
}

/// Parse the search grammar into a `ParsedQuery`. Recognizes the `cat:` / `ext:` / `path:` /
/// `obs:` / `obsm:` prefixes (case-insensitive prefix, value kept verbatim); everything else is a
/// free token. Empty / whitespace-only values after a prefix are ignored (e.g. a lone `cat:`).
fn parse_query(query: &str, include_figure_text: bool) -> ParsedQuery {
    let mut like_filters: Vec<(&'static str, String)> = Vec::new();
    let mut eq_filters: Vec<(&'static str, String)> = Vec::new();

    // `HAYSTACK` / `CAT_EXPR` are now module-scope consts (shared verbatim with `search_ids`).

    for term in split_terms(query) {
        // Recognize a `prefix:value` term. Split on the FIRST ':' so values can themselves contain
        // ':'. The prefix is matched case-insensitively and mapped to a `&'static str` literal;
        // the value keeps its original casing (paths/obs names can be mixed-case, and FTS5
        // unicode61 lowercases on its own anyway). An UNrecognized prefix → the whole term is a
        // free token (don't silently drop a value because of an incidental ':').
        let (prefix, value): (&'static str, &str) = match term.split_once(':') {
            Some((p, v)) => match p.to_ascii_lowercase().as_str() {
                // `type:` is the foundation's alias for `cat:` — fold it in here.
                "cat" | "type" => ("cat", v),
                "ext" => ("ext", v),
                "path" => ("path", v),
                "dir" => ("dir", v),
                "obs" => ("obs", v),
                "obsm" => ("obsm", v),
                "fig" => ("fig", v),
                _ => ("", term.as_str()),
            },
            None => ("", term.as_str()),
        };

        let value = value.trim();
        // A bare prefix with no value (`cat:`) contributes nothing.
        if matches!(prefix, "cat" | "ext" | "path" | "dir" | "obs" | "obsm" | "fig")
            && value.is_empty()
        {
            continue;
        }

        match prefix {
            // cat:/type: → SUBSTRING on the category column (foundation: `cat:data` matches
            // data_matrix + data_table, `cat:figure` matches figure + figure_pdf).
            "cat" => like_filters.push((CAT_EXPR, like_substring(value))),
            // ext: stays an EXACT match on the real column (precise + index-backed). Lowercase the
            // bind: stored exts are lowercase, and every OTHER filter is case-insensitive (via
            // like_substring) — without this, `ext:PNG` would silently return zero (the only
            // case-SENSITIVE filter). ASCII fold matches SQLite's ASCII-only collation.
            "ext" => eq_filters.push(("ext", value.to_ascii_lowercase())),
            // path:/dir: → substring scoped to the `path` column. (dir: is the foundation's
            // parent-dir op; a path-substring is the closest index-free approximation.)
            "path" | "dir" => like_filters.push(("LOWER(e.path)", like_substring(value))),
            // obs:/obsm: → the names/keys live in the `meta` JSON, so a substring match against the
            // meta column is the faithful translation (the foundation scopes obs to obs_columns).
            "obs" | "obsm" => like_filters.push(("LOWER(COALESCE(e.meta,''))", like_substring(value))),
            // fig: → the rendered-text column ALONE (never falls back to path/meta).
            "fig" => like_filters.push((FIG_EXPR, like_substring(value))),
            // Free token → whole-haystack substring (mirrors the foundation's hayOf indexOf).
            _ => {
                let v = term.trim();
                if !v.is_empty() {
                    let hay = if include_figure_text {
                        HAYSTACK_WITH_FIGURE_TEXT
                    } else {
                        HAYSTACK
                    };
                    like_filters.push((hay, like_substring(v)));
                }
            }
        }
    }

    ParsedQuery {
        // Every predicate AND-composed in `search` (the intuitive "narrow as you add words").
        like_filters,
        eq_filters,
    }
}

/// Build the shared `WHERE` clause + ordered bind-parameter list for a parsed query — the ONE
/// predicate builder used by BOTH the paged [`search`] and the unclamped [`search_all`], so the two
/// match identically (only their LIMIT differs). Returns `(where_sql, params)` where `where_sql` is
/// either empty (no predicate) or `" WHERE …"`, and `params` are the bound values in the exact
/// positional order the clause references them.
///
/// Composition:
///   * LIKE substring predicates (free / path: / dir: / obs: / obsm: / cat: / type:) → each
///     `<expr> LIKE ? ESCAPE '\\'`, AND-ed together (narrow as you add words).
///   * Equality filters (ext:) are GROUPED BY COLUMN into a single `e.<col> IN (?,?,…)`: repeated
///     values for the SAME column are a UNION (a row has exactly one ext, so `ext = a AND ext = b`
///     is unsatisfiable — selecting two type chips would always return zero). Different columns
///     still AND. First-seen column order is preserved for deterministic SQL.
///
/// There is no FTS join — LIKE reads the `entries` columns directly (the contentless FTS table can't
/// be read by LIKE; `entries` reconstructs the identical haystack).
fn build_where(parsed: &ParsedQuery) -> (String, Vec<rusqlite::types::Value>) {
    let mut params: Vec<rusqlite::types::Value> = Vec::new();
    let mut where_clauses: Vec<String> = Vec::new();

    // Base clause (§2.8): synthesized directory rows never surface through search. Prepending it
    // means `where_sql` is NEVER empty, so the bare-operator guard in the callers checks the PARSE
    // (both filter lists empty), not `where_sql.is_empty()`.
    where_clauses.push("e.is_dir = 0".to_string());

    // Substring predicates → `<sql_expr> LIKE ? ESCAPE '\\'`. `sql_expr` is a whitelisted `&'static
    // str` literal (never user input); the already lowercased + LIKE-escaped value binds positionally.
    for (expr, val) in &parsed.like_filters {
        where_clauses.push(format!("{expr} LIKE ? ESCAPE '\\'"));
        params.push(rusqlite::types::Value::Text(val.clone()));
    }

    // Equality filters grouped by column → one `e.<col> IN (?,?,…)` per distinct column (OR within
    // a column, AND across columns), preserving first-seen column order.
    let mut eq_cols: Vec<&'static str> = Vec::new();
    let mut eq_by_col: HashMap<&'static str, Vec<String>> = HashMap::new();
    for (col, val) in &parsed.eq_filters {
        if !eq_by_col.contains_key(col) {
            eq_cols.push(col);
        }
        eq_by_col.entry(col).or_default().push(val.clone());
    }
    for col in eq_cols {
        let vals = &eq_by_col[col];
        let placeholders = std::iter::repeat("?").take(vals.len()).collect::<Vec<_>>().join(",");
        where_clauses.push(format!("e.{col} IN ({placeholders})"));
        for v in vals {
            params.push(rusqlite::types::Value::Text(v.clone()));
        }
    }

    let where_sql = if where_clauses.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", where_clauses.join(" AND "))
    };
    (where_sql, params)
}

/// `search(query, offset, limit)` → a page of matching rows + the total match count.
///
/// Builds the SQL from the parse (which columns / whether an FTS join is needed) and binds every
/// value as a parameter. `total` is a `COUNT(*)` over the identical predicate, so paging UI sees
/// the full match count regardless of the page window. `limit` is clamped to `MAX_PAGE_LIMIT`.
///
/// NOTE: this is the PAGED command (capped at `MAX_PAGE_LIMIT`). The grouped-search UI must render
/// the COMPLETE match set, so it calls [`search_all`] instead; this stays for windowed callers
/// (e.g. the lineage `path:"…"` lookup) and the dev self-check.
///
/// `include_figure_text` folds the SVG `figure_text` column into the free-token haystack (the Lens
/// "figure text" checkbox). `false` reproduces the pre-feature SQL exactly, which is what makes the
/// opt-out path a guaranteed no-regression rather than a benchmarked one.
pub fn search(
    conn: &Connection,
    query: &str,
    offset: u32,
    limit: u32,
    include_figure_text: bool,
) -> Result<SearchResult, String> {
    let limit = limit.min(MAX_PAGE_LIMIT);
    let parsed = parse_query(query, include_figure_text);
    let (where_sql, params) = build_where(&parsed);

    // A non-empty query that parses to ZERO user predicates (e.g. a bare `cat:`/`ext:` with no
    // value) must NOT silently match the whole index. The base `e.is_dir = 0` clause makes
    // `where_sql` never empty, so we check the PARSE, not `where_sql`. A truly empty/whitespace
    // query keeps the no-predicate "everything" result (the browse pane owns the real empty state).
    if parsed.like_filters.is_empty() && parsed.eq_filters.is_empty() && !query.trim().is_empty() {
        return Ok(SearchResult { rows: Vec::new(), total: 0 });
    }

    let from = "entries e";

    // ---- total (offset/limit-independent COUNT over the same predicate) ----
    let count_sql = format!("SELECT COUNT(*) FROM {from}{where_sql}");
    let total: i64 = {
        let mut stmt = conn.prepare(&count_sql).map_err(|e| e.to_string())?;
        stmt.query_row(rusqlite::params_from_iter(params.iter()), |r| r.get(0))
            .map_err(|e| format!("search count failed: {e} (sql: {count_sql})"))?
    };

    // ---- page of rows ----
    // FTS results have no inherent stable order across calls; order by path for determinism.
    // Columns are `e.`-qualified: when `from` JOINs `fts`, an unqualified `path` is ambiguous
    // (both `entries` and `fts` have one). `row_from` reads by position, so order is preserved.
    let cols = row_cols_qualified("e");
    let page_sql =
        format!("SELECT {cols} FROM {from}{where_sql} ORDER BY e.path ASC LIMIT ? OFFSET ?");
    let mut page_params = params.clone();
    page_params.push(rusqlite::types::Value::Integer(limit as i64));
    page_params.push(rusqlite::types::Value::Integer(offset as i64));

    let mut stmt = conn.prepare(&page_sql).map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(page_params.iter()), row_from)
        .map_err(|e| format!("search page failed: {e} (sql: {page_sql})"))?
        .collect::<rusqlite::Result<Vec<Row>>>()
        .map_err(|e| e.to_string())?;

    Ok(SearchResult { rows, total: total.max(0) as u32 })
}

/// `search_all(query)` → EVERY matching row (NOT page-capped) + the total. The grouped-search view
/// needs the COMPLETE match set to render losslessly: the paged [`search`] clamps to
/// `MAX_PAGE_LIMIT` (500) and orders by `path ASC`, so on the real 43k-row index a common query
/// (e.g. `csv` ≈ 10k matches, `ext:png` ≈ 5k) rendered only the ALPHABETICALLY-FIRST 500 and
/// silently dropped the rest — the search analogue of the `list_page`→`list_all` truncation bug.
///
/// Same predicate as [`search`] (so `meta`/`obs:`/`obsm:` matching is identical — the frontend's
/// in-memory `Row[]` carries no `meta`, so this MUST run the predicate server-side, not client-side).
/// Bounded only by `BROWSE_ROW_HARD_CAP` (≫ the whole index — a pathological-payload backstop, not a
/// real cap); a single query can match at most the entire index. `Row` excludes `meta`, so even a
/// 40k-row match set stays a lean payload (same class as `list_all`). `total == rows.len()` here, so
/// the headline count and the rendered rows finally agree. NO `meta` parse.
///
/// `include_figure_text` folds the SVG `figure_text` column into the free-token haystack (the Lens
/// "figure text" checkbox). `false` reproduces the pre-feature SQL exactly, which is what makes the
/// opt-out path a guaranteed no-regression rather than a benchmarked one.
pub fn search_all(
    conn: &Connection,
    query: &str,
    include_figure_text: bool,
) -> Result<SearchResult, String> {
    let parsed = parse_query(query, include_figure_text);
    let (where_sql, params) = build_where(&parsed);

    // Bare-operator guard (see `search`): a non-empty query with no resolvable predicate → empty.
    // (The base `e.is_dir = 0` clause makes `where_sql` never empty, so check the parse.)
    if parsed.like_filters.is_empty() && parsed.eq_filters.is_empty() && !query.trim().is_empty() {
        return Ok(SearchResult { rows: Vec::new(), total: 0 });
    }

    let cols = row_cols_qualified("e");
    let sql = format!(
        "SELECT {cols} FROM entries e{where_sql} ORDER BY e.path ASC LIMIT {BROWSE_ROW_HARD_CAP}"
    );
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(params.iter()), row_from)
        .map_err(|e| format!("search_all failed: {e} (sql: {sql})"))?
        .collect::<rusqlite::Result<Vec<Row>>>()
        .map_err(|e| e.to_string())?;
    let total = rows.len() as u32;
    Ok(SearchResult { rows, total })
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// search_ids — tier-3 support for the top-hits band. IDS + PATHS only; NEVER parses `meta`.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// The `search_ids` payload element: a matching row's `id` and its `path`, nothing else.
///
/// PATH is carried alongside the id ON PURPOSE. `entries.id` is a SQLite rowid and CHURNS across a
/// reindex/reconcile, so a frontend that cached ids from an earlier load would join a stale id onto
/// the wrong file. `path` is uniquely indexed and stable, so the frontend joins on it and treats
/// `id` as a convenience only. Deliberately NOT a [`Row`]: this is the band's tier-3 candidate set,
/// and the frontend already holds every `Row` client-side.
#[derive(Serialize, Clone, Debug)]
pub struct IdPath {
    pub id: i64,
    pub path: String,
    /// True when EVERY free token also appears in this row's `figure_text` — i.e. the row would
    /// have been found by searching the figure's rendered text alone. Drives the UI's "figure
    /// text" badge: a figure that matched on text the user cannot see on screen reads as an
    /// arbitrary result unless the match is attributed. Always `false` when the caller did not
    /// opt in, so the badge can never appear on a default search.
    pub via_figure_text: bool,
}

/// `search_ids(tokens, exts, cats)` → `[{id, path}]` for the top-hits band's tier 3.
///
/// Takes ALREADY tokenized, NFD-normalized, ASCII-folded tokens from `src/query.ts` — this function
/// does NO parsing, so the frontend and the backend can never disagree about what a token is (the
/// two tokenizers used to differ on quoted phrases: TS split `"cell type"` into two tokens, Rust's
/// [`split_terms`] kept it as one). A quoted phrase arrives here as ONE element of `tokens`.
///
/// The predicate is BYTE-IDENTICAL to [`search_all`]'s: free tokens → [`HAYSTACK`] substring,
/// `cats` → [`CAT_EXPR`] substring, `exts` → exact equality grouped by [`build_where`] into
/// `e.ext IN (?,?)` (so two type chips UNION rather than annihilate — the B2 fix, inherited for
/// free). Tier 3 is defined frontend-side as "returned minus the rows tiers 0–2 already matched",
/// so scoping this query to `meta`/`tags` would silently drop recall the tree cannot recover; it
/// intentionally searches the whole haystack instead.
///
/// Empty `tokens` → empty result, even when `exts`/`cats` are present: with no free text there is
/// no tier 3 to compute (a pure filter is answered entirely client-side, no IPC).
///
/// MEMORY invariant (CONTRACT.md): `meta` is *matched* by the SQL predicate but never SELECTed and
/// never parsed — the payload is two scalar columns per row. `is_dir = 0` comes from
/// [`build_where`]'s base clause, so a synthesized directory row can never enter the frontend join.
///
/// `include_figure_text` folds the SVG `figure_text` column into the free-token haystack (the Lens
/// "figure text" checkbox). `false` reproduces the pre-feature SQL exactly, which is what makes the
/// opt-out path a guaranteed no-regression rather than a benchmarked one.
pub fn search_ids(
    conn: &Connection,
    tokens: &[String],
    exts: &[String],
    cats: &[String],
    include_figure_text: bool,
) -> Result<Vec<IdPath>, String> {
    if tokens.is_empty() {
        return Ok(Vec::new()); // no free text → no tier 3
    }

    // Compose the same `ParsedQuery` the grammar parser would have produced, but from pre-split
    // values: `like_substring` still lowercases + LIKE-escapes + `%…%`-wraps each bind (idempotent
    // on an already-folded token), and every value binds as a `?` — never spliced into SQL text.
    let mut parsed = ParsedQuery::default();
    let hay = if include_figure_text {
        HAYSTACK_WITH_FIGURE_TEXT
    } else {
        HAYSTACK
    };
    for t in tokens {
        parsed.like_filters.push((hay, like_substring(t)));
    }
    for c in cats {
        parsed.like_filters.push((CAT_EXPR, like_substring(c)));
    }
    for e in exts {
        parsed.eq_filters.push(("ext", e.to_ascii_lowercase()));
    }

    let (where_sql, params) = build_where(&parsed); // supplies the base `e.is_dir = 0`

    // Match provenance, computed in the SAME query rather than a second round-trip: 1 iff every
    // token is also present in this row's figure text. Skipped entirely (a literal 0) when the
    // caller did not opt in, so the default path binds and evaluates exactly what it did before.
    let (fig_expr, fig_params): (String, Vec<rusqlite::types::Value>) = if include_figure_text {
        let conds: Vec<String> = tokens
            .iter()
            .map(|_| format!("{FIG_EXPR} LIKE ? ESCAPE '\\'"))
            .collect();
        (
            format!("CASE WHEN {} THEN 1 ELSE 0 END", conds.join(" AND ")),
            tokens
                .iter()
                .map(|t| rusqlite::types::Value::Text(like_substring(t)))
                .collect(),
        )
    } else {
        ("0".to_string(), Vec::new())
    };

    let sql = format!(
        "SELECT e.id, e.path, {fig_expr} FROM entries e{where_sql} \
         ORDER BY e.path ASC LIMIT {BROWSE_ROW_HARD_CAP}"
    );
    // The SELECT-list binds precede the WHERE binds positionally.
    let mut all_params = fig_params;
    all_params.extend(params);
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let out = stmt
        .query_map(rusqlite::params_from_iter(all_params.iter()), |r| {
            Ok(IdPath {
                id: r.get(0)?,
                path: r.get(1)?,
                via_figure_text: r.get::<_, i64>(2)? != 0,
            })
        })
        .map_err(|e| format!("search_ids failed: {e} (sql: {sql})"))?
        .collect::<rusqlite::Result<Vec<IdPath>>>()
        .map_err(|e| e.to_string())?;
    Ok(out)
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// get_entry — the row + the FULL `meta` JSON (the ONLY place meta is parsed) + lineage.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// Of the given candidate `paths`, return the subset that is NOT a `path` in `entries` (i.e. the
/// dangling lineage targets — archived / deleted / out-of-tree files the crosslinks graph still
/// records). Implemented as ONE index-backed `WHERE path IN (…)` over the (small) candidate set —
/// it touches only the handful of paths a single entry references, never the whole table, so the
/// memory invariant holds. Returns the candidates that the `IN` query did NOT find as existing.
fn dangling_among(conn: &Connection, paths: &[String]) -> Result<Vec<String>, String> {
    if paths.is_empty() {
        return Ok(Vec::new());
    }
    // `?,?,…` placeholders for the candidate set (count is bounded by an entry's edge fan-out).
    let placeholders = std::iter::repeat("?").take(paths.len()).collect::<Vec<_>>().join(",");
    let sql = format!("SELECT path FROM entries WHERE path IN ({placeholders})");
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let existing = stmt
        .query_map(rusqlite::params_from_iter(paths.iter()), |r| r.get::<_, String>(0))
        .map_err(|e| e.to_string())?
        .collect::<rusqlite::Result<std::collections::HashSet<String>>>()
        .map_err(|e| e.to_string())?;
    // A candidate is dangling iff it is NOT in the set of paths the table actually has.
    Ok(paths.iter().filter(|p| !existing.contains(*p)).cloned().collect())
}

/// `get_entry(id)` → `EntryDetail{row, meta, refs, ref_by, dangling_refs, dangling_ref_by}`. Reads
/// the row plus the FULL `meta` JSON for ONE entry (the ONLY place `meta` is parsed), then resolves
/// lineage from the crosslinks adjacency by the entry's PATH (refs = outgoing, ref_by = incoming)
/// and flags which of those targets are NOT entries (dangling) via [`dangling_among`]. A NULL /
/// garbled `meta` degrades to JSON `null` (no hard error).
pub fn get_entry(
    conn: &Connection,
    crosslinks: &Crosslinks,
    id: i64,
) -> Result<EntryDetail, String> {
    let sql = format!("SELECT {ROW_COLS}, meta FROM entries WHERE id = ?1");
    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
    let (row, meta_str) = stmt
        .query_row([id], |r| {
            let row = row_from(r)?;
            // `meta` is the column AFTER the (now 12-wide) ROW_COLS block → index 12.
            let meta_str: Option<String> = r.get(12)?;
            Ok((row, meta_str))
        })
        .map_err(|e| format!("get_entry({id}): {e}"))?;

    let meta: Value = meta_str
        .as_deref()
        .and_then(|s| serde_json::from_str(s).ok())
        .unwrap_or(Value::Null);

    let refs = crosslinks.refs_of(&row.path);
    let ref_by = crosslinks.ref_by_of(&row.path);
    // Split lineage into resolvable vs dangling: a target not present as an entry is dangling, so
    // the frontend renders it as the disabled `.is-dangling` xref rather than a live (0-match) link.
    let dangling_refs = dangling_among(conn, &refs)?;
    let dangling_ref_by = dangling_among(conn, &ref_by)?;
    Ok(EntryDetail { row, meta, refs, ref_by, dangling_refs, dangling_ref_by })
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// facets — GROUP BY category / ext over the whole table (an index scan; never parses meta).
// ───────────────────────────────────────────────────────────────────────────────────────────

/// `facets()` → the filter-chip counts: `GROUP BY category` and `GROUP BY ext` over the whole
/// table, each ordered by descending count. NULL / empty keys are excluded. NO `meta` parse.
pub fn facets(conn: &Connection) -> Result<Facets, String> {
    fn group_count(conn: &Connection, col: &str) -> Result<Vec<Facet>, String> {
        // `col` is a hardcoded literal ("category"/"ext") — never user input — so interpolating
        // it into the SQL text is safe here.
        // is_dir = 0 (§2.8): no phantom `dir` category chip in the facet rail.
        let sql = format!(
            "SELECT {col} AS k, COUNT(*) AS c FROM entries \
             WHERE is_dir = 0 AND k IS NOT NULL AND k <> '' GROUP BY k ORDER BY c DESC, k ASC"
        );
        let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;
        let v = stmt
            .query_map([], |r| {
                Ok(Facet { key: r.get::<_, String>(0)?, count: r.get::<_, i64>(1)? as u32 })
            })
            .map_err(|e| e.to_string())?
            .collect::<rusqlite::Result<Vec<Facet>>>()
            .map_err(|e| e.to_string())?;
        Ok(v)
    }
    let categories = group_count(conn, "category")?;
    let exts = group_count(conn, "ext")?;
    Ok(Facets { categories, exts })
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Path helpers + the path-action commands (reveal_in_finder / copy_path). These do not touch the
// DB — the frontend passes the entry's stored (repo-relative POSIX) path.
// ───────────────────────────────────────────────────────────────────────────────────────────

/// Resolve a stored (repo-relative POSIX) path to an absolute path under the ACTIVE project's
/// `root` (threaded in by the caller from the managed [`Projects`] state — root-aware so the same
/// helper serves whichever project is resident). An already-absolute path (`/…`) is returned
/// unchanged regardless of `root`.
///
/// An EMPTY root (no project open, or a poisoned registry mutex) yields an EMPTY string, never
/// `"/{path}"`. That difference matters: `format!("{root}/{path}")` on an empty root produces a
/// path rooted at `/` that could name a real file, so a "nothing is open" state would quietly read
/// or reveal the wrong thing. An empty path fails loudly in every caller instead.
pub fn abs_of(root: &str, path: &str) -> String {
    if path.starts_with('/') {
        path.to_string()
    } else if root.is_empty() {
        String::new()
    } else {
        format!("{root}/{path}")
    }
}

/// Minimal percent-encoding for a `file://` URI body: encode space + a few reserved chars so a
/// path containing spaces (this project's paths do) yields a valid, copy-paste-able file URL. NOT
/// a full RFC 3986 encoder — only what a `file://` clipboard target needs.
pub fn encode_path_for_uri(abs: &str) -> String {
    let mut out = String::with_capacity(abs.len());
    for ch in abs.chars() {
        match ch {
            ' ' => out.push_str("%20"),
            '#' => out.push_str("%23"),
            '?' => out.push_str("%3F"),
            '%' => out.push_str("%25"),
            c => out.push(c),
        }
    }
    out
}

/// `reveal_in_finder(root, path)` → reveal the absolute path in Finder via `open -R`. The frontend
/// passes the entry's stored repo-relative path; it is resolved against the ACTIVE project's `root`
/// (threaded in by the command shim from the managed [`Projects`] state) first.
pub fn reveal_in_finder(root: &str, path: &str) -> Result<(), String> {
    let abs = abs_of(root, path);
    if abs.is_empty() {
        return Err("reveal_in_finder: no project is open".to_string());
    }
    std::process::Command::new("open")
        .arg("-R")
        .arg(&abs)
        .status()
        .map_err(|e| format!("reveal_in_finder: failed to spawn `open -R`: {e}"))
        .and_then(|s| if s.success() { Ok(()) } else { Err(format!("open -R exited: {s}")) })
}

/// `open_file(root, path)` → open the file in its default application via `open` (no `-R`, which
/// would only reveal it in Finder). Same path handling as [`reveal_in_finder`]: the frontend passes
/// the entry's stored repo-relative path, resolved against the ACTIVE project's `root` first.
pub fn open_file(root: &str, path: &str) -> Result<(), String> {
    let abs = abs_of(root, path);
    if abs.is_empty() {
        return Err("open_file: no project is open".to_string());
    }
    std::process::Command::new("open")
        .arg(&abs)
        .status()
        .map_err(|e| format!("open_file: failed to spawn `open`: {e}"))
        .and_then(|s| if s.success() { Ok(()) } else { Err(format!("open exited: {s}")) })
}

/// Run the canonical `repo_index export-sqlite` indexer as a subprocess to bring `INDEX.sqlite`
/// current with the on-disk repo (one command = incremental freshen + atomic SQLite rewrite,
/// ~2.6s on this repo), STREAMING the indexer's stderr phase log line-by-line to `on_line` as it
/// runs. The app itself NEVER writes the DB — it delegates to the Python indexer, so the SQLite
/// stays byte-identical to the terminal pipeline (zero divergence). On success the caller MUST
/// [`Db::reopen_at`] (the file was atomically replaced). Blocking (fs walk + subprocess); the
/// `reindex` / `index_project` commands run it on the blocking pool so the UI stays responsive.
///
/// stderr is PIPED and read line-by-line (vs. a buffered `.output()`) so the indexer's phase lines
/// ("[repo_index] walking …", "… assembling manifest …", "… wrote …INDEX.sqlite") reach the caller
/// live — `db.rs` stays the single owner of the subprocess command; `lib.rs` only supplies the
/// `on_line` callback (which emits the Tauri `index-progress` event per line). stdout is silenced
/// (the indexer's progress is all on STDERR); on a non-zero exit the LAST non-empty stderr line
/// becomes the error tail.
///
/// The interpreter is resolved to an ABSOLUTE path by [`crate::runtime::python_bin`] because a
/// bundled `.app` does not inherit the shell PATH; `PYTHONPATH` points at the parent of the
/// BUNDLED `repo_index` package ([`crate::runtime::pythonpath`]) so `-m repo_index` resolves
/// without the crawler being installed in whichever interpreter won.
///
/// Parameterized over `root` (the project to (re)index) — the `reindex` command passes the ACTIVE
/// project's root, and `index_project` passes an arbitrary one. The indexer writes
/// `<root>/_repo_index/{INDEX.sqlite,crosslinks.json}` (its fixed convention).
pub fn run_reindex_streamed(root: &str, mut on_line: impl FnMut(&str)) -> Result<(), String> {
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;

    let python = crate::runtime::python_bin()?;
    let pkg_parent = crate::runtime::pythonpath_required()?;
    let mut child = std::process::Command::new(&python)
        .args(["-m", "repo_index", "export-sqlite", "--root", root])
        .current_dir(root)
        .env("PYTHONPATH", &pkg_parent)
        .stdout(Stdio::null()) // phase lines are on STDERR; nothing useful on stdout
        .stderr(Stdio::piped()) // the change vs. run_reindex's `.output()` — stream, don't buffer
        .spawn()
        .map_err(|e| {
            format!(
                "reindex: failed to spawn `{python} -m repo_index export-sqlite`: {e} \
                 (set LENS_PYTHON to a python that can import repo_index)"
            )
        })?;

    // Drain stderr line-by-line, emitting each phase line as it arrives and retaining the last
    // non-empty line for a useful error tail on a non-zero exit.
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "reindex: child stderr unavailable".to_string())?;
    let mut last = String::from("(no stderr)");
    for line in BufReader::new(stderr).lines() {
        let line = line.map_err(|e| format!("reindex: reading stderr: {e}"))?;
        on_line(&line);
        if !line.trim().is_empty() {
            last = line;
        }
    }

    let status = child.wait().map_err(|e| format!("reindex: wait failed: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("reindex: indexer exited with {status} — {}", last.trim()))
    }
}

/// Run the Python DEFAULT build (`repo_index --root R --out <out_dir>`) which emits the frozen
/// `INDEX.json`/`INDEX.jsonl` manifest — and does NOT write `INDEX.sqlite` (that is `export-sqlite`'s
/// opt-in job, CONTRACTS §8), so it never touches the live WAL db and never contends the writer lock.
/// This is the Python half of the in-app Rebuild (§3.6 Path A): the Rust writer then ingests the
/// fresh JSONL. Streams stderr phase lines via `on_line`.
pub fn run_manifest_streamed(
    root: &str,
    out_dir: &str,
    mut on_line: impl FnMut(&str),
) -> Result<(), String> {
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;

    let python = crate::runtime::python_bin()?;
    let pkg_parent = crate::runtime::pythonpath_required()?;
    let mut child = std::process::Command::new(&python)
        .args(["-m", "repo_index", "--root", root, "--out", out_dir])
        .current_dir(root)
        .env("PYTHONPATH", &pkg_parent)
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("rebuild: failed to spawn `{python} -m repo_index`: {e} (set LENS_PYTHON)"))?;

    let stderr = child.stderr.take().ok_or("rebuild: child stderr unavailable")?;
    let mut last = String::from("(no stderr)");
    for line in BufReader::new(stderr).lines() {
        let line = line.map_err(|e| format!("rebuild: reading stderr: {e}"))?;
        on_line(&line);
        if !line.trim().is_empty() {
            last = line;
        }
    }
    let status = child.wait().map_err(|e| format!("rebuild: wait failed: {e}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("rebuild: manifest build exited with {status} — {}", last.trim()))
    }
}

/// `copy_path(root, path, kind)` → the requested string form. `kind ∈ {abs|rel|posix|file_uri}`.
/// The frontend writes the returned string to the clipboard. `abs` / `file_uri` resolve against the
/// ACTIVE project's `root` (threaded in by the command shim); `rel` and `posix` both return the
/// stored path unchanged (it is already repo-relative POSIX), as do unknown kinds.
pub fn copy_path(root: &str, path: &str, kind: &str) -> String {
    match kind {
        "abs" => abs_of(root, path),
        "file_uri" => format!("file://{}", encode_path_for_uri(&abs_of(root, path))),
        // "rel" / "posix" / anything else → the stored POSIX relative path, unchanged.
        _ => path.to_string(),
    }
}

// ───────────────────────────────────────────────────────────────────────────────────────────
// Tests — exercise the query logic + the grammar parser against a tiny in-memory DB built to
// mirror the real `entries`/`fts` schema (so they need no on-disk INDEX.sqlite).
// ───────────────────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// STEP 0 GATE (§3.0 / §0.5A): the bundled SQLite MUST be >= 3.51.3. Versions 3.7.0..=3.51.2
    /// corrupt a WAL database when >=2 connections (threads OR processes) write/checkpoint at the
    /// same instant — exactly the reader-pool + writer design this app adopts. Fixed 3.51.3
    /// (2026-03-13). Source: https://sqlite.org/wal.html#walresetbug
    #[test]
    fn sqlite_version_is_past_the_wal_reset_bug() {
        // version_number() encodes X*1_000_000 + Y*1_000 + Z, e.g. 3.51.3 -> 3_051_003.
        let n = rusqlite::version_number();
        assert!(
            n >= 3_051_003,
            "bundled SQLite {} (version_number {n}) is < 3.51.3 and exposed to the WAL-reset \
             corruption bug — bump libsqlite3-sys (PHASE_0_1_SPEC.md §3.0)",
            rusqlite::version(),
        );
    }

    /// Build an in-memory DB with the SAME schema as the real index + the SAME searchtext flatten
    /// (path + category + ext + extractor + tags + flattened-meta leaves/keys), then seed a few
    /// representative rows.
    fn fixture() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE entries(
                 id INTEGER PRIMARY KEY, path TEXT, category TEXT, ext TEXT,
                 size_bytes INTEGER, mtime_iso TEXT, is_symlink INTEGER, symlink_target TEXT,
                 symlink_ok INTEGER, extractor TEXT, tags TEXT, error TEXT,
                 n_obs INTEGER, n_vars INTEGER, meta TEXT);
             CREATE VIRTUAL TABLE fts USING fts5(path, searchtext, content='', tokenize='unicode61');",
        )
        .unwrap();

        // (id, path, category, ext, size, mtime, symlink_ok, extractor, n_obs, n_vars, meta, searchtext)
        let rows: &[(i64, &str, &str, &str, i64, &str, Option<i64>, &str, Option<i64>, Option<i64>, &str, &str)] = &[
            (
                1,
                "a/popv_v4/run/atlas.h5ad",
                "data_matrix",
                "h5ad",
                100,
                "2026-04-20T07:21:29Z",
                None,
                "h5ad",
                Some(2500),
                Some(5),
                r#"{"n_obs":2500,"n_vars":5,"obs_columns":["cell_type","timepoint"],"obsm":{"X_umap":[2500,2]}}"#,
                // searchtext = lowercased path+cat+ext+extractor+flattened meta keys/leaves.
                "a/popv_v4/run/atlas.h5ad data_matrix h5ad h5ad n_obs 2500 n_vars 5 obs_columns cell_type timepoint obsm x_umap",
            ),
            (
                2,
                "fig/popv_v4/umap.png",
                "figure",
                "png",
                200,
                "2026-04-21T07:21:29Z",
                None,
                "image",
                None,
                None,
                r#"{"width":800,"height":600}"#,
                "fig/popv_v4/umap.png figure png image width 800 height 600",
            ),
            (
                3,
                "docs/readme.md",
                "doc",
                "md",
                300,
                "2026-04-22T07:21:29Z",
                Some(1),
                "markdown",
                None,
                None,
                r#"{"title":"Readme"}"#,
                "docs/readme.md doc md markdown title readme",
            ),
        ];
        for (id, path, cat, ext, size, mtime, sok, extr, nobs, nvars, meta, st) in rows {
            conn.execute(
                "INSERT INTO entries(id,path,category,ext,size_bytes,mtime_iso,symlink_ok,extractor,n_obs,n_vars,meta)
                 VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
                rusqlite::params![id, path, cat, ext, size, mtime, sok, extr, nobs, nvars, meta],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO fts(rowid, path, searchtext) VALUES (?1, ?2, ?3)",
                rusqlite::params![id, path, st],
            )
            .unwrap();
        }
        // v2 migration: the read path filters `WHERE is_dir = 0`, so the fixture must be v2. This
        // adds is_dir/keys to the 3 seeded files and SYNTHESIZES their ancestor directory rows.
        // Coexistence tests assert those dir rows are excluded from list/search/facets/count.
        crate::tree::migrate_to_v2(&conn).unwrap();
        conn
    }

    #[test]
    fn basename_takes_last_segment() {
        assert_eq!(basename("a/b/c.txt"), "c.txt");
        assert_eq!(basename("noslash"), "noslash");
    }

    #[test]
    fn list_page_orders_and_maps() {
        let conn = fixture();
        let rows = list_page(&conn, 0, 10, "path").unwrap();
        assert_eq!(rows.len(), 3);
        // path ASC: a/… < docs/… < fig/…
        assert_eq!(rows[0].id, 1);
        assert_eq!(rows[0].name, "atlas.h5ad");
        assert_eq!(rows[0].n_obs, Some(2500));
        // symlink_ok NULL → false for row 1, 1 → true for row 3 (docs/readme.md).
        assert!(!rows[0].symlink_ok);
        // size DESC puts the 300-byte doc first.
        let by_size = list_page(&conn, 0, 10, "size").unwrap();
        assert_eq!(by_size[0].size_bytes, 300);
    }

    #[test]
    fn list_page_clamps_limit() {
        let conn = fixture();
        // A huge limit is clamped but still returns all 3 available rows.
        let rows = list_page(&conn, 0, 10_000, "path").unwrap();
        assert_eq!(rows.len(), 3);
    }

    /// REGRESSION (production bug 2026-06-22): the browse tree bulk-loads the WHOLE index via what
    /// used to be `list_page(0, 200000, …)`, but `list_page` clamps to `MAX_PAGE_LIMIT` (500) — so
    /// the tree silently truncated to 500 entries on the real 38k-row index. The 3-row fixture never
    /// exposed it. `list_all` must return EVERY row; `list_page` must STAY clamped (search paging).
    #[test]
    fn list_all_is_not_clamped_unlike_list_page() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE entries(
                 id INTEGER PRIMARY KEY, path TEXT, category TEXT, ext TEXT,
                 size_bytes INTEGER, mtime_iso TEXT, is_symlink INTEGER, symlink_target TEXT,
                 symlink_ok INTEGER, extractor TEXT, tags TEXT, error TEXT,
                 n_obs INTEGER, n_vars INTEGER, meta TEXT);",
        )
        .unwrap();
        let n = MAX_PAGE_LIMIT as i64 + 50; // 550 — comfortably past the page cap
        for i in 0..n {
            conn.execute(
                "INSERT INTO entries(id, path, category, ext, size_bytes, mtime_iso)
                 VALUES (?1, ?2, 'doc', 'md', 1, '2026-01-01T00:00:00Z')",
                rusqlite::params![i, format!("dir/file_{i:05}.md")],
            )
            .unwrap();
        }
        crate::tree::migrate_to_v2(&conn).unwrap(); // read path filters is_dir=0 → v2 required
        let all = list_all(&conn).unwrap();
        assert_eq!(all.len() as i64, n, "list_all must return ALL file rows, not a page");
        assert_eq!(all[0].path, "dir/file_00000.md", "list_all is path-ordered");
        // list_page stays page-bounded so the search view payload is still capped.
        let page = list_page(&conn, 0, 200_000, "path").unwrap();
        assert_eq!(page.len() as u32, MAX_PAGE_LIMIT, "list_page must stay clamped");
    }

    #[test]
    fn parse_free_token_is_whole_haystack_like() {
        let p = parse_query("popv_v4", false);
        // One whole-haystack substring predicate; value lowercased, `_` LIKE-escaped, %…%-wrapped.
        assert_eq!(p.like_filters.len(), 1);
        assert!(p.like_filters[0].0.starts_with("LOWER(e.path||"));
        assert_eq!(p.like_filters[0].1, "%popv\\_v4%");
        assert!(p.eq_filters.is_empty());
    }

    #[test]
    fn parse_prefixes_route_correctly() {
        let p = parse_query("cat:figure ext:png path:popv obs:cell_type obsm:X_umap free", false);
        // ext: → exact equality filter; everything else → substring LIKE predicates.
        assert_eq!(p.eq_filters, vec![("ext", "png".to_string())]);
        // 5 substring predicates in declaration order: cat, path, obs, obsm, free.
        assert_eq!(p.like_filters.len(), 5);
        assert_eq!(p.like_filters[0], ("LOWER(e.category)", "%figure%".to_string()));
        assert_eq!(p.like_filters[1], ("LOWER(e.path)", "%popv%".to_string()));
        assert_eq!(p.like_filters[2], ("LOWER(COALESCE(e.meta,''))", "%cell\\_type%".to_string()));
        assert_eq!(p.like_filters[3], ("LOWER(COALESCE(e.meta,''))", "%x\\_umap%".to_string()));
        assert_eq!(p.like_filters[4].1, "%free%");
        assert!(p.like_filters[4].0.starts_with("LOWER(e.path||"));
    }

    #[test]
    fn parse_type_aliases_cat_and_dir_scopes_path() {
        // type: is the foundation alias for cat: (substring on category).
        let p = parse_query("type:figure", false);
        assert_eq!(p.like_filters, vec![("LOWER(e.category)", "%figure%".to_string())]);
        // dir: scopes to the path column (parent-dir approximation).
        let p2 = parse_query("dir:harmony", false);
        assert_eq!(p2.like_filters, vec![("LOWER(e.path)", "%harmony%".to_string())]);
    }

    #[test]
    fn parse_bare_prefix_with_no_value_is_ignored() {
        let p = parse_query("cat: ext:png", false);
        assert_eq!(p.eq_filters, vec![("ext", "png".to_string())]);
        assert!(p.like_filters.is_empty());
    }

    #[test]
    fn like_substring_escapes_wildcards_and_lowercases() {
        // Lowercase, backslash-escape the LIKE wildcards `%`/`_`/`\\` (literal match, like the
        // foundation's indexOf), wrap in %…%. Binds as a `?` param → injection-safe.
        assert_eq!(like_substring("Cell_Type"), "%cell\\_type%");
        assert_eq!(like_substring("50%"), "%50\\%%");
        assert_eq!(like_substring("a\\b"), "%a\\\\b%");
        assert_eq!(like_substring("plain"), "%plain%");
    }

    #[test]
    fn parse_quoted_value_groups_into_one_predicate() {
        // Double-quotes group a multi-word value into a SINGLE term. The grouped value becomes one
        // whole-haystack LIKE predicate (the embedded space is matched literally).
        let p = parse_query("\"cell type\"", false);
        assert_eq!(p.like_filters.len(), 1);
        assert_eq!(p.like_filters[0].1, "%cell type%");
        // A prefix can carry a quoted value too: path:"a b" → one path-scoped LIKE.
        let p2 = parse_query("path:\"a b\"", false);
        assert_eq!(p2.like_filters, vec![("LOWER(e.path)", "%a b%".to_string())]);
    }

    #[test]
    fn search_free_token_matches_path_and_searchtext() {
        let conn = fixture();
        // "popv_v4" appears in two paths (h5ad + png).
        let res = search(&conn, "popv_v4", 0, 50, false).unwrap();
        assert_eq!(res.total, 2);
        assert_eq!(res.rows.len(), 2);
    }

    #[test]
    fn search_cat_filter_substring_no_fts_join() {
        let conn = fixture();
        // cat:figure → LIKE %figure% on category; the fixture's lone figure row matches.
        let res = search(&conn, "cat:figure", 0, 50, false).unwrap();
        assert_eq!(res.total, 1);
        assert_eq!(res.rows[0].category, "figure");
    }

    #[test]
    fn search_obs_token_hits_meta_json() {
        let conn = fixture();
        // obs:cell_type → substring match against the meta JSON; only the h5ad carries that obs
        // column (its meta contains "obs_columns":["cell_type",...]).
        let res = search(&conn, "obs:cell_type", 0, 50, false).unwrap();
        assert_eq!(res.total, 1);
        assert_eq!(res.rows[0].id, 1);
    }

    #[test]
    fn search_combines_fts_and_structured_filter() {
        let conn = fixture();
        // free "popv_v4" (2 rows) AND cat:figure (1 of them) → exactly the png.
        let res = search(&conn, "popv_v4 cat:figure", 0, 50, false).unwrap();
        assert_eq!(res.total, 1);
        assert_eq!(res.rows[0].ext, "png");
    }

    #[test]
    fn search_total_independent_of_page_window() {
        let conn = fixture();
        // page of size 1 over a 2-row match → rows.len()==1 but total==2.
        let res = search(&conn, "popv_v4", 0, 1, false).unwrap();
        assert_eq!(res.rows.len(), 1);
        assert_eq!(res.total, 2);
    }

    #[test]
    fn search_empty_query_returns_all() {
        let conn = fixture();
        let res = search(&conn, "", 0, 50, false).unwrap();
        assert_eq!(res.total, 3);
    }

    /// REGRESSION (B2): two `ext:` filters must UNION (a row has one ext, so AND-ing two equalities
    /// is always empty — selecting two type chips returned zero). `ext:png ext:md` → IN ('png','md').
    #[test]
    fn search_two_ext_filters_union_not_contradiction() {
        let conn = fixture();
        // png (row 2) + md (row 3) → both match; the old `ext=png AND ext=md` returned 0.
        let res = search(&conn, "ext:png ext:md", 0, 50, false).unwrap();
        assert_eq!(res.total, 2);
        let mut exts: Vec<&str> = res.rows.iter().map(|r| r.ext.as_str()).collect();
        exts.sort();
        assert_eq!(exts, vec!["md", "png"]);
    }

    /// REGRESSION (B10): `ext:` must be case-INSENSITIVE like every other filter; `ext:PNG` was the
    /// lone case-sensitive filter and silently returned zero.
    #[test]
    fn search_ext_filter_is_case_insensitive() {
        let conn = fixture();
        assert_eq!(search(&conn, "ext:PNG", 0, 50, false).unwrap().total, 1);
        assert_eq!(search(&conn, "ext:H5ad", 0, 50, false).unwrap().total, 1);
    }

    /// REGRESSION (B11): a non-empty query that parses to ZERO predicates (a bare `cat:`/`ext:`)
    /// must NOT match the whole index; a truly empty query still returns everything.
    #[test]
    fn search_bare_operator_does_not_match_all() {
        let conn = fixture();
        assert_eq!(search(&conn, "cat:", 0, 50, false).unwrap().total, 0);
        assert_eq!(search_all(&conn, "ext:", false).unwrap().total, 0);
        // …but a genuinely empty query keeps the no-predicate "everything" result.
        assert_eq!(search(&conn, "", 0, 50, false).unwrap().total, 3);
        assert_eq!(search_all(&conn, "", false).unwrap().total, 3);
    }

    /// REGRESSION (B1, the PRIMARY bug): the paged `search` clamps to `MAX_PAGE_LIMIT` (so the
    /// grouped view silently dropped the alphabetical tail on the real 43k index), but `search_all`
    /// must return EVERY match. The 3-row fixture can't exceed the cap, so build a 550-row one.
    #[test]
    fn search_all_is_not_clamped_unlike_search() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE entries(
                 id INTEGER PRIMARY KEY, path TEXT, category TEXT, ext TEXT,
                 size_bytes INTEGER, mtime_iso TEXT, is_symlink INTEGER, symlink_target TEXT,
                 symlink_ok INTEGER, extractor TEXT, tags TEXT, error TEXT,
                 n_obs INTEGER, n_vars INTEGER, meta TEXT);",
        )
        .unwrap();
        let n = MAX_PAGE_LIMIT as i64 + 50; // 550 — comfortably past the page cap
        for i in 0..n {
            conn.execute(
                "INSERT INTO entries(id, path, category, ext, size_bytes, mtime_iso)
                 VALUES (?1, ?2, 'doc', 'md', 1, '2026-01-01T00:00:00Z')",
                rusqlite::params![i, format!("notes/file_{i:05}.md")],
            )
            .unwrap();
        }
        crate::tree::migrate_to_v2(&conn).unwrap(); // search filters e.is_dir=0 → v2 required
        // Free token "notes" matches all 550 via the path haystack.
        let paged = search(&conn, "notes", 0, MAX_PAGE_LIMIT, false).unwrap();
        assert_eq!(paged.rows.len() as u32, MAX_PAGE_LIMIT, "paged search STAYS capped");
        assert_eq!(paged.total, n as u32, "…but total is the honest full count");
        let all = search_all(&conn, "notes", false).unwrap();
        assert_eq!(all.rows.len() as i64, n, "search_all returns EVERY match, uncapped");
        assert_eq!(all.total as i64, n, "search_all total == rows rendered (no count/render gap)");
        assert_eq!(all.rows[0].path, "notes/file_00000.md", "search_all is path-ordered");
    }

    // ── search_ids (tier 3 of the top-hits band): predicate parity + tokenization parity ──────────

    /// Build a bespoke single-purpose fixture from `(path, category, ext, extractor, meta)` tuples.
    /// Same shape as [`fixture`] (v2-migrated so `e.is_dir = 0` has rows to filter), but the caller
    /// picks the paths — needed for the fold / LIKE-escape / newline cases, which cannot be
    /// expressed with the shared 3-row fixture without perturbing the counts other tests assert.
    fn fixture_paths(rows: &[(&str, &str, &str, &str, &str)]) -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE entries(
                 id INTEGER PRIMARY KEY, path TEXT, category TEXT, ext TEXT,
                 size_bytes INTEGER, mtime_iso TEXT, is_symlink INTEGER, symlink_target TEXT,
                 symlink_ok INTEGER, extractor TEXT, tags TEXT, error TEXT,
                 n_obs INTEGER, n_vars INTEGER, meta TEXT);",
        )
        .unwrap();
        for (i, (path, cat, ext, extr, meta)) in rows.iter().enumerate() {
            conn.execute(
                "INSERT INTO entries(id,path,category,ext,size_bytes,mtime_iso,extractor,meta)
                 VALUES (?1,?2,?3,?4,1,'2026-01-01T00:00:00Z',?5,?6)",
                rusqlite::params![i as i64 + 1, path, cat, ext, extr, meta],
            )
            .unwrap();
        }
        crate::tree::migrate_to_v2(&conn).unwrap();
        conn
    }

    fn ids_of(v: &[IdPath]) -> Vec<i64> {
        v.iter().map(|p| p.id).collect()
    }

    /// The hoist is only safe if `parse_query` still emits the very consts `search_ids` composes
    /// with. Pin that here, so a future edit to either literal breaks ONE obvious test rather than
    /// silently splitting the two predicate builders apart.
    #[test]
    fn parse_query_uses_the_hoisted_haystack_and_cat_expr() {
        assert_eq!(parse_query("free", false).like_filters[0].0, HAYSTACK);
        assert_eq!(parse_query("cat:figure", false).like_filters[0].0, CAT_EXPR);
        assert_eq!(parse_query("type:figure", false).like_filters[0].0, CAT_EXPR);
    }

    /// THE contract test: `search_ids` must return EXACTLY the id set `search_all` returns for the
    /// same tokens. A tier 3 scoped to `meta`/`tags` (the tempting "the tree already did path") is
    /// what this forbids — on the live index that variant loses 5,070 of 15,247 `figure` hits.
    #[test]
    fn search_ids_matches_search_all_ids() {
        let conn = fixture();
        // Tokens that hit path-only, category-only, meta-only, and nothing at all.
        for tok in ["popv_v4", "figure", "readme", "cell_type", "cholesterol", "notebook"] {
            let via_all: Vec<i64> =
                search_all(&conn, tok, false).unwrap().rows.iter().map(|r| r.id).collect();
            let via_ids = ids_of(&search_ids(&conn, &[tok.to_string()], &[], &[], false).unwrap());
            assert_eq!(via_ids, via_all, "search_ids diverged from search_all on `{tok}`");
        }
        // Sanity that the loop is not vacuous: `cell_type` is reachable ONLY through `meta`.
        let meta_only = search_ids(&conn, &["cell_type".to_string()], &[], &[], false).unwrap();
        assert_eq!(ids_of(&meta_only), vec![1], "meta-only token must still be found");
        assert!(!search_ids(&conn, &["figure".to_string()], &[], &[], false).unwrap().is_empty());
        assert!(search_ids(&conn, &["cholesterol".to_string()], &[], &[], false).unwrap().is_empty());
    }

    /// Multi-token is AND (narrow as you add words), and the `cat:` scope routes through `CAT_EXPR`
    /// identically on both sides.
    #[test]
    fn search_ids_and_composes_tokens_and_cats_like_search_all() {
        let conn = fixture();
        // One token → both popv_v4 rows; adding a word must NARROW, never widen.
        assert_eq!(ids_of(&search_ids(&conn, &["popv_v4".into()], &[], &[], false).unwrap()), vec![1, 2]);
        let two = search_ids(&conn, &["popv_v4".into(), "atlas".into()], &[], &[], false).unwrap();
        assert_eq!(ids_of(&two), vec![1], "AND over tokens narrows to the h5ad");
        let via_all: Vec<i64> =
            search_all(&conn, "popv_v4 atlas", false).unwrap().rows.iter().map(|r| r.id).collect();
        assert_eq!(ids_of(&two), via_all, "multi-token AND matches search_all's");
        // `umap` is NOT a counter-example: it is in the png's path AND in the h5ad's meta
        // (`obsm.X_umap`), so it legitimately matches both — the haystack includes meta.
        assert_eq!(
            ids_of(&search_ids(&conn, &["popv_v4".into(), "umap".into()], &[], &[], false).unwrap()),
            vec![1, 2]
        );

        let with_cat = search_ids(&conn, &["popv_v4".into()], &[], &["figure".into()], false).unwrap();
        let via_all: Vec<i64> =
            search_all(&conn, "popv_v4 cat:figure", false).unwrap().rows.iter().map(|r| r.id).collect();
        assert_eq!(ids_of(&with_cat), via_all, "cat: predicate must match search_all's");
        assert_eq!(ids_of(&with_cat), vec![2]);
    }

    /// No free text → no tier 3, even when filters are present (a pure filter is answered entirely
    /// client-side; issuing IPC for it would be pure waste).
    #[test]
    fn search_ids_without_tokens_is_empty() {
        let conn = fixture();
        assert!(search_ids(&conn, &[], &[], &[], false).unwrap().is_empty());
        assert!(search_ids(&conn, &[], &["png".into()], &["figure".into()], false).unwrap().is_empty());
    }

    /// REGRESSION (B2 + B10): two ext chips must UNION via `e.ext IN (?,?)`, never AND into zero,
    /// and the ext bind is ASCII-folded so `ext:PNG` is not the one case-SENSITIVE filter.
    #[test]
    fn search_ids_two_exts_union_and_fold_case() {
        let conn = fixture();
        let toks = vec!["popv_v4".to_string()];
        assert_eq!(ids_of(&search_ids(&conn, &toks, &["png".into()], &[], false).unwrap()), vec![2]);
        assert_eq!(ids_of(&search_ids(&conn, &toks, &["h5ad".into()], &[], false).unwrap()), vec![1]);
        let both = search_ids(&conn, &toks, &["PNG".into(), "H5AD".into()], &[], false).unwrap();
        assert_eq!(ids_of(&both), vec![1, 2], "two exts OR (B2), and fold case (B10)");
    }

    /// A synthesized directory row can never reach the frontend join — `build_where`'s base
    /// `e.is_dir = 0` clause is inherited. `popv_v4` is BOTH a dir segment (`a/popv_v4`,
    /// `fig/popv_v4`) and part of two file paths, so a missing base clause would show up as 4.
    #[test]
    fn search_ids_excludes_dir_rows() {
        let conn = fixture();
        let hits = search_ids(&conn, &["popv_v4".to_string()], &[], &[], false).unwrap();
        assert_eq!(ids_of(&hits), vec![1, 2], "the two files, not their dir rows");
        for h in &hits {
            let is_dir: i64 = conn
                .query_row("SELECT is_dir FROM entries WHERE id = ?1", [h.id], |r| r.get(0))
                .unwrap();
            assert_eq!(is_dir, 0, "{} leaked a directory row", h.path);
        }
    }

    /// TOKENIZATION PARITY (C2): `search_ids` parses NOTHING. A token carrying a space — what
    /// `query.ts` produces from a `"quoted phrase"` — stays ONE contiguous substring predicate and
    /// is never re-split backend-side. The fixture's figure row holds `figure png image`, so
    /// `image figure` exists as two words but NOT as a contiguous run: one token → 0 hits, two
    /// tokens → 1 hit. Both agree with the string-parsing `search_all`.
    #[test]
    fn search_ids_treats_a_spaced_token_as_one_phrase() {
        let conn = fixture();
        let phrase = search_ids(&conn, &["image figure".to_string()], &[], &[], false).unwrap();
        assert!(phrase.is_empty(), "a spaced token is ONE substring, not two");
        assert_eq!(search_all(&conn, "\"image figure\"", false).unwrap().total, 0, "search_all agrees");

        let split = search_ids(&conn, &["image".into(), "figure".into()], &[], &[], false).unwrap();
        assert_eq!(ids_of(&split), vec![2], "as two tokens it is an AND and matches");
        assert_eq!(search_all(&conn, "image figure", false).unwrap().total, 1, "search_all agrees");

        // And the contiguous run that DOES exist is found as a single phrase token.
        let real = search_ids(&conn, &["figure png".to_string()], &[], &[], false).unwrap();
        assert_eq!(ids_of(&real), vec![2]);
    }

    /// TOKENIZATION PARITY (C2): the fold is ASCII-only on BOTH sides, and the index stores NFD.
    /// `query.ts::foldToken` NFD-normalizes then ASCII-lowercases; SQLite's `LOWER()` (no ICU) does
    /// the same to the column. So a decomposed `cafe\u{301}` matches and the PRECOMPOSED `caf\u{e9}`
    /// does not — the reason `foldToken` must never use `String.prototype.toLowerCase`.
    #[test]
    fn search_ids_fold_is_ascii_only_over_nfd() {
        let conn = fixture_paths(&[
            ("notes/cafe\u{301}_run.md", "doc", "md", "markdown", "{}"),
            ("notes/plain.md", "doc", "md", "markdown", "{}"),
        ]);
        let nfd = search_ids(&conn, &["cafe\u{301}".to_string()], &[], &[], false).unwrap();
        assert_eq!(ids_of(&nfd), vec![1], "NFD token matches the NFD-stored path");
        // Uppercase ASCII in the token is folded by `like_substring` (idempotent on a folded token).
        let upper = search_ids(&conn, &["CAFE\u{301}".to_string()], &[], &[], false).unwrap();
        assert_eq!(ids_of(&upper), vec![1], "ASCII fold applies to the bind too");
        // The precomposed form is a DIFFERENT byte sequence and must not match — this is exactly
        // what a `toLowerCase()`-based frontend fold would produce, and why it is banned.
        let nfc = search_ids(&conn, &["caf\u{e9}".to_string()], &[], &[], false).unwrap();
        assert!(nfc.is_empty(), "precomposed NFC must NOT match an NFD-stored path");
    }

    /// LIKE wildcards inside a token are matched LITERALLY (`like_substring` escapes `_`/`%`/`\`),
    /// so `cell_type` cannot wildcard-match `cellXtype` — identical to the frontend's `includes()`.
    #[test]
    fn search_ids_like_wildcards_are_literal() {
        let conn = fixture_paths(&[
            ("x/cell_type.csv", "data_table", "csv", "csv", "{}"),
            ("x/cellXtype.csv", "data_table", "csv", "csv", "{}"),
            ("x/50pct.csv", "data_table", "csv", "csv", r#"{"note":"50% serum"}"#),
        ]);
        assert_eq!(ids_of(&search_ids(&conn, &["cell_type".to_string()], &[], &[], false).unwrap()), vec![1]);
        assert_eq!(ids_of(&search_ids(&conn, &["50%".to_string()], &[], &[], false).unwrap()), vec![3]);
    }

    /// A path containing a literal newline round-trips byte-for-byte through the payload — the
    /// frontend joins on this string, so any normalization here would silently orphan the row.
    #[test]
    fn search_ids_path_with_newline_round_trips() {
        let conn = fixture_paths(&[("weird/line\nbreak.md", "doc", "md", "markdown", "{}")]);
        let hits = search_ids(&conn, &["break".to_string()], &[], &[], false).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].path, "weird/line\nbreak.md", "newline preserved verbatim");
    }

    /// The band joins on PATH (rowids churn across a reconcile), so every element must carry the
    /// real path — and the set must stay path-ordered like `search_all`'s.
    #[test]
    fn search_ids_returns_paths_in_path_order() {
        let conn = fixture();
        let hits = search_ids(&conn, &["popv_v4".to_string()], &[], &[], false).unwrap();
        assert_eq!(
            hits.iter().map(|h| h.path.as_str()).collect::<Vec<_>>(),
            vec!["a/popv_v4/run/atlas.h5ad", "fig/popv_v4/umap.png"],
            "ORDER BY e.path ASC, same as search_all"
        );
    }

    // ── v2 coexistence (§8 gate 6): dir rows exist but never leak into the flat read path ──────────

    #[test]
    fn dir_rows_excluded_from_list_search_facets_count() {
        let conn = fixture(); // 3 files + 6 synthesized dirs (a, a/popv_v4, a/popv_v4/run, fig, fig/popv_v4, docs)
        let total: i64 = conn.query_row("SELECT COUNT(*) FROM entries", [], |r| r.get(0)).unwrap();
        assert_eq!(total, 9, "3 files + 6 dirs present in the table");

        // every flat read path returns exactly the 3 files
        assert_eq!(list_all(&conn).unwrap().len(), 3);
        assert_eq!(list_page(&conn, 0, 100, "path").unwrap().len(), 3);
        assert_eq!(file_count(&conn).unwrap(), 3);
        assert!(list_all(&conn).unwrap().iter().all(|r| !r.is_dir), "no dir row leaks into list_all");

        // facets carry no phantom `dir` chip
        let f = facets(&conn).unwrap();
        assert!(f.categories.iter().all(|c| c.key != "dir"), "no `dir` category chip");

        // free-token search over a shared path segment returns files only
        let s = search_all(&conn, "popv_v4", false).unwrap();
        assert!(s.rows.iter().all(|r| !r.is_dir));
        assert_eq!(s.rows.len(), 2, "atlas.h5ad + umap.png, not their dir rows");
    }

    #[test]
    fn list_children_root_and_by_key() {
        use crate::pathkey::norm_key;
        let conn = fixture();

        // root: parent_key = "" → the three top-level dirs, folders-first, name-ordered
        let root = list_children(&conn, "", 100, 0).unwrap();
        assert_eq!(root.len(), 3);
        assert!(root.iter().all(|r| r.is_dir), "top level is all directories here");
        assert_eq!(
            root.iter().map(|r| r.name.as_str()).collect::<Vec<_>>(),
            vec!["a", "docs", "fig"],
            "case-insensitive name order"
        );
        // child_count populated by this bespoke mapper (rebuild-time hint)
        let a = root.iter().find(|r| r.name == "a").unwrap();
        assert_eq!(a.child_count, Some(1));

        // a leaf directory's children resolve by parent_key (the authoritative edge)
        let run = list_children(&conn, &norm_key("a/popv_v4/run"), 100, 0).unwrap();
        assert_eq!(run.len(), 1);
        assert_eq!(run[0].name, "atlas.h5ad");
        assert!(!run[0].is_dir);

        // count_children matches
        assert_eq!(count_children(&conn, "").unwrap(), 3);
        assert_eq!(count_children(&conn, &norm_key("a/popv_v4/run")).unwrap(), 1);
    }

    #[test]
    fn get_entry_resolves_a_directory_by_id() {
        // get_entry has NO is_dir filter — a dir may be inspected by id (§2.8).
        let conn = fixture();
        let dir_id: i64 = conn
            .query_row("SELECT id FROM entries WHERE path='a' AND is_dir=1", [], |r| r.get(0))
            .unwrap();
        let cl = Crosslinks::default();
        let detail = get_entry(&conn, &cl, dir_id).unwrap();
        assert!(detail.row.is_dir);
        assert_eq!(detail.row.name, "a");
    }

    #[test]
    fn get_entry_parses_meta_and_resolves_lineage() {
        let conn = fixture();
        let mut cl = Crosslinks::default();
        cl.refs.insert("a/popv_v4/run/atlas.h5ad".into(), vec!["fig/popv_v4/umap.png".into()]);
        cl.ref_by.insert("a/popv_v4/run/atlas.h5ad".into(), vec!["docs/readme.md".into()]);
        let d = get_entry(&conn, &cl, 1).unwrap();
        assert_eq!(d.row.id, 1);
        // meta parsed HERE → the obs_columns array is visible.
        assert_eq!(d.meta["n_obs"], serde_json::json!(2500));
        assert_eq!(d.refs, vec!["fig/popv_v4/umap.png".to_string()]);
        assert_eq!(d.ref_by, vec!["docs/readme.md".to_string()]);
        // Both targets ARE entries in the fixture → nothing dangling.
        assert!(d.dangling_refs.is_empty());
        assert!(d.dangling_ref_by.is_empty());
    }

    #[test]
    fn get_entry_flags_non_entry_targets_as_dangling() {
        let conn = fixture();
        let mut cl = Crosslinks::default();
        // One resolvable ref (fig/popv_v4/umap.png IS an entry) + one dangling ref (an archived
        // path with no entry row). ref_by points only at a non-entry → fully dangling.
        cl.refs.insert(
            "a/popv_v4/run/atlas.h5ad".into(),
            vec!["fig/popv_v4/umap.png".into(), "archived/deleted_stage_e.py".into()],
        );
        cl.ref_by
            .insert("a/popv_v4/run/atlas.h5ad".into(), vec!["legacy/gone.py".into()]);
        let d = get_entry(&conn, &cl, 1).unwrap();
        // refs/ref_by still carry EVERY edge target (contract shape unchanged)…
        assert_eq!(d.refs.len(), 2);
        assert_eq!(d.ref_by.len(), 1);
        // …and the dangling lists are exactly the non-entry subset.
        assert_eq!(d.dangling_refs, vec!["archived/deleted_stage_e.py".to_string()]);
        assert_eq!(d.dangling_ref_by, vec!["legacy/gone.py".to_string()]);
        // The resolvable ref is NOT flagged dangling.
        assert!(!d.dangling_refs.contains(&"fig/popv_v4/umap.png".to_string()));
    }

    #[test]
    fn get_entry_missing_id_errors() {
        let conn = fixture();
        let cl = Crosslinks::default();
        assert!(get_entry(&conn, &cl, 9999).is_err());
    }

    #[test]
    fn facets_group_and_order() {
        let conn = fixture();
        let f = facets(&conn).unwrap();
        // 3 distinct categories, each count 1.
        assert_eq!(f.categories.len(), 3);
        assert!(f.categories.iter().all(|c| c.count == 1));
        assert_eq!(f.exts.len(), 3);
    }

    /// The root is a parameter, not a constant — the same stored relative path resolves under
    /// whichever project is resident. (This test used to be written against the hardcoded
    /// `PROJECT_ROOT`, which is exactly what made it pass on one machine and describe nothing.)
    #[test]
    fn copy_path_kinds() {
        let root = "/Volumes/Some Drive/a project";
        assert_eq!(copy_path(root, "a/b.txt", "rel"), "a/b.txt");
        assert_eq!(copy_path(root, "a/b.txt", "posix"), "a/b.txt");
        assert_eq!(copy_path(root, "a/b.txt", "abs"), format!("{root}/a/b.txt"));
        let uri = copy_path(root, "a/b c.txt", "file_uri");
        assert!(uri.starts_with("file:///Volumes/Some%20Drive/"), "{uri}");
        assert!(uri.ends_with("/a/b%20c.txt"));
        // A DIFFERENT root resolves abs against that root (the per-project plumbing).
        assert_eq!(copy_path("/other/proj", "a/b.txt", "abs"), "/other/proj/a/b.txt");
    }

    #[test]
    fn abs_of_passthrough_for_absolute() {
        // Absolute paths pass through unchanged regardless of root…
        assert_eq!(abs_of("/some/root", "/already/abs"), "/already/abs");
        assert_eq!(abs_of("/whatever/root", "/already/abs"), "/already/abs");
        // …and a relative path is joined under the GIVEN root (root-aware resolution).
        assert_eq!(abs_of("/some/root", "a/b"), "/some/root/a/b");
    }

    /// With NO project open the resolver must produce nothing, not a path rooted at `/`. A folder
    /// named `/Users`, `/etc` or `/Applications` really exists, so `""` + `"Users/x"` would name a
    /// real file the user never asked for — and `open`/`reveal` would happily act on it.
    #[test]
    fn abs_of_refuses_to_resolve_against_an_empty_root() {
        assert_eq!(abs_of("", "a/b.txt"), "");
        assert_eq!(copy_path("", "a/b.txt", "abs"), "");
        // An absolute stored path still passes through — it needs no root.
        assert_eq!(abs_of("", "/already/abs"), "/already/abs");
        // …and the two commands that would otherwise spawn `open ""` say so instead.
        assert!(reveal_in_finder("", "a/b.txt").is_err());
        assert!(open_file("", "a/b.txt").is_err());
    }

    #[test]
    fn project_paths_derive_from_root() {
        let p = Project { name: "x".into(), root: "/r".into() };
        assert_eq!(p.index_path(), "/r/_repo_index/INDEX.sqlite");
        assert_eq!(p.crosslinks_path(), "/r/_repo_index/crosslinks.json");
    }

    // ── boot-time active-project selection (the SIGABRT-on-detached-volume regression) ──────────
    //
    // Regression cover for the 2026-07-31 → 08-03 crash loop: `last_active` named a project on an
    // external volume that was no longer mounted, `.setup()` propagated the index-open error, and
    // Tauri's panic inside the non-unwinding `did_finish_launching` became `abort()`. The app died
    // before creating a window, so there was no UI left to switch project from.

    /// A unique scratch dir under CARGO_MANIFEST_DIR (same convention as `writer.rs`'s `scratch`),
    /// cleaned by the caller.
    fn proj_scratch(tag: &str) -> std::path::PathBuf {
        let d = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join(format!("_projects_scratch_{tag}"));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    /// Make `<dir>/<name>` look like a REACHABLE project root: `index_reachable()` tests only for
    /// the index file's existence, so an empty file is a faithful stand-in (no SQLite needed).
    fn reachable_root(dir: &std::path::Path, name: &str) -> String {
        let root = dir.join(name);
        std::fs::create_dir_all(root.join("_repo_index")).unwrap();
        std::fs::write(root.join("_repo_index").join("INDEX.sqlite"), b"").unwrap();
        root.to_string_lossy().into_owned()
    }

    /// Write a `projects.json` naming `roots` (in order) with `last_active`, return its path.
    fn write_registry(dir: &std::path::Path, roots: &[&str], last_active: &str) -> String {
        let list: Vec<Project> = roots
            .iter()
            .map(|r| Project { name: basename(r), root: (*r).to_string() })
            .collect();
        let cfg = dir.join("projects.json");
        write_projects_file(&cfg.to_string_lossy(), &list, last_active).unwrap();
        cfg.to_string_lossy().into_owned()
    }

    /// THE REGRESSION: a `last_active` on a detached volume must not be selected at boot when a
    /// reachable project is registered — the app boots into the reachable one instead of dying.
    #[test]
    fn load_or_seed_skips_an_unreachable_last_active() {
        let dir = proj_scratch("skip_unreachable");
        let good = reachable_root(&dir, "mounted");
        let gone = "/Volumes/DUAL DRIVE".to_string(); // the real crash's stale root
        let cfg = write_registry(&dir, &[&good, &gone], &gone);

        let projects = Projects::load_or_seed(cfg);

        assert_eq!(
            projects.active_root(),
            good,
            "an unreachable last_active must fall back to a reachable project, not abort startup"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The fallback must NOT be persisted: `last_active` keeps naming the absent project, so
    /// replugging the volume restores it on the next launch without the user re-selecting it.
    #[test]
    fn load_or_seed_does_not_persist_the_unreachable_fallback() {
        let dir = proj_scratch("no_persist");
        let good = reachable_root(&dir, "mounted");
        let gone = "/Volumes/DUAL DRIVE".to_string();
        let cfg = write_registry(&dir, &[&good, &gone], &gone);

        let _ = Projects::load_or_seed(cfg.clone());

        let on_disk: ProjectsFile =
            serde_json::from_str(&std::fs::read_to_string(&cfg).unwrap()).unwrap();
        assert_eq!(
            on_disk.last_active, gone,
            "the boot-time fallback is a runtime choice — it must not rewrite last_active"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A REACHABLE `last_active` is still honoured verbatim — the fallback must not fire when the
    /// remembered project is perfectly fine (guards against a fix that always picks projects[0]).
    #[test]
    fn load_or_seed_honours_a_reachable_last_active() {
        let dir = proj_scratch("honour_reachable");
        let first = reachable_root(&dir, "first");
        let second = reachable_root(&dir, "second");
        let cfg = write_registry(&dir, &[&first, &second], &second);

        let projects = Projects::load_or_seed(cfg);

        assert_eq!(projects.active_root(), second, "a reachable last_active must win");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// When NOTHING is reachable (the one drive holding every project is unplugged) selection falls
    /// back to the old behavior and does not panic — `.setup()` then degrades onto the placeholder
    /// index, which is what keeps the window openable.
    #[test]
    fn load_or_seed_survives_every_project_being_unreachable() {
        let dir = proj_scratch("all_gone");
        let gone_a = "/Volumes/DUAL DRIVE";
        let gone_b = "/Volumes/Nope Not Mounted";
        let cfg = write_registry(&dir, &[gone_a, gone_b], gone_b);

        let projects = Projects::load_or_seed(cfg);

        assert_eq!(projects.active_root(), gone_b, "unchanged resolution when nothing is reachable");
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── zero registered folders is a REPRESENTABLE state ────────────────────────────────────────
    //
    // These four replace `seed_project_reproduces_legacy_paths`, which asserted that a first run
    // seeds one specific external volume's path. That behaviour is what these now forbid: it made
    // a stranger's first launch register a folder that could never exist on their machine, and it
    // made "I removed my last folder" impossible to express.

    /// No registry file at all — a genuine first run — is zero projects and nothing active.
    #[test]
    fn load_or_seed_with_no_file_yields_zero_projects() {
        let dir = proj_scratch("first_run");
        let cfg = dir.join("projects.json").to_string_lossy().into_owned();

        let projects = Projects::load_or_seed(cfg.clone());

        assert!(projects.list().unwrap().is_empty(), "a first run registers nothing");
        assert_eq!(projects.active_root(), "", "and opens nothing");
        assert!(projects.active_project_opt().is_none());
        assert!(
            !std::path::Path::new(&cfg).exists(),
            "a first run must not WRITE a registry either — the file appears when the user acts"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// An empty-but-valid registry is the state left behind by removing the last folder. It must
    /// survive a restart: re-seeding it was how the old code handed the user back a folder they
    /// had explicitly forgotten.
    #[test]
    fn load_or_seed_does_not_reseed_an_empty_registry() {
        let dir = proj_scratch("empty_registry");
        let cfg = write_registry(&dir, &[], "");

        let projects = Projects::load_or_seed(cfg);

        assert!(projects.list().unwrap().is_empty());
        assert_eq!(projects.active_root(), "");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The ACTIVE project can now be removed (the engine teardown moved to the command, §1.3), and
    /// removing it must clear the persisted pointer rather than leave `last_active` naming a root
    /// that is no longer registered.
    #[test]
    fn remove_can_drop_the_active_project_and_clears_the_pointer() {
        let dir = proj_scratch("remove_active");
        let only = reachable_root(&dir, "only");
        let cfg = write_registry(&dir, &[&only], &only);
        let projects = Projects::load_or_seed(cfg.clone());
        assert_eq!(projects.active_root(), only);

        projects.remove(&only).unwrap();

        assert!(projects.list().unwrap().is_empty());
        assert_eq!(projects.active_root(), "", "the active pointer goes with the entry");
        let on_disk: ProjectsFile =
            serde_json::from_str(&std::fs::read_to_string(&cfg).unwrap()).unwrap();
        assert!(on_disk.projects.is_empty());
        assert_eq!(on_disk.last_active, "", "persisted, so the next boot is a clean first run");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The replacement chosen after removing the active project: the most recently ADDED project
    /// that still has an index. A registered-but-unindexed folder is skipped — switching to it
    /// would fail `switch_project`'s own precondition.
    #[test]
    fn fallback_prefers_the_most_recently_added_indexed_project() {
        let dir = proj_scratch("fallback");
        let older = reachable_root(&dir, "older");
        let newer = reachable_root(&dir, "newer");
        // Registered, but never indexed — must not be chosen.
        let unindexed = dir.join("unindexed");
        std::fs::create_dir_all(&unindexed).unwrap();
        let unindexed = unindexed.to_string_lossy().into_owned();
        let cfg = write_registry(&dir, &[&older, &newer, &unindexed], &older);
        let projects = Projects::load_or_seed(cfg);

        let pick = projects.fallback_after_removing(&older).expect("a reachable fallback");
        assert_eq!(pick.root, newer, "most recently added, and indexed");
        assert!(
            projects.fallback_after_removing(&newer).map(|p| p.root) == Some(older.clone()),
            "removing the newer one falls back to the older INDEXED one, never the unindexed one"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── deleting an index: the guards ───────────────────────────────────────────────────────────

    /// The happy path — bytes are summed BEFORE the tree goes, because afterwards there is nothing
    /// left to measure and that number is what the user is told they reclaimed.
    #[test]
    fn delete_index_dir_reports_bytes_and_removes_the_tree() {
        let dir = proj_scratch("del_ok");
        let root = dir.join("proj");
        let idx = root.join("_repo_index");
        std::fs::create_dir_all(idx.join("sub")).unwrap();
        std::fs::write(idx.join("INDEX.sqlite"), vec![7u8; 1000]).unwrap();
        std::fs::write(idx.join("sub").join("crosslinks.json"), vec![7u8; 24]).unwrap();

        let freed = delete_index_dir(&root.to_string_lossy()).unwrap();

        assert_eq!(freed, 1024, "every file under the tree, summed before deletion");
        assert!(!idx.exists(), "the index dir is gone");
        assert!(root.exists(), "and the project folder itself is NOT touched");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A SYMLINKED `_repo_index` deletes nothing. `remove_dir_all` through a link would delete the
    /// link's target — some other directory entirely, chosen by whoever made the link.
    #[test]
    fn delete_index_dir_refuses_a_symlink() {
        let dir = proj_scratch("del_symlink");
        let root = dir.join("proj");
        std::fs::create_dir_all(&root).unwrap();
        let elsewhere = dir.join("real_data");
        std::fs::create_dir_all(&elsewhere).unwrap();
        std::fs::write(elsewhere.join("precious.csv"), b"keep me").unwrap();
        std::os::unix::fs::symlink(&elsewhere, root.join("_repo_index")).unwrap();

        let err = delete_index_dir(&root.to_string_lossy()).unwrap_err();

        assert!(err.contains("symlink"), "{err}");
        assert!(elsewhere.join("precious.csv").exists(), "the link target is untouched");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Roots that must never recruit the deletion path, whatever a corrupted registry says.
    #[test]
    fn delete_index_dir_refuses_dangerous_roots() {
        assert!(delete_index_dir("/").is_err(), "the filesystem root");
        assert!(delete_index_dir("/Users").is_err(), "a single-component root");
        assert!(delete_index_dir("relative/path").is_err(), "not absolute");
        assert!(delete_index_dir("").is_err(), "no project open");
        if let Ok(home) = std::env::var("HOME") {
            assert!(delete_index_dir(&home).is_err(), "the home directory");
        }
    }

    /// A project with no index at all reports an error rather than pretending it deleted something
    /// — the caller turns that into `index_deleted: false` and still completes the removal.
    #[test]
    fn delete_index_dir_errors_when_there_is_no_index() {
        let dir = proj_scratch("del_missing");
        let root = dir.join("proj");
        std::fs::create_dir_all(&root).unwrap();
        assert!(delete_index_dir(&root.to_string_lossy()).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ── the switcher's per-row status ───────────────────────────────────────────────────────────

    /// The whole point of the feature: a row that is broken must be DISTINGUISHABLE from a row that
    /// is fine. Three rows, three different states, one call.
    #[test]
    fn status_list_separates_working_missing_and_unindexed_rows() {
        let dir = proj_scratch("status");
        let good = reachable_root(&dir, "good");
        std::fs::write(
            std::path::Path::new(&good).join("_repo_index").join("INDEX.sqlite"),
            vec![0u8; 512],
        )
        .unwrap();
        let unindexed = dir.join("no_index");
        std::fs::create_dir_all(&unindexed).unwrap();
        let unindexed = unindexed.to_string_lossy().into_owned();
        let gone = "/Volumes/Nope Not Mounted".to_string();
        let cfg = write_registry(&dir, &[&good, &unindexed, &gone], &good);

        let rows = Projects::load_or_seed(cfg).status_list();

        assert_eq!(rows.len(), 3);
        let g = rows.iter().find(|r| r.root == good).unwrap();
        assert!(g.is_active && g.folder_exists && g.has_index);
        assert_eq!(g.index_bytes, 512, "the size of <root>/_repo_index/");

        let u = rows.iter().find(|r| r.root == unindexed).unwrap();
        assert!(u.folder_exists && !u.has_index && !u.is_active);
        assert_eq!(u.index_bytes, 0);

        let m = rows.iter().find(|r| r.root == gone).unwrap();
        assert!(!m.folder_exists && !m.has_index);
        assert_eq!(m.index_bytes, 0, "an unreachable row reports zeroes, never an error");
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ───────────────────────────────────────────────────────────────────────────────────────
    // Figure text (opt-in). Spec: docs/superpowers/specs/2026-08-10-repo-index-figure-text-design.md
    //
    // The whole design rests on ONE property: with the opt-in OFF the executed SQL is
    // byte-identical to the pre-feature build, so enabling figure-text indexing cannot slow or
    // pollute ordinary search. `default_search_cannot_see_figure_text` is that regression lock.
    // ───────────────────────────────────────────────────────────────────────────────────────

    /// `fixture()` builds the v1 shape and migrates, so the figure_text column arrives via the
    /// migration — then we seed a figure whose tokens appear NOWHERE else (not in the path, not in
    /// meta, not in searchtext), so any hit is unambiguously attributable to `figure_text`.
    fn fixture_with_figure_text() -> Connection {
        let conn = fixture();
        conn.execute(
            "UPDATE entries SET figure_text = ?1 WHERE path = ?2",
            rusqlite::params!["cux2 en-it-ul-1 zeroexpressing", "fig/popv_v4/umap.png"],
        )
        .unwrap();
        conn
    }

    #[test]
    fn file_count_excludes_dir_rows_on_a_v2_db_even_after_a_schema_version_bump() {
        // `file_count` must key on the is_dir COLUMN, not on `user_version >= USER_VERSION`:
        // the moment USER_VERSION moves past 2, a db still stamped 2 would take the legacy
        // branch and silently count synthesized DIRECTORY rows as files.
        let conn = fixture();
        conn.execute_batch("PRAGMA user_version = 2;").unwrap();
        assert_eq!(file_count(&conn).unwrap(), 3);
    }

    #[test]
    fn v1_db_gains_the_figure_text_column_on_migration() {
        let conn = fixture();
        let n: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM pragma_table_info('entries') WHERE name='figure_text'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 1, "migrate_to_v2 must add figure_text to a legacy index");
    }

    #[test]
    fn default_search_cannot_see_figure_text() {
        let conn = fixture_with_figure_text();
        assert_eq!(
            search_all(&conn, "cux2", false).unwrap().total,
            0,
            "figure text leaked into the DEFAULT haystack — the opt-in is not opt-in"
        );
    }

    #[test]
    fn opt_in_search_finds_a_figure_by_its_rendered_label() {
        let conn = fixture_with_figure_text();
        let res = search_all(&conn, "cux2", true).unwrap();
        assert_eq!(res.total, 1);
        assert_eq!(res.rows[0].path, "fig/popv_v4/umap.png");
    }

    #[test]
    fn opt_in_haystack_is_a_superset_never_a_replacement() {
        let conn = fixture_with_figure_text();
        assert_eq!(
            search_all(&conn, "popv_v4", true).unwrap().total,
            search_all(&conn, "popv_v4", false).unwrap().total,
        );
    }

    #[test]
    fn fig_prefix_scopes_to_figure_text_even_with_the_flag_off() {
        let conn = fixture_with_figure_text();
        assert_eq!(search_all(&conn, "fig:cux2", false).unwrap().total, 1);
        // …and it must NOT fall back to the path/meta haystack.
        assert_eq!(search_all(&conn, "fig:popv_v4", false).unwrap().total, 0);
    }

    #[test]
    fn parse_query_free_token_haystack_depends_on_the_opt_in() {
        assert_eq!(parse_query("free", false).like_filters[0].0, HAYSTACK);
        assert_eq!(
            parse_query("free", true).like_filters[0].0,
            HAYSTACK_WITH_FIGURE_TEXT
        );
    }

    #[test]
    fn search_ids_flags_rows_whose_figure_text_carries_the_query() {
        // Match PROVENANCE: without it, a figure that matched on text the user cannot see looks
        // like an arbitrary result. `via_figure_text` is what the badge renders.
        let conn = fixture_with_figure_text();

        // "cux2" exists ONLY in figure_text.
        let hits = search_ids(&conn, &["cux2".into()], &[], &[], true).unwrap();
        assert_eq!(hits.len(), 1);
        assert!(hits[0].via_figure_text);

        // "popv_v4" is in BOTH rows' paths, but in neither row's figure text.
        let hits = search_ids(&conn, &["popv_v4".into()], &[], &[], true).unwrap();
        assert!(hits.len() >= 2);
        assert!(hits.iter().all(|h| !h.via_figure_text));
    }

    #[test]
    fn search_ids_honours_the_opt_in_like_search_all() {
        let conn = fixture_with_figure_text();
        let off = search_ids(&conn, &["cux2".into()], &[], &[], false).unwrap();
        let on = search_ids(&conn, &["cux2".into()], &[], &[], true).unwrap();
        assert!(off.is_empty());
        assert_eq!(on.len(), 1);
        assert_eq!(on[0].path, "fig/popv_v4/umap.png");
    }
}
