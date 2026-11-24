The app offers it in Settings → **Update to 1.4.27** (or download below).

## Fixed: `claude --remote-control` with separate accounts per window

Since Claude Code 2.1.196, Remote Control turns itself off whenever `ANTHROPIC_BASE_URL` points anywhere other than Anthropic. With **Separate accounts per window** on, every new `claude` terminal was pointed at LimitSwitcher's router, so `/remote-control` disappeared. A window started with `claude --remote-control` (with or without a name) is no longer routed. It runs on the main account, and Remote Control works there. The setting's description now says so.

Routed windows still can't turn Remote Control on mid-session. If you use Remote Control often, turn the setting off. Auto swap of the main account keeps working without it.

## Changed: a frozen app now leaves a record in `app.log`

The app has no console window, so a freeze or an error in a click handler left nothing behind. Now:

- If the tray thread stops responding for 5 seconds, every thread's stack goes into `app.log`, under "tray thread not responding".
- Errors in window callbacks and background threads, and hard crashes, are written to `app.log`.

## Validation

- **Real PC:** with the router on, `/remote-control` was missing in a wrapped terminal, which had `ANTHROPIC_BASE_URL` set to the router. With the setting off, the wrapper and its PATH entry are removed, `claude` resolves to the real `claude.exe`, and no base URL is set.
- **Tests:** a new test runs the real `claude.cmd` wrapper against a stand-in app. `--continue` is routed, and `--continue --remote-control` isn't. That test fails on 1.4.26. Another new test makes a window stop pumping messages and checks that its thread's stack is written to the log exactly once. The full suite passes except for the one mod process test error, which also occurs without these changes.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
