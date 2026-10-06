The app offers it in Settings → **Update to 1.3.10** (or download below).

## Windows list: each window's session, folder and model

With **Separate accounts per window** on, each row in the **Windows** list now shows:

- **The session's title** in place of "Window 1": its name from `/rename`, else the title Claude Code gave it, else its first prompt. "Window 1" moves next to it, since that's what the account cards call it (**Use in Window 1**, **In Window 1**).
- **The folder it's working in** before the model (`~` for your home folder). It now shows for windows opened with **New window** too, and follows the session if it changes folder.

The title and folder appear after the window's first reply, once Claude Code's status line has run.

## Also in this update (from 1.3.9)

- **Logins expiring early:** switching to an account, or opening a window on it, while the app was renewing its login could leave Claude Code with an outdated login that was signed out within the hour. A switch or a new window now waits for the renewal.
- **New window** no longer covers "Login expired · Sign in again" on a card, and opens in your home folder instead of LimitSwitcher's folder.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
