"""Give memory the app no longer uses back to the system once it settles.

After drawing (a panel, the menu, the full view) and at startup, the process holds pages it won't
touch again until something is drawn next. Trimming the working set hands them back; if they are
needed again, Windows brings them back from RAM (not disk), in microseconds. Private (committed)
memory is unchanged. Once, a few seconds after things settle; never while something is moving.
"""
import gc
import sys
import threading

_timer = None
_lock = threading.Lock()


def trim():
    drawing = sys.modules.get(__package__ + ".flyout_render")  # never loaded just to be cleared
    if drawing is not None:  # decoded logos are only needed while something is drawn
        drawing.asset_source.cache_clear()
    gc.collect()
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessWorkingSetSize.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t)
        kernel32.SetProcessWorkingSetSize(kernel32.GetCurrentProcess(), ctypes.c_size_t(-1), ctypes.c_size_t(-1))


def trim_soon(delay=5.0):
    """Trim `delay` seconds from now (a later call moves it)."""
    global _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(delay, trim)
        _timer.daemon = True
        _timer.start()
