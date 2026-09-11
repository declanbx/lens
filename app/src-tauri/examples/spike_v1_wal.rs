//! V1 SPIKE (PHASE_0_1_SPEC.md §3.4 / §10.1): does normal WAL work on THIS exFAT mount?
//! Opens a db READ_WRITE, PRAGMA journal_mode=WAL, writes+commits, confirms -wal/-shm sidecars
//! appear, and a SECOND query_only connection reads the commit without reopen and is refused a
//! write. Decides reader-pool+writer (V1 pass) vs single-connection EXCLUSIVE mode (V1 fail).
//! Run: cargo run --example spike_v1_wal <scratch_dir_on_exfat>
use rusqlite::{Connection, OpenFlags};
use std::path::Path;

fn main() {
    let dir = std::env::args()
        .nth(1)
        .expect("usage: spike_v1_wal <scratch_dir_on_exfat>");
    let dir = Path::new(&dir);
    std::fs::create_dir_all(dir).unwrap();
    let db = dir.join("spike_v1.sqlite");
    let wal = dir.join("spike_v1.sqlite-wal");
    let shm = dir.join("spike_v1.sqlite-shm");
    for p in [&db, &wal, &shm] {
        let _ = std::fs::remove_file(p);
    }

    println!("bundled SQLite: {}", rusqlite::version());

    // ── writer connection (the sole writer in the real design) ──────────────
    let w = Connection::open_with_flags(
        &db,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .unwrap();
    let mode: String = w
        .query_row("PRAGMA journal_mode=WAL;", [], |r| r.get(0))
        .unwrap();
    println!("journal_mode -> {mode}");
    assert_eq!(mode, "wal", "V1 FAIL: WAL not accepted on this volume");
    w.execute_batch("PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;")
        .unwrap();
    w.execute_batch("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT);")
        .unwrap();
    w.execute("INSERT INTO t(v) VALUES ('hello-exfat')", []).unwrap();

    // ── sidecars present? (-shm is mmap-backed; needs POSIX locking on the volume) ──
    println!("-wal exists: {}", wal.exists());
    println!("-shm exists: {}", shm.exists());
    assert!(wal.exists(), "V1 FAIL: -wal sidecar missing");
    assert!(shm.exists(), "V1 FAIL: -shm sidecar missing");

    // ── second reader connection (READ_WRITE + query_only) sees the commit, no reopen ──
    let r = Connection::open_with_flags(
        &db,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .unwrap();
    r.execute_batch("PRAGMA query_only=ON;").unwrap();
    let v: String = r
        .query_row("SELECT v FROM t WHERE id=1", [], |r| r.get(0))
        .unwrap();
    println!("reader sees committed row: {v}");
    assert_eq!(v, "hello-exfat");

    // ── query_only rejects writes (statement-level read-only guarantee) ──
    let werr = r.execute("INSERT INTO t(v) VALUES ('nope')", []);
    println!("query_only rejects write: {}", werr.is_err());
    assert!(werr.is_err(), "V1 FAIL: query_only did not reject a write");

    // ── writer-owned checkpoint folds the WAL back ──
    w.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);").unwrap();

    drop(r);
    drop(w);
    for p in [&db, &wal, &shm] {
        let _ = std::fs::remove_file(p);
    }
    println!("\nV1 SPIKE PASSED: WAL + -shm + concurrent query_only reader all work on this mount.");
    println!("=> reader-pool + writer architecture is viable (NOT single-connection EXCLUSIVE mode).");
}
