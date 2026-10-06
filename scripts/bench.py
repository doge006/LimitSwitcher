"""Measure what can be measured without a desktop (a development tool): start-up time, the cost of
every animation frame the panel and the full view draw, memory, and the app's CPU while idle.

    python scripts/bench.py                                     # this copy
    python scripts/bench.py --against ../old-copy               # both, taking turns, compared

--against measures another copy of the app (a git worktree of the commit before a change) the
same way, alternating between the two so a busy or warming machine affects both alike.

Frames are drawn exactly as Windows draws them (Pillow), through fixed scripts of states and
in-between animation values, at 100%, 150% and 200% display scaling; scrolling is measured on its
own, a process per scale, so its peak memory is its own. A frame has 16.7 ms at 60 fps.
Each part runs in its own Python process, a few times; the typical (median) run is reported.
What needs a real desktop (the window manager showing the pixels, Windows' timer resolution)
is not measured here: scripts/measure.py and LIMITSWITCH_PROFILE cover the running app.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCALES = (1.0, 1.5, 2.0)


def child_env(root):
    env = dict(os.environ, PYSTRAY_BACKEND="dummy", PYTHONDONTWRITEBYTECODE="1",
               ACCOUNT_SWITCHER_HOME=os.environ.get("ACCOUNT_SWITCHER_HOME", "/tmp/limitswitcher-bench"))
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return env


def run_part(root, part, *args):
    """One part in a new process, with `root`'s copy of the app (this script either way)."""
    out = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), "--part", part, *args],
                         capture_output=True, text=True, env=child_env(root), cwd=root, timeout=600)
    if out.returncode:
        sys.exit(f"{part} failed:\n{out.stderr}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def peak_rss_mb():
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (2**20 if sys.platform == "darwin" else 1024)


def summary(times):
    """[seconds] -> {median, p95, max} in ms."""
    ms = sorted(t * 1000 for t in times)
    return {"median": statistics.median(ms), "p95": ms[min(len(ms) - 1, round(0.95 * (len(ms) - 1)))],
            "max": ms[-1], "frames": len(ms)}


# ---------- parts (each in its own process) ----------
def part_startup():
    """Import the app, make the demo controller, draw the first panel and the first full view frame."""
    t0 = time.perf_counter()
    from account_switcher import tray  # noqa: F401
    from account_switcher.web import Controller
    t1 = time.perf_counter()
    controller = Controller()
    state = controller.snapshot()
    t2 = time.perf_counter()
    from account_switcher import flyout_render as fr
    fr.render(state, None, 1.0)
    t3 = time.perf_counter()
    from account_switcher import fullview
    view = fullview.FullView(controller, Host(), state)
    view.resize(1280, 900, 1.0)
    view.frame()
    t4 = time.perf_counter()
    controller.close()
    return {"import": t1 - t0, "controller": t2 - t1, "first_panel": t3 - t2, "first_full_view": t4 - t3,
            "to_panel": t3 - t0}


class Host:
    def __init__(self):
        self.timers = {}

    def invalidate(self): pass
    def set_timer(self, name, ms): self.timers[name] = ms
    def kill_timer(self, name): self.timers.pop(name, None)
    def has_timer(self, name): return name in self.timers
    def set_cursor(self, kind): pass
    def clipboard(self): return ""


def ease(p):
    return 1 - (1 - p) ** 3


def part_panel():
    """The tray panel and its right-click menu: open, hover fades, a toggle. Each frame is drawn as
    the Windows host draws it (one painter per opened panel, when the code has one) and turned
    into the layered window's pixels (premultiplied BGRA in the window's bitmap), as _push does."""
    import ctypes
    from account_switcher import flyout_render as fr
    from account_switcher.web import Controller
    controller = Controller()
    state = controller.snapshot()
    results = {}
    has_painter = hasattr(fr, "Painter")
    for scale in SCALES:
        times = {"open": [], "hover": [], "toggle": [], "menu_hover": []}
        clock = time.perf_counter
        window = {}  # the window's bitmap: (size, buffer, painter count it holds)

        def show(image, painter):
            buffer = window.get("buffer")
            if buffer is None or window["size"] != image.size:
                buffer = window["buffer"] = ctypes.create_string_buffer(image.width * image.height * 4)
                window["size"], window["count"] = image.size, None
            if has_painter:
                step = painter is not None and painter.image is image and window["count"] == painter.count - 1
                fr.write_bgra(image, ctypes.addressof(buffer), painter.changed if step else None)
                window["count"] = painter.count if painter is not None and painter.image is image else None
            else:
                data = image.convert("RGBa").tobytes("raw", "BGRa")
                ctypes.memmove(buffer, data, len(data))

        def draw(bucket, render, *args, painter=None, **kwargs):
            start = clock()
            if painter is not None:
                kwargs["painter"] = painter
            image, hits = render(*args, **kwargs)
            show(image, painter)
            times[bucket].append(clock() - start)
            return image, hits

        def new_painter():
            return fr.Painter() if has_painter else None

        for _ in range(3):
            window.clear()
            draw("open", fr.render, state, None, scale, painter=new_painter())
        _, hits = fr.render(state, None, scale)
        rest = fr.targets(state)
        frames = 8  # a 0.12 s hover fade at 60 fps
        for _ in range(2):
            painter = new_painter()
            window.clear()
            fr.render(state, None, scale, **({"painter": painter} if painter else {}))
            for _, action in hits:  # the pointer passes over each control: fade in, fade out
                for i in range(1, frames + 1):
                    fx = {**rest, ("hover", action): ease(i / frames)}
                    draw("hover", fr.render, state, action, scale, fx=fx, painter=painter)
                for i in range(1, frames + 1):
                    fx = {**rest, ("hover", action): 1 - ease(i / frames)}
                    draw("hover", fr.render, state, None, scale, fx=fx, painter=painter)
            for key in ("autoSwap", "afk"):  # a 0.18 s toggle slide
                for i in range(1, 12):
                    fx = {**rest, ("toggle", key): ease(i / 11)}
                    draw("toggle", fr.render, state, "toggle:" + key, scale, fx=fx, painter=painter)
        items = [{"action": "panel", "label": "Open panel", "bold": True}, {"action": "full", "label": "Full view"}, "-",
                 {"action": "toggle:autoSwap", "label": "Auto swap", "checked": True, "enabled": True},
                 {"action": "toggle:afk", "label": "Auto resume", "checked": False, "enabled": True}, "-",
                 {"action": "quit", "label": "Quit"}]
        for _ in range(3):
            painter = new_painter()
            window.clear()
            for item in items:
                if item == "-":
                    continue
                for i in range(1, frames + 1):
                    draw("menu_hover", fr.render_menu, items, item["action"], scale,
                         fx={("hover", item["action"]): ease(i / frames)}, painter=painter)
        results[str(scale)] = {name: summary(values) for name, values in times.items()}
    controller.close()
    results["peak_rss_mb"] = peak_rss_mb()
    return results


def part_fullview():
    """The full view through a fixed script: cards rising in, hovers, the Settings and Add menus,
    toasts (scrolling is part_scroll). Every frame the animation asks for is drawn, as the host would."""
    from unittest import mock
    import random
    from account_switcher import fullview
    from account_switcher.web import Controller
    random.seed(7)
    clock = [1000.0]
    results = {}
    with mock.patch.object(fullview.time, "perf_counter", lambda: clock[0]), \
            mock.patch.object(fullview.time, "monotonic", lambda: clock[0]), \
            mock.patch("time.time", lambda: 1_790_000_000.0):
        controller = Controller()
        try:
            for scale in SCALES:
                times = {}
                view = fullview.FullView(controller, Host(), controller.snapshot())
                view.resize(1100, 800, scale)

                def step(n, label):
                    for _ in range(n):
                        clock[0] += 1 / 60
                        start = real()  # time.perf_counter is the script's clock here
                        view.frame()
                        times.setdefault(label, []).append(real() - start)

                step(1, "first")
                step(40, "rise")
                cards = [item for item in view.items if item[0] == "card"]
                for _ in range(2):
                    for _, _, x, y, w, h, _ in cards[:4]:
                        view.mouse_move(x + w / 2, y + h / 2 - view.scroll)
                        step(15, "hover")
                        view.mouse_move(x + w * 0.85, y + h - 40 - view.scroll)
                        step(15, "hover")
                    view.mouse_move(5, 5)
                    step(15, "hover")
                    view.activate("settings")
                    step(15, "menu")
                    view.activate("settings")
                    step(15, "menu")
                    view.activate("add")
                    step(15, "menu")
                    view.activate("add")
                    step(15, "menu")
                    view.toast("Swapped to another account", "ok")
                    step(30, "toast")
                results[str(scale)] = {name: summary(values) for name, values in times.items()}
        finally:
            controller.close()
    results["peak_rss_mb"] = peak_rss_mb()
    return results


def part_scroll(scale):
    """Scrolling the full view at one scale, in a process of its own (so its peak memory is the
    scroll's): a page of 16 cards, wheel notches down and back up, every frame the host would draw.
    Only frames where the page moved count as scroll frames."""
    from unittest import mock
    from account_switcher import fullview
    from account_switcher.web import Controller
    scale = float(scale)
    clock = [1000.0]
    times, moves, notches = [], 0, 0
    with mock.patch.object(fullview.time, "perf_counter", lambda: clock[0]), \
            mock.patch.object(fullview.time, "monotonic", lambda: clock[0]), \
            mock.patch("time.time", lambda: 1_790_000_000.0):
        controller = Controller()
        try:
            state = controller.snapshot()
            state["accounts"] = [dict(a, id=f"{a['id']}-{n}") for n in range(4) for a in state["accounts"]]
            view = fullview.FullView(controller, Host(), state)
            view.resize(1100, 800, scale)
            view.mouse_move(550, 400)  # the pointer rests over the page, as it does while scrolling

            drawn = [view.scroll]  # where the last frame showed the page

            def notch(pixels, timed):
                nonlocal moves, notches
                view.wheel(pixels)
                notches += timed
                for _ in range(30):  # half a second: the glide, and anything it set off
                    clock[0] += 1 / 60
                    start = real()
                    view.frame()
                    took = real() - start
                    if timed and view.scroll != drawn[0]:
                        times.append(took)
                        moves += 1
                    drawn[0] = view.scroll
            for timed in (False, True):  # the first pass draws every card once, as anyone's first scroll does
                for _ in range(12):
                    notch(64, timed)
                for _ in range(12):
                    notch(-64, timed)
        finally:
            controller.close()
    return {**summary(times), "per_notch": moves / max(1, notches), "peak_rss_mb": peak_rss_mb()}


def real():
    return REAL_PERF()


REAL_PERF = time.perf_counter


def part_idle(seconds):
    """The app as it starts (demo accounts, tray only, no window open), left alone: CPU and wake-ups."""
    import threading
    import psutil
    import pystray
    from account_switcher import tray, updates
    updates.latest_release = lambda timeout=10: None  # no network

    class Icon:
        def __init__(self, *args, **kwargs):
            self.icon = self.title = self.menu = None
            self.visible = False
            self.stopped = threading.Event()

        def run(self, setup=None):
            if setup:
                setup(self)
            self.stopped.wait()

        def notify(self, *args, **kwargs): pass
        def update_menu(self): pass
        def stop(self): self.stopped.set()

    pystray.Icon = Icon
    result = {}

    def sample():
        me = psutil.Process()
        time.sleep(5)  # start-up settles
        cpu0, ctx0, t0 = sum(me.cpu_times()[:2]), me.num_ctx_switches().voluntary, time.monotonic()
        time.sleep(seconds)
        cpu1, ctx1, t1 = sum(me.cpu_times()[:2]), me.num_ctx_switches().voluntary, time.monotonic()
        result.update(cpu_percent=100 * (cpu1 - cpu0) / (t1 - t0), wakeups_per_s=(ctx1 - ctx0) / (t1 - t0),
                      rss_mb=me.memory_info().rss / 2**20, threads=me.num_threads())
        os.write(1, (json.dumps(result) + "\n").encode())
        os._exit(0)

    threading.Thread(target=sample, daemon=True).start()
    tray.main(["--demo", "--quiet"])


PARTS = {"startup": part_startup, "panel": part_panel, "fullview": part_fullview, "scroll": part_scroll}


# ---------- the report ----------
def measure(roots, runs, idle_seconds):
    """{root: numbers}, each part run `runs` times per root, the roots taking turns."""
    data = {root: {"python": sys.version.split()[0], "cpu": cpu_name(), "platform": sys.platform} for root in roots}
    collected = {root: {} for root in roots}
    plan = [("startup", ())] * (runs * 3) + [("panel", ()), ("fullview", ())] * runs
    plan += [("scroll:" + str(scale), (str(scale),)) for scale in SCALES] * runs
    if idle_seconds:
        plan += [("idle", (str(idle_seconds),))] * max(1, runs // 2)
    for part, args in plan:
        for root in roots:
            collected[root].setdefault(part, []).append(run_part(root, part.split(":")[0], *args))
    for root in roots:
        for part, rounds in collected[root].items():
            data[root][part] = median_of(rounds)
        data[root]["startup"] = {key: value * 1000 for key, value in data[root]["startup"].items()}
    return data


def median_of(rounds):
    """The median over runs of every number in the nested result."""
    first = rounds[0]
    if isinstance(first, dict):
        return {key: median_of([r[key] for r in rounds]) for key in first}
    return statistics.median(rounds)


def cpu_name():
    try:
        with open("/proc/cpuinfo") as info:
            for line in info:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    import platform
    return platform.processor()


def rows(data):
    """[(group, metric, value, unit)] in report order."""
    out = []
    for key, label in (("import", "Import the app"), ("controller", "Load accounts"), ("first_panel", "First panel drawn"),
                       ("first_full_view", "First full view frame"), ("to_panel", "Launch to panel ready")):
        out.append(("Start-up", label, data["startup"][key], "ms"))
    for scale in SCALES:
        s = str(scale)
        for key, label in (("open", "open"), ("hover", "hover fade frame"), ("toggle", "toggle slide frame"),
                           ("menu_hover", "menu hover frame")):
            for stat in ("median", "p95"):
                out.append(("Panel", f"{label} @{int(scale * 100)}% ({stat})", data["panel"][s][key][stat], "ms"))
    out.append(("Panel", "peak memory (drawing process)", data["panel"]["peak_rss_mb"], "MB"))
    for scale in SCALES:
        s = str(scale)
        for key, label in (("first", "first frame"), ("rise", "cards rising frame"), ("hover", "hover frame"),
                           ("menu", "menu open/close frame"), ("toast", "toast frame")):
            stats = ("median",) if key == "first" else ("median", "p95")
            for stat in stats:
                out.append(("Full view", f"{label} @{int(scale * 100)}% ({stat})", data["fullview"][s][key][stat], "ms"))
    out.append(("Full view", "peak memory (drawing process)", data["fullview"]["peak_rss_mb"], "MB"))
    for scale in SCALES:
        part, at = data.get(f"scroll:{scale}"), f"@{int(scale * 100)}%"
        if part:
            out += [("Scrolling", f"frame {at} (median)", part["median"], "ms"),
                    ("Scrolling", f"frame {at} (p95)", part["p95"], "ms"),
                    ("Scrolling", f"frames per wheel notch {at}", part["per_notch"], ""),
                    ("Scrolling", f"peak memory {at} (drawing process)", part["peak_rss_mb"], "MB")]
    if "idle" in data:
        idle = data["idle"]
        out += [("Idle", "CPU (share of one core)", idle["cpu_percent"], "%"),
                ("Idle", "wake-ups per second", idle["wakeups_per_s"], "/s"),
                ("Idle", "memory (RSS)", idle["rss_mb"], "MB"),
                ("Idle", "threads", idle["threads"], "")]
    return out


def table(data, before=None):
    lines = []
    if before:
        lines += ["| Area | Measure | Before | After | Change |", "|---|---|---:|---:|---:|"]
        old = {(g, m): v for g, m, v, _ in rows(before)}
        for group, metric, value, unit in rows(data):
            was = old.get((group, metric))
            change = "" if not was else f"{100 * (value - was) / was:+.0f}%"
            lines.append(f"| {group} | {metric} | {fmt(was, unit)} | {fmt(value, unit)} | {change} |")
    else:
        lines += ["| Area | Measure | Value |", "|---|---|---:|"]
        for group, metric, value, unit in rows(data):
            lines.append(f"| {group} | {metric} | {fmt(value, unit)} |")
    return "\n".join(lines)


def fmt(value, unit):
    if value is None:
        return "–"
    if unit == "%":
        return f"{value:.2f}%"
    if unit == "":
        return f"{value:.0f}"
    return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--part", help=argparse.SUPPRESS)
    parser.add_argument("--runs", type=int, default=3, help="processes per part (start-up: 3x as many)")
    parser.add_argument("--idle", type=float, default=30, help="seconds of idle to measure (0: skip)")
    parser.add_argument("--against", help="another copy of the app to measure the same way and compare with")
    parser.add_argument("--out", help="save the numbers as JSON")
    parser.add_argument("args", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.part == "idle":
        part_idle(float(args.args[0]))
        return
    if args.part:
        print(json.dumps(PARTS[args.part](*args.args)))
        return
    roots = [os.path.abspath(args.against), ROOT] if args.against else [ROOT]
    measured = measure(roots, args.runs, args.idle)
    if args.out:
        with open(args.out, "w") as out:
            json.dump(measured, out, indent=1)
    data = measured[ROOT]
    print(f"{data['cpu']} · Python {data['python']} · {data['platform']}\n")
    print(table(data, measured[roots[0]] if args.against else None))


if __name__ == "__main__":
    main()
