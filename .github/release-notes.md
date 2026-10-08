The app offers it in Settings → **Update to 1.4.5** (or download below).

## Fixed: Jev compacted resumes it wasn't asked about

Jev's compaction on resume is meant for the resumes Claude Code warns about: "Resume this conversation?", which says the resume will use a share of your 5-hour limit. It ran on every resumed session over 100k tokens whose cache had expired instead, including the many Claude Code doesn't ask about (it asks only when reloading would use about 5% of the limit or more), and the "💡 Jev compaction is on" note showed every time `/resume` opened.

Now LimitSwitcher looks for the question itself after a session loads. When it shows, the note sits beside it and Jev compacts once you pick **Resume**, as before. When it doesn't, nothing is compacted and no note shows. The note is shorter too: `💡 Jev compaction ready: upon resume, Jev will compact, saving usage`.

## The Claude Code mod now updates with the app

Until now the mod stayed on whatever version Settings → Install put in, so fixes to it (like 1.4.4's "Jev saved" share, which could still read "~13k of 246k tokens (22%)" instead of 5%) never reached an installed copy. Now the first start after an app update updates the installed mod to the same release. Open Claude Code sessions pick it up after `/reload-plugins` or a restart.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
