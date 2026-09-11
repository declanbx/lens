"""repo_index.extractors.image — figure-text extraction from SVG. Stdlib-only.

Makes the text *rendered into* a figure searchable: axis labels, legends, gene
symbols, cell-type names, stat annotations. See
``docs/superpowers/specs/2026-08-10-repo-index-figure-text-design.md``.

Two encodings, both required
----------------------------
Matplotlib writes SVG text one of two ways, chosen by ``rcParams["svg.fonttype"]``:

* ``'none'``  -> real ``<text>`` elements.
* ``'path'``  -> glyph OUTLINES, referenced as ``<use xlink:href="#DejaVuSans-43"/>``
  where the id suffix is the character's **hex codepoint**. This is matplotlib's
  DEFAULT, and 1,749 of this repo's 4,295 SVGs (41%) use it — an extractor that
  reads only ``<text>`` indexes nothing at all for them.

Glyph decoding must segment on ``<g id="text_N">`` (matplotlib's per-text-artist
boundary) or adjacent labels fuse: measured on the real corpus before the fix,
three separate legend entries came out as the single token ``EN-ETEN-MigRGEN-ITIPC``.

Byte policy (CONTRACTS.md §2 amendment)
---------------------------------------
§2 otherwise requires header/footer-only reads. Figure text is interleaved with —
and trails — the plot data, so it cannot be read from a header; this extractor
reads the whole file under an explicit, configurable cap
(``figure_text_max_bytes``, default 20 MB), exactly as ``csv_rowcount_max_bytes``
already licenses a bounded full read for row counting. Above the cap it reads a
head plus a **tail**, because matplotlib emits axis text after the plot data, so a
head-only read of a large figure would capture nothing useful. Measured: 16 of
4,295 files exceed the default cap.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import Extractor, register

__all__ = ["FigureTextExtractor"]

#: Default whole-file read cap (CONTRACTS.md §6). Above this, head+tail only.
_DEFAULT_FIGURE_TEXT_MAX = 20 * 1024 * 1024

#: Fraction of the cap spent on the head; the remainder is the tail. Text sits
#: at the END of a matplotlib SVG, so the tail gets the larger share.
_HEAD_FRACTION = 4

#: Tokens outside this length band are noise (single chars; base64/path blobs).
_TOK_MIN, _TOK_MAX = 2, 40

_RE_TEXT = re.compile(rb"<text\b[^>]*>(.*?)</text>", re.S)
_RE_TITLE = re.compile(rb"<title\b[^>]*>(.*?)</title>", re.S)
_RE_TAG = re.compile(rb"<[^>]+>")
#: matplotlib's per-text-artist boundary; splitting on it keeps labels separate.
_RE_TEXT_GROUP = re.compile(rb'<g id="text_\d+"')
#: A glyph reference: the id suffix is the character's hex codepoint. Matches
#: both `href=` and `xlink:href=`.
_RE_GLYPH = re.compile(rb'href="#[A-Za-z][\w\- ]*?-([0-9a-fA-F]{2,6})"')

_TOKSPLIT = re.compile(r"[^A-Za-z0-9_.:+\-]+")
#: A token that is only digits and numeric punctuation carries no search value.
_NUMERIC = re.compile(r"^[\d.\-+:]+$")


def _plain(chunk: bytes) -> str:
    """Strip markup from an element body and collapse whitespace."""
    return " ".join(_RE_TAG.sub(b" ", chunk).decode("utf-8", "replace").split())


def _decode_glyph_run(chunk: bytes) -> str:
    """Rebuild the string a run of glyph ``<use>`` references spells out."""
    chars: List[str] = []
    for hex_cp in _RE_GLYPH.findall(chunk):
        try:
            cp = int(hex_cp, 16)
        except ValueError:  # pragma: no cover - regex already constrains this
            continue
        # Control codepoints are never real label text; a space (0x20) is.
        if cp == 0x20 or cp >= 0x21:
            chars.append(chr(cp))
    return "".join(chars).strip()


def _tokenize(fragments: List[str]) -> List[str]:
    """Deduped, lowercased, order-preserving tokens; bare numbers dropped."""
    seen: set = set()
    out: List[str] = []
    for frag in fragments:
        for word in _TOKSPLIT.split(frag):
            if not (_TOK_MIN <= len(word) <= _TOK_MAX):
                continue
            if _NUMERIC.match(word):
                continue
            low = word.lower()
            if low not in seen:
                seen.add(low)
                out.append(low)
    return out


class FigureTextExtractor(Extractor):
    """Extract rendered text from SVG figures (both matplotlib encodings)."""

    name = "figure_text"
    extensions = ("svg",)

    def __init__(self, config: Optional[Any] = None) -> None:
        """Hold an optional config carrying the §6 byte cap / on-off toggle.

        ``config`` may be None at registration time (singleton); the walker sets
        it once per run via ``base.apply_config``.
        """
        self.config = config

    # ------------------------------------------------------------------ #
    # config helpers (defensive: config may be None or predate the fields)
    # ------------------------------------------------------------------ #
    def _max_bytes(self) -> int:
        return int(
            getattr(self.config, "figure_text_max_bytes", _DEFAULT_FIGURE_TEXT_MAX)
        )

    def _enabled(self) -> bool:
        return bool(getattr(self.config, "index_figure_text", True))

    # ------------------------------------------------------------------ #
    # read
    # ------------------------------------------------------------------ #
    def _read(self, path: Path) -> Tuple[bytes, bool]:
        """Return ``(data, truncated)`` honouring the byte cap.

        Under the cap: the whole file. Over it: a head plus a tail, joined by a
        newline so the two fragments can never splice into a bogus token.
        """
        cap = self._max_bytes()
        size = path.stat().st_size
        if size <= cap:
            return path.read_bytes(), False
        head_n = cap // _HEAD_FRACTION
        tail_n = cap - head_n
        with open(path, "rb") as fh:
            head = fh.read(head_n)
            fh.seek(max(head_n, size - tail_n))
            tail = fh.read(tail_n)
        return head + b"\n" + tail, True

    # ------------------------------------------------------------------ #
    # extract
    # ------------------------------------------------------------------ #
    def extract(self, path: Path) -> Dict[str, Any]:
        """Return ``{figure_text, figure_text_mode[, figure_text_truncated]}``.

        Returns ``{}`` when the toggle is off (which skips the read entirely —
        the read IS the cost this flag exists to avoid) or when the figure
        carries no text. Never raises for a malformed/binary/empty file: the
        regexes simply find nothing.
        """
        if not self._enabled():
            return {}

        data, truncated = self._read(path)
        fragments: List[str] = []
        modes: set = set()

        for match in _RE_TEXT.finditer(data):
            body = _plain(match.group(1))
            if body:
                fragments.append(body)
                modes.add("text")

        # Glyph outlines, one run per <g id="text_N"> artist so labels stay apart.
        segments = _RE_TEXT_GROUP.split(data)
        for segment in segments[1:]:
            run = _decode_glyph_run(segment)
            if run:
                fragments.append(run)
                modes.add("glyph")

        for match in _RE_TITLE.finditer(data):
            body = _plain(match.group(1))
            if body:
                fragments.append(body)
                modes.add("title")

        tokens = _tokenize(fragments)
        if not tokens:
            return {}

        meta: Dict[str, Any] = {
            "figure_text": tokens,
            "figure_text_mode": "+".join(sorted(modes)),
        }
        if truncated:
            meta["figure_text_truncated"] = True
        return meta


register(FigureTextExtractor)
