The app offers it in Settings → **Update to 1.4.24** (or download below).

## Fixed: sessions waiting at a usage limit didn't continue after a switch

In 1.4.23, a session waiting at a usage limit was meant to continue as soon as LimitSwitcher switched to an account with room. It often didn't. Claude Code now lets the turn that hits the limit keep running for a few more seconds, and the messages that turn wrote made LimitSwitcher believe the session had already continued without it. So it didn't wake the session.

Now only a new prompt counts as the session having continued: one you type, Claude Code's own continue at the reset, or a notification. The rest of the turn that hit the limit is ignored.

## Validation

- **Real sessions:** both sessions that failed to continue on 2026-11-05 were replayed from their transcripts, at the moment their wait began and at the moment the app said continue. The 1.4.23 hook stays quiet in both, and this one wakes both.
- **Tests:** a regression test covers the turn that hit the limit, Claude Code's own continue, and a typed prompt. The full suite ran 442 tests with 11 skips. The one error, in a mod process test, also occurs without these changes.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
