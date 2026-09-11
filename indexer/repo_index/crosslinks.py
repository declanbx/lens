"""repo_index.crosslinks — doc/code -> artifact edge graph.

Builds edges from docs (``.md`` ``meta.outbound_refs``, kind ``doc_ref``) and
code path-literals (path-like strings found in a code entry's ``meta``, kind
``code_ref``) to indexed artifact paths they reference. Resolution is done via a
single dict index keyed by both relative POSIX path and basename, so there is no
O(n^2) scan of the entry list (CONTRACTS.md §0.7, §8). Emits ``crosslinks.dot``
(a Graphviz digraph) + ``crosslinks.json`` (``{nodes, edges, dangling_refs}``).

Stdlib-only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# Extensions we consider "indexed artifact" extensions for the purposes of
# recognising a path-like token. Compound suffixes are matched first. This is a
# superset of the §3 category map extensions so that a reference to any indexed
# artifact type is recognised. Kept local + lowercased; matching is suffix-based.
_INDEXED_EXTS: Tuple[str, ...] = (
    # compound first (longest) so "csv.gz" wins over "gz"
    "csv.gz", "tsv.gz",
    # data matrices / tables
    "h5ad", "h5", "npy", "npz", "loom", "csv", "tsv", "parquet", "xlsx",
    # config / structured
    "yaml", "yml", "toml", "json", "ini",
    # code
    "py", "r", "sh", "cpp", "c",
    # docs / notebooks
    "md", "txt", "rst", "ipynb",
    # figures
    "png", "pdf", "svg", "jpg", "jpeg",
    # models
    "pkl", "pt", "pth", "joblib", "model", "rds", "onnx",
    # logs / archives
    "log", "out", "err", "gz", "tgz", "zip", "tar",
)

# A token is "path-like" for the code-literal scan if it contains a "/" and ends
# in one of the indexed extensions (case-insensitive). Anchored at end so we do
# not match e.g. a sentence fragment. Built once at import time.
_PATHLIKE_RE = re.compile(
    r"[^\s\"'`<>|]+/[^\s\"'`<>|]*\.(?:" + "|".join(re.escape(e) for e in _INDEXED_EXTS) + r")\b",
    re.IGNORECASE,
)

# String literals inside Python/R/shell source frequently appear as plain
# basenames too (e.g. "phase4b_annotated.h5ad"); we additionally accept a bare
# basename ending in an indexed extension so a code reference can resolve via the
# basename index even without a directory component.
_BASENAME_RE = re.compile(
    r"[\w.\-]+\.(?:" + "|".join(re.escape(e) for e in _INDEXED_EXTS) + r")\b",
    re.IGNORECASE,
)


def _norm_ref(ref: str) -> str:
    """Normalise a raw reference string to a comparable relative-ish path.

    Strips surrounding whitespace, a leading ``./``, URL fragments/anchors, and a
    trailing slash. Backslashes are converted to forward slashes (defensive; the
    repo is POSIX-indexed). Does NOT resolve ``..`` (kept literal so an exact
    relpath match still works). Returns ``""`` for empty input.
    """
    if not ref:
        return ""
    r = ref.strip().strip("\"'`")
    # drop a markdown anchor / query fragment if present
    for cut in ("#", "?"):
        idx = r.find(cut)
        if idx != -1:
            r = r[:idx]
    r = r.replace("\\", "/")
    while r.startswith("./"):
        r = r[2:]
    r = r.rstrip("/")
    return r


def _is_external(ref: str) -> bool:
    """True if ``ref`` is an external URL / mailto / absolute scheme we cannot
    resolve to an indexed artifact (http(s), ftp, mailto, data:, etc.)."""
    low = ref.lower()
    return (
        "://" in low
        or low.startswith("mailto:")
        or low.startswith("data:")
        or low.startswith("tel:")
    )


def _iter_strings(obj: Any) -> Iterable[str]:
    """Yield every string found anywhere within ``obj`` (dict/list/scalar).

    Used to harvest path-literals from a code entry's ``meta`` regardless of
    which field they live in (the §4.6 code meta has no dedicated path field, so
    we scan all string values: imports, docstring lines, etc., plus any future
    path-literal field). Dict KEYS are also scanned.
    """
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_strings(item)


def _extract_code_path_literals(meta: Dict[str, Any]) -> List[str]:
    """Return ordered, deduped path-like literals harvested from a code ``meta``.

    Scans every string value in ``meta`` for tokens that look like a repo path
    (contain ``/`` and an indexed extension) OR a bare artifact basename. The
    §4.6 code extractor does not currently emit a dedicated path field, so this
    generic scan is what surfaces ``code_ref`` edges; it also tolerates a future
    explicit ``path_literals`` field by simply finding those strings too.
    """
    seen: Set[str] = set()
    out: List[str] = []
    for s in _iter_strings(meta):
        # path-like (has a directory component) takes priority
        for m in _PATHLIKE_RE.finditer(s):
            tok = m.group(0)
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
        # bare basenames (no slash) — only add tokens that did NOT already get
        # captured as part of a longer path-like match above
        for m in _BASENAME_RE.finditer(s):
            tok = m.group(0)
            if "/" in tok:
                continue
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _build_resolver(entries: List[Dict[str, Any]]) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """Build the resolution indices from entries (single O(n) pass).

    Returns ``(by_relpath, by_basename)`` where:
      - ``by_relpath`` maps the exact relative POSIX path -> itself (canonical).
      - ``by_basename`` maps a basename -> list of relpaths sharing that
        basename (a ref may be ambiguous; we keep all candidates).
    No O(n^2): each entry is visited once.
    """
    by_relpath: Dict[str, str] = {}
    by_basename: Dict[str, List[str]] = {}
    for e in entries:
        p = e.get("path")
        if not isinstance(p, str) or not p:
            continue
        by_relpath[p] = p
        base = p.rsplit("/", 1)[-1]
        by_basename.setdefault(base, []).append(p)
    return by_relpath, by_basename


def _resolve(
    ref: str,
    src_path: str,
    by_relpath: Dict[str, str],
    by_basename: Dict[str, List[str]],
) -> Optional[str]:
    """Resolve a single normalised reference to an indexed artifact relpath.

    Resolution order (all dict lookups, no scanning):
      1. exact relpath match (``ref`` is already a repo-relative path);
      2. relpath join against the source file's directory (``src_dir/ref``),
         which catches relative links inside docs/code;
      3. basename match — unambiguous (exactly one candidate) preferred; if
         multiple candidates share the basename, pick one that is NOT the source
         itself, preferring a unique non-source candidate, else the first.
    Returns the resolved relpath or ``None`` if unresolved.
    """
    if not ref:
        return None

    # 1. exact relpath
    if ref in by_relpath:
        return ref

    # 2. join against the source directory
    src_dir = src_path.rsplit("/", 1)[0] if "/" in src_path else ""
    if src_dir:
        joined = _collapse(src_dir + "/" + ref)
        if joined in by_relpath:
            return joined
    else:
        joined = _collapse(ref)
        if joined in by_relpath:
            return joined

    # 3. basename match
    base = ref.rsplit("/", 1)[-1]
    cands = by_basename.get(base)
    if cands:
        if len(cands) == 1:
            return cands[0]
        non_self = [c for c in cands if c != src_path]
        if len(non_self) == 1:
            return non_self[0]
        # ambiguous: prefer one that endswith the full ref (path tail match)
        if "/" in ref:
            tail_hits = [c for c in (non_self or cands) if c.endswith(ref)]
            if len(tail_hits) == 1:
                return tail_hits[0]
        return (non_self or cands)[0]
    return None


def _collapse(path: str) -> str:
    """Collapse ``.`` / ``..`` segments in a POSIX-style relative path string
    without touching the filesystem (so it is safe + space-safe). Leading ``..``
    that escape the root are preserved literally (they simply won't resolve)."""
    parts = path.split("/")
    out: List[str] = []
    for seg in parts:
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            if out and out[-1] != "..":
                out.pop()
            else:
                out.append("..")
        else:
            out.append(seg)
    return "/".join(out)


def build_graph(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the crosslink graph mapping (CONTRACTS.md §8).

    Builds a path/basename -> entry index, then scans doc entries
    (``meta.outbound_refs``, kind ``doc_ref``) and code entries (path-literals in
    ``meta``, kind ``code_ref``) for references that resolve to an indexed
    artifact path. Returns ``{"nodes": [path,...], "edges":
    [{"src","dst","kind"},...], "dangling_refs": [{"src","ref","kind"},...]}``.
    Unresolved refs go to ``dangling_refs``. Resolution is dict-indexed (no
    O(n^2)). Self-edges (src == dst) are dropped. Edges are deduped on
    (src, dst, kind) and the whole result is deterministically ordered.
    """
    by_relpath, by_basename = _build_resolver(entries)

    edges: List[Dict[str, str]] = []
    dangling: List[Dict[str, str]] = []
    edge_seen: Set[Tuple[str, str, str]] = set()
    node_set: Set[str] = set()

    def _add_edge(src: str, dst: str, kind: str) -> None:
        if src == dst:
            return
        key = (src, dst, kind)
        if key in edge_seen:
            return
        edge_seen.add(key)
        edges.append({"src": src, "dst": dst, "kind": kind})
        node_set.add(src)
        node_set.add(dst)

    for e in entries:
        src = e.get("path")
        if not isinstance(src, str) or not src:
            continue
        extractor = e.get("extractor")
        meta = e.get("meta") or {}
        if not isinstance(meta, dict):
            continue

        # --- doc refs (markdown outbound_refs) ---
        if extractor == "doc":
            refs = meta.get("outbound_refs") or []
            if isinstance(refs, list):
                for raw in refs:
                    if not isinstance(raw, str):
                        continue
                    if _is_external(raw):
                        continue
                    ref = _norm_ref(raw)
                    if not ref:
                        continue
                    dst = _resolve(ref, src, by_relpath, by_basename)
                    if dst is not None:
                        _add_edge(src, dst, "doc_ref")
                    else:
                        dangling.append({"src": src, "ref": ref, "kind": "doc_ref"})

        # --- code refs (path-literals harvested from meta) ---
        elif extractor == "code":
            for raw in _extract_code_path_literals(meta):
                if _is_external(raw):
                    continue
                ref = _norm_ref(raw)
                if not ref:
                    continue
                dst = _resolve(ref, src, by_relpath, by_basename)
                if dst is not None:
                    _add_edge(src, dst, "code_ref")
                else:
                    dangling.append({"src": src, "ref": ref, "kind": "code_ref"})

    edges.sort(key=lambda d: (d["src"], d["dst"], d["kind"]))
    dangling.sort(key=lambda d: (d["src"], d["ref"], d["kind"]))
    nodes = sorted(node_set)
    return {"nodes": nodes, "edges": edges, "dangling_refs": dangling}


def _dot_quote(s: str) -> str:
    """Quote a string for a Graphviz DOT id (space-safe: paths contain spaces).

    Graphviz double-quoted IDs require escaping ``"`` and ``\\``; newlines are
    rendered as the literal ``\\n`` escape so a stray newline can never break the
    file. The result includes the surrounding quotes.
    """
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")
    return '"' + s + '"'


def to_dot(graph: Dict[str, Any]) -> str:
    """Return a Graphviz digraph string for ``graph`` (``crosslinks.dot``).

    One ``"src" -> "dst";`` edge per resolved reference. Quoting is space-safe.
    Edge ``kind`` is encoded via color/style (``doc_ref`` = blue solid,
    ``code_ref`` = dark-green dashed) and a ``kind`` attribute so consumers can
    filter. All declared nodes are emitted (including isolated endpoints).
    """
    lines: List[str] = []
    lines.append("digraph repo_index_crosslinks {")
    lines.append("  rankdir=LR;")
    lines.append('  node [shape=box, fontsize=9, fontname="Helvetica"];')
    lines.append('  edge [fontsize=8, fontname="Helvetica"];')

    for n in graph.get("nodes", []):
        lines.append(f"  {_dot_quote(n)};")

    style = {
        "doc_ref": 'color="#2563eb", style=solid',
        "code_ref": 'color="#15803d", style=dashed',
    }
    for edge in graph.get("edges", []):
        src = edge.get("src", "")
        dst = edge.get("dst", "")
        kind = edge.get("kind", "")
        attr = style.get(kind, "color=gray")
        lines.append(
            f"  {_dot_quote(src)} -> {_dot_quote(dst)} "
            f"[{attr}, kind={_dot_quote(kind)}];"
        )

    lines.append("}")
    return "\n".join(lines) + "\n"


def write_crosslinks(graph: Dict[str, Any], out_dir: Path) -> Dict[str, Path]:
    """Write ``crosslinks.dot`` + ``crosslinks.json`` into ``out_dir``.

    Creates ``out_dir`` if needed. ``crosslinks.json`` is the full graph mapping
    (``nodes``, ``edges``, ``dangling_refs``); ``crosslinks.dot`` is the Graphviz
    rendering from :func:`to_dot`. Returns ``{"dot": Path, "json": Path}``.
    Space-safe (pathlib only).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    dot_path = out_dir / "crosslinks.dot"
    json_path = out_dir / "crosslinks.json"

    dot_path.write_text(to_dot(graph), encoding="utf-8")
    json_path.write_text(
        json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return {"dot": dot_path, "json": json_path}
