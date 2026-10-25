The app offers it in Settings → **Update to 1.4.22** (or download below).

## Free Claude accounts can't be switched to

Claude Code needs a paid plan, so a Claude account whose subscription has ended is of no use to switch to. Since 1.4.21 such an account shows as the Free plan; now it is also treated as unusable:

- **Switching:** it can't be switched to by hand, by Auto swap, or for a window of its own. If the account in use loses its subscription, Auto swap moves off it.
- **Taskbar panel:** it is left out of the list.
- **Full view:** it is still listed, marked **No subscription**, with no Swap button.

Once the subscription is renewed, the next usage check makes it usable again.

## Validation

- **Tests:** new regression tests check that a Free account isn't eligible, that a switch to it is refused, that it is usable again after resubscribing, and that the taskbar panel leaves it out; they fail without this change. The full suite ran 432 tests with 11 skips; the one error, in a mod process test, occurs identically without these changes.
- **On a Windows 11 desktop** with an account whose subscription had ended: the taskbar panel listed only the two paid Claude accounts, and the full view card showed "No subscription" with a disabled "Free plan" button.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
