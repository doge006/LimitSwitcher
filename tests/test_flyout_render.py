import unittest

try:
    from account_switcher import flyout_render as fr
except ImportError as error:  # Pillow not installed
    raise unittest.SkipTest(f"Pillow missing: {error}")
from account_switcher.web import Controller


class FlyoutRenderTests(unittest.TestCase):
    def setUp(self):
        self.controller = Controller()
        self.state = self.controller.snapshot()

    def tearDown(self):
        self.controller.close()

    def actions(self, state, hover=None, scale=1.0):
        image, hits = fr.render(state, hover, scale)
        return image, [action for _, action in hits]

    def test_degenerate_shapes_do_not_crash_drawing(self):
        """An animated bar eased to (or just past) zero size once made PIL raise "y1 must be greater
        than or equal to y0" and the panel stopped redrawing (app.log, 9/26)."""
        layout = fr.Layout()
        fill = (255, 255, 255, 200)
        for h in (0, -0.4, 0.001, 0.3):
            layout.rect(10, 10, 3, h, 1.5, fill)
            layout.rect(10, 10, h, 3, 1.5, fill)
        layout.shapes.append(("ellipse", 5, 5, 4, 4, fill))
        for scale in (1.0, 1.5, 2.0):
            fr.paint(layout, 200, 60, scale)
            fr.paint_clear(layout, 200, 60, scale)

    def test_regions_cover_every_control(self):
        image, actions = self.actions(self.state)
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.width, fr.WIDTH + 2 * fr.MARGIN)
        # Only accounts you can switch to are clickable; active ones are not.
        self.assertEqual(actions, ["full", "pin", "swap:claude-b", "swap:codex-b", "toggle:autoSwap", "toggle:afk", "compact", "quit"])

    def test_hit_test_finds_rows(self):
        _, hits = fr.render(self.state)
        (x, y, w, h), _ = next(item for item in hits if item[1] == "swap:claude-b")
        self.assertEqual(fr.hit_test(hits, x + w / 2, y + h / 2), "swap:claude-b")
        self.assertIsNone(fr.hit_test(hits, 1, 1))  # shadow margin is not clickable

    def test_corners_are_transparent_and_panel_is_opaque(self):
        image, _ = fr.render(self.state)
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        middle = image.getpixel((image.width // 2, image.height // 2))
        self.assertEqual(middle[3], 255)

    def test_scales_for_high_dpi(self):
        small, _ = fr.render(self.state, scale=1.0)
        large, _ = fr.render(self.state, scale=2.0)
        self.assertAlmostEqual(large.width, small.width * 2, delta=2)

    def test_busy_and_exhausted_accounts_are_not_clickable(self):
        state = self.controller.snapshot()
        state["accounts"][1]["eligible"] = False
        state["accounts"][1]["windows"][0]["used"] = 100
        _, actions = self.actions(state)
        self.assertNotIn("swap:claude-b", actions)
        state["busy"] = True
        _, actions = self.actions(state)
        self.assertEqual(actions, ["full", "pin", "compact", "quit"])

    def test_grows_with_more_accounts_without_scrolling(self):
        state = self.controller.snapshot()
        extra = dict(state["accounts"][1], id="claude-c", active=False)
        taller, _ = fr.render(dict(state, accounts=state["accounts"] + [extra]))
        normal, _ = fr.render(state)
        self.assertEqual(taller.height - normal.height, fr.ROW_H + 2)

    def test_names_are_emails_resets_and_subscription(self):
        layout, _ = fr.build(self.state)
        texts = [t[2] for t in layout.texts]
        self.assertIn("personal@example.com", texts)
        self.assertNotIn("Personal", texts)
        self.assertTrue(any(t.startswith("resets in ") and t.endswith("m") for t in texts))  # e.g. "resets in 2h 0m"
        self.assertFalse(any(t.startswith(("Renews", "Ends")) for t in texts))     # unknown subscription: nothing
        state = self.controller.snapshot()
        state["accounts"][0]["subscription"] = {"at": fr.time.time() + 12.5 * 86400, "ends": False}
        state["accounts"][1]["subscription"] = {"at": fr.time.time() + 3.2 * 86400, "ends": True}
        texts = [t[2] for t in fr.build(state)[0].texts]
        self.assertIn("Renews 12d", texts)
        self.assertIn("Ends 3d", texts)
        self.assertEqual(fr.subscription_text(state["accounts"][1])[1], fr.WARN)  # ends within a week: yellow
        state["accounts"][1]["subscription"] = {"at": fr.time.time() + 20.5 * 86400, "ends": True}
        self.assertEqual(fr.subscription_text(state["accounts"][1]), ("Ends 20d", fr.MUTED))  # weeks away: not yet

    def test_animation_values_change_the_frame(self):
        rest, _ = fr.render(self.state)
        mid, _ = fr.render(self.state, fx={("toggle", "afk"): 0.5, ("hover", "swap:claude-b"): 0.5})
        self.assertEqual(rest.size, mid.size)
        self.assertNotEqual(rest.tobytes(), mid.tobytes())
        self.assertEqual(fr.targets(self.state)[("toggle", "autoSwap")], 1.0)

    def test_pending_switch_and_status_notes(self):
        layout, _ = fr.build(self.state, pending="claude-b")
        self.assertIn("Switching…", [t[2] for t in layout.texts])
        self.assertNotIn("swap:codex-b", [a for _, a in layout.hits])  # no double switching
        state = self.controller.snapshot()
        state["accounts"][1]["status"] = "Login expired; sign in again"
        layout, _ = fr.build(state)
        self.assertIn("Sign in again", [t[2] for t in layout.texts])
        self.assertIn("relogin:" + state["accounts"][1]["id"], [a for _, a in layout.hits])  # one click to sign in

    def test_painter_frames_match_whole_redraws(self):
        """The panel draws again only what changed since its last frame (a hover fade, a switch
        sliding, a row asking to confirm): every frame must be the whole redraw, pixel for pixel."""
        from unittest import mock
        _, hits = fr.render(self.state)
        rest = fr.targets(self.state)
        frames = []
        for action in [a for _, a in hits][:4] + ["toggle:afk"]:
            frames += [(action, {**rest, ("hover", action): i / 4}, {}) for i in range(1, 5)]
            frames += [(None, {**rest, ("hover", action): 1 - i / 4}, {}) for i in range(1, 5)]
        frames += [("toggle:autoSwap", {**rest, ("toggle", "autoSwap"): i / 5}, {}) for i in range(6)]
        frames += [("swap:claude-b", rest, {"armed": "claude-b"}), (None, rest, {"pinned": True}),
                   (None, rest, {"pending": "claude-b"}), (None, rest, {})]
        with mock.patch("time.time", lambda: 1_790_000_000.0):  # "renews in" stays the same throughout
            for scale in (1.0, 1.25, 1.5, 2.0):
                painter, steps = fr.Painter(), 0
                for hover, fx, extra in frames:
                    image, hits = fr.render(self.state, hover, scale, fx=fx, painter=painter, **extra)
                    whole, whole_hits = fr.render(self.state, hover, scale, fx=fx, **extra)
                    self.assertEqual(image.tobytes(), whole.tobytes(), (scale, hover, extra))
                    self.assertEqual(hits, whole_hits)
                    steps += painter.changed is not None
                self.assertGreater(steps, len(frames) // 2)  # most frames were partial redraws
            painter = fr.Painter()
            items = [{"action": "panel", "label": "Open panel", "bold": True}, "-", {"action": "quit", "label": "Quit"}]
            for hover in ("panel", "quit", None):
                image, _ = fr.render_menu(items, hover, 1.5, painter=painter)
                self.assertEqual(image.tobytes(), fr.render_menu(items, hover, 1.5)[0].tobytes())

    def test_window_pixels_are_written_only_where_the_frame_changed(self):
        import ctypes
        painter = fr.Painter()
        first, _ = fr.render(self.state, None, 1.5, painter=painter)
        size = first.width * first.height * 4
        bitmap = ctypes.create_string_buffer(size)
        fr.write_bgra(first, ctypes.addressof(bitmap))
        self.assertEqual(bitmap.raw, first.convert("RGBa").tobytes("raw", "BGRa"))
        second, _ = fr.render(self.state, "swap:claude-b", 1.5, painter=painter,
                              fx={**fr.targets(self.state), ("hover", "swap:claude-b"): 0.5})
        self.assertTrue(painter.changed)
        fr.write_bgra(second, ctypes.addressof(bitmap), painter.changed)
        self.assertEqual(bitmap.raw, second.convert("RGBa").tobytes("raw", "BGRa"))

    def test_switch_needs_a_confirming_click(self):
        layout, _ = fr.build(self.state, armed="claude-b")
        self.assertIn("Click again", [t[2] for t in layout.texts])
        self.assertIn("swap:claude-b", [a for _, a in layout.hits])  # the second click lands on the same row

    def test_popped_out_panel_can_be_hidden(self):
        self.assertNotIn("hide", [a for _, a in fr.build(self.state)[0].hits])
        self.assertIn("hide", [a for _, a in fr.build(self.state, pinned=True)[0].hits])
        fr.render(self.state, pinned=True)  # draws the minimize icon

    def test_compact_panel_shows_only_the_accounts_in_use(self):
        full_image, _ = fr.render(self.state)
        state = dict(self.state, compact=True)
        layout, _ = fr.build(state)
        names = [t[2] for t in layout.texts]
        self.assertIn("personal@example.com", names)
        self.assertNotIn("second@example.com", names)  # not in use: not shown
        self.assertEqual(sorted(a for _, a in layout.hits), ["expand", "hide", "quit"])  # always popped out: no pop-out button
        pinned, _ = fr.build(state, pinned=True)
        self.assertIn("hide", [a for _, a in pinned.hits])  # popped out: can be minimized
        image, _ = fr.render(state)
        self.assertLess(image.size[1], full_image.size[1] * 0.5)
        self.assertLess(image.size[0], full_image.size[0])

    def test_taskbar_block_shows_the_account_in_use_sideways(self):
        layout, width = fr.build_block(self.state, "codex")
        names = [t[2] for t in layout.texts]
        self.assertIn("personal@example.com", names)          # the in-use account, full email
        self.assertNotIn("second@example.com", names)
        self.assertIn("Pro", names)  # just the plan: the icon says which provider
        self.assertNotIn("Codex · Pro", names)
        self.assertTrue(any(n.startswith("resets in") for n in names))
        self.assertEqual([a for _, a in layout.hits], ["open"])
        image, hits = fr.render_block(self.state, "codex", scale=1.5)
        self.assertEqual(image.size, (round(width * 1.5), 66))
        self.assertGreater(image.getpixel((2, 30))[3], 0)  # the resting plate catches the mouse
        self.assertLess(image.getpixel((2, 30))[3], 100)   # a light backing, not a box
        # Fewer limit columns when the taskbar is short on room.
        self.assertLess(fr.block_width(self.state, "claude", columns=1), fr.block_width(self.state, "claude", columns=3))
        light, _ = fr.render_block(self.state, "codex", light=True)  # dark text for a light taskbar
        self.assertEqual(light.size, (width, 44))

    def test_taskbar_block_swap_slides_one_out_then_the_next_in(self):
        state = self.controller.snapshot()
        for account in state["accounts"]:
            if account["provider"] == "codex":
                account["active"] = account["id"] == "codex-b"
        def shown(t):
            layout, _ = fr.build_block(state, "codex", fx={("active", "codex-a"): t, ("active", "codex-b"): 1 - t})
            return {t[2] for t in layout.texts}
        self.assertIn("personal@example.com", shown(0.8))       # first half: the old one leaving
        self.assertNotIn("second@example.com", shown(0.8))
        self.assertIn("second@example.com", shown(0.2))         # second half: the new one arriving
        self.assertNotIn("personal@example.com", shown(0.2))
        for t in (0.9, 0.55, 0.3, 0.0):
            fr.render_block(state, "codex", fx={("active", "codex-a"): t, ("active", "codex-b"): 1 - t})

    def test_panel_from_a_taskbar_block_lists_only_that_provider(self):
        state = dict(self.state, compact=True, taskbarAvailable=True)
        layout, _ = fr.build(state, only="codex")
        names = [t[2] for t in layout.texts]
        self.assertIn("CODEX", names)
        self.assertNotIn("CLAUDE", names)
        self.assertIn("second@example.com", names)  # every Codex account, to switch to, even in compact mode
        actions = [a for _, a in layout.hits]
        self.assertIn("toggle:taskbar", actions)     # the Taskbar switch, next to AFK
        self.assertNotIn("compact", actions)

    def test_block_keeps_to_the_plan_and_the_age(self):
        """The taskbar is short on room: the plan and how old the numbers are, nothing more (the
        provider is the icon; banked resets are one click away in the panel)."""
        import time
        state = self.controller.snapshot()
        state["accounts"][0]["credits"] = {"resets": 2}
        state["accounts"][0]["updated_at"] = time.time() - 180
        layout, _ = fr.build_block(state, "claude")
        texts = [t[2] for t in layout.texts]
        self.assertIn("Max 5x", texts)
        self.assertIn(" · 3m ago", texts)
        self.assertFalse(any("reset" in t and "resets in" not in t for t in texts))

    def test_empty_state_offers_adding_accounts(self):
        state = dict(self.state, accounts=[])
        _, actions = self.actions(state)
        self.assertIn("add:claude", actions)
        self.assertIn("add:codex", actions)

    def test_menu_renders_with_checks(self):
        items = [{"action": "panel", "label": "Open panel", "bold": True}, "-",
                 {"action": "toggle:afk", "label": "Auto resume", "checked": True},
                 {"action": "x", "label": "Disabled", "enabled": False}]
        image, hits = fr.render_menu(items, hover="toggle:afk", scale=1.5)
        self.assertEqual([a for _, a in hits], ["panel", "toggle:afk"])
        self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_limit_reached_row_is_greyed_out(self):
        state = self.controller.snapshot()
        normal, _ = fr.render(state)
        state["accounts"][1]["eligible"] = False
        greyed, _ = fr.render(state)
        # The row's text gets darker: compare the brightest pixel in that row's name area.
        row = (fr.MARGIN + 20, fr.MARGIN + 54 + 30 + fr.ROW_H + 8, fr.MARGIN + 200, fr.MARGIN + 54 + 30 + fr.ROW_H + 26)
        bright = lambda img: max(sum(p[:3]) for p in img.crop(row).getdata())
        self.assertLess(bright(greyed), bright(normal) * 0.7)


if __name__ == "__main__":
    unittest.main()
