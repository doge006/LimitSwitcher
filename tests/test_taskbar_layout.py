import unittest

from account_switcher import flyout_render as fr
from account_switcher import taskbar_layout as tl
from account_switcher.web import Controller


def demo_state():
    controller = Controller()
    try:
        return controller.snapshot()
    finally:
        controller.close()


class LayoutTests(unittest.TestCase):
    def test_default_is_what_the_taskbar_always_showed(self):
        self.assertEqual(tl.layout({}), {"main": ["claude", "codex"]})
        self.assertEqual(tl.layout({"taskbarDisplay": "right"}), {"right": ["claude", "codex"]})
        self.assertEqual(tl.layout({"taskbarLayout": {}}), {})  # every slot turned off

    def test_bad_saved_layouts_fall_back_to_the_default(self):
        for bad in ({"main": ["claude"]}, {"main": ["x", None]}, {"": ["claude", None]}, ["claude"], {"main": "codex"}):
            self.assertEqual(tl.layout({"taskbarLayout": bad}), {"main": ["claude", "codex"]}, bad)

    def test_dropdown_lists_dynamic_fixed_and_off_choices(self):
        state = {"accounts": [{"id": "a", "provider": "claude"}, {"id": "b", "provider": "codex"}]}
        self.assertEqual(tl.options(state), ["claude", "next:claude", "codex", "next:codex", "acct:a", "acct:b", None])
        self.assertEqual(tl.clean({"main": ["next:claude", "next:codex"]}),
                         {"main": ["next:claude", "next:codex"]})
        self.assertFalse(tl.valid_slot("next:unknown"))

    def test_changing_a_slot_and_dropping_an_empty_display(self):
        current = {"main": ["claude", "codex"]}
        self.assertEqual(tl.with_slot(current, "left", 1, "acct:a"), {"main": ["claude", "codex"], "left": [None, "acct:a"]})
        self.assertEqual(tl.with_slot({"main": [None, "codex"]}, "main", 1, None), {})
        self.assertEqual(current, {"main": ["claude", "codex"]})  # not changed in place

    def test_the_tray_menus_display_choice_moves_everything_there(self):
        self.assertEqual(tl.moved({"main": [None, None], "left": ["codex", "acct:a"]}, "right"),
                         {"right": ["codex", "acct:a"]})

    def test_labels_follow_name_mode(self):
        state = {"accounts": [{"id": "a", "provider": "claude", "email": "a@example.com", "name": "Work"}]}
        self.assertEqual(tl.label(state, "acct:a"), "Work")
        self.assertEqual(tl.label(state, "claude"), "Claude in use")
        self.assertEqual(tl.label(state, "next:claude"), "Claude next use")
        self.assertEqual(tl.label(state, None), "Off")
        self.assertEqual(tl.label(state, "acct:x"), "Removed account")


class BlockTests(unittest.TestCase):
    def test_a_block_for_one_account_shows_it_whatever_is_in_use(self):
        state = demo_state()
        other = next(a for a in state["accounts"] if a["provider"] == "claude" and not a["active"])
        layout, width = fr.build_block(state, "acct:" + other["id"])
        self.assertIn(fr.display_name(other), [t[2] for t in layout.texts])
        self.assertGreater(width, 0)
        self.assertEqual(fr.block_width(state, "acct:missing"), 0)  # removed: no block
        image, _ = fr.render_block(state, "acct:" + other["id"], scale=1.25)
        self.assertGreater(image.size[0], 0)


class SettingsTests(unittest.TestCase):
    def test_the_controller_keeps_a_layout_and_rejects_bad_ones(self):
        controller = Controller()
        try:
            controller.action("taskbar", {"layout": {"main": ["codex", "acct:claude-b"], "left": [None, "claude"]}})
            self.assertEqual(controller.snapshot()["taskbarLayout"], {"main": ["codex", "acct:claude-b"], "left": [None, "claude"]})
            with self.assertRaises(ValueError):
                controller.action("taskbar", {"layout": {"main": ["nope", None]}})
            controller.action("taskbar", {"display": "left"})  # the tray menu: all of it on the left display
            self.assertEqual(controller.snapshot()["taskbarLayout"], {"left": ["codex", "acct:claude-b"]})
            controller.action("taskbar", {"layout": {}})
            self.assertEqual(tl.layout(controller.snapshot()), {})
        finally:
            controller.close()

    def view(self, **extra):
        from tests.test_fullview import FullViewTests, state
        case = FullViewTests("test_settings_menu_toggles_preferences")
        case.setUp()
        displays = [{"id": "main", "label": "Main display"}, {"id": "left", "label": "Left display"}]
        settings = dict(taskbarAvailable=True, taskbar=True, taskbarDisplays=displays,
                        taskbarLayout={"main": ["claude", None]})
        settings.update(extra)
        case.view.set_state(state(**settings))
        case.view.frame()
        case.click("settings")
        return case

    def test_opening_a_dropdown_does_not_change_the_slot(self):
        case = self.view()
        case.click("slot:main:1")
        self.assertEqual(case.view.ui.slot_menu, ("main", 1))
        self.assertFalse(any(name == "taskbar" for name, _ in case.controller.calls))
        self.assertTrue(any(action == "slot-pick:1" for _, hits in case.view.overlay_hits for _, action, _ in hits))
        case.click("slot-pick:1")
        self.assertEqual(case.controller.calls[-1], ("taskbar", {"layout": {"main": ["claude", "next:claude"]}}))
        self.assertIsNone(case.view.ui.slot_menu)
        self.assertEqual(case.view.ui.menu, "settings")

    def test_dropdown_keyboard_selection_and_escape(self):
        case = self.view()
        case.click("slot:main:0")
        case.view.key("down")
        case.view.key("enter")
        self.assertEqual(case.controller.calls[-1], ("taskbar", {"layout": {"main": ["next:claude", None]}}))
        case.view.frame()
        case.click("slot:left:0")
        count = len(case.controller.calls)
        case.view.key("escape")
        self.assertIsNone(case.view.ui.slot_menu)
        self.assertEqual(case.view.ui.menu, "settings")
        self.assertEqual(len(case.controller.calls), count)

    def test_outside_click_only_dismisses_dropdown(self):
        case = self.view()
        case.click("slot:main:0")
        case.view.mouse_down(5, 5)
        case.view.mouse_up(5, 5)
        self.assertIsNone(case.view.ui.slot_menu)
        self.assertEqual(case.view.ui.menu, "settings")

    def test_long_dropdown_scrolls_and_stays_inside_the_window(self):
        from tests.test_fullview import account
        accounts = [account(i, "claude", active=i == 0) for i in range(30)]
        case = self.view(accounts=accounts)
        case.click("slot:main:0")
        case.view.key("end")
        case.view.frame()
        box, hits = case.view.overlay_hits[-1]
        self.assertGreaterEqual(box[0], 8)
        self.assertGreaterEqual(box[1], 8)
        self.assertLessEqual(box[1] + box[3], case.view.height - 8)
        self.assertLessEqual(len(hits), 10)
        self.assertEqual(hits[-1][1], f"slot-pick:{len(case.view.ui.slot_options) - 1}")
        case.view.key("enter")
        self.assertEqual(case.controller.calls[-1][1]["layout"], {})

    def test_changed_account_choices_close_an_open_dropdown(self):
        from tests.test_fullview import state, account
        case = self.view()
        case.click("slot:main:0")
        changed = dict(case.view.state, accounts=[account(1, "claude", active=True)])
        case.view.set_state(changed)
        self.assertIsNone(case.view.ui.slot_menu)


class NextUseTests(unittest.TestCase):
    def test_preview_uses_the_actual_switch_selection_policy(self):
        from account_switcher.live import LiveAccounts
        from account_switcher.core import next_account, demo_accounts
        rows = demo_accounts()
        current = rows[0].id
        manager = LiveAccounts.__new__(LiveAccounts)
        manager.accounts = lambda: rows
        expected = manager._best_other("claude", current)
        self.assertEqual(next_account(rows, "claude", current).id, expected.id)
        rows[1].status = "Login expired"
        self.assertIsNone(next_account(rows, "claude", current))
        rows[1].status = ""
        rows[1].five_hour = 100
        self.assertIsNone(next_account(rows, "claude", current))

    def test_preview_follows_weekly_reset_then_headroom(self):
        from account_switcher.core import demo_accounts, next_account
        from dataclasses import replace
        a = demo_accounts()[1]
        early = replace(a, id="early", five_hour=50, weekly=50, weekly_reset_at=10000)
        later = replace(a, id="later", five_hour=10, weekly=10, weekly_reset_at=20000)
        self.assertEqual(next_account([later, early], "claude", "active").id, "early")
        early.weekly_reset_at = later.weekly_reset_at
        self.assertEqual(next_account([later, early], "claude", "active").id, "later")

    def test_snapshot_and_taskbar_render_the_next_account_not_the_active_one(self):
        state = demo_state()
        for provider in ("claude", "codex"):
            account = fr.slot_account(state, "next:" + provider)
            self.assertIsNotNone(account)
            self.assertFalse(account["active"])
            layout, width = fr.build_block(state, "next:" + provider)
            texts = [t[2] for t in layout.texts]
            self.assertIn(fr.display_name(account), texts)
            self.assertTrue(any("Next use" in t for t in texts))
            self.assertGreater(width, 0)
        state["nextUse"]["claude"] = None
        self.assertIsNone(fr.slot_account(state, "next:claude"))
        self.assertEqual(fr.block_width(state, "next:claude"), 0)


if __name__ == "__main__":
    unittest.main()
