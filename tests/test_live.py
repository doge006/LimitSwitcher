"""Real-account backend against fake Claude Code / Codex files and a fake provider API."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import sys
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
from account_switcher.live import LiveAccounts, LiveGateway
from account_switcher.providers import Claude, Codex
from account_switcher.vault import Vault
from account_switcher.web import Controller


def jwt(claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def point_at_fake(claude, codex, api):
    """Never talk to the real providers from tests."""
    claude.USAGE_URL, claude.TOKEN_URL, claude.PROFILE_URL = api.base + "/claude/usage", api.base + "/claude/token", api.base + "/claude/profile"
    codex.USAGE_URL, codex.TOKEN_URL, codex.CHECK_URL = api.base + "/codex/usage", api.base + "/codex/token", api.base + "/codex/check"


class FakeAPI:
    """Usage per access token; token endpoints rotate tokens and count calls."""

    def __init__(self):
        self.claude_usage, self.codex_usage = {}, {}
        self.claude_profile, self.codex_check = {}, {}
        self.limited = set()
        self.calls = []
        self.refreshes = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                token = self.headers.get("Authorization", "")[7:]
                api.calls.append(self.path)
                if self.path in ("/claude/profile", "/codex/check"):
                    table = api.claude_profile if self.path == "/claude/profile" else api.codex_check
                    return self.reply(200, table.get(token, {"account": {"email": "x"}}))
                table = api.claude_usage if self.path == "/claude/usage" else api.codex_usage
                if token in api.limited:  # Anthropic answers an expired token with 429, not 401
                    return self.reply(429, {"error": "rate_limited"})
                if token not in table:
                    return self.reply(401, {"error": "expired"})
                self.reply(200, table[token])

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                api.refreshes.append((self.path, body["refresh_token"]))
                if body["refresh_token"] in api.dead:  # used up elsewhere: single-use tokens
                    return self.reply(400, {"error": "invalid_grant"})
                new = body["refresh_token"] + "+"
                if self.path == "/claude/token":
                    self.reply(200, {"access_token": "at-" + new, "refresh_token": new, "expires_in": 28800,
                                     "account": {"uuid": api.uuid_for.get(body["refresh_token"])}})
                else:
                    self.reply(200, {"access_token": "at-" + new, "refresh_token": new})

        self.uuid_for = {}
        self.dead = set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def claude_login(home, uuid, email, access, refresh, expires_in=3600, tier="default_claude_max_20x", extra=None):
    creds = {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "subscriptionType": "max",
                               "rateLimitTier": tier, "expiresAt": int((time.time() + expires_in) * 1000)}}
    creds.update(extra or {})
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / ".credentials.json").write_text(json.dumps(creds))
    config = json.loads((home / ".claude.json").read_text()) if (home / ".claude.json").exists() else {"theme": "dark"}
    config["oauthAccount"] = {"accountUuid": uuid, "emailAddress": email}
    (home / ".claude.json").write_text(json.dumps(config))


def codex_login(home, account_id, email, access, refresh, plan="pro"):
    (home / ".codex").mkdir(exist_ok=True)
    auth = {"tokens": {"access_token": access, "refresh_token": refresh, "account_id": account_id,
                       "id_token": jwt({"email": email, "https://api.openai.com/auth": {"chatgpt_plan_type": plan}})},
            "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (home / ".codex" / "auth.json").write_text(json.dumps(auth))


def claude_usage(five, week, fable=None, reset_in=3600, week_in=None):
    reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + reset_in))
    weekly = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + week_in)) if week_in else reset
    body = {"five_hour": {"utilization": five, "resets_at": reset}, "seven_day": {"utilization": week, "resets_at": weekly}}
    if fable is not None:
        body["limits"] = [{"kind": "weekly_scoped", "percent": fable, "resets_at": reset,
                           "scope": {"model": {"display_name": "Fable 5"}}}]
    return body


def codex_usage(five, week, limit_reached=False):
    now = time.time()
    return {"plan_type": "pro", "rate_limit": {"limit_reached": limit_reached,
            "primary_window": {"used_percent": five, "reset_at": now + 3000, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": week, "reset_at": now + 86400, "limit_window_seconds": 604800}}}


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.api = FakeAPI()
        claude, codex = Claude(home=self.home), Codex(home=self.home)
        point_at_fake(claude, codex, self.api)
        self.providers = {"claude": claude, "codex": codex}
        self.vault = Vault(root / "store")
        self.logs = []
        claude_login(self.home, "uuid-a", "a@example.com", "at-a", "rt-a", extra={"mcpOAuth": {"server": "keep"}})
        codex_login(self.home, "acct-x", "x@example.com", "at-x", "rt-x")
        self.api.claude_usage["at-a"] = claude_usage(40, 20, fable=10)
        self.api.codex_usage["at-x"] = codex_usage(5, 50)

    def tearDown(self):
        self.api.close()
        self.tmp.cleanup()

    def manager(self):
        m = LiveAccounts(lambda kind, value: self.logs.append((kind, value)), self.vault, self.providers)
        m.spacing = 0
        return m

    def by_email(self, manager, email):
        return next(a for a in manager.accounts() if a.email == email)

    def live_claude(self):
        return self.providers["claude"].read_live()

    def test_a_switch_waits_for_claude_codes_renewal_and_keeps_its_new_tokens(self):
        """Claude Code renews under its lock: read, renew, save. A switch in that window used to be
        overwritten, and the saved copy of the old account kept a used-up refresh token."""
        m = self.manager()
        m.sync_live()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b")
        m.sync_live()
        m.swap(self.by_email(m, "a@example.com").id)  # a in use, b saved
        lock = self.home / ".claude" / ".oauth_refresh.lock"
        lock.mkdir()  # Claude Code starts renewing a's login

        def claude_code_renews():
            time.sleep(0.6)
            claude_login(self.home, "uuid-a", "a@example.com", "at-a2", "rt-a2")  # its renewed tokens
            lock.rmdir()

        renewal = threading.Thread(target=claude_code_renews)
        renewal.start()
        m.swap(self.by_email(m, "b@example.com").id)
        renewal.join()
        self.assertEqual(self.live_claude().email, "b@example.com")  # the switch stands
        a = self.by_email(m, "a@example.com")
        saved = self.vault.read_secret(a.id)["credentials"]["claudeAiOauth"]["refreshToken"]
        self.assertEqual(saved, "rt-a2")  # a's newest tokens, not the used-up ones
        self.assertFalse(lock.exists())

    def test_imports_live_logins_and_reads_usage(self):
        m = self.manager()
        m.sync_live()
        self.assertEqual(sorted(a.email for a in m.accounts()), ["a@example.com", "x@example.com"])
        m.refresh()
        a = self.by_email(m, "a@example.com")
        self.assertEqual(a.plan, "Max 20x")
        self.assertEqual([(w["key"], w["used"], w["scope"]) for w in a.windows()],
                         [("five_hour", 40.0, "account"), ("weekly", 20.0, "account"), ("model-fable-5", 10.0, "model")])
        self.assertEqual(a.headroom, 60)
        self.assertIsNotNone(a.renews_at)
        x = self.by_email(m, "x@example.com")
        self.assertEqual([w["key"] for w in x.windows()], ["five_hour", "weekly"])
        self.assertEqual(x.plan, "Pro")
        self.assertEqual(self.api.refreshes, [])  # in-use accounts are never refreshed by us

    def test_new_login_is_added_and_switching_round_trips(self):
        m = self.manager()
        m.sync_live()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b", tier="default_claude_max_5x",
                     extra={"mcpOAuth": {"server": "keep"}})
        m.sync_live()
        b = self.by_email(m, "b@example.com")
        self.assertEqual(m.active["claude"], b.id)
        self.assertEqual(b.plan, "Max 5x")
        # Claude Code rotates B's tokens while B is in use...
        creds = json.loads((self.home / ".claude" / ".credentials.json").read_text())
        creds["claudeAiOauth"]["refreshToken"] = "rt-b-rotated"
        creds["mcpOAuth"] = {"server": "newer"}
        (self.home / ".claude" / ".credentials.json").write_text(json.dumps(creds))
        a = self.by_email(m, "a@example.com")
        m.swap(a.id)
        live = self.live_claude()
        self.assertEqual(live.email, "a@example.com")
        self.assertEqual(live.secret["credentials"]["claudeAiOauth"]["refreshToken"], "rt-a")
        self.assertEqual(live.secret["credentials"]["mcpOAuth"], {"server": "newer"})  # MCP logins untouched
        self.assertEqual(json.loads((self.home / ".claude.json").read_text())["theme"], "dark")  # other config kept
        # ...and switching back restores B with the rotated token we captured.
        m.swap(b.id)
        self.assertEqual(self.live_claude().secret["credentials"]["claudeAiOauth"]["refreshToken"], "rt-b-rotated")

    def test_inactive_accounts_refresh_but_live_one_waits(self):
        m = self.manager()
        m.sync_live()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b-old", "rt-b", expires_in=-10)
        self.api.uuid_for["rt-b"] = "uuid-b"
        m.sync_live()
        m.refresh()
        b = self.by_email(m, "b@example.com")
        self.assertIn("Waiting for Claude", b.status)  # live login expired: leave it to Claude Code
        self.assertEqual([r for r in self.api.refreshes if r[0] == "/claude/token"], [])
        a = self.by_email(m, "a@example.com")
        m.swap(a.id)  # now B is inactive and its expired token is ours to refresh
        self.api.claude_usage["at-rt-b+"] = claude_usage(10, 10)
        m.refresh(force=True)
        self.assertEqual(self.api.refreshes, [("/claude/token", "rt-b")])
        b = self.by_email(m, "b@example.com")
        self.assertEqual(b.status, "")
        self.assertEqual(self.vault.read_secret(b.id)["credentials"]["claudeAiOauth"]["refreshToken"], "rt-b+")

    def test_a_switch_waits_for_our_own_renewal_and_hands_over_the_new_tokens(self):
        """The usage check renews a saved login while it's switched to: the switch used to copy the
        refresh token that renewal was spending, and Claude Code's copy expired within the hour."""
        m = self.manager()
        m.sync_live()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b-old", "rt-b", expires_in=-10)
        self.api.uuid_for["rt-b"] = "uuid-b"
        self.api.claude_usage["at-rt-b+"] = claude_usage(10, 10)
        m.sync_live()
        a, b = self.by_email(m, "a@example.com"), self.by_email(m, "b@example.com")
        m.swap(a.id)  # b saved, with an expired token: ours to renew
        provider = self.providers["claude"]
        renewing, release = threading.Event(), threading.Event()
        real = provider.refresh

        def slow_refresh(secret, why=""):
            renewing.set()
            release.wait(5)
            return real(secret, why)

        with mock.patch.object(provider, "refresh", slow_refresh):
            check = threading.Thread(target=m.refresh, kwargs={"force": True})
            check.start()
            self.assertTrue(renewing.wait(5))
            switch = threading.Thread(target=m.swap, args=(b.id,))
            switch.start()
            switch.join(0.3)
            self.assertTrue(switch.is_alive())  # waits for the renewal under way
            release.set()
            check.join(5)
            switch.join(5)
        self.assertEqual(self.live_claude().email, "b@example.com")
        self.assertEqual(self.live_claude().secret["credentials"]["claudeAiOauth"]["refreshToken"], "rt-b+")
        self.assertEqual([r for r in self.api.refreshes if r[0] == "/claude/token"], [("/claude/token", "rt-b")])

    def test_name_mode_never_puts_an_email_in_a_notification(self):
        m = self.manager()
        m.sync_live()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b", expires_in=36000)
        m.sync_live()
        m.meta["nameMode"] = True
        m.meta["accounts"][self.by_email(m, "b@example.com").id]["label"] = "Work"
        self.logs.clear()
        m.swap(self.by_email(m, "a@example.com").id)
        m.swap(self.by_email(m, "b@example.com").id)
        messages = [text for kind, text in self.logs if kind == "log"]
        self.assertIn("Claude now uses Work", messages)
        self.assertTrue(any(text.startswith("Claude now uses Claude ") for text in messages))  # no name: "Claude 1"
        self.assertFalse([text for text in messages if "@" in text])

    def test_auto_swap_moves_to_most_headroom(self):
        m = self.manager()
        m.sync_live()
        for uuid, email, access, five in (("uuid-b", "b@example.com", "at-b", 70), ("uuid-c", "c@example.com", "at-c", 10)):
            claude_login(self.home, uuid, email, access, "rt-" + uuid, expires_in=36000)
            self.api.claude_usage[access] = claude_usage(five, 5)
            m.sync_live()
        m.swap(self.by_email(m, "a@example.com").id)
        self.api.claude_usage["at-a"] = claude_usage(100, 30)
        m.refresh(force=True)
        self.assertEqual(m.auto_swap(), [self.by_email(m, "c@example.com").id])
        self.assertEqual(self.live_claude().email, "c@example.com")
        self.assertIn(("log", "Claude now uses c@example.com (automatic)"), self.logs)
        m.meta["autoSwap"] = False
        self.api.claude_usage["at-c"] = claude_usage(100, 5)
        m.refresh(force=True)
        self.assertEqual(m.auto_swap(), [])

    def test_auto_swap_uses_the_weekly_that_resets_first(self):
        """Weekly quota that resets sooner is lost sooner: use it first, even with less room left,
        but not an account that has next to nothing left."""
        m = self.manager()
        m.sync_live()
        for uuid, email, access, week, days in (("uuid-b", "b@example.com", "at-b", 10, 6), ("uuid-c", "c@example.com", "at-c", 57, 4.8),
                                                ("uuid-d", "d@example.com", "at-d", 98, 1)):
            claude_login(self.home, uuid, email, access, "rt-" + uuid, expires_in=36000)
            self.api.claude_usage[access] = claude_usage(0, week, week_in=days * 86400)
            m.sync_live()
        m.swap(self.by_email(m, "a@example.com").id)
        self.api.claude_usage["at-a"] = claude_usage(100, 30)
        m.refresh(force=True)
        self.assertEqual(m.auto_swap(), [self.by_email(m, "c@example.com").id])  # not b (more room, later) nor d (2% left)

    def test_codex_limit_reached_and_remove(self):
        m = self.manager()
        m.sync_live()
        self.api.codex_usage["at-x"] = codex_usage(97, 50, limit_reached=True)
        m.refresh()
        x = self.by_email(m, "x@example.com")
        self.assertFalse(x.eligible)
        with self.assertRaises(RuntimeError):
            m.remove(x.id)  # cannot remove the account in use

    def test_reset_alert_is_offered_to_the_mod_once_a_used_up_provider_has_room(self):
        controller = Controller(gateway=lambda notify: LiveGateway(notify, self.vault, self.providers, background=False))
        try:
            m = controller.gateway.manager
            m.spacing = 0
            self.api.claude_usage["at-a"] = claude_usage(100, 30)  # the only Claude account: used up
            m.refresh(force=True)
            mod = {"source": "mod", "session": "s"}
            self.assertEqual(controller.alerts_for(mod), [])
            self.api.claude_usage["at-a"] = claude_usage(0, 30)  # its 5-hour window reset
            m.refresh(force=True)
            alerts = controller.alerts_for(mod)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["text"], "⇄ LimitSwitcher · Claude has room again: a@example.com's limit has reset")
            self.assertEqual(controller.alerts_for({"session": "s"}), [])  # only the mod toasts
            # What the mod gets from the local API with its status line report
            from account_switcher.web import make_server
            from urllib.request import ProxyHandler, Request, build_opener
            server = make_server(controller)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = server.hook_url[: -len("/api/afk")]
                request = Request(base + "/api/statusline", data=json.dumps(mod).encode(), method="POST",
                                  headers={"Authorization": "Bearer " + server.hook_token, "Content-Type": "application/json"})
                with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                    self.assertEqual(json.load(response)["alerts"], alerts)
            finally:
                server.shutdown()
                server.server_close()
            m.refresh(force=True)
            self.assertEqual(controller.alerts_for(mod), alerts)  # nothing new: the same alert, once
            with mock.patch("account_switcher.web.time.time", return_value=time.time() + 301):
                self.assertEqual(controller.alerts_for(mod), [])  # offered for 5 minutes
            # Switched off in Settings: no alert
            controller.action("resetAlerts", {"on": False})
            self.assertFalse(controller.snapshot()["resetAlerts"])
            self.assertFalse(m.meta["resetAlerts"])
            self.api.claude_usage["at-a"] = claude_usage(100, 30)
            m.refresh(force=True)
            self.api.claude_usage["at-a"] = claude_usage(0, 30)
            m.refresh(force=True)
            self.assertEqual(controller.alerts_for(mod), [])
        finally:
            controller.close()

    def test_controller_in_live_mode(self):
        controller = Controller(gateway=lambda notify: LiveGateway(notify, self.vault, self.providers, background=False))
        try:
            controller.gateway.manager.spacing = 0
            controller.gateway.manager.refresh()
            state = controller.snapshot()
            self.assertEqual(state["mode"], "live")
            names = {a["name"] for a in state["accounts"]}
            self.assertEqual(names, {"a@example.com", "x@example.com"})
            claude = next(a for a in state["accounts"] if a["provider"] == "claude")
            self.assertTrue(claude["active"])
            self.assertEqual(claude["headroom"], 60)
            self.assertGreater(claude["renewsAt"], time.time())
            with self.assertRaises(ValueError):
                controller.action("run", {"scenario": "normal"})
            self.assertTrue(state["launchAtLogin"])  # on by default: Codex goes through the app
            with mock.patch("account_switcher.integrations.set_start_with_windows") as start:
                controller.action("startup", {"on": False})
            start.assert_called_once_with(False)
            self.assertFalse(controller.snapshot()["launchAtLogin"])
            self.assertFalse(controller.gateway.manager.meta["startWithWindows"])  # remembered
            # Claude Code's status line: nothing of ours shows until the mod is installed (it reports
            # in); the usage still comes in without it.
            limits = {"five_hour": {"used_percentage": 40, "resets_at": time.time() + 3600}}
            self.assertIsNone(controller.statusline({"rate_limits": limits}))
            self.assertIsNone(controller.statusline({"rate_limits": limits, "session": "s", "source": "mod"}))
            line = controller.statusline({"rate_limits": limits})
            self.assertIn("LimitSwitcher", line or "")
            self.assertEqual(line.parts[0], {"t": "⇄", "c": "good"})  # the coloured pieces ride along
            m = controller.gateway.manager
            claude_id = next(a["id"] for a in controller.snapshot()["accounts"] if a["provider"] == "claude")
            # Name mode: the status line shows the account's name ("Claude 1" without one), not its email.
            self.assertIn("a@example.com", controller.statusline({"rate_limits": limits}))
            controller.action("names", {"on": True})
            self.assertIn("Claude 1", controller.statusline({"rate_limits": limits}))
            self.assertNotIn("@", controller.statusline({"rate_limits": limits}))
            controller.action("rename", {"id": claude_id, "name": "Work"})
            self.assertIn("Work", controller.statusline({"rate_limits": limits}))
            controller.action("names", {"on": False})
            # A rate limit's retry time follows the clock setting, also after it changes.
            with m.lock:
                m.meta["accounts"][claude_id].update(status="Rate limited by Claude · retrying at 18:37",
                                                     backoffUntil=time.mktime((2030, 1, 1, 18, 37, 0, 0, 0, -1)))
            controller.action("clock", {"on": False})
            status = next(a for a in m.accounts() if a.id == claude_id).status
            self.assertTrue(status.endswith("retrying at 6:37 PM"), status)
            controller.action("clock", {"on": True})
            status = next(a for a in m.accounts() if a.id == claude_id).status
            self.assertTrue(status.endswith("retrying at 18:37"), status)
        finally:
            controller.close()

    def test_every_setting_survives_a_restart(self):
        m = self.manager()
        m.meta.update({"taskbarDisplay": "right", "taskbarView": False, "compactPanel": True})
        m.save()
        again = self.manager()
        for key, value in {"taskbarDisplay": "right", "taskbarView": False, "compactPanel": True}.items():
            self.assertEqual(again.meta.get(key), value)

    def test_secrets_are_not_in_metadata(self):
        m = self.manager()
        m.sync_live()
        meta = (self.vault.meta_path).read_text()
        self.assertNotIn("rt-a", meta)
        self.assertNotIn("at-x", meta)
        stored = next(self.vault.secret_dir.iterdir()).read_bytes()
        self.assertTrue(stored.startswith(b"PLAIN") or stored.startswith(b"DPAPI"))

    def test_schedule_spares_the_api(self):
        m = self.manager()
        m.sync_live()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b", expires_in=36000)
        self.api.claude_usage["at-b"] = claude_usage(10, 10, reset_in=7200)
        m.sync_live()
        m.swap(self.by_email(m, "a@example.com").id)
        m.refresh()
        usage_calls = lambda: [c for c in self.api.calls if c.endswith("/usage")]
        first = len(usage_calls())
        m.refresh()  # nothing is due yet: no requests at all
        self.assertEqual(len(usage_calls()), first)
        b_id = self.by_email(m, "b@example.com").id
        a_id = m.active["claude"]
        now = time.time()
        self.assertAlmostEqual(m.due(a_id, m.meta["accounts"][a_id], True, now) - now, 60, delta=5)  # Claude in use, not live: every minute
        self.assertAlmostEqual(m.due(b_id, m.meta["accounts"][b_id], False, now) - now, 60, delta=5)  # not in use here (cloud sessions, other PCs)
        # A passed reset is applied locally without asking the API.
        m.meta["accounts"][b_id]["usage"][0]["resetsAt"] = now - 5
        b = self.by_email(m, "b@example.com")
        self.assertEqual(b.windows()[0]["used"], 0.0)
        self.assertEqual(len(usage_calls()), first)
        # Opening the panel only refetches stale data; a 429 backs off.
        m.refresh(max_age=120)
        self.assertEqual(len(usage_calls()), first)
        self.api.claude_usage.pop("at-a")
        m.meta["accounts"][a_id]["updatedAt"] = 0

    def test_failing_account_is_not_retried_in_a_loop(self):
        m = self.manager()
        m.sync_live()
        self.api.claude_usage.pop("at-a")  # every fetch for A now fails (401 on the live login)
        m.refresh(force=True)
        a = self.by_email(m, "a@example.com")
        self.assertIn("Waiting for Claude", a.status)
        before = len(self.api.calls)
        for _ in range(3):  # the login file is looked at often, the API not at all
            m.meta["accounts"][a.id]["attemptedAt"] = 0.0
            m.refresh()
        self.assertEqual(len(self.api.calls), before)
        self.assertGreaterEqual(m.next_delay(), 20)

    def test_a_login_the_client_never_replaces_reads_signed_out(self):
        """Claude Code renews within seconds; minutes later with the same refused login, it was
        signed out (the login was revoked), and waiting for it would only puzzle."""
        m = self.manager()
        m.sync_live()
        self.api.claude_usage.pop("at-a")
        m.refresh(force=True)
        a = self.by_email(m, "a@example.com")
        self.assertIn("Waiting for Claude", a.status)
        m.meta["accounts"][a.id]["waitingSince"] -= 400
        m.meta["accounts"][a.id]["attemptedAt"] = 0.0
        before = len(self.api.calls)
        m.refresh()
        a = self.by_email(m, "a@example.com")
        self.assertEqual(a.status, "Signed out · sign in to Claude Code again")
        self.assertEqual(len(self.api.calls), before)  # still no API call for it
        self.api.claude_usage["at-a2"] = claude_usage(30, 40)  # signed in again in Claude Code
        claude_login(self.home, "uuid-a", "a@example.com", "at-a2", "rt-a2")
        m.refresh()
        a = self.by_email(m, "a@example.com")
        self.assertEqual(a.status, "")
        self.assertTrue(a.usage)

    def test_a_login_the_client_renewed_is_checked_at_once_not_minutes_later(self):
        m = self.manager()
        m.sync_live()
        self.api.claude_usage.pop("at-a")
        m.refresh(force=True)
        a = self.by_email(m, "a@example.com")
        self.assertIn("Waiting for Claude", a.status)
        now = time.time()
        self.assertLessEqual(m.due(a.id, m.meta["accounts"][a.id], True, now) - now, 20)
        self.api.claude_usage["at-a2"] = claude_usage(30, 40)  # Claude Code renews its login...
        claude_login(self.home, "uuid-a", "a@example.com", "at-a2", "rt-a2")
        m.refresh()  # ...and the very next look finds it: no five-minute wait
        a = self.by_email(m, "a@example.com")
        self.assertEqual(a.status, "")
        self.assertTrue(a.usage)

    def test_status_line_keeps_claude_live_without_the_api(self):
        m = self.manager()
        m.sync_live()
        m.refresh(force=True)
        a = self.by_email(m, "a@example.com")
        m.live_since["claude"] = 0  # settled after the last switch
        calls = len(self.api.calls)
        reset = time.time() + 3600
        line = m.statusline({"five_hour": {"used_percentage": 77, "resets_at": reset},
                             "seven_day": {"used_percentage": 20.4, "resets_at": reset + 86400}})
        self.assertEqual(len(self.api.calls), calls)  # nothing asked of the API
        a = self.by_email(m, "a@example.com")
        five = next(w for w in a.windows() if w["key"] == "five_hour")
        self.assertEqual((five["used"], five["resetsAt"]), (77.0, reset))
        self.assertEqual(line, "⇄ LimitSwitcher · a@example.com · 5h 23% left · 1w 79% left")  # 79.6 left: rounded down
        meta = m.meta["accounts"][a.id]
        now = time.time()
        self.assertGreater(m.due(a.id, meta, True, now) - now, 1700)  # while live, the API only every 30 min
        m.live_since["claude"] = time.time()  # just switched: the report may still be the old account
        m.statusline({"five_hour": {"used_percentage": 5, "resets_at": reset}})
        five = next(w for w in self.by_email(m, "a@example.com").windows() if w["key"] == "five_hour")
        self.assertEqual(five["used"], 77.0)

    def test_a_lagging_api_answer_never_undoes_live_numbers(self):
        m = self.manager()
        m.sync_live()
        m.refresh(force=True)
        m.live_since["claude"] = 0
        m.statusline({"five_hour": {"used_percentage": 77, "resets_at": time.time() + 3600}})
        self.api.claude_usage["at-a"] = claude_usage(70, 20)  # the usage API, a little behind
        m.refresh(force=True)
        five = next(w for w in self.by_email(m, "a@example.com").windows() if w["key"] == "five_hour")
        self.assertEqual(five["used"], 77.0)
        self.api.claude_usage["at-a"] = claude_usage(80, 20)  # and once it's ahead, it counts
        m.refresh(force=True)
        five = next(w for w in self.by_email(m, "a@example.com").windows() if w["key"] == "five_hour")
        self.assertEqual(five["used"], 80.0)

    def test_stale_status_line_numbers_never_undo_newer_ones(self):
        """After a limit, Claude Code keeps sending its last numbers with every reply: they must not
        overwrite newer ones, and must not keep the API at its slow 'followed live' pace."""
        m = self.manager()
        m.sync_live()
        m.refresh(force=True)
        a = self.by_email(m, "a@example.com")
        m.live_since["claude"] = 0
        reset = time.time() + 3600
        m.statusline({"five_hour": {"used_percentage": 52, "resets_at": reset}})
        m.observe(a.id, [(300, 100.0, reset)])  # newer: the limit was reached
        meta = m.meta["accounts"][a.id]
        meta["liveAt"] = time.time() - 1000  # the last real change was a while ago
        m.statusline({"five_hour": {"used_percentage": 52, "resets_at": reset}})  # the stale repeat
        five = next(w for w in self.by_email(m, "a@example.com").windows() if w["key"] == "five_hour")
        self.assertEqual(five["used"], 100.0)
        now = time.time()
        self.assertLess(m.due(a.id, meta, True, now) - now, 400)  # back to the normal API pace
        m.statusline({"five_hour": {"used_percentage": 3, "resets_at": reset + 5 * 3600}})  # a new window
        five = next(w for w in self.by_email(m, "a@example.com").windows() if w["key"] == "five_hour")
        self.assertEqual(five["used"], 3.0)

    def test_an_idle_sessions_old_numbers_dont_fight_a_busy_one(self):
        """Two Claude Code sessions: one idle since before a switch (another account's 100%), one
        working now. Only a session whose numbers just moved (a new reply) counts, so the bar
        follows the busy session and never jumps to the idle one's numbers and back."""
        m = self.manager()
        m.sync_live()
        m.refresh(force=True)
        a = self.by_email(m, "a@example.com")
        m.live_since["claude"] = 0
        reset = time.time() + 3600
        old = {"five_hour": {"used_percentage": 100, "resets_at": reset - 1800}}

        def used():
            return next(w for w in self.by_email(m, "a@example.com").windows() if w["key"] == "five_hour")["used"]

        m.statusline({"five_hour": {"used_percentage": 50, "resets_at": reset}}, "busy")
        m.statusline({"five_hour": {"used_percentage": 52, "resets_at": reset}}, "busy")  # a reply: counts
        self.assertEqual(used(), 52.0)
        for _ in range(3):
            m.statusline(old, "idle")                 # an idle session, now and again: ignored
            self.assertEqual(used(), 52.0)
        m.statusline({"five_hour": {"used_percentage": 55, "resets_at": reset}}, "busy")
        self.assertEqual(used(), 55.0)
        self.assertIn(a.id, m.meta["accounts"])

    def test_the_old_accounts_numbers_never_land_on_the_new_one(self):
        """After a switch, Claude Code sessions keep the last reply's numbers (the old account's,
        at its limit) until they get a new one. Those must not show as the new account's usage,
        also from a session that reports for the first time after the switch."""
        m = self.manager()
        m.sync_live()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b")
        m.sync_live()
        m.swap(self.by_email(m, "a@example.com").id)  # a in use, b just added
        m.refresh(force=True)
        a, b = self.by_email(m, "a@example.com"), self.by_email(m, "b@example.com")
        m.live_since["claude"] = 0
        reset = time.time() + 3600
        old = {"five_hour": {"used_percentage": 100, "resets_at": reset},
               "seven_day": {"used_percentage": 40, "resets_at": reset + 86400}}
        m.statusline(old, "s1")
        before = self.by_email(m, "b@example.com").windows()
        m.swap(b.id, reason="limit")
        m.live_since["claude"] = 0  # long settled: the numbers are still the old ones
        m.session_moved_at = 0.0
        for session in ("s1", "s2", None):  # the session at the limit, a new one, and an anonymous one
            m.statusline(old, session)
            self.assertEqual(self.by_email(m, "b@example.com").windows(), before)
        fresh = {"five_hour": {"used_percentage": 2, "resets_at": reset + 4 * 3600}}
        m.statusline(fresh, "s1")  # a real reply on the new account
        five = next(w for w in self.by_email(m, "b@example.com").windows() if w["key"] == "five_hour")
        self.assertEqual(five["used"], 2.0)
        self.assertIn(a.id, m.meta["accounts"])

    def test_mac_sign_in_opens_in_terminal(self):
        m = self.manager()
        started = []
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("account_switcher.live.subprocess.Popen", side_effect=lambda args, **kw: started.append(args) or mock.Mock()), \
                mock.patch("account_switcher.live.threading.Thread"):
            m.add("claude")
        # Terminal runs a .command file by itself: it has the user's PATH and a window, and no
        # permission to control Terminal is needed.
        self.assertEqual(started[0][:3], ["/usr/bin/open", "-a", "Terminal"])
        script = Path(started[0][3])
        self.assertEqual(script.suffix, ".command")
        text = script.read_text()
        self.assertIn("export CLAUDE_CONFIG_DIR=", text)
        self.assertIn("claude auth login", text)

    def test_a_service_hiccup_keeps_the_numbers_quietly(self):
        from account_switcher.providers import ProviderError
        m = self.manager()
        m.sync_live()
        m.refresh(force=True)
        a = self.by_email(m, "a@example.com")
        before = a.windows()
        self.providers["claude"].fetch = lambda secret, allow_refresh, **_: (_ for _ in ()).throw(
            ProviderError("Usage service unavailable (503)", transient=True))
        for attempt in range(3):
            m.meta["accounts"][a.id]["backoffUntil"] = 0.0
            m.refresh(force=True)
            a = self.by_email(m, "a@example.com")
            self.assertEqual(a.windows(), before)  # last numbers stay
            if attempt < 2:
                self.assertEqual(a.status, "")  # a blip is not worth showing
        self.assertIn("isn't answering", a.status)  # a problem that lasts is
        self.assertGreater(m.meta["accounts"][a.id]["backoffUntil"], time.time() + 200)

    def test_an_expired_saved_login_answered_with_429_is_renewed_not_backed_off(self):
        """Anthropic says 429 (not 401) to an expired token: for a saved login that isn't in use
        here, renew it and ask again, instead of backing off for hours with stale numbers."""
        m = self.manager()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b", expires_in=-60)
        m.sync_live()
        claude_login(self.home, "uuid-a", "a@example.com", "at-a", "rt-a")
        m.sync_live()  # a is in use here; b's saved login has expired
        self.api.uuid_for["rt-b"] = "uuid-b"
        self.api.limited.add("at-b")
        self.api.claude_usage["at-rt-b+"] = claude_usage(35, 10)
        m.refresh(force=True)
        b = self.by_email(m, "b@example.com")
        self.assertEqual(b.status, "")
        self.assertEqual(max(w["used"] for w in b.windows()), 35.0)
        self.assertIn(("/claude/token", "rt-b"), self.api.refreshes)
        self.assertEqual(m.meta["accounts"][b.id].get("backoffUntil", 0.0), 0.0)
        self.assertNotIn(("/claude/token", "rt-a"), self.api.refreshes)  # the login in use here is never renewed

    def test_two_checks_at_once_renew_a_login_only_once(self):
        """Single-use refresh tokens: a second renewal with the same token kills the login."""
        m = self.manager()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b", expires_in=-60)
        m.sync_live()
        claude_login(self.home, "uuid-a", "a@example.com", "at-a", "rt-a")
        m.sync_live()
        self.api.uuid_for["rt-b"] = "uuid-b"
        self.api.claude_usage["at-rt-b+"] = claude_usage(20, 5)
        b = self.by_email(m, "b@example.com").id
        threads = [threading.Thread(target=m.refresh, kwargs={"only": b}) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([r for r in self.api.refreshes if r[1] == "rt-b"], [("/claude/token", "rt-b")])
        self.assertEqual(self.by_email(m, "b@example.com").status, "")

    def test_a_refused_saved_login_is_not_tried_again_until_it_is_replaced(self):
        m = self.manager()
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b", expires_in=-60)
        m.sync_live()
        claude_login(self.home, "uuid-a", "a@example.com", "at-a", "rt-a")
        m.sync_live()  # b is saved, not in use, and its token was renewed elsewhere
        self.api.dead.add("rt-b")
        m.refresh(force=True)
        b = self.by_email(m, "b@example.com")
        self.assertIn("sign in again", b.status.lower())
        tries = len(self.api.refreshes)
        for _ in range(3):  # the scheduled checks leave it alone
            m.meta["accounts"][b.id]["attemptedAt"] = m.meta["accounts"][b.id]["updatedAt"] = 0.0
            m.refresh()
        self.assertEqual(len(self.api.refreshes), tries)
        claude_login(self.home, "uuid-b", "b@example.com", "at-b2", "rt-b2")  # signed in again
        m.sync_live()
        self.assertNotIn("deadLogin", m.meta["accounts"][b.id])

    def test_a_429_for_a_token_of_unknown_age_renews_it_once(self):
        from account_switcher.providers import ProviderError
        claude = self.providers["claude"]
        self.api.uuid_for["rt-q"] = "uuid-q"
        self.api.limited.add("at-q")
        self.api.claude_usage["at-rt-q+"] = claude_usage(12, 3)
        secret = {"credentials": {"claudeAiOauth": {"accessToken": "at-q", "refreshToken": "rt-q"}},
                  "oauthAccount": {"accountUuid": "uuid-q", "emailAddress": "q@example.com"}}
        with self.assertRaises(ProviderError):  # the login in use here: never renewed by the app
            claude.fetch(secret, allow_refresh=False)
        windows, _, updated = claude.fetch(secret, allow_refresh=True)
        self.assertIsNotNone(updated)
        self.assertEqual(max(w["used"] for w in windows), 12.0)
        fresh = {"credentials": {"claudeAiOauth": {"accessToken": "at-q", "refreshToken": "rt-q2",
                                                   "expiresAt": int((time.time() + 3600) * 1000)}}}
        with self.assertRaises(ProviderError):  # a token that is still valid: a real rate limit, no renewal
            claude.fetch(fresh, allow_refresh=True)

    def test_a_renewed_login_is_saved_even_when_the_usage_call_after_it_fails(self):
        from account_switcher.providers import ProviderError
        claude = self.providers["claude"]
        self.api.uuid_for["rt-r"] = "uuid-r"
        self.api.limited.add("at-rt-r+")  # the usage API throttles the new token too
        secret = {"credentials": {"claudeAiOauth": {"accessToken": "at-r", "refreshToken": "rt-r", "expiresAt": 1}},
                  "oauthAccount": {"accountUuid": "uuid-r", "emailAddress": "r@example.com"}}
        saved = []
        with self.assertRaises(ProviderError):
            claude.fetch(secret, allow_refresh=True, save=saved.append)
        self.assertEqual(len(saved), 1)  # the old refresh token is spent: the new one must not be lost
        self.assertEqual(saved[0]["credentials"]["claudeAiOauth"]["accessToken"], "at-rt-r+")

    def test_cloudflare_blocking_the_client_is_not_an_expired_login(self):
        import io
        from unittest import mock
        from urllib.error import HTTPError
        from account_switcher import providers
        body = b'{"title":"Error 1010: Access denied","error_code":1010,"error_name":"browser_signature_banned"}'
        blocked = HTTPError("https://platform.claude.com/v1/oauth/token", 403, "Forbidden", {}, io.BytesIO(body))
        with mock.patch.object(providers, "urlopen", side_effect=blocked):
            with self.assertRaises(providers.ProviderError) as caught:
                providers._http("POST", "https://platform.claude.com/v1/oauth/token", {}, {})
        self.assertTrue(caught.exception.transient)
        self.assertFalse(caught.exception.relogin)

    def test_the_token_request_is_sent_with_claude_codes_user_agent(self):
        from unittest import mock
        from account_switcher import providers
        sent = []

        def fake(method, url, headers, body=None, attempt=0):
            sent.append(headers)
            return 200, {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}
        secret = {"credentials": {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}},
                  "oauthAccount": {"accountUuid": "u", "emailAddress": "x@example.com"}}
        with mock.patch.object(providers, "_http", fake):
            providers.Claude.refresh(self.providers["claude"], secret)
        self.assertTrue(sent[0]["User-Agent"].startswith("claude-cli/"))

    def test_accounts_not_in_use_are_checked_every_few_minutes_even_after_rate_limits(self):
        from account_switcher.live import IDLE_PACE_CAP, PROVIDER_IDLE
        m = self.manager()
        m.sync_live()
        a = self.by_email(m, "a@example.com")
        meta = dict(m.meta["accounts"][a.id], updatedAt=1000.0, attemptedAt=1000.0, pace=8.0, usage=[], status="")
        self.assertEqual(m.due(a.id, meta, False, 1000.0), 1000.0 + PROVIDER_IDLE["claude"] * IDLE_PACE_CAP)
        self.assertLessEqual(PROVIDER_IDLE["claude"] * IDLE_PACE_CAP, 1800)  # never more than half an hour
        again = self.manager()  # a restart starts the pace over
        self.assertEqual(again.meta["accounts"].get(a.id, {}).get("pace", 1.0), 1.0)

    def test_backoff_on_rate_limit(self):
        from account_switcher.providers import ProviderError
        m = self.manager()
        m.sync_live()
        calls = []
        def limited(secret, allow_refresh, **_):
            calls.append(1)
            raise ProviderError("Rate limited by the usage API; retrying automatically", retry_after=60, rate_limited=True)
        self.providers["claude"].fetch = limited
        m.refresh(force=True)
        m.refresh(force=True)
        self.assertEqual(len(calls), 1)  # second call held back by the backoff, even when forced
        a = self.by_email(m, "a@example.com")
        meta = m.meta["accounts"][a.id]
        wait = meta["backoffUntil"] - time.time()
        self.assertTrue(55 <= wait <= 70, wait)  # what Retry-After asked, plus a little jitter
        self.assertEqual(meta["pace"], 2.0)  # and a slower pace from now on
        self.assertIn("Rate limited", a.status)
        self.assertGreaterEqual(m.due(a.id, meta, True, time.time()), meta["backoffUntil"])
        again = self.manager()  # restarting keeps the backoff
        self.assertEqual(again.meta["accounts"][a.id]["backoffUntil"], meta["backoffUntil"])

    def test_a_hiccup_never_blocks_refresh_or_outlives_a_restart(self):
        """Offline at sign-in: the quiet back-off must not keep old numbers (e.g. 0% left after a
        reset) on screen through Refresh or the next start."""
        from account_switcher.providers import ProviderError
        m = self.manager()
        m.sync_live()
        real = self.providers["claude"].fetch
        calls = []
        def offline(secret, allow_refresh, **_):
            calls.append(1)
            raise ProviderError("offline", transient=True)
        self.providers["claude"].fetch = offline
        m.refresh(force=True)
        a = self.by_email(m, "a@example.com")
        self.assertGreater(m.meta["accounts"][a.id]["backoffUntil"], time.time())
        self.assertEqual(m.meta["accounts"][a.id]["backoffKind"], "transient")
        m.refresh(force=True)  # Refresh: tries again at once
        self.assertEqual(len(calls), 2)
        again = self.manager()  # a restart forgets it
        self.assertEqual(again.meta["accounts"][a.id]["backoffUntil"], 0.0)
        self.providers["claude"].fetch = real
        again.sync_live()
        again.refresh(force=True)
        self.assertEqual(self.by_email(again, "a@example.com").status, "")

    def test_rate_limit_without_retry_after_backs_off_exponentially_and_recovers(self):
        from account_switcher.providers import ProviderError
        m = self.manager()
        m.sync_live()
        real = self.providers["claude"].fetch
        self.providers["claude"].fetch = lambda secret, allow_refresh, **_: (_ for _ in ()).throw(
            ProviderError("Rate limited", rate_limited=True))
        m.refresh(force=True)
        a = self.by_email(m, "a@example.com")
        first = m.meta["accounts"][a.id]["backoffUntil"] - time.time()
        self.assertTrue(55 <= first <= 70, first)
        m.meta["accounts"][a.id]["backoffUntil"] = 0.0
        m.refresh(force=True)
        second = m.meta["accounts"][a.id]["backoffUntil"] - time.time()
        self.assertTrue(115 <= second <= 140, second)  # doubled
        self.assertEqual(m.meta["accounts"][a.id]["pace"], 4.0)
        self.providers["claude"].fetch = real
        m.meta["accounts"][a.id]["backoffUntil"] = 0.0
        m.refresh(force=True)
        meta = m.meta["accounts"][a.id]
        self.assertEqual((meta["backoffUntil"], meta["backoffFailures"]), (0.0, 0))
        self.assertLess(meta["pace"], 4.0)  # eases back after a success

    def test_subscription_dates_credits_and_manual_override(self):
        m = self.manager()
        m.sync_live()
        renew = time.time() + 12 * 86400
        self.api.claude_profile["at-a"] = {"organization": {"subscription": {"current_period_end": renew, "cancel_at_period_end": True}}}
        self.api.claude_usage["at-a"] = dict(claude_usage(10, 10), extra_usage={"is_enabled": True, "monthly_limit": 5000, "used_credits": 1250, "utilization": 25})
        self.api.codex_usage["at-x"] = dict(codex_usage(5, 5), credits={"has_credits": True, "unlimited": False, "balance": "12.5"})
        m.refresh(force=True)
        a, x = self.by_email(m, "a@example.com"), self.by_email(m, "x@example.com")
        self.assertEqual(a.subscription["ends"], True)
        self.assertAlmostEqual(a.subscription["at"], renew, delta=1)
        self.assertEqual(a.credits, {"kind": "extra", "enabled": True, "limit": 5000.0, "used": 1250.0, "utilization": 25.0})
        self.assertEqual(x.credits["balance"], 12.5)
        fields = json.loads((self.vault.root / "subscription-fields.json").read_text())
        self.assertIn("organization.subscription.current_period_end", fields["claude"])
        self.assertNotIn(str(int(renew)), json.dumps(fields))  # names only, never values
        m.set_subscription(x.id, renew + 86400, ends=False)
        self.assertEqual(self.by_email(m, "x@example.com").subscription, {"at": renew + 86400, "ends": False, "source": "manual"})
        profile_calls = len([c for c in self.api.calls if c == "/claude/profile"])
        m.refresh(force=True)
        self.assertEqual(len([c for c in self.api.calls if c == "/claude/profile"]), profile_calls)  # once a day

    def test_codex_cancelled_subscription_and_banked_resets(self):
        from account_switcher.providers import banked_resets
        m = self.manager()
        m.sync_live()
        end = time.time() + 9 * 86400
        # Cancellation flag beside the entitlement, as ChatGPT's account check can report it.
        self.api.codex_check["at-x"] = {"accounts": {"acct-x": {
            "entitlement": {"expires_at": end, "has_active_subscription": True},
            "last_active_subscription": {"will_renew": False}}}}
        self.api.codex_usage["at-x"] = dict(codex_usage(5, 5), rate_limit=dict(codex_usage(5, 5)["rate_limit"], available_resets=2))
        m.refresh(force=True)
        x = self.by_email(m, "x@example.com")
        self.assertEqual(x.subscription["ends"], True)
        self.assertAlmostEqual(x.subscription["at"], end, delta=1)
        self.assertEqual(x.credits["resets"], 2)
        self.assertIsNone(banked_resets({"rate_limit": {"reset_at": 1, "limit_window_seconds": 5}}))
        fields = json.loads((self.vault.root / "subscription-fields.json").read_text())
        self.assertIn("rate_limit.available_resets", fields["codex-usage"])

    def test_real_response_shapes(self):
        """Shapes from the user's subscription-fields.json (names only, values made up)."""
        from account_switcher.providers import next_monthly
        m = self.manager()
        m.sync_live()
        start = time.time() - (40 * 86400)
        self.api.claude_profile["at-a"] = {"account": {"has_claude_max": True},
                                          "organization": {"subscription_status": "active", "subscription_created_at": start}}
        self.api.codex_usage["at-x"] = dict(codex_usage(5, 5), email="x@example.com", user_id="u-1",
                                            rate_limit_reset_credits={"available_count": 1, "applicable_available_count": 1})
        self.api.codex_check["at-x"] = {"accounts": {"acct-x": {"entitlement": {"renews_at": None, "cancels_at": time.time() + 8 * 86400,
                                                                               "expires_at": time.time() + 8 * 86400}}}}
        m.refresh(force=True)
        a, x = self.by_email(m, "a@example.com"), self.by_email(m, "x@example.com")
        self.assertEqual(a.subscription["ends"], False)
        self.assertTrue(a.subscription["estimated"])
        self.assertAlmostEqual(a.subscription["at"], next_monthly(start), delta=1)
        self.assertGreater(a.subscription["at"], time.time())
        self.assertLess(a.subscription["at"], time.time() + 32 * 86400)
        self.assertEqual(x.subscription["ends"], True)
        self.assertEqual(x.credits["resets"], 1)
        # A cancelled Claude subscription, picked up because the detection logic changed.
        self.api.claude_profile["at-a"]["organization"]["subscription_status"] = "canceled"
        m.meta["accounts"][a.id]["subscriptionLogic"] = 1
        m.refresh(force=True)
        self.assertEqual(self.by_email(m, "a@example.com").subscription["ends"], True)

    def test_next_monthly_handles_month_ends(self):
        from datetime import datetime, timezone
        from account_switcher.providers import next_monthly
        start = datetime(2026, 1, 31, 12, tzinfo=timezone.utc).timestamp()
        now = datetime(2026, 2, 10, tzinfo=timezone.utc).timestamp()
        self.assertEqual(datetime.fromtimestamp(next_monthly(start, now), timezone.utc).date().isoformat(), "2026-02-28")



class WatchedRefreshTests(unittest.TestCase):
    def test_a_refresh_that_hangs_writes_every_threads_stack_into_the_log(self):
        from account_switcher import live
        with tempfile.TemporaryDirectory() as tmp:
            log = open(Path(tmp) / "app.log", "a+", encoding="utf-8")
            with mock.patch.object(live, "_trace_file", return_value=log):
                with live.watched(0.3):
                    time.sleep(1.0)
            log.flush()
            text = (Path(tmp) / "app.log").read_text()
            log.close()
        self.assertIn("most recent call first", text)  # faulthandler's dump of the stuck threads
        self.assertIn("test_a_refresh_that_hangs", text)

    def test_a_refresh_that_finishes_writes_nothing(self):
        from account_switcher import live
        with tempfile.TemporaryDirectory() as tmp:
            log = open(Path(tmp) / "app.log", "a+", encoding="utf-8")
            with mock.patch.object(live, "_trace_file", return_value=log):
                with live.watched(5):
                    pass
                time.sleep(0.2)
            log.flush()
            self.assertEqual((Path(tmp) / "app.log").read_text(), "")
            log.close()


if __name__ == "__main__":
    unittest.main()
