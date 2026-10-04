The app offers it in Settings → **Update to 1.2.0** (or download below).

## New

- **A much lighter Claude Code status line:** on Windows and macOS it is now a tiny native program (`LimitSwitcher Status` in Task Manager and Activity Monitor: about 1.7 MB and 2 ms a run, instead of a 9 MB Python process), refreshed every second so changes show at once. With a status line of your own, or from source, the Python script still runs (about half the work it was).
- **Jev compaction sits under the Claude Code Status mod in Settings** (off by default), with a masked field for your OpenRouter key. The key is saved to the `.env` file the mod reads, readable only by you, and shown only as dots. `/jevcompact` runs the compaction by hand in any session.
- **The status line shows Jev's work:** while it compacts, the icon turns yellow and says so right after `LimitSwitcher`; for 45 seconds after, it says what it saved (`Jev compacted ~120k tokens saved`). `ctx` is blue, and its count and percentage go green, yellow and red as the context fills.

## Changed

- **The "Claude Code status line" switch is gone:** the line comes with the Status mod (installing it is the choice to see it), and your own status line is still wrapped as before.
- The app looks at the login files at most every 3 seconds when a status line reports (on macOS that read the Keychain on every report).

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
