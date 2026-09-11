"""repo_index.app — an always-open native macOS app around INDEX.html.

A single WKWebView window (via pywebview) loads the self-contained INDEX.html
straight off disk (``file://``) — NO http server. The page's Refresh button and
"reveal in Finder" actions call back into Python through pywebview's ``js_api``
bridge (``window.pywebview.api.refresh()`` / ``.reveal(path)``), which is injected
into the page at runtime by pywebview (so the HTML stays self-contained — the
bridge is not an external asset).

pywebview is an OPTIONAL dependency (extra ``app``): the import is guarded so this
module — and the whole ``repo_index`` package — still imports without it. When it
is absent, :func:`run_app` prints an install hint and returns exit code 2 instead
of crashing.

The repo root is always PASSED IN (never hard-coded), so the app works on any
checkout / any machine.
"""

from __future__ import annotations

import hashlib
import json as _json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:  # pywebview is optional — keep the package importable without it.
    import webview  # type: ignore
except Exception:  # noqa: BLE001 - any import failure ⇒ feature simply unavailable
    webview = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# External "reveal" IPC (Finder Quick Action → running app jumps to a path)
# --------------------------------------------------------------------------- #
# The always-open app is a pywebview window owned by ONE Python process; an
# external `repo_index reveal-in-app <abspath>` is a DIFFERENT process and cannot
# call into the window directly. They rendezvous through two tiny files in an
# OS-temp directory keyed by the (resolved) repo root:
#   * app.pid     — the running window's PID (presence + liveness probe);
#   * reveal.json — {"path": <rel|"<dir>/__dir__">, "ts": <epoch>} (newest wins).
# The running window runs a daemon thread that polls reveal.json and, on a NEW
# ts, calls the page's window.__revealFromExternal(target) via evaluate_js (and
# best-effort brings the window forward). The IPC dir is under the user's CACHES
# dir — NOT under the repo / out_dir (never indexed/committed, polling never
# touches the slow external drive).
#
# CRITICAL: it must NOT depend on tempfile.gettempdir(). gettempdir() honours
# $TMPDIR, which DIFFERS between the always-open app (launched from the Dock with
# TMPDIR=/var/folders/…/T) and `reveal-in-app` run from a Finder Shortcut (no
# TMPDIR ⇒ /tmp). That split meant the Shortcut wrote reveal.json to a directory
# the app's watcher never polled — the app opened but never jumped. A per-USER,
# TMPDIR-independent base (same for both processes, which run as the same user)
# fixes it.

def _ipc_dir(root: Any) -> Path:
    """Per-root IPC directory under the user's cache dir (stable for a given root,
    INDEPENDENT of $TMPDIR so the Dock app and a Finder-Shortcut-launched
    `reveal-in-app` agree on it). Keyed by a hash of the RESOLVED root so both
    derive the same path regardless of cwd/symlinks/trailing slash."""
    key = hashlib.sha1(str(Path(root).resolve()).encode("utf-8")).hexdigest()[:12]
    home = Path.home()
    base = (home / "Library" / "Caches" / "repo_index") if sys.platform == "darwin" \
        else (home / ".cache" / "repo_index")
    return base / key


def _pidfile(root: Any) -> Path:
    return _ipc_dir(root) / "app.pid"


def _reveal_file(root: Any) -> Path:
    return _ipc_dir(root) / "reveal.json"


def _proc_start(pid: int) -> Optional[str]:
    """Best-effort process start timestamp (``ps -o lstart``) for PID-reuse defence.

    A pidfile records the PID *and* the owning process's start time; if the OS later
    recycles that PID for an unrelated process (after the app was SIGKILLed/crashed
    without cleanup), the start time won't match — so we can tell "same PID, DIFFERENT
    process" apart from "the app is still alive". Returns the stripped ``ps`` output,
    or ``None`` if ps is unavailable / the pid is gone (caller then degrades to a
    plain PID-liveness check).

    CRITICAL: ``ps -o lstart`` is LOCALE-DEPENDENT (e.g. "Sat Jun 20 17:30:00 2026"
    vs "Sat 20 Jun 17:30:00 2026" under a day-first LC_TIME). The app and the
    ``reveal-in-app`` caller can run with DIFFERENT locales (the app launched from the
    Dock, the CLI from a Finder Shortcut), so an unforced format would mismatch for
    the SAME process — making :func:`app_is_running` falsely report a live app dead
    and breaking the reveal. Force ``LC_ALL=C`` so writer and reader always agree."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=3, check=False,
            env={**os.environ, "LC_ALL": "C", "LC_TIME": "C"},
        )
        out = (r.stdout or "").strip()
        return out or None
    except Exception:  # noqa: BLE001 - ps missing/blocked ⇒ degrade gracefully
        return None


def app_is_running(root: Any) -> bool:
    """True iff a live ``repo_index app`` instance owns ``root``.

    Reads the pidfile (``"<pid>\\n<start-time>"``), probes the PID with ``os.kill(pid,
    0)``, and — crucially — rejects a RECYCLED PID by comparing the recorded start
    time to the live process's (so a stale pidfile whose PID was reused by an
    unrelated process reads as NOT running, and ``reveal-in-app`` launches a fresh
    window instead of writing a command no watcher will read). When ``ps`` is
    unavailable the start-time check is skipped (plain PID liveness, as before)."""
    try:
        lines = _pidfile(root).read_text(encoding="utf-8").splitlines()
        pid = int(lines[0].strip())
        stored_start = lines[1].strip() if len(lines) > 1 else ""
    except (OSError, ValueError, IndexError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = liveness probe, no signal delivered
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user — treat as running
    except OSError:
        return False
    # PID exists — guard against PID REUSE: a recorded start time that no longer
    # matches the live process means this is a DIFFERENT process on the same PID.
    if stored_start:
        cur = _proc_start(pid)
        if cur is not None and cur != stored_start:
            return False
    return True


def installed_bundle(name: str = "Repo Index") -> Optional[Path]:
    """Return the path to the installed ``<name>.app`` bundle (``install-app``) if it
    exists, else None. ``reveal-in-app`` launches THIS — the same app the user keeps in
    their Dock — instead of a bare ``python -m repo_index app`` interpreter process, so
    there is ONE app identity (branded icon, no stray "Python" window, ``open`` activates
    the existing instance rather than spawning a duplicate)."""
    p = Path.home() / "Applications" / f"{name}.app"
    return p if p.exists() else None


# A reveal command written within this many seconds before an app starts is treated as
# "for this launch" and fired by the watcher on startup; anything older is a stale
# leftover and skipped. This lets `reveal-in-app` queue the target then `open` the
# bundle (whose launcher takes no --reveal) and still have the new window jump to it.
_REVEAL_FRESH_ON_START = 25.0


def _initial_watcher_last(existing_ts: Optional[float], now: float) -> Optional[float]:
    """Compute the watcher's initial 'last seen' ts. Returns None when a FRESH command
    (written < _REVEAL_FRESH_ON_START ago) should be fired on startup, else the
    existing ts (so a stale leftover from a prior session is skipped)."""
    if existing_ts is not None and (now - existing_ts) < _REVEAL_FRESH_ON_START:
        return None  # fresh ⇒ let the watcher fire it on its first poll
    return existing_ts  # stale (or none) ⇒ skip it


def acquire_launch_lock(root: Any, ttl: float = 15.0) -> bool:
    """Best-effort single-launch guard. Returns True for the FIRST caller that wins
    an atomic lock file under the IPC dir; False while a recent launch is in flight
    (within ``ttl`` seconds). Prevents a stampede when several ``reveal-in-app`` calls
    fire in quick succession with no app yet running (e.g. a Quick Action looping over
    a multi-file selection) — without it, each would Popen its own ``repo_index app``.
    A stale lock (older than ``ttl`` — a launch that never produced a window) is stolen."""
    d = _ipc_dir(root)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return True  # can't lock ⇒ don't block the (single) launch
    lock = d / "launch.lock"
    now = time.time()
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, str(now).encode("ascii"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        try:
            prev = float(lock.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            prev = 0.0
        if now - prev > ttl:  # stale → steal it
            try:
                lock.write_text(str(now), encoding="utf-8")
                return True
            except OSError:
                return False
        return False
    except OSError:
        return True


def request_reveal(root: Any, rel: str) -> None:
    """Write the reveal command file (atomic, newest-ts-wins) that a running app's
    watcher polls. ``rel`` is a repo-relative path, or ``"<dir>/__dir__"`` for a
    directory (matching the page's revealInTree contract)."""
    d = _ipc_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    payload = {"path": rel, "ts": time.time()}
    rf = _reveal_file(root)
    tmp = rf.with_name(rf.name + ".tmp")
    tmp.write_text(_json.dumps(payload), encoding="utf-8")
    tmp.replace(rf)
    _log(root, "request_reveal wrote target=%r ts=%s" % (rel, payload["ts"]))


def _write_pidfile(root: Any) -> None:
    """Record this instance as ``"<pid>\\n<start-time>"`` (start time enables the
    PID-reuse defence in :func:`app_is_running`)."""
    try:
        _ipc_dir(root).mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        _pidfile(root).write_text(f"{pid}\n{_proc_start(pid) or ''}", encoding="utf-8")
    except OSError:
        pass


def _remove_pidfile(root: Any) -> None:
    """Remove our pidfile on exit — but only if it is still OURS (don't clobber a
    newer instance that took ownership of the same root). Matches on the PID line."""
    pf = _pidfile(root)
    try:
        if int(pf.read_text(encoding="utf-8").splitlines()[0].strip()) != os.getpid():
            return
    except (OSError, ValueError, IndexError):
        return
    try:
        pf.unlink()
    except OSError:
        pass


# A reveal target is re-applied across a page reload (see run_app/_on_loaded) only
# this long after the last GENUINE reveal — so a plain Refresh well after the user has
# navigated elsewhere doesn't yank them back to a stale target. The reload that drops
# the reveal happens within ~1s of the Refresh that follows a reveal, so this is ample.
_REVEAL_REAPPLY_TTL = 120.0


def _should_reapply_reveal(state_reveal: Dict[str, Any], now: float) -> bool:
    """True if a tracked reveal target should be re-applied on a (re)load: a target is
    set and the last genuine reveal was within :data:`_REVEAL_REAPPLY_TTL` seconds."""
    tgt = state_reveal.get("target")
    return bool(tgt) and (now - float(state_reveal.get("ts") or 0.0)) < _REVEAL_REAPPLY_TTL


def _log(root: Any, msg: str) -> None:
    """Append a timestamped line to the per-root reveal diagnostics log (best-effort).
    Read ``_ipc_dir(root)/reveal.log`` to trace a Finder→app reveal end-to-end when it
    misbehaves (the app is a GUI process with no console)."""
    try:
        d = _ipc_dir(root)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "reveal.log").open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S") + " " + str(msg) + "\n")
    except Exception:  # noqa: BLE001 - logging must never break anything
        pass


def _do_reveal(window: Any, rel: str, root: Any = None) -> None:
    """Jump the live window to ``rel`` via the page hook AND raise it to the foreground.
    Best-effort + logged. The RAISE must happen here, in the OWNING process: the CLI's
    ``open -a <bundle>`` cannot raise this window because the bundle launcher execs
    python, so the window's app identity is ``python3``, not the bundle — `open -a`
    targets the wrong identity. All window ops are marshalled to the UI thread by
    pywebview, so they are safe to call from the watcher thread."""
    if not rel:
        return
    status = None
    try:
        # Returns the page hook's status: 'ok' / 'notfound' / 'error:…' / 'no-hook'
        # (the latter when an OLD INDEX.html without the hook is loaded).
        status = window.evaluate_js(
            "(function(){ if(!window.__revealFromExternal) return 'no-hook';"
            " try{ return window.__revealFromExternal(%s); }catch(e){ return 'error:'+(e&&e.message||e); } })()"
            % _json.dumps(rel)
        )
    except Exception as exc:  # noqa: BLE001 - page may not be ready / API drift
        status = "evaljs-exc:%s" % exc
    if root is not None:
        _log(root, "do_reveal rel=%r -> %r" % (rel, status))
    # Raise to the foreground. restore() un-minimizes; show() un-hides; the on_top
    # flip (float-then-release) forces the window ABOVE the user's current frontmost
    # app — `restore()` alone leaves the (correctly-jumped) window buried, which is the
    # "opens but doesn't jump" symptom (the user is looking at a different app).
    for _op in ("restore", "show"):
        try:
            getattr(window, _op)()
        except Exception:  # noqa: BLE001
            pass
    try:
        window.on_top = True
        window.on_top = False
    except Exception:  # noqa: BLE001
        pass
    if root is not None:
        _log(root, "do_reveal raised window")


def _start_reveal_watcher(
    window: Any,
    root: Any,
    state_reveal: Optional[Dict[str, Any]] = None,
    poll_seconds: float = 0.5,
) -> threading.Thread:
    """Spawn a daemon thread that polls the reveal command file and jumps the live
    window on each NEW request. Initialises 'last seen' from any pre-existing file
    so a stale command from a prior session does NOT fire on startup.

    When ``state_reveal`` is given, each serviced reveal updates it (``target``/``ts``)
    so a subsequent page reload re-applies the LATEST target (not just the launch one)
    — see :func:`run_app`."""
    rf = _reveal_file(root)

    def _ts_of(path: Path) -> Optional[float]:
        try:
            obj = _json.loads(path.read_text(encoding="utf-8"))
            ts = obj.get("ts")
            return float(ts) if ts is not None else None
        except Exception:  # noqa: BLE001
            return None

    # Fire a reveal queued just before THIS launch (e.g. `reveal-in-app` wrote it then
    # `open`ed the bundle, whose launcher takes no --reveal); skip a stale leftover.
    state = {"last": _initial_watcher_last(_ts_of(rf), time.time())}
    _log(root, "watcher started (initial last=%s, file=%s exists=%s)" % (state["last"], rf, rf.exists()))

    def loop() -> None:
        while True:
            time.sleep(poll_seconds)
            try:
                if not rf.exists():
                    continue
                obj = _json.loads(rf.read_text(encoding="utf-8"))
                ts = obj.get("ts")
                rel = obj.get("path")
                tsf = float(ts) if ts is not None else None
                if tsf is not None and tsf != state["last"] and rel:
                    state["last"] = tsf
                    if state_reveal is not None:
                        state_reveal["target"] = str(rel)
                        state_reveal["ts"] = time.time()
                    _log(root, "watcher fired ts=%s target=%r" % (tsf, rel))
                    _do_reveal(window, str(rel), root)
            except Exception:  # noqa: BLE001 - a bad/raced write must not kill the watcher
                continue

    t = threading.Thread(target=loop, name="repo_index-reveal-watcher", daemon=True)
    t.start()
    return t


def _load_raw_entries(out_dir: Any) -> Optional[Dict[str, Any]]:
    """Load INDEX.json and return its ``{path: entry}`` RAW (unprojected) — the shape
    ``compute_entry_delta`` expects for its ``old_entries`` (it compares the prior
    against the RAW new entry, then projects only the delta's output). ``refresh()``
    diffs the new build against THIS snapshot of the page's state so the delta always
    composes in place. Returns None on any failure (missing/locked/corrupt index) →
    caller seeds lazily. NOTE: must be RAW, not projected — feeding projected entries
    makes every projection-altered field (capped first_paragraph, truncated columns)
    read as a phantom change."""
    try:
        from . import manifest as manifest_mod
        prior = manifest_mod.load_prior_entries(Path(out_dir))
        return prior or None
    except Exception:  # noqa: BLE001 - best-effort snapshot; never block app startup
        return None


class Api:
    """JS-callable bridge exposed to INDEX.html as ``window.pywebview.api``.

    Each method returns a JSON-serializable dict (resolved as a Promise on the JS
    side). The repo ``root`` / ``out_dir`` / build args are captured at construction
    so the page can never inject them.

    SECURITY — only ``refresh``, ``reveal`` and ``meta`` may be reachable from
    in-page JS. pywebview's bridge generator (``webview.util.inject_pywebview``'s
    nested ``get_functions``) skips underscore-prefixed names, then RECURSES into
    any PUBLIC non-callable attribute that has ``__module__`` — which
    ``pathlib.Path`` objects do. Storing the captured context as public ``Path``
    attributes would therefore expose the entire ``Path`` API (``write_text``/
    ``unlink``/``chmod``/``rename`` …) — and the same for every ``.parent``
    ancestor, i.e. the whole filesystem above root — to JavaScript, bypassing
    :func:`finder.reveal`'s path guard. The context is kept PRIVATE
    (underscore-prefixed) so the generator never walks it; the JS-reachable set
    collapses to exactly ``{"refresh", "reveal", "meta"}`` — all three take a str
    (or nothing) and return a plain dict, and ``meta`` never resolves its argument
    against the filesystem. ``test_app_finder`` asserts this and guards against
    regression.
    """

    def __init__(
        self,
        root: Path,
        out_dir: Path,
        config_path: Optional[Path] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._root = Path(root)
        self._out_dir = Path(out_dir)
        self._config_path = config_path
        self._overrides = overrides
        # Snapshot (RAW) of the entries the page reflects. refresh() diffs the new build
        # against _served (the page's own state), NOT new-vs-on-disk — so the delta ALWAYS
        # composes in place and the app never falls back to a location.reload(), which
        # WebKit's never-recycled WebContent process never reclaims (the measured over-days
        # RAM ratchet). Best-effort; None ⇒ seeded lazily on first refresh. Underscore-
        # private (kept off the JS bridge surface).
        self._served = _load_raw_entries(self._out_dir)

    def refresh(self, index_columns: Optional[bool] = None) -> Dict[str, Any]:
        """Run the incremental build (rewrites INDEX.* incl. the HTML); the page
        updates itself IN PLACE only when something actually changed. Returns
        ``{"ok": True, "unchanged": bool, "after": digest|None, "delta": {...}|None,
        "index_columns": bool}`` or ``{"ok": False, "error": ...}``.

        ``index_columns`` (optional bool) lets the in-app columns toggle flip the
        ``index_columns`` setting for THIS build: ``False`` strips the wide per-CSV/TSV
        ``columns`` lists (a full re-extract, then sticky on disk via config_used so
        later plain refreshes inherit it); ``True`` re-indexes them. ``None`` (a normal
        Refresh) carries no override and inherits the persisted setting. The effective
        setting after the build is echoed back as ``index_columns`` so the page can
        update its toggle + the inspector's "columns not indexed" state.

        ``before`` / ``after`` are the ON-DISK ``content_digest`` immediately before
        and after the build. The page compares ``before`` to the digest its
        in-memory ENTRIES reflect and applies the delta in place ONLY when they
        match — otherwise the on-disk index was advanced by another process since
        the page last synced (a ``query`` freshen, a commit, a manual build) and the
        page reloads instead of silently staying behind (see render_html
        ``applyResult``).

        ``unchanged`` is true when the build was a structural no-op (the
        content_digest in INDEX.json is byte-identical before and after — the
        skip-write gate in ``build_index`` left the artifacts untouched). The page
        uses it to AVOID re-rendering on every glance-Refresh.

        ``delta`` is an ENTRY-LEVEL diff ``{"added": [entry…], "changed": [entry…],
        "removed": [path…]}`` (entries projected exactly like the embedded ENTRIES)
        computed by capturing the prior INDEX.json entries BEFORE the build and
        diffing them against the new manifest's entries by path. The page applies it
        to its in-memory ENTRIES and re-renders the tree WITHOUT a full
        ``location.reload()`` — reloading the self-contained HTML in a
        never-recycled WKWebView is what lets memory ratchet up over days. On an
        unchanged refresh the delta is the empty diff. Computing the delta is
        best-effort: ANY failure degrades ``delta`` to ``None`` (the page then falls
        back to today's full reload) — the refresh itself never fails because of it.
        ``cli.build_index`` / ``manifest`` are imported lazily so a bare ``import
        repo_index.app`` stays cheap."""
        try:
            from .cli import build_index
            from . import manifest as manifest_mod
            # Seed the page-state snapshot lazily if construction couldn't (e.g. the
            # index didn't exist yet at launch).
            if self._served is None:
                self._served = _load_raw_entries(self._out_dir) or {}
            # A one-shot index_columns override (from the in-app toggle) for THIS build;
            # not persisted to self._overrides — stickiness comes from the on-disk
            # config_used (build_index inherits the prior setting on a plain refresh).
            ov = dict(self._overrides) if self._overrides else {}
            if index_columns is not None:
                ov["index_columns"] = bool(index_columns)
            manifest = build_index(
                self._root,
                self._out_dir,
                self._config_path,
                overrides=(ov or None),
                incremental=True,
                quiet=True,
            )
            after = manifest_mod._peek_committed_digest(self._out_dir)
            new_entries = manifest.get("entries") if isinstance(manifest, dict) else None
            delta: Optional[Dict[str, Any]] = None
            if isinstance(new_entries, list):
                try:
                    # Diff the new build against what the PAGE currently shows (self._served),
                    # NOT the on-disk prior — so the delta composes in place even when an
                    # external process (query freshen / commit / build) advanced the index
                    # since the page synced. _served and new_entries are both RAW (the shape
                    # compute_entry_delta compares); it projects only the delta's output.
                    # Advance _served (RAW) in lockstep with the page.
                    delta = manifest_mod.compute_entry_delta(self._served, new_entries)
                    self._served = {
                        e["path"]: e
                        for e in new_entries
                        if isinstance(e, dict) and isinstance(e.get("path"), str)
                    }
                except Exception:  # noqa: BLE001 - delta is best-effort; page reloads as last resort
                    delta = None
            unchanged = bool(
                delta is not None
                and not delta.get("added") and not delta.get("changed") and not delta.get("removed")
            )
            # Echo the EFFECTIVE index_columns (after persistence/inheritance) so the
            # page's toggle + the inspector's "columns not indexed" state stay in sync.
            eff_cols = True
            try:
                eff_cols = bool((manifest.get("config_used") or {}).get("index_columns", True))
            except Exception:  # noqa: BLE001
                eff_cols = True
            # No baseline check needed for the app: the delta is computed against the
            # page's own state, so it ALWAYS composes — the page reconciles it in place
            # and NEVER reloads (the RAM ratchet) except as a last resort if reconcile
            # itself throws. `after` advances the page's BASELINE_DIGEST. The HTTP/serve
            # path keeps its own baseline-guarded contract (it is stateless). See
            # render_html applyResult (kind === "app").
            return {"ok": True, "unchanged": unchanged, "after": after, "delta": delta, "index_columns": eff_cols}
        except Exception as exc:  # noqa: BLE001 - report to the page, never crash
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _compute_delta(
        manifest_mod: Any,
        prior_entries: Optional[Dict[str, Any]],
        manifest: Optional[Dict[str, Any]],
        unchanged: bool,
    ) -> Optional[Dict[str, Any]]:
        """Best-effort entry-level delta for the in-place tree update.

        Returns the empty diff on an ``unchanged`` build (digest-stable ⇒ nothing
        moved), the real diff (prior INDEX.json entries vs the new manifest's
        entries) on a change, or ``None`` if anything needed to compute it is
        missing/raises — in which case the page falls back to a full reload, never
        a broken partial update. Underscore-prefixed so the pywebview bridge never
        exposes it (the JS-reachable set stays {refresh, reveal, meta})."""
        if unchanged:
            return {"added": [], "changed": [], "removed": []}
        try:
            if prior_entries is None or not isinstance(manifest, dict):
                return None
            new_entries = manifest.get("entries")
            if not isinstance(new_entries, list):
                return None
            return manifest_mod.compute_entry_delta(prior_entries, new_entries)
        except Exception:  # noqa: BLE001 - delta is optional; fall back to reload
            return None

    def reveal(self, path: str) -> Dict[str, Any]:
        """Reveal ``root/path`` in the OS file browser (shared finder.reveal,
        same path-guard + dir-vs-file logic as the server)."""
        from . import finder
        return finder.reveal(self._root, path)

    def meta(self, path: str) -> Dict[str, Any]:
        """Return the FULL, untruncated ``meta`` dict for one entry by reading its
        line from ``out_dir/INDEX.jsonl``. Returns ``{"ok": True, "meta": {...}}``
        or ``{"ok": False, "error": ...}``.

        The HTML embed truncates long meta arrays (wide-table ``columns`` etc.) to
        a head+count to keep the WebView lean; the inspector's "load all" affordance
        calls this to restore the complete list on demand (app mode only — a static
        ``file://`` page has no bridge and shows the head + count, which is fine: the
        full list is one ``grep`` of INDEX.jsonl away).

        SECURITY: ``path`` is a STRING, the return is a plain dict, and this method
        NEVER resolves ``path`` against the filesystem — it only string/JSON-matches
        the ``path`` field of the fixed read-only ``self._out_dir / 'INDEX.jsonl'``.
        It therefore has a strictly smaller surface than ``reveal`` (no traversal is
        possible) and, like the rest of the context, stores no public ``Path`` attr,
        so the pywebview bridge exposes only ``{refresh, reveal, meta}`` to JS
        (asserted by ``test_app_finder``)."""
        import json as _json

        jsonl = self._out_dir / "INDEX.jsonl"
        try:
            # Compact JSONL lines start with `{"path":"<escaped>",...`; build the
            # exact quoted-key needle in the SAME encoding the file was written
            # with (ensure_ascii=False, manifest.write_outputs) so non-ASCII paths
            # still match the fast pre-filter before the full json.loads.
            needle = '"path":' + _json.dumps(path, ensure_ascii=False)
            with jsonl.open(encoding="utf-8") as fh:
                for line in fh:
                    if needle in line:
                        obj = _json.loads(line)
                        if obj.get("path") == path:
                            return {"ok": True, "meta": obj.get("meta") or {}}
            return {"ok": False, "error": "not found"}
        except Exception as exc:  # noqa: BLE001 - report to the page, never crash
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def run_app(
    root: Path,
    out_dir: Path,
    config_path: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
    title: str = "repo_index",
    quiet: bool = False,
    reveal: Optional[str] = None,
) -> int:
    """Open the always-on native window loading INDEX.html; return an exit code.

    If pywebview is not installed, prints an install hint to stderr and returns 2
    (does NOT crash). Ensures the index exists (builds it if INDEX.html is
    missing), then creates a WKWebView window loading the INDEX.html ``file://``
    URI with an :class:`Api` ``js_api`` bridge, and starts the event loop. Returns
    0 after the window closes.

    ``reveal`` (repo-relative path, or ``"<dir>/__dir__"``) is jumped to once the
    page has loaded — used when the app is launched fresh by ``reveal-in-app`` for a
    file that wasn't open yet. While running, a daemon watcher (started here) also
    services later external reveal requests in place; a pidfile keyed by ``root``
    lets ``reveal-in-app`` find this live instance instead of opening a 2nd window.
    """
    root = Path(root)
    out_dir = Path(out_dir)

    if webview is None:
        print(
            "[repo_index] the app needs pywebview — install it with:\n"
            "    pip install 'pywebview>=4'\n"
            "  (or `pip install repo_index[app]`). "
            "Falling back: use `repo_index open` (static page) or `repo_index serve`.",
            file=sys.stderr,
        )
        return 2

    html = out_dir / "INDEX.html"
    if not html.exists():
        if not quiet:
            print(f"[repo_index] no index at {out_dir} — building first…", file=sys.stderr)
        from .cli import build_index
        build_index(root, out_dir, config_path, overrides=overrides, quiet=True)

    api = Api(root, out_dir, config_path, overrides)
    if not quiet:
        print(f"[repo_index] opening app for {root}", file=sys.stderr)
    window = webview.create_window(
        title,
        url=html.resolve().as_uri(),
        js_api=api,
        width=1400,
        height=900,
    )
    # Record this live instance so an external `reveal-in-app` finds + jumps it
    # (instead of opening a second window), and wire the page's external-reveal hook
    # once the JS is ready. Both are best-effort and never block the window.
    _write_pidfile(root)

    # pywebview's `loaded` handler list is NEVER cleared across page loads and fires
    # on EVERY navigation, INCLUDING the `location.reload()` that the in-app Refresh
    # falls back to when the on-disk index advanced since the page loaded. That reload
    # re-inits the page with the tree fully collapsed and the (zero-state) page cannot
    # persist what was revealed — so the reveal MUST be re-applied here, on every load.
    # Two guards keep this correct: the WATCHER is started exactly once (a leak/dup-fire
    # otherwise), and the reveal is re-applied only while a target is set AND within
    # _REVEAL_REAPPLY_TTL of the last genuine reveal (so a plain Refresh long after the
    # user navigated elsewhere does not yank them back). state_reveal holds the latest
    # target (launch --reveal, then updated by the watcher on each external reveal).
    once = {"watcher": False}
    state_reveal: Dict[str, Any] = {"target": reveal, "ts": (time.time() if reveal else 0.0)}

    _log(root, "run_app started pid=%d reveal=%r html=%s" % (os.getpid(), reveal, html))

    def _on_loaded() -> None:
        _log(root, "page loaded")
        if not once["watcher"]:
            once["watcher"] = True
            _start_reveal_watcher(window, root, state_reveal)
        if _should_reapply_reveal(state_reveal, time.time()):
            _do_reveal(window, str(state_reveal["target"]), root)

    try:
        window.events.loaded += _on_loaded  # fire after the page (+ its JS hook) loads
    except Exception:  # noqa: BLE001 - older pywebview without the events API
        _log(root, "events.loaded unavailable — fallback wiring")
        if not once["watcher"]:
            once["watcher"] = True
            _start_reveal_watcher(window, root, state_reveal)
        if reveal:
            def _delayed_initial() -> None:
                time.sleep(2.0)
                _do_reveal(window, reveal, root)

            threading.Thread(target=_delayed_initial, daemon=True).start()

    try:
        webview.start()
    finally:
        _remove_pidfile(root)
    return 0


def install_app(
    root: Path,
    dest: Optional[Path] = None,
    name: str = "Repo Index",
    python: Optional[str] = None,
) -> int:
    """Create a real macOS .app bundle that launches ``repo_index app`` for ``root``.

    Builds ``<dest>/<name>.app`` (default dest ``~/Applications``) with an
    Info.plist, a launcher script (runs ``<python> -m repo_index app --root
    <root>``, with a friendly dialog if the data drive is unmounted), and the
    AppIcon.icns, then registers it with Launch Services so Spotlight finds it and
    it can be pinned to the Dock. Does NOT require pywebview (only running the app
    does). Returns 0 on success, 2 on a non-macOS platform.
    """
    import plistlib
    import shutil
    import subprocess as sp

    if sys.platform != "darwin":
        print("[repo_index] install-app is macOS-only.", file=sys.stderr)
        return 2

    root = Path(root).resolve()
    dest = Path(dest).expanduser() if dest else (Path.home() / "Applications")
    dest.mkdir(parents=True, exist_ok=True)
    app_dir = dest / f"{name}.app"
    contents = app_dir / "Contents"
    macos = contents / "MacOS"
    res = contents / "Resources"
    for p in (macos, res):
        p.mkdir(parents=True, exist_ok=True)
    py = python or sys.executable

    launcher = macos / "repo_index_app"
    launcher.write_text(
        "#!/bin/bash\n"
        f'ROOT="{root}"\n'
        f'PY="{py}"\n'
        'if [ ! -d "$ROOT" ]; then\n'
        "  osascript -e 'display dialog \"Repo Index: the data drive is not mounted, "
        "so the index is unavailable.\" buttons {\"OK\"} default button \"OK\" "
        "with icon caution with title \"Repo Index\"' >/dev/null 2>&1\n"
        "  exit 1\n"
        "fi\n"
        'exec "$PY" -m repo_index app --root "$ROOT" "$@"\n'
    )
    launcher.chmod(0o755)

    icns = Path(__file__).resolve().parent / "assets" / "AppIcon.icns"
    if icns.exists():
        shutil.copyfile(icns, res / "AppIcon.icns")

    info = {
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundleIdentifier": "com.repoindex.locator",
        "CFBundleExecutable": "repo_index_app",
        "CFBundleIconFile": "AppIcon",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "10.13",
    }
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump(info, fh)
    (contents / "PkgInfo").write_text("APPL????")

    lsregister = (
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    try:
        if Path(lsregister).exists():
            sp.run([lsregister, "-f", str(app_dir)], check=False)
    except Exception:  # noqa: BLE001 - registration is best-effort
        pass

    print(f"[repo_index] installed: {app_dir}", file=sys.stderr)
    print(
        "  -> Spotlight-search 'Repo Index', or open it from "
        f"{dest} and keep it in the Dock (right-click -> Options -> Keep in Dock).",
        file=sys.stderr,
    )
    return 0
