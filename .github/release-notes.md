The app offers it in Settings → **Update to 1.2.2** (or download below).

## Fixed

- **No new program for antivirus to object to:** 1.2.0 and 1.2.1 added a small native status line program (`LimitSwitcherStatus.exe` on Windows, `LimitSwitcher Status` on macOS); an update of 1.2.1 was stopped by Bitdefender (SuspiciousBehavior). It is gone: the Claude Code status line is the Python script again, as before 1.2.0.
- **The Python status line script is about twice as light:** a run takes about 15 ms and 9 MB (it was 58 ms), it runs every 5 seconds per open session (about 0.3% of a core), and it starts Python without site-packages. It also no longer rewrites its cache file when nothing changed.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
