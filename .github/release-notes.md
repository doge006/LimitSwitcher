The app offers it in Settings → **Update to 1.3.5** (or download below).

## Jev compaction drops old screenshots too

- **Old screenshots and other images are now judged too.** Before, Jev never saw the images in a session's old tool outputs, so a session full of screenshots kept every one through each swap. Now one Jev is done with becomes a short note, and one it is unsure about stays. On a real session with 187 screenshots, each swap had 56k to 107k fewer tokens to load, with no facts lost.
- **The extra trimming for long sessions ("budget mode") now starts above 333k tokens** instead of 250k. Below that, the normal pass loses fewer facts.
- **A shorter status line:** after a compaction it says `Jev saved ~120k` (the real number), so the line fits a normal window.

To get the Jev changes, click Settings → Claude Code Status mod → **Reinstall** (it updates the mod), then run `/reload-plugins` in any open session.

## Fixes

- **Two sessions on one account hitting its limit together:** the second one now goes on with the first on the new account. Before, it waited for the old account's reset, which could be hours.
- **The same when the app's background check switched first:** the session no longer waits for a reset.
- **A window with its own account** now gets the Jev compaction when it is switched at its limit, like the main login.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
