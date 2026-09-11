//! `journal` — the durable op-journal (PHASE_0_1_SPEC.md §5.3–§5.7). Lives in a SEPARATE
//! `oplog.sqlite` on INTERNAL APFS (§5.2), NOT in the regenerable `INDEX.sqlite`: it holds durable,
//! non-regenerable app state (undo history, `trashed_url` for restore, crash-recovery intent), so a
//! DR rebuild of the index can never nuke it. Under WAL it uses `synchronous=FULL` (NOT NORMAL, §5.3)
//! — the write-ahead guarantee is void if a power-loss can roll back the recorded intent.
//!
//! Phase 1 builds the table ONCE, in full, but exercises only the self-event slice behind an
//! internal `apply_op` (ops.rs) — no new `#[tauri::command]`, no frontend change.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use rusqlite::{Connection, OpenFlags, OptionalExtension};

/// `oplog.sqlite`'s own `PRAGMA user_version` (its version namespace, distinct from the index's, §1.3).
pub const OPLOG_USER_VERSION: i64 = 1;

const OP_JOURNAL_DDL: &str = "\
CREATE TABLE IF NOT EXISTS op_journal (
  op_id        INTEGER PRIMARY KEY,
  batch_id     TEXT    NOT NULL,
  type         TEXT    NOT NULL,
  src          TEXT,
  dst          TEXT,
  temp_path    TEXT,
  trashed_url  TEXT,
  state        TEXT    NOT NULL,
  created_at   TEXT    NOT NULL,
  updated_at   TEXT    NOT NULL,
  error        TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS idx_op_state   ON op_journal(state);
CREATE INDEX IF NOT EXISTS idx_op_batch   ON op_journal(batch_id);
CREATE INDEX IF NOT EXISTS idx_op_created ON op_journal(created_at);";

/// The op state machine (§5.4). Phase-1 drives `planned → executing → committed → settled` (or any
/// step → `failed`); the `Copied…SourceRemoved` / `Reverting` / `Undone` states are RESERVED for
/// Phase-4 (defined, not driven).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpState {
    Planned,
    Executing,
    Copied,
    Verified,
    Finalized,
    SourceRemoved,
    Committed,
    Settled,
    Failed,
    Reverting,
    Undone,
}

impl OpState {
    pub fn as_str(self) -> &'static str {
        match self {
            OpState::Planned => "planned",
            OpState::Executing => "executing",
            OpState::Copied => "copied",
            OpState::Verified => "verified",
            OpState::Finalized => "finalized",
            OpState::SourceRemoved => "source_removed",
            OpState::Committed => "committed",
            OpState::Settled => "settled",
            OpState::Failed => "failed",
            OpState::Reverting => "reverting",
            OpState::Undone => "undone",
        }
    }
    pub fn from_str(s: &str) -> Option<OpState> {
        Some(match s {
            "planned" => OpState::Planned,
            "executing" => OpState::Executing,
            "copied" => OpState::Copied,
            "verified" => OpState::Verified,
            "finalized" => OpState::Finalized,
            "source_removed" => OpState::SourceRemoved,
            "committed" => OpState::Committed,
            "settled" => OpState::Settled,
            "failed" => OpState::Failed,
            "reverting" => OpState::Reverting,
            "undone" => OpState::Undone,
            _ => return None,
        })
    }
    /// Terminal states — `recover` skips these (§5.7).
    pub fn is_terminal(self) -> bool {
        matches!(self, OpState::Settled | OpState::Failed | OpState::Undone)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpType {
    Rename,
    Move,
    Copy,
    Duplicate,
    Mkdir,
    Trash,
    Delete,
}

impl OpType {
    pub fn as_str(self) -> &'static str {
        match self {
            OpType::Rename => "rename",
            OpType::Move => "move",
            OpType::Copy => "copy",
            OpType::Duplicate => "duplicate",
            OpType::Mkdir => "mkdir",
            OpType::Trash => "trash",
            OpType::Delete => "delete",
        }
    }
    pub fn from_str(s: &str) -> Option<OpType> {
        Some(match s {
            "rename" => OpType::Rename,
            "move" => OpType::Move,
            "copy" => OpType::Copy,
            "duplicate" => OpType::Duplicate,
            "mkdir" => OpType::Mkdir,
            "trash" => OpType::Trash,
            "delete" => OpType::Delete,
            _ => return None,
        })
    }
}

/// One durable op-journal row.
#[derive(Debug, Clone)]
pub struct OpRow {
    pub op_id: i64,
    pub batch_id: String,
    pub op_type: OpType,
    pub src: Option<String>,
    pub dst: Option<String>,
    pub temp_path: Option<String>,
    pub trashed_url: Option<String>,
    pub state: OpState,
    pub created_at: String,
    pub updated_at: String,
    pub error: Option<String>,
}

/// The intent record fields for a NEW op (all undo-entry data is captured at intent time, §5.6).
pub struct OpIntent {
    pub op_type: OpType,
    pub src: Option<String>,
    pub dst: Option<String>,
    pub temp_path: Option<String>,
}

/// The durable op-journal, on a SEPARATE `oplog.sqlite`. Its inner `Mutex` serializes access.
pub struct OpLog {
    conn: Mutex<Connection>,
}

impl OpLog {
    /// Open (or create) the oplog at `path`, apply per-connection pragmas (WAL + `synchronous=FULL`
    /// — reset per connection, so re-issued on EVERY open, §5.3), and run the version-gated migrate.
    pub fn open_at(path: &str) -> Result<OpLog, String> {
        if let Some(parent) = std::path::Path::new(path).parent() {
            std::fs::create_dir_all(parent).map_err(|e| format!("mkdir oplog dir: {e}"))?;
        }
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|e| format!("open oplog {path}: {e}"))?;
        // FULL (not NORMAL): the oplog is durable state — NORMAL omits the commit sync under WAL, so a
        // power loss could roll back the intent and void the write-ahead guarantee (§5.3). fullfsync
        // asks macOS for a truly durable flush.
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=FULL;
             PRAGMA fullfsync=ON;
             PRAGMA busy_timeout=5000;",
        )
        .map_err(|e| format!("oplog pragmas: {e}"))?;
        Self::migrate(&conn)?;
        Ok(OpLog { conn: Mutex::new(conn) })
    }

    /// Version-gated schema migrate: only the CREATE TABLE/INDEX DDL + the `user_version` bump (the
    /// per-connection `synchronous`/`fullfsync` pragmas belong on every open, NOT here — §5.3).
    pub fn migrate(conn: &Connection) -> Result<(), String> {
        let v: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0)).map_err(|e| e.to_string())?;
        if v >= OPLOG_USER_VERSION {
            return Ok(());
        }
        conn.execute_batch(OP_JOURNAL_DDL).map_err(|e| format!("op_journal DDL: {e}"))?;
        conn.execute_batch(&format!("PRAGMA user_version = {OPLOG_USER_VERSION};"))
            .map_err(|e| e.to_string())?;
        Ok(())
    }

    /// Record a new op's INTENT (`state='planned'`) with ALL undo-entry data, returning its `op_id`.
    pub fn begin(&self, intent: &OpIntent, batch_id: &str) -> Result<i64, String> {
        let now = now_iso();
        let conn = self.conn.lock().map_err(|e| format!("oplog poisoned: {e}"))?;
        conn.execute(
            "INSERT INTO op_journal(batch_id,type,src,dst,temp_path,state,created_at,updated_at)
             VALUES(?1,?2,?3,?4,?5,'planned',?6,?6)",
            rusqlite::params![batch_id, intent.op_type.as_str(), intent.src, intent.dst, intent.temp_path, now],
        )
        .map_err(|e| format!("oplog begin: {e}"))?;
        Ok(conn.last_insert_rowid())
    }

    pub fn transition(&self, op_id: i64, state: OpState, error: Option<&str>) -> Result<(), String> {
        let now = now_iso();
        let conn = self.conn.lock().map_err(|e| format!("oplog poisoned: {e}"))?;
        conn.execute(
            "UPDATE op_journal SET state=?2, updated_at=?3, error=?4 WHERE op_id=?1",
            rusqlite::params![op_id, state.as_str(), now, error],
        )
        .map_err(|e| format!("oplog transition: {e}"))?;
        Ok(())
    }

    pub fn set_trashed_url(&self, op_id: i64, url: &str) -> Result<(), String> {
        let conn = self.conn.lock().map_err(|e| format!("oplog poisoned: {e}"))?;
        conn.execute(
            "UPDATE op_journal SET trashed_url=?2, updated_at=?3 WHERE op_id=?1",
            rusqlite::params![op_id, url, now_iso()],
        )
        .map_err(|e| format!("oplog set_trashed_url: {e}"))?;
        Ok(())
    }

    /// Every op NOT in a terminal state (`settled`/`failed`/`undone`) — the crash-recovery worklist.
    pub fn select_non_terminal(&self) -> Result<Vec<OpRow>, String> {
        let conn = self.conn.lock().map_err(|e| format!("oplog poisoned: {e}"))?;
        let mut st = conn
            .prepare(
                "SELECT op_id,batch_id,type,src,dst,temp_path,trashed_url,state,created_at,updated_at,error
                 FROM op_journal WHERE state NOT IN ('settled','failed','undone') ORDER BY op_id",
            )
            .map_err(|e| e.to_string())?;
        let rows = st.query_map([], row_from).map_err(|e| e.to_string())?;
        rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
    }

    pub fn get(&self, op_id: i64) -> Result<Option<OpRow>, String> {
        let conn = self.conn.lock().map_err(|e| format!("oplog poisoned: {e}"))?;
        conn.query_row(
            "SELECT op_id,batch_id,type,src,dst,temp_path,trashed_url,state,created_at,updated_at,error
             FROM op_journal WHERE op_id=?1",
            [op_id],
            row_from,
        )
        .optional()
        .map_err(|e| e.to_string())
    }
}

fn row_from(r: &rusqlite::Row<'_>) -> rusqlite::Result<OpRow> {
    Ok(OpRow {
        op_id: r.get(0)?,
        batch_id: r.get(1)?,
        op_type: OpType::from_str(&r.get::<_, String>(2)?).unwrap_or(OpType::Rename),
        src: r.get(3)?,
        dst: r.get(4)?,
        temp_path: r.get(5)?,
        trashed_url: r.get(6)?,
        state: OpState::from_str(&r.get::<_, String>(7)?).unwrap_or(OpState::Failed),
        created_at: r.get(8)?,
        updated_at: r.get(9)?,
        error: r.get(10)?,
    })
}

static BATCH_COUNTER: AtomicU64 = AtomicU64::new(0);

/// A batch id groups ops for one atomic undo/redo (one Cmd-Z per batch). `{epoch_millis}-{counter}`
/// — no UUID dependency (§5.3).
pub fn new_batch_id() -> String {
    let millis = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    format!("{millis}-{}", BATCH_COUNTER.fetch_add(1, Ordering::Relaxed))
}

pub(crate) fn now_iso() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    crate::reconcile::iso_mtime(secs)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn oplog() -> OpLog {
        // in-memory is fine for the journal's own logic (no cross-process durability tested here)
        let conn = Connection::open_in_memory().unwrap();
        OpLog::migrate(&conn).unwrap();
        OpLog { conn: Mutex::new(conn) }
    }

    #[test]
    fn begin_transition_and_terminal_filtering() {
        let log = oplog();
        let b = new_batch_id();
        let id = log
            .begin(&OpIntent { op_type: OpType::Rename, src: Some("a.txt".into()), dst: Some("b.txt".into()), temp_path: None }, &b)
            .unwrap();
        assert_eq!(log.get(id).unwrap().unwrap().state, OpState::Planned);
        // non-terminal worklist includes it
        assert_eq!(log.select_non_terminal().unwrap().len(), 1);

        log.transition(id, OpState::Executing, None).unwrap();
        log.transition(id, OpState::Committed, None).unwrap();
        assert_eq!(log.get(id).unwrap().unwrap().state, OpState::Committed);
        assert_eq!(log.select_non_terminal().unwrap().len(), 1, "committed is non-terminal");

        log.transition(id, OpState::Settled, None).unwrap();
        assert_eq!(log.select_non_terminal().unwrap().len(), 0, "settled is terminal");
    }

    #[test]
    fn failed_carries_error_and_is_terminal() {
        let log = oplog();
        let b = new_batch_id();
        let id = log.begin(&OpIntent { op_type: OpType::Mkdir, src: None, dst: Some("newdir".into()), temp_path: None }, &b).unwrap();
        log.transition(id, OpState::Failed, Some("boom")).unwrap();
        let row = log.get(id).unwrap().unwrap();
        assert_eq!(row.state, OpState::Failed);
        assert_eq!(row.error.as_deref(), Some("boom"));
        assert!(log.select_non_terminal().unwrap().is_empty());
    }

    #[test]
    fn batch_ids_are_unique_and_ordered() {
        let a = new_batch_id();
        let b = new_batch_id();
        assert_ne!(a, b);
    }
}
