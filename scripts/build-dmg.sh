#!/usr/bin/env bash
# Build a distributable Lens.dmg.
#
#   ./scripts/build-dmg.sh              # universal (Apple Silicon + Intel) — the default
#   ./scripts/build-dmg.sh --native     # only this Mac's architecture; much faster, for testing
#
# Output lands in ./release/ and the script prints the exact path at the end.
#
# The resulting app is NOT signed with an Apple Developer ID (that needs a paid Apple account),
# so the first launch on another Mac needs a one-time right-click ▸ Open. The README explains
# this to whoever you send it to.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_DIR/app"
OUT_DIR="$REPO_DIR/release"

TARGET="universal-apple-darwin"
TARGET_LABEL="universal (Apple Silicon + Intel)"
for arg in "$@"; do
  case "$arg" in
    --native) TARGET=""; TARGET_LABEL="native ($(uname -m)) only" ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── preflight ───────────────────────────────────────────────────────────────────────────────
step "Checking the toolchain"
command -v node  >/dev/null || die "node is not installed — see README ▸ Building from source"
command -v npm   >/dev/null || die "npm is not installed"
command -v cargo >/dev/null || die "Rust is not installed — https://rustup.rs"
echo "  node  $(node -v)"
echo "  cargo $(cargo --version | awk '{print $2}')"

if [ -n "$TARGET" ]; then
  # A universal binary needs BOTH architecture targets for the pinned toolchain.
  TOOLCHAIN="$(awk -F'"' '/^channel/{print $2}' "$APP_DIR/src-tauri/rust-toolchain.toml" 2>/dev/null || true)"
  for arch in aarch64-apple-darwin x86_64-apple-darwin; do
    if ! rustup target list --installed ${TOOLCHAIN:+--toolchain "$TOOLCHAIN"} 2>/dev/null | grep -qx "$arch"; then
      echo "  installing missing build target: $arch"
      rustup target add "$arch" ${TOOLCHAIN:+--toolchain "$TOOLCHAIN"}
    fi
  done
fi

# Keep build output inside the repo (gitignored) so this never depends on, or collides with,
# a target directory belonging to another checkout on the same machine.
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$REPO_DIR/app/src-tauri/target}"
echo "  build dir  $CARGO_TARGET_DIR"
echo "  target     $TARGET_LABEL"

# ── frontend dependencies ───────────────────────────────────────────────────────────────────
step "Installing frontend dependencies"
cd "$APP_DIR"
if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi

# ── build ───────────────────────────────────────────────────────────────────────────────────
step "Building the app (this takes several minutes the first time)"
if [ -n "$TARGET" ]; then
  npx tauri build --target "$TARGET"
  BUNDLE_DIR="$CARGO_TARGET_DIR/$TARGET/release/bundle"
else
  npx tauri build
  BUNDLE_DIR="$CARGO_TARGET_DIR/release/bundle"
fi

# ── collect ─────────────────────────────────────────────────────────────────────────────────
step "Collecting the installer"
DMG="$(find "$BUNDLE_DIR/dmg" -name '*.dmg' -maxdepth 1 2>/dev/null | head -1 || true)"
APP="$(find "$BUNDLE_DIR/macos" -name '*.app' -maxdepth 1 2>/dev/null | head -1 || true)"
[ -n "$DMG" ] || die "no .dmg was produced — look for the error above (searched $BUNDLE_DIR/dmg)"

mkdir -p "$OUT_DIR"
cp -f "$DMG" "$OUT_DIR/"
[ -n "$APP" ] && { rm -rf "$OUT_DIR/$(basename "$APP")"; cp -R "$APP" "$OUT_DIR/"; }

printf '\n\033[1;32m✓ Done\033[0m\n'
echo "  installer : $OUT_DIR/$(basename "$DMG")  ($(du -h "$OUT_DIR/$(basename "$DMG")" | cut -f1))"
[ -n "$APP" ] && echo "  app       : $OUT_DIR/$(basename "$APP")"
echo
echo "  Send the .dmg. On first launch the recipient must right-click the app ▸ Open"
echo "  (macOS blocks a double-click on an app that is not signed by a paid Apple account)."
