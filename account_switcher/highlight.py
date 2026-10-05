"""Windows: point a Claude Code window out on screen (separate accounts per window, profiles.py).

The window is found from the process that is the window (window.json's pid): first the console it
runs in (a classic console window, or the hidden pseudo console Windows Terminal owns: then Windows
Terminal's window, which holds every tab, so a single tab can't be singled out), else the first
visible top-level window of that process or one of its parents. It then gets an outline in the
Claude accent for a moment (four thin click-through bars, so it never takes the focus) and its
taskbar button flashes.
"""
import sys
import threading
import time

SHOW_FOR = 2.6     # seconds
THICK = 4          # px
COLOR = 0x5777D9   # COLORREF (0x00BBGGRR): Claude's accent, (217, 119, 87)


def window_of(pid, flash=True):
    """Find the window and point it out; True when it was found. Elsewhere than Windows: False."""
    if sys.platform != "win32" or not isinstance(pid, int):
        return False
    hwnd = find(pid)
    if not hwnd:
        return False
    if flash:
        _flash(hwnd)
    rect = _rect(hwnd)
    if rect and not _user32().IsIconic(hwnd):
        threading.Thread(target=_outline, args=(rect,), daemon=True, name="highlight").start()
    return True


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _lock = threading.Lock()
    _class = {}

    def _user32():
        return ctypes.WinDLL("user32", use_last_error=True)

    def _visible(hwnd):
        u = _user32()
        return bool(hwnd) and bool(u.IsWindowVisible(hwnd))

    def console_window(pid):
        """(The console window `pid` runs in, its title), or (None, ""). Needs this process to have no
        console of its own (the app runs as pythonw)."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleWindow.restype = wintypes.HWND
        if kernel32.GetConsoleWindow():
            return None, ""  # started from a console (a source copy): attaching would leave it
        with _lock:
            if not kernel32.AttachConsole(pid):
                return None, ""
            try:
                title = ctypes.create_unicode_buffer(512)
                kernel32.GetConsoleTitleW(title, len(title))
                return kernel32.GetConsoleWindow(), title.value
            finally:
                kernel32.FreeConsole()

    def top_windows(titles=None):
        """{pid: first visible top-level window with a title}; with a dict `titles`, it also gets
        {hwnd: (pid, title)} for every such window."""
        u = _user32()
        found = {}
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def each(hwnd, _):
            length = u.GetWindowTextLengthW(hwnd)
            if u.IsWindowVisible(hwnd) and length > 0 and not u.GetWindow(hwnd, 4):  # GW_OWNER
                owner = wintypes.DWORD()
                u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                found.setdefault(owner.value, hwnd)
                if titles is not None:
                    text = ctypes.create_unicode_buffer(length + 1)
                    u.GetWindowTextW(hwnd, text, length + 1)
                    titles[hwnd] = (owner.value, text.value)
            return True
        u.EnumWindows(EnumProc(each), 0)
        return found

    # The programs that draw a console's window when it isn't a classic one of its own.
    CONSOLE_HOSTS = {"windowsterminal", "openconsole", "conhost"}

    def find(pid):
        u = _user32()
        u.GetWindow.restype = wintypes.HWND
        u.GetAncestor.restype = wintypes.HWND
        hwnd, title = console_window(pid)
        if hwnd:
            if _visible(hwnd):
                return hwnd
            for owner in (u.GetWindow(hwnd, 4), u.GetAncestor(hwnd, 3)):  # GW_OWNER, GA_ROOTOWNER: Windows Terminal
                if owner and owner != hwnd and _visible(owner):
                    return owner
        from . import processes
        family = processes.family()
        titles = {}
        tops = top_windows(titles)
        if hwnd and title:  # a hidden pseudo console whose terminal doesn't own it: the terminal
            for top, (owner, text) in titles.items():  # window showing the console's title
                # (an elevated one's tab reads "Administrator:  <title>")
                if text.endswith(title) and family.get(owner, (0, ""))[1] in CONSOLE_HOSTS:
                    return top
        seen = set()
        while pid and pid not in seen:
            seen.add(pid)
            if pid in tops:
                return tops[pid]
            pid = family.get(pid, (0,))[0]
        return None

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    def _rect(hwnd):
        rect = RECT()
        try:  # without the invisible resize borders
            if ctypes.WinDLL("dwmapi").DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
                return rect.left, rect.top, rect.right, rect.bottom
        except OSError:
            pass
        if _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
            return rect.left, rect.top, rect.right, rect.bottom
        return None

    class FLASHWINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                    ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]

    def _flash(hwnd):
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x3, 4, 0)  # FLASHW_ALL, 4 times
        _user32().FlashWindowEx(ctypes.byref(info))

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR)]

    def _register():
        if "name" in _class:
            return _class["name"]
        u = _user32()
        u.DefWindowProcW.restype = LRESULT
        u.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        proc = WNDPROC(lambda h, m, w, l: u.DefWindowProcW(h, m, w, l))
        brush = ctypes.WinDLL("gdi32").CreateSolidBrush(COLOR)
        hinst = ctypes.WinDLL("kernel32").GetModuleHandleW(None)
        cls = WNDCLASSW(0, proc, 0, 0, hinst, None, None, brush, None, "LimitSwitcherHighlight")
        if not u.RegisterClassW(ctypes.byref(cls)):
            raise OSError("RegisterClassW failed")
        _class.update(name=cls.lpszClassName, proc=proc, hinst=hinst)  # the callback must outlive the windows
        return _class["name"]

    def _outline(rect):
        u = _user32()
        u.CreateWindowExW.restype = wintypes.HWND
        u.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                      wintypes.HINSTANCE, wintypes.LPVOID)
        with _lock:
            name = _register()
        left, top, right, bottom = rect
        t = THICK
        bars = ((left - t, top - t, right - left + 2 * t, t), (left - t, bottom, right - left + 2 * t, t),
                (left - t, top, t, bottom - top), (right, top, t, bottom - top))
        # topmost, no taskbar button, never activated, clicks go through, translucent
        style_ex = 0x8 | 0x80 | 0x8000000 | 0x20 | 0x80000
        windows = []
        for x, y, w, h in bars:
            hwnd = u.CreateWindowExW(style_ex, name, None, 0x80000000, x, y, w, h, None, None, _class["hinst"], None)  # WS_POPUP
            if hwnd:
                u.SetLayeredWindowAttributes(hwnd, 0, 235, 0x2)  # LWA_ALPHA
                u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
                windows.append(hwnd)
        msg = wintypes.MSG()
        end = time.monotonic() + SHOW_FOR
        while time.monotonic() < end:
            while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.015)
        for hwnd in windows:
            u.DestroyWindow(hwnd)
else:
    def find(pid):
        return None

    def _rect(hwnd):
        return None

    def _user32():
        raise OSError("Windows only")
