"""Tests for repo_index.export_sqlite — the DISASTER-RECOVERY-ONLY INDEX.sqlite
builder (PHASE_0_1_SPEC.md §3.6).

Builds a tiny in-memory manifest and asserts: every entry becomes a row; the full
(untruncated) meta round-trips through the `meta` JSON column; n_obs/n_vars are
denormalized for index scans; the contentless FTS5 table matches on a flattened
meta term (a column name); and the write is atomic (no leftover .tmp). Skips
cleanly if the local sqlite3 was compiled without FTS5 (the macOS system + conda
builds both ship it, per CONTRACTS §0.1).

Also covers the §3.6 flock-guarded writer-lock protocol added when Python's
`write_sqlite` was demoted to a DR-only path: it REFUSES while a live Lens
writer holds `<out_dir>/.lens-writer.lock` (both via a real held flock and via
the PID+liveness check alone, since exFAT flock may be a no-op, §V9) and
succeeds (flat v1-shape build, atomic replace, stale -wal/-shm sidecars
unlinked) when no live writer holds it. Per PHASE_0_1_SPEC.md §0.5D, the v2
schema/tree/keys are Rust-owned — this module keeps building the CURRENT FLAT
(v1) schema, so these tests assert v1 shape only; v2 coverage lives in the Rust
`tree.rs`/`db.rs` test suites.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from repo_index import export_sqlite


def _fts5_available() -> bool:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


pytestmark = pytest.mark.skipif(not _fts5_available(), reason="sqlite3 built without FTS5")


def _manifest():
    wide_cols = [f"gene_{i}" for i in range(500)]
    return {
        "schema_version": "1.0",
        "entries": [
            {
                "path": "data/atlas.h5ad", "category": "data_matrix", "ext": "h5ad",
                "size_bytes": 1234, "mtime_iso": "2026-01-01T00:00:00Z",
                "is_symlink": False, "symlink_target": None, "symlink_ok": None,
                "extractor": "h5ad", "tags": ["atlas"],
                "meta": {"n_obs": 15000, "n_vars": 300, "obs_columns": ["leiden", "cell_type"],
                         "obsm": {"X_umap": [15000, 2]}},
            },
            {
                "path": "tables/wide.csv", "category": "data_table", "ext": "csv",
                "size_bytes": 99, "mtime_iso": "2026-01-02T00:00:00Z",
                "is_symlink": False, "symlink_target": None, "symlink_ok": None,
                "extractor": "tabular", "tags": [],
                "meta": {"columns": wide_cols, "n_columns": 500},
            },
            {
                "path": "links/broken.csv", "category": "data_table", "ext": "csv",
                "size_bytes": 0, "mtime_iso": "2026-01-03T00:00:00Z",
                "is_symlink": True, "symlink_target": "../nope.csv", "symlink_ok": False,
                "extractor": "generic", "tags": [], "meta": {},
                "error": "FileNotFoundError: missing",
            },
        ],
    }, wide_cols


def test_write_sqlite_rows_meta_and_fts(tmp_path: Path):
    manifest, wide_cols = _manifest()
    out_dir = tmp_path / "_repo_index"
    db = export_sqlite.write_sqlite(manifest, out_dir)

    assert db == out_dir / "INDEX.sqlite"
    assert db.exists()
    assert not (out_dir / "INDEX.sqlite.tmp").exists()  # atomic: tmp renamed away

    con = sqlite3.connect(str(db))
    try:
        con.row_factory = sqlite3.Row
        # all entries present
        assert con.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3

        # denormalized n_obs/n_vars + FULL meta round-trips (untruncated 500 cols)
        row = con.execute(
            "SELECT n_obs, n_vars, meta, category FROM entries WHERE path=?",
            ("data/atlas.h5ad",),
        ).fetchone()
        assert row["n_obs"] == 15000 and row["n_vars"] == 300
        assert json.loads(row["meta"])["obs_columns"] == ["leiden", "cell_type"]

        wide = con.execute(
            "SELECT meta FROM entries WHERE path=?", ("tables/wide.csv",)
        ).fetchone()
        assert json.loads(wide["meta"])["columns"] == wide_cols  # all 500, not a head

        # symlink_ok tri-state: False -> 0, None -> NULL
        assert con.execute("SELECT symlink_ok FROM entries WHERE path=?",
                           ("links/broken.csv",)).fetchone()[0] == 0
        assert con.execute("SELECT symlink_ok FROM entries WHERE path=?",
                           ("data/atlas.h5ad",)).fetchone()[0] is None

        # n_obs index ordering (descending) for the "key matrices" strip
        top = con.execute(
            "SELECT path FROM entries WHERE n_obs IS NOT NULL ORDER BY n_obs DESC LIMIT 1"
        ).fetchone()
        assert top["path"] == "data/atlas.h5ad"

        # FTS5 finds a flattened-meta term (a column name) and an obs column
        hit = con.execute("SELECT rowid FROM fts WHERE fts MATCH 'gene_42'").fetchall()
        assert len(hit) == 1
        leiden = con.execute(
            "SELECT path FROM entries WHERE id IN "
            "(SELECT rowid FROM fts WHERE fts MATCH 'leiden')"
        ).fetchall()
        assert ("data/atlas.h5ad",) in [tuple(r) for r in leiden]
    finally:
        con.close()


def test_write_sqlite_is_idempotent(tmp_path: Path):
    manifest, _ = _manifest()
    out_dir = tmp_path / "_repo_index"
    export_sqlite.write_sqlite(manifest, out_dir)
    db = export_sqlite.write_sqlite(manifest, out_dir)  # second write overwrites cleanly
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# §3.6 flock-guarded writer-lock protocol
# --------------------------------------------------------------------------- #

def test_write_sqlite_refuses_when_flock_is_held(tmp_path: Path):
    """A genuinely held flock on `.lens-writer.lock` refuses the DR build —
    the real-filesystem case (a live Rust writer's own fd holds it)."""
    manifest, _ = _manifest()
    out_dir = tmp_path / "_repo_index"
    out_dir.mkdir(parents=True)
    lock_path = out_dir / export_sqlite._LOCK_NAME
    holder = open(lock_path, "w")
    try:
        holder.write(str(os.getpid()))
        holder.flush()
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="live Lens writer"):
            export_sqlite.write_sqlite(manifest, out_dir)
        # refused BEFORE touching the target — no half-written db left behind.
        assert not (out_dir / "INDEX.sqlite").exists()
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_write_sqlite_refuses_via_pid_liveness_even_when_flock_is_free(tmp_path: Path):
    """A recorded PID that is still alive refuses the DR build even when the
    flock itself is completely free — proves PID+liveness is the LOAD-BEARING
    check, not flock alone (§V9: exFAT flock may be a silent no-op)."""
    manifest, _ = _manifest()
    out_dir = tmp_path / "_repo_index"
    out_dir.mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "30"])
    try:
        (out_dir / export_sqlite._LOCK_NAME).write_text(str(proc.pid))
        with pytest.raises(RuntimeError, match="live Lens writer"):
            export_sqlite.write_sqlite(manifest, out_dir)
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_write_sqlite_succeeds_when_no_live_writer_holds_lock(tmp_path: Path):
    manifest, _ = _manifest()
    out_dir = tmp_path / "_repo_index"
    db = export_sqlite.write_sqlite(manifest, out_dir)
    assert db == out_dir / "INDEX.sqlite"
    assert db.exists()
    assert not (out_dir / "INDEX.sqlite.tmp").exists()
    con = sqlite3.connect(str(db))
    try:
        # v1 FLAT shape only (Rust owns v2 — §0.5D): no is_dir/path_key/etc.
        cols = {row[1] for row in con.execute("PRAGMA table_info(entries)")}
        assert cols == {
            "id", "path", "category", "ext", "size_bytes", "mtime_iso",
            "is_symlink", "symlink_target", "symlink_ok", "extractor",
            "tags", "error", "n_obs", "n_vars", "meta",
            # SVG figure text, deliberately its OWN column and NOT part of `meta`,
            # so db.rs's HAYSTACK (which scans meta) cannot see it and the default
            # search stays byte-identical. See test_figure_text.py §10.
            "figure_text",
        }
        assert con.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 3
    finally:
        con.close()


def test_write_sqlite_unlinks_stale_wal_shm_sidecars_before_replace(tmp_path: Path):
    manifest, _ = _manifest()
    out_dir = tmp_path / "_repo_index"
    out_dir.mkdir(parents=True)
    final_name = "INDEX.sqlite"
    stale = []
    for suffix in ("-wal", "-shm"):
        for prefix in ("", "._"):
            p = out_dir / f"{prefix}{final_name}{suffix}"
            p.write_bytes(b"stale sidecar bytes")
            stale.append(p)

    export_sqlite.write_sqlite(manifest, out_dir)

    for p in stale:
        assert not p.exists(), f"stale sidecar not unlinked: {p}"
