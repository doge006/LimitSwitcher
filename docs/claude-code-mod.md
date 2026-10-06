# Claude Code mod and Jev compaction

- [The Claude Code Status mod](#the-claude-code-status-mod)
- [Jev compaction](#jev-compaction)
- [Benchmarks](#benchmarks)

## The Claude Code Status mod

Optional. Claude Code mods (early access; Claude Code 2.1.287 or later) run inside Claude Code. The mod is two TypeScript plugins from this repository, installed together:

- **limit-status** (`mods/limit-status`) gives LimitSwitcher Claude Code's live usage after every turn, straight from Claude Code, runs the Jev compaction below, shows the reset alerts (Settings) as a toast, and adds [`/limits`](#limits). LimitSwitcher's line itself is in Claude Code's [status line](how-it-works.md#claude-code-status-line), where it can't be dismissed; installing the mod turns that on.
- **jev-compact** (`mods/jev-compact`) is the compaction. It is a plugin of its own because Claude Code skips a plugin's own compaction hook when that plugin starts the compaction.

Install it from **Settings → Claude Code Status mod → Install**. The app runs `claude plugin marketplace add` and `claude plugin install` for you (this repository is the marketplace) and tells the plugins where the app's files are. The row then shows **Active** while a session is reporting, **Installed** until one is, **Update** when only an older limit-status is there, or **Not installed**. Open sessions pick it up after `/reload-plugins`. Without the mod everything keeps working through the status line script (without Jev compaction).

### /limits

Every Claude account's usage at a glance, in any Claude Code session with the mod. It's made for the Claude app on your phone during a [Remote Control](https://code.claude.com/docs/en/remote-control) session, where Claude Code's status line doesn't show. The session's own account comes first, with its model, a bar for each limit and the context, and what Jev did for the session; then each other Claude account on one line (Codex accounts aren't listed). With separate accounts per window, it shows the window's own account, and where each account is in use:

```
⇄ doge2 · Opus 5.5 (medium) · window 1
🟢 5h  ██████░░░░  63% left
🟡 1w  █░░░░░░░░░  13% left
🟢 ctx ████████░░  78% left · 217k
🗜 Jev saved ~56k before the last swap (12m ago)

🟢 doge1 · 5h 92% · 1w 71% · main
🔴 doge3 · 5h 0% ↻38m · 1w 40%
```

The dot is each line's level (green, yellow under 30% left, red under 10%), and a used-up limit shows when it resets. The reply is markdown (a table with the bars, a list of the other accounts), which Claude Code and the Claude app draw without colours, hence the dots. The mod answers it from the app's numbers, so it costs no Claude usage; the reply is part of the conversation, so the model reads it with your next message (a few hundred tokens).

## Jev compaction

Optional. Swapping a long session to another account costs a cold start: the new account has none of the session cached, so its next turn reads the whole context uncached. With **Settings → Jev compaction** on, LimitSwitcher swaps the account as usual, then has the session compacted before it goes on:

1. The session hits its limit; the account is swapped at once.
2. The session's mod runs the compaction (every shortened output keeps a note listing the names, paths and values it held). [Jev](https://openrouter.ai/typesafe/jev-1.13) (TypeSafe's decision model, through OpenRouter) scores the session's older tool outputs: still needed, unsure, or done with. Outputs it is done with become a one-line note, unsure ones keep their head and tail, the rest stay whole; a file view or search (cheap to read again) needs a higher score to stay whole than a test run or a web page. A file read again later is replaced by a note without asking, and long old scripts and file contents Claude wrote are shortened (what they did is on disk). Screenshots and other images in old outputs are judged too: one it is done with becomes a note (an image has no head and tail to keep, so an unsure one stays whole).
3. The session goes on (Auto resume) or waits for your next message (Auto swap alone). While it runs, Claude Code shows `⇄ LimitSwitcher · Jev Compacting…` (also for `/jevcompact`).

What it never touches: anything you or Claude wrote, the first message, the 8 newest messages, calls still running, and error outputs. Nothing is summarised and no call is removed: Claude still sees every step it took, and each shortened output says so, so it re-runs the tool instead of guessing. Keys and tokens in the conversation are masked before anything is sent to Jev.

**Resuming an old session.** Claude Code asks "Resume this conversation?" when a session has been idle long enough that none of it is cached, and says what share of your 5-hour limit reloading it will use. That usage is only spent when the first message goes, not when you pick Resume. With Jev compaction on, `/resume` says so before you pick a session: its line in the command list ends `Jev compacts an old session before it goes on`, and a toast (top right, for a minute) says `Jev compaction is on: upon resuming, Jev will compact the context, saving usage.` Choosing **Resume** then has the session compacted before anything is sent: a band above the prompt says `⇄ LimitSwitcher · Jev compacting the resumed session…`, then what it saved (`Jev saved ~289k of 666k tokens (43%) before this resume`, for 20 seconds). A message you send meanwhile is held and sent as typed once it is done (one with an image is turned back, to send again; nothing is held over 4 minutes). It only does this when Claude Code says the cache has expired and the session holds 100k tokens or more. The question itself is Claude Code's own (a mod can't change it); pick **Resume**, not **Start a new conversation**. `claude --resume` typed in a shell compacts the same way, without the note before.

You can also run it by hand in any session with `/jevcompact` (it works with the toggle off). Your earlier thinking stays wherever the API allows it (an edit invalidates the thinking after it on accounts created since Aug 31, 2026; Claude Code then drops those blocks and retries by itself). It costs no Claude usage (it works on a used-up account) and a fraction of a cent of OpenRouter credit per swap. If Jev fails, the mod waits 30 seconds and tries again, three tries in all; then (or after 4 minutes at most) the session goes on without it. `/compact` and auto-compaction stay Claude Code's own.

**Setting it up:** install (or update) the mod, turn on Settings → **Jev compaction** (it sits under the mod's row and is off until you switch it on; off, the mod never asks for a compaction), and give it an [OpenRouter key](https://openrouter.ai/keys):

- paste it in the field that appears under the toggle. What you type shows only as dots; once saved it just says "Key set" with a Remove button (click it twice: the second click says Confirm). It is saved as the `.env` line below, readable only by you;
- or put an `OPENROUTER_API_KEY=...` line in the `.env` file in LimitSwitcher's data folder (`%LOCALAPPDATA%\LimitSwitcher\.env`, or `~/Library/Application Support/LimitSwitcher/.env`);
- or set the `OPENROUTER_API_KEY` environment variable, or put it in Claude Code's `settings.json` `env` block.

The app only writes the key you paste and checks that one is there; only the mod reads it, and the app never shows it again.

## Benchmarks

**How much it saves, and what it costs in quality:** benchmarked on a real 512k-token session (the one that built this) with the live Jev, against the other Jev compaction tools on the same session. *Quality* is a hindsight test: compact the session as it stood at three earlier points, then count the project facts (names, paths, values; not words or standard-library names the model knows anyway) Claude went on to use from memory that only a removed output held.

| | conversation smaller | after compaction (Claude Code's count) | facts lost that Claude used later | also |
|---|---|---|---|---|
| **this** | 44% | 116k | **2** | nothing deleted; errors, your text, recent messages untouched |
| cc-mod-jev | 62% | 104k | 353 | deletes calls, cuts error outputs |
| HAR5HA jev-compact | 75% | 97k | 379 | cuts error outputs, rewrites your text |
| fast-jev-compaction | 90% | 33k | 499 | deletes almost every call, touches the newest messages |

**Screenshots:** on a real 3-part session with 187 screenshots (426k, 458k and 150k tokens at each swap point), judging images too took each swap's load down by another 56k to 107k tokens, with no facts lost and no removed screenshot read again later at those points; at nine earlier points in the same session it lost 2 facts in all and 6 of 347 removed screenshots were read again.

**Long sessions** (a conversation over about 333k tokens) also get a budget: after the usual decisions, the least needed outputs, oldest first, keep stepping down (whole, head and tail, a note) until the conversation is about a third of its size. On a 731k-token session: 61% to 63% of the conversation cut for 20 to 25 facts lost (the default alone: 58% / 16); Claude Code counts 733k before and 185k after. It levels off near 64%: what is left is your text and Claude's, the newest messages, errors and the notes themselves.

Any compaction also drops the old system notices Claude Code repeats through a session, which is why every row ends far below the 512k it started at. Every note this one leaves names what it removed and nothing else still shows (the names, paths and values it held), so Claude re-checks instead of guessing; that cut the facts lost from about 30 to 2. cc-mod-jev ends 12k tokens smaller for over 150 times the loss. Outputs cheap to get again (file views, listings, searches) need a higher score to stay whole than test runs, web pages or anything that changed state.

**To measure your own:** `bun mods/jev-compact/bench/bench.ts <transcript.jsonl>` runs it on a saved Claude Code transcript (`~/.claude/projects/...`) and prints how much smaller the session gets and what Jev cost (`--fake 0.5` for a dry run without a key; `--decisions` lists every decision; `--quality` runs the hindsight test).
