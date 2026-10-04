The app offers it in Settings → **Update to 1.2.5** (or download below).

## Fixed

- **Auto swap and Auto resume now apply to sessions that are already open.** Claude Code reads its hooks when a session starts, and the limit hook was only added once a toggle was switched on, so a session opened before that never reported its limit: no swap, no resume, no Jev compaction. The hook now stays in place while the app runs and the app decides, so the toggles take effect at once. A session that was already open when you update needs one restart to get the hook.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
