"""Separate accounts per window (live.py's windows, claude_router.py, window.py, profiles.py): every
window an ordinary Claude Code window, each on the account the app gives it."""
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


class WindowTests(unittest.TestCase):
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
        self.m = LiveAccounts(lambda kind, value: self.logs.append((kind, value)), self.vault, self.providers)
        self.m.spacing = 0
        for uuid, email in (("uuid-c", "c@example.com"), ("uuid-b", "b@example.com"), ("uuid-a", "a@example.com")):
            claude_login(self.home, uuid, email, "at-" + email[0], "rt-" + email[0])
            self.m.sync_live()  # a ends up in use everywhere; b and c saved
        self.api.claude_usage.update({"at-a": claude_usage(10, 10), "at-b": claude_usage(20, 20), "at-c": claude_usage(30, 30)})
        self.m.refresh(force=True)
        self.m.router_url = lambda window_id: f"http://127.0.0.1:1/secret/{window_id}"
        self.opened = []
        self.patches = [mock.patch.object(Path, "home", return_value=self.home),
                        mock.patch.object(profiles, "open_window", lambda env, folder, window_id, title="":
                                          self.opened.append(env) or {"pid": os.getpid()})]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.api.close()
        self.tmp.cleanup()

    def account(self, email):
        return next(a for a in self.m.accounts() if a.email == email)

    def window(self, window_id):
        return next(w for w in self.m.windows() if w["id"] == window_id)

    def main_login(self):
        return self.providers["claude"].read_live().email

    def new_window(self, pid=None):
        self.m.meta["perWindow"] = True
        return self.m.allocate_window(pid or os.getpid(), "/work/app")["window"]

    def test_a_new_window_gets_a_free_account_and_the_router(self):
        self.assertIsNone(self.m.allocate_window(os.getpid(), "/work/app"))  # setting off: a plain window
        self.m.meta["perWindow"] = True
        first = self.m.allocate_window(os.getpid(), "/work/app")
        self.assertEqual(first["baseUrl"], "http://127.0.0.1:1/secret/" + first["window"])
        second = self.m.allocate_window(os.getpid(), "/work/two")["window"]
        third = self.m.allocate_window(os.getpid(), "/work/three")["window"]  # nothing free: the main account, routed
        accounts = [self.window(i)["accountId"] for i in (first["window"], second, third)]
        self.assertEqual(sorted(filter(None, accounts)), sorted([self.account("b@example.com").id, self.account("c@example.com").id]))
        self.assertIsNone(accounts[2])
        self.assertEqual([w["number"] for w in self.m.windows()], [1, 2, 3])
        self.assertEqual(self.window(first["window"])["cwd"], "/work/app")
        self.assertEqual(self.main_login(), "a@example.com")  # nothing written anywhere
        self.assertFalse((self.vault.root / "profiles").exists())
        self.m.router_url = None
        self.assertIsNone(self.m.allocate_window(os.getpid(), "/work/four"))  # the router isn't running: plain

    def test_the_router_asks_which_account_a_window_has(self):
        window = self.new_window()
        b = self.account("b@example.com").id
        self.assertEqual(self.m.window_route(window), (b, None))
        self.assertEqual(self.m.window_token(b), "at-b")
        self.assertEqual(self.m.window_route("unknown"), (None, None))  # the session's own login
        self.m.meta["perWindow"] = True
        chained = self.m.allocate_window(os.getpid(), "/w", "https://gateway.example/anthropic")["window"]
        self.assertEqual(self.m.window_route(chained)[1], "https://gateway.example/anthropic")

    def test_a_windows_login_is_renewed_here_and_the_main_login_never(self):
        window = self.new_window()
        b = self.account("b@example.com").id
        secret = self.vault.read_secret(b)
        secret["credentials"]["claudeAiOauth"]["expiresAt"] = int((time.time() + 60) * 1000)
        self.vault.write_secret(b, secret)
        self.api.refreshes.clear()
        self.assertEqual(self.m.window_token(b), "at-rt-b+")  # about to expire: renewed first
        self.assertEqual(self.api.refreshes, [("/claude/token", "rt-b")])
        self.assertEqual(self.m.window_token(b), "at-rt-b+")  # fresh now: as it is
        self.assertEqual(self.m.window_token(b, renew=True), "at-rt-b++")  # refused by Anthropic: renewed again
        self.m.swap_window(window, self.account("a@example.com").id)  # the main login's account
        self.assertIsNone(self.window(window)["accountId"])  # follows the main account
        a = self.account("a@example.com").id
        self.api.refreshes.clear()
        self.assertEqual(self.m.window_token(a, renew=True), "at-a")  # Claude Code's: read, never renewed here
        self.assertEqual(self.api.refreshes, [])

    def test_a_window_accounts_usage_is_checked_and_renewed_like_any_saved_account(self):
        self.new_window()
        b = self.account("b@example.com").id
        secret = self.vault.read_secret(b)
        secret["credentials"]["claudeAiOauth"]["expiresAt"] = int((time.time() + 60) * 1000)
        self.vault.write_secret(b, secret)
        self.api.claude_usage["at-rt-b+"] = claude_usage(25, 25)
        self.api.refreshes.clear()
        self.m.refresh(force=True)
        self.assertIn(("/claude/token", "rt-b"), self.api.refreshes)  # no Claude Code holds it: the app does
        self.assertNotIn(("/claude/token", "rt-a"), self.api.refreshes)  # the main login: Claude Code's

    def test_switching_one_window_leaves_the_rest_alone(self):
        one, two = self.new_window(), self.new_window()
        b, c = self.account("b@example.com").id, self.account("c@example.com").id
        self.assertEqual({self.window(one)["accountId"], self.window(two)["accountId"]}, {b, c})
        self.m.swap_window(one, c if self.window(one)["accountId"] == b else b)  # an account another window has: fine
        self.assertEqual(self.window(one)["accountId"], self.window(two)["accountId"])
        self.assertEqual(self.main_login(), "a@example.com")
        self.m.swap_window(one, None)
        self.assertIsNone(self.window(one)["accountId"])
        with self.assertRaises(ValueError):
            self.m.swap_window("gone", b)
        with self.assertRaises(ValueError):
            self.m.swap_window(one, self.m.find("codex", "acct-x"))

    def test_the_main_login_can_move_to_a_windows_account(self):
        window = self.new_window()
        b = self.window(window)["accountId"]
        self.m.swap(b)  # the window's requests then go with Claude Code's own (fresh) token
        self.assertEqual(self.main_login(), "b@example.com")
        self.assertEqual(self.m.window_token(b), "at-b")
        self.m.swap(self.account("a@example.com").id)
        self.assertEqual(self.m._best_other("claude", self.account("a@example.com").id).email, "b@example.com")  # any account

    def test_a_window_at_its_limit_skips_the_main_account_while_another_has_room(self):
        from account_switcher.web import Controller
        window = self.new_window()
        self.m.swap_window(window, self.account("b@example.com").id)
        self.api.claude_usage["at-a"] = claude_usage(1, 1)  # the main account has the most room
        self.api.claude_usage["at-b"] = claude_usage(100, 50)
        self.m.refresh(force=True)
        controller = Controller.__new__(Controller)
        controller.live, controller.gateway, controller.notify = True, mock.Mock(manager=self.m), lambda *_: None
        controller.afk_limit({"provider": "claude", "session": "w", "window": window})
        self.assertEqual(self.window(window)["accountId"], self.account("c@example.com").id)  # it stays apart
        self.api.claude_usage["at-c"] = claude_usage(100, 50)  # only the main account has room left: then that
        self.m.afk_sessions.clear()  # minutes later: past the loop guard and the "just switched" one
        self.m.auto_moved.clear()
        controller.afk_limit({"provider": "claude", "session": "w", "window": window})
        self.assertIsNone(self.window(window)["accountId"])

    def test_swapaccount_names_an_account_by_email_or_in_name_mode_its_name(self):
        m = self.m
        b, c = self.account("b@example.com").id, self.account("c@example.com").id
        text = m.swap_account(None, "s1", "")["text"]  # nothing given: the accounts to choose from
        self.assertIn("Which account?", text)
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            self.assertIn(email, text)
        self.assertIn("a@example.com · 5h **90%** · 1w **90%** · this window · main", text)
        answer = m.swap_account(None, "s1", "nobody")
        self.assertIn("No Claude account called **nobody**", answer["text"])
        self.assertNotIn("window", answer)
        self.assertEqual(m.windows(), [])  # nothing changed
        answer = m.swap_account(None, "s1", "B@Example.com")  # a window not routed yet: it joins
        self.assertEqual(answer["baseUrl"], "http://127.0.0.1:1/secret/" + answer["window"])
        window = answer["window"]
        self.assertEqual((self.window(window)["accountId"], self.window(window)["how"]), (b, "command"))
        self.assertIn("Window 1 now uses **b@example.com**", answer["text"])
        answer = m.swap_account(window, "s1", "c")  # the part before @: c@example.com
        self.assertNotIn("baseUrl", answer)  # routed already
        self.assertEqual(self.window(window)["accountId"], c)
        self.assertEqual(m.swap_account(None, "s1", "b")["text"].split(" now")[0], "Window 1")  # the session says which window
        self.assertEqual(len(m.windows()), 1)
        m.meta["nameMode"] = True
        m.meta["accounts"][c]["label"] = "Work"
        text = m.swap_account(window, "s1", "")["text"]
        self.assertIn("Work", text)
        self.assertIn("Claude", text)  # unnamed ones as the app shows them ("Claude 1")
        self.assertNotIn("@example.com", text)  # names, never emails
        self.assertIn("`/swapaccount <name or email>`", text)
        self.assertIn("now uses **Work**", m.swap_account(window, "s1", "work")["text"])
        self.assertIn("now uses", m.swap_account(window, "s1", "b@example.com")["text"])  # an email still works
        self.assertEqual(self.window(window)["accountId"], b)
        self.assertIn("main account", m.swap_account(window, "s1", "main")["text"])
        self.assertIsNone(self.window(window)["accountId"])
        m.meta["accounts"][c]["status"] = "Login expired; sign in again"
        self.assertIn("can't take this window", m.swap_account(window, "s1", "Work")["text"])
        self.assertIsNone(self.window(window)["accountId"])
        m.router_url = None
        self.assertIn("router isn't running", m.swap_account(window, "s1", "b")["text"])

    def test_windows_are_kept_across_a_restart(self):
        window = self.new_window()
        account = self.window(window)["accountId"]
        again = LiveAccounts(lambda *_: None, self.vault, self.providers)
        self.assertEqual(again.window_route(window), (account, None))

    def test_a_closed_window_is_forgotten_and_frees_its_account(self):
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        window = self.new_window(pid=ended.pid)
        account = self.window(window)["accountId"]
        self.m.claude_windows[window]["started"] = time.time() - live_module.WINDOW_GRACE - 1
        self.m.sync_windows(force=True)
        self.assertEqual(self.m.windows(), [])
        self.assertIn(account, [a.id for a in self.m.free_accounts()])
        self.assertEqual(json.loads((self.vault.root / "windows.json").read_text()), {})

    def test_a_window_whose_process_isnt_known_goes_after_a_long_idle(self):
        self.m.meta["perWindow"] = True
        window = self.m.allocate_window(None, "/w")["window"]
        self.m.sync_windows(force=True)
        self.assertEqual(len(self.m.windows()), 1)
        self.m.claude_windows[window]["started"] = time.time() - live_module.WINDOW_IDLE - 1
        self.m.window_route(window)  # a request just now: still open
        self.m.sync_windows(force=True)
        self.assertEqual(len(self.m.windows()), 1)
        self.m.window_seen[window] = time.time() - live_module.WINDOW_IDLE - 1
        self.m.sync_windows(force=True)
        self.assertEqual(self.m.windows(), [])

    def test_the_status_line_finds_the_windows_claude_code(self):
        """On Windows the wrapper knows only the cmd.exe running claude.cmd: the status line's parent chain
        says which process is Claude Code."""
        window = self.new_window(pid=500)  # cmd.exe
        tree = {900: (800, "python"), 800: (700, "cmd"), 700: (500, "claude"), 500: (400, "cmd"), 400: (1, "explorer")}
        with mock.patch.object(live_module.processes, "family", return_value=tree):
            self.m.statusline(None, "s1", window=window, parent=800)
        self.assertEqual(self.m.claude_windows[window]["pid"], 700)
        # a Claude Code further up (the one this window was started from) is never taken for it
        other = self.new_window(pid=600)
        tree = {900: (800, "python"), 800: (600, "sh"), 600: (300, "python"), 300: (200, "claude")}
        with mock.patch.object(live_module.processes, "family", return_value=tree):
            self.m.statusline(None, "s2", window=other, parent=800)
        self.assertEqual(self.m.claude_windows[other]["pid"], 600)

    def test_a_window_shows_its_sessions_title_and_where_it_is(self):
        window = self.new_window()
        transcript = Path(self.tmp.name) / "win-1.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "Ship the fix"}}) + "\n")
        self.m.statusline(None, "win-1", model="Opus", window=window, cwd="/work/app", transcript=str(transcript))
        row = self.window(window)
        self.assertEqual((row["title"], row["cwd"], row["model"]), ("Ship the fix", "/work/app", "Opus"))

    def test_a_windows_numbers_count_for_its_own_account(self):
        window = self.new_window()
        b = self.window(window)["accountId"]
        line = self.m.statusline({"five_hour": {"used_percentage": 64.0, "resets_at": time.time() + 3600}}, "win-1",
                                 model="Opus", window=window)
        self.assertIn(self.m.meta["accounts"][b]["email"], str(line))
        self.assertEqual(self.m.meta["accounts"][b]["usage"][0]["used"], 64.0)
        self.assertEqual(self.m.window_account(None, "win-1"), b)  # the mod's reports, remembered by session

    def test_limits_in_a_window_shows_its_own_account_and_where_each_is(self):
        window = self.new_window()
        b = self.account("b@example.com").id
        self.m.swap_window(window, b)
        text = self.m.limits_text("win-1", "Opus 5.5", None, None, window)
        lines = text.split("\n")
        self.assertEqual(lines[0], "**⇄ b@example.com** · Opus 5.5 · window 1")
        self.assertIn("- 🟢 a@example.com · 5h **90%** · 1w **90%** · main", lines)
        self.assertIn("- 🟢 c@example.com · 5h **70%** · 1w **70%**", lines)  # free: in use nowhere
        self.assertTrue(self.m.limits_text("win-1").startswith("**⇄ b@example.com** · window 1\n"))  # remembered
        self.assertTrue(self.m.limits_text("other").startswith("**⇄ a@example.com** · main\n"))

    def test_a_limit_in_a_window_moves_that_window_only(self):
        from account_switcher.web import Controller
        one, two = self.new_window(), self.new_window()
        b = self.account("b@example.com").id
        self.m.swap_window(one, b)
        self.m.swap_window(two, None)
        self.m.meta["afk"] = True
        self.api.claude_usage["at-b"] = claude_usage(100, 50)
        controller = Controller.__new__(Controller)
        controller.live, controller.gateway, controller.notify = True, mock.Mock(manager=self.m), lambda *_: None
        answer = controller.afk_limit({"provider": "claude", "session": "win-1", "window": one})
        self.assertEqual(answer["action"], "continue")
        self.assertEqual(self.window(one)["accountId"], self.account("c@example.com").id)
        self.assertIsNone(self.window(two)["accountId"])
        self.assertEqual(self.main_login(), "a@example.com")

    def test_a_window_switched_at_its_limit_is_compacted_with_jev_first(self):
        from account_switcher.web import Controller
        window = self.new_window()
        self.m.swap_window(window, self.account("b@example.com").id)
        self.m.window_account(window, "win-1")
        self.m.meta.update(afk=True, jevCompact=True, jevModInstalled=True)
        self.m.note_mod_session("win-1")
        self.api.claude_usage["at-b"] = claude_usage(100, 50)
        controller = Controller.__new__(Controller)
        controller.live, controller.gateway, controller.notify = True, mock.Mock(manager=self.m), lambda *_: None
        limit = {"provider": "claude", "session": "win-1", "window": window}
        with mock.patch("account_switcher.mod.jev_key_present", return_value=True):
            self.assertEqual(controller.afk_limit(limit), {"action": "wait", "seconds": 3})
            self.assertEqual(self.window(window)["accountId"], self.account("c@example.com").id)  # switched already
            self.assertEqual(controller.afk_limit(limit), {"action": "wait", "seconds": 3})  # not taken for c's limit
            request = self.m.compaction_request("win-1")
            self.assertTrue(self.m.compaction_done("win-1", request["id"], "done", 90_000))
            self.assertEqual(controller.afk_limit(limit), {"action": "wait", "seconds": 1, "restamp": True})
            answer = controller.afk_limit(limit)
        self.assertEqual(answer["action"], "continue")
        self.assertIn("shortened", answer["message"])
        self.assertEqual(self.window(window)["accountId"], self.account("c@example.com").id)  # no second switch

    def test_after_a_switch_a_windows_old_numbers_never_count_for_its_new_account(self):
        m = self.m
        b, c = self.account("b@example.com").id, self.account("c@example.com").id

        def report(window, five):
            m.statusline({"five_hour": {"used_percentage": five, "resets_at": time.time() + 3600}}, "s-" + window, window=window)

        def used(account):
            return m.meta["accounts"][account]["usage"][0]["used"]

        window = self.new_window()
        m.swap_window(window, b)
        report(window, 64.0)
        report(window, 66.0)
        self.assertEqual(used(b), 66.0)
        m.swap_window(window, c)
        report(window, 66.0)  # its status line, before a reply on c: still b's numbers
        self.assertEqual(used(c), 30.0)
        report(window, 41.0)  # its first reply on c
        self.assertEqual(used(c), 41.0)
        # a window switched by /swapaccount before it ever reported: its first report is the old account's
        fresh = m.swap_account(None, "s-new", "b")["window"]
        report(fresh, 99.0)
        self.assertEqual(used(b), 66.0)
        report(fresh, 70.0)
        self.assertEqual(used(b), 70.0)

    def test_a_dead_login_isnt_tried_again_on_every_request(self):
        window = self.new_window()
        b = self.window(window)["accountId"]
        self.assertEqual(self.m.window_route(window)[0], b)
        self.m.window_login_failed(window, b, "refused")
        self.assertIsNone(self.m.window_route(window)[0])  # the session's own login, without asking again
        self.m.swap_window(window, self.account("c@example.com").id)
        self.m.swap_window(window, b)
        self.assertEqual(self.m.window_route(window)[0], b)  # picked again by hand: tried again
        self.m.window_login_failed(window, b, "refused")
        secret = self.vault.read_secret(b)
        secret["credentials"]["claudeAiOauth"]["refreshToken"] = "rt-b-new"
        from account_switcher.providers import LiveLogin
        self.m.adopt("claude", LiveLogin("uuid-b", "b@example.com", "Max", secret))  # signed in again
        self.assertEqual(self.m.window_route(window)[0], b)

    def test_a_login_that_cant_be_used_is_said_once(self):
        window = self.new_window()
        b = self.window(window)["accountId"]
        self.m.window_login_failed(window, b, "refused")
        self.m.window_login_failed(window, b, "refused")
        said = [v for k, v in self.logs if k == "log" and "has expired" in str(v)]
        self.assertEqual(len(said), 1)
        self.assertTrue(self.m.meta["accounts"][b]["status"].startswith("Login expired"))

    def test_new_window_opens_a_terminal_on_the_router(self):
        b = self.account("b@example.com").id
        window = self.m.open_window(b)
        self.assertEqual(self.opened, [{"ANTHROPIC_BASE_URL": "http://127.0.0.1:1/secret/" + window, "LIMITSWITCHER_WINDOW": window}])
        self.assertEqual((self.window(window)["accountId"], self.window(window)["how"]), (b, "terminal"))
        self.assertEqual(self.m.claude_windows[window]["pid"], os.getpid())

    def test_windows_from_before_the_router_give_their_login_back_and_go(self):
        """1.3.x: a window had a config folder of its own. Its newest tokens come back; once its window
        has closed, the folder goes (its links, never what they point at)."""
        claude_home = self.home / ".claude"
        (claude_home / "projects").mkdir()
        (claude_home / "projects" / "session.jsonl").write_text("{}")
        folder = profiles.root(self.vault.root) / "window-1a2b3c4d"
        folder.mkdir(parents=True)
        os.symlink(claude_home / "projects", folder / "projects")
        own = Claude(config_dir=folder, keychain=False)
        secret = self.vault.read_secret(self.account("b@example.com").id)
        secret["credentials"]["claudeAiOauth"].update(accessToken="at-b9", refreshToken="rt-b9")
        own.write_live(secret)
        ended = subprocess.Popen([sys.executable, "-c", "pass"])
        ended.wait()
        profiles.set_info(folder, pid=ended.pid, started=time.time() - 3600, how="wrapper")
        self.m.sync_windows(force=True)
        self.assertEqual(self.vault.read_secret(self.account("b@example.com").id)["credentials"]["claudeAiOauth"]["refreshToken"], "rt-b9")
        self.assertFalse(folder.exists())
        self.assertTrue((claude_home / "projects" / "session.jsonl").exists())


class SignatureTests(unittest.TestCase):
    def test_a_new_login_is_seen_even_with_the_same_file_time(self):
        """Windows keeps file times to about 15 ms: two logins written close together can share one."""
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            claude = Claude(home=home, keychain=False)
            claude_login(home, "uuid-b", "b@example.com", "at-b", "rt-b")
            before = claude.signature()
            stamps = [os.stat(path).st_mtime_ns for path in (claude.credentials_file, claude.config_file)]
            claude_login(home, "uuid-c", "c@example.com", "at-c", "rt-c")
            for path, stamp in zip((claude.credentials_file, claude.config_file), stamps):
                os.utime(path, ns=(stamp, stamp))
            self.assertNotEqual(claude.signature(), before)


class App:
    """The app's /api/window, as the wrapper asks it."""

    def __init__(self, answer):
        self.asked = []
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                app.asked.append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                raw = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def state(self, folder):
        path = Path(folder) / "afk-hook.json"
        path.write_text(json.dumps({"url": f"http://127.0.0.1:{self.server.server_port}/api/afk", "token": "t"}))
        return path

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class WrapperTests(unittest.TestCase):
    ANSWER = {"window": "1a2b3c4d", "baseUrl": "http://127.0.0.1:47831/s3cret/1a2b3c4d"}

    def test_interactive_windows_only(self):
        self.assertTrue(wrapper.interactive([]))
        self.assertTrue(wrapper.interactive(["--continue"]))
        for args in (["auth", "login"], ["-p", "hi"], ["--print=x"], ["--version"], ["mcp", "list"]):
            self.assertFalse(wrapper.interactive(args), args)

    @unittest.skipIf(sys.platform == "win32", "the POSIX wrapper")
    def test_a_new_window_starts_as_usual_with_the_routers_address(self):
        app = App(self.ANSWER)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            state = app.state(tmp)
            real = tmp / "real"
            real.mkdir()
            (real / "claude").write_text('#!/bin/sh\necho "url=$ANTHROPIC_BASE_URL window=$LIMITSWITCHER_WINDOW dir=$CLAUDE_CONFIG_DIR '
                                         'pid=$$ args=$*"\n')
            (real / "claude").chmod(0o755)
            folder = profiles.install_wrapper(tmp, sys.executable, state)
            env = dict(os.environ, PATH=f"{folder}{os.pathsep}{real}{os.pathsep}{os.environ['PATH']}")
            for name in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "LIMITSWITCHER_WINDOW"):
                env.pop(name, None)
            try:
                out = subprocess.run(["claude", "--continue"], env=env, capture_output=True, text=True, timeout=30).stdout.split()
                self.assertEqual(out[:3], [f"url={self.ANSWER['baseUrl']}", "window=1a2b3c4d", "dir="])  # ~/.claude, as usual
                self.assertEqual(out[4:], ["args=--continue"])
                path, auth, body = app.asked[0]
                self.assertEqual((path, auth), ("/api/window", "Bearer t"))
                self.assertEqual(body["pid"], int(out[3][4:]))  # it became claude: the app watches that process
                self.assertIsNone(body["upstream"])
                out = subprocess.run(["claude", "auth", "status"], env=env, capture_output=True, text=True, timeout=30).stdout
                self.assertTrue(out.startswith("url= window= "))  # not a window: straight through
                self.assertEqual(len(app.asked), 1)
                inner = dict(env, LIMITSWITCHER_WINDOW="1a2b3c4d", ANTHROPIC_BASE_URL=self.ANSWER["baseUrl"])
                subprocess.run(["claude"], env=inner, capture_output=True, text=True, timeout=30)  # claude in a window: the same window
                self.assertEqual(len(app.asked), 1)
                mine = dict(env, ANTHROPIC_BASE_URL="https://gateway.example/anthropic")  # the person's own: the router forwards there
                subprocess.run(["claude"], env=mine, capture_output=True, text=True, timeout=30)
                self.assertEqual(app.asked[-1][2]["upstream"], "https://gateway.example/anthropic")
                app.close()
                out = subprocess.run(["claude"], env=env, capture_output=True, text=True, timeout=30).stdout
                self.assertTrue(out.startswith("url= window= "))  # the app isn't running: a plain window
            finally:
                app.server.server_close()

    def test_windows_wrapper_writes_the_variables_and_leaves_no_process(self):
        """claude.cmd runs window.py --env <file>, `call`s the file, and runs the real claude itself."""
        app = App(self.ANSWER)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "env.cmd"
                env = {k: v for k, v in os.environ.items() if k != "LIMITSWITCHER_WINDOW"}
                done = subprocess.run([sys.executable, wrapper.__file__, str(app.state(tmp)), "--env", str(out)], env=env, timeout=30)
                self.assertEqual(done.returncode, 0)
                self.assertEqual(out.read_text().splitlines(), [f'set "ANTHROPIC_BASE_URL={self.ANSWER["baseUrl"]}"',
                                                                 'set "LIMITSWITCHER_WINDOW=1a2b3c4d"'])
                self.assertEqual(app.asked[0][2]["pid"], os.getpid())  # its parent: the cmd.exe running claude.cmd
        finally:
            app.close()

    def test_the_windows_wrapper_script(self):
        import types
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(profiles, "sys", types.SimpleNamespace(platform="win32")):
            folder = profiles.install_wrapper(tmp, r"C:\Python\pythonw.exe", r"C:\Data\afk-hook.json")
            text = (folder / "claude.cmd").read_bytes().decode()
        self.assertIn("setlocal", text)  # the variables never outlive the window
        self.assertIn('if defined LIMITSWITCHER_WINDOW goto plain', text)
        self.assertIn('if /i "%~1"=="-p" goto plain', text)
        self.assertIn('--env "%LIMITSWITCHER_ENV%"', text)
        self.assertIn('call "%LIMITSWITCHER_ENV%"', text)
        self.assertTrue(text.rstrip().endswith("claude %*\r\nexit /b %ERRORLEVEL%"))


class OpenWindowTests(unittest.TestCase):
    @unittest.skipIf(sys.platform in ("darwin", "win32"), "the Linux terminal path")
    def test_new_window_starts_in_the_home_folder_with_the_routers_address(self):
        env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1/s/w1", "LIMITSWITCHER_WINDOW": "w1"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(Path, "home", return_value=Path(tmp)), \
                mock.patch.object(profiles.shutil, "which", return_value="/usr/bin/xterm"), \
                mock.patch.object(profiles.subprocess, "Popen") as popen:
            popen.return_value.pid = 4321
            self.assertEqual(profiles.open_window(env, Path(tmp) / "windows", "w1"), {"pid": 4321})
        self.assertEqual(popen.call_args.kwargs["cwd"], tmp)  # not the app's own folder
        self.assertEqual(popen.call_args.kwargs["env"]["LIMITSWITCHER_WINDOW"], "w1")
        self.assertNotIn("CLAUDE_CONFIG_DIR", {k for k in popen.call_args.kwargs["env"] if k not in os.environ})


class SessionTitleTests(unittest.TestCase):
    def test_title_from_rename_else_claude_codes_else_first_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "session.jsonl")

            def append(*entries):
                with open(path, "a", encoding="utf-8") as f:
                    for entry in entries:
                        f.write(json.dumps(entry) + "\n")

            append({"type": "user", "isMeta": True, "message": {"role": "user", "content": "Caveat: local commands"}},
                   {"type": "user", "message": {"role": "user", "content": "<command-name>/model</command-name>"}})
            self.assertIsNone(profiles.session_title(path))  # nothing typed yet
            append({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Fix the\nlogin bug"}]}},
                   {"type": "user", "message": {"role": "user", "content": "a later prompt"}})
            self.assertEqual(profiles.session_title(path), "Fix the login bug")
            append({"type": "summary", "summary": "Login bug fix"})
            self.assertEqual(profiles.session_title(path), "Login bug fix")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "custom-title", "customTitle": "Release work"}) + "\n" + '{"type": "custom-ti')
            self.assertEqual(profiles.session_title(path), "Release work")  # the half-written line waits
            with open(path, "a", encoding="utf-8") as f:
                f.write('tle", "customTitle": "Renamed again"}\n')
            self.assertEqual(profiles.session_title(path), "Renamed again")
            self.assertIsNone(profiles.session_title(str(Path(tmp) / "missing.jsonl")))


if __name__ == "__main__":
    unittest.main()
