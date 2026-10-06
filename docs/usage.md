# Using LimitSwitcher

- [Your accounts](#your-accounts)
- [Separate accounts per window](#separate-accounts-per-window)
- [Auto swap and Auto resume](#auto-swap-and-auto-resume)
- [The tray (Windows)](#the-tray-windows)
- [The menu bar (macOS)](#the-menu-bar-macos)
- [Settings](#settings)

What the app changes on your machine, and how it talks to the providers, is in [How it works](how-it-works.md). The optional Claude Code mod and Jev compaction are in [Claude Code mod](claude-code-mod.md).

## Your accounts

- **Works with:** Claude Code (the CLI and its VS Code extension) and Codex (the CLI, its editor extensions and the Claude Code Codex plugin). A Codex session or editor that was already open before LimitSwitcher started keeps its account until it's reloaded; LimitSwitcher tells you when that's the case.
- **Adding accounts:** whatever Claude Code / Codex login is active on your PC is picked up automatically. Signing in to another account (`claude auth login`, `codex login`) adds it too. **Add account** (in the full view or the tray menu) runs the official sign-in in a separate window and an isolated folder, so the login you're using isn't touched.
- **Switching:** click an account and every session moves to it, including sessions that are already open. Nothing needs restarting. ([How a switch works](how-it-works.md#switching-accounts).)
- **Usage:** read from each provider's own usage endpoint:
  - Claude: 5-hour, weekly and per-model weekly caps, plus extra usage.
  - Codex: 5-hour, weekly or 30-day windows, plus credits.

  These are read-only status endpoints, the same ones behind Claude Code's `/usage` and ChatGPT's usage page. **Checking usage does not use any of your quota.**
- **Subscription:** "Renews Oct 14" or "Ends Oct 14" shows when a subscription renews or has been cancelled.
  - Codex: the paid-through date comes from its login token; cancellation is read best-effort from ChatGPT's account check.
  - Claude: its profile reports only when the subscription started and whether it's active or cancelled. The renewal is estimated as the next monthly anniversary and shown with a ~ (e.g. "Renews ~Oct 14").
  - Both are checked at most once a day. When nothing is reported, click **Set renewal date** on the card; a date you enter always wins.
  - The names of the fields these endpoints return (never their values) are kept in `subscription-fields.json`, to help match the detection to real responses.
- **Usage limit resets:** banked resets are shown for Codex, which reports them. Claude's usage response doesn't include its free resets (they appear only in Claude's settings), so none are shown for Claude.
- **Token ownership:** each account should be managed from here only. If the same account is also signed in elsewhere and refreshes its token there, this copy expires and shows "Sign in again". The in-use account's token is never refreshed by this app; that stays with Claude Code / Codex.

Check the providers' terms for using several subscriptions this way; that's your call.

## Separate accounts per window

Claude Code, prototype; off by default, in Settings. Normally every Claude Code window shares one account, and **Swap to this** moves them all. With this on, each Claude Code terminal you open from then on starts on an account no other window is using, so two windows can work on two accounts at once.

- **What you see:** a **Windows** list appears above the accounts, one row per window that has its own account, with the account it's on. That account's card says **In Window 1** (and so on); the card marked **In use** is the main account, shared by every other window.
- **Moving one window to another account:** click the window in the list (it's outlined on screen, and its taskbar button flashes on Windows), then click **Use in Window 1** on the account you want. Only that window changes, on its next request. Click the window again, or press Esc, to cancel. **New window** on a card (shown on hover) opens a new terminal on that account straight away, in your home folder.
- **Limits:** when a window hits a usage limit, Auto swap moves that window alone to a free account, and Auto resume continues it.
- **Good to know:**
  - Only terminals opened after you turn it on get their own account; windows already open keep sharing the main one. The VS Code extension isn't covered.
  - Each window needs an account nobody else is using. When none is left, a new window shares the main account as before.
  - An account is in one place at a time (the main account or one window), because a login renewed in one place is signed out everywhere else. When a window closes, its account is free again.
  - Turning the setting off doesn't touch windows that already have their own account; they keep it until they close.
- **How it works:** see [How it works](how-it-works.md#separate-accounts-per-window).

## Auto swap and Auto resume

- **Auto swap:** each account is used to 100%; then the app moves to the account whose weekly limit resets first (so that quota is used before it's lost; one with under 5% left only when nothing has more), and the thread carries on with everything it had.
  - *Codex:* the request that hit the limit is sent again on the next account, so the session never sees the error. If ChatGPT's response headers already showed the account used up, a new turn simply starts on the next account.
  - *Claude:* Claude Code shows its limit message. The app's hook switches accounts right away, so your next message uses the new account; with Auto resume on, it also continues by itself.
  - *Claude threads:* nothing in them is tied to an account, so they carry over whole.
  - *Codex threads:* ChatGPT encrypts two things for the account that made them, which another account can't read. The app makes no extra requests for this:
    - *Hidden reasoning:* another account receives the plain-text summary ChatGPT returned with it.
    - *Compaction checkpoints* (the summary Codex keeps instead of old history): the thread stays on the checkpoint's account while that account has quota. Once it's used up, the thread moves on without that older summary; the recent conversation is kept.
- **Auto resume:** while it's on, a Claude Code session that stops on a usage limit continues by itself, with nobody typing.
  - When the hook fires, the app switches to an account with room (Auto swap) and Claude Code is told to continue where it left off.
  - If no account has room, it waits for the earliest reset (up to 6 hours) and then continues.
  - Codex needs no hook: its requests are retried on the next account automatically.
  - [How the hook works](how-it-works.md#the-auto-resume-hook).

## The tray (Windows)

- **Left-click the icon:** a compact panel opens above it. It lists every account by email with its plan and subscription status ("Renews 18d" / "Ends 3d"), plus 5h / 1w / per-model headroom with the time until each resets. Click an account to switch; you'll see "Switching…" until it lands. Clicking the icon again closes the panel, pinned or not.
- **Hidden icons (^):** if the icon lives in Windows' hidden-icons popup, left-clicking it closes that popup (the app sends it one Esc, only when that popup is the active window) and the panel takes its place. Right-clicking leaves it open, like Steam's menu.
- **Motion:** hovers fade, switches slide, the "in use" marker cross-fades on a switch, and bars glide to new values. Animation frames are drawn only while something moves.
- **The panel:**
  - **Pop out** (next to **Full view ›**) pins the panel: it stays open and you can drag it by its header. Click it again to put it back.
  - **Compact** (the button next to the power button) shrinks it to the accounts in use. The compact panel is always popped out; **Expand** brings the full panel back (still popped out) and **Hide** puts it away, so the tray icon opens the normal panel next time.
  - **Power** quits, after a second click to confirm.
  - **Full view ›** opens the full view: a native window of the app's own (no browser), drawn like the panel.
  - Esc or clicking elsewhere closes an unpinned panel.
- **Right-click:** a menu in the same style: Open panel, Full view, Auto swap, Auto resume and Quit. Toggling Auto swap or Auto resume keeps the menu open.
- **Hover:** the tooltip shows the account in use per provider and what's left.
- **The icon's dot:** green, amber or red for the tightest limit in use.
- **Taskbar view:** the accounts in use sit right on the taskbar, and step aside for full-screen apps.
- **Launching again:** opens the running copy's full view instead of starting a second copy.
- **Quit** stops everything the app started and puts the Codex and Claude Code settings back. The app starts with Windows (Codex routing depends on it); Settings → **Launch with Windows** turns that off.

## The menu bar (macOS)

- **Menu bar:** the tray becomes a menu bar icon.
  - Click it for the panel: it opens under the icon, with an arrow pointing at it, on the system's frosted popover material.
  - Drag the panel by its header or background: away from the menu bar it stays open where you leave it (the arrow goes), and you can move it again any time. Its dock button (an arrow up to a bar) slides it back under the menu bar icon. Docked, clicking elsewhere or Esc closes it.
  - Right-click (or Control-click) for the menu; **Full View…** opens the full view in its own native window.
  - There's no Dock icon.
- **Claude Code** keeps its login in the macOS Keychain ("Claude Code-credentials"). The app switches that item, and a running Claude Code picks it up on its next request.
- **Codex** works exactly as on Windows (the local router, `~/.codex/auth.json`).
- **Saved logins** are encrypted with a random key kept in your login Keychain, under `~/Library/Application Support/LimitSwitcher`.
- **Start at login:** a LaunchAgent (`~/Library/LaunchAgents/com.accountswitcher.app.plist`).

## Settings

![Settings](media/settings.gif)

In the full view, the gear opens Settings:

- **Auto swap:** move to the account whose weekly limit resets first when a limit hits.
- **Auto resume:** after a usage limit, the session continues by itself, on another account or once the limit resets.
- **Jev compaction** (under the Claude Code Status mod, off by default): before a swapped session goes on, shrink the tool outputs it no longer needs, so the new account loads less (see [Jev compaction](claude-code-mod.md#jev-compaction)).
- **Reset alerts** (on by default; needs the Claude Code Status mod): when every Claude (or every Codex) account has hit its limit, a toast in each open Claude Code session says which one has room again as soon as its limit resets (within about 30 seconds). Nothing shows while another account still has room, since Auto swap already uses it.
- **Name mode:** names instead of emails everywhere (panel, taskbar, status line, notifications), for screen sharing. Click an account's name in the full view to set it.
- **24-hour clock:** reset times like 14:30 instead of 2:30 PM.
- **Launch with Windows / macOS:** start in the tray when you sign in.
- **Taskbar view** (Windows): the accounts in use, right on the taskbar, on the display you choose.
- **Check for updates / Update to …**
