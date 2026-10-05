"""Animation frames on a real Windows desktop (GitHub Actions, workflow "Windows app", job
"frames"): how often the panel and the full view get a new frame while something moves, and how
long each one takes to draw and show. Demo accounts; nothing real is touched.

    python .github/win_frames.py --root <copy of the app> --out frames.json
    python .github/win_frames.py --compare before1.json,before2.json after1.json,after2.json

The same script measures two copies of the app (--root), so a change can be compared with the
code before it on the same machine. Frames are counted from the app's own timers, as a user
sees them; the frames per second a fade gets is 1000 / the median time between frames. Scrolling
also reports the frames each wheel notch gets and the app's memory after it.
"""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
import statistics
import sys
import tempfile
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
MSG_WAIT = user32.MsgWaitForMultipleObjects
MSG_WAIT.restype = wintypes.DWORD
MSG_WAIT.argtypes = (wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD)
QS_ALLINPUT, PM_REMOVE = 0x04FF, 1
WM_MOUSEWHEEL = 0x020A
user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def pump(seconds):
    """Run the message loop (timers, paints) for `seconds`, as the app's tray thread does."""
    msg = wintypes.MSG()
    end = time.perf_counter() + seconds
    while True:
        left = end - time.perf_counter()
        if left <= 0:
            return
        MSG_WAIT(0, None, False, max(1, int(left * 1000)), QS_ALLINPUT)
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


class Recorder:
    """Wraps a method: when each call started and how long it took (ms)."""

    def __init__(self, owner, name):
        self.calls = []
        original = getattr(owner, name)

        def wrapped(*args, **kwargs):
            start = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                self.calls.append((start, (time.perf_counter() - start) * 1000))
        setattr(owner, name, wrapped)

    def take(self):
        calls, self.calls = self.calls, []
        return calls


def cadence(calls):
    """{fps, interval (median ms between frames), draw (median ms per frame), frames}."""
    if len(calls) < 3:
        return None
    gaps = [(b[0] - a[0]) * 1000 for a, b in zip(calls, calls[1:])]
    gap = statistics.median(gaps)
    return {"fps": 1000 / gap, "interval": gap, "draw": statistics.median(c[1] for c in calls),
            "draw_p95": sorted(c[1] for c in calls)[round(0.95 * (len(calls) - 1))], "frames": len(calls)}


def merge(parts):
    """One cadence from several runs of the same motion (all their frames together)."""
    parts = [p for p in parts if p]
    if not parts:
        return None
    frames = sum(p["frames"] for p in parts)
    return {key: sum(p[key] * p["frames"] for p in parts) / frames for key in ("fps", "interval", "draw", "draw_p95")} | {"frames": frames}


class FakeIcon:
    _hwnd = None
    title = ""

    def notify(self, *args): pass


class FakeTray:
    def __init__(self, controller):
        self.controller = controller
        self.state = controller.snapshot()
        self.icon = FakeIcon()
        self.manual_swaps = set()

    def poke(self): pass
    def popup_visible(self, shown): pass
    def act(self, action, body): pass
    def open_full_view(self): pass
    def quit(self): pass


def measure_panel(flyout_module, controller, scale):
    """The panel at `scale`: its open animation, then the pointer over each row and switch."""
    tray = FakeTray(controller)

    class Panel(flyout_module.Flyout):
        def prepare(self):
            super().prepare()
            self.scale = scale  # as on a display at that scaling

    panel = Panel(tray)
    panel.pinned = True  # stays open on a desktop where it can't keep focus
    ticks = Recorder(panel, "_tick")
    steps = Recorder(panel, "fx_step")
    opens, closes = [], []
    for _ in range(4):  # open and close a few times: each is only about ten frames
        panel.open()
        pump(0.4)
        opens.append(cadence(ticks.take()))
        panel.close()
        pump(0.3)
        closes.append(cadence(ticks.take()))
    panel.open()
    pump(0.4)
    ticks.take()
    result = {"open": merge(opens), "close": merge(closes)}
    steps.take()
    hovers = []
    actions = [action for _, action in panel.hits if action.startswith(("swap:", "toggle:"))]
    for _ in range(2):
        for action in actions:
            panel._set_hover(action)
            pump(0.2)
            hovers.append(cadence(steps.take()))
            panel._set_hover(None)
            pump(0.2)
            hovers.append(cadence(steps.take()))
    result["hover"] = merge(hovers)
    panel._destroy()
    pump(0.1)
    return result


def measure_full_view(fullview_win, controller, scale):
    """The full view at `scale`: cards rising in as it opens, hover fades over the cards, then
    scrolling with the mouse wheel."""
    tray = FakeTray(controller)
    window = fullview_win.FullViewWindow(tray)
    window.show()
    if not window.hwnd:
        return None
    if scale != window.scale:  # as on a display at that scaling
        window.scale = scale
        window.view.tiles.clear()
        window.on_size()
    view = window.view
    frames = Recorder(view, "frame")
    view.seen.clear()  # rise in again, now the scale is set
    window.invalidate()
    pump(1.2)
    result = {"rise": cadence(frames.take())}
    hovers = []
    cards = [item for item in view.items if item[0] == "card"][:4]
    for _ in range(2):
        for _, _, x, y, w, h, _ in cards:
            view.mouse_move(x + w / 2, y + h / 2 - view.scroll)
            pump(0.35)
            hovers.append(cadence(frames.take()))
            view.mouse_move(2, 2)
            pump(0.35)
            hovers.append(cadence(frames.take()))
    result["hover"] = merge(hovers)
    result.update(measure_scroll(window, frames))
    user32.DestroyWindow(window.hwnd)
    pump(0.1)
    return result


def measure_scroll(window, frames):
    """Wheel notches over a page of 16 cards, as Windows sends them: the frames each notch gets
    (one, if the page jumps), their rate and cost, and the app's memory while it scrolls."""
    view = window.view
    state = dict(window.tray.state)
    state["accounts"] = [dict(a, id=f"{a['id']}-{n}") for n in range(4) for a in state["accounts"]]
    view.set_state(state)
    view.mouse_move(view.width / 2, view.height / 2)

    def notch(delta):
        user32.SendMessageW(window.hwnd, WM_MOUSEWHEEL, (delta & 0xFFFF) << 16, 0)
        pump(0.3)
        return frames.take()
    for delta in [-120] * 12 + [120] * 12:  # once over the page first: every card is drawn once
        notch(delta)
    glides, counts = [], []
    for _ in range(2):
        for delta in [-120] * 12 + [120] * 12:
            calls = notch(delta)
            counts.append(len(calls))
            glides.append(cadence(calls))
    return {"scroll": merge(glides), "scroll_frames": statistics.median(counts), "memory": memory_mb()}


def memory_mb():
    """This process's working set and private bytes now, and its peak working set (MB)."""
    class COUNTERS(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                 "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                 "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                                                 "PrivateUsage")]
    counters = COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
    psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    mb = 2 ** 20
    return {"working_set": counters.WorkingSetSize / mb, "private": counters.PrivateUsage / mb,
            "peak_working_set": counters.PeakWorkingSetSize / mb}


def measure(root):
    sys.path.insert(0, os.path.abspath(root))
    os.environ.setdefault("ACCOUNT_SWITCHER_HOME", tempfile.mkdtemp(prefix="limitswitcher-frames-"))
    from account_switcher import flyout, fullview_win
    from account_switcher.web import Controller
    flyout.enable_dpi_awareness()
    controller = Controller()
    out = {"panel": {}, "full_view": {}}
    try:
        pump(0.5)
        for scale in (1.0, 1.5, 2.0):
            out["panel"][str(scale)] = measure_panel(flyout, controller, scale)
        for scale in (1.0, 2.0):
            out["full_view"][str(scale)] = measure_full_view(fullview_win, controller, scale)
    finally:
        controller.close()
    return out


def rows(data):
    out = []
    for scale, parts in data["panel"].items():
        for motion, label in (("open", "open animation"), ("close", "close animation"), ("hover", "hover fades")):
            out.append((f"Panel {label} @{round(float(scale) * 100)}%", parts.get(motion)))
    for scale, parts in data["full_view"].items():
        for motion, label in (("rise", "cards rising in"), ("hover", "hover fades"), ("scroll", "scrolling (wheel)")):
            out.append((f"Full view {label} @{round(float(scale) * 100)}%", (parts or {}).get(motion)))
    return out


def scroll_rows(data):
    """[(label, value, unit)]: frames per wheel notch, and memory after scrolling, per scale."""
    out = []
    for scale, parts in data["full_view"].items():
        parts, at = parts or {}, f"@{round(float(scale) * 100)}%"
        out.append((f"Frames per wheel notch {at}", parts.get("scroll_frames"), ""))
        for key, label in (("working_set", "working set"), ("private", "private bytes"), ("peak_working_set", "peak working set")):
            out.append((f"Memory after scrolling {at}: {label}", (parts.get("memory") or {}).get(key), " MB"))
    return out


def combine(files):
    """Several runs of one copy: the frame-weighted mean of each measure."""
    runs = []
    for name in files:
        with open(name) as saved:
            runs.append(rows(json.load(saved)))
    return [(runs[0][i][0], merge([run[i][1] for run in runs])) for i in range(len(runs[0]))]


def compare(before_files, after_files):
    before, after = combine(before_files), combine(after_files)
    lines = ["| Motion | Frames per second, before | after | Draw + show per frame (median / p95 ms), before | after |",
             "|---|---:|---:|---:|---:|"]

    def fps(c):
        return "–" if not c else f"{c['fps']:.0f}"

    def draw(c):
        return "–" if not c else f"{c['draw']:.1f} / {c['draw_p95']:.1f}"
    for (label, b), (_, a) in zip(before, after):
        lines.append(f"| {label} | {fps(b)} | {fps(a)} | {draw(b)} | {draw(a)} |")
    sides = []
    for files in (before_files, after_files):  # the median over runs of each number
        runs = []
        for name in files:
            with open(name) as saved:
                runs.append(scroll_rows(json.load(saved)))
        sides.append([(runs[0][i][0], [run[i][1] for run in runs if run[i][1] is not None], runs[0][i][2])
                      for i in range(len(runs[0]))])

    def value(values, unit):
        return "–" if not values else f"{statistics.median(values):.0f}{unit}" if not unit else f"{statistics.median(values):.1f}{unit}"
    lines += ["", "| Scrolling | Before | After |", "|---|---:|---:|"]
    for (label, b, unit), (_, a, _) in zip(*sides):
        lines.append(f"| {label} | {value(b, unit)} | {value(a, unit)} |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".")
    parser.add_argument("--out")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="comma-separated JSON files of each")
    args = parser.parse_args()
    if args.compare:
        print(compare(args.compare[0].split(","), args.compare[1].split(",")))
        return
    data = measure(args.root)
    text = json.dumps(data, indent=1)
    if args.out:
        with open(args.out, "w") as out:
            out.write(text)
    print(text)


if __name__ == "__main__":
    main()
