The app offers it in Settings → **Update to 1.4.14** (or download below).

## Reliable Claude usage

Claude Code can keep reporting usage from a session's last reply, including after an account switch or app restart. Those cached figures could overwrite a fresh provider check, making five-hour and weekly usage correct itself and then become wrong again.

Account-specific provider checks now determine usage throughout LimitSwitcher and its status line. Normal Claude checks run every minute, including while sessions report, and still respect provider backoff. Status-line rendering, session details, and the mod's other features continue to work.

## Login and taskbar recovery

- Prevent a token-renewal race during account switches that could leave the official client's refresh token outdated.
- Restore taskbar blocks when Windows hides them independently of the app's cached state.
- Remeasure taskbar button positions after Explorer settles, and handle temporarily hidden taskbars without interrupting updates.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
