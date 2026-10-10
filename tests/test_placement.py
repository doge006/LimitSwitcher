import unittest
from types import SimpleNamespace as R

from account_switcher.placement import (free_gaps, panel_contains, place_above, place_blocks, place_menu,
                                       taskbar_buttons)

MONITOR = R(left=0, top=0, right=1920, bottom=1080)


def work_for(edge):
    return {"bottom": R(left=0, top=0, right=1920, bottom=1032), "top": R(left=0, top=48, right=1920, bottom=1080),
            "left": R(left=48, top=0, right=1920, bottom=1080), "right": R(left=0, top=0, right=1872, bottom=1080)}[edge]


class PlacementTests(unittest.TestCase):
    def inside(self, x, y, w, h, work):
        return work.left <= x and work.top <= y and x + w <= work.right and y + h <= work.bottom

    def test_panel_never_overlaps_the_taskbar(self):
        for edge in ("bottom", "top", "left", "right"):
            for scale in (1.0, 1.5, 2.0):
                work = work_for(edge)
                w, h = round(440 * scale), round(460 * scale)
                for anchor in ((1880, 1056), (20, 1056), (960, 1056), (1896, 20), (24, 540)):
                    x, y, _ = place_above(anchor, MONITOR, work, w, h, scale)
                    self.assertTrue(self.inside(x, y, w, h, work), (edge, scale, anchor, x, y))

    def test_panel_is_centred_over_the_icon_and_against_the_taskbar(self):
        work = work_for("bottom")
        x, y, slide = place_above((960, 1056), MONITOR, work, 440, 560, 1.0)
        self.assertEqual((x + 220, y + 560), (960, 1032))
        self.assertEqual(slide, (0, 1))

    def test_menu_never_covers_the_clicked_point(self):
        work = work_for("bottom")
        for point in ((1880, 1050), (10, 1050), (960, 500)):
            px, py = point
            x, y, _ = place_menu(point, MONITOR, work, 268, 400, 1.0)
            self.assertTrue(self.inside(x, y, 268, 400, work))
            covers = x <= px < x + 268 and y <= py < y + 400
            self.assertFalse(covers and py < work.bottom and (px, py) != (960, 500), point)

    def test_shadow_margin_is_not_the_panel(self):
        self.assertFalse(panel_contains(5, 5, 440, 560))
        self.assertTrue(panel_contains(220, 280, 440, 560))

    def test_taskbar_gaps_leave_room_around_the_buttons(self):
        self.assertEqual(free_gaps(0, 1600, [(600, 1300), (0, 180)], 12), [(192, 588), (1312, 1600)])
        self.assertEqual(free_gaps(0, 1000, [], 12), [(0, 1000)])
        self.assertEqual(free_gaps(0, 1000, [(0, 1000)], 12), [])

    def test_clock_reaching_left_of_the_notification_area_is_kept_clear(self):
        rect = (0, 1032, 1920, 1080)
        spans = [(900, 960, 1036, 1076),     # a task button
                 (1700, 1800, 1034, 1078),   # the clock, starting left of TrayNotifyWnd (1760)
                 (0, 1920, 1032, 1080),      # the whole bar: not a button
                 (1200, 1240, 0, 40)]        # another display's element
        occupied = taskbar_buttons(spans, rect)
        self.assertEqual(occupied, [(900, 960), (1700, 1800)])
        self.assertEqual(free_gaps(8, 1760 - 12, occupied, 12), [(8, 888), (972, 1688)])

    def test_blocks_go_left_and_right_and_shrink_before_giving_up(self):
        claude, codex = {3: 540, 2: 420, 1: 300}, {2: 420, 1: 300}
        spots = place_blocks([(192, 588), (1312, 1760)], [("claude", claude, "left"), ("codex", codex, "right")], 12)
        self.assertEqual(spots["claude"], (192, 300, 1))      # the left gap holds one column only
        self.assertEqual(spots["codex"], (1340, 420, 2))      # right-aligned in the right gap
        spots = place_blocks([(100, 1800)], [("claude", claude, "left"), ("codex", codex, "right")], 12)
        self.assertEqual(spots, {"claude": (100, 540, 3), "codex": (1380, 420, 2)})  # one wide gap: both
        self.assertEqual(place_blocks([(0, 200)], [("claude", claude, "left")], 12), {})    # no room: not shown
        self.assertEqual(place_blocks([(0, 400)], [("codex", codex, "right")], 12), {"codex": (100, 300, 1)})

