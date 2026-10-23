import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener, ProxyHandler

from account_switcher.web import Controller, make_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller()
        self.server = make_server(self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        parsed = urlsplit(self.server.launch_url)
        self.base = f"http://{parsed.netloc}"
        self.token = parse_qs(parsed.fragment)["token"][0]
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown()
        self.controller.close()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, body=None, auth=True, origin=None):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        if origin:
            headers["Origin"] = origin
        return self.opener.open(Request(self.base + path, json.dumps(body).encode() if body is not None else None, headers), timeout=5)

    def test_assets_state_and_request_protection(self):
        for provider in ('codex', 'claude', 'switcher'):
            with self.request('/assets/' + provider + '.png', auth=False) as response:
                self.assertEqual(response.headers['Content-Type'], 'image/png')
                self.assertTrue(response.read().startswith(b'\x89PNG\r\n\x1a\n'))
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        with self.assertRaises(HTTPError) as gone:  # no web full view any more
            self.request("/", auth=False)
        self.assertEqual(gone.exception.code, 404)
        with self.assertRaises(HTTPError) as unauth:
            self.request("/api/state", auth=False)
        self.assertEqual(unauth.exception.code, 401)
        with self.assertRaises(HTTPError) as cross_origin:
            self.request("/api/reset", {}, origin="https://unrelated.example")
        self.assertEqual(cross_origin.exception.code, 403)
        with self.request("/api/state") as response:
            self.assertEqual(len(json.load(response)["accounts"]), 4)

    def test_manual_swap_and_invalid_input(self):
        with self.request("/api/swap", {"id": "claude-b"}) as response:
            self.assertEqual(response.status, 202)
        self.wait_idle()
        self.assertEqual(self.controller.gateway.router.active["claude"], "claude-b")
        with self.assertRaises(HTTPError) as invalid:
            self.request("/api/preferences", {"afk": "false", "autoSwap": True})
        self.assertEqual(invalid.exception.code, 400)

    def test_taskbar_view_switch_and_display(self):
        self.assertTrue(self.controller.snapshot()["taskbar"])
        with self.request("/api/taskbar", {"on": False}) as response:
            self.assertEqual(response.status, 202)
        with self.request("/api/taskbar", {"display": "right"}):
            pass
        snapshot = self.controller.snapshot()
        self.assertFalse(snapshot["taskbar"])
        self.assertEqual(snapshot["taskbarDisplay"], "right")
        with self.assertRaises(HTTPError) as invalid:
            self.request("/api/taskbar", {"display": 3})
        self.assertEqual(invalid.exception.code, 400)

    def test_name_mode_shows_names_instead_of_emails(self):
        with self.request("/api/rename", {"id": "claude-a", "name": "  Work  "}):
            pass
        self.assertEqual(self.controller.snapshot()["accounts"][0]["name"], "personal@example.com")  # off: emails
        with self.request("/api/names", {"on": True}):
            pass
        accounts = {a["id"]: a for a in self.controller.snapshot()["accounts"]}
        self.assertEqual(accounts["claude-a"]["name"], "Work")
        self.assertEqual(accounts["claude-b"]["name"], "Claude 2")  # no name yet: never the email
        self.assertEqual(accounts["claude-a"]["email"], "personal@example.com")  # the full view can reveal it
        with self.assertRaises(HTTPError):
            self.request("/api/rename", {"id": "claude-a", "name": "x" * 41})

    def wait_idle(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self.controller.snapshot()["busy"]:
                return
            time.sleep(.05)
        self.fail("Web operation did not settle")


class DemoTests(unittest.TestCase):
    def test_demo_mode_needs_no_network(self):
        controller = Controller()
        try:
            controller.action("swap", {"id": "claude-b"})
            deadline = time.monotonic() + 3
            while controller.pending and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(controller.gateway.router.active["claude"], "claude-b")
            with self.assertRaises(ValueError):
                controller.action("run", {})  # the old Recovery lab is gone
        finally:
            controller.close()
