"""Draws the full view (the app's main window) with Pillow: no browser, same look as before.

Pure drawing, no windowing: each platform's host (Win32, AppKit, Tk) shows the frames and passes
input to fullview.FullView. The page is split into tiles (the header, each provider heading, each
account card) that are drawn once and cached; hovering or a change to one account redraws only
that tile, and scrolling only re-composes cached tiles. Shapes are anti-aliased with cached masks
instead of drawing the whole window at 2x. Everything is laid out in logical pixels times `scale`.
"""
from functools import lru_cache
import json
from pathlib import Path
import math
import sys
import time

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import flyout_render as fr
from .clock import clock_12h  # noqa: F401  (also used from here)

# Colours (the web full view's palette)
BG = (22, 22, 22)
SURFACE = (32, 32, 32)
SURFACE_2 = (39, 39, 39)
SURFACE_3 = (46, 46, 46)
LINE = (255, 255, 255, 18)
LINE_STRONG = (255, 255, 255, 33)
TEXT = (243, 243, 243)
MUTED = (163, 163, 163)
FAINT = (149, 149, 149)
TRACK = (255, 255, 255, 23)
GOOD, WARN, BAD = (76, 195, 138), (229, 181, 74), (239, 106, 91)
ACCENT = {"claude": (224, 138, 104), "codex": (162, 149, 247)}
ON_ACCENT = (22, 22, 22)
FOCUS = (138, 180, 255)
PROVIDERS = (("claude", "Claude", ""), ("codex", "Codex", ""))  # (id, title, caption shown at the right)

PAD = 28           # page padding
MAX_W = 1180       # content max width
GAP = 14           # between cards
CARD_MIN = 380     # narrowest card before the grid drops a column
RADIUS = 14
SHADOW = 18        # room around a card for its shadow
TOPBAR_H = 66
GROUP_HEAD_H = 34
SCROLL_STEP = 64


# ---------- small helpers ----------
def remaining(used):
    """What's left, rounded down: never more room than there is (93.4% used shows 6% left)."""
    return max(0, min(100, math.floor(100 - used + 1e-6)))


def level(left):
    return GOOD if left > 30 else WARN if left > 10 else BAD


def blend(color, bg, alpha):
    """An RGB colour at `alpha` (0..1) over bg: Pillow draws text without the ink's alpha."""
    return tuple(round(bg[i] * (1 - alpha) + color[i] * alpha) for i in range(3))


def over(bg, rgba):
    return blend(rgba[:3], bg, rgba[3] / 255)


def redact(email):
    """Name mode hides the email whole (not even its first letter or domain); a click shows it."""
    return "Show email" if email else ""


CLOCK_24 = None  # the Settings switch: True / False, or None to follow the system


def clock(ts):
    t = time.localtime(ts)
    if (not CLOCK_24) if CLOCK_24 is not None else clock_12h():
        return time.strftime("%I:%M %p", t).lstrip("0")
    return time.strftime("%H:%M", t)


def relative(ts):
    m = max(0, round((ts - time.time()) / 60))
    if m < 1:
        return "now"
    if m < 60:
        return f"in {m}m"
    if m < 1440:
        return f"in {m // 60}h {m % 60}m"
    return f"in {m // 1440}d {m % 1440 // 60}h"


def absolute(ts):
    same_day = time.localtime(ts)[:3] == time.localtime()[:3]
    return clock(ts) if same_day else time.strftime("%a ", time.localtime(ts)) + clock(ts)


def reset_text(ts, unused=False):
    """The reset time; a window nobody has used has none yet: it starts with the first message."""
    if ts:
        return f"Resets {absolute(ts)} · {relative(ts)}"
    return "Starts with your first message" if unused else "Reset time not reported"


def date_text(ts):
    t = time.localtime(ts)
    return time.strftime("%b ", t) + str(t.tm_mday)


def subscription_text(account):
    """"Renews Oct 14 · in 18d" / "Ends Oct 14 · in 3d", or ''."""
    sub = account.get("subscription") or {}
    if not sub.get("at") or sub["at"] < time.time() - 86400:
        return ""
    days = int((sub["at"] - time.time()) // 86400)
    when = f"in {days}d" if days >= 1 else "today"
    return f"{'Ends' if sub.get('ends') else 'Renews'} {'~' if sub.get('estimated') else ''}{date_text(sub['at'])} · {when}"


def credits_items(account):
    c = account.get("credits")
    if not c:
        return []
    items = []
    main = None
    if c.get("kind") == "credits":
        if c.get("unlimited"):
            main = ("Credits", "unlimited")
        elif isinstance(c.get("balance"), (int, float)):
            main = ("Credits", f"{c['balance']:,.2f}".rstrip("0").rstrip("."))
        elif c.get("enabled"):
            main = ("Credits", "available")
    elif c.get("kind") != "none":
        if not c.get("enabled"):
            main = ("Extra usage", "off")
        elif isinstance(c.get("limit"), (int, float)) and isinstance(c.get("used"), (int, float)):
            main = ("Extra usage", f"{max(0, c['limit'] - c['used']):,g} of {c['limit']:,g} left")
        else:
            main = ("Extra usage", "on")
    if main:
        items.append(main)
    if isinstance(c.get("resets"), int):
        items.append(("Usage limit resets", str(c["resets"])))
    return items


# ---------- text metrics ----------
_measure = None  # a platform's own text measure (the macOS full view: Core Text); None: FreeType


def set_measure(measure):
    """Width of a label as the platform draws it: text_w(value, size, bold) in logical px."""
    global _measure
    _measure = measure


def text_w(value, size, bold=False):
    return _measure(value, size, bold) if _measure else fr.text_w(value, size, bold)


def fit(value, size, bold, width):
    """`value`, cut with an ellipsis to fit `width`."""
    if text_w(value, size, bold) <= width:
        return value
    while value and text_w(value + "…", size, bold) > width:
        value = value[:-1]
    return value + "…"


def best_fit(options, size, width):
    """The first of `options` (longest first) that fits `width`; the last one, cut, when none does."""
    for text in options:
        if text_w(text, size, False) <= width:
            return text
    return fit(options[-1], size, False, width)


STALE_AFTER = 1800  # numbers older than this show their age (they're kept, not guessed)


def ago(seconds):
    minutes = int(seconds // 60)
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h {minutes % 60}m" if minutes < 1440 else f"{minutes // 1440}d"


def display_name(account):
    return account.get("name") or account.get("email") or account.get("alias", "")


# ---------- anti-aliased shapes from cached masks ----------
@lru_cache(maxsize=256)
def rr_mask(w, h, r):
    """Rounded-rectangle coverage mask (w, h, r in device px), drawn at 4x and reduced.

    Only the corners need the 4x drawing: they come from a small copy (2r+2 square) and the rest
    is solid, pixel for pixel what drawing the whole card at 4x gives (a card at 2x was a 4x
    image of several MB for every mask; this is a few KB)."""
    if r <= 0 or 2 * r + 2 > min(w, h):
        return _rr_mask_4x(w, h, r)
    corners = _rr_mask_4x(2 * r + 2, 2 * r + 2, r)
    mask = Image.new("L", (w, h), 255)
    far = r + 2
    for box, at in (((0, 0, r, r), (0, 0)), ((far, 0, far + r, r), (w - r, 0)),
                    ((0, far, r, far + r), (0, h - r)), ((far, far, far + r, far + r), (w - r, h - r))):
        mask.paste(corners.crop(box), at)
    return mask


def _rr_mask_4x(w, h, r):
    k = 4
    big = Image.new("L", (max(1, w * k), max(1, h * k)), 0)
    if w > 0 and h > 0:
        ImageDraw.Draw(big).rounded_rectangle((0, 0, w * k - 1, h * k - 1), max(0, r * k), fill=255)
    return big.reduce(k) if w and h else big


@lru_cache(maxsize=128)
def ring_mask(w, h, r, width):
    """A rounded-rectangle outline of `width` device px."""
    hole = Image.new("L", (w, h), 0)
    hole.paste(rr_mask(max(1, w - 2 * width), max(1, h - 2 * width), max(0, r - width)), (width, width))
    return ImageChops.subtract(rr_mask(w, h, r), hole)


BIG_MASK = 256 * 256  # masks this big (whole cards) aren't kept for every opacity


def _solid_middle(w, h, r):
    """rr_mask(w, h, r) is 255 in every row but its top and bottom r (it is made from corners)."""
    return w > 0 and h > 0 and (r <= 0 or 2 * r + 2 <= min(w, h))


def rr_alpha(w, h, r, alpha):
    """rr_mask at `alpha`. Small ones are kept; a card-sized one is made when needed (a fraction
    of a millisecond, only when a card is drawn again) rather than kept for every hover step."""
    if alpha >= 255:
        return rr_mask(w, h, r)
    if w * h > BIG_MASK:
        return rr_mask(w, h, r).point(lambda v: v * alpha // 255)
    return _rr_alpha(w, h, r, alpha)


@lru_cache(maxsize=256)
def _rr_alpha(w, h, r, alpha):
    return rr_mask(w, h, r).point(lambda v: v * alpha // 255)


def ring_alpha(w, h, r, width, alpha):
    """ring_mask at `alpha` (kept only when small, like rr_alpha)."""
    if alpha >= 255:
        return ring_mask(w, h, r, width)
    if w * h > BIG_MASK:
        return ring_mask(w, h, r, width).point(lambda v: v * alpha // 255)
    return _ring_alpha(w, h, r, width, alpha)


@lru_cache(maxsize=128)
def _ring_alpha(w, h, r, width, alpha):
    return ring_mask(w, h, r, width).point(lambda v: v * alpha // 255)


class Canvas:
    """Draws in logical px on an RGB(A) image of scale x that size."""

    def __init__(self, image, scale, bg, origin=(0, 0)):
        """origin: where `image` sits in the whole picture (device px), to redraw one part of
        it on its own: everything lands on exactly the pixels it would in the whole."""
        self.image, self.s, self.bg = image, scale, bg
        self.ox, self.oy = origin
        self.draw = ImageDraw.Draw(image)

    def px(self, v):
        return round(v * self.s)

    def box(self, x, y, w, h):
        x0, y0 = self.px(x), self.px(y)
        return x0 - self.ox, y0 - self.oy, max(1, self.px(x + w) - x0), max(1, self.px(y + h) - y0)

    def rect(self, x, y, w, h, r, fill):
        x0, y0, pw, ph = self.box(x, y, w, h)
        alpha = fill[3] if len(fill) == 4 else 255
        r = self.px(r)
        if pw * ph > BIG_MASK and _solid_middle(pw, ph, r):
            # A big one (a menu): only its rounded top and bottom need the mask; between them it is solid.
            self.bands(fill[:3], alpha, x0, y0, lambda: rr_mask(pw, ph, r), r, [(x0, y0 + r, x0 + pw, y0 + ph - r)])
            return
        self.image.paste(fill[:3], (x0, y0), rr_alpha(pw, ph, r, alpha))

    def outline(self, x, y, w, h, r, color, width=1):
        x0, y0, pw, ph = self.box(x, y, w, h)
        alpha = color[3] if len(color) == 4 else 255
        r, width = self.px(r), max(1, round(width * self.s))
        if pw * ph > BIG_MASK and _solid_middle(pw, ph, r) and _solid_middle(pw - 2 * width, ph - 2 * width, max(0, r - width)):
            # A big one: its top and bottom from the mask, its sides solid, and nothing in between.
            t = max(r, width)
            self.bands(color[:3], alpha, x0, y0, lambda: ring_mask(pw, ph, r, width), t,
                       [(x0, y0 + t, x0 + width, y0 + ph - t), (x0 + pw - width, y0 + t, x0 + pw, y0 + ph - t)])
            return
        self.image.paste(color[:3], (x0, y0), ring_alpha(pw, ph, r, width, alpha))

    def bands(self, color, alpha, x0, y0, mask, t, solid):
        """Paste `color` through mask() (at x0, y0, in this image's px) in its top and bottom `t`
        rows only, and fill the `solid` boxes (where the mask is 255): the same pixels as pasting
        through all of it."""
        mask = mask() if t > 0 else None
        for top in (0, mask.height - t) if mask else ():
            band = mask.crop((0, top, mask.width, top + t))
            if alpha < 255:
                band = band.point(lambda v: v * alpha // 255)
            self.image.paste(color, (x0, y0 + top), band)
        for bx0, by0, bx1, by1 in solid:
            if bx1 > bx0 and by1 > by0:
                if alpha >= 255:
                    self.image.paste(color, (bx0, by0, bx1, by1))
                else:
                    self.image.paste(color, (bx0, by0, bx1, by1), Image.new("L", (bx1 - bx0, by1 - by0), alpha))

    def dot(self, cx, cy, r, fill):
        self.rect(cx - r, cy - r, 2 * r, 2 * r, r, fill)

    def line(self, x, y, w, color):
        """A hairline across (1 device px)."""
        x0, y0 = self.px(x), self.px(y)
        x1 = self.px(x + w)
        self.image.paste(over(self.bg, color) if len(color) == 4 else color,
                         (x0 - self.ox, y0 - self.oy, x1 - self.ox, y0 - self.oy + max(1, round(self.s))))

    def text(self, x, y, value, size, fill, bold=False, anchor="ls", bg=None):
        if len(fill) == 4:
            fill = over(bg or self.bg, fill)
        fr.draw_text(self.draw, (x * self.s - self.ox, y * self.s - self.oy), value, size, bold, self.s, fill, anchor)

    def image_at(self, x, y, name, size):
        icon = fr.asset(name, self.px(size))
        self.image.paste(icon, (self.px(x) - self.ox, self.px(y) - self.oy), icon)

    def glyph(self, kind, cx, cy, size, color):
        icon = glyph(kind, self.px(size), color)
        self.image.paste(icon, (self.px(cx) - icon.width // 2, self.px(cy) - icon.height // 2), icon)

    def spin_glyph(self, kind, cx, cy, size, color, degrees):
        """glyph, turned `degrees` anticlockwise and laid over what is there (the refresh arrow)."""
        icon = glyph(kind, self.px(size), color)
        if degrees:
            icon = icon.rotate(degrees, resample=Image.Resampling.BICUBIC)
        self.image.alpha_composite(icon, (self.px(cx) - icon.width // 2 - self.ox, self.px(cy) - icon.height // 2 - self.oy))

    def mark(self, x, y, size, radius):
        """The app's logo, `size` square with rounded corners (the header)."""
        mark = fr.asset("switcher", self.px(size))
        rounded = Image.new("RGBA", mark.size, (0, 0, 0, 0))
        rounded.paste(mark, (0, 0), Image.composite(mark.getchannel("A"), Image.new("L", mark.size, 0),
                                                    rr_mask(*mark.size, self.px(radius))))
        self.image.alpha_composite(rounded, (self.px(x) - self.ox, self.px(y) - self.oy))


class Recorder:
    """The Canvas calls, kept (logical px) to be replayed by a platform's own painter instead of
    drawn into pixels here. Colours are resolved as Canvas resolves them (alpha text and hairlines
    are blended over the background), so a replay needs no more than the calls."""

    def __init__(self, scale, bg):
        self.s, self.bg, self.ops = scale, bg, []

    def px(self, v):
        return round(v * self.s)

    def rect(self, x, y, w, h, r, fill):
        self.ops.append(("rect", x, y, w, h, r, tuple(fill) if len(fill) == 4 else tuple(fill) + (255,)))

    def outline(self, x, y, w, h, r, color, width=1):
        self.ops.append(("outline", x, y, w, h, r, tuple(color) if len(color) == 4 else tuple(color) + (255,), width))

    def dot(self, cx, cy, r, fill):
        self.rect(cx - r, cy - r, 2 * r, 2 * r, r, fill)

    def line(self, x, y, w, color):
        self.ops.append(("line", x, y, w, over(self.bg, color) if len(color) == 4 else tuple(color)))

    def text(self, x, y, value, size, fill, bold=False, anchor="ls", bg=None):
        if len(fill) == 4:
            fill = over(bg or self.bg, fill)
        self.ops.append(("text", x, y, value, size, tuple(fill), bold, anchor))

    def image_at(self, x, y, name, size):
        self.ops.append(("image", x, y, name, size))

    def glyph(self, kind, cx, cy, size, color):
        self.ops.append(("glyph", kind, cx, cy, size, tuple(color), 0))

    def spin_glyph(self, kind, cx, cy, size, color, degrees):
        self.ops.append(("glyph", kind, cx, cy, size, tuple(color), degrees))

    def mark(self, x, y, size, radius):
        self.ops.append(("mark", x, y, size, radius))


@lru_cache(maxsize=64)
def glyph(kind, px, color):
    """Small line icons (caret, plus, refresh, check), drawn at 4x and reduced."""
    k = 4
    size = px * k
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    w = max(k, round(size * 0.1))
    c = color + (255,) if len(color) == 3 else color
    s = size
    if kind == "caret":
        d.line((s * .25, s * .38, s * .5, s * .63, s * .75, s * .38), fill=c, width=w, joint="curve")
    elif kind == "plus":
        d.line((s * .5, s * .2, s * .5, s * .8), fill=c, width=w)
        d.line((s * .2, s * .5, s * .8, s * .5), fill=c, width=w)
    elif kind == "refresh":
        d.arc((s * .18, s * .18, s * .82, s * .82), 40, 330, fill=c, width=w)
        d.line((s * .82, s * .12, s * .82, s * .42, s * .52, s * .42), fill=c, width=w, joint="curve")
    elif kind == "check":
        d.line((s * .2, s * .52, s * .42, s * .72, s * .8, s * .3), fill=c, width=w, joint="curve")
    elif kind == "left":
        d.line((s * .6, s * .25, s * .35, s * .5, s * .6, s * .75), fill=c, width=w, joint="curve")
    elif kind == "right":
        d.line((s * .4, s * .25, s * .65, s * .5, s * .4, s * .75), fill=c, width=w, joint="curve")
    return image.reduce(k)


@lru_cache(maxsize=16)
def shadow(w, h, scale, strength=1.0):
    """A card's soft shadow (0 1px 2px .3, 0 8px 24px .22) on a transparent image with SHADOW margin."""
    m = round(SHADOW * scale)
    full = (round(w * scale) + 2 * m, round(h * scale) + 2 * m)
    base = Image.new("L", full, 0)
    base.paste(rr_mask(round(w * scale), round(h * scale), round(RADIUS * scale)), (m, m))
    soft = base.point(lambda v: round(v * .22 * strength)).filter(ImageFilter.GaussianBlur(10 * scale))
    near = base.point(lambda v: round(v * .3 * strength)).filter(ImageFilter.GaussianBlur(1 * scale))
    out = Image.new("L", full, 0)
    out.paste(soft, (0, round(7 * scale)))
    lifted = Image.new("L", full, 0)
    lifted.paste(near, (0, round(scale)))
    out = ImageChops.lighter(out, lifted)
    layer = Image.new("RGBA", full, (0, 0, 0, 0))
    layer.putalpha(out)
    return layer


# ---------- layout: where everything goes (logical px, page coordinates) ----------
def columns_for(width):
    inner = min(MAX_W, width - 2 * PAD)
    return max(1, int((inner + GAP) // (CARD_MIN + GAP))), inner


def identity_height(account, name_mode):
    h = 22  # the name
    if name_mode:
        h += 18  # the email under it
    if account.get("plan"):
        h += 22
    return max(38, h)


def card_height(account, name_mode):
    h = 16 + identity_height(account, name_mode) + 14
    windows = account.get("windows") or []
    h += len(windows) * 50 + max(0, len(windows) - 1) * 11 if windows else 28
    if credits_items(account):
        h += 11 + 18
    return h + 14 + 13 + 32 + 14


def layout(state, width):
    """[(kind, key, x, y, w, h, data)] for the page, and its total height."""
    cols, inner = columns_for(width)
    left = (width - inner) / 2
    items = [("topbar", "topbar", left, PAD, inner, TOPBAR_H, None)]
    y = PAD + TOPBAR_H + 22
    accounts = state.get("accounts") or []
    if not accounts:
        items.append(("empty", "empty", left, y, inner, 170, None))
        return items, y + 170 + PAD
    card_w = (inner - (cols - 1) * GAP) / cols
    name_mode = bool(state.get("nameMode"))
    windows = state.get("windows") or []
    if windows or (state.get("perWindow") and state.get("mode") == "live"):
        # separate accounts per window: the open windows, above the accounts (with the setting on and
        # none open yet, a line saying how to get one)
        h = windows_height(windows, len(no_window_lines(state, inner)))
        items.append(("windows", "windows", left, y, inner, h, None))
        y += h + 26
    for provider, title, caption in PROVIDERS:
        group = [a for a in accounts if a["provider"] == provider]
        if not group:
            continue
        items.append(("group", "group:" + provider, left, y, inner, GROUP_HEAD_H, (provider, title, caption, len(group))))
        y += GROUP_HEAD_H + 12
        for row in range(0, len(group), cols):
            chunk = group[row:row + cols]
            h = max(card_height(a, name_mode) for a in chunk)
            for i, account in enumerate(chunk):
                items.append(("card", "card:" + account["id"], left + i * (card_w + GAP), y, card_w, h, account))
            y += h + GAP
        y += 26 - GAP
    return items, y - 26 + PAD


# ---------- tiles ----------
class Tile:
    """A drawn part of the page: `image` (Pillow), or `ops` recorded for a platform's own painter
    with the device `size` it covers and its `shape` (a card's rounded body and shadow)."""

    def __init__(self, image, hits, margin=0, live=(), ops=None, size=None, shape=None):
        self.image, self.hits, self.margin = image, hits, margin
        self.live = live  # parts drawn each frame on top (bars, percentages): [(kind, key, x, y, w, ...)]
        self.ops, self.shape = ops, shape
        self.width, self.height = image.size if image is not None else size


def mixc(a, b, t):
    """Colour between a and b (RGB or RGBA) at t."""
    t = max(0.0, min(1.0, t))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def quantize(t):
    return round(t * 8) / 8  # hover fades redraw a card in 8 steps, not every frame


def card_key(account, ui, name_mode, live, locked):
    """Everything a card's look depends on, so a cached tile is reused until one of them changes."""
    aid = account["id"]
    editing = ui.editing if ui.editing and ui.editing[0] == aid else None
    fades = sorted((k, quantize(v)) for k, v in ui.fades.items() if k.endswith(":" + aid) and v > 0)
    return json.dumps([account, fades, ui.pending == aid, ui.confirm == aid, editing, aid in ui.revealed,
                       name_mode, live, locked, CLOCK_24, int(time.time() // 60), getattr(ui, "window", None),
                       getattr(ui, "window_number", None), getattr(ui, "window_account", None), getattr(ui, "per_window", False),
                       getattr(ui, "own_windows", 0)],
                      sort_keys=True, default=str)


def draw_card(account, w, h, scale, ui, name_mode, live, locked):
    """One account card: returns a Tile (RGBA with shadow margin) with hits relative to the card.
    Bars and percentages are left out: they animate, so the frame draws them (Tile.live)."""
    m = round(SHADOW * scale)
    body = Image.new("RGB", (round(w * scale), round(h * scale)), SURFACE)
    hits, live_parts, border, spent = card_content(Canvas(body, scale, SURFACE), account, w, h, ui, name_mode, live, locked)
    # Shape: rounded card, border (accent when in use, stronger on hover), shadow, dimmed when spent
    card = body.convert("RGBA")
    r, line = round(RADIUS * scale), max(1, round(scale))
    ring = ring_alpha(body.width, body.height, r, line, border[3])
    for box in edge_boxes(*body.size, r + line + 2):  # the ring is empty further in
        card.paste(border[:3], box, ring.crop(box))
    card.putalpha(rr_alpha(body.width, body.height, r, 184 if spent else 255))  # .72 when spent
    tile = shadow(w, h, scale).copy()
    over_shadow(tile, card, m, None if spent else r + 2)
    return Tile(tile, hits, m, [part + (spent,) for part in live_parts])


def edge_boxes(w, h, band):
    """The strips `band` px wide along the four edges of a w x h image (all of it when they meet)."""
    if 2 * band >= min(w, h):
        return [(0, 0, w, h)]
    return [(0, 0, w, band), (0, h - band, w, h), (0, band, band, h - band), (w - band, band, w, h - band)]


def over_shadow(tile, card, m, band):
    """tile.alpha_composite(card, (m, m)). With `band`, the card is opaque further in than that
    from its edges: only those strips blend, and the middle is copied straight across (the same
    pixels; tests compare them), which is most of a card's drawing time saved."""
    boxes = edge_boxes(*card.size, band) if band else [(0, 0) + card.size]
    for box in boxes:
        tile.alpha_composite(card, (m + box[0], m + box[1]), box)
    if len(boxes) > 1:
        middle = (band, band, card.width - band, card.height - band)
        tile.paste(card.crop(middle), (m + band, m + band))


def record_card(account, w, h, scale, ui, name_mode, live, locked):
    """draw_card as recorded drawing calls, for a platform's own painter (no pixels here)."""
    m = round(SHADOW * scale)
    c = Recorder(scale, SURFACE)
    hits, live_parts, border, spent = card_content(c, account, w, h, ui, name_mode, live, locked)
    return Tile(None, hits, m, [part + (spent,) for part in live_parts], ops=c.ops,
                size=(round(w * scale) + 2 * m, round(h * scale) + 2 * m), shape=("card", w, h, border, spent))


def card_content(c, account, w, h, ui, name_mode, live, locked):
    """What a card shows, drawn with canvas `c` (logical px, the card's top left at 0, 0) on its
    SURFACE body: (hits, live parts, border colour, spent)."""
    provider, aid = account["provider"], account["id"]
    accent = ACCENT[provider]
    active, eligible = account.get("active"), account.get("eligible", True)

    def a(action):
        return quantize(ui.fades.get(action + ":" + aid, 0.0))

    card_t = a("card")
    hits, live_parts = [], []

    def hit(x, y, bw, bh, action, cursor="hand"):
        hits.append(((x, y, bw, bh), action + ":" + aid, cursor))

    # Head: avatar, identity, renewal + badge
    x, y = 18, 16
    c.rect(x, y, 38, 38, 11, accent + (36,))
    c.image_at(x + 8, y + 8, provider, 22)
    ix = x + 38 + 12
    side_w = 0
    sub = subscription_text(account)
    renew = sub or ("Set renewal date" if live else "")
    badge = "In use" if active else ("Limit reached" if not eligible else "")
    for value, size in ((renew, 11.5), (badge, 12)):
        if value:
            side_w = max(side_w, text_w(value, size, size == 12) + (13 if value == badge else 0))
    name_w = w - ix - 18 - (side_w + 12 if side_w else 0)
    iy = y + 15
    if name_mode:
        editing = ui.editing if ui.editing and ui.editing[0] == aid else None
        label = account.get("label") or ""
        box_w = min(260, name_w + 6)
        if editing:
            c.rect(ix - 6, iy - 16, box_w, 24, 6, SURFACE_2 + (255,))
            c.outline(ix - 6, iy - 16, box_w, 24, 6, FOCUS + (255,))
            text, caret = editing[1], editing[2]
            if editing[3] and text:  # all selected
                c.rect(ix, iy - 12, text_w(text, 15, True), 17, 3, FOCUS + (90,))
            c.text(ix, iy, text, 15, TEXT, True, bg=SURFACE_2)
            c.rect(ix + text_w(text[:caret], 15, True), iy - 13, 1.2, 17, 0, TEXT + (255,))
        else:
            if a("name"):
                c.outline(ix - 6, iy - 16, box_w, 24, 6, LINE_STRONG[:3] + (round(LINE_STRONG[3] * a("name")),))
            if label:
                c.text(ix, iy, fit(label, 15, True, name_w), 15, TEXT, True)
            else:
                c.text(ix, iy, "Name this account", 15, MUTED)
        hit(ix - 6, iy - 16, box_w, 24, "name", "text")
        iy += 18
        email = account.get("email") or ""
        shown = email if aid in ui.revealed else redact(email)
        if shown:
            c.text(ix, iy, fit(shown, 12, False, name_w), 12, mixc(MUTED, TEXT, a("email")))
            hit(ix, iy - 12, min(name_w, text_w(shown, 12) + 4), 16, "email")
    else:
        c.text(ix, iy, fit(display_name(account), 15, True, name_w), 15, TEXT, True)
    if account.get("plan"):
        py = iy + 6
        pw = text_w(account["plan"], 11, True) + 14
        c.rect(ix, py, pw, 17, 6, accent + (36,))
        c.text(ix + 7, py + 12.5, account["plan"], 11, accent, True, bg=over(SURFACE, accent + (36,)))
    right = w - 18
    if renew:
        rcolor = WARN if sub and fr.ends_soon(account.get("subscription")) else (MUTED if sub else FAINT)
        t = a("renew") if live else 0
        rw = text_w(renew, 11.5)
        rbg = mixc(SURFACE, SURFACE_3, t)
        if t:
            c.rect(right - rw - 4, y + 1, rw + 8, 17, 5, rbg + (255,))
        c.text(right, y + 14, renew, 11.5, mixc(rcolor, TEXT, t), anchor="rs", bg=rbg)
        if live:
            hit(right - rw - 4, y + 1, rw + 8, 17, "renew")
    if badge:
        bcolor = GOOD if active else BAD
        c.text(right, y + 33, badge, 12, bcolor, True, anchor="rs")
        c.dot(right - text_w(badge, 12, True) - 8, y + 29, 3.5, bcolor)

    # Usage windows: labels, tracks and reset times here; the fills and percentages animate (live)
    y = 16 + identity_height(account, name_mode) + 14
    windows = account.get("windows") or []
    if not windows:
        c.text(18, y + 18, "Usage not loaded yet", 12.5, FAINT)
        y += 28
    for win in windows:
        left = remaining(win.get("used", 0))
        c.text(18, y + 14, win.get("label", ""), 13, TEXT)
        c.text(w - 18, y + 14, " left", 12, MUTED, anchor="rs")
        live_parts.append(("pct", (aid, win["key"]), w - 18 - text_w(" left", 12), y + 14, left))
        c.rect(18, y + 24, w - 36, 6, 3, TRACK)
        live_parts.append(("bar", (aid, win["key"]), 18, y + 24, w - 36, left))
        c.text(18, y + 46, reset_text(win.get("resetsAt"), not win.get("used", 0)), 11.5, FAINT)
        y += 50 + 11
    if windows:
        y -= 11
    items = credits_items(account)
    if items:
        y += 11
        cx = 18
        for i, (label, value) in enumerate(items):
            if i == len(items) - 1 and len(items) > 1:  # the last one sits at the right
                cx = w - 18 - text_w(label + ": ", 12) - text_w(value, 12)
            c.text(cx, y + 13, label + ": ", 12, MUTED)
            c.text(cx + text_w(label + ": ", 12), y + 13, value, 12, TEXT)
            cx += text_w(label + ": " + value, 12) + 12

    # Foot: hint on the left, Remove and the swap button on the right (web .button: 32 tall, radius 8)
    fy = h - 14 - 32
    c.line(18, fy - 13, w - 36, LINE)
    switching = ui.pending == aid
    if ui.confirm == aid:
        c.text(18, fy + 21, "Remove this account?", 12.5, TEXT)
        bx = w - 18 - 86
        t = a("remove-yes")
        fill = mixc(BAD, blend((255, 255, 255), BAD, .1), t)
        c.rect(bx, fy, 86, 32, 8, fill + (255,))
        c.text(bx + 43, fy + 16, "Remove", 13, ON_ACCENT, True, anchor="mm", bg=fill)
        hit(bx, fy, 86, 32, "remove-yes")
        bx -= 6 + 80
        t = a("remove-no")
        quiet_button(c, bx, fy, 80, "Cancel", t)
        hit(bx, fy, 80, 32, "remove-no")
    else:
        status = account.get("status") or ""
        relogin = live and any(s in status.lower() for s in ("sign in", "expired", "missing"))
        # can open a window of its own (only offered while "Separate accounts per window" is on);
        # not while its login has expired (that needs signing in first) or its limit is reached
        own = (live and provider == "claude" and getattr(ui, "per_window", False) and not active
               and not account.get("pinned") and not relogin and eligible)
        # a window picked in the Windows list: the button gives it this account ("Use in Window 2"), any
        # but the one it is on (the main account's card puts it back on the main account)
        number = getattr(ui, "window_number", None)
        now_on = getattr(ui, "window_account", None)
        pick = (getattr(ui, "window", None) and provider == "claude" and eligible and not relogin
                and account["id"] != now_on and not (active and now_on is None))
        pick = (f"Use in Window {number}" if number else "Use in window") if pick else None
        bw = max(124, text_w(pick, 13, True) + 24) if pick else 124
        if relogin:
            signing = provider in (ui.signing_in or ())
            label = "Login expired · Sign in again"
            color = FAINT if signing else mixc(MUTED, TEXT, 0.6 + 0.4 * a("relogin"))
            c.text(18, fy + 21, label, 12, color)
            c.line(18, fy + 23, text_w(label, 12), color + (160,))
            if not signing:
                hit(18, fy + 4, text_w(label, 12), 22, "relogin")
        else:
            age = time.time() - (account.get("updated_at") or 0)
            old = live and account.get("updated_at") and age > STALE_AFTER
            if status or old:
                # the whole sentence when there is room, else shorter ones: never cut off mid-word
                hints = [status] if status else [f"Numbers from {ago(age)} ago · checking", f"{ago(age)} ago · checking", f"{ago(age)} old"]
            else:  # what the account is doing, and how old its numbers are
                main = "Shared by other windows" if getattr(ui, "own_windows", 0) else "Used by all sessions"
                if account.get("signed_out"):
                    main = f"{'Claude Code' if provider == 'claude' else provider.title()} is signed out · swap to put it back"
                parts = [main] if active else [] if eligible else ["Waiting for reset"]
                updated = (f"Updated {ago(age)} ago" if age >= 60 else "Updated now") if live and account.get("updated_at") else None
                hints = [" · ".join(parts + [updated] if updated else parts)]
                if updated:
                    hints += [" · ".join(parts + [updated.replace("Updated ", "")]), updated]
            room = w - 36 - bw - (0 if active else 96)  # the in-use card has no Remove button to leave room for
            hint = best_fit(hints, 12, room)
            color = WARN if status and not account.get("pinned") else FAINT
            if own and text_w(hint, 12) > room - 96:  # "New window" fades in over it on hover: the hint fades out
                color = mixc(color, SURFACE, card_t)
            c.text(18, fy + 21, hint, 12, color)
        bx = w - 18 - bw
        pinned = account.get("pinned")
        if pick:  # a window is picked (separate accounts per window): this account goes to that window only
            t = 0.0 if locked else a("swapWindow")
            fill = mixc(accent, blend((255, 255, 255), accent, .1), t)
            c.rect(bx, fy - t, bw, 32, 8, fill + (255,))
            c.text(bx + bw / 2, fy + 16 - t, pick, 13, ON_ACCENT, True, anchor="mm", bg=fill)
            if not locked:
                hit(bx, fy, bw, 32, "swapWindow")
        elif active and not switching and not account.get("signed_out"):
            c.text(bx + bw / 2, fy + 16, "In use", 13, accent, True, anchor="mm")
        elif pinned:  # a window has it as its own (another window can have it too, from the Windows list)
            c.text(bx + bw / 2, fy + 16, f"In Window {account.get('window') or '?'}", 13, accent, True, anchor="mm")
        elif switching:
            c.rect(bx, fy, bw, 32, 8, accent + (230,))
            c.text(bx + bw / 2, fy + 16, "Switching…", 13, ON_ACCENT, True, anchor="mm", bg=over(SURFACE, accent + (230,)))
        elif not eligible:
            c.rect(bx, fy, bw, 32, 8, SURFACE_2 + (102,))
            c.outline(bx, fy, bw, 32, 8, LINE_STRONG[:3] + (40,))
            c.text(bx + bw / 2, fy + 16, "Limit reached", 13, blend(TEXT, SURFACE, .4), anchor="mm")
        else:
            t = 0.0 if locked else a("swap")
            fill = blend(accent, SURFACE, .45) if locked else mixc(accent, blend((255, 255, 255), accent, .1), t)
            lift = t  # the web button rises 1 px on hover
            c.rect(bx, fy - lift, bw, 32, 8, fill + (255,))
            c.text(bx + bw / 2, fy + 16 - lift, "Swap to this", 13, ON_ACCENT, True, anchor="mm", bg=fill)
            if not locked:
                hit(bx, fy, bw, 32, "swap")
        if live and not active and not switching and card_t > 0:  # Remove fades in while the card is hovered
            rx = bx - 6 - 80
            if not pinned:
                quiet_button(c, rx, fy, 80, "Remove", a("remove"), card_t)
                if not locked and card_t >= .5:
                    hit(rx, fy, 80, 32, "remove")
                if own:  # a new Claude Code window that keeps this account
                    rx -= 6 + 90
                    quiet_button(c, rx, fy, 90, "New window", a("openWindow"), card_t)
                    if not locked and card_t >= .5:
                        hit(rx, fy, 90, 32, "openWindow")

    border = accent + (115,) if active else mixc(LINE, LINE_STRONG, card_t)
    return hits, live_parts, border, not eligible and not active


def quiet_button(c, x, y, w, label, hover, visible=1.0):
    """The web's .button.quiet: no fill until hovered, muted text that lightens."""
    bg = mixc(SURFACE, SURFACE_3, hover)
    if hover:
        c.rect(x, y, w, 32, 8, SURFACE_3 + (round(255 * hover * visible),))
    color = mixc(SURFACE, mixc(MUTED, TEXT, hover), visible)
    c.text(x + w / 2, y + 16, label, 13, color, anchor="mm", bg=mixc(SURFACE, bg, visible))


def button(c, x, y, w, hover, active=False):
    """The web's .button: surface-2, a strong hairline, surface-3 when hovered."""
    base = mixc(SURFACE_2, SURFACE_3, max(hover, 1.0 if active else 0.0))
    c.rect(x, y, w, 32, 8, base + (255,))
    c.outline(x, y, w, 32, 8, LINE_STRONG)
    return base


def draw_topbar(state, w, scale, ui):
    image = Image.new("RGBA", (round(w * scale), round(TOPBAR_H * scale)), (0, 0, 0, 0))
    return Tile(image, topbar_content(Canvas(image, scale, BG), state, w, ui))


def record_topbar(state, w, scale, ui):
    c = Recorder(scale, BG)
    return Tile(None, topbar_content(c, state, w, ui), ops=c.ops, size=(round(w * scale), round(TOPBAR_H * scale)))


def topbar_content(c, state, w, ui):
    """The header, drawn with canvas `c` over the page background: its hits."""
    hits = []
    f = lambda key: quantize(ui.fades.get(key, 0.0))
    c.mark(0, 11, 44, 12)
    c.text(58, 30, "LimitSwitcher", 22, TEXT, True)
    c.text(58, 51, "Every Claude and Codex limit, at a glance.", 13, MUTED)
    live = state.get("mode") == "live"
    x = w
    # Refresh (the web .icon-button: 36 square, surface, a faint hairline; the arrow spins once when clicked)
    x -= 36
    t = f("refresh")
    c.rect(x, 15, 36, 36, 9, mixc(SURFACE, SURFACE_3, t) + (255,))
    c.outline(x, 15, 36, 36, 9, LINE)
    turn = ui.fades.get("spin", 0.0)
    c.spin_glyph("refresh", x + 18, 33, 18, mixc(MUTED, TEXT, t), 360 * (1 - (1 - turn) ** 3) if 0 < turn < 1 else 0)
    hits.append(((x, 15, 36, 36), "refresh", "hand"))
    # Add account: padding 14, a plus, gap 8, the label, padding 14
    if live:
        label = "Add account"
        bw = 14 + 12 + 8 + text_w(label, 13) + 14
        x -= 10 + bw
        base = button(c, x, 17, bw, f("add"), ui.menu == "add")
        c.glyph("plus", x + 14 + 6, 33, 13, TEXT)
        c.text(x + 14 + 12 + 8, 33, label, 13, TEXT, anchor="lm", bg=base)
        hits.append(((x, 17, bw, 32), "add", "hand"))
        ui.anchors["add"] = (x, bw)
    # Settings, with the automation state pill and a caret
    busy = state.get("busy")
    pill = "Working…" if busy else "Auto resume" if state.get("afk") else "Auto swap" if state.get("autoSwap") else "Manual"
    pill_bg, pill_fg = ((229, 181, 74, 38), WARN) if busy else ((76, 195, 138, 36), GOOD) if (state.get("afk") or state.get("autoSwap")) else (SURFACE_3 + (255,), MUTED)
    pw = text_w(pill, 11, True) + 18
    bw = 14 + text_w("Settings", 13) + 8 + pw + 8 + 12 + 14
    x -= 10 + bw
    base = button(c, x, 17, bw, f("settings"), ui.menu == "settings")
    c.text(x + 14, 33, "Settings", 13, TEXT, anchor="lm", bg=base)
    px_ = x + 14 + text_w("Settings", 13) + 8
    c.rect(px_, 24, pw, 18, 9, pill_bg)
    c.text(px_ + pw / 2, 33, pill, 11, pill_fg, True, anchor="mm", bg=over(base, pill_bg))
    c.glyph("caret", x + bw - 14 - 6, 33, 13, MUTED)
    hits.append(((x, 17, bw, 32), "settings", "hand"))
    ui.anchors["settings"] = (x, bw)
    # The version above the update button, outside the menu so they are seen: left of Settings
    update = state.get("update") or {}
    version = "Version " + str(update.get("current") or "")
    room = x - 10 - (58 + text_w("Every Claude and Codex limit, at a glance.", 13)) - 14
    label, action, primary = next(((l, a_, p_) for l, a_, p_ in update_buttons(update)
                                   if text_w(l, 12, p_) + 24 <= room), update_buttons(update)[-1])
    bw = max(text_w(label, 12, primary) + 24, text_w(version, 11) + 8)
    if bw > room and not primary:
        return hits  # a window too narrow for it: the title keeps the room
    x -= 10 + bw
    hot = quantize(ui.fades.get(action, 0.0)) if action else 0.0
    c.text(x + bw / 2, 16, version, 11, FAINT, anchor="mm", bg=BG)
    bh, by = 24, 25  # its bottom lines up with the Settings button's (49)
    if primary:
        fill = mixc(GOOD, blend((255, 255, 255), GOOD, .1), hot)
        c.rect(x, by, bw, bh, 7, fill + (255,))
        c.text(x + bw / 2, by + bh / 2, label, 12, ON_ACCENT, True, anchor="mm", bg=fill)
    else:
        base = mixc(SURFACE, SURFACE_3, hot)
        c.rect(x, by, bw, bh, 7, SURFACE_2 + (255,))
        c.outline(x, by, bw, bh, 7, LINE_STRONG)
        c.text(x + bw / 2, by + bh / 2, label, 12, mixc(MUTED, TEXT, hot) if action else MUTED, anchor="mm", bg=SURFACE_2)
    if action:
        hits.append(((x, by, bw, bh), action, "hand"))
    return hits


def update_buttons(update):
    """[(label, action, primary)] for the update button in the top bar, longest first: the first that
    fits the room is used."""
    if update.get("installing"):
        return [("Updating…", None, True)]
    if update.get("available"):
        return [(f"Update to {update.get('latest')}", "update:install", True)]
    if update.get("checking"):
        return [("Checking…", None, False)]
    if update.get("error"):
        return [("Check failed · Retry", "update:check", False), ("Retry", "update:check", False)]
    if update.get("latest"):
        return [("Up to date · Check again", "update:check", False), ("Up to date", "update:check", False)]
    return [("Check for updates", "update:check", False), ("Check", "update:check", False)]


def draw_group(data, w, scale):
    image = Image.new("RGBA", (round(w * scale), round(GROUP_HEAD_H * scale)), (0, 0, 0, 0))
    group_content(Canvas(image, scale, BG), data, w)
    return Tile(image, [])


def record_group(data, w, scale):
    c = Recorder(scale, BG)
    group_content(c, data, w)
    return Tile(None, [], ops=c.ops, size=(round(w * scale), round(GROUP_HEAD_H * scale)))


def group_content(c, data, w):
    provider, title, caption, count = data
    c.image_at(2, 7, provider, 20)
    c.text(32, 22, title, 15, ACCENT[provider], True)
    tx = 32 + text_w(title, 15, True) + 10
    c.text(tx, 22, f"{count} account{'s' if count != 1 else ''}", 12, MUTED)
    if caption:
        c.text(w - 2, 22, caption, 12, FAINT, anchor="rs")


WINDOW_ROW_H = 40


def windows_height(windows, lines=1):
    if not windows:  # none yet: a box saying how to get one
        return GROUP_HEAD_H + 12 + max(WINDOW_ROW_H, 22 + 18 * lines)
    return GROUP_HEAD_H + 12 + len(windows) * (WINDOW_ROW_H + 8) - 8


def no_window_lines(state, w):
    """With the setting on and no window yet: what to do (or why a new window would share the main account),
    wrapped to the Windows list's width w."""
    if state.get("windows"):
        return []
    if free_for_window(state):
        text = "Open a new terminal and run claude: it starts on an account no other window is using, and shows up here."
    else:
        text = "Open a new terminal and run claude: it starts on the main account (no other is free) and shows up here."
    return wrap(text, 12.5, w - 32)


def free_for_window(state):
    """How many Claude accounts a new window could start on: used nowhere else, under their limits."""
    return sum(1 for a in state.get("accounts") or [] if a["provider"] == "claude" and not a.get("active")
               and not a.get("pinned") and a.get("eligible", True) and not a.get("status"))


def draw_windows(state, w, h, scale, ui):
    image = Image.new("RGBA", (round(w * scale), round(h * scale)), (0, 0, 0, 0))
    return Tile(image, windows_content(Canvas(image, scale, BG), state, w, ui))


def record_windows(state, w, h, scale, ui):
    c = Recorder(scale, BG)
    return Tile(None, windows_content(c, state, w, ui), ops=c.ops, size=(round(w * scale), round(h * scale)))


def folder_name(cwd):
    """A window's folder as its last part ("~" for the home folder, not the user's name)."""
    path = (cwd or "").replace("\\", "/").rstrip("/")
    if not path:
        return ""
    if path.lower() == str(Path.home()).replace("\\", "/").rstrip("/").lower():
        return "~"
    return path.rsplit("/", 1)[-1] or path


def windows_content(c, state, w, ui):
    """Separate accounts per window: each open window with its account. A click picks a window (the app
    points it out on screen); the account cards then offer "Use in Window N" for that window alone.
    With the setting on and no window yet, one line says how to get one."""
    windows = state.get("windows") or []
    accounts = {a["id"]: a for a in state.get("accounts") or []}
    picked = getattr(ui, "window", None)
    accent = ACCENT["claude"]
    c.text(2, 22, "Windows", 15, TEXT, True)
    if windows:
        n = len(windows)
        # the setting is off: these are left from when it was on; new ones share the main account
        own = sum(1 for window in windows if window.get("accountId"))
        caption = (f"{own} of {n} on {'its' if own == 1 else 'their'} own account" if own
                   else f"{n} on the main account") + ("" if state.get("perWindow") else " · the setting is off")
    else:
        caption = "Each new Claude Code terminal gets its own account"
    c.text(2 + text_w("Windows", 15, True) + 10, 22, caption, 12, MUTED)
    if windows:
        number = getattr(ui, "window_number", None)
        hint = f"Now pick an account for Window {number} below" if picked else "Click a window to change its account"
        c.text(w - 2, 22, hint, 12, accent if picked else FAINT, anchor="rs")
    hits = []
    y = GROUP_HEAD_H + 12
    if not windows:  # the setting is on, nothing open yet: what to do, or why it can't happen
        lines = no_window_lines(state, w)
        box_h = max(WINDOW_ROW_H, 22 + 18 * len(lines))
        c.outline(0, y, w, box_h, 9, LINE_STRONG)
        top = y + box_h / 2 - 9 * (len(lines) - 1)
        for i, line in enumerate(lines):
            c.text(16, top + 18 * i, line, 12.5, MUTED, anchor="lm")
        return hits
    for window in windows:
        action = "window:" + window["id"]
        on = picked == window["id"]
        hot = quantize(ui.fades.get(action, 0.0))
        base = mixc(SURFACE, SURFACE_2, max(hot, 1.0 if on else 0.0))
        c.rect(0, y, w, WINDOW_ROW_H, 9, base + (255,))
        c.outline(0, y, w, WINDOW_ROW_H, 9, (accent + (200,)) if on else mixc(LINE, LINE_STRONG, hot))
        mid = y + WINDOW_ROW_H / 2
        # the session's title (its name from /rename, Claude Code's title, or its first prompt) once it
        # has one; then "Window N" (what the account cards call it), its folder and model
        number = f"Window {window['number']}"
        account = accounts.get(window.get("accountId"))
        who = display_name(account) if account else "Main account"
        who_w = text_w(who, 13, True)
        title = fit(window["title"], 13, True, (w - who_w) * .5) if window.get("title") else number
        c.text(16, mid, title, 13, TEXT, True, anchor="lm", bg=base)
        detail = " · ".join(p for p in (number if window.get("title") else None, folder_name(window.get("cwd")),
                                          window.get("model")) if p)
        if detail:
            c.text(16 + text_w(title, 13, True) + 10, mid, fit(detail, 12, False, w - 60 - text_w(title, 13, True) - who_w),
                   12, MUTED, anchor="lm", bg=base)
        c.text(w - 16, mid, who, 13, accent if account else FAINT, True, anchor="rm", bg=base)
        hits.append(((0, y, w, WINDOW_ROW_H), action, "hand"))
        y += WINDOW_ROW_H + 8
    return hits


EMPTY_H = 170


def draw_empty(state, w, scale, ui):
    image = Image.new("RGBA", (round(w * scale), round(EMPTY_H * scale)), (0, 0, 0, 0))
    return Tile(image, empty_content(Canvas(image, scale, BG), state, w, ui))


def record_empty(state, w, scale, ui):
    c = Recorder(scale, BG)
    return Tile(None, empty_content(c, state, w, ui), ops=c.ops, size=(round(w * scale), round(EMPTY_H * scale)))


def empty_content(c, state, w, ui):
    h = EMPTY_H
    c.outline(0, 0, w, h, RADIUS, LINE_STRONG)
    live = state.get("mode") == "live"
    c.text(w / 2, 58, "No accounts yet", 16, TEXT, True, anchor="ms")
    c.text(w / 2, 84, "Sign in to Claude Code or Codex as usual and the account appears here automatically, or add one now."
           if live else "No sample accounts.", 13, MUTED, anchor="ms")
    hits = []
    if live:
        widths = [text_w(f"Add {t} account", 13) + 28 for _, t, _ in PROVIDERS]
        x = w / 2 - (sum(widths) + 10) / 2
        for (provider, title, _), bw in zip(PROVIDERS, widths):
            hot = ui.hover == "add:" + provider
            c.rect(x, 108, bw, 32, 8, (SURFACE_3 if hot else SURFACE_2) + (255,))
            c.outline(x, 108, bw, 32, 8, LINE_STRONG)
            c.text(x + bw / 2, 124, f"Add {title} account", 13, TEXT, anchor="mm", bg=SURFACE_3 if hot else SURFACE_2)
            hits.append(((x, 108, bw, 32), "add:" + provider, "hand"))
            x += bw + 10
    return hits


@lru_cache(maxsize=1)  # only the window's current size (about 5 MB at 150%)
def backdrop(width, height, scale):
    """The window background: plain."""
    return Image.new("RGB", (round(width * scale), round(height * scale)), BG)


def release():
    """The full view closed: let go of everything drawn for it (shapes, shadows, the backdrop).
    It is all drawn again, the same, the next time it opens."""
    for cached in (rr_mask, ring_mask, _rr_alpha, _ring_alpha, glyph, shadow, backdrop):
        cached.cache_clear()


# ---------- overlays: menus, the date editor, toasts ----------
class Surface:
    """Where overlays draw: the whole frame (Pillow here; macOS has its own with the same calls).
    With `base` (the page the frame was made from, the same pixels until an overlay draws), a
    fade needs no copy of the frame, and `boxes` says which device areas the overlays drew in."""

    def __init__(self, image, scale, base=None):
        self.image, self.s, self.base = image, scale, base
        self.boxes = []

    def canvas(self, bg):
        return Canvas(self.image, self.s, bg)

    def shadow(self, x, y, w, h, strength):
        """A floating surface's shadow (logical box), 4 px lower. Every overlay starts with one
        (panel()), and draws within it."""
        s = self.s
        sh = shadow(w, h, s, strength)
        x0, y0 = round(x * s) - round(SHADOW * s), round(y * s) - round(SHADOW * s) + round(4 * s)
        self.image.paste(sh, (x0, y0), sh)
        m = round(8 * s)  # and a little more, for anything drawn along its edge
        self.boxes.append((min(x0, round(x * s)) - m, min(y0, round(y * s)) - m,
                           max(x0 + sh.width, round((x + w) * s)) + m, max(y0 + sh.height, round((y + h) * s)) + m))

    def fade_begin(self, t):
        """Start drawing something that shows at opacity t (0..1); fade_end finishes it."""
        if self.base is None:
            return self.image.copy()
        return [(box, self.image.crop(box)) for box in self.boxes]  # where it differs from base so far

    def fade_end(self, before, box, t):
        """What was drawn since fade_begin over `box` (logical, plus its shadow) at opacity t."""
        m = SHADOW + 8
        x, y, w, h = box
        region = tuple(round(v * self.s) for v in (x - m, y - m, x + w + m, y + h + m))
        region = (max(0, region[0]), max(0, region[1]), min(self.image.width, region[2]), min(self.image.height, region[3]))
        if region[2] > region[0] and region[3] > region[1]:
            if isinstance(before, list):  # the page, with what overlays drew before this one
                under = self.base.crop(region)
                for (x0, y0, _, _), part in before:
                    under.paste(part, (x0 - region[0], y0 - region[1]))
            else:
                under = before.crop(region)
            self.image.paste(Image.blend(under, self.image.crop(region), max(0.0, min(1.0, t))), region[:2])
            self.boxes.append(region)


def panel(surface, scale, x, y, w, h):
    """A floating surface (menus, editor) with its shadow, drawn straight onto the frame."""
    surface.shadow(x, y, w, h, 1.4)
    c = surface.canvas(SURFACE_3)
    c.rect(x, y, w, h, 10, SURFACE_3 + (255,))
    c.outline(x, y, w, h, 10, LINE_STRONG)
    return c


def toggle(c, x, y, pos, hot, bg, small=False):
    """The web switch, 40x22 (a sub-setting's: 32x18): pos 0 (off) to 1 (on) as it slides, hot when hovered."""
    w, h = (32, 18) if small else (40, 22)
    track = mixc(bg, GOOD, pos)
    if pos > 0:
        c.rect(x, y, w, h, h / 2, (blend((255, 255, 255), track, .08) if hot and pos == 1 else track) + (255,))
    if pos < 1:
        c.outline(x, y, w, h, h / 2, mixc(MUTED if hot else FAINT, GOOD, pos) + (255,), 1.5)
    r = (h / 2 - 4 if hot else h / 2 - 4.5)
    c.dot(x + h / 2 - 0.5 + (w - h) * pos, y + h / 2, r, mixc(MUTED, (255, 255, 255), pos))


SETTINGS = (("autoSwap", "Auto swap", "Move to the account whose weekly resets first"),
            ("afk", "Auto resume", "Continue the session on another account"),
            ("afkAll", "Continue every session", "Including large sessions (400k+), automatically"),
            ("waitNearReset", "Wait for a near reset", "No switch when the 5-hour limit resets within 15 min"),
            ("resetAlerts", "Reset alerts", "A toast in Claude Code once a limit frees up"),
            ("nameMode", "Name mode", "Names instead of emails, for screen sharing"),
            ("clock24", "24-hour clock", "Reset times like 14:30 instead of 2:30 PM"))

SUB_SETTINGS = {"afkAll"}  # Auto resume's own: drawn set in under it
SUB_INDENT = 20

PER_WINDOW = ("perWindow", "Separate accounts per window",
              "Each new Claude Code terminal starts on an account no other window is using, and any window can be "
              "moved to another. Everything else stays your usual Claude Code.")
ROW_PAD = 26  # a setting's row: its name, plus 16 per line of description
SLOT_ROW_H = 36  # a display's line in the settings: its name, then its two taskbar slots beside it
MOD_NAME = "Claude Code Status mod"
SETTINGS_W = 400  # wide, so the descriptions take one or two lines and the menu stays short
MOD_TEXT_W = 250  # beside the button
MOD_TEXT = {"active": ("Active · feeding usage live", GOOD), "update": ("Update to add Jev compaction", WARN), "installed": ("Installed · run /reload-plugins in an open session", WARN),
            "installing": ("Installing…", WARN), "missing": ("Not installed", MUTED), "unknown": ("Not installed", MUTED)}


def mod_row(state):
    """(text lines, color, height) of the Claude Code mod's line in Settings: its name, its state (wrapped,
    never cut off) and its button beside them."""
    mod = state.get("mod") or {}
    text, color = MOD_TEXT.get(mod.get("status") or "unknown") or (mod.get("text") or "Couldn't install", BAD)
    lines = wrap(text, 12, MOD_TEXT_W)[:3]
    return lines, color, 42 + 16 * len(lines)


KEY_ROW_H = 40
JEV_ROW = ("jevCompact", "Jev compaction", "Shrink a swapped session for the new account")


def jev_key_field(c, ui, state, x, y, w, hits):
    """The OpenRouter key under Jev compaction. With no key: a field to paste one into (what is typed
    shows only as dots). With one saved: "Key set" and a Remove button that takes two clicks (the
    second says Confirm), so a saved key is never shown, nor typed over. A key set in the
    environment or Claude Code's settings is named but left to whoever set it."""
    editing = ui.editing if ui.editing and ui.editing[0] == "jevkey" else None
    source = state.get("jevKey")
    mid = y + 14
    if source in ("env", "settings") and not editing:
        c.text(x, mid, "Key set in " + ("the environment" if source == "env" else "Claude Code's settings"), 12, MUTED, anchor="lm")
        return
    if source == "file" and not editing:
        c.text(x, mid, "Key set", 12, GOOD, anchor="lm")
        confirm = ui.jev_confirm
        label = "Confirm" if confirm else "Remove"
        bw = text_w(label, 12, confirm) + 24
        bx = x + w - bw
        hot = ui.hover == "jevkey-clear:"
        base = SURFACE_2 if not hot else blend((255, 255, 255), SURFACE_2, .06)
        c.rect(bx, y, bw, 28, 7, base + (255,))
        c.outline(bx, y, bw, 28, 7, (BAD + (255,)) if confirm else LINE_STRONG)
        c.text(bx + bw / 2, mid, label, 12, BAD if confirm else TEXT, confirm, anchor="mm", bg=base)
        hits.append(((bx, y, bw, 28), "jevkey-clear:", "hand"))
        return
    hot = ui.hover == "name:jevkey"
    c.rect(x, y, w, 28, 7, SURFACE_2 + (255,))
    c.outline(x, y, w, 28, 7, FOCUS + (255,) if editing else LINE_STRONG)
    dots = min(len(editing[1]) if editing else 0, int((w - 24) // 8))  # a long key shows as a full row of dots
    for i in range(dots):
        c.dot(x + 12 + 8 * i, mid, 2.4, TEXT)
    if editing:
        caret = min(editing[2], dots)
        c.rect(x + 10 + 8 * caret, mid - 8, 1.2, 16, 0, TEXT + (255,))
    else:
        c.text(x + 10, mid, "OpenRouter key", 12, MUTED if hot else FAINT, anchor="lm", bg=SURFACE_2)
    hits.append(((x, y, w, 28), "name:jevkey", "text"))


def settings_menu(image, scale, state, ui, x, y, prefs):
    w = SETTINGS_W
    rows = list(SETTINGS)
    if state.get("mode") == "live":
        rows.append(PER_WINDOW)
    if state.get("mode") == "live" and sys.platform in ("win32", "darwin"):
        rows.append(("launchAtLogin", "Launch with " + ("macOS" if sys.platform == "darwin" else "Windows"),
                     "Start in the tray at sign-in"))
    taskbar = bool(state.get("taskbarAvailable"))
    if taskbar:
        rows.append(("taskbar", "Taskbar view", "The accounts in use, on the taskbar"))
    displays = state.get("taskbarDisplays") or []
    chooser = taskbar and state.get("taskbar") and bool(displays)
    live_mod = state.get("mode") == "live"
    wrapped = [wrap(desc, 12, w - 80 - (SUB_INDENT if key in SUB_SETTINGS else 0)) for key, _, desc in rows]
    jev_lines = wrap(JEV_ROW[2], 12, w - 80)
    key_h = KEY_ROW_H if state.get("jevCompact") else 0  # the OpenRouter key's field, under Jev compaction
    jev_h = ROW_PAD + 16 * len(jev_lines) + key_h if live_mod else 0  # Jev compaction sits below the mod it belongs to
    h = 16 + sum(ROW_PAD + 16 * len(lines) for lines in wrapped) + (13 if taskbar else 0) \
        + (SLOT_ROW_H * len(displays) + 6 if chooser else 0) + 8 + (mod_row(state)[2] + jev_h if live_mod else 0)
    c = panel(image, scale, x, y, w, h)
    hits = []

    def toggle_row(key, title, lines, ry):
        """One setting's switch, name and description; returns its height."""
        row_h = ROW_PAD + 16 * len(lines)
        on = prefs.get(key, bool(state.get(key)))
        locked = state.get("busy") and key in ("autoSwap", "afk")
        hot = ui.hover == "set:" + key and not locked
        dx = SUB_INDENT if key in SUB_SETTINGS else 0  # a setting of the one above it: set in under it
        sub = key in SUB_SETTINGS
        if sub:  # a smaller switch, its name a size down
            toggle(c, x + 22 + dx, ry + 9, ui.fades.get("tog:" + key, 1.0 if on else 0.0), hot, SURFACE_3, small=True)
        else:
            toggle(c, x + 14 + dx, ry + 6, ui.fades.get("tog:" + key, 1.0 if on else 0.0), hot, SURFACE_3)
        c.text(x + 66 + dx, ry + 20, title, 13 if sub else 14, TEXT if not locked else MUTED, True)
        for i, line in enumerate(lines):
            c.text(x + 66 + dx, ry + 36 + 16 * i, line, 12, MUTED)
        if not locked:
            hits.append(((x + 8, ry + 2, w - 16, row_h - 4), "set:" + key, "hand"))
        return row_h

    ry = y + 8
    for (key, title, _), lines in zip(rows, wrapped):
        if key == "taskbar":
            c.line(x + 14, ry + 2, w - 28, LINE)
            ry += 13
        ry += toggle_row(key, title, lines, ry)
    if chooser:  # per display, what its taskbar shows: two slots (left, right); a click cycles each
        from . import taskbar_layout
        layout = taskbar_layout.layout(state)
        bw = (w - 66 - 106 - 14 - 6) / 2
        for d in displays:
            name = "On " + d["label"][0].lower() + d["label"][1:]
            c.text(x + 66, ry + 18, fit(name, 12, False, 100), 12, MUTED, anchor="lm")
            slots = layout.get(d["id"]) or [None, None]
            for index in (0, 1):
                bx, by = x + 66 + 106 + index * (bw + 6), ry + 5
                action = f"slot:{d['id']}:{index}"
                hot = ui.hover == action
                on = slots[index] is not None
                base = SURFACE_2 if not hot else blend((255, 255, 255), SURFACE_2, .06)
                c.rect(bx, by, bw, 26, 7, base + (255,))
                c.outline(bx, by, bw, 26, 7, (GOOD + (150,)) if on else LINE_STRONG)
                label = fit(taskbar_layout.label(state, slots[index]), 12, on, bw - 14)
                c.text(bx + bw / 2, by + 13, label, 12, (TEXT if on else MUTED) if not hot else TEXT, on,
                       anchor="mm", bg=base)
                hits.append(((bx, by, bw, 26), action, "hand"))
            ry += SLOT_ROW_H
    if live_mod:  # the optional Claude Code mod: live usage from Claude Code, shown in a spot of its own
        lines, color, mod_h = mod_row(state)
        ry = y + h - 8 - jev_h - mod_h
        c.line(x + 14, ry + 2, w - 28, LINE)
        status = (state.get("mod") or {}).get("status") or "unknown"
        c.text(x + 16, ry + 20, MOD_NAME, 14, TEXT, True, anchor="lm")
        for i, line in enumerate(lines):
            c.text(x + 16, ry + 40 + 16 * i, line, 12, color, anchor="lm")
        if status != "installing":
            label = "Reinstall" if status in ("active", "installed") else "Update" if status == "update" else "Install"
            primary = status not in ("active", "installed")
            bw = text_w(label, 12, primary) + 24
            bx = x + w - 14 - bw
            hot = ui.hover == "mod:install"
            if primary:
                fill = GOOD if not hot else blend((255, 255, 255), GOOD, .1)
                c.rect(bx, ry + 15, bw, 28, 7, fill + (255,))
                c.text(bx + bw / 2, ry + 29, label, 12, ON_ACCENT, True, anchor="mm", bg=fill)
            else:
                base = SURFACE_2 if not hot else blend((255, 255, 255), SURFACE_2, .06)
                c.rect(bx, ry + 15, bw, 28, 7, base + (255,))
                c.outline(bx, ry + 15, bw, 28, 7, LINE_STRONG)
                c.text(bx + bw / 2, ry + 29, label, 12, TEXT, anchor="mm", bg=base)
            hits.append(((bx, ry + 15, bw, 28), "mod:install", "hand"))
        # Jev compaction is a part of the mod, off until it's switched on: under the mod's own row
        ry = y + h - 8 - jev_h
        c.line(x + 14, ry + 2, w - 28, LINE)
        ry += toggle_row(JEV_ROW[0], JEV_ROW[1], jev_lines, ry + 4) + 4
        if key_h:
            jev_key_field(c, ui, state, x + 66, ry - 2, w - 66 - 14, hits)
    return (x, y, w, h), hits


def add_menu(image, scale, ui, x, y):
    w, h = 260, 12 + 2 * 36
    c = panel(image, scale, x, y, w, h)
    hits = []
    for i, (provider, title, _) in enumerate(PROVIDERS):
        ry = y + 6 + i * 36
        hot = ui.hover == "add:" + provider
        if hot:
            c.rect(x + 6, ry, w - 12, 36, 6, (255, 255, 255, 15))
        c.image_at(x + 16, ry + 10, provider, 16)
        c.text(x + 42, ry + 18, f"{title} account", 13, TEXT, anchor="lm", bg=over(SURFACE_3, (255, 255, 255, 15)) if hot else SURFACE_3)
        hits.append(((x + 6, ry, w - 12, 36), "add:" + provider, "hand"))
    return (x, y, w, h), hits


def month_grid(year, month):
    """Weeks (Monday first) of day numbers, 0 for blanks."""
    import calendar
    return calendar.Calendar(0).monthdayscalendar(year, month)


def date_editor(image, scale, ui, x, y):
    """Renews / Ends, a month calendar, where the date came from, and the buttons."""
    ed = ui.editor
    weeks = month_grid(ed["year"], ed["month"])
    w = 280
    h = 12 + 28 + 10 + 30 + 22 + len(weeks) * 30 + 8 + 34 + 12 + 32 + 12
    c = panel(image, scale, x, y, w, h)
    hits = []
    hover = ui.hover
    ry = y + 12
    kx = x + 14
    for kind, label in (("renews", "Renews"), ("ends", "Ends (cancelled)")):
        on = ed["ends"] == (kind == "ends")
        hot = hover == "ed-kind:" + kind
        c.dot(kx + 8, ry + 14, 8, (GOOD if on else (MUTED if hot else FAINT)) + (255,))
        c.dot(kx + 8, ry + 14, 6.5 if not on else 3.5, SURFACE_3 + (255,) if not on else (255, 255, 255))
        c.text(kx + 22, ry + 14, label, 13, TEXT, anchor="lm")
        bw = 22 + text_w(label, 13) + 6
        hits.append(((kx - 4, ry, bw + 8, 28), "ed-kind:" + kind, "hand"))
        kx += bw + 18
    ry += 28 + 10
    # Month header
    title = time.strftime("%B %Y", (ed["year"], ed["month"], 1, 0, 0, 0, 0, 1, -1))
    c.text(x + w / 2, ry + 15, title, 13, TEXT, True, anchor="mm")
    for kind, bx in (("left", x + 14), ("right", x + w - 14 - 28)):
        hot = hover == "ed-" + kind
        if hot:
            c.rect(bx, ry + 1, 28, 28, 7, (255, 255, 255, 16))
        c.glyph(kind, bx + 14, ry + 15, 16, TEXT if hot else MUTED)
        hits.append(((bx, ry + 1, 28, 28), "ed-" + kind, "hand"))
    ry += 30
    cw = (w - 28) / 7
    for i, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
        c.text(x + 14 + cw * i + cw / 2, ry + 11, name, 11, FAINT, anchor="mm")
    ry += 22
    today = time.localtime()[:3]
    for week in weeks:
        for i, day in enumerate(week):
            if not day:
                continue
            cx = x + 14 + cw * i
            chosen = (ed["year"], ed["month"], day) == ed["date"]
            hot = hover == f"ed-day:{day}"
            if chosen:
                c.rect(cx + 3, ry + 1, cw - 6, 28, 7, GOOD + (255,))
            elif hot:
                c.rect(cx + 3, ry + 1, cw - 6, 28, 7, (255, 255, 255, 16))
            is_today = (ed["year"], ed["month"], day) == today
            color = ON_ACCENT if chosen else (TEXT if hot or is_today else MUTED)
            c.text(cx + cw / 2, ry + 15, str(day), 12.5, color, is_today or chosen, anchor="mm",
                   bg=GOOD if chosen else (over(SURFACE_3, (255, 255, 255, 16)) if hot else SURFACE_3))
            hits.append(((cx + 3, ry + 1, cw - 6, 28), f"ed-day:{day}", "hand"))
        ry += 30
    ry += 8
    note = {"auto": "Detected from your account. Change it if it is wrong.", "manual": "Set by you."}.get(
        ed.get("source"), "Not reported by the provider. Enter it from your billing page.")
    for i, line in enumerate(wrap(note, 11.5, w - 28)[:2]):
        c.text(x + 14, ry + 12 + i * 16, line, 11.5, FAINT)
    ry += 34 + 12
    bx = x + w - 14
    for action, label, primary in (("ed-save", "Save", True), ("ed-cancel", "Cancel", False)) + \
            ((("ed-clear", "Use detected", False),) if ed.get("source") == "manual" else ()):
        bw = text_w(label, 13) + 28
        bx -= bw
        hot = hover == action
        if primary:
            fill = (TEXT if not hot else blend(BG, TEXT, .08))
            c.rect(bx, ry, bw, 32, 8, fill + (255,))
            c.text(bx + bw / 2, ry + 16, label, 13, BG, True, anchor="mm", bg=fill)
        else:
            if hot:
                c.rect(bx, ry, bw, 32, 8, (255, 255, 255, 16))
            c.text(bx + bw / 2, ry + 16, label, 13, TEXT if hot else MUTED, anchor="mm",
                   bg=over(SURFACE_3, (255, 255, 255, 16)) if hot else SURFACE_3)
        hits.append(((bx, ry, bw, 32), action, "hand"))
        bx -= 6
    return (x, y, w, h), hits


def wrap(text, size, width, bold=False):
    lines, line = [], ""
    for word in text.split():
        trial = (line + " " + word).strip()
        if text_w(trial, size, bold) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


TOAST_COLORS = {"error": BAD, "ok": GOOD, "": ACCENT["codex"]}


def toasts(image, scale, items, vw, vh):
    """Bottom right, newest at the bottom. items: (text, kind, alpha): each fades in and out."""
    y = vh - 20
    for text, kind, alpha in reversed(items[-4:]):
        lines = wrap(text, 13, 380 - 42)
        h = 22 + 18 * len(lines)
        w = min(380, 42 + max(text_w(line, 13) for line in lines) + 14)
        y -= h
        x = vw - 20 - w
        before = image.fade_begin(alpha) if alpha < 1 else None
        dy = 4 * (1 - alpha)
        c = panel(image, scale, x, y + dy, w, h)
        if kind == "error":
            c.outline(x, y + dy, w, h, 11, BAD + (128,))
        c.dot(x + 18, y + dy + 17, 4, TOAST_COLORS.get(kind, ACCENT["codex"]))
        for i, line in enumerate(lines):
            c.text(x + 32, y + dy + 16 + i * 18, line, 13, TEXT, anchor="lm")
        if before is not None:
            image.fade_end(before, (x, y, w, h), alpha)
        y -= 8
