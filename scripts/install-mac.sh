#!/bin/bash
# Installs (or updates) LimitSwitcher on macOS from the latest GitHub release:
#   curl -fsSL https://raw.githubusercontent.com/doge006/LimitSwitcher/main/scripts/install-mac.sh | bash
# It downloads the Apple silicon DMG, closes a running copy (which puts the
# Codex and Claude Code settings back), puts LimitSwitcher.app in Applications and opens it.
# Saved accounts and settings are in ~/Library/Application Support/LimitSwitcher and are kept.
#
# The app isn't notarized by Apple. macOS checks apps downloaded by a browser, and warns about
# those that aren't; a download made here (curl) isn't marked as coming from the internet, so
# there's no warning. With --dmg <file or URL>, it installs that DMG instead (testing a build);
# a file a browser downloaded has that mark, which is removed from the installed copy.
# The app's own updater runs this too (with --from-app).
set -euo pipefail

REPO="doge006/LimitSwitcher"
DMG=""
FROM_APP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dmg) DMG="$2"; shift 2 ;;
    --from-app) FROM_APP=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[36m%s\033[0m\n' "$1"; }
fail() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "This installs the macOS app. On Windows, use LimitSwitcher-Setup.exe from the releases page."
# Apple silicon only (true even from a Terminal running under Rosetta).
KIND=AppleSilicon
if [ -z "$DMG" ] && [ "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != 1 ]; then
  fail "LimitSwitcher for Mac needs Apple silicon (M1 or later); this Mac has an Intel processor."
fi

# /Applications when this user can write there (admin accounts can), else ~/Applications.
if [ -w /Applications ]; then TARGET=/Applications; else TARGET="$HOME/Applications"; mkdir -p "$TARGET"; fi
APP="$TARGET/LimitSwitcher.app"

WORK="$(mktemp -d)"
MOUNT="$WORK/mount"
cleanup() {
  hdiutil detach -quiet "$MOUNT" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# 1. The disk image: the latest release's, or the newest pre-release's while there is no release.
if [ -z "$DMG" ]; then
  DMG="https://github.com/$REPO/releases/latest/download/LimitSwitcher-$KIND.dmg"
  if ! curl -fsIL -o /dev/null "$DMG" 2>/dev/null; then
    DMG="$(curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=10" 2>/dev/null \
      | grep -o "\"browser_download_url\": *\"[^\"]*/LimitSwitcher-$KIND\.dmg\"" | head -n 1 | sed 's/.*"\(https[^"]*\)"$/\1/' || true)"
    [ -n "$DMG" ] || fail "No release to install yet (github.com/$REPO/releases)."
  fi
fi
case "$DMG" in
  http://*|https://*)
    say "Downloading LimitSwitcher..."
    curl -fL --progress-bar -o "$WORK/LimitSwitcher.dmg" "$DMG" || fail "The download failed ($DMG)."
    DMG="$WORK/LimitSwitcher.dmg" ;;
  *) [ -f "$DMG" ] || fail "No such file: $DMG" ;;
esac
mkdir -p "$MOUNT"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MOUNT" "$DMG" || fail "Couldn't open the disk image."
[ -d "$MOUNT/LimitSwitcher.app" ] || fail "The disk image has no LimitSwitcher.app."

# 2. Close the running copy (any kind: this one, or one installed from source), so it puts the
#    Codex and Claude Code settings back. Its launcher.conf says which Python and script it runs.
quit_copy() {
  local app="$1" conf="$1/Contents/Resources/launcher.conf"
  [ -f "$conf" ] || return 0
  local python script
  python="$(sed -n 2p "$conf")"; script="$(sed -n 3p "$conf")"
  case "$python" in /*) ;; *) python="$app/Contents/Resources/$python" ;; esac
  case "$script" in /*) ;; *) script="$app/Contents/Resources/$script" ;; esac
  [ -x "$python" ] && [ -f "$script" ] && "$python" -B "$script" --quit >/dev/null 2>&1 || true
}
for existing in /Applications/LimitSwitcher.app "$HOME/Applications/LimitSwitcher.app" \
                /Applications/LimitSwitch.app "$HOME/Applications/LimitSwitch.app"; do
  if [ -d "$existing" ]; then
    [ $FROM_APP = 1 ] || say "Closing the running LimitSwitcher..."
    quit_copy "$existing"
  fi
done
# Wait for every LimitSwitcher process to end (the full view has its own, which closes with the
# app); a full view window still open after 10 s is closed so the app can be replaced.
for _ in $(seq 1 40); do
  # By name too: macOS can hide a process's arguments (and so its path) while it is ending.
  pgrep -f '/Contents/MacOS/LimitSwitch' >/dev/null 2>&1 || pgrep -x LimitSwitcher >/dev/null 2>&1 || break
  sleep 0.25
done
pkill -f 'LimitSwitcher --full-view' >/dev/null 2>&1 || true
sleep 0.5

# 3. Replace the app (one copy only; also those from before the renames).
say "Installing to $TARGET..."
for old in /Applications/LimitSwitcher.app "$HOME/Applications/LimitSwitcher.app" \
           /Applications/LimitSwitch.app "$HOME/Applications/LimitSwitch.app" \
           "/Applications/Account Switcher.app" "$HOME/Applications/Account Switcher.app"; do
  if [ -d "$old" ]; then rm -rf "$old" 2>/dev/null || fail "Couldn't remove the old $old (try closing it first)."; fi
done
ditto "$MOUNT/LimitSwitcher.app" "$APP"
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
codesign --verify --deep --strict "$APP" 2>/dev/null || fail "The installed app failed its signature check; download it again."

# 4. Start it: with its window, or quietly in the menu bar after an update from the app.
if [ $FROM_APP = 1 ]; then open "$APP" --args --at-login; else open "$APP"; fi
printf '\033[32m%s\033[0m\n' "LimitSwitcher is installed in $TARGET and running in the menu bar."
