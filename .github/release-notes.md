The app offers it in Settings → **Update to 1.4.26** (or download below).

## Fixed: the full view didn't open on some Windows 10 PCs

On some Windows 10 PCs, clicking **Full view** did nothing. Windows returned a 64-bit handle for the display, and the code that reads the display's scaling couldn't accept a handle that large. It raised an error (`OverflowError: int too long to convert` in `app.log`) before the window was created. The full view now reads the scaling the same way the tray panel does, which accepts any handle.

## Validation

- **Real PC:** the error and traceback came from the `app.log` of an affected Windows 10 PC running 1.4.25.
- **Tests:** a new regression test passes in a 64-bit display handle. It fails on 1.4.25 with the same `OverflowError` and passes now. The full suite ran 445 tests with 11 skips. The one error, in a mod process test, also occurs without this change.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
