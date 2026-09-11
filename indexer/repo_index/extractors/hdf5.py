"""repo_index.extractors.hdf5 — generic .h5 structural metadata (STUB).

Shallow top-level groups + datasets (depth <= 2), SHAPES ONLY, no data reads.
See CONTRACTS.md §4.2. h5py is OPTIONAL (guarded import); if absent extract()
returns {}.

extract() return-dict keys
--------------------------
    top_level_groups : list[str]            -- names of top-level groups
    datasets         : dict[str, list[int]] -- {name: [shape...]} for datasets at depth<=2
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .base import Extractor, register

try:  # optional enhancer
    import h5py  # type: ignore
except Exception:  # noqa: BLE001
    h5py = None  # type: ignore[assignment]


class Hdf5Extractor(Extractor):
    """Extractor for generic HDF5 ``.h5`` files (shallow, shapes only)."""

    name = "hdf5"
    extensions = ("h5",)

    def extract(self, path: Path) -> Dict[str, Any]:
        """Return the §4.2 hdf5 metadata dict; {} if h5py absent.

        Lists top-level group names and dataset shapes for datasets at depth
        <= 2 only; never reads dataset values.
        """
        if h5py is None:
            return {}

        top_level_groups: List[str] = []
        datasets: Dict[str, List[int]] = {}

        with h5py.File(str(path), "r") as f:
            # Depth 1: top-level members.
            for name in f.keys():
                item = f.get(name)
                if isinstance(item, h5py.Group):
                    top_level_groups.append(name)
                    # Depth 2: one level inside each top-level group.
                    for child in item.keys():
                        sub = item.get(child)
                        if isinstance(sub, h5py.Dataset):
                            datasets[f"{name}/{child}"] = [int(d) for d in sub.shape]
                elif isinstance(item, h5py.Dataset):
                    datasets[name] = [int(d) for d in item.shape]

        return {
            "top_level_groups": top_level_groups,
            "datasets": datasets,
        }


register(Hdf5Extractor)
