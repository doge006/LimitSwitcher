"""Draws the tray flyout and its right-click menu as images plus clickable regions.

Pure Pillow; no windowing. Shapes are drawn at 2x on an opaque canvas and downsampled
(anti-aliased edges, correct blending); text is drawn at final size by FreeType.
Everything is in logical pixels times `scale`.
"""
from collections import OrderedDict
import ctypes
import difflib
from functools import lru_cache
import math
import os
from pathlib import Path
import sys
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ASSETS = Path(__file__).with_name("static") / "assets"
PROVIDERS = (("claude", "Claude"), ("codex", "Codex"))
SS = 2  # supersampling factor for shapes

WIDTH = 404         # panel width
COMPACT_WIDTH = 320  # compact panel width
MENU_WIDTH = 232
MARGIN = 18         # transparent margin that holds the shadow
RADIUS = 8
ROW_H = 64
BG = (32, 32, 32, 255)
FOOTER = (27, 27, 27, 255)
BORDER = (255, 255, 255, 22)
TEXT = (243, 243, 243, 255)
MUTED = (163, 163, 163, 255)
FAINT = (140, 140, 140, 255)
TRACK = (255, 255, 255, 26)
HOVER = (255, 255, 255, 13)
ACTIVE = (255, 255, 255, 8)
PILL = (255, 255, 255, 16)
PILL_HOVER = (255, 255, 255, 28)
GOOD, WARN, BAD = (76, 195, 138, 255), (229, 181, 74, 255), (239, 106, 91, 255)
ACCENT = {"claude": (224, 138, 104, 255), "codex": (162, 149, 247, 255)}


# ---------- data helpers ----------
def remaining(used):
    """What's left, rounded down: never more room than there is (93.4% used shows 6% left)."""
    return max(0, min(100, math.floor(100 - used + 1e-6)))


def level_rgb(left):
    return GOOD if left > 30 else WARN if left > 10 else BAD


def display_name(account):
    return account.get("name") or account.get("email") or account["alias"]


def short_label(window):
    if window["key"] == "five_hour":
        return "5h"
    if window["key"] == "weekly":
        return "1w"
    if window["key"] == "monthly":
        return "30d"
    return window["label"].split(" · ")[-1]


def until(ts):
    m = max(0, int((ts - time.time()) / 60))
    if m >= 1440:
        return f"{m // 1440}d {m % 1440 // 60}h"
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def age_text(account):
    """How old the account's numbers are ("now", "12m ago", "3h ago"), or None before any."""
    updated = account.get("updated_at") or 0
    if not updated:
        return None
    minutes = int((time.time() - updated) // 60)
    return "now" if minutes < 1 else f"{minutes}m ago" if minutes < 60 else f"{minutes // 60}h ago"


def age_room(size):
    """The width kept for the age at any value, so nothing next to it moves as "now" becomes
    "1m ago", "12m ago"..."""
    return max(text_w(sample, size) for sample in ("now", "00m ago", "00h ago"))


def stale(account):
    """Numbers 30 minutes old or more: their age is shown as a warning."""
    updated = account.get("updated_at") or 0
    return bool(updated) and time.time() - updated >= 1800


def status_note(account):
    """Short warning when usage could not be fetched or is old, else None."""
    status = account.get("status") or ""
    if not status:
        return age_text(account) if stale(account) else None
    if "sign in" in status.lower() or "missing" in status.lower():
        return "Sign in again"
    return "Retrying"  # rate limited or the service is down: last numbers shown, checked again soon


# ---------- fonts & images ----------
@lru_cache(maxsize=1)
def _font_files():
    """(regular, semibold) font files: Segoe UI on Windows, San Francisco on macOS, the desktop's
    own sans-serif on Linux (fontconfig), DejaVu as the last resort."""
    if sys.platform == "win32":
        fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        return fonts / "segoeui.ttf", fonts / "seguisb.ttf"
    if sys.platform == "darwin":
        for name in ("SFNS.ttf", "SFNSText.ttf"):
            path = Path("/System/Library/Fonts") / name
            if path.exists():
                return path, path  # a variable font: its Semibold instance is picked in font()
        helvetica = Path("/System/Library/Fonts/HelveticaNeue.ttc")
        return helvetica, helvetica
    dejavu = Path("/usr/share/fonts/truetype/dejavu")
    found = []
    for pattern in ("sans-serif", "sans-serif:weight=200"):  # 200: fontconfig's semibold
        try:
            import subprocess
            path = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True,
                                  timeout=3).stdout.strip()
            found.append(Path(path) if path and Path(path).exists() else None)
        except (OSError, subprocess.SubprocessError):
            found.append(None)
    return found[0] or dejavu / "DejaVuSans.ttf", found[1] or dejavu / "DejaVuSans-Bold.ttf"


@lru_cache(maxsize=64)
def font(size, bold, scale):
    regular, semibold = _font_files()
    path = semibold if bold else regular
    try:
        if path.suffix == ".ttc" and bold:
            face = ImageFont.truetype(str(path), round(size * scale), index=1)  # Helvetica Neue Bold... close enough
        else:
            face = ImageFont.truetype(str(path), round(size * scale))
        if regular == semibold and path.suffix == ".ttf":
            try:  # one variable font (San Francisco): pick the weight
                face.set_variation_by_name("Semibold" if bold else "Regular")
            except (OSError, ValueError, AttributeError):
                pass
        return face
    except OSError:
        return ImageFont.load_default(round(size * scale))


_glyphs = OrderedDict()   # (font, text, anchor, sub-pixel start) -> (mask, offset)
_GLYPHS_MAX = 400
_fast_text = True


def draw_text(draw, xy, value, size, bold, scale, fill, anchor):
    """draw.text, with the rasterised text kept: the same label at the same sub-pixel position
    is drawn from its cached mask instead of by FreeType again. Pixel for pixel what draw.text
    draws (it is the same getmask2 and draw_bitmap that draw.text uses; tests compare them)."""
    global _fast_text
    face = font(size, bold, scale)
    if _fast_text:
        try:
            ink = draw._getink(fill)[0]
            x, y = xy
            start = (math.modf(x)[0], math.modf(y)[0])
            key = (size, bold, scale, value, anchor, start, draw.fontmode)
            hit = _glyphs.get(key)
            if hit is None:
                hit = _glyphs[key] = face.getmask2(value, draw.fontmode, None, None, None, 0, anchor, ink, start)
                if len(_glyphs) > _GLYPHS_MAX:
                    _glyphs.popitem(last=False)
            else:
                _glyphs.move_to_end(key)
            mask, offset = hit
            draw.draw.draw_bitmap((int(x) + offset[0], int(y) + offset[1]), mask, ink)
            return
        except (AttributeError, TypeError, ValueError):  # another Pillow: its own path, from now on
            _fast_text = False
    draw.text(xy, value, font=face, fill=fill, anchor=anchor)


_masks = OrderedDict()   # a label's coverage mask, by text and sub-pixel position


def text_mask(x, y, value, size, bold, scale, anchor):
    """(mask, left, top): a label's coverage at device position (x, y), as the taskbar blocks
    draw it. The mask only depends on the text and the position's fraction, so it is kept and
    moved to whole pixels (the same pixels as making it again; tests compare them)."""
    ix, iy = math.floor(x), math.floor(y)
    key = (value, size, bold, scale, anchor, x - ix, y - iy)
    hit = _masks.get(key)
    if hit is None:
        f = font(size, bold, scale)
        fx, fy = x - ix + 64, y - iy + 64  # made away from 0, where int() and floor() agree
        box = _probe.textbbox((fx, fy), value, font=f, anchor=anchor)
        left, top = int(box[0]) - 1, int(box[1]) - 1
        mask = Image.new("L", (int(box[2]) + 2 - left, int(box[3]) + 2 - top), 0)
        ImageDraw.Draw(mask).text((fx - left, fy - top), value, font=f, fill=255, anchor=anchor)
        hit = _masks[key] = (mask, left - 64, top - 64)
        if len(_masks) > 200:
            _masks.popitem(last=False)
    else:
        _masks.move_to_end(key)
    mask, left, top = hit
    return mask, left + ix, top + iy


_probe = ImageDraw.Draw(Image.new("L", (1, 1)))


@lru_cache(maxsize=1024)
def text_w(value, size, bold=False):
    return font(size, bold, 1).getlength(value)


@lru_cache(maxsize=16)
def asset(name, px):
    # Pillow resizes RGBA through premultiplied RGBa; the source is kept in RGBa already, so
    # the steps (and pixels) are Pillow's own without a full-size conversion per size.
    return asset_source(name).resize((px, px), Image.Resampling.LANCZOS).convert("RGBA")


@lru_cache(maxsize=4)
def asset_source(name):
    """The full-size logo, decoded once (not once per size: the Claude logo is 937 px, several MB
    per decode). Dropped again by the memory cleanup once windows close (memory.trim)."""
    with Image.open(ASSETS / f"{name}.png") as source:
        return source.convert("RGBA").convert("RGBa")


# ---------- layout ----------
class Layout:
    """Collects shapes, text, images and hit regions in logical pixels."""

    def __init__(self):
        self.shapes, self.texts, self.images, self.hits = [], [], [], []

    def rect(self, x, y, w, h, r, fill):
        self.shapes.append(("rect", x, y, w, h, r, fill))

    def dot(self, cx, cy, r, fill):
        self.shapes.append(("ellipse", cx - r, cy - r, cx + r, cy + r, fill))

    def icon(self, kind, cx, cy, r, fill):
        self.shapes.append((kind, cx, cy, r, fill))

    def text(self, x, y, value, size, fill, bold=False, anchor="lm"):
        self.texts.append((x, y, value, size, bold, fill, anchor))

    def image(self, x, y, name, size):
        self.images.append((x, y, name, size))

    def hit(self, x, y, w, h, action):
        self.hits.append(((x, y, w, h), action))


def fit(value, size, bold, width):
    if text_w(value, size, bold) <= width:
        return value
    while value and text_w(value + "…", size, bold) > width:
        value = value[:-1]
    return value + "…"


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(4))


def fade(color, t):
    return color[:3] + (round(color[3] * max(0.0, min(1.0, t))),)


def switch(layout, x, cy, pos, hover):
    """Toggle whose knob position (0..1) can be animated."""
    off_track = (255, 255, 255, 40 + round(20 * hover))
    layout.rect(x, cy - 8, 34, 16, 8, mix(off_track, GOOD, pos))
    if pos < 1:
        layout.rect(x + 1, cy - 7, 32, 14, 7, fade(FOOTER, 1 - pos))
    knob = 10 + hover
    layout.dot(x + 8 + 18 * pos, cy, knob / 2, mix(MUTED, (20, 20, 20, 255), pos))


def icon_button(layout, x, y, size, kind, action, hover_amount, active=False, r=6.5):
    t = max(hover_amount, 0.6 if active else 0.0)
    if t > 0:
        layout.rect(x, y, size, size, 6 if size > 24 else 5, fade(PILL_HOVER, t))
    layout.icon(kind, x + size / 2, y + size / 2, r, mix(MUTED, TEXT, t))
    layout.hit(x, y, size, size, action)


def days_text(ts):
    seconds = ts - time.time()
    if seconds <= 0:
        return None
    days = int(seconds // 86400)
    return f"{days}d" if days >= 1 else f"{int(seconds // 3600)}h"


ENDS_SOON = 7 * 86400  # a cancelled subscription shows in yellow from this close to its end


def ends_soon(sub):
    """A cancelled subscription that ends within ENDS_SOON (the yellow one)."""
    sub = sub or {}
    return bool(sub.get("ends")) and bool(sub.get("at")) and sub["at"] - time.time() <= ENDS_SOON


def subscription_text(account):
    """("Renews 12d" | "Ends 3d", color) or (None, None)."""
    sub = account.get("subscription") or {}
    when = days_text(sub["at"]) if sub.get("at") else None
    if not when:
        return None, None
    when = ("~" if sub.get("estimated") else "") + when
    return (f"Ends {when}", WARN if ends_soon(sub) else MUTED) if sub.get("ends") else (f"Renews {when}", MUTED)


def targets(state, hover=None):
    """Resting values of everything that animates, derived from state + hover."""
    fx = {("toggle", p): 1.0 if state.get(p, True) else 0.0 for p in ("autoSwap", "afk", "taskbar")}
    for a in state["accounts"]:
        fx[("active", a["id"])] = 1.0 if a["active"] else 0.0
        for w in a["windows"][:3]:
            fx[("bar", a["id"], w["key"])] = remaining(w["used"])
    if hover:
        fx[("hover", hover)] = 1.0
    return fx


DIM = 0.45


def dim_row(layout, marks):
    """Fade everything drawn for one row (limit reached)."""
    shapes, texts, _ = marks
    for i in range(shapes, len(layout.shapes)):
        op = list(layout.shapes[i])
        op[-1] = fade(op[-1], DIM)
        layout.shapes[i] = tuple(op)
    for i in range(texts, len(layout.texts)):
        x, y, value, size, bold, fill, anchor = layout.texts[i]
        layout.texts[i] = (x, y, value, size, bold, fade(fill, DIM), anchor)


def build(state, hover=None, pending=None, pinned=False, fx=None, armed=None, compact=None, only=None):
    """Lay out the flyout. Returns (layout, panel_height). Coordinates exclude MARGIN.

    fx holds in-between animation values (see targets()); missing keys use resting values.
    only: a provider; the panel opened from its taskbar block lists just that provider's accounts.
    """
    L, W = Layout(), WIDTH
    busy = state.get("busy")
    rest = targets(state, hover)
    fx = {**rest, **(fx or {})}
    h = lambda action: fx.get(("hover", action), 0.0)
    compact = state.get("compact") if compact is None else compact  # only the accounts in use
    if only:  # opened from a taskbar block, to switch: always the full list for that provider
        compact = False
        state = dict(state, accounts=[a for a in state["accounts"] if a["provider"] == only])

    # Header: mark, title, pop-out, "Full view" (none when compact).
    if compact:
        return _build_compact(L, COMPACT_WIDTH, state, fx, h, pinned, armed)
    L.image(16, 16, "switcher", 22)
    L.text(46, 27, "LimitSwitcher", 14, TEXT, bold=True)
    pill_w = 92
    pill_x = W - 16 - pill_w
    L.rect(pill_x, 13, pill_w, 28, 6, mix(PILL, PILL_HOVER, h("full")))
    L.text(pill_x + 13, 27, "Full view", 12, TEXT)
    L.text(W - 16 - 13 + 2 * h("full"), 26, "›", 16, TEXT, anchor="rm")
    L.hit(pill_x, 13, pill_w, 28, "full")
    icon_button(L, pill_x - 34, 13, 28, "popin" if pinned else "popout", "pin", h("pin"), active=pinned)
    if pinned:  # a popped-out panel stays open: hide it without quitting
        icon_button(L, pill_x - 66, 13, 28, "minimize", "hide", h("hide"))
    y = 54

    accounts_all = state["accounts"]
    if not accounts_all:
        y += 10
        L.text(W / 2, y + 12, "No accounts yet", 13, TEXT, bold=True, anchor="mm")
        L.text(W / 2, y + 34, "Sign in to Claude Code or Codex, or add one here.", 11, MUTED, anchor="mm")
        y += 54
        for i, (provider, title) in enumerate(PROVIDERS):
            bx = W / 2 - 124 + i * 128
            action = "add:" + provider
            L.rect(bx, y, 120, 30, 6, mix(PILL, PILL_HOVER, h(action)))
            L.image(bx + 12, y + 8, provider, 14)
            L.text(bx + 32, y + 15, f"Add {title}", 12, TEXT)
            L.hit(bx, y, 120, 30, action)
        y += 44

    for provider, title in PROVIDERS:
        accounts = [a for a in accounts_all if a["provider"] == provider]
        if not accounts:
            continue
        y += 6
        L.image(16, y + 4, provider, 14)
        L.text(37, y + 11, title.upper(), 11, ACCENT[provider], bold=True)
        L.text(37 + text_w(title.upper(), 11, True) + 7, y + 11, str(len(accounts)), 11, FAINT)
        y += 24
        for account in accounts:
            key = "swap:" + account["id"]
            top = y
            marks = (len(L.shapes), len(L.texts), len(L.images))
            switchable = account["eligible"] and not account["active"] and not busy and not pending
            act = fx.get(("active", account["id"]), 0.0)
            confirming = switchable and armed == account["id"]
            hov = max(h(key), 1.0 if confirming else 0.0) if switchable else 0.0
            if act > 0 or hov > 0:
                L.rect(8, top, W - 16, ROW_H, 6, (255, 255, 255, round(ACTIVE[3] * act + HOVER[3] * hov)))
            if act > 0.01:  # green marker grows from the centre as the account becomes active
                bar_h = (ROW_H - 26) * act
                L.rect(10, top + ROW_H / 2 - bar_h / 2, 3, bar_h, 1.5, fade(GOOD, act))

            # Line 1, right to left: status, subscription; the name takes the rest.
            cy = top + 16
            right = W - 22
            if pending == account["id"]:
                status, color = "Switching…", TEXT
            elif account["active"]:
                status, color = "In use", fade(GOOD, max(act, 0.35))
            elif not account["eligible"]:
                status, color = "Limit", BAD
            elif confirming:
                status, color = "Click again", GOOD
            elif switchable and hov > 0:
                status, color = "Switch", fade(TEXT, hov)
            else:
                status, color = "", FAINT
            if status:
                L.text(right, cy, status, 11, color, bold=True, anchor="rm")
                right -= text_w(status, 11, True)
                if account["active"] and pending != account["id"]:
                    L.dot(right - 6, cy, 3, fade(GOOD, max(act, 0.35)))
                    right -= 10
                right -= 12
            note = status_note(account)
            sub_text, sub_color = subscription_text(account)
            age = None if note else age_text(account)
            if note:
                L.text(right, cy, note, 11, WARN, anchor="rm")
                if note == "Sign in again":  # clickable: the app's own sign-in, other logins untouched
                    L.hit(right - text_w(note, 11) - 4, cy - 10, text_w(note, 11) + 8, 20, "relogin:" + account["id"])
                right -= text_w(note, 11) + 12
            if age:  # how old the numbers are, on every account
                L.text(right, cy, age, 11, FAINT, anchor="rm")
                right -= age_room(11) + 12
            if not note and sub_text:
                L.text(right, cy, sub_text, 11, sub_color, anchor="rm")
                right -= text_w(sub_text, 11) + 12
            resets = (account.get("credits") or {}).get("resets")
            if resets:
                label = f"{resets} reset" + ("s" if resets != 1 else "")
                L.text(right, cy, label, 11, ACCENT[provider], anchor="rm")
                right -= text_w(label, 11) + 12
            chip = account.get("plan") or ""
            chip_w = text_w(chip, 10, True) + 12 if chip else 0
            name = fit(display_name(account), 13, True, right - 22 - (chip_w + 8 if chip else 0))
            L.text(22, cy, name, 13, TEXT, bold=True)
            if chip:
                cx = 22 + text_w(name, 13, True) + 8
                L.rect(cx, top + 8, chip_w, 16, 4, ACCENT[provider][:3] + (38,))
                L.text(cx + 6, cy, chip, 10, ACCENT[provider], bold=True)

            # Lines 2-3: up to three compact meters, each with its reset timer underneath.
            windows = account["windows"][:3]
            if not windows:
                L.text(22, top + 38, "Usage not loaded yet" if not account.get("status") else account["status"], 11, FAINT)
            else:
                col_w = (W - 44 + 14) / len(windows)
                for i, window in enumerate(windows):
                    left = fx.get(("bar", account["id"], window["key"]), remaining(window["used"]))
                    shown = remaining(window["used"])
                    x0 = 22 + i * col_w
                    ly = top + 36
                    label = short_label(window)
                    label_w = max(20, text_w(label, 11) + 7)
                    L.text(x0, ly, label, 11, MUTED)
                    bar_x, bar_w = x0 + label_w, col_w - label_w - 52
                    L.rect(bar_x, ly - 2, bar_w, 4, 2, TRACK)
                    if left > 0.5:
                        L.rect(bar_x, ly - 2, max(4, bar_w * left / 100), 4, 2, level_rgb(left))
                    L.text(x0 + col_w - 14, ly, f"{shown:.0f}%", 11, level_rgb(shown), bold=True, anchor="rm")
                    right_edge = x0 + col_w - 14
                    L.text(right_edge, ly + 14, "left", 10, FAINT, anchor="rm")  # under the %
                    if window.get("resetsAt"):
                        room = right_edge - text_w("left", 10) - 6 - bar_x
                        full = "resets in " + until(window["resetsAt"])
                        if text_w(full, 10) <= room:
                            L.text(bar_x, ly + 14, full, 10, FAINT)
                        else:  # three meters: a small clock and the time
                            L.icon("clock", bar_x + 4, ly + 14, 3.6, FAINT)
                            L.text(bar_x + 11, ly + 14, until(window["resetsAt"]), 10, FAINT)
            if switchable:
                L.hit(8, top, W - 16, ROW_H, key)
            if not account["eligible"]:
                dim_row(L, marks)  # greyed out, like the full view
            y += ROW_H + 2

    # Footer: switches and quit, PowerToys-style strip.
    y += 8
    footer_h = 48
    L.rect(0, y, W, footer_h, 0, FOOTER)
    L.rect(0, y, W, 1, 0, BORDER)
    cy = y + footer_h / 2
    x = 16
    prefs = [("autoSwap", "Auto swap"), ("afk", "Auto resume")]
    if state.get("taskbarAvailable"):
        prefs.append(("taskbar", "Taskbar"))
    room = W - 16 - 64 - 12 - x if not only else W - 16 - 30 - 12 - x
    if sum(42 + text_w(label, 12) + 18 for _, label in prefs) > room:  # short names when the full ones don't fit
        prefs = [(pref, {"Auto swap": "Swap", "Auto resume": "Resume"}.get(label, label)) for pref, label in prefs]
    if armed == "quit":  # the first click on the power button asks for a second
        L.text(x, cy, "Click again to quit", 12, BAD, bold=True)
        prefs = []
    for pref, label in prefs:
        action = "toggle:" + pref
        locked = busy and pref != "taskbar"
        switch(L, x, cy, fx[("toggle", pref)], h(action))
        L.text(x + 42, cy, label, 12, TEXT if not locked else MUTED)
        width = 42 + text_w(label, 12) + (14 if len(prefs) > 2 else 18)
        if not locked:
            L.hit(x - 4, cy - 14, width, 28, action)
        x += width + 4
    if not only:
        icon_button(L, W - 16 - 64, cy - 15, 30, "compact", "compact", h("compact"))
    quit_button(L, W - 16 - 30, cy - 15, 30, h("quit"), armed == "quit")
    return L, y + footer_h


def quit_button(L, x, y, size, hover, armed, r=6.5):
    """The power button; red once clicked, until the second click quits (or it times out)."""
    if armed:
        L.rect(x, y, size, size, 6 if size > 24 else 5, BAD[:3] + (60,))
        L.icon("power", x + size / 2, y + size / 2, r, BAD)
        L.hit(x, y, size, size, "quit")
    else:
        icon_button(L, x, y, size, "power", "quit", hover, r=r)


def _build_compact(L, W, state, fx, h, pinned, armed=None):
    """Compact panel: the account in use for each provider, small but readable. It is always
    popped out (it stays open and can be dragged), so its buttons are quit, expand and hide, at
    the top right in line with the first account. When an account swaps, the old row slides out
    to the left while the new one slides in from the right."""
    y = 6
    bx = W - 8
    if armed == "quit":  # the first click on the power button: "Quit?" where expand and hide were
        bx -= 24
        quit_button(L, bx, y, 24, h("quit"), True, r=5.5)
        L.text(bx - 6, y + 12, "Quit?", 11, BAD, bold=True, anchor="rm")
        L.hit(bx - 6 - text_w("Quit?", 11, True), y, text_w("Quit?", 11, True) + 6, 24, "quit")
        bx -= 48
    else:
        for kind, action in (("power", "quit"), ("expand", "expand"), ("minimize", "hide")):
            bx -= 24
            if action == "quit":
                quit_button(L, bx, y, 24, h(action), False, r=5.5)
            else:
                icon_button(L, bx, y, 24, kind, action, h(action), r=5.5)
    slots = []
    for provider, _ in PROVIDERS:
        rows = [a for a in state["accounts"] if a["provider"] == provider
                and (a["active"] or fx.get(("active", a["id"]), 0.0) > 0.01)]
        if rows:
            slots.append(rows)
    if not slots:
        L.text(12, y + 12, "No account in use", 12, MUTED)
        return L, y + 32
    for index, rows in enumerate(slots):
        for account in rows:
            # Swapping, one after the other: the old row slides out to the left and fades in the
            # first half, then the new one slides in from the right and fades in.
            t = fx.get(("active", account["id"]), 1.0 if account["active"] else 0.0)
            alpha = max(0.0, 2 * t - 1)
            if alpha <= 0.01:
                continue
            marks = (len(L.shapes), len(L.texts), len(L.images), len(L.hits))
            _compact_row(L, W, account, fx, y, (bx - 6) if index == 0 else W - 12)
            if t < 0.999:
                dx = min(1.0, 2 * (1 - t)) * 36 * (1 if account["active"] else -1)
                move_row(L, marks, dx, alpha, clickable=account["active"])
        y += 52
    return L, y


def _compact_row(L, W, account, fx, top, right):
    provider = account["provider"]
    cy = top + 12
    L.image(12, cy - 7, provider, 14)
    note = status_note(account)
    if note:
        L.text(right, cy, note, 11, WARN, anchor="rm")
        if note == "Sign in again":
            L.hit(right - text_w(note, 11) - 4, cy - 10, text_w(note, 11) + 8, 20, "relogin:" + account["id"])
        right -= text_w(note, 11) + 8
    elif age_text(account):
        L.text(right, cy, age_text(account), 11, FAINT, anchor="rm")
        right -= age_room(11) + 8
    L.text(32, cy, fit(display_name(account), 13, True, right - 32), 13, TEXT, bold=True)
    windows = account["windows"][:3]
    ly = top + 31
    if not windows:
        L.text(12, ly, account.get("status") or "Usage not loaded yet", 11, FAINT)
        return
    col_w = (W - 24 + 10) / len(windows)
    for i, window in enumerate(windows):
        left = fx.get(("bar", account["id"], window["key"]), remaining(window["used"]))
        shown_left = remaining(window["used"])
        x0 = 12 + i * col_w
        label = short_label(window)
        label_w = max(17, text_w(label, 11) + 6)
        L.text(x0, ly, label, 11, MUTED)
        pct_right = x0 + col_w - 10
        bar_x, bar_w = x0 + label_w, col_w - label_w - 46
        L.rect(bar_x, ly - 2, bar_w, 4, 2, TRACK)
        if left > 0.5:
            L.rect(bar_x, ly - 2, max(4, bar_w * left / 100), 4, 2, level_rgb(left))
        L.text(pct_right, ly, f"{shown_left:.0f}%", 11, level_rgb(shown_left), bold=True, anchor="rm")
        L.text(pct_right, ly + 13, "left", 10, FAINT, anchor="rm")
        if window.get("resetsAt"):
            room = pct_right - text_w("left", 10) - 5 - bar_x
            full = "resets in " + until(window["resetsAt"])
            if text_w(full, 10) <= room:
                L.text(bar_x, ly + 13, full, 10, FAINT)
            elif text_w(until(window["resetsAt"]), 10) + 11 <= room:  # a clock and the time
                L.icon("clock", bar_x + 4, ly + 13, 3.5, FAINT)
                L.text(bar_x + 11, ly + 13, until(window["resetsAt"]), 10, FAINT)
            # no room at all: the reset time is in the full panel and the full view


def move_row(layout, marks, dx, alpha, clickable=True):
    """Shift everything drawn since marks sideways by dx and fade it to alpha (swap slide)."""
    shapes, texts, images, hits = marks
    for i in range(shapes, len(layout.shapes)):
        op = list(layout.shapes[i])
        if op[0] == "ellipse":
            op[1] += dx
            op[3] += dx
        else:
            op[1] += dx
        op[-1] = fade(op[-1], alpha)
        layout.shapes[i] = tuple(op)
    for i in range(texts, len(layout.texts)):
        x, y, value, size, bold, fill, anchor = layout.texts[i]
        layout.texts[i] = (x + dx, y, value, size, bold, fade(fill, alpha), anchor)
    for i in range(images, len(layout.images)):
        x, y, name, size = layout.images[i][:4]
        layout.images[i] = (x + dx, y, name, size, alpha)
    if not clickable:
        del layout.hits[hits:]
    else:
        layout.hits[hits:] = [((x + dx, y, w, h), action) for (x, y, w, h), action in layout.hits[hits:]]


def build_menu(items, hover=None, fx=None):
    """Right-click menu. items: dicts {action, label, checked?, bold?, enabled?} or "-"."""
    L, W = Layout(), MENU_WIDTH
    fx = fx if fx is not None else ({("hover", hover): 1.0} if hover else {})
    y = 5
    for item in items:
        if item == "-":
            L.rect(10, y + 4, W - 20, 1, 0, BORDER)
            y += 9
            continue
        enabled = item.get("enabled", True)
        amount = fx.get(("hover", item["action"]), 0.0)
        if amount > 0 and enabled:
            L.rect(5, y, W - 10, 32, 5, fade((255, 255, 255, 16), amount))
        if item.get("checked"):
            L.icon("check", 20, y + 16, 5, TEXT if enabled else FAINT)
        L.text(36, y + 16, item["label"], 12, TEXT if enabled else FAINT, bold=item.get("bold", False))
        if enabled:
            L.hit(5, y, W - 10, 32, item["action"])
        y += 32
    return L, y + 5


@lru_cache(maxsize=8)
def frame(width, height, scale):
    """Rounded panel mask and its soft shadow; depend only on size, so cached."""
    M, big = MARGIN, scale * SS
    full = (round((width + 2 * M) * scale), round((height + 2 * M) * scale))
    mask = Image.new("L", (round((width + 2 * M) * big), round((height + 2 * M) * big)), 0)
    ImageDraw.Draw(mask).rounded_rectangle((M * big, M * big, (M + width) * big - 1, (M + height) * big - 1), RADIUS * big, fill=255)
    mask = mask.resize(full, Image.Resampling.LANCZOS)
    shadow = Image.new("RGBA", full, (0, 0, 0, 0))
    shadow.putalpha(mask.point(lambda v: v * 110 // 255).filter(ImageFilter.GaussianBlur(12 * scale)))
    shadow = shadow.transform(full, Image.AFFINE, (1, 0, 0, 0, 1, -4 * scale))  # drop 4px
    return mask, shadow


def paint(layout, width, height, scale):
    """Rasterise a layout. Returns (RGBA image incl. shadow margin, hits in logical px incl. margin)."""
    M = MARGIN
    full = (round((width + 2 * M) * scale), round((height + 2 * M) * scale))
    mask, shadow = frame(width, height, scale)
    # Exactly SS times the panel, so every device pixel is the mean of its own SS x SS block
    # (at 125% an odd height used to be one row short, resized by a factor just under SS).
    canvas = Image.new("RGB", (full[0] * SS, full[1] * SS), BG[:3])
    _draw_shapes(ImageDraw.Draw(canvas, "RGBA"), layout.shapes, width, height, scale)
    panel = canvas.reduce(SS)
    # Images and text go on the opaque panel (so translucent text blends), then the
    # rounded shape is cut and the result laid over the cached shadow.
    _draw_top(panel, layout.images, layout.texts, scale)
    panel = panel.convert("RGBA")
    panel.putalpha(mask)
    image = shadow.copy()
    # Only the rounded edge blends with the shadow; inside it the result is the panel's own pixel.
    # So blend the four edge strips and copy the middle straight across (tests check the result
    # is identical to blending all of it).
    edge = math.ceil((M + RADIUS) * scale) + 2
    fw, fh = image.size
    if fw > 2 * edge and fh > 2 * edge:
        for box in ((0, 0, fw, edge), (0, fh - edge, fw, fh), (0, edge, edge, fh - edge), (fw - edge, edge, fw, fh - edge)):
            image.alpha_composite(panel, dest=box[:2], source=box)
        middle = (edge, edge, fw - edge, fh - edge)
        image.paste(panel.crop(middle), middle[:2])
    else:
        image.alpha_composite(panel)
    return image, _hits(layout)


def _hits(layout):
    return [((MARGIN + x, MARGIN + y, w, h), action) for (x, y, w, h), action in layout.hits]


def _draw_shapes(d, shapes, width, height, scale):
    """The shapes and the panel's border, at SS times the size (d draws on the big canvas)."""
    M, big = MARGIN, scale * SS

    def P(v):
        return v * big

    line = max(1, round(P(1.4)))
    for op in shapes:
        kind = op[0]
        if kind == "rect":
            _, x, y, w, h, r, fill = op
            if w <= 0 or h <= 0:
                continue  # an animated bar at (or eased just past) zero size: nothing to draw
            box = (P(M + x), P(M + y), P(M + x + w) - 1, P(M + y + h) - 1)
            if box[2] < box[0] or box[3] < box[1]:
                continue
            if r:
                d.rounded_rectangle(box, P(r), fill=fill)
            else:
                d.rectangle(box, fill=fill)
        elif kind == "ellipse":
            _, x1, y1, x2, y2, fill = op
            if x2 < x1 or y2 < y1:
                continue
            d.ellipse((P(M + x1), P(M + y1), P(M + x2), P(M + y2)), fill=fill)
        else:
            _, cx, cy, r, fill = op
            cx, cy, r = P(M + cx), P(M + cy), P(r)
            if kind == "power":
                d.arc((cx - r, cy - r + P(1), cx + r, cy + r + P(1)), 300, 240, fill=fill, width=line)
                d.line((cx, cy - r - P(1), cx, cy + P(1)), fill=fill, width=line)
            elif kind in ("compact", "expand"):
                # Four corners: pointing out (expand) or in (compact).
                arm = r * .55
                for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
                    if kind == "expand":
                        px, py = cx + sx * r, cy + sy * r
                        d.line((px - sx * arm, py, px, py, px, py - sy * arm), fill=fill, width=line, joint="curve")
                    else:
                        px, py = cx + sx * r * .35, cy + sy * r * .35
                        d.line((px + sx * arm, py, px, py, px, py + sy * arm), fill=fill, width=line, joint="curve")
            elif kind == "clock":
                d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=fill, width=line)
                d.line((cx, cy - r * .55, cx, cy, cx + r * .45, cy + r * .3), fill=fill, width=line, joint="curve")
            elif kind == "check":
                d.line((cx - r, cy, cx - r * .3, cy + r * .7, cx + r, cy - r * .7), fill=fill, width=line, joint="curve")
            elif kind == "minimize":
                d.line((cx - r * .75, cy + r * .45, cx + r * .75, cy + r * .45), fill=fill, width=line)
            elif kind in ("popout", "popin"):
                # A window with an arrow leaving it (pop out) or entering it (pop in).
                d.rounded_rectangle((cx - r, cy - r * .6, cx + r * .6, cy + r), P(1.5), outline=fill, width=line)
                if kind == "popout":
                    d.line((cx - r * .1, cy + r * .1, cx + r, cy - r), fill=fill, width=line)
                    d.line((cx + r * .25, cy - r, cx + r, cy - r, cx + r, cy - r * .25), fill=fill, width=line, joint="curve")
                else:
                    d.line((cx + r, cy - r, cx + r * .05, cy - r * .05), fill=fill, width=line)
                    d.line((cx - r * .05, cy - r * .7, cx + r * .05, cy - r * .05, cx + r * .7, cy + r * .05), fill=fill, width=line, joint="curve")
    d.rounded_rectangle((P(M), P(M), P(M + width) - 1, P(M + height) - 1), P(RADIUS), outline=BORDER, width=max(1, round(big)))


def _draw_top(panel, images, texts, scale):
    """Images, then text, on the panel at its final size."""
    M = MARGIN
    for x, y, name, size, *faded in images:
        icon = asset(name, round(size * scale))
        if faded and faded[0] < 1:  # swap slide: fading out or in
            icon = icon.copy()
            icon.putalpha(icon.getchannel("A").point(lambda v: round(v * max(0.0, faded[0]))))
        panel.paste(icon, (round((M + x) * scale), round((M + y) * scale)), icon)
    draw = ImageDraw.Draw(panel, "RGBA")
    for x, y, value, size, bold, fill, anchor in texts:
        if len(fill) == 4 and fill[3] < 255:
            # Pillow ignores the ink's alpha for text, so fade by mixing with the panel colour.
            t = fill[3] / 255
            fill = tuple(round(BG[i] * (1 - t) + fill[i] * t) for i in range(3)) + (255,)
        draw_text(draw, ((M + x) * scale, (M + y) * scale), value, size, bold, scale, fill, anchor)


@lru_cache(maxsize=512)
def _text_extent(value, size, bold, scale, anchor):
    return font(size, bold, scale).getbbox(value, anchor=anchor)


def _device_box(x0, y0, x1, y1, scale, pad=2):
    """A logical box (panel coordinates, without the margin) as device px, with room to spare."""
    M = MARGIN
    return (math.floor((M + x0) * scale) - pad, math.floor((M + y0) * scale) - pad,
            math.ceil((M + x1) * scale) + pad, math.ceil((M + y1) * scale) + pad)


def _shape_box(op, scale):
    kind = op[0]
    if kind == "rect":
        _, x, y, w, h, _, _ = op
        return _device_box(min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h), scale)
    if kind == "ellipse":
        _, x1, y1, x2, y2, _ = op
        return _device_box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), scale)
    _, cx, cy, r, _ = op  # icons: lines 1.4 px wide, the power symbol 1 px lower
    return _device_box(cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3, scale)


def _image_box(op, scale):
    x, y, _, size = op[:4]
    return _device_box(x, y, x + size, y + size, scale)


def _text_box(op, scale):
    x, y, value, size, bold, _, anchor = op
    left, top, right, bottom = _text_extent(value, size, bold, scale, anchor)
    px, py = (MARGIN + x) * scale, (MARGIN + y) * scale
    return (math.floor(px + left) - 3, math.floor(py + top) - 3, math.ceil(px + right) + 3, math.ceil(py + bottom) + 3)


def _changed(old, new, box, scale):
    """Device boxes of what differs between two lists of drawing ops. Ops in both, in the same
    order, draw the same pixels; only the inserted, removed and changed ones (old and new place)
    need drawing again."""
    if old == new:
        return []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            out += [box(op, scale) for op in old[i1:i2]] + [box(op, scale) for op in new[j1:j2]]
    return out


def _merge(boxes, size):
    """Boxes clipped to the image, overlapping ones merged."""
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


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


class Painter:
    """paint() for one popup's frames, drawing again only what changed since its last frame: a
    hover fade or a toggle changes one row, not the whole panel. Each changed area is rebuilt
    from the background up with everything that touches it, in the same order, so the frame is
    pixel for pixel what paint() draws (tests compare them)."""

    def __init__(self):
        self.last = None      # ((width, height, scale), shapes, images, texts) of the last frame
        self.image = None     # the last frame
        self.changed = None   # device boxes the last frame changed (None: all of it)
        self.count = 0        # frames painted; `changed` is relative to frame count - 1

    def paint(self, layout, width, height, scale):
        M = MARGIN
        full = (round((width + 2 * M) * scale), round((height + 2 * M) * scale))
        key = (width, height, scale)
        shapes, images, texts = list(layout.shapes), list(layout.images), list(layout.texts)
        boxes = None
        if self.last and self.last[0] == key:
            _, old_shapes, old_images, old_texts = self.last
            boxes = _merge(_changed(old_shapes, shapes, _shape_box, scale) + _changed(old_images, images, _image_box, scale)
                           + _changed(old_texts, texts, _text_box, scale), full)
            if sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes) > 0.6 * full[0] * full[1]:
                boxes = None  # most of it: one whole redraw is quicker
        self.last = (key, shapes, images, texts)
        self.count += 1
        if boxes is None:
            self.image = None  # let it go before a whole redraw makes the next
            self.image, _ = paint(layout, width, height, scale)
            self.changed = None
            return self.image, _hits(layout)
        image = self.image.copy()  # a new frame (the window compares frames by identity)
        if boxes:
            # Scratch canvases at the frame's own coordinates, only as tall as the lowest change
            # and not cleared: each changed box is filled before it is drawn, and nothing outside
            # the boxes is read (memory is only touched where something is drawn).
            bottom = max(b[3] for b in boxes)
            big = Image.new("RGB", (full[0] * SS, bottom * SS), None)
            small = Image.new("RGB", (full[0], bottom), None)
            mask, shadow = frame(width, height, scale)
            d = ImageDraw.Draw(big, "RGBA")
            for box in boxes:
                ss = tuple(v * SS for v in box)
                big.paste(BG[:3], ss)
                _draw_shapes(d, [op for op in shapes if _overlaps(_shape_box(op, scale), box)], width, height, scale)
                small.paste(big.crop(ss).reduce(SS), box[:2])
                _draw_top(small, [op for op in images if _overlaps(_image_box(op, scale), box)],
                          [op for op in texts if _overlaps(_text_box(op, scale), box)], scale)
                region = small.crop(box).convert("RGBA")
                region.putalpha(mask.crop(box))
                out = shadow.crop(box)
                out.alpha_composite(region)
                image.paste(out, box[:2])
        self.image, self.changed = image, boxes
        return image, _hits(layout)


def write_bgra(image, address, boxes=None):
    """Write an RGBA image as premultiplied BGRA (what a layered window shows) to memory at
    `address`, rows of image.width * 4 bytes; with `boxes`, only those parts (device px)."""
    if boxes is None:
        data = image.convert("RGBa").tobytes("raw", "BGRa")
        ctypes.memmove(address, data, len(data))
        return
    stride = image.width * 4
    for x0, y0, x1, y1 in boxes:
        data = image.crop((x0, y0, x1, y1)).convert("RGBa").tobytes("raw", "BGRa")
        row = (x1 - x0) * 4
        for i in range(y1 - y0):
            ctypes.memmove(address + (y0 + i) * stride + x0 * 4, data[i * row:(i + 1) * row], row)


def render(state, hover=None, scale=1.0, pending=None, pinned=False, fx=None, armed=None, compact=None, only=None, painter=None):
    """(image, hits). With a Painter, only what changed since its last frame is drawn again."""
    layout, height = build(state, hover, pending, pinned, fx, armed, compact, only)
    compact = (state.get("compact") if compact is None else compact) and not only
    return (painter.paint if painter else paint)(layout, COMPACT_WIDTH if compact else WIDTH, height, scale)


def render_menu(items, hover=None, scale=1.0, fx=None, painter=None):
    layout, height = build_menu(items, hover, fx)
    return (painter.paint if painter else paint)(layout, MENU_WIDTH, height, scale)


def hit_test(hits, x, y):
    """Action under a point in logical px (including margin), or None."""
    for (hx, hy, hw, hh), action in hits:
        if hx <= x < hx + hw and hy <= y < hy + hh:
            return action
    return None


def header_height():
    """Pinned flyouts can be dragged by the header strip (above the first section)."""
    return MARGIN + 50


# ---------- taskbar view (Windows): one block per provider, drawn straight onto the taskbar ----------
BLOCK_PAD, BLOCK_PAD_R = 12, 16   # generous on the right: the block sits in open taskbar space
BLOCK_COL, BLOCK_GAP = 104, 16    # one column per limit
THEMES = {
    False: {"text": TEXT, "muted": MUTED, "faint": FAINT, "track": (255, 255, 255, 34),
            "plate": (22, 22, 22, 84), "plate_hover": (58, 58, 58, 130),  # a light backing: readable on translucent taskbars
            "good": GOOD, "warn": WARN, "bad": BAD, "accent": ACCENT},
    True: {"text": (26, 26, 26, 255), "muted": (84, 84, 84, 255), "faint": (104, 104, 104, 255),
           "track": (0, 0, 0, 30), "plate": (252, 252, 252, 110), "plate_hover": (236, 236, 236, 170),
           "good": (24, 138, 86, 255), "warn": (168, 116, 0, 255), "bad": (196, 58, 46, 255),
           "accent": {"claude": (186, 92, 58, 255), "codex": (108, 88, 214, 255)}},
}


@lru_cache(maxsize=4)
def dark_asset(name, px):
    icon = asset(name, px)
    dark = ImageOps.invert(icon.convert("RGB")).convert("RGBA")
    dark.putalpha(icon.getchannel("A"))
    return dark


def _block_level(theme, left):
    return theme["good"] if left > 30 else theme["warn"] if left > 10 else theme["bad"]


def block_row(L, account, fx, height, theme, x=0.0, columns=3):
    """One account's content (icon, name, provider line, a column per limit) from x.
    Returns its width, padding included."""
    provider = account["provider"]
    y1, bar_y, y3 = round(height * .3), height * .5, round(height * .74)
    L.image(x + BLOCK_PAD, round(height / 2) - 8, provider + ("@dark" if theme is THEMES[True] and provider == "codex" else ""), 16)
    name = fit(display_name(account), 12, True, 260)
    note = status_note(account)
    # Kept short for the taskbar: the plan and how old the numbers are (the provider is the icon;
    # everything else is one click away in the panel).
    title = account.get("plan") or ""
    problem = note if account.get("status") else None  # sign in again, retrying: instead of the plan
    sub, sub_color = (problem, theme["warn"]) if problem else (title, theme["accent"][provider])
    age = None if problem else age_text(account)
    tx = x + BLOCK_PAD + 22
    L.text(tx, y1, name, 12, theme["text"], bold=True)
    L.text(tx, y3, sub, 10, sub_color)
    if age:  # how old the numbers are, after the plan (amber once 30 minutes old)
        lead = " · " if sub else ""
        L.text(tx + text_w(sub, 10), y3, lead + age, 10, theme["warn"] if stale(account) else theme["faint"])
    # The age's width is kept at any value: the bars don't move as it counts up.
    sub_w = text_w(sub, 10) + (text_w(" · " if sub else "", 10) + age_room(10) if age else 0)
    x = tx + max(text_w(name, 12, True), sub_w) + BLOCK_GAP
    windows = account["windows"][:columns]
    if not windows:
        label = "Usage not loaded yet"
        L.text(x, height / 2, label, 11, theme["faint"])
        return math.ceil(x + text_w(label, 11) + BLOCK_PAD_R)
    for i, window in enumerate(windows):
        if i:
            x += BLOCK_COL + BLOCK_GAP
        left = fx.get(("bar", account["id"], window["key"]), remaining(window["used"]))
        shown = remaining(window["used"])
        color = _block_level(theme, shown)
        right = x + BLOCK_COL
        L.text(x, y1 - 2, short_label(window), 11, theme["muted"])
        L.text(right, y1 - 2, " left", 10, theme["faint"], anchor="rm")
        L.text(right - text_w(" left", 10), y1 - 2, f"{shown:.0f}%", 11, color, bold=True, anchor="rm")
        L.rect(x, bar_y - 1.5, BLOCK_COL, 3, 1.5, theme["track"])
        if left > 0.5:
            L.rect(x, bar_y - 1.5, max(3, BLOCK_COL * left / 100), 3, 1.5, _block_level(theme, left))
        if window.get("resetsAt"):
            full = "resets in " + until(window["resetsAt"])
            if text_w(full, 10) <= BLOCK_COL:
                L.text(x, y3, full, 10, theme["faint"])
            else:
                L.icon("clock", x + 4, y3, 3.5, theme["faint"])
                L.text(x + 11, y3, until(window["resetsAt"]), 10, theme["faint"])
    return math.ceil(x + BLOCK_COL + BLOCK_PAD_R)


# A taskbar block shows a slot: a provider's account in use ("claude", "codex"), or one account
# whatever is in use ("acct:<id>").
def slot_account(state, slot):
    """The account a slot shows now, or None."""
    if isinstance(slot, str) and slot.startswith("acct:"):
        return next((a for a in state["accounts"] if a["id"] == slot[5:]), None)
    return next((a for a in state["accounts"] if a["provider"] == slot and a["active"]), None)


def block_accounts(state, slot, fx):
    """The accounts a block draws: the one in use, plus one sliding out mid-swap (one account's
    block: just that account)."""
    if isinstance(slot, str) and slot.startswith("acct:"):
        account = slot_account(state, slot)
        return [account] if account else []
    return [a for a in state["accounts"] if a["provider"] == slot
            and (a["active"] or fx.get(("active", a["id"]), 0.0) > 0.01)]


def block_width(state, slot, height=44, light=False, columns=3):
    """Resting width of a slot's block, or 0 when it has no account to show."""
    account = slot_account(state, slot)
    return block_row(Layout(), account, {}, height, THEMES[light], columns=columns) if account else 0


def build_block(state, provider, fx=None, hover=None, height=44, light=False, width=None, columns=3):
    """Lay out one taskbar block (`provider`: its slot, see slot_account). Returns (layout, width).

    Swapping works like the compact panel: the old account slides out to the left and fades,
    then the new one slides in from the right; `width` (animated by the host) glides between
    the two accounts' widths."""
    theme, fx = THEMES[light], fx or {}
    L = Layout()
    width = width or block_width(state, provider, height, light, columns) or 120
    hov = fx.get(("hover", "open"), 1.0 if hover == "open" else 0.0)
    L.rect(0, 0, width, height, 6, mix(theme["plate"], theme["plate_hover"], hov))
    pinned = isinstance(provider, str) and provider.startswith("acct:")  # one account: no swap slide
    for account in block_accounts(state, provider, fx):
        t = 1.0 if pinned else fx.get(("active", account["id"]), 1.0 if account["active"] else 0.0)
        alpha = max(0.0, 2 * t - 1)
        if alpha <= 0.01:
            continue
        marks = (len(L.shapes), len(L.texts), len(L.images), len(L.hits))
        block_row(L, account, fx, height, theme, columns=columns)
        if not account["eligible"]:
            dim_row(L, marks[:3])
        if t < 0.999:
            move_row(L, marks, min(1.0, 2 * (1 - t)) * 36 * (1 if account["active"] else -1), alpha)
    L.hit(0, 0, width, height, "open")
    return L, width


def _mask_blit(canvas, mask, x, y, fill):
    """Composite a solid colour through a coverage mask at (x, y), clipped to the canvas."""
    if x + mask.width <= 0 or y + mask.height <= 0:
        return
    if x < 0 or y < 0:
        mask = mask.crop((max(0, -x), max(0, -y), mask.width, mask.height))
        x, y = max(0, x), max(0, y)
    if mask.width <= 0 or mask.height <= 0 or x >= canvas.width or y >= canvas.height:
        return
    if fill[3] < 255:
        mask = mask.point(lambda v, a=fill[3]: v * a // 255)
    layer = Image.new("RGBA", mask.size, fill[:3] + (255,))
    layer.putalpha(mask)
    canvas.alpha_composite(layer, (x, y))


def paint_clear(layout, width, height, scale):
    """Rasterise a layout onto a transparent canvas (no panel, no shadow): each shape, text and
    image is composited properly, so translucent pieces blend with the taskbar behind them."""
    canvas = Image.new("RGBA", (max(1, round(width * scale)), max(1, round(height * scale))), (0, 0, 0, 0))
    big = scale * SS
    for op in layout.shapes:
        kind, fill = op[0], op[-1]
        if fill[3] <= 0:
            continue
        if kind == "rect":
            _, x, y, w, h, r, _ = op
            if w <= 0 or h <= 0:
                continue  # an animated bar at (or eased just past) zero size: nothing to draw
            box = (x, y, x + w, y + h)
        elif kind == "ellipse":
            _, x1, y1, x2, y2, _ = op
            if x2 < x1 or y2 < y1:
                continue
            box = (x1, y1, x2, y2)
        else:  # clock
            _, cx, cy, r, _ = op
            box = (cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1)
        left, top = int(box[0] * scale) - 1, int(box[1] * scale) - 1
        size = (int(box[2] * scale) + 2 - left, int(box[3] * scale) + 2 - top)
        mask = Image.new("L", (size[0] * SS, size[1] * SS), 0)
        d = ImageDraw.Draw(mask)
        P = lambda v, o: v * big - o * SS
        if kind == "rect":
            coords = (P(x, left), P(y, top), P(x + w, left) - 1, P(y + h, top) - 1)
            if coords[2] < coords[0] or coords[3] < coords[1]:
                continue  # smaller than a pixel once scaled
            d.rounded_rectangle(coords, r * big, fill=255) if r else d.rectangle(coords, fill=255)
        elif kind == "ellipse":
            d.ellipse((P(x1, left), P(y1, top), P(x2, left), P(y2, top)), fill=255)
        else:
            line = max(1, round(1.4 * big))
            cx, cy, rr = P(cx, left), P(cy, top), r * big
            d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), outline=255, width=line)
            d.line((cx, cy - rr * .55, cx, cy, cx + rr * .45, cy + rr * .3), fill=255, width=line, joint="curve")
        _mask_blit(canvas, mask.reduce(SS), left, top, fill)
    for x, y, name, size, *faded in layout.images:
        # "@dark": a light mark (Codex), darkened for a light taskbar
        icon = dark_asset(name[:-5], round(size * scale)) if name.endswith("@dark") else asset(name, round(size * scale))
        alpha = faded[0] if faded else 1.0
        if alpha < 1:
            icon = icon.copy()
            icon.putalpha(icon.getchannel("A").point(lambda v: round(v * max(0.0, alpha))))
        px, py = round(x * scale), round(y * scale)
        if px + icon.width <= 0 or px >= canvas.width:
            continue
        if px < 0:
            icon, px = icon.crop((-px, 0, icon.width, icon.height)), 0
        if px < canvas.width:
            canvas.alpha_composite(icon, (px, max(0, py)))
    for x, y, value, size, bold, fill, anchor in layout.texts:
        if fill[3] <= 0:
            continue
        mask, left, top = text_mask(x * scale, y * scale, value, size, bold, scale, anchor)
        _mask_blit(canvas, mask, left, top, fill)
    return canvas, list(layout.hits)


def render_block(state, provider, hover=None, scale=1.0, fx=None, height=44, light=False, width=None, columns=3):
    layout, width = build_block(state, provider, fx, hover, height, light, width, columns)
    return paint_clear(layout, width, height, scale)
