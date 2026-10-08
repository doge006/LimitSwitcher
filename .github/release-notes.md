The app offers it in Settings → **Update to 1.4.5** (or download below).

## Fixed: Jev compacted resumes it wasn't asked about

Jev's compaction on resume is meant for the resumes Claude Code warns about: "Resume this conversation?", which says the resume will use a share of your 5-hour limit. It ran on every resumed session over 100k tokens whose cache had expired instead, including the many Claude Code doesn't ask about (it asks only when reloading would use about 5% of the limit or more), and the "💡 Jev compaction is on" note showed every time `/resume` opened.

Now LimitSwitcher looks for the question itself after a session loads. When it shows, the note sits beside it and Jev compacts once you pick **Resume**, as before. When it doesn't, nothing is compacted and no note shows. Updating the app updates its Claude Code mod.

Also in 1.4.4 and later: the "Jev saved" toast's share is of the whole context (an older build could say "~13k of 246k tokens (22%)"; that is 5%).

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
| **macOS** (Apple silicon, M1 or later) | `LimitSwitcher-AppleSilicon.dmg`, or the one-line Terminal install in the [README](https://github.com/doge006/LimitSwitcher#install-macos) |
