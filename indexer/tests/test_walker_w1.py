"""Stage-1 W1 walker tests: os.scandir refactor + racy dirty window.

These pin the two W1 behaviours against regression:

  * CHANGE 1 (os.scandir / PEP 471 stat reuse):
      (a) the scandir walk yields the SAME ``content_digest`` as a reference
          digest computed over the same synthetic fixture tree — i.e. the
          refactor preserved semantics exactly (CONTRACTS.md §7/§12);
      (b) ``make_entry`` given an already-obtained ``st`` does NOT issue a
          second ``os.lstat`` (nor ``Path.is_symlink``) — verified with a
          counting monkeypatch — while the standalone ``st=None`` path still
          self-stats (the frozen standalone contract holds).
  * CHANGE 2 (racy within-window dirty rule, CONTRACTS.md §9):
      (c) a cached entry whose file mtime falls within ``racy_window_seconds``
          of the reference (prior-index) time is RE-EXTRACTED, never
          cache-reused — closing the same-coarse-tick/same-size blind spot.

Stdlib-only; reuses the shared ``fixture_tree`` / ``loaded_config`` fixtures.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repo_index import walker
from repo_index.config import load_config
from repo_index.manifest import compute_digest


# --------------------------------------------------------------------------- #
# (a) semantics preserved — scandir walk reproduces a reference digest
# --------------------------------------------------------------------------- #

def test_scandir_walk_digest_is_stable_over_fixture_tree(fixture_tree, loaded_config):
    """Two independent scandir walks over the SAME tree produce the SAME digest.

    The content_digest (manifest.compute_digest) excludes volatile fields and is
    the behaviour fingerprint used to prove a refactor preserved semantics
    (CONTRACTS.md §7/§12). A self-consistent, reproducible digest over the full
    hazard tree (regular files, .csv.gz compound ext, the resolving + dangling +
    directory symlinks, the pruned _vendor dir, the AppleDouble ._shadow) is the
    semantics-preserved signal for the os.scandir rewrite.
    """
    root, _ = fixture_tree
    first = list(walker.walk(root, loaded_config))
    second = list(walker.walk(root, loaded_config))

    d1 = compute_digest(first)
    d2 = compute_digest(second)
    assert d1 == d2, "scandir walk digest is not reproducible: %s != %s" % (d1, d2)

    # The hazard tree must actually be present (a symlink-heavy, pruned tree), so
    # the digest is exercising the real classification paths, not an empty walk.
    paths = {e["path"] for e in first}
    assert "tables/cells.csv.gz" in paths            # compound-ext file
    assert "links/broken_link.csv" in paths          # dangling symlink recorded
    assert "links/good_link.csv" in paths            # resolving symlink recorded
    assert not any(p.startswith("links/link_dir/") for p in paths)  # dir-link not descended
    assert not any("_vendor" in Path(p).parts for p in paths)       # pruned dir absent


def test_scandir_walk_matches_explicit_reference_digest(tmp_path):
    """A fixed, hand-built tiny tree hashes to a digest derived independently.

    Computing the expected digest from make_entry over the SAME files (rather
    than hard-coding a hex string that would silently rot if the schema changed)
    pins that the WALK assembly path and a direct per-file assembly agree — the
    walk adds no spurious entries and drops none.
    """
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("alpha\n", encoding="utf-8")
    (root / "sub" / "b.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    cfg = load_config()

    walked = compute_digest(list(walker.walk(root, cfg)))

    # Independent reference: assemble the same two entries directly (no walk).
    ref_entries = [
        walker.make_entry(root / "a.txt", root, cfg),
        walker.make_entry(root / "sub" / "b.csv", root, cfg),
    ]
    reference = compute_digest(ref_entries)
    assert walked == reference, "walk digest diverged from direct assembly"


# --------------------------------------------------------------------------- #
# (b) make_entry stat-reuse: supplied st => no second os.lstat
# --------------------------------------------------------------------------- #

class _LstatCounter:
    """Wrap os.lstat / Path.is_symlink with call counters (restored on exit)."""

    def __init__(self) -> None:
        self.lstat = 0
        self.is_symlink = 0
        self._real_lstat = os.lstat
        self._real_is_symlink = Path.is_symlink

    def __enter__(self) -> "_LstatCounter":
        real_lstat = self._real_lstat
        real_is_symlink = self._real_is_symlink

        def counting_lstat(path, *a, **k):
            self.lstat += 1
            return real_lstat(path, *a, **k)

        def counting_is_symlink(p, *a, **k):
            self.is_symlink += 1
            return real_is_symlink(p, *a, **k)

        os.lstat = counting_lstat                       # type: ignore[assignment]
        Path.is_symlink = counting_is_symlink           # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> None:
        os.lstat = self._real_lstat                     # type: ignore[assignment]
        Path.is_symlink = self._real_is_symlink         # type: ignore[assignment]


def test_make_entry_with_supplied_st_does_not_lstat(tmp_path):
    """make_entry(st=..., is_symlink_hint=...) issues NO os.lstat / is_symlink.

    The walk threads the scandir-cached stat_result + symlink flag so the common
    path costs ~0 explicit stat syscalls (PEP 471). When st is supplied the
    function must use it verbatim and never re-stat the node.
    """
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "cells.csv"
    target.write_text("gene,score\nFOXG1,0.9\n", encoding="utf-8")
    cfg = load_config()

    st = os.stat(target, follow_symlinks=False)
    with _LstatCounter() as c:
        entry = walker.make_entry(
            target, root, cfg, st=st, is_symlink_hint=False
        )
    assert c.lstat == 0, "make_entry(st=...) called os.lstat %d time(s)" % c.lstat
    assert c.is_symlink == 0, (
        "make_entry(st=...) called Path.is_symlink %d time(s)" % c.is_symlink
    )
    # And the entry is well-formed from the supplied stat (size matches the file).
    assert entry["size_bytes"] == st.st_size
    assert entry["is_symlink"] is False


def test_make_entry_without_st_still_self_stats(tmp_path):
    """The standalone contract holds: make_entry(no st) self-stats via os.lstat
    and yields the SAME entry as the st-supplied call (no behavioural drift)."""
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "cells.csv"
    target.write_text("gene,score\nFOXG1,0.9\n", encoding="utf-8")
    cfg = load_config()

    with _LstatCounter() as c:
        standalone = walker.make_entry(target, root, cfg)
    assert c.lstat >= 1, "make_entry(no st) did not self-stat via os.lstat"

    st = os.stat(target, follow_symlinks=False)
    threaded = walker.make_entry(target, root, cfg, st=st, is_symlink_hint=False)
    assert standalone == threaded, "st vs no-st entries diverged"


def test_walk_common_path_no_explicit_lstat_on_nonsymlinks(fixture_tree, loaded_config):
    """A full walk over the hazard tree issues ZERO explicit os.lstat on any
    NON-symlink node — every regular file's / dir's size+mtime comes from the
    listing primitive's cache (scandir DirEntry under W1; the batched
    getattrlistbulk record under W4).

    W4 (Axis A) note: the bulk getattrlistbulk primitive returns mtime+size for
    all children in one syscall, but does NOT tell us whether a symlink resolves
    to a dir nor give a W1-faithful link size/target — so the FEW symlinks
    (correctness-over-micro-opt) fall back to os.lstat via make_entry's self-stat.
    Those per-symlink lstats are the intended W4 behaviour; what must NOT regress
    is a per-entry lstat storm over the ~25k regular files. Under the os.scandir
    fallback (non-Darwin or a fs without getattrlistbulk) even the symlinks pay
    zero explicit lstat, so this assertion holds in BOTH modes.
    """
    root, _ = fixture_tree
    real_lstat = os.lstat
    lstat_targets: list[str] = []

    def counting_lstat(path, *a, **k):
        lstat_targets.append(os.fspath(path))
        return real_lstat(path, *a, **k)

    os.lstat = counting_lstat  # type: ignore[assignment]
    try:
        entries = list(walker.walk(root, loaded_config))
    finally:
        os.lstat = real_lstat  # type: ignore[assignment]

    assert entries, "walk produced no entries"
    non_symlink_lstats = [p for p in lstat_targets if not os.path.islink(p)]
    assert not non_symlink_lstats, (
        "walk issued explicit os.lstat on non-symlink node(s): %s" % non_symlink_lstats
    )


# --------------------------------------------------------------------------- #
# (c) racy within-window dirty rule
# --------------------------------------------------------------------------- #

def _csv_entry_for(root: Path, cfg, **kw):
    """make_entry for tables/cells.csv with whatever kwargs the test needs."""
    target = root / "tables" / "cells.csv"
    return walker.make_entry(target, root, cfg, **kw)


def test_racy_window_forces_reextract_within_window(fixture_tree, loaded_config):
    """A cached file whose mtime is within racy_window_seconds of the reference
    time is RE-EXTRACTED (cache NOT reused), even with identical size+mtime.

    We seed the cache with a sentinel meta that real extraction would never
    produce; if the entry comes back carrying that sentinel, the cache was reused
    (rule failed); if it carries real extractor output, it was re-extracted.
    """
    root, _ = fixture_tree
    cfg = loaded_config
    target = root / "tables" / "cells.csv"
    st = os.stat(target, follow_symlinks=False)
    rel = target.relative_to(root).as_posix()

    # Build a cache entry that EXACTLY matches the file's size+mtime, but carries a
    # sentinel extractor/meta so a reuse is unambiguously detectable.
    sentinel = {"__sentinel__": "from_cache_should_not_survive"}
    cache = {
        rel: {
            "path": rel,
            "size_bytes": int(st.st_size),
            "mtime_iso": walker.iso_mtime(st.st_mtime),
            "extractor": "sentinel_extractor",
            "meta": dict(sentinel),
        }
    }

    # Reference time AFTER the file's mtime -> file is within the window -> dirty.
    reference_time = st.st_mtime + 0.5  # file_mtime > reference_time - window
    entry = _csv_entry_for(
        root, cfg, cache=cache, st=st, is_symlink_hint=False,
        reference_time=reference_time,
    )
    assert entry["meta"] != sentinel, "racy file was cache-reused (should re-extract)"
    assert entry["extractor"] != "sentinel_extractor"
    # It was actually extracted as a real CSV.
    assert entry["extractor"] == "tabular"


def test_racy_window_reuses_cache_outside_window(fixture_tree, loaded_config):
    """A cached file whose mtime is comfortably OLDER than the reference time
    (outside the racy window) is cache-reused (the sentinel survives), proving the
    rule is a targeted window and not a blanket cache disable."""
    root, _ = fixture_tree
    cfg = loaded_config
    target = root / "tables" / "cells.csv"
    st = os.stat(target, follow_symlinks=False)
    rel = target.relative_to(root).as_posix()

    sentinel = {"__sentinel__": "from_cache_should_survive"}
    cache = {
        rel: {
            "path": rel,
            "size_bytes": int(st.st_size),
            "mtime_iso": walker.iso_mtime(st.st_mtime),
            "extractor": "sentinel_extractor",
            "meta": dict(sentinel),
        }
    }

    window = getattr(cfg, "racy_window_seconds", 2)
    # Reference time well in the future of the file mtime + window: file_mtime is
    # NOT > reference_time - window, so the file is clean and the cache is reused.
    reference_time = st.st_mtime + window + 3600.0
    entry = _csv_entry_for(
        root, cfg, cache=cache, st=st, is_symlink_hint=False,
        reference_time=reference_time,
    )
    assert entry["meta"] == sentinel, "non-racy cached entry was not reused"
    assert entry["extractor"] == "sentinel_extractor"


def test_racy_rule_disabled_when_reference_time_none(fixture_tree, loaded_config):
    """reference_time=None disables the rule entirely (cache reused as before),
    preserving the pre-W1 incremental behaviour when no prior index time exists."""
    root, _ = fixture_tree
    cfg = loaded_config
    target = root / "tables" / "cells.csv"
    st = os.stat(target, follow_symlinks=False)
    rel = target.relative_to(root).as_posix()

    sentinel = {"__sentinel__": "reused_when_disabled"}
    cache = {
        rel: {
            "path": rel,
            "size_bytes": int(st.st_size),
            "mtime_iso": walker.iso_mtime(st.st_mtime),
            "extractor": "sentinel_extractor",
            "meta": dict(sentinel),
        }
    }
    entry = _csv_entry_for(
        root, cfg, cache=cache, st=st, is_symlink_hint=False, reference_time=None
    )
    assert entry["meta"] == sentinel, "rule fired even though reference_time=None"


def test_is_racy_helper_boundary():
    """The _is_racy predicate matches the documented rule at the boundary:
    file_mtime > reference_time - racy_window_seconds."""

    class _Cfg:
        racy_window_seconds = 2

    cfg = _Cfg()
    ref = 1000.0
    # Inside the window (newer than ref-2): dirty.
    assert walker._is_racy(999.0, ref, cfg) is True
    assert walker._is_racy(1000.0, ref, cfg) is True
    assert walker._is_racy(1005.0, ref, cfg) is True
    # Exactly at the boundary ref-window is NOT strictly greater -> clean.
    assert walker._is_racy(998.0, ref, cfg) is False
    # Older than the window: clean.
    assert walker._is_racy(990.0, ref, cfg) is False
    # None reference disables the rule.
    assert walker._is_racy(1e9, None, cfg) is False
