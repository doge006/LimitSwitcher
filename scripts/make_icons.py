"""Draws the app icon (a development tool; its output is committed under static/assets).

    python scripts/make_icons.py

The mark: a gauge (Codex's violet into Claude's orange) with its needle heading for the limit,
on a dark tile. Written as
  switcher.png      256 px, the tile filling the image (in-app header, tray, web favicon)
  switcher.ico      Windows sizes, 16-256 px
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "account_switcher" / "static" / "assets"
SS = 4  # supersampling
TOP, BOTTOM = (54, 60, 76), (16, 18, 24)
ORANGE = ((236, 146, 112), (217, 119, 87))     # Claude (#D97757)
VIOLET = ((120, 150, 255), (150, 110, 250))    # Codex (blue to violet)


def squircle(size, exponent=5.0):
    """A superellipse mask (a rounded square), size x size."""
    mask = Image.new("L", (size, size), 0)
    half = size / 2
    points = []
    for i in range(720):
        t = 2 * math.pi * i / 720
        c, s = math.cos(t), math.sin(t)
        x = half + half * math.copysign(abs(c) ** (2 / exponent), c)
        y = half + half * math.copysign(abs(s) ** (2 / exponent), s)
        points.append((x, y))
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask


def gradient(size, top, bottom):
    column = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return column.resize((size, size))


def gauge(size, colors_left, colors_right, radius, width):
    """The gauge's dial: a 240-degree arc open at the bottom, Codex's violet on the left half and
    Claude's orange on the right (the two limits it watches), with round ends. Returns RGBA."""
    c = size / 2
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for (start, end), colors in (((150, 270), colors_left), ((270, 390), colors_right)):
        shape = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(shape)
        d.arc((c - radius, c - radius, c + radius, c + radius), start, end, fill=255, width=round(width))
        for angle in (start, end):  # round caps
            a, mid = math.radians(angle), radius - width / 2
            x, y = c + mid * math.cos(a), c + mid * math.sin(a)
            d.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=255)
        fill = gradient(size, *colors).rotate(-35, resample=Image.Resampling.BICUBIC, expand=False).convert("RGBA")
        fill.putalpha(shape)
        layer.alpha_composite(fill)
    return layer


def needle(size, angle, length, width):
    """A white needle from the centre towards `angle` degrees (clockwise from 3 o'clock), tapering
    to a point, with a round hub. Returns RGBA."""
    c = size / 2
    shape = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(shape)
    a = math.radians(angle)
    n = (-math.sin(a), math.cos(a))
    tip = (c + length * math.cos(a), c + length * math.sin(a))
    d.polygon([tip, (c + n[0] * width / 2, c + n[1] * width / 2), (c - n[0] * width / 2, c - n[1] * width / 2)],
              fill=255)
    hub = width * .8
    d.ellipse((c - hub, c - hub, c + hub, c + hub), fill=255)
    layer = Image.new("RGBA", (size, size), (246, 247, 250, 0))
    layer.putalpha(shape)
    return layer


def tile(size):
    """The dark tile with the mark, filling size x size (supersampled internally)."""
    big = size * SS
    base = gradient(big, TOP, BOTTOM).convert("RGBA")
    mark = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    mark.alpha_composite(gauge(big, VIOLET, ORANGE, big * .33, big * .12))
    mark.alpha_composite(needle(big, -40, big * .27, big * .11))
    base.alpha_composite(mark, (0, round(big * .04)))  # the dial's weight is above its centre
    mask = squircle(big)
    base.putalpha(mask)
    return base.resize((size, size), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    tile(256).save(ASSETS / "switcher.png")
    tile(256).save(ASSETS / "switcher.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    print("wrote", ", ".join(p.name for p in (ASSETS / "switcher.png", ASSETS / "switcher.ico")))
