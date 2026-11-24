"""The tray thread's hang watch: a window that stops answering gets every thread's stack into the log."""
import ctypes
import sys
import tempfile
import threading
import time
import types
import unittest
from ctypes import wintypes
from unittest import mock


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class HangWatchTest(unittest.TestCase):
    def test_hung_window_dumps_stacks_once(self):
        from account_switcher import tray
        user32 = ctypes.windll.user32
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
        icon, release = types.SimpleNamespace(_hwnd=None), threading.Event()

        def stuck_tray():  # a message-only window (never shown) whose thread then stops pumping
            icon._hwnd = user32.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 0, 0, wintypes.HWND(-3), None, None, None)
            release.wait(30)
            user32.DestroyWindow(icon._hwnd)

        threading.Thread(target=stuck_tray, name="stuck-tray", daemon=True).start()
        stopped = threading.Event()
        with tempfile.TemporaryFile("w+", encoding="utf-8") as handle, \
                mock.patch.object(tray, "HANG_SECONDS", 1), self.assertLogs("account_switcher", "WARNING") as logs:
            watch = threading.Thread(target=tray.watch_for_hangs, args=(icon, handle, stopped), daemon=True)
            watch.start()
            time.sleep(8)  # two missed pings
            stopped.set()
            release.set()
            watch.join(10)
            handle.seek(0)
            dump = handle.read()
        self.assertIn("stuck_tray", dump)  # the stuck thread's own frame is in the dump
        self.assertEqual(dump.count("stuck_tray"), 1, "one dump per hang, not one per ping")
        self.assertTrue(any("not responding" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
