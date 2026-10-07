The app offers it in Settings → **Update to 1.4.2** (or download below).

## Fixed: "Switch" on a login that has to be signed in again

The taskbar's panel could show **Switch** next to **Sign in again** on the same account. Switching to it couldn't work, so it's no longer offered: sign in again first (click **Sign in again**), then switch.

## `/limits` looks better

The bars and numbers line up in a block, in the terminal and in the Claude app on your phone, and the other accounts are an aligned list. Times until a reset read "resets in 3h 48m".

## `/jevcompact` says what it saved

Under the command's "Jev compacting…", a line now says how it went: `Jev compacted: saved ~39k of 331k tokens`, or why it didn't run.

## Also

- Fixed: with Jev compaction on, the note beside Claude Code's "Resume this conversation?" could be missing when Claude Code was started with `claude --resume` or `claude --continue`.
- The panel says **starts w your first message** and the taskbar **starts w first msg** under a limit that hasn't started yet.
- `app.log` now tells saved copies of a login apart (a short fingerprint, never the token itself), so a login that has to be signed in again can be traced to whichever copy spent it.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
