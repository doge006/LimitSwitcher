"""Animation frames in step with the display (Windows).

A window timer fires on Windows' 15.6 ms tick, unevenly (5 to 28 ms apart were measured) and out of step
with the screen, so motion stutters, most of all on a fast display (144 Hz: a frame every 6.9 ms).
While a window animates, one thread waits for each display refresh (DwmFlush) and posts that window
its usual WM_TIMER message. A window still drawing its last frame gets no second one (it calls
ack() when it handles the message), so a slow frame skips refreshes instead of queueing them.
"""
import ctypes
import threading
import time
from ctypes import wintypes

WM_TIMER = 0x0113
_lock = threading.Lock()
_wanted = threading.Event()
_ticking = {}  # (hwnd, timer id) -> a message is posted and not handled yet
_thread = None


def _wait_for_refresh(dwmflush):
    began = time.perf_counter()
    if dwmflush is None or dwmflush() != 0 or time.perf_counter() - began < 0.002:
        # no compositor answer (or the display is off, when it returns at once): a 60 Hz pace
        time.sleep(max(0.0, 1 / 60 - (time.perf_counter() - began)))


def _run():
    user32 = ctypes.windll.user32
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    try:
        dwmflush = ctypes.WinDLL("dwmapi").DwmFlush
    except OSError:
        dwmflush = None
    while True:
        _wanted.wait()
        _wait_for_refresh(dwmflush)
        with _lock:
            for (hwnd, timer), posted in list(_ticking.items()):
                if not posted:
                    if user32.PostMessageW(hwnd, WM_TIMER, timer, 0):
                        _ticking[hwnd, timer] = True
                    else:  # the window is gone
                        _ticking.pop((hwnd, timer), None)
            if not _ticking:
                _wanted.clear()


def start(hwnd, timer):
    """Send `hwnd` WM_TIMER(`timer`) once per display refresh until stop()."""
    global _thread
    with _lock:
        _ticking.setdefault((hwnd, timer), False)
        _wanted.set()
        if _thread is None:
            _thread = threading.Thread(target=_run, name="vsync", daemon=True)
            _thread.start()


def ack(hwnd, timer):
    """The window handled the message: it can have the next refresh."""
    with _lock:
        if (hwnd, timer) in _ticking:
            _ticking[hwnd, timer] = False


def stop(hwnd, timer):
    with _lock:
        _ticking.pop((hwnd, timer), None)


def running(hwnd, timer):
    with _lock:
        return (hwnd, timer) in _ticking
