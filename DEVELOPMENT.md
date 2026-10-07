# Developing LimitSwitcher

## Install and update from source (Windows and macOS)

One installer for both; it detects the OS. Run it again at any time to update.

- **Windows:** double-click `Install.cmd` (or `Update.cmd`, which does the same).
- **macOS:** in Terminal, run `bash Install.command` in this folder (or `bash Update.command`).
  - Double-clicking works only if the folder came from `git clone`. A downloaded ZIP is flagged by macOS, and it refuses to open the script ("can't verify it's free of malware"). To double-click anyway, clear the flag once with `xattr -dr com.apple.quarantine <folder>`, or use **System Settings → Privacy & Security → Open Anyway**.
  - The **LimitSwitcher** app the installer creates opens without that warning, because it's made on your Mac.

Each run:
1. Makes sure Python 3.10+ and git are there. Windows installs them with winget; macOS asks for Apple's command line tools.
2. Updates this folder from GitHub (`main`). Local edits and local-only commits are never lost: without `--force` it stops and says why, and with `--force` it saves them to `git stash` or a backup branch first.
3. Sets up a private Python environment (`.venv`) with this OS's requirements; they're only reinstalled when they change.
4. Puts the app where you'd expect it: a Start menu shortcut on Windows, **LimitSwitcher** in Applications on macOS (`/Applications`, or `~/Applications` if that isn't writable).
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

The **Tests** workflow runs both on every push and pull request. The Windows and macOS workflows (installers, smoke tests on real desktops, profiling) are run by hand from the Actions tab.

Real-account tests use fake login files and a fake provider API. Tray tests use pystray's dummy backend and need `pystray` + `Pillow`. Outside Windows and macOS, saved logins are stored unencrypted in owner-only files; that fallback is for development only. The local API listens on 127.0.0.1 only and needs a per-run token.

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

On macOS, `scripts/mac_memory.py` shows where the menu bar app's memory goes, step by step (Python, PyObjC, the app's code), running with the installed app's own Python in demo mode; `scripts/compare_native.py` draws the full view natively off screen at Retina size and reports its memory, time per frame and how close it is to the Windows drawing:

```sh
/Applications/LimitSwitcher.app/Contents/Resources/runtime/bin/python3 -B scripts/mac_memory.py
```

The **macOS app** workflow (job `dmg`) runs it too, and prints the running app's footprint with the full view open and closed.

## Publishing a release

Bump `VERSION` in `account_switcher/version.py`, write the release notes in `.github/release-notes.md` and add the highlights to `CHANGELOG.md`, merge, then run the **Release** workflow (Actions tab). It builds `LimitSwitcher-Setup.exe` (`scripts/build_windows.ps1`, Inno Setup) and the two Mac DMGs (tested through `scripts/install-mac.sh`, including an update over a running copy), installs the exe silently and checks that the app runs, installs it again over the running copy (as an update does), uninstalls it, then publishes release `v<VERSION>`. Run it with **Publish** off to build and test only; the installer is then kept as a download on the run for 7 days. Users get it on their next launch.

The Mac disk images come from the **macOS app** workflow with **job** `dmg` (one Apple silicon runner): `scripts/build_mac.py` builds `LimitSwitcher-AppleSilicon.dmg` (the app with its own Python, ad-hoc signed, not notarized), then installs it with `scripts/install-mac.sh --dmg`, starts it and checks the menu bar icon, the window, the status line, the measuring snippet in `docs/performance.md`, quitting, and that nothing was written inside the app. The DMG is kept on the run for 14 days (the Mac app is Apple silicon only; `build_mac.py --arch x86_64` still builds an Intel one if ever needed).

## Layout

- `account_switcher/tray.py`: the app (tray icon, notifications, startup).
- `account_switcher/flyout.py` + `flyout_render.py`: the Windows panel and right-click menu (layered windows + Pillow drawing).
- `account_switcher/taskbar.py` + `placement.py`: the Windows taskbar view.
- `account_switcher/fullview.py` + `fullview_render.py`: the full view (behaviour and Pillow drawing), shown by `fullview_win.py` (Win32), `fullview_mac.py` (AppKit) and `fullview_tk.py` (Linux, Tk).
- `account_switcher/live.py`: real accounts (import, usage refresh, switching, auto swap, Auto resume decisions, add/remove).
- `account_switcher/providers.py`: Claude Code / Codex login files and usage APIs.
- `account_switcher/connections.py`: the usage APIs' HTTPS connections, kept open between checks (and TLS sessions resumed).
- `account_switcher/codex_proxy.py` + `codex_config.py`: the Codex router and the config lines that point Codex at it.
- `account_switcher/claude_router.py` + `window.py` + `profiles.py`: separate accounts per window: the Claude Code router (a window's requests with its account's login), the `claude` wrapper and its install, and the windows from before 1.4. The app's side (which window has which account, `/swapaccount`, a window's limits) is in `live.py`; `tests/test_windows_sim.py` runs it end to end with fake Claude Code windows (`tests/fake_claude.py`).
- `account_switcher/claude_hooks.py`, `afk_hook.py`, `statusline.py`: the Claude Code hook (Auto resume) and status line.
- `mods/limitswitcher` + `mods/jev-compact` + `.claude-plugin/marketplace.json` + `account_switcher/mod.py`: the optional Claude Code mod (live usage; `/limits` and `/swapaccount`, answered by `limits_text` and `swap_account` in `live.py`; the Jev compaction before a swapped session goes on), and the app's install/status of it. The swap side of the compaction is `claude_limit` in `live.py` (the hook's answer waits for it). Each plugin has its own tests: `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude plugin test mods/<name>`; `mods/jev-compact/bench/bench.ts` measures the compaction on saved transcripts.
- `account_switcher/integrations.py`: sets all of that up while the app runs and undoes it on quit.
- `account_switcher/web.py`: the controller and the local API (status line, hook, the macOS panel, a second launch).
- `account_switcher/core.py` + `demo.py`: the account model and routing; sample accounts for `--demo`.
- `account_switcher/vault.py`, `keychain.py`: encrypted storage (DPAPI on Windows, the Keychain on macOS).
- `account_switcher/macos_app.py` + `static/menu.*`: the macOS menu bar app and its panel.
- `account_switcher/version.py` + `updates.py`: the version, and update checks / installs from GitHub Releases.
- `scripts/installer.py` (+ `Install.cmd` / `Install.command`): install and update from source.
- `scripts/build_windows.ps1` + `LimitSwitcher.iss`, `win_launcher.c`: the Windows installer (the app, its own Python and `LimitSwitcher.exe`).
- `scripts/build_mac.py` + `mac_launcher.c`: the macOS disk images (LimitSwitcher.app with its own Python); `scripts/install-mac.sh`: the README's Mac install command, also run by the app's updater (it installs the latest release's DMG, or `--dmg <file>`).
- `scripts/make_icons.py`: draws the app icon. `scripts/make_media.py` draws the README's screenshots and GIF (the **Media** workflow runs it on Windows).
- `docs/`: the user and design docs the README links to; `docs/media/`: the screenshots and GIFs (`demo.gif`, `settings.gif`).
- `scripts/mac_memory.py`: where the Mac menu bar app's memory goes (see Measuring performance).
- `scripts/compare_native.py`: the Mac full view drawn natively vs the Pillow drawing (images, memory, timing).
- `scripts/render_frames.py`: frame hashes of the full view (Pillow, as Windows draws it) through a fixed script of states: run before and after a change to the drawing code to prove Windows pixels are unchanged.
- The full view's own process logs its memory at each step to app.log ("full view memory: ...").
