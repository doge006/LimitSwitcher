The app offers it in Settings → **Update to 1.4.17** (or download below).

## Taskbar blocks stay visible over Start and Search

Opening Start or Search raises Windows' taskbar into a layer that ordinary windows can't reach. Explorer sometimes keeps it there after Start closes, until you click the taskbar or switch windows. The usage blocks were ordinary windows, so they went behind the taskbar during that time.

The blocks are now drawn inside the taskbar itself, so they move with it. A small helper process shows them, and LimitSwitcher sends it only the rendered pixels and receives its mouse events. LimitSwitcher itself stays separate from Explorer.

- The helper is replaced if it stops responding for 2 seconds.
- If the helper can't run, the blocks fall back to the previous floating windows.
- While the taskbar is still raised, Windows doesn't allow new blocks to be added. They appear as soon as it settles.

## Taskbar recovery

- A taskbar that briefly drops out of Windows' window list, or can't be read for a moment, keeps its blocks and is checked again. Before, its blocks were closed.
- A failed taskbar measurement keeps the last good one, instead of falling back to an estimate that could remove a block.

## Validation

- **Tests:** 440 ran with 15 skips. The one error, in a mod process test, occurs identically without these changes. The Python and Claude Code mod checks passed on GitHub.
- **On a Windows 11 desktop with two displays:**
  - The blocks stayed drawn with Start open.
  - The hand cursor held while hovering.
  - Clicking a block opened the panel, and right-clicking opened LimitSwitcher's menu.
  - The helper used about 13 MB of memory and no measurable CPU while idle.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
