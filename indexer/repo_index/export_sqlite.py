"""repo_index.export_sqlite — DISASTER-RECOVERY-ONLY INDEX.sqlite builder.

A DERIVED artifact — NOT one of the frozen INDEX.json / INDEX.jsonl / INDEX.html
outputs (CONTRACTS.md §5/§8). It is a single SQLite file with:

  * an ``entries`` table (one row per file; the full per-entry ``meta`` is stored
    as a JSON string, read ONLY when a row's inspector opens, plus denormalized
    ``n_obs`` / ``n_vars`` so the matrix strip + shape cells are an index scan, no
    JSON parse); and
  * a contentless FTS5 virtual table over a precomputed ``searchtext`` (path +
    category + ext + tags + flattened meta — the same haystack the HTML ``hayOf()``
    builds), so path/obs/column search is sub-millisecond.

**Demoted to a flock-guarded disaster-recovery path (PHASE_0_1_SPEC.md §0.5D /
§3.6).** In NORMAL operation Rust is the SOLE writer of the live
``INDEX.sqlite`` — it owns the v2 schema, directory-tree synthesis, and
``path_key``/``parent_key``/``name_key`` computation, and ingests the
Python-emitted flat JSONL manifest into the live index in one
``BEGIN IMMEDIATE … COMMIT`` (§2.6 Path A). Python no longer writes the live
sqlite while the app may be running: a whole-file ``os.replace`` would orphan
the name-associated ``-wal``/``-shm`` of an OPEN WAL database — corruption
risk. :func:`write_sqlite` therefore stays the CURRENT FLAT (v1-shape) schema
below — no dir rows, no ``is_dir``/``path_key``/``parent_key``/``name_key``,
no v2 logic of any kind (that is Rust-owned, §0.5D) — and additionally REFUSES
to run while a live Lens writer holds the index (§_acquire_writer_lock).

It exists to back a low-memory native viewer (an ``NSOutlineView`` reading rows on
demand never holds the 21k-object graph the WebView does) and to give agents an
indexed query surface without loading the whole index.

Stdlib-only: uses the standard-library :mod:`sqlite3` (+ :mod:`fcntl` for the
writer-lock guard, POSIX-only — this tool targets macOS). FTS5 is compiled into
the SQLite that ships with CPython on macOS (verified in both the system and
conda interpreters), so this satisfies CONTRACTS §0.1 (stdlib-only core) — it is
NOT a third-party dependency. INDEX.json / INDEX.jsonl remain the authoritative,
full-fidelity, schema-frozen artifacts; INDEX.sqlite is regenerable and is written
into the (gitignored) ``_repo_index`` dir, so it is never committed.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


_SCHEMA = """
CREATE TABLE entries(
  id INTEGER PRIMARY KEY,
  path TEXT, category TEXT, ext TEXT,
  size_bytes INTEGER, mtime_iso TEXT,
  is_symlink INTEGER, symlink_target TEXT, symlink_ok INTEGER,
  extractor TEXT, tags TEXT, error TEXT,
  n_obs INTEGER, n_vars INTEGER, meta TEXT,
  figure_text TEXT
);
CREATE INDEX idx_entries_path ON entries(path);
CREATE INDEX idx_entries_cat ON entries(category);
CREATE INDEX idx_entries_nobs ON entries(n_obs DESC);
CREATE VIRTUAL TABLE fts USING fts5(
  path, searchtext, content='', tokenize='unicode61'
);
"""


def _flatten_meta(meta: Any, out: List[str]) -> None:
    """Append every string / scalar leaf and dict KEY in ``meta`` to ``out``.

    Mirrors the HTML ``hayOf()`` flatten so native FTS search covers the same
    surface as the in-page search (obs_columns / columns / defs / keys …). No
    recursion limit is needed — extractor meta is shallow (CONTRACTS §4).
    """
    if meta is None:
        return
    if isinstance(meta, str):
        out.append(meta)
        return
    if isinstance(meta, bool):  # bool before int (bool is an int subclass)
        out.append(str(meta))
        return
    if isinstance(meta, (int, float)):
        out.append(str(meta))
        return
    if isinstance(meta, list):
        for v in meta:
            _flatten_meta(v, out)
        return
    if isinstance(meta, dict):
        for k, v in meta.items():
            out.append(str(k))
            _flatten_meta(v, out)


#: Keys the SVG figure-text extractor contributes. They are lifted OUT of the
#: meta blob here and into the dedicated ``entries.figure_text`` column.
_FIGURE_TEXT_KEYS = ("figure_text", "figure_text_mode", "figure_text_truncated")


def _split_figure_text(meta: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """Split ``meta`` into ``(meta_without_figure_keys, figure_text_or_None)``.

    This is THE exclusion boundary for figure text. ``db.rs``'s ``HAYSTACK``
    scans ``COALESCE(e.meta,'')``, so leaving these keys in the meta column would
    silently fold every figure's legend into EVERY default search — the opposite
    of the opt-in the checkbox exists to provide. Lifting them into their own
    column means the default search SQL is byte-identical to the pre-feature
    build, and the opt-in path pays for itself only when enabled.

    The full text still lives in ``meta`` in INDEX.json / INDEX.jsonl, so agents
    can grep it and ``content_digest`` still covers it; the split is a
    presentation-boundary concern, not a change to what the manifest records.

    Never mutates the input (the manifest dict is shared across readers).
    """
    if not isinstance(meta, dict):
        return {}, None
    if not any(k in meta for k in _FIGURE_TEXT_KEYS):
        return meta, None
    clean = {k: v for k, v in meta.items() if k not in _FIGURE_TEXT_KEYS}
    tokens = meta.get("figure_text")
    if isinstance(tokens, list):
        text = " ".join(str(t) for t in tokens).lower()
    elif tokens:
        text = str(tokens).lower()
    else:
        text = None
    return clean, (text or None)


def _searchtext(entry: Dict[str, Any]) -> str:
    """Build the lowercased FTS haystack for one entry (path + cat + ext +
    extractor + tags + flattened meta).

    Figure text is EXCLUDED (see :func:`_split_figure_text`) so this haystack and
    ``db.rs``'s ``HAYSTACK`` keep covering the same surface.
    """
    parts: List[str] = [
        str(entry.get("path") or ""),
        str(entry.get("category") or ""),
        str(entry.get("ext") or ""),
        str(entry.get("extractor") or ""),
    ]
    tags = entry.get("tags")
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags)
    clean_meta, _ = _split_figure_text(entry.get("meta") or {})
    _flatten_meta(clean_meta, parts)
    return " ".join(parts).lower()


_LOCK_NAME = ".lens-writer.lock"
_SIDECAR_SUFFIXES = ("-wal", "-shm")


def _pid_is_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists (POSIX ``kill(pid, 0)``).

    ``ProcessLookupError`` -> dead. ``PermissionError`` -> exists but owned by
    someone else (still alive, from our point of view). Any other ``OSError``
    (e.g. a bogus/negative pid) is treated as "not alive" defensively — a false
    negative here only means the DR build proceeds when it perhaps shouldn't
    (self-correcting: the live writer's OWN flock would then refuse it below),
    never a false "still held" deadlock.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _acquire_writer_lock(out_dir: Path) -> Iterator[None]:
    """Acquire the exclusive ``<out_dir>/.lens-writer.lock`` guard, or refuse.

    PHASE_0_1_SPEC.md §3.6: Rust is the sole writer of the LIVE ``INDEX.sqlite``
    (WAL mode); this whole-file Python rebuild may run ONLY when no live Rust
    writer holds the index, because ``tmp.replace(final)`` unlinks the inode the
    writer's ``-wal``/``-shm`` are bound to. The guard is a PID file **plus** an
    OS advisory ``flock(LOCK_EX|LOCK_NB)`` — PID+liveness is the LOAD-BEARING
    check (exFAT ``flock`` may be a silent no-op, spec §V9), ``flock`` is the
    belt-and-braces backup for a real POSIX filesystem. Acquire-or-abort: raises
    ``RuntimeError`` immediately (never blocks) if a live writer is detected,
    either by a still-alive recorded PID or by a failed ``flock`` acquire.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    lock_path = out_dir / _LOCK_NAME
    refusal = RuntimeError(
        "INDEX.sqlite has a live Lens writer — quit Lens or use the app's Rebuild"
    )
    # "a+" creates the file if absent, never truncates, and is seekable/readable
    # (plain "a" is write-only on some platforms) — we need to read the prior
    # holder's PID before deciding whether to attempt the flock at all.
    fh = open(lock_path, "a+")
    try:
        fh.seek(0)
        prior = fh.read().strip()
        if prior:
            try:
                prior_pid = int(prior)
            except ValueError:
                prior_pid = 0
            if prior_pid and prior_pid != os.getpid() and _pid_is_alive(prior_pid):
                raise refusal
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise refusal from exc
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _unlink_stale_sidecars(final: Path) -> None:
    """Remove any stale ``-wal``/``-shm`` sidecars (+ their AppleDouble ``._``
    twins) next to ``final`` BEFORE the atomic replace (§3.6 orphaned-WAL
    hazard).

    SQLite associates ``-wal``/``-shm`` with a db by FILENAME, not inode. After
    this whole-file replace, a stray sidecar left over from a prior WAL session
    could be misapplied the next time the Rust writer opens the fresh db —
    a corruption risk, especially on exFAT. A missing sidecar (the common case)
    is silently fine.
    """
    for suffix in _SIDECAR_SUFFIXES:
        for candidate in (
            final.with_name(final.name + suffix),
            final.with_name("._" + final.name + suffix),
        ):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def write_sqlite(manifest: Dict[str, Any], out_dir: Path) -> Path:
    """DISASTER-RECOVERY ONLY (PHASE_0_1_SPEC.md §3.6): build a fresh, whole-file
    ``out_dir/INDEX.sqlite`` from a manifest dict; return its path.

    In NORMAL operation Rust is the sole writer of the LIVE ``INDEX.sqlite`` —
    it ingests the Python-emitted JSONL manifest into the live index in one
    transaction (§2.6 Path A). This function is the secondary, whole-file
    fallback for when that live path is unavailable; it REFUSES to run while a
    live Lens writer holds ``<out_dir>/.lens-writer.lock`` (see
    :func:`_acquire_writer_lock`) rather than racing an open WAL file, and
    raises ``RuntimeError`` in that case.

    Builds at ``INDEX.sqlite.tmp``, closes it cleanly, unlinks any stale
    ``-wal``/``-shm`` sidecars (+ their ``._`` AppleDouble twins) next to the
    target, then atomically ``replace``s ``INDEX.sqlite`` — all while holding
    the writer lock — so a concurrent reader never observes a half-written DB
    and the next WAL open never picks up an orphaned sidecar. Idempotent —
    overwrites any prior DB. The ``meta`` column carries the FULL, untruncated
    per-entry metadata (so the native viewer has the complete column lists the
    HTML embed truncates). Schema stays the CURRENT FLAT (v1) shape — no dir
    rows, no ``is_dir``/``path_key``/``parent_key``/``name_key`` — the v2
    schema/tree/keys are Rust-owned (§0.5D) and are never built here.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / "INDEX.sqlite"
    tmp = out_dir / "INDEX.sqlite.tmp"

    with _acquire_writer_lock(out_dir):
        if tmp.exists():
            tmp.unlink()

        entries = manifest.get("entries") or []

        con = sqlite3.connect(str(tmp))
        try:
            con.execute("PRAGMA journal_mode=OFF")
            con.execute("PRAGMA synchronous=OFF")
            con.executescript(_SCHEMA)

            rows: List[tuple] = []
            fts_rows: List[tuple] = []
            for i, e in enumerate(entries, start=1):
                meta = e.get("meta")
                if not isinstance(meta, dict):
                    meta = {}
                meta, figure_text = _split_figure_text(meta)
                n_obs = meta.get("n_obs")
                n_vars = meta.get("n_vars")
                symlink_ok = e.get("symlink_ok")
                rows.append((
                    i,
                    e.get("path"),
                    e.get("category"),
                    e.get("ext"),
                    e.get("size_bytes"),
                    e.get("mtime_iso"),
                    1 if e.get("is_symlink") else 0,
                    e.get("symlink_target"),
                    (None if symlink_ok is None else (1 if symlink_ok else 0)),
                    e.get("extractor"),
                    json.dumps(e.get("tags") or [], ensure_ascii=False),
                    e.get("error"),
                    n_obs if isinstance(n_obs, int) else None,
                    n_vars if isinstance(n_vars, int) else None,
                    json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
                    figure_text,
                ))
                fts_rows.append((i, e.get("path"), _searchtext(e)))

            con.executemany(
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
            con.executemany(
                "INSERT INTO fts(rowid, path, searchtext) VALUES (?,?,?)", fts_rows
            )
            con.commit()
        finally:
            con.close()

        _unlink_stale_sidecars(final)
        tmp.replace(final)

    return final
