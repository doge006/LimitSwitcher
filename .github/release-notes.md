The app offers it in Settings → **Update to 1.4.18** (or download below).

## Taskbar blocks survive an Explorer restart

When Explorer restarted (after a crash, or a manual restart), LimitSwitcher could try to add its blocks to the old taskbar in the moment before the new one appeared. Windows refused, and LimitSwitcher switched to the floating-window fallback for the rest of the session. Those floating windows go behind the taskbar whenever Start or Search opens, so the blocks flickered in and out until LimitSwitcher was restarted.

The blocks now wait for the new taskbar and appear on it, as they already did while Start had the taskbar raised. If the helper does fall back to floating windows, the log now records the Windows error code.

## Validation

- **Tests:** a new regression test sends the helper a frame for a destroyed taskbar; it fails without this change and passes with it. The full suite ran 441 tests with 15 skips; the one error, in a mod process test, occurs identically without these changes.
- **On a Windows 11 desktop with two displays:** after the update, the blocks were children of both taskbars and stayed visible and on top with Start open.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
