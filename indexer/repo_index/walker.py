"""repo_index.walker — pruned, space-safe filesystem walk.

Hand-rolled directory recursion (topdown, ``followlinks=False`` semantics): prune
dirs BEFORE descending, skip ``._*`` basenames, record-not-traverse symlinks +
detect broken links via lstat, and run every file through extract_meta (never
raises out). Yields per-file entry dicts (CONTRACTS.md §5.1). Stdlib-only.

Two interchangeable per-directory listing primitives feed ONE shared classify/
emit gate (:func:`_process_child`), so the §0/§9 prune/skip/symlink policy is
identical regardless of which produced a child:

* W1 — ``os.scandir`` (replaces ``os.walk`` so the stat ``scandir`` already
  performs, PEP 471, is reused: each ``DirEntry`` carries a cached
  ``stat(follow_symlinks=False)`` + ``is_symlink()`` threaded into
  :func:`make_entry` via ``st``/``is_symlink_hint``).
* W4 (Axis A) — ONE ``getattrlistbulk(2)`` syscall batch per directory
  (:mod:`repo_index._bulkstat`), returning name + objtype + mtime + (file)
  datalength for ALL children at once. On exFAT (this volume, via fskit) every
  ``DirEntry`` is ``DT_UNKNOWN``, so the W1 path pays a per-child stat to resolve
  type; the bulk batch collapses that storm to ~1 syscall/dir. Darwin-only and
  fully import-guarded: on a non-Darwin build, on any ``OSError``
  (ENOTSUP/EINVAL/unreadable dir), or on a per-record decode anomaly the walk
  degrades to the W1 ``os.scandir`` path for that directory, so behaviour is
  never worse than W1. The FEW symlinks (~550 tree-wide) still go through
  ``os.lstat`` (make_entry self-stat) so their size/target/resolve-status stay
  exactly W1-correct (correctness-over-micro-opt). The garbage exFAT inode/FILEID
  is never requested or read — freshness keys only on (mtime, size).

``make_entry``'s standalone contract is kept: called without ``st`` it self-stats
via ``os.lstat`` as before. See CONTRACTS.md §9 for the full prune/skip/symlink
policy.

W6 (Axis A) — "parallel list / serial extract". The walk's dominant cost is the
per-directory listing+classification I/O (``_bulkstat.listdir_bulk`` ~1.78s of a
~2.19s warm walk); that I/O parallelises ~2.23x at 4 threads but REGRESSES at
>=8 (the USB/exFAT bus saturates). Extraction (``extract_meta`` ->
h5py/pyarrow) is NOT thread-safe, so it MUST stay single-threaded. When
``config.walk_threads > 1`` (and the tree is non-trivial) :func:`walk` therefore
runs the listing + the §0/§9 classify/descent gate on a small
``ThreadPoolExecutor`` (workers return plain ``(subdirs, emit-candidates,
error-entries)`` tuples — they NEVER call ``make_entry`` / ``extract_meta``) and
the MAIN thread calls :func:`make_entry` SERIALLY on every collected candidate,
so extraction is provably single-threaded. The shared classify gate
(:func:`_classify_child`) is identical to the serial path, so the parallel walk
emits the EXACT same entry SET (and the same order-independent ``content_digest``)
— only the emission ORDER may differ (the manifest path-sorts; §12).
``walk_threads <= 1`` keeps the serial recursion unchanged (no pool, no
behaviour change).
"""

from __future__ import annotations

import fnmatch
import os
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from . import _bulkstat
from .extractors.base import apply_config, extract_meta, ext_of

# W4 (Axis A): batched getattrlistbulk(2) listing replaces the per-entry stat
# storm on exFAT (DT_UNKNOWN). Import-guarded; only true where the libSystem
# symbol resolves (Darwin). When False, walk() uses the W1 os.scandir path
# globally; when True, any per-directory bulk failure still falls back to
# os.scandir for that directory (behaviour is never worse than W1).
_BULK_SUPPORTED = _bulkstat.SUPPORTED

# A link-node stat handed to make_entry: either a real os.stat_result (scandir)
# or the tiny mtime+size shim from the bulk path. make_entry reads only
# st.st_mtime / st.st_size, so the two are interchangeable there.
StatT = Union[os.stat_result, "_bulkstat._StatLike"]


def iso_mtime(ts: float) -> str:
    """Return an ISO-8601 UTC timestamp string (e.g. ``2026-06-01T10:00:00Z``)
    for a POSIX mtime float. Stdlib-only (datetime, timezone.utc).

    Truncates to whole seconds and emits a trailing ``Z`` (Zulu/UTC) rather than
    ``+00:00`` so the format matches CONTRACTS.md §5.1 exactly.
    """
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _starts_with_skip_prefix(name: str, config: Any) -> bool:
    """True if ``name`` starts with any configured skip prefix (e.g. ``._``)."""
    prefixes = getattr(config, "skip_prefixes", ("._",))
    return any(name.startswith(p) for p in prefixes)


def _matches_any(rel: str, globs: List[str]) -> bool:
    """True if the relative POSIX path ``rel`` matches any fnmatch glob in ``globs``.

    Uses :func:`fnmatch.fnmatch` over the POSIX relative path so patterns like
    ``"outputs/**"`` / ``"*.py"`` / ``"sub/data.csv"`` behave the way a user
    expects when filtering the walk (``--include`` / ``--exclude``).
    """
    return any(fnmatch.fnmatch(rel, pat) for pat in globs)


def _excluded(rel: str, exclude_globs: List[str]) -> bool:
    """True if ``rel`` is excluded by any ``exclude_globs`` pattern (exclude wins)."""
    return bool(exclude_globs) and _matches_any(rel, exclude_globs)


def _included(rel: str, include_globs: List[str]) -> bool:
    """True if ``rel`` is kept under ``include_globs`` (no includes set = keep all)."""
    return (not include_globs) or _matches_any(rel, include_globs)


def _dir_excluded(rel_dir: str, exclude_globs: List[str]) -> bool:
    """True if an entire directory subtree is excluded (prune-before-descend).

    A directory ``d`` is dropped from descent when an exclude pattern matches the
    directory's own relative path OR its ``d/**`` subtree form, so an excluded
    subtree is never walked at all (mirrors the prune_dirs behaviour for the
    user-supplied excludes).
    """
    if not exclude_globs:
        return False
    if _matches_any(rel_dir, exclude_globs):
        return True
    subtree = rel_dir.rstrip("/") + "/**"
    return _matches_any(subtree, exclude_globs)


def _is_racy(
    file_mtime: float, reference_time: Optional[float], config: Any
) -> bool:
    """True if ``file_mtime`` falls inside the racy dirty window (CONTRACTS.md §9).

    ``reference_time`` is the prior index's own generation time (POSIX float),
    typically the prior INDEX.json's mtime. A file whose mtime is greater than
    ``reference_time - racy_window_seconds`` was (or may have been) written within
    one coarse filesystem tick of the prior index and therefore cannot be trusted
    to differ from its cached size+mtime — it is racy and must be re-extracted.
    ``reference_time is None`` disables the rule (returns False).
    """
    if reference_time is None:
        return False
    window = getattr(config, "racy_window_seconds", 2)
    try:
        window = float(window)
    except (TypeError, ValueError):
        window = 2.0
    return file_mtime > reference_time - window


def _maybe_strip_columns(
    meta: Dict[str, Any], config: Any
) -> Dict[str, Any]:
    """Drop the wide ``columns`` list from ``meta`` when ``index_columns`` is off.

    The ``index_columns`` toggle (config / ``--no-columns``) is a FILE-SIZE lever:
    it removes the wide per-CSV/TSV/parquet ``columns`` list (the bulk of
    INDEX.json/.jsonl) while ALWAYS keeping the standalone ``n_columns`` COUNT and
    the tiny, search-critical h5ad ``obs_columns`` / ``var_columns`` lists (those
    live under different keys and are never touched here). ``columns`` is produced
    ONLY by the tabular extractor, so this is a no-op for every other entry.

    Returns ``meta`` UNCHANGED (same object, zero-cost) when the toggle is on or
    when ``columns`` is absent. When it must strip, it returns a SHALLOW COPY with
    ``columns`` removed and NEVER mutates the input — the cache-reuse path passes a
    dict shared by reference across the run (the parsed prior INDEX.json), so an
    in-place pop would corrupt the cache for other readers and make behavior
    order-dependent. ``getattr`` default True keeps this backward-safe for a config
    object that predates the field. See CONTRACTS.md §4.4.
    """
    if getattr(config, "index_columns", True):
        return meta
    if not isinstance(meta, dict) or "columns" not in meta:
        return meta
    return {k: v for k, v in meta.items() if k != "columns"}


#: Keys the SVG figure-text extractor contributes, stripped as a unit.
_FIGURE_TEXT_KEYS = ("figure_text", "figure_text_mode", "figure_text_truncated")


def _maybe_strip_figure_text(meta: Dict[str, Any], config: Any) -> Dict[str, Any]:
    """Drop the SVG ``figure_text`` keys from ``meta`` when the toggle is off.

    The extractor already short-circuits its (expensive) read when
    ``index_figure_text`` is off, so on the FRESH-extract path this is a no-op.
    It earns its keep on the CACHE-REUSE path: a prior toggle-ON build's meta is
    replayed from INDEX.json, and without this strip a toggle-OFF index would
    still carry figure text.

    Same discipline as :func:`_maybe_strip_columns` — returns ``meta`` unchanged
    (same object) when there is nothing to do, and otherwise a SHALLOW COPY, never
    an in-place pop: the cached dict is shared by reference across the run, so
    mutating it would corrupt other readers and make behavior order-dependent.
    """
    if getattr(config, "index_figure_text", True):
        return meta
    if not isinstance(meta, dict) or not any(k in meta for k in _FIGURE_TEXT_KEYS):
        return meta
    return {k: v for k, v in meta.items() if k not in _FIGURE_TEXT_KEYS}


def make_entry(
    abs_path: Path,
    root: Path,
    config: Any,
    cache: Optional[Dict[str, Dict[str, Any]]] = None,
    st: Optional[os.stat_result] = None,
    is_symlink_hint: Optional[bool] = None,
    reference_time: Optional[float] = None,
) -> Dict[str, Any]:
    """Build one entry dict (CONTRACTS.md §5.1) for ``abs_path``.

    Computes the relative POSIX path, ext (compound-aware via ext_of), category
    (config.category_for), lstat size/mtime (follow_symlinks=False so dangling
    links never raise), symlink fields (target/ok), ontology tags, then runs
    extract_meta to fill ``extractor``/``meta``/``error``. ``error`` is omitted
    when None.

    Stat reuse (PEP 471): when the caller already obtained the link-node
    ``os.stat_result`` (e.g. from ``os.scandir``'s cached
    ``DirEntry.stat(follow_symlinks=False)``) it passes it as ``st`` together with
    ``is_symlink_hint`` (``DirEntry.is_symlink()``), so this function does NOT
    re-issue ``abs_path.is_symlink()`` + ``os.lstat`` — the common walk path costs
    ~0 explicit ``os.lstat`` calls. When ``st`` is None (the standalone contract)
    it falls back to ``abs_path.is_symlink()`` + ``os.lstat`` exactly as before,
    so calling ``make_entry(path, root, config)`` directly still works.

    Broken-symlink safety: all stat-ing uses ``follow_symlinks=False`` semantics
    (``os.lstat`` / ``DirEntry.stat(follow_symlinks=False)``), never following the
    link, so a dangling link yields a valid entry (``symlink_ok=False``, size from
    the link itself) rather than raising. For ANY symlink (broken OR resolving)
    the extractor is NOT invoked: the link is recorded as ``generic`` with
    ``meta={}`` per the record-not-traverse invariant (§0.6/§9). Running the
    extractor on a resolving link would follow it to the target and re-index that
    target's metadata under the link path — while ``size_bytes`` comes from the
    link node itself, producing a size/meta mismatch and double-indexing any
    target that is also reached directly by the walk.

    Racy dirty window (CONTRACTS.md §9): when ``reference_time`` is supplied (the
    prior index's own generation time, as a POSIX float), a cached entry whose
    file mtime is within ``config.racy_window_seconds`` of that reference time is
    treated as DIRTY and re-extracted, never cache-reused — closing the
    same-coarse-tick/same-size blind spot of low-resolution filesystem timestamps
    (exFAT 2s mtime granularity). ``None`` disables the rule.
    """
    # Relative POSIX path from root (space-safe; pathlib only).
    rel = abs_path.relative_to(root).as_posix()

    ext = ext_of(abs_path)
    category = config.category_for(ext)

    symlink_target: Any = None
    symlink_ok: Any = None

    # Source the link-node stat. Prefer the scandir-cached stat_result + symlink
    # flag threaded by the walk (no extra syscall); else self-stat with os.lstat
    # (follow_symlinks=False semantics) so the standalone contract holds and a
    # dangling link still never raises.
    if st is None:
        is_symlink = abs_path.is_symlink()
        st = os.lstat(abs_path)
    else:
        is_symlink = (
            is_symlink_hint
            if is_symlink_hint is not None
            else abs_path.is_symlink()
        )
    file_mtime = st.st_mtime
    size_bytes = int(st.st_size)
    mtime = iso_mtime(file_mtime)

    if is_symlink:
        try:
            symlink_target = os.readlink(abs_path)
        except OSError:
            symlink_target = None
        # exists() follows the link: True => target resolves, False => broken.
        symlink_ok = bool(abs_path.exists())

    entry: Dict[str, Any] = {
        "path": rel,
        "category": category,
        "ext": ext,
        "size_bytes": size_bytes,
        "mtime_iso": mtime,
        "is_symlink": is_symlink,
        "symlink_target": symlink_target,
        "symlink_ok": symlink_ok,
        "tags": config.tag(rel),
    }

    # Record-not-traverse: a symlink (broken OR resolving) is NEVER passed to an
    # extractor. The extractor would follow the link to its target and re-index
    # the target's metadata under this link path (with size_bytes still from the
    # link node), which both double-indexes any directly-walked target and emits a
    # size/meta mismatch. Symlinks are recorded as generic/{} per §0.6/§9/§5.1.
    if is_symlink:
        entry["extractor"] = "generic"
        entry["meta"] = {}
        return entry

    # Incremental reuse: if a prior index cached this exact path with the same
    # size + mtime and no recorded error, the file is unchanged — reuse its
    # extracted metadata instead of re-opening it. This is what turns a full
    # re-extract (minutes) into a stat-only diff (~1s) in steady state. A prior
    # ERROR is deliberately NOT reused (always retried, in case it was a
    # transient lock or a mid-write that has since completed with the same mtime).
    #
    # Racy guard (git index-format "racy" fix; Mercurial dirstate-v2
    # MTIME_SECOND_AMBIGUOUS): a file written WITHIN a coarse-granularity tick of
    # the prior index's own generation time can carry the SAME mtime AND the SAME
    # size as the cached entry yet differ in content (exFAT's 2s mtime
    # granularity makes a sub-tick write invisible to the size+mtime gate). When
    # such a file's mtime is within ``racy_window_seconds`` of the reference time,
    # treat it as DIRTY (re-extract) rather than trusting the cache.
    if cache is not None and not _is_racy(file_mtime, reference_time, config):
        prior = cache.get(rel)
        if (
            prior is not None
            and prior.get("size_bytes") == size_bytes
            and prior.get("mtime_iso") == mtime
            and prior.get("extractor") is not None
            and "error" not in prior
        ):
            entry["extractor"] = prior.get("extractor")
            # Strip on a COPY (never mutate the shared cached meta) when the
            # index_columns toggle is off — see _maybe_strip_columns.
            entry["meta"] = _maybe_strip_figure_text(
                _maybe_strip_columns(prior.get("meta", {}), config), config
            )
            return entry

    extractor_name, meta, error = extract_meta(abs_path)
    entry["extractor"] = extractor_name
    entry["meta"] = _maybe_strip_figure_text(_maybe_strip_columns(meta, config), config)
    if error is not None:
        entry["error"] = error
    return entry


def walk(
    root: Path,
    config: Any,
    out_dir: Optional[Path] = None,
    cache: Optional[Dict[str, Dict[str, Any]]] = None,
    reference_time: Optional[float] = None,
) -> Iterator[Dict[str, Any]]:
    """Walk ``root`` and yield an entry dict per file (CONTRACTS.md §5.1, §9).

    Hand-rolled ``os.scandir`` recursion (topdown, ``followlinks=False``
    semantics) replacing ``os.walk`` so the stat ``scandir`` already performs
    (PEP 471) is reused instead of discarded: each ``DirEntry`` carries a cached
    ``stat(follow_symlinks=False)`` + ``is_symlink()`` that are threaded into
    :func:`make_entry`, removing the two explicit ``os.lstat`` syscalls the old
    ``os.walk`` path paid per file (``abs_path.is_symlink()`` + ``os.lstat``).

    Before descending, each directory's children are classified so pruned/skip/
    symlinked dirs are never descended into (all checks BEFORE any stat/extract
    where the invariant requires it):

    - any basename in ``config.prune_dirs`` is dropped (prune-before-descend);
    - any basename matching a ``config.skip_prefixes`` prefix (``._``) is dropped
      FIRST, before any stat/extract (mandatory, §0.4 — files AND dirs);
    - the OUT dir (compared by realpath) is never descended into / indexed, even
      under a custom ``--out`` inside root;
    - any directory whose relative path matches a ``config.exclude_globs`` pattern
      (or its ``d/**`` subtree form) is dropped from descent so an excluded
      subtree is never walked (exclude-before-descend, mirroring prune_dirs);
    - any symlinked directory is dropped from descent BUT still emitted as an
      entry (record-not-traverse, §9) so the symlink itself is indexed and broken
      links are surfaced — without double-counting the symlink-heavy
      ``important_assets`` tree or following self-referential cycles.

    For files: ``._``-prefixed basenames are skipped; files whose relative path is
    matched by ``config.exclude_globs`` (exclude wins) or not matched by a
    non-empty ``config.include_globs`` are dropped; every remaining file is turned
    into an entry via :func:`make_entry` inside a per-file try/except, so one
    bad/locked/dangling file never aborts the walk (a failure is yielded as a
    minimal entry carrying an ``error`` string).

    A directory that cannot be listed (``os.scandir`` raises ``OSError``, e.g.
    permission denied) is surfaced as a synthetic ``error`` entry rather than
    being silently dropped, so an inaccessible subtree shows up as a health signal
    (counts toward ``n_errors`` / ``query errors``) instead of vanishing.

    ``reference_time`` (the prior index's generation time, POSIX float) is
    threaded into :func:`make_entry` to drive the racy dirty window (§9); ``None``
    disables it.

    W6 (Axis A): when ``config.walk_threads > 1`` the dominant per-directory
    LISTING + child-classification I/O is fanned across a small thread pool while
    :func:`make_entry` (the only extracting call) stays SERIAL on this thread —
    see :func:`_walk_parallel` and the module docstring. ``walk_threads <= 1``
    keeps the serial recursion (:func:`_descend`) unchanged. Both paths route
    every child through the SAME :func:`_classify_child` gate, so the parallel
    walk yields the identical entry SET / ``content_digest`` (only the emission
    ORDER may differ; the manifest path-sorts, §7/§12).
    """
    root = Path(root)
    prune_dirs = set(getattr(config, "prune_dirs", set()))
    # Resolve the output directory so we never descend into / index our own
    # artifacts even when --out points at a custom path INSIDE root (basename
    # pruning only catches the default name). Compared by realpath below.
    out_real: Optional[str] = None
    if out_dir is not None:
        try:
            out_real = os.path.realpath(out_dir)
        except Exception:  # noqa: BLE001
            out_real = None
    # Cheap pre-filter for the per-directory OUT-dir guard below. os.path.realpath
    # resolves EVERY path component via lstat, so calling it on every one of the
    # ~4.4k directories is the DOMINANT explicit-lstat cost on this deep tree
    # (~37k lstats — measured). A real directory keeps its own basename under
    # realpath (only ancestry symlinks are resolved), so realpath(abs_path) can
    # equal out_real ONLY when the dir's own name equals out_real's basename. Gate
    # the realpath on that O(1) string check so it runs for ~1 dir, not all 4.4k.
    out_basename: Optional[str] = (
        os.path.basename(out_real) if out_real is not None else None
    )
    include_globs: List[str] = list(getattr(config, "include_globs", []) or [])
    exclude_globs: List[str] = list(getattr(config, "exclude_globs", []) or [])

    # Wire config-driven thresholds (csv/json size gates, incl. CLI overrides) into
    # the registered extractor singletons ONCE before walking (the frozen
    # extract_meta(path) signature takes no config). No-op for config-less ones.
    apply_config(config)

    def _rel_of(abs_path: Path, fallback: str) -> str:
        try:
            return abs_path.relative_to(root).as_posix()
        except Exception:  # noqa: BLE001
            return fallback

    def _emit_file(
        abs_path: Path, st: Optional[os.stat_result], is_link: bool
    ) -> Iterator[Dict[str, Any]]:
        """Yield the entry for an ALREADY-GATED file/symlink node (§0.2 try/except).

        The include/exclude gate is applied UPSTREAM in :func:`_classify_child`, so
        this helper only turns a passed candidate into an entry, wrapping
        :func:`make_entry` in the per-file try/except so one bad/locked/dangling
        file never aborts the walk (a failure becomes a minimal ``error`` entry).
        """
        try:
            yield make_entry(
                abs_path,
                root,
                config,
                cache=cache,
                st=st,
                is_symlink_hint=is_link,
                reference_time=reference_time,
            )
        except Exception as exc:  # noqa: BLE001 - one bad file != abort
            yield _error_entry(abs_path, root, config, exc)

    def _classify_child(
        dir_path: Path,
        name: str,
        is_link: bool,
        is_real_dir: bool,
        st: Optional[StatT],
    ) -> Tuple[str, Any]:
        """Apply the §0/§9 gate to ONE already-classified child; return a verdict.

        This is the SINGLE source of truth for the prune / out-dir / exclude /
        symlink record-not-traverse / include-exclude policy. It is shared by BOTH
        the serial driver (:func:`_process_child`) and the W6 parallel listing
        worker (:func:`_list_dir`), so the two paths apply byte-identical policy —
        the parallel walk can therefore only differ from the serial walk in
        emission ORDER, never in the entry SET (§7/§12). It performs NO extraction
        and NEVER calls :func:`make_entry` (safe to run on a worker thread).

        ``._``-prefixed names are dropped by the caller BEFORE this point
        (mandatory §0.4 skip-before-stat). Returns one of:

        * ``("descend", abs_path)`` — a real subdirectory that survived the descent
          gate and must be walked;
        * ``("emit", (abs_path, st, is_link))`` — a file / symlink-to-file / broken
          symlink / symlink-to-dir / other node that passed include-exclude and is
          to be turned into a single entry via :func:`make_entry` (SERIALLY);
        * ``("skip", None)`` — a pruned/out/excluded dir, an excluded file, or an
          excluded symlinked dir: produce nothing.

        ``st`` is the link-node stat (``follow_symlinks=False`` semantics): an
        ``os.stat_result`` from scandir, a tiny ``_StatLike`` (mtime+size) from the
        bulk path, or ``None`` (make_entry then self-stats via ``os.lstat``). For
        symlinks ``st`` is deliberately ``None`` on the bulk path so make_entry
        self-stats the link node, keeping ``size_bytes`` / ``symlink_target`` /
        ``symlink_ok`` exactly W1-correct. The ``abs_path.is_dir()`` follow used to
        detect a symlinked-dir is a read-only stat — thread-safe.
        """
        abs_path = dir_path / name

        if is_real_dir:
            # --- directory descent gate (prune/out-dir/exclude) ---
            if name in prune_dirs:
                return ("skip", None)  # never descend into a pruned dir
            # Only a basename match can possibly resolve to the OUT dir (see the
            # out_basename rationale in walk()); skip the costly realpath for the
            # ~4.4k dirs that cannot match. Behaviour is identical to the prior
            # unconditional realpath — just far fewer lstat syscalls.
            if out_real is not None and name == out_basename:
                try:
                    if os.path.realpath(abs_path) == out_real:
                        return ("skip", None)  # never index the OUT dir itself
                except OSError:
                    pass
            rel_dir = _rel_of(abs_path, name)
            if _dir_excluded(rel_dir, exclude_globs):
                return ("skip", None)  # exclude-before-descend: never walk subtree
            return ("descend", abs_path)

        # Not a real directory. A symlink that RESOLVES to a directory is recorded
        # as a single entry but NOT descended into (record-not-traverse, §9).
        if is_link:
            try:
                links_to_dir = abs_path.is_dir()  # follow_symlinks=True
            except OSError:
                links_to_dir = False
            if links_to_dir:
                # Symlinked dir: emit-not-descend. Subject BOTH to its dir-subtree
                # exclude check AND — exactly like the pre-W6 _emit_file gate it
                # used to flow through — to the file include/exclude gate, so a
                # non-empty include that does not match the link path drops it.
                rel_dir = _rel_of(abs_path, name)
                if _dir_excluded(rel_dir, exclude_globs):
                    return ("skip", None)
                if _excluded(rel_dir, exclude_globs) or not _included(
                    rel_dir, include_globs
                ):
                    return ("skip", None)
                return ("emit", (abs_path, st, True))

        # Regular file, symlink-to-file, broken symlink, or other node. The file
        # include/exclude gate (exclude wins; empty include = keep all) is applied
        # here so the candidate emerging from _classify_child is always one to emit.
        rel = _rel_of(abs_path, name)
        if _excluded(rel, exclude_globs) or not _included(rel, include_globs):
            return ("skip", None)
        return ("emit", (abs_path, st, is_link))

    def _process_child(
        dir_path: Path,
        name: str,
        is_link: bool,
        is_real_dir: bool,
        st: Optional[StatT],
        subdirs: List[Path],
    ) -> Iterator[Dict[str, Any]]:
        """Serial driver: classify ONE child via :func:`_classify_child`, then act.

        Real dirs that survive the gate are appended to ``subdirs`` (descended
        after this dir's own files, topdown); emit candidates are turned into
        entries inline via :func:`_emit_file`. Identical §0/§9 policy to the W6
        parallel path because both route the decision through
        :func:`_classify_child`.
        """
        verdict, payload = _classify_child(dir_path, name, is_link, is_real_dir, st)
        if verdict == "descend":
            subdirs.append(payload)
        elif verdict == "emit":
            cand_path, cand_st, cand_link = payload
            yield from _emit_file(cand_path, cand_st, cand_link)
        # "skip": nothing to do.

    def _descend_scandir(dir_path: Path) -> Iterator[Dict[str, Any]]:
        """List ``dir_path`` via ``os.scandir`` (the W1 primitive / global fallback).

        scandir's cached per-entry stat (PEP 471) is reused for size/mtime/symlink
        classification. A failed listing becomes a synthetic error entry
        (§0.10/§9) instead of silently dropping the subtree.
        """
        try:
            scan = os.scandir(dir_path)
        except OSError as exc:
            # Unreadable dir: surface as a health signal, never silently drop.
            yield _error_entry(dir_path, root, config, exc)
            return

        subdirs: List[Path] = []
        with scan:
            for entry in scan:
                name = entry.name
                # MANDATORY ._ skip BEFORE any stat/extract — files AND dirs (§0.4).
                if _starts_with_skip_prefix(name, config):
                    continue
                # Classify without following links. is_dir(follow_symlinks=False)
                # is True only for a REAL directory node; a symlink that resolves to
                # a dir is caught inside _process_child and emitted-not-descended.
                try:
                    is_link = entry.is_symlink()
                except OSError:
                    is_link = False
                try:
                    is_real_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_real_dir = False
                # Reuse the scandir-cached link-node stat (no extra os.lstat).
                st: Optional[StatT]
                if is_real_dir:
                    st = None  # dirs never reach make_entry
                else:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        st = None
                yield from _process_child(
                    dir_path, name, is_link, is_real_dir, st, subdirs
                )

        for sub in subdirs:
            yield from _descend(sub)

    def _descend_bulk(dir_path: Path) -> Iterator[Dict[str, Any]]:
        """List ``dir_path`` via ONE ``getattrlistbulk(2)`` batch (W4, Darwin only).

        Collapses the W1 per-entry ``lstat`` storm (DT_UNKNOWN on exFAT) into one
        batched syscall returning name + objtype + mtime + (file) datalength for
        ALL children at once. Any failure — ``OSError`` (ENOTSUP/EINVAL/listing
        denied) or a per-record decode anomaly (``ValueError``) — degrades this
        directory to the ``os.scandir`` path so behaviour is never worse than W1.
        ``getattrlistbulk`` gives us mtime+size but NOT whether a symlink resolves
        to a dir; for the FEW symlinks (~550 tree-wide) we hand ``st=None`` to
        ``_process_child`` so make_entry self-stats the link via ``os.lstat`` and
        ``size_bytes``/``symlink_target``/``symlink_ok``/resolve-status stay
        exactly W1-correct (correctness-over-micro-opt). The inode/FILEID is never
        requested or read (exFAT returns garbage; §9 keys only on mtime+size).
        """
        subdirs: List[Path] = []
        try:
            children = list(_bulkstat.listdir_bulk(os.fspath(dir_path)))
        except OSError as exc:
            # ENOTSUP/EINVAL (fs without getattrlistbulk) OR an unreadable dir.
            # Distinguish: if scandir can also not open it, it is a genuine
            # unreadable dir -> synthetic error entry (§0.10/§9); otherwise the
            # bulk primitive is simply unsupported here -> fall back silently.
            try:
                os.close(os.open(os.fspath(dir_path), os.O_RDONLY))
            except OSError:
                yield _error_entry(dir_path, root, config, exc)
                return
            yield from _descend_scandir(dir_path)
            return
        except ValueError:
            # Per-record decode anomaly: never emit a corrupt entry — re-list the
            # whole directory with the trusted scandir path.
            yield from _descend_scandir(dir_path)
            return

        for name, kind, st_like in children:
            # MANDATORY ._ skip BEFORE any stat/extract — files AND dirs (§0.4).
            if _starts_with_skip_prefix(name, config):
                continue
            is_real_dir = kind == "dir"
            is_link = kind == "symlink"
            # Files/others: use the bulk mtime+size shim. Symlinks: st=None so
            # make_entry self-stats the link node (W1-exact size/target/ok). Dirs:
            # never reach make_entry.
            st: Optional[StatT] = None if (is_real_dir or is_link) else st_like
            yield from _process_child(
                dir_path, name, is_link, is_real_dir, st, subdirs
            )

        for sub in subdirs:
            yield from _descend(sub)

    # Pick the listing primitive ONCE: the W4 bulk batch on a supporting Darwin
    # build, else the W1 scandir path. Per-directory failures inside the bulk path
    # fall back to scandir for that directory, so a partial-support filesystem
    # never degrades below W1.
    _descend = _descend_bulk if _BULK_SUPPORTED else _descend_scandir

    # ------------------------------------------------------------------ #
    # W6 (Axis A) — parallel LISTING / serial EXTRACT.
    #
    # The listing-only workers below mirror the EXACT classification of
    # _descend_scandir / _descend_bulk (same ._ skip, same primitive + fallback,
    # same _classify_child gate) but instead of recursing + yielding entries they
    # RETURN a (subdirs, candidates, errors) triple for ONE directory and call
    # NOTHING that extracts. The MAIN thread (the parallel driver) then calls
    # make_entry on each candidate SERIALLY, so extraction is provably
    # single-threaded even though listing fans out across the pool.
    # ------------------------------------------------------------------ #
    _ListResult = Tuple[List[Path], List[Tuple[Path, Optional[StatT], bool]], List[Dict[str, Any]]]

    def _list_dir_scandir(dir_path: Path) -> "_ListResult":
        """LISTING-ONLY mirror of :func:`_descend_scandir` (no recurse, no extract).

        Returns ``(subdirs, candidates, errors)`` for ONE directory. An unreadable
        dir becomes a synthetic error entry (§0.10/§9) in ``errors``. Runs on a
        worker thread: every call here (``os.scandir``, ``DirEntry`` stat/symlink
        probes, the ``_classify_child`` realpath/is_dir follows) is a read-only
        filesystem op — none touches the extractor singletons or global state.
        """
        subdirs: List[Path] = []
        candidates: List[Tuple[Path, Optional[StatT], bool]] = []
        errors: List[Dict[str, Any]] = []
        try:
            scan = os.scandir(dir_path)
        except OSError as exc:
            errors.append(_error_entry(dir_path, root, config, exc))
            return subdirs, candidates, errors

        with scan:
            for entry in scan:
                name = entry.name
                # MANDATORY ._ skip BEFORE any stat/extract — files AND dirs (§0.4).
                if _starts_with_skip_prefix(name, config):
                    continue
                try:
                    is_link = entry.is_symlink()
                except OSError:
                    is_link = False
                try:
                    is_real_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_real_dir = False
                st: Optional[StatT]
                if is_real_dir:
                    st = None
                else:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        st = None
                verdict, payload = _classify_child(
                    dir_path, name, is_link, is_real_dir, st
                )
                if verdict == "descend":
                    subdirs.append(payload)
                elif verdict == "emit":
                    candidates.append(payload)
        return subdirs, candidates, errors

    def _list_dir_bulk(dir_path: Path) -> "_ListResult":
        """LISTING-ONLY mirror of :func:`_descend_bulk` (no recurse, no extract).

        Same getattrlistbulk(2) batch + same degradation policy as the serial bulk
        path: an OSError on an openable dir means the fs lacks the primitive (fall
        back to the scandir listing), an OSError on an un-openable dir is a genuine
        unreadable dir (synthetic error entry), and a per-record ValueError
        degrades the WHOLE directory to the scandir listing. Returns ``(subdirs,
        candidates, errors)``; calls NOTHING that extracts.
        """
        subdirs: List[Path] = []
        candidates: List[Tuple[Path, Optional[StatT], bool]] = []
        errors: List[Dict[str, Any]] = []
        try:
            children = list(_bulkstat.listdir_bulk(os.fspath(dir_path)))
        except OSError as exc:
            try:
                os.close(os.open(os.fspath(dir_path), os.O_RDONLY))
            except OSError:
                errors.append(_error_entry(dir_path, root, config, exc))
                return subdirs, candidates, errors
            return _list_dir_scandir(dir_path)
        except ValueError:
            return _list_dir_scandir(dir_path)

        for name, kind, st_like in children:
            # MANDATORY ._ skip BEFORE any stat/extract — files AND dirs (§0.4).
            if _starts_with_skip_prefix(name, config):
                continue
            is_real_dir = kind == "dir"
            is_link = kind == "symlink"
            st: Optional[StatT] = None if (is_real_dir or is_link) else st_like
            verdict, payload = _classify_child(
                dir_path, name, is_link, is_real_dir, st
            )
            if verdict == "descend":
                subdirs.append(payload)
            elif verdict == "emit":
                candidates.append(payload)
        return subdirs, candidates, errors

    _list_dir = _list_dir_bulk if _BULK_SUPPORTED else _list_dir_scandir

    def _walk_parallel(workers: int) -> Iterator[Dict[str, Any]]:
        """Drive the parallel-list / serial-extract walk (W6).

        WORKER threads run :func:`_list_dir` (listing + the §0/§9 classify gate)
        on a small ``ThreadPoolExecutor``; the MAIN thread (this generator) drains
        their ``(subdirs, candidates, errors)`` results, fans new subdirectories
        back into the pool, accumulates every emit-candidate, and AFTER the tree is
        fully listed calls :func:`make_entry` on each candidate SERIALLY (the ONLY
        place extraction runs — guaranteeing single-threaded h5py/pyarrow access).
        Error entries (unreadable dirs, §0.10) are surfaced too. The in-flight
        future set is bounded (``submit_cap``) so a deep tree never schedules every
        directory at once. Emission ORDER may differ from the serial topdown walk;
        the entry SET and the order-independent ``content_digest`` are identical
        (§7/§12), and the manifest path-sorts entries downstream.
        """
        candidates: List[Tuple[Path, Optional[StatT], bool]] = []
        error_entries: List[Dict[str, Any]] = []
        # Bound concurrently-submitted futures to keep the futures set small on a
        # deep tree (candidates themselves are tiny tuples). A few * workers keeps
        # every worker fed without materialising the whole frontier at once.
        submit_cap = max(2 * workers, workers + 4)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = deque([root])  # directories not yet submitted
            in_flight = set()        # Futures currently running _list_dir

            def _fill() -> None:
                while pending and len(in_flight) < submit_cap:
                    in_flight.add(pool.submit(_list_dir, pending.popleft()))

            _fill()
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in done:
                    in_flight.discard(fut)
                    subdirs, cands, errs = fut.result()
                    candidates.extend(cands)
                    error_entries.extend(errs)
                    pending.extend(subdirs)
                _fill()

        # SERIAL extraction on the main thread — the ONLY make_entry call site in
        # the parallel path. Listing is fully done; nothing else runs concurrently.
        for abs_path, st, is_link in candidates:
            yield from _emit_file(abs_path, st, is_link)
        yield from error_entries

    # walk_threads <= 1 (or a tiny/leaf tree) keeps the SERIAL recursion unchanged
    # — no pool, no thread, byte-identical to the pre-W6 behaviour. Otherwise fan
    # the listing across the pool while extraction stays serial on this thread.
    walk_threads = int(getattr(config, "walk_threads", 1) or 1)
    if walk_threads <= 1:
        yield from _descend(root)
    else:
        yield from _walk_parallel(walk_threads)


def _error_entry(
    abs_path: Path, root: Path, config: Any, exc: BaseException
) -> Dict[str, Any]:
    """Build a minimal, schema-valid entry for a file whose ``make_entry`` failed.

    Used only when even the cheap lstat/path handling in :func:`make_entry`
    raised (e.g. a permission-denied or vanished node). Fills every required §5.1
    field with safe defaults and records the failure in ``error`` so the walk
    surfaces it instead of crashing.
    """
    try:
        rel = abs_path.relative_to(root).as_posix()
    except Exception:  # noqa: BLE001
        rel = abs_path.name
    try:
        ext = ext_of(abs_path)
    except Exception:  # noqa: BLE001
        ext = ""
    try:
        category = config.category_for(ext)
    except Exception:  # noqa: BLE001
        category = "other"
    return {
        "path": rel,
        "category": category,
        "ext": ext,
        "size_bytes": 0,
        "mtime_iso": iso_mtime(0.0),
        "is_symlink": False,
        "symlink_target": None,
        "symlink_ok": None,
        "extractor": "generic",
        "tags": [],
        "meta": {},
        "error": f"{type(exc).__name__}: {exc}",
    }
