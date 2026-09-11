"""Tests for repo_index.finder_action — the Shortcuts Quick Action recipe.

The module is pure (it emits text, no artifacts), so everything is unit-testable: the
Run Shell Script body must reveal each selected file via the verified-correct
`reveal-in-app` CLI with the root baked in space-safely, and the recipe must tell the
user to set the Finder surface (the GUI step that — per the macOS 26 diagnosis — is
the only reliable way to surface a Quick Action).
"""

from __future__ import annotations

from pathlib import Path

from repo_index import finder_action as fa


def test_shell_body_reveals_each_file_space_safe():
    root = "/tmp/repo root with spaces"
    body = fa.shell_body(root, python="/usr/bin/python3")
    assert 'for f in "$@"' in body            # iterates the Finder selection
    assert "reveal-in-app" in body
    assert "--root" in body
    assert '"$f"' in body                       # each path quoted
    # the resolved root is single-quoted (space-safe)
    assert "'" + str(Path(root).resolve()) + "'" in body
    # NOT the dead python3-executes-the-file pattern
    assert "python3 \"$f\"" not in body


def test_command_prefix_is_self_contained():
    """Absolute (console script OR '<python> -m repo_index') so the Shortcut runs
    without the user's login PATH."""
    pref = fa.command_prefix(python="/usr/bin/python3")
    assert pref.startswith("'")
    assert "repo_index" in pref


def test_recipe_sets_finder_surface_and_input():
    root = "/tmp/x"
    text = fa.recipe(root, name="Reveal in Repo Index", python="/usr/bin/python3")
    # the GUI steps that make it a Finder Quick Action receiving files
    assert "Run Shell Script" in text
    assert "Use as Quick Action" in text
    assert "Finder" in text
    assert "Files and Folders" in text
    assert "as arguments" in text
    # contains the actual command to paste
    assert "reveal-in-app" in text
    assert "Reveal in Repo Index" in text
