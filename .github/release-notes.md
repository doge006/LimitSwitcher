The app offers it in Settings → **Update to 1.3.2** (or download below). To get the toast, use **Reinstall** on the Claude Code Status mod in Settings (or `/plugin update`), then `/reload-plugins` in open sessions.

## New: reset alerts in Claude Code

- **When every Claude (or every Codex) account has hit its limit**, each open Claude Code session shows a toast as soon as one has room again: `⇄ LimitSwitcher · Claude has room again: work@example.com's limit has reset`. It names the account in use when that's the one that reset.
- **Nothing shows while another account still has room**, since Auto swap already moves you there.
- **On by default.** Settings → **Reset alerts** turns it off. It needs the Claude Code Status mod (limit-status 0.6.0), and a session hears of a reset on its next report, within about 30 seconds.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
