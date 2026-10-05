"""The full view's behaviour, independent of the platform: what is where, what the pointer is over,
what a click or a key does, and which cached tiles to redraw. A host (fullview_win, fullview_mac,
fullview_tk) owns the window: it passes input here, asks frame() for pixels, and runs the few
timers this asks for (a minute tick for "resets in", toast expiry, a switch timeout). Nothing runs
while the window is idle, and closing it frees every cached tile.
"""
import calendar
import json
import math
import re
import time

from PIL import Image

from . import fullview_render as vr

FAIL = re.compile(r"fail|exhaust|error|stopped|attention|interrupt|quota|not found|closed without", re.I)
SWAP = re.compile(r"→|swap|selected|routed|failover|continue|now uses", re.I)
OK = re.compile(r"added|completed|started|restored|opened", re.I)


def classify(text):
    if FAIL.search(text) and not re.search(r"→|failover|routed|selected", text, re.I):
        return "fail"
    if SWAP.search(text):
        return "swap"
    return "ok" if OK.search(text) else ""


def window_size(area_width, area_height):
    """Half the work area's width, and tall enough for two rows of cards (920, or 95% of a shorter
    screen), in logical px."""
    return max(720, round(area_width / 2)), max(480, min(920, round(area_height * .95)))


def widest_percent():
    """Logical width of the widest percentage a card shows ("100%")."""
    global _widest
    if _widest is None:
        _widest = vr.text_w("100%", 13, True)
    return _widest


_widest = None


def merge_boxes(boxes, size):
    """Boxes clipped to the page, with overlapping ones merged (each area is then drawn once)."""
    width, height = size
    out = []
    for x0, y0, x1, y1 in boxes:
        box = [max(0, x0), max(0, y0), min(width, x1), min(height, y1)]
        if box[0] >= box[2] or box[1] >= box[3]:
            continue
        merged = True
        while merged:
            merged = False
            for other in out:
                if box[0] <= other[2] and other[0] <= box[2] and box[1] <= other[3] and other[1] <= box[3]:
                    out.remove(other)
                    box = [min(box[0], other[0]), min(box[1], other[1]), max(box[2], other[2]), max(box[3], other[3])]
                    merged = True
                    break
        out.append(box)
    return [tuple(b) for b in out]


ANIM_MS = 15  # between animation frames: under Windows' timer tick (15.6 ms), so one frame per tick


def ease(p):
    """The web's ease-out (no overshoot)."""
    return 1 - (1 - p) ** 3


class Motion:
    """Values that glide to a target over a short time. Only while something moves does the view
    ask its host for frames (about 60 a second); at rest there are none."""

    def __init__(self):
        self.values, self.runs = {}, {}

    def get(self, key, default=0.0):
        return self.values.get(key, default)

    def to(self, key, target, seconds, start=None, delay=0.0):
        """Glide key to target. A key seen for the first time appears at its target, unless a start is given."""
        if start is None:
            if key not in self.values:
                self.values[key] = target
                return
            start = self.values[key]
        run = self.runs.get(key)
        if (run[1] if run else self.values.get(key)) == target and start == self.values.get(key):
            return
        self.values[key] = start
        self.runs[key] = (start, target, time.perf_counter() + delay, seconds)

    def settle(self):
        """Jump everything to where it is going."""
        for key, run in self.runs.items():
            self.values[key] = run[1]
        self.runs.clear()

    def step(self):
        now = time.perf_counter()
        for key, (start, end, began, seconds) in list(self.runs.items()):
            p = min(1.0, max(0.0, (now - began) / seconds))
            self.values[key] = start + (end - start) * ease(p)
            if p >= 1:
                del self.runs[key]
        return bool(self.runs)


JEV_KEY = "jevkey"  # the editing id of the OpenRouter key field in Settings (its text is never drawn)


class UI:
    """What the drawing needs to know beyond the app state."""

    def __init__(self):
        self.hover = None          # action under the pointer
        self.hover_card = None     # account whose card the pointer is over
        self.pending = None        # account being switched to
        self.confirm = None        # account asking "Remove?"
        self.editing = None        # (account id or JEV_KEY, text, caret, all selected)
        self.jev_confirm = False   # Settings: "Remove" on the saved OpenRouter key was clicked once; the next click confirms
        self.revealed = set()      # accounts whose email is shown in name mode
        self.menu = None           # "settings" | "add"
        self.editor = None         # the renewal date editor's state
        self.signing_in = ()
        self.anchors = {}
        self.fades = {}            # what is animating right now: hover amounts, toggle positions, the spin
        self.window = None         # the window picked in the Windows list (separate accounts per window)


class FullView:
    def __init__(self, controller, host, state):
        self.controller, self.host = controller, host
        self.state = state
        self.ui = UI()
        self.scroll = 0.0
        self.width = self.height = 0
        self.scale = 1.0
        self.tiles = {}            # key -> (cache key, Tile)
        self.items, self.content_h = [], 0
        self.page_hits, self.overlay_hits = [], []
        self.prefs = {}            # toggles flipped here, shown at once until the state catches up
        self.toast_list = []       # [(text, kind, expires at)]
        self.last_log = None
        self.pointer = None
        self.pressed = None
        self.motion = Motion()
        self.seen = set()          # tiles already on screen (new ones rise in, like the web page)
        self.last_page = None      # (layout, [(box, what)], image): the page, to redraw only what changes
        self.incremental = True
        self.native = False        # a host that draws with the platform (macOS): tiles are recorded, not drawn
        self.changed = None        # device boxes the last frame changed (None: all of it), for the host
        self.had_overlays = False
        self.take_log(state)
        try:
            controller.action("refresh", {"ifOlderThan": 60})  # fresh numbers when the window opens
        except (RuntimeError, ValueError):
            pass

    # ---------- state ----------
    def set_state(self, state):
        self.state = state
        ui = self.ui
        ui.signing_in = tuple(state.get("signingIn") or ())
        if ui.pending and any(a["id"] == ui.pending and a["active"] for a in state["accounts"]):
            ui.pending = None  # the switch landed
            self.host.kill_timer("pending")
        ids = {a["id"] for a in state["accounts"]}
        if ui.confirm not in ids:
            ui.confirm = None
        if ui.window not in {w["id"] for w in state.get("windows") or []}:
            ui.window = None  # that window closed
        if ui.editing and ui.editing[0] not in ids and ui.editing[0] != JEV_KEY:
            ui.editing = None
        if ui.editing and ui.editing[0] == JEV_KEY and not (ui.menu == "settings" and state.get("jevCompact")):
            ui.editing = None
        if state.get("jevKey") != "file":
            ui.jev_confirm = False
        if self.prefs and not state.get("busy"):
            self.prefs = {k: v for k, v in self.prefs.items() if bool(state.get(k)) != v}
        self.take_log(state)
        self.relayout()
        self.host.invalidate()

    def take_log(self, state):
        log = state.get("log") or []
        newest = log[-1]["id"] if log else 0
        if self.last_log is None:
            self.last_log = newest  # no toasts for history
            return
        fresh = [e for e in log if e["id"] > self.last_log][-3:]
        self.last_log = newest
        for entry in fresh:
            kind = classify(entry["text"])
            if kind:
                self.toast(entry["text"], "error" if kind == "fail" else "ok" if kind == "ok" else "")

    def toast(self, text, kind=""):
        now = time.monotonic()
        self.toast_list.append((text, kind, now + (6.0 if kind == "error" else 3.8), now))
        self.toast_list = self.toast_list[-4:]
        self.schedule_toasts()
        self.host.invalidate()

    def schedule_toasts(self):
        if self.toast_list:  # wake when the first one starts fading out
            wait = min(t[2] for t in self.toast_list) - 0.2 - time.monotonic()
            self.host.set_timer("toast", max(20, int(wait * 1000)))
        else:
            self.host.kill_timer("toast")

    def timer(self, name):
        if name == "anim":
            pass  # just the next frame
        elif name == "toast":
            self.host.kill_timer("toast")  # frames take over until it has faded out
        elif name == "pending":
            self.host.kill_timer("pending")
            self.ui.pending = None
        elif name == "minute":  # reset times move on: the next minute's cache keys redraw the cards
            self.host.set_timer("minute", 60_000 - int(time.time() * 1000) % 60_000 + 50)
        self.host.invalidate()

    # ---------- geometry ----------
    def resize(self, width, height, scale):
        if (width, height, scale) == (self.width, self.height, self.scale):
            return
        if scale != self.scale:
            self.tiles.clear()
        self.width, self.height, self.scale = width, height, scale
        self.relayout()
        if not self.host.has_timer("minute"):
            self.timer("minute")

    def relayout(self):
        if not self.width:
            return
        self.items, self.content_h = vr.layout(self.state, self.width)
        self.scroll = max(0.0, min(self.scroll, self.max_scroll()))

    def max_scroll(self):
        return max(0.0, self.content_h - self.height)

    def wheel(self, pixels):
        """Scroll by `pixels` logical px (positive: down the page)."""
        before = self.scroll
        self.scroll = max(0.0, min(self.scroll + pixels, self.max_scroll()))
        if self.scroll != before:
            self.ui.menu = None if self.ui.menu == "add" else self.ui.menu
            self.refresh_hover()
            self.host.invalidate()

    # ---------- drawing ----------
    def tile(self, kind, key, w, h, data):
        ui, state = self.ui, self.state
        live = state.get("mode") == "live"
        locked = bool(state.get("busy") or ui.pending)
        if kind == "card":
            cache = vr.card_key(data, ui, bool(state.get("nameMode")), live, locked)
            make = lambda: (vr.record_card if self.native else vr.draw_card)(
                data, w, h, self.scale, ui, bool(state.get("nameMode")), live, locked)
        elif kind == "topbar":
            cache = (w, tuple(vr.quantize(ui.fades.get(k, 0.0)) for k in ("settings", "add", "refresh", "update:check", "update:install")),
                     ui.fades.get("spin", 0.0), ui.menu, live, state.get("busy"), state.get("afk"), state.get("autoSwap"),
                     repr(sorted((state.get("update") or {}).items())))
            make = lambda: (vr.record_topbar if self.native else vr.draw_topbar)(state, w, self.scale, ui)
        elif kind == "windows":
            cache = (w, h, json.dumps([state.get("windows"), [(a["id"], a.get("label"), a.get("email")) for a in state.get("accounts") or []],
                                       state.get("nameMode"), ui.window,
                                       sorted((k, vr.quantize(v)) for k, v in ui.fades.items() if k.startswith("window:"))], default=str))
            make = lambda: (vr.record_windows if self.native else vr.draw_windows)(state, w, h, self.scale, ui)
        elif kind == "group":
            cache = (w, data)
            make = lambda: (vr.record_group if self.native else vr.draw_group)(data, w, self.scale)
        else:
            cache = (w, live, ui.hover)
            make = lambda: (vr.record_empty if self.native else vr.draw_empty)(state, w, self.scale, ui)
        held = self.tiles.get(key)
        if held and held[0] == cache:
            return held[1]
        made = make()
        self.tiles[key] = (cache, made)
        return made

    def build(self):
        """What goes on the page this frame: ([(device box, signature, how to draw it)], still
        moving). Also sets the page's clickable regions."""
        s, motion, ui = self.scale, self.motion, self.ui
        moving = motion.step()
        ui.fades = {key[1]: value for key, value in motion.values.items() if key[0] in ("h", "tog", "spin") and value}
        vr.CLOCK_24 = self.state.get("clock24")
        prefs = {k: self.prefs.get(k, bool(self.state.get(k))) for k in ("autoSwap", "afk", "nameMode", "taskbar", "launchAtLogin", "clock24", "afkSkipLarge", "waitNearReset", "jevCompact", "perWindow", "resetAlerts")}
        for key, on in prefs.items():
            motion.to(("tog", "tog:" + key), 1.0 if on else 0.0, 0.2)
            ui.fades["tog:" + key] = motion.get(("tog", "tog:" + key))
        # What goes on the page, in drawing order: (device box, what it shows, how to draw it).
        ops = []
        hits = []
        keep = set()
        fresh = []
        for index, (kind, key, x, y, w, h, data) in enumerate(self.items):
            keep.add(key)
            if key not in self.seen:  # rises in: 4 px up and fades in, 25 ms after the one before
                self.seen.add(key)
                motion.to(("rise", key), 1.0, 0.3, start=0.0, delay=0.025 * min(index, 12))
                fresh.append((key, 0.025 * min(index, 12)))
                moving = True
            rise = motion.get(("rise", key), 1.0)
            top = y - self.scroll + 4 * (1 - rise)
            if top > self.height or top + h < -vr.SHADOW:
                continue  # off screen: not drawn (still cached)
            t = self.tile(kind, key, w, h, data)
            tx, ty = round(x * s) - t.margin, round(top * s) - t.margin
            ops.append(((tx, ty, tx + t.width, ty + t.height),
                        ("tile", key, self.tiles[key][0], tx, ty, rise), ("tile", t, tx, ty, rise)))
            for part in t.live:  # bars fill and percentages count, on top of the cached card
                params, still = self.live(part, x, top, rise)
                moving |= still
                if params:
                    ops.append((self.live_box(params), params, ("live", params)))
            for (hx, hy, hw, hh), action, cursor in t.hits:
                hits.append(((x + hx, y + hy, hw, hh), action, cursor, key))
        for key in list(self.tiles):  # accounts that went away
            if key not in keep:
                del self.tiles[key]
        now = time.perf_counter()
        for key, delay in fresh:  # drawing them took a moment: start their rise from here
            start, end, _, seconds = motion.runs[("rise", key)]
            motion.runs[("rise", key)] = (start, end, now + delay, seconds)
        self.page_hits = hits
        return ops, moving

    def frame(self):
        """The window's pixels (RGB, device px) and the clickable regions."""
        ops, moving = self.build()
        image = self.compose(ops)
        self.overlay_hits = []
        overlays = self.overlays_showing()
        if overlays:
            image = image.copy()  # the page stays as it is, for the next frame to build on
            moving |= self.draw_overlays(vr.Surface(image, self.scale))
        else:
            self.draw_overlays(vr.Surface(image, self.scale))  # nothing to draw; keeps the toast timers right
        if overlays or self.had_overlays:
            self.changed = None  # menus and toasts sit on top of the page: all of it (while they show)
        self.had_overlays = overlays
        self.animate(moving)
        return image

    def animate(self, moving):
        """Frames keep coming while something moves. A running timer is left alone: on Windows it
        repeats by itself, and setting it again after each frame would start its wait over, so
        frames came a frame's drawing time plus 16 ms apart, which Windows' 15.6 ms timer tick
        rounds up to every other tick (32 a second). 15 ms keeps it on every tick (64 a second).
        The macOS and Linux hosts' timers fire once, so they are set again here each frame."""
        if moving or self.motion.runs:
            if not self.host.has_timer("anim"):
                self.host.set_timer("anim", ANIM_MS)
        else:
            self.host.kill_timer("anim")

    def draw_native(self, painter):
        """Draw the frame with a platform painter (macOS: fullview_cg), straight to the window:
        the recorded tiles, the live bars and percentages, then menus and toasts. No pixels are
        kept here; returns the frame's [(device box, signature)] so the host can tell what moved."""
        ops, moving = self.build()
        painter.background()
        for box, _, how in ops:
            if not painter.visible(box):
                continue
            if how[0] == "tile":
                _, t, tx, ty, rise = how
                painter.tile(t, tx, ty, rise)
            else:
                self.paint_live(painter.canvas(vr.BG), how[1])
        self.overlay_hits = []
        overlays = self.overlays_showing()
        moving |= self.draw_overlays(painter.surface())
        self.had_overlays = overlays
        self.changed = None
        self.animate(moving)
        return [(box, sig) for box, sig, _ in ops]

    def overlays_showing(self):
        ui = self.ui
        return bool(ui.menu or ui.editor or [t for t in self.toast_list if t[2] > time.monotonic()])

    def compose(self, ops):
        """The page: the backdrop with the tiles and live parts on it. Only what changed since
        the last frame is drawn again: each changed area is rebuilt from the backdrop up, with
        everything that touches it, in the same order, so it comes out pixel for pixel as a
        whole redraw would (tests compare the two). A new size or scroll redraws everything."""
        s = self.scale
        size = (round(self.width * s), round(self.height * s))
        last = self.last_page
        same_layout = (last is not None and last[0] == (size, self.scroll, s)
                       and [o[1][:2] for o in last[1]] == [o[1][:2] for o in ops])
        if not same_layout or not self.incremental:
            self.last_page = None  # let the old page go before making the new one
            page = Image.new("RGB", size, vr.BG)  # the backdrop: one colour
            self.paint_region(page, (0, 0) + page.size, ops)
            self.changed = None  # everything
        else:
            page = last[2]
            dirty = []
            for (old_box, old_sig, _), (box, sig, _) in zip(last[1], ops):
                if old_sig != sig:
                    dirty += [old_box, box]
            self.changed = merge_boxes(dirty, page.size)
            for box in self.changed:
                region = Image.new("RGB", (box[2] - box[0], box[3] - box[1]), vr.BG)
                self.paint_region(region, box, ops)
                page.paste(region, box[:2])
        self.last_page = ((size, self.scroll, s), [(box, sig, None) for box, sig, _ in ops], page)
        return page

    def paint_region(self, region, box, ops):
        """Draw every op that touches `box` (device px) onto `region`, the image of that box."""
        ox, oy, x1, y1 = box
        canvas = vr.Canvas(region, self.scale, vr.BG, origin=(ox, oy))
        for (bx0, by0, bx1, by1), _, how in ops:
            if bx1 <= ox or bx0 >= x1 or by1 <= oy or by0 >= y1:
                continue
            if how[0] == "tile":
                _, t, tx, ty, rise = how
                mask = t.image if rise >= 1 else t.image.getchannel("A").point(lambda v: round(v * rise))
                region.paste(t.image, (tx - ox, ty - oy), mask)
            else:
                self.paint_live(canvas, how[1])

    def live(self, part, x, top, rise):
        """One bar fill or percentage at its animated value: (what to draw or None, still moving)."""
        kind, key, px, py, *rest = part
        motion = self.motion
        spent = rest[-1]
        alpha = rise * (0.72 if spent else 1.0)
        if kind == "bar":
            width, target = rest[0], rest[1]
            if ("bar", key) not in motion.values:
                motion.to(("bar", key), target, 0.5, start=0.0)  # fills from empty when it first shows
            else:
                motion.to(("bar", key), target, 0.5)
            value = motion.get(("bar", key))
            params = None
            if value > 0.3:
                params = ("bar", x + px, top + py, max(6, width * value / 100), vr.level(target) + (round(255 * alpha),))
        else:
            target = rest[0]
            motion.to(("pct", key), target, 0.35)
            value = motion.get(("pct", key))
            params = ("pct", x + px, top + py, f"{value:.0f}%", vr.level(target) + (round(255 * alpha),))
        return params, ("bar", key) in motion.runs or ("pct", key) in motion.runs

    def live_box(self, params):
        """Device box that holds everything a live part draws (generous for the text)."""
        s = self.scale
        if params[0] == "bar":
            _, x, y, w, _ = params
            return (round(x * s) - 1, round(y * s) - 1, round((x + w) * s) + 2, round((y + 6) * s) + 2)
        _, x, y, _, _ = params
        width = widest_percent() * s
        return (math.floor(x * s - width) - 4, math.floor((y - 17) * s) - 2, math.ceil(x * s) + 4, math.ceil((y + 6) * s) + 2)

    def paint_live(self, canvas, params):
        if params[0] == "bar":
            _, x, y, w, color = params
            canvas.rect(x, y, w, 6, 3, color)
        else:
            _, x, y, text, color = params
            canvas.text(x, y, text, 13, color, True, anchor="rs")

    def draw_overlays(self, image):  # image: a Surface (vr.Surface, or a platform's own)
        """Menus, the date editor and toasts, fading in (and toasts out). True while any moves."""
        ui, s, motion = self.ui, self.scale, self.motion
        top = next((y for kind, _, _, y, *_ in self.items if kind == "topbar"), vr.PAD) - self.scroll
        left = next((x for kind, _, x, *_ in self.items if kind == "topbar"), vr.PAD)
        moving = False

        def faded(key, draw):
            t = motion.get(("open", key), 1.0)
            before = image.fade_begin(t) if t < 1 else None
            box, hits = draw(4 * (1 - t))  # rises 4 px as it fades in, like the web menus
            self.overlay_hits.append((box, hits))
            if before is not None:
                image.fade_end(before, box, t)
            return t < 1

        if ui.menu == "settings" and "settings" in ui.anchors:
            ax, aw = ui.anchors["settings"]
            moving |= faded("settings", lambda dy: vr.settings_menu(image, s, self.state, ui, max(8, left + ax + aw - vr.SETTINGS_W),
                                                                    top + 17 + 32 + 6 + dy, self.prefs))
        elif ui.menu == "add" and "add" in ui.anchors:
            ax, aw = ui.anchors["add"]
            moving |= faded("add", lambda dy: vr.add_menu(image, s, ui, left + ax + aw - 260, top + 17 + 32 + 6 + dy))
        if ui.editor:
            anchor = next((h for h in self.page_hits if h[1] == "renew:" + ui.editor["id"]), None)
            if anchor is None:
                ui.editor = None
            else:
                (hx, hy, hw, hh) = anchor[0]
                x = max(12, min(hx + hw - 280, self.width - 292))
                y = hy + hh + 6 - self.scroll
                height = 12 + 28 + 10 + 30 + 22 + len(calendar.Calendar(0).monthdayscalendar(ui.editor["year"], ui.editor["month"])) * 30 + 8 + 34 + 12 + 32 + 12
                if y + height > self.height - 8:  # no room below: open above the date
                    y = max(8, hy - self.scroll - 6 - height)
                moving |= faded("editor", lambda dy: vr.date_editor(image, s, ui, x, y + dy))
        now = time.monotonic()
        self.toast_list = [t for t in self.toast_list if t[2] > now]
        if self.toast_list:
            shown = [(text, kind, min(1.0, (now - born) / 0.2, (gone - now) / 0.2)) for text, kind, gone, born in self.toast_list]
            vr.toasts(image, s, shown, self.width, self.height)
            moving |= any(alpha < 1 for _, _, alpha in shown)
        self.schedule_toasts()
        return moving

    # ---------- hit testing ----------
    def hit_at(self, x, y):
        """(action, cursor, tile key) under a window point (logical px), or Nones."""
        for (bx, by, bw, bh), hits in reversed(self.overlay_hits):
            if bx <= x < bx + bw and by <= y < by + bh:
                for (hx, hy, hw, hh), action, cursor in hits:
                    if hx <= x < hx + hw and hy <= y < hy + hh:
                        return action, cursor, "overlay"
                return None, "arrow", "overlay"
        py = y + self.scroll
        for (hx, hy, hw, hh), action, cursor, key in self.page_hits:
            if hx <= x < hx + hw and hy <= py < hy + hh:
                return action, cursor, key
        return None, "arrow", None

    def card_at(self, x, y):
        py = y + self.scroll
        for kind, key, ix, iy, w, h, data in self.items:
            if kind == "card" and ix <= x < ix + w and iy <= py < iy + h:
                return data["id"]
        return None

    def refresh_hover(self):
        if self.pointer:
            self.mouse_move(*self.pointer)

    # ---------- input ----------
    def mouse_move(self, x, y):
        self.pointer = (x, y)
        action, cursor, where = self.hit_at(x, y)
        card = None if where == "overlay" else self.card_at(x, y)
        self.hover_to(action, card)
        self.host.set_cursor(cursor)

    def hover_to(self, action, card):
        """Hover fades like the web's transitions: .15 s for buttons, .25 s for a card's border."""
        ui, motion = self.ui, self.motion
        if (action, card) == (ui.hover, ui.hover_card):
            return
        for old, new, key, seconds in ((ui.hover, action, lambda a: a, 0.15),
                                       (ui.hover_card, card, lambda c: "card:" + c, 0.25)):
            if old != new:
                if old:
                    motion.to(("h", key(old)), 0.0, seconds, start=motion.get(("h", key(old))))
                if new:
                    motion.to(("h", key(new)), 1.0, seconds, start=motion.get(("h", key(new))))
        ui.hover, ui.hover_card = action, card
        self.host.invalidate()

    def mouse_leave(self):
        self.pointer = None
        self.hover_to(None, None)

    def mouse_down(self, x, y):
        self.pressed = self.hit_at(x, y)[0]

    def mouse_up(self, x, y):
        action, _, where = self.hit_at(x, y)
        pressed, self.pressed = self.pressed, None
        ui = self.ui
        if action is None or action != pressed:
            if where != "overlay":  # a click on nothing closes menus and finishes editing
                changed = bool(ui.menu or ui.editor or ui.confirm or ui.jev_confirm)
                ui.menu = ui.editor = ui.confirm = None
                ui.jev_confirm = False
                if ui.editing:
                    self.commit_name()
                    changed = True
                if changed:
                    self.host.invalidate()
            return
        self.activate(action)
        self.refresh_hover()
        self.host.invalidate()

    def activate(self, action):
        ui, state = self.ui, self.state
        if ui.jev_confirm and action != "jevkey-clear:":
            ui.jev_confirm = False  # anything else cancels the question
        if ui.editing and not action.startswith("name:" + ui.editing[0]):
            self.commit_name()
        kind, _, arg = action.partition(":")
        if kind == "settings":
            ui.menu = None if ui.menu == "settings" else "settings"
            ui.editor = None
            self.motion.to(("open", "settings"), 1.0, 0.18, start=0.0)
        elif kind == "add" and not arg:
            ui.menu = None if ui.menu == "add" else "add"
            ui.editor = None
            self.motion.to(("open", "add"), 1.0, 0.18, start=0.0)
        elif kind == "add":
            ui.menu = None
            self.act("add", {"provider": arg})
        elif kind == "refresh":
            self.motion.to(("spin", "spin"), 1.0, 0.45, start=0.0)
            self.act("reset")
        elif kind == "set":
            if arg in ("autoSwap", "afk"):
                prefs = {"autoSwap": self.prefs.get("autoSwap", bool(state.get("autoSwap"))),
                         "afk": self.prefs.get("afk", bool(state.get("afk")))}
                prefs[arg] = not prefs[arg]
                self.prefs.update(prefs)
                if not self.act("preferences", prefs):
                    self.prefs = {}
            elif arg == "nameMode":
                self.act("names", {"on": not state.get("nameMode")})
            elif arg == "taskbar":
                self.act("taskbar", {"on": not state.get("taskbar")})
            elif arg == "clock24":
                self.act("clock", {"on": not state.get("clock24")})
            elif arg == "resetAlerts":
                self.act("resetAlerts", {"on": not state.get("resetAlerts", True)})
            elif arg == "waitNearReset":
                self.act("waitNearReset", {"on": not state.get("waitNearReset", True)})
            elif arg == "jevCompact":
                self.act("jevCompact", {"on": not state.get("jevCompact")})
            elif arg == "afkSkipLarge":
                self.act("afkSkipLarge", {"on": not state.get("afkSkipLarge", True)})
            elif arg == "perWindow":
                self.act("perWindow", {"on": not state.get("perWindow")})
            elif arg == "launchAtLogin":
                self.act("startup", {"on": not state.get("launchAtLogin")})
        elif kind == "mod":  # Settings: install the Claude Code mod
            self.act("installMod")
        elif kind == "update":  # Settings: check for updates / update now
            self.act("installUpdate" if arg == "install" else "checkUpdate")
        elif kind == "display":
            self.act("taskbar", {"display": arg})
        elif kind == "slot":  # Settings: a display's taskbar slot, cycled to its next choice
            from . import taskbar_layout
            display, _, index = arg.rpartition(":")
            if index in ("0", "1") and display:
                layout = taskbar_layout.layout(state)
                current = (layout.get(display) or [None, None])[int(index)]
                self.act("taskbar", {"layout": taskbar_layout.with_slot(
                    layout, display, int(index), taskbar_layout.next_slot(state, current))})
        elif kind == "swap":
            if ui.pending or state.get("busy"):
                return
            ui.pending = arg
            self.host.set_timer("pending", 8000)
            if not self.act("swap", {"id": arg}):
                ui.pending = None
        elif kind == "openWindow":  # a Claude Code window that keeps this account
            self.act(kind, {"id": arg})
        elif kind == "window":  # the Windows list: pick a window (it's pointed out on screen), or unpick it
            ui.window = None if ui.window == arg else arg
            if ui.window:
                self.act("highlightWindow", {"window": arg})
        elif kind == "swapWindow":  # then an account for it: only that window moves
            if ui.window:
                self.act("swapWindow", {"window": ui.window, "id": arg})
            ui.window = None
        elif kind == "remove":
            ui.confirm = arg
        elif kind == "remove-no":
            ui.confirm = None
        elif kind == "remove-yes":
            ui.confirm = None
            self.act("remove", {"id": arg})
        elif kind == "relogin":
            account = self.account(arg)
            if account:
                self.act("add", {"provider": account["provider"], "id": arg})
        elif kind == "email":
            ui.revealed ^= {arg}
        elif kind == "name":
            if not ui.editing or ui.editing[0] != arg:
                label = (self.account(arg) or {}).get("label") or ""
                ui.editing = (arg, label, len(label), bool(label))
        elif kind == "jevkey-clear":  # Settings: remove the saved OpenRouter key, on the second click
            if ui.jev_confirm:
                ui.jev_confirm = False
                self.act("jevKey", {"key": None})
            else:
                ui.jev_confirm = True
        elif kind == "renew":
            self.open_editor(arg)
        elif kind.startswith("ed-"):
            self.editor_action(kind[3:], arg)

    def account(self, account_id):
        return next((a for a in self.state["accounts"] if a["id"] == account_id), None)

    def act(self, action, body=None):
        try:
            self.controller.action(action, body or {})
            return True
        except (RuntimeError, ValueError) as error:
            self.toast(str(error), "error")
            return False

    # ---------- the renewal date editor ----------
    def open_editor(self, account_id):
        account = self.account(account_id)
        if not account or self.state.get("mode") != "live":
            return
        sub = account.get("subscription") or {}
        at = sub.get("at") or time.time() + 30 * 86400
        t = time.localtime(at)
        self.ui.menu = None
        self.motion.to(("open", "editor"), 1.0, 0.18, start=0.0)
        self.ui.editor = {"id": account_id, "year": t.tm_year, "month": t.tm_mon, "date": (t.tm_year, t.tm_mon, t.tm_mday),
                          "ends": bool(sub.get("ends")), "source": sub.get("source")}

    def editor_action(self, kind, arg):
        ed = self.ui.editor
        if not ed:
            return
        if kind == "kind":
            ed["ends"] = arg == "ends"
        elif kind in ("left", "right"):
            month = ed["month"] + (1 if kind == "right" else -1)
            ed["year"], ed["month"] = ed["year"] + (month - 1) // 12, (month - 1) % 12 + 1
        elif kind == "day":
            ed["date"] = (ed["year"], ed["month"], int(arg))
        elif kind == "save":
            y, m, d = ed["date"]
            at = time.mktime((y, m, d, 12, 0, 0, 0, 0, -1))
            if self.act("subscription", {"id": ed["id"], "at": at, "ends": ed["ends"]}):
                self.ui.editor = None
        elif kind == "clear":
            if self.act("subscription", {"id": ed["id"], "at": None}):
                self.ui.editor = None
        elif kind == "cancel":
            self.ui.editor = None

    # ---------- keyboard (the account name in name mode) ----------
    def commit_name(self):
        editing, self.ui.editing = self.ui.editing, None
        if editing and editing[0] == JEV_KEY:  # an empty field changes nothing: Remove clears the key
            if editing[1].strip():
                self.act("jevKey", {"key": editing[1]})
        elif editing:
            account = self.account(editing[0])
            name = " ".join(editing[1].split())
            if account is not None and name != (account.get("label") or ""):
                self.act("rename", {"id": editing[0], "name": name})
        self.host.invalidate()

    def key(self, name, ctrl=False):
        """Named keys: escape, enter, backspace, delete, left, right, home, end, a (with ctrl), v (with ctrl)."""
        ui = self.ui
        if ui.editing:
            aid, text, caret, everything = ui.editing
            if name == "escape":
                ui.editing = None
            elif name == "enter":
                self.commit_name()
                return
            elif ctrl and name == "a":
                ui.editing = (aid, text, len(text), True)
            elif ctrl and name == "v":
                self.char(self.host.clipboard())
                return
            elif name in ("backspace", "delete"):
                if everything:
                    text, caret = "", 0
                elif name == "backspace" and caret > 0:
                    text, caret = text[:caret - 1] + text[caret:], caret - 1
                elif name == "delete":
                    text = text[:caret] + text[caret + 1:]
                ui.editing = (aid, text, caret, False)
            elif name in ("left", "right", "home", "end"):
                caret = {"left": max(0, caret - 1) if not everything else 0, "right": min(len(text), caret + 1),
                         "home": 0, "end": len(text)}[name]
                ui.editing = (aid, text, caret, False)
            else:
                return
            self.host.invalidate()
            return
        if name == "escape" and (ui.menu or ui.editor or ui.confirm):
            ui.menu = ui.editor = ui.confirm = None
            ui.jev_confirm = False
            self.host.invalidate()

    def char(self, value):
        ui = self.ui
        if not ui.editing or not value:
            return
        value = "".join(ch for ch in value if ch.isprintable()).replace("\n", " ")
        if ui.editing[0] == JEV_KEY:
            value = "".join(value.split())  # a key has no spaces: pasted with a stray newline or space
        aid, text, caret, everything = ui.editing
        if everything:
            text, caret = "", 0
        text = (text[:caret] + value + text[caret:])[:300 if aid == JEV_KEY else 40]
        ui.editing = (aid, text, min(len(text), caret + len(value)), False)
        self.host.invalidate()

    def close(self):
        """The window closed: finish an edit, drop every cached tile."""
        if self.ui.editing:
            self.commit_name()
        self.tiles.clear()
        self.last_page = None
        vr.release()
