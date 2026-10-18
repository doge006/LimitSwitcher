The app offers it in Settings → **Update to 1.4.16** (or download below).

## Taskbar recovery after fullscreen games

- Keep temporarily hidden taskbars tracked, including when the app starts during a fullscreen game.
- Restore blocks when the taskbar returns, without requiring a click or window change.
- Repair hidden blocks and incorrect window ordering without moving them or taking focus.
- Preserve intentional hiding for fullscreen apps and the ordering of screenshot tools and other overlays.

## Explorer isolation

Taskbar blocks now belong to LimitSwitcher's own tray window instead of Explorer. This keeps the two apps' input queues separate.

Taskbar button measurements run on a dedicated background MTA thread, with cached results delivered asynchronously to the tray thread. A stalled measurement cannot block the tray or spawn additional measurement workers.

These changes remove identified app-side hang risks. They do not establish the cause of any particular Explorer hang.

## Validation

The Python suite ran 426 tests with 19 skips and no failures. Focused tests cover fullscreen recovery without activation, native window ownership and ordering, focus preservation, overlays, and stalled measurement providers. Python and Claude Code mod checks passed on GitHub.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
