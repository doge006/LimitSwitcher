The app offers it in Settings → **Update to 1.4.4** (or download below).

## Fixed: Jev's saved share was overstated

After Jev compacted a resumed session, the toast could say "saved ~31k of 342k tokens (34%)": the tokens saved were right, but the share was worked out against Jev's own estimate of the part it can prune (messages and tool results), not the whole context with Claude Code's system prompt and tools. It now reads "saved ~31k of 342k tokens (9%)".

`/jevcompact` had the same flaw in its "of" figure ("saved ~30k of 50k tokens"): it now gives the session's whole context and the share of it. The status line, `/limits` and the context count after a compaction were already right.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
