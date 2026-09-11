"""repo_index.selfheal — drift detection + pre-commit helper.

Re-walks the FS, recomputes the content_digest (volatile-free), compares to the
committed INDEX.json digest, and reports added/removed/changed paths on drift.
This is the self-healing mechanism (the pre-commit hook regenerates + stages the
artifacts so the committed index can never silently drift). Stdlib-only. See
CONTRACTS.md §7.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import manifest as _manifest


def _digest_triple(entry: Dict[str, Any]) -> Tuple[Any, Any, str]:
    """The digest-relevant comparison triple for one entry: (size_bytes,
    extractor, canonical-meta-json). ``meta`` is canonicalized to a stable string
    so two metas compare equal iff they serialize identically (CONTRACTS.md §7
    uses the same canonical form for the digest)."""
    meta_canon = json.dumps(
        entry.get("meta", {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return (entry.get("size_bytes"), entry.get("extractor"), meta_canon)


def diff_entries(
    committed: List[Dict[str, Any]],
    current: List[Dict[str, Any]],
) -> Tuple[List[str], List[str], List[str]]:
    """Return (added, removed, changed) path lists between two entry lists.

    "changed" = same path present in both but differing on the digest-relevant
    triple (size_bytes, extractor, meta). O(n) via dict-by-path (no O(n^2)).
    ``added`` = in current but not committed; ``removed`` = in committed but not
    current. All three lists are returned sorted.
    """
    committed_by_path = {e.get("path"): e for e in committed}
    current_by_path = {e.get("path"): e for e in current}

    committed_paths = set(committed_by_path)
    current_paths = set(current_by_path)

    added = sorted(current_paths - committed_paths)
    removed = sorted(committed_paths - current_paths)

    changed: List[str] = []
    for path in committed_paths & current_paths:
        if _digest_triple(committed_by_path[path]) != _digest_triple(
            current_by_path[path]
        ):
            changed.append(path)
    changed.sort()

    return added, removed, changed


def _load_committed(index_path: Path) -> Dict[str, Any]:
    """Load the committed INDEX.json mapping (stdlib json)."""
    with Path(index_path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _walk_current(root: Path, config: Any) -> List[Dict[str, Any]]:
    """Re-walk ``root`` and return the live entry list (walk + entries only, NOT
    a full re-render). Imported lazily so a missing/stub walker only matters when
    a live check is actually run."""
    from . import walker  # local import: avoid import cycle / stub-at-load issues

    return list(walker.walk(Path(root), config))


def recompute(root: Path, config: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Re-walk ``root`` and return ``(content_digest, entries)`` for the live FS.

    Reuses :func:`repo_index.walker.walk` + :func:`repo_index.manifest.compute_digest`
    so the recomputed digest is byte-for-byte the one a fresh build would write.
    This powers both :func:`check` and the pre-commit hook helper.
    """
    entries = _walk_current(root, config)
    return _manifest.compute_digest(entries), entries


def _broken_symlinks(entries: List[Dict[str, Any]]) -> set:
    """Set of rel paths that are symlinks with ``symlink_ok is False`` (broken)."""
    return {
        e.get("path")
        for e in entries
        if e.get("is_symlink") and e.get("symlink_ok") is False
    }


def diff_health(
    committed: List[Dict[str, Any]],
    current: List[Dict[str, Any]],
) -> Tuple[List[str], List[str]]:
    """Return (newly_broken, newly_repaired) symlink path lists between two sets.

    The content_digest (CONTRACTS.md §7) intentionally covers only
    ``(path, size_bytes, extractor, meta)``; a FILE symlink that flips healthy ->
    broken keeps the same lstat size (the target-path string length) and the same
    ``generic``/``{}`` extractor+meta, so the digest does not change. Broken
    symlinks are a declared first-class health signal, so ``check`` compares the
    broken-symlink path SETS directly to surface such health regressions that the
    digest alone is blind to. ``newly_broken`` = broken now but not before;
    ``newly_repaired`` = broken before but not now. Both returned sorted.
    """
    committed_broken = _broken_symlinks(committed)
    current_broken = _broken_symlinks(current)
    newly_broken = sorted(p for p in current_broken - committed_broken if p)
    newly_repaired = sorted(p for p in committed_broken - current_broken if p)
    return newly_broken, newly_repaired


def is_clean(root: Path, index_path: Path, config: Any) -> Tuple[
    bool, List[str], List[str], List[str], List[str], List[str]
]:
    """Pre-commit helper: return ``(clean, added, removed, changed, newly_broken,
    newly_repaired)``.

    Recomputes the live digest and compares it to the committed
    ``content_digest`` in ``index_path``; ALSO compares the broken-symlink path
    sets (a health signal the digest is blind to — see :func:`diff_health`).
    ``clean`` is True iff the digests match AND no symlink flipped broken/healthy.
    When dirty, the path-level diff lists are populated (against the committed
    ``entries``) so the hook can report exactly what drifted. Does not print or
    exit — callers (``check``, the hook) decide presentation.
    """
    committed = _load_committed(index_path)
    committed_digest = committed.get("content_digest")
    committed_entries = committed.get("entries", [])

    current_digest, current_entries = recompute(root, config)

    newly_broken, newly_repaired = diff_health(committed_entries, current_entries)
    health_clean = not newly_broken and not newly_repaired

    if current_digest == committed_digest and health_clean:
        return True, [], [], [], [], []

    added, removed, changed = diff_entries(committed_entries, current_entries)
    return False, added, removed, changed, newly_broken, newly_repaired


def check(root: Path, index_path: Path, config: Any) -> int:
    """Verify the committed index against the live FS; return an exit code.

    Re-walks ``root`` (walk + digest only, NOT a full re-render), recomputes the
    content_digest, and compares to the ``content_digest`` in the committed
    INDEX.json at ``index_path``. ALSO compares the broken-symlink path sets so a
    health regression the digest is blind to (a file symlink flipping
    broken/healthy) still trips the check. If clean: print ``OK`` and return 0.
    If drifted: print the added / removed / changed (and newly-broken /
    newly-repaired) path lists and return 1. See CONTRACTS.md §7.
    """
    clean, added, removed, changed, newly_broken, newly_repaired = is_clean(
        Path(root), Path(index_path), config
    )
    if clean:
        print("OK")
        return 0

    print("DRIFT: committed INDEX.json no longer matches the filesystem.")
    print(f"added ({len(added)}):")
    for p in added:
        print(f"  + {p}")
    print(f"removed ({len(removed)}):")
    for p in removed:
        print(f"  - {p}")
    print(f"changed ({len(changed)}):")
    for p in changed:
        print(f"  ~ {p}")
    print(f"newly broken symlinks ({len(newly_broken)}):")
    for p in newly_broken:
        print(f"  ✗ {p}")
    print(f"newly repaired symlinks ({len(newly_repaired)}):")
    for p in newly_repaired:
        print(f"  ✓ {p}")
    return 1
