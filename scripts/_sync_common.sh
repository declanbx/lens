#!/usr/bin/env bash
# Shared plumbing for sync-from-home.sh / sync-to-home.sh.
#
# WHY THIS EXISTS
#   Lens is developed in a working copy that lives inside a much larger research repository
#   (the "home" copy). This repository is the standalone, shareable release of the same code.
#   These scripts move changes between the two WITHOUT either side ever being surprised:
#   every run is a dry run unless you pass --apply, and every run prints exactly which files
#   would change before anything is written.
#
# THE MAPPING (the two trees are not laid out the same way)
#   home/lens/         <->  release/app/
#   home/repo_index/   <->  release/indexer/repo_index/
#
# Everything else in the release repo (README, HOW_IT_WORKS, scripts/, docs/, .gitignore)
# is release-only and is NEVER synced in either direction.
set -euo pipefail

# The home working copy — the Lens sources inside the larger research repository.
#
# Deliberately NOT hardcoded: this repository is public, and the path names a private
# research folder. Resolution order: the LENS_HOME environment variable, else a
# .lens-home file in the repository root (one line, gitignored, yours alone).
_home_file="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.lens-home"
if [ -n "${LENS_HOME:-}" ]; then
  HOME_DIR="$LENS_HOME"
elif [ -f "$_home_file" ]; then
  HOME_DIR="$(sed -e 's/[[:space:]]*$//' -e '/^[[:space:]]*$/d' -e '/^#/d' "$_home_file" | head -1)"
else
  echo "Set LENS_HOME to your working copy of the research repo's tools/repo_index," >&2
  echo "or write that path into $_home_file (one line)." >&2
  exit 2
fi
if [ -z "$HOME_DIR" ]; then
  echo "No home working copy resolved — LENS_HOME is empty and $_home_file has no path." >&2
  exit 2
fi

# This repository (the script lives in <repo>/scripts/).
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Never move build output, caches, editor droppings, macOS resource forks, or a built index.
RSYNC_EXCLUDES=(
  --exclude 'node_modules/'
  --exclude 'target/'
  --exclude 'dist/'
  --exclude 'gen/'
  --exclude '__pycache__/'
  --exclude '*.egg-info/'
  --exclude '.pytest_cache/'
  --exclude '_repo_index/'
  --exclude '.git/'
  --exclude '.DS_Store'
  --exclude '._*'
  --exclude '.venv/'
  # ── Files that live in the HOME copy but were deliberately relocated (or dropped) when this
  #    standalone repo was built. Excluding them in BOTH directions stops a sync from re-adding
  #    development clutter here, and stops --delete from removing it from the home copy there.
  #    Long-form design specs now live in this repo under docs/internal/.
  --exclude 'CONTRACT.md'
  --exclude 'FINDER_ROADMAP.md'
  --exclude 'HANDOFF.md'
  --exclude 'LIVE_INDEX_PLAN.md'
  --exclude 'PERF_OPTIMIZATIONS.md'
  --exclude 'PERF_PLAN_*.md'
  --exclude 'PHASE_0_1_SPEC*.md'
  --exclude 'SEARCH_BUGHUNT_*.md'
  --exclude 'SEARCH_REDESIGN_*.md'
  --exclude '_design/'
  --exclude '_perf_audit_*/'
  --exclude '_writer_lock_fix_*/'
  --exclude '_pathkey_oracle_scratch/'
  --exclude '_ops_scratch_*/'
  --exclude '_helper_scratch_*/'
  --exclude 'mock_data.js'
  --exclude 'harness.html'
  --exclude '*.bak.css'
  --exclude '*.bak.md'
)

APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    -h|--help) SHOW_HELP=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

require_dirs() {
  [ -d "$HOME_DIR/lens" ]       || die "no home working copy at: $HOME_DIR/lens
       (is the external drive mounted? override with LENS_HOME=/path/to/tools/repo_index)"
  [ -d "$HOME_DIR/repo_index" ] || die "no crawler at: $HOME_DIR/repo_index"
  [ -d "$REPO_DIR/app" ]        || die "this does not look like the Lens release repo: $REPO_DIR"
}

# Refuse to overwrite anything that is not already safe in git, so every sync is undoable
# with `git checkout .` or `git stash`.
require_clean_git() {
  local target_desc="$1"
  if ! git -C "$REPO_DIR" diff --quiet || ! git -C "$REPO_DIR" diff --cached --quiet; then
    die "the release repo has uncommitted changes.
       Commit or stash them first, so this sync can be undone if it is wrong.
       Affected: $target_desc
       See: git -C \"$REPO_DIR\" status"
  fi
}

# Print the file-level plan (rsync --itemize-changes, read as: '>f' = file content differs,
# '*deleting' = file exists only on the destination side).
plan() {
  local src="$1" dst="$2" label="$3"
  echo
  echo "── $label"
  echo "   from: $src"
  echo "     to: $dst"
  local out
  out="$(rsync -a --delete --itemize-changes --dry-run "${RSYNC_EXCLUDES[@]}" "$src" "$dst" || true)"
  if [ -z "$out" ]; then
    echo "   (already identical)"
  else
    echo "$out" | sed 's/^/   /'
  fi
}

run() {
  local src="$1" dst="$2"
  rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$src" "$dst"
}

footer() {
  echo
  if [ "$APPLY" -eq 1 ]; then
    echo "✓ applied."
  else
    echo "This was a DRY RUN — nothing was written."
    echo "Re-run with --apply to make these changes."
  fi
}
