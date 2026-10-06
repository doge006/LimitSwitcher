# LimitSwitcher

[![Tests](https://github.com/doge006/LimitSwitcher/actions/workflows/tests.yml/badge.svg)](https://github.com/doge006/LimitSwitcher/actions/workflows/tests.yml)
[![Latest release](https://img.shields.io/github/v/release/doge006/LimitSwitcher)](https://github.com/doge006/LimitSwitcher/releases/latest)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-informational)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A Windows tray / macOS menu bar app that shows every Claude Code and Codex usage limit at a glance, switches accounts in one click, and can switch automatically when the account in use hits a limit, so a long task keeps going.

![The full view](docs/media/demo.gif)

<p align="center"><img src="docs/media/panel.png" alt="The tray panel" width="420"></p>

**On the taskbar** (Windows): the accounts in use sit right on the taskbar, and step aside for full-screen apps.

![The taskbar view](docs/media/taskbar.png)

## Features

- **Every limit at a glance:** 5-hour, weekly and per-model caps for every Claude and Codex account, with reset times and subscription renewals, in the tray panel, a full view, and right on the Windows taskbar.
- **One-click switching that open sessions pick up:** Claude Code and Codex sessions move to the new account on their next request. Nothing needs restarting.
- **Auto swap and Auto resume:** when an account hits its limit, the app moves to the account whose weekly limit resets first, and the stopped Claude Code session continues by itself. Codex requests are retried on the next account before the session ever sees the error.
- **Separate accounts per window** (preview): each new Claude Code terminal can run on its own account, so two windows can work on two accounts at once.
- **Jev compaction** (optional): before a swapped session continues, old tool outputs it no longer needs are shrunk, so the new account loads less. On a real 512k-token session it cut the conversation by 44% and lost 2 facts Claude needed later, against 353 to 499 for other tools.
- **Live usage in Claude Code's status line,** through an optional Claude Code mod.
- **Lightweight:** native windows, no browser or Electron. About 13 MB in the Windows tray and 47 MB in the macOS menu bar, with no CPU use between checks ([measurements](docs/performance.md)).
- **Checking usage uses none of your quota:** it reads the same read-only endpoints as Claude Code's `/usage` and ChatGPT's usage page.

## Why

I built this for myself because nothing else did all of it. Usage trackers show limits but don't switch accounts; account switchers swap logins but leave you to notice the limit, and open sessions often need a restart. Few cover both Claude Code and Codex, fewer run on Windows, and most are heavy.

## Install

### Install (Windows)

Download **`LimitSwitcher-Setup.exe`** from the [latest release](https://github.com/doge006/LimitSwitcher/releases/latest) and run it. It installs for your Windows user only (no admin rights), asks where to put it (`%LOCALAPPDATA%\Programs\LimitSwitcher` by default), and has boxes for a Start menu entry (on) and a desktop shortcut (off). It brings its own Python, and starts in the tray when you sign in (turn that off in Settings).

Windows may say "Windows protected your PC" the first time, because the installer isn't code-signed: click **More info → Run anyway**.

Your accounts and settings are kept in `%LOCALAPPDATA%\LimitSwitcher`, so updating, reinstalling or uninstalling keeps them. Uninstall from **Settings → Apps**; it closes the app and puts the Codex and Claude Code settings back first.

### Install (macOS)

Paste this into Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/doge006/LimitSwitcher/main/scripts/install-mac.sh | bash
```

It downloads the app (for Apple silicon Macs: M1 or later) from the [latest release](https://github.com/doge006/LimitSwitcher/releases/latest), puts **LimitSwitcher** in Applications and opens it. It brings its own Python, so nothing else is needed. Running the same command again updates it.

Prefer to click? Download `LimitSwitcher-AppleSilicon.dmg` from the latest release and drag the app to Applications. The app isn't notarized by Apple (that needs a paid developer account), so macOS blocks it the first time: open **System Settings → Privacy & Security** and click **Open Anyway**. The Terminal command avoids that, because macOS only checks apps downloaded by a browser.

It lives in the menu bar (no Dock icon) and starts there when you log in. Your accounts and settings are kept in `~/Library/Application Support/LimitSwitcher`.

### Updates

The app checks GitHub Releases at launch and every 2 hours. Settings → **Update to …** downloads and installs the new version and restarts the app.

## How it works

LimitSwitcher is a single Python process. It polls each provider's usage endpoint over long-lived HTTPS connections, keeps every saved login encrypted, and when you switch it writes the chosen login where Claude Code reads it and routes Codex's requests through a local router that adds the chosen account's login. Claude Code's status line, a `StopFailure` hook and the optional mod report back to it through a local API. Everything it changes in Claude Code's and Codex's settings is tagged and put back when it quits.

| Area | Built with |
|---|---|
| App core | Python: account model, usage clients, Codex router (`127.0.0.1`), local API |
| Windows UI | Win32 layered windows through `ctypes`, drawn with Pillow; taskbar view, tray panel, full view |
| macOS UI | AppKit through PyObjC: a WebKit menu bar popover, and a Core Graphics full view in its own process |
| Secrets | Windows DPAPI, the macOS Keychain |
| Claude Code mod | Two TypeScript plugins (`mods/`): live usage, toasts, Jev compaction |
| Packaging | Inno Setup with a small C launcher (Windows); a self-contained, ad-hoc signed `.app` in a DMG (macOS) |
| CI | GitHub Actions: unit and plugin tests on every push; installer, smoke and performance tests on real Windows 10/11 and macOS runners |

More detail:

- **[Using LimitSwitcher](docs/usage.md):** accounts, switching, per-window accounts, Auto swap and Auto resume, the tray and menu bar, Settings.
- **[How it works](docs/how-it-works.md):** architecture, exactly what the app changes in Claude Code's and Codex's config, rate limits, storage and privacy.
- **[Claude Code mod and Jev compaction](docs/claude-code-mod.md):** setup and benchmarks.
- **[Performance](docs/performance.md):** measured memory and CPU, and how to measure it yourself.

## Privacy

Saved logins are encrypted (DPAPI on Windows, the Keychain on macOS) and never leave your machine except to the providers' own usage and token endpoints. The local API listens on `127.0.0.1` only and needs a per-run token. Check the providers' terms for using several subscriptions this way; that's your call.

## Development

```sh
python -m account_switcher.tray          # run the app from source
python -m account_switcher.tray --demo   # sample accounts, no real logins touched
python -m unittest discover -s tests     # unit tests
```

Building the installers, the plugin tests, benchmarks, publishing a release and the code layout are in [DEVELOPMENT.md](DEVELOPMENT.md). Release history is in [CHANGELOG.md](CHANGELOG.md).

## License

MIT: see [LICENSE](LICENSE).

## Credits

Built with [Claude Code](https://claude.com/claude-code).

`account_switcher/providers.py` follows the usage clients of [Codex Vitals](https://github.com/Joowonoil/Codex-Vitals) (MIT; see `THIRD-PARTY-NOTICES.txt`).
