"""repo_index.query — query CLI over INDEX.jsonl.

Streams INDEX.jsonl line-by-line (greppable without loading the whole file) and
filters entries by field predicates: substring on path, by category/ext/extractor,
and the highest-value query "which entry has obs column X / obsm key Y". Returns
matching entries. Stdlib-only. See CONTRACTS.md §8/§11.

CLI verbs (see CONTRACTS.md §8 agent-map "How to query"):
    find <substr>        -> path substring match
    has-obs <col>        -> entries whose meta.obs_columns contains <col>
    obsm <key>           -> entries whose meta.obsm has key <key>
    by-type <category>   -> entries of a given category
    broken               -> broken symlinks (symlink_ok == false)
    errors               -> entries with an extraction error
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


def iter_entries(jsonl_path: Path) -> Iterator[Dict[str, Any]]:
    """Yield each entry dict from INDEX.jsonl, one per line (stdlib json.loads).

    Streams the file (no full-file load) so it scales to a large index. Blank
    lines and unparseable lines are skipped (the index should never contain
    them, but a robust reader does not abort on one bad line).
    """
    p = Path(jsonl_path)
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (ValueError, TypeError):
                continue


def query(
    jsonl_path: Path,
    *,
    path_substr: Optional[str] = None,
    category: Optional[str] = None,
    ext: Optional[str] = None,
    extractor: Optional[str] = None,
    obs_column: Optional[str] = None,
    obsm_key: Optional[str] = None,
    broken: bool = False,
    errors: bool = False,
) -> List[Dict[str, Any]]:
    """Return entries from INDEX.jsonl matching ALL supplied predicates.

    Predicates (any may be None / False = ignored): ``path_substr`` (substring
    on rel path), ``category``/``ext``/``extractor`` (exact), ``obs_column``
    (entry's meta.obs_columns contains it), ``obsm_key`` (entry's meta.obsm has
    it), ``broken`` (symlink_ok is False), ``errors`` (an ``error`` field is
    present). The obs_column/obsm_key predicates serve the single highest-value
    agent query "which h5ad has column X / obsm key Y".
    """
    results: List[Dict[str, Any]] = []
    for entry in iter_entries(jsonl_path):
        if path_substr is not None and path_substr not in entry.get("path", ""):
            continue
        if category is not None and entry.get("category") != category:
            continue
        if ext is not None and entry.get("ext") != ext:
            continue
        if extractor is not None and entry.get("extractor") != extractor:
            continue

        meta = entry.get("meta") or {}
        if obs_column is not None:
            cols = meta.get("obs_columns")
            if not isinstance(cols, list) or obs_column not in cols:
                continue
        if obsm_key is not None:
            obsm = meta.get("obsm")
            if not isinstance(obsm, dict) or obsm_key not in obsm:
                continue

        if broken and entry.get("symlink_ok") is not False:
            continue
        if errors and entry.get("error") is None:
            continue

        results.append(entry)
    return results


def _default_jsonl(explicit: Optional[str]) -> Path:
    """Resolve the INDEX.jsonl path: explicit flag, else ``.repo_index/INDEX.jsonl``
    under the current working directory."""
    if explicit:
        return Path(explicit)
    return Path.cwd() / ".repo_index" / "INDEX.jsonl"


def _format_entry(entry: Dict[str, Any]) -> str:
    """One-line human/agent summary: path plus the most useful meta for the file
    kind (h5ad dims + obsm keys; tabular columns count; error string)."""
    parts = [entry.get("path", "?")]
    meta = entry.get("meta") or {}
    if entry.get("extractor") == "h5ad" and "n_obs" in meta:
        obsm = meta.get("obsm")
        obsm_keys = list(obsm.keys()) if isinstance(obsm, dict) else []
        parts.append(
            f"[{meta.get('n_obs')}x{meta.get('n_vars')} obsm={obsm_keys}]"
        )
    elif "n_columns" in meta:
        parts.append(f"[{meta.get('n_columns')} cols]")
    if entry.get("symlink_ok") is False:
        parts.append("[BROKEN SYMLINK]")
    if entry.get("error") is not None:
        parts.append(f"[ERROR: {entry.get('error')}]")
    return " ".join(parts)


def main(argv: Optional[List[str]] = None) -> int:
    """argparse front-end for the ``query`` subcommand; prints matches, returns 0.

    Supports the verb-style queries (``find``, ``has-obs``, ``obsm``,
    ``by-type``, ``broken``, ``errors``) AND a flag form
    (--path/--category/--ext/--extractor/--obs-column/--obsm-key). Prints each
    matching entry's path plus key meta. Called by cli.main for the ``query``
    subcommand. Returns 0 always (no match is not an error); returns 2 on a
    usage error.
    """
    parser = argparse.ArgumentParser(
        prog="repo_index query",
        description="Query INDEX.jsonl produced by repo_index.",
    )
    parser.add_argument(
        "--jsonl",
        default=None,
        help="Path to INDEX.jsonl (default: ./.repo_index/INDEX.jsonl).",
    )
    # Flag-style predicates (combinable).
    parser.add_argument("--path", dest="path_substr", default=None)
    parser.add_argument("--category", default=None)
    parser.add_argument("--ext", default=None)
    parser.add_argument("--extractor", default=None)
    parser.add_argument("--obs-column", dest="obs_column", default=None)
    parser.add_argument("--obsm-key", dest="obsm_key", default=None)
    parser.add_argument("--broken", action="store_true")
    parser.add_argument("--errors", action="store_true")
    parser.add_argument(
        "--count", action="store_true", help="Print only the match count."
    )

    sub = parser.add_subparsers(dest="verb")
    p_find = sub.add_parser("find", help="path substring match")
    p_find.add_argument("substr")
    p_obs = sub.add_parser("has-obs", help="entries whose obs_columns contain COL")
    p_obs.add_argument("col")
    p_obsm = sub.add_parser("obsm", help="entries whose obsm has KEY")
    p_obsm.add_argument("key")
    p_type = sub.add_parser("by-type", help="entries of CATEGORY")
    p_type.add_argument("category")
    sub.add_parser("broken", help="broken symlinks")
    sub.add_parser("errors", help="entries with an extraction error")

    args = parser.parse_args(argv)

    kw: Dict[str, Any] = {
        "path_substr": args.path_substr,
        "category": args.category,
        "ext": args.ext,
        "extractor": args.extractor,
        "obs_column": args.obs_column,
        "obsm_key": args.obsm_key,
        "broken": args.broken,
        "errors": args.errors,
    }

    verb = args.verb
    if verb == "find":
        kw["path_substr"] = args.substr
    elif verb == "has-obs":
        kw["obs_column"] = args.col
    elif verb == "obsm":
        kw["obsm_key"] = args.key
    elif verb == "by-type":
        kw["category"] = args.category
    elif verb == "broken":
        kw["broken"] = True
    elif verb == "errors":
        kw["errors"] = True

    jsonl = _default_jsonl(args.jsonl)
    if not jsonl.exists():
        parser.error(f"INDEX.jsonl not found: {jsonl}")
        return 2

    matches = query(jsonl, **kw)
    if args.count:
        print(len(matches))
    else:
        for entry in matches:
            print(_format_entry(entry))
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(main())
