The app offers it in Settings → **Update to 1.3.9** (or download below).

## Fixes

- **Logins expiring early:** when the app renewed an idle account's login at the moment you switched to it (or opened a window on it), Claude Code could get the old copy of the login and be signed out within the hour ("Login expired · Sign in again"). A switch or a new window now waits for that renewal and hands over the new login.
- **Separate accounts per window:** a card whose login has expired no longer shows **New window** on top of its "Login expired · Sign in again" link (the window couldn't have started anyway), and neither does a card whose limit is reached.
- **New window** now opens the terminal in your home folder, like one you open yourself, instead of LimitSwitcher's own folder.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
