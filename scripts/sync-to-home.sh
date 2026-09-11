#!/usr/bin/env bash
# Push changes from this release repository back to the HOME working copy.
#
#   ./scripts/sync-to-home.sh            # show what would change (safe, writes nothing)
#   ./scripts/sync-to-home.sh --apply    # actually copy them over
#
# Use this after changing Lens here (or after pulling someone else's commits) to bring the
# copy inside the big research repo back in step.
#
# ⚠ The home copy is tracked by the research repository's own git. Run `git status` there
#   afterwards and commit deliberately — this script does not commit for you.
source "$(dirname "${BASH_SOURCE[0]}")/_sync_common.sh"

if [ "${SHOW_HELP:-0}" = 1 ]; then sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0; fi

require_dirs

echo "PUSH  release repo ──▶ home"
echo "      release: $REPO_DIR"
echo "      home:    $HOME_DIR"

plan "$REPO_DIR/app/"                "$HOME_DIR/lens/"       "lens/        (the desktop application)"
plan "$REPO_DIR/indexer/repo_index/" "$HOME_DIR/repo_index/" "repo_index/  (the Python crawler)"

if [ "$APPLY" -eq 1 ]; then
  echo
  echo "About to overwrite files inside the research repository."
  printf "Type 'yes' to continue: "
  read -r reply
  [ "$reply" = "yes" ] || { echo "aborted."; exit 1; }
  run "$REPO_DIR/app/"                "$HOME_DIR/lens/"
  run "$REPO_DIR/indexer/repo_index/" "$HOME_DIR/repo_index/"
  echo
  echo "Now review there:"
  echo "    git -C \"$(dirname "$(dirname "$HOME_DIR")")\" status"
fi
footer
