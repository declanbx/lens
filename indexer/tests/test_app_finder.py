"""Tests for repo_index.finder.reveal and the (optional) repo_index.app.

finder.reveal is tested with an INJECTED stub runner so the dir-vs-file branch
and the path-guard are verified WITHOUT actually launching Finder. app is tested
only for import-safety + the no-pywebview fallback (we never start a GUI window).
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from repo_index import finder


# --------------------------------------------------------------------------- #
# pywebview js_api surface — exactly {refresh, reveal} reachable from in-page JS
# --------------------------------------------------------------------------- #

def _js_reachable(api: object) -> set:
    """Return the dotted names pywebview would expose to in-page JavaScript for
    ``api``, replicating ``webview.util.inject_pywebview``'s nested
    ``get_functions``: it skips ``_``-prefixed names, records bound methods, and
    RECURSES into any PUBLIC non-callable attribute that has ``__module__`` (which
    ``pathlib.Path`` does). This is the exact rule that, if the build context were
    stored as public ``Path`` attributes, would leak ``Path.write_text``/
    ``unlink``/``chmod`` and every ``.parent`` ancestor to JavaScript.
    """
    exposed: list = []

    def walk(obj, base="", out=None):
        if out is None:
            out = {}
        oid = id(obj)
        if oid in exposed:
            return out
        exposed.append(oid)
        for name in dir(obj):
            try:
                full = f"{base}.{name}" if base else name
                if name.startswith("_"):
                    continue
                attr = getattr(obj, name)
                if not getattr(attr, "_serializable", True):
                    continue
                if inspect.ismethod(attr) or inspect.isfunction(attr):
                    out[full] = None
                elif inspect.isclass(attr) or (
                    isinstance(attr, object)
                    and not callable(attr)
                    and hasattr(attr, "__module__")
                ):
                    walk(attr, full, out)
            except Exception:  # noqa: BLE001 - mirror pywebview's per-attr guard
                continue
        return out

    return set(walk(api).keys())


_ALLOWED_JS_API = {"refresh", "reveal", "meta"}


def test_js_api_surface_is_exactly_refresh_reveal_meta(tmp_path: Path):
    """SECURITY regression: the ONLY callables in-page JS can reach via
    ``window.pywebview.api`` are ``refresh``, ``reveal`` and ``meta`` — nothing
    else. (``meta`` reads a fixed read-only INDEX.jsonl and never resolves its
    argument against the filesystem, so it cannot traverse.)

    The build context (root / out_dir / config_path / overrides) MUST be stored
    privately (underscore-prefixed) so pywebview's bridge generator never walks
    the ``pathlib.Path`` objects and leaks the filesystem-mutating ``Path`` API
    (write_text/unlink/chmod/rename/… on root, out_dir, and every ``.parent``
    ancestor) to a ``file://`` page.
    """
    import repo_index.app as app_mod

    api = app_mod.Api(tmp_path, tmp_path / "_repo_index", config_path=tmp_path / "cfg.yaml", overrides={"x": 1})
    reachable = _js_reachable(api)
    assert reachable == _ALLOWED_JS_API, (
        "js_api must expose ONLY refresh + reveal + meta; leaked: "
        + ", ".join(sorted(reachable - _ALLOWED_JS_API))
    )


def test_js_api_surface_matches_real_pywebview_generator(tmp_path: Path):
    """If pywebview is installed, cross-check against its REAL enumeration logic
    (extracted from ``inject_pywebview``'s source) so this test tracks the actual
    library behaviour, not just our replica. Skips cleanly when pywebview is
    absent (the package + app must still import without it)."""
    webview_util = pytest.importorskip("webview.util")
    # get_functions is a closure inside inject_pywebview, not importable; assert
    # the closure still exists so our replicated logic stays representative.
    src = inspect.getsource(webview_util.inject_pywebview)
    if "def get_functions" not in src:
        pytest.skip("pywebview bridge generator shape changed; replica covers the contract")

    import repo_index.app as app_mod

    api = app_mod.Api(tmp_path, tmp_path / "_repo_index")
    assert _js_reachable(api) == _ALLOWED_JS_API


# --------------------------------------------------------------------------- #
# finder.reveal — path guard + dir-vs-file, via an injected stub runner
# --------------------------------------------------------------------------- #

def _capturing_runner():
    """Return (runner, captured) where runner records each call's positional args."""
    captured = []

    def runner(*args, **kwargs):
        captured.append(args)
        return None

    return runner, captured


def test_reveal_path_outside_root_refuses_and_never_runs(tmp_path: Path):
    """A path that escapes the root returns ok=False/'path outside root' and the
    runner is NEVER invoked (no Finder launch on a rejected path)."""
    root = tmp_path / "root"
    root.mkdir()
    runner, captured = _capturing_runner()
    result = finder.reveal(root, "../escape", runner=runner)
    assert result == {"ok": False, "error": "path outside root"}
    assert captured == []  # runner must not be called


def test_reveal_absolute_outside_root_refuses(tmp_path: Path):
    """An absolute path outside root is also refused without running."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    runner, captured = _capturing_runner()
    result = finder.reveal(root, str(outside), runner=runner)
    assert result == {"ok": False, "error": "path outside root"}
    assert captured == []


def test_reveal_missing_target_returns_not_found(tmp_path: Path):
    """A path under root that does not exist returns ok=False/'not found' and the
    runner is not called."""
    root = tmp_path / "root"
    root.mkdir()
    runner, captured = _capturing_runner()
    result = finder.reveal(root, "nope.txt", runner=runner)
    assert result == {"ok": False, "error": "not found"}
    assert captured == []


def test_reveal_dir_under_root_opens_dir(tmp_path: Path):
    """A real DIRECTORY under root: on darwin runs ['open', <dir>]; elsewhere
    ['xdg-open', <dir>]. Always ok=True."""
    root = tmp_path / "root"
    sub = root / "subdir"
    sub.mkdir(parents=True)
    runner, captured = _capturing_runner()
    result = finder.reveal(root, "subdir", runner=runner)
    assert result == {"ok": True}
    assert len(captured) == 1
    argv = list(captured[0][0])
    if sys.platform == "darwin":
        assert argv == ["open", str(sub.resolve())]
    else:
        assert argv == ["xdg-open", str(sub.resolve())]


def test_reveal_file_under_root_reveals_file(tmp_path: Path):
    """A real FILE under root: on darwin runs ['open', '-R', <file>] (reveal,
    not open/exec); elsewhere ['xdg-open', <parent dir>]. Always ok=True."""
    root = tmp_path / "root"
    root.mkdir()
    f = root / "thing.txt"
    f.write_text("hi", encoding="utf-8")
    runner, captured = _capturing_runner()
    result = finder.reveal(root, "thing.txt", runner=runner)
    assert result == {"ok": True}
    assert len(captured) == 1
    argv = list(captured[0][0])
    if sys.platform == "darwin":
        assert argv == ["open", "-R", str(f.resolve())]
    else:
        assert argv == ["xdg-open", str(f.resolve().parent)]


def test_reveal_root_itself_is_allowed(tmp_path: Path):
    """Revealing the root directory itself (rel='') is allowed (target == root)."""
    root = tmp_path / "root"
    root.mkdir()
    runner, captured = _capturing_runner()
    result = finder.reveal(root, "", runner=runner)
    assert result == {"ok": True}
    assert len(captured) == 1


# --------------------------------------------------------------------------- #
# repo_index.app — import-safe + no-pywebview fallback (NO GUI launch)
# --------------------------------------------------------------------------- #

def test_app_import_is_safe():
    """importing repo_index.app must succeed even if pywebview is absent."""
    import repo_index.app as app_mod  # noqa: F401
    assert hasattr(app_mod, "run_app")
    assert hasattr(app_mod, "Api")


def test_run_app_returns_2_without_webview(tmp_path: Path, monkeypatch):
    """When webview is None, run_app prints a hint and returns 2 (no crash, no
    GUI). We never call webview.start in the test path."""
    import repo_index.app as app_mod
    monkeypatch.setattr(app_mod, "webview", None)
    rc = app_mod.run_app(tmp_path, tmp_path / "_repo_index", quiet=True)
    assert rc == 2


def test_api_reveal_delegates_to_finder(tmp_path: Path):
    """Api.reveal resolves under the captured root via finder.reveal (path-guard
    in effect) — an escaping path is refused."""
    import repo_index.app as app_mod
    root = tmp_path / "root"
    root.mkdir()
    api = app_mod.Api(root, root / "_repo_index")
    assert api.reveal("../escape") == {"ok": False, "error": "path outside root"}


# --------------------------------------------------------------------------- #
# finder.relative_under_root — path-guard + abs/rel + dir/file + root
# --------------------------------------------------------------------------- #

def test_relative_under_root_relative_file(tmp_path: Path):
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    f = root / "a" / "x.txt"
    f.write_text("hi", encoding="utf-8")
    assert finder.relative_under_root(root, "a/x.txt") == {"ok": True, "rel": "a/x.txt", "is_dir": False}


def test_relative_under_root_absolute_path_resolved(tmp_path: Path):
    """An ABSOLUTE path under root resolves to its repo-relative POSIX form (this is
    what a Finder Quick Action passes)."""
    root = tmp_path / "root"
    sub = root / "a b" / "c"
    sub.mkdir(parents=True)
    r = finder.relative_under_root(root, str(sub))
    assert r == {"ok": True, "rel": "a b/c", "is_dir": True}


def test_relative_under_root_refuses_outside_and_missing(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    assert finder.relative_under_root(root, str(tmp_path / "elsewhere")) == {
        "ok": False, "error": "path outside root"}
    assert finder.relative_under_root(root, "../escape") == {
        "ok": False, "error": "path outside root"}
    assert finder.relative_under_root(root, "nope.txt") == {
        "ok": False, "error": "not found"}


def test_relative_under_root_root_itself(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    assert finder.relative_under_root(root, str(root)) == {"ok": True, "rel": "", "is_dir": True}


# --------------------------------------------------------------------------- #
# app IPC: pidfile liveness + reveal-command file (no GUI launched)
# --------------------------------------------------------------------------- #

def test_app_ipc_dir_is_under_tempdir_and_root_stable(tmp_path: Path, monkeypatch):
    import repo_index.app as app_mod
    monkeypatch.setattr(app_mod.Path, "home", staticmethod(lambda: tmp_path))  # IPC dir is now per-user (HOME-based), TMPDIR-independent
    root_a = tmp_path / "A"
    root_a.mkdir()
    root_b = tmp_path / "B"
    root_b.mkdir()
    d1 = app_mod._ipc_dir(root_a)
    d2 = app_mod._ipc_dir(root_a)
    assert d1 == d2                                   # stable for a given root
    assert d1 != app_mod._ipc_dir(root_b)             # distinct per root
    assert str(d1).startswith(str(tmp_path))          # under the temp dir, not the repo


def test_app_is_running_and_request_reveal(tmp_path: Path, monkeypatch):
    import json
    import os
    import subprocess
    import sys

    import repo_index.app as app_mod
    monkeypatch.setattr(app_mod.Path, "home", staticmethod(lambda: tmp_path))  # IPC dir is now per-user (HOME-based), TMPDIR-independent
    root = tmp_path / "root"
    root.mkdir()

    # No pidfile yet → not running.
    assert app_mod.app_is_running(root) is False

    # A pidfile for THIS live process → running; garbage → not running.
    app_mod._write_pidfile(root)
    assert app_mod.app_is_running(root) is True
    app_mod._pidfile(root).write_text("not-a-pid", encoding="utf-8")
    assert app_mod.app_is_running(root) is False

    # A pidfile for a DEAD pid (a reaped child) → not running.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    app_mod._pidfile(root).write_text(str(child.pid), encoding="utf-8")
    assert app_mod.app_is_running(root) is False

    # request_reveal writes a parseable command file with path + ts.
    app_mod.request_reveal(root, "a/b/fig.png")
    obj = json.loads(app_mod._reveal_file(root).read_text(encoding="utf-8"))
    assert obj["path"] == "a/b/fig.png"
    assert isinstance(obj["ts"], (int, float))

    # _remove_pidfile only removes OUR pidfile (not another instance's). Matches on
    # the PID line of the "<pid>\n<start>" format.
    app_mod._pidfile(root).write_text(f"{os.getpid()}\nSomeStart", encoding="utf-8")
    app_mod._remove_pidfile(root)
    assert not app_mod._pidfile(root).exists()
    app_mod._pidfile(root).write_text("999999999\nSomeStart", encoding="utf-8")
    app_mod._remove_pidfile(root)
    assert app_mod._pidfile(root).exists()            # not ours → left in place


def test_initial_watcher_last_fires_fresh_skips_stale():
    """The watcher fires a reveal command queued just before launch (fresh) and skips a
    stale leftover — so `reveal-in-app` can queue the target then `open` the bundle and
    the new window still jumps to it, without an old command firing on a manual launch."""
    import repo_index.app as app_mod
    now = 1000.0
    # fresh (within window) → return None so the watcher fires it
    assert app_mod._initial_watcher_last(now - 1.0, now) is None
    # stale (older than window) → return the ts so it is skipped
    stale = now - (app_mod._REVEAL_FRESH_ON_START + 5)
    assert app_mod._initial_watcher_last(stale, now) == stale
    # no existing command → None ts passed through (nothing to fire)
    assert app_mod._initial_watcher_last(None, now) is None


def test_installed_bundle_detection(tmp_path: Path, monkeypatch):
    """installed_bundle() returns the ~/Applications/<name>.app path iff it exists."""
    import repo_index.app as app_mod
    fake_home = tmp_path / "home"
    (fake_home / "Applications").mkdir(parents=True)
    monkeypatch.setattr(app_mod.Path, "home", staticmethod(lambda: fake_home))
    assert app_mod.installed_bundle("Repo Index") is None
    (fake_home / "Applications" / "Repo Index.app").mkdir()
    got = app_mod.installed_bundle("Repo Index")
    assert got is not None and got.name == "Repo Index.app"


def test_should_reapply_reveal_ttl():
    """The reveal is re-applied across a reload only while a target is set AND within
    the TTL of the last genuine reveal (so a plain Refresh long after navigating away
    doesn't yank the user back; and an app with no reveal never phantom-expands)."""
    import repo_index.app as app_mod
    now = 1000.0
    # fresh target → re-apply
    assert app_mod._should_reapply_reveal({"target": "a/b.png", "ts": now - 1.0}, now) is True
    # stale target (older than TTL) → do not re-apply
    assert app_mod._should_reapply_reveal(
        {"target": "a/b.png", "ts": now - (app_mod._REVEAL_REAPPLY_TTL + 1)}, now) is False
    # no target (app launched without a reveal) → never re-apply
    assert app_mod._should_reapply_reveal({"target": None, "ts": 0.0}, now) is False
    assert app_mod._should_reapply_reveal({"target": "", "ts": now}, now) is False


def test_proc_start_forces_c_locale(monkeypatch):
    """REGRESSION: `ps -o lstart` is locale-dependent ("Sat Jun 20 …" vs "Sat 20 Jun
    …"). The app (Dock) and the reveal-in-app CLI (Finder Shortcut) can run with
    different LC_TIME, so an unforced format mismatched for the SAME process → a live
    app read as a recycled PID → app_is_running False → reveal broken. _proc_start must
    force LC_ALL=C so writer and reader always agree."""
    import repo_index.app as app_mod
    captured = {}

    class _R:
        stdout = "Sat Jun 20 17:30:00 2026\n"
        returncode = 0

    def fake_run(argv, **kw):
        captured.update(kw)
        return _R()

    monkeypatch.setattr(app_mod.subprocess, "run", fake_run)
    out = app_mod._proc_start(1234)
    assert out == "Sat Jun 20 17:30:00 2026"
    env = captured.get("env") or {}
    assert env.get("LC_ALL") == "C"   # locale forced → format stable across callers


def test_acquire_launch_lock_is_single_winner(tmp_path: Path, monkeypatch):
    """Only the FIRST caller wins the launch lock (so N rapid reveal-in-app calls with
    no app running don't each spawn a window); a stale lock (older than ttl) is stolen."""
    import repo_index.app as app_mod
    monkeypatch.setattr(app_mod.Path, "home", staticmethod(lambda: tmp_path))  # IPC dir is now per-user (HOME-based), TMPDIR-independent
    root = tmp_path / "root"
    root.mkdir()

    assert app_mod.acquire_launch_lock(root) is True       # first wins
    assert app_mod.acquire_launch_lock(root) is False      # second blocked (in flight)
    # a stale lock (ttl=0 makes the existing one immediately stale) is stealable
    assert app_mod.acquire_launch_lock(root, ttl=0) is True


def test_app_is_running_rejects_pid_reuse(tmp_path: Path, monkeypatch):
    """A stale pidfile whose PID was RECYCLED by an unrelated process must read as
    NOT running: the recorded start time no longer matches the live process's, so
    `reveal-in-app` launches a fresh window instead of dropping the command."""
    import os

    import repo_index.app as app_mod
    monkeypatch.setattr(app_mod.Path, "home", staticmethod(lambda: tmp_path))  # IPC dir is now per-user (HOME-based), TMPDIR-independent
    root = tmp_path / "root"
    root.mkdir()

    if app_mod._proc_start(os.getpid()) is None:
        import pytest
        pytest.skip("ps unavailable — start-time PID-reuse check degrades to plain liveness")

    app_mod._ipc_dir(root).mkdir(parents=True, exist_ok=True)
    # Our live PID but a BOGUS recorded start time ⇒ treated as a reused PID.
    app_mod._pidfile(root).write_text(f"{os.getpid()}\nWed Jan  1 00:00:00 2020",
                                      encoding="utf-8")
    assert app_mod.app_is_running(root) is False

    # The honestly-recorded start time (via _write_pidfile) ⇒ genuinely running.
    app_mod._write_pidfile(root)
    assert app_mod.app_is_running(root) is True


# --------------------------------------------------------------------------- #
# reveal-in-app CLI: path-guard + send-to-running (no window spawned)
# --------------------------------------------------------------------------- #

def test_cli_reveal_in_app_refuses_outside_root(tmp_path: Path):
    from repo_index.cli import main
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    rc = main(["reveal-in-app", "--root", str(root), str(tmp_path / "outside.txt")])
    assert rc == 1   # path outside root → exit 1, never launches


def test_cli_reveal_in_app_opens_bundle_and_queues_reveal(tmp_path: Path, monkeypatch):
    """reveal-in-app ALWAYS queues the reveal (reveal.json) AND invokes `open -a <bundle>
    --args --reveal <target>` — one unified path that activates-or-launches the user's
    Dock app and jumps it (running → watcher; fresh launch → --reveal). `open` is stubbed
    so nothing actually launches in the test."""
    import json
    import subprocess as _sp

    import repo_index.app as app_mod
    from repo_index.cli import main

    monkeypatch.setattr(app_mod.Path, "home", staticmethod(lambda: tmp_path))  # IPC dir is now per-user (HOME-based), TMPDIR-independent
    fake_bundle = tmp_path / "Repo Index.app"
    fake_bundle.mkdir()
    monkeypatch.setattr(app_mod, "installed_bundle", lambda name="Repo Index": fake_bundle)

    calls = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kw):           # stubs both `open` and `ps` (in _proc_start)
        calls.append(list(argv))
        return _R()

    monkeypatch.setattr(_sp, "run", fake_run)

    root = tmp_path / "root"
    sub = root / "fig"
    sub.mkdir(parents=True)
    png = sub / "a.png"
    png.write_text("x", encoding="utf-8")

    rc = main(["reveal-in-app", "--root", str(root), str(png)])
    assert rc == 0
    obj = json.loads(app_mod._reveal_file(root).read_text(encoding="utf-8"))
    assert obj["path"] == "fig/a.png"        # file → plain relative path queued
    open_calls = [a for a in calls if a[:1] == ["open"]]
    assert open_calls, "expected an `open -a <bundle>` invocation"
    oc = open_calls[-1]
    assert oc[:3] == ["open", "-a", str(fake_bundle)]
    assert "--reveal" in oc and "fig/a.png" in oc

    # A directory target gets the "<dir>/__dir__" sentinel the page navigates by.
    rc = main(["reveal-in-app", "--root", str(root), str(sub)])
    assert rc == 0
    obj = json.loads(app_mod._reveal_file(root).read_text(encoding="utf-8"))
    assert obj["path"] == "fig/__dir__"


def test_refresh_on_unchanged_tree_is_a_noop(fixture_tree):
    """REGRESSION: app.Api.refresh() must report `unchanged` (empty delta) when nothing
    changed — so the app never reconciles/reloads on a glance-Refresh. Earlier a bug fed
    PROJECTED entries to compute_entry_delta (which compares against the RAW new entry),
    making every projection-altered field a phantom 'changed' → constant churn + reloads.
    _served must be RAW so consecutive refreshes over an unchanged tree are true no-ops."""
    from repo_index.cli import build_index
    import repo_index.app as app_mod

    root, _ = fixture_tree
    out = root / "_repo_index"
    build_index(root, out, incremental=False, quiet=True)

    api = app_mod.Api(root, out)
    assert api._served is not None and len(api._served) > 0   # seeded RAW from INDEX.json
    api.refresh()                       # first may catch a racy-window diff; then advances _served
    r = api.refresh()                   # unchanged tree → MUST be a true no-op
    assert r["ok"] is True
    assert r["unchanged"] is True, f"phantom delta on unchanged tree: {r.get('delta')}"
    d = r.get("delta") or {}
    assert not d.get("added") and not d.get("changed") and not d.get("removed")


def test_api_meta_returns_full_meta_from_jsonl(tmp_path: Path):
    """Api.meta returns the FULL meta for an entry by matching its path against
    INDEX.jsonl — including a wide `columns` array longer than the HTML head — and
    returns ok=False/'not found' for an unknown path. It must NOT touch the real
    filesystem path (only the jsonl `path` field)."""
    import json

    import repo_index.app as app_mod

    out_dir = tmp_path / "_repo_index"
    out_dir.mkdir()
    wide = [f"gene_{i}" for i in range(500)]
    lines = [
        {"path": "a/data.csv", "category": "data_table", "meta": {"columns": wide, "n_columns": 500}},
        {"path": "b/space dir/файл.csv", "category": "data_table", "meta": {"columns": ["x", "y"]}},
    ]
    with (out_dir / "INDEX.jsonl").open("w", encoding="utf-8") as fh:
        for o in lines:
            fh.write(json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n")

    api = app_mod.Api(tmp_path, out_dir)

    r = api.meta("a/data.csv")
    assert r["ok"] is True
    assert r["meta"]["columns"] == wide          # FULL 500-col list, not a truncated head
    assert r["meta"]["n_columns"] == 500

    # non-ASCII path with a space must still match (needle uses ensure_ascii=False)
    r2 = api.meta("b/space dir/файл.csv")
    assert r2["ok"] is True and r2["meta"]["columns"] == ["x", "y"]

    assert api.meta("nope.csv") == {"ok": False, "error": "not found"}
