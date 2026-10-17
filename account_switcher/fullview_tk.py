"""Linux host for the full view: a plain Tk window showing frames drawn by fullview.FullView.

Tk runs on a thread of its own, started when the window opens and gone when it closes, so the
tray's own loop is untouched and nothing is left running while the window is closed. Other
threads reach it through Tk's after(), which tkinter hands to the Tk thread.
"""
import logging
from pathlib import Path
import threading

from . import fullview_render as vr
from .fullview import FullView, window_size

log = logging.getLogger("account_switcher.fullview")
ICON = Path(__file__).with_name("static") / "assets" / "switcher.png"
CURSORS = {"arrow": "", "hand": "hand2", "text": "xterm"}
KEYS = {"Escape": "escape", "Return": "enter", "KP_Enter": "enter", "BackSpace": "backspace", "Delete": "delete",
        "Left": "left", "Right": "right", "Home": "home", "End": "end", "Up": "up", "Down": "down", "Tab": "tab"}


class TkFullView:
    def __init__(self, tray):
        self.tray = tray
        self.root = None
        self.lock = threading.Lock()

    # ---------- any thread ----------
    def post_show(self):
        with self.lock:
            root = self.root
            if root is None:
                self.root = "starting"
                threading.Thread(target=self.run, daemon=True, name="full-view").start()
                return
        if root != "starting":
            root.after(0, self.raise_window)

    def post_state(self):
        root = self.root
        if root not in (None, "starting"):
            root.after(0, lambda: self.view and self.view.set_state(self.tray.state))

    def dismiss(self):
        root = self.root
        if root not in (None, "starting"):
            root.after(0, self.close)

    # ---------- the Tk thread ----------
    def run(self):
        try:
            self.open_window()
        except Exception:
            log.exception("full view window failed")
        finally:
            if getattr(self, "view", None):
                self.view.close()
            self.view = self.photo = self.label = None
            with self.lock:
                self.root = None
            # Tk objects must be freed on the thread that made them, never later by another one.
            import gc
            gc.collect()

    def open_window(self):
        import tkinter
        try:
            from PIL import ImageTk
        except ImportError:
            ImageTk = None
        self.view = None
        root = tkinter.Tk(className="AccountSwitcher")
        with self.lock:
            self.root = root  # callbacks queued now run once the loop starts
        self.tk, self.ImageTk = tkinter, ImageTk
        self.timers, self.pending_draw, self.photo, self.cursor = {}, False, None, "arrow"
        root.title("LimitSwitcher")
        root.configure(background="#%02x%02x%02x" % vr.BG)
        try:
            root.iconphoto(True, tkinter.PhotoImage(file=str(ICON)))
        except tkinter.TclError:
            pass
        self.scale = max(1.0, round(root.winfo_fpixels("1i") / 96 * 4) / 4)
        width, height = window_size(root.winfo_screenwidth() / self.scale, root.winfo_screenheight() / self.scale - 60)
        pw, ph = round(width * self.scale), round(height * self.scale)
        root.geometry(f"{pw}x{ph}+{(root.winfo_screenwidth() - pw) // 2}+{max(0, (root.winfo_screenheight() - ph) // 2)}")
        root.minsize(round(520 * self.scale), round(420 * self.scale))
        self.label = tkinter.Label(root, borderwidth=0, highlightthickness=0, background=root["background"])
        self.label.pack(fill="both", expand=True)
        self.view = FullView(self.tray.controller, self, self.tray.state)
        self.view.resize(width, height, self.scale)
        if self.view.content_h < height:  # no empty space under the cards
            ph = round(max(self.view.content_h, 420) * self.scale)
            root.geometry(f"{pw}x{ph}+{(root.winfo_screenwidth() - pw) // 2}+{max(0, (root.winfo_screenheight() - ph) // 2)}")
        label = self.label
        label.bind("<Configure>", lambda e: self.resize(e.width, e.height))
        label.bind("<Motion>", lambda e: self.view.mouse_move(e.x / self.scale, e.y / self.scale))
        label.bind("<Leave>", lambda e: self.view.mouse_leave())
        label.bind("<ButtonPress-1>", lambda e: self.view.mouse_down(e.x / self.scale, e.y / self.scale))
        label.bind("<ButtonRelease-1>", lambda e: self.view.mouse_up(e.x / self.scale, e.y / self.scale))
        label.bind("<Button-4>", lambda e: self.view.wheel(-vr.SCROLL_STEP))
        label.bind("<Button-5>", lambda e: self.view.wheel(vr.SCROLL_STEP))
        label.bind("<MouseWheel>", lambda e: self.view.wheel(-e.delta / 120 * vr.SCROLL_STEP))
        root.bind("<Key>", self.on_key)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.raise_window()
        root.mainloop()

    def raise_window(self):
        root = self.root
        root.deiconify()
        root.lift()
        root.focus_force()

    def close(self):
        root = self.root
        if root not in (None, "starting"):
            for after_id in self.timers.values():
                root.after_cancel(after_id)
            self.timers = {}
            root.destroy()

    def resize(self, width, height):
        if width > 1 and height > 1:
            self.view.resize(width / self.scale, height / self.scale, self.scale)
            self.invalidate()

    def draw(self):
        self.pending_draw = False
        if not self.view or not self.view.width:
            return
        image = self.view.frame()
        if self.ImageTk is not None:
            if self.photo is not None and (self.photo.width(), self.photo.height()) == image.size:
                self.photo.paste(image)
            else:
                self.photo = self.ImageTk.PhotoImage(image)
                self.label.configure(image=self.photo)
        else:  # Pillow without its Tk module: hand Tk a PPM
            import io
            data = io.BytesIO()
            image.save(data, "PPM")
            self.photo = self.tk.PhotoImage(data=data.getvalue())
            self.label.configure(image=self.photo)

    def on_key(self, event):
        ctrl = bool(event.state & 0x4)
        name = KEYS.get(event.keysym)
        if ctrl and event.keysym.lower() in ("a", "v"):
            self.view.key(event.keysym.lower(), True)
        elif name:
            self.view.key(name, ctrl)
        elif event.char and event.char.isprintable() and not ctrl:
            self.view.char(event.char)

    # ---------- the host interface FullView uses ----------
    def invalidate(self):
        if not self.pending_draw and self.root not in (None, "starting"):
            self.pending_draw = True
            self.root.after_idle(self.draw)

    def set_timer(self, name, ms):
        self.kill_timer(name)
        self.timers[name] = self.root.after(max(10, int(ms)), lambda: self.fire(name))

    def fire(self, name):
        self.timers.pop(name, None)
        if self.view:
            self.view.timer(name)

    def kill_timer(self, name):
        after_id = self.timers.pop(name, None)
        if after_id and self.root not in (None, "starting"):
            self.root.after_cancel(after_id)

    def has_timer(self, name):
        return name in self.timers

    def set_cursor(self, kind):
        if kind != self.cursor:
            self.cursor = kind
            self.label.configure(cursor=CURSORS.get(kind, ""))

    def clipboard(self):
        try:
            return self.root.clipboard_get()
        except self.tk.TclError:
            return ""
