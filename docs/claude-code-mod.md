# Claude Code mod and Jev compaction

- [The Claude Code Status mod](#the-claude-code-status-mod)
- [Jev compaction](#jev-compaction)
- [Benchmarks](#benchmarks)

## The Claude Code Status mod

Optional. Claude Code mods (early access; Claude Code 2.1.287 or later) run inside Claude Code. The mod is two TypeScript plugins from this repository, installed together:

- **limitswitcher** (`mods/limitswitcher`; called limit-status before 1.3.8, which updating replaces) reports session activity after every turn (usage percentages come from account-specific provider checks), runs the Jev compaction below, shows the reset alerts (Settings) as a toast, and adds [`/limits`](#limits) and [`/swapaccount`](#swapaccount). LimitSwitcher's line itself is in Claude Code's [status line](how-it-works.md#claude-code-status-line), where it can't be dismissed; installing the mod turns that on.
- **jev-compact** (`mods/jev-compact`) is the compaction. It is a plugin of its own because Claude Code skips a plugin's own compaction hook when that plugin starts the compaction.

Install it from **Settings → Claude Code Status mod → Install**. The app runs `claude plugin marketplace add` and `claude plugin install` for you (this repository is the marketplace) and tells the plugins where the app's files are. The row then shows **Active** while a session is reporting, **Installed** until one is, **Update** when only an older limitswitcher is there, or **Not installed**. Open sessions pick it up after `/reload-plugins`. Without the mod everything keeps working through the status line script (without Jev compaction).

### /limits

Every Claude account's usage at a glance, in any Claude Code session with the mod. It's made for the Claude app on your phone during a [Remote Control](https://code.claude.com/docs/en/remote-control) session, where Claude Code's status line doesn't show. The session's own account comes first, with its model, a bar for each limit and the context, and what Jev did for the session; then each other Claude account on one line (Codex accounts aren't listed). With separate accounts per window, it shows the window's own account, and where each account is in use:

```
⇄ doge2 · Opus 5.5 (medium) · window 1

🟢 5h   ██████░░░░   63% left
🟡 1w   █░░░░░░░░░   13% left
🟢 ctx  ████████░░   78% left · 217k

🗜 Jev saved ~56k before the last swap (12m ago)

Other accounts

🟢 doge1  5h  92%  1w  71% · main
🔴 doge3  5h   0%  1w  40%  ↻ 38m
```

The dot is each line's level (green, yellow under 30% left, red under 10%), and a used-up limit shows when it resets. The reply is markdown (a table with the bars, a list of the other accounts), which Claude Code and the Claude app draw without colours, hence the dots. The mod answers it from the app's numbers, so it costs no Claude usage; the reply is part of the conversation, so the model reads it with your next message (a few hundred tokens).

### /swapaccount

`/swapaccount <name or email>` moves the window it's typed in, and only that one, to another Claude account from its next message; every other window, and new ones, stay as they are. See [Separate accounts per window](usage.md#separate-accounts-per-window). In name mode a name or an email picks the account, otherwise an email (or the part before the @). `/swapaccount main` puts the window back on the main account. With nothing after it, or a name that doesn't match:

```
No Claude account called nope.

Claude accounts (/swapaccount <name or email>, or main)

- 🟢 doge1 · 5h 92% · 1w 71% · this window · main
- 🟡 doge2 · 5h 63% · 1w 13% · window 2
- 🔴 doge3 · 5h 0% · 1w 40%
```

## Jev compaction

Optional. Swapping a long session to another account costs a cold start: the new account has none of the session cached, so its next turn reads the whole context uncached. With **Settings → Jev compaction** on, LimitSwitcher swaps the account as usual, then has the session compacted before it goes on:

1. The session hits its limit; the account is swapped at once.
2. The mod first compresses consecutive identical output lines with their exact repetition counts. Byte-identical calls with identical inputs can refer to a retained result by its original tool-use ID. Changed file versions, different commands, indentation and line order remain separate evidence. [Jev](https://openrouter.ai/typesafe/jev-1.13) scores the remaining older outputs: needed outputs stay whole, uncertain ones keep their head and tail, and low-scored outputs become omission notes. Shortening is accepted only when the actual replacement is smaller. Rerun cost does not determine relevance. Images remain whole when Jev is uncertain; low-scored images may be removed.
3. The session goes on (Auto resume) or waits for your next message (Auto swap alone). While it runs, Claude Code shows `⇄ LimitSwitcher · Jev Compacting…` (also for `/jevcompact`). If the session is very large, Auto resume may first ask for your OK: a band above the prompt says so, and `/resumeok` answers it ([Auto resume](usage.md#auto-swap-and-auto-resume)).

What it preserves: user and assistant prose, every original tool input (including scripts and edits), the first message, the 8 newest messages, calls still running, and error outputs. No call is removed. Exact-duplicate references retain their canonical output across subsequent compactions; existing omission and repetition notes are protected from further pruning. Size targets are soft and never override a retention decision.

Omission indexes are navigation hints, not a complete record: they can miss short numbers, units, relationships, or facts the scorer mistakenly calls irrelevant. Current files and reruns cannot necessarily recover historical or remote observations. No immutable archive or retrieval tool is enabled. Credential-pattern masking is applied before sending scorer state, but it is not a general personal-data redaction system.

**Resuming an old session.** On a Pro or Max plan, Claude Code asks "Resume this conversation?" when you load a conversation idle long enough that none of it is cached and reloading it would use a real share of your 5-hour limit (about 5% or more), and says what share that is. That usage is only spent when your first message goes. With Jev compaction on, a toast beside the question says `💡 Jev compaction ready: upon resume, Jev will compact, saving usage` Nothing happens while the question is up. Once you pick **Resume** (or press Esc), Jev compacts the session right away: an orange band above the prompt says `⇄ LimitSwitcher · Jev compacting the resumed session…`, then a toast says what it saved (`✅ Jev saved ~289k of 666k tokens (43%) before this resume`). A message you type meanwhile waits in Claude Code's own queue and goes, as typed, once Jev is done (one sent in the second before Jev starts is put back in the box instead, to send again). **Start a new conversation** compacts nothing. It only does this when Claude Code actually asked the question, for a loaded conversation of 100k tokens or more: a resume Claude Code doesn't ask about gets no note and no compaction (an account switch is the swap's own compaction, above).

You can also run it by hand in any session with `/jevcompact` (it works with the toggle off); a line under it then says what it saved (`Jev compacted: saved ~39k of 331k tokens`), or why it didn't run. Your earlier thinking stays wherever the API allows it (an edit invalidates the thinking after it on accounts created since Aug 31, 2026; Claude Code then drops those blocks and retries by itself). The compaction itself makes no Claude model call (it works on a used-up account); its OpenRouter cost depends on scorer requests and provider billing. A whole compaction attempt has a 55-second deadline and at most three scorer requests in flight. Late replies are discarded and queued requests stop. The runtime may still finish an HTTP request already sent, so the deadline does not promise remote cancellation or prevent its billing. On a swap failure, the mod retries after 30 seconds, three attempts in all; the app stops waiting after 4 minutes. `/compact` and auto-compaction stay Claude Code's own.

**Setting it up:** install (or update) the mod, turn on Settings → **Jev compaction** (it sits under the mod's row and is off until you switch it on; off, the mod never asks for a compaction), and give it an [OpenRouter key](https://openrouter.ai/keys):

- paste it in the field that appears under the toggle. What you type shows only as dots; once saved it just says "Key set" with a Remove button (click it twice: the second click says Confirm). It is saved as the `.env` line below, readable only by you;
- or put an `OPENROUTER_API_KEY=...` line in the `.env` file in LimitSwitcher's data folder (`%LOCALAPPDATA%\LimitSwitcher\.env`, or `~/Library/Application Support/LimitSwitcher/.env`);
- or set the `OPENROUTER_API_KEY` environment variable, or put it in Claude Code's `settings.json` `env` block.

The app only writes the key you paste and checks that one is there; only the mod reads it, and the app never shows it again.

## Verification and measurement

`claude plugin test mods/jev-compact` covers historical values and units, input recipes, source/indentation boundaries, unresolved errors, exact duplicate references over two compactions, repeat-count round trips, malformed scores, negative savings, and job deadlines including late replies. The tests use synthetic content and fake scorers. They verify retained evidence, not a live model's ability to reason from it.

Run `bun mods/jev-compact/bench/retention.ts` for synthetic continuation-evidence checks. `--baseline <absolute path to an older src/compact.ts>` compares a trusted local baseline on the exact same fixtures. Three additional seeds cover repetitive and unique-output profiles; every fake score is 0.99. Checks require the original prose, inputs and reconstructed outputs after two compactions. These deliberately controlled profiles expose retention and compression tradeoffs rather than estimating average user savings.

Against baseline commit `79cbb03`, each repetitive fixture went from about 530k estimated tokens to 8.6k, versus 176k with the baseline; evidence checks passed 153/153 versus 99/153. Across both passes, estimated scorer input was 12.6k versus 70.8k. The unique-output fixtures retained more evidence at a cost: 277.4k remaining versus 274.4k, with 153/153 checks versus 149/153, and 74.2k estimated scorer input versus 70.9k. Character counts use 3.2 characters per token. No live Jev, continuation model, or billing was measured, and favorable repeat-heavy results do not establish typical savings.

The existing `summary.ts --oracle --budget 0` benchmark uses hindsight identifier labels. It still reports identifier and stricter whole-line losses, which are diagnostic proxies, not semantic completeness. Its fixtures use repository files, so editing the repository changes the input; compare implementations on identical generated messages. Historical real-session percentages and identifier-loss comparisons from older releases do not describe the current policy.

An index alone cannot guarantee recovery. A regression intentionally demonstrates that a unique short value can still disappear when scored irrelevant. Future evaluation should run actual held-out continuations and count wrong answers/actions, retrieval failures, latency, and billed input/output/cache tokens across compaction and subsequent work. Such runs need separate live evaluation; the shipped offline harness makes no network calls.

### Archive capability status

Archival retrieval is disabled. A temporary synthetic probe in the installed `claude plugin test` host called `$.tool.register({name: 'jev_synthetic_probe', description: 'Read a synthetic constant', inputSchema: {type: 'object', properties: {}, additionalProperties: false}})` with no filesystem or model calls. It returned `TypeError: $.tool.register is not a function`. The installed executable contains runtime registration support, but the isolated test host cannot prove the actual registration, permission, private-storage, restart and retrieval lifecycle. The probe was removed after recording the result; no production registration or archive was installed.

Before enabling archives, prove immutable private storage outside repositories, exact verified input/output round trips, opaque session-scoped IDs with bounded reads, tool availability after restart, reference survival over repeated compactions, and safe fallback on denied access, corruption, failed writes, missing blobs, or unsupported image payloads. Until then, the compactor writes no archive files and emits no external artifact references.
