"""repo_index.manifest — assemble INDEX.json + INDEX.jsonl + digest.

Collects walker entries, builds the summary, captures git HEAD (subprocess, args
as a list, cwd=root, tolerate non-git), computes the content_digest over the
VOLATILE-FREE canonical serialization, and writes INDEX.json + INDEX.jsonl. See
CONTRACTS.md §5.2/§5.3/§7. Stdlib-only.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "1.0"


def git_commit(root: Path) -> Optional[str]:
    """Return the current git HEAD sha for ``root``, or None for a non-git root.

    Runs ``git rev-parse HEAD`` via subprocess with args as a LIST (never a shell
    string), cwd=root, capturing output; any failure (not a repo, git missing)
    returns None. Space-safe.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:  # noqa: BLE001 - git absent, permission, etc. -> non-git
        return None
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    return sha or None


def _digest_payload(entries: List[Dict[str, Any]]) -> List[List[Any]]:
    """Build the canonical, path-sorted 4-tuple-as-list payload for the digest.

    Each element is ``[path, size_bytes, extractor, meta]`` — the ONLY
    digest-relevant fields. Volatile fields (generated_at, git_commit, mtime_iso,
    error) are excluded by construction. Sorted by path so the digest is
    deterministic regardless of the input ordering.
    """
    return [
        [
            e.get("path"),
            e.get("size_bytes"),
            e.get("extractor"),
            e.get("meta", {}),
        ]
        for e in sorted(entries, key=lambda x: x.get("path", ""))
    ]


def compute_digest(entries: List[Dict[str, Any]]) -> str:
    """Return the sha256 content_digest (CONTRACTS.md §7).

    Builds the canonical, path-sorted list ``[[path, size_bytes, extractor,
    meta], ...]`` EXCLUDING volatile fields (generated_at, git_commit, mtime_iso,
    error), serializes with ``json.dumps(payload, sort_keys=True,
    separators=(",", ":"), ensure_ascii=False)`` and returns the hex sha256.
    Deterministic across runs on an unchanged tree.
    """
    payload = _digest_payload(entries)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_summary(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return the summary block (CONTRACTS.md §5.2).

    Keys: total_files, total_bytes, by_category{}, by_ext{}, n_symlinks,
    n_broken_symlinks, n_errors. Single O(n) pass over entries (no O(n^2)).
    """
    total_files = 0
    total_bytes = 0
    by_category: Dict[str, int] = {}
    by_ext: Dict[str, int] = {}
    n_symlinks = 0
    n_broken_symlinks = 0
    n_errors = 0

    for e in entries:
        total_files += 1
        size = e.get("size_bytes")
        if isinstance(size, int):
            total_bytes += size

        category = e.get("category", "other")
        by_category[category] = by_category.get(category, 0) + 1

        ext = e.get("ext", "")
        by_ext[ext] = by_ext.get(ext, 0) + 1

        if e.get("is_symlink"):
            n_symlinks += 1
            if e.get("symlink_ok") is False:
                n_broken_symlinks += 1

        if e.get("error") is not None:
            n_errors += 1

    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "by_category": dict(sorted(by_category.items())),
        "by_ext": dict(sorted(by_ext.items())),
        "n_symlinks": n_symlinks,
        "n_broken_symlinks": n_broken_symlinks,
        "n_errors": n_errors,
    }


def _config_used(config: Any) -> Dict[str, Any]:
    """Echo a JSON-serializable view of the resolved config (CONTRACTS.md §5.2).

    Prefers ``config.raw`` (the fully-merged raw mapping) when present so the
    echoed config round-trips cleanly; otherwise reconstructs a serializable view
    from the Config attributes (compiled regexes / sets are made JSON-safe).
    """
    raw = getattr(config, "raw", None)
    if isinstance(raw, dict) and raw:
        try:
            json.dumps(raw)  # cheap serializability probe
            return raw
        except (TypeError, ValueError):
            pass  # fall through to attribute reconstruction

    out: Dict[str, Any] = {}
    prune = getattr(config, "prune_dirs", None)
    if prune is not None:
        out["prune_dirs"] = sorted(prune)
    for attr in (
        "skip_prefixes",
        "out_dirname",
        "ext_to_category",
        "csv_rowcount_max_bytes",
        "csvgz_rowcount_max_bytes",
        "json_parse_max_bytes",
        "agent_map_top_n_h5ad",
        "label_obs_substrings",
    ):
        val = getattr(config, attr, None)
        if val is None:
            continue
        if isinstance(val, tuple):
            val = list(val)
        out[attr] = val
    tags = getattr(config, "ontology_tags", None)
    if tags:
        # ontology_tags are (tag, compiled-regex) pairs; echo the pattern string.
        serial = []
        for item in tags:
            try:
                tag, pat = item
                pattern = getattr(pat, "pattern", str(pat))
                serial.append({"tag": tag, "pattern": pattern})
            except Exception:  # noqa: BLE001
                continue
        out["ontology_tags"] = serial
    return out


def build_manifest(
    root: Path,
    entries: List[Dict[str, Any]],
    config: Any,
    tool_version: str,
) -> Dict[str, Any]:
    """Assemble the full INDEX.json mapping (CONTRACTS.md §5.2).

    Sorts entries by path, builds summary, captures git_commit + generated_at
    (volatile), computes content_digest (volatile-free), and echoes
    config_used. Returns the dict; does NOT write it (see write_outputs).
    """
    sorted_entries = sorted(entries, key=lambda e: e.get("path", ""))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "root": str(Path(root).resolve()),
        "git_commit": git_commit(Path(root)),
        "tool_version": tool_version,
        "content_digest": compute_digest(sorted_entries),
        "config_used": _config_used(config),
        "summary": build_summary(sorted_entries),
        "entries": sorted_entries,
    }


def _atomic_write_text(path: Path, write_fn: Any) -> None:
    """Write ``path`` atomically: serialize via ``write_fn(fh)`` to a temp sibling
    on the SAME volume, then ``Path.replace`` (atomic ``os.replace``) into place.

    A concurrent reader — an agent ``grep``, the always-open app, or
    ``_peek_committed_digest`` — therefore never observes a truncated / half-written
    artifact during the ~0.9s rewrite, and a crash / drive-yank mid-write leaves the
    PREVIOUS complete file intact (crash-safe on the removable exFAT volume). The
    temp MUST be a sibling (same filesystem) so the rename is a true same-FS atomic
    replace — a cross-volume rename would raise ``EXDEV``. Any stale temp from a
    prior hard-kill is cleared first, and the temp is cleaned up on failure (on
    success ``replace`` has already consumed it).
    """
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            write_fn(fh)
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_outputs(manifest: Dict[str, Any], out_dir: Path) -> Dict[str, Path]:
    """Write INDEX.json and INDEX.jsonl into ``out_dir``; return their paths.

    INDEX.json is the full mapping (§5.2); INDEX.jsonl is one entry object per
    line, path-sorted, no wrapping array (§5.3). Creates out_dir if needed.
    Returns ``{"index_json": Path, "index_jsonl": Path}``.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    index_json = out_dir / "INDEX.json"
    index_jsonl = out_dir / "INDEX.jsonl"

    _atomic_write_text(
        index_json,
        lambda fh: (
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=False),
            fh.write("\n"),
        ),
    )

    entries = sorted(
        manifest.get("entries", []), key=lambda e: e.get("path", "")
    )

    def _emit_jsonl(fh: Any) -> None:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")

    _atomic_write_text(index_jsonl, _emit_jsonl)

    return {"index_json": index_json, "index_jsonl": index_jsonl}


def load_prior_entries(out_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load the previous INDEX.json's entries as a ``path -> entry`` cache.

    Returns ``{}`` if no prior index exists or it can't be read/parsed. Drives
    INCREMENTAL refresh: the walker reuses an entry whose ``(size_bytes,
    mtime_iso)`` are unchanged instead of re-extracting it (see
    ``walker.make_entry``). The prior INDEX.json IS the cache — no separate cache
    file is kept — so the cache can never disagree with the last published index.
    """
    index_json = Path(out_dir) / "INDEX.json"
    try:
        with index_json.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return {}
    cache: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        if isinstance(e, dict) and isinstance(e.get("path"), str):
            cache[e["path"]] = e
    return cache


# Entry fields that make an entry "changed" for the in-place tree delta. These are
# every field the HTML tree row / inspector renders off, MINUS the volatile ones a
# re-stat alone perturbs without a real content change: `generated_at`/`git_commit`
# are manifest-level (not per-entry); `error` is excluded because it's transient
# diagnostics, not a structural change. `meta` IS included (obs columns, n_obs, …
# drive the row). Compared with plain `==` (entries are JSON-only scalars/lists/
# dicts, so equality is value equality).
_DELTA_FIELDS = (
    "size_bytes",
    "mtime_iso",
    "extractor",
    "meta",
    "is_symlink",
    "symlink_target",
    "symlink_ok",
)


def compute_entry_delta(
    old_entries: Dict[str, Dict[str, Any]],
    new_entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Diff a path->entry OLD cache against the NEW entry list → an in-place delta.

    Returns ``{"added": [entry…], "changed": [entry…], "removed": [path…]}`` where:

    * ``added``   — entries whose ``path`` is in NEW but not OLD;
    * ``removed`` — paths in OLD but not NEW (just the path strings);
    * ``changed`` — same ``path`` present in both whose ``(size_bytes, mtime_iso,
      extractor, meta, is_symlink, symlink_target, symlink_ok)`` differ (``error``
      and the manifest-level volatiles are NOT diffed — a bare re-stat is a no-op).

    ``added`` and ``changed`` entries are PROJECTED through
    ``render_html._project_entry_for_html`` so their ``meta`` is truncated EXACTLY
    like the entries embedded in INDEX.html (wide ``columns`` → head + ``n_*_total``),
    keeping the delta byte-for-byte consistent with the page's ENTRIES model. The
    project import is lazy (cheap ``import repo_index.manifest``). ``old_entries`` is
    the shape returned by :func:`load_prior_entries`. Stdlib-only, pure (no I/O)."""
    from .render_html import _project_entry_for_html

    old = old_entries if isinstance(old_entries, dict) else {}
    new_by_path: Dict[str, Dict[str, Any]] = {}
    for e in new_entries or []:
        if isinstance(e, dict) and isinstance(e.get("path"), str):
            new_by_path[e["path"]] = e

    added: List[Dict[str, Any]] = []
    changed: List[Dict[str, Any]] = []
    for path, entry in new_by_path.items():
        prior = old.get(path)
        if prior is None:
            added.append(_project_entry_for_html(entry))
        elif any(prior.get(f) != entry.get(f) for f in _DELTA_FIELDS):
            changed.append(_project_entry_for_html(entry))

    removed = [p for p in old.keys() if p not in new_by_path]

    # Path-sorted for determinism (tests + a stable, reviewable payload).
    added.sort(key=lambda e: e.get("path", ""))
    changed.sort(key=lambda e: e.get("path", ""))
    removed.sort()
    return {"added": added, "changed": changed, "removed": removed}


def _peek_committed_digest(out_dir: Path) -> Optional[str]:
    """Cheaply read the committed ``content_digest`` from INDEX.json WITHOUT a
    full parse. The digest is the 6th top-level key of the pretty-printed file
    (after schema_version/generated_at/root/git_commit/tool_version), so it lives
    in the first few hundred bytes — reading the first 4 KB and regexing it out
    avoids loading the ~50 MB document just to compare a hash.

    Returns the 64-hex digest, or ``None`` if the file is absent / too short /
    unparseable (the caller then proceeds to write, never skips on uncertainty).
    Used by the skip-write-on-unchanged gate (cli.build_index) and by the app's
    refresh-was-a-no-op detection (app.Api.refresh).
    """
    index_json = Path(out_dir) / "INDEX.json"
    try:
        with index_json.open("r", encoding="utf-8") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    m = re.search(r'"content_digest"\s*:\s*"([0-9a-f]{64})"', head)
    return m.group(1) if m else None


def _peek_prior_index_columns(out_dir: Path) -> Optional[bool]:
    """Cheaply read the prior build's ``index_columns`` setting WITHOUT a full parse.

    ``index_columns`` is echoed inside ``config_used`` (manifest carries
    ``config.raw`` there, which contains the flag once it is in DEFAULTS/merged).
    ``config_used`` sits AFTER ``content_digest`` and is itself a sizeable nested
    block (prune_dirs, ext_to_category, ontology_tags …) but it precedes the huge
    ``entries`` array, so a bounded head read of the file finds the flag without
    json.loading the multi-MB document. The 4 KB cap of ``_peek_committed_digest``
    is too small here (the flag can sit deep inside config_used), so a larger head
    is read.

    Returns ``True`` / ``False`` when the key is found, or ``None`` when the file is
    absent / the key is not present (a legacy index predating the toggle — the
    caller treats ``None`` as True, since old builds always carried columns). Used
    by cli.build_index to force a full re-extract when the toggle flips.
    """
    index_json = Path(out_dir) / "INDEX.json"
    try:
        with index_json.open("r", encoding="utf-8") as fh:
            head = fh.read(262144)  # 256 KB — config_used precedes `entries`
    except OSError:
        return None
    m = re.search(r'"index_columns"\s*:\s*(true|false)', head)
    if not m:
        return None
    return m.group(1) == "true"


def _peek_html_digest(out_dir: Path) -> Optional[str]:
    """Cheaply read the ``content_digest`` embedded in INDEX.html's trailing
    ``<!-- digest: … -->`` marker WITHOUT loading the whole (multi-MB) file.

    ``render_html`` writes the digest as an HTML comment at the very END of the
    document (after ``</html>``, past the big embedded JSON blob + JS), so reading
    the last few KB and regexing it out is enough — no need to scan the ~10 MB page.

    Returns the 64-hex digest, or ``None`` if the file is absent / too short / the
    marker is missing. Used by the skip-write gate (``cli.build_index``) to detect
    when INDEX.html has fallen BEHIND INDEX.json — e.g. after an HTML-skipping
    ``query`` freshen (``write_html_artifact=False`` rewrites json/jsonl only) — so
    the HTML is regenerated rather than left stale. A stale INDEX.html otherwise
    silently feeds the always-open app a behind-the-times tree on a reload.
    """
    index_html = Path(out_dir) / "INDEX.html"
    try:
        with index_html.open("rb") as fh:
            try:
                fh.seek(-4096, 2)  # last 4 KB — the tail marker lives here
            except OSError:
                fh.seek(0)
            tail = fh.read()
    except OSError:
        return None
    m = re.search(rb"<!-- digest:\s*([0-9a-f]{64})\s*-->", tail)
    return m.group(1).decode("ascii") if m else None
