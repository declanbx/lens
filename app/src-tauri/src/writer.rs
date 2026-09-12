//! `writer` — the WAL connection builders + the single `IndexWriter` (PHASE_0_1_SPEC.md §3). Retires
//! the old read-only-index invariant: Rust becomes the SOLE writer of `INDEX.sqlite` (WAL mode) and
//! owns the v2 schema/tree/keys. Depends on the SQLite bump past the WAL-reset bug (§3.0) — the
//! reader-pool + writer design corrupts a WAL db on SQLite ≤ 3.51.2.
//!
//! Two connection kinds share [`apply_common_pragmas`]:
//!   * [`connect_reader`] — READ_WRITE (NOT strictly read-only, so it can join the WAL `-shm`
//!     handshake) clamped to read-only USAGE by `PRAGMA query_only=ON` (rejects writes at prepare
//!     time). The resident [`Db`](crate::db::Db) pool is built from these.
//!   * [`connect_writer`] — READ_WRITE|CREATE, the ONE writer; runs [`migrate_to_v2`] on open and
//!     is guarded by a process-exclusive `.lens-writer.lock`.

use std::os::unix::io::AsRawFd;
use std::path::Path;
use std::sync::Mutex;

use rusqlite::{Connection, OpenFlags};

use crate::tree;

/// Cap the `-wal` file after a checkpoint truncation so a burst of writes can't leave a giant WAL on
/// the (space-constrained) exFAT mount. Autocheckpoint (1000 pages) keeps it small in steady state.
const JOURNAL_SIZE_LIMIT_BYTES: i64 = 32 * 1024 * 1024;

/// Pragmas shared by every connection (§3.1). `journal_mode=WAL` is STICKY in the db header, so a
/// reader "inherits" it; setting it again is a harmless confirm. `synchronous=NORMAL` is the
/// WAL-recommended crash-safe setting for the REGENERABLE index (a power loss risks only the last
/// commit — the durable op-journal uses FULL instead, §5.3).
pub fn apply_common_pragmas(conn: &Connection) -> Result<(), String> {
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         PRAGMA busy_timeout=5000;
         PRAGMA wal_autocheckpoint=1000;",
    )
    .map_err(|e| format!("apply_common_pragmas: {e}"))
}

/// A RESIDENT read connection. READ_WRITE (so it can create/write the `-shm`) clamped to read-only
/// USAGE by `query_only=ON`, preserving the statement-level read-only guarantee without the
/// WAL-readability problem a strictly-`READ_ONLY` handle has.
pub fn connect_reader(index_path: &str) -> Result<Connection, String> {
    let conn = Connection::open_with_flags(
        index_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| format!("connect_reader {index_path}: {e}"))?;
    apply_common_pragmas(&conn)?;
    conn.execute_batch("PRAGMA query_only=ON;")
        .map_err(|e| format!("query_only: {e}"))?;
    Ok(conn)
}

/// The WRITER connection — the ONLY writer of `INDEX.sqlite`. READ_WRITE|CREATE (creates a missing
/// file on cold first run). Runs [`migrate_to_v2`] on open (fresh db → v2 schema; legacy v1 → in-
/// place upgrade). `NO_MUTEX` is sound because [`IndexWriter`]'s inner `Mutex` serializes all access.
pub fn connect_writer(index_path: &str) -> Result<Connection, String> {
    let conn = Connection::open_with_flags(
        index_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| format!("connect_writer {index_path}: {e}"))?;
    apply_common_pragmas(&conn)?;
    conn.execute_batch(&format!("PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT_BYTES};"))
        .map_err(|e| format!("journal_size_limit: {e}"))?;
    tree::migrate_to_v2(&conn)?;
    Ok(conn)
}

/// Best-effort v2 migration via a TRANSIENT writable connection (§2.9): ensures the db is v2 before
/// the `query_only` reader pool (which cannot migrate) queries it, so a not-yet-upgraded v1 db never
/// makes a `WHERE is_dir = 0` read raise `no such column`. Silently degrades if the file is missing
/// or the disk is read-only. Idempotent + `BEGIN IMMEDIATE`-guarded → racing a real writer is safe.
pub fn ensure_v2_if_writable(index_path: &str) {
    if let Ok(conn) = Connection::open_with_flags(
        index_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        let _ = apply_common_pragmas(&conn);
        let _ = tree::migrate_to_v2(&conn);
    }
}

// ── single-writer lock: process-identity file + advisory flock (§3.9) ───────────────────────────

/// A process-exclusive guard on `<index_dir>/.lens-writer.lock`. Backed by BOTH `flock(LOCK_EX)` AND
/// a recorded process IDENTITY, because exFAT `flock` may be a silent no-op (V9): if `flock` works it
/// alone is authoritative; if it is a no-op, a recorded LIVE foreign owner still forces a refusal.
/// Released (flock dropped, record cleared) when this value drops.
///
/// ⚠ The record is `pid + that process's START TIME`, never a bare pid. A bare pid is not an identity:
/// pids are recycled, and a reboot restarts the counter, so a day-old lock file naming pid 700 matched
/// a freshly-spawned system daemon after the macOS 27 update and locked the app out of its own index
/// (no writer ⇒ no watcher ⇒ dead live-index AND a failing Rebuild). A start time makes the identity
/// unforgeable by reuse: a recycled pid always has a later start time than the one recorded.
pub struct WriterLock {
    // Holds the advisory flock for its lifetime; releasing = dropping the File (closes the fd).
    file: std::fs::File,
}

/// What the lock file records. `Full` is written by this version; `Legacy` is a bare pid written by a
/// pre-fix build (still read so an upgrade in place is not a hard failure).
#[derive(Debug, PartialEq)]
enum LockRecord {
    Legacy { pid: u32 },
    Full { pid: u32, start_sec: u64, start_usec: u64 },
}

impl LockRecord {
    fn pid(&self) -> u32 {
        match *self {
            LockRecord::Legacy { pid } | LockRecord::Full { pid, .. } => pid,
        }
    }
}

/// The live facts about a pid, from `proc_pidinfo(PROC_PIDTBSDINFO)`. `None` ⇒ no such process (or one
/// we are not allowed to inspect — which equally means it is not one of OUR Lens instances).
struct ProcInfo {
    start_sec: u64,
    start_usec: u64,
    uid: u32,
    name: String,
}

fn cstr_field(buf: &[libc::c_char]) -> String {
    let bytes: Vec<u8> = buf.iter().take_while(|&&c| c != 0).map(|&c| c as u8).collect();
    String::from_utf8_lossy(&bytes).into_owned()
}

fn proc_info(pid: u32) -> Option<ProcInfo> {
    if pid == 0 {
        return None;
    }
    let mut bsd: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let sz = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    let rc = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDTBSDINFO,
            0,
            &mut bsd as *mut libc::proc_bsdinfo as *mut libc::c_void,
            sz,
        )
    };
    if rc != sz {
        return None;
    }
    let name = {
        let n = cstr_field(&bsd.pbi_name);
        if n.is_empty() { cstr_field(&bsd.pbi_comm) } else { n }
    };
    Some(ProcInfo { start_sec: bsd.pbi_start_tvsec, start_usec: bsd.pbi_start_tvusec, uid: bsd.pbi_uid, name })
}

/// Is the owner named by `rec` STILL the process that wrote the lock? Only then may we refuse.
/// Any other answer — process gone, pid recycled (start time differs), or a foreign-uid/non-Lens
/// process wearing a legacy bare pid — means the record is stale and the lock is free to take.
fn owner_still_live(rec: &LockRecord) -> bool {
    let Some(live) = proc_info(rec.pid()) else {
        return false; // no such process, or not ours to inspect ⇒ not a live Lens instance
    };
    match *rec {
        LockRecord::Full { start_sec, start_usec, .. } => {
            // Exact identity match. A recycled pid cannot forge the start time.
            live.start_sec == start_sec && live.start_usec == start_usec
        }
        LockRecord::Legacy { .. } => {
            // No start time to compare. Fall back to the strongest available evidence: a live Lens
            // instance runs as US and is called `lens`. Anything else (a root daemon that inherited
            // the pid, say) is treated as stale rather than locking the user out of their own index.
            live.uid == unsafe { libc::getuid() } && live.name == "lens"
        }
    }
}

/// The phrase every "someone else holds the write lock" message is built from.
///
/// `open` reports its failures as strings, and the UI has to tell a lock conflict (a second Lens
/// window — ordinary, recoverable by quitting it) apart from an unreadable folder (an unplugged
/// drive) so it can say which one happened. Rather than let a `contains("another Lens")` spread to
/// every caller, the phrase and the test for it live here together and cannot drift apart.
pub const LOCK_HELD_MARKER: &str = "another Lens instance";

/// True when `err` came from a lock already held by a different Lens process.
pub fn lock_held_by_other(err: &str) -> bool {
    err.contains(LOCK_HELD_MARKER)
}

impl WriterLock {
    pub fn acquire(index_dir: &Path) -> Result<WriterLock, String> {
        std::fs::create_dir_all(index_dir).map_err(|e| format!("mkdir {index_dir:?}: {e}"))?;
        let path = index_dir.join(".lens-writer.lock");
        let file = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&path)
            .map_err(|e| format!("open lock {path:?}: {e}"))?;

        let flock_ok = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0;
        if !flock_ok {
            // flock definitively says another process holds it.
            return Err(format!(
                "{LOCK_HELD_MARKER} is editing this index (flock held on {path:?})"
            ));
        }
        // flock succeeded — but it may be a no-op on exFAT, so verify the recorded owner is not still
        // running (a genuinely working flock would have failed above if one still held it).
        if let Some(rec) = read_record(&path) {
            if rec.pid() != std::process::id() && owner_still_live(&rec) {
                return Err(format!(
                    "{LOCK_HELD_MARKER} (pid {}) is editing this index",
                    rec.pid()
                ));
            }
        }
        write_record(&file)?;
        Ok(WriterLock { file })
    }
}

impl Drop for WriterLock {
    fn drop(&mut self) {
        // Clear our record so a later acquirer doesn't see a stale live-looking owner, then release
        // the flock by closing the fd (implicit on File drop).
        use std::io::{Seek, Write};
        if self.file.set_len(0).is_ok() {
            let _ = self.file.seek(std::io::SeekFrom::Start(0));
            let _ = self.file.write_all(b"");
        }
        let _ = unsafe { libc::flock(self.file.as_raw_fd(), libc::LOCK_UN) };
    }
}

/// Parse `"<pid> <start_sec> <start_usec>"`, or a pre-fix bare `"<pid>"`. Anything else ⇒ `None`
/// (an empty or garbled file is a released//corrupt lock, i.e. free).
fn read_record(path: &Path) -> Option<LockRecord> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut it = text.split_whitespace();
    let pid: u32 = it.next()?.parse().ok()?;
    match (it.next(), it.next()) {
        (Some(s), Some(u)) => {
            let start_sec = s.parse().ok()?;
            let start_usec = u.parse().ok()?;
            Some(LockRecord::Full { pid, start_sec, start_usec })
        }
        _ => Some(LockRecord::Legacy { pid }),
    }
}

/// Record OUR identity: pid + our own start time, so a future acquirer can tell us apart from
/// whatever later inherits our pid.
fn write_record(file: &std::fs::File) -> Result<(), String> {
    use std::io::{Seek, Write};
    let me = std::process::id();
    let line = match proc_info(me) {
        Some(p) => format!("{me} {} {}", p.start_sec, p.start_usec),
        // Should not happen (we can always inspect ourselves); a bare pid still beats no record.
        None => format!("{me}"),
    };
    let mut f = file;
    f.set_len(0).map_err(|e| e.to_string())?;
    f.seek(std::io::SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    write!(f, "{line}").map_err(|e| e.to_string())?;
    f.flush().map_err(|e| e.to_string())?;
    Ok(())
}

// ── IndexWriter — the single-writer state ────────────────────────────────────────────────────────

/// The SOLE writer of `INDEX.sqlite`. Its inner `Mutex` is what makes the connection's `NO_MUTEX`
/// sound and enforces the WAL single-writer rule in-process. `root` is kept in lockstep with the
/// reader on project switch (§3.9).
pub struct IndexWriter {
    conn: Mutex<Connection>,
    root: Mutex<String>,
    // Held for the writer's lifetime; released on drop (readers-first-writer-last shutdown, §0.5G).
    // Behind a Mutex so a project switch can release the old project's lock and acquire the new one
    // in place (the managed Arc<IndexWriter> can't be swapped).
    lock: Mutex<WriterLock>,
}

impl IndexWriter {
    /// Acquire the single-writer lock in the index's directory, open the writer connection (migrating
    /// to v2), and record `root`. Returns `Err` if another live instance holds the lock (the caller
    /// degrades to read-only mode, §3.9).
    pub fn open(root: &str, index_path: &str) -> Result<IndexWriter, String> {
        let dir = Path::new(index_path)
            .parent()
            .ok_or_else(|| format!("index path has no parent dir: {index_path}"))?;
        let lock = WriterLock::acquire(dir)?;
        let conn = connect_writer(index_path)?;
        Ok(IndexWriter {
            conn: Mutex::new(conn),
            root: Mutex::new(root.to_string()),
            lock: Mutex::new(lock),
        })
    }

    /// Run `f` under the writer lock (the reconciler flush + the ingest go through here). No blocking
    /// work (stat / subprocess) must run inside `f` — take it only for the batched transaction (§3.2).
    pub fn with_conn<T>(&self, f: impl FnOnce(&Connection) -> Result<T, String>) -> Result<T, String> {
        let conn = self.conn.lock().map_err(|e| format!("writer mutex poisoned: {e}"))?;
        f(&conn)
    }

    /// Ingest a Python manifest into the live index in one `BEGIN IMMEDIATE … COMMIT` (§2.6 Path A).
    pub fn ingest(
        &self,
        entries: &[tree::ManifestEntry],
        now_iso: &str,
        generation: u64,
    ) -> Result<tree::IngestStats, String> {
        self.with_conn(|c| tree::ingest_entries(c, entries, "lens-rust", now_iso, generation))
    }

    /// Fold the WAL back into the main db and truncate it — the writer OWNS checkpointing (a
    /// `query_only` reader cannot checkpoint). Called on graceful quit (writer last) and periodically.
    pub fn checkpoint_truncate(&self) -> Result<(), String> {
        self.with_conn(|c| {
            c.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")
                .map_err(|e| format!("wal_checkpoint(TRUNCATE): {e}"))
        })
    }

    pub fn root(&self) -> Result<String, String> {
        self.root.lock().map(|g| g.clone()).map_err(|e| format!("root mutex poisoned: {e}"))
    }

    /// Repoint the writer at a different project's index (project switch, §3.9): acquire the NEW
    /// project's single-writer lock, reconnect the connection (migrating the new index), and swap
    /// both in place — releasing the old project's lock (its `WriterLock` drops). The new lock is
    /// acquired BEFORE the old is released, so no window exists where neither is held. NOTE: the
    /// caller must move the reader pool + op-journal in lockstep. The caller must move the reader
    /// pool in lockstep.
    pub fn switch(&self, root: &str, index_path: &str) -> Result<(), String> {
        let dir = Path::new(index_path)
            .parent()
            .ok_or_else(|| format!("index path has no parent dir: {index_path}"))?;
        let new_lock = WriterLock::acquire(dir)?; // Err → another live instance edits the new project
        let conn = connect_writer(index_path)?;
        *self.conn.lock().map_err(|e| format!("writer mutex poisoned: {e}"))? = conn;
        *self.root.lock().map_err(|e| format!("root mutex poisoned: {e}"))? = root.to_string();
        *self.lock.lock().map_err(|e| format!("writer lock mutex poisoned: {e}"))? = new_lock;
        Ok(())
    }
}

#[cfg(test)]
mod tests {

    /// The status bar decides what to TELL the user from this predicate, so it has to hold for the
    /// real messages `acquire` produces — not for a string written out by hand in a test.
    #[test]
    fn a_second_acquire_is_recognised_as_a_lock_conflict() {
        let dir = scratch("lockmsg");
        let first = WriterLock::acquire(&dir).expect("first acquire should succeed");
        let err = match WriterLock::acquire(&dir) {
            Ok(_) => panic!("second acquire must fail while the first lock is held"),
            Err(e) => e,
        };
        assert!(
            lock_held_by_other(&err),
            "a real lock conflict was not recognised as one: {err}"
        );
        drop(first);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// ...and must not fire on an unrelated failure, or an unplugged drive would be reported as a
    /// second Lens window.
    #[test]
    fn an_unrelated_error_is_not_a_lock_conflict() {
        assert!(!lock_held_by_other("open lock \"/Volumes/gone/_repo_index\": No such file or directory"));
        assert!(!lock_held_by_other("disk I/O error"));
    }
    use super::*;
    use std::path::PathBuf;

    /// A unique scratch dir on the exFAT mount (CARGO_MANIFEST_DIR), cleaned by the caller.
    fn scratch(tag: &str) -> PathBuf {
        let d = Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("_writer_scratch_{tag}"));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn wal_roundtrip_reader_sees_writer_commits_without_reopen() {
        let dir = scratch("wal");
        let db = dir.join("INDEX.sqlite");
        let dbs = db.to_str().unwrap();

        let w = connect_writer(dbs).unwrap();
        w.execute("INSERT INTO entries(path,category,is_dir,path_key,parent_key,name,name_key,sort_key,indexed_at) \
                   VALUES('a.csv','data_table',0,'a.csv','','a.csv','a.csv','a.csv','t')", []).unwrap();

        // A separate reader connection sees the first commit.
        let r = connect_reader(dbs).unwrap();
        let n1: i64 = r.query_row("SELECT COUNT(*) FROM entries WHERE is_dir=0", [], |x| x.get(0)).unwrap();
        assert_eq!(n1, 1);

        // A NEW writer commit is visible to the SAME reader connection in its next read txn — NO reopen.
        w.execute("INSERT INTO entries(path,category,is_dir,path_key,parent_key,name,name_key,sort_key,indexed_at) \
                   VALUES('b.csv','data_table',0,'b.csv','','b.csv','b.csv','b.csv','t')", []).unwrap();
        let n2: i64 = r.query_row("SELECT COUNT(*) FROM entries WHERE is_dir=0", [], |x| x.get(0)).unwrap();
        assert_eq!(n2, 2, "WAL reader sees the new commit with no reopen");

        // -wal / -shm sidecars exist on the exFAT mount.
        assert!(dir.join("INDEX.sqlite-wal").exists());
        assert!(dir.join("INDEX.sqlite-shm").exists());

        drop(r);
        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn query_only_reader_rejects_writes() {
        let dir = scratch("qo");
        let db = dir.join("INDEX.sqlite");
        let dbs = db.to_str().unwrap();
        let _w = connect_writer(dbs).unwrap(); // create the schema
        let r = connect_reader(dbs).unwrap();
        let err = r.execute(
            "INSERT INTO entries(path,is_dir,path_key,parent_key,name,name_key,sort_key,indexed_at) \
             VALUES('x',0,'x','','x','x','x','t')",
            [],
        );
        assert!(err.is_err(), "query_only must reject a write");
        drop(_w);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn second_index_writer_is_refused_when_flock_works() {
        let dir = scratch("lock");
        let db = dir.join("INDEX.sqlite");
        let dbs = db.to_str().unwrap();

        let w1 = IndexWriter::open(dir.to_str().unwrap(), dbs).unwrap();

        // Determine whether flock is functional on THIS mount: a second flock LOCK_EX|LOCK_NB on the
        // same file from another fd must fail for the guard to be authoritative.
        let lockfile = dir.join(".lens-writer.lock");
        let probe = std::fs::OpenOptions::new().read(true).write(true).open(&lockfile).unwrap();
        let flock_works = unsafe { libc::flock(probe.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0;

        let w2 = IndexWriter::open(dir.to_str().unwrap(), dbs);
        if flock_works {
            assert!(w2.is_err(), "a second writer must be refused while the first holds the lock");
        } else {
            eprintln!("SKIP lock-refusal assertion: flock is a no-op on this mount (V9); \
                       PID-liveness backup can't distinguish same-process holders");
        }
        drop(w1);
        drop(w2);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// REGRESSION (macOS 27 update, 2026-09-10). The lock file held a bare pid written the previous
    /// day; the update rebooted the machine, the pid counter restarted, and that pid was reissued to
    /// a root system daemon. The old check read "pid exists" (and counted `EPERM` — a process we may
    /// not signal — as ALIVE), so Lens refused its own index: no writer ⇒ no live watcher, and the
    /// in-app Rebuild failed. A bare pid must never lock the user out.
    #[test]
    fn a_recycled_pid_owned_by_a_foreign_process_does_not_hold_the_lock() {
        let dir = scratch("stale_legacy");
        // pid 1 is launchd: always alive, always root, always `EPERM` to a user process.
        std::fs::write(dir.join(".lens-writer.lock"), b"1").unwrap();
        assert!(proc_info(1).is_none() || proc_info(1).unwrap().name != "lens");

        let lock = WriterLock::acquire(&dir);
        assert!(
            lock.is_ok(),
            "a legacy bare-pid lock naming a live NON-Lens process is stale, not held: {:?}",
            lock.err()
        );
        drop(lock);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// Same failure one level finer: the pid IS recorded with a start time, but the live process with
    /// that pid started at a different instant — i.e. the pid was recycled. Stale, not held.
    #[test]
    fn a_recorded_pid_whose_start_time_differs_is_a_recycled_pid() {
        let dir = scratch("stale_reuse");
        std::fs::write(dir.join(".lens-writer.lock"), b"1 1 1").unwrap(); // launchd did not start at epoch+1s
        let lock = WriterLock::acquire(&dir);
        assert!(lock.is_ok(), "start-time mismatch ⇒ pid reuse ⇒ stale: {:?}", lock.err());
        drop(lock);
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The protection the fix must NOT weaken: an owner that is genuinely still running — same pid AND
    /// same start time — is refused even when `flock` is a no-op on the mount.
    #[test]
    fn an_owner_still_running_under_its_recorded_identity_is_refused() {
        let dir = scratch("live_owner");
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let info = proc_info(child.id()).expect("can inspect our own child");
        std::fs::write(
            dir.join(".lens-writer.lock"),
            format!("{} {} {}", child.id(), info.start_sec, info.start_usec),
        )
        .unwrap();

        let lock = WriterLock::acquire(&dir);
        let refused = lock.is_err();
        drop(lock);
        let _ = child.kill();
        let _ = child.wait();
        let _ = std::fs::remove_dir_all(&dir);
        assert!(refused, "a live owner matching its recorded identity must still be refused");
    }

    /// The lock we write must carry an identity, not a bare pid — otherwise the next boot repeats the
    /// bug. Three whitespace-separated fields, first is our pid.
    #[test]
    fn the_lock_we_write_records_pid_and_start_time() {
        let dir = scratch("record_fmt");
        let lock = WriterLock::acquire(&dir).unwrap();
        let text = std::fs::read_to_string(dir.join(".lens-writer.lock")).unwrap();
        let fields: Vec<&str> = text.split_whitespace().collect();
        assert_eq!(fields.len(), 3, "expected `pid start_sec start_usec`, got {text:?}");
        assert_eq!(fields[0].parse::<u32>().unwrap(), std::process::id());
        assert_eq!(
            read_record(&dir.join(".lens-writer.lock")).unwrap().pid(),
            std::process::id()
        );
        drop(lock);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn writer_checkpoint_truncate_ok() {
        let dir = scratch("ckpt");
        let db = dir.join("INDEX.sqlite");
        let w = IndexWriter::open(dir.to_str().unwrap(), db.to_str().unwrap()).unwrap();
        w.with_conn(|c| {
            c.execute("INSERT INTO entries(path,is_dir,path_key,parent_key,name,name_key,sort_key,indexed_at) \
                       VALUES('z',0,'z','','z','z','z','t')", []).map_err(|e| e.to_string())?;
            Ok(())
        })
        .unwrap();
        w.checkpoint_truncate().unwrap();
        drop(w);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
