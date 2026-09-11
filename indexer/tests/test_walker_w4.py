"""Stage-2 W4 walker tests: batched getattrlistbulk(2) directory listing.

W4 (Axis A) replaces the W1 per-entry stat storm — on exFAT every scandir
``DirEntry`` is ``DT_UNKNOWN`` so ``is_symlink``/``is_dir``/``stat`` each force an
``lstat`` (~36.5k tree-wide) — with ONE ``getattrlistbulk(2)`` syscall per
directory returning name + objtype + mtime + (file) datalength for all children.

These pin the W4 behaviour against regression:

  (a) PRIMITIVE GROUND TRUTH (Darwin): for a fixture dir holding regular files, a
      real subdir, a symlink-to-file, a broken symlink, and a symlink-to-dir, the
      bulk primitive's (name, kind, mtime-as-iso, size) matches os.lstat/os.scandir
      ground truth for EVERY child.
  (b) DIGEST EQUALITY: the full walk over the hazard fixture yields the IDENTICAL
      content_digest whether the bulk path or the os.scandir fallback drove it.
  (c) GRACEFUL DEGRADATION: forcing the fallback (bulk fn raises OSError /
      ValueError, or _BULK_SUPPORTED off) yields byte-identical entries — proving
      the fallback is never worse than W1, and a per-record anomaly degrades the
      whole directory rather than corrupting an entry.
  (d) NO INODE/FILEID: the bulk stat shim carries no inode field and exFAT's
      garbage FILEID never enters identity or the freshness gate.

Stdlib-only (ctypes is stdlib). Reuses the shared fixture_tree / loaded_config.
The bulk-primitive ground-truth checks skip when getattrlistbulk is unsupported
(non-Darwin / a fs without it); the fallback-equivalence checks run everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repo_index import _bulkstat, walker
from repo_index.config import load_config
from repo_index.manifest import compute_digest


_DARWIN_BULK = _bulkstat.SUPPORTED
_skip_no_bulk = pytest.mark.skipif(
    not _DARWIN_BULK, reason="getattrlistbulk unsupported (non-Darwin / fs lacks it)"
)


def _build_mixed_dir(root: Path) -> dict:
    """Build a dir holding one of every node kind the classifier must split.

    Returns ``{name: kind}`` ground truth for the DIRECT children of ``root``.
    """
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / "data.csv").write_text("gene,score\nFOXG1,0.9\nEMX1,0.4\n", encoding="utf-8")
    (root / "sub" / "inner.txt").write_text("inner\n", encoding="utf-8")
    # symlink -> file (resolves), symlink -> nonexistent (broken), symlink -> dir.
    os.symlink(os.path.join("sub", "inner.txt"), root / "good_link")
    os.symlink("does_not_exist_target", root / "broken_link")
    os.symlink("sub", root / "dir_link")
    return {
        "a.txt": "file",
        "data.csv": "file",
        "sub": "dir",
        "good_link": "symlink",
        "broken_link": "symlink",
        "dir_link": "symlink",
    }


# --------------------------------------------------------------------------- #
# (a) bulk primitive ground-truth: (name, kind, mtime-iso, size) per child
# --------------------------------------------------------------------------- #

@_skip_no_bulk
def test_bulk_primitive_matches_lstat_ground_truth(tmp_path):
    """listdir_bulk's (name, kind, mtime-iso, size) == os.lstat/scandir truth.

    For regular files the bulk DATALENGTH must equal the lstat size and the
    bulk MODTIME (to whole-second ISO, the indexed resolution) must equal the
    lstat mtime. Kinds (file/dir/symlink) must match the lstat S_IS* split for
    every child including the symlink-to-file, broken symlink, and dir symlink.
    """
    root = tmp_path / "mixed"
    root.mkdir()
    truth_kind = _build_mixed_dir(root)

    got = {name: (kind, st) for (name, kind, st) in _bulkstat.listdir_bulk(str(root))}
    assert set(got) == set(truth_kind), "bulk children != actual children"

    for name, expect_kind in truth_kind.items():
        kind, st = got[name]
        abs_p = root / name
        lst = os.lstat(abs_p)
        assert kind == expect_kind, "%s: kind %r != %r" % (name, kind, expect_kind)
        # mtime to the indexed (whole-second) ISO resolution matches lstat.
        assert walker.iso_mtime(st.st_mtime) == walker.iso_mtime(lst.st_mtime), (
            "%s: bulk mtime %r != lstat mtime %r"
            % (name, st.st_mtime, lst.st_mtime)
        )
        if expect_kind == "file":
            # Regular-file DATALENGTH is authoritative and equals lstat size.
            assert st.st_size == lst.st_size, (
                "%s: bulk size %d != lstat size %d"
                % (name, st.st_size, lst.st_size)
            )


@_skip_no_bulk
def test_bulk_primitive_kind_split_is_exhaustive(tmp_path):
    """Every child lands in exactly one of {file, dir, symlink}; none 'other'."""
    root = tmp_path / "mixed"
    root.mkdir()
    _build_mixed_dir(root)
    kinds = {kind for (_n, kind, _st) in _bulkstat.listdir_bulk(str(root))}
    assert kinds <= {"file", "dir", "symlink"}, "unexpected kind: %r" % (kinds,)
    assert "other" not in kinds


@_skip_no_bulk
def test_bulk_primitive_decodes_unicode_names(tmp_path):
    """A non-ASCII filename round-trips through the attrreference NAME decode."""
    root = tmp_path / "uni"
    root.mkdir()
    (root / "café_данные.csv").write_text("x\n", encoding="utf-8")
    names = {n for (n, _k, _s) in _bulkstat.listdir_bulk(str(root))}
    assert "café_данные.csv" in names


# --------------------------------------------------------------------------- #
# (b) full-walk digest equality: bulk path vs os.scandir fallback
# --------------------------------------------------------------------------- #

def _force_scandir(monkeypatch):
    """Force walk() onto the os.scandir fallback for the WHOLE walk."""
    monkeypatch.setattr(walker, "_BULK_SUPPORTED", False)


@_skip_no_bulk
def test_full_walk_digest_bulk_equals_fallback(fixture_tree, loaded_config, monkeypatch):
    """The hazard-tree walk yields the SAME content_digest under the bulk path
    and under the forced os.scandir fallback — the W4 listing primitive preserves
    walk semantics exactly (CONTRACTS.md §7/§12)."""
    root, _ = fixture_tree

    # Bulk path active (Darwin default).
    assert walker._BULK_SUPPORTED is True
    bulk_entries = list(walker.walk(root, loaded_config))
    bulk_digest = compute_digest(bulk_entries)

    # Force the fallback for the second walk.
    _force_scandir(monkeypatch)
    fb_entries = list(walker.walk(root, loaded_config))
    fb_digest = compute_digest(fb_entries)

    assert bulk_digest == fb_digest, "bulk vs scandir-fallback digest diverged"

    # The hazard tree is actually present (not an empty/short-circuited walk).
    paths = {e["path"] for e in bulk_entries}
    assert "tables/cells.csv.gz" in paths            # compound-ext regular file
    assert "links/broken_link.csv" in paths          # dangling symlink recorded
    assert "links/good_link.csv" in paths            # resolving symlink recorded
    assert not any(p.startswith("links/link_dir/") for p in paths)  # dir-link not descended
    assert not any("_vendor" in Path(p).parts for p in paths)       # pruned dir absent


@_skip_no_bulk
def test_full_walk_entries_bulk_equal_fallback(fixture_tree, loaded_config, monkeypatch):
    """Not just the digest — the FULL per-entry dicts (every schema field incl.
    size_bytes, mtime_iso, is_symlink, symlink_target, symlink_ok, tags, error)
    are identical between the bulk path and the os.scandir fallback. This is the
    strongest no-drift guarantee: the symlink-fallback in the bulk path produces
    byte-identical symlink records to W1."""
    root, _ = fixture_tree

    bulk = sorted(walker.walk(root, loaded_config), key=lambda e: e["path"])
    _force_scandir(monkeypatch)
    fb = sorted(walker.walk(root, loaded_config), key=lambda e: e["path"])

    assert [e["path"] for e in bulk] == [e["path"] for e in fb]
    for b, f in zip(bulk, fb):
        assert b == f, "entry diverged for %s:\n bulk=%r\n  fb=%r" % (b["path"], b, f)


# --------------------------------------------------------------------------- #
# (c) graceful degradation: a raising / unsupported bulk fn falls back cleanly
# --------------------------------------------------------------------------- #

@_skip_no_bulk
def test_bulk_oserror_falls_back_to_scandir(fixture_tree, loaded_config, monkeypatch):
    """If listdir_bulk raises OSError (e.g. ENOTSUP on a non-supporting fs), the
    walk degrades to os.scandir for that directory and yields identical entries —
    behaviour is never worse than W1."""
    root, _ = fixture_tree
    reference = sorted(walker.walk(root, loaded_config), key=lambda e: e["path"])

    import errno as _errno

    def _raise_enotsup(path):
        raise OSError(_errno.ENOTSUP, "Operation not supported", path)

    monkeypatch.setattr(_bulkstat, "listdir_bulk", _raise_enotsup)
    degraded = sorted(walker.walk(root, loaded_config), key=lambda e: e["path"])
    assert degraded == reference, "OSError fallback diverged from W1 entries"


@_skip_no_bulk
def test_bulk_valueerror_falls_back_to_scandir(fixture_tree, loaded_config, monkeypatch):
    """A per-record decode anomaly (ValueError) degrades the WHOLE directory to
    os.scandir rather than emitting a corrupt entry — identical entries result."""
    root, _ = fixture_tree
    reference = sorted(walker.walk(root, loaded_config), key=lambda e: e["path"])

    def _raise_decode(path):
        raise ValueError("simulated packed-record decode anomaly")

    monkeypatch.setattr(_bulkstat, "listdir_bulk", _raise_decode)
    degraded = sorted(walker.walk(root, loaded_config), key=lambda e: e["path"])
    assert degraded == reference, "ValueError fallback diverged from W1 entries"


@_skip_no_bulk
def test_bulk_unreadable_dir_still_yields_error_entry(tmp_path, loaded_config, monkeypatch):
    """A genuinely unreadable directory is surfaced as a synthetic error entry
    (§0.10/§9), NOT silently dropped, even when the bulk primitive fails to open
    it — the bulk path distinguishes 'fs unsupported' from 'cannot open'."""
    root = tmp_path / "tree"
    (root / "locked").mkdir(parents=True)
    (root / "ok.txt").write_text("x\n", encoding="utf-8")
    locked = root / "locked"
    (locked / "hidden.txt").write_text("y\n", encoding="utf-8")

    # Make the subdir unreadable+unsearchable so BOTH bulk-open and scandir fail.
    os.chmod(locked, 0o000)
    try:
        entries = list(walker.walk(root, loaded_config))
    finally:
        os.chmod(locked, 0o755)  # restore so tmp cleanup can recurse

    err = [e for e in entries if e.get("error") and e["path"].endswith("locked")]
    assert err, "unreadable dir did not surface as an error entry"
    # The readable sibling is still indexed.
    assert any(e["path"] == "ok.txt" for e in entries)


def test_walk_works_when_bulk_unsupported(fixture_tree, loaded_config, monkeypatch):
    """With _BULK_SUPPORTED forced False (the non-Darwin path), the walk runs on
    os.scandir and still produces a well-formed, non-empty entry set — proving the
    import-guard degradation path is wired (runs on EVERY platform)."""
    _force_scandir(monkeypatch)
    root, _ = fixture_tree
    entries = list(walker.walk(root, loaded_config))
    assert entries, "scandir-fallback walk produced no entries"
    assert any(e["path"] == "tables/cells.csv" for e in entries)


# --------------------------------------------------------------------------- #
# (d) inode / FILEID is never used (exFAT returns garbage; key only on mtime+size)
# --------------------------------------------------------------------------- #

@_skip_no_bulk
def test_stat_shim_has_no_inode_field(tmp_path):
    """The bulk _StatLike shim exposes ONLY st_mtime + st_size — no st_ino / inode.
    exFAT's FILEID is garbage (a 2^64-ish value); it must never enter identity or
    freshness, so the shim deliberately omits it."""
    root = tmp_path / "d"
    root.mkdir()
    (root / "f.txt").write_text("z\n", encoding="utf-8")
    (_n, _k, st), = list(_bulkstat.listdir_bulk(str(root)))

    assert hasattr(st, "st_mtime") and hasattr(st, "st_size")
    assert not hasattr(st, "st_ino"), "stat shim leaked an inode field"
    # __slots__ is exactly the two freshness fields, nothing else.
    assert set(getattr(type(st), "__slots__", ())) == {"st_mtime", "st_size"}


def test_module_never_requests_fileid_attr():
    """Defensive guard: the bulk module never DEFINES a FILEID constant nor reads
    a st_ino, and the requested commonattr mask omits the FILEID bit (0x00200000)
    — so a future edit cannot silently start keying on the garbage exFAT inode.
    (The module docstring mentions ATTR_CMN_FILEID only to document that it is
    deliberately NOT requested, so we check for a constant DEFINITION, not prose.)"""
    src = Path(_bulkstat.__file__).read_text(encoding="utf-8")
    # No FILEID constant is DEFINED, and st_ino is never READ (an attribute access
    # `.st_ino`) anywhere. The docstring may MENTION the names to document the
    # deliberate omission, so we forbid the constant-definition and attribute-read
    # forms specifically rather than any textual occurrence.
    assert "ATTR_CMN_FILEID =" not in src, "module defines an ATTR_CMN_FILEID constant"
    assert "_ATTR_CMN_FILEID" not in src, "module defines a _ATTR_CMN_FILEID constant"
    assert ".st_ino" not in src, "module reads a .st_ino attribute"
    assert "st_ino" not in getattr(_bulkstat._StatLike, "__slots__", ()), (
        "stat shim __slots__ leaked st_ino"
    )
    # The requested commonattr mask must NOT include the FILEID bit (0x00200000).
    ATTR_CMN_FILEID = 0x00200000
    requested = (
        _bulkstat._ATTR_CMN_RETURNED_ATTRS
        | _bulkstat._ATTR_CMN_NAME
        | _bulkstat._ATTR_CMN_OBJTYPE
        | _bulkstat._ATTR_CMN_MODTIME
    )
    assert requested & ATTR_CMN_FILEID == 0, "FILEID bit set in requested attrs"
    # And the fileattr mask is exactly DATALENGTH — no inode/fileid bits there either.
    assert _bulkstat._ATTR_FILE_DATALENGTH == 0x00000200


@_skip_no_bulk
def test_freshness_gate_keys_on_mtime_size_only(tmp_path):
    """An entry built from a bulk stat reuses the cache iff size+mtime match —
    never on inode. We seed a cache keyed on the file's (size, mtime_iso); the
    bulk-built entry must reuse it (proving the gate reads only mtime+size, which
    is all the shim carries)."""
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "cells.csv"
    target.write_text("gene,score\nFOXG1,0.9\n", encoding="utf-8")
    cfg = load_config()

    (_n, _k, st), = list(_bulkstat.listdir_bulk(str(root)))
    rel = "cells.csv"
    sentinel = {"__sentinel__": "reused_from_cache_via_mtime_size"}
    cache = {
        rel: {
            "path": rel,
            "size_bytes": int(st.st_size),
            "mtime_iso": walker.iso_mtime(st.st_mtime),
            "extractor": "sentinel_extractor",
            "meta": dict(sentinel),
        }
    }
    # Pass the shim straight through make_entry's st= param (it reads only
    # st.st_mtime/st.st_size) — the cache hit proves the gate is mtime+size only.
    entry = walker.make_entry(
        target, root, cfg, cache=cache, st=st, is_symlink_hint=False
    )
    assert entry["meta"] == sentinel, "bulk-stat entry did not reuse the mtime+size cache"
    assert entry["extractor"] == "sentinel_extractor"
