"""Windows host for the tray flyout and right-click menu: per-pixel-alpha layered popups.

Windows exist only while open. They live on the tray's thread (pystray's message loop
dispatches their messages). Timers run only during the ~0.17 s open/close animation, plus a
once-a-minute tick while the flyout is open so "renews in" times stay current. Redraws
happen only when the hovered item or the app state changes.
"""
import ctypes
from ctypes import wintypes
import logging
import time

from . import flyout_render as fr

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x00080000, 0x00000080, 0x00000008
WM_DESTROY, WM_ACTIVATE, WM_SETCURSOR, WM_KEYDOWN, WM_TIMER = 0x0002, 0x0006, 0x0020, 0x0100, 0x0113
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSELEAVE = 0x0200, 0x0201, 0x0202, 0x02A3
WM_APP_REFRESH, WM_APP_CLOSE = 0x8000 + 1, 0x8000 + 2
WM_MOVE, WM_NCHITTEST, HTCLIENT, HTCAPTION = 0x0003, 0x0084, 1, 2
SW_SHOWNA, VK_ESCAPE, ULW_ALPHA, TME_LEAVE = 8, 0x1B, 0x2, 0x2
IDC_ARROW, IDC_HAND = 32512, 32649
MONITOR_DEFAULTTONEAREST = 2
CLASS_NAME = "AccountSwitcherFlyout"
TIMER_ANIM, TIMER_MINUTE, TIMER_PENDING, TIMER_FX, TIMER_FOCUS, TIMER_ARMED = 1, 2, 3, 4, 5, 6
CONFIRM_MS = 4000  # how long a first click on an account waits for the confirming click
log = logging.getLogger("account_switcher.flyout")


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HANDLE)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("guidItem", GUID)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def _sig(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, list(argtypes)


H = wintypes.HANDLE
_sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, H, wintypes.HINSTANCE, ctypes.c_void_p)
_sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.ShowWindow, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.LoadCursorW, H, wintypes.HINSTANCE, ctypes.c_void_p)
_sig(user32.SetCursor, H, H)
_sig(user32.SetTimer, ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p)
_sig(user32.KillTimer, wintypes.BOOL, wintypes.HWND, ctypes.c_size_t)
_sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.TrackMouseEvent, wintypes.BOOL, ctypes.POINTER(TRACKMOUSEEVENT))
_sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_sig(user32.MonitorFromPoint, H, wintypes.POINT, wintypes.DWORD)
_sig(user32.GetMonitorInfoW, wintypes.BOOL, H, ctypes.POINTER(MONITORINFO))
_sig(user32.GetDC, wintypes.HDC, wintypes.HWND)
_sig(user32.ReleaseDC, ctypes.c_int, wintypes.HWND, wintypes.HDC)
_sig(user32.UpdateLayeredWindow, wintypes.BOOL, wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
     ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
     ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD)
_sig(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
_sig(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
_sig(gdi32.CreateDIBSection, wintypes.HBITMAP, wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
     ctypes.POINTER(ctypes.c_void_p), H, wintypes.DWORD)
_sig(gdi32.SelectObject, H, wintypes.HDC, H)
_sig(gdi32.DeleteObject, wintypes.BOOL, H)
_sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
try:
    shell32 = ctypes.WinDLL("shell32")
    _sig(shell32.Shell_NotifyIconGetRect, ctypes.c_long, ctypes.POINTER(NOTIFYICONIDENTIFIER), ctypes.POINTER(wintypes.RECT))
except (AttributeError, OSError):
    shell32 = None
try:
    shcore = ctypes.WinDLL("shcore")
    _sig(shcore.GetDpiForMonitor, ctypes.c_long, H, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT))
except (AttributeError, OSError):
    shcore = None


# ---------- outside-click watcher (only while a popup is open) ----------
WH_MOUSE_LL = 14
PRESSES = {0x0201: "left", 0x0204: "right", 0x0207: "middle"}
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


_sig(user32.SetWindowsHookExW, H, ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
_sig(user32.UnhookWindowsHookEx, wintypes.BOOL, H)
_sig(user32.CallNextHookEx, LRESULT, H, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class OutsideClicks:
    """Low-level mouse hook, installed only while a popup is open: a press anywhere outside
    the popup (the tray icon included) closes it immediately, without waiting for focus
    changes or for the tray's own click notification, which can arrive late."""
    hook = None
    proc = None

    @classmethod
    def start(cls):
        if cls.hook:
            return
        cls.proc = HOOKPROC(cls._on_mouse)
        cls.hook = user32.SetWindowsHookExW(WH_MOUSE_LL, cls.proc, kernel32.GetModuleHandleW(None), 0)
        if not cls.hook:
            log.warning("mouse hook unavailable: %s", ctypes.get_last_error())

    @classmethod
    def stop(cls):
        if cls.hook:
            user32.UnhookWindowsHookEx(cls.hook)
            cls.hook = None

    @classmethod
    def _on_mouse(cls, code, wparam, lparam):
        try:
            if code >= 0 and wparam in PRESSES:
                info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                point = (info.pt.x, info.pt.y)
                for popup in [p for p in Popup._windows.values() if p.modal]:
                    if not popup.contains(point):
                        popup.outside_press(point, PRESSES[wparam])
        except Exception:
            log.exception("outside-click check failed")
        return user32.CallNextHookEx(None, code, wparam, lparam)


_sig(user32.GetForegroundWindow, wintypes.HWND)
_sig(user32.GetWindowThreadProcessId, wintypes.DWORD, wintypes.HWND, ctypes.c_void_p)
_sig(user32.AttachThreadInput, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
_sig(user32.BringWindowToTop, wintypes.BOOL, wintypes.HWND)
_sig(user32.SetFocus, wintypes.HWND, wintypes.HWND)
_sig(kernel32.GetCurrentThreadId, wintypes.DWORD)


def force_foreground(hwnd):
    """Make our panel the real foreground window.

    Windows often refuses SetForegroundWindow to a tray app (the click went to Explorer).
    When it does, Windows' own hidden-icons popup keeps focus, stays open behind us and
    flashes back on close. Briefly sharing input with the current foreground thread is the
    standard way around that; once we are foreground, Windows closes its popup itself.
    """
    if user32.SetForegroundWindow(hwnd) and user32.GetForegroundWindow() == hwnd:
        return True
    foreground = user32.GetForegroundWindow()
    theirs = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    ours = kernel32.GetCurrentThreadId()
    attached = bool(theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, theirs, False)
    return user32.GetForegroundWindow() == hwnd


# Windows' own hidden-icons popup (the ^ next to the clock): Windows 11 / Windows 10.
OVERFLOW_CLASSES = ("TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow")
_sig(user32.GetClassNameW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_sig(user32.GetAncestor, wintypes.HWND, wintypes.HWND, wintypes.UINT)
_sig(user32.keybd_event, None, wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t)


def overflow_in_front():
    """Is Windows' hidden-icons popup the foreground window right now?"""
    foreground = user32.GetForegroundWindow()
    if not foreground:
        return False
    root = user32.GetAncestor(foreground, 2) or foreground  # GA_ROOT
    name = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(root, name, 128)
    return name.value in OVERFLOW_CLASSES


def close_overflow():
    """Close Windows' hidden-icons popup the way a user would: one Esc keypress, sent only
    when that popup is the foreground window (so it can never reach another app).
    Explorer then closes and resets it itself, unlike hiding its window from outside."""
    if not overflow_in_front():
        return False
    user32.keybd_event(VK_ESCAPE, 0, 0, 0)
    user32.keybd_event(VK_ESCAPE, 0, 2, 0)  # KEYEVENTF_KEYUP
    return True


def in_rect(rect, point):
    return rect is not None and rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


def enable_dpi_awareness():
    """Per-monitor DPI awareness so the flyout is drawn sharp at 125/150/200 %."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def cursor():
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def monitor_at(x, y):
    """(monitor rect, work rect, scale) for the monitor containing a physical-pixel point."""
    monitor = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    user32.GetMonitorInfoW(monitor, ctypes.byref(info))
    scale = 1.0
    if shcore is not None:
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        if shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0 and dpi_x.value:
            scale = dpi_x.value / 96
    return info.rcMonitor, info.rcWork, scale


def icon_rect(icon):
    """Screen rectangle of the tray icon (pystray registers it with uID 0), or None."""
    if shell32 is None or icon is None or not getattr(icon, "_hwnd", None):
        return None
    ident = NOTIFYICONIDENTIFIER(ctypes.sizeof(NOTIFYICONIDENTIFIER), icon._hwnd, 0)
    rect = wintypes.RECT()
    try:
        if shell32.Shell_NotifyIconGetRect(ctypes.byref(ident), ctypes.byref(rect)) == 0 and rect.right > rect.left:
            return rect
    except OSError:
        pass
    return None


from .placement import place_above, place_menu, panel_contains  # noqa: E402


class Popup:
    """One layered window: fade/slide in and out, hover, click, Esc and click-away."""
    _registered = False
    _proc = None
    _windows = {}      # hwnd -> popup
    _creating = None   # popup whose CreateWindowExW is in progress

    def __init__(self, tray):
        self.tray = tray
        self.hwnd = None
        self.hover = self.pressed = None
        self.hits = []
        self.closing = False
        self.closed_at = self.opened_at = 0.0
        self.anim = None
        self.tracking = False
        self.alpha = 0.0
        self.x = self.y = 0
        self.slide = (0, 1)
        self.scale = 1.0
        self.fx, self.fx_anims = {}, {}   # animated values and their running transitions
        self.painter = None   # draws each frame again only where it changed (while open)

    # Subclasses: render(hover) -> (image, hits); position(width, height); activate(action)
    modal = True             # closes on a click elsewhere, hides the tray tooltip (not the taskbar blocks)
    ex_style = 0
    owner = None             # window this one stays above (the taskbar, for its blocks)
    dismiss_on_deactivate = True
    minute_ticks = False
    FX_SECONDS = {"hover": 0.12, "toggle": 0.18, "active": 0.4, "bar": 0.45}

    def fx_targets(self):
        return {("hover", self.hover): 1.0} if self.hover else {}

    def retarget(self):
        """Start short ease-out transitions toward the new resting values."""
        goal, now = self.fx_targets(), time.perf_counter()
        for key in set(goal) | {k for k in self.fx if k[0] == "hover"}:
            target = goal.get(key, 0.0)
            current = self.fx.get(key)
            if current is None:
                if key[0] != "hover":
                    self.fx[key] = target  # new item: appear at its value, no animation
                    continue
                current = self.fx[key] = 0.0
            running = self.fx_anims.get(key)
            if (running[1] if running else current) != target:
                self.fx_anims[key] = (current, target, now, self.FX_SECONDS[key[0]])
        if self.fx_anims and self.hwnd:
            user32.SetTimer(self.hwnd, TIMER_FX, 15, None)

    def fx_step(self):
        now = time.perf_counter()
        for key, (start, end, began, duration) in list(self.fx_anims.items()):
            progress = min(1.0, (now - began) / duration)
            self.fx[key] = start + (end - start) * (1 - (1 - progress) ** 3)
            if progress >= 1:
                del self.fx_anims[key]
                if key[0] == "hover" and end == 0.0:
                    self.fx.pop(key, None)
        if not self.fx_anims:
            user32.KillTimer(self.hwnd, TIMER_FX)  # nothing moving: no timer at all
        self.redraw(retarget=False)

    opener = "left"          # mouse button on the tray icon that opens this popup
    take_focus = True        # the panel takes focus; the right-click menu does not (like Steam's)
    focus_delay = 0          # ms to wait before taking focus (after closing Windows' tray popup)
    icon_box = None          # screen rect of the tray icon (or around the click that opened us)
    suppress_until = 0.0     # ignore the tray's own notification for a press we already handled

    def contains(self, point):
        """Is a screen point on the visible panel (not its shadow)?"""
        return self.on_panel((point[0] - self.x) / self.scale, (point[1] - self.y) / self.scale)

    def on_panel(self, x, y):
        return panel_contains(x, y, self.size[0] / self.scale, self.size[1] / self.scale)

    def outside_press(self, point, button):
        on_icon = in_rect(self.icon_box, point)
        if on_icon and button == self.opener:
            self.suppress_until = time.monotonic() + 2.5  # its notification may come late
        if self.closing or (getattr(self, "pinned", False) and not on_icon):
            return
        self.close()

    def toggle(self):
        if time.monotonic() < self.suppress_until:
            self.suppress_until = 0.0  # this is the notification for the press that closed us
            return
        if self.hwnd and not self.closing:
            self.close()
        elif time.monotonic() - self.closed_at > 0.3:  # ignore the click that just dismissed it
            self.open()

    def open(self):
        if self.hwnd:
            self._destroy()
        self._register()
        self.hover = self.pressed = None
        self.painter = fr.Painter()
        self.prepare()
        self.fx = {k: v for k, v in self.fx_targets().items() if k[0] != "hover"}
        self.fx_anims = {}
        image, self.hits = self.render(None)
        self.size = image.size
        self.x, self.y, self.slide = self.position(*image.size)
        Popup._creating = self
        try:
            self.hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | self.ex_style, CLASS_NAME,
                                               "LimitSwitcher", WS_POPUP, self.x, self.y, *image.size,
                                               self.owner, None, kernel32.GetModuleHandleW(None), None)
        finally:
            Popup._creating = None
        if not self.hwnd:
            log.error("CreateWindowExW failed: %s", ctypes.get_last_error())
            return
        Popup._windows[self.hwnd] = self
        if self.modal:
            self.tray.popup_visible(True)
        self.closing = False
        self.opened_at = time.monotonic()
        self._push(image, 0)
        user32.ShowWindow(self.hwnd, SW_SHOWNA)
        if self.take_focus:
            if self.focus_delay:
                # Let Windows' hidden-icons popup handle its Esc first, then take focus.
                user32.SetTimer(self.hwnd, TIMER_FOCUS, self.focus_delay, None)
                self.focus_delay = 0
            else:
                force_foreground(self.hwnd)
        if self.modal:
            OutsideClicks.start()
        if self.minute_ticks:
            user32.SetTimer(self.hwnd, TIMER_MINUTE, 60_000, None)
        self._animate(0.0, 1.0, 0.17)

    def prepare(self):
        pass

    def close(self):
        if self.hwnd and not self.closing:
            self.closing = True
            self.closed_at = time.monotonic()
            self._animate(self.alpha, 0.0, 0.11)

    def dismiss(self):
        """Any thread: remove the window (used on quit)."""
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_CLOSE, 0, 0)

    def state_changed(self):
        """Any thread: ask the window's own thread to redraw."""
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_REFRESH, 0, 0)

    # ---------- drawing ----------
    def redraw(self, retarget=True):
        if not self.hwnd or self.closing:
            return
        if retarget:
            self.retarget()
        image, self.hits = self.render(self.hover)
        if image.size != self.size:  # height changed: keep the edge next to the taskbar fixed
            dy = image.size[1] - self.size[1]
            self.size = image.size
            if self.slide[1] > 0:
                self.y -= dy
        self._push(image, self.alpha)

    def _push(self, image, alpha, offset=(0, 0)):
        """Show `image` on the layered window. Its premultiplied-BGRA copy lives in a bitmap kept
        for the window's life, written only when the image changes: fading and sliding (the open
        and close animations) move and fade the same pixels, so they skip the conversion."""
        self.alpha, self.image = alpha, image
        width, height = image.size
        surface = getattr(self, "_surface", None)
        if surface is None or surface[0] != (width, height):
            self._release_surface()
            screen = user32.GetDC(None)
            memory = gdi32.CreateCompatibleDC(screen)
            info = BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height  # top-down
            info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
            bits = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(screen, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
            user32.ReleaseDC(None, screen)
            if not bitmap or not bits.value:
                gdi32.DeleteDC(memory)
                return
            old = gdi32.SelectObject(memory, bitmap)
            surface = self._surface = [(width, height), memory, bitmap, bits, old, None, None]
        if surface[5] is not image:
            painter = self.painter
            # A frame the painter built on the one in the bitmap: convert only what it changed.
            step = painter is not None and painter.image is image and surface[6] == painter.count - 1
            fr.write_bgra(image, surface[3].value, painter.changed if step else None)
            surface[5], surface[6] = image, painter.count if painter is not None and painter.image is image else None
        screen = user32.GetDC(None)
        blend = BLENDFUNCTION(0, 0, max(0, min(255, round(alpha * 255))), 1)
        user32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wintypes.POINT(self.x + offset[0], self.y + offset[1])),
                                   ctypes.byref(wintypes.SIZE(width, height)), surface[1],
                                   ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
        user32.ReleaseDC(None, screen)

    def _release_surface(self):
        surface, self._surface = getattr(self, "_surface", None), None
        if surface:
            _, memory, bitmap, _, old, _, _ = surface
            gdi32.SelectObject(memory, old)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory)

    # ---------- animation (timer only while moving) ----------
    def _animate(self, start, end, duration):
        self.anim = (start, end, time.perf_counter(), duration)
        user32.SetTimer(self.hwnd, TIMER_ANIM, 10, None)
        self._tick()

    def _tick(self):
        start, end, began, duration = self.anim
        progress = min(1.0, (time.perf_counter() - began) / duration)
        value = start + (end - start) * (1 - (1 - progress) ** 3)
        travel = round(8 * self.scale * (1 - value))  # short slide in from the screen side,
        self._push(self.image, value, (-self.slide[0] * travel, -self.slide[1] * travel))  # never over the taskbar
        if progress >= 1:
            user32.KillTimer(self.hwnd, TIMER_ANIM)
            self.anim = None
            if end == 0.0:
                self._destroy()

    # ---------- input ----------
    def _logical(self, lparam):
        x = ctypes.c_short(lparam & 0xFFFF).value / self.scale
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value / self.scale
        return x, y

    def _set_hover(self, action):
        if action != self.hover:
            self.hover = action
            self.redraw()

    def drag_region(self, x, y):
        return False

    def wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:
            if wparam == TIMER_ANIM and self.anim:
                self._tick()
            elif wparam == TIMER_MINUTE:
                self.redraw()
            elif wparam == TIMER_FX:
                self.fx_step()
            elif wparam == TIMER_FOCUS:
                user32.KillTimer(hwnd, TIMER_FOCUS)
                if not self.closing:
                    force_foreground(hwnd)
            elif wparam == TIMER_PENDING:
                user32.KillTimer(hwnd, TIMER_PENDING)
                self.pending_timeout()
            elif wparam == TIMER_ARMED:
                user32.KillTimer(hwnd, TIMER_ARMED)
                self.armed_timeout()
            return 0
        if msg == WM_APP_CLOSE:
            self._destroy()
            return 0
        if msg == WM_APP_REFRESH:
            self.redraw()
            return 0
        if msg == WM_ACTIVATE and (wparam & 0xFFFF) == 0:  # WA_INACTIVE: clicked elsewhere
            # Focus can bounce while the tray click is still being processed; only a
            # deactivation after the popup has settled means the user clicked away.
            if self.dismiss_on_deactivate and time.monotonic() - self.opened_at > 0.25:
                self.close()
            return 0
        if msg == WM_KEYDOWN and wparam == VK_ESCAPE:
            self.close()
            return 0
        if msg == WM_NCHITTEST:
            px = ctypes.c_short(lparam & 0xFFFF).value - self.x
            py = ctypes.c_short((lparam >> 16) & 0xFFFF).value - self.y
            x, y = px / self.scale, py / self.scale
            if self.drag_region(x, y) and fr.hit_test(self.hits, x, y) is None:
                return HTCAPTION  # Windows moves the window natively while dragging
            return HTCLIENT
        if msg == WM_MOVE:
            if not self.anim:
                self.x = ctypes.c_short(lparam & 0xFFFF).value
                self.y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            return 0
        if msg == WM_MOUSEMOVE:
            if not self.tracking:
                track = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                self.tracking = bool(user32.TrackMouseEvent(ctypes.byref(track)))
            if not self.closing:
                self._set_hover(fr.hit_test(self.hits, *self._logical(lparam)))
            return 0
        if msg == WM_MOUSELEAVE:
            self.tracking = False
            if not self.closing:
                self._set_hover(None)
            return 0
        if msg == WM_SETCURSOR:
            user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND if self.hover else IDC_ARROW)))
            return 1
        if msg == WM_LBUTTONDOWN:
            x, y = self._logical(lparam)
            if not self.on_panel(x, y):
                self.close()  # a click on the soft shadow means "somewhere else"
                return 0
            self.pressed = fr.hit_test(self.hits, x, y)
            return 0
        if msg == WM_LBUTTONUP:
            action = fr.hit_test(self.hits, *self._logical(lparam))
            pressed, self.pressed = self.pressed, None
            # Accept the click if the release lands on the pressed item or the hovered one.
            if action and action in (pressed, self.hover) and not self.closing:
                self.activate(action)
            return 0
        if msg == WM_DESTROY:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def pending_timeout(self):
        pass

    def _destroy(self):
        if self.hwnd:
            hwnd, self.hwnd = self.hwnd, None
            Popup._windows.pop(hwnd, None)
            for timer in (TIMER_ANIM, TIMER_MINUTE, TIMER_PENDING, TIMER_FX, TIMER_FOCUS):
                user32.KillTimer(hwnd, timer)
            user32.DestroyWindow(hwnd)
            self._release_surface()
            self.painter = None
            if not any(popup.modal for popup in Popup._windows.values()):
                OutsideClicks.stop()
                if self.modal:
                    self.tray.popup_visible(False)
                    from .memory import trim_soon
                    trim_soon()  # the drawing is done: hand its memory back
        self.anim, self.tracking, self.closing = None, False, False
        self.fx_anims = {}

    @classmethod
    def _register(cls):
        if Popup._registered:
            return

        def proc(hwnd, msg, wparam, lparam):
            owner = Popup._windows.get(hwnd) or Popup._creating
            if owner is not None:
                try:
                    return owner.wndproc(hwnd, msg, wparam, lparam)
                except Exception:  # never let a Python error escape into Win32, but record it
                    log.exception("popup message %#x failed", msg)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        Popup._proc = WNDPROC(proc)  # keep a reference for the process lifetime
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = Popup._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        wc.lpszClassName = CLASS_NAME
        user32.RegisterClassExW(ctypes.byref(wc))
        Popup._registered = True


class Flyout(Popup):
    """The accounts panel. Pop out pins it: it stays open and can be dragged by its header."""
    minute_ticks = True

    def __init__(self, tray):
        self.only = None      # a provider: the panel lists only its accounts (opened from a taskbar block)
        super().__init__(tray)
        self.pinned = False
        self.pending = None   # account id being switched to
        self.armed = None     # account id clicked once: the next click on it switches
        self.origin = None    # (screen rect, provider) when opened from a taskbar block

    @property
    def pinned(self):
        """Popped out: stays open, can be dragged. The compact panel always is."""
        return self._pinned or (bool(self.tray.state.get("compact")) and not self.only)

    @pinned.setter
    def pinned(self, value):
        self._pinned = value

    @property
    def dismiss_on_deactivate(self):
        return not self.pinned

    def prepare(self):
        self.tray.poke()  # fetch fresh usage if the numbers are older than a minute
        if self.origin:  # opened from a taskbar block: above it, with just that provider's accounts
            (box, self.only), self.origin = self.origin, None
            self.anchor, self.icon_box = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), box
            self.monitor, self.work, self.scale = monitor_at(*self.anchor)
            return
        self.only = None
        rect = icon_rect(getattr(self.tray, "icon", None))
        self.anchor = ((rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2) if rect else cursor()
        ax, ay = self.anchor
        self.icon_box = (rect.left, rect.top, rect.right, rect.bottom) if rect else (ax - 20, ay - 20, ax + 20, ay + 20)
        self.monitor, self.work, self.scale = monitor_at(*self.anchor)

    def position(self, width, height):
        if self.pinned and getattr(self, "pinned_at", None):
            return (*self.pinned_at, (0, 0))
        return place_above(self.anchor, self.monitor, self.work, width, height, self.scale)

    def fx_targets(self):
        return fr.targets(self.tray.state, self.hover)

    def render(self, hover):
        state = self.tray.state
        if self.pending and any(a["id"] == self.pending and a["active"] and not a.get("signed_out") for a in state["accounts"]):
            self.pending = None  # the switch landed
        return fr.render(state, hover, self.scale, pending=self.pending, pinned=self.pinned, fx=self.fx, armed=self.armed,
                         only=self.only, painter=self.painter)

    def drag_region(self, x, y):
        # Popped out: drag by the header (anywhere outside a button when compact, which has none).
        return self.pinned and ((self.tray.state.get("compact") and not self.only) or y < fr.header_height())

    def toggle(self):
        # Clicking the tray icon always closes an open panel, pinned or not.
        super().toggle()

    def close(self):
        if self.pinned:
            self.pinned_at = (self.x, self.y)
        self.armed = None
        super().close()

    def pending_timeout(self):
        self.pending = None
        self.redraw()

    def armed_timeout(self):
        self.armed = None
        self.redraw()

    def activate(self, action):
        tray = self.tray
        if action == "full":
            if not self.pinned:
                self.close()
            tray.open_full_view()
        elif action == "pin":
            self.pinned = not self.pinned
            if not self.pinned:  # pop back in: return to the tray and behave like a flyout again
                self.pinned_at = None
                image, self.hits = self.render(self.hover)
                self.x, self.y, self.slide = place_above(self.anchor, self.monitor, self.work, *image.size, self.scale)
                self.size = image.size
                self._push(image, self.alpha)
                force_foreground(self.hwnd)
            else:
                self.redraw()
        elif action == "expand":  # from the compact panel: the full panel, still popped out where it is
            self.pinned = True
            tray.act("compact", {"on": False})
        elif action == "compact":
            tray.act("compact", {"on": True})
        elif action == "hide":
            if tray.state.get("compact") and not self.only:
                # Hiding the compact panel: the tray icon opens the normal panel next time.
                self.pinned, self.pinned_at = False, None
                tray.act("compact", {"on": False})
            self.close()  # a popped-out full panel comes back where it was on the next tray click
        elif action == "quit":
            if self.armed != "quit":  # two clicks, so a stray one never quits
                self.armed = "quit"
                self.redraw()
                user32.SetTimer(self.hwnd, TIMER_ARMED, CONFIRM_MS, None)
                return
            self._destroy()
            tray.quit()
        elif action.startswith("swap:"):
            # Two clicks: the first asks for confirmation, so a stray click never switches.
            if self.armed != action[5:]:
                self.armed = action[5:]
                self.redraw()
                user32.SetTimer(self.hwnd, TIMER_ARMED, CONFIRM_MS, None)
                return
            user32.KillTimer(self.hwnd, TIMER_ARMED)
            self.armed = None
            self.pending = action[5:]
            self.redraw()
            user32.SetTimer(self.hwnd, TIMER_PENDING, 6000, None)
            tray.act("swap", {"id": self.pending})
        elif action == "toggle:taskbar":
            tray.act("taskbar", {"on": not tray.state.get("taskbar", True)})
        elif action.startswith("toggle:"):
            key = action[7:]
            prefs = {"autoSwap": tray.state["autoSwap"], "afk": tray.state["afk"]}
            prefs[key] = not prefs[key]
            tray.act("preferences", prefs)
        elif action.startswith("add:"):
            tray.act("add", {"provider": action[4:]})
        elif action.startswith("relogin:"):  # Sign in again, for that account
            account = next((a for a in tray.state["accounts"] if a["id"] == action[8:]), None)
            if account:
                tray.act("add", {"provider": account["provider"], "id": account["id"]})


class TrayMenu(Popup):
    """Right-click menu drawn in the same style as the flyout."""

    def items(self):
        state = self.tray.state
        rows = [{"action": "panel", "label": "Open panel", "bold": True},
                {"action": "full", "label": "Full view"},
                "-"]
        for item in state.get("pendingResumes") or []:  # a large session waiting for an OK to continue
            rows += [{"action": "resume:yes:" + item["session"], "label": f"Continue large session (~{item['tokens'] // 1000}k tokens)", "bold": True},
                     {"action": "resume:no:" + item["session"], "label": "Don't continue it"}, "-"]
        rows += [
                {"action": "toggle:autoSwap", "label": "Auto swap", "checked": state["autoSwap"], "enabled": not state["busy"]},
                {"action": "toggle:afk", "label": "Auto resume", "checked": state["afk"], "enabled": not state["busy"]}]
        if getattr(self.tray, "taskbar", None):
            rows += ["-", {"action": "toggle:taskbar", "label": "Taskbar view", "checked": state.get("taskbar", True)}]
            displays = state.get("taskbarDisplays") or []
            if len(displays) > 1 and state.get("taskbar", True):  # which display's taskbar
                from . import taskbar_layout
                shown = {key for key, slots in taskbar_layout.layout(state).items() if any(slots)}
                rows += [{"action": "display:" + d["id"], "label": "On " + d["label"][0].lower() + d["label"][1:],
                          "checked": d["id"] in shown} for d in displays]
        return rows + ["-", {"action": "quit", "label": "Quit"}]

    opener = "right"
    take_focus = False       # outside clicks are caught by the mouse watcher instead

    def prepare(self):
        self.point = cursor()
        self.monitor, self.work, self.scale = monitor_at(*self.point)
        rect = icon_rect(getattr(self.tray, "icon", None))
        px, py = self.point
        self.icon_box = (rect.left, rect.top, rect.right, rect.bottom) if rect else (px - 20, py - 20, px + 20, py + 20)

    def position(self, width, height):
        return place_menu(self.point, self.monitor, self.work, width, height, self.scale)

    def render(self, hover):
        return fr.render_menu(self.items(), hover, self.scale, fx=self.fx, painter=self.painter)

    def activate(self, action):
        tray = self.tray
        if action.startswith(("toggle:", "display:")):  # toggles keep the menu open, showing the new state
            key = action[7:] if action.startswith("toggle:") else action
            if key == "taskbar":
                tray.act("taskbar", {"on": not tray.state.get("taskbar", True)})
            elif key.startswith("display:"):
                tray.act("taskbar", {"display": key[8:]})
            else:
                prefs = {"autoSwap": tray.state["autoSwap"], "afk": tray.state["afk"]}
                prefs[key] = not prefs[key]
                tray.act("preferences", prefs)
            tray.state = tray.controller.snapshot()
            self.redraw()
            return
        self.close()
        if action.startswith("resume:"):
            _, answer, session = action.split(":", 2)
            tray.act("resumeSession", {"session": session, "approve": answer == "yes"})
            return
        if action == "panel":
            tray.flyout.open()
        elif action == "full":
            tray.open_full_view()
        elif action == "quit":
            self._destroy()
            tray.quit()


def tray_icon_class():
    """pystray's Win32 icon, with left/right clicks routed to our own popups."""
    import pystray._win32 as backend

    class Icon(backend.Icon):
        popups = None  # (flyout, menu), set by the tray

        def _on_notify(self, wparam, lparam):
            if self.popups and lparam == WM_LBUTTONDOWN:
                # React on press, not release: the release can arrive late (and the panel
                # visibly lingers), while the press is delivered immediately.
                self.popups[1].close()
                flyout = self.popups[0]
                if not (flyout.hwnd and not flyout.closing) and close_overflow():
                    flyout.focus_delay = 150  # our icon was clicked in the hidden-icons popup
                flyout.toggle()
            elif self.popups and lparam in (WM_LBUTTONUP, 0x0203):  # release / double-click: handled on press
                # Windows grants foreground rights on the release; claim them now so the panel
                # is really active (and the hidden-icons popup gets out of the way).
                flyout = self.popups[0]
                if flyout.hwnd and not flyout.closing and not overflow_in_front():
                    force_foreground(flyout.hwnd)
            elif self.popups and lparam == 0x0205:  # WM_RBUTTONUP
                if not self.popups[0].pinned:
                    self.popups[0].close()
                self.popups[1].toggle()  # the menu leaves Windows' tray popup alone
            else:
                super()._on_notify(wparam, lparam)

    return Icon
