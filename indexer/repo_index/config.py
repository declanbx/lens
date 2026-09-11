"""repo_index.config — configuration loading (STUB, but IMPORTABLE).

Loads defaults.yaml + optional overrides into a Config object holding: the prune
set, ext->category map, ontology tag regexes, and performance thresholds. See
CONTRACTS.md §3, §6, §9. Stdlib-only at import; pyyaml is optional (a stdlib
fallback parses the simple flat defaults.yaml shipped here).

This module is a STUB this phase: ``load_config`` raises NotImplementedError when
CALLED, but the module imports cleanly and ``DEFAULTS`` is a real importable
constant so downstream code and __init__ re-exports work.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# pyyaml is OPTIONAL. When absent, a stdlib fallback parses the flat
# defaults.yaml shipped here. NEVER let a missing lib raise at import time.
try:  # pragma: no cover - environment dependent
    import yaml  # type: ignore
except Exception:  # noqa: BLE001
    yaml = None  # type: ignore[assignment]

# tomllib is stdlib (>=3.11); used for .toml override files when given.
try:  # pragma: no cover - environment dependent
    import tomllib  # type: ignore
except Exception:  # noqa: BLE001
    tomllib = None  # type: ignore[assignment]

# Defaults mirrored from defaults.yaml for code that needs them before a config
# file is parsed. The authoritative source is defaults.yaml; load_config()
# merges that file (and overrides) over these. This constant is importable now.
DEFAULTS: Dict[str, Any] = {
    "schema_version": "1.0",
    "out_dirname": "_repo_index",
    "prune_dirs": [
        "_vendor", "__pycache__", ".pytest_cache", ".git", "site-packages",
        "node_modules", ".venv", "venv", ".mypy_cache", ".ipynb_checkpoints",
        ".ruff_cache", ".eggs", "build", "dist", ".repo_index", "_repo_index",
        ".cache",
    ],
    "skip_prefixes": ["._"],
    "csv_rowcount_max_bytes": 100 * 1024 * 1024,   # 100 MB raw
    "csvgz_rowcount_max_bytes": 25 * 1024 * 1024,  # 25 MB compressed
    "json_parse_max_bytes": 5 * 1024 * 1024,       # 5 MB
    "code_parse_max_bytes": 5 * 1024 * 1024,       # 5 MB code/doc cheap-read gate
    "racy_window_seconds": 2,                      # incremental "racy" dirty window
    "walk_threads": 4,                             # W6: parallel-list thread count
    "index_columns": True,                         # capture wide per-CSV/TSV/parquet `columns` lists (keep n_columns regardless)
    "index_figure_text": True,                     # extract rendered text from SVG figures
    "figure_text_max_bytes": 20 * 1024 * 1024,     # 20 MB whole-read cap; above it, head+tail
    "agent_map_top_n_h5ad": 25,
    "label_obs_substrings": ["leiden", "label", "type", "annotation"],
    "include_globs": [],
    "exclude_globs": [],
    "ext_to_category": {
        "py": "code", "r": "code", "sh": "code", "cpp": "code", "c": "code",
        "h5ad": "data_matrix", "h5": "data_matrix", "npy": "data_matrix",
        "npz": "data_matrix", "loom": "data_matrix",
        "csv": "data_table", "tsv": "data_table", "csv.gz": "data_table",
        "tsv.gz": "data_table", "parquet": "data_table", "xlsx": "data_table",
        "yaml": "config", "yml": "config", "toml": "config", "json": "config",
        "ini": "config",
        "md": "doc", "txt": "doc", "rst": "doc",
        "ipynb": "notebook",
        "png": "figure", "pdf": "figure_pdf", "svg": "figure", "jpg": "figure",
        "jpeg": "figure",
        "pkl": "model", "pt": "model", "pth": "model", "joblib": "model",
        "model": "model", "rds": "model", "onnx": "model",
        "log": "log", "out": "log", "err": "log",
        "gz": "archive", "tgz": "archive", "zip": "archive", "tar": "archive",
    },
    "ontology_tags": [
        # {"tag": "...", "pattern": "<regex over relative posix path>"}
    ],
}


@dataclass
class Config:
    """Resolved configuration (CONTRACTS.md §3/§6/§9).

    Attributes
    ----------
    prune_dirs : set[str]
        Directory basenames removed from os.walk dirnames before descent.
    skip_prefixes : tuple[str, ...]
        Basename prefixes to skip entirely (e.g. ``"._"``).
    out_dirname : str
        Default output dir basename under root (``.repo_index``).
    ext_to_category : dict[str, str]
        Lowercased ext (compound-aware) -> category string.
    ontology_tags : list[tuple[str, "re.Pattern"]]
        (tag, compiled-regex-over-relpath) pairs.
    csv_rowcount_max_bytes, csvgz_rowcount_max_bytes, json_parse_max_bytes : int
        Performance gate thresholds.
    racy_window_seconds : int
        Coarse-granularity "racy" dirty window for incremental cache reuse. A
        cached entry whose file mtime is within this many seconds of the prior
        index's own generation time is treated as DIRTY (re-extracted), closing
        the same-coarse-tick/same-size blind spot of low-resolution filesystem
        timestamps (exFAT 2s mtime granularity; git index-format "racy" fix;
        Mercurial dirstate-v2 MTIME_SECOND_AMBIGUOUS). See CONTRACTS.md §9.
    walk_threads : int
        W6 (Axis A): size of the thread pool that parallelises per-directory
        LISTING + child classification during the walk (the dominant I/O cost on
        exFAT). Extraction (``extract_meta`` -> h5py/pyarrow) is NOT thread-safe
        and always runs SERIALLY on the main thread regardless of this value.
        ``<= 1`` (or a tiny tree) uses the serial recursion unchanged. The default
        of 4 matched a ~2.23x directory-I/O speedup on this USB/exFAT volume;
        ``>= 8`` regressed (the bus saturates), so a small pool is intentional.
    agent_map_top_n_h5ad : int
        How many largest h5ad to list in INDEX.agent.md.
    label_obs_substrings : tuple[str, ...]
        obs-column name substrings treated as label-like in the agent map.
    include_globs : list[str]
        If non-empty, only files/symlinks whose relative POSIX path matches one of
        these fnmatch globs are indexed. Empty = include everything.
    exclude_globs : list[str]
        Files/symlinks (and whole directory subtrees) whose relative POSIX path
        matches one of these fnmatch globs are dropped before descent. Exclude
        wins over include.
    index_columns : bool
        When True (default), the wide per-CSV/TSV/parquet ``columns`` list is kept
        on each tabular entry's meta. When False, the walker drops that list (a
        FILE-SIZE / build-speed lever: it shrinks INDEX.json/.jsonl) while ALWAYS
        retaining the ``n_columns`` count and the tiny h5ad ``obs_columns`` /
        ``var_columns`` lists. The extractor is unaffected — the strip is central
        in walker.make_entry. Flipping this forces a full re-extract (cli.build_index)
        so the columns actually come back / get stripped. See CONTRACTS.md §4.4.
    index_figure_text : bool
        When True (default), SVG figures are read and the text rendered into them
        (axis labels, legends, gene symbols) is captured as ``meta["figure_text"]``.
        When False the extractor skips the read entirely — the read IS the cost this
        lever exists to avoid (measured: 16.7 s over 4,295 SVGs / 7.17 GB) — and the
        walker additionally strips the keys centrally so a cached entry from a prior
        toggle-ON build cannot leak through. Flipping this forces a full re-extract.
    figure_text_max_bytes : int
        Whole-file read cap for the SVG figure-text extractor (default 20 MB). Above
        it, a head plus a TAIL are read instead — matplotlib emits axis text after
        the plot data, so a head-only read of a large figure captures nothing useful.
        This is the CONTRACTS.md §2 "cheap read" escape hatch, on the same footing as
        ``csv_rowcount_max_bytes``.
    raw : dict
        The fully-merged raw config mapping (echoed into INDEX.json config_used).
    """

    prune_dirs: set = field(default_factory=set)
    skip_prefixes: Tuple[str, ...] = ("._",)
    out_dirname: str = "_repo_index"
    ext_to_category: Dict[str, str] = field(default_factory=dict)
    ontology_tags: List[Tuple[str, Any]] = field(default_factory=list)
    csv_rowcount_max_bytes: int = DEFAULTS["csv_rowcount_max_bytes"]
    csvgz_rowcount_max_bytes: int = DEFAULTS["csvgz_rowcount_max_bytes"]
    json_parse_max_bytes: int = DEFAULTS["json_parse_max_bytes"]
    code_parse_max_bytes: int = DEFAULTS["code_parse_max_bytes"]
    racy_window_seconds: int = DEFAULTS["racy_window_seconds"]
    walk_threads: int = DEFAULTS["walk_threads"]
    agent_map_top_n_h5ad: int = DEFAULTS["agent_map_top_n_h5ad"]
    label_obs_substrings: Tuple[str, ...] = tuple(DEFAULTS["label_obs_substrings"])
    index_columns: bool = DEFAULTS["index_columns"]
    index_figure_text: bool = DEFAULTS["index_figure_text"]
    figure_text_max_bytes: int = DEFAULTS["figure_text_max_bytes"]
    include_globs: List[str] = field(default_factory=list)
    exclude_globs: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def category_for(self, ext: str) -> str:
        """Return the category for a (compound-aware) extension key.

        ``ext`` is the lowercased key produced by ``extractors.base.ext_of``
        (compound-aware: ``"csv.gz"`` arrives as-is and is looked up directly, so
        it correctly maps to ``data_table`` rather than the bare ``gz`` ->
        ``archive``). Unknown/empty extensions fall back to ``"other"``.
        See CONTRACTS.md §3.
        """
        if not ext:
            return "other"
        return self.ext_to_category.get(ext.lower(), "other")

    def tag(self, path: str) -> List[str]:
        """Return the ontology tags matching ``path`` (a relative POSIX path).

        Applies each compiled ``ontology_tags`` regex (``re.search``) to the
        path and collects the matching tags, preserving config order and
        de-duplicating. Driven entirely by the regexes from defaults.yaml /
        overrides. See CONTRACTS.md §5.1.
        """
        tags: List[str] = []
        for name, pattern in self.ontology_tags:
            try:
                if pattern.search(path) and name not in tags:
                    tags.append(name)
            except Exception:  # noqa: BLE001 - a bad regex must never abort a walk
                continue
        return tags


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``base`` deep-merged with ``over`` (``over`` wins).

    Nested dicts merge recursively; every other type (incl. lists) is replaced
    wholesale by the override. Operates on a shallow copy of ``base`` so the
    caller's dict is never mutated.
    """
    out = dict(base)
    for key, val in over.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(val, dict)
        ):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _parse_yaml_text(text: str) -> Dict[str, Any]:
    """Parse YAML ``text`` to a dict, preferring pyyaml, else a stdlib fallback.

    The stdlib fallback is deliberately minimal: it handles ONLY the flat-ish
    shape of the shipped ``defaults.yaml`` (top-level scalars, ``- item`` lists,
    and ``{tag: x, pattern: y}`` inline mappings). It is sufficient to bootstrap
    the tool on a bare CPython with no pyyaml installed. Anything it cannot parse
    is simply skipped (never raises).
    """
    if yaml is not None:
        loaded = yaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    return _stdlib_yaml_fallback(text)


def _decode_yaml_escapes(s: str) -> str:
    """Decode YAML double-quoted backslash escapes (``\\.`` -> ``.`` etc.).

    YAML's double-quoted style processes backslash escapes (``\\\\`` -> ``\\``,
    ``\\.`` -> ``.``, ``\\n`` -> newline). pyyaml does this; the stdlib fallback
    historically did not, so a pattern like ``"leiden_2\\.0"`` arrived as the
    8-char ``leiden_2\\.0`` under stdlib (a doubled backslash) vs the escaped-dot
    ``leiden_2\\.0`` (a single backslash) under pyyaml — diverging the compiled
    ontology regex and silently breaking stdlib-only tag classification. We decode
    via ``codecs.unicode_escape`` (guarded) so the two paths agree. Latin-1
    round-trips bytes <256 so non-ASCII content is preserved rather than
    mojibaked; on any failure we return the input unchanged (never raise).
    """
    if "\\" not in s:
        return s
    try:
        return s.encode("latin-1", "backslashreplace").decode("unicode_escape")
    except Exception:  # noqa: BLE001 - escape decoding is best-effort
        return s


def _coerce_scalar(token: str) -> Any:
    """Coerce a YAML scalar token to a Python value (int/bool/null/str).

    A double-quoted token has its YAML backslash escapes decoded (so it matches
    pyyaml's behaviour); single-quoted YAML does NOT process backslash escapes, so
    its contents are taken verbatim.
    """
    tok = token.strip()
    if tok.startswith('"') and tok.endswith('"'):
        return _decode_yaml_escapes(tok[1:-1])
    if tok.startswith("'") and tok.endswith("'"):
        return tok[1:-1]
    low = tok.lower()
    if low in ("null", "~", ""):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        pass
    return tok


def _stdlib_yaml_fallback(text: str) -> Dict[str, Any]:
    """Minimal, dependency-free parser for the shipped flat defaults.yaml.

    Supports the shapes used by the shipped defaults.yaml:
      * ``key: scalar`` (top level);
      * ``key:`` opening an indented block that is EITHER a list of ``- item``
        lines (incl. ``- {tag: x, pattern: y}`` inline maps) OR a nested
        ``child: value`` mapping (e.g. ``ext_to_category:``);
      * inline ``[]`` / ``{}`` empty containers.
    Indentation determines block membership (children are more-indented than
    their parent key). Comments (``#``) and blank lines are ignored. Not a
    general YAML parser — it exists only so the tool boots without pyyaml.
    """

    def _strip_inline_comment(s: str) -> str:
        # Drop a trailing " # ..." comment (the shipped file only uses these).
        return s.split(" #", 1)[0].rstrip() if " #" in s else s

    def _indent(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    def _split_top_level(s: str, sep: str) -> List[str]:
        """Split ``s`` on ``sep`` only when OUTSIDE single/double quotes.

        An inline map value such as ``pattern: "a,b"`` contains a comma inside a
        quoted scalar; a naive ``str.split(",")`` would shred it. This tracks
        quote state so separators inside quotes are preserved.
        """
        parts: List[str] = []
        buf: List[str] = []
        quote: Optional[str] = None
        for ch in s:
            if quote is not None:
                buf.append(ch)
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
                buf.append(ch)
            elif ch == sep:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
        parts.append("".join(buf))
        return parts

    def _parse_inline_map(item: str) -> Dict[str, Any]:
        obj: Dict[str, Any] = {}
        for part in _split_top_level(item[1:-1], ","):
            if ":" in part:
                # Split on the FIRST top-level colon so a colon inside a quoted
                # value (or a "::" token) does not mis-key the entry.
                kv = _split_top_level(part, ":")
                k = kv[0]
                v = ":".join(kv[1:]) if len(kv) > 1 else ""
                obj[k.strip()] = _coerce_scalar(v)
        return obj

    # Pre-tokenize into (indent, content) keeping only meaningful lines.
    lines: List[Tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        lines.append((_indent(raw_line), raw_line.strip()))

    def parse_block(i: int, indent: int) -> Tuple[Any, int]:
        """Parse a block of items at >= ``indent`` starting at index ``i``.

        Returns (value, next_index). The block is a list if its first item is a
        ``- ...`` line, otherwise a mapping of ``key: value`` pairs.
        """
        if i >= len(lines):
            return {}, i
        first_ind, first_content = lines[i]
        is_list = first_content.startswith("- ")
        container: Any = [] if is_list else {}

        while i < len(lines):
            ind, content = lines[i]
            if ind < indent:
                break
            if ind > indent:
                # Shouldn't happen for well-formed input; skip defensively.
                i += 1
                continue

            if content.startswith("- "):
                item = _strip_inline_comment(content[2:].strip())
                if item.startswith("{") and item.endswith("}"):
                    container.append(_parse_inline_map(item))
                else:
                    container.append(_coerce_scalar(item))
                i += 1
                continue

            if ":" in content:
                key, _, rest = content.partition(":")
                key = key.strip()
                rest = _strip_inline_comment(rest.strip())
                if rest in ("", "[]", "{}"):
                    if rest == "{}":
                        container[key] = {}
                        i += 1
                    elif rest == "[]":
                        container[key] = []
                        i += 1
                    else:
                        # Open a nested block from the next, more-indented line.
                        if i + 1 < len(lines) and lines[i + 1][0] > indent:
                            child, i = parse_block(i + 1, lines[i + 1][0])
                            container[key] = child
                        else:
                            container[key] = {}
                            i += 1
                else:
                    container[key] = _coerce_scalar(rest)
                    i += 1
                continue

            # Unrecognized line; skip.
            i += 1

        return container, i

    if not lines:
        return {}
    top_indent = min(ind for ind, _ in lines)
    value, _ = parse_block(0, top_indent)
    return value if isinstance(value, dict) else {}


def _read_config_file(path: Path) -> Dict[str, Any]:
    """Read a YAML/TOML override file into a dict; ``{}`` on any failure.

    Never raises: a missing file, parse error, or absent tomllib degrades to an
    empty override so the tool keeps running on defaults.
    """
    try:
        suffix = path.suffix.lower()
        if suffix == ".toml":
            if tomllib is None:
                return {}
            with path.open("rb") as fh:
                loaded = tomllib.load(fh)
            return loaded if isinstance(loaded, dict) else {}
        text = path.read_text(encoding="utf-8")
        return _parse_yaml_text(text)
    except Exception:  # noqa: BLE001 - overrides are best-effort
        return {}


def _compile_ontology_tags(
    raw_tags: Any,
) -> List[Tuple[str, "re.Pattern"]]:
    """Compile the ``ontology_tags`` config list into (tag, regex) pairs.

    Accepts the list-of-dicts shape ``[{"tag": str, "pattern": str}, ...]``.
    Patterns compile case-insensitively. An entry with a bad/empty pattern or a
    regex that fails to compile is skipped (never raises). Order is preserved.
    """
    compiled: List[Tuple[str, "re.Pattern"]] = []
    if not isinstance(raw_tags, list):
        return compiled
    for item in raw_tags:
        if not isinstance(item, dict):
            continue
        tag = item.get("tag")
        pattern = item.get("pattern")
        if not tag or not pattern:
            continue
        try:
            compiled.append((str(tag), re.compile(str(pattern), re.IGNORECASE)))
        except re.error:
            continue
    return compiled


def _coerce_walk_threads(value: Any) -> int:
    """Coerce the ``walk_threads`` config value to a sane positive int (W6).

    Falls back to the default on any non-numeric / un-coercible input (never
    raises — a bad override must not abort config loading). A value ``<= 1`` is
    preserved verbatim (it selects the SERIAL walk path); negative / zero are
    clamped to 1 so the walk always has the serial-or-parallel choice well-defined.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return int(DEFAULTS["walk_threads"])
    return n if n >= 1 else 1


def load_config(
    path: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> "Config":
    """Load defaults.yaml, merge an optional override file/dict, return a Config.

    Resolution order (later wins): packaged ``defaults.yaml`` -> ``path``
    (a YAML/TOML override file) -> ``overrides`` (a dict, typically from CLI
    flags). The raw merged mapping is echoed onto ``Config.raw`` for
    INDEX.json's ``config_used``. ``ontology_tags`` regexes are compiled
    (case-insensitive); the prune set is built. pyyaml is optional — when absent
    the shipped flat defaults.yaml is read via a stdlib fallback parser. See
    CONTRACTS.md §3/§6/§9.
    """
    # Start from the in-code DEFAULTS so the tool works even if defaults.yaml is
    # somehow unreadable; then layer the packaged YAML on top.
    merged: Dict[str, Any] = dict(DEFAULTS)

    defaults_path = Path(__file__).resolve().parent / "defaults.yaml"
    file_cfg = _read_config_file(defaults_path)
    if file_cfg:
        merged = _deep_merge(merged, file_cfg)

    if path is not None:
        merged = _deep_merge(merged, _read_config_file(Path(path)))

    if overrides:
        merged = _deep_merge(merged, dict(overrides))

    return Config(
        prune_dirs=set(merged.get("prune_dirs", [])),
        skip_prefixes=tuple(merged.get("skip_prefixes", ("._",))),
        out_dirname=str(merged.get("out_dirname", "_repo_index")),
        ext_to_category=dict(merged.get("ext_to_category", {})),
        ontology_tags=_compile_ontology_tags(merged.get("ontology_tags", [])),
        csv_rowcount_max_bytes=int(
            merged.get("csv_rowcount_max_bytes", DEFAULTS["csv_rowcount_max_bytes"])
        ),
        csvgz_rowcount_max_bytes=int(
            merged.get(
                "csvgz_rowcount_max_bytes", DEFAULTS["csvgz_rowcount_max_bytes"]
            )
        ),
        json_parse_max_bytes=int(
            merged.get("json_parse_max_bytes", DEFAULTS["json_parse_max_bytes"])
        ),
        code_parse_max_bytes=int(
            merged.get("code_parse_max_bytes", DEFAULTS["code_parse_max_bytes"])
        ),
        racy_window_seconds=int(
            merged.get("racy_window_seconds", DEFAULTS["racy_window_seconds"])
        ),
        walk_threads=_coerce_walk_threads(
            merged.get("walk_threads", DEFAULTS["walk_threads"])
        ),
        agent_map_top_n_h5ad=int(
            merged.get("agent_map_top_n_h5ad", DEFAULTS["agent_map_top_n_h5ad"])
        ),
        label_obs_substrings=tuple(
            merged.get("label_obs_substrings", DEFAULTS["label_obs_substrings"])
        ),
        index_columns=bool(merged.get("index_columns", DEFAULTS["index_columns"])),
        index_figure_text=bool(
            merged.get("index_figure_text", DEFAULTS["index_figure_text"])
        ),
        figure_text_max_bytes=int(
            merged.get("figure_text_max_bytes", DEFAULTS["figure_text_max_bytes"])
        ),
        include_globs=list(merged.get("include_globs", []) or []),
        exclude_globs=list(merged.get("exclude_globs", []) or []),
        raw=merged,
    )
