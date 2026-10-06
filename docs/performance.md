# Performance

LimitSwitcher is built to be barely noticeable:

- **One small process.** Every window is native: no browser, no Electron. On Windows the app draws them itself; on macOS the full view is drawn with the system's own graphics, in a second process that exists only while its window is open (its memory goes back to macOS when you close it), and the menu bar panel uses the system's WebKit view, loaded only while it's open.
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

macOS 27 (Retina display):

| | Memory (footprint) |
|---|---|
| In the menu bar, windows closed | 47 MB |
| Menu bar panel open | 61 MB |
| Full view open (its own process, on top of the menu bar app) | 70 MB |

(The footprint is Activity Monitor's Memory column. Opening the full view briefly takes more while its cards fade in, then settles within a few seconds.)

## Measure it on Windows

In PowerShell, with LimitSwitcher running. This samples it over 60 seconds:

```powershell
$before = (Get-Process LimitSwitcher).CPU; Start-Sleep 60; $p = Get-Process LimitSwitcher
'{0:N1} MB memory ({1:N1} MB private), {2:N2}% of one CPU core' -f ($p.WorkingSet64 / 1MB), ($p.PrivateMemorySize64 / 1MB), (($p.CPU - $before) / 60 * 100)
```

Try it idle in the tray, with the panel open, and with the full view open. Task Manager shows it too, as **LimitSwitcher**.

## Measure it on macOS

In Terminal, with LimitSwitcher running. This samples it over 60 seconds:

```sh
pids=$(pgrep -fl 'MacOS/LimitSwitcher' | grep -v -- --full-view | head -1 | cut -d' ' -f1)
cpu() { ps -o time= -p "$pids" | awk -F: '{s=0; for (i=1; i<=NF; i++) s=s*60+$i; print s}'; }
a=$(cpu); sleep 60; b=$(cpu)
footprint -p "$pids" | awk -v a="$a" -v b="$b" '/Footprint:/ {printf "%s %s memory, %.2f%% of one CPU core\n", $(NF-5), $(NF-4), (b-a)/60*100; exit}'
```

The memory is the app's footprint, the same number as Activity Monitor's Memory column (search for **LimitSwitcher**). It leaves out the system libraries every app shares. Try it idle in the menu bar and with the panel open. The full view runs as a second LimitSwitcher process, only while its window is open, and ends when it closes, so its memory goes back to macOS: `pgrep -f 'LimitSwitcher --full-view'` gives its pid while it's open.

The release and macOS workflows run both snippets from this page, so they stay correct.

For profiling and benchmarking during development (`scripts/measure.py`, `scripts/bench.py`, the frames job), see [DEVELOPMENT.md](../DEVELOPMENT.md#measuring-performance).
