The app offers it in Settings → **Update to 1.4.21** (or download below).

## An ended Claude subscription shows as the Free plan

When a Claude subscription ends, Anthropic's usage service refuses the account's login with "OAuth authentication is currently not allowed for this organization". LimitSwitcher read that as an expired login: it kept the account's last numbers on screen (often 100% left), showed "Login expired", and renewed the login on every check, which could never help.

The account now shows **No subscription · Free plan** with no usage numbers, and its login is no longer renewed. Auto swap skips it. It is still checked every few minutes, so its numbers come back on their own once the subscription is renewed.

Also fixed: a saved login that was renewed and then still refused was renewed again on every check, instead of waiting for a new sign-in.

## Validation

- **Tests:** two new regression tests cover the ended subscription and the repeated renewal; both fail without this change. The full suite ran 431 tests with 11 skips; the one error, in a mod process test, occurs identically without these changes.
- **On a Windows 11 desktop** with an account whose subscription had ended: after the update, the account made one usage call, renewed nothing, and showed the Free plan with no numbers.

## Download

| | |
|---|---|
| **Windows** | `LimitSwitcher-Setup.exe` |
