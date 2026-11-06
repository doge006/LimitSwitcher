The app offers it in Settings → **Update to 1.4.23** (or download below).

## Fixed: an error box at shutdown

When Windows shut down or signed out, a "python.exe - Application Error (0xc0000142)" box could appear. Windows ends the taskbar blocks' helper process first, and LimitSwitcher started it again while Windows was ending the session, which fails. It is no longer started again once Windows is shutting down.

## Fixed: sessions waiting for a reset didn't continue after a switch

A Claude Code session that met a usage limit while no other account had room waited until that limit reset, which could be hours. It kept waiting even after LimitSwitcher switched to an account with room. Now a waiting session asks again within about 15 seconds of any Claude switch, and continues on the new account.

## Free accounts

- **Upgrading:** a login made while an account was on the Free plan keeps being refused after you buy a plan, even after it is renewed. Free cards now have an **Upgraded? Sign in again** link, which picks up the new plan.
- **"Rate limited · retrying":** Free accounts were checked every 5 minutes, which got them rate limited for an hour at a time, so their cards said "Rate limited · retrying" instead of "Free plan". They are now checked hourly and keep showing **No subscription · Free plan**.
- **Renewal dates:** an account that moved to the Free plan kept its old renewal date, including one set by hand. Moving onto or off the Free plan now drops it, and a new subscription's date is looked up again.

## Removing accounts

Every account can be removed, including the one in use. Claude Code or Codex keeps using that login until your next switch, and LimitSwitcher doesn't add it back unless that login changes, for example when you sign in again. An account a separate window is using as its own still has to be given back first.

## Smoother animation

Animation frames are now timed to each display refresh instead of Windows' 15.6 ms timer, which fired unevenly and out of step with the screen. On a 144 Hz display the full view's frames came 7.9 ms apart (median) instead of 14.6 ms.

## Validation

- **Tests:** new tests cover each change above. The renewal-date test fails without the fix. The full suite ran 442 tests with 11 skips. The one error, in a mod process test, also occurs without these changes.
- **On a Windows 11 desktop:**
  - Windows' System log showed the shutdown error box about a second after LimitSwitcher logged "Windows is ending the session", and it started the day after the taskbar helper was added.
  - Frame timing was measured on the full view with a 144 Hz display.
  - A real shutdown and a live switch waking a waiting session haven't been observed yet.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
