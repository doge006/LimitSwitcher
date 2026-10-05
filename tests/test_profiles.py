"""Separate accounts per window (profiles.py, window.py): each Claude Code window with a login of its own."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
from account_switcher import live as live_module, profiles, window as wrapper
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
        codex_login(self.home, "acct-x", "x@example.com", "at-x", "rt-x")
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
        (self.home / ".claude" / "projects").mkdir()
        (self.home / ".claude" / "projects" / "session.jsonl").write_text("{}")
        self.m = LiveAccounts(lambda kind, value: self.logs.append((kind, value)), self.vault, self.providers)
        self.m.spacing = 0
        for uuid, email in (("uuid-c", "c@example.com"), ("uuid-b", "b@example.com"), ("uuid-a", "a@example.com")):
            claude_login(self.home, uuid, email, "at-" + email[0], "rt-" + email[0])
            self.m.sync_live()  # a ends up in use everywhere; b and c saved
        self.api.claude_usage.update({"at-a": claude_usage(10, 10), "at-b": claude_usage(20, 20), "at-c": claude_usage(30, 30)})
        self.m.refresh(force=True)
        self.patches = [mock.patch.object(Path, "home", return_value=self.home),
                        mock.patch.object(profiles, "open_window", lambda d, title="": profiles.set_info(d, pid=os.getpid()))]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.api.close()
        self.tmp.cleanup()

    def account(self, email):
        return next(a for a in self.m.accounts() if a.email == email)

    def window(self, email):
        return next(w for w in self.m.windows() if w["accountId"] == self.account(email).id)

    def main_login(self):
        return self.providers["claude"].read_live().email

    def test_a_window_gets_its_own_login_and_shares_the_rest(self):
        directory = self.m.open_profile(self.account("b@example.com").id)
        self.assertEqual(Claude(config_dir=directory, keychain=False).read_live().email, "b@example.com")
        self.assertEqual(self.main_login(), "a@example.com")  # every other window: unchanged
        self.assertTrue((directory / "settings.json").is_symlink())
        self.assertEqual(json.loads((directory / "settings.json").read_text()), {"model": "opus"})
        self.assertTrue((directory / "projects" / "session.jsonl").exists())  # /resume sees every window's sessions
        config = json.loads((directory / ".claude.json").read_text())
        self.assertEqual(config["theme"], "dark")  # the user's own setup comes along
        self.assertEqual(config["oauthAccount"]["emailAddress"], "b@example.com")
        b = self.account("b@example.com")
        self.assertTrue(b.pinned)
        self.assertEqual((b.status, b.window), ("In window 1", 1))
        self.assertEqual([(w["number"], w["accountId"]) for w in self.m.windows()], [(1, b.id)])

    def test_an_account_is_in_one_place_at_a_time(self):
        with self.assertRaises(RuntimeError):
            self.m.open_profile(self.account("a@example.com").id)  # the main login
        self.m.open_profile(self.account("b@example.com").id)
        with self.assertRaises(RuntimeError):
            self.m.open_profile(self.account("b@example.com").id)  # already in a window
        with self.assertRaises(RuntimeError):
            self.m.swap(self.account("b@example.com").id)  # the other windows can't take it either
        self.assertEqual(self.main_login(), "a@example.com")
        best = self.m._best_other("claude", self.account("a@example.com").id)
        self.assertEqual(best.email, "c@example.com")  # Auto swap skips it

    def test_the_window_owns_its_tokens(self):
        directory = self.m.open_profile(self.account("b@example.com").id)
        b = self.account("b@example.com").id
        own = Claude(config_dir=directory, keychain=False)
        secret = own.read_live().secret
        secret["credentials"]["claudeAiOauth"].update(accessToken="at-b2", refreshToken="rt-b2", expiresAt=int(time.time() * 1000) - 1)
        own.write_live(secret)  # Claude Code renewed it in the window
        self.m.sync_live()
        self.assertEqual(self.vault.read_secret(b)["credentials"]["claudeAiOauth"]["refreshToken"], "rt-b2")
        self.api.refreshes.clear()
        self.m.refresh(force=True)  # expired, but the app never renews it (that would sign the window out)
        self.assertNotIn(("/claude/token", "rt-b2"), self.api.refreshes)

    def test_switching_one_window_leaves_the_rest_alone(self):
        directory = self.m.open_profile(self.account("b@example.com").id)
        window = self.window("b@example.com")["id"]
        with self.assertRaises(RuntimeError):
            self.m.swap_window(window, self.account("a@example.com").id)  # the main login's account
        own = Claude(config_dir=directory, keychain=False)
        secret = own.read_live().secret
        secret["credentials"]["claudeAiOauth"]["refreshToken"] = "rt-b3"
        own.write_live(secret)
        self.m.swap_window(window, self.account("c@example.com").id)
        self.assertEqual(own.read_live().email, "c@example.com")
        self.assertEqual(self.main_login(), "a@example.com")
        b = self.account("b@example.com")
        self.assertFalse(b.pinned)  # free again, with its newest tokens
        self.assertEqual(self.vault.read_secret(b.id)["credentials"]["claudeAiOauth"]["refreshToken"], "rt-b3")
        self.assertTrue(self.account("c@example.com").pinned)
        self.assertEqual(self.window("c@example.com")["id"], window)

    def test_a_limit_in_a_window_moves_that_window_only(self):
        from account_switcher.web import Controller
        directory = self.m.open_profile(self.account("b@example.com").id)
        line = self.m.statusline({"five_hour": {"used_percentage": 64.0, "resets_at": time.time() + 3600}},
                                 "win-1", model="Opus", config_dir=str(directory))
        self.assertIn("b@example.com", str(line))  # the window's status line shows its own account
        b = self.account("b@example.com").id
        self.assertEqual(self.m.meta["accounts"][b]["usage"][0]["used"], 64.0)
        self.assertEqual(self.window("b@example.com")["model"], "Opus")
        self.assertEqual(self.m.profile_account(None, "win-1"), b)  # the mod's reports, which carry no folder
        self.m.meta["afk"] = True
        self.api.claude_usage["at-b"] = claude_usage(100, 50)
        controller = Controller.__new__(Controller)
        controller.live, controller.gateway, controller.notify = True, mock.Mock(manager=self.m), lambda *_: None
        answer = controller.afk_limit({"provider": "claude", "session": "win-1", "configDir": str(directory)})
        self.assertEqual(answer["action"], "continue")
        self.assertEqual(Claude(config_dir=directory, keychain=False).read_live().email, "c@example.com")
        self.assertEqual(self.main_login(), "a@example.com")

    def test_the_wrapper_gets_a_free_account_when_the_setting_is_on(self):
        self.assertIsNone(self.m.allocate_window(os.getpid(), "/work/app"))  # setting off
        self.m.meta["perWindow"] = True
        directory = self.m.allocate_window(os.getpid(), "/work/app")
        self.assertIn(Claude(config_dir=directory, keychain=False).read_live().email, ("b@example.com", "c@example.com"))
        self.assertEqual(self.m.windows()[0]["cwd"], "/work/app")
        self.assertIsNotNone(self.m.allocate_window(os.getpid(), "/work/two"))
        self.assertIsNone(self.m.allocate_window(os.getpid(), "/work/three"))  # nothing free: shares the main login

    def test_a_closed_windows_profile_goes_and_frees_its_account(self):
        directory = self.m.open_profile(self.account("b@example.com").id)
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        profiles.set_info(directory, pid=ended.pid, started=time.time() - live_module.WINDOW_GRACE - 1)
        self.m.sync_live()
        self.assertFalse(directory.exists())
        self.assertTrue((self.home / ".claude" / "projects" / "session.jsonl").exists())  # links removed, never what they point at
        self.assertFalse(self.account("b@example.com").pinned)
        self.assertEqual(self.m.windows(), [])


class WrapperTests(unittest.TestCase):
    def test_interactive_windows_only(self):
        self.assertTrue(wrapper.interactive([]))
        self.assertTrue(wrapper.interactive(["--continue"]))
        for args in (["auth", "login"], ["-p", "hi"], ["--print=x"], ["--version"], ["mcp", "list"]):
            self.assertFalse(wrapper.interactive(args), args)

    @unittest.skipIf(sys.platform == "win32", "the POSIX wrapper")
    def test_a_new_window_starts_in_the_profile_the_app_gives(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            profile = tmp / "profiles" / "window-1"
            profile.mkdir(parents=True)
            asked = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    asked.append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                    raw = json.dumps({"configDir": str(profile)}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                state = tmp / "afk-hook.json"
                state.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/api/afk", "token": "t"}))
                real = tmp / "real"
                real.mkdir()
                (real / "claude").write_text('#!/bin/sh\necho "dir=$CLAUDE_CONFIG_DIR args=$*"\n')
                (real / "claude").chmod(0o755)
                folder = profiles.install_wrapper(tmp, sys.executable, state)
                env = dict(os.environ, PATH=f"{folder}{os.pathsep}{real}{os.pathsep}{os.environ['PATH']}")
                env.pop("CLAUDE_CONFIG_DIR", None)
                out = subprocess.run(["claude", "--continue"], env=env, capture_output=True, text=True, timeout=30).stdout
                self.assertEqual(out.strip(), f"dir={profile} args=--continue")
                self.assertEqual(asked[0][:2], ("/api/window", "Bearer t"))
                out = subprocess.run(["claude", "auth", "status"], env=env, capture_output=True, text=True, timeout=30).stdout
                self.assertEqual(out.strip(), "dir= args=auth status")  # not a window: straight through
                self.assertEqual(len(asked), 1)
                server.shutdown()
                server.server_close()
                out = subprocess.run(["claude"], env=env, capture_output=True, text=True, timeout=30).stdout
                self.assertEqual(out.strip(), "dir= args=")  # the app isn't running: the main login
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
