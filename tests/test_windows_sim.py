"""Separate accounts per window, end to end: the real app (local API, hook, status line, the `claude`
wrapper) with several fake Claude Code windows (fake_claude.py) on fake accounts, as a person would
use it: windows open, take turns, renew their logins, hit usage limits, get switched, and close.

What must hold throughout:
- a window's switch changes that window's login only (the others, and the main login, stay);
- an account is in one place at a time: never handed to a second window or to the main login;
- the app never renews a login a window (or the main login) is using;
- Auto swap moves only the window that hit the limit, to a free account, or leaves it when none is.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
from account_switcher import live as live_module, profiles
from account_switcher.integrations import Integrations
from account_switcher.live import LiveGateway
from account_switcher.providers import Claude, Codex
from account_switcher.vault import Vault
from account_switcher.web import Controller, make_server
from test_live import FakeAPI, claude_login, point_at_fake

HERE = Path(__file__).resolve().parent
EMAILS = ("a", "b", "c", "d")


@unittest.skipIf(sys.platform == "win32", "drives the POSIX wrapper")
class WindowSimulation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.ctl, self.bin = root / "home", root / "ctl", root / "fakebin"
        for folder in (self.home, self.ctl, self.bin):
            folder.mkdir()
        self.log, self.usage_file = root / "log.jsonl", root / "usage.json"
        self.log.touch()
        self.env_patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env_patch.start()
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.grace = mock.patch.object(live_module, "WINDOW_GRACE", 0)
        self.grace.start()
        (self.bin / "claude").write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_claude.py'} \"$@\"\n")
        (self.bin / "claude").chmod(0o755)
        # The person's own status line: the app wraps it (and so is in every window's status line)
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "echo mine"}}))
        self.api = FakeAPI()
        claude, codex = Claude(home=self.home, keychain=False), Codex(home=self.home)
        point_at_fake(claude, codex, self.api)
        self.usage = {}
        for letter in EMAILS:
            self.set_usage("at-" + letter, 10 * EMAILS.index(letter) + 5, 20)
        self.vault = Vault(root / "store")
        self.controller = Controller(gateway=lambda notify: LiveGateway(notify, self.vault, {"claude": claude, "codex": codex}, background=False))
        self.m = self.controller.gateway.manager
        self.m.spacing = 0
        # each account signed in once (the app imports whatever the main login is): a last, so it's the main login
        for letter in reversed(EMAILS):
            claude_login(self.home, "uuid-" + letter, letter + "@example.com", "at-" + letter, "rt-" + letter)
            self.m.sync_live()
        self.m.meta.update(perWindow=True, afk=True, autoSwap=True, startWithWindows=False)
        self.m.refresh(force=True)
        self.server = make_server(self.controller)
        import threading
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.integrations = Integrations(self.controller.gateway, self.server.hook_url, self.server.hook_token,
                                         codex_home=root / "no-codex", claude_root=self.home / ".claude")
        self.controller.gateway.integrations = self.integrations
        self.integrations.start()
        self.wrapper = profiles.wrapper_dir(self.vault.root)
        self.windows = {}
        self.api.refreshes.clear()

    def tearDown(self):
        for name, (process, _) in self.windows.items():
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stderr.close()
        self.integrations.stop()
        self.server.shutdown()
        self.server.server_close()
        self.controller.close()
        self.api.close()
        self.grace.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    # ---------- the fake world ----------
    def set_usage(self, token, five, week=20):
        self.usage[token] = [five, week]
        reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        self.api.claude_usage[token] = {"five_hour": {"utilization": five, "resets_at": reset},
                                        "seven_day": {"utilization": week, "resets_at": reset}}
        self.usage_file.write_text(json.dumps(self.usage))

    def sync_usage_file(self):
        """Pick up tokens the windows renewed (fake_claude copies their numbers to the new token)."""
        self.usage = json.loads(self.usage_file.read_text())
        for token, (five, week) in self.usage.items():
            if token not in self.api.claude_usage:
                self.set_usage(token, five, week)

    def entries(self, window=None, step=None):
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        return [r for r in rows if (window is None or r["window"] == window) and (step is None or r["step"] == step)]

    def wait_for(self, check, what, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            value = check()
            if value:
                return value
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}; log:\n" + self.log.read_text()[-3000:])

    def open_window(self, name, wait=True):
        """A new terminal running `claude` (through the wrapper, which is first on PATH)."""
        env = dict(os.environ, PATH=f"{self.wrapper}{os.pathsep}{self.bin}{os.pathsep}{os.environ['PATH']}",
                   FAKE_NAME=name, FAKE_LOG=str(self.log), FAKE_USAGE=str(self.usage_file), FAKE_CTL=str(self.ctl), FAKE_STEPS="control")
        process = subprocess.Popen(["claude"], env=env, cwd=self.home, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.windows[name] = (process, [0])
        if not wait:
            return None
        start = self.wait_for(lambda: self.entries(name, "start"), f"{name} to start")[0]
        return start["configDir"]

    def do(self, name, step):
        """The window does one step; returns its log entry."""
        process, count = self.windows[name]
        count[0] += 1
        before = len(self.entries(name))
        path = self.ctl / f"{name}.{count[0]}"
        path.with_suffix(".tmp").write_text(step)
        os.replace(path.with_suffix(".tmp"), path)
        if step == "exit":
            process.wait(timeout=30)
            return None
        return self.wait_for(lambda: self.entries(name)[before:] and self.entries(name)[-1], f"{name}: {step}", timeout=60)

    # ---------- what the app thinks ----------
    def login_in(self, config_dir):
        claude = Claude(config_dir=config_dir, keychain=False) if config_dir else Claude(home=self.home, keychain=False)
        login = claude.read_live()
        return login.email if login else None

    def where(self):
        """{email: where it's in use} from the login files themselves (the truth, not the app's view)."""
        places = {}
        main = self.login_in(None)
        places.setdefault(main, []).append("main")
        for name, directory in profiles.existing(self.vault.root).items():
            places.setdefault(self.login_in(directory), []).append(name)
        return places

    def assert_one_place_each(self):
        places = self.where()
        for email, spots in places.items():
            self.assertEqual(len(spots), 1, f"{email} is in use in {spots}")
        return places

    def assert_no_renewals_of_logins_in_use(self):
        in_use = set()
        for directory in [None] + list(profiles.existing(self.vault.root).values()):
            claude = Claude(config_dir=directory, keychain=False) if directory else Claude(home=self.home, keychain=False)
            login = claude.read_live()
            if login:
                in_use.add(login.secret["credentials"]["claudeAiOauth"]["refreshToken"])
        renewed = {token for _, token in self.api.refreshes}
        self.assertFalse(renewed & in_use, f"the app renewed a login in use: {renewed & in_use}")

    # ---------- the scenario ----------
    def test_windows_with_accounts_of_their_own(self):
        m = self.m
        # 1. Three windows open: each gets a free account of its own; a fourth shares the main login.
        dirs = {name: self.open_window(name) for name in ("W1", "W2", "W3")}
        self.assertTrue(all(dirs.values()), dirs)
        dirs["W4"] = self.open_window("W4")
        self.assertIsNone(dirs["W4"])  # nothing free: plain `claude`, the main login
        emails = {name: self.do(name, "turn")["email"] for name in ("W1", "W2", "W3", "W4")}
        self.assertEqual(emails["W4"], "a@example.com")
        self.assertEqual(sorted(emails[n] for n in ("W1", "W2", "W3")), ["b@example.com", "c@example.com", "d@example.com"])
        self.assert_one_place_each()
        self.assertEqual(len(m.windows()), 3)
        self.assertEqual({w["accountId"] for w in m.windows()}, {self.m.find("claude", "uuid-" + emails[n][0]) for n in ("W1", "W2", "W3")})
        # their status lines: the user's own line still shows, with each window's own account
        for name in ("W1", "W2", "W3"):
            line = self.entries(name, "turn")[-1]["line"]
            self.assertIn("mine", line)
        # each window's numbers count for its own account
        for name in ("W1", "W2", "W3", "W4"):
            account = m.find("claude", "uuid-" + emails[name][0])
            five = self.usage["at-" + emails[name][0]][0]
            self.assertEqual(m.meta["accounts"][account]["usage"][0]["used"], float(five), name)

        # 2. W1 hits its limit while no account is free: it stays (no account taken from anywhere).
        w1 = emails["W1"]
        self.set_usage("at-" + w1[0], 100)
        entry = self.do("W1", "turn")
        self.assertEqual((entry["step"], entry["hook"]), ("limit", [0]))  # stop: nothing to move to
        self.assertEqual(self.do("W2", "turn")["email"], emails["W2"])
        self.assertEqual(self.login_in(dirs["W1"]), w1)
        self.assertEqual(self.login_in(None), "a@example.com")
        self.assert_one_place_each()

        # 3. W3 closes: its account is free again (its profile goes, its login is kept).
        self.do("W3", "exit")
        m.sync_live()
        self.assertNotIn(dirs["W3"], [str(d) for d in profiles.existing(self.vault.root).values()])
        freed = emails["W3"]
        self.assertIn(freed, [a.email for a in m.free_accounts()])

        # 4. W1 hits its limit again: Auto swap moves W1, and only W1, to the freed account; Auto resume continues it.
        entry = self.do("W1", "turn")
        self.assertEqual((entry["step"], entry["hook"]), ("limit", [2]), entry)
        self.assertEqual(self.do("W1", "turn")["email"], freed)  # its next request: the new account
        self.assertEqual(self.do("W2", "turn")["email"], emails["W2"])
        self.assertEqual(self.do("W4", "turn")["email"], "a@example.com")
        self.assert_one_place_each()
        self.assertFalse(m.meta["accounts"][m.find("claude", "uuid-" + w1[0])].get("status", "").startswith("In window"))

        # 5. W2 renews its login (Claude Code does, on its own): the app keeps the new tokens and renews nothing.
        renewed = self.do("W2", "renew")
        self.sync_usage_file()
        m.sync_live()
        w2_account = m.find("claude", "uuid-" + emails["W2"][0])
        self.assertEqual(self.vault.read_secret(w2_account)["credentials"]["claudeAiOauth"]["refreshToken"], renewed["refresh"])
        self.assertEqual(self.do("W2", "turn")["token"], renewed["token"])
        m.refresh(force=True)
        self.assert_no_renewals_of_logins_in_use()

        # 6. The main login hits its limit: the only free account (W1's old one) is used up, so nothing
        #    moves; and no window's account is ever taken for the main login.
        self.set_usage("at-a", 100)
        m.meta["afk"] = False  # with Auto resume on, the main login's hook would wait for the reset here (as it should)
        entry = self.do("W4", "turn")
        self.assertEqual(entry["step"], "limit")
        self.assertEqual(self.login_in(None), "a@example.com")
        self.assert_one_place_each()
        # W1's old account resets: now the main login moves to it, and the windows stay where they are.
        self.set_usage("at-" + w1[0], 5)
        m.refresh(force=True)
        m.meta["afk"] = True
        entry = self.do("W4", "turn")
        self.assertEqual((entry["step"], entry["hook"]), ("limit", [2]), entry)
        self.assertEqual(self.do("W4", "turn")["email"], w1)
        self.assertEqual(self.login_in(dirs["W1"]), freed)
        self.assertEqual(self.login_in(dirs["W2"]), emails["W2"])
        self.assert_one_place_each()

        # 7. By hand, from the full view: pick W2 (pointed out on screen), then "Use in window" with a
        #    free account. No free one: refused, nothing changes.
        w2 = next(w for w in m.windows() if w["accountId"] == w2_account)["id"]
        for taken in (freed, w1):  # W1's account, the main login's: never a second place
            with self.assertRaises(RuntimeError):
                m.swap_window(w2, m.find("claude", "uuid-" + taken[0]))
        # a was given up by the main login in step 6, so it's free: it may go to W2
        self.set_usage("at-a", 10)
        m.refresh(force=True)
        self.controller.action("highlightWindow", {"window": w2})
        self.controller.action("swapWindow", {"window": w2, "id": m.find("claude", "uuid-a")})
        self.wait_for(lambda: self.login_in(dirs["W2"]) == "a@example.com", "W2 to move to a")
        self.assertEqual(self.do("W2", "turn")["email"], "a@example.com")
        self.assertEqual(self.do("W1", "turn")["email"], freed)
        self.assertEqual(self.do("W4", "turn")["email"], w1)
        self.assert_one_place_each()
        self.assert_no_renewals_of_logins_in_use()

        # 8. Everything closes: the profiles go, every account is free, the newest tokens are kept.
        for name in ("W1", "W2", "W4"):
            self.do(name, "exit")
        m.sync_live()
        self.assertEqual(profiles.existing(self.vault.root), {})
        self.assertEqual(m.windows(), [])
        self.assertEqual(self.vault.read_secret(w2_account)["credentials"]["claudeAiOauth"]["refreshToken"], renewed["refresh"])
        for name, (process, _) in self.windows.items():
            self.assertEqual(process.returncode, 0, process.stderr.read().decode()[-2000:])


    def test_windows_opened_at_once_never_share_an_account(self):
        self.m.meta["accounts"][self.m.find("claude", "uuid-d")]["status"] = "Login expired; sign in again"  # 2 free: b, c
        for name in ("W1", "W2", "W3", "W4", "W5"):
            self.open_window(name, wait=False)  # all at the same moment
        starts = {name: self.wait_for(lambda n=name: self.entries(n, "start"), f"{name} to start")[0]["configDir"]
                  for name in ("W1", "W2", "W3", "W4", "W5")}
        own = [d for d in starts.values() if d]
        self.assertEqual(len(own), 2, starts)
        emails = sorted(self.login_in(d) for d in own)
        self.assertEqual(emails, ["b@example.com", "c@example.com"])
        for name in starts:
            self.assertIn(self.do(name, "turn")["email"], ("a@example.com", "b@example.com", "c@example.com"))
        self.assert_one_place_each()


if __name__ == "__main__":
    unittest.main()
