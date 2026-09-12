"""repo_index.extractors — extractor subpackage.

Importing this subpackage imports every extractor submodule so that each one
self-registers (via ``register(...)`` at the bottom of its module). The import
ORDER matters only for the fallback: ``generic`` is imported LAST and registers
itself as the FALLBACK extractor. Compound-extension precedence (csv.gz before
gz) is handled by ``base.get_extractor`` regardless of import order.

All submodules must import cleanly with ZERO third-party libraries installed
(every optional lib is behind a guarded try-import); only CALLING a stubbed
extract() raises NotImplementedError during the stub phase.

Re-exports the registry API from base for convenience.
"""

from __future__ import annotations

from .base import (  # noqa: F401
    Extractor,
    REGISTRY,
    register,
    get_extractor,
    extract_meta,
    apply_config,
    ext_of,
)
from . import base as base  # noqa: F401  (use base.FALLBACK for the LIVE fallback)

# Self-registering submodule imports. Order: specific binary, then text, then
# the generic fallback LAST.
from . import h5ad as h5ad  # noqa: F401,E402
from . import hdf5 as hdf5  # noqa: F401,E402
from . import npy as npy  # noqa: F401,E402
from . import tabular as tabular  # noqa: F401,E402
from . import xlsx as xlsx  # noqa: F401,E402
from . import structured as structured  # noqa: F401,E402
from . import image as image  # noqa: F401,E402
from . import code as code  # noqa: F401,E402
from . import doc as doc  # noqa: F401,E402
from . import generic as generic  # noqa: F401,E402  (registers FALLBACK; keep last)


def __getattr__(attr_name: str):  # PEP 562: keep FALLBACK a LIVE re-export.
    """Resolve ``FALLBACK`` lazily so it never freezes to None at import time.

    ``register(..., fallback=True)`` in generic.py sets ``base.FALLBACK`` AFTER
    this package's import line for it runs, so a plain ``from .base import
    FALLBACK`` would capture the pre-registration ``None``. Routing the attribute
    through ``base`` returns the live value instead.
    """
    if attr_name == "FALLBACK":
        return base.FALLBACK
    raise AttributeError(f"module {__name__!r} has no attribute {attr_name!r}")

__all__ = [
    "Extractor",
    "REGISTRY",
    "FALLBACK",
    "register",
    "get_extractor",
    "extract_meta",
    "apply_config",
    "ext_of",
    "h5ad",
    "hdf5",
    "npy",
    "tabular",
    "xlsx",
    "structured",
    "image",
    "code",
    "doc",
    "generic",
]
