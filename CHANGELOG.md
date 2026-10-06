# Changelog

The highlights of each release, newest first. The full notes for each version are on its [release page](https://github.com/doge006/LimitSwitcher/releases).

## [1.3.6](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.6) (2026-10-06)

- Full view: mouse-wheel scrolling glides over about 10 frames instead of jumping 64 px.
- Menus, the date picker and toasts redraw only where they are: the slowest Settings menu frame at 200% scaling went from 41 ms to 18 ms, toasts from 20 ms to 4 ms.
- Lower peak memory with the full view open (131 MB vs 154 MB in the test run).

## [1.3.5](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.5) (2026-10-05)

- Jev compaction judges screenshots and other images in old outputs too: 56k to 107k fewer tokens per swap on a session with 187 screenshots, with no facts lost.
- Budget mode for long sessions starts above 333k tokens instead of 250k.
- Shorter status line after a compaction (`Jev saved ~120k`).
- Fixed: a second session on the same account now goes on with the first on the new account instead of waiting for a reset.
- Windows with their own account get Jev compaction too.

## [1.3.4](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.4) (2026-10-05)

- Separate accounts per window is easier to follow: guidance when it's turned on, clearer wording, and its buttons only show while it's on.

## [1.3.3](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.3) (2026-10-05)

- Usage checks reuse their HTTPS connection (and resume TLS sessions): a check went from 3.3 KB to 0.85 KB.
- Per-window accounts share settings, history, `/ide`, `/rewind` checkpoints and plans with the other windows, and keep settings changes when a window closes.
- The macOS app is 26 MB smaller; `app.log` is capped at 1 MB.
- The Windows installer is packed differently, clearing several antivirus heuristic flags.
- Fixes for quitting during start-up, and for logins written within 15 ms of each other on Windows.

## [1.3.2](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.2) (2026-10-05)

- Reset alerts: when every account has hit its limit, each open Claude Code session shows a toast as soon as one has room again.

## [1.3.1](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.1) (2026-10-05)

- Windows animations run at 63 frames a second (from about 32 to 38); panel hover frames take 3–8 ms instead of 8–25 ms.
- CPU use with the full view open dropped from 0.94% to 0.05%.

## [1.3.0](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.0) (2026-10-05)

- Separate accounts per window (preview): each new Claude Code terminal can run on an account of its own, with per-window Auto swap and Auto resume.

## [1.2.6](https://github.com/doge006/LimitSwitcher/releases/tag/v1.2.6) (2026-10-05)

- Auto swap picks the account whose weekly limit resets first, so that quota is used before it's lost.
- Fixed: Auto resume continues after a Jev compaction; the compaction shows in LimitSwitcher's status line; `ctx` follows it.

## [1.2.4](https://github.com/doge006/LimitSwitcher/releases/tag/v1.2.4) (2026-10-04)

- A tighter Settings menu that fits a normal window; the version and update button sit in the top bar.
- Update checks at launch and every 2 hours.

## [1.2.3](https://github.com/doge006/LimitSwitcher/releases/tag/v1.2.3) (2026-10-04)

- Fixed: the status line's context count and percentage come from the same figure.
- An unused 5-hour window says "Starts with your first message".

## [1.2.2](https://github.com/doge006/LimitSwitcher/releases/tag/v1.2.2) (2026-10-04)

- Removed the native status line program added in 1.2.0 after an antivirus flagged it; the Python status line script is about twice as light (15 ms a run).

## [1.2.1](https://github.com/doge006/LimitSwitcher/releases/tag/v1.2.1) (2026-10-04)

- A saved OpenRouter key is never shown again: Settings says "Key set" with a two-click Remove.

## [1.2.0](https://github.com/doge006/LimitSwitcher/releases/tag/v1.2.0) (2026-10-04)

- Jev compaction moves under the Claude Code Status mod in Settings, with a masked key field; `/jevcompact` runs it by hand.
- The status line shows Jev's progress and what it saved, with a coloured `ctx`.

## [1.1.0](https://github.com/doge006/LimitSwitcher/releases/tag/v1.1.0) (2026-10-03)

- Jev compaction (optional): shrink old tool outputs before a swapped session continues, so the new account's cold start reads less.
- Model and effort in the status line.
- Fixed: a newly switched-to account no longer shows the old account's usage.

## [1.0.11](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.11) (2026-10-03)

- The data folder moves from `AccountSwitcher` to `LimitSwitcher` by itself.
- Hung `claude plugin` commands are ended; a stuck refresh logs every thread's stack.

## [1.0.10](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.10) (2026-10-02)

- Reinstall really updates the Claude Code Status mod; no more empty status line row with the mod.

## [1.0.9](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.9) (2026-10-02)

- A coloured status line, drawn by the mod; it no longer blinks out while the app is busy.

## [1.0.8](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.8) (2026-10-02)

- Large sessions (400k+ tokens) ask before continuing on a new account; the app waits for a 5-hour reset that is under 15 minutes away.
- The optional Claude Code mod: live usage straight from Claude Code after every turn.
- Fixed: Auto resume no longer waits behind Claude Code's usage-limit dialog.

## [1.0.7](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.7) (2026-10-02)

- Fixed: the Auto resume hook stops when its Claude Code session closes; sign-in windows end their whole process.

## [1.0.6](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.6) (2026-10-01)

- "Waiting for Claude Code" clears within seconds of Claude Code renewing its login.

## [1.0.5](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.5) (2026-09-30)

- Fixed: Claude logins no longer expire on their own (renewals were blocked by Cloudflare); renewed logins are saved at once.

## [1.0.4](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.4) (2026-09-29)

- A customizable taskbar view per display (Windows).
- Switches hold Claude Code's own login locks, so a renewal in progress isn't lost.

## [1.0.3](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.3) (2026-09-29)

- Fixed: installing or updating on Windows while Claude Code is open.

## [1.0.2](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.2) (2026-09-29)

- Fixed: updating from the app when an earlier download was still in the Temp folder.

## [1.0.1](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.1) (2026-09-29)

- Fewer "Rate limited by Claude"; Auto resume after a reset; what's left is rounded down everywhere.

## [1.0.0](https://github.com/doge006/LimitSwitcher/releases/tag/v1.0.0) (2026-09-28)

- First release: every Claude Code and Codex usage limit in the Windows tray and taskbar or the macOS menu bar, one-click switching, Auto swap and Auto resume.
