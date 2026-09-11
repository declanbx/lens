"""``python -m repo_index`` entrypoint.

Delegates to :func:`repo_index.cli.main` and exits with its return code. See
CONTRACTS.md §11. Kept intentionally tiny so the orchestration logic lives in
cli.py.
"""

from __future__ import annotations

import sys


def _entry() -> int:
    # Imported here so a bare `import repo_index` stays cheap (cli pulls in the
    # rest of the pipeline lazily).
    from .cli import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(_entry())
