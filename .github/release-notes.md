The app offers it in Settings → **Update to 1.3.8** (or download below).

## Jev compaction when you resume an old conversation

On a Pro or Max plan, Claude Code asks **"Resume this conversation?"** when you load a conversation that has been idle long enough that none of it is cached, and says what share of your 5-hour limit reloading it will use. With **Settings → Jev compaction** on:

- **A note beside the question:** 💡 *Jev compaction is on: upon resuming, Jev will compact the context, saving usage.* (also over `/resume`'s list of sessions).
- **Nothing happens while it asks.** Pick **Resume** (or press Esc) and Jev compacts the conversation right away, before anything is sent to Claude. An orange band above the prompt says so while it runs, then a toast says what it saved: ✅ *Jev saved ~289k of 666k tokens (43%) before this resume.* The resume uses about that much less of your limit.
- **Start a new conversation** compacts nothing.
- **A message you type meanwhile** waits in Claude Code's queue and goes, as typed, once Jev is done.
- Only for conversations of 100k tokens or more that Claude Code says are no longer cached. Switching accounts keeps its own compaction.

## The mod is now called limitswitcher

The Claude Code mod's main plugin is renamed from `limit-status` to `limitswitcher`, so its toasts carry the app's name. Settings shows **Claude Code Status mod → Update**: it installs `limitswitcher` and removes `limit-status`. Then `/reload-plugins` in open sessions (or open a new one). More in [the mod's docs](https://github.com/doge006/LimitSwitcher/blob/main/docs/claude-code-mod.md#jev-compaction).

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
