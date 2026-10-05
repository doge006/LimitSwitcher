"""macOS menu bar app: the Mac counterpart of the Windows tray (tray.py + flyout.py).

- Menu bar icon (an SF Symbol, drawn as a template image so it matches the menu bar).
- Click: the panel (static/menu.html in a transparent WKWebView over the system's popover
  material), in a borderless window of the app's own just under the icon. Drag it by its header
  or background and it stays open where you leave it (the Mac version of "pop out"), as often
  as you like; its dock button slides it back under the icon. Docked, a click elsewhere or Esc
  closes it. (Not an NSPopover: a popover dragged off becomes a window the app can't move.)
- Right-click (or Control-click): a native menu.
- Full View: a native window, drawn like the Windows one (fullview_mac.py; no WebKit), in a process
  of its own that ends when it closes (fullview_mac_app.py): its memory never stays with this one.
- Notifications when Auto swap moves an account.
No Dock icon (accessory app). Built on PyObjC (pyobjc-framework-Cocoa and -WebKit).
"""
import os
import subprocess
import threading
import time

import logging

import objc
from AppKit import (NSApp, NSView, NSApplication, NSApplicationActivationPolicyAccessory,
                    NSBackingStoreBuffered, NSColor, NSEventModifierFlagCommand, NSEventModifierFlagOption,
                    NSEventMaskLeftMouseUp, NSEventMaskRightMouseUp, NSEventModifierFlagControl,
                    NSEventTypeRightMouseUp, NSImage, NSMenu, NSMenuItem, NSOffState, NSOnState,
                    NSPanel, NSStatusBar, NSVariableStatusItemLength)
from Foundation import NSMakeRect, NSMakeSize, NSObject, NSURL, NSURLRequest
from PyObjCTools import AppHelper
from WebKit import WKWebView, WKWebViewConfiguration

from .tray import APP, PROVIDERS, active_accounts, short_name, tooltip, tray_level

log = logging.getLogger("account_switcher.macos")
PANEL_WIDTH = 392
PANEL_RADIUS = 12
ARROW_HEIGHT, ARROW_WIDTH = 10, 22  # the docked panel's arrow up to the menu bar icon
PAGE_KEEP = 90  # seconds the panel's page stays loaded after it closes (opening again is instant)
POPUP_LEVEL = 101  # NSPopUpMenuWindowLevel: over other windows, like a popover
TERMINATE_NOW = 1  # NSTerminateNow
SYMBOLS = {None: "arrow.triangle.2.circlepath", "good": "arrow.triangle.2.circlepath",
           "warn": "arrow.triangle.2.circlepath", "bad": "exclamationmark.arrow.triangle.2.circlepath"}


def notify(title, text):
    """A standard macOS notification (Notification Center)."""
    script = f'display notification {_as(text)} with title {_as(title)}'
    subprocess.Popen(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _as(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def protocols(*names):
    """The named Objective-C protocols that exist here. Conforming is only a declaration: a
    missing one (older macOS, or not loaded) must not stop the app from starting."""
    found = []
    for name in names:
        try:
            found.append(objc.protocolNamed(name))
        except Exception:
            pass
    return found


class PanelWindow(NSPanel):
    """The panel's borderless window. It can become key (the page takes clicks and Esc)."""

    def canBecomeKeyWindow(self):
        return True


class PanelEdge(NSView):
    """The panel's thin light edge (as a popover has), drawn along its outline, arrow included.
    Clicks go through to the page below."""
    outline = None

    def drawRect_(self, _rect):
        if self.outline is not None:
            NSColor.colorWithWhite_alpha_(1.0, 0.14).setStroke()
            self.outline.setLineWidth_(1.0)
            self.outline.stroke()

    def hitTest_(self, _point):
        return None


def panel_outline(width, height, arrow_x=None):
    """The panel's shape: a rounded rectangle, with the arrow pointing up at the menu bar icon
    while docked (arrow_x: its tip, from the left edge). Inset half a point for a crisp edge."""
    from AppKit import NSBezierPath
    r, i = PANEL_RADIUS, 0.5
    top = height - (ARROW_HEIGHT if arrow_x is not None else 0)
    x0, y0, x1, y1 = i, i, width - i, top - i
    path = NSBezierPath.bezierPath()
    path.moveToPoint_((x0 + r, y0))
    path.lineToPoint_((x1 - r, y0))
    path.appendBezierPathWithArcFromPoint_toPoint_radius_((x1, y0), (x1, y0 + r), r)
    path.lineToPoint_((x1, y1 - r))
    path.appendBezierPathWithArcFromPoint_toPoint_radius_((x1, y1), (x1 - r, y1), r)
    if arrow_x is not None:
        half = ARROW_WIDTH / 2
        ax = max(x0 + r + half, min(arrow_x, x1 - r - half))
        tip = height - i
        path.lineToPoint_((ax + half, y1))  # soft sides and tip, like a popover's arrow
        path.curveToPoint_controlPoint1_controlPoint2_((ax, tip), (ax + half * 0.45, y1), (ax + 2.5, tip))
        path.curveToPoint_controlPoint1_controlPoint2_((ax - half, y1), (ax - 2.5, tip), (ax - half * 0.45, y1))
    path.lineToPoint_((x0 + r, y1))
    path.appendBezierPathWithArcFromPoint_toPoint_radius_((x0, y1), (x0, y1 - r), r)
    path.lineToPoint_((x0, y0 + r))
    path.appendBezierPathWithArcFromPoint_toPoint_radius_((x0, y0), (x0 + r, y0), r)
    path.closePath()
    return path


def outline_mask(width, height, outline):
    """The panel's material, cut to its outline."""
    def draw(_rect):
        NSColor.blackColor().set()
        outline.fill()
        return True
    return NSImage.imageWithSize_flipped_drawingHandler_(NSMakeSize(width, height), False, draw)


def web_view(url, frame, transparent=False, handler=None):
    config = WKWebViewConfiguration.alloc().init()
    if handler is not None:
        config.userContentController().addScriptMessageHandler_name_(handler, "app")
    view = WKWebView.alloc().initWithFrame_configuration_(frame, config)
    if transparent:
        view.setValue_forKey_(False, "drawsBackground")  # let the popover material show through
        if view.respondsToSelector_("setUnderPageBackgroundColor:"):
            view.setUnderPageBackgroundColor_(NSColor.clearColor())
    view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
    return view


class Bridge(NSObject, protocols=protocols("WKScriptMessageHandler")):
    """Messages from the panel page: its height, and Full View / Quit."""

    def initWithApp_(self, app):
        self = objc.super(Bridge, self).init()
        self.app = app
        return self

    def userContentController_didReceiveScriptMessage_(self, _controller, message):
        body = message.body()
        kind = body.get("type") if hasattr(body, "get") else None
        if kind in ("height", "width"):
            self.app.resize_panel(kind, float(body.get("value") or 0))
            if kind == "height":
                self.app.page_shown()
        elif kind == "dock":
            self.app.dock()
        elif kind == "dragStart":
            self.app.drag_start()
        elif kind == "dragMove":
            self.app.drag_move()
        elif kind == "dragEnd":
            self.app.drag_end()
        elif kind == "escape":
            if not self.app.detached:
                self.app.hide_panel()
        elif kind == "full":
            self.app.hide_panel()
            self.app.showFullView_(None)
        elif kind == "quit":
            self.app.quit_(None)


class MenuBarApp(NSObject, protocols=protocols("NSWindowDelegate")):
    def initWithController_server_openNow_cleanup_(self, controller, server, open_now, cleanup):
        self = objc.super(MenuBarApp, self).init()
        self.controller, self.server, self.open_now, self.cleanup = controller, server, open_now, cleanup
        self.url = server.launch_url
        self.state = controller.snapshot()
        self.last_active = {a["provider"]: a["id"] for a in active_accounts(self.state)}
        self.full = None  # the full view's process (fullview_mac_app.Started)
        self.panel_size = [PANEL_WIDTH, 420]
        self.detached = False
        self.drag = None
        self.click_monitor = None
        self.quitting = False
        return self

    # ---------- setup ----------
    def applicationDidFinishLaunching_(self, _note):
        NSApp.setMainMenu_(self.main_menu())
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.item.setAutosaveName_("AccountSwitcher")  # macOS remembers its place and visibility by this
        self.item.setVisible_(True)
        button = self.item.button()
        button.setTarget_(self)
        button.setAction_("statusClicked:")
        button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
        self.shown_level = object()
        self.bridge = Bridge.alloc().initWithApp_(self)
        self.panel_view = None  # the page: loaded when the panel opens, let go a while after it closes
        self.page_ready = self.page_drawn = self.show_pending = False
        self.page_generation = 0
        self.panel = self.make_panel()
        self.server.quit = lambda: AppHelper.callAfter(self.quit_, None)  # the API's shutdown
        self.controller.quit_app = lambda: AppHelper.callAfter(self.quit_, None)
        self.controller.on_update_available = lambda version: notify(
            APP, f"Version {version} is available. Update from the full view's Settings.")
        self.server.show = lambda: AppHelper.callAfter(self.showFullView_, None)  # opened again (Spotlight, Finder)
        self.refresh()
        threading.Thread(target=self.watch, daemon=True).start()
        if self.open_now:
            self.showFullView_(None)
        AppHelper.callLater(4, self.check_status_item)
        AppHelper.callLater(30, self.trim_regularly)
        if os.environ.get("LIMITSWITCH_PANEL_TEST"):  # CI: it can't click the menu bar
            AppHelper.callLater(3, self.panel_test)

    @objc.python_method
    def check_status_item(self):
        """macOS gives a status item it won't show a height of 0 (it's hidden: not allowed in
        the menu bar, or no room). Then the app would be invisible, so say so and open the window."""
        if os.environ.get("ACCOUNT_SWITCHER_DEBUG"):
            self.describe_status_item()
        window = self.item.button().window() if self.item.button() is not None else None
        if window is None or window.frame().size.height > 0:
            return
        self.describe_status_item()
        self.showFullView_(None)
        from AppKit import NSAlert, NSUserDefaults
        defaults = NSUserDefaults.standardUserDefaults()
        if defaults.boolForKey_("HiddenIconAlertSuppressed"):
            return
        alert = NSAlert.alloc().init()
        alert.setMessageText_("macOS is hiding LimitSwitcher's menu bar icon")
        alert.setInformativeText_("Turn on LimitSwitcher under System Settings › Menu Bar › Allow in the Menu Bar. "
                                  "If it's already on, the menu bar may be full: quit another menu bar app, or hold ⌘ "
                                  "and drag icons out to make room. Everything also works from this window.")
        alert.addButtonWithTitle_("Open Menu Bar Settings")
        alert.addButtonWithTitle_("OK")
        alert.setShowsSuppressionButton_(True)
        NSApp.activateIgnoringOtherApps_(True)
        choice = alert.runModal()
        if alert.suppressionButton().state():
            defaults.setBool_forKey_(True, "HiddenIconAlertSuppressed")
        if choice == 1000:  # NSAlertFirstButtonReturn
            subprocess.Popen(["/usr/bin/open", "x-apple.systempreferences:com.apple.ControlCenter-Settings.extension"])

    @objc.python_method
    def describe_status_item(self):
        button = self.item.button()
        window = button.window() if button is not None else None
        frame = window.frame() if window is not None else None
        log.warning("status item: visible=%s button=%s window=%s frame=%s onscreen=%s occlusion=%s level=%s number=%s",
                    self.item.isVisible(), button is not None, window is not None,
                    None if frame is None else (frame.origin.x, frame.origin.y, frame.size.width, frame.size.height),
                    None if window is None else window.isVisible(),
                    None if window is None else window.occlusionState(),
                    None if window is None else window.level(),
                    None if window is None else window.windowNumber())

    @objc.python_method
    def main_menu(self):
        """The standard app and Edit / Window menus: ⌘Q, ⌘W, copy and paste in the window."""
        bar = NSMenu.alloc().init()

        def submenu(title, items):
            holder = bar.addItemWithTitle_action_keyEquivalent_(title, None, "")
            menu = NSMenu.alloc().initWithTitle_(title)
            for entry in items:
                if entry is None:
                    menu.addItem_(NSMenuItem.separatorItem())
                    continue
                label, action, key, *rest = entry
                item = menu.addItemWithTitle_action_keyEquivalent_(label, action, key)
                if rest and rest[0] == "self":
                    item.setTarget_(self)
                elif rest:
                    item.setKeyEquivalentModifierMask_(rest[0])
            holder.setSubmenu_(menu)
            return menu

        submenu(APP, [(f"About {APP}", "orderFrontStandardAboutPanel:", ""), None,
                      (f"Hide {APP}", "hide:", "h"),
                      ("Hide Others", "hideOtherApplications:", "h", NSEventModifierFlagCommand | NSEventModifierFlagOption),
                      None, (f"Quit {APP}", "quit:", "q", "self")])
        submenu("Edit", [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), None, ("Cut", "cut:", "x"),
                         ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")])
        window = submenu("Window", [("Minimize", "performMiniaturize:", "m"), ("Close", "performClose:", "w"), None,
                                    ("Show LimitSwitcher", "showFullView:", "0", "self")])
        NSApp.setWindowsMenu_(window)
        return bar

    def applicationShouldTerminate_(self, _app):
        """Every way of quitting (⌘Q, the Dock, the menus, logging out) ends here. Cocoa ends the
        process right after, so undo the Codex / Claude changes now."""
        if self.full is not None and self.full.alive():
            self.full.close()  # also when it was quit some other way than quit_ (still starting, too)
        try:
            self.cleanup()
        except Exception:
            log.exception("cleanup on quit failed")
        return TERMINATE_NOW

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _visible):
        self.showFullView_(None)  # clicked in the Dock or opened again
        return False

    def applicationDidResignActive_(self, _note):
        if not self.detached:
            self.hide_panel()  # docked: goes away like a popover

    @objc.python_method
    def make_panel(self):
        """The panel's window: borderless, the popover material with rounded corners and a
        shadow, the page on top. Made once; shown and hidden."""
        from AppKit import NSAppearance, NSVisualEffectView
        width, height = self.panel_size
        style = 1 << 7  # borderless, non-activating panel
        panel = PanelWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setLevel_(POPUP_LEVEL)
        panel.setCollectionBehavior_((1 << 0) | (1 << 8))  # every Space, and over full-screen apps
        dark = NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
        if dark is not None:
            panel.setAppearance_(dark)  # the dark panel design, like the Windows tray
        material = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        material.setMaterial_(6)  # NSVisualEffectMaterialPopover
        material.setBlendingMode_(0)  # behind the window
        material.setState_(1)  # always active
        material.setAutoresizingMask_(2 | 16)  # width and height follow the window
        self.panel_edge = PanelEdge.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
        self.panel_edge.setAutoresizingMask_(2 | 16)
        material.addSubview_(self.panel_edge)
        panel.setContentView_(material)
        self.panel_material = material
        self.arrow_x = None
        return panel

    @objc.python_method
    def shape_panel(self, frame):
        """Lay the panel out for `frame` (its new size): the page below the arrow while docked,
        the material cut to the outline, the edge along it."""
        width, height = frame.size.width, frame.size.height
        arrow = None if self.detached else self.arrow_x
        page = height - (ARROW_HEIGHT if arrow is not None else 0)
        self.panel_material.setFrame_(NSMakeRect(0, 0, width, height))
        if self.panel_view is not None:
            self.panel_view.setFrame_(NSMakeRect(0, 0, width, page))
        self.panel_edge.setFrame_(NSMakeRect(0, 0, width, height))
        outline = panel_outline(width, height, arrow)
        self.panel_material.setMaskImage_(outline_mask(width, height, outline))
        self.panel_edge.outline = outline
        self.panel_edge.setNeedsDisplay_(True)

    @objc.python_method
    def docked_frame(self):
        """Just under the menu bar icon, centered on it, kept on its screen."""
        from AppKit import NSScreen
        width, height = self.panel_size
        button = self.item.button()
        window = button.window() if button is not None else None
        if window is None:
            screen = NSScreen.mainScreen().visibleFrame()
            return NSMakeRect(screen.origin.x + screen.size.width - width - 12,
                              screen.origin.y + screen.size.height - height - 6, width, height)
        icon = window.convertRectToScreen_(button.convertRect_toView_(button.bounds(), None))
        screen = (window.screen() or NSScreen.mainScreen()).visibleFrame()
        center = icon.origin.x + icon.size.width / 2
        x = center - width / 2
        x = max(screen.origin.x + 8, min(x, screen.origin.x + screen.size.width - width - 8))
        self.arrow_x = center - x  # the arrow points at the icon even when the panel is pushed aside
        height += ARROW_HEIGHT
        top = icon.origin.y - 2
        return NSMakeRect(x, top - height, width, height)

    @objc.python_method
    def load_page(self):
        """The panel's page (a WKWebView, and the Web Content process macOS runs for it)."""
        base, token = self.url.split("/#token=")
        width, height = self.panel_size
        view = web_view(f"{base}/menu#token={token}", NSMakeRect(0, 0, width, height), transparent=True,
                        handler=self.bridge)
        view.setAutoresizingMask_(2 | 16)  # the arrow's room above it stays fixed
        self.panel_material.addSubview_positioned_relativeTo_(view, -1, self.panel_edge)  # under the edge
        self.panel_view = view
        self.page_ready = self.page_drawn = False
        self.page_started = time.monotonic()

    @objc.python_method
    def drop_page(self, generation):
        """Closed for PAGE_KEEP seconds: let the page go. Its Web Content process ends with it, and
        the panel stays light while it's closed (the next click loads it again, in a blink)."""
        if generation != self.page_generation or self.panel_view is None or self.panel.isVisible():
            return
        view, self.panel_view = self.panel_view, None
        view.configuration().userContentController().removeScriptMessageHandlerForName_("app")
        view.removeFromSuperview()
        self.page_ready = self.show_pending = False
        from .memory import trim_soon
        trim_soon()

    @objc.python_method
    def page_shown(self, late=False):
        """The page has drawn (it sent its height): a panel waiting for it opens now."""
        if not late:
            self.page_drawn = True
        if not self.page_ready and not late and os.environ.get("LIMITSWITCH_PANEL_TEST"):
            log.warning("panel test: page drew in %.2f s", time.monotonic() - self.page_started)
        self.page_ready = True
        if self.show_pending:
            self.show_pending = False
            self.show_panel()

    @objc.python_method
    def page_late(self):
        if self.show_pending and not self.page_ready:
            log.warning("panel: the page took over 1.5 s to draw; opened without waiting")
        self.page_shown(late=True)

    @objc.python_method
    def show_panel(self):
        self.page_generation += 1  # a pending drop_page no longer applies
        if self.panel_view is None:
            self.load_page()
        if not self.page_ready:  # opens once the page has drawn, so it never shows empty
            if not self.show_pending:
                self.show_pending = True
                # WebKit only draws a page that is on screen: put the panel there, invisible and
                # letting clicks through, until the page reports its size.
                frame = self.docked_frame()
                self.shape_panel(frame)
                self.panel.setFrame_display_(frame, False)
                self.panel.setIgnoresMouseEvents_(True)
                self.panel.setAlphaValue_(0.0)
                self.panel.orderFront_(None)
                AppHelper.callLater(1.5, self.page_late)  # in case the page is slow: open anyway
            return
        self.panel.setIgnoresMouseEvents_(False)
        self.set_detached(False)
        frame = self.docked_frame()
        self.shape_panel(frame)
        self.panel.setFrame_display_(frame, True)
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.setAlphaValue_(0.0)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.animator().setAlphaValue_(1.0)
        if self.click_monitor is None:  # docked: a click in another app closes it
            from AppKit import NSEvent, NSEventMaskLeftMouseDown, NSEventMaskRightMouseDown
            self.click_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown, self.clicked_elsewhere)
        try:
            self.controller.action("refresh", {"ifOlderThan": 45})  # like opening the Windows panel
        except (RuntimeError, ValueError):
            pass

    @objc.python_method
    def panel_test(self, step=0):
        """CI only (LIMITSWITCH_PANEL_TEST): open the panel, move it away as a drag would, dock
        it again; each frame goes to app.log for the smoke test."""
        def frame():
            f = self.panel.frame()
            return f"({f.origin.x:.0f}, {f.origin.y:.0f}, {f.size.width:.0f}, {f.size.height:.0f})"
        if step == 0:
            self.show_panel()
            AppHelper.callLater(0.5, self.panel_test, 0.5)
        elif step == 0.5:  # a busy runner can take a while for the first page: wait for it (up to 10 s)
            self.test_waits = getattr(self, "test_waits", 0) + 1
            waiting = not self.page_drawn and self.test_waits < 20
            AppHelper.callLater(0.5 if waiting else 1, self.panel_test, 0.5 if waiting else 1)
        elif step == 1:
            log.warning("panel test: docked %s visible=%s", frame(), self.panel.isVisible())
            f = self.panel.frame()
            self.panel.setFrameOrigin_((f.origin.x - 300, f.origin.y - 200))  # as drag_move does
            self.set_detached(True)
            AppHelper.callLater(1, self.panel_test, 2)
        elif step == 2:
            log.warning("panel test: moved %s detached=%s", frame(), self.detached)
            self.dock()
            AppHelper.callLater(1, self.panel_test, 3)
            return
        elif step == 3:  # the full view's own process draws it and logs "full view drawn"
            log.warning("panel test: docked again %s detached=%s visible=%s", frame(), self.detached,
                        self.panel.isVisible())
            self.hide_panel()
            self.showFullView_(None)
            AppHelper.callLater(3, self.panel_test, 4)
        elif step == 4:  # the page let go (as PAGE_KEEP after closing) and loaded again: a second load
            self.drop_page(self.page_generation)
            self.show_panel()
            AppHelper.callLater(3, self.panel_test, 5)
        elif step == 5:
            log.warning("panel test: opened again after its page was let go, visible=%s", self.panel.isVisible())
            self.hide_panel()

    @objc.python_method
    def trim_regularly(self):
        """Every 10 minutes: memory freed by usage checks and redraws goes back to macOS."""
        from .memory import trim
        trim()
        AppHelper.callLater(600, self.trim_regularly)

    @objc.python_method
    def clicked_elsewhere(self, _event):
        if not self.detached:
            self.hide_panel()

    @objc.python_method
    def hide_panel(self):
        self.show_pending = False
        if self.panel is not None and self.panel.isVisible():
            self.panel.orderOut_(None)
            self.page_generation += 1
            AppHelper.callLater(PAGE_KEEP, self.drop_page, self.page_generation)
            from .memory import trim_soon
            trim_soon()
        if self.click_monitor is not None:
            from AppKit import NSEvent
            NSEvent.removeMonitor_(self.click_monitor)
            self.click_monitor = None
        self.drag = None

    @objc.python_method
    def set_detached(self, detached):
        """Detached (dragged away): stays open, floats above other windows, shows its dock button,
        and loses the arrow (the window gets shorter by it; the page stays where it is)."""
        was = self.detached
        self.detached = detached
        if detached and not was and self.panel.isVisible():
            f = self.panel.frame()
            frame = NSMakeRect(f.origin.x, f.origin.y, f.size.width, f.size.height - ARROW_HEIGHT)
            self.shape_panel(frame)
            self.panel.setFrame_display_(frame, True)
        from AppKit import NSFloatingWindowLevel
        self.panel.setLevel_(NSFloatingWindowLevel if detached else POPUP_LEVEL)
        if self.panel_view is not None:
            self.panel_view.evaluateJavaScript_completionHandler_(
                f"window.setDetached && window.setDetached({'true' if detached else 'false'})", None)

    # ---------- status item ----------
    def statusClicked_(self, sender):
        event = NSApp.currentEvent()
        if event is not None and (event.type() == NSEventTypeRightMouseUp
                                  or event.modifierFlags() & NSEventModifierFlagControl):
            self.item.setMenu_(self.build_menu())
            self.item.button().performClick_(None)  # shows the menu
            self.item.setMenu_(None)  # so the next left click opens the panel again
            return
        self.togglePanel_(sender)

    def togglePanel_(self, _sender):
        if self.panel.isVisible():
            self.hide_panel()
        else:
            self.show_panel()

    @objc.python_method
    def resize_panel(self, kind, value):
        """The page's size (compact is narrower and shorter). The top edge stays put."""
        if value <= 0:
            return
        if kind == "width":
            self.panel_size[0] = value
        else:
            from AppKit import NSScreen
            screen = NSScreen.mainScreen()
            room = screen.visibleFrame().size.height - 40 if screen is not None else 760
            self.panel_size[1] = min(value, room)  # taller than the screen: the page scrolls
        if self.detached:
            f = self.panel.frame()
            top = f.origin.y + f.size.height
            frame = NSMakeRect(f.origin.x, top - self.panel_size[1], *self.panel_size)
        elif self.panel.isVisible():
            frame = self.docked_frame()
        else:
            return
        self.shape_panel(frame)
        self.panel.setFrame_display_(frame, True)

    @objc.python_method
    def dock(self):
        """The dock button: the panel slides back under the menu bar icon (arrow and all)."""
        self.set_detached(False)
        frame = self.docked_frame()
        self.shape_panel(frame)
        self.panel.setFrame_display_animate_(frame, True, True)

    # Dragging: the page covers the whole window, so it reports a press on its header or
    # background (dragStart), each move (dragMove) and the release (dragEnd), and the window
    # follows the mouse from where it was pressed. Works docked or not, as often as you like.
    @objc.python_method
    def drag_start(self):
        from AppKit import NSEvent
        mouse, frame = NSEvent.mouseLocation(), self.panel.frame()
        self.drag = (mouse.x, mouse.y, frame.origin.x, frame.origin.y)

    @objc.python_method
    def drag_move(self):
        if self.drag is None:
            return
        from AppKit import NSEvent
        mouse = NSEvent.mouseLocation()
        x0, y0, fx, fy = self.drag
        dx, dy = mouse.x - x0, mouse.y - y0
        self.panel.setFrameOrigin_((fx + dx, fy + dy))
        if not self.detached and abs(dx) + abs(dy) > 6:  # moved away from the menu bar: it stays open
            self.set_detached(True)

    @objc.python_method
    def drag_end(self):
        self.drag_move()
        self.drag = None

    @objc.python_method
    def build_menu(self):
        state = self.controller.snapshot()
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        def add(title, action, key="", checked=None, enabled=True):
            item = menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
            item.setTarget_(self)
            item.setEnabled_(enabled)
            if checked is not None:
                item.setState_(NSOnState if checked else NSOffState)
            return item

        add("Open Panel", "togglePanel:")
        add("Full View…", "showFullView:")
        menu.addItem_(NSMenuItem.separatorItem())
        add("Auto Swap", "toggleAutoSwap:", checked=state["autoSwap"], enabled=not state["busy"])
        add("Auto Resume", "toggleAfk:", checked=state["afk"], enabled=not state["busy"])
        menu.addItem_(NSMenuItem.separatorItem())
        add(f"Quit {APP}", "quit:", "q")
        return menu

    def toggleAutoSwap_(self, _sender):
        self.set_pref("autoSwap")

    def toggleAfk_(self, _sender):
        self.set_pref("afk")

    @objc.python_method
    def set_pref(self, key):
        state = self.controller.snapshot()
        prefs = {"autoSwap": state["autoSwap"], "afk": state["afk"]}
        prefs[key] = not prefs[key]
        try:
            self.controller.action("preferences", prefs)
        except (RuntimeError, ValueError) as error:
            notify(APP, str(error))

    # ---------- full view ----------
    def showFullView_(self, _sender):
        """Starts the full view's process, or brings it to the front if it's open."""
        if self.full is not None and self.full.alive():
            self.full.front()
            return
        from .fullview_mac_app import start
        try:
            self.full = start(self.url)
        except Exception:
            log.exception("the full view couldn't start")
            notify(APP, "The full view couldn't open. Details are in app.log.")

    # ---------- state ----------
    @objc.python_method
    def refresh(self):
        state = self.state = self.controller.snapshot()
        level = tray_level(state)
        if level != self.shown_level:
            self.shown_level = level
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(SYMBOLS.get(level), APP)
            if image is None:  # older macOS: plain text
                self.item.button().setTitle_("⇄")
            else:
                image.setTemplate_(True)
                self.item.button().setImage_(image)
        self.item.button().setToolTip_(tooltip(state))
        self.announce_failovers(state)

    @objc.python_method
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
            notify(f"{dict(PROVIDERS)[provider]} switched accounts", f"{reason}Now using {short_name(account)}.")

    @objc.python_method
    def watch(self):
        """Block until the controller changes, then refresh on the main thread."""
        seen = self.state["revision"]
        condition = self.controller.condition
        while True:
            with condition:
                condition.wait_for(lambda: self.controller.revision != seen or self.controller.closed)
                if self.controller.closed:
                    break
            time.sleep(.15)
            seen = self.controller.revision
            AppHelper.callAfter(self.refresh)
        AppHelper.callAfter(self.quit_, None)

    def quit_(self, _sender):
        if not self.quitting:
            self.quitting = True
            self.hide_panel()
            if self.full is not None and self.full.alive():
                self.full.close()
            NSApp.terminate_(None)  # -> applicationShouldTerminate_, which cleans up


def run(controller, server, open_now=False, cleanup=lambda: None):
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # menu bar only, no Dock icon
    delegate = MenuBarApp.alloc().initWithController_server_openNow_cleanup_(controller, server, open_now, cleanup)
    app.setDelegate_(delegate)
    AppHelper.runEventLoop(installInterrupt=True)
