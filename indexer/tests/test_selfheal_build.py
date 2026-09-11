"""End-to-end build + self-heal drift tests (CONTRACTS.md §7, §8, §11).

Exercises the top-level ``build_index`` orchestration (writes all artifacts) and
``selfheal.check`` (re-walks, recomputes the digest, reports OK / drift). Each
test skips while the relevant orchestration piece is still a stub, so the suite
is green during the stub phase and becomes a live integration check once the
render+cli / outputs owners land their code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_build_index_writes_artifacts(fixture_tree, tmp_path):
    """build_index() walks the tree and writes INDEX.json + INDEX.jsonl."""
    root, _ = fixture_tree
    from repo_index import cli
    out_dir = tmp_path / "out"
    try:
        manifest = cli.build_index(root, out_dir=out_dir)
    except NotImplementedError:
        pytest.skip("cli.build_index not implemented yet (stub phase)")
    assert (out_dir / "INDEX.json").exists()
    assert (out_dir / "INDEX.jsonl").exists()
    doc = json.loads((out_dir / "INDEX.json").read_text(encoding="utf-8"))
    assert doc["schema_version"] == "1.0"
    assert doc["content_digest"] == manifest["content_digest"]


def test_cli_query_verbs_route_through_front_end(fixture_tree, tmp_path, capsys):
    """The advertised `python -m repo_index query <verb> ...` forms reach
    query.main through cli.main (previously the top-level parser rejected the
    verb argument with exit 2). Verifies the verbs documented in INDEX.agent.md."""
    root, _ = fixture_tree
    from repo_index import cli
    out_dir = tmp_path / "out"
    try:
        cli.build_index(root, out_dir=out_dir,
                        write_html_artifact=False, write_dot_artifact=False)
    except NotImplementedError:
        pytest.skip("cli.build_index not implemented yet (stub phase)")

    base = ["--root", str(root), "--out", str(out_dir), "query"]

    # by-type data_table -> the csv(.gz) entries (a verb WITH an argument: the
    # previously-broken case where the arg was rejected as unrecognized).
    rc = cli.main(base + ["by-type", "data_table"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tables/cells.csv" in out

    # broken -> the dangling fixture symlink (a no-arg verb that used to be
    # swallowed as a path substring and silently matched nothing).
    rc = cli.main(base + ["broken"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "links/broken_link.csv" in out

    # find <substr> -> path substring via the explicit verb.
    rc = cli.main(base + ["find", "module"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "code/module.py" in out

    # Back-compat: a bare `query <substr>` still does a path substring search.
    rc = cli.main(base + ["module"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "code/module.py" in out

    # has-obs <col> reaches query.main (no h5ad obs column in the no-h5py tree is
    # fine — the point is the verb+arg routes without an argparse exit-2 error).
    rc = cli.main(base + ["has-obs", "leiden_cosine_2.0"])
    assert rc == 0


def test_selfheal_check_ok_then_drift(fixture_tree, loaded_config, tmp_path):
    """selfheal.check returns 0 on an unchanged tree, 1 after a real change."""
    root, _ = fixture_tree
    from repo_index import manifest as manifest_mod
    from repo_index import selfheal, walker

    try:
        entries = list(walker.walk(root, loaded_config))
        doc = manifest_mod.build_manifest(root, entries, loaded_config, tool_version="0.1.0")
    except NotImplementedError:
        pytest.skip("walker/manifest not implemented yet (stub phase)")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    index_path = out_dir / "INDEX.json"
    index_path.write_text(json.dumps(doc), encoding="utf-8")

    try:
        rc_ok = selfheal.check(root, index_path, loaded_config)
    except NotImplementedError:
        pytest.skip("selfheal.check not implemented yet (stub phase)")
    assert rc_ok == 0, "check should report OK on an unchanged tree"

    # Introduce a real change: add a new indexed file -> digest drifts.
    (root / "tables" / "new_table.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    rc_drift = selfheal.check(root, index_path, loaded_config)
    assert rc_drift == 1, "check should detect drift after adding a file"


def test_diff_entries_classifies_changes():
    """diff_entries returns (added, removed, changed) by digest-relevant triple."""
    from repo_index import selfheal
    committed = [
        {"path": "a", "size_bytes": 1, "extractor": "code", "meta": {}},
        {"path": "b", "size_bytes": 2, "extractor": "doc", "meta": {"h1_title": "x"}},
        {"path": "c", "size_bytes": 3, "extractor": "generic", "meta": {}},
    ]
    current = [
        {"path": "a", "size_bytes": 1, "extractor": "code", "meta": {}},      # same
        {"path": "b", "size_bytes": 99, "extractor": "doc", "meta": {"h1_title": "x"}},  # changed (size)
        {"path": "d", "size_bytes": 4, "extractor": "generic", "meta": {}},   # added
    ]
    try:
        added, removed, changed = selfheal.diff_entries(committed, current)
    except NotImplementedError:
        pytest.skip("selfheal.diff_entries not implemented yet (stub phase)")
    assert set(added) == {"d"}
    assert set(removed) == {"c"}
    assert set(changed) == {"b"}


def test_diff_health_detects_symlink_flip():
    """diff_health surfaces a symlink that flips broken/healthy — a health signal
    the content_digest (path,size,extractor,meta) is blind to."""
    from repo_index import selfheal
    committed = [
        {"path": "links/x", "is_symlink": True, "symlink_ok": True},
        {"path": "links/y", "is_symlink": True, "symlink_ok": False},
    ]
    current = [
        {"path": "links/x", "is_symlink": True, "symlink_ok": False},  # now broken
        {"path": "links/y", "is_symlink": True, "symlink_ok": True},   # now repaired
    ]
    newly_broken, newly_repaired = selfheal.diff_health(committed, current)
    assert newly_broken == ["links/x"]
    assert newly_repaired == ["links/y"]


def test_selfheal_check_detects_external_symlink_break(tmp_path, loaded_config):
    """A file symlink to an EXTERNAL target that breaks leaves the digest
    unchanged (same lstat size, generic/{} meta) — but check must still report
    DRIFT via the broken-symlink set comparison."""
    from repo_index import manifest as manifest_mod
    from repo_index import selfheal, walker

    root = tmp_path / "tree"
    (root / "links").mkdir(parents=True)
    target = tmp_path / "external_target.csv"
    target.write_text("a,b\n1,2\n", encoding="utf-8")
    link = root / "links" / "ext.csv"
    link.symlink_to(target)  # absolute external target, resolves

    try:
        entries = list(walker.walk(root, loaded_config))
        doc = manifest_mod.build_manifest(root, entries, loaded_config, "0.1.0")
    except NotImplementedError:
        pytest.skip("walker/manifest not implemented yet (stub phase)")
    index_path = tmp_path / "INDEX.json"
    index_path.write_text(json.dumps(doc), encoding="utf-8")
    built_digest = doc["content_digest"]

    assert selfheal.check(root, index_path, loaded_config) == 0

    # Break the external target: digest stays identical, health flips.
    target.unlink()
    recomputed_digest, _ = selfheal.recompute(root, loaded_config)
    assert recomputed_digest == built_digest, "digest should be unchanged"
    assert selfheal.check(root, index_path, loaded_config) == 1, (
        "check must detect the symlink health regression despite identical digest"
    )
