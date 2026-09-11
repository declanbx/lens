"""repo_index.extract_one — stateless ``extract-one`` / ``extract-batch`` helper.

Gives the Rust Lens writer (PHASE_0_1_SPEC.md §6) the *heavy* (h5py/pyarrow)
per-file metadata without reimplementing any extractor: a thin driver over four
FROZEN entry points, reimplementing none of them — ``config.load_config``,
``extractors.base.apply_config``, ``extractors.base.extract_meta``,
``walker._maybe_strip_columns``. Rust owns the cheap stat-derived fields
(path/size_bytes/mtime_iso/is_symlink*); Python owns everything that requires
opening the file (extractor/meta/error/n_obs/n_vars) plus, optionally,
ext/category/tags (§6.3). This module is UNCHANGED by the Rust-sole-sqlite-
writer decision — it was never the sqlite writer.

Wire contract (§6.3): ``extract-one`` takes ONE path argument and prints ONE
JSON object to stdout, exit 0. ``extract-batch`` reads NDJSON on stdin — one
``{"path": "/abs/or/rel"}`` per line, blank lines skipped — and prints one JSON
object per line to stdout, in input order, each echoing ``input_path`` for
defensive correlation. STDOUT carries NDJSON ONLY; diagnostics go to STDERR.
Exit is 0 whenever the process ran: a per-file failure is in-band in that
record's ``error``; a non-zero exit means the helper itself could not start.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, IO, Optional

from .extractors.base import apply_config, ext_of, extract_meta
from .walker import _maybe_strip_columns


def _resolve_path(raw_path: str, root: Path) -> Path:
    """Resolve a request's ``path`` (absolute or root-relative) to an absolute Path.

    A bare join for the relative case — no ``.resolve()`` / symlink-following —
    so :func:`extract_record`'s own ``abs_path.is_symlink()`` check inspects the
    exact requested node, mirroring how the walker builds ``abs_path`` from
    ``root``.
    """
    p = Path(raw_path)
    return p if p.is_absolute() else (root / p)


def extract_record(
    abs_path: Path, root: Path, config: Any, *, path_fields: bool
) -> Dict[str, Any]:
    """Build one §6.3 response record for ``abs_path``. Never raises.

    Symlinks short-circuit to ``("generic", {}, None)`` (record-not-traverse) —
    mirrors ``walker.make_entry`` (``walker.py:274-277``), since ``extract_meta``
    would FOLLOW the link rather than record it. Denormalizes ``n_obs``/
    ``n_vars`` EXACTLY as ``export_sqlite.py:142-143`` does (an
    ``isinstance(int)`` guard on the raw meta value — a float/null becomes
    ``None``; do not "improve" it, divergence is itself a parity break, §6.3).

    ``apply_config`` is deliberately NOT called here — it pushes size-gate
    thresholds onto the extractor SINGLETONS (process-wide state), so it must
    run ONCE, before any file in a batch is processed; the callers
    (:func:`run_one` / :func:`run_batch`) call it as their first statement
    (§6.4 callout). ``_maybe_strip_columns`` (the ``--no-columns`` strip) IS a
    separate, per-record step, applied here.
    """
    rec: Dict[str, Any] = {"v": 1, "input_path": str(abs_path)}
    try:
        if abs_path.is_symlink():
            extractor, meta, error = "generic", {}, None
        else:
            extractor, meta, error = extract_meta(abs_path)
            meta = _maybe_strip_columns(meta, config)
        n = meta.get("n_obs")
        rec["n_obs"] = n if isinstance(n, int) else None
        n = meta.get("n_vars")
        rec["n_vars"] = n if isinstance(n, int) else None
        rec.update(extractor=extractor, meta=meta, error=error)
        if path_fields:
            ext = ext_of(abs_path)
            rec["ext"] = ext
            rec["category"] = config.category_for(ext)
            try:
                rel = abs_path.relative_to(root).as_posix()
            except ValueError:
                rel = None
            rec["tags"] = config.tag(rel) if rel is not None else []
    except Exception as exc:  # noqa: BLE001 - the helper NEVER raises (§6.2)
        rec.update(extractor="generic", meta={}, n_obs=None, n_vars=None,
                    error=f"{type(exc).__name__}: {exc}")
    return rec


def run_one(
    raw_path: str,
    root: Path,
    config: Any,
    *,
    path_fields: bool,
    stdout: Optional[IO[str]] = None,
) -> int:
    """Extract ONE path; print its §6.3 JSON record to ``stdout``. Returns 0.

    ``apply_config(config)`` MUST be the FIRST statement (§6.4 callout) — it
    pushes size-gate thresholds onto the extractor singletons before
    extraction runs; without it a non-default gate (e.g. ``--max-csv-bytes 1``)
    would silently fall back to the extractor's own built-in default.

    ``stdout`` defaults to ``None`` (resolved to ``sys.stdout`` INSIDE the
    function body, not as a default-argument expression) so a caller that
    monkeypatches ``sys.stdout`` after this module is imported is still
    honored — a bound default like ``stdout=sys.stdout`` would capture the
    stream object at import time and silently ignore any later swap.
    """
    if stdout is None:
        stdout = sys.stdout
    apply_config(config)
    root = Path(root)
    abs_path = _resolve_path(raw_path, root)
    rec = extract_record(abs_path, root, config, path_fields=path_fields)
    stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
    stdout.flush()
    return 0


def run_batch(
    root: Path,
    config: Any,
    *,
    path_fields: bool,
    stdin: Optional[IO[str]] = None,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
) -> int:
    """Drain NDJSON requests from ``stdin``; print one JSON record per line to
    ``stdout``, in input order. Returns 0.

    ``stdin``/``stdout``/``stderr`` default to ``None``, resolved to
    ``sys.stdin``/``sys.stdout``/``sys.stderr`` INSIDE the function body (see
    :func:`run_one`'s docstring for why NOT a bound default-argument
    expression — it would freeze the stream object at import time).

    ``apply_config(config)`` MUST be the FIRST statement (§6.4 callout), run
    ONCE for the whole batch, not per-line — this is what amortizes the
    interpreter + h5py/numpy import cost across N files (§6.7: the
    per-file-spawn cost is pathological at a launch reconcile of thousands of
    changed files). Blank lines are skipped per the wire contract (never sent
    as a request, never answered — so line count is not 1:1 with output count
    when blanks are present, only non-blank-request count is). Each response
    is flushed immediately so a concurrent Rust reader draining stdout
    line-by-line is never starved mid-batch (§6.8).

    A request line that is not a well-formed ``{"path": ...}`` JSON object
    still produces exactly one in-band-error response (never silently
    dropped), so the caller's per-request accounting never desyncs from a
    garbled line — this defends a case the §6.3 wire contract does not
    explicitly cover (it assumes well-formed requests).
    """
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout
    if stderr is None:
        stderr = sys.stderr
    apply_config(config)
    root = Path(root)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        raw_path: Optional[str] = None
        try:
            req = json.loads(line)
            if isinstance(req, dict):
                raw_path = req.get("path")
        except Exception:  # noqa: BLE001 - a malformed request line, not a crash
            raw_path = None
        # A non-string "path" (int/float/bool/list/dict/None) must be caught
        # HERE, not left for _resolve_path -> Path(non_str) to raise: that
        # TypeError would escape this per-line try/except-free branch and
        # abort the WHOLE batch, violating "exit 0 whenever the process ran;
        # per-file failure is in-band" (§6.2).
        if not isinstance(raw_path, str):
            rec: Dict[str, Any] = {
                "v": 1,
                "input_path": line,
                "extractor": "generic",
                "meta": {},
                "n_obs": None,
                "n_vars": None,
                "error": "malformed request: expected {\"path\": ...}",
            }
            print(
                f"[repo_index] extract-batch: skipping malformed line: {line!r}",
                file=stderr,
            )
        else:
            abs_path = _resolve_path(raw_path, root)
            rec = extract_record(abs_path, root, config, path_fields=path_fields)
        stdout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        stdout.flush()
    return 0
