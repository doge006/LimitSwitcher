"""The taskbar host process: shows the taskbar blocks as child windows of Explorer's taskbars.

Start and Search lift the taskbar into a window band no ordinary window can reach; a child of
the taskbar goes with it. A child of another process's window shares that thread's input queue,
so this process does nothing but show pixels and report the mouse: commands come on stdin and
events leave on stdout (taskbar_host.Host), both on their own threads, and the window thread only
runs short posted work. The tray process never owns an Explorer child.
"""
import base64
import ctypes
from ctypes import wintypes
import json
import os
import queue
import sys
import threading

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32, kernel32 = ctypes.WinDLL("gdi32"), ctypes.WinDLL("kernel32")
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def sig(function, restype, *argtypes):
    function.restype, function.argtypes = restype, argtypes


sig(user32.DefWindowProcW, LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
sig(user32.CreateWindowExW, wintypes.HWND, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
sig(user32.DestroyWindow, wintypes.BOOL, wintypes.HWND)
sig(user32.IsWindow, wintypes.BOOL, wintypes.HWND)
sig(user32.GetWindow, wintypes.HWND, wintypes.HWND, wintypes.UINT)
sig(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.UINT)
sig(user32.ScreenToClient, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.POINT))
sig(user32.UpdateLayeredWindow, wintypes.BOOL, wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.POINTER(wintypes.SIZE),
    wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
sig(user32.GetDC, wintypes.HDC, wintypes.HWND)
sig(user32.ReleaseDC, ctypes.c_int, wintypes.HWND, wintypes.HDC)
sig(user32.PostMessageW, wintypes.BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
sig(user32.GetMessageW, wintypes.BOOL, ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
sig(user32.DispatchMessageW, LRESULT, ctypes.POINTER(wintypes.MSG))
sig(user32.SetTimer, ctypes.c_size_t, wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p)
sig(user32.LoadCursorW, wintypes.HANDLE, wintypes.HINSTANCE, ctypes.c_void_p)
sig(user32.SetCursor, wintypes.HANDLE, wintypes.HANDLE)
sig(user32.AllowSetForegroundWindow, wintypes.BOOL, wintypes.DWORD)
sig(gdi32.CreateCompatibleDC, wintypes.HDC, wintypes.HDC)
sig(gdi32.DeleteDC, wintypes.BOOL, wintypes.HDC)
sig(gdi32.SelectObject, wintypes.HGDIOBJ, wintypes.HDC, wintypes.HGDIOBJ)
sig(gdi32.DeleteObject, wintypes.BOOL, wintypes.HGDIOBJ)
sig(gdi32.CreateDIBSection, wintypes.HBITMAP, wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
sig(kernel32.GetModuleHandleW, wintypes.HMODULE, wintypes.LPCWSTR)
sig(kernel32.OpenProcess, wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
sig(kernel32.WaitForSingleObject, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)

WM_DESTROY, WM_CLOSE, WM_TIMER, WM_SETCURSOR, WM_MOUSEACTIVATE, WM_NCHITTEST = 0x2, 0x10, 0x113, 0x20, 0x21, 0x84
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN, WM_RBUTTONUP = 0x200, 0x201, 0x202, 0x204, 0x205
WM_CONTEXTMENU, WM_MOUSELEAVE, WM_APP_WORK = 0x7B, 0x2A3, 0x8001
MOUSE = {WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONUP, WM_MOUSELEAVE}
WS_CHILD, WS_CLIPSIBLINGS = 0x40000000, 0x04000000
WS_EX_LAYERED, WS_EX_NOPARENTNOTIFY, WS_EX_NOACTIVATE = 0x80000, 0x4, 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x4, 0x10, 0x40
HWND_MESSAGE, IDC_ARROW, IDC_HAND, TIMER_RAISE = -3, 32512, 32649, 1


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("hwndTrack", wintypes.HWND),
                ("dwHoverTime", wintypes.DWORD)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("proc", WNDPROC), ("extra", ctypes.c_int), ("window_extra", ctypes.c_int),
                ("instance", wintypes.HINSTANCE), ("icon", wintypes.HICON), ("cursor", wintypes.HANDLE),
                ("background", wintypes.HBRUSH), ("menu", wintypes.LPCWSTR), ("name", wintypes.LPCWSTR)]


sig(user32.TrackMouseEvent, wintypes.BOOL, ctypes.POINTER(TRACKMOUSEEVENT))
sig(user32.RegisterClassW, wintypes.ATOM, ctypes.POINTER(WNDCLASSW))


class Window:
    """One block: a layered child of a taskbar and the bitmap it shows."""

    def __init__(self, block_id, parent, hwnd):
        self.id, self.parent, self.hwnd = block_id, parent, hwnd
        self.size = self.memory = self.bitmap = self.old = None
        self.bits = ctypes.c_void_p()
        self.hand = self.tracking = False

    def release(self):
        if self.memory:
            gdi32.SelectObject(self.memory, self.old)
            gdi32.DeleteObject(self.bitmap)
            gdi32.DeleteDC(self.memory)
        self.memory = self.size = None


class HostWindows:
    def __init__(self, tray_pid, write):
        self.tray_pid, self.write = tray_pid, write
        self.work = queue.SimpleQueue()
        self.windows, self.by_hwnd = {}, {}
        self.waiting = {}  # block id -> its latest frame, while its taskbar refuses a new child
        self.proc = WNDPROC(self.wndproc)  # kept alive for the windows' lifetime
        instance = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(WNDCLASSW(0, self.proc, 0, 0, instance, None, None, None, None,
                                                     "LimitSwitcherTaskbarHost")))
        self.control = user32.CreateWindowExW(0, "LimitSwitcherTaskbarHost", "", 0, 0, 0, 0, 0, HWND_MESSAGE,
                                              None, instance, None)
        user32.SetTimer(self.control, TIMER_RAISE, 500, None)

    def post(self, command):
        """Reader thread: hand a decoded command to the window thread."""
        self.work.put(command)
        user32.PostMessageW(self.control, WM_APP_WORK, 0, 0)

    def apply(self, command):
        op = command.get("op")
        if op == "ping":
            self.write(dict(type="pong"))
        elif op == "remove":
            self.waiting.pop(command["id"], None)
            self.destroy(command["id"])
        elif op == "update":
            self.update(command)

    def destroy(self, block_id):
        window = self.windows.pop(block_id, None)
        if window:
            self.by_hwnd.pop(window.hwnd, None)
            user32.DestroyWindow(window.hwnd)
            window.release()

    def update(self, frame):
        block_id, parent = frame["id"], frame["parent"]
        window = self.windows.get(block_id)
        if window and (window.parent != parent or not user32.IsWindow(window.hwnd)):
            self.destroy(block_id)
            window = None
        created = window is None
        if created:
            hwnd = user32.CreateWindowExW(WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_NOPARENTNOTIFY,
                                          "LimitSwitcherTaskbarHost", "LimitSwitcher", WS_CHILD | WS_CLIPSIBLINGS,
                                          0, 0, 1, 1, parent, None, kernel32.GetModuleHandleW(None), None)
            if not hwnd:
                error = ctypes.get_last_error()
                if error == 5:
                    # Access denied: Start or Search has the taskbar in a higher band, and Explorer
                    # can leave it there a while. A child made earlier survives; retry until then.
                    self.waiting[block_id] = frame
                else:
                    self.write(dict(type="failed", id=block_id, reason="create", error=error, retry=False))
                return
            self.waiting.pop(block_id, None)
            window = self.windows[block_id] = self.by_hwnd[hwnd] = Window(block_id, parent, hwnd)
        width, height = frame["width"], frame["height"]
        if window.size != (width, height):
            window.release()
            screen = user32.GetDC(None)
            header = (ctypes.c_int32 * 10)(40, width, -height, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)  # 32 bpp, top-down
            window.memory = gdi32.CreateCompatibleDC(screen)
            window.bitmap = gdi32.CreateDIBSection(screen, header, 0, ctypes.byref(window.bits), None, 0)
            user32.ReleaseDC(None, screen)
            window.old = gdi32.SelectObject(window.memory, window.bitmap)
            window.size = (width, height)
        ctypes.memmove(window.bits.value, frame["rgba"], width * height * 4)
        if window.hand != frame.get("hand", False):
            window.hand = not window.hand
            if window.tracking:
                self.cursor(window)  # the tray just saw the mouse arrive on (or leave) a button
        point = wintypes.POINT(frame["x"], frame["y"])
        user32.ScreenToClient(parent, ctypes.byref(point))
        # A new block goes above the taskbar's XAML content, a sibling child covering the whole bar.
        # Otherwise keep the order: each reorder makes the tray lay the taskbar out again.
        user32.SetWindowPos(window.hwnd, None, point.x, point.y, width, height,
                            SWP_NOACTIVATE | SWP_SHOWWINDOW | (0 if created else SWP_NOZORDER))
        screen = user32.GetDC(None)
        blend = ctypes.c_uint32(0x01000000 | (max(0, min(255, round(frame.get("alpha", 1.0) * 255))) << 16))
        user32.UpdateLayeredWindow(window.hwnd, screen, None, ctypes.byref(wintypes.SIZE(width, height)), window.memory,
                                   ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), 2)  # ULW_ALPHA
        user32.ReleaseDC(None, screen)

    @staticmethod
    def cursor(window):
        user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND if window and window.hand else IDC_ARROW)))

    def raise_blocks(self):
        """Explorer can bring its own content back over ours (Start, Search, a new layout)."""
        covered = set()
        for window in self.windows.values():
            top = user32.GetWindow(window.parent, 5)  # GW_CHILD: the topmost child (none while Start is open)
            if top and top not in self.by_hwnd:
                covered.add(window.parent)
        for window in list(self.windows.values()):
            if window.parent in covered:
                user32.SetWindowPos(window.hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if hwnd == self.control:
                if msg == WM_APP_WORK:
                    while True:
                        try:
                            self.apply(self.work.get_nowait())
                        except queue.Empty:
                            break
                    return 0
                if msg == WM_TIMER and wparam == TIMER_RAISE:
                    for frame in list(self.waiting.values()):
                        self.update(frame)
                    self.raise_blocks()
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            window = self.by_hwnd.get(hwnd)
            if msg == WM_MOUSEACTIVATE:
                return 3  # MA_NOACTIVATE
            if msg == WM_NCHITTEST:
                return 1  # HTCLIENT
            if msg == WM_SETCURSOR:
                self.cursor(window)
                return 1
            if msg in (WM_RBUTTONDOWN, WM_CONTEXTMENU):
                return 0  # never let Explorer's taskbar menu open from our block
            if window and msg in MOUSE:
                if msg == WM_MOUSEMOVE:
                    if not window.tracking:
                        track = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), 2, hwnd, 0)  # TME_LEAVE
                        window.tracking = bool(user32.TrackMouseEvent(ctypes.byref(track)))
                    self.cursor(window)  # Explorer's shared input queue can reset it between messages
                elif msg == WM_MOUSELEAVE:
                    window.tracking = False
                elif msg != WM_MOUSEMOVE:
                    user32.AllowSetForegroundWindow(self.tray_pid)  # the panel this click opens takes focus
                x, y = ctypes.c_short(lparam & 0xFFFF).value, ctypes.c_short((lparam >> 16) & 0xFFFF).value
                self.write(dict(type="mouse", id=window.id, msg=msg, x=x, y=y))
                return 0
            if msg == WM_DESTROY and window:
                # Explorer restarted (its taskbar took our child with it), or we removed it.
                if self.windows.get(window.id) is window:
                    del self.windows[window.id]
                    self.by_hwnd.pop(hwnd, None)
                    window.release()
                    self.write(dict(type="destroyed", id=window.id))
                return 0
        except Exception:
            pass  # one bad frame must never stop this thread pumping Explorer's shared input
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main():
    tray_pid = int(sys.argv[1])
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # physical pixels, as the tray uses
    except (AttributeError, OSError):
        pass
    events = queue.SimpleQueue()

    def writer():
        out = sys.stdout.buffer
        while True:
            out.write((json.dumps(events.get(), separators=(",", ":")) + "\n").encode("ascii"))
            out.flush()

    host = HostWindows(tray_pid, events.put)

    def reader():
        for line in sys.stdin.buffer:
            try:
                command = json.loads(line)
                if "rgba" in command:
                    command["rgba"] = base64.b64decode(command["rgba"])
                    if len(command["rgba"]) != command["width"] * command["height"] * 4:
                        continue
            except (ValueError, KeyError, TypeError):
                continue
            host.post(command)
        os._exit(0)  # the tray closed the pipe: Windows removes our windows with the process

    def watch_tray():
        handle = kernel32.OpenProcess(0x00100000, False, tray_pid)  # SYNCHRONIZE
        if handle:
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        os._exit(0)

    for target in (writer, reader, watch_tray):
        threading.Thread(target=target, daemon=True).start()
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
