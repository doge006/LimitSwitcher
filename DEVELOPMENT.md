# Developing LimitSwitcher

## Install and update from source (Windows)

Double-click `Install.cmd` (or `Update.cmd`, which does the same). Run it again at any time to update.

Each run:
1. Makes sure Python 3.10+ and git are there (installing them with winget).
2. Updates this folder from GitHub (`main`). Local edits and local-only commits are never lost: without `--force` it stops and says why, and with `--force` it saves them to `git stash` or a backup branch first.
3. Sets up a private Python environment (`.venv`) with this OS's requirements; they're only reinstalled when they change.
4. Adds a Start menu shortcut.
5. Closes any running copy and starts the new version. Its output is also saved to `update.log` next to `app.log`.

Options: `--branch NAME` (follow another branch), `--force`, `--no-launch`. A copy from source also offers updates in Settings; there, **Update** runs this installer.

A first install from nothing: `git clone https://github.com/doge006/LimitSwitcher.git`, then run the installer in that folder.

## Development (any OS)

```sh
python -m account_switcher.tray          # the app (from a source copy: .venv\\Scripts\\python on Windows)
python -m account_switcher.tray --demo   # sample accounts, no real logins touched (Linux: needs python3-tk)
python -m unittest discover -s tests -v
```

The mod's plugins have their own tests, run by Claude Code (2.1.287 or later):

```sh
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude plugin test mods/limitswitcher
CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude plugin test mods/jev-compact
```

The **Tests** workflow runs both on every push and pull request. The Windows workflows (the installer, smoke tests on a real desktop, profiling) are run by hand from the Actions tab.

Real-account tests use fake login files and a fake provider API. Tray tests use pystray's dummy backend and need `pystray` + `Pillow`. Outside Windows, saved logins are stored unencrypted in owner-only files; that fallback is for development only. The local API listens on 127.0.0.1 only and needs a per-run token.

## Demo mode

`--demo` shows sample accounts instead of your real logins, with no network use. It's for screenshots and the Windows smoke test.

## Measuring performance

`scripts/measure.py` measures the running app: memory (working set and private), CPU share and threads over a stretch of time.

```powershell
.venv\Scripts\python -m pip install psutil     # once (an installed copy: runtime\python.exe -m pip ...)
.venv\Scripts\python scripts\measure.py        # 60 s; --seconds N
```

Measure the tray alone, with the panel open, and with the full view open, a few times each, and quote the typical number with the machine it ran on.

`scripts/bench.py` measures what needs no desktop: start-up, the time each animation frame of the panel and the full view takes to draw (at 100%, 150% and 200%), scrolling the full view (frames per wheel notch, time per frame, peak memory), memory, and idle CPU. `--against <folder>` measures another copy the same way, taking turns, and prints both side by side (a `git worktree` of the commit before a change):

```sh
git worktree add ../before main
python scripts/bench.py --against ../before
```

On a real Windows desktop, the **Windows app** workflow with **job** `frames` counts the frames per second the panel and the full view get while they animate and scroll, and the app's memory after scrolling, for this commit and for **base** (default `main`), and puts both in the run's summary.

## Publishing a release

Bump `VERSION` in `account_switcher/version.py`, write the release notes in `.github/release-notes.md` and add the highlights to `CHANGELOG.md`, merge, then run the **Release** workflow (Actions tab). It builds `LimitSwitcher-Setup.exe` (`scripts/build_windows.ps1`, Inno Setup), installs it silently and checks that the app runs, installs it again over the running copy (as an update does), uninstalls it, then publishes release `v<VERSION>`. Run it with **Publish** off to build and test only; the installer is then kept as a download on the run for 7 days. Users get it on their next launch.

Mac support ended with 1.4.19. `scripts/install-mac.sh` stays only because installed Mac copies run it from `main` when they see a newer release: it puts 1.4.19 back and starts it again.

## Layout

- `account_switcher/tray.py`: the app (tray icon, notifications, startup).
- `account_switcher/flyout.py` + `flyout_render.py`: the Windows panel and right-click menu (layered windows + Pillow drawing).
- `account_switcher/taskbar.py` + `placement.py`: the Windows taskbar view.
- `account_switcher/fullview.py` + `fullview_render.py`: the full view (behaviour and Pillow drawing), shown by `fullview_win.py` (Win32) and `fullview_tk.py` (Linux, Tk).
- `account_switcher/live.py`: real accounts (import, usage refresh, switching, auto swap, Auto resume decisions, add/remove).
- `account_switcher/providers.py`: Claude Code / Codex login files and usage APIs.
- `account_switcher/connections.py`: the usage APIs' HTTPS connections, kept open between checks (and TLS sessions resumed).
- `account_switcher/codex_proxy.py` + `codex_config.py`: the Codex router and the config lines that point Codex at it.
- `account_switcher/claude_router.py` + `window.py` + `profiles.py`: separate accounts per window: the Claude Code router (a window's requests with its account's login), the `claude` wrapper and its install, and the windows from before 1.4. The app's side (which window has which account, `/swapaccount`, a window's limits) is in `live.py`; `tests/test_windows_sim.py` runs it end to end with fake Claude Code windows (`tests/fake_claude.py`).
- `account_switcher/claude_hooks.py`, `afk_hook.py`, `statusline.py`: the Claude Code hook (Auto resume) and status line.
- `mods/limitswitcher` + `mods/jev-compact` + `.claude-plugin/marketplace.json` + `account_switcher/mod.py`: the optional Claude Code mod (live usage; `/limits` and `/swapaccount`, answered by `limits_text` and `swap_account` in `live.py`; the Jev compaction before a swapped session goes on), and the app's install/status of it. The swap side of the compaction is `claude_limit` in `live.py` (the hook's answer waits for it). Each plugin has its own tests: `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude plugin test mods/<name>`; `mods/jev-compact/bench/bench.ts` measures the compaction on saved transcripts.
- `account_switcher/integrations.py`: sets all of that up while the app runs and undoes it on quit.
- `account_switcher/web.py`: the controller and the local API (status line, hook, a second launch).
- `account_switcher/core.py` + `demo.py`: the account model and routing; sample accounts for `--demo`.
- `account_switcher/vault.py`: encrypted storage (DPAPI on Windows).
- `account_switcher/version.py` + `updates.py`: the version, and update checks / installs from GitHub Releases.
- `scripts/installer.py` (+ `Install.cmd`): install and update from source.
- `scripts/build_windows.ps1` + `LimitSwitcher.iss`, `win_launcher.c`: the Windows installer (the app, its own Python and `LimitSwitcher.exe`).
- `scripts/install-mac.sh`: kept for installed Mac copies' updater; it reinstalls 1.4.19 (see Publishing a release).
- `scripts/make_icons.py`: draws the app icon. `scripts/make_media.py` draws the README's screenshots and GIF (the **Media** workflow runs it on Windows).
- `docs/`: the user and design docs the README links to; `docs/media/`: the screenshots and GIFs (`demo.gif`, `settings.gif`).
- `scripts/render_frames.py`: frame hashes of the full view (Pillow, as Windows draws it) through a fixed script of states: run before and after a change to the drawing code to prove Windows pixels are unchanged.
