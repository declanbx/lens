"""repo_index.extractors.base — Extractor ABC + registry (FULLY IMPLEMENTED).

This is the contract surface every other extractor and the walker code against.
See CONTRACTS.md §2. Stdlib-only. Nothing here imports a third-party library.

Public API
----------
- ``Extractor``      : ABC; subclasses set ``extensions`` + ``name`` and implement ``extract``.
- ``REGISTRY``       : dict ext -> singleton Extractor instance.
- ``FALLBACK``       : the generic fallback instance (set via register(..., fallback=True)).
- ``register``       : instantiate + register a subclass (self-registration helper).
- ``get_extractor``  : resolve the Extractor for a path (compound-ext aware).
- ``extract_meta``   : the ONLY safe entry point — wraps extract() in try/except,
                       returns (name, meta_dict, error_or_None).
- ``ext_of``         : lowercased compound-aware extension key for a path.
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

__all__ = [
    "Extractor",
    "REGISTRY",
    "FALLBACK",
    "register",
    "get_extractor",
    "extract_meta",
    "apply_config",
    "ext_of",
]


class Extractor(ABC):
    """Base class for all per-file metadata extractors.

    Subclasses declare the lowercased extensions they handle (WITHOUT the leading
    dot; compound extensions like ``"csv.gz"`` are allowed) and implement
    :meth:`extract`. Extractors must be CHEAP: open headers / attrs / footers
    only, never load full data arrays / matrices / dataframes. Extractors must
    NOT raise for expected failure modes (missing optional lib, corrupt / locked
    file) — return ``{}`` or a partial dict; :func:`extract_meta` converts any
    escaped exception into an error string. An extractor self-registers by
    calling :func:`register` at module-import time (done at the bottom of each
    extractor module).
    """

    #: Lowercased extensions handled, no leading dot. Compound exts ("csv.gz")
    #: take precedence over their simple suffix ("gz") in get_extractor().
    extensions: Tuple[str, ...] = ()

    #: Stable, unique registry name (e.g. "h5ad", "tabular", "code"). Recorded
    #: as the ``extractor`` field on every entry.
    name: str = "base"

    @abstractmethod
    def extract(self, path: Path) -> Dict[str, Any]:
        """Return a metadata dict for ``path`` (keys per CONTRACTS.md §4).

        CHEAP reads only. May return ``{}`` when the optional dependency is
        absent or the file yields nothing. Let UNEXPECTED errors propagate;
        :func:`extract_meta` handles them and never lets them abort the walk.
        """
        raise NotImplementedError  # pragma: no cover - abstract


# --------------------------------------------------------------------------- #
# Registry state
# --------------------------------------------------------------------------- #

#: ext key (incl. compound like "csv.gz") -> singleton Extractor instance.
REGISTRY: Dict[str, "Extractor"] = {}

#: The generic fallback instance, used when no extension matches. Set by
#: generic.py via register(..., fallback=True).
FALLBACK: Optional["Extractor"] = None


def _compound_keys() -> List[str]:
    """Registered keys that contain a dot (compound exts), longest first.

    Used so that "csv.gz" is tested before the bare "gz". Recomputed cheaply on
    each call; the registry is tiny (tens of keys), so this is not a hot path.
    """
    return sorted((k for k in REGISTRY if "." in k), key=len, reverse=True)


def register(extractor_cls: Type["Extractor"], *, fallback: bool = False) -> "Extractor":
    """Instantiate ``extractor_cls`` once and register the singleton.

    The instance is registered in :data:`REGISTRY` under each of its
    ``extensions`` (last registration wins on collision; a :func:`warnings.warn`
    is emitted on collision). If ``fallback=True`` the module-level
    :data:`FALLBACK` is also set to this instance (used when no extension
    matches). Returns the instance. Called at the bottom of each extractor
    module so that importing the module self-registers it.
    """
    global FALLBACK
    instance = extractor_cls()
    for raw in instance.extensions:
        key = raw.lower().lstrip(".")
        if key in REGISTRY and REGISTRY[key] is not instance:
            warnings.warn(
                f"repo_index: extension {key!r} re-registered: "
                f"{type(REGISTRY[key]).__name__} -> {type(instance).__name__}",
                stacklevel=2,
            )
        REGISTRY[key] = instance
    if fallback:
        FALLBACK = instance
        REGISTRY.setdefault("", instance)  # sentinel for no-extension lookups
    return instance


def ext_of(path: Path) -> str:
    """Return the lowercased extension KEY for ``path`` (compound-aware).

    Honors compound suffixes that are present in :data:`REGISTRY` (e.g.
    "csv.gz", "tsv.gz") before the single suffix. Returns ``""`` for files with
    no extension. This is the key used both for extractor resolution and for the
    manifest ``by_ext`` summary so the two never disagree.
    """
    name = path.name
    # Strip a leading dot for dotfiles with no real extension (e.g. ".gitignore"
    # -> "gitignore" would be wrong; treat a name with no internal dot as "").
    parts = name.split(".")
    if len(parts) == 1:
        return ""  # no extension at all
    # A leading-dot dotfile whose ONLY dot is the leading one (".gitignore",
    # ".gitattributes", ".editorconfig") has no real extension: split gives
    # ["", "gitignore"], and keying that as "gitignore" would invent a bogus
    # by_ext bucket. Treat it as "" (no extension), per the docstring contract.
    if name.startswith(".") and len(parts) == 2:
        return ""
    # Compound: last two dotted segments, lowercased, e.g. "csv.gz".
    if len(parts) >= 3 or (len(parts) == 2 and not name.startswith(".")):
        compound = ".".join(parts[-2:]).lower()
        if compound in REGISTRY:
            return compound
    simple = parts[-1].lower()
    return simple


def get_extractor(path: Path) -> "Extractor":
    """Resolve the Extractor for ``path`` by extension (compound-ext aware).

    Tests the longest registered compound suffix first ("csv.gz", "tsv.gz"),
    then the simple extension, then :data:`FALLBACK` (generic). Never returns
    ``None`` once generic is registered. Raises :class:`RuntimeError` only if
    ``FALLBACK`` is unset (programmer error: the generic extractor module was
    never imported), which is NOT a per-file error.
    """
    name = path.name.lower()
    for ck in _compound_keys():
        if name.endswith("." + ck):
            return REGISTRY[ck]
    key = ext_of(path)
    if key and key in REGISTRY:
        return REGISTRY[key]
    if FALLBACK is None:
        raise RuntimeError(
            "repo_index: no FALLBACK extractor registered — "
            "import repo_index.extractors before resolving extractors."
        )
    return FALLBACK


def apply_config(config: Any) -> None:
    """Push the resolved ``config`` onto every registered extractor singleton that
    carries a ``config`` attribute (e.g. the tabular/structured extractors that
    read the §6 row-count / json-parse size thresholds off it).

    The frozen ``extract_meta(path)`` signature deliberately takes no config, so
    config-driven thresholds (and CLI ``--max-csv-bytes`` / ``--max-json-bytes``
    overrides, which the CLI folds into the config) are wired in here ONCE at the
    start of a walk rather than per-file. Extractors that do not use a config are
    left untouched. Never raises (a singleton without a settable ``config`` is
    simply skipped)."""
    seen: set = set()
    for extractor in REGISTRY.values():
        if id(extractor) in seen:
            continue
        seen.add(id(extractor))
        if hasattr(extractor, "config"):
            try:
                extractor.config = config
            except Exception:  # noqa: BLE001 - never let wiring abort a walk
                pass


def extract_meta(path: Path) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """Safely extract metadata for ``path``.

    Resolves :func:`get_extractor`, calls its ``extract()`` inside try/except,
    and returns ``(extractor_name, meta_dict, error_or_None)``:

    - On success: ``(extractor.name, meta_dict, None)``.
    - On ANY exception: ``(extractor.name, {}, "ExcType: message")`` — the walk
      is never interrupted. The error string is ``f"{type(e).__name__}: {e}"``.

    This is the ONLY path through which the walker invokes extraction; the
    walker must not call ``extract()`` directly.
    """
    extractor = get_extractor(path)
    try:
        meta = extractor.extract(path)
        if meta is None:  # defensive: treat a None return as empty
            meta = {}
        return extractor.name, meta, None
    except Exception as exc:  # noqa: BLE001 - intentional: never abort the walk
        return extractor.name, {}, f"{type(exc).__name__}: {exc}"
