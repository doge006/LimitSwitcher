The app offers it in Settings → **Update to 1.1.0** (or download below).

## New

- **Jev compaction (optional, off by default):** when a session hits its limit and LimitSwitcher swaps the account, the session can be compacted before it goes on, so the new account's cold start (nothing cached) reads less. [Jev](https://openrouter.ai/typesafe/jev-1.13) (through OpenRouter) scores the older tool outputs: ones no longer needed become a one-line note naming what they held, unsure ones keep their head and tail. Your text, Claude's text, the newest messages and errors are never touched. It uses no Claude usage and a fraction of a cent of OpenRouter credit per swap. On a real 512k-token session the conversation got 44% smaller with 2 facts lost (other Jev compaction tools: 353 to 499 lost); sessions over about 250k tokens get cut further (61–63% on a 731k-token one). While it runs, Claude Code shows `⇄ LimitSwitcher · Jev compacting…`. If Jev fails it retries twice, 30 seconds apart, then the session goes on without it.
  - **To turn it on:** update the Claude Code Status mod (Settings → **Update**), turn on Settings → **Jev compaction**, and put `OPENROUTER_API_KEY=...` in the `.env` file in LimitSwitcher's data folder (`%LOCALAPPDATA%\LimitSwitcher\.env` or `~/Library/Application Support/LimitSwitcher/.env`). Details in the [README](https://github.com/doge006/LimitSwitcher#jev-compaction-optional).
- **Model and effort in the status line:** `⇄ LimitSwitcher · you@example.com · Opus 5.5 (high) · 5h 80% left · 1w 64% left`.
- **The status line is back where it belongs:** LimitSwitcher's line is in Claude Code's status line again (under the prompt, can't be dismissed), not a band above it. Installing the mod turns it on; the mod still feeds the live usage.

## Fixed

- **A newly added or switched-to account no longer shows the old account's usage** (0% left on a fresh account until you used it). Claude Code keeps the last reply's numbers until the next reply; those are now ignored after a switch.
- **"Signed out · sign in to Claude Code again"** instead of "Waiting for Claude Code" when Claude Code's login was signed out and nothing renews it.
- **The status line could stay missing** after Claude Code rewrote its settings at the wrong moment; it's put back on the next look.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
