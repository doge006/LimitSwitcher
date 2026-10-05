The app offers it in Settings → **Update to 1.3.1** (or download below).

## Smoother animations (Windows)

- **The full view animates at 63 frames a second** (every Windows timer tick) instead of about 32–38: cards rising in as it opens, and the hover fades over cards.
- **The panel's hover fades and switches keep up at high scaling.** At 200% they ran at about 39 frames a second and now run at 63. The panel and the right-click menu now redraw only what changed in a frame, so a hover frame takes 3–8 ms instead of 8–25 ms.
- **Less work in the background.** With the full view open, the app's CPU use measured 0.05% instead of 0.94%. Idle CPU and memory are unchanged, and peak memory while drawing is a few MB lower.

Nothing looks different: animation timings and curves are the same, and frames are drawn pixel for pixel as before. The only exception is panel shape edges at 125%, 175% and 225% scaling, which can differ by under a quarter of a pixel.

Separate accounts per window (new in 1.3.0, preview) is unchanged. See the [1.3.0 notes](https://github.com/doge006/LimitSwitcher/releases/tag/v1.3.0).

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
