"""Windows taskbar view: the account in use for each provider, drawn in the taskbar's empty space.

One small block per provider (Claude from the left, Codex from the right), each the compact
panel's row laid out sideways: icon, name, and a column per limit with "% left" and
"resets in". The blocks are layered, click-through-free popups that never take focus; a
click opens the panel above the block, a right-click opens the menu.

Nothing polls. The layout is worked out again only when the taskbar can have changed
(a window opened or closed, display or theme settings changed, Explorer restarted) and once
a minute with the reset times. The blocks are owned by the taskbar, so Windows keeps them
just above it, and they hide while an app is full screen on that display (see follow_taskbar);
screenshot tools and other overlays never make them hide and come back.
"""
import ctypes
from ctypes import wintypes
import logging
import math
import time
import winreg

from . import flyout as fl
from . import flyout_render as fr
from . import taskbar_layout
from .placement import free_gaps, place_blocks, taskbar_buttons
from .profiler import event

user32, kernel32 = fl.user32, fl.kernel32
log = logging.getLogger("account_switcher.taskbar")

WM_APP_TASKBAR = 0x8000 + 20
WM_TIMER, WM_SETTINGCHANGE, WM_DISPLAYCHANGE = 0x0113, 0x001A, 0x007E
WM_RBUTTONUP, WM_MOUSEACTIVATE, MA_NOACTIVATE, WM_DESTROY = 0x0205, 0x0021, 3, 0x0002
WS_EX_NOACTIVATE = 0x08000000
TIMER_LAYOUT, TIMER_MINUTE, TIMER_COVER = 71, 72, 73
HSHELL_WINDOWACTIVATED, WS_EX_TOPMOST, SW_HIDE, SW_SHOWNA = 4, 0x8, 0, 8
HSHELL_WINDOWCREATED, HSHELL_WINDOWDESTROYED = 1, 2
ABM_NEW, ABM_REMOVE, ABN_STATECHANGE, ABN_POSCHANGED, ABN_FULLSCREENAPP = 0, 1, 0, 1, 2
PROVIDER_ORDER = ("claude", "codex")


class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uCallbackMessage", wintypes.UINT),
                ("uEdge", wintypes.UINT), ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


sig = fl._sig
sig(user32.FindWindowW, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
sig(user32.FindWindowExW, wintypes.HWND, wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR)
sig(user32.GetWindowRect, wintypes.BOOL, wintypes.HWND, ctypes.POINTER(wintypes.RECT))
sig(user32.IsWindowVisible, wintypes.BOOL, wintypes.HWND)
sig(user32.IsWindow, wintypes.BOOL, wintypes.HWND)
sig(user32.GetWindowLongW, wintypes.LONG, wintypes.HWND, ctypes.c_int)
sig(user32.RegisterShellHookWindow, wintypes.BOOL, wintypes.HWND)
sig(user32.DeregisterShellHookWindow, wintypes.BOOL, wintypes.HWND)
sig(user32.RegisterWindowMessageW, wintypes.UINT, wintypes.LPCWSTR)
shell32 = ctypes.WinDLL("shell32")
sig(shell32.SHAppBarMessage, ctypes.c_size_t, wintypes.DWORD, ctypes.POINTER(APPBARDATA))
sig(shell32.SHQueryUserNotificationState, ctypes.c_long, ctypes.POINTER(ctypes.c_int))
sig(user32.GetAncestor, wintypes.HWND, wintypes.HWND, wintypes.UINT)
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x80, 0x40000
sig(user32.GetWindow, wintypes.HWND, wintypes.HWND, wintypes.UINT)
sig(user32.IsIconic, wintypes.BOOL, wintypes.HWND)
sig(user32.GetWindowTextW, ctypes.c_int, wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
    sig(dwmapi.DwmGetWindowAttribute, ctypes.c_long, wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
except OSError:
    dwmapi = None
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def window_rect(hwnd):
    rect = wintypes.RECT()
    if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return rect.left, rect.top, rect.right, rect.bottom
    return None


def setting(name, default, key=r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except OSError:
        return default


def light_taskbar():
    return bool(setting("SystemUsesLightTheme", 0, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"))


def auto_hide():
    data = APPBARDATA(ctypes.sizeof(APPBARDATA))
    return bool(shell32.SHAppBarMessage(4, ctypes.byref(data)) & 1)  # ABM_GETSTATE & ABS_AUTOHIDE


# ---------- the taskbar's own buttons, through UI Automation (plain COM calls) ----------
class _COM:
    """Just enough IUIAutomation to list the taskbar's buttons and where they are."""
    CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"   # CUIAutomation
    IID = "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}"     # IUIAutomation
    # Buttons (task buttons, Start, Search, Task view, Widgets), list items and menu items,
    # split buttons, and the search box.
    KINDS = {50000, 50004, 50007, 50011, 50031}
    ole32 = None
    automation = None

    @staticmethod
    def call(obj, index, argtypes=(), *args, restype=ctypes.c_long):
        """Method `index` of a COM object's vtable."""
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])(obj, *args)

    @classmethod
    def release(cls, obj):
        if obj:
            cls.call(obj, 2, restype=ctypes.c_ulong)

    @classmethod
    def start(cls):
        if cls.automation:
            return cls.automation
        cls.ole32 = ctypes.OleDLL("ole32")
        try:
            cls.ole32.CoInitializeEx(None, 2)  # apartment-threaded, the tray thread's
        except OSError:
            pass  # already initialised differently: still usable
        clsid, iid = fl.GUID(), fl.GUID()
        cls.ole32.CLSIDFromString(cls.CLSID, ctypes.byref(clsid))
        cls.ole32.CLSIDFromString(cls.IID, ctypes.byref(iid))
        automation = ctypes.c_void_p()
        cls.ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(automation))
        cls.automation = automation
        return automation

    # IUIAutomation / IUIAutomationElement / IUIAutomationCacheRequest vtable slots
    CREATE_CACHE_REQUEST, CREATE_TRUE_CONDITION, ELEMENT_FROM_HANDLE = 20, 21, 6
    FIND_ALL, FIND_ALL_BUILD_CACHE = 6, 8
    CURRENT_CONTROL_TYPE, CURRENT_RECT, CACHED_CONTROL_TYPE, CACHED_RECT = 21, 43, 53, 75
    ADD_PROPERTY, PUT_TREE_FILTER, PUT_ELEMENT_MODE = 3, 9, 11
    CREATE_PROPERTY_CONDITION, CREATE_OR_CONDITION = 23, 28
    CONTROL_TYPE_ID, RECT_ID = 30003, 30001
    kinds_condition = None

    class VARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                    ("value", ctypes.c_int32), ("pad", ctypes.c_int32), ("pad2", ctypes.c_void_p)]  # 24 bytes

    @classmethod
    def kinds(cls, automation):
        """A condition matching only KINDS (made once): Explorer then sends back only those, a few
        dozen elements instead of every element of the taskbar. None if it can't be made."""
        if cls.kinds_condition is None:
            out = (ctypes.POINTER(ctypes.c_void_p),)
            combined = None
            try:
                for kind in sorted(cls.KINDS):
                    one = ctypes.c_void_p()
                    if cls.call(automation, cls.CREATE_PROPERTY_CONDITION, (ctypes.c_int, cls.VARIANT) + out,
                                cls.CONTROL_TYPE_ID, cls.VARIANT(3, 0, 0, 0, kind, 0, None), ctypes.byref(one)) < 0 or not one:
                        raise OSError("property condition")
                    if combined is None:
                        combined = one
                        continue
                    both = ctypes.c_void_p()
                    ok = cls.call(automation, cls.CREATE_OR_CONDITION, (ctypes.c_void_p, ctypes.c_void_p) + out,
                                  combined, one, ctypes.byref(both)) >= 0 and both
                    cls.release(one)
                    cls.release(combined)
                    combined = both if ok else None
                    if combined is None:
                        raise OSError("or condition")
                cls.kinds_condition = combined
            except OSError:
                if combined:
                    cls.release(combined)
                cls.kinds_condition = False
        return cls.kinds_condition or None

    @classmethod
    def buttons(cls, hwnd):
        """[(left, right, top, bottom)] of the button-like elements under a window.

        One request to Explorer brings every element with its type and position (a cache
        request); reading them one by one would be three round trips per element, and the
        Windows 11 taskbar has hundreds."""
        automation = cls.start()
        element, condition, found, cache = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        spans = []
        try:
            out = (ctypes.POINTER(ctypes.c_void_p),)
            if cls.call(automation, cls.ELEMENT_FROM_HANDLE, (wintypes.HWND,) + out, hwnd, ctypes.byref(element)) < 0 \
                    or not element:
                return None
            if cls.call(automation, cls.CREATE_TRUE_CONDITION, out, ctypes.byref(condition)) < 0:
                return None
            wanted = cls.kinds(automation) or condition  # only the buttons, when that condition could be made
            cached = (cls.call(automation, cls.CREATE_CACHE_REQUEST, out, ctypes.byref(cache)) >= 0 and cache
                      and cls.call(cache, cls.ADD_PROPERTY, (ctypes.c_int,), cls.CONTROL_TYPE_ID) >= 0
                      and cls.call(cache, cls.ADD_PROPERTY, (ctypes.c_int,), cls.RECT_ID) >= 0
                      and cls.call(cache, cls.PUT_TREE_FILTER, (ctypes.c_void_p,), condition) >= 0
                      and cls.call(cache, cls.PUT_ELEMENT_MODE, (ctypes.c_int,), 0) >= 0  # cached values only
                      and cls.call(element, cls.FIND_ALL_BUILD_CACHE, (ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p) + out,
                                   4, wanted, cache, ctypes.byref(found)) >= 0 and found)
            if not cached:  # one element at a time, the slow way
                found = ctypes.c_void_p()
                if cls.call(element, cls.FIND_ALL, (ctypes.c_int, ctypes.c_void_p) + out, 4, wanted,
                            ctypes.byref(found)) < 0 or not found:
                    return None  # FindAll(TreeScope_Descendants)
            kind_slot, rect_slot = (cls.CACHED_CONTROL_TYPE, cls.CACHED_RECT) if cached else \
                (cls.CURRENT_CONTROL_TYPE, cls.CURRENT_RECT)
            count = ctypes.c_int()
            cls.call(found, 3, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(count))  # get_Length
            for i in range(min(count.value, 400)):
                item = ctypes.c_void_p()
                if cls.call(found, 4, (ctypes.c_int,) + out, i, ctypes.byref(item)) < 0 or not item:  # GetElement
                    continue
                try:
                    kind, rect = ctypes.c_int(), wintypes.RECT()
                    cls.call(item, kind_slot, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(kind))
                    if kind.value in cls.KINDS and cls.call(item, rect_slot, (ctypes.POINTER(wintypes.RECT),), ctypes.byref(rect)) >= 0:
                        spans.append((rect.left, rect.right, rect.top, rect.bottom))
                finally:
                    cls.release(item)
        finally:
            for obj in (found, cache, condition, element):
                cls.release(obj)
        return spans


class Bar:
    """One taskbar's geometry, in physical pixels."""

    def __init__(self, hwnd, key, label, rect, scale, left, right, occupied, light, measured):
        self.hwnd, self.key, self.label = hwnd, key, label
        self.rect, self.scale, self.light, self.measured = rect, scale, light, measured
        self.left, self.right, self.occupied = left, right, occupied
        self.signature = None

    @property
    def height(self):
        """Block height in logical px: the taskbar's less 2 px above and below (44 on Windows 11)."""
        return max(30, min(44, round((self.rect[3] - self.rect[1]) / self.scale) - 4))


def taskbars():
    """[(hwnd, key, label)]: the main taskbar, then those on other displays (when Windows shows
    the taskbar on all displays), named by where they are next to the main one."""
    main = user32.FindWindowW("Shell_TrayWnd", None)
    found = [(main, "main", "Main display")] if main else []
    main_rect = window_rect(main)
    others = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        name = ctypes.create_unicode_buffer(40)
        user32.GetClassNameW(hwnd, name, 40)
        rect = window_rect(hwnd)
        if name.value == "Shell_SecondaryTrayWnd" and rect and user32.IsWindowVisible(hwnd):
            others.append((rect, hwnd))
        return True

    user32.EnumWindows(each, 0)
    counts = {}
    for rect, hwnd in sorted(others):
        side = "left" if main_rect and rect[0] < main_rect[0] else "right"
        counts[side] = counts.get(side, 0) + 1
        suffix = f" {counts[side]}" if counts[side] > 1 else ""
        found.append((hwnd, side + suffix.replace(" ", "-"), f"{side.title()} display{suffix}"))
    return found


def full_screen_app(bar_rect):
    """Is an app full screen on the display with this taskbar? The foreground app window covering
    the whole display (games, video, a browser's F11: Explorer does not always lower the taskbar for
    these), or Windows reporting a full-screen Direct3D app or presentation mode. Screenshot tools
    and other overlays are tool windows without a taskbar button, so they don't count."""
    state = ctypes.c_int()
    if shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in (3, 4):
        return True  # QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE
    window = user32.GetForegroundWindow()
    if not window:
        return False
    window = user32.GetAncestor(window, 2) or window  # GA_ROOT
    name = ctypes.create_unicode_buffer(40)
    user32.GetClassNameW(window, name, 40)
    if name.value in SHELL_CLASSES or user32.GetWindowLongW(window, -20) & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
        return False
    rect = window_rect(window)
    if not rect:
        return False
    screen, _, _ = fl.monitor_at((bar_rect[0] + bar_rect[2]) // 2, (bar_rect[1] + bar_rect[3]) // 2)
    return covers(rect, screen)


def covers(rect, screen):
    return bool(rect) and (rect[0] <= screen.left and rect[1] <= screen.top and rect[2] >= screen.right
                           and rect[3] >= screen.bottom)


def covered_from_above(bar_hwnd, bar_rect, ours, tool_windows):
    """Is the taskbar under a window covering its whole display? Walks the windows above it in
    z-order (those can hide it): visible, not minimized, not cloaked, not ours. The taskbar view's
    own check that does not depend on which window has focus (a video switching to full screen
    from its own button, Steam's player). Tool windows count only while Explorer says a full-screen
    app is open (`tool_windows`): otherwise they are screenshot tools and overlays."""
    screen, _, _ = fl.monitor_at((bar_rect[0] + bar_rect[2]) // 2, (bar_rect[1] + bar_rect[3]) // 2)
    window = user32.GetWindow(bar_hwnd, 3)  # GW_HWNDPREV: the next window up
    for _ in range(300):
        if not window:
            return False
        if window not in ours and user32.IsWindowVisible(window) and not user32.IsIconic(window):
            ex = user32.GetWindowLongW(window, -20)
            cloaked = ctypes.c_int(0)
            if dwmapi and dwmapi.DwmGetWindowAttribute(window, 14, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
                pass
            elif ex & 0x20:  # WS_EX_TRANSPARENT: a click-through overlay, never what hides the taskbar
                pass
            elif (tool_windows or not ex & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)) and covers(window_rect(window), screen):
                return True
        window = user32.GetWindow(window, 3)
    return False


def button_windows(with_titles):
    """The windows that have a taskbar button (and their titles when the taskbar shows them):
    what the taskbar's buttons depend on. Reading this is cheap; asking Explorer where the
    buttons are is not, so that is only done when this (or anything else) has changed."""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        ex = user32.GetWindowLongW(hwnd, -20)
        if ex & WS_EX_TOOLWINDOW or (user32.GetWindow(hwnd, 4) and not ex & WS_EX_APPWINDOW):  # GW_OWNER
            return True
        cloaked = ctypes.c_int(0)
        if dwmapi and dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4) == 0 and cloaked.value:
            return True  # DWMWA_CLOAKED: another virtual desktop, or a suspended app
        if with_titles:
            title = ctypes.create_unicode_buffer(128)
            user32.GetWindowTextW(hwnd, title, 128)
            found.append((hwnd, title.value))
        else:
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return tuple(sorted(found))


def layout_signature(hwnd, rect, notify):
    """Everything the taskbar's layout depends on that can be read without asking Explorer."""
    combine = setting("TaskbarGlomLevel", 0)
    return (hwnd, rect, notify, auto_hide(), light_taskbar(), setting("TaskbarAl", 1), setting("TaskbarDa", 1),
            setting("TaskbarSmallIcons", 0), setting("TaskbarSi", 1), combine,
            setting("FavoritesChanges", 0, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Taskband"),  # pins
            button_windows(combine != 0),
            time.strftime("%x"))  # the clock's date: a wider one (10/10) moves the clock's left edge


def read_bar(hwnd, key="main", label="Main display", previous=None):
    """Where a taskbar is and which parts of it are free, or None (hidden, vertical, auto-hide).
    `previous`: the last reading, reused as it is when nothing it depends on has changed."""
    rect = window_rect(hwnd)
    if not rect or not user32.IsWindowVisible(hwnd) or auto_hide():
        return None
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if width <= height * 3:
        return None  # a taskbar on the side of the screen has no room for a wide block
    notify = window_rect(user32.FindWindowExW(hwnd, None, "TrayNotifyWnd", None))
    try:
        signature = layout_signature(hwnd, rect, notify)
    except Exception:
        log.warning("taskbar layout signature", exc_info=True)
        signature = None
    if previous is not None and signature is not None and previous.signature == signature and previous.key == key:
        event("taskbar: layout unchanged, not measured")
        return previous
    if previous is not None and previous.signature and signature:
        for index, (a, b) in enumerate(zip(previous.signature, signature)):
            if a != b:
                event(f"taskbar: measured, signature part {index} changed")
    else:
        event("taskbar: measured, no earlier signature" if signature else "taskbar: measured, no signature")
    _, _, scale = fl.monitor_at((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    if notify and notify[0] > rect[0] + width // 2:
        right = notify[0]
    else:  # other displays: no notification area, maybe a clock (a button, measured below)
        right = rect[2] - round((260 if key == "main" else 8) * scale)
    occupied, measured = None, False
    try:
        started = time.perf_counter()
        spans = _COM.buttons(hwnd)
        event("taskbar: UI Automation walk", time.perf_counter() - started)
        if spans:
            occupied = taskbar_buttons(spans, rect)
            measured = bool(occupied)
    except Exception:
        log.warning("taskbar buttons could not be read", exc_info=True)
    if not measured:
        # Estimate: Windows 11 centres the buttons (widgets on the far left), Windows 10 starts them on the left.
        centred = setting("TaskbarAl", 1) == 1
        mid = (rect[0] + rect[2]) // 2
        occupied = [(mid - width // 4, mid + width // 4)] if centred else [(rect[0], rect[0] + width // 2)]
        if centred and setting("TaskbarDa", 1) and key == "main":
            occupied.append((rect[0], rect[0] + round(190 * scale)))
        if key != "main":
            occupied.append((rect[2] - round(130 * scale), rect[2]))  # its clock
    bar = Bar(hwnd, key, label, rect, scale, rect[0] + round(8 * scale), right - round(12 * scale), occupied,
              light_taskbar(), measured)
    bar.signature = signature if measured else None  # an estimate is worth measuring again next time
    return bar


class TaskbarBlock(fl.Popup):
    """One block on a display's taskbar: a slot (taskbar_layout.py), left or right."""
    modal = False            # other popups ignore it; it never closes on outside clicks
    take_focus = False
    dismiss_on_deactivate = False
    ex_style = WS_EX_NOACTIVATE
    FX_SECONDS = {**fl.Popup.FX_SECONDS, "width": 0.32}

    def __init__(self, view, slot):
        super().__init__(view.tray)
        self.view, self.slot = view, slot
        self.spot = None  # (edge x, "left" | "right", columns) in physical px
        self.bar = None

    @property
    def provider(self):
        """The provider of the account it shows (the panel it opens lists that provider)."""
        account = fr.slot_account(self.tray.state, self.slot)
        return account["provider"] if account else (self.slot if self.slot in PROVIDER_ORDER else None)

    def on_panel(self, x, y):
        return True  # no shadow margin: the whole image is the block

    def contains(self, point):
        return self.hwnd is not None and self.x <= point[0] < self.x + self.size[0] and self.y <= point[1] < self.y + self.size[1]

    def fx_targets(self):
        state, bar = self.tray.state, self.bar
        fx = {key: value for key, value in fr.targets(state).items() if key[0] in ("active", "bar")}
        if self.hover:
            fx[("hover", self.hover)] = 1.0
        fx[("width",)] = fr.block_width(state, self.slot, bar.height, bar.light, self.spot[2]) or 120
        return fx

    def render(self, hover):
        bar = self.bar
        return fr.render_block(self.tray.state, self.slot, hover, self.scale, self.fx, bar.height, bar.light,
                               self.fx.get(("width",)), self.spot[2])

    def place(self, bar, spot):
        self.bar, self.spot, self.scale = bar, spot, bar.scale

    def position(self, width, height):
        edge, side, _ = self.spot
        x = edge if side == "left" else edge - width
        rect = self.bar.rect
        y = rect[1] + (rect[3] - rect[1] - height) // 2
        return x, y, (0, -1)  # opens rising out of the taskbar's bottom edge

    def redraw(self, retarget=True):
        if not self.hwnd or self.closing:
            return
        if retarget:
            self.retarget()
        image, self.hits = self.render(self.hover)
        self.size = image.size
        if not self.anim:
            self.x, self.y, _ = self.position(*image.size)
        self._push(image, self.alpha)

    @property
    def owner(self):
        # Owned by the taskbar: Windows keeps it just above the taskbar in one step whenever the
        # taskbar comes forward (clicks, focus changes), so it never drops behind and back.
        return self.bar.hwnd if self.bar else None

    def wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        if msg == WM_DESTROY and self.hwnd == hwnd:  # its taskbar went away (Explorer restarted)
            fl.Popup._windows.pop(hwnd, None)
            self.hwnd, self.anim, self.fx_anims, self.closing = None, None, {}, False
            self.view.later()
            return 0
        if msg == WM_RBUTTONUP:
            self.view.open_menu()
            return 0
        return super().wndproc(hwnd, msg, wparam, lparam)

    def activate(self, action):
        if action == "open":
            self.view.open_panel(self)


class TaskbarView:
    """Keeps the chosen blocks on each chosen display's taskbar (taskbar_layout.py). Everything
    here runs on the tray thread; other threads call post()."""

    def __init__(self, tray):
        self.tray = tray
        self.blocks = {}         # (display id, slot index) -> TaskbarBlock
        self.bars = {}           # display id -> Bar (the taskbar the display's slots go on)
        self.stale = True        # re-read the taskbars' layout on the next sync
        self.hwnd = None
        self.hooked = False
        self.covered = {}        # display id -> a full-screen app is in front there: its blocks are down
        self.shell_message = None
        self.appbar_message = None
        self.appbar = False      # registered as an app bar: Explorer tells us when a full-screen app opens
        self.full_screen = False  # Explorer's word (ABN_FULLSCREENAPP): a full-screen app is open somewhere
        self.settle_until = 0.0  # keep checking for full screen until then (an app takes a moment to get there)

    # ---------- wiring into pystray's hidden window ----------
    def attach(self, icon):
        """Add our messages to pystray's window procedure (before the icon runs)."""
        self.icon = icon
        handlers = icon._message_handlers
        handlers[WM_APP_TASKBAR] = lambda w, l: event("taskbar: app state changed") or self.sync()
        handlers[WM_TIMER] = self.on_timer
        handlers[WM_SETTINGCHANGE] = lambda w, l: event("taskbar: setting changed") or self.later()
        for message in (WM_DISPLAYCHANGE, fl_taskbar_created()):
            previous = handlers.get(message)
            handlers[message] = self._chain(previous)
        self.shell_message = user32.RegisterWindowMessageW("SHELLHOOK")
        handlers[self.shell_message] = self.on_shell
        self.appbar_message = user32.RegisterWindowMessageW("LimitSwitcherAppBar")
        handlers[self.appbar_message] = self.on_appbar

    def _chain(self, previous):
        def handler(wparam, lparam):
            if previous:
                previous(wparam, lparam)
            if self.appbar:  # Explorer restarted: it forgot the app bar
                self.appbar = False
                self.register_appbar()
            self.later()
        return handler

    def _appbar_data(self):
        data = APPBARDATA(ctypes.sizeof(APPBARDATA))
        data.hWnd, data.uCallbackMessage = self.hwnd, self.appbar_message or 0
        return data

    def register_appbar(self):
        """An app bar that takes no space (no ABM_SETPOS): only so Explorer sends ABN_FULLSCREENAPP,
        the one signal for an app going full screen without another window coming forward
        (a browser's F11, a video's full-screen button, a game switching modes)."""
        if self.appbar or not self.hwnd or not self.appbar_message:
            return
        self.full_screen = False
        self.appbar = bool(shell32.SHAppBarMessage(ABM_NEW, ctypes.byref(self._appbar_data())))

    def unregister_appbar(self):
        if self.appbar:
            shell32.SHAppBarMessage(ABM_REMOVE, ctypes.byref(self._appbar_data()))
        self.appbar = self.full_screen = False

    def post(self):
        """Any thread: sync with the latest state on the tray thread."""
        hwnd = getattr(getattr(self, "icon", None), "_hwnd", None)
        if hwnd:
            user32.PostMessageW(hwnd, WM_APP_TASKBAR, 0, 0)

    def later(self, ms=400):
        """Re-read the taskbar a moment from now (after its own buttons have finished moving)."""
        event("taskbar: later()")
        self.stale = True
        if self.hwnd:
            user32.SetTimer(self.hwnd, TIMER_LAYOUT, ms, None)

    def hook(self):
        if self.hooked:
            return
        self.hwnd = self.icon._hwnd
        user32.RegisterShellHookWindow(self.hwnd)
        user32.SetTimer(self.hwnd, TIMER_MINUTE, 60_000, None)
        self.register_appbar()
        self.hooked = True

    def unhook(self):
        if not self.hooked:
            return
        user32.DeregisterShellHookWindow(self.hwnd)
        self.unregister_appbar()
        user32.KillTimer(self.hwnd, TIMER_MINUTE)
        user32.KillTimer(self.hwnd, TIMER_LAYOUT)
        user32.KillTimer(self.hwnd, TIMER_COVER)
        self.hooked = False

    # ---------- events ----------
    def on_timer(self, wparam, lparam):
        event({TIMER_LAYOUT: "taskbar: timer layout", TIMER_MINUTE: "taskbar: timer minute",
               TIMER_COVER: "taskbar: timer cover"}.get(wparam, f"taskbar: timer {wparam}"))
        if wparam == TIMER_LAYOUT:
            user32.KillTimer(self.hwnd, TIMER_LAYOUT)
            self.sync()
        elif wparam == TIMER_MINUTE:  # reset times move on; the taskbar may have changed too
            self.stale = True
            self.sync()
        elif wparam == TIMER_COVER:
            self.follow_taskbar()

    def on_shell(self, wparam, lparam):
        code = wparam & 0x7FFF
        event(f"taskbar: shell event {code}")
        if code in (HSHELL_WINDOWCREATED, HSHELL_WINDOWDESTROYED):
            self.later()  # a taskbar button came or went: the free space moved
        elif code == HSHELL_WINDOWACTIVATED:  # includes full-screen ("rude") apps
            self.settle()

    def on_appbar(self, wparam, lparam):
        event(f"taskbar: app bar notice {wparam}")
        if wparam == ABN_FULLSCREENAPP:
            self.full_screen = bool(lparam)
            self.settle()
        elif wparam in (ABN_STATECHANGE, ABN_POSCHANGED):
            self.later()

    def settle(self, seconds=3.0):
        """Check now, then a few times a second for a moment: a window going full screen (or
        leaving it) gets there over a few frames, after the event that told us."""
        self.settle_until = time.monotonic() + seconds
        self.follow_taskbar()

    def follow_taskbar(self):
        """Hide a display's blocks when an app is full screen there, and bring them back after.

        Covered: the foreground window covers the whole display (games, video, a browser's F11),
        or a window above the taskbar in z-order covers it (whatever has focus: Steam's video
        player). Explorer's ABN_FULLSCREENAPP, which comes even when no other window comes
        forward, starts a check. Neither counts once that window is minimized or gone, so the
        blocks come back as soon as the taskbar can be seen again.
        Screenshot tools and overlays are tool windows, so they never make the blocks hide.
        Instant, with no fade: the taskbar does not fade. Each display on its own: a game on one
        screen leaves the other screen's blocks up."""
        uncovered = False
        ours = {block.hwnd for block in self.blocks.values() if block.hwnd} | set(fl.Popup._windows)
        for key, bar in self.bars.items():
            covered = bool(user32.IsWindow(bar.hwnd)
                           and (full_screen_app(bar.rect)
                                or covered_from_above(bar.hwnd, bar.rect, ours, self.full_screen)))
            if covered == self.covered.get(key, False):
                continue
            self.covered[key] = covered
            uncovered |= not covered
            for (display, _), block in self.blocks.items():
                if display == key and block.hwnd and not block.closing:
                    user32.ShowWindow(block.hwnd, SW_HIDE if covered else SW_SHOWNA)
        if time.monotonic() < self.settle_until:
            user32.SetTimer(self.hwnd, TIMER_COVER, 250, None)
        elif any(self.covered.values()) or self.full_screen:  # it may leave (or arrive) without activating anything
            user32.SetTimer(self.hwnd, TIMER_COVER, 1000, None)
        elif self.hwnd:
            user32.KillTimer(self.hwnd, TIMER_COVER)
        if uncovered:
            self.sync()  # blocks that were due while it was down

    # ---------- layout ----------
    def enabled(self):
        return bool(self.tray.state.get("taskbar", True)) and not self.tray.quitting

    def sync(self):
        try:
            self._sync()
        except Exception:
            log.exception("taskbar view update failed")

    def _sync(self):
        if not self.enabled():
            self.close_all()
            self.unhook()
            return
        self.hook()
        state = self.tray.state
        wanted = taskbar_layout.layout(state)
        if wanted != getattr(self, "wanted", None):  # changed in the settings
            self.wanted, self.stale = wanted, True
        if self.stale:
            found = taskbars()
            displays = [{"id": key, "label": label} for _, key, label in found]
            if displays != getattr(self.tray.controller, "taskbar_displays", None):
                self.tray.controller.taskbar_displays = displays  # the settings list them
                self.tray.controller.notify("changed", None)
            bars = {}
            for key in wanted:
                # A display that isn't connected now: its slots go to the main one, unless the
                # main one has its own; never some other display by accident.
                entry = next((t for t in found if t[1] == key), None)
                if entry is None and "main" not in wanted and "main" not in bars:
                    entry = next((t for t in found if t[1] == "main"), None)
                if entry is not None:
                    bars[key] = read_bar(*entry, previous=self.bars.get(key))
            self.bars, self.stale = bars, False
            self.covered = {key: value for key, value in self.covered.items() if key in bars}
        self.follow_taskbar()
        shown = set()
        for key, bar in self.bars.items():
            slots = wanted.get(key) or [None, None]
            asked = []
            for index, slot in enumerate(slots):
                account = fr.slot_account(state, slot) if slot else None
                if account is None:
                    continue
                columns = range(min(3, max(1, len(account["windows"]))), 0, -1)
                asked.append((index, {c: math.ceil(fr.block_width(state, slot, bar.height, bar.light, c) * bar.scale)
                                      for c in columns}, "left" if index == 0 else "right"))
            spots = place_blocks(free_gaps(bar.left, bar.right, bar.occupied, round(12 * bar.scale)), asked,
                                 round(12 * bar.scale))
            for index, (x, width, columns) in spots.items():
                side = "left" if index == 0 else "right"
                spot = (x if side == "left" else x + width, side, columns)
                block = self.blocks.get((key, index))
                if block is not None and block.slot != slots[index]:
                    block.slot = slots[index]  # another account in this slot: redrawn below
                if block is None:
                    block = self.blocks[(key, index)] = TaskbarBlock(self, slots[index])
                if block.hwnd and block.bar is not None and block.bar.hwnd != bar.hwnd:
                    block._destroy()  # moving to another display's taskbar: a new window owned by it
                block.place(bar, spot)
                shown.add((key, index))
                if block.hwnd and not block.closing:
                    block.redraw()
                elif not self.covered.get(key):
                    block.open()
        for place, block in self.blocks.items():
            if place not in shown:
                block.close()

    def close_all(self):
        for block in self.blocks.values():
            block.close()

    def dismiss(self):
        for block in self.blocks.values():
            block.dismiss()

    # ---------- clicks ----------
    def open_panel(self, block):
        """The panel above the block, listing just that provider's accounts. A second click on
        the same block closes it; a click on the other block switches it over."""
        flyout = self.tray.flyout
        if flyout is None:
            return
        if flyout.pinned and flyout.hwnd and not flyout.closing:
            # The panel is popped out (and already lists every account): bring it to the front
            # instead of closing it and opening it again as this block's panel.
            fl.force_foreground(flyout.hwnd)
            return
        same = flyout.only == block.provider and getattr(flyout, "origin_block", None) is block
        if same and flyout.hwnd and not flyout.closing:
            flyout.close()
            return
        if same and time.monotonic() < flyout.suppress_until:
            flyout.suppress_until = 0.0  # the press on this block already closed it
            return
        flyout.origin = ((block.x, block.bar.rect[1], block.x + block.size[0], block.bar.rect[3]), block.provider)
        flyout.origin_block = block
        flyout.open()

    def open_menu(self):
        menu = self.tray.menu
        if menu is not None:
            flyout = self.tray.flyout
            if flyout and not flyout.pinned:
                flyout.close()
            menu.toggle()


def fl_taskbar_created():
    return user32.RegisterWindowMessageW("TaskbarCreated")
