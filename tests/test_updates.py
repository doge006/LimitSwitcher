import unittest
from unittest import mock

from account_switcher import updates, version
from account_switcher.web import Controller


class VersionTests(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(version.newer("v1.0.1", "1.0.0"))
        self.assertTrue(version.newer("1.10.0", "1.9.9"))
        self.assertFalse(version.newer("1.0.0", "1.0.0"))
        self.assertFalse(version.newer("v0.9", "1.0.0"))
        self.assertFalse(version.newer("", "1.0.0"))


class UpdateCheckTests(unittest.TestCase):
    def release(self, tag):
        return {"version": tag, "url": "https://example.invalid/r", "setup": "https://example.invalid/LimitSwitcher-Setup.exe", "notes": ""}

    def test_newer_release_is_available(self):
        with mock.patch.object(updates, "latest_release", return_value=self.release("99.0.0")):
            state = updates.check()
        self.assertTrue(state["available"])
        self.assertEqual(state["latest"], "99.0.0")

    def test_same_version_is_up_to_date(self):
        with mock.patch.object(updates, "latest_release", return_value=self.release(version.VERSION)):
            self.assertFalse(updates.check()["available"])

    def test_offline_is_an_error_not_a_crash(self):
        with mock.patch.object(updates, "latest_release", side_effect=OSError("offline")):
            state = updates.check()
        self.assertFalse(state["available"])
        self.assertEqual(state["error"], "Couldn't reach GitHub")

    def test_controller_announces_a_new_version_once(self):
        controller = Controller()
        seen = []
        controller.on_update_available = seen.append
        try:
            with mock.patch.object(updates, "latest_release", return_value=self.release("99.0.0")):
                controller.check_updates()
                controller.check_updates()
            self.assertEqual(seen, ["99.0.0"])
            self.assertTrue(controller.snapshot()["update"]["available"])
        finally:
            controller.close()

    def test_a_failed_check_keeps_an_update_already_found(self):
        controller = Controller()
        try:
            with mock.patch.object(updates, "latest_release", return_value=self.release("99.0.0")):
                controller.check_updates()
            with mock.patch.object(updates, "latest_release", side_effect=OSError("offline")):
                self.assertFalse(controller.check_updates())
            self.assertTrue(controller.snapshot()["update"]["available"])  # still offered while offline
            self.assertTrue(controller.snapshot()["update"]["error"])
        finally:
            controller.close()

    def test_updates_are_checked_every_two_hours_and_a_failure_retries_sooner(self):
        from account_switcher import web
        self.assertEqual(web.update_wait(True, 0), 2 * 3600)
        self.assertEqual(web.update_wait(False, 1), 300)          # no network yet after boot: soon again
        self.assertEqual(web.update_wait(False, web.UPDATE_RETRIES), 300)
        self.assertEqual(web.update_wait(False, web.UPDATE_RETRIES + 1), 2 * 3600)  # then the normal rhythm

    def test_the_watcher_checks_at_once_and_stops_when_the_app_closes(self):
        import threading
        controller = Controller()
        called = threading.Event()
        controller.check_updates = lambda announce=True: called.set() or True
        try:
            controller.watch_updates()
            self.assertTrue(called.wait(5))  # the launch check
        finally:
            controller.close()


if __name__ == "__main__":
    unittest.main()
