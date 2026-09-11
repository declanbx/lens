"""repo_index.extractors.doc — Markdown metadata.

.md: H1 title + first paragraph + outbound path-like refs (markdown links and
inline `code` spans that look like repo paths). outbound_refs feed crosslinks.py.
Stdlib-only. See CONTRACTS.md §4.7.

extract() return-dict keys
--------------------------
    { "h1_title": str|None,            # first "# " heading text
      "first_paragraph": str|None,     # first non-empty, non-heading block, trimmed
      "n_outbound_path_refs": int,
      "outbound_refs": list[str] }     # dedup, order-preserving path-like tokens
"""

from __future__ import annotations

import re  # stdlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Extractor, register

# §6 cheap-read gate for docs. A machine-generated/huge .md must NOT be fully
# materialized (`fh.read()`); mirrors the code/json gates. Reused as the default
# when no config is attached to the singleton.
_DEFAULT_DOC_MAX = 5 * 1024 * 1024  # 5 MB

# First ATX H1 heading: exactly one leading '#'. Allow up to 3 leading spaces.
_H1 = re.compile(r"^\s{0,3}#\s+(?P<title>.+?)\s*#*\s*$")
# Any ATX heading line (to detect heading blocks when scanning for paragraphs).
_ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
# Markdown inline link: [text](target) — capture the target.
_MD_LINK = re.compile(r"\[[^\]]*\]\(\s*<?(?P<target>[^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# Inline code span: `...` — capture the inner text.
_CODE_SPAN = re.compile(r"`(?P<code>[^`]+)`")

# Known indexed data/code extensions a path-like token may end in (CONTRACTS §3).
_PATH_EXTS = (
    ".py", ".r", ".sh", ".cpp", ".c",
    ".h5ad", ".h5", ".npy", ".npz", ".loom",
    ".csv", ".tsv", ".csv.gz", ".tsv.gz", ".parquet", ".xlsx",
    ".yaml", ".yml", ".toml", ".json", ".ini",
    ".md", ".txt", ".rst",
    ".ipynb",
    ".png", ".pdf", ".svg", ".jpg", ".jpeg",
    ".pkl", ".pt", ".pth", ".joblib", ".rds", ".onnx",
    ".log", ".out", ".err",
    ".gz", ".tgz", ".zip", ".tar",
)
# Repo-anchored prefixes a path-like token may start with (CONTRACTS task spec).
_PATH_PREFIXES = (
    "outputs/",
    "assets/",
    "popv_",
    "research_questions/",
    "Cell Ranger",
)


class DocExtractor(Extractor):
    """Extractor for Markdown docs (title, first paragraph, outbound refs)."""

    name = "doc"
    extensions = ("md",)

    def __init__(self, config: Optional[Any] = None) -> None:
        """Hold an optional config carrying the doc size threshold.

        Implementations read ``config.code_parse_max_bytes`` (shared with the code
        extractor, default 5 MB, §6), then fall back to ``config.json_parse_max_bytes``,
        then the §6 default. ``apply_config`` pushes the resolved config in.
        """
        self.config = config

    def _doc_max(self) -> int:
        cfg = self.config
        val = getattr(cfg, "code_parse_max_bytes", None)
        if val is None:
            val = getattr(cfg, "json_parse_max_bytes", None)
        if val is None:
            return _DEFAULT_DOC_MAX
        try:
            return int(val)
        except (TypeError, ValueError):
            return _DEFAULT_DOC_MAX

    def extract(self, path: Path) -> Dict[str, Any]:
        """Return the §4.7 doc metadata dict.

        Scans for the first ``# `` H1 heading, the first non-heading paragraph,
        and path-like outbound references (markdown links + inline code spans
        that contain ``/`` and an indexed extension). Refs are deduped, order
        preserved, and feed the crosslink graph. A doc larger than the §6 size
        gate is NOT read (returns ``{"row_count_reason": "size_gated"}``) so a
        machine-generated/huge .md is never fully materialized.
        """
        try:
            if path.stat().st_size > self._doc_max():
                return {"row_count_reason": "size_gated"}
        except OSError:
            return {}
        try:
            with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return {}
        except Exception:  # noqa: BLE001
            return {}

        lines = text.splitlines()
        h1_title = self._first_h1(lines)
        first_paragraph = self._first_paragraph(lines)
        refs = self._outbound_refs(text)

        return {
            "h1_title": h1_title,
            "first_paragraph": first_paragraph,
            "n_outbound_path_refs": len(refs),
            "outbound_refs": refs,
        }

    # ------------------------------------------------------------------ #
    @staticmethod
    def _first_h1(lines: List[str]) -> Optional[str]:
        in_fence = False
        for line in lines:
            if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = _H1.match(line)
            if m:
                title = m.group("title").strip()
                if title:
                    return title
        return None

    @staticmethod
    def _first_paragraph(lines: List[str]) -> Optional[str]:
        in_fence = False
        block: List[str] = []
        for line in lines:
            stripped_lead = line.lstrip()
            if stripped_lead.startswith("```") or stripped_lead.startswith("~~~"):
                in_fence = not in_fence
                if block:
                    para = " ".join(b.strip() for b in block).strip()
                    if para:
                        return para
                    block = []
                continue
            if in_fence:
                continue
            if line.strip() == "":
                if block:
                    para = " ".join(b.strip() for b in block).strip()
                    if para:
                        return para
                    block = []
                continue
            if _ANY_HEADING.match(line):
                # A heading is not a paragraph; flush any block then skip.
                if block:
                    para = " ".join(b.strip() for b in block).strip()
                    if para:
                        return para
                    block = []
                continue
            block.append(line)
        if block:
            para = " ".join(b.strip() for b in block).strip()
            if para:
                return para
        return None

    # ------------------------------------------------------------------ #
    @classmethod
    def _outbound_refs(cls, text: str) -> List[str]:
        candidates: List[str] = []
        for m in _MD_LINK.finditer(text):
            candidates.append(m.group("target"))
        for m in _CODE_SPAN.finditer(text):
            # A code span may hold multiple whitespace-separated tokens.
            for tok in m.group("code").split():
                candidates.append(tok)

        refs: List[str] = []
        seen = set()
        for raw in candidates:
            tok = cls._clean_token(raw)
            if not tok or tok in seen:
                continue
            if cls._is_path_like(tok):
                seen.add(tok)
                refs.append(tok)
        return refs

    @staticmethod
    def _clean_token(tok: str) -> str:
        tok = tok.strip()
        # Drop in-page anchors / query strings — keep the path part.
        for sep in ("#", "?"):
            if sep in tok:
                tok = tok.split(sep, 1)[0]
        # Trim surrounding angle brackets / quotes already handled by regex; trim
        # trailing punctuation commonly adjacent in prose.
        tok = tok.strip().strip(",;")
        return tok

    @staticmethod
    def _is_path_like(tok: str) -> bool:
        # External URLs and mailto are not repo path refs.
        low = tok.lower()
        if "://" in low or low.startswith("mailto:"):
            return False
        if "/" not in tok:
            return False
        if low.endswith(_PATH_EXTS):
            return True
        for prefix in _PATH_PREFIXES:
            if tok.startswith(prefix):
                return True
        return False


register(DocExtractor)
