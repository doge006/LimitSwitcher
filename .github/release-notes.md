The app offers it in Settings → **Update to 1.4.19** (or download below).

## Taskbar blocks no longer flicker when a window opens or closes

Each time a window opens or closes, LimitSwitcher reads where the taskbar's buttons now are, which takes about a tenth of a second. Until that reading arrived, it laid the blocks out with a rough estimate that assumed the buttons covered the middle half of the taskbar. The blocks narrowed for that moment, then animated back to their width once the reading came in, so they flickered on every window opened or closed.

The blocks now keep the last reading until the new one arrives. They change width only when a new button really needs their space, in one move. The estimate is still used when there is no earlier reading, or the taskbar moved or changed size.

## Validation

- **Tests:** a new regression test checks that a pending reading keeps the last one, and that a moved taskbar does not; it fails without this change. The full suite ran 442 tests with 15 skips; the one error, in a mod process test, occurs identically without these changes.
- **On a Windows 11 desktop:**
  - Polling the taskbar reading while Notepad opened and closed: before, one reading each time dropped the widest free space from 676 px to 274 px. After, the space changed only by the new button's own width.
  - The block's on-screen width, sampled every 16 ms, stayed at 352 px while a window opened and closed.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
