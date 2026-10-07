The app offers it in Settings → **Update to 1.4.0** (or download below).

## /swapaccount: one window on another account

Type **`/swapaccount <name or email>`** in any Claude Code window (with the Claude Code Status mod): that window, and only that one, goes on that account from its next message. Every other window, and new ones, stay on the main account, so you can work in one window on one account's limits and in another on another's.

- In name mode, type the name or the email; otherwise the email (or the part before the @).
- **`/swapaccount`** alone, or a name that doesn't match, lists the accounts you can pick and what each has left (names only, in name mode).
- **`/swapaccount main`** puts the window back on the main account.
- The window shows up in the **Windows** list, where you can also move it from the app.

The mod updates itself (Claude Code picks up its new version); open sessions get `/swapaccount` after `/reload-plugins` or a restart.

## Separate accounts per window, rebuilt

A window on its own account is now your usual Claude Code in every way: the same settings, plugins and mods, `/resume`, agents mode (`claude agents`), history and Remote Control. Only the account its messages go to differs: a small router in LimitSwitcher puts that account's login on the window's requests and passes everything else through unchanged. Before, such a window had a config folder of its own linked to yours, which could drift apart:

- **Fixed:** on Windows, a separate window could lose its plugins (and `/jevcompact` with them) after a settings change.
- **Fixed:** sessions in separate windows were missing from agents mode.

What else changes:

- **Limits:** a window on its own account gets the same Auto swap, Auto resume and Jev compaction as the main account. At its limit it moves alone to the best other account; it moves onto the main account only when no other has room.
- **Sharing:** two windows (or a window and the main account) can use the same account. When it runs out, each moves on by itself.
- **The Windows list** shows windows on the main account too, and any window can be moved to any account, the main one included.
- **Lighter:** no process of the `claude` wrapper stays open per window on Windows, and the router costs a few milliseconds of CPU per streamed answer.
- LimitSwitcher has to be running for a window on its own account (its messages go through the app). Restarting or updating the app is fine: the window carries on when it's back.
- Windows opened with 1.3.x keep working until you close them; their folders are cleaned up then.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
