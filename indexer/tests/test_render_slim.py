"""Tests for the HTML-embed slimming + the skip-write-on-unchanged gate.

Slimming (render_html): long meta arrays (wide-table `columns`) are truncated to a
head + `n_<key>_total` count IN THE EMBED ONLY; `obs_columns` is deliberately kept
full (tiny + search-critical); the input manifest is never mutated; and the full
arrays must remain in INDEX.json / INDEX.jsonl (agents grep them).

Skip-write (cli.build_index + manifest._peek_committed_digest): an incremental
rebuild over an unchanged tree must NOT re-serialize the ~100 MB of artifacts, but
a real change must.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from repo_index import render_html as rh


# --------------------------------------------------------------------------- #
# Embed truncation
# --------------------------------------------------------------------------- #

def test_truncate_meta_keeps_obs_columns_drops_wide_columns():
    cols = [f"c{i}" for i in range(200)]
    obs = [f"o{i}" for i in range(60)]
    meta = {"columns": cols, "obs_columns": obs, "n_columns": 200}
    out = rh._truncate_meta_for_html(meta, head=48)

    assert out is not meta                       # a copy was made (something changed)
    assert out["columns"] == cols[:48]           # wide columns truncated to head
    assert out["n_columns_total"] == 200         # ... with the true total recorded
    assert out["obs_columns"] == obs             # obs_columns kept FULL (excluded key)
    assert "n_obs_columns_total" not in out
    assert meta["columns"] == cols               # input NOT mutated


def test_truncate_meta_returns_same_object_when_nothing_to_do():
    small = {"columns": ["a", "b"], "n_columns": 2, "obs_columns": ["x"]}
    assert rh._truncate_meta_for_html(small, head=48) is small   # zero-cost passthrough


def test_truncate_caps_first_paragraph():
    meta = {"first_paragraph": "x" * 5000}
    out = rh._truncate_meta_for_html(meta)
    assert len(out["first_paragraph"]) <= rh._HTML_FIRST_PARAGRAPH_MAX + 1
    assert out["first_paragraph"].endswith("…")


def _manifest_with_wide_entry():
    cols = [f"g{i}" for i in range(300)]
    return {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "root": "/tmp/x", "git_commit": None, "tool_version": "test",
        "content_digest": "0" * 64,
        "summary": {"total_files": 1, "total_bytes": 99, "by_category": {},
                    "by_ext": {}, "n_symlinks": 0, "n_broken_symlinks": 0, "n_errors": 0},
        "entries": [{
            "path": "t/wide.csv", "category": "data_table", "ext": "csv",
            "size_bytes": 99, "mtime_iso": "2026-01-01T00:00:00Z",
            "is_symlink": False, "symlink_target": None, "symlink_ok": None,
            "extractor": "tabular", "tags": [],
            "meta": {"columns": cols, "obs_columns": [f"o{i}" for i in range(60)],
                     "n_columns": 300},
        }],
    }, cols


def _extract_embed(html: str) -> dict:
    m = re.search(
        r'<script id="repo-index-data" type="application/json">(.*?)</script>',
        html, re.S,
    )
    assert m, "embedded data <script> not found"
    return json.loads(m.group(1))   # json.loads decodes the \\u003c escaping for us


def test_render_html_truncates_embed_and_does_not_mutate_input():
    manifest, cols = _manifest_with_wide_entry()
    html = rh.render_html(manifest)

    blob = _extract_embed(html)
    e = blob["entries"][0]
    assert len(e["meta"]["columns"]) == rh._HTML_HEAD          # embed: head only
    assert e["meta"]["n_columns_total"] == 300                # ... + true total
    assert len(e["meta"]["obs_columns"]) == 60                # obs kept full

    # the ORIGINAL manifest entry is untouched (full 300 cols) -> write_outputs,
    # which receives this same manifest, still emits the full array to JSON/JSONL.
    assert manifest["entries"][0]["meta"]["columns"] == cols

    # the source <script> text node is freed after parse (memory)
    assert "_s.remove()" in html


def test_truncation_shrinks_embed():
    manifest, _ = _manifest_with_wide_entry()
    # The real (head=48) embedded blob ...
    trunc_blob = rh._embed_json(rh._project_manifest(manifest))
    # ... vs an embed built with an effectively-infinite head (no truncation). We
    # pass head explicitly because the module default is bound at def-time.
    full_proj = dict(rh._project_manifest(manifest))
    full_proj["entries"] = [
        rh._project_entry_for_html(e, head=10**9) for e in manifest["entries"]
    ]
    full_blob = rh._embed_json(full_proj)
    assert len(trunc_blob) < len(full_blob)   # truncation makes the embed smaller


# --------------------------------------------------------------------------- #
# Default browse-sort is "newest" (the app/fresh index opens newest-first)
# --------------------------------------------------------------------------- #

def test_default_sortkey_is_newest():
    """The embedded JS must default to "newest" so the app/fresh index opens with
    the most-recently-touched files at the top of every sibling group.

    NOTE (deliberate trade-off): on coarse-mtime exFAT a touched / re-stat'd file
    can jump to the top of its sibling group and reshuffle the tree mid-browse —
    the stable "name" sort stays *available* in the sort menu for when a fixed order
    is wanted. This is the inverse of the earlier default (which was "name"); the
    change was an explicit product decision (open newest-first), so this test pins
    the new contract rather than the old one.
    """
    manifest, _ = _manifest_with_wide_entry()
    html = rh.render_html(manifest)

    # the active default is the mtime-descending key ...
    m = re.search(r'let\s+sortKey\s*=\s*"([^"]+)"', html)
    assert m, "could not find the `let sortKey=...` default in the embedded JS"
    assert m.group(1) == "newest", f'default sortKey should be "newest", got "{m.group(1)}"'

    # ... and is NOT the old name-stable default any more
    assert 'let sortKey="name"' not in html

    # but "name" is still a selectable entry in the SORTS menu list (not removed)
    assert '["name","Name"]' in html

    # the static toolbar label matches the new default (before the JS init runs)
    assert '<span id="sortLabel">Newest</span>' in html


def test_reveal_in_tree_disambiguates_dir_sentinel():
    """revealInTree must not blindly trust a trailing "/__dir__" as the directory
    sentinel: a real FILE entry at the exact path takes precedence (so a file named
    "__dir__" still selects, rather than mis-routing to its parent folder). Lock the
    exact-match guard into the rendered page."""
    manifest, _ = _manifest_with_wide_entry()
    html = rh.render_html(manifest)
    assert "const exact = ENTRIES.find(x=>x.path===path);" in html
    assert 'const isDir = !exact && path.endsWith("/__dir__");' in html


# --------------------------------------------------------------------------- #
# Skip-write-on-unchanged gate
# --------------------------------------------------------------------------- #

def test_skip_write_on_unchanged_then_rewrite_on_change(fixture_tree):
    from repo_index.cli import build_index
    from repo_index import manifest as manifest_mod

    root, _ = fixture_tree
    out_dir = root / "_repo_index"

    # 1) initial FULL build writes all artifacts
    build_index(root, out_dir, incremental=False, quiet=True)
    idx = out_dir / "INDEX.json"
    html = out_dir / "INDEX.html"
    assert idx.exists() and html.exists()
    digest1 = manifest_mod._peek_committed_digest(out_dir)
    assert digest1 and len(digest1) == 64
    json_mtime = idx.stat().st_mtime_ns
    html_mtime = html.stat().st_mtime_ns

    # 2) incremental rebuild over the UNCHANGED tree -> gate fires, no rewrite
    time.sleep(0.02)
    build_index(root, out_dir, incremental=True, quiet=True)
    assert idx.stat().st_mtime_ns == json_mtime          # INDEX.json untouched
    assert html.stat().st_mtime_ns == html_mtime          # INDEX.html untouched
    assert manifest_mod._peek_committed_digest(out_dir) == digest1

    # 3) CHANGE the tree -> incremental rebuild MUST rewrite (new digest)
    (root / "added_file.txt").write_text("new content", encoding="utf-8")
    time.sleep(0.02)
    build_index(root, out_dir, incremental=True, quiet=True)
    assert idx.stat().st_mtime_ns != json_mtime          # rewritten
    assert manifest_mod._peek_committed_digest(out_dir) != digest1


def test_peek_digest_missing_file_returns_none(tmp_path: Path):
    from repo_index import manifest as manifest_mod
    assert manifest_mod._peek_committed_digest(tmp_path) is None
