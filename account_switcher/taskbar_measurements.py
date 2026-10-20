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
        self.records, self.pending, self.active = {}, {}, set()
        self.rerun = {}
        self.thread = None

    def retain(self, handles):
        with self.lock:
            self.active = set(handles)
            self.records = {h: value for h, value in self.records.items() if h in self.active}
            self.rerun = {h: value for h, value in self.rerun.items() if h in self.active}

    def refresh(self, hwnd):
        """Request one reading after a shell animation, even if an older read is running."""
        with self.lock:
            if hwnd in self.pending:
                self.rerun[hwnd] = self.rerun.get(hwnd, self.pending[hwnd])
            elif hwnd in self.records:
                signature, _, spans = self.records[hwnd]
                self.records[hwnd] = (signature, float("-inf"), spans)

    def buttons(self, hwnd, signature):
        with self.lock:
            self.active.add(hwnd)
            record = self.records.get(hwnd)
            matching = record is not None and record[0] == signature
            if hwnd in self.pending:
                if self.pending[hwnd] != signature or hwnd in self.rerun:
                    self.rerun[hwnd] = signature
            elif not (matching and time.monotonic() - record[1] < 1):
                self.pending[hwnd] = signature
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
                    self.pending.pop(hwnd, None)
                    continue
            try:
                spans = self.measure(hwnd)
            except Exception:
                log.warning("taskbar buttons could not be read", exc_info=True)
                spans = None
            with self.lock:
                self.pending.pop(hwnd, None)
                active = hwnd in self.active
                if active:
                    previous = self.records.get(hwnd)
                    if spans is None and previous is not None and previous[0] == signature:
                        spans = previous[2]  # a transient provider failure cannot erase good geometry
                    self.records[hwnd] = (signature, time.monotonic(), spans)
                    if hwnd in self.rerun:
                        latest = self.rerun.pop(hwnd)
                        self.pending[hwnd] = latest
                        self.requests.put((hwnd, latest))
            if active:
                self.ready(hwnd)
