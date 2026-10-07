The app offers it in Settings → **Update to 1.4.1** (or download below).

## Fixed: "Not logged in" after a restart

If Claude Code's login had expired and nothing renewed it (Claude Code wasn't running, or its own renewal went wrong), Claude Code said **Not logged in** and the taskbar disappeared until you swapped accounts by hand. Now:

- **LimitSwitcher renews it** and writes it back. It does this under Claude Code's own locks, so the two never renew at once.
- **A login that can't be renewed any more** (signed out elsewhere, or a spent copy from before 1.3.9) is marked **Login expired · Sign in again**, and with Auto swap on, Claude Code moves to the best other account by itself, as a swap by hand would.

## The taskbar says when a limit starts

A limit nobody has used yet has no reset time. The taskbar and the panel now say **starts with a message** under it (the full view already said "Starts with your first message"), rather than nothing.

## Also in this update (from 1.4.0)

- **`/swapaccount <name or email>`** puts one Claude Code window on another account; everything else stays your usual Claude Code.
- Separate accounts per window, rebuilt: every window is an ordinary Claude Code window, and only the account its messages go to differs.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
