"""macOS pieces that can run anywhere: the Keychain wrapper (against a stand-in for Apple's
`security` tool), Claude Code's Keychain naming, and the Keychain-keyed vault encryption."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from account_switcher import keychain
from account_switcher.providers import Claude
from tests.test_live import claude_login

FAKE_SECURITY = r'''#!/usr/bin/env python3
import json, os, shlex, sys
store = os.environ["FAKE_KEYCHAIN"]
data = json.load(open(store)) if os.path.exists(store) else {}
def save(): json.dump(data, open(store, "w"))
def run(args):
    cmd, rest = args[0], args[1:]
    opts, i = {}, 0
    while i < len(rest):
        if rest[i] in ("-s", "-a", "-X", "-w") and i + 1 < len(rest) and not rest[i + 1].startswith("-"):
            opts[rest[i]] = rest[i + 1]; i += 2
        else:
            opts[rest[i]] = True; i += 1
    key = opts.get("-s", "") + "|" + opts.get("-a", "")
    if cmd == "find-generic-password":
        if key not in data: sys.exit(44)
        print(data[key])
    elif cmd == "add-generic-password":
        data[key] = bytes.fromhex(opts["-X"]).hex(); save()
    elif cmd == "delete-generic-password":
        data.pop(key, None); save()
if sys.argv[1:] == ["-i"]:
    for line in sys.stdin:
        if line.strip(): run(shlex.split(line))
else:
    run(sys.argv[1:])
'''


@unittest.skipIf(sys.platform == "win32", "stand-in tool is a POSIX script")
class KeychainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        tool = root / "security"
        tool.write_text(FAKE_SECURITY)
        tool.chmod(0o755)
        self.patches = [mock.patch.object(keychain, "SECURITY", str(tool)),
                        mock.patch.dict(os.environ, {"FAKE_KEYCHAIN": str(root / "keychain.json"), "USER": "daniel"})]
        for p in self.patches:
            p.start()
        self.root = root

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_round_trip_keeps_json_intact(self):
        value = json.dumps({"claudeAiOauth": {"accessToken": "a b\"c", "expiresAt": 1}})
        keychain.put("Claude Code-credentials", "daniel", value)
        self.assertEqual(keychain.get("Claude Code-credentials", "daniel"), value)
        self.assertIsNone(keychain.get("Claude Code-credentials", "someone"))
        keychain.delete("Claude Code-credentials", "daniel")
        self.assertIsNone(keychain.get("Claude Code-credentials", "daniel"))

    def test_a_failing_keychain_is_not_a_missing_item(self):
        self.assertIsNone(keychain.get("svc", "acct", strict=True))  # really missing: fine
        failing = self.root / "failing"
        failing.write_text("#!/bin/sh\nexit 51\n")
        failing.chmod(0o755)
        with mock.patch.object(keychain, "SECURITY", str(failing)):
            self.assertIsNone(keychain.get("svc", "acct"))
            with self.assertRaises(OSError):
                keychain.get("svc", "acct", strict=True)

    def test_claude_reads_and_switches_through_the_keychain(self):
        home = self.root / "home"
        home.mkdir()
        claude_login(home, "uuid-a", "a@example.com", "at-a", "rt-a")  # writes ~/.claude.json + a file
        (home / ".claude" / ".credentials.json").unlink()  # on macOS the login is only in the Keychain
        creds = {"claudeAiOauth": {"accessToken": "at-a", "refreshToken": "rt-a", "subscriptionType": "max"}}
        keychain.put("Claude Code-credentials", "daniel", json.dumps(creds))
        claude = Claude(home=home, keychain=True)
        login = claude.read_live()
        self.assertEqual((login.email, login.secret["credentials"]["claudeAiOauth"]["accessToken"]), ("a@example.com", "at-a"))
        before = claude.signature()
        other = dict(login.secret, credentials={"claudeAiOauth": {"accessToken": "at-b", "refreshToken": "rt-b"}},
                     oauthAccount={"accountUuid": "uuid-b", "emailAddress": "b@example.com"})
        claude.write_live(other)
        self.assertNotEqual(claude.signature(), before)  # Claude Code notices, and so does the app
        self.assertEqual(json.loads(keychain.get("Claude Code-credentials", "daniel"))["claudeAiOauth"]["accessToken"], "at-b")
        self.assertEqual(claude.read_live().email, "b@example.com")
        self.assertFalse((home / ".claude" / ".credentials.json").exists())

    def test_service_name_matches_claude_code(self):
        self.assertEqual(Claude(home=self.root, keychain=True).keychain_service, "Claude Code-credentials")
        custom = Claude(config_dir="/tmp/isolated", keychain=True)
        self.assertRegex(custom.keychain_service, r"^Claude Code-credentials-[0-9a-f]{8}$")


class MacVaultCipherTests(unittest.TestCase):
    def test_encrypt_then_mac(self):
        import importlib
        from account_switcher import vault
        with mock.patch.object(sys, "platform", "darwin"):
            mac = importlib.reload(vault)
            try:
                mac._key = bytes(range(32))
                blob = mac.protect(b'{"token": "secret"}')
                self.assertNotIn(b"secret", blob)
                self.assertEqual(mac.unprotect(blob), b'{"token": "secret"}')
                with self.assertRaises(ValueError):
                    mac.unprotect(blob[:-1] + bytes([blob[-1] ^ 1]))
            finally:
                mac._key = None
        importlib.reload(vault)


if __name__ == "__main__":
    unittest.main()


class MacAppShapeTests(unittest.TestCase):
    """PyObjC can't be imported here, so check the rule that made the app exit at launch:
    a method on an Objective-C class becomes a selector, one argument per underscore,
    unless it is marked @objc.python_method."""

    def test_objc_method_names_match_their_arguments(self):
        import ast
        tree = ast.parse((Path(__file__).resolve().parent.parent / "account_switcher" / "macos_app.py").read_text())
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            if not any(getattr(b, "id", "") == "NSObject" for b in cls.bases):
                continue
            for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
                if any("python_method" in ast.dump(d) for d in fn.decorator_list):
                    continue
                args = len(fn.args.args) - 1
                self.assertEqual(fn.name.count("_"), args, f"{cls.name}.{fn.name}: {args} argument(s)")


class FullViewProcessTest(unittest.TestCase):
    """The Mac full view's own process reaches the menu bar app only through its local API."""

    def test_remote_state_and_actions(self):
        import threading
        from account_switcher.fullview_mac_app import Remote
        from account_switcher.web import Controller, make_server
        controller = Controller(live=False)
        server = make_server(controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            remote = Remote(server.launch_url)
            state = remote.state()
            self.assertEqual(state["revision"], controller.revision)
            self.assertEqual(len(state["accounts"]), len(controller.snapshot()["accounts"]))
            remote.action("clock", {"on": not state["clock24"]})
            changed = remote.state(state["revision"])  # the long poll answers once it changes
            self.assertEqual(changed["clock24"], not state["clock24"])
            with self.assertRaises((ValueError, RuntimeError)):
                remote.action("no-such-action", {})
            with self.assertRaises(RuntimeError):  # a wrong token is refused, not ignored
                Remote(server.launch_url.split("#token=")[0] + "#token=wrong").state()
        finally:
            server.shutdown()
            server.server_close()
        with self.assertRaises(RuntimeError):  # the app has quit: the full view's process ends
            remote.state()
