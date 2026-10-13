The app offers it in Settings → **Update to 1.4.12** (or download below).

## LimitSwitcher no longer freezes when a session hits its limit

When a Claude Code session hit a usage limit while the tray was refreshing, LimitSwitcher could freeze: it stopped answering Claude Code until it was restarted. `/swapaccount` still moved the window, but then waited 30 seconds and said "LimitSwitcher didn't answer; try again in a moment"; the status line, `/limits` and the reset alerts stopped updating too. Two parts of the app were each waiting for the other. That can no longer happen.

If your LimitSwitcher is frozen now, Settings → Update may not respond: quit it (from Task Manager on Windows or Activity Monitor on macOS if the tray menu doesn't respond), start it again, then update.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
