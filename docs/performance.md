# Performance

LimitSwitcher is built to be barely noticeable:

- **One small process.** Every window is native: no browser, no Electron. The app draws them itself.
- **It sleeps** until an account is due for a usage check or something changes, and uses no CPU in between. Claude accounts are checked about once a minute (the one in use follows Claude Code's own status line instead, with a check every 30 minutes), Codex accounts every 90 seconds in use and every 5 minutes otherwise. The connection to each service stays open between checks, so a check is under 1 KB in all rather than a new secure connection (several KB) every time.
- **Windows exist only while they're open.** The panel, the menus and the full view are created when you open them and freed when you close them, and animation frames are drawn only while something moves.
- **Checking usage doesn't touch your limits:** the usage endpoints it reads don't count against them.

## Measured

Windows 11:

| | Memory (working set) | Private memory | CPU (one core) |
|---|---|---|---|
| In the tray and taskbar, windows closed | 12.8 MB | 35.5 MB | 0.08% |
| Tray panel open (popped out) | 34.0 MB | 39.2 MB | 0.13% |
| Full view open | 35.8 MB | 45.8 MB | 0.16% |

(The working set is what Task Manager shows. It can be lower than the private memory, because pages the app isn't using are handed back to Windows until they're needed again.)

## Measure it on Windows

In PowerShell, with LimitSwitcher running. This samples it over 60 seconds:

```powershell
$before = (Get-Process LimitSwitcher).CPU; Start-Sleep 60; $p = Get-Process LimitSwitcher
'{0:N1} MB memory ({1:N1} MB private), {2:N2}% of one CPU core' -f ($p.WorkingSet64 / 1MB), ($p.PrivateMemorySize64 / 1MB), (($p.CPU - $before) / 60 * 100)
```

Try it idle in the tray, with the panel open, and with the full view open. Task Manager shows it too, as **LimitSwitcher**.

The release workflow runs the snippet from this page, so it stays correct.

For profiling and benchmarking during development (`scripts/measure.py`, `scripts/bench.py`, the frames job), see [DEVELOPMENT.md](../DEVELOPMENT.md#measuring-performance).
