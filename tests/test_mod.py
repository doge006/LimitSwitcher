import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from account_switcher import mod
from account_switcher.web import Controller


def done(code=0, out="", err=""):
    return subprocess.CompletedProcess(["claude"], code, out, err)


class ModCommandTests(unittest.TestCase):
    def test_installed_reads_claude_codes_own_list(self):
        listing = json.dumps([{"id": "other@x"}, {"id": "limitswitcher@limitswitcher", "enabled": True},
                              {"id": "jev-compact@limitswitcher"}])
        with mock.patch.object(mod, "_run", return_value=done(out=listing)):
            self.assertTrue(mod.installed())
        older = json.dumps([{"id": "limitswitcher@limitswitcher"}])  # from before the Jev compaction: install again
        with mock.patch.object(mod, "_run", return_value=done(out=older)):
            self.assertFalse(mod.installed())
        renamed = json.dumps([{"id": "limit-status@limitswitcher"}, {"id": "jev-compact@limitswitcher"}])  # before 1.3.8
        with mock.patch.object(mod, "_run", return_value=done(out=renamed)):
            self.assertFalse(mod.installed())
        with mock.patch.object(mod, "_run", return_value=done(out="[]")):
            self.assertFalse(mod.installed())

    def test_installed_is_unknown_without_claude_code(self):
        with mock.patch.object(mod, "_run", side_effect=FileNotFoundError("no claude")):
            self.assertIsNone(mod.installed())
        with mock.patch.object(mod, "_run", return_value=done(code=1)):
            self.assertIsNone(mod.installed())

    def test_install_adds_the_marketplace_then_both_plugins_with_their_files(self):
        calls = []
        with mock.patch.object(mod, "_run", side_effect=lambda args, timeout: calls.append(args) or done()):
            self.assertIsNone(mod.install("/data/afk-hook.json"))
        self.assertEqual(calls[0][:2], ["marketplace", "add"])
        self.assertIn(["install", "limitswitcher@limitswitcher", "--config", "statePath=/data/afk-hook.json"], calls)
        self.assertIn(["install", "jev-compact@limitswitcher", "--config", f"envFile={Path('/data/.env')}"], calls)
        self.assertIn(["update", "limitswitcher@limitswitcher"], calls)  # a newer version, when there is one
        self.assertIn(["update", "jev-compact@limitswitcher"], calls)
        self.assertEqual(calls[-1], ["uninstall", "limit-status@limitswitcher"])  # its name before 1.3.8, if there

    def test_install_is_fine_when_already_there_and_says_why_when_not(self):
        with mock.patch.object(mod, "_run", return_value=done(code=1, err="Marketplace already exists")):
            self.assertIsNone(mod.install("/s"))
        with mock.patch.object(mod, "_run", return_value=done(code=1, err="network down")):
            self.assertEqual(mod.install("/s"), "network down")
        with mock.patch.object(mod, "_run", side_effect=FileNotFoundError("Claude Code isn't installed")):
            self.assertIn("isn't installed", mod.install("/s"))


class ModProcessTests(unittest.TestCase):
    def test_a_command_that_hangs_is_ended_with_what_it_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text("#!/bin/sh\nsleep 30\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            ended = []
            with mock.patch.object(mod.shutil, "which", return_value=str(fake)), \
                    mock.patch.object(mod.processes, "end_tree", side_effect=lambda pid: (ended.append(pid), os.kill(pid, 9))):
                with self.assertRaises(subprocess.TimeoutExpired):
                    mod._run(["list", "--json"], 0.5)
            self.assertEqual(len(ended), 1)  # the whole tree, not only the shim

    def test_configure_hands_the_new_path_to_claude_code(self):
        seen = []
        with mock.patch.object(mod, "_run", side_effect=lambda args, timeout, stdin_text=None: seen.append((args, stdin_text)) or done()):
            self.assertIsNone(mod.configure("/data/LimitSwitcher/afk-hook.json"))
        self.assertEqual(seen[0][0], ["configure", "limitswitcher@limitswitcher", "--values-stdin"])
        self.assertEqual(json.loads(seen[0][1]), {"statePath": "/data/LimitSwitcher/afk-hook.json"})
        self.assertEqual(seen[1][0], ["configure", "jev-compact@limitswitcher", "--values-stdin"])
        self.assertEqual(json.loads(seen[1][1]), {"envFile": str(Path("/data/LimitSwitcher/.env"))})
        with mock.patch.object(mod, "_run", return_value=done(code=1, err="not installed")):
            self.assertEqual(mod.configure("/x"), "not installed")


class ModConfigAtStartTests(unittest.TestCase):
    def make(self, tmp, seen_at):
        from account_switcher.integrations import Integrations
        gateway = mock.Mock()
        gateway.manager.vault.root = Path(tmp)
        gateway.manager.meta = {"modSeenAt": seen_at}
        gateway.manager.lock = threading.RLock()
        return Integrations(gateway, "http://127.0.0.1:1/api/afk", "t"), gateway.manager

    def test_the_mod_is_told_the_new_data_folder_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            integrations, manager = self.make(tmp, time.time())
            with mock.patch("account_switcher.mod.configure", return_value=None) as configure:
                integrations.refresh_mod_config()
                integrations.refresh_mod_config()
            configure.assert_called_once_with(integrations.state_file)
            self.assertEqual(manager.meta["modStatePath"], str(integrations.state_file))

    def test_the_installed_mod_is_updated_once_per_app_version(self):
        from account_switcher.version import VERSION
        with tempfile.TemporaryDirectory() as tmp:
            integrations, manager = self.make(tmp, time.time())
            manager.meta["modVersion"] = "1.4.3"  # last synced by an older app
            with mock.patch("account_switcher.mod.install", return_value=None) as install, \
                    mock.patch("account_switcher.mod.configure", return_value=None):
                integrations.sync_mod()
                integrations.sync_mod()
            install.assert_called_once_with(integrations.state_file)
            self.assertEqual(manager.meta["modVersion"], VERSION)

    def test_a_failed_mod_update_is_tried_again_and_no_mod_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            integrations, manager = self.make(tmp, time.time())
            with mock.patch("account_switcher.mod.install", return_value="offline") as install, \
                    mock.patch("account_switcher.mod.configure", return_value=None):
                integrations.sync_mod()
            install.assert_called_once()
            self.assertNotIn("modVersion", manager.meta)  # the next start tries again
            integrations, manager = self.make(tmp, 0)  # never installed
            with mock.patch("account_switcher.mod.install") as install:
                integrations.sync_mod()
            install.assert_not_called()

    def test_nothing_is_asked_without_the_mod_or_when_it_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            integrations, manager = self.make(tmp, 0)  # never reported: not in use
            with mock.patch("account_switcher.mod.configure") as configure:
                integrations.refresh_mod_config()
            configure.assert_not_called()
            manager.meta["modSeenAt"] = time.time()
            with mock.patch("account_switcher.mod.configure", return_value="refused"):
                integrations.refresh_mod_config()
            self.assertNotIn("modStatePath", manager.meta)  # tried again at the next start


class ModStateTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller()

    def tearDown(self):
        self.controller.close()

    def test_demo_mode_has_no_mod(self):
        self.assertEqual(self.controller.snapshot()["mod"], {"status": "unavailable"})

    def test_status_follows_reports_and_install(self):
        c = self.controller
        c.live = True
        c.mod_checked = time.time()  # no look-up thread in this test
        self.assertEqual(c.mod_state()["status"], "unknown")
        c.mod_installed = False
        self.assertEqual(c.mod_state()["status"], "missing")
        c.mod_installed = True
        self.assertEqual(c.mod_state()["status"], "installed")
        c.mod_seen = time.time()
        self.assertEqual(c.mod_state()["status"], "active")
        c.mod_seen = time.time() - 300  # no session reports any more
        self.assertEqual(c.mod_state()["status"], "installed")
        c.mod_busy = "installing"
        self.assertEqual(c.mod_state()["status"], "installing")
        c.mod_busy = "Install failed · see log"
        self.assertEqual(c.mod_state(), {"status": "error", "text": "Install failed · see log"})

    def test_a_report_from_the_mod_makes_it_active(self):
        c = self.controller
        c.live = True
        c.gateway = mock.Mock()
        c.gateway.manager.meta = {}
        c.gateway.manager.lock = threading.RLock()
        c.gateway.manager.statusline.return_value = "line"
        c.statusline({"rate_limits": None, "session": "s1"})  # the status line script
        self.assertEqual(c.mod_seen, 0.0)
        c.statusline({"rate_limits": None, "session": "s1", "source": "mod"})
        self.assertGreater(c.mod_seen, 0.0)
        self.assertTrue(c.mod_installed)

    def test_the_mod_feeds_the_usage_and_the_status_line_shows_the_line(self):
        c = self.controller
        c.live = True
        c.gateway = mock.Mock()
        c.gateway.manager.statusline.return_value = "line"
        c.gateway.manager.lock = threading.RLock()
        c.gateway.manager.meta = {"modSeenAt": time.time()}
        self.assertIsNone(c.statusline({"session": "s", "source": "mod"}))  # the mod draws nothing
        c.gateway.manager.note_mod_session.assert_called_with("s")
        self.assertEqual(c.statusline({"session": "s"}), "line")  # the status line shows it

    def test_the_status_line_shows_the_line_only_once_the_mod_is_in_use(self):
        c = self.controller
        c.live = True
        c.gateway = mock.Mock()
        c.gateway.manager.statusline.return_value = "line"
        c.gateway.manager.lock = threading.RLock()
        c.gateway.manager.meta = {}
        self.assertIsNone(c.statusline({"session": "s"}))  # no mod, nothing shown
        c.statusline({"session": "s", "source": "mod"})
        self.assertEqual(c.statusline({"session": "s"}), "line")

    def test_model_and_effort_reach_the_line(self):
        c = self.controller
        c.live = True
        c.gateway = mock.Mock()
        c.gateway.manager.lock = threading.RLock()
        c.gateway.manager.meta = {"modSeenAt": time.time()}
        c.statusline({"session": "s", "model": "Opus 5.5", "effort": "high", "cwd": "/x/proj", "transcript": "/t/s.jsonl"})
        c.gateway.manager.statusline.assert_called_with(None, "s", model="Opus 5.5", effort="high", window=None,
                                                        cwd="/x/proj", transcript="/t/s.jsonl", parent=None)

    def test_an_older_mod_alone_asks_for_the_update(self):
        c = self.controller
        c.live = True
        c.mod_checked = time.time()
        c.mod_seen = time.time()
        c.mod_installed = False  # limitswitcher reports, jev-compact isn't there
        self.assertEqual(c.mod_state()["status"], "update")

    def test_the_first_report_of_the_mod_puts_the_status_line_command_in(self):
        c = self.controller
        c.live = True
        c.gateway = mock.Mock()
        c.gateway.manager.meta = {}
        c.gateway.manager.lock = threading.RLock()
        c.gateway.manager.statusline.return_value = "line"
        c.statusline({"session": "s", "source": "mod"})
        self.assertGreater(c.gateway.manager.meta["modSeenAt"], 0)
        c.gateway.integrations.apply_afk.assert_called_once()  # the line is shown from now on
        c.statusline({"session": "s", "source": "mod"})
        c.gateway.integrations.apply_afk.assert_called_once()  # not again for every report
        c.mod_check_result = None
        with mock.patch("account_switcher.mod.installed", return_value=False):
            c.mod_check()  # uninstalled: wanted again
        self.assertEqual(c.gateway.manager.meta["modSeenAt"], 0.0)
        self.assertEqual(c.gateway.integrations.apply_afk.call_count, 2)


if __name__ == "__main__":
    unittest.main()
