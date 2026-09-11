"""Per-extractor contract tests on synthetic fixtures.

Each test asserts the extractor returns EXACTLY the CONTRACTS.md §4 keys (extra
keys forbidden; degraded reads may return ``{}``). Tests call ``extract()``
through the safe ``extract_meta()`` path the walker uses, so a stub
(NotImplementedError) surfaces as an ``error`` string rather than a crash, and we
``skip`` on it — the assertions become live once each owner implements their
extractor. The registry-resolution + compound-ext tests do NOT depend on any
extractor body and run for real now.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_index.extractors.base import (
    REGISTRY,
    ext_of,
    extract_meta,
    get_extractor,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _meta_or_skip(path: Path):
    """Run extract_meta; skip if the extractor is still a NotImplementedError stub.

    Returns (extractor_name, meta_dict). A genuine non-stub error fails the test
    (the extractor must never raise out, but it may legitimately return {} with no
    error for a degraded read).
    """
    name, meta, error = extract_meta(path)
    if error and "NotImplementedError" in error:
        pytest.skip("%s extractor not implemented yet (stub phase)" % name)
    assert error is None, (
        "extract_meta must not surface a non-stub error for a healthy fixture: "
        "%r" % error
    )
    return name, meta


def _assert_exact_keys(meta: dict, allowed: set, required: set | None = None):
    """Assert meta has no keys outside ``allowed``; if non-empty, has ``required``.

    A degraded extractor may legally return ``{}`` (CONTRACTS §4); we only check
    the required keys when the dict is non-empty.
    """
    assert set(meta).issubset(allowed), (
        "unexpected keys %s (allowed: %s)" % (set(meta) - allowed, allowed)
    )
    if meta and required is not None:
        assert required.issubset(set(meta)), (
            "missing required keys %s (got %s)" % (required - set(meta), set(meta))
        )


# --------------------------------------------------------------------------- #
# Registry / resolution (no extractor body needed — live now)
# --------------------------------------------------------------------------- #

def test_registry_populated_and_fallback_present():
    """Every shipped extractor self-registered; generic is the FALLBACK sentinel."""
    for ext in ("h5ad", "h5", "npy", "npz", "csv", "tsv", "csv.gz", "tsv.gz",
                "parquet", "yaml", "yml", "toml", "json", "ipynb", "py", "r",
                "sh", "md"):
        assert ext in REGISTRY, "extension %r not registered" % ext
    assert "" in REGISTRY, "generic FALLBACK sentinel ('') not registered"
    assert REGISTRY[""].name == "generic"


def test_compound_ext_precedence(tmp_path):
    """csv.gz resolves to the tabular extractor, NOT the generic gz/archive path."""
    p = tmp_path / "x.csv.gz"
    p.write_bytes(b"")
    assert ext_of(p) == "csv.gz"
    assert get_extractor(p).name == "tabular"
    # Bare .gz (non-tabular) must NOT be claimed by tabular -> falls to generic.
    g = tmp_path / "x.gz"
    g.write_bytes(b"")
    assert ext_of(g) == "gz"
    assert get_extractor(g).name == "generic"


def test_no_extension_resolves_generic(tmp_path):
    """A no-extension file resolves to the generic fallback with ext key ''."""
    p = tmp_path / "Snakefile"
    p.write_text("rule all:\n", encoding="utf-8")
    assert ext_of(p) == ""
    assert get_extractor(p).name == "generic"


def test_ext_of_leading_dot_dotfile_has_no_extension(tmp_path):
    """A leading-dot dotfile whose only dot is the leading one has ext '' (no
    real extension) — not a bogus 'gitignore'/'gitattributes' by_ext bucket."""
    for name in (".gitignore", ".gitattributes", ".editorconfig"):
        p = tmp_path / name
        p.write_text("x\n", encoding="utf-8")
        assert ext_of(p) == "", "%s should have no extension key" % name
        assert get_extractor(p).name == "generic"
    # A dotfile WITH a real extension still keys on that extension.
    hidden = tmp_path / ".hidden.txt"
    hidden.write_text("x\n", encoding="utf-8")
    assert ext_of(hidden) == "txt"
    # Compound still wins for a dotfile with multiple segments.
    dotgz = tmp_path / ".backup.csv.gz"
    dotgz.write_bytes(b"")
    assert ext_of(dotgz) == "csv.gz"


# --------------------------------------------------------------------------- #
# h5ad (§4.1) — h5py required
# --------------------------------------------------------------------------- #

def test_h5ad_extractor(fixture_tree):
    root, markers = fixture_tree
    if not markers.get("h5ad"):
        pytest.importorskip("h5py", reason="h5py needed to build/read the .h5ad fixture")
    name, meta = _meta_or_skip(root / "data" / "tiny.h5ad")
    assert name == "h5ad"
    allowed = {
        "n_obs", "n_vars", "X_encoding", "X_dtype", "obs_index", "obs_columns",
        "var_columns", "n_var_columns", "obsm", "varm", "layers", "obsp",
        "uns_keys", "has_raw",
    }
    required = allowed - {"X_dtype"}  # X_dtype may legitimately be null
    _assert_exact_keys(meta, allowed, required)
    if meta:
        assert meta["n_obs"] == 5 and meta["n_vars"] == 3
        assert meta["X_encoding"] == "csr_matrix"
        assert meta["obs_index"] == "cell_id"
        # obs_columns is the highest-value agent field: ordered + decoded to str.
        assert meta["obs_columns"] == ["leiden", "cell_type"]
        assert all(isinstance(c, str) for c in meta["obs_columns"])
        assert meta["var_columns"] == ["gene_name"]
        assert meta["n_var_columns"] == 1
        # obsm DATASET reports its shape [n_obs, k]; we built X_umap as n_obs x 2.
        assert "X_umap" in meta["obsm"]
        assert meta["obsm"]["X_umap"] == [5, 2]
        assert meta["layers"] == ["counts"]
        assert meta["has_raw"] is False


def test_h5ad_degrades_without_h5py(fixture_tree, monkeypatch):
    """If h5py is unavailable the h5ad extractor returns {} (never raises)."""
    import repo_index.extractors.h5ad as h5mod
    if h5mod.h5py is None:
        pytest.skip("h5py already absent; degraded path implicitly covered")
    root, markers = fixture_tree
    if not markers.get("h5ad"):
        pytest.skip("no .h5ad fixture (h5py was absent at build time)")
    monkeypatch.setattr(h5mod, "h5py", None)
    name, meta, error = extract_meta(root / "data" / "tiny.h5ad")
    if error and "NotImplementedError" in error:
        pytest.skip("h5ad extractor not implemented yet (stub phase)")
    assert name == "h5ad"
    assert error is None
    assert meta == {}


# --------------------------------------------------------------------------- #
# hdf5 (§4.2) — h5py required
# --------------------------------------------------------------------------- #

def test_hdf5_extractor(fixture_tree):
    root, markers = fixture_tree
    if not markers.get("h5"):
        pytest.importorskip("h5py", reason="h5py needed to build/read the .h5 fixture")
    name, meta = _meta_or_skip(root / "data" / "tiny.h5")
    assert name == "hdf5"
    allowed = {"top_level_groups", "datasets"}
    _assert_exact_keys(meta, allowed, allowed)
    if meta:
        assert "group_a" in meta["top_level_groups"]
        # top_ds is a top-level dataset, shape [4]; shapes only, no reads.
        assert meta["datasets"].get("top_ds") == [4]


# --------------------------------------------------------------------------- #
# npy / npz (§4.3) — stdlib (works WITHOUT numpy)
# --------------------------------------------------------------------------- #

def test_npy_extractor(fixture_tree):
    root, _ = fixture_tree
    name, meta = _meta_or_skip(root / "data" / "small.npy")
    assert name == "npy"
    allowed = {"shape", "dtype", "fortran_order"}
    _assert_exact_keys(meta, allowed, allowed)
    if meta:
        assert meta["shape"] == [7, 5]
        assert meta["fortran_order"] is False
        assert isinstance(meta["dtype"], str) and meta["dtype"]


def test_npz_extractor(fixture_tree):
    root, _ = fixture_tree
    name, meta = _meta_or_skip(root / "data" / "bundle.npz")
    assert name == "npy"
    allowed = {"n_members", "members"}
    _assert_exact_keys(meta, allowed, allowed)
    if meta:
        assert meta["n_members"] == 2
        assert set(meta["members"]) == {"alpha", "beta"}
        assert meta["members"]["alpha"]["shape"] == [3, 4]
        assert set(meta["members"]["alpha"]) == {"shape", "dtype"}


# --------------------------------------------------------------------------- #
# tabular (§4.4)
# --------------------------------------------------------------------------- #

def test_csv_extractor(fixture_tree):
    root, _ = fixture_tree
    name, meta = _meta_or_skip(root / "tables" / "cells.csv")
    assert name == "tabular"
    allowed = {"columns", "n_columns", "delimiter", "row_count",
               "row_count_exact", "row_count_reason"}
    required = {"columns", "n_columns", "delimiter", "row_count", "row_count_exact"}
    _assert_exact_keys(meta, allowed, required)
    if meta:
        assert meta["columns"] == ["gene", "score", "label"]
        assert meta["n_columns"] == 3
        assert meta["delimiter"] == ","
        # Small file: exact row count (3 data rows) within the size gate.
        assert meta["row_count"] == 3
        assert meta["row_count_exact"] is True


def test_csv_gz_extractor(fixture_tree):
    """Compound .csv.gz resolves to tabular and reads the header via gzip stream."""
    root, _ = fixture_tree
    name, meta = _meta_or_skip(root / "tables" / "cells.csv.gz")
    assert name == "tabular"
    allowed = {"columns", "n_columns", "delimiter", "row_count",
               "row_count_exact", "row_count_reason"}
    _assert_exact_keys(meta, allowed, None)
    if meta:
        assert meta["columns"] == ["gene", "score", "label"]
        assert meta["n_columns"] == 3


def test_csv_row_count_respects_embedded_quoted_newlines(tmp_path):
    """An exact row count must count CSV RECORDS, not physical newlines: a quoted
    field with an embedded newline is one record, so 2 logical rows over 3
    physical lines reports row_count=2 (not 3) while still claiming exact."""
    p = tmp_path / "annotated.csv"
    # 2 data records; the first has a quoted embedded newline (3 physical lines).
    p.write_text('a,b\n"x\nstill x",2\n3,4\n', encoding="utf-8")
    name, meta = _meta_or_skip(p)
    assert name == "tabular"
    if meta:
        assert meta["columns"] == ["a", "b"]
        assert meta["row_count"] == 2, "embedded-newline record over-counted"
        assert meta["row_count_exact"] is True


def test_csv_header_read_is_bounded(tmp_path):
    """The header read is bounded: a pathological single-line CSV with no early
    newline does not force an unbounded readline; columns parse from the cap."""
    from repo_index.extractors import tabular as _tab
    cap = _tab._HEADER_READ_CAP
    p = tmp_path / "wide.csv"
    # One huge header line (> cap) with the newline only at the very end.
    ncols = (cap // 4) + 50000  # comfortably exceeds the byte cap
    header = ",".join("c%d" % i for i in range(ncols))
    p.write_text(header + "\n1,2\n", encoding="utf-8")
    assert p.stat().st_size > cap
    name, meta = _meta_or_skip(p)
    assert name == "tabular"
    if meta:
        # Some columns parse (from the capped prefix), but never the full line.
        assert meta["n_columns"] >= 1
        assert meta["n_columns"] < ncols


def test_csv_size_gated_row_count(tmp_path):
    """Over the csv size gate, the header still reads but row_count is size_gated."""
    from repo_index.extractors import tabular as _tab
    ext = _tab.TabularExtractor()

    class _Cfg:
        csv_rowcount_max_bytes = 32      # tiny gate to force the gated branch
        csvgz_rowcount_max_bytes = 32

    ext.config = _Cfg()
    p = tmp_path / "big.csv"
    p.write_text("gene,score\n" + "A,1\n" * 100, encoding="utf-8")
    meta = ext.extract(p)
    assert meta["columns"] == ["gene", "score"]   # header ALWAYS read cheaply
    assert meta["row_count"] is None
    assert meta["row_count_exact"] is False
    assert meta["row_count_reason"] == "size_gated"


# --------------------------------------------------------------------------- #
# structured (§4.5)
# --------------------------------------------------------------------------- #

def test_yaml_extractor(fixture_tree):
    root, _ = fixture_tree
    name, meta = _meta_or_skip(root / "conf" / "params.yaml")
    assert name == "structured"
    allowed = {"top_level_keys", "n_keys"}
    _assert_exact_keys(meta, allowed, allowed)
    if meta:
        # Top-level keys only (model.layers is nested, must NOT appear).
        assert set(meta["top_level_keys"]) == {"name", "seed", "model", "threshold"}
        assert "layers" not in meta["top_level_keys"]
        assert meta["n_keys"] == 4


# --------------------------------------------------------------------------- #
# code (§4.6)
# --------------------------------------------------------------------------- #

def test_py_extractor(fixture_tree):
    root, _ = fixture_tree
    name, meta = _meta_or_skip(root / "code" / "module.py")
    assert name == "code"
    allowed = {"docstring_first_line", "defs", "classes", "imports"}
    _assert_exact_keys(meta, allowed, allowed)
    if meta:
        assert meta["docstring_first_line"] == "Module docstring first line."
        assert meta["defs"] == ["my_func"]
        assert meta["classes"] == ["MyClass"]
        # Top-level imports: module names from import / from-import.
        assert "os" in meta["imports"]
        assert "pathlib" in meta["imports"]


def test_py_syntax_error_returns_empty(tmp_path):
    """A .py with a syntax error degrades to {} (ast.parse fails -> empty)."""
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n", encoding="utf-8")
    name, meta, error = extract_meta(bad)
    if error and "NotImplementedError" in error:
        pytest.skip("code extractor not implemented yet (stub phase)")
    assert name == "code"
    assert error is None
    assert meta == {}


def test_py_size_gated_not_read(tmp_path):
    """A .py over the code size gate is NOT read/parsed (cheap-read, §6); it
    degrades to a size-only marker instead of materializing the whole file."""
    from repo_index.extractors import code as _code
    ext = _code.CodeExtractor()

    class _Cfg:
        code_parse_max_bytes = 64  # tiny gate to force the gated branch

    ext.config = _Cfg()
    big = tmp_path / "generated.py"
    big.write_text("x = 1\n" * 100, encoding="utf-8")  # > 64 bytes
    meta = ext.extract(big)
    assert meta == {"row_count_reason": "size_gated"}
    # A small .py under the gate still parses normally.
    small = tmp_path / "tiny.py"
    small.write_text("import os\ndef f(): pass\n", encoding="utf-8")
    meta2 = ext.extract(small)
    assert meta2.get("defs") == ["f"]
    assert "os" in meta2.get("imports", [])


def test_r_and_sh_stream_without_reading_whole_file(tmp_path):
    """.R / .sh extractors stream line-by-line and still extract the contracted
    keys (they no longer materialize the whole file via read().splitlines())."""
    from repo_index.extractors import code as _code
    ext = _code.CodeExtractor()
    r = tmp_path / "script.R"
    r.write_text(
        "#' @title My R Title\n"
        "library(Seurat)\n"
        "run <- function(x) x + 1\n",
        encoding="utf-8",
    )
    rm = ext.extract(r)
    assert rm["functions"] == ["run"]
    assert rm["libraries"] == ["Seurat"]
    assert rm["roxygen_title"] == "My R Title"
    sh = tmp_path / "run.sh"
    sh.write_text("#!/bin/bash\n# First comment line\necho hi\n", encoding="utf-8")
    sm = ext.extract(sh)
    assert sm["shebang"] == "#!/bin/bash"
    assert sm["first_comment"] == "First comment line"


# --------------------------------------------------------------------------- #
# doc (§4.7)
# --------------------------------------------------------------------------- #

def test_md_extractor(fixture_tree):
    root, _ = fixture_tree
    name, meta = _meta_or_skip(root / "docs" / "README.md")
    assert name == "doc"
    allowed = {"h1_title", "first_paragraph", "n_outbound_path_refs", "outbound_refs"}
    _assert_exact_keys(meta, allowed, allowed)
    if meta:
        assert meta["h1_title"] == "Project Title"
        assert meta["first_paragraph"].startswith("This is the first paragraph")
        # Path-like refs: the markdown link target + the inline code path.
        assert "data/small.npy" in meta["outbound_refs"]
        assert "tables/cells.csv" in meta["outbound_refs"]
        assert meta["n_outbound_path_refs"] == len(meta["outbound_refs"])
        # Dedup: no duplicate entries.
        assert len(meta["outbound_refs"]) == len(set(meta["outbound_refs"]))


def test_md_size_gated_not_read(tmp_path):
    """A .md over the doc size gate is NOT read (cheap-read, §6) — it degrades to
    a size-only marker instead of fully materializing the document."""
    from repo_index.extractors import doc as _doc
    ext = _doc.DocExtractor()

    class _Cfg:
        code_parse_max_bytes = 64  # tiny gate to force the gated branch

    ext.config = _Cfg()
    big = tmp_path / "generated.md"
    big.write_text("# title\n" + ("lorem " * 200), encoding="utf-8")  # > 64 bytes
    meta = ext.extract(big)
    assert meta == {"row_count_reason": "size_gated"}
    # A small .md under the gate still parses normally.
    small = tmp_path / "tiny.md"
    small.write_text("# T\n\nhello world\n", encoding="utf-8")
    meta2 = ext.extract(small)
    assert meta2.get("h1_title") == "T"


# --------------------------------------------------------------------------- #
# generic (§4.8)
# --------------------------------------------------------------------------- #

def test_generic_extractor_returns_empty(tmp_path):
    """A figure (.png) hits the generic fallback and returns {}."""
    p = tmp_path / "plot.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG magic only; never opened as image
    name, meta, error = extract_meta(p)
    if error and "NotImplementedError" in error:
        pytest.skip("generic extractor not implemented yet (stub phase)")
    assert name == "generic"
    assert error is None
    assert meta == {}
