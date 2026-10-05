The app offers it in Settings → **Update to 1.2.6** (or download below). To get the mod changes, use **Reinstall** on the mod in Settings (or `/plugin update`), then `/reload-plugins` in open sessions.

## Fixed

- **Auto resume continues after a Jev compaction.** The limit hook checked whether the session had already gone on by itself by seeing if its transcript changed, so a notice Claude Code added while it waited (the account switch, the compaction's log line) made it skip the continue. It now looks for a new message instead.
- **Jev compaction shows in LimitSwitcher's line in the status bar**, for `/jevcompact` too: `⇄ LimitSwitcher · Jev Compacting…` (yellow) while it runs, then `Jev Compacted (saved ~120k tokens)` for 45 seconds. The mod no longer prints its own separate line.
- **ctx follows the compaction.** Right after Jev compacts, ctx shows the size less what Jev saved, until the next reply brings Claude Code's own figure.
- **ctx no longer disappears during a usage limit.** When Claude Code sends no figures (a limit, a compaction), the session's last ones stay, however long the wait.
- **A cancelled subscription is yellow only in its last 7 days.** Before that its end date is grey.

## Changed

- **Auto swap uses the account whose weekly limit resets first**, so that quota is used before it is lost. An account with under 5% left is only picked when no other has more.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
