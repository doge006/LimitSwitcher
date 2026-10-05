# LimitSwitcher

A Windows tray / macOS menu bar app that shows every Claude Code and Codex usage limit at a glance, switches accounts in one click, and can switch automatically when the account in use hits a limit.

![The full view](docs/media/demo.gif)

<p align="center"><img src="docs/media/panel.png" alt="The tray panel" width="420"></p>

**On the taskbar** (Windows): the accounts in use sit right on the taskbar, and step aside for full-screen apps.

![The taskbar view](docs/media/taskbar.png)

## Why not something else?

I made this mainly for myself and decided to publish it on GitHub. Nothing else (at the time) did what I wanted:

- **Usage trackers** show your limits, but don't switch accounts for you.
- **Account switchers** swap logins, but you have to notice the limit yourself, and sessions that are already open often need a restart.
- **Few cover both** Claude Code and Codex, and fewer run on Windows.
- **Most are heavy.** I wanted mine to be lightweight.

LimitSwitcher does all of it in one small tray app: every limit at a glance, one-click switching that open sessions pick up, and Auto swap / Auto resume, so a long task keeps going when an account runs out.

## Install (Windows)

Download **`LimitSwitcher-Setup.exe`** from the [latest release](https://github.com/doge006/LimitSwitcher/releases/latest) and run it. It installs for your Windows user only (no admin rights), asks where to put it (`%LOCALAPPDATA%\Programs\LimitSwitcher` by default), and has boxes for a Start menu entry (on) and a desktop shortcut (off). It starts in the tray when you sign in; turn that off in Settings. It brings its own Python, so nothing else is needed.

Windows may say "Windows protected your PC" the first time, because the installer isn't code-signed: click **More info → Run anyway**.

Your accounts and settings are kept in `%LOCALAPPDATA%\LimitSwitcher` (`AccountSwitcher` before the app was renamed: an old folder is moved over by itself), so updating, reinstalling or uninstalling keeps them. Uninstall from **Settings → Apps**; it closes the app and puts the Codex and Claude Code settings back first.

**Updates:** the app checks GitHub Releases once at launch and tells you when a new version is out. Settings → **Update to …** downloads the new installer, which closes the app, replaces its files and starts it again. **Check for updates** checks now.

## Install (macOS)

Paste this into Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/doge006/LimitSwitcher/main/scripts/install-mac.sh | bash
```

It downloads the app (for Apple silicon Macs: M1 or later) from the [latest release](https://github.com/doge006/LimitSwitcher/releases/latest), puts **LimitSwitcher** in Applications and opens it. It brings its own Python, so nothing else is needed. Running the same command again updates it.

Prefer to click? Download `LimitSwitcher-AppleSilicon.dmg` from the latest release and drag the app to Applications. The app isn't notarized by Apple (that needs a paid developer account), so macOS blocks it the first time: open **System Settings → Privacy & Security** and click **Open Anyway**. The Terminal command avoids that, because macOS only checks apps downloaded by a browser.

It lives in the menu bar (no Dock icon) and starts there when you log in; turn that off in Settings. Your accounts and settings are kept in `~/Library/Application Support/LimitSwitcher` (`AccountSwitcher` before the rename: an old folder is moved over by itself). **Updates:** Settings → **Update to …** runs the same install for you. See [macOS](#macos) below for how it works there.

## Performance

Built to be barely noticeable:

- **One small process.** Every window is native: no browser, no Electron. On Windows the app draws them itself; on macOS the full view is drawn with the system's own graphics, in a second process that exists only while its window is open (its memory goes back to macOS when you close it), and the menu bar panel uses the system's WebKit view, loaded only while it's open.
- **It sleeps** until an account is due for a usage check or something changes, and uses no CPU in between. Claude accounts are checked about once a minute (the one in use follows Claude Code's own status line instead, with a check every 30 minutes), Codex accounts every 90 seconds in use and every 5 minutes otherwise. The connection to each service stays open between checks, so a check is under 1 KB in all rather than a new secure connection (several KB) every time.
- **Windows exist only while they're open.** The panel, the menus and the full view are created when you open them and freed when you close them, and animation frames are drawn only while something moves.
- **Checking usage doesn't touch your limits:** the usage endpoints it reads don't count against them.

### Measured

Windows 11:

| | Memory (working set) | Private memory | CPU (one core) |
|---|---|---|---|
| In the tray and taskbar, windows closed | 12.8 MB | 35.5 MB | 0.08% |
| Tray panel open (popped out) | 34.0 MB | 39.2 MB | 0.13% |
| Full view open | 35.8 MB | 45.8 MB | 0.16% |

(The working set is what Task Manager shows. It can be lower than the private memory, because pages the app isn't using are handed back to Windows until they're needed again.)

MacOS 27 (Retina display):

| | Memory (footprint) |
|---|---|
| In the menu bar, windows closed | 47 MB |
| Menu bar panel open | 61 MB |
| Full view open (its own process, on top of the menu bar app) | 70 MB |

(The footprint is Activity Monitor's Memory column. Opening the full view briefly takes more while its cards fade in, then settles within a few seconds.)

### Measure it on Windows

In PowerShell, with LimitSwitcher running. This samples it over 60 seconds:

```powershell
$before = (Get-Process LimitSwitcher).CPU; Start-Sleep 60; $p = Get-Process LimitSwitcher
'{0:N1} MB memory ({1:N1} MB private), {2:N2}% of one CPU core' -f ($p.WorkingSet64 / 1MB), ($p.PrivateMemorySize64 / 1MB), (($p.CPU - $before) / 60 * 100)
```

Try it idle in the tray, with the panel open, and with the full view open. Task Manager shows it too, as **LimitSwitcher**.

### Measure it on macOS

In Terminal, with LimitSwitcher running. This samples it over 60 seconds:

```sh
pids=$(pgrep -fl 'MacOS/LimitSwitcher' | grep -v -- --full-view | head -1 | cut -d' ' -f1)
cpu() { ps -o time= -p "$pids" | awk -F: '{s=0; for (i=1; i<=NF; i++) s=s*60+$i; print s}'; }
a=$(cpu); sleep 60; b=$(cpu)
footprint -p "$pids" | awk -v a="$a" -v b="$b" '/Footprint:/ {printf "%s %s memory, %.2f%% of one CPU core\n", $(NF-5), $(NF-4), (b-a)/60*100; exit}'
```

The memory is the app's footprint, the same number as Activity Monitor's Memory column (search for **LimitSwitcher**). It leaves out the system libraries every app shares. Try it idle in the menu bar and with the panel open. The full view runs as a second LimitSwitcher process, only while its window is open, and ends when it closes, so its memory goes back to macOS: `pgrep -f 'LimitSwitcher --full-view'` gives its pid while it's open.

## macOS

- **Menu bar:** the tray becomes a menu bar icon.
  - Click it for the panel: it opens under the icon, with an arrow pointing at it, on the system's frosted popover material.
  - Drag the panel by its header or background: away from the menu bar it stays open where you leave it (the arrow goes), and you can move it again any time. Its dock button (an arrow up to a bar) slides it back under the menu bar icon. Docked, clicking elsewhere or Esc closes it.
  - Right-click (or Control-click) for the menu; **Full View…** opens the full view in its own native window.
  - There's no Dock icon.
- **Claude Code** keeps its login in the macOS Keychain ("Claude Code-credentials"). The app switches that item, and a running Claude Code picks it up on its next request.
- **Codex** works exactly as on Windows (the local router, `~/.codex/auth.json`).
- **Saved logins** are encrypted with a random key kept in your login Keychain, under `~/Library/Application Support/LimitSwitcher`.
- **Start at login:** a LaunchAgent (`~/Library/LaunchAgents/com.accountswitcher.app.plist`).

## Your accounts

- **Works with:** Claude Code (the CLI and its VS Code extension) and Codex (the CLI, its editor extensions and the Claude Code Codex plugin). A Codex session or editor that was already open before LimitSwitcher started keeps its account until it's reloaded; LimitSwitcher tells you when that's the case.
- **Adding accounts:** whatever Claude Code / Codex login is active on your PC is picked up automatically. Signing in to another account (`claude auth login`, `codex login`) adds it too. **Add account** (in the full view or the tray menu) runs the official sign-in in a separate window and an isolated folder, so the login you're using isn't touched.
- **Switching:** click an account and every session moves to it, including sessions that are already open. Nothing needs restarting.
  - *Claude Code:* the app saves the outgoing account's newest tokens and writes the chosen login into `~/.claude/.credentials.json` + `~/.claude.json`. A running Claude Code notices and uses it on its next request.
  - *Codex:* the chosen login is written into `~/.codex/auth.json` (so new windows and Codex's `/status` show it), and while the app runs, Codex sends its requests through the app (a local router on `127.0.0.1`), which adds the chosen account's login. So a switch also applies to sessions that are already open, on their next request.
  - *On quit:* the app puts `~/.codex/config.toml` back exactly as it was, so Codex works without the app, on the chosen account.
- **Separate accounts per window** (Claude Code, prototype; Settings): each new Claude Code window gets an account of its own, and you can move one window to another account without touching the rest.
  - *How:* while it's on, a small `claude` wrapper goes first on your PATH (Windows: your user PATH, for terminals opened from then on; elsewhere, add the folder it names). Each new interactive window asks the app for a free account and starts with its own config folder (`CLAUDE_CONFIG_DIR`, in the app's data folder under `profiles/`) holding only that login; settings, CLAUDE.md, plugins, skills and session history are linked to `~/.claude`, so `/resume` sees every window's sessions. When no account is free, or the app isn't running, the window shares the main login as before. Windows already open keep sharing it until reopened.
  - *Switching one window:* the full view lists the windows above the accounts. Click one: it's outlined on screen and its taskbar button flashes (Windows; in Windows Terminal the whole window, since its tabs share it). Then click **Use in window** on an account: only that window's login changes, and it uses the new account on its next request. **Own window** on a card opens a new terminal on that account directly.
  - *Limits:* a usage limit in such a window moves that window alone to a free account (Auto swap), and Auto resume continues it. Because a login renewed in one place is signed out everywhere else, an account is in one place at a time: the main login or one window. The app never renews a window's tokens itself, and when a window closes its folder goes and the account is free again.
  - From a terminal: `python -m account_switcher.profiles list | open EMAIL | env EMAIL | remove window-<id>`.
- **What the app changes in `~/.codex/config.toml` while it runs** (every line is tagged `# account-switcher` and removed again on quit):
  - `openai_base_url` points at the router;
  - `enable_request_compression = false`, so the router can read requests;
  - `daemon_auto_start = false`. Codex's shared background server opens a console window for every command on Windows ([openai/codex#44768](https://github.com/openai/codex/issues/44768), [#48074](https://github.com/openai/codex/issues/48074)). Sessions also attach to it whenever it's running, and it keeps the settings it started with, so its sessions would bypass the router. The app stops a running one as soon as no session has been active for 90 seconds; after that, each Codex session runs in its own terminal, goes through the router, and nothing flashes.
- **Start with Windows:** on by default, since Codex's requests go through the app. Switch it off in the full view's Settings (**Launch with Windows**).
- **Usage:** read from each provider's own usage endpoint:
  - Claude: 5-hour, weekly and per-model weekly caps, plus extra usage.
  - Codex: 5-hour, weekly or 30-day windows, plus credits.

  These are read-only status endpoints, the same ones behind Claude Code's `/usage` and ChatGPT's usage page. **Checking usage does not use any of your quota.**
- **Staying clear of rate limits:**
  - Claude: every account is checked every minute, and the account in use only every 30 minutes while Claude Code's status line already reports it live. Requests identify as Claude Code, because Claude's usage endpoint throttles other clients hard.
  - Codex: the account in use every 90 seconds (45 near a limit), others every 5 minutes or just after a reset.
  - Passed reset times are applied locally without a request.
  - Requests are spaced out.
  - Opening the panel only refetches data older than 2 minutes, and Refresh works at most every 30 seconds.
  - A 429 backs off exponentially, from 5 minutes up to an hour.
- **Subscription:** "Renews Oct 14" or "Ends Oct 14" shows when a subscription renews or has been cancelled.
  - Codex: the paid-through date comes from its login token; cancellation is read best-effort from ChatGPT's account check.
  - Claude: its profile reports only when the subscription started and whether it's active or cancelled. The renewal is estimated as the next monthly anniversary and shown with a ~ (e.g. "Renews ~Oct 14").
  - Both are checked at most once a day. When nothing is reported, click **Set renewal date** on the card; a date you enter always wins.
  - The names of the fields these endpoints return (never their values) are kept in `subscription-fields.json`, to help match the detection to real responses.
- **Usage limit resets:** banked resets are shown for Codex, which reports them. Claude's usage response doesn't include its free resets (they appear only in Claude's settings), so none are shown for Claude.
- **Auto swap:** each account is used to 100%; then the app moves to the account whose weekly limit resets first (so that quota is used before it's lost; one with under 5% left only when nothing has more), and the thread carries on with everything it had.
  - *Codex:* the request that hit the limit is sent again on the next account, so the session never sees the error. If ChatGPT's response headers already showed the account used up, a new turn simply starts on the next account.
  - *Claude:* Claude Code shows its limit message. The hook below switches accounts right away, so your next message uses the new account; with Auto resume on, it also continues by itself.
  - *Claude threads:* nothing in them is tied to an account, so they carry over whole.
  - *Codex threads:* ChatGPT encrypts two things for the account that made them, which another account can't read. The app makes no extra requests for this:
    - *Hidden reasoning:* another account receives the plain-text summary ChatGPT returned with it.
    - *Compaction checkpoints* (the summary Codex keeps instead of old history): the thread stays on the checkpoint's account while that account has quota. Once it's used up, the thread moves on without that older summary; the recent conversation is kept.
- **Auto resume:** while it's on, a Claude Code session that stops on a usage limit continues by itself, with nobody typing.
  - The app adds a `StopFailure` hook to `~/.claude/settings.json` while it runs, whatever the toggles say, and removes it when it quits (Claude Code reads hooks when a session starts, so a hook added later would miss open sessions); the app answers "do nothing" while both are off. Only its own entry is added.
  - While Auto resume is on, it also turns Claude Code's own wait (`autoContinueAtUsageLimit`) **on**. With it off, a usage limit opens a "What do you want to do?" dialog that holds the session until you answer it, and the app's continue waits behind it. With it on there is only a one-line wait, which Claude Code cancels by itself when the account is switched or a new turn starts. The hook also skips its continue if the session has already gone on by itself while it waited. Your own setting is put back when Auto resume is off or the app quits.
  - When the hook fires, the app switches to an account with room (Auto swap) and Claude Code is told to continue where it left off.
  - If no account has room, it waits for the earliest reset (up to 6 hours) and then continues.
  - Codex needs no hook: its requests are retried on the next account automatically.
- **Storage:** saved logins are encrypted with Windows DPAPI (tied to your Windows user) under `%LOCALAPPDATA%\LimitSwitcher`. Nothing is sent anywhere except the providers' own usage and token endpoints.
- **Token ownership:** each account should be managed from here only. If the same account is also signed in elsewhere and refreshes its token there, this copy expires and shows "Sign in again". The in-use account's token is never refreshed by this app; that stays with Claude Code / Codex.

Check the providers' terms for using several subscriptions this way; that's your call.

## Tray

- **Left-click the icon:** a compact panel opens above it. It lists every account by email with its plan and subscription status ("Renews 18d" / "Ends 3d"), plus 5h / 1w / per-model headroom with the time until each resets. Click an account to switch; you'll see "Switching…" until it lands. Clicking the icon again closes the panel, pinned or not.
- **Hidden icons (^):** if the icon lives in Windows' hidden-icons popup, left-clicking it closes that popup (the app sends it one Esc, only when that popup is the active window) and the panel takes its place. Right-clicking leaves it open, like Steam's menu.
- **Motion:** hovers fade, switches slide, the "in use" marker cross-fades on a switch, and bars glide to new values. Animation frames are drawn only while something moves.
- **The panel:**
  - **Pop out** (next to **Full view ›**) pins the panel: it stays open and you can drag it by its header. Click it again to put it back.
  - **Compact** (the button next to the power button) shrinks it to the accounts in use. The compact panel is always popped out; **Expand** brings the full panel back (still popped out) and **Hide** puts it away, so the tray icon opens the normal panel next time.
  - **Power** quits, after a second click to confirm.
  - **Full view ›** opens the full view: a native window of the app's own (no browser), drawn like the panel.
  - Esc or clicking elsewhere closes an unpinned panel.
- **Right-click:** a menu in the same style: Open panel, Full view, Auto swap, Auto resume and Quit. Toggling Auto swap or Auto resume keeps the menu open.
- **Hover:** the tooltip shows the account in use per provider and what's left.
- **The icon's dot:** green, amber or red for the tightest limit in use.
- **Launching again:** opens the running copy's full view instead of starting a second copy.
- **Quit** stops everything the app started and puts the Codex and Claude Code settings back. The app starts with Windows (Codex routing depends on it); Settings → **Launch with Windows** turns that off.

## Settings

![Settings](docs/media/settings.gif)

In the full view, the gear opens Settings:

- **Auto swap:** move to the account whose weekly limit resets first when a limit hits.
- **Auto resume:** after a usage limit, the session continues by itself, on another account or once the limit resets.
- **Jev compaction** (under the Claude Code Status mod, off by default): before a swapped session goes on, shrink the tool outputs it no longer needs, so the new account loads less (see below).
- **Reset alerts** (on by default; needs the Claude Code Status mod): when every Claude (or every Codex) account has hit its limit, a toast in each open Claude Code session says which one has room again as soon as its limit resets (within about 30 seconds). Nothing shows while another account still has room, since Auto swap already uses it.
- **Name mode:** names instead of emails everywhere (panel, taskbar, status line, notifications), for screen sharing. Click an account's name in the full view to set it.
- **24-hour clock:** reset times like 14:30 instead of 2:30 PM.
- **Launch with Windows / macOS:** start in the tray when you sign in.
- **Taskbar view** (Windows): the accounts in use, right on the taskbar, on the display you choose.
- **Check for updates / Update to …**

## Claude Code Status mod (optional)

Claude Code mods (early access; Claude Code 2.1.287 or later) run inside Claude Code. The mod is two plugins from this repository, installed together:

- **limit-status** (`mods/limit-status`) gives LimitSwitcher Claude Code's live usage after every turn, straight from Claude Code, runs the Jev compaction below, and shows the reset alerts (Settings) as a toast. LimitSwitcher's line itself is in Claude Code's status line (see below), where it can't be dismissed; installing the mod turns that on.
- **jev-compact** (`mods/jev-compact`) is the compaction. It is a plugin of its own because Claude Code skips a plugin's own compaction hook when that plugin starts the compaction.

Install it from **Settings → Claude Code Status mod → Install**. The app runs `claude plugin marketplace add` and `claude plugin install` for you (this repository is the marketplace) and tells the plugins where the app's files are. The row then shows **Active** while a session is reporting, **Installed** until one is, **Update** when only an older limit-status is there, or **Not installed**. Open sessions pick it up after `/reload-plugins`. Without the mod everything keeps working through the status line script below (without Jev compaction).

## Jev compaction (optional)

Swapping a long session to another account costs a cold start: the new account has none of the session cached, so its next turn reads the whole context uncached. With **Settings → Jev compaction** on, LimitSwitcher swaps the account as usual, then has the session compacted before it goes on:

1. The session hits its limit; the account is swapped at once.
2. The session's mod runs the compaction (every shortened output keeps a note listing the names, paths and values it held). [Jev](https://openrouter.ai/typesafe/jev-1.13) (TypeSafe's decision model, through OpenRouter) scores the session's older tool outputs: still needed, unsure, or done with. Outputs it is done with become a one-line note, unsure ones keep their head and tail, the rest stay whole; a file view or search (cheap to read again) needs a higher score to stay whole than a test run or a web page. A file read again later is replaced by a note without asking, and long old scripts and file contents Claude wrote are shortened (what they did is on disk).
3. The session goes on (Auto resume) or waits for your next message (Auto swap alone). While it runs, Claude Code shows `⇄ LimitSwitcher · Jev Compacting…` (also for `/jevcompact`).

What it never touches: anything you or Claude wrote, the first message, the 8 newest messages, calls still running, and error outputs. Nothing is summarised and no call is removed: Claude still sees every step it took, and each shortened output says so, so it re-runs the tool instead of guessing. Keys and tokens in the conversation are masked before anything is sent to Jev.

You can also run it by hand in any session with `/jevcompact` (it works with the toggle off). Your earlier thinking stays wherever the API allows it (an edit invalidates the thinking after it on accounts created since Aug 31, 2026; Claude Code then drops those blocks and retries by itself). It costs no Claude usage (it works on a used-up account) and a fraction of a cent of OpenRouter credit per swap. If Jev fails, the mod waits 30 seconds and tries again, three tries in all; then (or after 4 minutes at most) the session goes on without it. `/compact` and auto-compaction stay Claude Code's own.

**Setting it up:** install (or update) the mod, turn on Settings → **Jev compaction** (it sits under the mod's row and is off until you switch it on; off, the mod never asks for a compaction), and give it an [OpenRouter key](https://openrouter.ai/keys): paste it in the field that appears under the toggle (what you type shows only as dots, and once saved it just says "Key set" with a Remove button (click it twice: the second click says Confirm); it is saved as the `.env` line below, readable only by you), or put an `OPENROUTER_API_KEY=...` line in the `.env` file in LimitSwitcher's data folder (`%LOCALAPPDATA%\LimitSwitcher\.env`, or `~/Library/Application Support/LimitSwitcher/.env`), the `OPENROUTER_API_KEY` environment variable, or Claude Code's `settings.json` `env` block. The app only writes the key you paste and checks that one is there; only the mod reads it, and the app never shows it again.

**How much it saves, and what it costs in quality:** benchmarked on a real 512k-token session (the one that built this) with the live Jev, against the other Jev compaction tools on the same session. *Quality* is a hindsight test: compact the session as it stood at three earlier points, then count the project facts (names, paths, values; not words or standard-library names the model knows anyway) Claude went on to use from memory that only a removed output held.

| | conversation smaller | after compaction (Claude Code's count) | facts lost that Claude used later | also |
|---|---|---|---|---|
| **this** | 44% | 116k | **2** | nothing deleted; errors, your text, recent messages untouched |
| cc-mod-jev | 62% | 104k | 353 | deletes calls, cuts error outputs |
| HAR5HA jev-compact | 75% | 97k | 379 | cuts error outputs, rewrites your text |
| fast-jev-compaction | 90% | 33k | 499 | deletes almost every call, touches the newest messages |

**Long sessions** (a conversation over about 333k tokens) also get a budget: after the usual decisions, the least needed outputs, oldest first, keep stepping down (whole, head and tail, a note) until the conversation is about a third of its size. On a 731k-token session: 61% to 63% of the conversation cut for 20 to 25 facts lost (the default alone: 58% / 16); Claude Code counts 733k before and 185k after. It levels off near 64%: what is left is your text and Claude's, the newest messages, errors and the notes themselves.

Any compaction also drops the old system notices Claude Code repeats through a session, which is why every row ends far below the 512k it started at. Every note this one leaves names what it removed and nothing else still shows (the names, paths and values it held), so Claude re-checks instead of guessing; that cut the facts lost from about 30 to 2. cc-mod-jev ends 12k tokens smaller for over 150 times the loss. Outputs cheap to get again (file views, listings, searches) need a higher score to stay whole than test runs, web pages or anything that changed state. To measure your own: `bun mods/jev-compact/bench/bench.ts <transcript.jsonl>` runs it on a saved Claude Code transcript (`~/.claude/projects/...`) and prints how much smaller the session gets and what Jev cost (`--fake 0.5` for a dry run without a key; `--decisions` lists every decision; `--quality` runs the hindsight test).

## Claude Code status line

LimitSwitcher shows itself in Claude Code's status line (the line under the prompt) once the Claude Code Status mod is installed (Settings → **Claude Code Status mod**).

- **Why it's there:** Claude Code hands the status line the live 5-hour and weekly usage of the account in use. That's how LimitSwitcher follows Claude usage live, after every reply, without asking Claude's usage API.
- **With the mod installed, without a status line of your own:** it shows `⇄ LimitSwitcher`, the account in use, the session's model and effort (`Opus 5.5 (high)`), what's left of its limits, and the session's context (`ctx 183k/82% left`: tokens in use and what's left of Claude Code's context window; the numbers go green, yellow and red as it fills). While Jev compacts the icon is yellow and says so; for 45 seconds after, the line says what it saved (`Jev saved ~120k`), and ctx shows the size less what Jev saved until the next reply. With your own status line, the context is already in the input Claude Code gives it.
- **With your own status line:** LimitSwitcher runs yours for you, so the usage still comes in, and yours stays exactly as it was. With the mod installed, a dim `⇄ LimitSwitcher` follows it, so you can see the app is on.
- **Without the mod, and without one of your own:** Claude Code's status line is left alone, and the account in use is checked through the usage API instead (every minute).
- **Every session stays current:** Claude Code only knows the usage from a session's own last reply, so an idle session would keep old numbers. LimitSwitcher has Claude Code refresh the status line every 5 seconds (unless you set your own `refreshInterval`; a run is a small process start of about 15 ms, well under 1% of a core per open session), so a compaction shows up within a few seconds. It gives your own status line command its freshest numbers for the account.
- **On quit** your original status line setting is put back.

## Development

Building from source, running the tests, publishing a release and how the code is laid out: see [DEVELOPMENT.md](DEVELOPMENT.md).

## License

MIT: see [LICENSE](LICENSE).

## Credits

Built with [Claude Code](https://claude.com/claude-code).

`account_switcher/providers.py` follows the usage clients of Codex Vitals (https://github.com/Joowonoil/Codex-Vitals, MIT; see `THIRD-PARTY-NOTICES.txt`).
