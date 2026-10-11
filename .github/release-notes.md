The app offers it in Settings → **Update to 1.4.10** (or download below).

## Taskbar view comes back after a screenshot

Since 1.4.9, taking a screenshot (Win+Shift+S, the Snipping Tool) could make the taskbar view disappear until you clicked into a window. The screenshot tool stays the app in use after the snip, and 1.4.9 still counted its full-screen overlay.

The taskbar view no longer guesses from which app is in use or how big its windows are. It checks what Windows actually draws over the taskbar: the blocks hide only while a full-screen window (a game, a video, a browser's F11) sits on top of it, and come back the moment the taskbar shows again, with no click. Screenshot tools never make them hide.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
