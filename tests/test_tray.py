import os
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

os.environ["PYSTRAY_BACKEND"] = "dummy"  # menu/icon logic only; no desktop needed
try:
    import pystray
    from account_switcher import tray
except ImportError as error:  # pystray/Pillow not installed
    raise unittest.SkipTest(f"tray dependencies missing: {error}")
from account_switcher.web import Controller, make_server


class FakeIcon:
    def __init__(self, name, icon, title, menu):
        self.icon, self.title, self.menu = icon, title, menu
        self.notes, self.menu_updates, self.stopped = [], 0, False

    def notify(self, message, title=None):
        self.notes.append((title, message))

    def update_menu(self):
        self.menu_updates += 1

    def stop(self):
        self.stopped = True


class FakeFlyout:
    def __init__(self):
        self.toggles = self.refreshes = 0

    def toggle(self):
        self.toggles += 1

    def state_changed(self):
        self.refreshes += 1

    def dismiss(self):
        pass


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller()
        self.server = make_server(self.controller)
        self.tray = tray.Tray(self.controller, self.server, icon_factory=FakeIcon)
        self.icon = self.tray.icon

    def tearDown(self):
        self.controller.close()
        self.server.server_close()

    def settle(self):
        self.assertTrue(wait_for(lambda: not self.controller.snapshot()["busy"]))
        self.tray.refresh()

    def test_windows_shutdown_is_not_blocked_and_settings_are_put_back(self):
        """pystray answers unknown messages with 0, which for WM_QUERYENDSESSION means "don't shut
        down". The tray says yes, and cleans up when the session really ends."""
        from unittest import mock

        class WinIcon(FakeIcon):
            def __init__(self, *args):
                super().__init__(*args)
                self._message_handlers = {}
        with mock.patch.object(tray.sys, "platform", "win32"):
            tray_ = tray.Tray(self.controller, self.server, icon_factory=WinIcon)
        cleaned = []
        tray_.on_end_session = lambda: cleaned.append(True)
        handlers = tray_.icon._message_handlers
        self.assertEqual(handlers[tray.WM_QUERYENDSESSION](0, 0), 1)  # yes, Windows may shut down
        handlers[tray.WM_ENDSESSION](0, 0)  # the shutdown was cancelled by something else
        self.assertEqual(cleaned, [])
        handlers[tray.WM_ENDSESSION](1, 0)  # it is really ending
        self.assertEqual(cleaned, [True])

    def test_tooltip_and_status_dot(self):
        state = self.controller.snapshot()
        text = tray.tooltip(state)
        self.assertLessEqual(len(text), 127)
        self.assertIn("Claude: personal@example.com · 36% left", text)
        self.assertIn("Codex: personal@example.com · 49% left", text)
        self.assertEqual(tray.tray_level(state), "good")
        state["accounts"][0]["eligible"] = False
        self.assertEqual(tray.tray_level(state), "bad")
        self.assertIn("limit reached", tray.tooltip(state))
        self.assertEqual(tray.icon_image("warn").size, (64, 64))

    def test_menu_without_flyout_opens_full_view(self):
        items = [i for i in self.tray.menu_items() if i is not pystray.Menu.SEPARATOR]
        self.assertEqual([i.text for i in items], ["Full view", "Auto swap", "Auto resume", "Quit"])
        self.assertTrue(items[0].default)
        self.assertTrue(items[1].checked)
        self.assertFalse(items[2].checked)

    def test_menu_with_flyout_and_toggles(self):
        flyout = FakeFlyout()
        tray_ = tray.Tray(self.controller, self.server, icon_factory=FakeIcon, flyout=flyout)
        items = [i for i in tray_.menu_items() if i is not pystray.Menu.SEPARATOR]
        self.assertEqual(items[0].text, "Accounts")
        items[0](tray_.icon)  # left-click activates the default item
        self.assertEqual(flyout.toggles, 1)
        next(i for i in items if i.text == "Auto resume")(tray_.icon)
        self.assertTrue(wait_for(lambda: self.controller.afk))
        tray_.refresh()
        self.assertIn("Auto resume", tray_.icon.title)
        self.assertGreater(flyout.refreshes, 0)  # open flyout is told to redraw

    def test_manual_swap_is_not_announced(self):
        self.tray.refresh()
        self.controller.action("swap", {"id": "claude-b"})
        self.settle()
        self.assertEqual(self.controller.gateway.router.active["claude"], "claude-b")
        self.assertEqual(self.icon.notes, [])

    def test_refresh_only_touches_what_changed(self):
        self.tray.refresh()
        updates, title = self.icon.menu_updates, self.icon.title
        self.tray.refresh()
        self.assertEqual(self.icon.menu_updates, updates)
        self.assertEqual(self.icon.title, title)

    def test_failover_is_announced(self):
        self.tray.refresh()
        router = self.controller.gateway.router
        spent = router.current("claude")
        spent.exhausted, spent.five_hour = True, 100
        router.active["claude"] = "claude-b"  # what Auto swap does when a limit hits
        self.tray.refresh()
        self.assertEqual(len(self.icon.notes), 1)
        title, message = self.icon.notes[0]
        self.assertEqual(title, "Claude switched accounts")
        self.assertIn("personal@example.com hit its limit", message)
        self.assertIn("second@example.com", message)

    def test_watch_follows_changes_and_quits_with_controller(self):
        thread = threading.Thread(target=self.tray.watch, daemon=True)
        thread.start()
        self.controller.action("swap", {"id": "codex-b"})
        self.assertTrue(wait_for(lambda: "Codex: second@example.com" in self.icon.title))
        self.controller.close()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(self.icon.stopped)

    def test_dashboard_shutdown_quits_tray(self):
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        thread.start()
        try:
            parsed = urlsplit(self.server.launch_url)
            token = parse_qs(parsed.fragment)["token"][0]
            request = Request(f"http://{parsed.netloc}/api/shutdown", b"{}",
                              {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
            self.assertTrue(wait_for(lambda: self.icon.stopped))
        finally:
            self.server.shutdown()

    def test_shutdown_asked_while_starting_quits_once_up(self):
        # tray.main: the URL file is written before the tray exists, and a quit asked for in
        # between (an update right after opening the app) is kept until the tray is up.
        server = make_server(self.controller)
        try:
            server.quit_asked = threading.Event()
            server.quit = server.quit_asked.set
            server.quit()

            class RunIcon(FakeIcon):
                def run(self, setup):
                    setup(self)
            tray_ = tray.Tray(self.controller, server, icon_factory=RunIcon)
            self.assertFalse(tray_.icon.stopped)
            tray_.run()
            self.assertTrue(tray_.icon.stopped)
        finally:
            server.server_close()

    def test_opening_again_shows_the_running_copy(self):
        from account_switcher.tray import show_running
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        thread.start()
        try:
            self.server.show = None
            self.assertFalse(show_running(self.server.launch_url))  # no window of its own: use a browser
            shown = threading.Event()
            self.server.show = shown.set
            self.assertTrue(show_running(self.server.launch_url))
            self.assertTrue(shown.is_set())
            self.assertFalse(show_running(self.server.launch_url.split("#")[0] + "#token=wrong"))
        finally:
            self.server.shutdown()

    def test_no_idle_threads_without_timeout(self):
        before = {t.name for t in threading.enumerate()}
        server = make_server(self.controller)
        try:
            self.assertEqual({t.name for t in threading.enumerate()} - before, set())
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()


class LogTests(unittest.TestCase):
    def test_app_log_is_capped(self):
        import logging
        import tempfile
        from unittest import mock
        from account_switcher import tray
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict("os.environ", {"ACCOUNT_SWITCHER_HOME": folder}), \
                mock.patch.object(tray, "LOG_LIMIT", 2000):
            handler = tray.log_handler()
            logger = logging.getLogger("account_switcher.test-cap")
            logger.addHandler(handler)
            logger.propagate = False
            try:
                for i in range(200):
                    logger.warning("line %d %s", i, "x" * 40)
            finally:
                logger.removeHandler(handler)
                handler.close()
            files = sorted(os.listdir(folder))
            self.assertEqual(files, ["app.log", "app.log.1"])
            self.assertTrue(all(os.path.getsize(os.path.join(folder, f)) <= 2000 for f in files))
