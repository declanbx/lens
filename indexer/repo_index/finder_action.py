"""repo_index.finder_action — Finder right-click integration for "reveal in app".

macOS 26 (Tahoe) reality (diagnosed empirically, not assumed): the two
auto-installable routes are dead ends for a generic any-file action —
- an Automator ``.workflow`` Service registers with ``pbs`` but Finder no longer
  surfaces it; and
- a LaunchServices "Open With" droplet that claims the abstract root UTI
  ``public.item`` is omitted from Finder's per-file menu by ``LSCopyApplicationURLsForURL``
  / ``NSWorkspace.urlsForApplications(toOpen:)`` for every file type.

The mechanism Apple actually supports for the right-click **Quick Actions** submenu
is a **Shortcuts.app Quick Action** (Receive Files → Run Shell Script). The
``shortcuts`` CLI can run/sign but NOT create/import a shortcut, and the Finder
"surface" is not encoded in any inspectable file field — so a generated ``.shortcut``
cannot be *verified* to appear. Rather than ship another unverifiable artifact, this
module emits a precise, pre-baked **recipe**: the exact Run Shell Script body (with
the repo root + CLI path already filled in) plus the click steps to build it once in
Shortcuts.app, where the GUI sets the Finder surface correctly. The action routes to
``repo_index reveal-in-app`` (verified-correct).

Stdlib-only; pure (no side effects beyond printing), so it is fully unit-testable.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Optional


def _shquote(s: str) -> str:
    """POSIX single-quote a string for safe embedding in a shell command (paths on
    this repo contain spaces)."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def command_prefix(python: Optional[str] = None) -> str:
    """Return the shell command prefix that invokes the repo_index CLI from a minimal
    environment (a Shortcuts "Run Shell Script" action does NOT inherit the user's
    login PATH). Prefers the installed ``repo_index`` console script (absolute,
    single-quoted); falls back to ``"<python>" -m repo_index`` with an absolute
    interpreter — both self-contained."""
    console = shutil.which("repo_index")
    if console:
        return _shquote(console)
    py = python or sys.executable
    return _shquote(py) + " -m repo_index"


def shell_body(root: Any, python: Optional[str] = None) -> str:
    """The exact Run Shell Script body for the Quick Action (Shell: zsh, Pass Input:
    *as arguments*). Finder passes the selected file paths as ``"$@"``; each is
    revealed in the app. Root + CLI path are baked in and space-safe."""
    root_q = _shquote(str(Path(root).resolve()))
    cmd = command_prefix(python)
    return 'for f in "$@"; do\n  ' + cmd + " reveal-in-app --root " + root_q + ' "$f"\ndone'


def recipe(root: Any, name: str = "Reveal in Repo Index", python: Optional[str] = None) -> str:
    """A copy-pasteable, click-by-click recipe to create the Finder Quick Action in
    Shortcuts.app (the only reliable route on macOS 26). Pure text."""
    body = shell_body(root, python)
    indented = "\n".join("      " + ln for ln in body.splitlines())
    return (
        "Create a Finder Quick Action in Shortcuts.app (one-time, ~1 min):\n\n"
        "  1. Open the Shortcuts app → File ▸ New Shortcut (⌘N).\n"
        f"  2. Name it: {name}\n"
        "  3. Add the action \"Run Shell Script\" (search the right-hand list).\n"
        "  4. In that action set:  Shell = zsh   ·   Pass Input = as arguments\n"
        "  5. Replace its script text with EXACTLY:\n\n"
        f"{indented}\n\n"
        "  6. Open the shortcut's settings (the ⓘ / 'Shortcut Details' panel) and:\n"
        "       • tick \"Use as Quick Action\"\n"
        "       • tick \"Finder\"\n"
        "       • set \"Receive\"  →  Files and Folders   (\"What's on screen\" off)\n"
        "  7. Save. Now right-click any file in Finder → Quick Actions → "
        f"\"{name}\".\n\n"
        "It runs `repo_index reveal-in-app` on the selected file, which jumps the\n"
        "always-open app to it (launching one if needed). Works on the exFAT drive\n"
        "and is independent of file type."
    )


def install(
    root: Any,
    dest: Optional[Path] = None,  # accepted for CLI symmetry; unused (no artifact written)
    name: str = "Reveal in Repo Index",
    python: Optional[str] = None,
) -> int:
    """Print the Shortcuts Quick Action recipe for ``root``. Returns 0.

    No file is written: on macOS 26 a generated ``.shortcut`` cannot be verified to
    surface in Finder (the Finder quick-action flag is set in the GUI, not an
    inspectable file field), and the Automator/Open-With routes are confirmed dead —
    so the honest, reliable deliverable is the exact recipe to build it once."""
    root = Path(root).resolve()
    if sys.platform != "darwin":
        print("[repo_index] the Finder Quick Action is macOS-only.", file=sys.stderr)
        return 2
    print(recipe(root, name=name, python=python), file=sys.stderr)
    return 0
