The app offers it in Settings → **Update to 1.4.15** (or download below).

## Taskbar account dropdowns

Each display's left and right taskbar blocks now use dropdowns. Choose **In use**, **Next use**, a fixed account, or **Off** for each slot. Next use previews the next eligible Auto swap account, excluding the current account, and updates as usage changes.

The dropdowns support mouse selection, keyboard navigation, and scrolling for long account lists.

## Secondary-monitor recovery

Taskbar visibility, position, and ordering changes now trigger recovery directly. Temporarily hidden secondary taskbars retain their monitor assignment, and unexpectedly hidden blocks can reappear without a click or window change. Full-screen coverage and intentional hiding remain respected.

## Jev compaction 0.1.3

- Preserve original tool inputs, scripts, edits, and distinct historical file versions.
- Compress exact duplicate outputs and repeated lines while preserving their source references, whitespace, and repetition counts.
- Protect needed evidence from size-target overrides and reject replacement notes that increase context size.
- Validate scorer answers, limit request concurrency, and discard late replies after a compaction deadline.

Savings depend on the workload. A falsely low relevance score can still remove unique evidence; archive retrieval remains disabled pending runtime verification. Offline retention tests and a reproducible synthetic benchmark are included.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
