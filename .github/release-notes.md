The app offers it in Settings → **Update to 1.4.25** (or download below).

## Fixed: `/jevcompact` reported savings larger than the session

`/jevcompact` could say it saved more tokens than the session held: "saved ~516k" on a session of about 186k. To measure the saving, the mod counted the session's whole history, including everything from before earlier compactions. It now counts only the messages the compaction was given. For that session the real saving was about 107k (186k → 79k).

The same figure is subtracted from the session's size in the status line and when Auto resume decides whether a large session needs your OK. Both now get the correct number.

## Changed: two Jev settings

- **Jev compaction** turns Jev on or off as a whole. With it off, `/jevcompact` answers "Jev compaction is off" and compacts nothing. Before, it ran regardless.
- **Auto Jev compact** (new, under it, on by default) compacts a session on its own after a swap or before a resume. Turn it off to compact only by hand with `/jevcompact`.

Claude Code's mod API can't remove a command once added, so `/jevcompact` still shows in the command list while the setting is off.

## Validation

- **Real session:** the session that reported "saved ~516k" was measured from its transcript. Its live context held about 186k tokens; the transcript held about 1.7M across 10 compactions.
- **Tests:** new regression tests cover a session with earlier compactions, `/jevcompact` with the setting off, Auto Jev compact off for swaps and resumes, and the new Settings row. The mod's 37 tests pass. The full suite ran 443 tests with 11 skips. The one error, in a mod process test, also occurs without these changes.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
