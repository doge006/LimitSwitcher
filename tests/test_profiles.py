"""Per-window accounts (profiles.py): a Claude Code window that keeps its own account."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
from account_switcher import profiles
from account_switcher.live import LiveAccounts
from account_switcher.providers import Claude, Codex
from account_switcher.vault import Vault
from test_live import FakeAPI, claude_login, claude_usage, codex_login, point_at_fake


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.api = FakeAPI()
        claude, codex = Claude(home=self.home, keychain=False), Codex(home=self.home)
        point_at_fake(claude, codex, self.api)
        self.providers = {"claude": claude, "codex": codex}
        self.vault = Vault(root / "store")
        self.logs = []
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b")
        codex_login(self.home, "acct-x", "x@example.com", "at-x", "rt-x")
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
        (self.home / ".claude" / "projects").mkdir()
        (self.home / ".claude" / "projects" / "session.jsonl").write_text("{}")
        self.m = LiveAccounts(lambda kind, value: self.logs.append((kind, value)), self.vault, self.providers)
        self.m.spacing = 0
        self.m.sync_live()
        claude_login(self.home, "uuid-a", "a@example.com", "at-a", "rt-a")  # a is now in use everywhere, b saved
        self.m.sync_live()
        self.api.claude_usage.update({"at-a": claude_usage(10, 10), "at-b": claude_usage(20, 20)})
        self.home_patch = mock.patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.opened = []
        self.open_patch = mock.patch.object(profiles, "open_window", lambda d, title="": self.opened.append((d, title)))
        self.open_patch.start()

    def tearDown(self):
        self.open_patch.stop()
        self.home_patch.stop()
        self.api.close()
        self.tmp.cleanup()

    def account(self, email):
        return next(a for a in self.m.accounts() if a.email == email)

    def open_b(self):
        return self.m.open_profile(self.account("b@example.com").id)

    def test_a_window_gets_its_own_login_and_shares_the_rest(self):
        directory = self.open_b()
        self.assertEqual(self.opened, [(directory, "Claude Code · b@example.com")])
        own = Claude(config_dir=directory, keychain=False).read_live()
        self.assertEqual(own.email, "b@example.com")
        self.assertEqual(self.providers["claude"].read_live().email, "a@example.com")  # every other window: unchanged
        self.assertTrue((directory / "settings.json").is_symlink())
        self.assertEqual(json.loads((directory / "settings.json").read_text()), {"model": "opus"})
        self.assertTrue((directory / "projects" / "session.jsonl").exists())  # /resume sees every window's sessions
        config = json.loads((directory / ".claude.json").read_text())
        self.assertEqual(config["theme"], "dark")  # the user's own setup comes along
        self.assertEqual(config["oauthAccount"]["emailAddress"], "b@example.com")
        b = self.account("b@example.com")
        self.assertTrue(b.pinned)
        self.assertEqual(b.status, "In its own window")

    def test_the_account_every_window_uses_cant_also_have_its_own(self):
        with self.assertRaises(RuntimeError):
            self.m.open_profile(self.account("a@example.com").id)
        self.assertEqual(profiles.existing(self.vault.root), {})

    def test_never_switched_to_while_a_window_has_it(self):
        self.open_b()
        b = self.account("b@example.com")
        with self.assertRaises(RuntimeError):
            self.m.swap(b.id)
        self.assertEqual(self.providers["claude"].read_live().email, "a@example.com")
        self.m.refresh(force=True)
        self.assertIsNone(self.m._best_other("claude", self.account("a@example.com").id))  # Auto swap skips it

    def test_the_window_owns_its_tokens(self):
        directory = self.open_b()
        b = self.account("b@example.com").id
        # Claude Code renews in the window: the app takes the new tokens over
        own = Claude(config_dir=directory, keychain=False)
        secret = own.read_live().secret
        secret["credentials"]["claudeAiOauth"].update(accessToken="at-b2", refreshToken="rt-b2", expiresAt=int(time.time() * 1000) - 1)
        own.write_live(secret)
        self.m.sync_live()
        self.assertEqual(self.vault.read_secret(b)["credentials"]["claudeAiOauth"]["refreshToken"], "rt-b2")
        self.m.refresh(force=True)  # expired, but the app never renews it (that would sign the window out)
        self.assertEqual(self.api.refreshes, [])

    def test_the_windows_status_line_and_limits_are_its_own(self):
        from account_switcher.web import Controller
        directory = self.open_b()
        b = self.account("b@example.com").id
        line = self.m.statusline({"five_hour": {"used_percentage": 64.0, "resets_at": time.time() + 3600}},
                                 "win-1", config_dir=str(directory))
        self.assertIn("b@example.com", str(line))
        self.assertEqual(self.m.meta["accounts"][b]["usage"][0]["used"], 64.0)
        self.assertEqual(self.m.profile_account(None, "win-1"), b)  # the mod's reports, which carry no folder
        controller = Controller.__new__(Controller)
        controller.live, controller.gateway, controller.notify = True, mock.Mock(manager=self.m), lambda *_: None
        self.assertEqual(controller.afk_limit({"provider": "claude", "session": "win-2", "configDir": str(directory)}),
                         {"action": "stop"})  # its limit stays in that window: no swap for everyone else

    def test_giving_it_back_keeps_the_newest_login_and_the_shared_files(self):
        directory = self.open_b()
        b = self.account("b@example.com").id
        own = Claude(config_dir=directory, keychain=False)
        secret = own.read_live().secret
        secret["credentials"]["claudeAiOauth"]["refreshToken"] = "rt-b3"
        own.write_live(secret)
        self.assertTrue(self.m.close_profile(b))
        self.assertFalse(directory.exists())
        self.assertTrue((self.home / ".claude" / "settings.json").exists())  # links removed, never what they point at
        self.assertTrue((self.home / ".claude" / "projects" / "session.jsonl").exists())
        self.assertEqual(self.vault.read_secret(b)["credentials"]["claudeAiOauth"]["refreshToken"], "rt-b3")
        self.assertFalse(self.account("b@example.com").pinned)
        self.m.swap(b)  # an ordinary account again
        self.assertEqual(self.providers["claude"].read_live().email, "b@example.com")


if __name__ == "__main__":
    unittest.main()
