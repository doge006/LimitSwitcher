"""Taskbar UI Automation reads, isolated from the window/message-loop thread."""
import logging
import queue
import threading
import time

log = logging.getLogger("account_switcher.taskbar")


class TaskbarMeasurements:
    """One lazy daemon worker. A stuck provider cannot block the UI or spawn more workers.

    Only plain button rectangles cross threads; COM objects stay on the worker. Reads
    return the last matching measurement immediately and request a refresh at most once
    per second. ready(hwnd) must post a message, never update windows from this thread.
    """

    def __init__(self, measure, ready):
        self.measure, self.ready = measure, ready
        self.lock = threading.Lock()
        self.requests = queue.Queue()
        self.records, self.pending, self.active = {}, set(), set()
        self.thread = None

    def retain(self, handles):
        with self.lock:
            self.active = set(handles)
            self.records = {h: value for h, value in self.records.items() if h in self.active}

    def buttons(self, hwnd, signature):
        with self.lock:
            self.active.add(hwnd)
            record = self.records.get(hwnd)
            matching = record is not None and record[0] == signature
            if hwnd not in self.pending and not (matching and time.monotonic() - record[1] < 1):
                self.pending.add(hwnd)
                self.requests.put((hwnd, signature))
                if self.thread is None:
                    self.thread = threading.Thread(target=self._run, name="taskbar-measurements", daemon=True)
                    self.thread.start()
            return record[2] if matching else None

    def _run(self):
        while True:
            hwnd, signature = self.requests.get()
            with self.lock:
                if hwnd not in self.active:
                    self.pending.discard(hwnd)
                    continue
            try:
                spans = self.measure(hwnd)
            except Exception:
                log.warning("taskbar buttons could not be read", exc_info=True)
                spans = None
            with self.lock:
                self.pending.discard(hwnd)
                active = hwnd in self.active
                if active:
                    self.records[hwnd] = (signature, time.monotonic(), spans)
            if active:
                self.ready(hwnd)
