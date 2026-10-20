"""The taskbar host transport: frames out, mouse events back, a hung host replaced."""
import sys
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from account_switcher import taskbar_host
from account_switcher.taskbar_host import Host

if sys.platform == "win32":
    from account_switcher import taskbar as tb

# A stand-in host: answers pings, reports a click for every frame and records what it got.
ECHO = r"""
import base64, json, sys
for line in sys.stdin:
    command = json.loads(line)
    if command["op"] == "ping":
        print(json.dumps({"type": "pong"}), flush=True)
    elif command["op"] == "update":
        assert len(base64.b64decode(command["rgba"])) == command["width"] * command["height"] * 4
        print(json.dumps({"type": "mouse", "id": command["id"], "msg": 514, "x": 3, "y": 4}), flush=True)
"""
SILENT = "import sys\nfor line in sys.stdin: pass\n"


class HostTransportTests(unittest.TestCase):
    def host(self, script):
        woke = threading.Event()
        host = Host(woke.set, _command=[sys.executable, "-c", script])
        self.addCleanup(host.close)
        return host, woke

    def wait_for(self, host, woke, kind):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            woke.wait(0.2)
            woke.clear()
            events = [e for e in host.events() if e["type"] == kind]
            if events:
                return events
        self.fail(f"no {kind} event")

    def test_frame_reaches_host_and_mouse_comes_back(self):
        host, woke = self.host(ECHO)
        host.update(7, 123, 10, 20, 2, 1, b"\0" * 8, 0.5, True)
        self.assertEqual(self.wait_for(host, woke, "mouse"), [dict(type="mouse", id=7, msg=514, x=3, y=4)])

    def test_events_of_removed_blocks_are_dropped(self):
        host, woke = self.host(ECHO)
        host.update(7, 123, 10, 20, 2, 1, b"\0" * 8)
        self.wait_for(host, woke, "mouse")
        host.remove(7)
        host.update(8, 123, 10, 20, 2, 1, b"\0" * 8)
        self.assertEqual({e["id"] for e in self.wait_for(host, woke, "mouse")}, {8})

    def test_bad_frame_is_refused(self):
        host, _ = self.host(ECHO)
        with self.assertRaises(ValueError):
            host.update(7, 123, 0, 0, 2, 2, b"\0" * 8)

    def test_host_that_stops_answering_is_replaced(self):
        with patch.object(taskbar_host, "PING_SECONDS", 0.1), patch.object(taskbar_host, "HUNG_SECONDS", 0.5):
            host, woke = self.host(SILENT)
            host.update(7, 123, 0, 0, 1, 1, b"\0" * 4)
            failed = self.wait_for(host, woke, "failed")
        self.assertEqual(failed[0]["reason"], "render-host-unavailable")
        self.assertTrue(failed[0]["retry"])


@unittest.skipUnless(sys.platform == "win32", "Windows taskbar")
class HostedBlockTests(unittest.TestCase):
    def view(self):
        tray = NS(state={"taskbar": True, "accounts": []}, quitting=False, controller=NS(taskbar_displays=[], notify=Mock()))
        view = tb.TaskbarView(tray)
        view.hooked, view.hwnd, view.host = True, 55, Mock()
        return view

    def test_hosted_block_sends_pixels_to_its_taskbar(self):
        view = self.view()
        block = tb.TaskbarBlock(view, "claude")
        block.hosted, block.host_id, block.x, block.y = view.host, 5, 100, 1004
        block.bar = NS(hwnd=777)
        image = tb.fr.Image.new("RGBA", (2, 1), (255, 0, 0, 255))
        block._push(image, 0.5, (0, 3))
        view.host.update.assert_called_once()
        args = view.host.update.call_args.args
        self.assertEqual(args[:6], (5, 777, 100, 1007, 2, 1))
        self.assertEqual(args[6], bytes([0, 0, 255, 255]) * 2)  # premultiplied BGRA
        self.assertEqual(args[7], 0.5)

    def test_hosted_block_is_left_to_the_taskbar_by_cover_checks(self):
        view = self.view()
        view.bars = {"main": NS(hwnd=123, rect=(0, 1000, 1920, 1048))}
        view.blocks[("main", 0)] = NS(hwnd=456, closing=False, x=10, size=(100, 44), hosted=view.host)
        with patch.object(tb.user32, "IsWindow", return_value=True), \
                patch.object(tb.user32, "IsWindowVisible", return_value=False), \
                patch.object(tb, "taskbar_covered", return_value=False), \
                patch.object(tb.user32, "ShowWindow") as show, patch.object(tb.user32, "SetWindowPos") as raise_, \
                patch.object(tb.user32, "SetTimer"):
            view.follow_taskbar()
        show.assert_not_called()
        raise_.assert_not_called()

    def test_mouse_from_host_reaches_its_block(self):
        view = self.view()
        block = Mock(hosted=view.host, host_id=5, hwnd=99)
        view.blocks[("main", 0)] = block
        view.host.events.return_value = [dict(type="mouse", id=5, msg=0x202, x=3, y=4)]
        view.on_host(0, 0)
        block.host_mouse.assert_called_once_with(dict(type="mouse", id=5, msg=0x202, x=3, y=4))

    def test_mouse_from_host_keeps_hover_without_tracking_our_message_window(self):
        view = self.view()
        block = tb.TaskbarBlock(view, "claude")
        block.hwnd, block.scale, block.hits = 99, 1.0, [((0, 0, 280, 44), "open")]
        block.redraw = Mock()
        with patch.object(tb.fl.user32, "TrackMouseEvent") as track:
            block.host_mouse(dict(msg=tb.fl.WM_MOUSEMOVE, x=10, y=10))
            block.host_mouse(dict(msg=tb.fl.WM_MOUSEMOVE, x=12, y=10))
        track.assert_not_called()  # it would report a leave at once and drop the hand cursor
        self.assertEqual(block.hover, "open")
        block.host_mouse(dict(msg=tb.fl.WM_MOUSELEAVE, x=0, y=0))
        self.assertIsNone(block.hover)
        self.assertFalse(block.tracking)

    def test_host_failure_falls_back_to_popups(self):
        view = self.view()
        host = view.host
        block = Mock(hosted=host, host_id=5, hwnd=99)
        view.blocks[("main", 0)] = block
        host.events.return_value = [dict(type="failed", reason="render-host-unavailable", retry=False)]
        with patch.object(tb.user32, "SetTimer"):
            view.on_host(0, 0)
        block._destroy.assert_called_once()
        host.close.assert_called_once()
        self.assertIsNone(view.host)
        self.assertTrue(view.host_failed)
        self.assertEqual(tb.TaskbarBlock.owner.fget(NS(view=view, hosted=view.host)), 55)  # reopened as popups

    def test_hosted_block_opens_a_real_message_window(self):
        view = self.view()
        view.hwnd = None
        block = tb.TaskbarBlock(view, "claude")
        bar = tb.Bar(777, "main", "Main display", (0, 1000, 1920, 1048), 1, 8, 1700, [], False, True)
        block.place(bar, (8, "left", 1))
        with patch.object(tb.fr, "render_block", return_value=(tb.fr.Image.new("RGBA", (40, 44)), [])),                 patch.object(tb.fr, "block_width", return_value=40), patch.object(tb.fr, "targets", return_value={}):
            block.open()
            self.addCleanup(block._destroy)
            self.assertTrue(block.hwnd)  # Windows refuses a layered message-only window
            self.assertFalse(tb.user32.IsWindowVisible(block.hwnd))
            args = view.host.update.call_args.args
            self.assertEqual(args[:3] + args[4:6], (block.host_id, 777, 8, 40, 44))  # y slides in as it opens
            block._destroy()
        view.host.remove.assert_called_once_with(block.host_id)


if __name__ == "__main__":
    unittest.main()
