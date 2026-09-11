"""repo_index.extractors.generic — fallback extractor (STUB; registers FALLBACK).

The catch-all for any file no other extractor handles: png, pdf, svg, log, pkl,
pt, joblib, xlsx, rds, cloupe, docx, pptx, gz (non-tabular), no-extension files,
and anything unmatched. Returns {} — category / size / mtime are captured by the
walker / manifest, NOT here. See CONTRACTS.md §4.8.

This module MUST be imported LAST by extractors/__init__.py because it registers
itself as the FALLBACK via register(..., fallback=True).

extract() return-dict keys
--------------------------
    {}    # always empty; structural facts live on the entry, not in meta
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .base import Extractor, register


class GenericExtractor(Extractor):
    """Fallback extractor: returns {} (no per-type metadata)."""

    name = "generic"
    # No extensions: this instance is reached only via FALLBACK in get_extractor.
    extensions = ()

    def extract(self, path: Path) -> Dict[str, Any]:
        """Return ``{}`` — the generic fallback carries no per-type metadata.

        Implementation is trivial: the fallback carries no per-type metadata;
        category / size / mtime / symlink facts are recorded on the entry by the
        walker / manifest, not here.
        """
        return {}


register(GenericExtractor, fallback=True)
