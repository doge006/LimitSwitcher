"""LimitSwitcher: a notification-area icon is the whole resident app.

Left-click shows a compact flyout (accounts, usage, one-click swap, Auto swap / Auto resume) with a
"Full view" button that opens the dashboard (the local Web UI) in a browser app window.
Right-click offers a short menu.

Idle cost is kept near zero: no Tk, no timers, no polling. The tray's Win32 message
loop and one refresh thread both block until something happens; the icon image and
menu are rebuilt only when what they show actually changes.
"""
import argparse
import json
import math
import logging
from pathlib import Path
import sys
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

if sys.platform != "darwin":  # the Mac menu bar app (macos_app.py) draws with AppKit and needs neither
    import pystray
    from PIL import Image, ImageDraw
    from PIL import IcoImagePlugin  # noqa: F401  pystray saves the icon as ICO; loaded up front, Pillow
    # doesn't load all ~40 of its format plugins (TIFF, PDF, ...) to find it

from .web import Controller, clear_url_file, make_server, write_url_file

APP = "LimitSwitcher"
WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016  # Windows is shutting down / signing out
PROVIDERS = (("claude", "Claude"), ("codex", "Codex"))
ASSETS = Path(__file__).with_name("static") / "assets"
LEVEL_RGB = {"good": (76, 195, 138), "warn": (229, 181, 74), "bad": (239, 106, 91)}
_local = build_opener(ProxyHandler({}))  # loopback only; never through a system proxy


# ---------- pure presentation helpers (unit-tested) ----------
def remaining(used):
    """What's left, rounded down: never more room than there is (93.4% used shows 6% left)."""
    return max(0, min(100, math.floor(100 - used + 1e-6)))


def headroom(account):
    """Remaining percent of the tightest account-wide window (100 when not known yet)."""
    return account["headroom"] if account.get("headroom", -1) >= 0 else 100


def level(left):
    return "good" if left > 30 else "warn" if left > 10 else "bad"


def short_name(account):
    return account.get("name") or account["alias"]


def active_accounts(state):
    return [a for a in state["accounts"] if a["active"]]


def tray_level(state):
    """Worst level across the accounts currently in use; drives the icon's status dot."""
    levels = [level(headroom(a)) if a["eligible"] else "bad" for a in active_accounts(state)]
    for name in ("bad", "warn", "good"):
        if name in levels:
            return name
    return None


def tooltip(state):
    lines = [APP]
    for provider, title in PROVIDERS:
        account = next((a for a in active_accounts(state) if a["provider"] == provider), None)
        if account:
            status = f"{headroom(account):.0f}% left" if account["eligible"] else "limit reached"
            lines.append(f"{title}: {short_name(account)} · {status}")
    modes = ["Auto swap" if state["autoSwap"] else "Manual"] + (["Auto resume"] if state["afk"] else [])
    lines.append(" · ".join(modes))
    return "\n".join(lines)[:127]  # Windows tooltip limit


def menu_signature(state):
    """Everything the menu shows; the menu is rebuilt only when this changes."""
    return (state["autoSwap"], state["afk"], state["busy"],
            tuple((p["session"], p["tokens"]) for p in state.get("pendingResumes") or []))


# ---------- icon ----------
_icons = {}


def icon_image(status):
    """App mark with a small status dot. Built once per status and cached."""
    if status not in _icons:
        with Image.open(ASSETS / "switcher.png") as source:
            image = source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        if status:
            draw = ImageDraw.Draw(image)
            draw.ellipse((38, 38, 63, 63), fill=(22, 22, 22, 255))
            draw.ellipse((42, 42, 59, 59), fill=LEVEL_RGB[status] + (255,))
        _icons[status] = image
    return _icons[status]


# ---------- full view ----------
def app_version():
    """This copy's version, and the commit for a git copy, for app.log."""
    from .version import ROOT, VERSION
    git = ROOT / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = git / head[5:]
            head = ref.read_text().strip() if ref.exists() else head[5:]
        return f"{VERSION} ({head[:7]})"
    except OSError:
        return VERSION


def show_running(url):
    """Ask the running copy to show its own window; False when it has none (open a browser)."""
    try:
        base, token = url.split("/#token=")
        request = Request(base + "/api/show", data=b"{}", method="POST",
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with _local.open(request, timeout=10) as response:
            return bool(json.load(response).get("shown"))
    except (OSError, ValueError, AttributeError):
        return False


def quit_running(url):
    try:
        base, token = url.split("/#token=")
        request = Request(base + "/api/shutdown", data=b"{}", method="POST",
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with _local.open(request, timeout=5):
            pass
    except (OSError, ValueError):
        pass


def user_url_file():
    """The running copy's URL for this user, whichever folder it runs from."""
    from .vault import data_dir
    return data_dir() / "running.url"


def existing_instance(url_file):
    """Launch URL of an already-running copy, or None."""
    try:
        url = Path(url_file).read_text(encoding="utf-8").strip()
        base, token = url.split("/#token=")
        request = Request(base + "/api/state?after=-1", headers={"Authorization": "Bearer " + token})
        with _local.open(request, timeout=2) as response:
            json.load(response)
        return url
    except (OSError, ValueError):
        return None


def _log_path():
    from .vault import data_dir
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "app.log"  # errors only; empty in normal use


# ---------- tray host ----------
class Tray:
    def __init__(self, controller, server, icon_factory=None, flyout=None):
        self.controller, self.server = controller, server
        self.url = server.launch_url
        self.state = controller.snapshot()
        self.shown = {"title": None, "level": None, "menu": None}
        self.last_active = {a["provider"]: a["id"] for a in active_accounts(self.state)}
        self.quitting = False
        native = icon_factory is None and sys.platform == "win32"
        if icon_factory is None:
            if native:
                from .flyout import tray_icon_class
                icon_factory = tray_icon_class()
            else:
                icon_factory = pystray.Icon
        self.icon = icon_factory("account-switcher", icon_image(tray_level(self.state)),
                                 tooltip(self.state), pystray.Menu(self.menu_items))
        # The API's shutdown (and an update that swaps the app's files) quits the tray too.
        server.quit = self.quit
        controller.quit_app = self.quit
        controller.on_update_available = lambda version: self.icon.notify(
            f"Version {version} is available. Update from the full view's Settings.", APP)
        # Opened again (Start menu, shortcut): this copy opens the full view, so it is sized by
        # the DPI-aware process and an open one is brought to the front instead of a second one.
        server.show = self.open_full_view
        self.full_view = None
        self.menu = self.taskbar = None
        self.on_end_session = None  # set by main(): undo the app's changes before Windows ends it
        if sys.platform == "win32" and hasattr(self.icon, "_message_handlers"):
            # pystray answers every message it doesn't know with 0, which for Windows' "may I
            # shut down?" (WM_QUERYENDSESSION) means no: the app was listed as preventing shutdown.
            # Say yes, and when the session really ends (WM_ENDSESSION), put the Codex and Claude
            # Code settings back, as a quit does, before the process is ended.
            self.icon._message_handlers[WM_QUERYENDSESSION] = lambda w, l: 1
            self.icon._message_handlers[WM_ENDSESSION] = lambda w, l: self.end_session(w)
        if native:
            try:  # the full view: a window of our own, drawn like the panel (no browser)
                from .fullview_win import FullViewWindow, WM_APP_SHOW
                self.full_view = FullViewWindow(self)
                self.icon._message_handlers[WM_APP_SHOW] = lambda w, l: self.full_view.show()
            except Exception:
                logging.getLogger("account_switcher").exception("native full view unavailable")
                self.full_view = None
            from .flyout import Flyout, TrayMenu
            flyout, self.menu = Flyout(self), TrayMenu(self)
            self.icon.popups = (flyout, self.menu)  # left/right clicks open our own popups
            try:  # the accounts in use, shown on the taskbar itself
                from .taskbar import TaskbarView
                self.taskbar = TaskbarView(self)
                self.taskbar.attach(self.icon)
                controller.taskbar_available = True
            except Exception:
                logging.getLogger("account_switcher").exception("taskbar view unavailable")
                self.taskbar = None
        self.flyout = flyout
        if sys.platform.startswith("linux") and icon_factory is pystray.Icon:
            try:  # the full view: a Tk window drawn like the Windows one (no browser)
                import tkinter  # noqa: F401  (python3-tk; without it the browser is used)
                from .fullview_tk import TkFullView
                self.full_view = TkFullView(self)
            except ImportError:
                self.full_view = None

    # Right-click menu; account swaps live in the flyout. Rebuilt only when it changes.
    def menu_items(self):
        state = self.state
        if self.flyout:
            yield pystray.MenuItem("Accounts", self.toggle_flyout, default=True)
            yield pystray.MenuItem("Full view", self.open_full_view)
        else:
            yield pystray.MenuItem("Full view", self.open_full_view, default=True)
        yield pystray.Menu.SEPARATOR
        for item in state.get("pendingResumes") or []:  # a large session waiting for an OK to continue
            yield pystray.MenuItem(f"Continue large session (~{item['tokens'] // 1000}k tokens)",
                                   self.resume(item["session"], True))
            yield pystray.MenuItem("Don't continue it", self.resume(item["session"], False))
            yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Auto swap", self.toggle("autoSwap"),
                               checked=lambda _: state["autoSwap"], enabled=not state["busy"])
        yield pystray.MenuItem("Auto resume", self.toggle("afk"),
                               checked=lambda _: state["afk"], enabled=not state["busy"])
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Quit", self.quit)

    def toggle_flyout(self):
        self.flyout.toggle()

    def open_full_view(self):
        """Any thread: open the full view, or bring it to the front. Only ever the app's own window."""
        if self.full_view:
            self.full_view.post_show()
        else:
            logging.getLogger("account_switcher").error("full view: no native window (see the error above)")
            self.icon.notify("The full view couldn't open (on Linux it needs python3-tk). Details are in app.log.", APP)

    def popup_visible(self, shown):
        """Hide the tooltip while the panel or menu is open so it can't cover them."""
        self.popup_open = shown
        title = "" if shown else tooltip(self.state)
        if title != self.shown["title"]:
            self.icon.title = self.shown["title"] = title

    def poke(self):
        """Refresh usage soon if it is more than a minute old (panel opened)."""
        try:
            self.controller.action("refresh", {"ifOlderThan": 60})
        except (RuntimeError, ValueError):
            pass

    def toggle(self, key):
        def flip():
            prefs = {"autoSwap": self.state["autoSwap"], "afk": self.state["afk"]}
            prefs[key] = not prefs[key]
            self.act("preferences", prefs)
        return flip

    def resume(self, session, approve):
        return lambda: self.act("resumeSession", {"session": session, "approve": approve})

    def act(self, action, body):
        try:
            self.controller.action(action, body)
        except (RuntimeError, ValueError) as error:
            self.icon.notify(str(error), APP)

    def refresh(self):
        """Push the latest state to the icon, touching only what changed."""
        state = self.state = self.controller.snapshot()
        title = "" if getattr(self, "popup_open", False) else tooltip(state)
        if title != self.shown["title"]:
            self.icon.title = self.shown["title"] = title
        status = tray_level(state)
        if status != self.shown["level"]:
            self.icon.icon = icon_image(status)
            self.shown["level"] = status
        signature = menu_signature(state)
        if signature != self.shown["menu"]:
            self.shown["menu"] = signature
            self.icon.update_menu()
        for popup in (self.flyout, self.menu):
            if popup:
                popup.state_changed()
        if self.full_view:
            self.full_view.post_state()
        if self.taskbar:
            self.taskbar.post()
        self.announce_failovers(state)

    def announce_failovers(self, state):
        for account in active_accounts(state):
            provider, previous = account["provider"], self.last_active.get(account["provider"])
            self.last_active[provider] = account["id"]
            if previous is None or previous == account["id"]:
                continue
            if account["id"] in self.controller.manual_swaps:
                self.controller.manual_swaps.discard(account["id"])
                continue
            old = next((a for a in state["accounts"] if a["id"] == previous), None)
            reason = f"{short_name(old)} hit its limit. " if old and not old["eligible"] else ""
            self.icon.notify(f"{reason}Now using {short_name(account)}.", f"{dict(PROVIDERS)[provider]} switched accounts")

    def watch(self):
        """Block until the controller changes, refresh, repeat. No timeout, so no idle wake-ups."""
        seen = self.state["revision"]
        condition = self.controller.condition
        while True:
            with condition:
                condition.wait_for(lambda: self.controller.revision != seen or self.controller.closed)
                if self.controller.closed:
                    break
            time.sleep(.15)  # coalesce bursts such as streamed text into one refresh
            seen = self.controller.revision
            try:
                self.refresh()
            except Exception:  # never let a display hiccup kill the watcher
                pass
        self.quit()

    def quit(self, *_):
        if not self.quitting:
            self.quitting = True
            for popup in (self.flyout, self.menu):
                if popup:
                    popup.dismiss()
            if self.taskbar:
                self.taskbar.dismiss()
            if self.full_view:
                self.full_view.dismiss()
            self.icon.stop()

    def end_session(self, ending):
        """WM_ENDSESSION: Windows is shutting down or signing out (ending is nonzero). The
        process is ended right after this returns, so the settings are put back now."""
        if ending and self.on_end_session:
            logging.getLogger("account_switcher").warning("Windows is ending the session; closing")
            try:
                self.on_end_session()
            except Exception:
                logging.getLogger("account_switcher").exception("cleanup at shutdown")
        return 0

    def run(self, open_now=False):
        def setup(icon):
            icon.visible = True
            self.refresh()
            threading.Thread(target=self.watch, daemon=True).start()
            if open_now:
                self.open_full_view()
        from .memory import trim_soon
        trim_soon(30)  # after start-up (imports, first usage fetch) has settled
        self.icon.run(setup=setup)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--demo", action="store_true", help="Synthetic sample accounts instead of your real logins")
    parser.add_argument("--url-file", help="Where the private dashboard URL is kept while running")
    parser.add_argument("--quiet", action="store_true", help="Start in the tray without opening the full view")
    parser.add_argument("--show", action="store_true", help="Open the full view even with --quiet (opened by the user)")
    parser.add_argument("--quit", action="store_true", help="Close the running copy (the installer and uninstaller use it)")
    args = parser.parse_args(argv)

    # This folder's copy (--url-file), and whichever copy runs for this user: two copies at once
    # (an installed one and one from source) would both refresh the same saved logins, and a
    # refresh by one makes the other's copy of the token invalid ("Sign in again").
    url_files = [f for f in (args.url_file, user_url_file()) if f]
    if args.quit:  # closes cleanly: Codex and Claude Code settings are put back first
        for url_file in url_files:
            running = existing_instance(url_file)
            if running:
                quit_running(running)
                for _ in range(60):
                    if not Path(url_file).exists():
                        break
                    time.sleep(0.25)
        return

    if args.url_file:
        running = next((url for url in map(existing_instance, url_files) if url), None)
        if running:  # second launch: just bring up the dashboard of the running copy
            if sys.platform == "win32":  # this launch may take the foreground; let the running copy
                import ctypes
                ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            show_running(running)  # the running copy opens (or brings up) its own window
            return

    if sys.platform == "win32":
        from .flyout import enable_dpi_awareness
        enable_dpi_awareness()
    if sys.platform in ("win32", "darwin"):
        logging.basicConfig(filename=str(_log_path()), level=logging.WARNING,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("account_switcher").warning("started, version %s", app_version())
    from .profiler import start as start_profiler
    profiler = start_profiler()  # only with LIMITSWITCH_PROFILE set (a development tool)
    controller = Controller(live=not args.demo)
    server = make_server(controller, args.port)
    write_url_file(args.url_file, server.launch_url)
    if args.url_file:
        write_url_file(user_url_file(), server.launch_url)
    # A long poll interval: the loop never has to exit on its own because quitting
    # goes through the tray, so this thread just sleeps between connections.
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 60}, daemon=True).start()
    controller.watch_updates()  # at launch, then every 2 hours
    integrations = None
    if controller.live:  # route Codex through the app, and the Claude AFK hook
        from .integrations import Integrations
        integrations = Integrations(controller.gateway, server.hook_url, server.hook_token)
        controller.gateway.integrations = integrations
        try:
            integrations.start()
        except Exception:
            logging.getLogger("account_switcher").exception("integrations failed to start")
    done = []

    def shutdown():
        """Undo what the app set up. Runs once, from the host's quit or on the way out."""
        if done:
            return
        done.append(True)
        try:
            if integrations:
                integrations.stop()  # Codex and Claude Code keep working without the app
        finally:
            controller.close()  # stops any proxy / Claude processes this app owns
            clear_url_file(args.url_file, server.launch_url)
            clear_url_file(user_url_file(), server.launch_url)
            if profiler:
                profiler.report()

    try:
        if sys.platform == "darwin":
            from .macos_app import run  # menu bar app
            # Cocoa ends the process inside its own quit, so the app runs shutdown there.
            run(controller, server, open_now=args.show or not args.quiet, cleanup=shutdown)
        else:
            tray = Tray(controller, server)
            tray.on_end_session = shutdown
            tray.run(open_now=args.show or not args.quiet)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
