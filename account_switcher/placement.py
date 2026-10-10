"""Where popups go on screen. Pure maths (unit-tested), no Windows calls.

Rule: the whole window, including its transparent shadow margin, stays inside the work
area. Windows treats every non-transparent pixel of a layered window (the soft shadow
too) as clickable, so any overlap with the taskbar would swallow clicks on the tray icon.
"""
from . import flyout_render as fr


def taskbar_edge(monitor, work):
    if work.left > monitor.left:
        return "left"
    if work.top > monitor.top:
        return "top"
    if work.right < monitor.right:
        return "right"
    return "bottom"


SLIDE = {"bottom": (0, 1), "top": (0, -1), "left": (-1, 0), "right": (1, 0)}


def clamp(value, low, high):
    return max(low, min(value, high))


def place_above(anchor, monitor, work, width, height, scale):
    """Top-left for the panel image: centred on the anchor (the tray icon), against the
    taskbar edge, entirely inside the work area. Returns (x, y, slide direction)."""
    ax, ay = anchor
    edge = taskbar_edge(monitor, work)
    min_x, max_x = work.left, work.right - width
    min_y, max_y = work.top, work.bottom - height
    if edge in ("bottom", "top"):
        x = clamp(round(ax - width / 2), min_x, max_x)
        y = max_y if edge == "bottom" else min_y
    else:
        y = clamp(round(ay - height / 2), min_y, max_y)
        x = min_x if edge == "left" else max_x
    return x, y, SLIDE[edge]


def place_menu(point, monitor, work, width, height, scale):
    """Context menu next to the pointer, never covering the point that was clicked."""
    px, py = point
    x = px - width if px + width > work.right else px
    y = py - height if py + height > work.bottom else py
    x = clamp(x, work.left, work.right - width)
    y = clamp(y, work.top, work.bottom - height)
    return x, y, SLIDE[taskbar_edge(monitor, work)]


def panel_contains(x, y, width, height):
    """Is a logical point (incl. margin) on the visible panel rather than its shadow?"""
    m = fr.MARGIN
    return m <= x < width - m and m <= y < height - m


# ---------- taskbar view: blocks in the empty stretches of the taskbar ----------
def taskbar_buttons(spans, rect):
    """(left, right) of the taskbar's own buttons, from UI Automation's (left, right, top, bottom)
    spans: the ones on this taskbar, button-sized. The notification area's buttons count too
    (its clock, icons, the hidden-icons arrow): on Windows 11 the clock can reach left of the
    TrayNotifyWnd window, and a block placed up to that window's edge then covered the clock."""
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    return [(a, b) for a, b, top, bottom in spans
            if b - a < width * .4 and bottom - top >= height * .4 and a < rect[2] and b > rect[0]
            and top >= rect[1] - 2 and bottom <= rect[3] + 2]


def free_gaps(left, right, occupied, margin):
    """Empty stretches of [left, right) once the taskbar's own buttons (occupied: (left, right)
    spans), each widened by margin, are taken out. Sorted left to right."""
    gaps, x = [], left
    for a, b in sorted((a - margin, b + margin) for a, b in occupied):
        if a > x:
            gaps.append((x, min(a, right)))
        x = max(x, b)
        if x >= right:
            break
    if right > x:
        gaps.append((x, right))
    return [(a, b) for a, b in gaps if b > a]


def place_blocks(gaps, wanted, spacing):
    """Where each provider's block goes. wanted: [(provider, {columns: width}, side)], at most two,
    left side first: a "left" block sits left-aligned in its gap, a "right" one right-aligned,
    never to the left of the other. Most blocks shown wins, then most limit columns, then the first further left and
    the second further right. Returns {provider: (x, width, columns)}, x the left edge."""
    def fits(gap, width):
        return gap[1] - gap[0] >= width

    options = [sorted(o.items(), reverse=True) for _, o, _ in wanted]
    best, best_score = {}, None
    if len(wanted) == 2:
        for i, a in enumerate(gaps):
            for c1, w1 in options[0]:
                for j in range(i, len(gaps)):
                    b = gaps[j]
                    for c2, w2 in options[1]:
                        ok = fits(a, w1 + spacing + w2) if i == j else fits(a, w1) and fits(b, w2)
                        score = (2, c1 + c2, -i, j)
                        if ok and (best_score is None or score > best_score):
                            best_score = score
                            best = {wanted[0][0]: (a[0], w1, c1), wanted[1][0]: (b[1] - w2, w2, c2)}
    if best:
        return best
    for index, (provider, _, side) in enumerate(wanted):  # not both: one alone, the first one first
        side_left = side == "left"
        for columns, width in options[index]:
            order = gaps if side_left else list(reversed(gaps))
            gap = next((g for g in order if fits(g, width)), None)
            if gap:
                return {provider: (gap[0] if side_left else gap[1] - width, width, columns)}
    return {}
