The app offers it in Settings → **Update to 1.4.13** (or download below).

## The right usage after `/swapaccount`

After `/swapaccount` moved a Claude Code window to another account, that account could show the previous account's usage (say 5% left when it had 50%): right for a moment, then wrong, and it stayed wrong for a long time. Until the window's first reply on its new account, Claude Code still reports the old account's numbers, and one of those reports was counted for the new account. Those reports are now ignored until the new account's own numbers come in.

An account already showing the wrong number corrects itself when its limit window resets or the app next reads its usage.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
