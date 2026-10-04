import time
import unittest
import unittest.mock

try:
    from account_switcher import fullview, fullview_render as vr
except ImportError as error:  # Pillow missing
    raise unittest.SkipTest(f"full view dependencies missing: {error}")


def account(i, provider, active=False, eligible=True, **extra):
    now = time.time()
    view = {"id": f"{provider}-{i}", "provider": provider, "alias": f"a{i}", "email": f"user{i}@example.com",
            "name": f"user{i}@example.com", "label": "", "plan": "Pro", "active": active, "eligible": eligible,
            "status": None, "subscription": None, "credits": None,
            "windows": [{"key": "five_hour", "label": "5-hour limit", "used": 40, "resetsAt": now + 3600, "scope": "account"}]}
    view.update(extra)
    return view


def state(**extra):
    base = {"revision": 1, "mode": "live", "busy": False, "afk": False, "autoSwap": True, "nameMode": False,
            "taskbar": True, "taskbarAvailable": False, "log": [], "signingIn": [],
            "accounts": [account(1, "claude", active=True), account(2, "claude"), account(1, "codex", active=True)]}
    base.update(extra)
    return base


class Host:
    def __init__(self):
        self.timers, self.invalidated, self.cursor, self.paste = {}, 0, "arrow", ""

    def invalidate(self):
        self.invalidated += 1

    def set_timer(self, name, ms):
        self.timers[name] = ms

    def kill_timer(self, name):
        self.timers.pop(name, None)

    def has_timer(self, name):
        return name in self.timers

    def set_cursor(self, kind):
        self.cursor = kind

    def clipboard(self):
        return self.paste


class Controller:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def action(self, name, body):
        if name == self.fail:
            raise RuntimeError("An operation is already running")
        self.calls.append((name, body))


class FullViewTests(unittest.TestCase):
    def setUp(self):
        self.controller, self.host = Controller(), Host()
        self.view = fullview.FullView(self.controller, self.host, state())
        self.view.resize(960, 900, 1.0)
        self.view.frame()

    def click(self, action):
        box = next(h[0] for h in self.view.page_hits + [(b, a, c, None) for box, hits in self.view.overlay_hits
                                                         for b, a, c in hits] if h[1] == action)
        x, y = box[0] + box[2] / 2, box[1] + box[3] / 2
        if not any(h[1] == action for h in self.view.page_hits):
            y += self.view.scroll  # overlay hits are in window coordinates
        y -= self.view.scroll
        self.view.mouse_move(x, y)
        self.view.mouse_down(x, y)
        self.view.mouse_up(x, y)
        self.view.frame()

    def test_asks_for_fresh_usage_when_opened(self):
        self.assertEqual(self.controller.calls[0], ("refresh", {"ifOlderThan": 60}))

    def test_swap_button_switches_and_shows_progress(self):
        self.click("swap:claude-2")
        self.assertIn(("swap", {"id": "claude-2"}), self.controller.calls)
        self.assertEqual(self.view.ui.pending, "claude-2")
        self.assertIn("pending", self.host.timers)
        landed = state(accounts=[account(1, "claude"), account(2, "claude", active=True), account(1, "codex", active=True)])
        self.view.set_state(landed)
        self.assertIsNone(self.view.ui.pending)

    def test_no_swap_button_on_the_account_in_use(self):
        self.assertFalse(any(h[1] == "swap:claude-1" for h in self.view.page_hits))

    def test_settings_menu_toggles_preferences(self):
        self.click("settings")
        self.assertEqual(self.view.ui.menu, "settings")
        self.click("set:afk")
        self.assertIn(("preferences", {"autoSwap": True, "afk": True}), self.controller.calls)
        self.click("set:nameMode")
        self.assertIn(("names", {"on": True}), self.controller.calls)

    def test_jev_key_field_is_masked_and_saved_on_enter(self):
        self.view.set_state(state(jevCompact=True, jevKey=None))
        self.click("settings")
        self.click("name:jevkey")
        self.assertEqual(self.view.ui.editing[0], "jevkey")
        self.host.paste = "sk-or-v1-abc\n"
        self.view.key("v", ctrl=True)
        self.assertEqual(self.view.ui.editing[1], "sk-or-v1-abc")  # the stray newline is gone
        self.view.frame()
        self.view.key("enter")
        self.assertIn(("jevKey", {"key": "sk-or-v1-abc"}), self.controller.calls)
        self.assertIsNone(self.view.ui.editing)

    def test_a_saved_jev_key_shows_key_set_and_is_removed_on_the_second_click(self):
        self.view.set_state(state(jevCompact=True, jevKey="file"))
        self.click("settings")
        hits = [a for _, h in self.view.overlay_hits for _, a, _ in h]
        self.assertNotIn("name:jevkey", hits)  # nothing to type into: the saved key is never shown
        self.click("jevkey-clear:")
        self.assertFalse(any(c[0] == "jevKey" for c in self.controller.calls))  # the first click only asks
        self.assertTrue(self.view.ui.jev_confirm)
        self.click("set:nameMode")  # anything else cancels the question
        self.assertFalse(self.view.ui.jev_confirm)
        self.click("jevkey-clear:")
        self.click("jevkey-clear:")  # Confirm
        self.assertIn(("jevKey", {"key": None}), self.controller.calls)
        self.assertFalse(self.view.ui.jev_confirm)
        self.view.set_state(state(jevCompact=True, jevKey=None))  # removed: the field comes back, empty
        self.view.frame()
        self.assertIn("name:jevkey", [a for _, h in self.view.overlay_hits for _, a, _ in h])

    def test_closing_settings_cancels_the_remove_question(self):
        self.view.set_state(state(jevCompact=True, jevKey="file"))
        self.click("settings")
        self.click("jevkey-clear:")
        self.view.mouse_up(5, 5)  # a click on nothing closes the menu
        self.assertFalse(self.view.ui.jev_confirm)

    def test_jev_key_field_only_shows_with_jev_compaction_on(self):
        self.view.set_state(state(jevCompact=False))
        self.click("settings")
        self.assertFalse(any(a == "name:jevkey" for _, hits in self.view.overlay_hits for _, a, _ in hits))
        self.view.set_state(state(jevCompact=True, jevKey="env"))
        self.view.frame()
        self.assertFalse(any(a == "name:jevkey" for _, hits in self.view.overlay_hits for _, a, _ in hits))  # set elsewhere

    def test_card_hints_are_shortened_not_cut_off(self):
        ago = "1h 20m"
        options = [f"Numbers from {ago} ago · checking", f"{ago} ago · checking", f"{ago} old"]
        wide = vr.text_w(options[0], 12) + 1
        self.assertEqual(vr.best_fit(options, 12, wide), options[0])
        # a card with Swap and Remove beside it has less room: the second, shorter one, whole
        room = vr.text_w(options[1], 12) + 1
        self.assertEqual(vr.best_fit(options, 12, room), options[1])
        self.assertEqual(vr.best_fit(options, 12, vr.text_w(options[2], 12) + 1), options[2])
        self.assertTrue(vr.best_fit(options, 12, 20).endswith("…"))  # only when nothing fits

    def test_mod_row_shows_its_whole_state(self):
        for status in ("installed", "active", "missing", "installing"):
            lines, _, height = vr.mod_row({"mod": {"status": status}})
            self.assertEqual(" ".join(lines), vr.MOD_TEXT[status][0])  # nothing cut off
            self.assertEqual(height, 42 + 16 * len(lines))
        lines, _, _ = vr.mod_row({"mod": {"status": "error", "text": "Claude Code not found"}})
        self.assertEqual(" ".join(lines), "Claude Code not found")
        self.assertEqual(vr.MOD_NAME, "Claude Code Status mod")

    def test_settings_installs_the_claude_code_mod(self):
        self.view.set_state(state(mod={"status": "missing"}))
        self.click("settings")
        self.click("mod:install")
        self.assertIn(("installMod", {}), self.controller.calls)

    def test_remove_asks_first(self):
        self.view.mouse_move(700, 200)  # over the second Claude card: its Remove button fades in
        self.view.frame()
        self.view.motion.settle()
        self.view.frame()
        self.click("remove:claude-2")
        self.assertFalse(any(c[0] == "remove" for c in self.controller.calls))
        self.click("remove-yes:claude-2")
        self.assertIn(("remove", {"id": "claude-2"}), self.controller.calls)

    def test_rename_in_name_mode(self):
        self.view.set_state(state(nameMode=True))
        self.view.frame()
        self.click("name:claude-2")
        for ch in "Work":
            self.view.char(ch)
        self.view.key("enter")
        self.assertIn(("rename", {"id": "claude-2", "name": "Work"}), self.controller.calls)

    def test_renewal_date_editor_saves_the_picked_day(self):
        self.click("renew:claude-2")
        self.assertIsNotNone(self.view.ui.editor)
        self.click("ed-kind:ends")
        self.click("ed-day:15")
        self.click("ed-save")
        name, body = self.controller.calls[-1]
        self.assertEqual(name, "subscription")
        self.assertTrue(body["ends"])
        self.assertEqual(time.localtime(body["at"]).tm_mday, 15)
        self.assertIsNone(self.view.ui.editor)

    def test_errors_become_toasts(self):
        view = fullview.FullView(Controller(fail="swap"), self.host, state())
        view.resize(960, 900, 1.0)
        view.frame()
        view.activate("swap:claude-2")
        self.assertIsNone(view.ui.pending)
        self.assertEqual(view.toast_list[-1][:2], ("An operation is already running", "error"))

    def test_scrolls_within_the_page(self):
        many = state(accounts=[account(i, "claude") for i in range(12)])
        self.view.set_state(many)
        self.view.wheel(10_000)
        self.assertEqual(self.view.scroll, self.view.max_scroll())
        self.assertGreater(self.view.scroll, 0)
        self.view.wheel(-10_000)
        self.assertEqual(self.view.scroll, 0)

    def test_unchanged_cards_are_not_redrawn(self):
        before = {key: tile for key, (_, tile) in self.view.tiles.items()}
        self.view.mouse_move(700, 200)  # hover one card
        self.view.frame()
        redrawn = [key for key, (_, tile) in self.view.tiles.items() if before.get(key) is not tile]
        self.assertEqual(redrawn, ["card:claude-2"])

    def test_animates_only_while_something_moves(self):
        self.view.frame()
        self.assertIn("anim", self.host.timers)  # cards rising in, bars filling
        self.view.motion.settle()
        self.view.frame()
        self.assertNotIn("anim", self.host.timers)  # at rest: no frames at all
        self.view.mouse_move(700, 200)
        self.view.frame()
        self.assertIn("anim", self.host.timers)  # the hover fades in

    def test_bars_fill_from_empty_then_follow_changes(self):
        motion = self.view.motion
        bar = ("bar", ("claude-2", "five_hour"))
        self.assertIn(bar, motion.runs)
        self.assertEqual(motion.runs[bar][:2], (0.0, 60))  # from empty to 60% left
        motion.settle()
        changed = state()
        changed["accounts"][1]["windows"][0]["used"] = 90
        self.view.set_state(changed)
        self.view.frame()
        self.assertEqual(motion.runs[bar][:2], (60, 10))

    def test_closing_frees_the_tiles(self):
        self.view.close()
        self.assertEqual(self.view.tiles, {})

    def test_frame_is_window_sized(self):
        self.view.resize(800, 600, 1.5)
        image = self.view.frame()
        self.assertEqual(image.size, (1200, 900))


class HelperTests(unittest.TestCase):
    def test_redact_hides_the_whole_email(self):
        shown = vr.redact("someone@example.com")
        self.assertNotIn("@", shown)
        self.assertNotIn("example", shown)
        self.assertEqual(vr.redact(""), "")

    def test_columns_follow_the_width(self):
        self.assertEqual(vr.columns_for(960)[0], 2)
        self.assertEqual(vr.columns_for(600)[0], 1)

    def test_credits_line(self):
        self.assertEqual(vr.credits_items({"credits": {"kind": "credits", "balance": 12.5, "resets": 2}}),
                         [("Credits", "12.5"), ("Usage limit resets", "2")])


if __name__ == "__main__":
    unittest.main()


class HostTimerTests(unittest.TestCase):
    def test_windows_host_knows_every_timer_the_view_uses(self):
        """The Win32 host maps timer names to ids; a missing one broke every paint once (a white window)."""
        import ast
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "account_switcher"
        used = set(re.findall(r'(?:set_timer|kill_timer|has_timer)\("(\w+)"', (root / "fullview.py").read_text()))
        tree = ast.parse((root / "fullview_win.py").read_text())
        timers = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "TIMERS")
        self.assertTrue(used)
        self.assertEqual(used - set(timers), set())


class ClockTests(unittest.TestCase):
    def test_24_hour_setting_overrides_the_system(self):
        stamp = time.mktime((2026, 9, 27, 14, 30, 0, 0, 0, -1))
        try:
            vr.CLOCK_24 = True
            self.assertEqual(vr.clock(stamp), "14:30")
            vr.CLOCK_24 = False
            self.assertEqual(vr.clock(stamp), "2:30 PM")
        finally:
            vr.CLOCK_24 = None

    def test_setting_is_saved_by_the_controller(self):
        from account_switcher.web import Controller as Real
        controller = Real()
        try:
            controller.action("clock", {"on": True})
            self.assertTrue(controller.snapshot()["clock24"])
            controller.action("clock", {"on": False})
            self.assertFalse(controller.snapshot()["clock24"])
        finally:
            controller.close()


class IncrementalDrawingTests(unittest.TestCase):
    """Redrawing only what changed must give exactly the pixels of a whole redraw."""

    def test_every_frame_matches_a_whole_redraw(self):
        from account_switcher.web import Controller
        clock = [1000.0]
        controller = Controller()
        try:
            with unittest.mock.patch.object(fullview.time, "perf_counter", lambda: clock[0]):
                views = []
                for incremental in (True, False):
                    view = fullview.FullView(controller, Host(), controller.snapshot())
                    view.resize(900, 600, 1.25)
                    view.incremental = incremental
                    views.append(view)

                def step(n):
                    for _ in range(n):
                        clock[0] += 0.04
                        state = controller.snapshot()
                        frames = []
                        for view in views:
                            view.set_state(state)
                            frames.append(view.frame().tobytes())
                        self.assertEqual(frames[0], frames[1])

                step(25)  # rising in
                cards = [item for item in views[0].items if item[0] == "card"]
                for _, _, x, y, w, h, _ in cards[:3]:
                    for view in views:
                        view.mouse_move(x + w / 2, y + h / 2 - view.scroll)
                    step(6)
                    for view in views:
                        view.mouse_move(x + w * 0.85, y + h - 40 - view.scroll)
                    step(5)
                account = controller.gateway.router.current("claude")
                account.five_hour = min(100, account.five_hour + 8)  # usage goes up: bars and % animate
                step(15)
                for view in views:
                    view.activate("settings")
                step(8)
                for view in views:
                    view.activate("settings")
                    view.wheel(100)
                step(8)
                self.assertIsNotNone(views[0].last_page)
        finally:
            controller.close()


class FakePainter:
    """Stands in for the macOS painter: keeps what it was asked to draw."""
    OPS = {"rect", "outline", "line", "text", "image", "glyph", "mark"}

    def __init__(self, test):
        self.test, self.tiles, self.live, self.overlays = test, [], [], 0

    def background(self):
        pass

    def visible(self, box):
        return True

    def tile(self, tile, x, y, alpha):
        self.test.assertIsNone(tile.image)  # recorded, never drawn into pixels
        for op in tile.ops:
            self.test.assertIn(op[0], self.OPS)
        self.tiles.append((tile, x, y, alpha))

    def canvas(self, bg):
        from account_switcher import fullview_render as vr
        rec = vr.Recorder(1.0, bg)
        self.live.append(rec)
        return rec

    def surface(self):
        painter = self

        class Surface:
            def canvas(self, bg):
                painter.overlays += 1
                return painter.canvas(bg)

            def shadow(self, *args):
                pass

            def fade_begin(self, t):
                return None

            def fade_end(self, before, box, t):
                pass
        return Surface()


class NativeDrawingTests(unittest.TestCase):
    """The macOS full view records tiles as drawing calls and draws them itself."""

    def test_native_frames_record_and_draw_everything(self):
        from account_switcher.web import Controller
        controller = Controller()
        try:
            view = fullview.FullView(controller, Host(), controller.snapshot())
            view.native = True
            view.resize(900, 600, 2.0)
            painter = FakePainter(self)
            boxes = view.draw_native(painter)
            self.assertTrue(boxes)
            cards = [t for t, *_ in painter.tiles if t.shape and t.shape[0] == "card"]
            self.assertTrue(cards)
            self.assertTrue(any(op[0] == "text" for t in cards for op in t.ops))
            self.assertEqual(len(view.tiles), len(view.items))  # nothing kept but the recorded calls
            view.activate("settings")
            view.toast("Swapped", "ok")
            for _ in range(10):
                view.draw_native(painter)
            self.assertGreater(painter.overlays, 0)
            self.assertTrue(view.overlay_hits)
            self.assertIsNone(view.last_page)  # no picture of the page
        finally:
            controller.close()
