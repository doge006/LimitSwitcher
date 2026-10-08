# Changelog

The highlights of each release, newest first. The full notes for each version are on its [release page](https://github.com/doge006/LimitSwitcher/releases).

## [1.4.5](https://github.com/doge006/LimitSwitcher/releases/tag/v1.4.5) (2026-10-08)

- Fixed: Jev compacted every resumed session over 100k tokens, and said "Jev compaction is on" when you opened `/resume`, even when Claude Code never asked "Resume this conversation?". Jev now compacts on resume only when that question actually shows (Claude Code asks only when the resume would use a real share of your 5-hour limit), and the note shows beside the question only.
- The note is shorter: `💡 Jev compaction ready: upon resume, Jev will compact, saving usage`.
- The Claude Code mod now updates itself with the app: the first start after an update brings the installed mod up to the new release, so its fixes no longer wait for Settings → Install. Before, it stayed on the version first installed (why the 1.4.4 toast fix didn't show for everyone).

## [1.4.4](https://github.com/doge006/LimitSwitcher/releases/tag/v1.4.4) (2026-10-08)

- Fixed: the toast after Jev compacted a resumed session overstated the share it saved ("saved ~31k of 342k tokens (34%)" instead of 9%). The share is now of the whole context. `/jevcompact`'s "of" figure is now the whole context too, not Jev's smaller estimate of what it could prune.

## [1.4.3](https://github.com/doge006/LimitSwitcher/releases/tag/v1.4.3) (2026-10-08)

- Fixed: when Claude Code signed itself out, LimitSwitcher could end up with no account in use, and the taskbar view disappeared until a switch by hand in full view. The last account now stays in use, marked "Signed out"; switching to it puts its saved login back.
- Fixed: quitting the app took the Auto resume hook out of Claude Code, so sessions started while the app was closed never got Auto swap or Auto resume. The hook now stays (it does nothing while the app is closed); the installer, updater and uninstaller still take it out.

## [1.4.2](https://github.com/doge006/LimitSwitcher/releases/tag/v1.4.2) (2026-10-07)

- Auto resume's question before continuing a very large session now shows in Claude Code itself (a band above the prompt; `/resumeok` says yes), not only in the tray. New **Continue every session** under Auto resume (replacing "Skip large sessions") continues every session without asking.
- Fixed: the taskbar panel offered "Switch" on an account whose login has to be signed in again. Such an account now shows only "Sign in again".
- `/limits` reads better: the bars and numbers line up, and the other accounts are a tidy list.
- `/jevcompact` says how it went under the command (`Jev compacted: saved ~39k of 331k tokens`), not only in the status line.
- Fixed: the Jev compaction note could miss "Resume this conversation?" when Claude Code was started with `--resume` or `--continue`.
- Shorter "starts w your first message" in the panel and "starts w first msg" on the taskbar.
- app.log tells saved copies of a login apart (a short fingerprint, never the token), to trace a login that had to be signed in again.

## [1.4.1](https://github.com/doge006/LimitSwitcher/releases/tag/v1.4.1) (2026-10-07)

- Fixed: Claude Code could say "Not logged in" after a restart when its login had expired and nobody renewed it. The app now renews it (under Claude Code's own locks); a login that can't be renewed is marked "Sign in again" and Auto swap moves Claude Code to the best other account, as a swap by hand did.
- The taskbar and the panel say "starts with a message" under a limit that hasn't started yet, like the full view.

## [1.4.0](https://github.com/doge006/LimitSwitcher/releases/tag/v1.4.0) (2026-10-07)

- New `/swapaccount <name or email>` (Claude Code Status mod): puts the window it's typed in, and only that one, on another Claude account from its next message. New windows stay on the main account. A wrong or missing name lists the accounts (names only in name mode).
- Separate accounts per window, rebuilt: every window is your usual Claude Code (settings, plugins, mods, `/resume`, agents mode, history); only the account its messages go to differs, through a small router in the app. Fixed: a separate window could lose its plugins (and `/jevcompact`) after a settings change on Windows, and its sessions were missing from agents mode.
- A window on its own account gets the same Auto swap, Auto resume and Jev compaction as the main account, moving alone to the best other account. Accounts can be shared between windows; a window moves onto the main account only when no other has room.
- The Windows list shows windows on the main account too, and any window can be moved to any account (the main one included).
- No wrapper process stays open per window on Windows.

## [1.3.10](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.10) (2026-10-06)

- Windows list: each window shows its session's title (its /rename name, Claude Code's title, or its first prompt), then "Window N", its folder and its model. The folder now shows for windows opened with New window too.

## [1.3.9](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.9) (2026-10-06)

- Fixed: a switch (or a new window) at the moment the app renewed that account's login could leave Claude Code with a spent copy, signed out within the hour. It now waits for the renewal and hands over the new login.
- Fixed: "New window" no longer overlaps "Login expired · Sign in again" on a card; it isn't offered on an expired login or a used-up account.
- New window opens in your home folder instead of LimitSwitcher's folder.

## [1.3.8](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.8) (2026-10-06)

- Jev compaction when you resume an old conversation: after Claude Code's "Resume this conversation?" (Pro/Max), picking Resume has Jev compact it before anything is sent, so the resume uses less of your 5-hour limit. A note sits beside the question, a band shows it compacting and a toast what it saved; "Start a new conversation" compacts nothing.
- The Claude Code mod's plugin is renamed from limit-status to limitswitcher (Settings → Update replaces it).

## [1.3.7](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.7) (2026-10-06)

- New `/limits` command (Claude Code Status mod): every Claude account's limits at a glance, made for the phone over Remote Control. Shows the session's own account with bars, what Jev did for it, and where each account is in use with separate accounts per window.

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
