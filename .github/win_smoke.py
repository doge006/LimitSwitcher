"""Windows smoke test on a real Windows machine (GitHub Actions): the app starts, its full view
opens with LimitSwitcher's own taskbar identity (not Edge's), and quitting cleans up; then,
in demo mode, the taskbar view shows a block per provider and animates a swap.
Screenshots go to ./shots."""
import ctypes
import json
from pathlib import Path
import sys
import time
import subprocess
from urllib.request import ProxyHandler, Request, build_opener

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from account_switcher import win_window  # noqa: E402

SHOTS = ROOT / "shots"
SHOTS.mkdir(exist_ok=True)
failures = []


def check(ok, text):
    print(("PASS " if ok else "FAIL ") + text, flush=True)
    if not ok:
        failures.append(text)


def api(path, body=None):
    base, token = (ROOT / ".runtime" / "tray.url").read_text().strip().split("/#token=")
    request = Request(base + path, data=json.dumps(body or {}).encode(), method="POST",
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
        return json.load(response)


for _ in range(120):
    if (ROOT / ".runtime" / "tray.url").exists():
        break
    time.sleep(0.25)
check((ROOT / ".runtime" / "tray.url").exists(), "the app started")


def full_views():
    """The app's own full view windows (native, no browser)."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def each(hwnd, _):
        name = ctypes.create_unicode_buffer(64)
        ctypes.windll.user32.GetClassNameW(ctypes.c_void_p(hwnd), name, 64)
        if name.value == "AccountSwitcherFullView" and ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            found.append(hwnd)
        return True

    ctypes.windll.user32.EnumWindows(each, 0)
    return found


def memory(hwnd):
    """The app's working set and private bytes, in MB (the process owning hwnd)."""
    class COUNTERS(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + \
                   [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                    "PeakPagefileUsage", "PrivateUsage")]
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    process = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0010, False, pid.value)
    counters = COUNTERS(cb=ctypes.sizeof(COUNTERS))
    ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(process), ctypes.byref(counters), counters.cb)
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(process))
    return round(counters.WorkingSetSize / 2**20, 1), round(counters.PrivateUsage / 2**20, 1)


windows = []
for _ in range(80):
    windows = full_views()
    if windows:
        break
    time.sleep(0.25)
check(bool(windows), "the full view opened in its own window")
if windows:
    check(win_window.get_identity(windows[0]) == win_window.APP_ID, "the window has LimitSwitcher's taskbar identity")
    time.sleep(1)
    rect = (ctypes.c_long * 4)()
    ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(windows[0]), rect)
    print("full view size:", rect[2] - rect[0], "x", rect[3] - rect[1], flush=True)
    check(rect[3] - rect[1] >= 420, "the full view opens at its own size (as tall as its content)")
    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # as a second launch does
    api("/api/show")  # opened again (Start menu or tray): the open one comes to the front
    time.sleep(2)
    check(len(full_views()) == 1, "opening again keeps one full view")
    check(ctypes.windll.user32.GetForegroundWindow() == windows[0], "opening again brings it to the front")
    first = tuple(rect)
    # What a user does: close it, open it again (the tray), then launch the app again (the Start
    # menu shortcut). Always one window, the same size each time, and never a browser.
    ctypes.windll.user32.PostMessageW(ctypes.c_void_p(windows[0]), 0x0010, 0, 0)  # WM_CLOSE
    time.sleep(1)
    check(not full_views(), "closing the full view closes it")
    api("/api/show")
    time.sleep(2)
    again = full_views()
    check(len(again) == 1, "opening it again opens one window")
    if again:
        ctypes.windll.user32.GetWindowRect(ctypes.c_void_p(again[0]), rect)
        print("reopened size:", rect[2] - rect[0], "x", rect[3] - rect[1], flush=True)
        check(tuple(rect) == first, "it opens again at the same size and place")
        windows = again
    subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), "LimitSwitcher.pyw"], cwd=ROOT)
    time.sleep(6)
    check(len(full_views()) == 1, "launching the app again keeps one full view")
    ws, private = memory(windows[0])
    print(f"app memory with the full view open: working set {ws} MB, private {private} MB", flush=True)
    edge = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe"], capture_output=True, text=True).stdout
    check("msedge.exe" not in edge, "no browser runs for the full view")
time.sleep(2)
ImageGrab.grab().save(SHOTS / "1-full-view.png")
config = Path.home() / ".codex" / "config.toml"
check(config.exists() and "account-switcher" in config.read_text(), "Codex is routed while running")
api("/api/shutdown")
for _ in range(40):
    if not (ROOT / ".runtime" / "tray.url").exists():
        break
    time.sleep(0.25)
check(not (ROOT / ".runtime" / "tray.url").exists(), "quit ends the app")
check(not config.exists() or "account-switcher" not in config.read_text(), "quit restores the Codex config")

# ---- Taskbar view: demo accounts are in use, so a block per provider sits on the taskbar ----
from ctypes import wintypes  # noqa: E402
import subprocess  # noqa: E402

from account_switcher import taskbar  # noqa: E402

user32 = ctypes.windll.user32


def blocks():
    """Visible LimitSwitcher popups sitting on the taskbar: [(left, top, right, bottom)]."""
    found = []
    bar = taskbar.window_rect(user32.FindWindowW("Shell_TrayWnd", None))

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        rect = taskbar.window_rect(hwnd)
        if name.value == "AccountSwitcherFlyout" and user32.IsWindowVisible(hwnd) and rect and bar \
                and rect[1] >= bar[1] - 2 and rect[3] <= bar[3] + 2:
            found.append(rect)
        return True

    user32.EnumWindows(each, 0)
    return sorted(found)


class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
                ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG), ("dmDisplayOrientation", wintypes.DWORD),
                ("dmDisplayFixedOutput", wintypes.DWORD), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                ("dmFormName", wintypes.WCHAR * 32), ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD),
                ("dmReserved2", wintypes.DWORD), ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


# The runner's screen is 1024 px wide, too narrow for the blocks beside its taskbar buttons: use a
# typical 1920 x 1080 (the app itself would simply not show a block that does not fit).
mode = DEVMODEW(dmSize=ctypes.sizeof(DEVMODEW), dmFields=0x80000 | 0x100000, dmPelsWidth=1920, dmPelsHeight=1080)
print("display 1920x1080:", user32.ChangeDisplaySettingsW(ctypes.byref(mode), 0), flush=True)  # 0 = changed
time.sleep(3)
info = taskbar.read_bar(*taskbar.taskbars()[0])
print("taskbar:", info and {"rect": info.rect, "scale": info.scale, "free from": info.left, "to": info.right,
                            "buttons": sorted(info.occupied), "measured": info.measured, "light": info.light}, flush=True)
subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), "LimitSwitcher.pyw", "--demo"], cwd=ROOT)
for _ in range(120):
    if (ROOT / ".runtime" / "tray.url").exists():
        break
    time.sleep(0.25)
found = []
for _ in range(40):
    found = blocks()
    if len(found) >= 2:
        break
    time.sleep(0.25)
time.sleep(1)  # let the rise-in animation finish before measuring
found = blocks()
print("blocks:", found, flush=True)
check(len(found) == 2, "a block for Claude and one for Codex sit on the taskbar")
if found:
    check(all(r[3] - r[1] <= (info.rect[3] - info.rect[1] if info else 99) for r in found), "the blocks fit inside the taskbar")
    if len(found) == 2:
        check(found[0][2] <= found[1][0], "the blocks do not overlap")
    for a, b, *_ in (info.occupied if info and info.measured else []):
        check(not any(r[0] < b and a < r[2] for r in found), f"no block covers the taskbar button at {a}-{b}")
    owner = user32.FindWindowW("Shell_TrayWnd", None)
    owned = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def owned_by_taskbar(hwnd, _):
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, 64)
        if name.value == "AccountSwitcherFlyout" and user32.IsWindowVisible(hwnd) and user32.GetWindow(hwnd, 4) == owner:
            owned.append(hwnd)  # GW_OWNER: kept above the taskbar by Windows, no re-raising (no flicker)
        return True

    user32.EnumWindows(owned_by_taskbar, 0)
    check(len(owned) == len(found), "the blocks are owned by the taskbar")
    user32.SetForegroundWindow(owner)  # focus moves to the taskbar and back: the blocks stay put
    time.sleep(0.3)
    check(blocks() == found, "the blocks stay put when focus changes")
time.sleep(1)
screen = ImageGrab.grab()
height = info.rect[3] - info.rect[1] if info else 48
screen.crop((0, screen.height - height - 40, screen.width, screen.height)).save(SHOTS / "2-taskbar.png")
api("/api/swap", {"id": "codex-b"})  # the Codex block slides the old account out and the new one in
time.sleep(0.2)
ImageGrab.grab().crop((0, screen.height - height - 40, screen.width, screen.height)).save(SHOTS / "3-taskbar-swapping.png")
time.sleep(1)
ImageGrab.grab().crop((0, screen.height - height - 40, screen.width, screen.height)).save(SHOTS / "4-taskbar-swapped.png")
if len(found) == 2:  # a click on the Codex block opens the panel with only the Codex accounts
    right = found[1]
    user32.SetCursorPos((right[0] + right[2]) // 2, (right[1] + right[3]) // 2)
    user32.mouse_event(2, 0, 0, 0, 0)
    user32.mouse_event(4, 0, 0, 0, 0)
    time.sleep(1)
    ImageGrab.grab().crop((screen.width // 2, screen.height // 2, screen.width, screen.height)).save(SHOTS / "5-codex-panel.png")
    user32.SetCursorPos(10, 10)
if found:  # a full-screen window (a game, a video) covers the taskbar, and the blocks go down with it
    msg = wintypes.MSG()

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.02)

    user32.CreateWindowExW.restype = wintypes.HWND
    full = user32.CreateWindowExW(0, "Static", "Full screen", 0x80000000 | 0x10000000, 0, 0,
                                  screen.width, screen.height, None, None, None, None)
    user32.keybd_event(0x12, 0, 0, 0)  # an Alt tap lets this process take the foreground
    user32.SetForegroundWindow(full)
    user32.keybd_event(0x12, 0, 2, 0)
    pump(2)
    print("full-screen window in front:", user32.GetForegroundWindow() == full, "· taskbar on top:",
          bool(user32.GetWindowLongW(owner, -20) & 0x8), flush=True)
    ImageGrab.grab().crop((0, screen.height - height - 40, screen.width, screen.height)).save(SHOTS / "6-under-full-screen.png")
    check(not blocks(), "the blocks go down with the taskbar under a full-screen window")
    user32.DestroyWindow(full)
    back = []
    for _ in range(12):
        pump(0.5)
        back = blocks()
        if len(back) == len(found) and back[0][0] == found[0][0] and back[-1][2] == found[-1][2]:
            break
    print("after full screen:", back, "· taskbar on top:", bool(user32.GetWindowLongW(owner, -20) & 0x8), flush=True)
    # Claude is anchored by its left edge, Codex by its right (its width changed with the swap above).
    check(len(back) == len(found) and back[0][0] == found[0][0] and back[-1][2] == found[-1][2],
          "the blocks are back, in place, when it closes")

    def in_place():
        return len(blocks()) == len(found)  # where: Windows 10 gives the test's windows taskbar buttons too

    def wait_for(want, seconds):
        """Pump messages until the blocks are back in place (want True) or gone (False)."""
        end = time.time() + seconds
        while time.time() < end:
            pump(0.25)
            if (in_place() if want else not blocks()):
                return True
        return False

    def front(hwnd):
        user32.keybd_event(0x12, 0, 0, 0)
        user32.SetForegroundWindow(hwnd)
        user32.keybd_event(0x12, 0, 2, 0)

    def tell(name):
        ImageGrab.grab().crop((0, screen.height - height - 40, screen.width, screen.height)).save(SHOTS / name)
        print(f"  foreground ours: {user32.GetForegroundWindow() in mine} · taskbar on top:",
              bool(user32.GetWindowLongW(owner, -20) & 0x8), "· blocks:", len(blocks()), flush=True)

    sig = taskbar.sig
    sig(user32.SetWindowPos, wintypes.BOOL, wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.UINT)
    sig(user32.SetLayeredWindowAttributes, wintypes.BOOL, wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD)
    mine = []
    # A screenshot overlay (Win+Shift+S): a full-screen window, not a tool window, comes to the
    # front. When the snip is done its window drops out of sight while its app stays in front
    # (here: sent behind everything; then made see-through). The taskbar shows again, so the
    # blocks must too, with no click.
    for case, done in (("behind", lambda w: user32.SetWindowPos(w, wintypes.HWND(1), 0, 0, 0, 0, 0x13)),  # HWND_BOTTOM
                       ("see-through", lambda w: user32.SetLayeredWindowAttributes(w, 0, 0, 2))):  # LWA_ALPHA 0
        overlay = user32.CreateWindowExW(0x80000 | 0x8, "Static", "Snip", 0x80000000 | 0x10000000, 0, 0,
                                         screen.width, screen.height, None, None, None, None)  # LAYERED|TOPMOST
        user32.SetLayeredWindowAttributes(overlay, 0, 200, 2)
        mine.append(overlay)
        front(overlay)
        pump(1)
        print(f"screenshot overlay ({case}) up:", flush=True)
        tell(f"6-snip-{case}-up.png")
        done(overlay)
        back = wait_for(True, 4)
        print(f"screenshot done ({case}), its app still in front:", flush=True)
        tell(f"6-snip-{case}-done.png")
        check(back, f"the blocks come back after a screenshot ({case}) with no click")
        user32.DestroyWindow(overlay)
        pump(0.5)
    # A browser's F11 / a video's full-screen button: the window in front grows to full screen with
    # nothing else activating, then shrinks back.
    video = user32.CreateWindowExW(0, "Static", "Video", 0x80000000 | 0x10000000, 100, 100, 800, 500,
                                   None, None, None, None)
    mine.append(video)
    front(video)
    pump(1)
    check(in_place(), "the blocks stay up over an ordinary window in front")
    user32.SetWindowPos(video, None, 0, 0, screen.width, screen.height, 0x14)  # NOZORDER|NOACTIVATE
    check(wait_for(False, 4), "the blocks go down when the window in front goes full screen (F11)")
    tell("6-f11.png")
    user32.SetWindowPos(video, None, 100, 100, 800, 500, 0x14)
    check(wait_for(True, 4), "the blocks come back when it leaves full screen")
    user32.DestroyWindow(video)
    # The real Win+Shift+S, where Windows has it: open the snipping overlay, cancel it.
    subprocess.run(["explorer.exe", "ms-screenclip:"])
    pump(3)
    clip = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ScreenClippingHost.exe"], capture_output=True, text=True).stdout
    if "ScreenClippingHost" in clip:
        print("real snipping overlay up:", flush=True)
        tell("6-real-snip-up.png")
        user32.keybd_event(0x1B, 0, 0, 0)  # Esc
        user32.keybd_event(0x1B, 0, 2, 0)
        back = wait_for(True, 4)
        tell("6-real-snip-done.png")
        check(back, "the blocks are up after the real snipping overlay closes")
    else:
        print("no snipping overlay on this runner: skipped", flush=True)
# ---- The full view with the demo accounts: drawn natively, hover, the settings menu ----
api("/api/show")
views = []
for _ in range(40):
    views = full_views()
    if views:
        break
    time.sleep(0.25)
check(bool(views), "the full view opens in demo mode")
if views:
    hwnd = wintypes.HWND(views[0])
    time.sleep(1)
    client = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    box = (origin.x, origin.y, origin.x + client.right, origin.y + client.bottom)
    ImageGrab.grab(box).save(SHOTS / "7-full-view-demo.png")
    before = memory(views[0])
    scale = user32.GetDpiForWindow(hwnd) / 96
    width = client.right / scale
    inner = min(1180, width - 56)
    left = (width - inner) / 2
    # The pointer over the first card (hover), then a click on Settings (right of the refresh button).
    user32.SetCursorPos(origin.x + round((left + 100) * scale), origin.y + round(200 * scale))
    time.sleep(0.5)
    ImageGrab.grab(box).save(SHOTS / "8-full-view-hover.png")
    sx, sy = origin.x + round((left + inner - 36 - 10 - 30) * scale), origin.y + round((28 + 33) * scale)
    user32.SetCursorPos(sx, sy)
    user32.mouse_event(2, 0, 0, 0, 0)
    user32.mouse_event(4, 0, 0, 0, 0)
    time.sleep(0.7)
    ImageGrab.grab(box).save(SHOTS / "9-full-view-settings.png")
    user32.mouse_event(2, 0, 0, 0, 0)  # and closed again
    user32.mouse_event(4, 0, 0, 0, 0)
    for _ in range(6):  # scroll down and back
        user32.mouse_event(0x0800, 0, 0, ctypes.c_ulong(-120 & 0xFFFFFFFF).value, 0)
        time.sleep(0.05)
    time.sleep(0.5)
    ImageGrab.grab(box).save(SHOTS / "10-full-view-scrolled.png")
    after = memory(views[0])
    print("full view (demo) memory: before", before, "after hover, menu and scroll", after, flush=True)
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    time.sleep(1)
    check(not full_views(), "closing the full view closes it")
    print("app memory once closed:", memory(user32.FindWindowW("AccountSwitcherFlyout", None) or views[0]), flush=True)
# ---- Taskbar layout (Settings: per display, two slots): any account, either side, or off ----
def settle(count):
    now = blocks()
    for _ in range(24):
        now = blocks()
        if len(now) == count:
            break
        time.sleep(0.25)
    time.sleep(0.6)  # widths glide to their new size
    return blocks()


if found:
    shown = settle(len(found)) if api("/api/taskbar", {"layout": {"main": ["codex", "acct:claude-b"]}}) is not None else []
    ImageGrab.grab().crop((0, screen.height - height - 40, screen.width, screen.height)).save(SHOTS / "11-taskbar-codex-then-claude-b.png")
    check(len(shown) == 2, "a layout of Codex in use, then one Claude account, shows two blocks")
    alone = settle(1) if api("/api/taskbar", {"layout": {"main": [None, "codex"]}}) is not None else []
    check(len(alone) == 1 and alone[0][2] > screen.width // 2, "a right slot alone sits on the right")
    api("/api/taskbar", {"layout": {}})
    check(not settle(0), "a layout with every slot off shows nothing")
    back = settle(2) if api("/api/taskbar", {"layout": {"main": ["claude", "codex"]}}) is not None else []
    check(len(back) == 2, "the default layout brings both blocks back")
api("/api/taskbar", {"on": False})  # the menu's "Taskbar view" switch
gone = found
for _ in range(20):
    gone = blocks()
    if not gone:
        break
    time.sleep(0.25)
check(not gone, "turning the taskbar view off removes the blocks")
api("/api/shutdown")
time.sleep(2)
log = Path.home() / "AppData" / "Local" / "LimitSwitcher" / "app.log"
text = log.read_text() if log.exists() else ""
print("---- app.log ----\n" + (text or "(none)"))
check("Traceback" not in text and " ERROR " not in text, "no errors in app.log")
sys.exit(1 if failures else 0)
