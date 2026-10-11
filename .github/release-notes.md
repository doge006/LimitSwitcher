The app offers it in Settings → **Update to 1.4.11** (or download below).

## Jev compacts very long sessions

On a very long session (hundreds of tool calls), Jev compaction failed with "the conversation does not fit the Jev state budget of 20000 tokens", both on its own when your usage ran low and with `/jevcompact`. Jev reads an outline of the whole conversation, shortened in steps to fit, and on such sessions even the shortest outline was too big.

Now, when even the shortest outline is too big, Jev's outline leaves out the oldest steps (it says how many) and keeps the newest. Each output Jev scores still comes with its own command and a preview of its text, so it can judge outputs from early in the session too.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
