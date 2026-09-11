//! `tree` — the v2 schema owner + directory-tree synthesis + migration + JSONL ingest
//! (PHASE_0_1_SPEC.md §2.4–§2.6, §0.5D). Rust is the SOLE owner of the v2 schema, the directory
//! synthesis, and the normalized-key computation; Python emits only the flat JSONL manifest, which
//! this module ingests (Path A). There is no `sqlite_tree.py` and no cross-language parity fixture.
//!
//! Two ways rows reach the v2 `entries` table, both PURE metadata transforms (no file walk, no
//! re-extraction):
//!   * **Path A — JSONL ingest** ([`ingest_entries`]): the common case. Read the Python manifest,
//!     synthesize dir rows + keys, UPSERT in ONE `BEGIN IMMEDIATE … COMMIT` (WAL snapshot isolation
//!     → readers see old-or-new atomically), no inode swap.
//!   * **Path B — in-place upgrade** ([`migrate_to_v2`]): a Rust writer opening a legacy v1 db
//!     ALTERs it to v2, preserving file-row ids.
//!
//! The `content_digest` / `index_schema.json` / `INDEX.json` manifest are UNTOUCHED — dir rows and
//! keys live in the sqlite ONLY (§2.1). Directory rows and normalized keys are a deterministic
//! function of the `path` set already in the manifest.

use std::collections::BTreeMap;

use rusqlite::{params, Connection, OptionalExtension};
use serde::Deserialize;
use serde_json::value::RawValue;

use crate::pathkey::{basename_of, depth_of, norm_key, parent_of, NORM_VERSION};

/// The derived cache's shape version — the fast integer gate the reader checks on open (§2.9).
pub const USER_VERSION: i64 = 3;

/// The version at which the v2 TREE columns (`is_dir`/`path_key`/…) arrived. Distinct from
/// [`USER_VERSION`] on purpose: readers asking "does this db have `is_dir`?" must compare
/// against THIS, not against the current schema version, or every future bump silently sends
/// an already-migrated db down the legacy branch (see `db::file_count`).
pub const TREE_ROWS_USER_VERSION: i64 = 2;
/// `schema_meta.schema_version` string (distinct namespace from the manifest's `"1.0"`, §1.3).
pub const SCHEMA_VERSION: &str = "2";

// ── the v2 DDL (§2.4) — Rust-owned; the DR Python builder reuses the FLAT v1 shape, not this ──────

/// The v2 `entries` table: the 15 v1 columns (byte-identical order/types) + 10 v2 tree/key columns,
/// all a pure function of `path`.
const CREATE_ENTRIES_V2: &str = "\
CREATE TABLE entries(
  id             INTEGER PRIMARY KEY,
  path           TEXT,
  category       TEXT,
  ext            TEXT,
  size_bytes     INTEGER,
  mtime_iso      TEXT,
  is_symlink     INTEGER,
  symlink_target TEXT,
  symlink_ok     INTEGER,
  extractor      TEXT,
  tags           TEXT,
  error          TEXT,
  n_obs          INTEGER,
  n_vars         INTEGER,
  meta           TEXT,
  figure_text    TEXT,
  is_dir         INTEGER NOT NULL DEFAULT 0,
  parent_key     TEXT    NOT NULL DEFAULT '',
  parent_id      INTEGER,
  depth          INTEGER NOT NULL DEFAULT 0,
  name           TEXT    NOT NULL DEFAULT '',
  path_key       TEXT    NOT NULL DEFAULT '',
  name_key       TEXT    NOT NULL DEFAULT '',
  sort_key       TEXT    NOT NULL DEFAULT '',
  child_count    INTEGER,
  indexed_at     TEXT    NOT NULL DEFAULT ''
);";

/// The three v1 indexes (kept byte-identical).
const CREATE_V1_INDEXES: &str = "\
CREATE INDEX IF NOT EXISTS idx_entries_path ON entries(path);
CREATE INDEX IF NOT EXISTS idx_entries_cat  ON entries(category);
CREATE INDEX IF NOT EXISTS idx_entries_nobs ON entries(n_obs DESC);";

/// The four v2 indexes. `idx_entries_path_key` is UNIQUE — it enforces the exFAT collapse and is the
/// UPSERT conflict target; it is built LAST (after any dedupe) so a case-collision on a
/// case-sensitive FS degrades rather than aborting the build.
const CREATE_V2_INDEXES: &str = "\
CREATE INDEX IF NOT EXISTS idx_entries_children ON entries(parent_key, is_dir DESC, name_key);
CREATE INDEX IF NOT EXISTS idx_entries_parent_id ON entries(parent_id);
CREATE INDEX IF NOT EXISTS idx_entries_sort ON entries(sort_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_path_key ON entries(path_key);";

const CREATE_SCHEMA_META: &str =
    "CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT);";

/// The contentless FTS5 table — created for SCHEMA-SHAPE compatibility with external `sqlite3 -json`
/// consumers and the Python DR build, but NEVER maintained by the live Rust writer or the ingest
/// (§2.4 / §4.6): search is `LIKE`-based, so a stale/empty `fts` is inert for the app.
const CREATE_FTS: &str =
    "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, searchtext, content='', tokenize='unicode61');";

/// The 22 columns a row INSERT binds (id auto-assigned; `parent_id`/`child_count` resolved in pass
/// 2). Used for dir synthesis during migration, BEFORE the UNIQUE `path_key` index exists (so it
/// cannot carry an `ON CONFLICT(path_key)` target — synthesis already excludes collisions).
const INSERT_SQL: &str = "\
INSERT INTO entries
  (path,category,ext,size_bytes,mtime_iso,is_symlink,symlink_target,symlink_ok,extractor,tags,error,
   n_obs,n_vars,meta,is_dir,parent_key,depth,name,path_key,name_key,sort_key,indexed_at)
VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22)";

/// The same INSERT plus `ON CONFLICT(path_key) DO UPDATE` — the idempotent UPSERT used by the ingest
/// (where the UNIQUE `path_key` index already exists in the v2 schema).
pub(crate) const UPSERT_SQL: &str = "\
INSERT INTO entries
  (path,category,ext,size_bytes,mtime_iso,is_symlink,symlink_target,symlink_ok,extractor,tags,error,
   n_obs,n_vars,meta,is_dir,parent_key,depth,name,path_key,name_key,sort_key,indexed_at)
VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22)
ON CONFLICT(path_key) DO UPDATE SET
  path=excluded.path, category=excluded.category, ext=excluded.ext, size_bytes=excluded.size_bytes,
  mtime_iso=excluded.mtime_iso, is_symlink=excluded.is_symlink, symlink_target=excluded.symlink_target,
  symlink_ok=excluded.symlink_ok, extractor=excluded.extractor, tags=excluded.tags, error=excluded.error,
  n_obs=excluded.n_obs, n_vars=excluded.n_vars, meta=excluded.meta, is_dir=excluded.is_dir,
  parent_key=excluded.parent_key, depth=excluded.depth, name=excluded.name,
  name_key=excluded.name_key, sort_key=excluded.sort_key, indexed_at=excluded.indexed_at";

// ── manifest entry (Path A input) ────────────────────────────────────────────────────────────────

/// One entry of the Python manifest (`INDEX.json` `entries[]` / one `INDEX.jsonl` line). `meta` is
/// captured as `RawValue` so its exact bytes are stored verbatim — never re-serialized (§4.7:
/// `serde_json` sorts map keys, so byte-parity via re-serialization is unachievable).
#[derive(Deserialize)]
pub struct ManifestEntry {
    pub path: String,
    pub category: String,
    pub ext: String,
    pub size_bytes: i64,
    pub mtime_iso: String,
    pub is_symlink: bool,
    pub symlink_target: Option<String>,
    pub symlink_ok: Option<bool>,
    pub extractor: String,
    #[serde(default)]
    pub tags: Vec<String>,
    pub meta: Box<RawValue>,
    #[serde(default)]
    pub error: Option<String>,
}

/// The insertable column values for one row (id auto; parent_id/child_count resolved later).
pub(crate) struct RowValues {
    path: String,
    category: String,
    ext: String,
    size_bytes: i64,
    mtime_iso: Option<String>,
    is_symlink: Option<i64>,
    symlink_target: Option<String>,
    symlink_ok: Option<i64>,
    extractor: String,
    tags: String,
    error: Option<String>,
    n_obs: Option<i64>,
    n_vars: Option<i64>,
    meta: String,
    is_dir: i64,
    parent_key: String,
    depth: i64,
    name: String,
    path_key: String,
    name_key: String,
    sort_key: String,
    indexed_at: String,
}

impl RowValues {
    /// Derive a FILE row from a manifest entry (§2.5). Denormalizes `n_obs`/`n_vars` from `meta`
    /// with the EXACT `isinstance(int)` rule `export_sqlite.py` uses (a float/null → NULL).
    pub(crate) fn from_entry(e: &ManifestEntry, now_iso: &str) -> Self {
        let name = basename_of(&e.path).to_string();
        let path_key = norm_key(&e.path);
        RowValues {
            path: e.path.clone(),
            category: e.category.clone(),
            ext: e.ext.clone(),
            size_bytes: e.size_bytes,
            mtime_iso: Some(e.mtime_iso.clone()),
            is_symlink: Some(if e.is_symlink { 1 } else { 0 }),
            symlink_target: e.symlink_target.clone(),
            symlink_ok: e.symlink_ok.map(|b| if b { 1 } else { 0 }),
            extractor: e.extractor.clone(),
            tags: serde_json::to_string(&e.tags).unwrap_or_else(|_| "[]".into()),
            error: e.error.clone(),
            n_obs: int_meta_field(&e.meta, "n_obs"),
            n_vars: int_meta_field(&e.meta, "n_vars"),
            meta: e.meta.get().to_string(),
            is_dir: 0,
            parent_key: norm_key(parent_of(&e.path)),
            depth: depth_of(&e.path),
            name_key: norm_key(&name),
            name,
            sort_key: path_key.clone(),
            path_key,
            indexed_at: now_iso.to_string(),
        }
    }

    /// Derive a synthesized DIRECTORY row from its canonical display path (§2.5).
    pub(crate) fn dir(path: &str, now_iso: &str) -> Self {
        let name = basename_of(path).to_string();
        let path_key = norm_key(path);
        RowValues {
            path: path.to_string(),
            category: "dir".into(),
            ext: String::new(),
            size_bytes: 0,
            mtime_iso: None,
            is_symlink: None,
            symlink_target: None,
            symlink_ok: None,
            extractor: "dir".into(),
            tags: "[]".into(),
            error: None,
            n_obs: None,
            n_vars: None,
            meta: "{}".into(),
            is_dir: 1,
            parent_key: norm_key(parent_of(path)),
            depth: depth_of(path),
            name_key: norm_key(&name),
            name,
            sort_key: path_key.clone(),
            path_key,
            indexed_at: now_iso.to_string(),
        }
    }

    pub(crate) fn path_key(&self) -> &str {
        &self.path_key
    }
    pub(crate) fn parent_key(&self) -> &str {
        &self.parent_key
    }

    /// Bind the 22 column values and execute the prepared statement (either [`UPSERT_SQL`] or the
    /// plain [`INSERT_SQL`], depending on whether a UNIQUE `path_key` conflict target exists yet).
    pub(crate) fn bind_exec(&self, stmt: &mut rusqlite::Statement<'_>) -> Result<(), String> {
        stmt.execute(params![
            self.path,
            self.category,
            self.ext,
            self.size_bytes,
            self.mtime_iso,
            self.is_symlink,
            self.symlink_target,
            self.symlink_ok,
            self.extractor,
            self.tags,
            self.error,
            self.n_obs,
            self.n_vars,
            self.meta,
            self.is_dir,
            self.parent_key,
            self.depth,
            self.name,
            self.path_key,
            self.name_key,
            self.sort_key,
            self.indexed_at,
        ])
        .map_err(|e| format!("upsert {}: {e}", self.path))?;
        Ok(())
    }
}

/// Extract an integer meta field with `export_sqlite`'s `isinstance(int)` semantics (§6.3): a JSON
/// integer → its value; a float / null / missing / non-number → `None`.
fn int_meta_field(meta: &RawValue, key: &str) -> Option<i64> {
    let v: serde_json::Value = serde_json::from_str(meta.get()).ok()?;
    match v.get(key) {
        Some(serde_json::Value::Number(n)) if n.is_i64() || n.is_u64() => n.as_i64(),
        _ => None,
    }
}

// ── directory synthesis (§2.5) ───────────────────────────────────────────────────────────────────

/// Every ancestor directory of every file, DEDUPED BY `path_key` (merging case/NFC variants into ONE
/// node, exFAT-style), path-sorted (parent < child). Canonical display spelling = first path-sorted
/// occurrence. Pure — no I/O. (§2.5 reference algorithm, implemented once here.)
pub fn ancestor_dirs(file_paths: &[String]) -> Vec<String> {
    let mut sorted: Vec<&str> = file_paths.iter().map(|s| s.as_str()).collect();
    sorted.sort_unstable();
    let mut seen: BTreeMap<String, String> = BTreeMap::new(); // path_key -> canonical display path
    for p in sorted {
        let mut anc = parent_of(p);
        while !anc.is_empty() {
            seen.entry(norm_key(anc)).or_insert_with(|| anc.to_string());
            anc = parent_of(anc);
        }
    }
    let mut dirs: Vec<String> = seen.into_values().collect();
    dirs.sort_unstable();
    dirs
}

// ── Path A: JSONL ingest ─────────────────────────────────────────────────────────────────────────

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct IngestStats {
    pub files: usize,
    pub dirs: usize,
    pub deleted: usize,
}

/// Parse a flat JSONL manifest (one `{...}` per line; blank lines skipped) into entries.
pub fn parse_jsonl(text: &str) -> Result<Vec<ManifestEntry>, String> {
    let mut out = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let e: ManifestEntry =
            serde_json::from_str(line).map_err(|err| format!("jsonl line {}: {err}", i + 1))?;
        out.push(e);
    }
    Ok(out)
}

/// Parse an `INDEX.json` object (with an `entries` array) into entries.
pub fn parse_index_json(text: &str) -> Result<Vec<ManifestEntry>, String> {
    #[derive(Deserialize)]
    struct Manifest {
        entries: Vec<ManifestEntry>,
    }
    let m: Manifest = serde_json::from_str(text).map_err(|e| format!("INDEX.json: {e}"))?;
    Ok(m.entries)
}

/// Path A. Ingest a manifest into the live index in ONE `BEGIN IMMEDIATE … COMMIT`: synthesize the
/// directory tree + keys, UPSERT every row (`ON CONFLICT(path_key) DO UPDATE`), delete anything the
/// manifest no longer contains, resolve `parent_id`/`child_count`, stamp `schema_meta`. Idempotent
/// (re-running reconciles the index to the manifest). No inode swap, no orphaned `-wal`/`-shm`.
pub fn ingest_entries(
    conn: &Connection,
    entries: &[ManifestEntry],
    writer_tag: &str,
    now_iso: &str,
    generation: u64,
) -> Result<IngestStats, String> {
    migrate_to_v2(conn)?; // guarantee the v2 schema (its own txn); no-op on an already-v2 db

    // File path_keys — used to exclude a synthesized dir that collides with a file (§2.5).
    let file_paths: Vec<String> = entries.iter().map(|e| e.path.clone()).collect();
    let mut file_key_set = std::collections::HashSet::new();
    for e in entries {
        file_key_set.insert(norm_key(&e.path));
    }
    let dir_paths: Vec<String> = ancestor_dirs(&file_paths)
        .into_iter()
        .filter(|d| !file_key_set.contains(&norm_key(d)))
        .collect();

    immediate_txn(conn, |c| {
        c.execute_batch("CREATE TEMP TABLE IF NOT EXISTS seen(k TEXT PRIMARY KEY); DELETE FROM seen;")
            .map_err(|e| format!("seen temp: {e}"))?;
        let n_files;
        let n_dirs;
        {
            let mut up = c.prepare(UPSERT_SQL).map_err(|e| format!("prepare upsert: {e}"))?;
            let mut seen = c.prepare("INSERT OR IGNORE INTO seen(k) VALUES (?1)").map_err(|e| e.to_string())?;
            let mut nf = 0usize;
            for e in entries {
                let rv = RowValues::from_entry(e, now_iso);
                seen.execute(params![rv.path_key]).map_err(|e| e.to_string())?;
                rv.bind_exec(&mut up)?;
                nf += 1;
            }
            let mut nd = 0usize;
            for d in &dir_paths {
                let rv = RowValues::dir(d, now_iso);
                seen.execute(params![rv.path_key]).map_err(|e| e.to_string())?;
                rv.bind_exec(&mut up)?;
                nd += 1;
            }
            n_files = nf;
            n_dirs = nd;
        }
        // Delete anything the manifest no longer contains (full reconcile-to-manifest semantics).
        let deleted = c
            .execute("DELETE FROM entries WHERE path_key NOT IN (SELECT k FROM seen)", [])
            .map_err(|e| format!("prune: {e}"))?;
        c.execute_batch("DROP TABLE seen;").ok();
        resolve_links(c)?;
        // index_generation is MONOTONIC (§4.5) — a re-ingest must BUMP it, never reset it. Use the
        // current value + 1, with the passed `generation` only as an optional floor.
        let next_gen = current_generation(c).saturating_add(1).max(generation);
        write_schema_meta(c, writer_tag, now_iso, next_gen)?;
        Ok(IngestStats { files: n_files, dirs: n_dirs, deleted })
    })
}

// ── Path B: in-place v1 → v2 migration ───────────────────────────────────────────────────────────

/// Ensure `conn`'s db is at [`USER_VERSION`]. Idempotent (gated on `PRAGMA user_version`). On a
/// fresh/empty db creates the current schema; on a legacy v1 db ALTERs it in place, PRESERVING
/// file-row ids (dir rows get fresh appended ids); on a v2 db it adds only what v3 introduced (the
/// `figure_text` column). A crash mid-migration leaves the old `user_version`, so it re-runs cleanly.
///
/// Name kept as `migrate_to_v2` for its many call sites; it migrates to the CURRENT version.
pub fn migrate_to_v2(conn: &Connection) -> Result<(), String> {
    let v: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .map_err(|e| format!("user_version: {e}"))?;
    if v >= USER_VERSION {
        return Ok(());
    }
    let has_entries: bool = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'",
            [],
            |_| Ok(true),
        )
        .optional()
        .map_err(|e| e.to_string())?
        .unwrap_or(false);

    immediate_txn(conn, |c| {
        if !has_entries {
            // Fresh db → create the whole v2 schema.
            c.execute_batch(CREATE_ENTRIES_V2).map_err(|e| format!("create entries: {e}"))?;
            c.execute_batch(CREATE_V1_INDEXES).map_err(|e| format!("v1 idx: {e}"))?;
            c.execute_batch(CREATE_V2_INDEXES).map_err(|e| format!("v2 idx: {e}"))?;
            c.execute_batch(CREATE_FTS).map_err(|e| format!("fts: {e}"))?;
            c.execute_batch(CREATE_SCHEMA_META).map_err(|e| format!("schema_meta: {e}"))?;
        } else if !column_exists(c, "entries", "is_dir")? {
            // Legacy v1 shape → widen + backfill + synthesize.
            migrate_v1_rows(c)?;
        }
        // v3: the SVG figure-text column. Added separately from the v2 tree widen because a db
        // may already be at v2 (so `migrate_v1_rows` above is skipped) and still lack it. O(1)
        // metadata-only ADD COLUMN; the values arrive with the next `export-sqlite` / ingest.
        if !column_exists(c, "entries", "figure_text")? {
            c.execute_batch("ALTER TABLE entries ADD COLUMN figure_text TEXT;")
                .map_err(|e| format!("add figure_text: {e}"))?;
        }
        // schema_meta may be absent on a legacy db even after ALTER.
        c.execute_batch(CREATE_SCHEMA_META).map_err(|e| format!("schema_meta: {e}"))?;
        write_schema_meta(c, "lens-rust", "", 0)?;
        c.execute_batch(&format!("PRAGMA user_version = {USER_VERSION};"))
            .map_err(|e| format!("set user_version: {e}"))?;
        Ok(())
    })
}

/// The v1→v2 in-place upgrade body (§2.6 Path B): runs inside the migration transaction.
fn migrate_v1_rows(c: &Connection) -> Result<(), String> {
    // 1. Widen — each ADD COLUMN is O(1) metadata-only with a constant DEFAULT. path_key is added as
    //    a PLAIN column here; the UNIQUE index is built after dedupe guarantees uniqueness.
    for ddl in [
        "ALTER TABLE entries ADD COLUMN is_dir INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE entries ADD COLUMN parent_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE entries ADD COLUMN parent_id INTEGER",
        "ALTER TABLE entries ADD COLUMN depth INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE entries ADD COLUMN name TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE entries ADD COLUMN path_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE entries ADD COLUMN name_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE entries ADD COLUMN sort_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE entries ADD COLUMN child_count INTEGER",
        "ALTER TABLE entries ADD COLUMN indexed_at TEXT NOT NULL DEFAULT ''",
    ] {
        c.execute_batch(ddl).map_err(|e| format!("alter: {ddl}: {e}"))?;
    }

    // 2. Backfill file rows — compute derived keys in Rust (bundled SQLite has no casefold), UPDATE
    //    by id. is_dir stays 0.
    let rows: Vec<(i64, String)> = {
        let mut st = c
            .prepare("SELECT id, path FROM entries")
            .map_err(|e| e.to_string())?;
        let it = st
            .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
            .map_err(|e| e.to_string())?;
        it.collect::<Result<_, _>>().map_err(|e| e.to_string())?
    };
    {
        let mut up = c
            .prepare(
                "UPDATE entries SET is_dir=0, parent_key=?2, depth=?3, name=?4, path_key=?5,
                 name_key=?6, sort_key=?7, indexed_at=?8 WHERE id=?1",
            )
            .map_err(|e| e.to_string())?;
        for (id, path) in &rows {
            let name = basename_of(path).to_string();
            let pk = norm_key(path);
            up.execute(params![
                id,
                norm_key(parent_of(path)),
                depth_of(path),
                name,
                pk,
                norm_key(basename_of(path)),
                pk.clone(),
                "",
            ])
            .map_err(|e| format!("backfill id {id}: {e}"))?;
        }
    }

    // 3. Dedupe by path_key BEFORE the unique index (two stored paths can casefold-collapse on a
    //    case-sensitive source FS). Keep newest mtime_iso, DETERMINISTIC tiebreak MAX(id).
    c.execute_batch(
        "DELETE FROM entries WHERE id NOT IN (
           SELECT id FROM (
             SELECT id, ROW_NUMBER() OVER (
               PARTITION BY path_key ORDER BY mtime_iso DESC, id DESC) AS rn
             FROM entries
           ) WHERE rn = 1
         );",
    )
    .map_err(|e| format!("dedupe: {e}"))?;

    // 4. Synthesize dir rows: ancestor_dirs(all file paths) minus any path already present.
    let file_paths: Vec<String> = rows.iter().map(|(_, p)| p.clone()).collect();
    let mut present: std::collections::HashSet<String> =
        file_paths.iter().map(|p| norm_key(p)).collect();
    {
        let mut ins = c.prepare(INSERT_SQL).map_err(|e| e.to_string())?;
        for d in ancestor_dirs(&file_paths) {
            let dk = norm_key(&d);
            if present.contains(&dk) {
                continue; // a file already occupies this path_key (dir-file collision) → skip
            }
            present.insert(dk);
            RowValues::dir(&d, "").bind_exec(&mut ins)?;
        }
    }

    // 5. Build the 4 v2 indexes (UNIQUE path_key last) + resolve links.
    c.execute_batch(CREATE_V2_INDEXES).map_err(|e| format!("v2 idx: {e}"))?;
    resolve_links(c)?;
    Ok(())
}

// ── shared resolution + provenance ───────────────────────────────────────────────────────────────

/// Resolve `parent_id` (the dir row whose `path_key == this.parent_key`; NULL at the top level) and
/// `child_count` (COUNT of rows with `parent_key == this.path_key`, dirs only). Pure SQL over the
/// ALREADY-COMPUTED key columns (no `norm_key()` call in SQL). Shared by Path A and Path B.
fn resolve_links(c: &Connection) -> Result<(), String> {
    c.execute(
        "UPDATE entries SET parent_id = (
           SELECT p.id FROM entries p WHERE p.path_key = entries.parent_key AND p.is_dir = 1)",
        [],
    )
    .map_err(|e| format!("resolve parent_id: {e}"))?;
    c.execute(
        "UPDATE entries SET child_count = (
           SELECT COUNT(*) FROM entries c WHERE c.parent_key = entries.path_key)
         WHERE is_dir = 1",
        [],
    )
    .map_err(|e| format!("resolve child_count: {e}"))?;
    Ok(())
}

/// Read the current `schema_meta.index_generation` (0 if absent/unparseable).
fn current_generation(c: &Connection) -> u64 {
    c.query_row("SELECT value FROM schema_meta WHERE key='index_generation'", [], |r| {
        r.get::<_, String>(0)
    })
    .ok()
    .and_then(|s| s.parse().ok())
    .unwrap_or(0)
}

fn write_schema_meta(
    c: &Connection,
    writer_tag: &str,
    now_iso: &str,
    generation: u64,
) -> Result<(), String> {
    let mut st = c
        .prepare("INSERT INTO schema_meta(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        .map_err(|e| e.to_string())?;
    let rows: &[(&str, String)] = &[
        ("schema_version", SCHEMA_VERSION.to_string()),
        ("norm_version", NORM_VERSION.to_string()),
        ("writer", writer_tag.to_string()),
        ("built_at", now_iso.to_string()),
        ("index_generation", generation.to_string()),
        ("last_index_time", now_iso.to_string()),
    ];
    for (k, v) in rows {
        // Don't clobber a real built_at/last_index_time with an empty migrate-time stamp.
        if now_iso.is_empty() && matches!(*k, "built_at" | "last_index_time") {
            st.execute(params![k, ""]).map_err(|e| e.to_string())?;
        } else {
            st.execute(params![k, v]).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// Read `schema_meta.schema_version` (or fall back to the `PRAGMA user_version` int as a string).
pub fn schema_version(conn: &Connection) -> Result<Option<String>, String> {
    if let Some(v) = conn
        .query_row(
            "SELECT value FROM schema_meta WHERE key='schema_version'",
            [],
            |r| r.get::<_, String>(0),
        )
        .optional()
        .map_err(|e| e.to_string())?
    {
        return Ok(Some(v));
    }
    let uv: i64 = conn
        .query_row("PRAGMA user_version", [], |r| r.get(0))
        .map_err(|e| e.to_string())?;
    Ok(Some(uv.to_string()))
}

// ── helpers ──────────────────────────────────────────────────────────────────────────────────────

pub(crate) fn column_exists(c: &Connection, table: &str, col: &str) -> Result<bool, String> {
    let mut st = c
        .prepare(&format!("PRAGMA table_info({table})"))
        .map_err(|e| e.to_string())?;
    let found = st
        .query_map([], |r| r.get::<_, String>(1))
        .map_err(|e| e.to_string())?
        .filter_map(|r| r.ok())
        .any(|name| name == col);
    Ok(found)
}

/// Run `f` inside a `BEGIN IMMEDIATE` transaction (takes the WAL write lock up front, avoiding a
/// read→write upgrade `SQLITE_BUSY`); COMMIT on Ok, ROLLBACK on Err. Uses raw `execute_batch` rather
/// than `unchecked_transaction` so we get IMMEDIATE (not DEFERRED) on a `&Connection`.
pub fn immediate_txn<T>(
    conn: &Connection,
    f: impl FnOnce(&Connection) -> Result<T, String>,
) -> Result<T, String> {
    conn.execute_batch("BEGIN IMMEDIATE")
        .map_err(|e| format!("BEGIN IMMEDIATE: {e}"))?;
    match f(conn) {
        Ok(v) => {
            conn.execute_batch("COMMIT")
                .map_err(|e| format!("COMMIT: {e}"))?;
            Ok(v)
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

/// Create a fresh, empty v2 db schema on `conn` (used by the writer's cold first run and by tests).
pub fn create_fresh_v2(conn: &Connection) -> Result<(), String> {
    migrate_to_v2(conn)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mem_v2() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        migrate_to_v2(&c).unwrap();
        c
    }

    fn entry(path: &str, meta: &str) -> ManifestEntry {
        serde_json::from_str(&format!(
            r#"{{"path":"{path}","category":"data_table","ext":"csv","size_bytes":10,
                "mtime_iso":"2026-04-20T07:21:29Z","is_symlink":false,"symlink_target":null,
                "symlink_ok":null,"extractor":"csv","tags":[],"meta":{meta}}}"#
        ))
        .unwrap()
    }

    #[test]
    fn ancestor_dirs_dedupes_by_key_and_sorts() {
        let files = vec![
            "a/b/c.csv".to_string(),
            "a/b/d.csv".to_string(),
            "a/e.csv".to_string(),
            "top.csv".to_string(),
        ];
        assert_eq!(ancestor_dirs(&files), vec!["a".to_string(), "a/b".to_string()]);
    }

    #[test]
    fn ancestor_dirs_merges_case_variants_into_one_node() {
        // A/x.csv and a/y.csv → ONE dir node keyed by path_key; canonical spelling = first sorted.
        let files = vec!["A/x.csv".to_string(), "a/y.csv".to_string()];
        let dirs = ancestor_dirs(&files);
        assert_eq!(dirs, vec!["A".to_string()]); // "A" sorts before "a"
        assert_eq!(norm_key("A"), norm_key("a"));
    }

    #[test]
    fn ingest_builds_v2_rows_keys_and_dirs() {
        let c = mem_v2();
        let entries = vec![
            entry("a/b/atlas.csv", r#"{"n_obs":2500,"n_vars":5}"#),
            entry("a/readme.md", "{}"),
            entry("top.csv", r#"{"n_obs":1.5}"#), // float → n_obs NULL
        ];
        let stats = ingest_entries(&c, &entries, "lens-rust", "2026-07-03T00:00:00Z", 1).unwrap();
        assert_eq!(stats.files, 3);
        assert_eq!(stats.dirs, 2, "a and a/b synthesized");

        let user_version: i64 = c.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
        // Assert against the CONSTANT, not a literal: the schema version advances (v3 added
        // `figure_text`) and a hardcoded number re-breaks this test on every bump.
        assert_eq!(user_version, USER_VERSION);

        // file rows only excluded elsewhere; here count all
        let total: i64 = c.query_row("SELECT COUNT(*) FROM entries", [], |r| r.get(0)).unwrap();
        assert_eq!(total, 5);
        let dirs: i64 = c.query_row("SELECT COUNT(*) FROM entries WHERE is_dir=1", [], |r| r.get(0)).unwrap();
        assert_eq!(dirs, 2);

        // keys + parent linkage
        let (pk, parent_key, depth, name): (String, String, i64, String) = c
            .query_row(
                "SELECT path_key,parent_key,depth,name FROM entries WHERE path='a/b/atlas.csv'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .unwrap();
        assert_eq!(pk, norm_key("a/b/atlas.csv"));
        assert_eq!(parent_key, norm_key("a/b"));
        assert_eq!(depth, 2);
        assert_eq!(name, "atlas.csv");

        // n_obs denorm: integer kept, float dropped
        let nobs: Option<i64> = c
            .query_row("SELECT n_obs FROM entries WHERE path='a/b/atlas.csv'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(nobs, Some(2500));
        let nobs_top: Option<i64> = c
            .query_row("SELECT n_obs FROM entries WHERE path='top.csv'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(nobs_top, None);

        // parent_id resolves to the dir row; child_count on 'a/b' == 1
        let (parent_id, is_dir): (Option<i64>, i64) = c
            .query_row("SELECT parent_id,is_dir FROM entries WHERE path='a/b'", [], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap();
        assert_eq!(is_dir, 1);
        let a_id: i64 = c.query_row("SELECT id FROM entries WHERE path='a'", [], |r| r.get(0)).unwrap();
        assert_eq!(parent_id, Some(a_id));
        let cc: i64 = c.query_row("SELECT child_count FROM entries WHERE path='a/b'", [], |r| r.get(0)).unwrap();
        assert_eq!(cc, 1);

        // schema_meta stamped
        let sv: String = c.query_row("SELECT value FROM schema_meta WHERE key='schema_version'", [], |r| r.get(0)).unwrap();
        assert_eq!(sv, "2");
        let nv: String = c.query_row("SELECT value FROM schema_meta WHERE key='norm_version'", [], |r| r.get(0)).unwrap();
        assert_eq!(nv, "1");
    }

    #[test]
    fn ingest_is_idempotent_and_prunes_removed_files() {
        let c = mem_v2();
        let e1 = vec![entry("a/b/x.csv", "{}"), entry("a/c/y.csv", "{}")];
        let s1 = ingest_entries(&c, &e1, "lens-rust", "t1", 1).unwrap();
        let ids1: Vec<(String, i64)> = collect_ids(&c);

        // Re-ingest identical → same row set, SAME ids (idempotent UPSERT).
        let s2 = ingest_entries(&c, &e1, "lens-rust", "t2", 2).unwrap();
        assert_eq!(s1.files, s2.files);
        assert_eq!(ids1, collect_ids(&c), "ids stable across re-ingest");

        // Ingest a manifest missing a/c/y.csv → the file AND its now-empty a/c dir are pruned.
        let e2 = vec![entry("a/b/x.csv", "{}")];
        ingest_entries(&c, &e2, "lens-rust", "t3", 3).unwrap();
        let remaining: i64 = c.query_row("SELECT COUNT(*) FROM entries WHERE path='a/c/y.csv'", [], |r| r.get(0)).unwrap();
        assert_eq!(remaining, 0);
        let dir_ac: i64 = c.query_row("SELECT COUNT(*) FROM entries WHERE path='a/c'", [], |r| r.get(0)).unwrap();
        assert_eq!(dir_ac, 0, "empty dir pruned when its only file is gone");
    }

    #[test]
    fn case_variant_dirs_merge_and_both_files_reachable() {
        let c = mem_v2();
        let entries = vec![entry("A/x.csv", "{}"), entry("a/y.csv", "{}")];
        ingest_entries(&c, &entries, "lens-rust", "t", 1).unwrap();
        // exactly ONE dir node
        let ndir: i64 = c.query_row("SELECT COUNT(*) FROM entries WHERE is_dir=1", [], |r| r.get(0)).unwrap();
        assert_eq!(ndir, 1);
        // both files carry parent_key == norm_key("A") == norm_key("a"); list_children(parent_key) reaches both
        let reach: i64 = c
            .query_row(
                "SELECT COUNT(*) FROM entries WHERE is_dir=0 AND parent_key=?1",
                params![norm_key("A")],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(reach, 2);
    }

    #[test]
    fn migrate_v1_preserves_file_ids_and_is_idempotent() {
        // Build a legacy v1 db by hand.
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(
            "CREATE TABLE entries(
               id INTEGER PRIMARY KEY, path TEXT, category TEXT, ext TEXT, size_bytes INTEGER,
               mtime_iso TEXT, is_symlink INTEGER, symlink_target TEXT, symlink_ok INTEGER,
               extractor TEXT, tags TEXT, error TEXT, n_obs INTEGER, n_vars INTEGER, meta TEXT);
             CREATE INDEX idx_entries_path ON entries(path);
             CREATE VIRTUAL TABLE fts USING fts5(path, searchtext, content='', tokenize='unicode61');",
        )
        .unwrap();
        for (id, path) in [(1, "a/b/x.csv"), (2, "a/y.csv"), (3, "top.csv")] {
            c.execute(
                "INSERT INTO entries(id,path,category,ext,size_bytes,mtime_iso,extractor,tags,meta)
                 VALUES(?1,?2,'data_table','csv',10,'2026-01-01T00:00:00Z','csv','[]','{}')",
                params![id, path],
            )
            .unwrap();
        }
        migrate_to_v2(&c).unwrap();
        let uv: i64 = c.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
        assert_eq!(uv, USER_VERSION);
        // file ids preserved
        for (id, path) in [(1, "a/b/x.csv"), (2, "a/y.csv"), (3, "top.csv")] {
            let got: i64 = c.query_row("SELECT id FROM entries WHERE path=?1", params![path], |r| r.get(0)).unwrap();
            assert_eq!(got, id, "file id preserved for {path}");
        }
        // dir rows synthesized (a, a/b)
        let dirs: i64 = c.query_row("SELECT COUNT(*) FROM entries WHERE is_dir=1", [], |r| r.get(0)).unwrap();
        assert_eq!(dirs, 2);
        // parent linkage + keys populated
        let pk: String = c.query_row("SELECT path_key FROM entries WHERE path='a/b/x.csv'", [], |r| r.get(0)).unwrap();
        assert_eq!(pk, norm_key("a/b/x.csv"));

        // idempotent second run
        migrate_to_v2(&c).unwrap();
        let total: i64 = c.query_row("SELECT COUNT(*) FROM entries", [], |r| r.get(0)).unwrap();
        assert_eq!(total, 5);
    }

    #[test]
    fn path_a_and_path_b_produce_equivalent_row_sets() {
        // Path A: fresh v2 + ingest.
        let a = mem_v2();
        let entries = vec![entry("d/e/f.csv", "{}"), entry("d/g.csv", "{}"), entry("h.csv", "{}")];
        ingest_entries(&a, &entries, "lens-rust", "t", 1).unwrap();

        // Path B: legacy v1 with the same files, migrated.
        let b = Connection::open_in_memory().unwrap();
        b.execute_batch(
            "CREATE TABLE entries(
               id INTEGER PRIMARY KEY, path TEXT, category TEXT, ext TEXT, size_bytes INTEGER,
               mtime_iso TEXT, is_symlink INTEGER, symlink_target TEXT, symlink_ok INTEGER,
               extractor TEXT, tags TEXT, error TEXT, n_obs INTEGER, n_vars INTEGER, meta TEXT);",
        )
        .unwrap();
        for (i, e) in entries.iter().enumerate() {
            b.execute(
                "INSERT INTO entries(id,path,category,ext,size_bytes,mtime_iso,extractor,tags,meta)
                 VALUES(?1,?2,'data_table','csv',10,'2026-04-20T07:21:29Z','csv','[]','{}')",
                params![(i + 1) as i64, e.path],
            )
            .unwrap();
        }
        migrate_to_v2(&b).unwrap();

        let key_set = |c: &Connection| -> Vec<(String, i64, String)> {
            let mut st = c
                .prepare("SELECT path_key, is_dir, parent_key FROM entries ORDER BY path_key, is_dir")
                .unwrap();
            st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
                .unwrap()
                .map(|r| r.unwrap())
                .collect()
        };
        assert_eq!(key_set(&a), key_set(&b), "Path A and Path B yield the same (path_key,is_dir,parent_key) set");
    }

    fn collect_ids(c: &Connection) -> Vec<(String, i64)> {
        let mut st = c.prepare("SELECT path, id FROM entries ORDER BY path").unwrap();
        st.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
            .unwrap()
            .map(|r| r.unwrap())
            .collect()
    }
}
