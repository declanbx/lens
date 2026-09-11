"""repo_index.serve — a tiny localhost HTTP server with an in-page /refresh.

A static ``file://`` INDEX.html cannot regenerate the index itself: a browser has
no filesystem walk, the extractors can't run in JS, and ``fetch()`` of the sibling
INDEX.json is blocked by ``file://`` CORS (which is why the manifest is embedded
inline). ``repo_index serve`` serves INDEX.html over ``http://127.0.0.1`` and
exposes ``POST /refresh``, which runs the (incremental, ~3s) build so the page's
Refresh button (and the ``r`` key) can regenerate the index in place — no terminal
round-trip.

Binds 127.0.0.1 ONLY (never exposed to the network). The refresh path takes no
user input (the root is fixed at server start) — it only re-runs the configured
build. Stdlib-only.
"""

from __future__ import annotations

import http.server
import json
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import finder
from . import manifest as manifest_mod

_CTYPES = {
    "INDEX.html": "text/html; charset=utf-8",
    "INDEX.json": "application/json; charset=utf-8",
    "INDEX.jsonl": "application/json; charset=utf-8",
    "INDEX.agent.md": "text/markdown; charset=utf-8",
    "crosslinks.json": "application/json; charset=utf-8",
    "crosslinks.dot": "text/plain; charset=utf-8",
}


def _refresh_with_delta(
    out_dir: Path, build_fn: Callable[[], Any]
) -> Dict[str, Any]:
    """Run ``build_fn`` and return ``{"ok": True, "unchanged": bool, "before":
    digest|None, "after": digest|None, "delta": …}``.

    Mirrors ``app.Api.refresh``'s contract for the served page so the browser can
    update its tree IN PLACE (no full reload) on a change. The ``build_fn`` closure
    (``cli._serve_build``) returns None, so — unlike the app, which gets the manifest
    back from ``build_index`` — the NEW entries are re-read from the freshly written
    INDEX.json via ``load_prior_entries`` AFTER the build; the OLD entries are read
    the same way BEFORE it. ``unchanged`` is the digest-before == digest-after peek,
    matching the app. The delta is best-effort: any failure degrades it to ``None``
    (the page then falls back to a full ``location.reload()``), and the build itself
    always runs. Raises only if ``build_fn`` itself raises (the handler maps that to
    HTTP 500, exactly as before)."""
    try:
        before = manifest_mod._peek_committed_digest(out_dir)
    except Exception:  # noqa: BLE001
        before = None
    try:
        prior_entries: Optional[Dict[str, Any]] = manifest_mod.load_prior_entries(out_dir)
    except Exception:  # noqa: BLE001
        prior_entries = None

    build_fn()  # may raise — propagate so the handler returns 500 (unchanged behaviour)

    try:
        after = manifest_mod._peek_committed_digest(out_dir)
    except Exception:  # noqa: BLE001
        after = None
    unchanged = bool(before is not None and before == after)

    delta: Optional[Dict[str, Any]]
    if unchanged:
        delta = {"added": [], "changed": [], "removed": []}
    else:
        try:
            if prior_entries is None:
                delta = None
            else:
                new_entries = manifest_mod.load_prior_entries(out_dir)  # re-read fresh INDEX.json
                delta = manifest_mod.compute_entry_delta(
                    prior_entries, list(new_entries.values())
                )
        except Exception:  # noqa: BLE001 - delta is optional; fall back to reload
            delta = None
    # before/after (ON-DISK prior + new digests) let the page verify its in-memory
    # baseline still matches the delta's prior before applying it in place; on a
    # mismatch (the on-disk index was advanced elsewhere) it reloads instead of
    # silently falling behind. Mirrors app.Api.refresh. See render_html applyResult.
    return {"ok": True, "unchanged": unchanged, "before": before, "after": after, "delta": delta}


def serve(
    root: Path,
    out_dir: Path,
    build_fn: Callable[[], Any],
    port: int = 8765,
    open_browser: bool = True,
    log: Callable[[str], None] = print,
) -> int:
    """Serve ``out_dir`` over localhost with a POST /refresh that runs ``build_fn``.

    Routes: ``GET /`` (and /INDEX.html) -> INDEX.html; ``GET /<artifact>`` for the
    known index artifacts; ``GET /healthz``; ``POST /refresh`` -> run ``build_fn``
    (the incremental build, which rewrites INDEX.* incl. the HTML) under a lock and
    return ``{"ok":true}``. Binds 127.0.0.1 only; tries ``port..port+24`` for a free
    one. Opens the browser to the chosen URL unless ``open_browser`` is False.
    Blocks until Ctrl-C. Returns an int exit code.
    """
    root = Path(root)
    out_dir = Path(out_dir)
    lock = threading.Lock()

    # Make sure there is something to serve before we bind.
    if not (out_dir / "INDEX.html").exists():
        log(f"[repo_index] no index at {out_dir} — building first…")
        build_fn()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence per-request logging
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _serve_file(self, name: str) -> None:
            try:
                data = (out_dir / name).read_bytes()
            except OSError:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            self._send(200, data, _CTYPES.get(name, "application/octet-stream"))

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/INDEX.html"):
                self._serve_file("INDEX.html")
                return
            if path == "/healthz":
                self._send(200, b'{"ok":true}', "application/json")
                return
            name = path.lstrip("/")
            if name in _CTYPES:
                self._serve_file(name)
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def _read_json(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                obj = json.loads(raw or b"{}")
                return obj if isinstance(obj, dict) else {}
            except Exception:  # noqa: BLE001
                return {}

        def _reveal(self, rel: str) -> None:
            # Resolve under root and REFUSE anything outside it (no traversal).
            # The actual resolve + path-guard + dir-vs-file `open`/`open -R` logic
            # lives in finder.reveal (shared with the pywebview app, DRY); here we
            # only map its {"ok",...} result onto an HTTP status code.
            try:
                result = finder.reveal(root, rel)
                if result.get("ok"):
                    self._send(200, json.dumps(result).encode(), "application/json")
                elif result.get("error") == "not found":
                    self._send(404, json.dumps(result).encode(), "application/json")
                else:  # "path outside root" (or any other guard rejection)
                    self._send(403, json.dumps(result).encode(), "application/json")
            except Exception as exc:  # noqa: BLE001 - report, don't crash the server
                self._send(500, json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}).encode(), "application/json")

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/refresh":
                with lock:
                    try:
                        result = _refresh_with_delta(out_dir, build_fn)
                        self._send(200, json.dumps(result).encode(), "application/json")
                    except Exception as exc:  # noqa: BLE001 - report, don't crash the server
                        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                        self._send(500, json.dumps(payload).encode(), "application/json")
                return
            if path == "/reveal":
                self._reveal(str(self._read_json().get("path") or ""))
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

    httpd = None
    chosen = port
    for candidate in range(port, port + 25):
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            chosen = candidate
            break
        except OSError:
            continue
    if httpd is None:
        raise RuntimeError(f"no free port in {port}..{port + 24}")

    url = f"http://127.0.0.1:{chosen}/"
    log(f"[repo_index] serving {root}")
    log(f"[repo_index] open {url}  — Refresh button / 'r' key regenerates in place; Ctrl-C to stop")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("[repo_index] stopped")
    finally:
        httpd.server_close()
    return 0
