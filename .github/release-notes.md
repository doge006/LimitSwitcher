The app offers it in Settings → **Update to 1.4.3** (or download below).

## Fixed: no account in use after Claude Code signs out

When Claude Code signed itself out (for example after a login it couldn't renew), LimitSwitcher dropped the account in use. Nothing showed as in use, the taskbar view went away, and only a switch by hand in full view brought it back.

Now the last account stays in use, marked **Signed out** in the taskbar, the panel and the full view. Switch to it to put its saved login back in Claude Code. Nothing signs you back in on its own, so a `/logout` you meant stays put. `app.log` now notes when Claude Code's login file had no login.

## Fixed: Auto resume missing in sessions opened while the app was closed

Quitting LimitSwitcher used to take its hook out of Claude Code. Claude Code reads hooks when a session starts, so a session opened while the app was closed never got Auto swap or Auto resume, even after the app was opened again. The hook now stays when you quit (it exits at once while the app is closed). The installer, updater and uninstaller still take it out.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
