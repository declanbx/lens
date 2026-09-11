"""Tests for the SVG figure-text extractor (spec:
docs/superpowers/specs/2026-08-10-repo-index-figure-text-design.md).

All fixtures are synthesised at runtime (CONTRACTS.md §9). The two SVG text
encodings under test are the two matplotlib actually emits:

  * ``svg.fonttype='none'`` -> real ``<text>`` elements.
  * ``svg.fonttype='path'`` (matplotlib's DEFAULT) -> glyph OUTLINES referenced
    as ``<use xlink:href="#DejaVuSans-43"/>``, where the id suffix is the
    character's HEX CODEPOINT. 41% of this repo's SVGs are this form, so an
    extractor that only reads ``<text>`` silently indexes nothing for them.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #

def _svg_text_mode(path: Path, *labels: str) -> None:
    """Write an SVG whose text is real ``<text>`` elements (fonttype='none')."""
    body = "".join(
        '<text x="10" y="%d" style="font: 10px DejaVu Sans">%s</text>' % (20 * i, t)
        for i, t in enumerate(labels, start=1)
    )
    path.write_text(
        '<?xml version="1.0"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        + body +
        "</svg>\n",
        encoding="utf-8",
    )


def _glyph_group(gid: int, s: str) -> str:
    """One matplotlib text-artist group: ``<g id="text_N">`` of glyph ``<use>``s."""
    uses = "".join(
        '<use xlink:href="#DejaVuSans-%x"/>' % ord(ch) for ch in s
    )
    return '<g id="text_%d"><g transform="translate(10 20)">%s</g></g>' % (gid, uses)


def _svg_glyph_mode(path: Path, *labels: str) -> None:
    """Write an SVG whose text is glyph outlines (matplotlib fonttype='path')."""
    groups = "".join(_glyph_group(i, t) for i, t in enumerate(labels, start=1))
    path.write_text(
        '<?xml version="1.0"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
        '<defs><path id="DejaVuSans-43" d="M 0 0 L 1 1"/></defs>'
        + groups +
        "</svg>\n",
        encoding="utf-8",
    )


class _Cfg:
    """Minimal stand-in for the resolved Config (only the fields read here)."""

    def __init__(self, **kw):
        self.figure_text_max_bytes = kw.get("figure_text_max_bytes", 20 * 1024 * 1024)
        self.index_figure_text = kw.get("index_figure_text", True)


@pytest.fixture
def extractor():
    from repo_index.extractors.image import FigureTextExtractor
    return FigureTextExtractor(config=_Cfg())


# --------------------------------------------------------------------------- #
# 1. <text>-encoded SVGs
# --------------------------------------------------------------------------- #

def test_extracts_tokens_from_text_elements(extractor, tmp_path):
    p = tmp_path / "fig.svg"
    _svg_text_mode(p, "CUX2 expression", "UMAP1")

    meta = extractor.extract(p)

    assert "cux2" in meta["figure_text"]
    assert "expression" in meta["figure_text"]
    assert "umap1" in meta["figure_text"]
    assert meta["figure_text_mode"] == "text"


# --------------------------------------------------------------------------- #
# 2. glyph-outline SVGs (the 41% a naive extractor misses entirely)
# --------------------------------------------------------------------------- #

def test_decodes_glyph_outline_text_from_use_codepoints(extractor, tmp_path):
    p = tmp_path / "fig.svg"
    _svg_glyph_mode(p, "CUX2")

    meta = extractor.extract(p)

    assert "cux2" in meta["figure_text"]
    assert meta["figure_text_mode"] == "glyph"


# --------------------------------------------------------------------------- #
# 3. adjacent text artists must not fuse
# --------------------------------------------------------------------------- #

def test_adjacent_glyph_groups_do_not_fuse_into_one_token(extractor, tmp_path):
    """Two separate <g id="text_N"> artists are two tokens, never one.

    Regression lock: concatenating every <use> in document order produced
    `EN-ETEN-MigRGEN-ITIPC` on the real corpus.
    """
    p = tmp_path / "fig.svg"
    _svg_glyph_mode(p, "EN", "IN")

    toks = extractor.extract(p)["figure_text"]

    assert "en" in toks
    assert "in" in toks
    assert "enin" not in toks


# --------------------------------------------------------------------------- #
# 4. the byte cap
# --------------------------------------------------------------------------- #

def test_oversize_file_is_capped_but_still_yields_trailing_text(tmp_path):
    """Above the cap we read a head + a TAIL, because matplotlib writes the axis
    text AFTER the plot data — a head-only read would index nothing useful."""
    from repo_index.extractors.image import FigureTextExtractor

    p = tmp_path / "big.svg"
    filler = '<path d="%s"/>' % ("M 0 0 L 1 1 " * 400)
    p.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text>headlabel</text>'
        + filler * 60 +
        "<text>taillabel</text></svg>",
        encoding="utf-8",
    )
    assert p.stat().st_size > 4096

    ex = FigureTextExtractor(config=_Cfg(figure_text_max_bytes=4096))
    meta = ex.extract(p)

    assert meta["figure_text_truncated"] is True
    assert "taillabel" in meta["figure_text"]


def test_file_under_cap_is_not_marked_truncated(extractor, tmp_path):
    p = tmp_path / "small.svg"
    _svg_text_mode(p, "LDLR")

    assert extractor.extract(p).get("figure_text_truncated") is None


# --------------------------------------------------------------------------- #
# 5. never raise (CONTRACTS.md §1: extractors never abort the walk)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name,payload",
    [
        ("empty.svg", b""),
        ("truncated.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><text>abc'),
        ("binary.svg", bytes(range(256)) * 8),
        ("noxml.svg", b"this is not xml at all"),
    ],
)
def test_malformed_svg_never_raises(tmp_path, name, payload):
    import repo_index.extractors  # noqa: F401  (registers the extractor)
    from repo_index.extractors.base import extract_meta

    p = tmp_path / name
    p.write_bytes(payload)

    extractor_name, meta, error = extract_meta(p)

    assert error is None, "extract_meta surfaced an error for %s: %s" % (name, error)
    assert isinstance(meta, dict)


def test_textless_svg_yields_no_figure_text_key(extractor, tmp_path):
    p = tmp_path / "plain.svg"
    p.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="1" cy="1" r="1"/></svg>',
        encoding="utf-8",
    )

    assert "figure_text" not in extractor.extract(p)


# --------------------------------------------------------------------------- #
# 6. tokenisation rules
# --------------------------------------------------------------------------- #

def test_tokens_are_deduped_lowercased_and_drop_bare_numbers(extractor, tmp_path):
    p = tmp_path / "fig.svg"
    _svg_text_mode(p, "CUX2 cux2 0.75 -12 EN-IT-UL-1")

    toks = extractor.extract(p)["figure_text"]

    assert toks.count("cux2") == 1
    assert "0.75" not in toks
    assert "-12" not in toks
    assert "en-it-ul-1" in toks


# --------------------------------------------------------------------------- #
# 7. the extractor is wired into the registry for .svg
# --------------------------------------------------------------------------- #

def test_svg_resolves_to_the_figure_text_extractor(tmp_path):
    import repo_index.extractors  # noqa: F401
    from repo_index.extractors.base import get_extractor

    assert get_extractor(tmp_path / "any.svg").name == "figure_text"


# --------------------------------------------------------------------------- #
# 8. config knobs (§6) — mirror the index_columns lever
# --------------------------------------------------------------------------- #

def test_config_defaults_enable_figure_text_with_a_20mb_cap(loaded_config):
    assert loaded_config.index_figure_text is True
    assert loaded_config.figure_text_max_bytes == 20 * 1024 * 1024


def test_cli_no_figure_text_flag_turns_the_toggle_off():
    from repo_index import cli

    args = cli._build_parser().parse_args(["--no-figure-text"])

    assert cli._overrides_from_args(args)["index_figure_text"] is False


def test_cli_default_leaves_figure_text_unset_so_prior_build_wins():
    """A plain read must not override a persisted --no-figure-text."""
    from repo_index import cli

    args = cli._build_parser().parse_args([])

    assert "index_figure_text" not in cli._overrides_from_args(args)


# --------------------------------------------------------------------------- #
# 9. central strip in walker.make_entry (belt-and-braces for the cache path)
# --------------------------------------------------------------------------- #

def _cfg_with(loaded_config, **kw):
    import dataclasses
    return dataclasses.replace(loaded_config, **kw)


def test_make_entry_keeps_figure_text_when_toggle_on(tmp_path, loaded_config):
    from repo_index.walker import make_entry

    _svg_text_mode(tmp_path / "fig.svg", "CUX2")
    cfg = _cfg_with(loaded_config, index_figure_text=True)

    entry = make_entry(tmp_path / "fig.svg", tmp_path, cfg)

    assert "cux2" in entry["meta"]["figure_text"]


def test_make_entry_strips_figure_text_from_a_cached_entry_when_toggle_off(
    tmp_path, loaded_config
):
    """The cache holds meta from a prior toggle-ON build; the strip is what
    guarantees a toggle-OFF index never carries figure text."""
    from repo_index.walker import make_entry

    p = tmp_path / "fig.svg"
    _svg_text_mode(p, "CUX2")
    on = make_entry(p, tmp_path, _cfg_with(loaded_config, index_figure_text=True))
    cache = {on["path"]: on}

    off = make_entry(
        p, tmp_path, _cfg_with(loaded_config, index_figure_text=False), cache=cache
    )

    assert "figure_text" not in off["meta"]
    assert "figure_text_mode" not in off["meta"]
    # The cached dict must not have been mutated in place.
    assert "figure_text" in on["meta"]


# --------------------------------------------------------------------------- #
# 10. the exclusion boundary — figure text must NOT reach the default haystack
# --------------------------------------------------------------------------- #

def _figure_manifest():
    return {
        "schema_version": "1.0",
        "entries": [
            {
                "path": "figures/umap.svg", "category": "figure", "ext": "svg",
                "size_bytes": 4096, "mtime_iso": "2026-01-01T00:00:00Z",
                "is_symlink": False, "symlink_target": None, "symlink_ok": None,
                "extractor": "figure_text", "tags": [],
                "meta": {
                    "figure_text": ["cux2", "umap1", "en-it-ul-1"],
                    "figure_text_mode": "glyph",
                },
            },
        ],
    }


@pytest.fixture
def figure_db(tmp_path):
    import sqlite3
    from repo_index import export_sqlite

    db = export_sqlite.write_sqlite(_figure_manifest(), tmp_path / "_repo_index")
    con = sqlite3.connect(str(db))
    yield con
    con.close()


def test_figure_text_lands_in_its_own_sqlite_column(figure_db):
    value = figure_db.execute(
        "SELECT figure_text FROM entries WHERE path=?", ("figures/umap.svg",)
    ).fetchone()[0]

    assert "cux2" in value
    assert "en-it-ul-1" in value


def test_figure_text_is_absent_from_the_meta_column(figure_db):
    """`db.rs` HAYSTACK scans COALESCE(e.meta,'') — figure text reaching the meta
    column would silently put it in every default search."""
    meta = figure_db.execute(
        "SELECT meta FROM entries WHERE path=?", ("figures/umap.svg",)
    ).fetchone()[0]

    assert "cux2" not in meta
    assert "figure_text" not in meta


def test_figure_text_is_absent_from_the_fts_haystack(figure_db):
    """`fts` is contentless, so probe it the only way it can be probed: MATCH.

    A path token must still match (proving the row IS indexed), while a
    figure-text token must not.
    """
    assert figure_db.execute(
        "SELECT COUNT(*) FROM fts WHERE fts MATCH ?", ("umap",)
    ).fetchone()[0] == 1, "sanity: the row is in the FTS index at all"

    assert figure_db.execute(
        "SELECT COUNT(*) FROM fts WHERE fts MATCH ?", ("cux2",)
    ).fetchone()[0] == 0


def test_non_figure_meta_still_round_trips_into_the_meta_column(tmp_path):
    """The pop must be surgical — ordinary meta keys are untouched."""
    import sqlite3
    from repo_index import export_sqlite

    manifest = _figure_manifest()
    manifest["entries"][0]["meta"]["width_px"] = 640
    db = export_sqlite.write_sqlite(manifest, tmp_path / "_repo_index")

    con = sqlite3.connect(str(db))
    try:
        meta = con.execute("SELECT meta FROM entries").fetchone()[0]
    finally:
        con.close()

    assert "width_px" in meta


def test_html_embed_drops_figure_text_so_the_foundation_haystack_is_unchanged():
    """The static HTML page has no checkbox, so carrying figure text would only
    change its default search semantics and inflate the WKWebView string graph."""
    from repo_index.render_html import _project_entry_for_html

    projected = _project_entry_for_html(_figure_manifest()["entries"][0])

    assert "figure_text" not in projected["meta"]
    assert "figure_text_mode" not in projected["meta"]
