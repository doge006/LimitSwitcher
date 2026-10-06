"""Codex router (switch, transparent failover, encrypted items), Codex config edits,
the Claude AFK hook and its decisions. Everything runs against local fakes."""
import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
from account_switcher import afk_hook, claude_hooks, codex_config, mod
from account_switcher.codex_proxy import CodexProxy, ThreadState
from account_switcher.integrations import Integrations, RoutedAccounts
from account_switcher.live import JEV_SHOWN, MOVED_RECENTLY, LiveAccounts, LiveGateway
from account_switcher.providers import Claude, Codex
from account_switcher.vault import Vault
from account_switcher.web import Controller, make_server
from tests.test_live import FakeAPI, claude_login, claude_usage, codex_login, codex_usage, point_at_fake

LOCAL = build_opener(ProxyHandler({}))


class FakeChatGPT:
    """Responses endpoint. Encrypted items name the token that produced them; another token
    rejects them like the real service does. Tokens in `limited` are out of quota."""

    def __init__(self):
        self.limited, self.seen = set(), []
        self.usage = {}  # token -> primary used percent reported in response headers
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                token = self.headers.get("Authorization", "")[7:]
                upstream.seen.append({"path": self.path, "token": token, "workspace": self.headers.get("ChatGPT-Account-Id"),
                                      "input": body.get("input", [])})
                if token in upstream.limited:
                    return self.send(429, {"error": {"type": "usage_limit_reached", "resets_at": 1999999999}})
                for item in body.get("input", []):
                    owner = str(item.get("encrypted_content", "")).split(":")[0]
                    if item.get("encrypted_content") and owner != token:
                        return self.send(400, {"error": {"code": "invalid_encrypted_content",
                                                         "message": "The encrypted content could not be verified."}})
                items = body.get("input", [])
                n = len(upstream.seen)
                if any(i.get("type") == "compaction_trigger" for i in items):
                    out = [{"type": "compaction", "encrypted_content": f"{token}:cmp{n}"}]
                elif items and "plain-text handoff" in json.dumps(items[-1]):
                    owned = items[0].get("encrypted_content", "")
                    out = [{"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": f"HANDOFF OF {owned}"}]}]
                else:
                    out = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": f"thought {n}"}],
                            "encrypted_content": f"{token}:r{n}"}]
                events = [{"type": "response.output_item.done", "item": item} for item in out]
                events.append({"type": "response.completed", "response": {"id": "r"}})
                raw = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                if token in upstream.usage:
                    self.send_header("x-codex-primary-used-percent", str(upstream.usage[token]))
                    self.send_header("x-codex-primary-window-minutes", "300")
                    self.send_header("x-codex-primary-reset-at", "1999999999")
                self.end_headers()
                self.wfile.write(raw)

            def send(self, status, body):
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


def post(url, body, headers=None):
    request = Request(url, data=json.dumps(body).encode(), method="POST",
                      headers=dict({"Content-Type": "application/json", "Authorization": "Bearer client-token"}, **(headers or {})))
    try:
        with LOCAL.open(request, timeout=10) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.api, self.upstream = FakeAPI(), FakeChatGPT()
        self.claude, self.codex = Claude(home=self.home), Codex(home=self.home)
        point_at_fake(self.claude, self.codex, self.api)
        self.manager = LiveAccounts(lambda *_: None, Vault(root / "store"), {"claude": self.claude, "codex": self.codex})
        self.manager.spacing = 0
        codex_login(self.home, "acct-y", "y@example.com", "at-y", "rt-y")
        self.manager.sync_live()
        codex_login(self.home, "acct-x", "x@example.com", "at-x", "rt-x")  # x is the login in the file
        self.manager.sync_live()
        self.api.codex_usage.update({"at-x": codex_usage(10, 10), "at-y": codex_usage(20, 20)})
        self.manager.refresh(force=True)
        self.x = self.id_of("x@example.com")
        self.y = self.id_of("y@example.com")
        self.manager.enable_routing("codex")
        self.proxy = CodexProxy(RoutedAccounts(self.manager), port=0, upstream=self.upstream.base)
        self.url = self.proxy.start() + "/responses"

    def tearDown(self):
        self.proxy.close()
        self.upstream.close()
        self.api.close()
        self.tmp.cleanup()

    def id_of(self, email):
        return next(a.id for a in self.manager.accounts() if a.email == email)

    def test_uses_the_chosen_account_everywhere(self):
        status, _ = post(self.url, {"input": []})
        self.assertEqual(status, 200)
        self.assertEqual((self.upstream.seen[-1]["token"], self.upstream.seen[-1]["workspace"]), ("at-x", "acct-x"))
        self.manager.swap(self.y)
        self.assertEqual(self.codex.read_live().email, "y@example.com")  # new windows and /status agree
        post(self.url, {"input": []})
        self.assertEqual((self.upstream.seen[-1]["token"], self.upstream.seen[-1]["workspace"]), ("at-y", "acct-y"))

    def test_usage_limit_is_retried_on_another_account(self):
        self.upstream.limited.add("at-x")
        status, body = post(self.url, {"input": [{"type": "message", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertIn(b"response.completed", body)
        self.assertEqual([s["token"] for s in self.upstream.seen], ["at-x", "at-y"])
        self.assertEqual(self.manager.active["codex"], self.y)
        x = next(a for a in self.manager.accounts() if a.id == self.x)
        self.assertFalse(x.eligible)

    def test_no_account_left_passes_the_limit_through(self):
        self.upstream.limited.update({"at-x", "at-y"})
        status, body = post(self.url, {"input": []})
        self.assertEqual(status, 429)
        self.assertIn(b"usage_limit_reached", body)

    def test_auto_swap_off_does_not_move(self):
        self.manager.meta["autoSwap"] = False
        self.upstream.limited.add("at-x")
        status, _ = post(self.url, {"input": []})
        self.assertEqual(status, 429)
        self.assertEqual(self.manager.active["codex"], self.x)

    def test_encrypted_items_follow_the_account_that_made_them(self):
        _, body = post(self.url, {"input": []})
        produced = next(json.loads(line[5:])["item"] for line in body.split(b"\n")
                        if line.startswith(b"data:") and b"reasoning" in line)
        self.manager.swap(self.y)
        history = [{"type": "message", "content": "hi"}, produced, {"type": "message", "content": "go on"}]
        status, _ = post(self.url, {"input": history})
        self.assertEqual(status, 200)
        sent = self.upstream.seen[-1]
        self.assertEqual(sent["token"], "at-y")
        self.assertNotIn("encrypted_content", json.dumps(sent["input"]))  # nothing y cannot read
        self.assertEqual(len(sent["input"]), 3)  # x's reasoning is there, as its summary

    def test_own_history_goes_through_untouched_and_unparsed(self):
        """The account's own items: the request's bytes go out as they came (no parse, no rewrite)."""
        _, body = post(self.url, {"input": []})
        produced = next(json.loads(line[5:])["item"] for line in body.split(b"\n")
                        if line.startswith(b"data:") and b"reasoning" in line)
        history = {"input": [{"type": "message", "content": "hi"}, produced, {"type": "message", "content": "go on"}]}
        from account_switcher import codex_proxy
        with mock.patch.object(codex_proxy, "json", mock.Mock(wraps=json)) as spy:  # the router's json only
            status, _ = post(self.url, history)
        loads = spy.loads
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.seen[-1]["input"], history["input"])
        requests_parsed = [c for c in loads.call_args_list
                           if c.args and isinstance(c.args[0], bytes) and b'"go on"' in c.args[0]]
        self.assertEqual(requests_parsed, [])  # the request body itself was never parsed

    def test_unknown_encrypted_items_are_dropped_after_a_rejection(self):
        foreign = {"type": "reasoning", "encrypted_content": "someone-else:abc"}
        status, _ = post(self.url, {"input": [foreign, {"type": "message", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.upstream.seen), 2)
        self.assertEqual(self.upstream.seen[-1]["input"], [{"type": "message", "content": "hi"}])

    def test_missing_saved_login_falls_back_to_the_sessions_own(self):
        self.manager.swap(self.y)
        self.manager.active["codex"] = self.x  # routed to an account whose saved login is gone
        self.manager.vault.delete_secret(self.x)
        post(self.url, {"input": []})
        self.assertEqual(self.upstream.seen[-1]["token"], "client-token")

    def test_websocket_and_wrong_secret(self):
        request = Request(self.url, headers={"Upgrade": "websocket", "Connection": "Upgrade"})
        with self.assertRaises(HTTPError) as caught:
            LOCAL.open(request, timeout=5)
        self.assertEqual(caught.exception.code, 426)
        wrong = self.url.replace(self.proxy.secret, "guess")
        status, _ = post(wrong, {"input": []})
        self.assertEqual(status, 403)
        self.assertEqual(self.upstream.seen, [])

    def items_of(self, body):
        return [json.loads(line[5:])["item"] for line in body.split(b"\n")
                if line.startswith(b"data:") and b"output_item.done" in line]

    def wait_for(self, check, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline and not check():
            time.sleep(0.05)
        return check()

    def test_no_extra_requests_and_the_thread_stays_with_its_checkpoints_account(self):
        _, body = post(self.url, {"input": [{"type": "compaction_trigger"}]})
        checkpoint = self.items_of(body)[0]
        self.manager.swap(self.y)
        post(self.url, {"input": [checkpoint, {"type": "message", "role": "user", "content": "go on"}]})
        self.assertEqual(self.upstream.seen[-1]["token"], "at-x")  # x can still read it: stay
        self.assertEqual(self.upstream.seen[-1]["input"][0], checkpoint)
        self.assertEqual(len(self.upstream.seen), 2)  # only Codex's own requests, nothing extra
        self.upstream.limited.add("at-x")  # x used up: the thread moves on without the checkpoint
        status, _ = post(self.url, {"input": [checkpoint, {"type": "message", "role": "user", "content": "go on"}]})
        self.assertEqual((status, self.upstream.seen[-1]["token"]), (200, "at-y"))
        self.assertEqual(self.upstream.seen[-1]["input"], [{"type": "message", "role": "user", "content": "go on"}])

    def test_hidden_reasoning_travels_as_its_summary(self):
        _, body = post(self.url, {"input": []})
        reasoning = self.items_of(body)[0]
        self.manager.swap(self.y)
        post(self.url, {"input": [reasoning, {"type": "message", "role": "user", "content": "go on"}]})
        sent = self.upstream.seen[-1]["input"]
        self.assertEqual(sent[0], {"type": "message", "role": "assistant",
                                   "content": [{"type": "output_text", "text": reasoning["summary"][0]["text"]}]})

    def test_uses_the_account_fully_and_moves_between_turns(self):
        self.upstream.usage["at-x"] = 99
        start = [{"type": "message", "role": "user", "content": "task"}]
        post(self.url, {"input": start})
        post(self.url, {"input": start + [{"type": "message", "role": "user", "content": "next"}]})
        self.assertEqual(self.upstream.seen[-1]["token"], "at-x")  # 99%: keep using it
        self.upstream.usage["at-x"] = 100
        mid_turn = start + [{"type": "function_call", "call_id": "c", "name": "shell", "arguments": "{}"},
                            {"type": "function_call_output", "call_id": "c", "output": "ok"}]
        post(self.url, {"input": mid_turn})  # reports 100%
        post(self.url, {"input": mid_turn + [{"type": "message", "role": "user", "content": "next"}]})
        self.assertEqual(self.upstream.seen[-1]["token"], "at-y")  # used up: the new turn starts on y
        self.assertEqual(self.manager.active["codex"], self.y)

    def test_live_usage_never_adds_windows_the_account_does_not_have(self):
        entry = self.manager.meta["accounts"][self.x]
        entry["usage"] = [w for w in entry["usage"] if w["key"] == "five_hour"]  # no weekly limit
        self.manager.observe(self.x, [(300, 42.0, 1999999999.0), (10080, 0.0, None), (0, 0.0, None)])
        self.assertEqual([(w["key"], w["used"]) for w in entry["usage"]], [("five_hour", 42.0)])

    def test_thread_state_survives_a_restart(self):
        saved = {}
        state = ThreadState(save=saved.update)
        state.remember("enc", "acct")
        state.write_out("enc", "text")
        again = ThreadState(load=lambda: saved)
        self.assertEqual((again.owner("enc"), again.text("enc")), ("acct", "text"))

    def test_quitting_writes_the_chosen_account_into_the_login_file(self):
        self.manager.swap(self.y)
        self.manager.disable_routing("codex")
        self.assertEqual(self.codex.read_live().email, "y@example.com")
        self.assertEqual(self.manager.active["codex"], self.y)

    def test_the_login_files_tokens_are_never_rotated_here(self):
        self.manager.swap(self.y)
        del self.api.codex_usage["at-y"]  # y's token now looks expired to the usage endpoint
        self.manager.refresh(force=True)
        self.assertNotIn(("/codex/token", "rt-y"), self.api.refreshes)  # y is in the file: Codex owns it

    def test_signing_in_elsewhere_is_followed(self):
        self.manager.swap(self.y)
        self.manager.sync_live()
        self.assertEqual(self.manager.active["codex"], self.y)  # routed choice kept
        codex_login(self.home, "acct-z", "z@example.com", "at-z", "rt-z")
        self.manager.sync_live()
        self.assertEqual(self.manager.active["codex"], self.id_of("z@example.com"))


class CodexConfigTests(unittest.TestCase):
    def test_apply_and_restore_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = ('model = "gpt-6"\nopenai_base_url = "https://example.test/v1"\n\n'
                        '[features]\ndaemon_auto_start = true\nother = true\n\n[projects."C:/work"]\ntrust_level = "trusted"\n')
            path = Path(tmp) / "config.toml"
            path.write_text(original)
            codex_config.apply("http://127.0.0.1:1/s/backend-api/codex", tmp)
            if codex_config.tomllib:
                data = codex_config.tomllib.loads(path.read_text())
                self.assertEqual(data["openai_base_url"], "http://127.0.0.1:1/s/backend-api/codex")
                self.assertEqual(data["features"], {"daemon_auto_start": False, "enable_request_compression": False, "other": True})
                self.assertEqual(data["model"], "gpt-6")
                self.assertEqual(data["projects"]["C:/work"]["trust_level"], "trusted")
            codex_config.apply("http://127.0.0.1:2/s/backend-api/codex", tmp)  # re-applying replaces, never stacks
            self.assertEqual(path.read_text().count("openai_base_url = \"http://127.0.0.1"), 1)
            codex_config.restore(tmp)
            self.assertEqual(path.read_text(), original)

    def test_no_config_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_config.apply("http://127.0.0.1:1/s/backend-api/codex", tmp)
            if codex_config.tomllib:
                data = codex_config.tomllib.loads((Path(tmp) / "config.toml").read_text())
                self.assertFalse(data["features"]["daemon_auto_start"])
            codex_config.restore(tmp)
            self.assertEqual((Path(tmp) / "config.toml").read_text().strip(), "")


class ClaudeHookTests(unittest.TestCase):
    def test_install_keeps_other_settings_and_uninstall_removes_only_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            theirs = {"model": "opus", "hooks": {"StopFailure": [{"hooks": [{"type": "command", "command": "notify.sh"}]}],
                                                 "Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}
            path.write_text(json.dumps(theirs))
            claude_hooks.install(Path(tmp) / "state.json", tmp)
            claude_hooks.install(Path(tmp) / "state.json", tmp)  # idempotent
            data = json.loads(path.read_text())
            ours = [h for g in data["hooks"]["StopFailure"] for h in g["hooks"] if claude_hooks.MARK in h["command"]]
            self.assertEqual(len(ours), 1)
            self.assertTrue(ours[0]["asyncRewake"])
            self.assertEqual(data["model"], "opus")
            claude_hooks.uninstall(tmp)
            self.assertEqual(json.loads(path.read_text()), theirs)

    def test_status_line_wraps_the_users_own_and_puts_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, state = Path(tmp) / "settings.json", Path(tmp) / "state.json"
            theirs = {"model": "opus", "statusLine": {"type": "command", "command": "my-line.sh", "padding": 2}}
            path.write_text(json.dumps(theirs))
            self.assertEqual(claude_hooks.install_statusline(state, tmp), "my-line.sh")
            self.assertEqual(claude_hooks.install_statusline(state, tmp), "my-line.sh")  # again: still theirs
            line = json.loads(path.read_text())["statusLine"]
            self.assertIn(claude_hooks.STATUS_MARK, line["command"])
            self.assertEqual(line["padding"], 2)  # their options stay
            self.assertEqual(line["refreshInterval"], claude_hooks.STATUS_REFRESH)  # idle sessions stay current
            claude_hooks.uninstall_statusline(state, tmp)
            self.assertEqual(json.loads(path.read_text()), theirs)
            path.write_text(json.dumps({"model": "opus"}))  # no status line of their own
            self.assertIsNone(claude_hooks.install_statusline(state, tmp))
            claude_hooks.uninstall_statusline(state, tmp)
            self.assertEqual(json.loads(path.read_text()), {"model": "opus"})

    def test_own_status_line_is_found_also_behind_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, state = Path(tmp) / "settings.json", Path(tmp) / "state.json"
            path.write_text(json.dumps({"model": "opus"}))
            self.assertIsNone(claude_hooks.own_statusline(state, tmp))
            claude_hooks.install_statusline(state, tmp)
            self.assertIsNone(claude_hooks.own_statusline(state, tmp))  # only ours
            claude_hooks.uninstall_statusline(state, tmp)
            path.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-line.sh"}}))
            self.assertEqual(claude_hooks.own_statusline(state, tmp)["command"], "my-line.sh")
            claude_hooks.install_statusline(state, tmp)
            self.assertEqual(claude_hooks.own_statusline(state, tmp)["command"], "my-line.sh")

    def test_installed_windows_app_runs_its_scripts_with_the_bundled_python(self):
        # LimitSwitcher.exe always starts the app, so the hook and status line use runtime\python.exe.
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "LimitSwitcher.exe"
            (Path(tmp) / "runtime").mkdir()
            (Path(tmp) / "runtime" / "python.exe").write_text("")
            with mock.patch.object(claude_hooks.sys, "executable", str(exe)), \
                    mock.patch.object(claude_hooks.sys, "platform", "win32"), \
                    mock.patch.object(claude_hooks, "_short", lambda path: str(path).replace("\\", "/")):
                for command in (claude_hooks.hook_command(Path(tmp) / "s.json"),
                                claude_hooks.statusline_command(Path(tmp) / "s.json")):
                    self.assertTrue(command.startswith(str(Path(tmp) / "runtime" / "python.exe").replace("\\", "/")), command)
                    self.assertNotIn("LimitSwitcher.exe", command)

    def test_auto_resume_turns_claude_codes_own_wait_on_and_puts_it_back(self):
        """Off, a usage limit opens a dialog that holds the hook's continue until it is answered."""
        with tempfile.TemporaryDirectory() as tmp:
            path, state = Path(tmp) / "settings.json", Path(tmp) / "state.json"
            for theirs in ({"model": "opus"}, {"model": "opus", "autoContinueAtUsageLimit": False},
                           {"model": "opus", "autoContinueAtUsageLimit": True}):
                path.write_text(json.dumps(theirs))
                claude_hooks.enable_auto_continue(state, tmp)
                claude_hooks.enable_auto_continue(state, tmp)  # again: the backup still holds theirs
                self.assertIs(json.loads(path.read_text())["autoContinueAtUsageLimit"], True)
                claude_hooks.restore_auto_continue(state, tmp)
                self.assertEqual(json.loads(path.read_text()), theirs)
            # Changed by the user meanwhile: theirs stays.
            path.write_text(json.dumps({"model": "opus"}))
            claude_hooks.enable_auto_continue(state, tmp)
            path.write_text(json.dumps({"model": "opus", "autoContinueAtUsageLimit": False}))
            claude_hooks.restore_auto_continue(state, tmp)
            self.assertIs(json.loads(path.read_text())["autoContinueAtUsageLimit"], False)

    def test_invalid_settings_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{ not json")
            with self.assertRaises(ValueError):
                claude_hooks.install(Path(tmp) / "state.json", tmp)
            self.assertEqual(path.read_text(), "{ not json")


class AfkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.api = FakeAPI()
        claude, codex = Claude(home=self.home), Codex(home=self.home)
        point_at_fake(claude, codex, self.api)
        self.claude = claude
        self.gateway = LiveGateway(lambda *_: None, Vault(root / "store"), {"claude": claude, "codex": codex}, background=False)
        self.manager = self.gateway.manager
        self.manager.spacing = 0
        claude_login(self.home, "uuid-b", "b@example.com", "at-b", "rt-b")
        self.manager.sync_live()
        claude_login(self.home, "uuid-a", "a@example.com", "at-a", "rt-a")
        self.manager.sync_live()
        self.api.claude_usage.update({"at-a": claude_usage(100, 50), "at-b": claude_usage(10, 10)})
        self.manager.refresh(force=True)

    def tearDown(self):
        self.api.close()
        self.tmp.cleanup()

    def test_off_means_leave_the_session_alone(self):
        self.manager.meta["autoSwap"] = False
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "stop"})

    def test_switches_and_continues(self):
        self.gateway.set_afk(True)
        answer = self.manager.claude_limit("s1")
        self.assertEqual(answer["action"], "continue")
        self.assertEqual(self.claude.read_live().email, "b@example.com")  # Claude Code reloads this file

    def test_large_session_switches_then_asks_before_continuing(self):
        self.gateway.set_afk(True)
        self.assertEqual(self.manager.claude_limit("s1", 600_000), {"action": "wait", "seconds": 5})
        self.assertEqual(self.claude.read_live().email, "b@example.com")  # Auto swap still moves
        self.assertEqual([p["session"] for p in self.manager.pending_list()], ["s1"])
        self.assertEqual(self.manager.claude_limit("s1", 600_000), {"action": "wait", "seconds": 5})  # still asking
        self.manager.resume_decision("s1", True)
        self.assertEqual(self.manager.claude_limit("s1", 600_000)["action"], "continue")
        self.assertEqual(self.manager.pending_list(), [])

    def test_declined_large_session_is_left_alone(self):
        self.gateway.set_afk(True)
        self.manager.claude_limit("s1", 600_000)
        self.manager.resume_decision("s1", False)
        self.assertEqual(self.manager.claude_limit("s1", 600_000), {"action": "stop"})
        self.assertEqual(self.manager.pending_list(), [])

    def test_a_question_nobody_polls_any_more_is_not_shown(self):
        self.gateway.set_afk(True)
        self.manager.claude_limit("s1", 600_000)
        self.manager.pending_resumes["s1"]["seen"] -= 120  # its Claude Code was closed
        self.assertEqual(self.manager.pending_list(), [])

    def test_near_reset_waits_instead_of_switching(self):
        self.gateway.set_afk(True)
        self.api.claude_usage["at-a"] = claude_usage(100, 50, reset_in=600)
        answer = self.manager.claude_limit("s1")
        self.assertEqual(answer["action"], "wait")
        self.assertEqual(self.claude.read_live().email, "a@example.com")  # no switch
        self.assertEqual(self.manager.auto_swap(), [])  # nor from the background check

    def test_far_reset_still_switches(self):
        self.gateway.set_afk(True)
        self.api.claude_usage["at-a"] = claude_usage(100, 50, reset_in=3600)
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")

    def test_near_reset_switches_when_the_setting_is_off(self):
        self.gateway.set_afk(True)
        self.manager.meta["waitNearReset"] = False
        self.api.claude_usage["at-a"] = claude_usage(100, 50, reset_in=600)
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")

    def test_unknown_size_still_continues(self):
        self.gateway.set_afk(True)
        self.assertEqual(self.manager.claude_limit("s1", None)["action"], "continue")

    def test_large_session_continues_when_the_setting_is_off(self):
        self.gateway.set_afk(True)
        self.manager.meta["afkSkipLarge"] = False
        self.assertEqual(self.manager.claude_limit("s1", 600_000)["action"], "continue")

    def test_one_limit_continues_once(self):
        self.gateway.set_afk(True)
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "stop"})  # reported again: no second wake or switch
        self.assertEqual(self.claude.read_live().email, "b@example.com")

    def test_many_hooks_at_once_continue_only_once(self):
        """Several hooks asking together (waits that end at the same moment) must not all be told
        to continue: that woke Claude Code dozens of times, each turn failing at once."""
        self.gateway.set_afk(True)
        answers = []
        threads = [threading.Thread(target=lambda i=i: answers.append(self.manager.claude_limit(f"s{i % 2}")))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for a in answers if a["action"] == "continue"), 2)  # one per session at most
        self.assertLessEqual(len(self.manager.afk_continues), 2)

    def test_a_second_session_on_the_same_account_goes_on_with_the_first(self):
        """Two sessions hit the limit of the account they share: the first moves the app to b, the
        second (its turn went out on a) goes on there too, without a second switch or a wait."""
        self.gateway.set_afk(True)
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")
        self.assertEqual(self.manager.claude_limit("s2")["action"], "continue")
        self.assertEqual(self.claude.read_live().email, "b@example.com")
        b = next(a for a in self.manager.accounts() if a.email == "b@example.com")
        self.assertTrue(b.eligible)  # not taken for used up

    def test_a_limit_after_the_background_check_switched_goes_on(self):
        self.gateway.set_afk(True)
        self.assertEqual(len(self.manager.auto_swap()), 1)  # a is used up: the background check moves first
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")
        self.assertEqual(self.claude.read_live().email, "b@example.com")

    def test_a_limit_long_after_a_switch_is_the_new_accounts_own(self):
        self.gateway.set_afk(True)
        self.manager.auto_swap()
        account, at = self.manager.auto_moved["claude"]
        self.manager.auto_moved["claude"] = (account, at - MOVED_RECENTLY - 1)
        self.api.claude_usage["at-b"] = claude_usage(100, 10, reset_in=3600)
        self.assertEqual(self.manager.claude_limit("s1")["action"], "wait")  # b is used up now too: wait for a reset

    def test_a_continued_turn_that_fails_at_once_is_not_continued_again(self):
        self.gateway.set_afk(True)
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")
        self.manager.afk_sessions["s1"]["continues"][-1] -= 60  # a minute later, the new account hits a limit too
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "stop"})

    def test_no_continue_onto_an_account_that_only_looks_free(self):
        """b's numbers are old and say it has room, but it is used up (on another computer):
        checked first, so the session waits instead of continuing into another limit."""
        self.gateway.set_afk(True)
        b = next(i for i, m in self.manager.meta["accounts"].items() if m.get("email") == "b@example.com")
        self.manager.meta["accounts"][b]["updatedAt"] = time.time() - 3600
        self.api.claude_usage["at-b"] = claude_usage(100, 10, reset_in=1800)
        self.assertEqual(self.manager.claude_limit("s1")["action"], "wait")
        self.assertEqual(self.claude.read_live().email, "a@example.com")  # no pointless switch

    def test_a_limit_marks_the_account_used_up_when_the_api_cannot_say(self):
        self.manager.meta["autoSwap"] = False
        self.gateway.set_afk(True)
        current = self.manager.active["claude"]
        self.manager.meta["accounts"][current]["backoffUntil"] = time.time() + 600  # rate limited
        self.manager.claude_limit("s1")
        account = next(a for a in self.manager.accounts() if a.id == current)
        self.assertFalse(account.eligible)
        self.assertEqual(max(w["used"] for w in account.windows()), 100.0)

    def test_continues_on_an_account_whose_limit_reset_while_the_api_is_rate_limited(self):
        # a ran out while b was already used up; b's window has reset since, but the usage API
        # answers 429 now: the reset alone says b has room again.
        self.gateway.set_afk(True)
        m = self.manager
        b = next(a.id for a in m.accounts() if a.email == "b@example.com")
        now = time.time()
        with m.lock:
            m.meta["accounts"][b].update(
                usage=[{"key": "five_hour", "label": "5-hour", "used": 100.0, "resetsAt": now - 60, "scope": "account"},
                       {"key": "weekly", "label": "Weekly", "used": 40.0, "resetsAt": now + 86400, "scope": "account"}],
                updatedAt=now - 3600, backoffUntil=now + 600, status="Rate limited by Claude · retrying at 18:37")
        self.assertEqual(m.claude_limit("s1")["action"], "continue")
        self.assertEqual(self.claude.read_live().email, "b@example.com")

    def test_waits_for_a_reset_when_no_account_has_room(self):
        self.gateway.set_afk(True)
        self.api.claude_usage["at-b"] = claude_usage(100, 10, reset_in=1800)
        self.manager.refresh(force=True)
        answer = self.manager.claude_limit("s1")
        self.assertEqual(answer["action"], "wait")
        self.assertTrue(60 <= answer["seconds"] <= 3700)
        self.api.claude_usage["at-a"] = claude_usage(5, 50)  # the window reset
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")

    def test_endpoint_needs_the_hook_token(self):
        self.gateway.set_afk(True)
        controller = Controller(gateway=lambda notify: self.gateway)
        server = make_server(controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            status, _ = post(server.hook_url, {"provider": "claude", "session": "s"})
            self.assertEqual(status, 403)
            status, body = post(server.hook_url, {"provider": "claude", "session": "s"},
                                {"Authorization": "Bearer " + server.hook_token})
            self.assertEqual((status, json.loads(body)["action"]), (200, "continue"))
        finally:
            server.shutdown()
            server.server_close()

    def test_limits_lists_only_claude_accounts_the_one_in_use_first(self):
        codex_login(self.home, "acct-x", "x@example.com", "at-x", "rt-x")
        self.manager.sync_live()
        controller = Controller(gateway=lambda notify: self.gateway)
        controller.live = True
        controller.statusline({"session": "s", "model": "Opus 5.5", "effort": "high"})  # the status line's model
        text = controller.limits_text({"session": "s", "model": "ignored", "context": {"tokens": 183_000, "percent": 18}})
        lines = text.split("\n")
        self.assertEqual(lines[0], "**⇄ a@example.com** · Opus 5.5 (high)")  # no windows of their own: no "main"
        self.assertTrue(lines[4].startswith("| 🔴 **5h** | `░░░░░░░░░░` | 0% left · ↻"), lines[4])  # used up: when it resets
        self.assertEqual(lines[5], "| 🟢 **1w** | `█████░░░░░` | 50% left |")
        self.assertEqual(lines[6], "| 🟢 **ctx** | `████████░░` | 82% left · 183k |")
        self.assertEqual(lines[-1], "- 🟢 b@example.com · 5h **90%** · 1w **90%**")
        self.assertNotIn("x@example.com", text)  # Codex accounts aren't Claude's
        self.assertNotIn("Jev", text)
        # A session whose status line never reported: the mod's own model, no effort
        self.assertTrue(controller.limits_text({"session": "t", "model": "Sonnet 5.5"}).startswith("**⇄ a@example.com** · Sonnet 5.5\n"))

    def test_limits_shows_what_jev_did_for_the_session(self):
        controller = Controller(gateway=lambda notify: self.gateway)
        controller.live = True
        self.manager.compactions["s"] = {"id": "j1", "status": "running", "at": time.time()}
        self.assertIn("\n\n⏳ Jev compacting…\n", controller.limits_text({"session": "s"}))
        self.manager.compactions["s"].update(status="done", saved=56_400, finishedAt=time.time() - 120)
        self.assertIn("\n\n🗜 Jev saved ~56k before the last swap (2m ago)\n", controller.limits_text({"session": "s"}))
        self.assertNotIn("Jev", controller.limits_text({"session": "other"}))  # another session's compaction

    def test_limits_endpoint_needs_the_hook_token(self):
        controller = Controller(gateway=lambda notify: self.gateway)
        controller.live = True
        server = make_server(controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = server.hook_url.replace("/api/afk", "/api/limits")
        try:
            self.assertEqual(post(url, {"session": "s"})[0], 403)
            status, body = post(url, {"session": "s"}, {"Authorization": "Bearer " + server.hook_token})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["text"].startswith("**⇄ a@example.com**"))
        finally:
            server.shutdown()
            server.server_close()

    def test_status_line_script_reports_live_usage(self):
        from account_switcher import statusline
        controller = Controller(gateway=lambda notify: self.gateway)
        server = make_server(controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.manager.live_since["claude"] = 0
        self.manager.meta["modSeenAt"] = time.time()  # the mod is in use: the line shows in Claude Code
        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({"url": server.hook_url, "token": server.hook_token, "statusline": None}))
        event = {"session_id": "s", "rate_limits": {"five_hour": {"used_percentage": 40, "resets_at": time.time() + 600}}}
        try:
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(event).encode()))), \
                    mock.patch.object(sys, "stdout", out):
                self.assertEqual(statusline.main(["statusline", str(state)]), 0)
            import re
            self.assertIn("a@example.com · 5h 60% left", re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue()))  # coloured
            self.assertIn("\x1b[94m5h\x1b[0m \x1b[92m60%\x1b[0m\x1b[90m left\x1b[0m", out.getvalue())  # 5h blue, plenty left green
            with_model = dict(event, model={"id": "claude-opus-5-5", "display_name": "Opus 5.5"}, effort={"level": "high"},
                              context_window={"total_input_tokens": 183_000, "remaining_percentage": 82})
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(with_model).encode()))), \
                    mock.patch.object(sys, "stdout", out):
                statusline.main(["statusline", str(state)])
            self.assertIn("⇄ LimitSwitcher · a@example.com · Opus 5.5 (high) · 5h 60% left",
                          re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue()))  # name · model (effort) · usage
            self.assertIn("ctx 183k/82% left", re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue()))
            five = next(w for w in self.manager.accounts() if w.email == "a@example.com").windows()[0]
            self.assertEqual(five["used"], 40.0)
            state.write_text(json.dumps({"url": server.hook_url, "token": "wrong", "statusline": None}))
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(event).encode()))), \
                    mock.patch.object(sys, "stdout", out):
                statusline.main(["statusline", str(state)])
            self.assertNotIn("LimitSwitcher", out.getvalue())  # a wrong token gets no app line...
            self.assertIn("ctx 183k/82% left", re.sub(r"\x1b\[[0-9;]*m", "", out.getvalue()))  # ...but the context stays (the last figures)
        finally:
            server.shutdown()
            server.server_close()

    def test_hook_script_wakes_claude_on_continue(self):
        answers = [{"action": "wait", "seconds": 1}, {"action": "continue", "message": "go on"}]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                raw = json.dumps(answers.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/api/afk", "token": "t"}))
        try:
            stderr = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"error": "rate_limit", "session_id": "s"}))), \
                    mock.patch.object(sys, "stderr", stderr), mock.patch.object(afk_hook.time, "sleep"):
                self.assertEqual(afk_hook.main(["hook", str(state)]), 2)
            self.assertEqual(stderr.getvalue(), "go on")
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"error": "server_error"}))):
                self.assertEqual(afk_hook.main(["hook", str(state)]), 0)  # other failures: ignored
            state.unlink()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"error": "rate_limit"}))):
                self.assertEqual(afk_hook.main(["hook", str(state)]), 0)  # app not running
        finally:
            server.shutdown()
            server.server_close()


class ClaudeFullUseTests(AfkTests):
    def test_keeps_using_an_account_until_its_limit(self):
        self.api.claude_usage["at-a"] = claude_usage(99, 50)
        self.manager.refresh(force=True)
        self.assertEqual(self.manager.auto_swap(), [])
        self.assertEqual(self.claude.read_live().email, "a@example.com")

    def test_auto_swap_without_afk_switches_but_does_not_continue(self):
        self.manager.meta["autoSwap"] = True
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "stop"})
        self.assertEqual(self.claude.read_live().email, "b@example.com")  # the next message uses b

    def test_neither_on_leaves_everything_alone(self):
        self.manager.meta["autoSwap"] = False
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "stop"})
        self.assertEqual(self.claude.read_live().email, "a@example.com")


class JevKeyFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state_file = Path(self.dir.name) / "afk-hook.json"
        self.root = Path(self.dir.name) / "claude"
        env = mock.patch.dict(os.environ)
        env.start()
        os.environ.pop("OPENROUTER_API_KEY", None)
        self.addCleanup(env.stop)

    def source(self):
        return mod.jev_key_source(self.state_file, self.root)

    def test_saved_replaced_and_removed_keeping_other_lines(self):
        env = mod.env_file(self.state_file)
        env.write_text("OTHER=1\nOPENROUTER_API_KEY=old\n", encoding="utf-8")
        mod.set_jev_key(self.state_file, " 'sk-or-new' ")
        self.assertEqual(env.read_text(encoding="utf-8"), "OTHER=1\nOPENROUTER_API_KEY=sk-or-new\n")
        self.assertEqual(self.source(), "file")
        if sys.platform != "win32":
            self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        mod.set_jev_key(self.state_file, None)
        self.assertEqual(env.read_text(encoding="utf-8"), "OTHER=1\n")
        self.assertIsNone(self.source())
        env.write_text("OPENROUTER_API_KEY=x\n", encoding="utf-8")
        mod.set_jev_key(self.state_file, None)
        self.assertFalse(env.exists())

    def test_nonsense_is_refused(self):
        for bad in ("", "two words", "a" * 400):
            with self.assertRaises(ValueError):
                mod.set_jev_key(self.state_file, bad)
        self.assertIsNone(self.source())

    def test_the_environment_wins_like_the_mod_does(self):
        mod.set_jev_key(self.state_file, "from-file")
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "from-env"}):
            self.assertEqual(self.source(), "env")


class JevCompactionTests(unittest.TestCase):
    """After a limit the account is swapped at once; with Jev compaction on, the session goes on
    only once its mod has compacted it (or given up), so the new account loads less."""
    setUp = AfkTests.setUp
    tearDown = AfkTests.tearDown

    def ready(self, afk=True):
        self.gateway.set_afk(afk)
        self.manager.meta.update(jevCompact=True, jevModInstalled=True)
        self.manager.note_mod_session("s1")
        key = mock.patch("account_switcher.mod.jev_key_present", return_value=True)
        key.start()
        self.addCleanup(key.stop)

    def test_swaps_at_once_then_waits_for_the_compaction_then_continues(self):
        self.ready()
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "wait", "seconds": 3})
        self.assertEqual(self.claude.read_live().email, "b@example.com")  # swapped already
        request = self.manager.compaction_request("s1")
        self.assertIsNotNone(request)
        self.assertIsNone(self.manager.compaction_request("other"))
        self.assertTrue(self.manager.compacting("s1"))
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "wait", "seconds": 3})  # still compacting
        self.assertFalse(self.manager.compaction_done("s1", "wrong-id", "done", 1000))
        self.assertTrue(self.manager.compaction_done("s1", request["id"], "done", 120_000))
        self.assertFalse(self.manager.compacting("s1"))
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "wait", "seconds": 1, "restamp": True})
        answer = self.manager.claude_limit("s1")
        self.assertEqual(answer["action"], "continue")
        self.assertIn("Re-run the tool before relying on exact details", answer["message"])  # told once, after a compaction
        self.assertEqual(self.claude.read_live().email, "b@example.com")  # no second swap

    def test_off_or_not_ready_continues_at_once(self):
        self.gateway.set_afk(True)
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")  # the setting is off

    def test_an_old_session_without_the_mod_is_not_waited_for(self):
        self.ready()
        self.manager.mod_sessions["s1"] -= 600
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")

    def test_no_key_is_not_waited_for(self):
        self.ready()
        with mock.patch("account_switcher.mod.jev_key_present", return_value=False):
            self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")

    def test_the_mod_missing_is_not_waited_for(self):
        self.ready()
        self.manager.meta["jevModInstalled"] = False
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")

    def test_a_failed_compaction_still_continues(self):
        self.ready()
        self.manager.claude_limit("s1")
        request = self.manager.compaction_request("s1")
        self.manager.compaction_done("s1", request["id"], "failed", reason="Jev failed: 503")
        self.assertEqual(self.manager.claude_limit("s1")["action"], "wait")  # restamp
        answer = self.manager.claude_limit("s1")
        self.assertEqual(answer["action"], "continue")
        self.assertNotIn("shortened", answer["message"])  # nothing was shortened

    def test_a_compaction_that_never_reports_is_given_up_on(self):
        self.ready()
        self.manager.claude_limit("s1")
        self.manager.compactions["s1"]["at"] -= 300
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "wait", "seconds": 1, "restamp": True})
        self.assertEqual(self.manager.claude_limit("s1")["action"], "continue")
        self.assertEqual(self.manager.compactions["s1"]["status"], "failed")

    def test_compacted_below_the_large_size_needs_no_question(self):
        self.ready()
        self.manager.claude_limit("s1", 600_000)
        request = self.manager.compaction_request("s1")
        self.assertIsNone(self.manager.compacted_context("s1"))  # not done yet
        self.manager.compaction_done("s1", request["id"], "done", 300_000)
        self.assertEqual(self.manager.compacted_context("s1")["saved"], 300_000)  # the status line takes it off the ctx until the next reply
        self.assertIsNone(self.manager.compacted_context("s2"))
        self.manager.claude_limit("s1", 600_000)  # restamp
        self.assertEqual(self.manager.claude_limit("s1", 600_000)["action"], "continue")
        self.assertEqual(self.manager.pending_list(), [])

    def test_still_large_after_compacting_asks_as_before(self):
        self.ready()
        self.manager.claude_limit("s1", 900_000)
        request = self.manager.compaction_request("s1")
        self.manager.compaction_done("s1", request["id"], "done", 100_000)
        self.manager.claude_limit("s1", 900_000)  # restamp
        self.assertEqual(self.manager.claude_limit("s1", 900_000), {"action": "wait", "seconds": 5})
        self.assertEqual([p["session"] for p in self.manager.pending_list()], ["s1"])

    def test_auto_swap_alone_compacts_then_leaves_the_next_message_to_the_user(self):
        self.ready(afk=False)
        self.assertEqual(self.manager.claude_limit("s1")["action"], "wait")
        request = self.manager.compaction_request("s1")
        self.manager.compaction_done("s1", request["id"], "done", 50_000)
        self.manager.claude_limit("s1")  # restamp
        self.assertEqual(self.manager.claude_limit("s1"), {"action": "stop"})

    def test_not_compacted_again_right_away(self):
        self.ready()
        self.manager.claude_limit("s1")
        request = self.manager.compaction_request("s1")
        self.manager.compaction_done("s1", request["id"], "done", 50_000)
        self.manager.claude_limit("s1")
        self.manager.claude_limit("s1")
        self.assertFalse(self.manager.jev_wanted("s1", time.time()))

    def test_the_status_line_says_it_is_compacting_and_shows_model_and_effort(self):
        self.ready()
        self.manager.claude_limit("s1")
        line = self.manager.statusline(None, "s1", model="Opus 5.5", effort="high")
        self.assertIn("b@example.com · Opus 5.5 (high) · ", line)
        self.assertTrue(str(line).startswith("⇄ LimitSwitcher · Jev Compacting… · b@example.com"))  # right after the app's name
        self.assertEqual(line.parts[0], {"t": "⇄", "c": "warn"})                                   # the icon turns yellow
        self.assertIn({"t": "Jev Compacting…", "c": "warn"}, line.parts)
        idle = self.manager.statusline(None, "s2")
        self.assertNotIn("Jev Compacting", idle)
        self.assertEqual(idle.parts[0], {"t": "⇄", "c": "good"})

    def test_a_compaction_by_hand_shows_in_the_status_line_too(self):
        """/jevcompact: the mod says it started, the line says so, then what it saved; nothing waits for it."""
        self.ready()
        self.assertTrue(self.manager.compaction_started("s1", "hand-1"))
        line = self.manager.statusline(None, "s1")
        self.assertTrue(str(line).startswith("⇄ LimitSwitcher · Jev Compacting… · "))
        self.assertEqual(line.parts[0], {"t": "⇄", "c": "warn"})
        self.assertTrue(self.manager.compaction_done("s1", "hand-1", "done", 148_000))
        line = self.manager.statusline(None, "s1")
        self.assertIn("⇄ LimitSwitcher · Jev saved ~148k · ", line)
        self.assertEqual(line.parts[0], {"t": "⇄", "c": "good"})
        self.assertEqual(self.manager.compacted_context("s1")["saved"], 148_000)
        self.assertNotEqual(self.manager.claude_limit("s1")["action"], "wait")  # a limit later is not held for it

    def test_a_compaction_by_hand_does_not_replace_one_before_a_swap(self):
        self.ready()
        self.manager.claude_limit("s1")
        self.assertFalse(self.manager.compaction_started("s1", "hand-1"))
        self.assertIsNotNone(self.manager.compaction_request("s1"))

    def test_the_status_line_says_what_a_finished_compaction_saved_for_a_while(self):
        self.ready()
        self.manager.claude_limit("s1")
        request = self.manager.compaction_request("s1")
        self.assertTrue(self.manager.compaction_done("s1", request["id"], "done", 120_000))
        line = self.manager.statusline(None, "s1")
        self.assertIn("LimitSwitcher · Jev saved ~120k · b@example.com", line)
        self.assertEqual(line.parts[0], {"t": "⇄", "c": "good"})  # the icon is the normal green one
        self.assertIn({"t": "~120k", "c": "good"}, line.parts)
        self.assertNotIn("Jev saved", self.manager.statusline(None, "s2"))  # only that session's line
        self.manager.compactions["s1"]["finishedAt"] -= JEV_SHOWN + 1
        self.assertNotIn("Jev saved", self.manager.statusline(None, "s1"))

    def test_end_to_end_through_the_local_api_and_the_hook(self):
        """The hook asks, the mod picks the compaction up from its status line report and reports
        back, the transcript changes, and the hook still wakes the session (restamp)."""
        self.ready()
        controller = Controller(gateway=lambda notify: self.gateway)
        server = make_server(controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        state = Path(self.tmp.name) / "state.json"
        state.write_text(json.dumps({"url": server.hook_url, "token": server.hook_token, "statusline": None}))
        transcript = Path(self.tmp.name) / "t.jsonl"
        transcript.write_text('{"message": {"usage": {"input_tokens": 1000}}}\n')
        base = server.hook_url[: -len("/api/afk")]

        def post(path, body):
            from urllib.request import ProxyHandler, Request, build_opener
            request = Request(base + path, data=json.dumps(body).encode(), method="POST",
                              headers={"Authorization": "Bearer " + server.hook_token, "Content-Type": "application/json",
                                       "Host": server.expected_host})
            with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
                return json.load(response)

        compacted = threading.Event()

        def mod():  # what limit-status and jev-compact do in the session
            for _ in range(200):
                answer = post("/api/statusline", {"session": "s1", "source": "mod", "rate_limits": None})
                if answer.get("compact"):
                    transcript.write_text('{"compacted": true}\n')  # the compaction rewrote it
                    post("/api/compaction", {"session": "s1", "id": answer["compact"]["id"], "outcome": "done", "saved": 40_000})
                    compacted.set()
                    return
                time.sleep(0.02)

        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            if not compacted.is_set() and len(sleeps) == 2:
                mod()

        try:
            stderr = io.StringIO()
            event = {"error": "rate_limit", "session_id": "s1", "transcript_path": str(transcript)}
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
                    mock.patch.object(sys, "stderr", stderr), mock.patch.object(afk_hook.time, "sleep", side_effect=fake_sleep), \
                    mock.patch.object(afk_hook, "owner", return_value=(None, None)):
                self.assertEqual(afk_hook.main(["hook", str(state)]), 2)  # woken despite the rewrite
            self.assertTrue(compacted.is_set())
            self.assertIn("Continue exactly where you left off", stderr.getvalue())
            self.assertEqual(self.claude.read_live().email, "b@example.com")
        finally:
            server.shutdown()
            server.server_close()
            controller.close()


@unittest.skipIf(sys.platform == "win32", "stand-in CLI is a POSIX shell script")
class CodexServerWatchTests(unittest.TestCase):
    def test_stops_the_shared_server_only_when_idle(self):
        from account_switcher.integrations import CodexServerWatch
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "app-server-control").mkdir()
            (home / "app-server-control" / "app-server-control.sock").write_text("")
            day = home / "sessions" / "2026" / "09" / "26"
            day.mkdir(parents=True)
            log = day / "rollout.jsonl"
            log.write_text("{}")
            calls = home / "calls"
            cli = home / "codex"
            cli.write_text(f"#!/bin/sh\necho \"$*\" >> '{calls}'\n")
            cli.chmod(0o755)
            watch = CodexServerWatch(home, cli=str(cli))
            self.assertFalse(watch.check())  # a session is active
            os.utime(log, (time.time() - 600, time.time() - 600))
            self.assertTrue(watch.check())
            self.assertEqual(calls.read_text().split(), ["app-server", "daemon", "stop"])
            self.assertFalse(watch.check())  # the same (stale) socket is not stopped twice

    def test_restarts_the_plugins_old_broker_once_idle_and_mentions_old_sessions(self):
        from account_switcher import integrations, processes
        from account_switcher.processes import Process
        now = time.time()
        found = [Process(10, 1, "node", now - 600, "node C:\\x\\app-server-broker.mjs serve --endpoint pipe"),
                 Process(11, 10, "codex", now - 600, "codex app-server"),
                 Process(12, 1, "codex", now - 600, "C:\\bin\\codex.exe"),
                 Process(13, 1, "node", now - 5, "node /x/app-server-broker.mjs serve --endpoint e"),  # after the router
                 Process(14, 1, "codex", now - 600, "codex login")]
        notes, ended = [], []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(integrations, "codex_home_path", lambda home=None: Path(tmp)), \
                mock.patch.object(processes, "listing", lambda names: found), \
                mock.patch.object(processes, "end_tree", ended.append):
            day = Path(tmp) / "sessions" / "2026" / "09" / "27"
            day.mkdir(parents=True)
            log = day / "rollout.jsonl"
            log.write_text("{}")
            watch = integrations.CodexServerWatch(tmp, lambda kind, text: notes.append(text), routed_since=now - 60)
            self.assertEqual([(p.pid, kind) for p, kind in watch.older_codex()], [(10, "broker"), (12, "session")])
            self.assertFalse(watch.check_older())  # a job is running: leave it
            self.assertEqual(ended, [])
            os.utime(log, (now - 600, now - 600))
            self.assertTrue(watch.check_older())
            self.assertEqual(ended, [10])
            self.assertEqual(sum("before LimitSwitcher, doesn't go through it" in n for n in notes), 1)  # said once
        # VS Code's own Codex, started before the router: named, mentioned once, never ended.
        editor = [Process(20, 21, "codex", now - 600, "codex.exe app-server --analytics-default-enabled")]
        tree = {21: (22, "code"), 22: (1, "explorer")}
        notes, ended = [], []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(integrations, "codex_home_path", lambda home=None: Path(tmp)), \
                mock.patch.object(processes, "listing", lambda names: editor), \
                mock.patch.object(processes, "family", lambda: tree), \
                mock.patch.object(processes, "end_tree", ended.append):
            watch = integrations.CodexServerWatch(tmp, lambda kind, text: notes.append(text), routed_since=now - 60)
            watch.check_older(quiet=0)
            watch.check_older(quiet=0)
            self.assertEqual(ended, [])
            self.assertEqual(len(notes), 1)
            self.assertIn("Codex in VS Code started", notes[0])
        with mock.patch.object(processes, "listing", lambda names: found):
            other = integrations.CodexServerWatch("/somewhere/else", routed_since=now)
            self.assertEqual(other.older_codex(), [])  # another Codex folder: not ours to judge


class IntegrationTests(unittest.TestCase):
    def test_start_and_stop_leave_codex_and_claude_as_they_were(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home, claude_root = root / "codex", root / "claude"
            codex_home.mkdir()
            claude_root.mkdir()
            (codex_home / "config.toml").write_text('model = "m"\n')
            (claude_root / "settings.json").write_text('{"model": "opus"}')
            gateway = LiveGateway(lambda *_: None, Vault(root / "store"),
                                  {"claude": Claude(config_dir=claude_root, home=root), "codex": Codex(codex_home=codex_home)},
                                  background=False)
            gateway.manager.meta.update(afk=True, startWithWindows=False, modSeenAt=time.time())
            integrations = Integrations(gateway, "http://127.0.0.1:1/api/afk", "t", codex_home=codex_home,
                                        claude_root=claude_root, upstream="http://127.0.0.1:9")
            with mock.patch("account_switcher.codex_proxy.DEFAULT_PORT", 0), \
                    mock.patch("account_switcher.integrations.DEFAULT_PORT", 0):
                integrations.start()
            try:
                self.assertIn("openai_base_url", (codex_home / "config.toml").read_text())
                self.assertIn(claude_hooks.MARK, (claude_root / "settings.json").read_text())
                self.assertIn(claude_hooks.STATUS_MARK, (claude_root / "settings.json").read_text())
                self.assertTrue(integrations.state_file.exists())
            finally:
                integrations.stop()
            self.assertEqual((codex_home / "config.toml").read_text(), 'model = "m"\n')
            self.assertEqual(json.loads((claude_root / "settings.json").read_text()), {"model": "opus"})
            self.assertFalse(integrations.state_file.exists())


    def test_status_line_off_leaves_claude_code_alone_unless_the_user_has_one(self):
        # Off (the default) and no status line of their own: ours would only add an empty line.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_root = root / "claude"
            claude_root.mkdir()
            (claude_root / "settings.json").write_text('{"model": "opus"}')
            gateway = LiveGateway(lambda *_: None, Vault(root / "store"), {"claude": Claude(config_dir=claude_root, home=root)},
                                  background=False)
            gateway.manager.meta.update(startWithWindows=False)
            integrations = Integrations(gateway, "http://127.0.0.1:1/api/afk", "t", codex_home=root / "no-codex",
                                        claude_root=claude_root)
            with mock.patch("account_switcher.integrations.codex_present", return_value=False):
                integrations.start()
            try:
                self.assertFalse(claude_hooks.statusline_installed(claude_root))
                gateway.manager.meta["modSeenAt"] = time.time()  # the mod is in use
                integrations.apply_afk()
                self.assertTrue(claude_hooks.statusline_installed(claude_root))
                gateway.manager.meta["modSeenAt"] = 0
                integrations.apply_afk()
                self.assertNotIn("statusLine", json.loads((claude_root / "settings.json").read_text()))
                # Their own status line: ours runs it (for live usage), their line unchanged.
                (claude_root / "settings.json").write_text('{"statusLine": {"type": "command", "command": "mine.sh"}}')
                integrations.apply_afk()
                self.assertTrue(claude_hooks.statusline_installed(claude_root))
            finally:
                integrations.stop()
            self.assertEqual(json.loads((claude_root / "settings.json").read_text())["statusLine"]["command"], "mine.sh")

    def test_status_line_command_is_installed_while_the_mod_is_in_use(self):
        """The mod feeds the usage; the line is the status line's (a spot nobody can dismiss):
        installing the mod is the choice to see it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_root = root / "claude"
            claude_root.mkdir()
            gateway = LiveGateway(lambda *_: None, Vault(root / "store"), {"claude": Claude(config_dir=claude_root, home=root)},
                                  background=False)
            gateway.manager.meta.update(startWithWindows=False)
            integrations = Integrations(gateway, "http://127.0.0.1:1/api/afk", "t", codex_home=root / "no-codex",
                                        claude_root=claude_root)
            with mock.patch("account_switcher.integrations.codex_present", return_value=False):
                integrations.start()
            try:
                self.assertFalse(claude_hooks.statusline_installed(claude_root))  # no mod
                gateway.manager.meta["modSeenAt"] = time.time()
                integrations.apply_afk()
                self.assertTrue(claude_hooks.statusline_installed(claude_root))
                gateway.manager.meta["modSeenAt"] = time.time() - 15 * 24 * 3600  # long gone
                integrations.apply_afk()
                self.assertFalse(claude_hooks.statusline_installed(claude_root))
            finally:
                integrations.stop()

    def test_status_line_comes_back_when_settings_are_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_root = root / "claude"
            claude_root.mkdir()
            (claude_root / "settings.json").write_text('{"model": "opus"}')
            gateway = LiveGateway(lambda *_: None, Vault(root / "store"), {"claude": Claude(config_dir=claude_root, home=root)},
                                  background=False)
            gateway.manager.meta.update(startWithWindows=False, modSeenAt=time.time())
            integrations = Integrations(gateway, "http://127.0.0.1:1/api/afk", "t", codex_home=root / "no-codex",
                                        claude_root=claude_root)
            integrations.SETTINGS_EVERY = 0.05
            with mock.patch("account_switcher.integrations.codex_present", return_value=False):
                integrations.start()
            try:
                self.assertTrue(claude_hooks.statusline_installed(claude_root))
                time.sleep(0.2)
                (claude_root / "settings.json").write_text('{"model": "sonnet"}')  # e.g. Claude Code rewriting it
                deadline = time.time() + 5
                while time.time() < deadline and not claude_hooks.statusline_installed(claude_root):
                    time.sleep(0.05)
                self.assertTrue(claude_hooks.statusline_installed(claude_root))
                self.assertEqual(json.loads((claude_root / "settings.json").read_text())["model"], "sonnet")
            finally:
                integrations.stop()
            self.assertFalse(claude_hooks.statusline_installed(claude_root))


if __name__ == "__main__":
    unittest.main()


class StatusLineMarkerTests(unittest.TestCase):
    def test_the_line_is_coloured_by_how_much_is_left(self):
        from account_switcher import statusline
        from account_switcher.live import StatusLine, level_color
        self.assertEqual([level_color(v) for v in (100, 31, 30, 11, 10, 0)], ["good", "good", "warn", "warn", "bad", "bad"])
        line = StatusLine([[("⇄", "good"), (" ", None), ("LimitSwitcher", "dim")], [("doge", "dim")],
                           [("5h", "label"), (" ", None), ("12%", "warn"), (" left", "dim")]])
        self.assertEqual(str(line), "⇄ LimitSwitcher · doge · 5h 12% left")  # plain text stays plain
        painted = statusline.paint(line.parts)
        self.assertIn("\x1b[92m⇄\x1b[0m", painted)              # the icon, green
        self.assertIn("\x1b[90mLimitSwitcher\x1b[0m", painted)  # grey
        self.assertIn("\x1b[90mdoge\x1b[0m", painted)           # the account, grey
        self.assertIn("\x1b[94m5h\x1b[0m", painted)             # the label, blue
        self.assertIn("\x1b[93m12%\x1b[0m\x1b[90m left\x1b[0m", painted)  # a little left: yellow number, grey word
        painted = statusline.context_painted({"context_window": {"total_input_tokens": 900000, "remaining_percentage": 8}})
        self.assertEqual(painted, "\x1b[94mctx \x1b[0m\x1b[91m900k\x1b[0m\x1b[90m/\x1b[0m\x1b[91m8%\x1b[0m\x1b[90m left\x1b[0m")
        self.assertEqual(statusline.context_painted({"context_window": {"total_input_tokens": 1_250_000}}), "\x1b[94mctx \x1b[0m1.2M")

    def test_the_light_json_helpers_agree_with_the_json_module(self):
        import json
        from account_switcher import statusline
        samples = [{"a": [1, 2.5, None, True, False, "é\n\u2028 \"q\""], "b": {"c": -1e-7, "d": []}}, [], {}, "x", 0,
                   {"rate_limits": {"five_hour": {"used_percentage": 40.5, "resets_at": 1.7e9}}, "session_id": "s"}]
        for value in samples:
            self.assertEqual(statusline.loads(statusline.dumps(value)), value)
            self.assertEqual(statusline.loads(json.dumps(value)), value)
            self.assertEqual(json.loads(statusline.dumps(value)), value)
        self.assertEqual(statusline.loads(b' {"a": NaN} ')["a"] != statusline.loads(b'{"a": NaN}')["a"], True)  # NaN, as json reads it
        for bad in (b"", b"{", b"{} x", b"nope"):
            with self.assertRaises(ValueError):
                statusline.loads(bad)
        with self.assertRaises(TypeError):
            statusline.dumps({"a": object()})
        with mock.patch.dict(sys.modules, {"_json": None}):  # a Python without them: the json module
            loads, dumps = statusline._json_functions()
        self.assertIs(loads, json.loads)
        self.assertIs(dumps, json.dumps)

    def test_the_last_line_stands_in_while_the_app_is_busy(self):
        from account_switcher import statusline
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "statusline-cache.json")
            state = {"url": "http://127.0.0.1:9/api/statusline", "token": "t"}
            self.assertIsNone(statusline.report(state, {}, cache)[0])  # nothing yet, and nobody answering
            statusline.remember(cache, "the line")
            self.assertEqual(statusline.report(state, {}, cache)[0], "the line")  # a blank would blink
            old = json.loads(Path(cache).read_text())
            old["at"] -= statusline.CACHE_FOR + 5
            Path(cache).write_text(json.dumps(old))
            self.assertIsNone(statusline.report(state, {}, cache)[0])  # not for long

    def test_context_part(self):
        from account_switcher import statusline
        window = {"total_input_tokens": 183400, "remaining_percentage": 82.4}
        self.assertEqual(statusline.context_part({"context_window": window}), "ctx 183k/82% left")
        self.assertEqual(statusline.context_part({"context_window": {"total_input_tokens": 1_250_000}}), "ctx 1.2M")
        self.assertIsNone(statusline.context_part({}))  # before the first reply
        self.assertIsNone(statusline.context_part({"context_window": {"total_input_tokens": 0}}))
        # count and percentage from the same figure: the current context, not the lagging reported percentage
        live = {"current_usage": {"input_tokens": 0, "cache_creation_input_tokens": 90_000, "cache_read_input_tokens": 110_000},
                "total_input_tokens": 50_000, "context_window_size": 400_000, "remaining_percentage": 99}
        self.assertEqual(statusline.context_part({"context_window": live}), "ctx 200k/50% left")

    def test_session_context_lasts_and_follows_a_jev_compaction(self):
        from account_switcher import statusline
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ctx.json")
            window = {"context_window": {"current_usage": {"input_tokens": 0, "cache_read_input_tokens": 300_000},
                                         "context_window_size": 1_000_000}}
            with mock.patch.object(statusline.time, "time", return_value=1000.0):
                self.assertEqual(statusline.session_context(path, window, None), "ctx 300k/70% left")
            with mock.patch.object(statusline.time, "time", return_value=1000.0 + 6 * 3600):
                self.assertEqual(statusline.session_context(path, {}, None), "ctx 300k/70% left")  # no figures, hours on: the last ones
                self.assertEqual(statusline.session_context(path, window, None), "ctx 300k/70% left")  # the same figure: still from t=1000
            compacted = {"saved": 120_000, "at": 2000.0}
            self.assertEqual(statusline.session_context(path, {}, compacted), "ctx 180k/82% left")      # less what Jev saved
            self.assertEqual(statusline.session_context(path, window, compacted), "ctx 180k/82% left")  # the old figure again: still less
            newer = {"context_window": {"current_usage": {"input_tokens": 5_000, "cache_read_input_tokens": 190_000},
                                        "context_window_size": 1_000_000}}
            with mock.patch.object(statusline.time, "time", return_value=3000.0):
                self.assertEqual(statusline.session_context(path, newer, compacted), "ctx 195k/80% left")  # the next reply's own
            self.assertIsNone(statusline.session_context(None, {}, None))

    def test_hook_does_not_wake_a_session_that_went_on_meanwhile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            def line(**entry):
                return json.dumps(entry) + "\n"

            path.write_text(line(type="user", uuid="u1") + line(type="assistant", uuid="a1")
                            + line(type="assistant", uuid="e1", isApiErrorMessage=True))  # the limit's own reply
            before = afk_hook.stamp(str(path))
            self.assertEqual(before, "a1")
            with path.open("a") as handle:  # notices while the hook waits: not the session going on
                handle.write(line(type="system", uuid="s1", content="Remote Control disconnected"))
                handle.write(line(type="system", subtype="compact_boundary", uuid="s2"))
                handle.write(line(type="user", uuid="m1", isMeta=True))
                handle.write(line(type="file-history-snapshot"))
                handle.write("not json\n")
            self.assertEqual(afk_hook.stamp(str(path)), before)
            with path.open("a") as handle:  # a new reply: it went on
                handle.write(line(type="assistant", uuid="a2"))
            self.assertNotEqual(afk_hook.stamp(str(path)), before)
            self.assertIsNone(afk_hook.stamp(str(Path(tmp) / "missing")))

    def test_own_status_line_gets_the_apps_freshest_numbers(self):
        """An idle session hands over the numbers from its last reply; the user's command gets
        the app's current ones instead (everything else in the input unchanged)."""
        import io
        from account_switcher import statusline
        seen = {}

        def run(command, raw):
            seen["input"] = json.loads(raw)
            return b"mine\n"
        stale = {"session_id": "idle", "model": {"id": "x"},
                 "rate_limits": {"five_hour": {"used_percentage": 0, "resets_at": 1}}}
        fresh = {"five_hour": {"used_percentage": 91, "resets_at": 2}, "seven_day": {"used_percentage": 40, "resets_at": 3}}
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"url": "http://127.0.0.1:1/x", "token": "t", "statusline": "my-line.sh"}))
            out = io.BytesIO()
            with mock.patch.object(statusline, "report", return_value=("line", fresh, None)), \
                    mock.patch.object(statusline, "run_previous", side_effect=run), \
                    mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(json.dumps(stale).encode()))), \
                    mock.patch.object(sys, "stdout", mock.Mock(buffer=out)):
                statusline.main(["statusline.py", str(state)])
        self.assertEqual(seen["input"]["rate_limits"], fresh)
        self.assertEqual(seen["input"]["model"], {"id": "x"})
        self.assertTrue(out.getvalue().startswith(b"mine  "))

    def test_own_status_line_gets_the_marker_after_its_last_line(self):
        from account_switcher import statusline
        self.assertEqual(statusline.with_marker(b"~/proj main\n"), b"~/proj main  " + statusline.MARKER.encode() + b"\n")
        self.assertTrue(statusline.with_marker(b"one\ntwo").startswith(b"one\ntwo  "))
        self.assertEqual(statusline.with_marker(b""), statusline.MARKER.encode() + b"\n")
