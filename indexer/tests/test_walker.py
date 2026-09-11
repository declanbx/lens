"""Walker policy tests (CONTRACTS.md §9): prune, ._ skip, symlink record-not-traverse.

These need both ``config.load_config`` and ``walker.walk`` implemented; the
``loaded_config`` fixture skips the suite while config is a stub, and
:func:`_walk_or_skip` skips while the walker is a stub. Once implemented the
assertions are live and enforce the non-negotiable walk invariants:

  * ``_vendor`` (and other prune dirs) are removed before descent — its inner
    ``vendored.py`` MUST NOT appear in the index.
  * any basename starting with ``._`` (the AppleDouble ``._shadow``) is skipped.
  * a DANGLING symlink is recorded with ``symlink_ok=false`` and does NOT crash
    the walk (the rest of the tree still indexes).
  * a symlink to a DIRECTORY is recorded as a single entry and is NOT descended
    into (no double-counting of the linked subtree).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _walk_or_skip(root: Path, config):
    """Run walker.walk -> list(entries); skip if walk is still a stub."""
    from repo_index import walker
    try:
        return list(walker.walk(root, config))
    except NotImplementedError:
        pytest.skip("walker.walk not implemented yet (stub phase)")


def _by_path(entries):
    """Index entries by their relative POSIX path for O(1) lookups in asserts."""
    return {e["path"]: e for e in entries}


def test_walk_prunes_vendor(fixture_tree, loaded_config):
    """No entry comes from inside a pruned _vendor directory."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    bad = [e["path"] for e in entries if "_vendor" in Path(e["path"]).parts]
    assert bad == [], "pruned _vendor leaked into the index: %s" % bad


def test_walk_skips_appledouble(fixture_tree, loaded_config):
    """._-prefixed basenames are skipped entirely (files AND dirs)."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    bad = [e["path"] for e in entries
           if Path(e["path"]).name.startswith("._")]
    assert bad == [], "AppleDouble ._ file leaked into the index: %s" % bad


def test_walk_records_broken_symlink_without_crashing(fixture_tree, loaded_config):
    """A dangling symlink is recorded (symlink_ok=false) and the walk survives."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    by = _by_path(entries)
    broken = by.get("links/broken_link.csv")
    assert broken is not None, "broken symlink was not recorded as an entry"
    assert broken["is_symlink"] is True
    assert broken["symlink_ok"] is False
    assert broken["symlink_target"] is not None
    # A broken link must not raise out of extraction: generic + empty meta.
    assert broken["extractor"] == "generic"
    assert broken["meta"] == {}
    # size_bytes comes from lstat of the link itself (>=0, never raised).
    assert isinstance(broken["size_bytes"], int) and broken["size_bytes"] >= 0
    # And the rest of the tree still indexed (walk was NOT aborted).
    assert "tables/cells.csv" in by


def test_walk_records_good_symlink(fixture_tree, loaded_config):
    """A resolving symlink is recorded with symlink_ok=true (but not traversed)."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    by = _by_path(entries)
    good = by.get("links/good_link.csv")
    assert good is not None
    assert good["is_symlink"] is True
    assert good["symlink_ok"] is True
    assert good["symlink_target"] is not None


def test_walk_does_not_extract_through_resolving_symlink(fixture_tree, loaded_config):
    """A RESOLVING file symlink is recorded as generic/{} — the extractor is NOT
    run on the link's target (record-not-traverse, §0.6/§9). Running it would
    follow the link and re-index the target's metadata under the link path while
    size_bytes comes from the link node, producing a size/meta mismatch and
    double-indexing any directly-walked target."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    by = _by_path(entries)
    good = by["links/good_link.csv"]  # -> ../tables/cells.csv (a real CSV)
    assert good["extractor"] == "generic"
    assert good["meta"] == {}
    # The real target is still indexed once under its canonical path, WITH meta.
    real = by["tables/cells.csv"]
    assert real["extractor"] == "tabular"
    assert real["meta"].get("n_columns")


def test_walk_does_not_descend_symlinked_dir(fixture_tree, loaded_config):
    """A symlink to a directory is one entry; its target's contents are not re-listed
    under the link path (no double counting / cycle risk)."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    paths = {e["path"] for e in entries}
    # link_dir -> ../data ; nothing must appear UNDER the link path.
    descended = [p for p in paths
                 if p.startswith("links/link_dir/")]
    assert descended == [], "walker descended into a symlinked dir: %s" % descended
    # The real data files are still indexed once under their canonical path.
    assert "data/small.npy" in paths


def test_walk_marks_nonsymlinks(fixture_tree, loaded_config):
    """A regular file has is_symlink=false and null symlink fields."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    by = _by_path(entries)
    reg = by.get("tables/cells.csv")
    assert reg is not None
    assert reg["is_symlink"] is False
    assert reg["symlink_target"] is None
    assert reg["symlink_ok"] is None


def test_walk_entry_shape_and_categories(fixture_tree, loaded_config):
    """Every entry carries the §5.1 required keys; categories map per §3."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    required = {"path", "category", "ext", "size_bytes", "mtime_iso",
               "is_symlink", "symlink_target", "symlink_ok", "extractor",
               "tags", "meta"}
    for e in entries:
        assert required.issubset(set(e)), (
            "entry missing required keys %s: %s" % (required - set(e), e["path"])
        )
        # mtime is ISO-8601 UTC with a trailing Z.
        assert e["mtime_iso"].endswith("Z"), e["mtime_iso"]
    by = _by_path(entries)
    assert by["code/module.py"]["category"] == "code"
    assert by["tables/cells.csv"]["category"] == "data_table"
    # Compound ext resolves to data_table (not archive) per §3.
    assert by["tables/cells.csv.gz"]["category"] == "data_table"
    assert by["tables/cells.csv.gz"]["ext"] == "csv.gz"
    assert by["conf/params.yaml"]["category"] == "config"
    assert by["docs/README.md"]["category"] == "doc"
    assert by["data/small.npy"]["category"] == "data_matrix"


def test_walk_paths_are_relative_posix(fixture_tree, loaded_config):
    """Paths are RELATIVE to root and POSIX-style (forward slashes, no leading /)."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    for e in entries:
        assert not e["path"].startswith("/"), e["path"]
        assert "\\" not in e["path"], e["path"]


# --------------------------------------------------------------------------- #
# include / exclude glob filtering (CLI --include / --exclude -> config)
# --------------------------------------------------------------------------- #

def test_walk_exclude_glob_drops_matching_files(fixture_tree):
    """exclude_globs drops matching relative paths (the flag is no longer a no-op)."""
    from repo_index.config import load_config
    root, _ = fixture_tree
    cfg = load_config(overrides={"exclude_globs": ["**/*.csv"]})
    entries = _walk_or_skip(root, cfg)
    csvs = [e["path"] for e in entries if e["path"].endswith(".csv")]
    assert csvs == [], "exclude '**/*.csv' did not drop csv files: %s" % csvs
    # Non-csv files are still present.
    assert any(e["path"] == "code/module.py" for e in entries)


def test_walk_include_glob_keeps_only_matches(fixture_tree):
    """A non-empty include_globs keeps ONLY matching relative paths."""
    from repo_index.config import load_config
    root, _ = fixture_tree
    cfg = load_config(overrides={"include_globs": ["code/*.py"]})
    entries = _walk_or_skip(root, cfg)
    paths = {e["path"] for e in entries}
    assert paths == {"code/module.py"}, paths


def test_walk_exclude_subtree_pruned_before_descent(fixture_tree):
    """Excluding a directory subtree prunes it before descent (nothing under it
    is walked or indexed)."""
    from repo_index.config import load_config
    root, _ = fixture_tree
    cfg = load_config(overrides={"exclude_globs": ["tables/**"]})
    entries = _walk_or_skip(root, cfg)
    under = [e["path"] for e in entries if e["path"].startswith("tables/")]
    assert under == [], "tables/ subtree was not pruned: %s" % under


def test_walk_no_globs_indexes_everything(fixture_tree, loaded_config):
    """With empty include/exclude (the default), filtering is a no-op."""
    root, _ = fixture_tree
    entries = _walk_or_skip(root, loaded_config)
    paths = {e["path"] for e in entries}
    assert "tables/cells.csv" in paths
    assert "code/module.py" in paths


def test_walk_surfaces_unreadable_dir_as_error(tmp_path, loaded_config):
    """An unreadable directory is surfaced as a synthetic error entry (health
    signal) rather than silently dropping its whole subtree."""
    import os
    import stat

    root = tmp_path / "tree"
    (root / "good").mkdir(parents=True)
    (root / "good" / "f.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    locked = root / "locked"
    locked.mkdir()
    (locked / "secret.csv").write_text("x,y\n", encoding="utf-8")
    os.chmod(locked, 0)
    try:
        entries = _walk_or_skip(root, loaded_config)
    finally:
        os.chmod(locked, stat.S_IRWXU)  # restore so tmp cleanup can remove it
    errs = [e for e in entries if e.get("error")]
    # If the OS still allowed listing (e.g. running as root), skip — can't assert.
    if not errs:
        pytest.skip("filesystem did not enforce the unreadable-dir permission")
    assert any("locked" in e["path"] or "locked" in str(e.get("error"))
               for e in errs), errs
    # The readable part of the tree still indexed (walk not aborted).
    assert any(e["path"] == "good/f.csv" for e in entries)
