"""Tests for the W3 in-place tree-update DELTA computation (Python side).

These cover the pure, stdlib-only entry-level diff that powers the no-reload
refresh: ``manifest.compute_entry_delta`` (added/changed/removed classification +
the HTML-embed meta projection), plus the two callers that wrap it —
``app.Api._compute_delta`` (always-open native app) and
``serve._refresh_with_delta`` (the localhost server's POST /refresh). The JS that
consumes the delta is exercised in the browser, not here; this file pins the
contract the page relies on: a delta whose added/changed entries are truncated
EXACTLY like the embedded ENTRIES, and a graceful degrade-to-None on any trouble.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from repo_index import manifest as manifest_mod
from repo_index.render_html import _HTML_HEAD, _project_entry_for_html


# --------------------------------------------------------------------------- #
# helpers — build minimal entry dicts and a path->entry "old cache"
# --------------------------------------------------------------------------- #

def _entry(path: str, **over: Any) -> Dict[str, Any]:
    """A minimal but schema-shaped entry; override any field via kwargs."""
    e: Dict[str, Any] = {
        "path": path,
        "ext": path.rsplit(".", 1)[-1] if "." in path else "",
        "category": "code",
        "size_bytes": 100,
        "mtime_iso": "2026-01-01T00:00:00+00:00",
        "extractor": "code_v1",
        "is_symlink": False,
        "symlink_target": None,
        "symlink_ok": None,
        "error": None,
        "meta": {},
    }
    e.update(over)
    return e


def _as_cache(entries: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Shape a list as load_prior_entries would: {path -> entry}."""
    return {e["path"]: e for e in entries}


# --------------------------------------------------------------------------- #
# compute_entry_delta — added / changed / removed classification
# --------------------------------------------------------------------------- #

def test_delta_added_changed_removed_basic():
    old = _as_cache([
        _entry("a.py"),
        _entry("b.py"),
        _entry("c.py"),
    ])
    new = [
        _entry("a.py"),                       # unchanged
        _entry("b.py", size_bytes=200),       # changed (size)
        _entry("d.py"),                       # added
        # c.py removed
    ]
    delta = manifest_mod.compute_entry_delta(old, new)
    assert [e["path"] for e in delta["added"]] == ["d.py"]
    assert [e["path"] for e in delta["changed"]] == ["b.py"]
    assert delta["removed"] == ["c.py"]
    # changed carries the NEW value, not the stale one.
    assert delta["changed"][0]["size_bytes"] == 200


def test_delta_each_diff_field_triggers_changed():
    """Every field in _DELTA_FIELDS makes an entry 'changed'."""
    base = _entry("x.py")
    variants = {
        "size_bytes": 999,
        "mtime_iso": "2026-02-02T00:00:00+00:00",
        "extractor": "code_v2",
        "meta": {"defs": ["f"]},
        "is_symlink": True,
        "symlink_target": "../y",
        "symlink_ok": False,
    }
    for field, val in variants.items():
        old = _as_cache([base])
        new = [_entry("x.py", **{field: val})]
        delta = manifest_mod.compute_entry_delta(old, new)
        assert [e["path"] for e in delta["changed"]] == ["x.py"], (
            "field %r should mark the entry changed" % field
        )
        assert not delta["added"] and not delta["removed"]


def test_delta_error_only_change_is_a_noop():
    """A difference in `error` ALONE must NOT mark an entry changed (transient
    diagnostics, not a structural change — keeps a re-stat from churning the tree)."""
    old = _as_cache([_entry("x.py", error=None)])
    new = [_entry("x.py", error="boom")]
    delta = manifest_mod.compute_entry_delta(old, new)
    assert delta == {"added": [], "changed": [], "removed": []}


def test_delta_identical_is_empty():
    same = [_entry("a.py"), _entry("b.py")]
    delta = manifest_mod.compute_entry_delta(_as_cache(same), [dict(e) for e in same])
    assert delta == {"added": [], "changed": [], "removed": []}


def test_delta_outputs_are_path_sorted():
    old = _as_cache([_entry("z.py"), _entry("a.py")])
    new = [
        _entry("z.py", size_bytes=1),   # changed
        _entry("a.py", size_bytes=1),   # changed
        _entry("m.py"),                 # added
        _entry("b.py"),                 # added
    ]
    delta = manifest_mod.compute_entry_delta(old, new)
    assert [e["path"] for e in delta["added"]] == ["b.py", "m.py"]
    assert [e["path"] for e in delta["changed"]] == ["a.py", "z.py"]


def test_delta_removed_is_paths_only_sorted():
    old = _as_cache([_entry("z.py"), _entry("a.py"), _entry("m.py")])
    delta = manifest_mod.compute_entry_delta(old, [])  # everything removed
    assert delta["removed"] == ["a.py", "m.py", "z.py"]
    assert delta["added"] == [] and delta["changed"] == []


def test_delta_empty_old_makes_everything_added():
    new = [_entry("a.py"), _entry("b.py")]
    delta = manifest_mod.compute_entry_delta({}, new)
    assert [e["path"] for e in delta["added"]] == ["a.py", "b.py"]
    assert delta["changed"] == [] and delta["removed"] == []


# --------------------------------------------------------------------------- #
# meta projection — added/changed entries truncated EXACTLY like the embed
# --------------------------------------------------------------------------- #

def test_delta_projects_added_meta_like_embed():
    """A wide `columns` array on an ADDED entry is truncated to the HTML head +
    an n_columns_total count — byte-for-byte what _project_entry_for_html does,
    so the delta stays consistent with the page's embedded ENTRIES."""
    wide = [f"col_{i}" for i in range(_HTML_HEAD + 12)]
    new_entry = _entry("t.csv", category="data_table", meta={"columns": wide})
    delta = manifest_mod.compute_entry_delta({}, [new_entry])
    got = delta["added"][0]
    expected = _project_entry_for_html(new_entry)
    assert got == expected
    assert got["meta"]["columns"] == wide[:_HTML_HEAD]
    assert got["meta"]["n_columns_total"] == len(wide)
    # the ORIGINAL entry must be untouched (projection never mutates input)
    assert len(new_entry["meta"]["columns"]) == len(wide)


def test_delta_projects_changed_meta_like_embed():
    wide_old = [f"c{i}" for i in range(3)]
    wide_new = [f"c{i}" for i in range(_HTML_HEAD + 5)]
    old = _as_cache([_entry("t.csv", category="data_table", meta={"columns": wide_old})])
    new = [_entry("t.csv", category="data_table", meta={"columns": wide_new})]
    delta = manifest_mod.compute_entry_delta(old, new)
    got = delta["changed"][0]
    assert got["meta"]["columns"] == wide_new[:_HTML_HEAD]
    assert got["meta"]["n_columns_total"] == len(wide_new)


def test_delta_short_meta_not_truncated():
    """obs_columns stays FULL in the page (not a truncate key); a short columns
    list also passes through untouched."""
    obs = [f"obs_{i}" for i in range(80)]
    new_entry = _entry("m.h5ad", category="data_matrix",
                       meta={"obs_columns": obs, "columns": ["x", "y"]})
    delta = manifest_mod.compute_entry_delta({}, [new_entry])
    got = delta["added"][0]
    assert got["meta"]["obs_columns"] == obs           # untruncated
    assert got["meta"]["columns"] == ["x", "y"]
    assert "n_obs_columns_total" not in got["meta"]


# --------------------------------------------------------------------------- #
# robustness — bad inputs degrade, never raise
# --------------------------------------------------------------------------- #

def test_delta_ignores_entries_without_string_path():
    new = [_entry("a.py"), {"no_path": 1}, {"path": 123}, None]  # type: ignore[list-item]
    delta = manifest_mod.compute_entry_delta({}, new)  # type: ignore[arg-type]
    assert [e["path"] for e in delta["added"]] == ["a.py"]


def test_delta_non_dict_old_treated_as_empty():
    delta = manifest_mod.compute_entry_delta(None, [_entry("a.py")])  # type: ignore[arg-type]
    assert [e["path"] for e in delta["added"]] == ["a.py"]


# --------------------------------------------------------------------------- #
# app.Api._compute_delta — unchanged → empty; missing prior → None
# --------------------------------------------------------------------------- #

def test_api_compute_delta_unchanged_is_empty():
    import repo_index.app as app_mod
    out = app_mod.Api._compute_delta(manifest_mod, {"a.py": _entry("a.py")},
                                     {"entries": [_entry("a.py")]}, unchanged=True)
    assert out == {"added": [], "changed": [], "removed": []}


def test_api_compute_delta_changed_diffs():
    import repo_index.app as app_mod
    prior = {"a.py": _entry("a.py")}
    manifest = {"entries": [_entry("a.py", size_bytes=500), _entry("b.py")]}
    out = app_mod.Api._compute_delta(manifest_mod, prior, manifest, unchanged=False)
    assert [e["path"] for e in out["changed"]] == ["a.py"]
    assert [e["path"] for e in out["added"]] == ["b.py"]


def test_api_compute_delta_missing_prior_is_none():
    """If the prior entries couldn't be captured, the delta is None so the page
    falls back to a full reload rather than a wrong partial update."""
    import repo_index.app as app_mod
    out = app_mod.Api._compute_delta(manifest_mod, None,
                                     {"entries": [_entry("a.py")]}, unchanged=False)
    assert out is None


def test_api_compute_delta_bad_manifest_is_none():
    import repo_index.app as app_mod
    out = app_mod.Api._compute_delta(manifest_mod, {"a.py": _entry("a.py")},
                                     {"entries": "not a list"}, unchanged=False)
    assert out is None


def _js_reachable(api: object) -> set:
    """Replicate pywebview's bridge enumeration: skip ``_``-prefixed names, record
    bound methods, recurse into public non-callable attrs with ``__module__``. Same
    rule as test_app_finder's copy — inlined here so this module is independent."""
    import inspect

    seen: list = []

    def walk(obj, base="", out=None):
        if out is None:
            out = {}
        if id(obj) in seen:
            return out
        seen.append(id(obj))
        for name in dir(obj):
            try:
                full = f"{base}.{name}" if base else name
                if name.startswith("_"):
                    continue
                attr = getattr(obj, name)
                if inspect.ismethod(attr) or inspect.isfunction(attr):
                    out[full] = None
                elif inspect.isclass(attr) or (
                    not callable(attr) and hasattr(attr, "__module__")
                ):
                    walk(attr, full, out)
            except Exception:
                continue
        return out

    return set(walk(api).keys())


def test_api_compute_delta_is_private_not_on_js_surface():
    """_compute_delta is underscore-prefixed so the pywebview bridge never exposes
    it — the JS-reachable set must remain {refresh, reveal, meta}."""
    import repo_index.app as app_mod
    api = app_mod.Api(Path("/tmp/root"), Path("/tmp/root/_repo_index"))
    assert _js_reachable(api) == {"refresh", "reveal", "meta"}


# --------------------------------------------------------------------------- #
# serve._refresh_with_delta — end-to-end around a stub build_fn (real INDEX.json)
# --------------------------------------------------------------------------- #

def _write_index(out_dir: Path, entries: List[Dict[str, Any]]) -> None:
    """Write a real INDEX.json/.jsonl for ``entries`` via the manifest writers
    (so _peek_committed_digest + load_prior_entries read genuine files)."""
    class _Cfg:  # minimal config echo target
        raw = {"out_dirname": "_repo_index"}
    doc = manifest_mod.build_manifest(out_dir.parent, entries, _Cfg(), "test")
    manifest_mod.write_outputs(doc, out_dir)


def test_serve_refresh_with_delta_changed(tmp_path: Path):
    from repo_index import serve as serve_mod
    out_dir = tmp_path / "_repo_index"
    out_dir.mkdir()
    _write_index(out_dir, [_entry("a.py"), _entry("c.py")])

    # build_fn rewrites the index: a.py grows, c.py removed, d.py added.
    def build_fn() -> None:
        _write_index(out_dir, [_entry("a.py", size_bytes=777), _entry("d.py")])

    result = serve_mod._refresh_with_delta(out_dir, build_fn)
    assert result["ok"] is True
    assert result["unchanged"] is False
    d = result["delta"]
    assert d is not None
    assert [e["path"] for e in d["added"]] == ["d.py"]
    assert [e["path"] for e in d["changed"]] == ["a.py"]
    assert d["removed"] == ["c.py"]
    assert d["changed"][0]["size_bytes"] == 777
    # before/after digests are reported and DIFFER on a real change (the page uses
    # `before` to verify its in-memory baseline before applying the delta in place).
    assert isinstance(result["before"], str) and isinstance(result["after"], str)
    assert result["before"] != result["after"]


def test_serve_refresh_with_delta_unchanged_is_empty(tmp_path: Path):
    """A digest-stable rebuild (build_fn rewrites the SAME entries) reports
    unchanged=True and an empty delta — the page then just toasts, no re-render."""
    from repo_index import serve as serve_mod
    out_dir = tmp_path / "_repo_index"
    out_dir.mkdir()
    entries = [_entry("a.py"), _entry("b.py")]
    _write_index(out_dir, entries)

    def build_fn() -> None:
        _write_index(out_dir, [dict(e) for e in entries])  # identical structure

    result = serve_mod._refresh_with_delta(out_dir, build_fn)
    assert result["ok"] is True
    assert result["unchanged"] is True
    assert result["delta"] == {"added": [], "changed": [], "removed": []}
    # On a no-op rebuild before == after (and the page, when its baseline matches,
    # just toasts "up to date" without touching the tree).
    assert result["before"] == result["after"]
    assert isinstance(result["after"], str)


def test_serve_refresh_with_delta_propagates_build_error(tmp_path: Path):
    """If build_fn raises, _refresh_with_delta propagates (the handler maps it to
    HTTP 500, exactly as the pre-W3 server did)."""
    import pytest
    from repo_index import serve as serve_mod
    out_dir = tmp_path / "_repo_index"
    out_dir.mkdir()
    _write_index(out_dir, [_entry("a.py")])

    def build_fn() -> None:
        raise RuntimeError("walk blew up")

    with pytest.raises(RuntimeError, match="walk blew up"):
        serve_mod._refresh_with_delta(out_dir, build_fn)
