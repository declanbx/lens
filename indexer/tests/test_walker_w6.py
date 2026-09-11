"""Stage-3 W6 walker tests: parallel LISTING / serial EXTRACT (Axis A).

W6 fans the dominant per-directory LISTING + child classification I/O across a
small ``ThreadPoolExecutor`` (``config.walk_threads`` workers) while keeping
``make_entry`` / ``extract_meta`` strictly SERIAL on the main thread — extraction
(h5py/pyarrow) is not thread-safe, so it must never run off-thread. These tests
pin that contract:

  (a) DIGEST EQUALITY: a parallel walk (walk_threads=4) yields the IDENTICAL
      content_digest as the serial walk (walk_threads=1) over the hazard fixture
      tree (broken / dir symlinks, pruned dir, ._ file, compound-ext file).
  (b) EXTRACTION-IS-MAIN-THREAD-ONLY: with the extractor entry point
      (``walker.extract_meta``) monkeypatched to record
      ``threading.current_thread()`` and an EMPTY cache (forces extraction of
      EVERY file), a parallel walk records the MainThread for every extracted
      file — extraction never escapes the main thread.
  (c) SERIAL PATH for walk_threads <= 1: no ThreadPoolExecutor is ever
      constructed (no worker threads spawned) and the entries are identical to
      the parallel walk.
  (d) ENTRY-SET EQUALITY: not just the digest — the full per-entry dicts (paths +
      size/extractor/meta/symlink/tags/error fields) are identical parallel vs
      serial.

Stdlib-only (threading / concurrent.futures are stdlib). Reuses the shared
fixture_tree / loaded_config from conftest. Runs on every platform: the parallel
path is exercised regardless of whether the W4 bulk primitive is available
(it degrades to the scandir listing inside each worker).
"""

from __future__ import annotations

import threading
from pathlib import Path

from repo_index import walker
from repo_index.config import load_config
from repo_index.manifest import compute_digest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _cfg(threads: int):
    """A resolved Config with walk_threads overridden (everything else default)."""
    return load_config(overrides={"walk_threads": threads})


def _sorted_walk(root: Path, cfg, **kw):
    """Run walk() to a path-sorted list (emission order may differ parallel vs
    serial; the entry SET / digest do not — §7/§12 — so sort to compare)."""
    return sorted(walker.walk(root, cfg, **kw), key=lambda e: e["path"])


def _extractable_files_present(root: Path) -> int:
    """Count the non-symlink, non-pruned regular files the walk will extract.

    Mirrors the fixture-tree shape: every file under data/ tables/ conf/ code/
    docs/ is extractable; symlinks under links/ are recorded generic (never
    extracted); ._shadow is skipped; _vendor is pruned. We just assert "several"
    in the test, so a positive count here is the guard that the fixture is real.
    """
    n = 0
    for p in root.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        parts = p.relative_to(root).parts
        if parts and parts[0] == "_vendor":
            continue
        if p.name.startswith("._"):
            continue
        n += 1
    return n


# --------------------------------------------------------------------------- #
# (a) parallel digest == serial digest over the hazard fixture
# --------------------------------------------------------------------------- #

def test_parallel_digest_equals_serial(fixture_tree):
    """walk_threads=4 yields the SAME content_digest as walk_threads=1 over the
    hazard tree (incl. broken/dir symlinks, pruned _vendor, ._shadow). The W6
    parallel-list path preserves walk semantics exactly (§7/§12)."""
    root, _ = fixture_tree

    serial = list(walker.walk(root, _cfg(1)))
    parallel = list(walker.walk(root, _cfg(4)))

    assert compute_digest(serial) == compute_digest(parallel), (
        "parallel vs serial content_digest diverged"
    )

    # The hazard tree is actually present (not an empty/short-circuited walk).
    paths = {e["path"] for e in parallel}
    assert "tables/cells.csv.gz" in paths              # compound-ext regular file
    assert "links/broken_link.csv" in paths            # dangling symlink recorded
    assert "links/good_link.csv" in paths              # resolving symlink recorded
    assert "links/link_dir" in paths                   # symlink-to-dir: single entry
    # Invariants under concurrency: dir-link not descended, pruned dir absent,
    # AppleDouble skipped.
    assert not any(p.startswith("links/link_dir/") for p in paths)
    assert not any("_vendor" in Path(p).parts for p in paths)
    assert not any(Path(p).name.startswith("._") for p in paths)


def test_parallel_digest_equals_serial_more_threads(fixture_tree):
    """Digest is invariant to the pool size (8 threads == 1 thread digest), so a
    larger pool never changes the entry SET — only (potentially) the order."""
    root, _ = fixture_tree
    serial = list(walker.walk(root, _cfg(1)))
    parallel8 = list(walker.walk(root, _cfg(8)))
    assert compute_digest(serial) == compute_digest(parallel8)


# --------------------------------------------------------------------------- #
# (b) EXTRACTION runs ONLY on the main thread (the core W6 safety contract)
# --------------------------------------------------------------------------- #

def test_extraction_is_main_thread_only(fixture_tree, monkeypatch):
    """Monkeypatch the extractor entry point as imported by the walker
    (``walker.extract_meta``) to record the calling thread, run a PARALLEL walk
    (walk_threads=4) with an EMPTY cache so EVERY file is extracted, and assert
    every recorded thread is the MainThread. This is the non-negotiable W6
    guarantee: listing fans out, extraction never leaves the main thread."""
    root, _ = fixture_tree

    main = threading.main_thread()
    recorded = []
    rec_lock = threading.Lock()
    real_extract = walker.extract_meta

    def _spy_extract(path):
        with rec_lock:
            recorded.append(threading.current_thread())
        return real_extract(path)

    monkeypatch.setattr(walker, "extract_meta", _spy_extract)

    # Empty cache (NOT None) -> make_entry has a cache to consult, misses on every
    # path, and falls through to extract_meta for EVERY non-symlink file.
    entries = list(walker.walk(root, _cfg(4), cache={}))

    assert recorded, "extract_meta was never called (no files extracted?)"
    # Several extractable files exist in the fixture (sanity: the fixture is real).
    assert _extractable_files_present(root) >= 3
    # EVERY extraction happened on the main thread.
    off_main = [t for t in recorded if t is not main]
    assert not off_main, (
        "extract_meta ran off the main thread on %d call(s): %r"
        % (len(off_main), {t.name for t in off_main})
    )
    # And the walk still produced a real entry set.
    assert any(e["path"] == "tables/cells.csv" for e in entries)


def test_extraction_main_thread_only_under_scandir_listing(fixture_tree, monkeypatch):
    """Same main-thread-only guarantee when the parallel workers use the SCANDIR
    listing (W4 bulk forced off) — proving the serial-extract guarantee is a
    property of the driver, not of the listing primitive."""
    root, _ = fixture_tree
    monkeypatch.setattr(walker, "_BULK_SUPPORTED", False)

    main = threading.main_thread()
    recorded = []
    rec_lock = threading.Lock()
    real_extract = walker.extract_meta

    def _spy_extract(path):
        with rec_lock:
            recorded.append(threading.current_thread())
        return real_extract(path)

    monkeypatch.setattr(walker, "extract_meta", _spy_extract)
    list(walker.walk(root, _cfg(4), cache={}))

    assert recorded, "extract_meta was never called"
    assert all(t is main for t in recorded), "extraction escaped the main thread"


# --------------------------------------------------------------------------- #
# (c) walk_threads <= 1 uses the SERIAL path (no pool / no worker threads)
# --------------------------------------------------------------------------- #

class _PoolSpy:
    """Records every ThreadPoolExecutor construction so a test can assert the
    serial path never instantiates one. Delegates to the real executor so a
    parallel walk still works under the spy."""

    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1
        from concurrent.futures import ThreadPoolExecutor as _Real
        self._real = _Real(*args, **kwargs)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


def test_serial_path_spawns_no_pool(fixture_tree, monkeypatch):
    """walk_threads=1 must take the EXISTING serial recursion — no
    ThreadPoolExecutor is constructed (hence no worker threads spawned)."""
    root, _ = fixture_tree
    _PoolSpy.instances = 0
    monkeypatch.setattr(walker, "ThreadPoolExecutor", _PoolSpy)

    entries = list(walker.walk(root, _cfg(1)))
    assert _PoolSpy.instances == 0, (
        "walk_threads=1 constructed a ThreadPoolExecutor (took the parallel path)"
    )
    assert entries, "serial walk produced no entries"


def test_parallel_path_does_spawn_pool(fixture_tree, monkeypatch):
    """Counterpart to the above: walk_threads=4 DOES construct exactly one pool —
    so the serial-vs-parallel branch is genuinely keyed on walk_threads."""
    root, _ = fixture_tree
    _PoolSpy.instances = 0
    monkeypatch.setattr(walker, "ThreadPoolExecutor", _PoolSpy)

    list(walker.walk(root, _cfg(4)))
    assert _PoolSpy.instances == 1, (
        "walk_threads=4 should construct exactly one ThreadPoolExecutor, got %d"
        % _PoolSpy.instances
    )


def test_serial_and_parallel_entries_identical_thread_counts(fixture_tree):
    """Across walk_threads in {1, 2, 4, 8} the path-sorted entry list is byte
    -identical: the thread count is a pure throughput knob, never a behaviour one."""
    root, _ = fixture_tree
    baseline = _sorted_walk(root, _cfg(1))
    for t in (2, 4, 8):
        got = _sorted_walk(root, _cfg(t))
        assert [e["path"] for e in got] == [e["path"] for e in baseline], (
            "path set diverged at walk_threads=%d" % t
        )
        for a, b in zip(got, baseline):
            assert a == b, (
                "entry diverged at walk_threads=%d for %s:\n par=%r\n ser=%r"
                % (t, b["path"], a, b)
            )


# --------------------------------------------------------------------------- #
# (d) FULL per-entry equality (every schema field) parallel vs serial
# --------------------------------------------------------------------------- #

def test_full_entry_set_parallel_equals_serial(fixture_tree):
    """The strongest no-drift guarantee: every per-entry dict (path, size_bytes,
    ext, category, mtime_iso, is_symlink, symlink_target, symlink_ok, extractor,
    meta, tags, error) is identical between the parallel and serial walks — only
    emission ORDER may differ (we path-sort to compare)."""
    root, _ = fixture_tree

    serial = _sorted_walk(root, _cfg(1))
    parallel = _sorted_walk(root, _cfg(4))

    assert [e["path"] for e in parallel] == [e["path"] for e in serial], (
        "parallel vs serial path SET diverged"
    )
    for par, ser in zip(parallel, serial):
        assert par == ser, (
            "entry diverged for %s:\n parallel=%r\n   serial=%r"
            % (ser["path"], par, ser)
        )


def test_parallel_equals_serial_with_excludes(fixture_tree):
    """Exclude/include globs are honoured identically under the parallel walk —
    an excluded dir subtree and an excluded file vanish parallel-vs-serial alike
    (exclude-before-descend must hold across the pool)."""
    root, _ = fixture_tree
    overrides = {"exclude_globs": ["data/**", "*.yaml"]}

    serial = _sorted_walk(root, load_config(overrides={**overrides, "walk_threads": 1}))
    parallel = _sorted_walk(root, load_config(overrides={**overrides, "walk_threads": 4}))

    par_paths = {e["path"] for e in parallel}
    # Excluded subtree + excluded file are gone.
    assert not any(p.startswith("data/") for p in par_paths)
    assert "conf/params.yaml" not in par_paths
    # Kept files survive.
    assert "code/module.py" in par_paths
    # And parallel == serial exactly.
    assert [e["path"] for e in parallel] == [e["path"] for e in serial]
    for par, ser in zip(parallel, serial):
        assert par == ser


def test_parallel_unreadable_dir_surfaces_error_entry(tmp_path):
    """A genuinely unreadable directory is surfaced as a synthetic error entry
    (§0.10/§9) under the PARALLEL driver too — never silently dropped — while a
    readable sibling is still indexed."""
    root = tmp_path / "tree"
    (root / "locked").mkdir(parents=True)
    (root / "ok.txt").write_text("x\n", encoding="utf-8")
    (root / "locked" / "hidden.txt").write_text("y\n", encoding="utf-8")

    import os
    os.chmod(root / "locked", 0o000)
    try:
        entries = list(walker.walk(root, _cfg(4)))
    finally:
        os.chmod(root / "locked", 0o755)  # restore for tmp cleanup

    err = [e for e in entries if e.get("error") and e["path"].endswith("locked")]
    assert err, "unreadable dir did not surface as an error entry under parallel walk"
    assert any(e["path"] == "ok.txt" for e in entries), "readable sibling not indexed"
