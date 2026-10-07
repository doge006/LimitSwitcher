"""Separate accounts per window, end to end: the real app (local API, hook, status line, router, the
`claude` wrapper) with several fake Claude Code windows (fake_claude.py) on fake accounts and a fake
Anthropic, as a person would use it: windows open, take turns, renew their logins, hit usage limits,
get switched, and close.

What must hold throughout:
- every window is an ordinary one: ~/.claude and its one login; no config folder of its own;
- a window's requests are billed to that window's account (the router puts its login on them), and
  a window's switch moves that window only (the others, and the main login, stay);
- a window on the main account sends the main login unchanged, and follows the main account's switches;
- the app renews a window account's login itself, and never the main login (Claude Code's);
- Auto swap moves only the window that hit the limit, to a free account, or leaves it when none is.
"""
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
from account_switcher import claude_router, live as live_module, profiles
from account_switcher.integrations import Integrations
from account_switcher.live import LiveGateway
from account_switcher.providers import Claude, Codex
from account_switcher.vault import Vault
from account_switcher.web import Controller, make_server
from test_live import FakeAPI, claude_login, point_at_fake

HERE = Path(__file__).resolve().parent
EMAILS = ("a", "b", "c", "d")


def letter(token):
    """The account a fake token belongs to: at-b, at-br (renewed by Claude Code), at-rt-b+ (by the app)."""
    token = token[3:] if token.startswith("at-") else token
    token = token[3:] if token.startswith("rt-") else token
    return token[:1]


class ByLetter(dict):
    """The usage API's table: any of an account's tokens, renewed or not, finds its numbers."""

    def __init__(self, sim):
        super().__init__()
        self.sim = sim

    def __contains__(self, token):
        return letter(token) in self.sim.five

    def __getitem__(self, token):
        return self.get(token)

    def get(self, token, default=None):
        if letter(token) not in self.sim.five:
            return default
        reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        weekly = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + self.sim.week_in.get(letter(token), 3 * 86400)))
        five, week = self.sim.five[letter(token)]
        return {"five_hour": {"utilization": five, "resets_at": reset}, "seven_day": {"utilization": week, "resets_at": weekly}}


class FakeAnthropic:
    """POST /v1/messages: billed to the account whose token it carries; 429 at its limit."""

    def __init__(self, sim):
        self.seen = []  # (path, token)
        anthropic = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                token = self.headers.get("Authorization", "")[7:]
                anthropic.seen.append((self.path, token))
                who = letter(token)
                if who not in sim.five or token in sim.api.dead:
                    status, body = 401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid token"}}
                elif sim.five[who][0] >= 100:
                    status, body = 429, {"type": "error", "error": {"type": "rate_limit_error", "message": "limit"}, "email": f"{who}@example.com"}
                else:
                    status, body = 200, {"email": f"{who}@example.com", "token": token, "five": sim.five[who][0], "week": sim.five[who][1]}
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipIf(sys.platform == "win32", "drives the POSIX wrapper")
class WindowSimulation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.ctl, self.bin = root / "home", root / "ctl", root / "fakebin"
        for folder in (self.home, self.ctl, self.bin):
            folder.mkdir()
        self.log = root / "log.jsonl"
        self.log.touch()
        self.env_patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env_patch.start()
        for name in ("CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "LIMITSWITCHER_WINDOW"):
            os.environ.pop(name, None)
        self.grace = mock.patch.object(live_module, "WINDOW_GRACE", 0)
        self.grace.start()
        (self.bin / "claude").write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_claude.py'} \"$@\"\n")
        (self.bin / "claude").chmod(0o755)
        # The person's own status line: the app wraps it (and so is in every window's status line)
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "echo mine"}}))
        self.five = {}
        self.week_in = {}  # seconds until an account's weekly limit resets (Auto swap uses the soonest first)
        self.api = FakeAPI()
        self.api.claude_usage = ByLetter(self)
        self.anthropic = FakeAnthropic(self)
        self.upstream = mock.patch.object(claude_router, "UPSTREAM", self.anthropic.base)
        self.upstream.start()
        claude, codex = Claude(home=self.home, keychain=False), Codex(home=self.home)
        point_at_fake(claude, codex, self.api)
        for name in EMAILS:
            self.set_usage(name, 10 * EMAILS.index(name) + 5, 20)
        self.vault = Vault(root / "store")
        self.controller = Controller(gateway=lambda notify: LiveGateway(notify, self.vault, {"claude": claude, "codex": codex}, background=False))
        self.m = self.controller.gateway.manager
        self.m.spacing = 0
        # each account signed in once (the app imports whatever the main login is): a last, so it's the main login
        for name in reversed(EMAILS):
            claude_login(self.home, "uuid-" + name, name + "@example.com", "at-" + name, "rt-" + name)
            self.m.sync_live()
        self.m.meta.update(perWindow=True, afk=True, autoSwap=True, startWithWindows=False)
        self.m.refresh(force=True)
        self.server = make_server(self.controller)
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
        self.anthropic.close()
        self.api.close()
        self.upstream.stop()
        self.grace.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    # ---------- the fake world ----------
    def set_usage(self, name, five, week=20):
        self.five[name] = [five, week]

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
                   FAKE_NAME=name, FAKE_LOG=str(self.log), FAKE_ANTHROPIC=self.anthropic.base, FAKE_CTL=str(self.ctl),
                   FAKE_STEPS="control", FAKE_STATE=str(self.integrations.state_file))
        process = subprocess.Popen(["claude"], env=env, cwd=self.home, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.windows[name] = (process, [0])
        if not wait:
            return None
        return self.wait_for(lambda: self.entries(name, "start"), f"{name} to start")[0]["routed"]

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

    def later(self, seconds):
        """Time passes, for Auto resume's loop guard (what it continued, and when)."""
        for state in self.m.afk_sessions.values():
            state["continues"] = [t - seconds for t in state["continues"]]
        self.m.afk_continues = [t - seconds for t in self.m.afk_continues]

    def account(self, name):
        return self.m.find("claude", "uuid-" + name)

    def window_account(self, window_id):
        return next(w["accountId"] for w in self.m.windows() if w["id"] == window_id)

    def main_login(self):
        login = Claude(home=self.home, keychain=False).read_live()
        return login.email if login else None

    def assert_ordinary(self):
        """Every window is an ordinary Claude Code window: the one ~/.claude, no folder of its own."""
        self.assertEqual(profiles.existing(self.vault.root), {})
        for row in self.entries(step="turn"):
            self.assertEqual(row["own"], self.main_login() if row is self.entries(step="turn")[-1] else row["own"])

    def assert_no_renewals_of_the_main_login(self):
        main = Claude(home=self.home, keychain=False).read_live().secret["credentials"]["claudeAiOauth"]["refreshToken"]
        self.assertNotIn(main, {token for _, token in self.api.refreshes}, "the app renewed the main login")

    # ---------- the scenario ----------
    def test_windows_with_accounts_of_their_own(self):
        m = self.m
        # 1. Four windows open: three get a free account each; the fourth starts on the main account
        #    (nothing else free), routed all the same, so it can be moved later.
        ids = {name: self.open_window(name) for name in ("W1", "W2", "W3", "W4")}
        self.assertTrue(all(ids.values()), ids)
        self.assertEqual(len(set(ids.values())), 4)
        emails = {name: self.do(name, "turn")["email"] for name in ids}
        self.assertEqual(emails["W4"], "a@example.com")
        self.assertEqual(sorted(emails[n] for n in ("W1", "W2", "W3")), ["b@example.com", "c@example.com", "d@example.com"])
        for name in ids:  # every window sent the main login; the router billed it to the window's account
            self.assertEqual(self.entries(name, "turn")[-1]["sent"], "at-a")
        self.assertEqual(profiles.existing(self.vault.root), {})  # no config folders
        self.assertEqual(self.window_account(ids["W4"]), None)
        self.assertEqual({self.window_account(ids[n]) for n in ("W1", "W2", "W3")},
                         {self.account(emails[n][0]) for n in ("W1", "W2", "W3")})
        # their status lines: the user's own line still shows, and each window's numbers count for its own account
        for name in ids:
            self.assertIn("mine", self.entries(name, "turn")[-1]["line"])
            self.assertEqual(m.meta["accounts"][self.account(emails[name][0])]["usage"][0]["used"],
                             float(self.five[emails[name][0]][0]), name)

        # 2. W1 hits its limit: Auto swap moves W1, and only W1, to the best other account (the one whose
        #    weekly limit resets first), as it would the main login; Auto resume continues it.
        w1 = emails["W1"]
        best = next(e for e in (emails["W2"], emails["W3"]) if e != w1)
        self.week_in[best[0]] = 86400
        m.refresh(force=True)
        self.set_usage(w1[0], 100)
        entry = self.do("W1", "turn")
        self.assertEqual((entry["step"], entry["hook"]), ("limit", [2]), entry)
        self.assertEqual(self.do("W1", "turn")["email"], best)  # its next request: the new account
        for name in ("W2", "W3", "W4"):  # everyone else: unchanged
            self.assertEqual(self.do(name, "turn")["email"], emails[name])
        self.assertEqual(self.main_login(), "a@example.com")

        # 3. W3 closes: it leaves the Windows list.
        self.do("W3", "exit")
        m.sync_windows(force=True)
        self.assertNotIn(ids["W3"], [w["id"] for w in m.windows()])
        self.set_usage(w1[0], 5)
        m.refresh(force=True)

        # 5. Renewals. W2's account is no Claude Code's: the app renews it when it's about to expire, and
        #    W2's next request goes with the new token. The main login is Claude Code's: W4 renews it, its
        #    next request carries the new token through unchanged, and the app keeps (never renews) it.
        w2_account = self.account(emails["W2"][0])
        secret = self.vault.read_secret(w2_account)
        secret["credentials"]["claudeAiOauth"]["expiresAt"] = int((time.time() + 60) * 1000)
        self.vault.write_secret(w2_account, secret)
        old_refresh = secret["credentials"]["claudeAiOauth"]["refreshToken"]
        entry = self.do("W2", "turn")
        self.assertEqual((entry["email"], entry["token"]), (emails["W2"], "at-" + old_refresh + "+"))
        self.assertEqual(self.api.refreshes, [("/claude/token", old_refresh)])
        renewed = self.do("W4", "renew")
        entry = self.do("W4", "turn")
        self.assertEqual((entry["email"], entry["token"]), ("a@example.com", renewed["token"]))
        m.sync_live(force=True)
        self.assertEqual(self.vault.read_secret(self.account("a"))["credentials"]["claudeAiOauth"]["refreshToken"], renewed["refresh"])
        m.refresh(force=True)
        self.assert_no_renewals_of_the_main_login()

        # 6. The main login hits its limit: it moves to the best account, also one a window has (W4,
        #    on the main account, moves with it); the windows on accounts of their own stay where they are.
        self.week_in[w1[0]] = 3600
        m.refresh(force=True)
        self.set_usage("a", 100)
        entry = self.do("W4", "turn")
        self.assertEqual((entry["step"], entry["hook"]), ("limit", [2]), entry)
        self.assertEqual(self.main_login(), w1)
        self.assertEqual(self.do("W4", "turn")["email"], w1)
        self.assertEqual(self.do("W1", "turn")["email"], best)
        self.assertEqual(self.do("W2", "turn")["email"], emails["W2"])
        self.assert_no_renewals_of_the_main_login()

        # 7. By hand, from the full view: pick W2 (pointed out on screen), then "Use in window". Any
        #    account can be given, also one another window (or the main login) uses: nothing is copied.
        self.set_usage("a", 10)
        m.refresh(force=True)
        self.controller.action("highlightWindow", {"window": ids["W2"]})
        self.controller.action("swapWindow", {"window": ids["W2"], "id": self.account("a")})
        self.assertEqual(self.do("W2", "turn")["email"], "a@example.com")
        self.controller.action("swapWindow", {"window": ids["W2"], "id": self.account(best[0])})  # W1's account too
        self.assertEqual(self.do("W2", "turn")["email"], best)
        self.assertEqual(self.do("W1", "turn")["email"], best)
        self.controller.action("swapWindow", {"window": ids["W2"], "id": self.account(w1[0])})  # the main login's: follows it
        self.assertIsNone(self.window_account(ids["W2"]))
        self.assertEqual(self.do("W2", "turn")["email"], w1)
        self.assertEqual(self.do("W4", "turn")["email"], w1)
        self.assertEqual(self.entries("W2", "turn")[-1]["token"], self.entries("W2", "turn")[-1]["sent"])  # sent as it came
        self.assert_no_renewals_of_the_main_login()

        # 8. Everything closes: the windows go, every account is free, the newest tokens are kept.
        for name in ("W1", "W2", "W4"):
            self.do(name, "exit")
        m.sync_windows(force=True)
        self.assertEqual(m.windows(), [])
        self.assertEqual(json.loads((self.vault.root / "windows.json").read_text()), {})
        self.assertEqual(self.vault.read_secret(w2_account)["credentials"]["claudeAiOauth"]["refreshToken"], old_refresh + "+")
        for name, (process, _) in self.windows.items():
            self.assertEqual(process.returncode, 0, process.stderr.read().decode()[-2000:])
        self.assertTrue(all(row["routed"] for row in self.entries(step="start")))

    def swapaccount_world(self):
        """The person's example: three accounts, user1 (a) the main login, user2 (b), user3 (c); user3's
        weekly limit resets before user2's, so it's the next best. "Separate accounts per window" is off:
        every window starts as plain `claude`, and /swapaccount moves one."""
        self.m.meta["perWindow"] = False
        self.integrations.apply_per_window()
        self.set_usage("d", 100)  # 3 accounts in play
        self.week_in.update(b=5 * 86400, c=86400)
        self.m.refresh(force=True)

    def test_swapaccount_moves_one_window_and_auto_swap_follows_each_account(self):
        self.swapaccount_world()
        self.assertIsNone(self.open_window("W1"))  # a plain window: not routed
        self.assertEqual(self.do("W1", "turn")["email"], "a@example.com")
        # /swapaccount user3 in W1: from its next message W1 is on c, and it's in the Windows list
        said = self.do("W1", "swapaccount:c@example.com")["text"]
        self.assertIn("now uses **c@example.com**", said)
        self.assertEqual(self.do("W1", "turn")["email"], "c@example.com")
        w1 = self.entries("W1", "turn")[-1]["routed"]
        self.assertEqual([(w["id"], w["accountId"]) for w in self.m.windows()], [(w1, self.account("c"))])
        # a new window is still user1
        self.assertIsNone(self.open_window("W2"))
        self.assertEqual(self.do("W2", "turn")["email"], "a@example.com")
        self.assertEqual(self.main_login(), "a@example.com")
        # user1 runs out: the main login goes to the next best, user3 (W1 has it too: fine)
        self.set_usage("a", 100)
        entry = self.do("W2", "turn")
        self.assertEqual((entry["step"], entry["hook"]), ("limit", [2]), entry)
        self.assertEqual(self.main_login(), "c@example.com")
        self.assertEqual(self.do("W2", "turn")["email"], "c@example.com")
        self.assertEqual(self.do("W1", "turn")["email"], "c@example.com")
        # user3 runs out (a while later: a limit right after a resume is taken for a loop and stops):
        # both go to user2, each when its own turn meets the limit
        self.later(120)
        self.set_usage("c", 100)
        for name in ("W1", "W2"):
            entry = self.do(name, "turn")
            self.assertEqual((entry["step"], entry["hook"]), ("limit", [2]), (name, entry))
            self.assertEqual(self.do(name, "turn")["email"], "b@example.com", name)
        self.assertEqual(self.main_login(), "b@example.com")
        self.assertEqual(self.window_account(w1), self.account("b"))

    def test_only_the_window_on_a_used_up_account_moves(self):
        self.swapaccount_world()
        self.open_window("W1")
        self.open_window("W2")
        self.do("W1", "swapaccount:c@example.com")
        self.assertEqual(self.do("W1", "turn")["email"], "c@example.com")
        self.set_usage("c", 100)  # only user3 runs out
        entry = self.do("W1", "turn")
        self.assertEqual((entry["step"], entry["hook"]), ("limit", [2]), entry)
        self.assertEqual(self.do("W1", "turn")["email"], "b@example.com")  # user3's window goes to user2
        self.assertEqual(self.do("W2", "turn")["email"], "a@example.com")  # the rest stay on user1
        self.assertEqual(self.main_login(), "a@example.com")

    def test_swapaccount_names_and_lists(self):
        self.swapaccount_world()
        self.open_window("W1")
        said = self.do("W1", "swapaccount:nobody")["text"]
        self.assertIn("No Claude account called **nobody**", said)
        for email in ("a@example.com", "b@example.com", "c@example.com"):
            self.assertIn(email, said)
        self.assertIsNone(self.entries("W1", "swapaccount")[-1]["routed"])  # nothing changed
        self.assertEqual(self.m.windows(), [])
        self.assertIn("now uses **b@example.com**", self.do("W1", "swapaccount:b")["text"])  # the part before @ does
        self.assertEqual(self.do("W1", "turn")["email"], "b@example.com")
        # name mode: names in the list, never emails; a name or an email picks one
        self.m.meta["nameMode"] = True
        self.m.meta["accounts"][self.account("c")]["label"] = "Work"
        said = self.do("W1", "swapaccount:")["text"]
        self.assertIn("Work", said)
        self.assertNotIn("@example.com", said)
        self.assertIn("now uses **Work**", self.do("W1", "swapaccount:work")["text"])
        self.assertEqual(self.do("W1", "turn")["email"], "c@example.com")
        self.assertIn("now uses", self.do("W1", "swapaccount:b@example.com")["text"])
        self.assertEqual(self.do("W1", "turn")["email"], "b@example.com")
        self.assertIn("main account", self.do("W1", "swapaccount:main")["text"])  # back on the main account
        self.assertEqual(self.do("W1", "turn")["email"], "a@example.com")
        self.assertEqual(len(self.m.windows()), 1)  # one window throughout: the same one

    def test_windows_opened_at_once_never_share_an_account(self):
        self.m.meta["accounts"][self.account("d")]["status"] = "Login expired; sign in again"  # 2 free: b, c
        for name in ("W1", "W2", "W3", "W4", "W5"):
            self.open_window(name, wait=False)  # all at the same moment
        starts = {name: self.wait_for(lambda n=name: self.entries(n, "start"), f"{name} to start")[0]["routed"]
                  for name in ("W1", "W2", "W3", "W4", "W5")}
        self.assertTrue(all(starts.values()), starts)
        own = sorted(filter(None, (self.window_account(i) for i in starts.values())))
        self.assertEqual(own, sorted([self.account("b"), self.account("c")]))
        emails = sorted(self.do(name, "turn")["email"] for name in starts)
        self.assertEqual(emails, ["a@example.com"] * 3 + ["b@example.com", "c@example.com"])

    def test_the_app_restarting_keeps_each_windows_account(self):
        window = self.open_window("W1")
        first = self.do("W1", "turn")["email"]
        self.assertNotEqual(first, "a@example.com")
        port = self.integrations.claude_router.port
        self.integrations.stop()  # an update: the app quits and starts again
        integrations = Integrations(self.controller.gateway, self.server.hook_url, self.server.hook_token,
                                    codex_home=Path(self.tmp.name) / "no-codex", claude_root=self.home / ".claude")
        self.controller.gateway.integrations = self.integrations = integrations
        self.m.claude_windows = self.m._load_windows()
        integrations.start()
        self.assertEqual(integrations.claude_router.port, port)  # the same address the window has
        self.assertEqual(self.do("W1", "turn")["email"], first)
        self.assertEqual(self.window_account(window), self.account(first[0]))


if __name__ == "__main__":
    unittest.main()
