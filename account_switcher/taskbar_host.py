"""Asynchronous, pixel-only IPC for the disposable Explorer child-window host (taskbar_host_win).

The tray/routing process never owns an Explorer child or joins its input queue.
Only geometry, rendered pixels and mouse events cross the anonymous pipes.
"""
import base64
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time


MAX_FRAME_BYTES = 16 * 1024 * 1024
PING_SECONDS, HUNG_SECONDS = 0.5, 2.0  # a host that stops pumping messages also stalls Explorer's input


def command():
    root = Path(__file__).resolve().parent.parent
    bundled = root / "runtime" / "python.exe"
    executable = bundled if bundled.is_file() else Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    return [str(executable), "-c", "import sys; sys.path.insert(0, " + repr(str(root))
            + "); from account_switcher.taskbar_host_win import main; main()", str(__import__("os").getpid())]


class Host:
    """Nonblocking controller. Block IDs must be unique across remove/recreate.

    ``notify`` is called on a worker thread and should only post a tray message.
    ``events()`` drains dictionaries with type/id and event-specific fields.
    """
    def __init__(self, notify, *, _popen=subprocess.Popen, _command=None):
        self._notify = notify
        self._popen = _popen
        self._command = _command
        self._condition = threading.Condition()
        self._desired = {}
        self._dirty = set()
        self._events = queue.Queue(maxsize=256)
        self._closed = False
        self._process = None
        self._pong = 0.0
        self._thread = threading.Thread(target=self._run, name="taskbar-host-transport", daemon=True)
        self._thread.start()

    def update(self, block_id, parent, x, y, width, height, rgba, alpha=1.0, hand=False):
        """`rgba`: premultiplied BGRA rows, as a layered window takes them."""
        if width <= 0 or height <= 0 or len(rgba) != width * height * 4 or len(rgba) > MAX_FRAME_BYTES:
            raise ValueError("Invalid taskbar frame")
        frame = dict(op="update", id=block_id, parent=int(parent), x=int(x), y=int(y),
                     width=int(width), height=int(height), rgba=bytes(rgba), alpha=float(alpha), hand=bool(hand))
        with self._condition:
            if not self._closed:
                self._desired[block_id] = frame
                self._dirty.add(block_id)
                self._condition.notify()

    def remove(self, block_id):
        with self._condition:
            if not self._closed:
                self._desired.pop(block_id, None)
                self._dirty.add(block_id)
                self._condition.notify()

    def events(self):
        result = []
        while True:
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                return result

    def _emit(self, event):
        if self._closed:
            return
        try:
            self._events.put_nowait(event)
        except queue.Full:
            return
        try:
            self._notify()
        except Exception:
            pass  # A closing tray must never prevent pipe cleanup.

    def _read(self, process):
        try:
            for line in process.stdout:
                if len(line) > 4096:
                    break
                event = json.loads(line)
                if isinstance(event, dict) and event.get("type") == "pong":
                    self._pong = time.monotonic()
                elif isinstance(event, dict) and event.get("type") in {"mouse", "destroyed", "failed"}:
                    with self._condition:
                        active = "id" not in event or event["id"] in self._desired
                    if active and process is self._process:
                        self._emit(event)
        except (OSError, ValueError):
            pass
        finally:
            with self._condition:
                self._condition.notify()

    def _run(self):
        failures = 0
        while not self._closed:
            with self._condition:
                while not self._desired and not self._closed:
                    self._condition.wait()
            if self._closed:
                return
            process, started = None, time.monotonic()
            try:
                process = self._popen(self._command or command(), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self._process = process
                threading.Thread(target=self._read, args=(process,), name="taskbar-host-events", daemon=True).start()
                with self._condition:
                    self._dirty.update(self._desired)
                pinged = self._pong = time.monotonic()
                while not self._closed:
                    with self._condition:
                        if not self._dirty:
                            self._condition.wait(timeout=0.5)
                        if process.poll() is not None:
                            raise OSError("Taskbar host exited")
                        batch = [self._desired.get(key, dict(op="remove", id=key)) for key in self._dirty]
                        self._dirty.clear()
                    now = time.monotonic()
                    if now - self._pong > HUNG_SECONDS:
                        raise OSError("Taskbar host stopped responding")
                    if now - pinged > PING_SECONDS:
                        batch.append(dict(op="ping"))
                        pinged = now
                    for item in batch:
                        message = dict(item)
                        if "rgba" in message:
                            message["rgba"] = base64.b64encode(message["rgba"]).decode("ascii")
                        process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode("ascii"))
                    if batch:
                        process.stdin.flush()
            except (OSError, ValueError):
                if time.monotonic() - started > 60:
                    failures = 0  # an occasional Explorer restart is not a crash loop
                failures += 1
                self._emit(dict(type="failed", reason="render-host-unavailable", retry=failures <= 3))
            finally:
                if process:
                    self._dispose(process)
                if self._process is process:
                    self._process = None
            if failures > 3:
                return  # Bound crash loops. A new Host is required after persistent failure.
            with self._condition:
                self._condition.wait_for(lambda: self._closed, timeout=min(0.5 * 2 ** failures, 8))

    @staticmethod
    def _dispose(process):
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def close(self):
        with self._condition:
            self._closed = True
            self._desired.clear()
            self._dirty.clear()
            self._condition.notify_all()
        process = self._process
        if process:
            # A renderer stuck in a shell call or a blocked pipe cannot delay the tray.
            threading.Thread(target=self._dispose, args=(process,), daemon=True).start()
