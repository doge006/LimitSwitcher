"""Offline regressions for token ownership and the Windows taskbar view."""
import base64
import json
import sys
import threading
import time
import unittest
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
    def view(self):
        tray = NS(state={"taskbar": True, "accounts": []}, quitting=False,
                  controller=NS(taskbar_displays=[{"id": "main", "label": "Main display"}], notify=Mock()))
        view = tb.TaskbarView(tray)
        view.hooked, view.hwnd = True, 55
        return view

    def test_hidden_taskbar_closes_blocks_and_can_return(self):
        view = self.view()
        block = Mock()
        view.blocks[("main", 0)] = block
        bar = tb.Bar(123, "main", "Main display", (0, 1000, 1920, 1048), 1, 8, 1700, [], False, True)
        with patch.object(tb, "taskbars", return_value=[(123, "main", "Main display")]), \
                patch.object(tb, "read_bar", side_effect=[None, bar]), \
                patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb, "taskbar_covered", return_value=False), \
                patch.object(tb.user32, "KillTimer"):
            view._sync()
            self.assertEqual(view.bars, {})
            block.close.assert_called_once()
            view.stale = True
            view._sync()
            self.assertIs(view.bars["main"], bar)

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
        view.blocks[("main", 0)] = NS(hwnd=456, closing=False, x=100, size=(200, 44))
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
        view.blocks[("main", 0)] = NS(hwnd=456, closing=False, x=100, size=(200, 44))
        with patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb, "taskbar_covered", return_value=True), \
                patch.object(tb.user32, "IsWindowVisible", return_value=True), \
                patch.object(tb.user32, "ShowWindow") as show, \
                patch.object(tb.user32, "SetTimer") as timer:
            view.follow_taskbar()
        show.assert_called_once_with(456, tb.SW_HIDE)
        timer.assert_called_once_with(55, tb.TIMER_COVER, 1000, None)

    def test_hidden_owner_keeps_secondary_block_hidden_until_layout_refresh(self):
        view = self.view()
        view.bars = {"right": NS(hwnd=222, rect=(1920, 1000, 3840, 1048))}
        view.blocks[("right", 0)] = NS(hwnd=456, closing=False, x=2000, size=(200, 44))
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
        def windows(callback, _):
            callback(222, 0)

        def name(hwnd, buffer, count):
            buffer.value = "Shell_SecondaryTrayWnd"
            return len(buffer.value)

        with patch.object(tb.user32, "FindWindowW", return_value=111), \
                patch.object(tb.user32, "EnumWindows", side_effect=windows), \
                patch.object(tb.user32, "GetClassNameW", side_effect=name), \
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
                patch.object(tb.user32, "KillTimer"):
            view._sync()
        read.assert_called_once_with(222, "right", "Right display", previous=None)
        self.assertEqual(view.bars, {})
        block.close.assert_called_once()

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
        view.blocks[("right", 0)] = NS(hwnd=456, closing=False)
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
        block = NS(hwnd=456, closing=False)
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


if __name__ == "__main__":
    unittest.main()
