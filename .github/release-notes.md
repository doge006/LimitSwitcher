The app offers it in Settings → **Update to 1.4.8** (or download below).

## Status line context after Claude compacts

After `/compact`, or when Claude Code compacts a conversation by itself, the `ctx` on the status line kept showing the size from before the compaction until the next reply. Claude Code goes on reporting the old figure until then. The status line now shows the compacted size that Claude Code records for the conversation, as soon as the compaction is done.

## Taskbar view and full-screen video

- The taskbar view no longer stays on top of a full-screen video (Steam's player, for one) until you click it. It hides while any window covers the display, whichever window has focus, and it keeps watching for a few seconds after a window comes forward, since apps take a moment to go full screen.
- It also comes back as soon as the full-screen window is minimized or closed, instead of staying away.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
