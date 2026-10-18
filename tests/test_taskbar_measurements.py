"""UI Automation may stall; the tray thread must keep running independently."""
import threading
import unittest
from unittest.mock import Mock, patch

from account_switcher.taskbar_measurements import TaskbarMeasurements


class TaskbarMeasurementTests(unittest.TestCase):
    def test_slow_provider_is_off_thread_and_requests_do_not_pile_up(self):
        entered, release, ready = threading.Event(), threading.Event(), threading.Event()
        threads, calls = [], []

        def measure(hwnd):
            calls.append(hwnd)
            threads.append(threading.get_ident())
            entered.set()
            release.wait(3)
            return [(1, 2, 3, 4)]

        worker = TaskbarMeasurements(measure, lambda hwnd: ready.set())
        self.addCleanup(release.set)
        self.assertIsNone(worker.buttons(123, ("layout",)))
        self.assertTrue(entered.wait(1))
        for _ in range(100):
            self.assertIsNone(worker.buttons(123, ("layout",)))
        self.assertEqual(calls, [123])
        self.assertEqual(worker.requests.qsize(), 0)
        self.assertNotEqual(threads[0], threading.get_ident())
        self.assertTrue(worker.thread.daemon)
        release.set()
        self.assertTrue(ready.wait(1))
        self.assertEqual(worker.buttons(123, ("layout",)), [(1, 2, 3, 4)])
        self.assertEqual(calls, [123])

    def test_changed_layout_never_reuses_old_rectangles(self):
        ready = threading.Event()
        worker = TaskbarMeasurements(lambda hwnd: [(1, 2, 3, 4)], lambda hwnd: ready.set())
        with patch("account_switcher.taskbar_measurements.time.monotonic", return_value=100):
            worker.buttons(123, ("old",))
            self.assertTrue(ready.wait(1))
            ready.clear()
            self.assertIsNone(worker.buttons(123, ("new",)))
            self.assertTrue(ready.wait(1))
            self.assertEqual(worker.buttons(123, ("new",)), [(1, 2, 3, 4)])

    def test_unchanged_layout_keeps_last_rectangles_while_refreshing(self):
        ready = threading.Event()
        measure = Mock(return_value=[(1, 2, 3, 4)])
        worker = TaskbarMeasurements(measure, lambda hwnd: ready.set())
        with patch("account_switcher.taskbar_measurements.time.monotonic", return_value=100) as clock:
            worker.buttons(123, ("same",))
            self.assertTrue(ready.wait(1))
            ready.clear()
            clock.return_value = 102
            self.assertEqual(worker.buttons(123, ("same",)), [(1, 2, 3, 4)])
            self.assertTrue(ready.wait(1))
            self.assertEqual(measure.call_count, 2)

    def test_disconnected_taskbar_discards_inflight_result(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        notified = Mock()

        def measure(hwnd):
            entered.set()
            release.wait(3)
            completed.set()
            return [(1, 2, 3, 4)]

        worker = TaskbarMeasurements(measure, notified)
        self.addCleanup(release.set)
        worker.buttons(123, ("old explorer",))
        self.assertTrue(entered.wait(1))
        worker.retain(())
        release.set()
        self.assertTrue(completed.wait(1))
        # A removed handle must stay absent even if its provider returns later.
        with worker.lock:
            self.assertNotIn(123, worker.records)
        notified.assert_not_called()

    def test_failed_measurement_is_cached_and_worker_can_measure_again(self):
        ready = threading.Event()
        measure = Mock(side_effect=[OSError("synthetic provider failure"), [(1, 2, 3, 4)]])
        worker = TaskbarMeasurements(measure, lambda hwnd: ready.set())
        with self.assertLogs("account_switcher.taskbar", level="WARNING"), \
                patch("account_switcher.taskbar_measurements.time.monotonic", return_value=100) as clock:
            worker.buttons(123, ("layout",))
            self.assertTrue(ready.wait(1))
            self.assertIsNone(worker.buttons(123, ("layout",)))
            self.assertEqual(measure.call_count, 1)
            ready.clear()
            clock.return_value = 102
            worker.buttons(123, ("layout",))
            self.assertTrue(ready.wait(1))
            self.assertEqual(worker.buttons(123, ("layout",)), [(1, 2, 3, 4)])


if __name__ == "__main__":
    unittest.main()
