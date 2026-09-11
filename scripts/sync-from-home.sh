#!/usr/bin/env bash
# Pull changes from the HOME working copy into this release repository.
#
#   ./scripts/sync-from-home.sh            # show what would change (safe, writes nothing)
#   ./scripts/sync-from-home.sh --apply    # actually copy them in
#
# Use this after you have changed Lens in the big research repo and want those changes here,
# ready to commit and push to GitHub.
#
# It refuses to run if this repo has uncommitted changes, so whatever it does can always be
# undone with `git checkout .`.
source "$(dirname "${BASH_SOURCE[0]}")/_sync_common.sh"

if [ "${SHOW_HELP:-0}" = 1 ]; then sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0; fi

require_dirs
[ "$APPLY" -eq 1 ] && require_clean_git "the app/ and indexer/repo_index/ trees"

echo "PULL  home ──▶ release repo"
echo "      home:    $HOME_DIR"
echo "      release: $REPO_DIR"

plan "$HOME_DIR/lens/"       "$REPO_DIR/app/"                "app/        (the desktop application)"
plan "$HOME_DIR/repo_index/" "$REPO_DIR/indexer/repo_index/" "indexer/    (the Python crawler)"

if [ "$APPLY" -eq 1 ]; then
  run "$HOME_DIR/lens/"       "$REPO_DIR/app/"
  run "$HOME_DIR/repo_index/" "$REPO_DIR/indexer/repo_index/"
  echo
  echo "Now review and commit:"
  echo "    git -C \"$REPO_DIR\" diff"
  echo "    git -C \"$REPO_DIR\" commit -am 'sync from home'"
fi
footer
