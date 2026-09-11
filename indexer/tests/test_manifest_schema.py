"""Manifest assembly, schema validation, and digest-stability tests.

Covers CONTRACTS.md §5.2 (INDEX.json shape), §7 (deterministic content_digest),
and validates the assembled manifest against ``tools/repo_index/index_schema.json``
(JSON Schema draft 2020-12). Uses ``jsonschema`` when installed; otherwise falls
back to a structural check that enforces the same required keys + enums.

These need ``config.load_config``, ``walker.walk``, and the ``manifest`` builders
implemented; each test skips cleanly while any of those is a stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:  # optional: prefer real JSON-Schema validation when available
    import jsonschema  # type: ignore
except Exception:  # noqa: BLE001
    jsonschema = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _build_manifest_or_skip(root: Path, config):
    """walk -> build_manifest; skip if any required piece is still a stub."""
    from repo_index import manifest, walker
    try:
        entries = list(walker.walk(root, config))
    except NotImplementedError:
        pytest.skip("walker.walk not implemented yet (stub phase)")
    try:
        return manifest.build_manifest(root, entries, config, tool_version="0.1.0")
    except NotImplementedError:
        pytest.skip("manifest.build_manifest not implemented yet (stub phase)")


def _structural_validate(doc: dict, schema: dict) -> None:
    """Minimal stdlib structural check mirroring index_schema.json.

    Enforces the top-level required keys, the summary required keys, and the
    per-entry required keys + category enum. Used only when ``jsonschema`` is
    absent so the suite still meaningfully validates on a bare stdlib install.
    """
    top_required = set(schema["required"])
    assert top_required.issubset(set(doc)), (
        "INDEX.json missing top-level keys: %s" % (top_required - set(doc))
    )
    assert doc["schema_version"] == "1.0"
    assert isinstance(doc["content_digest"], str) and len(doc["content_digest"]) == 64
    assert all(c in "0123456789abcdef" for c in doc["content_digest"])

    summary_required = set(schema["properties"]["summary"]["required"])
    assert summary_required.issubset(set(doc["summary"])), (
        "summary missing keys: %s" % (summary_required - set(doc["summary"]))
    )

    entry_def = schema["$defs"]["entry"]
    entry_required = set(entry_def["required"])
    allowed_entry_keys = set(entry_def["properties"])  # incl. optional "error"
    category_enum = set(entry_def["properties"]["category"]["enum"])
    for e in doc["entries"]:
        assert entry_required.issubset(set(e)), (
            "entry missing keys %s: %s" % (entry_required - set(e), e.get("path"))
        )
        assert set(e).issubset(allowed_entry_keys), (
            "entry has unexpected keys %s: %s"
            % (set(e) - allowed_entry_keys, e.get("path"))
        )
        assert e["category"] in category_enum, e["category"]


def _validate(doc: dict, schema: dict) -> None:
    """Validate ``doc`` against ``schema`` (jsonschema if present, else structural)."""
    if jsonschema is not None:
        jsonschema.validate(instance=doc, schema=schema)
    else:
        _structural_validate(doc, schema)


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #

def test_manifest_validates_against_schema(fixture_tree, loaded_config, index_schema):
    """The assembled INDEX.json validates against index_schema.json."""
    root, _ = fixture_tree
    doc = _build_manifest_or_skip(root, loaded_config)
    _validate(doc, index_schema)


def test_manifest_summary_counts(fixture_tree, loaded_config):
    """Summary tallies match the fixture tree (1 broken symlink, errors counted)."""
    root, _ = fixture_tree
    doc = _build_manifest_or_skip(root, loaded_config)
    summ = doc["summary"]
    assert summ["total_files"] == len(doc["entries"])
    # Exactly one DANGLING link in the fixture tree.
    assert summ["n_broken_symlinks"] == 1
    # good_link + broken_link + link_dir = 3 symlinks total.
    assert summ["n_symlinks"] == 3
    assert summ["n_broken_symlinks"] <= summ["n_symlinks"]
    # by_category / by_ext are non-empty O(n) tallies.
    assert sum(summ["by_category"].values()) == summ["total_files"]


def test_manifest_entries_sorted_by_path(fixture_tree, loaded_config):
    """entries are sorted by path (CONTRACTS §5.2)."""
    root, _ = fixture_tree
    doc = _build_manifest_or_skip(root, loaded_config)
    paths = [e["path"] for e in doc["entries"]]
    assert paths == sorted(paths)


def test_manifest_error_field_omitted_when_clean(fixture_tree, loaded_config):
    """Healthy entries omit the optional 'error' key (only present on failure)."""
    root, _ = fixture_tree
    doc = _build_manifest_or_skip(root, loaded_config)
    clean = [e for e in doc["entries"] if e["path"] == "code/module.py"]
    if clean:
        # module.py is valid python -> no extraction error -> no 'error' key.
        assert "error" not in clean[0] or clean[0].get("error") is None


# --------------------------------------------------------------------------- #
# content_digest stability (CONTRACTS §7)
# --------------------------------------------------------------------------- #

def test_content_digest_stable_across_two_runs(fixture_tree, loaded_config):
    """Two builds over the SAME unchanged tree yield an IDENTICAL content_digest."""
    root, _ = fixture_tree
    doc1 = _build_manifest_or_skip(root, loaded_config)
    doc2 = _build_manifest_or_skip(root, loaded_config)
    assert doc1["content_digest"] == doc2["content_digest"]
    # And the digest is the documented 64-hex sha256.
    d = doc1["content_digest"]
    assert len(d) == 64 and all(c in "0123456789abcdef" for c in d)


def test_content_digest_excludes_volatile_fields(fixture_tree, loaded_config):
    """compute_digest is invariant to mtime/error/git/generated_at (§7)."""
    root, _ = fixture_tree
    from repo_index import manifest, walker
    try:
        entries = list(walker.walk(root, loaded_config))
    except NotImplementedError:
        pytest.skip("walker.walk not implemented yet (stub phase)")
    try:
        base = manifest.compute_digest(entries)
    except NotImplementedError:
        pytest.skip("manifest.compute_digest not implemented yet (stub phase)")

    # Mutate ONLY volatile fields on a copy; digest must not change.
    import copy
    mutated = copy.deepcopy(entries)
    for e in mutated:
        e["mtime_iso"] = "1999-01-01T00:00:00Z"
        e["error"] = "InjectedError: should be excluded from the digest"
    assert manifest.compute_digest(mutated) == base


def test_content_digest_changes_on_real_change(fixture_tree, loaded_config):
    """Changing a digest-relevant field (size_bytes) DOES change the digest."""
    root, _ = fixture_tree
    from repo_index import manifest, walker
    try:
        entries = list(walker.walk(root, loaded_config))
    except NotImplementedError:
        pytest.skip("walker.walk not implemented yet (stub phase)")
    try:
        base = manifest.compute_digest(entries)
    except NotImplementedError:
        pytest.skip("manifest.compute_digest not implemented yet (stub phase)")
    import copy
    mutated = copy.deepcopy(entries)
    if not mutated:
        pytest.skip("empty fixture tree (unexpected)")
    mutated[0]["size_bytes"] = mutated[0]["size_bytes"] + 12345
    assert manifest.compute_digest(mutated) != base


def test_jsonl_matches_entries(fixture_tree, loaded_config, tmp_path):
    """write_outputs emits one JSON object per INDEX.jsonl line, path-sorted (§5.3)."""
    import json
    root, _ = fixture_tree
    doc = _build_manifest_or_skip(root, loaded_config)
    from repo_index import manifest
    out_dir = tmp_path / "out"
    try:
        paths = manifest.write_outputs(doc, out_dir)
    except NotImplementedError:
        pytest.skip("manifest.write_outputs not implemented yet (stub phase)")
    jsonl = Path(paths["index_jsonl"])
    assert jsonl.exists()
    lines = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == len(doc["entries"])
    parsed = [json.loads(ln) for ln in lines]
    # No wrapping array; each line is a standalone entry object, path-sorted.
    assert [p["path"] for p in parsed] == sorted(p["path"] for p in parsed)
    assert [p["path"] for p in parsed] == [e["path"] for e in doc["entries"]]
