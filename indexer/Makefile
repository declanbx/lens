# repo_index Makefile
#
# Targets:
#   index        -- regenerate the repo index (_repo_index/) for the repository root
#   check        -- drift check: re-walk + compare digest to committed INDEX.json (exit 1 on drift)
#   test         -- run the pytest suite
#   install-hook -- install the self-heal pre-commit hook into .git/hooks
#
# Space-safety: the repository path contains spaces. GNU Make cannot reliably
# carry space-containing paths in Make *variables*, and `.ONESHELL` is unavailable
# on GNU Make < 3.82 (macOS ships 3.81), so each recipe is written as ONE shell
# command that resolves the paths inside the shell (quoted) via `git rev-parse`.
# Shell `$(...)`/`$$var` are escaped as `$$(...)`/`$$var` for Make.

SHELL := /bin/bash
PY ?= python3

.DEFAULT_GOAL := index

.PHONY: index check test install-hook open serve app help

help:
	@echo "repo_index make targets:"
	@echo "  make index         regenerate _repo_index/ for the repository root"
	@echo "  make check         drift check (exit 1 if the index is stale)"
	@echo "  make test          run the pytest suite"
	@echo "  make install-hook  install the self-heal pre-commit hook"
	@echo "  make open          open the generated INDEX.html in your browser"
	@echo "  make serve         serve on localhost with an in-page Refresh button"
	@echo "  make app           open the always-on native window (needs pywebview)"

# Regenerate the index for the whole repository root. Stdlib-only core, so this
# runs without the optional [full] extras installed.
index:
	@set -eu; \
	repo_root="$$(git rev-parse --show-toplevel)"; \
	tool_dir="$$repo_root/tools/repo_index"; \
	echo "repo_index: indexing $$repo_root ..."; \
	PYTHONPATH="$$tool_dir" "$(PY)" -m repo_index --root "$$repo_root" --out "$$repo_root/_repo_index"

# Open the generated INDEX.html in the default browser (builds nothing).
open:
	@set -eu; \
	repo_root="$$(git rev-parse --show-toplevel)"; \
	tool_dir="$$repo_root/tools/repo_index"; \
	PYTHONPATH="$$tool_dir" "$(PY)" -m repo_index open --root "$$repo_root"

# Serve the index on localhost; the page's Refresh button regenerates in place.
serve:
	@set -eu; \
	repo_root="$$(git rev-parse --show-toplevel)"; \
	tool_dir="$$repo_root/tools/repo_index"; \
	PYTHONPATH="$$tool_dir" "$(PY)" -m repo_index serve --root "$$repo_root"

# Open the always-on native window (single WKWebView, no http server). Needs the
# optional `app` extra (pywebview); without it the command prints an install hint.
app:
	@set -eu; \
	repo_root="$$(git rev-parse --show-toplevel)"; \
	tool_dir="$$repo_root/tools/repo_index"; \
	PYTHONPATH="$$tool_dir" "$(PY)" -m repo_index app --root "$$repo_root"

# Verify the committed index against the live filesystem; exit 1 on drift.
check:
	@set -eu; \
	repo_root="$$(git rev-parse --show-toplevel)"; \
	tool_dir="$$repo_root/tools/repo_index"; \
	PYTHONPATH="$$tool_dir" "$(PY)" -m repo_index check --root "$$repo_root"

# Run the test suite. PYTHONPATH points at the package dir so the tests import
# repo_index without an install; the tests build their own tmp fixtures.
test:
	@set -eu; \
	repo_root="$$(git rev-parse --show-toplevel)"; \
	tool_dir="$$repo_root/tools/repo_index"; \
	PYTHONPATH="$$tool_dir" "$(PY)" -m pytest "$$tool_dir/tests" -q

# Install the self-heal pre-commit hook (symlink so it tracks this file).
install-hook:
	@set -eu; \
	repo_root="$$(git rev-parse --show-toplevel)"; \
	git_dir="$$(git rev-parse --git-dir)"; \
	hooks_dir="$$git_dir/hooks"; \
	mkdir -p "$$hooks_dir"; \
	src="$$repo_root/tools/repo_index/hooks/pre-commit"; \
	dst="$$hooks_dir/pre-commit"; \
	ln -sf "$$src" "$$dst"; \
	chmod +x "$$src"; \
	echo "repo_index: installed pre-commit hook -> $$dst"
