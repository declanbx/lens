"""Regression tests for the app-view-sync fix (INDEX.html freshness + JS guard).

Background: the always-open app embeds the manifest at launch and updates its tree
via entry-level deltas computed by the backend against the ON-DISK prior index. If
another process advances the on-disk index between app refreshes (a `query`
freshen, a commit, a manual build), the app's in-memory baseline silently falls
behind and a later refresh only applies the LAST delta — leaving the displayed tree
missing files (and, worst case, toasting "up to date — no changes"). The fix:

1. The refresh contract now reports the on-disk `before`/`after` digests so the page
   can verify its in-memory baseline matches the delta's prior before applying it in
   place (covered for the serve path in test_delta.py; the JS guard is asserted here
   via the rendered page).
2. The reload fallback re-embeds INDEX.html, so INDEX.html must never lag
   INDEX.json. A `query` freshen rewrites json/jsonl with write_html_artifact=False,
   so the skip-write gate now verifies the HTML's OWN embedded digest (not mere
   existence) before skipping — regenerating a stale HTML. These tests pin both the
   `_peek_html_digest` reader and that gate behaviour, plus the tail digest marker.

These build a REAL index over the conftest fixture tree (stdlib-only; h5py-derived
files degrade gracefully when absent), so they exercise the genuine render + gate.
"""

from __future__ import annotations

from pathlib import Path

from repo_index import manifest as manifest_mod
from repo_index.cli import build_index


def _build(root: Path, out: Path, **kw) -> dict:
    """Build the index over ``root`` with the heavy optional artifacts off."""
    return build_index(
        root,
        out,
        None,
        write_dot_artifact=False,
        write_agent_map_artifact=False,
        quiet=True,
        **kw,
    )


def test_peek_html_digest_matches_committed(fixture_tree):
    """After a build, the digest embedded in INDEX.html's tail marker equals the
    committed INDEX.json digest (the tail marker is present and parseable)."""
    root, _ = fixture_tree
    out = root / "_repo_index"
    _build(root, out)

    html_digest = manifest_mod._peek_html_digest(out)
    json_digest = manifest_mod._peek_committed_digest(out)
    assert html_digest is not None, "tail <!-- digest: … --> marker not found in INDEX.html"
    assert html_digest == json_digest


def test_tail_marker_is_after_html_close(fixture_tree):
    """The digest marker lives at the FILE TAIL (after </html>), so _peek_html_digest
    can read it without scanning the multi-MB embedded blob."""
    root, _ = fixture_tree
    out = root / "_repo_index"
    _build(root, out)

    text = (out / "INDEX.html").read_text(encoding="utf-8")
    i_close = text.rfind("</html>")
    i_marker = text.rfind("<!-- digest:")
    assert i_close != -1 and i_marker != -1
    assert i_marker > i_close, "digest marker must be at the tail, after </html>"
    # And it is within the last few KB (what _peek_html_digest reads).
    assert len(text) - i_marker < 4096


def test_skipwrite_regenerates_stale_html(fixture_tree):
    """A `query`-style freshen (write_html_artifact=False) advances INDEX.json past
    INDEX.html. The next build that owns the HTML must NOT skip on existence alone —
    it must detect the digest lag and regenerate the page, so a reload is never fed
    a stale tree."""
    root, _ = fixture_tree
    out = root / "_repo_index"

    # 1) Full build: html and json agree at digest D1.
    _build(root, out)
    d1 = manifest_mod._peek_committed_digest(out)
    assert manifest_mod._peek_html_digest(out) == d1

    # 2) Change the tree, then freshen JSON ONLY (mimics `repo_index query`).
    (root / "code" / "newly_added.py").write_text("y = 2\n", encoding="utf-8")
    _build(root, out, write_html_artifact=False)
    d2 = manifest_mod._peek_committed_digest(out)
    assert d2 != d1, "adding a file should change the on-disk json digest"
    # The HTML now LAGS the json (still at D1) — this is the silent-staleness setup.
    assert manifest_mod._peek_html_digest(out) == d1

    # 3) A subsequent HTML-owning build sees json==new manifest digest (tree did not
    #    change since step 2) AND a stale html → must regenerate, not skip.
    _build(root, out, write_html_artifact=True)
    assert manifest_mod._peek_html_digest(out) == d2
    assert manifest_mod._peek_committed_digest(out) == d2


def test_no_op_build_still_skips_when_html_current(fixture_tree):
    """The skip-write optimisation is preserved: a genuine no-op rebuild (html ALREADY
    at the committed digest) leaves INDEX.html byte-identical (not re-rendered)."""
    root, _ = fixture_tree
    out = root / "_repo_index"
    _build(root, out)
    html = out / "INDEX.html"
    before_bytes = html.read_bytes()
    before_mtime = html.stat().st_mtime

    # Nothing changed on disk → digest stable AND html already current → gate skips.
    _build(root, out, write_html_artifact=True)
    assert html.read_bytes() == before_bytes
    # mtime unchanged confirms the file was not rewritten (atomic replace would bump it).
    assert html.stat().st_mtime == before_mtime


def test_rendered_page_carries_baseline_guard(fixture_tree):
    """Lock the JS-side guard into the rendered page: the in-memory baseline digest
    is tracked and the delta is only applied when r.before matches it (else reload).
    Guards against a regression that re-introduces the silent desync."""
    root, _ = fixture_tree
    out = root / "_repo_index"
    _build(root, out)
    text = (out / "INDEX.html").read_text(encoding="utf-8")

    assert "BASELINE_DIGEST" in text
    assert "DATA.content_digest" in text
    # the guard predicate + the post-apply baseline advance
    assert "r.before===BASELINE_DIGEST" in text
    assert "BASELINE_DIGEST=r.after" in text
