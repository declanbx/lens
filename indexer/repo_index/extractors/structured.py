"""repo_index.extractors.structured — yaml/toml/json/ipynb metadata.

.yaml/.yml: pyyaml if present else a cheap stdlib top-level-key regex scan.
.toml: tomllib (stdlib >= 3.11). .json: stdlib json, SIZE-GATED (parse only when
size <= json threshold, §6). .ipynb: stdlib json. See CONTRACTS.md §4.5.

extract() return-dict keys
--------------------------
    yaml/yml/toml -> { "top_level_keys": list[str], "n_keys": int }
    json (dict)   -> { "top_level_keys": list[str], "n_keys": int }
    json (list)   -> { "length": int }
    json (scalar) -> { "json_type": "str"|"int"|"float"|"bool"|"null" }
    json (>thr)   -> { "row_count_reason": "size_gated" }   # size-only, no parse
    ipynb         -> { "n_cells": int, "n_code": int, "n_markdown": int,
                       "kernel": str|None, "first_title": str|None }
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import Extractor, register

try:  # optional enhancer (yaml); stdlib fallback scan otherwise
    import yaml  # type: ignore
except Exception:  # noqa: BLE001
    yaml = None  # type: ignore[assignment]

try:  # stdlib >= 3.11
    import tomllib as _tomllib  # type: ignore
except Exception:  # noqa: BLE001
    _tomllib = None  # type: ignore[assignment]

_DEFAULT_JSON_MAX = 5 * 1024 * 1024  # 5 MB §6

# Top-level YAML key: an UNINDENTED line "key:" (allows letters/digits/._- and
# spaces inside the key) before the colon. Comments / list items / blanks ignored.
_YAML_TOP_KEY = re.compile(r"^(?P<key>[A-Za-z_][\w .\-]*?)\s*:(?:\s|$)")

# First markdown H1/H2 heading text (used for ipynb first_title).
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")


class StructuredExtractor(Extractor):
    """Extractor for structured config / notebook files (cheap key/cell scan)."""

    name = "structured"
    extensions = ("yaml", "yml", "toml", "json", "ipynb")

    def __init__(self, config: Optional[Any] = None) -> None:
        """Hold an optional config carrying the json parse size threshold.

        Implementations read ``config.json_parse_max_bytes`` (default 5 MB,
        §6), falling back to the default when config is None.
        """
        self.config = config

    def _json_max(self) -> int:
        return int(getattr(self.config, "json_parse_max_bytes", _DEFAULT_JSON_MAX))

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #
    def extract(self, path: Path) -> Dict[str, Any]:
        """Return the §4.5 structured metadata dict.

        Dispatches on extension: yaml/yml (pyyaml or stdlib key scan), toml
        (tomllib), json (size-gated stdlib parse), ipynb (stdlib parse for cell
        counts + kernel + first markdown title).
        """
        lname = path.name.lower()
        if lname.endswith(".ipynb"):
            return self._extract_ipynb(path)
        if lname.endswith(".json"):
            return self._extract_json(path)
        if lname.endswith(".toml"):
            return self._extract_toml(path)
        if lname.endswith(".yaml") or lname.endswith(".yml"):
            return self._extract_yaml(path)
        return {}

    # ------------------------------------------------------------------ #
    # yaml
    # ------------------------------------------------------------------ #
    def _extract_yaml(self, path: Path) -> Dict[str, Any]:
        if yaml is not None:
            try:
                with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                    data = yaml.safe_load(fh)
            except Exception:  # noqa: BLE001 - malformed yaml -> stdlib scan
                return self._yaml_stdlib_scan(path)
            if isinstance(data, dict):
                keys = [str(k) for k in data.keys()]
                return {"top_level_keys": keys, "n_keys": len(keys)}
            # Non-mapping document (list/scalar): no top-level keys.
            return {"top_level_keys": [], "n_keys": 0}
        return self._yaml_stdlib_scan(path)

    @staticmethod
    def _yaml_stdlib_scan(path: Path) -> Dict[str, Any]:
        keys: List[str] = []
        seen = set()
        try:
            with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    # Skip comments, blanks, document markers, and indented lines.
                    if not raw or raw[0] in " \t\n\r#":
                        continue
                    stripped = raw.rstrip("\n\r")
                    if stripped in ("---", "...") or stripped.startswith("- "):
                        continue
                    m = _YAML_TOP_KEY.match(stripped)
                    if m:
                        key = m.group("key").strip()
                        if key and key not in seen:
                            seen.add(key)
                            keys.append(key)
        except OSError:
            return {}
        except Exception:  # noqa: BLE001
            return {}
        return {"top_level_keys": keys, "n_keys": len(keys)}

    # ------------------------------------------------------------------ #
    # toml
    # ------------------------------------------------------------------ #
    def _extract_toml(self, path: Path) -> Dict[str, Any]:
        if _tomllib is None:
            return {}
        try:
            with open(path, "rb") as fh:
                data = _tomllib.load(fh)
        except Exception:  # noqa: BLE001 - malformed toml
            return {}
        if isinstance(data, dict):
            keys = [str(k) for k in data.keys()]
            return {"top_level_keys": keys, "n_keys": len(keys)}
        return {"top_level_keys": [], "n_keys": 0}

    # ------------------------------------------------------------------ #
    # json (size-gated)
    # ------------------------------------------------------------------ #
    def _extract_json(self, path: Path) -> Dict[str, Any]:
        try:
            size = path.stat().st_size
        except OSError:
            return {}
        if size > self._json_max():
            return {"row_count_reason": "size_gated"}
        try:
            with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001 - malformed json
            return {}
        if isinstance(data, dict):
            keys = [str(k) for k in data.keys()]
            return {"top_level_keys": keys, "n_keys": len(keys)}
        if isinstance(data, list):
            return {"length": len(data)}
        return {"json_type": self._json_scalar_type(data)}

    @staticmethod
    def _json_scalar_type(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):  # bool BEFORE int (bool is an int subclass)
            return "bool"
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        if isinstance(value, str):
            return "str"
        return "null"

    # ------------------------------------------------------------------ #
    # ipynb
    # ------------------------------------------------------------------ #
    def _extract_ipynb(self, path: Path) -> Dict[str, Any]:
        # Size-gate the parse: a notebook with megabytes of embedded base64 cell
        # OUTPUTS would otherwise be fully materialized by json.load (a cheap-read
        # violation, CONTRACTS.md §6). Over the json threshold -> size-only.
        try:
            size = path.stat().st_size
        except OSError:
            return {}
        if size > self._json_max():
            return {"row_count_reason": "size_gated"}
        try:
            with open(path, "rt", encoding="utf-8", errors="replace") as fh:
                nb = json.load(fh)
        except Exception:  # noqa: BLE001 - malformed notebook json
            return {}
        if not isinstance(nb, dict):
            return {}
        cells = nb.get("cells")
        if not isinstance(cells, list):
            cells = []
        n_cells = len(cells)
        n_code = 0
        n_markdown = 0
        first_title: Optional[str] = None
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            ctype = cell.get("cell_type")
            if ctype == "code":
                n_code += 1
            elif ctype == "markdown":
                n_markdown += 1
                if first_title is None:
                    first_title = self._first_md_title(cell.get("source"))
        kernel = self._kernel_name(nb)
        return {
            "n_cells": n_cells,
            "n_code": n_code,
            "n_markdown": n_markdown,
            "kernel": kernel,
            "first_title": first_title,
        }

    @staticmethod
    def _kernel_name(nb: Dict[str, Any]) -> Optional[str]:
        meta = nb.get("metadata")
        if not isinstance(meta, dict):
            return None
        ks = meta.get("kernelspec")
        if isinstance(ks, dict):
            for k in ("display_name", "name"):
                v = ks.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        li = meta.get("language_info")
        if isinstance(li, dict):
            v = li.get("name")
            if isinstance(v, str) and v.strip():
                return v
        return None

    @staticmethod
    def _first_md_title(source: Any) -> Optional[str]:
        # nbformat source may be a list of lines or a single string.
        if isinstance(source, list):
            lines = [s for s in source if isinstance(s, str)]
        elif isinstance(source, str):
            lines = source.splitlines()
        else:
            return None
        for line in lines:
            for sub in line.splitlines() or [line]:
                m = _MD_HEADING.match(sub)
                if m:
                    title = m.group("title").strip()
                    if title:
                        return title
        return None


register(StructuredExtractor)
