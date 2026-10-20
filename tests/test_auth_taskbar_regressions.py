"""Offline regressions for token ownership and the Windows taskbar view."""
import base64
import json
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from account_switcher.live import LiveAccounts
if sys.platform == "win32":
    from account_switcher import taskbar as tb


def secret_for(provider, access="saved-access", expires=1):
    if provider == "claude":
        return {"credentials": {"claudeAiOauth": {
            "accessToken": access, "refreshToken": "saved-refresh", "expiresAt": expires * 1000}}}
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
    return {"auth": {"tokens": {"access_token": f"header.{payload}.signature",
                                "refresh_token": "saved-refresh", "account_id": "workspace"}}}


class TokenOwnershipTests(unittest.TestCase):
    def manager(self, provider):
        manager = LiveAccounts.__new__(LiveAccounts)
        manager.lock = threading.RLock()
        manager.meta = {"accounts": {"target": {"identity": "identity-B", "provider": provider}}}
        manager.live_ids = {provider: "other"}
        manager.legacy_held = {}
        manager.token_locks = {}
        manager.vault = Mock()
        manager.vault.read_secret.return_value = secret_for(provider)
        client = manager.providers = {provider: Mock()}
        client[provider].refresh.return_value = secret_for(provider, expires=int(time.time()) + 3600)
        return manager, client[provider]

    def request(self, manager, provider):
        return manager.window_token("target") if provider == "claude" else manager.credentials("target")

    def test_switch_while_request_waits_does_not_rotate_client_login(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                manager, client = self.manager(provider)
                attempted = threading.Event()
                lock = threading.Lock()
                lock.acquire()

                class ObservedLock:
                    def __enter__(self):
                        attempted.set()
                        if not lock.acquire(timeout=3):
                            raise RuntimeError("test lock timed out")

                    def __exit__(self, *args):
                        lock.release()

                manager.token_locks["target"] = ObservedLock()
                result, errors = [], []

                def request():
                    try:
                        result.append(self.request(manager, provider))
                    except Exception as error:
                        errors.append(error)

                worker = threading.Thread(target=request, daemon=True)
                worker.start()
                try:
                    self.assertTrue(attempted.wait(2), "request never reached the token lock")
                    with manager.lock:
                        manager.live_ids[provider] = "target"
                        live = secret_for(provider, access="official-access")
                        client.read_live.return_value = NS(identity="identity-B", secret=live)
                finally:
                    lock.release()
                worker.join(3)
                self.assertFalse(worker.is_alive(), "request deadlocked with the switch")
                self.assertEqual(errors, [])
                expected = "official-access" if provider == "claude" else (
                    live["auth"]["tokens"]["access_token"], "workspace")
                self.assertEqual(result, [expected])
                client.refresh.assert_not_called()
                manager.vault.write_secret.assert_not_called()

    def test_saved_accounts_still_renew_and_persist(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                manager, client = self.manager(provider)
                self.request(manager, provider)
                client.refresh.assert_called_once()
                manager.vault.write_secret.assert_called_once_with("target", client.refresh.return_value)

    def test_missing_or_replaced_live_login_never_rotates_saved_copy(self):
        for provider in ("claude", "codex"):
            for login in (None, NS(identity="different-account")):
                with self.subTest(provider=provider, login=login):
                    manager, client = self.manager(provider)
                    manager.live_ids[provider] = "target"
                    client.read_live.return_value = login
                    with self.assertRaisesRegex(RuntimeError, "login is changing"):
                        self.request(manager, provider)
                    client.refresh.assert_not_called()
                    manager.vault.write_secret.assert_not_called()

    def test_unknown_account_raises(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                manager, client = self.manager(provider)
                manager.meta["accounts"].clear()
                with self.assertRaisesRegex(ValueError, "Unknown account"):
                    self.request(manager, provider)
                client.refresh.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows taskbar integration")
class TaskbarRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Synthetic HWNDs in these tests must never change real desktop windows.
        styles = patch.object(tb.user32, "GetWindowLongW", return_value=tb.WS_EX_TOPMOST)
        styles.start()
        self.addCleanup(styles.stop)

    def view(self):
        tray = NS(state={"taskbar": True, "accounts": []}, quitting=False,
                  controller=NS(taskbar_displays=[{"id": "main", "label": "Main display"}], notify=Mock()))
        view = tb.TaskbarView(tray)
        view.hooked, view.hwnd = True, 55
        return view

    def test_first_launch_hidden_taskbar_is_retried_without_activation(self):
        view = self.view()
        block = Mock()
        view.blocks[("main", 0)] = block
        bar = tb.Bar(123, "main", "Main display", (0, 1000, 1920, 1048), 1, 8, 1700, [], False, True)
        visible = {"value": False}
        with patch.object(tb, "taskbars", return_value=[(123, "main", "Main display")]), \
                patch.object(tb, "read_bar", return_value=bar) as read, \
                patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb.user32, "IsWindowVisible", side_effect=lambda h: visible["value"]), \
                patch.object(tb, "taskbar_covered", return_value=False), \
                patch.object(tb.user32, "SetTimer") as timer, patch.object(tb.user32, "KillTimer"):
            view._sync()
            self.assertEqual(view.bars, {})
            self.assertEqual(view.waiting, {"main": 123})
            read.assert_not_called()
            timer.assert_any_call(55, tb.TIMER_COVER, 1000, None)
            block.close.assert_called_once()
            visible["value"] = True
            view.on_timer(tb.TIMER_COVER, 0)
            timer.assert_any_call(55, tb.TIMER_LAYOUT, 100, None)
            view.on_timer(tb.TIMER_LAYOUT, 0)
            self.assertIs(view.bars["main"], bar)
            self.assertEqual(view.waiting, {})
            read.assert_called_once_with(123, "main", "Main display", previous=None, measurements=view.measurements)

    def test_fullscreen_refresh_retains_secondary_bar_until_it_returns(self):
        view = self.view()
        view.tray.state["taskbarLayout"] = {"right": ["claude", None]}
        bar = tb.Bar(222, "right", "Right display", (1920, 1000, 3840, 1048), 1, 1928, 3700, [], False, True)
        view.bars["right"] = bar
        block = Mock(hwnd=456, closing=False, hosted=None, x=2000, size=(100, 44), bar=bar, slot="claude")
        view.blocks[("right", 0)] = block
        visible = {"value": False}
        with patch.object(tb, "taskbars", return_value=[(222, "right", "Right display")]), \
                patch.object(tb.user32, "IsWindowVisible", side_effect=lambda h: visible["value"]), \
                patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb, "read_bar", return_value=bar) as read, \
                patch.object(tb, "taskbar_covered", return_value=False), \
                patch.object(tb.fr, "slot_account", return_value={"windows": [1]}), \
                patch.object(tb.fr, "block_width", return_value=100), \
                patch.object(tb, "place_blocks", return_value={0: (2000, 100, 1)}), \
                patch.object(tb.user32, "ShowWindow") as show, \
                patch.object(tb.user32, "SetTimer"), patch.object(tb.user32, "KillTimer"):
            view._sync()  # layout refresh while Explorer hides the bar for the game
            self.assertIs(view.bars["right"], bar)
            self.assertEqual(view.waiting, {"right": 222})
            self.assertTrue(view.covered["right"])
            read.assert_not_called()
            block.close.assert_not_called()
            show.assert_called_once_with(456, tb.SW_HIDE)
            visible["value"] = True
            view.on_timer(tb.TIMER_COVER, 0)  # game left fullscreen; no activation/shell event
            self.assertFalse(view.covered["right"])
            self.assertEqual(view.waiting, {})
            show.assert_any_call(456, tb.SW_SHOWNA)
            read.assert_called_once_with(222, "right", "Right display", previous=bar,
                                         measurements=view.measurements)

    def test_layout_is_remeasured_after_animation_with_same_signature(self):
        rect = (0, 1000, 1920, 1048)
        moving = [(10, 750, 1000, 1048), (760, 1500, 1000, 1048)]
        settled = [(800, 900, 1000, 1048)]
        with patch.object(tb, "window_rect", return_value=rect), \
                patch.object(tb.user32, "IsWindowVisible", return_value=True), \
                patch.object(tb, "auto_hide", return_value=False), \
                patch.object(tb, "layout_signature", return_value=("same windows",)), \
                patch.object(tb.fl, "monitor_at", return_value=(None, None, 1)), \
                patch.object(tb, "light_taskbar", return_value=False), \
                patch.object(tb.time, "monotonic", return_value=100) as clock, \
                patch.object(tb._COM, "buttons", side_effect=[moving, settled]) as measure:
            first = tb.read_bar(123)
            self.assertIs(tb.read_bar(123, previous=first), first)
            clock.return_value = 102
            second = tb.read_bar(123, previous=first)
        self.assertEqual(measure.call_count, 2)
        self.assertEqual(second.occupied, [(800, 900)])

    def test_layout_event_schedules_settled_measurement(self):
        view = self.view()
        with patch.object(tb.user32, "SetTimer") as timer:
            view.later()
        timer.assert_any_call(55, tb.TIMER_LAYOUT, 400, None)
        timer.assert_any_call(55, tb.TIMER_SETTLED, 1900, None)

    def test_settled_timer_forces_layout_refresh_and_stops(self):
        view = self.view()
        view.stale = False
        view.sync = Mock()
        with patch.object(tb.user32, "KillTimer") as timer:
            view.on_timer(tb.TIMER_SETTLED, 0)
        timer.assert_called_once_with(55, tb.TIMER_SETTLED)
        self.assertTrue(view.stale)
        view.sync.assert_called_once()

    def test_hidden_block_is_restored_without_coverage_transition(self):
        view = self.view()
        view.bars = {"main": NS(hwnd=123, rect=(0, 1000, 1920, 1048))}
        view.covered = {"main": False}
        view.blocks[("main", 0)] = NS(hwnd=456, closing=False, hosted=None, x=100, size=(200, 44))
        with patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb, "taskbar_covered", return_value=False), \
                patch.object(tb.user32, "IsWindowVisible", side_effect=lambda hwnd: hwnd == 123), \
                patch.object(tb.user32, "ShowWindow") as show, \
                patch.object(tb.user32, "KillTimer"):
            view.follow_taskbar()
        show.assert_called_once_with(456, tb.SW_SHOWNA)

    def test_fullscreen_keeps_blocks_hidden(self):
        view = self.view()
        view.bars = {"main": NS(hwnd=123, rect=(0, 1000, 1920, 1048))}
        view.covered = {"main": True}
        view.blocks[("main", 0)] = NS(hwnd=456, closing=False, hosted=None, x=100, size=(200, 44))
        with patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb, "taskbar_covered", return_value=True), \
                patch.object(tb.user32, "IsWindowVisible", return_value=True), \
                patch.object(tb.user32, "ShowWindow") as show, \
                patch.object(tb.user32, "SetTimer") as timer:
            view.follow_taskbar()
        show.assert_called_once_with(456, tb.SW_HIDE)
        timer.assert_called_once_with(55, tb.TIMER_COVER, 1000, None)

    def test_uncovered_blocks_keep_recovery_timer_without_layout_polling(self):
        view = self.view()
        view.bars = {"right": NS(hwnd=222, rect=(1920, 1000, 3840, 1048))}
        view.blocks[("right", 0)] = NS(hwnd=456, closing=False, hosted=None, x=2000, size=(200, 44))
        with patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb.user32, "IsWindowVisible", return_value=True), \
                patch.object(tb, "taskbar_covered", return_value=False), \
                patch.object(tb.user32, "SetTimer") as timer, \
                patch.object(tb.user32, "SetWindowPos") as position, \
                patch.object(tb, "read_bar") as measure:
            view.on_timer(tb.TIMER_COVER, 0)
        timer.assert_called_once_with(55, tb.TIMER_COVER, 1000, None)
        position.assert_not_called()  # a healthy block must not rise over another overlay
        measure.assert_not_called()

    def test_hidden_owner_keeps_secondary_block_hidden_until_layout_refresh(self):
        view = self.view()
        view.bars = {"right": NS(hwnd=222, rect=(1920, 1000, 3840, 1048))}
        view.blocks[("right", 0)] = NS(hwnd=456, closing=False, hosted=None, x=2000, size=(200, 44))
        with patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb.user32, "IsWindowVisible", side_effect=lambda hwnd: hwnd == 456), \
                patch.object(tb, "taskbar_covered") as coverage, \
                patch.object(tb.user32, "ShowWindow") as show, patch.object(tb.user32, "SetTimer"):
            view.follow_taskbar()
        show.assert_called_once_with(456, tb.SW_HIDE)
        coverage.assert_not_called()

    def test_activation_retries_previously_hidden_taskbar(self):
        view = self.view()
        view.wanted = {"main": ["claude", "codex"]}
        view.follow_taskbar = Mock()
        with patch.object(tb.user32, "SetTimer") as timer:
            view.settle()
        timer.assert_any_call(55, tb.TIMER_LAYOUT, 100, None)
        view.follow_taskbar.assert_called_once()

    def test_secondary_taskbar_stays_on_its_display_while_hidden(self):
        with patch.object(tb.user32, "FindWindowW", return_value=111), \
                patch.object(tb.user32, "FindWindowExW", side_effect=lambda parent, after, *_: None if after else 222), \
                patch.object(tb.user32, "IsWindowVisible", return_value=False), \
                patch.object(tb, "window_rect", side_effect=lambda h: (0, 1000, 1920, 1048) if h == 111 else (1920, 1000, 3840, 1048)):
            self.assertEqual(tb.taskbars(), [(111, "main", "Main display"), (222, "right", "Right display")])

    def test_hidden_secondary_taskbar_does_not_move_its_block_to_main(self):
        view = self.view()
        view.tray.state["taskbarLayout"] = {"right": ["claude", None]}
        block = Mock()
        view.blocks[("right", 0)] = block
        with patch.object(tb, "taskbars", return_value=[(111, "main", "Main display"), (222, "right", "Right display")]), \
                patch.object(tb, "read_bar", return_value=None) as read, \
                patch.object(tb.user32, "IsWindowVisible", return_value=False), \
                patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb.user32, "KillTimer"), patch.object(tb.user32, "SetTimer"):
            view._sync()
        read.assert_not_called()
        self.assertEqual(view.bars, {})
        self.assertEqual(view.waiting, {"right": 222})
        block.close.assert_called_once()

    def two_displays(self):
        view = self.view()
        view.tray.state["taskbarLayout"] = {"main": ["claude", None], "right": ["codex", None]}
        main = tb.Bar(111, "main", "Main display", (0, 1000, 1920, 1048), 1, 8, 1700, [], False, True)
        right = tb.Bar(222, "right", "Right display", (1920, 1000, 3840, 1048), 1, 1928, 3700, [], False, True)
        view.bars = {"main": main, "right": right}
        for key, bar, hwnd in (("main", main, 333), ("right", right, 444)):
            view.blocks[(key, 0)] = Mock(hwnd=hwnd, closing=False, hosted=None, x=bar.left, size=(100, 44), bar=bar,
                                         slot="claude" if key == "main" else "codex")
        return view, main, right

    def desktop(self):
        stack = ExitStack()
        for name in ("SetTimer", "KillTimer", "ShowWindow", "SetWindowPos"):
            stack.enter_context(patch.object(tb.user32, name))
        for name in ("IsWindow", "IsWindowVisible"):
            stack.enter_context(patch.object(tb.user32, name, return_value=True))
        stack.enter_context(patch.object(tb, "auto_hide", return_value=False))
        stack.enter_context(patch.object(tb, "taskbar_covered", return_value=False))
        stack.enter_context(patch.object(tb, "block_needs_raise", return_value=False))
        stack.enter_context(patch.object(tb.fr, "slot_account", return_value={"windows": [1]}))
        stack.enter_context(patch.object(tb.fr, "block_width", return_value=100))
        return stack

    def test_secondary_missing_from_discovery_keeps_blocks_and_is_rediscovered(self):
        view, main, right = self.two_displays()
        with self.desktop(), \
                patch.object(tb, "taskbars", return_value=[(111, "main", "Main display")]) as discovery, \
                patch.object(tb, "read_bar", return_value=main):
            view._sync()  # Start is open: enumeration skips the raised secondary taskbar
            self.assertIs(view.bars["right"], right)
            self.assertEqual(view.waiting, {"right": 222})
            view.blocks[("right", 0)].close.assert_not_called()
            discovery.return_value = [(111, "main", "Main display"), (222, "right", "Right display")]
            view.on_timer(tb.TIMER_COVER, 0)
            view.on_timer(tb.TIMER_LAYOUT, 0)
            self.assertEqual(view.waiting, {})
            self.assertIn("right", view.bars)

    def test_unreadable_visible_secondary_keeps_geometry_and_retries(self):
        view, main, right = self.two_displays()
        with self.desktop(), \
                patch.object(tb, "taskbars", return_value=[(111, "main", "Main display"), (222, "right", "Right display")]), \
                patch.object(tb, "read_bar", side_effect=[main, tb.TaskbarReadPending(), main, right]) as read:
            view._sync()
            self.assertIs(view.bars["right"], right)
            self.assertEqual(view.waiting, {"right": 222})
            view.blocks[("right", 0)].close.assert_not_called()
            view.on_timer(tb.TIMER_COVER, 0)
            view.on_timer(tb.TIMER_LAYOUT, 0)
            self.assertEqual(read.call_count, 4)
            self.assertEqual(view.waiting, {})

    def test_blocks_are_owned_by_our_tray_instead_of_explorer(self):
        view = self.view()
        block = tb.TaskbarBlock(view, "claude")
        block.bar = NS(hwnd=222)
        self.assertEqual(block.owner, 55)

    def test_taskbar_cover_can_raise_block_without_overriding_other_overlays(self):
        with patch.object(tb, "window_rect", return_value=(2000, 1000, 2200, 1044)), \
                patch.object(tb.user32, "WindowFromPoint", return_value=789), \
                patch.object(tb.user32, "GetAncestor", return_value=222) as ancestor:
            self.assertTrue(tb.block_needs_raise(456, 222))
            ancestor.return_value = 999  # screenshot tool/menu/other overlay
            self.assertFalse(tb.block_needs_raise(456, 222))

    def test_measurement_completion_posts_without_touching_windows_on_worker(self):
        view = self.view()
        view.sync = Mock()
        with patch.object(tb.user32, "PostMessageW") as post:
            view.measurement_ready(222)
        post.assert_called_once_with(55, tb.WM_APP_TASKBAR_MEASURED, 222, 0)
        view.sync.assert_not_called()

    def test_measurement_message_consumes_results_and_ignores_disabled_view(self):
        view = self.view()
        view.bars["right"] = NS(hwnd=222, measured_at=100)
        view.stale = False
        view.sync = Mock()
        view.on_measurement(222, 0)
        self.assertEqual(view.bars["right"].measured_at, 0)
        self.assertTrue(view.stale)
        view.sync.assert_called_once()
        view.hooked = False
        view.sync.reset_mock()
        view.on_measurement(222, 0)
        view.sync.assert_not_called()

    def test_secondary_taskbar_show_event_restores_its_layout_without_activation(self):
        view = self.view()
        view.tray.state["taskbarLayout"] = {"right": ["claude", None]}
        bar = tb.Bar(222, "right", "Right display", (1920, 1000, 3840, 1048), 1, 1928, 3700, [], False, True)
        def name(hwnd, buffer, count):
            buffer.value = "Shell_SecondaryTrayWnd"
            return len(buffer.value)
        with patch.object(tb, "taskbars", return_value=[(111, "main", "Main display"), (222, "right", "Right display")]), \
                patch.object(tb, "read_bar", side_effect=[None, bar]), \
                patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb.user32, "IsWindowVisible", return_value=True), \
                patch.object(tb, "taskbar_covered", return_value=False), \
                patch.object(tb.user32, "GetClassNameW", side_effect=name), \
                patch.object(tb.user32, "KillTimer"), patch.object(tb.user32, "SetTimer") as timer:
            view._sync()
            self.assertNotIn("right", view.bars)
            view.on_window_event(99, tb.EVENT_OBJECT_SHOW, 222, 0, 0, 0, 0)
            timer.assert_any_call(55, tb.TIMER_LAYOUT, 100, None)
            view.on_timer(tb.TIMER_LAYOUT, 0)
        self.assertIs(view.bars["right"], bar)

    def test_taskbar_events_queue_layout_without_measuring_in_callback(self):
        view = self.view()
        view.later = Mock()
        def name(hwnd, buffer, count):
            buffer.value = "Shell_SecondaryTrayWnd"
            return len(buffer.value)
        with patch.object(tb.user32, "GetClassNameW", side_effect=name), \
                patch.object(tb, "read_bar") as read:
            for event in tb.WINDOW_EVENTS:
                view.on_window_event(99, event, 222, 0, 0, 0, 0)
        self.assertEqual(view.later.call_args_list, [unittest.mock.call(100)] * len(tb.WINDOW_EVENTS))
        read.assert_not_called()

    def test_taskbar_client_reorder_refreshes_layout(self):
        view = self.view()
        view.later = Mock()
        def name(hwnd, buffer, count):
            buffer.value = "Shell_SecondaryTrayWnd"
            return len(buffer.value)
        with patch.object(tb.user32, "GetClassNameW", side_effect=name):
            view.on_window_event(99, tb.EVENT_OBJECT_REORDER, 222, -4, 0, 0, 0)
        view.later.assert_called_once_with(100)

    def test_unexpected_secondary_block_hide_queues_recovery_without_activation(self):
        view = self.view()
        view.covered = {"main": False, "right": False}
        view.blocks[("right", 0)] = NS(hwnd=456, closing=False, hosted=None)
        with patch.object(tb.user32, "SetTimer") as timer, \
                patch.object(tb.user32, "GetClassNameW") as name, \
                patch.object(tb.time, "monotonic", return_value=100):
            view.on_window_event(99, tb.EVENT_OBJECT_HIDE, 456, 0, 0, 0, 0)
        timer.assert_called_once_with(55, tb.TIMER_COVER, 100, None)
        name.assert_not_called()
        self.assertEqual(view.settle_until, 103)

    def test_covered_closing_or_animating_blocks_do_not_restart_layout(self):
        view = self.view()
        view.covered = {"right": True}
        block = NS(hwnd=456, closing=False, hosted=None)
        view.blocks[("right", 0)] = block
        with patch.object(tb.user32, "SetTimer") as timer, patch.object(tb.user32, "GetClassNameW") as name:
            view.on_window_event(99, tb.EVENT_OBJECT_HIDE, 456, 0, 0, 0, 0)
            view.covered["right"] = False
            block.closing = True
            view.on_window_event(99, tb.EVENT_OBJECT_HIDE, 456, 0, 0, 0, 0)
            block.closing = False
            for event in (tb.EVENT_OBJECT_SHOW, tb.EVENT_OBJECT_LOCATIONCHANGE, tb.EVENT_OBJECT_REORDER):
                view.on_window_event(99, event, 456, 0, 0, 0, 0)
        timer.assert_not_called()
        name.assert_not_called()

    def test_unrelated_child_and_late_events_are_ignored(self):
        view = self.view()
        view.later = Mock()
        with patch.object(tb.user32, "GetClassNameW", return_value=0) as name:
            view.on_window_event(99, tb.EVENT_OBJECT_SHOW, 789, 0, 0, 0, 0)
            name.assert_called_once()
            name.reset_mock()
            view.on_window_event(99, tb.EVENT_OBJECT_SHOW, 789, -4, 0, 0, 0)
            view.on_window_event(99, tb.EVENT_OBJECT_SHOW, 789, 0, 1, 0, 0)
            view.hooked = False
            view.on_window_event(99, tb.EVENT_OBJECT_SHOW, 789, 0, 0, 0, 0)
            name.assert_not_called()
        view.later.assert_not_called()

    def test_hook_keeps_callback_alive_and_releases_it_on_disable(self):
        view = self.view()
        view.hooked = False
        view.icon = NS(_hwnd=55)
        with patch.object(tb.user32, "RegisterShellHookWindow"), \
                patch.object(tb.user32, "DeregisterShellHookWindow"), \
                patch.object(tb.user32, "SetTimer"), patch.object(tb.user32, "KillTimer"), \
                patch.object(view, "register_appbar"), patch.object(view, "unregister_appbar"), \
                patch.object(tb.user32, "SetWinEventHook", return_value=999) as register, \
                patch.object(tb.user32, "UnhookWinEvent") as unregister:
            view.hook()
            self.assertIs(register.call_args.args[3], view.window_callback)
            self.assertEqual(view.window_hook, 999)
            view.hook()
            register.assert_called_once()
            view.unhook()
            unregister.assert_called_once_with(999)
            self.assertIsNone(view.window_callback)
            self.assertIsNone(view.window_hook)


@unittest.skipUnless(sys.platform == "win32", "Native Windows owned-window recovery")
class NativeTaskbarRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Off-screen, account-free native windows exercise the actual visibility and
        # z-order APIs without touching Explorer, the running app, or the foreground.
        self.windows = []
        self.addCleanup(self.destroy_windows)
        owner = self.window(None)
        block = self.window(owner)
        self.owner, self.block = owner, block
        self.view = tb.TaskbarView(NS())
        self.view.bars = {"right": NS(hwnd=owner, rect=(-30000, -30000, -29800, -29960))}
        self.view.blocks = {("right", 0): NS(hwnd=block, closing=False, hosted=None, x=-30000, size=(200, 40))}
        self.view.covered = {"right": False}
        for name in ("SetTimer", "KillTimer"):
            timer = patch.object(tb.user32, name)
            timer.start()
            self.addCleanup(timer.stop)

    def window(self, owner, topmost=True):
        hwnd = tb.user32.CreateWindowExW((tb.WS_EX_TOPMOST if topmost else 0) | tb.WS_EX_NOACTIVATE,
                                        "STATIC", "Synthetic taskbar regression", tb.fl.WS_POPUP,
                                        -30000, -30000, 200, 40, owner, None, None, None)
        self.assertTrue(hwnd)
        self.windows.append(hwnd)
        tb.user32.ShowWindow(hwnd, tb.SW_SHOWNA)
        return hwnd

    def destroy_windows(self):
        for hwnd in reversed(self.windows):
            tb.user32.DestroyWindow(hwnd)

    def demote(self):
        self.assertTrue(tb.user32.SetWindowPos(self.block, -2, 0, 0, 0, 0,
                        tb.SWP_NOSIZE | tb.SWP_NOMOVE | tb.SWP_NOACTIVATE | tb.SWP_NOOWNERZORDER))
        self.assertFalse(tb.user32.GetWindowLongW(self.block, tb.GWL_EXSTYLE) & tb.WS_EX_TOPMOST)
        self.assertTrue(tb.user32.IsWindowVisible(self.block))

    def is_above(self, first, second):
        hwnd = tb.user32.GetWindow(first, 2)  # GW_HWNDNEXT: below this window in z-order
        while hwnd:
            if hwnd == second:
                return True
            hwnd = tb.user32.GetWindow(hwnd, 2)
        return False

    def test_visible_demoted_block_recovers_on_timer_without_activation(self):
        self.demote()
        cover = self.window(None, topmost=False)
        self.assertTrue(tb.user32.SetWindowPos(cover, 0, 0, 0, 0, 0,
                        tb.SWP_NOSIZE | tb.SWP_NOMOVE | tb.SWP_NOACTIVATE))
        self.assertTrue(self.is_above(cover, self.block))
        foreground = tb.fl.user32.GetForegroundWindow()
        rect = tb.window_rect(self.block)
        with patch.object(tb, "taskbar_covered", return_value=False):
            self.view.on_timer(tb.TIMER_COVER, 0)  # no shell, hide, or activation event
        self.assertTrue(tb.user32.GetWindowLongW(self.block, tb.GWL_EXSTYLE) & tb.WS_EX_TOPMOST)
        self.assertTrue(tb.user32.IsWindowVisible(self.block))
        self.assertTrue(self.is_above(self.block, cover))
        self.assertEqual(tb.window_rect(self.block), rect)
        self.assertEqual(tb.fl.user32.GetForegroundWindow(), foreground)

    def test_hidden_block_recovers_on_timer_without_native_event(self):
        tb.user32.ShowWindow(self.block, tb.SW_HIDE)
        self.assertFalse(tb.user32.IsWindowVisible(self.block))
        with patch.object(tb, "taskbar_covered", return_value=False):
            self.view.on_timer(tb.TIMER_COVER, 0)
        self.assertTrue(tb.user32.IsWindowVisible(self.block))

    def test_fullscreen_does_not_promote_demoted_block(self):
        self.demote()
        self.view.covered["right"] = True
        with patch.object(tb, "taskbar_covered", return_value=True):
            self.view.on_timer(tb.TIMER_COVER, 0)
        self.assertFalse(tb.user32.IsWindowVisible(self.block))
        self.assertFalse(tb.user32.GetWindowLongW(self.block, tb.GWL_EXSTYLE) & tb.WS_EX_TOPMOST)

    def test_healthy_block_stays_below_existing_topmost_overlay(self):
        overlay = self.window(None)
        self.assertTrue(tb.user32.SetWindowPos(overlay, tb.HWND_TOPMOST, 0, 0, 0, 0,
                        tb.SWP_NOSIZE | tb.SWP_NOMOVE | tb.SWP_NOACTIVATE))
        self.assertTrue(self.is_above(overlay, self.block))
        with patch.object(tb, "taskbar_covered", return_value=False):
            self.view.on_timer(tb.TIMER_COVER, 0)
        self.assertTrue(self.is_above(overlay, self.block))

    def test_topmost_taskbar_can_be_raised_over_independent_block_then_repaired(self):
        bar = self.window(None)
        self.assertTrue(tb.user32.SetWindowPos(bar, tb.HWND_TOPMOST, 0, 0, 0, 0,
                        tb.SWP_NOSIZE | tb.SWP_NOMOVE | tb.SWP_NOACTIVATE))
        self.assertTrue(self.is_above(bar, self.block))
        self.view.bars["right"].hwnd = bar
        with patch.object(tb, "taskbar_covered", return_value=False), \
                patch.object(tb.user32, "WindowFromPoint", return_value=bar):
            self.view.on_timer(tb.TIMER_COVER, 0)
        self.assertTrue(self.is_above(self.block, bar))

    def test_real_block_stays_owned_by_our_hidden_tray_when_taskbar_is_destroyed(self):
        from PIL import Image
        tray = self.window(None)
        tb.user32.ShowWindow(tray, tb.SW_HIDE)
        fake_explorer = self.window(None)
        view = tb.TaskbarView(NS(state={"accounts": []}))
        view.hwnd = tray
        view.later = Mock()
        block = tb.TaskbarBlock(view, "claude")
        self.addCleanup(block._destroy)
        bar = tb.Bar(fake_explorer, "right", "Right display", (-30000, -30000, -29600, -29952),
                     1, -30000, -29600, [], False, True)
        block.place(bar, (-30000, "left", 1))
        with patch.object(block, "render", return_value=(Image.new("RGBA", (200, 44), "black"), [])), \
                patch.object(tb.fr, "block_width", return_value=200):
            block.open()
        self.assertTrue(block.hwnd)
        self.assertEqual(tb.user32.GetWindow(block.hwnd, 4), tray)
        self.assertTrue(tb.user32.IsWindowVisible(block.hwnd))
        tb.user32.DestroyWindow(fake_explorer)
        self.assertTrue(tb.user32.IsWindow(block.hwnd))


if __name__ == "__main__":
    unittest.main()
