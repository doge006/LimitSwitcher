The app offers it in Settings → **Update to 1.4.6** (or download below).

## Jev compaction keeps what the session still needs

Jev's scores for old tool outputs run low, so when a resumed session was compacted, almost every old output was removed whole and replaced by a short note. Some of those outputs were needed again later, and the notes left out many of the names, paths and values they held.

Now an output Jev is unsure about keeps its start and end, with a note naming what the middle held. Notes no longer repeat a name another note already gives, big outputs get longer name lists, and a file that is read again later still lists what its older copy held.

Measured on 12 benchmark sessions with Jev's own answers:

| | 1.4.5 | 1.4.6 |
|---|---|---|
| Outputs needed later that were cut | 36 of 290 | **0** |
| Project details lost | 173 | **18** |
| Context removed | 87% | 85% |

The Claude Code mod updates with the app; open sessions pick it up after `/reload-plugins` or a restart.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
