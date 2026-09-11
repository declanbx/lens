"""repo_index — a mature, self-healing index of a large scientific repository.

Walks the filesystem once and extracts CHEAP structural metadata per file type
(e.g. an .h5ad yields obs column names + n_obs x n_vars WITHOUT loading the
matrix), serving BOTH a human (searchable self-contained HTML map) and an LLM
agent (machine-readable JSON / JSONL + a terse agent map).

Stdlib-only core; every third-party library is an optional enhancer behind a
guarded try-import (see CONTRACTS.md). Public API is re-exported here.

Importing this package self-registers all extractors (via
``repo_index.extractors``).
"""

from __future__ import annotations

__version__ = "0.1.0"

# Importing the extractors subpackage triggers self-registration of every
# extractor (h5ad, hdf5, npy, tabular, structured, code, doc, generic).
from . import extractors as extractors  # noqa: F401  (side-effect import)
from .extractors.base import (  # noqa: F401
    Extractor,
    REGISTRY,
    register,
    get_extractor,
    extract_meta,
)

# config.load_config / DEFAULTS are part of the public API but config.py is a
# stub this phase; import lazily-tolerant so a bare import of repo_index never
# fails even before config is implemented. The names are still exported for
# implementers and downstream callers.
try:  # pragma: no cover - tolerant during the stub phase
    from .config import load_config, DEFAULTS  # noqa: F401
except Exception:  # noqa: BLE001
    load_config = None  # type: ignore[assignment]
    DEFAULTS = None  # type: ignore[assignment]

# build_index is the top-level orchestration entry (implemented in cli/manifest).
# Re-exported lazily-tolerant for the same reason.
try:  # pragma: no cover - tolerant during the stub phase
    from .cli import build_index  # noqa: F401
except Exception:  # noqa: BLE001
    build_index = None  # type: ignore[assignment]

__all__ = [
    "__version__",
    "Extractor",
    "REGISTRY",
    "register",
    "get_extractor",
    "extract_meta",
    "load_config",
    "DEFAULTS",
    "build_index",
    "extractors",
]
