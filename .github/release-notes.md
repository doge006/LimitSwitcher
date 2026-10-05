The app offers it in Settings → **Update to 1.3.0** (or download below).

## New: separate accounts per window (Claude Code, preview)

Each Claude Code window can now have an account of its own, and you can move one window to another account without touching the rest.

- **Turn it on** in Settings → **Separate accounts per window**. Each new terminal in which you run `claude` then gets a free account of its own. Restart Windows Terminal once if it's already open, since it reads PATH only when it starts. Your settings, CLAUDE.md, plugins, skills and session history are shared with every window, so `/resume` still sees every session. Only the login differs.
- **The full view lists those windows** above the accounts, with each window's folder, model and account. Click a window to point it out on screen: it gets an outline and its taskbar button flashes. Then click **Use in window** on an account card, and only that window switches, on its next request.
- **Limits stay per window.** When a window hits its usage limit, Auto swap moves just that window to a free account, and Auto resume continues it.
- **One place at a time.** An account is used either by the main login or by one window, never two, because a login renewed in one place is signed out everywhere else. When a window closes, its account is free again.
- **Own window** (hover over a Claude card) opens a new terminal directly on that account.
- When no account is free, or LimitSwitcher isn't running, a new window shares the main login as before.

**Not yet tested on a real PC.** The logic is covered by an end-to-end simulation (the real app with simulated Claude Code windows on fake accounts), but the Windows-only parts haven't run on real Windows yet:
- putting `claude` first on your PATH;
- finding the window on screen and outlining it;
- opening a terminal from **Own window**.

Please report anything odd. Turning the setting off, or quitting the app, takes the wrapper away again.

Known limits: tabs in Windows Terminal share one window, so the whole window is outlined, not a single tab. The VS Code extension hasn't been checked, and its windows probably keep sharing the main login. Windows that were open before you turned the setting on keep the main login until you reopen them. On macOS the wrapper's folder has to be added to PATH by hand, and nothing is outlined on screen.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
