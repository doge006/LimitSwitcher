"""Animation ticks follow the display's refreshes, one at a time per window."""
import ctypes
import sys
import time
import unittest
from ctypes import wintypes

if sys.platform == "win32":
    from account_switcher import vsync


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class VsyncTests(unittest.TestCase):
    def setUp(self):
        user32 = self.user32 = ctypes.windll.user32
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        self.hwnd = user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, wintypes.HWND(-3), None, None, None)  # HWND_MESSAGE
        self.assertTrue(self.hwnd)
        self.addCleanup(user32.DestroyWindow, self.hwnd)

    def ticks(self, seconds, ack):
        msg, seen, end = wintypes.MSG(), [], time.perf_counter() + seconds
        while time.perf_counter() < end:
            while self.user32.PeekMessageW(ctypes.byref(msg), self.hwnd, vsync.WM_TIMER, vsync.WM_TIMER, 1):  # PM_REMOVE
                seen.append(time.perf_counter())
                if ack:
                    vsync.ack(self.hwnd, 7)
            time.sleep(0.001)
        return seen

    def test_ticks_come_once_per_refresh_and_stop(self):
        vsync.start(self.hwnd, 7)
        seen = self.ticks(0.5, ack=True)
        vsync.stop(self.hwnd, 7)
        self.assertGreaterEqual(len(seen), 20)  # 60 Hz or faster: about 30+ in half a second
        gaps = sorted(b - a for a, b in zip(seen, seen[1:]))
        self.assertLess(gaps[len(gaps) // 2], 0.02)  # typical gap: one refresh (16.7 ms at 60 Hz)
        self.assertFalse(vsync.running(self.hwnd, 7))
        self.assertLessEqual(len(self.ticks(0.1, ack=True)), 1)  # at most the one already posted

    def test_an_unhandled_tick_is_not_followed_by_more(self):
        vsync.start(self.hwnd, 7)
        time.sleep(0.2)  # a window busy drawing: no ack
        seen = self.ticks(0.05, ack=False)
        vsync.stop(self.hwnd, 7)
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
