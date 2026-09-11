"""repo_index.finder — shared, testable "reveal in Finder" logic.

Both the localhost server (:mod:`repo_index.serve`) and the always-open pywebview
app (:mod:`repo_index.app`) need to resolve a relative path under the repo root
and reveal it in the OS file browser. That logic lives here ONCE so the two
front-ends stay DRY and so the path-guard / dir-vs-file behaviour can be unit
tested WITHOUT actually launching Finder (the ``runner`` is injectable).

``open -R`` REVEALS (selects) a file in Finder — it does NOT open or execute it,
so revealing a script/binary is safe. A directory is opened directly (``open``).
On non-darwin platforms ``xdg-open`` is used (the parent dir for a file).

Stdlib-only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict


def relative_under_root(root: Any, path: str) -> Dict[str, Any]:
    """Resolve ``path`` (absolute OR relative) against ``root`` and return its
    repo-relative POSIX form, with the SAME path-guard as :func:`reveal`.

    Returns ``{"ok": True, "rel": "<posix>", "is_dir": bool}`` (``rel`` is ``""``
    for the root itself), or ``{"ok": False, "error": "path outside root"}`` /
    ``{"ok": False, "error": "not found"}``. Used by the ``reveal-in-app`` CLI to
    turn a Finder-supplied absolute path into the relative path the app navigates
    by (and the app/serve front-ends never see an out-of-tree path). ``root_r /
    path`` correctly handles an absolute ``path`` (the absolute operand wins) and a
    relative one (joined under root)."""
    root_r = Path(root).resolve()
    target = (root_r / path).resolve()
    if target != root_r and root_r not in target.parents:
        return {"ok": False, "error": "path outside root"}
    if not target.exists():
        return {"ok": False, "error": "not found"}
    rel = "" if target == root_r else target.relative_to(root_r).as_posix()
    return {"ok": True, "rel": rel, "is_dir": target.is_dir()}


def reveal(root: Any, rel: str, runner: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """Reveal ``root/rel`` in the OS file browser; return a JSON-serializable dict.

    Resolves ``target = (Path(root).resolve() / rel).resolve()`` and REFUSES
    anything outside ``root`` (no path traversal): if ``target`` is neither the
    root itself nor a descendant of it, returns ``{"ok": False, "error": "path
    outside root"}`` WITHOUT calling ``runner``. If the target does not exist,
    returns ``{"ok": False, "error": "not found"}``.

    Otherwise, on darwin: ``runner(["open", "-R", <file>])`` reveals/selects a
    FILE, ``runner(["open", <dir>])`` opens a DIRECTORY. On other platforms:
    ``runner(["xdg-open", <dir-or-file's-parent>])``. Returns ``{"ok": True}``.

    ``runner`` defaults to :func:`subprocess.run`; tests inject a stub to verify
    the dir-vs-file branch and the path-guard without launching anything.
    """
    root_r = Path(root).resolve()
    target = (root_r / rel).resolve()
    # Path guard: target must be the root or a descendant of it.
    if target != root_r and root_r not in target.parents:
        return {"ok": False, "error": "path outside root"}
    if not target.exists():
        return {"ok": False, "error": "not found"}
    is_dir = target.is_dir()
    if sys.platform == "darwin":
        if is_dir:
            runner(["open", str(target)])          # open the folder
        else:
            runner(["open", "-R", str(target)])    # reveal/select the file
    else:
        opent = target if is_dir else target.parent
        runner(["xdg-open", str(opent)])
    return {"ok": True}
