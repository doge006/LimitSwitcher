The app offers it in Settings → **Update to 1.3.3** (or download below).

## Lighter on the network

- **Usage checks reuse their connection.** Each check used to set up a new secure connection to Anthropic or OpenAI first. Now the connection stays open between checks, and when the service has closed it in the meantime, the next one picks up where the last left off. In a local test, a check went from 3.3 KB to 0.85 KB, and real servers send larger certificates, so the saving there is bigger.

## Fixes

- **Separate accounts per window:**
  - On Windows without Developer Mode, `settings.json` and the prompt history are now shared through hard links. Before, each window got a copy of its own.
  - A window's own account now also shares editor connections (`/ide`), `/rewind` checkpoints, plans and `keybindings.json` with your other windows.
  - What you change in a window's settings is kept when the window closes: a folder you trusted, an MCP server you added, a `/config` choice. Before, it went with the window's folder. If the same setting was also changed elsewhere in the meantime, the other change wins.
- **macOS:** quitting right after opening the full view no longer leaves the full view open.
- **Closing the app while it is still starting** (an update right after opening it) now closes it once it is up. Before, it kept running without its dashboard, and on macOS the update could end with no copy running.
- **macOS:** the app is 26 MB smaller. It no longer ships PyObjC's test suite or pip, which it never used.
- **Windows:** a login written within about 15 ms of the previous one (a switch, then Claude Code renewing it) is no longer missed. Windows keeps file times only that precisely, so the app now also compares the file's contents. Found by the first run of the per-window tests on Windows.
- **`app.log` is capped at 1 MB**, with one older copy kept.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
