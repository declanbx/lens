"""repo_index.agent_map — INDEX.agent.md generator.

Terse map for LLM agents (CONTRACTS.md §8): (1) summary stats; (2) Key data
matrices table — top-N .h5ad by size with n_obs x n_vars + obsm keys + label-like
obs columns; (3) Health — broken symlinks + errors; (4) How to query this index —
concrete jsonl-grep + query-CLI examples. Stdlib-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List


def _md_escape(text: str) -> str:
    """Escape pipe characters so a value never breaks a Markdown table cell."""
    return str(text).replace("|", "\\|")


def _label_like(obs_columns: List[str], substrings: List[str]) -> List[str]:
    """Return obs column names whose (lowercased) name contains any of the
    label-like substrings (e.g. leiden/label/type/annotation)."""
    subs = [s.lower() for s in substrings]
    out = []
    for col in obs_columns:
        low = col.lower()
        if any(s in low for s in subs):
            out.append(col)
    return out


def _fmt_list(items: List[str], limit: int = 12) -> str:
    """Comma-join a list for a table cell, truncating with an ellipsis count."""
    if not items:
        return "-"
    if len(items) <= limit:
        return ", ".join(_md_escape(i) for i in items)
    shown = ", ".join(_md_escape(i) for i in items[:limit])
    return f"{shown}, … (+{len(items) - limit})"


def render_agent_map(manifest: Dict[str, Any], config: Any) -> str:
    """Return the full INDEX.agent.md text (CONTRACTS.md §8).

    Sections: summary stats; a "Key data matrices" table of the top-N h5ad by
    size_bytes (config.agent_map_top_n_h5ad) showing n_obs x n_vars, obsm keys,
    and label-like obs columns (name contains any config.label_obs_substrings);
    a "Health" section listing broken symlinks + extraction errors; and a "How to
    query this index" section with concrete grep-over-INDEX.jsonl and query-CLI
    examples. Pure string assembly, no FS reads beyond the manifest.
    """
    entries: List[Dict[str, Any]] = manifest.get("entries", [])
    summary: Dict[str, Any] = manifest.get("summary", {})

    top_n = getattr(config, "agent_map_top_n_h5ad", 25)
    label_subs = list(getattr(config, "label_obs_substrings",
                              ["leiden", "label", "type", "annotation"]))

    lines: List[str] = []

    # ---- Header ---------------------------------------------------------- #
    lines.append("# repo_index — agent map")
    lines.append("")
    lines.append(
        f"Root: `{manifest.get('root', '?')}`  ·  "
        f"tool v{manifest.get('tool_version', '?')}  ·  "
        f"generated {manifest.get('generated_at', '?')}"
    )
    git = manifest.get("git_commit")
    lines.append(f"git HEAD: `{git}`" if git else "git HEAD: (non-git root)")
    lines.append(f"content_digest: `{manifest.get('content_digest', '?')}`")
    lines.append("")

    # ---- 1. Summary ------------------------------------------------------ #
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- total_files: {summary.get('total_files', 0)}")
    lines.append(f"- total_bytes: {summary.get('total_bytes', 0)}")
    lines.append(f"- n_symlinks: {summary.get('n_symlinks', 0)}")
    lines.append(f"- n_broken_symlinks: {summary.get('n_broken_symlinks', 0)}")
    lines.append(f"- n_errors: {summary.get('n_errors', 0)}")
    lines.append("")
    by_cat = summary.get("by_category", {})
    if by_cat:
        lines.append("By category:")
        lines.append("")
        lines.append("| category | count |")
        lines.append("|---|---|")
        for cat, cnt in sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"| {_md_escape(cat)} | {cnt} |")
        lines.append("")

    # ---- 2. Key data matrices ------------------------------------------- #
    lines.append("## Key data matrices")
    lines.append("")
    h5ads = [
        e for e in entries
        if e.get("extractor") == "h5ad" and isinstance(e.get("meta"), dict)
        and "n_obs" in (e.get("meta") or {})
    ]
    h5ads.sort(key=lambda e: e.get("size_bytes", 0), reverse=True)
    h5ads = h5ads[:top_n]

    if not h5ads:
        lines.append("_No `.h5ad` files with extractable structure found._")
        lines.append("")
    else:
        lines.append(
            "Top h5ad by size. `obsm` keys and label-like `obs` columns "
            "(name contains "
            + "/".join(f"`{s}`" for s in label_subs)
            + ") shown."
        )
        lines.append("")
        lines.append(
            "| path | n_obs × n_vars | obsm | layers | label-like obs |"
        )
        lines.append("|---|---|---|---|---|")
        for e in h5ads:
            meta = e.get("meta", {})
            n_obs = meta.get("n_obs")
            n_vars = meta.get("n_vars")
            obsm = meta.get("obsm")
            obsm_keys = list(obsm.keys()) if isinstance(obsm, dict) else []
            layers = meta.get("layers") or []
            obs_cols = meta.get("obs_columns") or []
            labels = _label_like(obs_cols, label_subs)
            lines.append(
                f"| `{_md_escape(e.get('path', '?'))}` "
                f"| {n_obs} × {n_vars} "
                f"| {_fmt_list(obsm_keys)} "
                f"| {_fmt_list(layers)} "
                f"| {_fmt_list(labels)} |"
            )
        lines.append("")

    # ---- 3. Health ------------------------------------------------------- #
    lines.append("## Health")
    lines.append("")
    broken = [
        e for e in entries
        if e.get("is_symlink") and e.get("symlink_ok") is False
    ]
    errored = [e for e in entries if e.get("error") is not None]

    lines.append(
        f"Broken symlinks: {len(broken)}  ·  extraction errors: {len(errored)}"
    )
    lines.append("")
    if broken:
        lines.append("Broken symlinks:")
        lines.append("")
        for e in broken:
            tgt = e.get("symlink_target")
            lines.append(f"- `{_md_escape(e.get('path', '?'))}` → `{tgt}`")
        lines.append("")
    if errored:
        lines.append("Extraction errors:")
        lines.append("")
        for e in errored:
            lines.append(
                f"- `{_md_escape(e.get('path', '?'))}`: "
                f"{_md_escape(e.get('error', ''))}"
            )
        lines.append("")
    if not broken and not errored:
        lines.append("No broken symlinks or extraction errors. ✅")
        lines.append("")

    # ---- 4. How to query this index ------------------------------------- #
    lines.append("## How to query this index")
    lines.append("")
    lines.append(
        "Each line of `INDEX.jsonl` is a standalone JSON object (one file per "
        "line), so agents can `grep` it directly without parsing the whole "
        "index. `INDEX.json` holds the summary + the same entries as an array."
    )
    lines.append("")
    lines.append("Grep over INDEX.jsonl (fast, no parser):")
    lines.append("")
    lines.append("```sh")
    lines.append("# which h5ad has obs column 'leiden_cosine_2.0'")
    lines.append("grep -l 'leiden_cosine_2.0' .repo_index/INDEX.jsonl  # (per-line)")
    lines.append("grep '\"leiden_cosine_2.0\"' .repo_index/INDEX.jsonl")
    lines.append("# which file has obsm key X_umap")
    lines.append("grep '\"X_umap\"' .repo_index/INDEX.jsonl")
    lines.append("# all broken symlinks")
    lines.append("grep '\"symlink_ok\":false' .repo_index/INDEX.jsonl")
    lines.append("# all entries that hit an extraction error")
    lines.append("grep '\"error\":' .repo_index/INDEX.jsonl")
    lines.append("```")
    lines.append("")
    lines.append("Query CLI (`repo_index query`, structured predicates):")
    lines.append("")
    lines.append("```sh")
    lines.append("python -m repo_index query find phase4b")
    lines.append("python -m repo_index query has-obs leiden_cosine_2.0")
    lines.append("python -m repo_index query obsm X_umap")
    lines.append("python -m repo_index query by-type data_matrix")
    lines.append("python -m repo_index query broken")
    lines.append("python -m repo_index query errors")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def write_agent_map(manifest: Dict[str, Any], config: Any, out_dir: Path) -> Path:
    """Render and write INDEX.agent.md into ``out_dir``; return its path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "INDEX.agent.md"
    out_path.write_text(render_agent_map(manifest, config), encoding="utf-8")
    return out_path
