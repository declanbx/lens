"""Tests for the index_columns on/off toggle (FILE-SIZE / build-speed lever).

The toggle (config key ``index_columns`` / CLI ``--columns`` / ``--no-columns``,
default ON) drops the wide per-CSV/TSV/parquet ``columns`` list from each tabular
entry's meta while ALWAYS keeping the ``n_columns`` count (and the tiny h5ad
obs_columns / var_columns, which live under different keys). The strip is central in
walker.make_entry — NOT in the extractor — so it applies on BOTH the fresh-extract
and incremental cache-reuse paths, and a copy is made so the shared prior cache is
never mutated. Flipping the toggle forces a full re-extract in cli.build_index so the
columns actually come back / get stripped over an unchanged tree.

Coverage:
  (a) walker strips columns + keeps n_columns when off, on fresh AND cache-reuse,
      without mutating the prior cache;
  (b) build_index forces a full re-extract when the toggle flips (columns reappear
      on --columns after a --no-columns build over an UNCHANGED tree);
  (c) config: --no-columns -> index_columns False, default True;
  (d) content_digest differs columns-on vs off but is STABLE across same-setting
      builds;
  (e) render: COLUMNS card degrades to a count-only placeholder;
  (f) _peek_prior_index_columns reads the prior setting from a bounded head.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from repo_index import config as config_mod
from repo_index import manifest as manifest_mod
from repo_index import render_html as rh
from repo_index import walker as walker_mod


# --------------------------------------------------------------------------- #
# (c) config + CLI flag parsing
# --------------------------------------------------------------------------- #

def test_config_index_columns_default_true():
    cfg = config_mod.load_config()
    assert cfg.index_columns is True
    # Echoed into raw so it surfaces in config_used.
    assert cfg.raw.get("index_columns") is True


def test_config_index_columns_override_false():
    cfg = config_mod.load_config(overrides={"index_columns": False})
    assert cfg.index_columns is False
    assert cfg.raw.get("index_columns") is False


def test_cli_no_columns_sets_override_false_and_default_none():
    from repo_index.cli import _build_parser, _overrides_from_args

    parser = _build_parser()

    # No flag -> dest is None -> NOT in overrides (preserves config default).
    args = parser.parse_args(["build"])
    assert args.index_columns is None
    assert "index_columns" not in _overrides_from_args(args)

    # --no-columns -> False override.
    args = parser.parse_args(["build", "--no-columns"])
    assert args.index_columns is False
    assert _overrides_from_args(args)["index_columns"] is False

    # --columns -> True override (lets the user re-enable / re-index).
    args = parser.parse_args(["build", "--columns"])
    assert args.index_columns is True
    assert _overrides_from_args(args)["index_columns"] is True


# --------------------------------------------------------------------------- #
# (a) walker strip — fresh extract + cache reuse, no mutation
# --------------------------------------------------------------------------- #

def test_walker_helper_keeps_columns_when_on():
    cfg = config_mod.load_config()  # index_columns True
    meta = {"columns": ["a", "b"], "n_columns": 2}
    out = walker_mod._maybe_strip_columns(meta, cfg)
    assert out is meta  # zero-cost passthrough


def test_walker_helper_strips_on_copy_when_off():
    cfg = config_mod.load_config(overrides={"index_columns": False})
    meta = {"columns": ["a", "b"], "n_columns": 2, "delimiter": ","}
    out = walker_mod._maybe_strip_columns(meta, cfg)
    assert out is not meta              # a copy
    assert "columns" not in out
    assert out["n_columns"] == 2       # count kept
    assert out["delimiter"] == ","     # siblings kept
    assert meta["columns"] == ["a", "b"]  # input NOT mutated


def test_walker_walk_strips_columns_fresh_extract(fixture_tree):
    root, _ = fixture_tree
    cfg = config_mod.load_config(overrides={"index_columns": False})
    entries = list(walker_mod.walk(root, cfg))
    csv = next(e for e in entries if e["path"].endswith("tables/cells.csv"))
    assert csv["extractor"] == "tabular"
    assert "columns" not in csv["meta"]
    assert csv["meta"]["n_columns"] == 3   # gene,score,label

    # With the default config the columns ARE present.
    cfg_on = config_mod.load_config()
    entries_on = list(walker_mod.walk(root, cfg_on))
    csv_on = next(e for e in entries_on if e["path"].endswith("tables/cells.csv"))
    assert csv_on["meta"]["columns"] == ["gene", "score", "label"]
    assert csv_on["meta"]["n_columns"] == 3


def test_walker_strips_cache_reuse_without_mutating_prior(fixture_tree):
    root, _ = fixture_tree
    cfg_off = config_mod.load_config(overrides={"index_columns": False})

    # Build a prior cache the way load_prior_entries would: entries carrying the
    # full columns list (from a prior columns-ON build), keyed by rel path.
    cfg_on = config_mod.load_config()
    prior_entries = list(walker_mod.walk(root, cfg_on))
    cache = {e["path"]: e for e in prior_entries}
    csv_rel = next(p for p in cache if p.endswith("tables/cells.csv"))
    assert cache[csv_rel]["meta"]["columns"] == ["gene", "score", "label"]
    cached_meta = cache[csv_rel]["meta"]

    # Re-walk with the toggle OFF, reusing that cache (unchanged tree -> cache hit).
    ref = time.time() + 10_000  # far future so nothing is "racy"; cache reused
    entries = list(walker_mod.walk(root, cfg_off, cache=cache, reference_time=None))
    csv = next(e for e in entries if e["path"].endswith("tables/cells.csv"))
    assert "columns" not in csv["meta"]      # stripped on the reuse path
    assert csv["meta"]["n_columns"] == 3     # count kept

    # The SHARED prior cache dict was NOT mutated (strip copied).
    assert cached_meta["columns"] == ["gene", "score", "label"]
    assert "columns" in cache[csv_rel]["meta"]


# --------------------------------------------------------------------------- #
# (d) digest: stable across same-setting builds, differs across settings
# --------------------------------------------------------------------------- #

def _digest_for(root: Path, *, index_columns: bool) -> str:
    cfg = config_mod.load_config(overrides={"index_columns": index_columns})
    entries = list(walker_mod.walk(root, cfg))
    man = manifest_mod.build_manifest(root, entries, cfg, "test")
    return man["content_digest"]


def test_digest_differs_on_vs_off_but_stable_within(fixture_tree):
    root, _ = fixture_tree
    on1 = _digest_for(root, index_columns=True)
    on2 = _digest_for(root, index_columns=True)
    off1 = _digest_for(root, index_columns=False)
    off2 = _digest_for(root, index_columns=False)

    assert on1 == on2          # stable across two same-setting builds
    assert off1 == off2        # stable
    assert on1 != off1         # the toggle materially changes meta


# --------------------------------------------------------------------------- #
# (f) _peek_prior_index_columns
# --------------------------------------------------------------------------- #

def test_peek_prior_index_columns_missing_returns_none(tmp_path: Path):
    assert manifest_mod._peek_prior_index_columns(tmp_path) is None


def test_peek_prior_index_columns_reads_setting(fixture_tree):
    from repo_index.cli import build_index

    root, _ = fixture_tree
    out_dir = root / "_repo_index"

    build_index(root, out_dir, incremental=False, quiet=True,
                overrides={"index_columns": True})
    assert manifest_mod._peek_prior_index_columns(out_dir) is True

    build_index(root, out_dir, incremental=False, quiet=True,
                overrides={"index_columns": False})
    assert manifest_mod._peek_prior_index_columns(out_dir) is False


# --------------------------------------------------------------------------- #
# (b) build_index forces a full re-extract when the toggle flips
# --------------------------------------------------------------------------- #

def _csv_entry(out_dir: Path) -> dict:
    obj = json.loads((out_dir / "INDEX.json").read_text(encoding="utf-8"))
    return next(e for e in obj["entries"] if e["path"].endswith("tables/cells.csv"))


def test_toggle_flip_forces_rebuild_over_unchanged_tree(fixture_tree):
    from repo_index.cli import build_index

    root, _ = fixture_tree
    out_dir = root / "_repo_index"

    # 1) full build WITH columns.
    build_index(root, out_dir, incremental=False, quiet=True,
                overrides={"index_columns": True})
    on_entry = _csv_entry(out_dir)
    assert on_entry["meta"]["columns"] == ["gene", "score", "label"]
    digest_on = manifest_mod._peek_committed_digest(out_dir)

    # 2) INCREMENTAL build with --no-columns over the UNCHANGED tree. Without the
    #    force-full-on-flip this would cache-reuse and keep columns; it must instead
    #    re-extract and strip them.
    time.sleep(0.02)
    build_index(root, out_dir, incremental=True, quiet=True,
                overrides={"index_columns": False})
    off_entry = _csv_entry(out_dir)
    assert "columns" not in off_entry["meta"]
    assert off_entry["meta"]["n_columns"] == 3
    digest_off = manifest_mod._peek_committed_digest(out_dir)
    assert digest_off != digest_on   # the flip really rewrote the index

    # 3) INCREMENTAL build with --columns again over the UNCHANGED tree: the columns
    #    must REAPPEAR (re-index works).
    time.sleep(0.02)
    build_index(root, out_dir, incremental=True, quiet=True,
                overrides={"index_columns": True})
    back_entry = _csv_entry(out_dir)
    assert back_entry["meta"]["columns"] == ["gene", "score", "label"]
    assert manifest_mod._peek_committed_digest(out_dir) == digest_on  # round-trip


def test_no_columns_persists_across_plain_freshen(fixture_tree):
    """A PLAIN read (no --columns/--no-columns) must NOT reverse a prior
    --no-columns build. `repo_index query`/`open`/`export-sqlite` auto-freshen via
    build_index with overrides derived from CLI args; a plain read carries no
    index_columns override. The toggle must be sourced from the prior index's
    config_used as the effective default, not from the config default (True) —
    otherwise the freshen silently re-adds every wide `columns` list (the ~21.5 MB
    the feature exists to remove) and pays a full re-extract. Regression guard."""
    from repo_index import cli

    root, _ = fixture_tree
    out_dir = root / "_repo_index"

    # 1) A --no-columns build (what the user explicitly asked for once).
    cli.build_index(root, out_dir, incremental=False, quiet=True,
                    overrides={"index_columns": False})
    assert "columns" not in _csv_entry(out_dir)["meta"]
    assert _csv_entry(out_dir)["meta"]["n_columns"] == 3
    digest_off = manifest_mod._peek_committed_digest(out_dir)

    # 2) Backdate INDEX.json so _freshen's TTL does not short-circuit the re-walk,
    #    then run the EXACT call a plain `query` makes (overrides=None).
    idx = out_dir / "INDEX.json"
    old = time.time() - 3600
    import os
    os.utime(idx, (old, old))
    cli._freshen(root, out_dir, None, None,
                 want_html=False, no_refresh=False, full=False)

    # 3) Columns STILL stripped, config_used STILL False, digest unchanged (no
    #    spurious full re-extract that re-bloats the index).
    after = _csv_entry(out_dir)
    assert "columns" not in after["meta"], (
        "plain freshen silently re-added the wide columns list — the --no-columns "
        "toggle is not persistent"
    )
    assert after["meta"]["n_columns"] == 3
    assert manifest_mod._peek_prior_index_columns(out_dir) is False
    assert manifest_mod._peek_committed_digest(out_dir) == digest_off


def test_explicit_columns_flag_still_overrides_prior(fixture_tree):
    """Persistence must NOT swallow an EXPLICIT flag: a --columns build over a prior
    --no-columns index must still re-index the columns (the prior is only the
    default, not a lock)."""
    from repo_index import cli

    root, _ = fixture_tree
    out_dir = root / "_repo_index"

    cli.build_index(root, out_dir, incremental=False, quiet=True,
                    overrides={"index_columns": False})
    assert "columns" not in _csv_entry(out_dir)["meta"]

    # Explicit --columns over the unchanged tree: columns must reappear.
    time.sleep(0.02)
    cli.build_index(root, out_dir, incremental=True, quiet=True,
                    overrides={"index_columns": True})
    assert _csv_entry(out_dir)["meta"]["columns"] == ["gene", "score", "label"]
    assert manifest_mod._peek_prior_index_columns(out_dir) is True


# --------------------------------------------------------------------------- #
# (e) render: COLUMNS card degrades + manifest schema still validates
# --------------------------------------------------------------------------- #

def _manifest_no_columns():
    """A manifest whose tabular entry has n_columns but NO columns, with
    config_used.index_columns False (a --no-columns build)."""
    return {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "root": "/tmp/x", "git_commit": None, "tool_version": "test",
        "content_digest": "0" * 64,
        "config_used": {"index_columns": False},
        "summary": {"total_files": 1, "total_bytes": 99, "by_category": {},
                    "by_ext": {}, "n_symlinks": 0, "n_broken_symlinks": 0,
                    "n_errors": 0},
        "entries": [{
            "path": "t/wide.csv", "category": "data_table", "ext": "csv",
            "size_bytes": 99, "mtime_iso": "2026-01-01T00:00:00Z",
            "is_symlink": False, "symlink_target": None, "symlink_ok": None,
            "extractor": "tabular", "tags": [],
            "meta": {"n_columns": 300},   # NO `columns` key
        }],
    }


def test_project_manifest_carries_index_columns():
    man = _manifest_no_columns()
    proj = rh._project_manifest(man)
    assert proj["index_columns"] is False

    # Legacy manifest (no config_used / no flag) -> default True (backward-compat).
    man_legacy = dict(man)
    man_legacy.pop("config_used")
    assert rh._project_manifest(man_legacy)["index_columns"] is True


def test_render_columns_card_degrades_to_placeholder():
    html = rh.render_html(_manifest_no_columns())
    # DATA carries the flag for the JS to branch on.
    assert '"index_columns": false' in html or '"index_columns":false' in html
    # The placeholder branch (count-only, with the in-app re-index cue) is present.
    assert "DATA.index_columns===false" in html
    assert "names not indexed" in html
    assert "re-index" in html   # points to the in-app ⊞ toggle, not the terminal


def test_truncate_meta_handles_absent_columns_without_fabricating():
    meta = {"n_columns": 300, "obs_columns": ["x"]}
    out = rh._truncate_meta_for_html(meta, head=48)
    assert out is meta                       # nothing to truncate -> passthrough
    assert "columns" not in out              # not invented
    assert "n_columns_total" not in out      # not fabricated
    assert out["n_columns"] == 300


def test_manifest_schema_still_valid_with_index_columns(fixture_tree):
    """Building with the flag set keeps INDEX.json schema-valid (the flag rides
    inside config_used, which is schema-open; no new top-level key was added)."""
    from repo_index.cli import build_index

    root, _ = fixture_tree
    out_dir = root / "_repo_index"
    build_index(root, out_dir, incremental=False, quiet=True,
                overrides={"index_columns": False})
    obj = json.loads((out_dir / "INDEX.json").read_text(encoding="utf-8"))
    # The flag is echoed in config_used, NOT as a new top-level key.
    assert obj["config_used"]["index_columns"] is False
    assert "index_columns" not in {k for k in obj if k != "config_used"}
