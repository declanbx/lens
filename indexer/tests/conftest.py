"""Shared pytest fixtures for repo_index tests.

Everything here BUILDS synthetic fixtures at runtime in a tmp dir (CONTRACTS.md
testing requirement). No fixture is committed to the repo; each test run
re-creates a tiny, deterministic tree so the tests are hermetic and run on a bare
stdlib CPython (third-party fixture builders like h5py are guarded /
``importorskip``).

The fixture tree deliberately includes EVERY structural hazard the walker /
extractors must survive (CONTRACTS.md §9):

    fixture_tree/
      data/
        small.npy              # numpy .npy (stdlib-writable header)
        bundle.npz             # .npz zip of two .npy members
        tiny.h5ad              # AnnData-shaped HDF5 (h5py REQUIRED; else skipped)
        tiny.h5               # generic HDF5 (h5py REQUIRED; else skipped)
      tables/
        cells.csv             # plain csv with a header
        cells.csv.gz          # gzipped csv (compound-ext path)
      conf/
        params.yaml           # yaml with a few top-level keys
      code/
        module.py             # python with a docstring/def/class/import
      docs/
        README.md             # markdown: h1 + paragraph + a path-like ref
      links/
        good_link.csv         # symlink -> ../tables/cells.csv (resolves)
        broken_link.csv       # symlink -> nonexistent target (DANGLING)
        link_dir              # symlink -> ../data (a DIRECTORY; must NOT descend)
      ._shadow               # AppleDouble shadow file (MUST be skipped)
      _vendor/
        vendored.py           # inside a pruned dir (MUST NOT be indexed)

``content_digest`` stability tests run two builds over the SAME tree and compare
the digests, so the tree must be fully deterministic (no random content).
"""

from __future__ import annotations

import gzip
import json
import os
import struct
import zipfile
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Low-level synthetic-file writers (stdlib only where possible)
# --------------------------------------------------------------------------- #

def _write_npy(path: Path, shape, descr: str = "<f4", fortran_order: bool = False,
               n_data_bytes: int = 0) -> None:
    """Write a *valid* .npy file header (v1.0) + ``n_data_bytes`` zero bytes.

    Stdlib-only: builds the magic/version/header-len/header-dict exactly per the
    NumPy .npy format spec so the header-only extractor can parse it WITHOUT
    numpy installed. The body is just zero padding (tests only read the header).
    """
    header = (
        "{'descr': '%s', 'fortran_order': %s, 'shape': %s, }"
        % (descr, "True" if fortran_order else "False", tuple(shape))
    )
    # Pad so that (magic(6)+ver(2)+hlen(2)+header) is a multiple of 64, header
    # ends with '\n' — matching numpy's own writer.
    base = 6 + 2 + 2 + len(header) + 1
    pad = (64 - base % 64) % 64
    header = header + (" " * pad) + "\n"
    with open(path, "wb") as f:
        f.write(b"\x93NUMPY")
        f.write(bytes([1, 0]))                       # version 1.0
        f.write(struct.pack("<H", len(header)))      # header length (uint16, LE)
        f.write(header.encode("latin1"))
        if n_data_bytes:
            f.write(b"\x00" * n_data_bytes)


def _npy_bytes(shape, descr: str = "<i8", fortran_order: bool = False) -> bytes:
    """Return the bytes of a header-only .npy (no data) for embedding in a zip."""
    header = (
        "{'descr': '%s', 'fortran_order': %s, 'shape': %s, }"
        % (descr, "True" if fortran_order else "False", tuple(shape))
    )
    base = 6 + 2 + 2 + len(header) + 1
    pad = (64 - base % 64) % 64
    header = header + (" " * pad) + "\n"
    out = b"\x93NUMPY" + bytes([1, 0]) + struct.pack("<H", len(header))
    out += header.encode("latin1")
    return out


def _write_npz(path: Path) -> None:
    """Write a .npz (zip of .npy members) with two named arrays, header-only."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("alpha.npy", _npy_bytes((3, 4), "<f8"))
        z.writestr("beta.npy", _npy_bytes((10,), "<i4"))


def _maybe_write_h5ad(path: Path) -> bool:
    """Write a tiny AnnData-shaped .h5ad via h5py (attrs match CONTRACTS §4.1).

    Returns True if written, False if h5py is unavailable. Does NOT write a real
    sparse matrix payload beyond what's needed for the structural attrs the
    extractor reads (X is a csr_matrix GROUP carrying the ``shape`` attr; obs/var
    carry the dataframe ``column-order`` + ``_index`` attrs).
    """
    try:
        import h5py  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return False

    n_obs, n_vars = 5, 3
    with h5py.File(path, "w") as f:
        # X as a sparse csr_matrix GROUP (the contract's sparse branch).
        X = f.create_group("X")
        X.attrs["encoding-type"] = "csr_matrix"
        X.attrs["encoding-version"] = "0.1.0"
        X.attrs["shape"] = np.array([n_obs, n_vars], dtype="int64")
        X.create_dataset("data", data=np.zeros(0, dtype="float32"))
        X.create_dataset("indices", data=np.zeros(0, dtype="int32"))
        X.create_dataset("indptr", data=np.zeros(n_obs + 1, dtype="int32"))

        # obs dataframe group: _index + ordered column-order, two columns.
        obs = f.create_group("obs")
        obs.attrs["encoding-type"] = "dataframe"
        obs.attrs["encoding-version"] = "0.2.0"
        obs.attrs["_index"] = "cell_id"
        obs.attrs["column-order"] = np.array(
            ["leiden", "cell_type"], dtype=h5py.string_dtype()
        )
        obs.create_dataset("cell_id", data=np.array(
            ["c%d" % i for i in range(n_obs)], dtype=h5py.string_dtype()))
        obs.create_dataset("leiden", data=np.zeros(n_obs, dtype="int32"))
        obs.create_dataset("cell_type", data=np.array(
            ["t"] * n_obs, dtype=h5py.string_dtype()))

        # var dataframe group.
        var = f.create_group("var")
        var.attrs["encoding-type"] = "dataframe"
        var.attrs["encoding-version"] = "0.2.0"
        var.attrs["_index"] = "gene_id"
        var.attrs["column-order"] = np.array(
            ["gene_name"], dtype=h5py.string_dtype())
        var.create_dataset("gene_id", data=np.array(
            ["g%d" % i for i in range(n_vars)], dtype=h5py.string_dtype()))
        var.create_dataset("gene_name", data=np.array(
            ["G%d" % i for i in range(n_vars)], dtype=h5py.string_dtype()))

        # obsm: a DATASET (X_umap, shape n_obs x 2) so its shape is reported.
        obsm = f.create_group("obsm")
        obsm.create_dataset("X_umap", data=np.zeros((n_obs, 2), dtype="float32"))
        # other annotation groups.
        f.create_group("varm")
        layers = f.create_group("layers")
        layers.create_dataset("counts", data=np.zeros((n_obs, n_vars), dtype="float32"))
        f.create_group("obsp")
        f.create_group("varp")
        uns = f.create_group("uns")
        uns.attrs["title"] = "tiny"
    return True


def _maybe_write_h5(path: Path) -> bool:
    """Write a tiny generic .h5 with top-level groups + a shallow dataset."""
    try:
        import h5py  # type: ignore
        import numpy as np  # type: ignore
    except Exception:
        return False
    with h5py.File(path, "w") as f:
        g = f.create_group("group_a")
        g.create_dataset("inner", data=np.zeros((2, 2), dtype="int32"))
        f.create_dataset("top_ds", data=np.zeros((4,), dtype="float32"))
    return True


# --------------------------------------------------------------------------- #
# The fixture-tree builder + the pytest fixture
# --------------------------------------------------------------------------- #

def build_fixture_tree(root: Path) -> dict:
    """Build the synthetic hazard tree under ``root``. Returns a dict of markers.

    The returned dict records which optional fixtures were written (e.g.
    ``h5ad`` may be skipped if h5py is unavailable) so tests can conditionally
    assert. The tree content is deterministic for digest-stability tests.
    """
    written = {}

    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "tables").mkdir(parents=True, exist_ok=True)
    (root / "conf").mkdir(parents=True, exist_ok=True)
    (root / "code").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "links").mkdir(parents=True, exist_ok=True)
    (root / "_vendor").mkdir(parents=True, exist_ok=True)

    # --- .npy / .npz (stdlib-writable headers) ---
    _write_npy(root / "data" / "small.npy", shape=(7, 5), descr="<f4")
    written["npy"] = True
    _write_npz(root / "data" / "bundle.npz")
    written["npz"] = True

    # --- .h5ad / .h5 (h5py required; recorded if skipped) ---
    written["h5ad"] = _maybe_write_h5ad(root / "data" / "tiny.h5ad")
    written["h5"] = _maybe_write_h5(root / "data" / "tiny.h5")

    # --- .csv + .csv.gz (compound ext) ---
    csv_text = "gene,score,label\nFOXG1,0.9,EN\nEMX1,0.8,EN\nGAD1,0.1,IN\n"
    (root / "tables" / "cells.csv").write_text(csv_text, encoding="utf-8")
    with gzip.open(root / "tables" / "cells.csv.gz", "wt", encoding="utf-8") as fh:
        fh.write(csv_text)
    written["csv"] = True
    written["csv.gz"] = True

    # --- .yaml ---
    (root / "conf" / "params.yaml").write_text(
        "name: experiment_x\nseed: 42\nmodel:\n  layers: 3\nthreshold: 0.5\n",
        encoding="utf-8",
    )
    written["yaml"] = True

    # --- .py ---
    (root / "code" / "module.py").write_text(
        '"""Module docstring first line.\n\nMore text.\n"""\n'
        "import os\n"
        "from pathlib import Path\n\n\n"
        "def my_func(a, b):\n    return a + b\n\n\n"
        "class MyClass:\n    pass\n",
        encoding="utf-8",
    )
    written["py"] = True

    # --- .md (h1 + paragraph + path-like outbound ref) ---
    (root / "docs" / "README.md").write_text(
        "# Project Title\n\n"
        "This is the first paragraph describing things.\n\n"
        "See [the data](data/small.npy) and `tables/cells.csv` for details.\n",
        encoding="utf-8",
    )
    written["md"] = True

    # --- symlinks: good (resolves), broken (dangling), and a DIR link ---
    good = root / "links" / "good_link.csv"
    broken = root / "links" / "broken_link.csv"
    dir_link = root / "links" / "link_dir"
    # Relative targets keep the tree relocatable.
    os.symlink(os.path.join("..", "tables", "cells.csv"), good)
    os.symlink(os.path.join("..", "tables", "does_not_exist.csv"), broken)
    os.symlink(os.path.join("..", "data"), dir_link)
    written["symlink_good"] = good.is_symlink()
    written["symlink_broken"] = broken.is_symlink()
    written["symlink_dir"] = dir_link.is_symlink()

    # --- AppleDouble shadow file (must be skipped by the walker) ---
    (root / "._shadow").write_text("appledouble junk", encoding="utf-8")
    written["appledouble"] = True

    # --- pruned _vendor dir with a .py inside (must NOT be indexed) ---
    (root / "_vendor" / "vendored.py").write_text(
        "x = 1\n", encoding="utf-8")
    written["vendor"] = True

    return written


@pytest.fixture
def fixture_tree(tmp_path: Path):
    """Build the synthetic hazard tree in a fresh tmp dir; yield (root, markers)."""
    root = tmp_path / "fixture_tree"
    root.mkdir()
    markers = build_fixture_tree(root)
    return root, markers


@pytest.fixture
def loaded_config():
    """Return a resolved Config from defaults (skips the suite if config is a stub)."""
    from repo_index.config import load_config
    try:
        return load_config()
    except NotImplementedError:
        pytest.skip("config.load_config not implemented yet (stub phase)")


# --------------------------------------------------------------------------- #
# Helpers exposed BOTH as plain functions (default import mode) AND as fixtures
# (so tests work under --import-mode=importlib, where `from conftest import ...`
# is not importable). Test modules use the fixtures.
# --------------------------------------------------------------------------- #

@pytest.fixture
def index_schema() -> dict:
    """Fixture wrapping :func:`load_index_schema` (importlib-mode safe)."""
    return load_index_schema()


@pytest.fixture
def real_file():
    """Fixture returning the :func:`real_or_skip` resolver (importlib-mode safe)."""
    return real_or_skip


def project_root() -> Path:
    """Absolute path to the repository root (4 levels up from this file:
    tools/repo_index/tests/conftest.py -> repo root)."""
    return Path(__file__).resolve().parents[3]


def real_or_skip(rel_paths):
    """Return the first existing real-file path among ``rel_paths`` or skip.

    ``rel_paths`` are relative to :func:`project_root`. Used by the self-test
    module so the suite still passes on a checkout that lacks the big artifacts.
    """
    rootp = project_root()
    for rel in rel_paths:
        cand = rootp / rel
        try:
            if cand.exists() or cand.is_symlink():
                return cand
        except OSError:
            continue
    pytest.skip("no real fixture found among: %s" % (rel_paths,))


def load_index_schema() -> dict:
    """Load tools/repo_index/index_schema.json as a dict."""
    schema_path = Path(__file__).resolve().parents[1] / "index_schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))
