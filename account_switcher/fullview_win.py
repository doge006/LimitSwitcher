"""Windows host for the full view: an ordinary window of the app's own (no browser).

It lives on the tray's thread, like the panel: pystray's message loop dispatches its messages.
Frames come from fullview.FullView and are blitted with SetDIBitsToDevice. It exists only while
open; closing it destroys the window and frees the cached tiles. Other threads use post_*().
"""
import ctypes
from ctypes import wintypes
import logging
from pathlib import Path

from . import fullview_render as vr
from .fullview import FullView, window_size

log = logging.getLogger("account_switcher.fullview")
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
H = wintypes.HANDLE

CLASS_NAME = "AccountSwitcherFullView"
TITLE = "LimitSwitcher"
ICON = Path(__file__).with_name("static") / "assets" / "switcher.ico"
WS_OVERLAPPEDWINDOW = 0x00CF0000
WM_DESTROY, WM_SIZE, WM_PAINT, WM_CLOSE, WM_ERASEBKGND = 0x0002, 0x0005, 0x000F, 0x0010, 0x0014
WM_SETCURSOR, WM_GETMINMAXINFO, WM_KEYDOWN, WM_CHAR, WM_TIMER = 0x0020, 0x0024, 0x0100, 0x0102, 0x0113
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_MOUSEWHEEL, WM_MOUSELEAVE = 0x0200, 0x0201, 0x0202, 0x020A, 0x02A3
WM_DPICHANGED, WM_ACTIVATE = 0x02E0, 0x0006
WM_APP_STATE, WM_APP_SHOW, WM_APP_RENDER = 0x8000 + 31, 0x8000 + 32, 0x8000 + 33
HTCLIENT, TME_LEAVE, SW_SHOW, SW_RESTORE = 1, 2, 5, 9
CURSORS = {"arrow": 32512, "hand": 32649, "text": 32513}
KEYS = {0x1B: "escape", 0x0D: "enter", 0x08: "backspace", 0x2E: "delete", 0x25: "left", 0x27: "right",
        0x24: "home", 0x23: "end", 0x41: "a", 0x56: "v"}
TIMERS = {"minute": 11, "toast": 12, "pending": 13, "anim": 14}


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", H), ("hCursor", H), ("hbrBackground", H), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", H)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", wintypes.POINT), ("ptMaxSize", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("ptMinTrackSize", wintypes.POINT), ("ptMaxTrackSize", wintypes.POINT)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


def _sig(fn, restype, *argtypes):
    fn.restype, fn.argtypes = restype, list(argtypes)


_sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.RegisterClassExW, wintypes.ATOM, ctypes.POINTER(WNDCLASSEXW))
_sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, H, wintypes.HINSTANCE, ctypes.c_void_p)
_sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.ShowWindow, wintypes.BOOL, wintypes.HWND, ctypes.c_int)
_sig(user32.IsIconic, wintypes.BOOL, wintypes.HWND)
_sig(user32.SetForegroundWindow, wintypes.BOOL, wintypes.HWND)
_sig(user32.BeginPaint, wintypes.HDC, wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
_sig(user32.EndPaint, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
_sig(user32.InvalidateRect, wintypes.BOOL, wintypes.HWND, ctypes.c_void_p, wintypes.BOOL)
_sig(user32.GetClientRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_sig(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, wintypes.UINT)
_sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_sig(user32.SetTimer, ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p)
_sig(user32.KillTimer, wintypes.BOOL, wintypes.HWND, ctypes.c_size_t)
_sig(user32.TrackMouseEvent, wintypes.BOOL, ctypes.POINTER(TRACKMOUSEEVENT))
_sig(user32.SetCursor, H, H)
_sig(user32.LoadCursorW, H, wintypes.HINSTANCE, ctypes.c_void_p)
_sig(user32.LoadImageW, H, wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT)
_sig(user32.GetKeyState, ctypes.c_short, ctypes.c_int)
_sig(user32.GetDpiForWindow, wintypes.UINT, wintypes.HWND)
_sig(user32.MonitorFromPoint, H, wintypes.POINT, wintypes.DWORD)
_sig(user32.GetMonitorInfoW, wintypes.BOOL, H, ctypes.POINTER(MONITORINFO))
_sig(user32.GetCursorPos, wintypes.BOOL, ctypes.POINTER(wintypes.POINT))
_sig(user32.OpenClipboard, wintypes.BOOL, wintypes.HWND)
_sig(user32.CloseClipboard, wintypes.BOOL)
_sig(user32.GetClipboardData, H, wintypes.UINT)
_sig(user32.SetCapture, wintypes.HWND, wintypes.HWND)
_sig(user32.ReleaseCapture, wintypes.BOOL)
_sig(kernel32.GlobalLock, ctypes.c_void_p, H)
_sig(kernel32.GlobalUnlock, wintypes.BOOL, H)
_sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
_sig(gdi32.SetDIBitsToDevice, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD, wintypes.DWORD,
     ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
    _sig(dwmapi.DwmSetWindowAttribute, ctypes.c_long, wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
except OSError:
    dwmapi = None


def _xy(lparam):
    return ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value


class FullViewWindow:
    """The full view's window. show()/post_state() may be called from any thread."""
    _proc = None
    _registered = False

    def __init__(self, tray):
        self.tray = tray
        self.hwnd = None
        self.view = None
        self.scale = 1.0
        self.frame = None          # the view's latest frame (an RGB image); converted where painted
        self.dirty = True
        self.render_posted = False
        self.tracking = False
        self.cursor = "arrow"
        self.timers = set()

    # ---------- any thread ----------
    def post_show(self):
        """Open the window, or bring it to the front: on the tray thread."""
        hwnd = getattr(getattr(self.tray, "icon", None), "_hwnd", None)
        if hwnd:  # windows belong to the thread that makes them: always the tray's
            user32.PostMessageW(hwnd, WM_APP_SHOW, 0, 0)
        else:
            log.error("full view: the tray isn't running yet")

    def dismiss(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    def post_state(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_STATE, 0, 0)

    # ---------- the tray thread ----------
    def show(self):
        if self.hwnd:
            log.warning("full view: brought to the front")
            if user32.IsIconic(self.hwnd):
                user32.ShowWindow(self.hwnd, SW_RESTORE)
            from .flyout import force_foreground
            force_foreground(self.hwnd)
            return
        log.warning("full view: opening")
        self._register()
        width, height, x, y, scale = self.placement()
        self.scale = scale
        self.view = FullView(self.tray.controller, self, self.tray.state)
        self.hwnd = user32.CreateWindowExW(0, CLASS_NAME, TITLE, WS_OVERLAPPEDWINDOW, x, y, width, height,
                                           None, None, kernel32.GetModuleHandleW(None), None)
        if not self.hwnd:
            log.error("full view window: %s", ctypes.get_last_error())
            self.view = None
            return
        FullViewWindow._windows[self.hwnd] = self
        self.scale = (user32.GetDpiForWindow(self.hwnd) or 96) / 96
        self.style_title_bar()
        try:  # the taskbar button: our name and icon, pinned too, not Python's
            from .integrations import launcher
            from .win_window import set_identity
            set_identity(self.hwnd, launcher(), ICON)
        except Exception:
            log.warning("full view taskbar identity", exc_info=True)
        self.on_size()
        self.fit_content(x, y, width, height)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        from .flyout import force_foreground
        force_foreground(self.hwnd)

    def placement(self):
        """Size (the app's full-view size) and centre on the display with the pointer, in device px."""
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        monitor = user32.MonitorFromPoint(point, 2)
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(info)
        user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        scale = 1.0
        try:
            dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
            if ctypes.windll.shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:
                scale = dpi_x.value / 96
        except (AttributeError, OSError):
            pass
        work = info.rcWork
        area_w, area_h = work.right - work.left, work.bottom - work.top
        logical_w, logical_h = window_size(area_w / scale, area_h / scale)
        width, height = min(area_w, round(logical_w * scale)), min(area_h, round(logical_h * scale))
        return width, height, work.left + (area_w - width) // 2, work.top + (area_h - height) // 2, scale

    def fit_content(self, x, y, width, height):
        """No empty space under the cards: open only as tall as the page (keeping it centred)."""
        rect = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        spare = rect.bottom - round(max(self.view.content_h, 420) * self.scale)
        if spare > 0:
            user32.SetWindowPos(self.hwnd, None, x, y + spare // 2, width, height - spare, 0x0014)  # NOZORDER|NOACTIVATE
            self.on_size()

    def style_title_bar(self):
        """Dark title bar in our background colour, so the window reads as one surface."""
        if not dwmapi:
            return
        on = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(self.hwnd, 20, ctypes.byref(on), 4)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        r, g, b = vr.BG
        color = ctypes.c_uint(r | g << 8 | b << 16)
        dwmapi.DwmSetWindowAttribute(self.hwnd, 35, ctypes.byref(color), 4)  # DWMWA_CAPTION_COLOR (Windows 11)

    # ---------- the host interface FullView uses ----------
    def invalidate(self):
        """Draw a new frame soon (once, however many changes come in before it)."""
        self.dirty = True
        if self.hwnd and not self.render_posted:
            self.render_posted = True
            user32.PostMessageW(self.hwnd, WM_APP_RENDER, 0, 0)

    def render(self):
        """Draw the view's next frame and mark what it changed for repainting."""
        self.render_posted = False
        if not self.dirty or not self.view:
            return
        self.dirty = False
        previous = self.frame
        self.frame = self.view.frame()
        changed = self.view.changed
        if previous is None or previous.size != self.frame.size or changed is None:
            user32.InvalidateRect(self.hwnd, None, False)
            return
        for x0, y0, x1, y1 in changed:
            user32.InvalidateRect(self.hwnd, ctypes.byref(wintypes.RECT(x0, y0, x1, y1)), False)

    def set_timer(self, name, ms):
        if self.hwnd:
            user32.SetTimer(self.hwnd, TIMERS[name], max(10, int(ms)), None)
            self.timers.add(name)

    def kill_timer(self, name):
        if self.hwnd and name in self.timers:
            user32.KillTimer(self.hwnd, TIMERS[name])
        self.timers.discard(name)

    def has_timer(self, name):
        return name in self.timers

    def set_cursor(self, kind):
        self.cursor = kind

    def clipboard(self):
        text = ""
        if user32.OpenClipboard(self.hwnd):
            try:
                handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
                if handle:
                    pointer = kernel32.GlobalLock(handle)
                    if pointer:
                        text = ctypes.wstring_at(pointer)
                        kernel32.GlobalUnlock(handle)
            finally:
                user32.CloseClipboard()
        return text

    # ---------- window messages ----------
    def on_size(self):
        rect = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rect))
        if rect.right and rect.bottom:
            self.view.resize(rect.right / self.scale, rect.bottom / self.scale, self.scale)
            self.invalidate()

    def paint(self):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
        try:
            if self.frame is None:
                self.dirty = True
                self.render()
            if self.frame is not None:
                # Only the area that needs painting (what render() marked, and anything uncovered)
                # is converted to Windows' pixel format and copied: no second copy of the window.
                area = ps.rcPaint
                x0, y0 = max(0, area.left), max(0, area.top)
                x1, y1 = min(self.frame.width, area.right), min(self.frame.height, area.bottom)
                if x1 > x0 and y1 > y0:
                    width, height = x1 - x0, y1 - y0
                    whole = (x0, y0, x1, y1) == (0, 0) + self.frame.size  # each frame of a scroll: no extra copy
                    data = (self.frame if whole else self.frame.crop((x0, y0, x1, y1))).tobytes("raw", "BGRX")
                    header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
                    gdi32.SetDIBitsToDevice(hdc, x0, y0, width, height, 0, 0, 0, height, data, ctypes.byref(header), 0)
        finally:
            user32.EndPaint(self.hwnd, ctypes.byref(ps))

    def logical(self, lparam):
        x, y = _xy(lparam)
        return x / self.scale, y / self.scale

    def wndproc(self, hwnd, msg, wparam, lparam):
        view = self.view
        if msg == WM_PAINT:
            self.paint()
            return 0
        if msg == WM_ERASEBKGND:
            return 1  # every pixel is painted: no white flash
        if msg == WM_SIZE:
            self.on_size()
            return 0
        if msg == WM_APP_RENDER:
            self.render()
            return 0
        if msg == WM_APP_STATE:
            view.set_state(self.tray.state)
            return 0
        if msg == WM_MOUSEMOVE:
            if not self.tracking:
                track = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                self.tracking = bool(user32.TrackMouseEvent(ctypes.byref(track)))
            view.mouse_move(*self.logical(lparam))
            user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(CURSORS.get(self.cursor, 32512))))
            return 0
        if msg == WM_MOUSELEAVE:
            self.tracking = False
            view.mouse_leave()
            return 0
        if msg == WM_SETCURSOR and (lparam & 0xFFFF) == HTCLIENT:
            user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(CURSORS.get(self.cursor, 32512))))
            return 1
        if msg == WM_LBUTTONDOWN:
            user32.SetCapture(hwnd)
            view.mouse_down(*self.logical(lparam))
            return 0
        if msg == WM_LBUTTONUP:
            user32.ReleaseCapture()
            view.mouse_up(*self.logical(lparam))
            return 0
        if msg == WM_MOUSEWHEEL:
            delta = ctypes.c_short((wparam >> 16) & 0xFFFF).value
            # Whole notches (120) glide; a touchpad's small steps are already smooth, so they don't.
            view.wheel(-delta / 120 * vr.SCROLL_STEP, glide=delta % 120 == 0)
            return 0
        if msg == WM_KEYDOWN:
            name = KEYS.get(wparam)
            ctrl = bool(user32.GetKeyState(0x11) & 0x8000)
            if name and (name not in ("a", "v") or ctrl):
                view.key(name, ctrl)
            return 0
        if msg == WM_CHAR:
            if wparam >= 32 and not user32.GetKeyState(0x11) & 0x8000:
                view.char(chr(wparam))
            return 0
        if msg == WM_TIMER:
            name = next((n for n, i in TIMERS.items() if i == wparam), None)
            if name:
                view.timer(name)
            return 0
        if msg == WM_GETMINMAXINFO:
            info = ctypes.cast(lparam, ctypes.POINTER(MINMAXINFO)).contents
            info.ptMinTrackSize.x, info.ptMinTrackSize.y = round(520 * self.scale), round(420 * self.scale)
            return 0
        if msg == WM_DPICHANGED:  # moved to a display with another scale: take its size and redraw sharp
            self.scale = ((wparam & 0xFFFF) or 96) / 96
            rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            user32.SetWindowPos(hwnd, None, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, 0x0014)
            self.on_size()
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            for name in list(self.timers):
                self.kill_timer(name)
            FullViewWindow._windows.pop(hwnd, None)
            self.hwnd = None
            if view:
                view.close()
            self.view = self.frame = None
            from .memory import trim_soon
            trim_soon()  # its tiles and frame are gone: hand the memory back
            log.warning("full view: closed")
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    _windows = {}
    _creating = None

    @classmethod
    def _register(cls):
        if cls._registered:
            return

        def proc(hwnd, msg, wparam, lparam):
            owner = cls._windows.get(hwnd)
            if owner is not None and owner.view is not None:
                try:
                    return owner.wndproc(hwnd, msg, wparam, lparam)
                except Exception:
                    log.exception("full view message %#x failed", msg)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        cls._proc = WNDPROC(proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0x0003  # CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = cls._proc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
        wc.hIcon = user32.LoadImageW(None, str(ICON), 1, 32, 32, 0x10)    # IMAGE_ICON, LR_LOADFROMFILE
        wc.hIconSm = user32.LoadImageW(None, str(ICON), 1, 16, 16, 0x10)
        wc.lpszClassName = CLASS_NAME
        user32.RegisterClassExW(ctypes.byref(wc))
        cls._registered = True
