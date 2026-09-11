"""repo_index.extractors.code — source code metadata.

.py: stdlib ast, TOP-LEVEL defs/classes/imports + module docstring first line (no
execution). .R/.r: regex for functions/libraries + roxygen title (no execution).
.sh: shebang + first comment. All stdlib-only. See CONTRACTS.md §4.6.

extract() return-dict keys
--------------------------
    .py    -> { "docstring_first_line": str|None,
                "defs": list[str], "classes": list[str], "imports": list[str] }
    .R/.r  -> { "functions": list[str], "libraries": list[str],
                "roxygen_title": str|None }
    .sh    -> { "shebang": str|None, "first_comment": str|None }
"""

from __future__ import annotations

import ast  # stdlib; used for .py
import re  # stdlib; used for .R/.sh
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Extractor, register

# §6 cheap-read gate for source files. A multi-MB generated/minified .py or a
# machine-generated .R with a large embedded data block must NOT be fully
# materialized into memory (ast.parse builds a full AST on top of the source).
# Mirrors structured.py's json gate; reused as the default when no config is
# attached to the singleton.
_DEFAULT_CODE_MAX = 5 * 1024 * 1024  # 5 MB

# R: `name <- function(` or `name = function(`  (also <<- ; name may be quoted).
_R_FUNC = re.compile(
    r"""^\s*(?P<name>(?:`[^`]+`|[A-Za-z.][\w.]*|"[^"]+"|'[^']+'))\s*(?:<<-|<-|=)\s*function\b"""
)
# R: library(pkg) / require(pkg) / requireNamespace("pkg") — capture the package.
_R_LIB = re.compile(
    r"""\b(?:library|require|requireNamespace)\s*\(\s*(?P<pkg>[A-Za-z.][\w.]*|"[^"]+"|'[^']+')"""
)
# roxygen explicit @title line.
_ROX_TITLE = re.compile(r"""^\s*#'\s*@title\s+(?P<title>.+?)\s*$""")
# any roxygen line (#' ...), used as fallback first title.
_ROX_ANY = re.compile(r"""^\s*#'\s*(?P<text>.+?)\s*$""")


class CodeExtractor(Extractor):
    """Extractor for Python / R / shell source files (cheap, no execution)."""

    name = "code"
    extensions = ("py", "r", "sh")

    def __init__(self, config: Optional[Any] = None) -> None:
        """Hold an optional config carrying the source-file size threshold.

        Implementations read ``config.code_parse_max_bytes`` (default 5 MB, §6),
        then fall back to ``config.json_parse_max_bytes``, then the §6 default.
        ``apply_config`` (base.py) pushes the resolved config in before a walk.
        """
        self.config = config

    def _code_max(self) -> int:
        cfg = self.config
        val = getattr(cfg, "code_parse_max_bytes", None)
        if val is None:
            val = getattr(cfg, "json_parse_max_bytes", None)
        if val is None:
            return _DEFAULT_CODE_MAX
        try:
            return int(val)
        except (TypeError, ValueError):
            return _DEFAULT_CODE_MAX

    def _over_gate(self, path: Path) -> bool:
        """True if ``path`` exceeds the §6 code size gate (stat-only, no read)."""
        try:
            return path.stat().st_size > self._code_max()
        except OSError:
            return False

    def extract(self, path: Path) -> Dict[str, Any]:
        """Return the §4.6 code metadata dict, dispatched by extension.

        .py uses ast.parse (top-level defs/classes/imports + docstring first
        line; syntax error -> {}); .R/.r uses regex (function assignments,
        library()/require() args, roxygen @title); .sh reads the shebang and the
        first non-shebang comment. Files larger than the §6 code size gate are
        NOT read (returns ``{"row_count_reason": "size_gated"}``), mirroring the
        json/ipynb gates so a multi-MB generated source is never materialized.
        """
        lname = path.name.lower()
        if self._over_gate(path):
            return {"row_count_reason": "size_gated"}
        if lname.endswith(".py"):
            return self._extract_py(path)
        if lname.endswith(".r"):
            return self._extract_r(path)
        if lname.endswith(".sh"):
            return self._extract_sh(path)
        return {}

    # ------------------------------------------------------------------ #
    # python (ast, top-level only)
    # ------------------------------------------------------------------ #
    def _extract_py(self, path: Path) -> Dict[str, Any]:
        try:
            with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except OSError:
            return {}
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return {}

        docstring_first_line: Optional[str] = None
        doc = ast.get_docstring(tree, clean=True)
        if doc:
            for line in doc.splitlines():
                if line.strip():
                    docstring_first_line = line.strip()
                    break

        defs: List[str] = []
        classes: List[str] = []
        imports: List[str] = []
        seen_imports = set()

        for node in tree.body:  # TOP-LEVEL only
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs.append(node.name)
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".")[0]
                    if mod and mod not in seen_imports:
                        seen_imports.add(mod)
                        imports.append(mod)
            elif isinstance(node, ast.ImportFrom):
                # Relative imports (level>0) have module possibly None.
                mod = node.module.split(".")[0] if node.module else None
                if mod and mod not in seen_imports:
                    seen_imports.add(mod)
                    imports.append(mod)

        return {
            "docstring_first_line": docstring_first_line,
            "defs": defs,
            "classes": classes,
            "imports": imports,
        }

    # ------------------------------------------------------------------ #
    # R (regex; no execution)
    # ------------------------------------------------------------------ #
    def _extract_r(self, path: Path) -> Dict[str, Any]:
        functions: List[str] = []
        libraries: List[str] = []
        seen_fn = set()
        seen_lib = set()
        roxygen_title: Optional[str] = None
        first_rox_any: Optional[str] = None

        try:
            # Stream line-by-line (constant memory) rather than read().splitlines()
            # so the whole file is never held at once even under the size gate.
            with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                lines = (raw.rstrip("\r\n") for raw in fh)
                for line in lines:
                    self._scan_r_line(
                        line, functions, libraries, seen_fn, seen_lib
                    )
                    if roxygen_title is None:
                        mt = _ROX_TITLE.match(line)
                        if mt:
                            roxygen_title = mt.group("title").strip()
                    if first_rox_any is None:
                        ma = _ROX_ANY.match(line)
                        if ma:
                            text = ma.group("text").strip()
                            if text and not text.startswith("@"):
                                first_rox_any = text
        except OSError:
            return {}

        if roxygen_title is None:
            roxygen_title = first_rox_any

        return {
            "functions": functions,
            "libraries": libraries,
            "roxygen_title": roxygen_title,
        }

    def _scan_r_line(
        self,
        line: str,
        functions: List[str],
        libraries: List[str],
        seen_fn: set,
        seen_lib: set,
    ) -> None:
        """Scan one R source line for a function assignment and library() calls."""
        mf = _R_FUNC.match(line)
        if mf:
            name = self._strip_quotes(mf.group("name"))
            if name and name not in seen_fn:
                seen_fn.add(name)
                functions.append(name)
        for ml in _R_LIB.finditer(line):
            pkg = self._strip_quotes(ml.group("pkg"))
            if pkg and pkg not in seen_lib:
                seen_lib.add(pkg)
                libraries.append(pkg)

    @staticmethod
    def _strip_quotes(token: str) -> str:
        token = token.strip()
        if len(token) >= 2 and token[0] in "`\"'" and token[-1] == token[0]:
            return token[1:-1]
        return token

    # ------------------------------------------------------------------ #
    # shell
    # ------------------------------------------------------------------ #
    def _extract_sh(self, path: Path) -> Dict[str, Any]:
        shebang: Optional[str] = None
        first_comment: Optional[str] = None

        try:
            # Stream line-by-line: the shebang + first comment live at the top, so
            # we read at most a handful of lines and never materialize the file.
            with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                first = True
                for raw in fh:
                    line = raw.rstrip("\r\n")
                    if first:
                        first = False
                        if line.startswith("#!"):
                            shebang = line.strip()
                            continue
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("#") and not stripped.startswith("#!"):
                        text = stripped.lstrip("#").strip()
                        if text:
                            first_comment = text
                            break
                    else:
                        # First real (non-comment, non-blank) line ends the block.
                        break
        except OSError:
            return {}

        return {"shebang": shebang, "first_comment": first_comment}


register(CodeExtractor)
