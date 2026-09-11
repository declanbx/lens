"""repo_index.extractors.h5ad — AnnData .h5ad structural metadata (STUB).

Reads ATTRS / KEYS / SHAPES ONLY via h5py — NEVER reads X / layers / obs / var
data arrays. See CONTRACTS.md §4.1. h5py is OPTIONAL (guarded import); if absent
extract() returns {} (degraded to size/type-only) and the walk continues.

extract() return-dict keys
--------------------------
    n_obs            : int        -- from X shape
    n_vars           : int        -- from X shape
    X_encoding       : str        -- "csr_matrix" | "csc_matrix" | "dense"
    X_dtype          : str|None   -- dtype string if cheaply available else None
    obs_index        : str|None   -- obs.attrs["_index"], decoded
    obs_columns      : list[str]  -- obs.attrs["column-order"], ordered, decoded
    var_columns      : list[str]  -- var.attrs["column-order"], ordered, decoded
    n_var_columns    : int
    obsm             : dict        -- {key: [n_obs, k] | None}  (dataset->shape, group->None)
    varm             : list[str]   -- list(varm.keys())
    layers           : list[str]   -- list(layers.keys())
    obsp             : list[str]   -- list(obsp.keys())
    uns_keys         : list[str]   -- list(uns.keys())
    has_raw          : bool        -- ".raw" top-level group present

Dimension rule: X GROUP -> sparse, use X.attrs["shape"]; X DATASET -> dense, use
X.shape. Decode numpy-bytes attrs to str. obs_columns and obsm keys are the
single highest-value agent query fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Extractor, register

try:  # optional enhancer
    import h5py  # type: ignore
except Exception:  # noqa: BLE001
    h5py = None  # type: ignore[assignment]


def _decode(value: Any) -> Any:
    """Decode a single attr value that may be numpy bytes/str to a Python str.

    Handles ``bytes`` -> ``str`` (utf-8, errors replaced) and leaves anything
    else (already-str, numpy str scalar) coerced to ``str``. Used for scalar
    attrs like ``_index`` and ``encoding-type``.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _decode_list(value: Any) -> List[str]:
    """Decode an attr that is an array/list of (possibly bytes) names to list[str].

    ``obs.attrs["column-order"]`` is typically a numpy object/bytes array; iterate
    and decode each element. A scalar (single column) is wrapped in a one-element
    list. Returns ``[]`` for a missing / empty / un-iterable value.
    """
    if value is None:
        return []
    # numpy arrays, lists, tuples are iterable; a bare scalar is not what we want
    # to iterate char-by-char, so treat bytes/str scalars as a single element.
    if isinstance(value, (bytes, str)):
        return [_decode(value)]
    try:
        return [_decode(v) for v in value]
    except TypeError:
        return [_decode(value)]


class H5adExtractor(Extractor):
    """Extractor for AnnData ``.h5ad`` files (h5py, attrs/keys/shapes only)."""

    name = "h5ad"
    extensions = ("h5ad",)

    def extract(self, path: Path) -> Dict[str, Any]:
        """Return the §4.1 h5ad metadata dict; {} if h5py absent.

        Reads obs.attrs["column-order"]/["_index"], var.attrs["column-order"],
        obsm/varm/layers/obsp/uns keys, and X shape/encoding via h5py — NEVER
        materializing any data array.
        """
        if h5py is None:
            return {}

        n_obs = 0
        n_vars = 0
        x_encoding = "dense"
        x_dtype: Optional[str] = None
        obs_index: Optional[str] = None
        obs_columns: List[str] = []
        var_columns: List[str] = []
        obsm: Dict[str, Any] = {}
        varm: List[str] = []
        layers: List[str] = []
        obsp: List[str] = []
        uns_keys: List[str] = []
        has_raw = False

        with h5py.File(str(path), "r") as f:
            # --- X: dimensions + encoding (NO data read) ---
            x = f.get("X")
            if x is not None:
                if isinstance(x, h5py.Group):
                    # sparse: encoding-type in {csr_matrix, csc_matrix}; shape attr
                    enc = x.attrs.get("encoding-type")
                    if enc is not None:
                        x_encoding = _decode(enc)
                    shape = x.attrs.get("shape")
                    if shape is not None:
                        dims = list(shape)
                        if len(dims) >= 2:
                            n_obs, n_vars = int(dims[0]), int(dims[1])
                    # dtype from the 'data' child dataset if cheaply present
                    data = x.get("data")
                    if data is not None and hasattr(data, "dtype"):
                        x_dtype = str(data.dtype)
                else:
                    # dense dataset: shape + dtype directly off the dataset header
                    x_encoding = "dense"
                    shape = getattr(x, "shape", None)
                    if shape is not None and len(shape) >= 2:
                        n_obs, n_vars = int(shape[0]), int(shape[1])
                    if hasattr(x, "dtype"):
                        x_dtype = str(x.dtype)

            # --- obs: index name + ordered column names (attrs only) ---
            obs = f.get("obs")
            if obs is not None and isinstance(obs, h5py.Group):
                idx = obs.attrs.get("_index")
                if idx is not None:
                    obs_index = _decode(idx)
                obs_columns = _decode_list(obs.attrs.get("column-order"))

            # --- var: ordered column names (attrs only) ---
            var = f.get("var")
            if var is not None and isinstance(var, h5py.Group):
                var_columns = _decode_list(var.attrs.get("column-order"))

            # --- obsm: dataset -> shape list; group -> None ---
            obsm_grp = f.get("obsm")
            if obsm_grp is not None and isinstance(obsm_grp, h5py.Group):
                for k in obsm_grp.keys():
                    item = obsm_grp.get(k)
                    if isinstance(item, h5py.Dataset):
                        obsm[k] = [int(d) for d in item.shape]
                    else:
                        obsm[k] = None

            # --- key-only groups ---
            for grp_name, target in (
                ("varm", "_varm"),
                ("layers", "_layers"),
                ("obsp", "_obsp"),
                ("uns", "_uns"),
            ):
                grp = f.get(grp_name)
                names: List[str] = []
                if grp is not None and isinstance(grp, h5py.Group):
                    names = list(grp.keys())
                if grp_name == "varm":
                    varm = names
                elif grp_name == "layers":
                    layers = names
                elif grp_name == "obsp":
                    obsp = names
                else:
                    uns_keys = names

            # --- has_raw: a top-level "raw" group present ---
            raw = f.get("raw")
            has_raw = raw is not None

        return {
            "n_obs": n_obs,
            "n_vars": n_vars,
            "X_encoding": x_encoding,
            "X_dtype": x_dtype,
            "obs_index": obs_index,
            "obs_columns": obs_columns,
            "var_columns": var_columns,
            "n_var_columns": len(var_columns),
            "obsm": obsm,
            "varm": varm,
            "layers": layers,
            "obsp": obsp,
            "uns_keys": uns_keys,
            "has_raw": bool(has_raw),
        }


register(H5adExtractor)
