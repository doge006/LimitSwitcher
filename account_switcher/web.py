"""The app's controller and its loopback API (Claude Code's status line and AFK hook, the macOS
panel, a second launch asking the running copy to show its window). Standard library only."""
from collections import deque
from dataclasses import asdict
import json
from pathlib import Path
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler

from .demo import DemoGateway
from .local_http import LocalServer

ASSETS = Path(__file__).with_name("static")
UPDATE_EVERY = 2 * 3600   # seconds between update checks (the first is at launch)
UPDATE_RETRY_AFTER = 300  # a check that couldn't reach GitHub is tried again after this, up to UPDATE_RETRIES times
UPDATE_RETRIES = 3


def update_wait(ok, failures):
    """Seconds until the next update check: the full interval after a good one, a short retry after
    one that couldn't reach GitHub (the first few times, then the full interval again)."""
    return UPDATE_EVERY if ok or failures > UPDATE_RETRIES else UPDATE_RETRY_AFTER


class Controller:
    def __init__(self, live=False, gateway=None):
        self.condition = threading.Condition(threading.RLock())
        self.operations = threading.Lock()
        self.revision = 0
        self.log = deque(maxlen=150)
        self.log_total = 0
        self.pending = False
        self.afk = False
        self.compact = False  # the tray / menu bar panel shows only the accounts in use
        self.taskbar = True   # Windows: the accounts in use shown on the taskbar
        self.taskbar_display = "main"  # which display's taskbar
        self.taskbar_layout = None     # what each display's taskbar shows (taskbar_layout.py)
        self.taskbar_displays = []     # [{id, label}], filled in by the Windows tray
        self.taskbar_available = False  # set by the Windows tray
        self.name_mode = False   # show names instead of emails (screen sharing)
        self.afk_skip_large = True
        self.mod_seen = 0.0      # when the Claude Code mod last reported
        self.mod_installed = None  # True / False, None until looked up (or when it can't be told)
        self.mod_busy = None     # "installing" while that runs, else an error to show
        self.mod_checked = 0.0
        self.wait_near_reset = True
        self.jev_key_demo = False
        self.jev_compact = False  # compact a session with Jev before it goes on after a swap
        self.clock24 = None      # 24-hour clock: None follows the system
        self.labels = {}         # account id -> name (demo; real accounts keep theirs in the metadata)
        self.closed = False
        if gateway is not None:
            self.gateway = gateway(self.notify)
        elif live:
            from .live import LiveGateway
            self.gateway = LiveGateway(self.notify)
        else:
            self.gateway = DemoGateway(self.notify)
        self.live = getattr(self.gateway, "live", False)
        # Account ids the user picked by hand (panel, full view or reset), so the tray can tell
        # those apart from automatic failovers worth a notification.
        self.manual_swaps = set()
        from .version import VERSION
        self.update = {"current": VERSION, "latest": None, "available": False, "checking": False, "error": None}
        self.quit_app = None             # the host's quit (an update that swaps files needs the app closed)
        self.on_update_available = None  # the host's notification, once per version

    def check_updates(self, announce=True):
        """Any thread: ask GitHub Releases for a newer version (at launch, and from Settings)."""
        from . import updates
        with self.condition:
            if self.update.get("checking"):
                return True
            self.update = dict(self.update, checking=True, error=None)
        self.notify("changed", None)
        result = updates.check()
        with self.condition:
            previous = self.update.get("latest")
            if result.get("error") and previous:  # offline for a moment: an update already found stays offered
                result = dict(self.update, checked=result["checked"], error=result["error"])
            self.update = dict(result, checking=False)
        self.notify("changed", None)
        if announce and result.get("available") and result.get("latest") != previous and self.on_update_available:
            self.on_update_available(result["latest"])
        return not result.get("error")

    def watch_updates(self):
        """Background thread: a check now (at launch), then every UPDATE_EVERY seconds; one that
        couldn't reach GitHub (no network yet after boot) is retried sooner."""
        def loop():
            retries = 0
            while not self.closed:
                ok = self.check_updates()
                retries = 0 if ok else retries + 1
                wait = update_wait(ok, retries)
                end = time.monotonic() + wait
                while not self.closed and time.monotonic() < end:
                    time.sleep(min(30, max(0.1, end - time.monotonic())))
        threading.Thread(target=loop, daemon=True, name="update-check").start()

    def notify(self, kind, value):
        with self.condition:
            if kind == "log":
                self.log_total += 1
                self.log.append({"id": self.log_total, "at": time.time(), "text": str(value)})
            self.revision += 1
            self.condition.notify_all()

    def names(self, accounts):
        """Add each account's name ("label") and, in name mode, show it instead of the email, so
        every surface (tray, taskbar, notifications) follows. An account without a name shows as
        "Claude 1" and so on: in name mode an email never appears outside the full view."""
        meta = self.gateway.manager.meta if self.live else None
        name_mode = bool(meta.get("nameMode")) if meta is not None else self.name_mode
        counts = {}
        for view in accounts:
            saved = (meta["accounts"].get(view["id"]) or {}) if meta is not None else {}
            view["label"] = (saved.get("label") if meta is not None else self.labels.get(view["id"])) or ""
            counts[view["provider"]] = counts.get(view["provider"], 0) + 1
            if name_mode:
                view["name"] = view["label"] or f"{view['provider'].title()} {counts[view['provider']]}"
        return name_mode

    def set_names(self, action, body):
        if action == "names":
            on = bool(body.get("on"))
            if self.live:
                with self.gateway.manager.lock:
                    self.gateway.manager.meta["nameMode"] = on
                    self.gateway.manager.save()
            self.name_mode = on
            return
        name, account_id = body.get("name"), body.get("id")
        if not isinstance(name, str) or len(name) > 40:
            raise ValueError("Names are up to 40 characters")
        if account_id not in {a.id for a in self.gateway.router.accounts}:
            raise ValueError("Unknown account")
        name = " ".join(name.split())
        if self.live:
            with self.gateway.manager.lock:
                entry = self.gateway.manager.meta["accounts"].setdefault(account_id, {})
                entry["label"] = name
                self.gateway.manager.save()
        else:
            self.labels[account_id] = name

    def snapshot(self):
        # Read accounts before taking the controller lock: the live account manager has its
        # own lock and posts log lines (which need this lock) while holding it.
        router = self.gateway.router
        active = dict(router.active)
        accounts = [account_view(a, active.get(a.provider) == a.id) for a in router.accounts]
        name_mode = self.names(accounts)
        auto_swap = router.auto_swap
        with self.condition:
            return {
                "revision": self.revision,
                "mode": "live" if self.live else "demo",
                "accounts": accounts,
                "autoSwap": auto_swap, "afk": self.afk_enabled(),
                "compact": bool(self.gateway.manager.meta.get("compactPanel")) if self.live else self.compact,
                "taskbar": bool(self.gateway.manager.meta.get("taskbarView", True)) if self.live else self.taskbar,
                "taskbarDisplay": (self.gateway.manager.meta.get("taskbarDisplay") or "main") if self.live else self.taskbar_display,
                "taskbarDisplays": list(self.taskbar_displays),
                "taskbarLayout": (self.gateway.manager.meta.get("taskbarLayout") if self.live else self.taskbar_layout),
                "taskbarAvailable": self.taskbar_available,
                "nameMode": name_mode,
                "update": dict(self.update),
                "clock24": self.clock_24(),
                "mod": self.mod_state(),
                "pendingResumes": self.gateway.manager.pending_list() if self.live else [],
                "waitNearReset": bool(self.gateway.manager.meta.get("waitNearReset", True)) if self.live else self.wait_near_reset,
                "afkSkipLarge": bool(self.gateway.manager.meta.get("afkSkipLarge", True)) if self.live else self.afk_skip_large,
                "jevCompact": bool(self.gateway.manager.meta.get("jevCompact")) if self.live else self.jev_compact,
                "jevKey": self.jev_key_source(),
                "launchAtLogin": bool(self.gateway.manager.meta.get("startWithWindows", True)) if self.live else False,
                "busy": self.pending,
                "log": list(self.log),
                "signingIn": sorted(getattr(getattr(self.gateway, "manager", None), "logins", {})),
            }

    def action(self, action, body):
        if self.closed:
            raise RuntimeError("The server is shutting down")
        if action not in {"resumeSession", "waitNearReset", "installMod", "checkMod", "preferences", "swap", "reset", "refresh", "add", "remove", "subscription", "compact", "taskbar", "names", "rename", "startup", "checkUpdate", "installUpdate", "clock", "afkSkipLarge", "jevCompact", "jevKey"}:
            raise ValueError("Unknown action")
        if action in {"names", "rename"}:  # name mode (screen sharing) and account names; instant
            self.set_names(action, body)
            self.notify("changed", None)
            return
        if action in ("installMod", "checkMod"):  # the optional Claude Code mod (Settings)
            self.mod_action(action)
            return
        if action == "resumeSession":  # answer to "continue this large session?"; instant
            if self.live and isinstance(body.get("session"), str):
                self.gateway.manager.resume_decision(body["session"], bool(body.get("approve")))
            self.notify("changed", None)
            return
        if action == "waitNearReset":  # don't switch away from a 5-hour limit that resets within 15 minutes
            on = bool(body.get("on"))
            if self.live:
                with self.gateway.manager.lock:
                    self.gateway.manager.meta["waitNearReset"] = on
                    self.gateway.manager.save()
            self.wait_near_reset = on
            self.notify("changed", None)
            return
        if action == "jevCompact":  # compact with Jev before a swapped session goes on; instant
            on = bool(body.get("on"))
            if self.live:
                manager = self.gateway.manager
                with manager.lock:
                    manager.meta["jevCompact"] = on
                    manager.save()
                if on:
                    self.jev_compact_notes()
            self.jev_compact = on
            self.notify("changed", None)
            return
        if action == "jevKey":  # the OpenRouter key for the Jev compaction (the app never sends it back); instant
            self.set_jev_key(body.get("key"))
            self.notify("changed", None)
            return
        if action == "afkSkipLarge":  # Auto resume leaves large sessions alone; instant
            on = bool(body.get("on"))
            if self.live:
                with self.gateway.manager.lock:
                    self.gateway.manager.meta["afkSkipLarge"] = on
                    self.gateway.manager.save()
            self.afk_skip_large = on
            self.notify("changed", None)
            return
        if action == "clock":  # 24-hour clock in the full view; instant
            on = bool(body.get("on"))
            if self.live:
                with self.gateway.manager.lock:
                    self.gateway.manager.meta["clock24"] = on
                    self.gateway.manager.save()
            self.clock24 = on
            self.notify("changed", None)
            return
        if action == "checkUpdate":  # Settings: Check for updates
            threading.Thread(target=self.check_updates, kwargs={"announce": False}, daemon=True).start()
            return
        if action == "installUpdate":  # Settings: Update now
            if not self.update.get("available"):
                raise ValueError("No update to install")
            self.update = dict(self.update, installing=True)
            self.notify("changed", None)

            def install():
                from . import updates
                error = updates.install(self.update)
                if error:
                    self.update = dict(self.update, installing=False, error=error)
                    self.notify("log", "Update failed: " + error)
                elif self.quit_app and self.update.get("kind") == "installer":
                    self.quit_app()  # the installer replaces the files once the app has closed, then starts it
            threading.Thread(target=install, daemon=True).start()
            return
        if action == "startup":  # start at sign-in (Windows Run entry / macOS login item); instant
            if not self.live:
                raise ValueError("Starting at sign-in needs real-account mode")
            on = bool(body.get("on"))
            with self.gateway.manager.lock:
                self.gateway.manager.meta["startWithWindows"] = on
                self.gateway.manager.save()
            from .integrations import set_start_with_windows
            set_start_with_windows(on)
            self.notify("changed", None)
            return
        if action in {"compact", "taskbar"}:  # the panel's size / the taskbar view and its display; instant
            changes = {}
            if "on" in body:
                on = bool(body["on"])
                setattr(self, action, on)
                changes["compactPanel" if action == "compact" else "taskbarView"] = on
            if action == "taskbar" and ("display" in body or "layout" in body):
                from . import taskbar_layout
                current = taskbar_layout.layout(self.snapshot_taskbar())
                if "display" in body:  # the tray menu's "On <display>": everything there
                    if not isinstance(body["display"], str) or not body["display"] or len(body["display"]) > 40:
                        raise ValueError("Unknown display")
                    self.taskbar_display = changes["taskbarDisplay"] = body["display"]
                    new = taskbar_layout.moved(current, body["display"])
                else:  # the settings' slots; {} shows nothing anywhere
                    new = taskbar_layout.clean(body["layout"]) if body["layout"] else {}
                    if new is None:
                        raise ValueError("Invalid taskbar layout")
                self.taskbar_layout = changes["taskbarLayout"] = new
            if self.live and changes:
                with self.gateway.manager.lock:
                    self.gateway.manager.meta.update(changes)
                    self.gateway.manager.save()
            self.notify("changed", None)
            return
        if action == "refresh":  # cheap and lock-free: just nudges the usage refresher
            if hasattr(self.gateway, "poke"):
                self.gateway.poke(body.get("ifOlderThan", 0) if isinstance(body.get("ifOlderThan", 0), (int, float)) else 0)
            return
        if action == "subscription":  # manual renewal / end date; instant, no worker thread
            if not self.live:
                raise ValueError("Renewal dates need real-account mode")
            at = body.get("at")
            if at is not None and (not isinstance(at, (int, float)) or isinstance(at, bool)):
                raise ValueError("Date must be a timestamp or null")
            self.gateway.manager.set_subscription(body.get("id"), at, bool(body.get("ends")))
            return
        if action in {"add", "remove"} and not self.live:
            raise ValueError("Adding and removing accounts needs real-account mode")
        if action == "add" and body.get("provider") not in {"claude", "codex"}:
            raise ValueError("Choose Claude or Codex")
        if action in {"swap", "remove"} and body.get("id") not in {a.id for a in self.gateway.router.accounts}:
            raise ValueError("Unknown account")
        if action == "preferences" and (type(body.get("afk")) is not bool or type(body.get("autoSwap")) is not bool):
            raise ValueError("Preferences must be booleans")
        if not self.operations.acquire(blocking=False):
            raise RuntimeError("An operation is already running")
        self.pending = True
        self.notify("changed", None)

        def work():
            try:
                if action == "preferences":
                    self.gateway.router.auto_swap = body["autoSwap"]
                    if self.live:
                        self.gateway.set_afk(body["afk"])  # also updates the Claude hook and status line
                    self.afk = body["afk"]
                    if hasattr(self.gateway, "apply_preferences"):
                        self.gateway.apply_preferences()
                elif action == "swap":
                    self.manual_swaps.add(body["id"])
                    if hasattr(self.gateway, "swap"):
                        self.gateway.swap(body["id"])
                    else:
                        self.gateway.router.swap(body["id"])
                elif action == "add":
                    expect = body.get("id") if isinstance(body.get("id"), str) else None
                    self.gateway.manager.add(body["provider"], expect=expect)  # expect: the account to sign back in
                elif action == "remove":
                    self.gateway.manager.remove(body["id"])
                elif action == "reset":
                    self.gateway.reset()  # the refresh button: fetch usage now (demo: fresh sample accounts)
                    if not self.live:
                        self.manual_swaps.update(self.gateway.router.active.values())
            except Exception as error:
                self.notify("log", str(error))
            finally:
                self.pending = False
                self.operations.release()
                self.notify("changed", None)

        threading.Thread(target=work, daemon=True).start()

    def clock_24(self):
        """The 24-hour clock setting, or the system's own choice until it has been set."""
        saved = self.gateway.manager.meta.get("clock24") if self.live else self.clock24
        if saved is None:
            from .clock import clock_12h
            return not clock_12h()
        return bool(saved)

    def snapshot_taskbar(self):
        """The saved taskbar settings (what taskbar_layout.layout() reads)."""
        meta = self.gateway.manager.meta if self.live else {}
        return {"taskbarLayout": meta.get("taskbarLayout") if self.live else self.taskbar_layout,
                "taskbarDisplay": (meta.get("taskbarDisplay") if self.live else self.taskbar_display) or "main"}

    # ---------- the optional Claude Code mod (mod.py) ----------
    def mod_state(self):
        """{"status": ..., "text": ...} for Settings: missing, installing, installed (no session
        has reported yet), active (a session reported lately), or an error to show."""
        if not self.live:
            return {"status": "unavailable"}
        if self.mod_busy == "installing":
            return {"status": "installing"}
        if self.mod_busy:
            return {"status": "error", "text": self.mod_busy}
        if self.mod_installed is False and time.time() - self.mod_seen < 120:
            return {"status": "update"}  # limit-status alone, from before the Jev compaction
        if time.time() - self.mod_seen < 120:
            return {"status": "active"}
        if self.mod_installed:
            return {"status": "installed"}
        if self.mod_installed is None and time.time() - self.mod_checked > 600:
            threading.Thread(target=self.mod_check, daemon=True, name="mod-check").start()
        return {"status": "missing" if self.mod_installed is False else "unknown"}

    def jev_key_source(self):
        """Where the OpenRouter key is ("file", "env", "settings") or None; never the key itself."""
        if not self.live:
            return "file" if self.jev_key_demo else None
        from . import mod
        integrations = getattr(self.gateway, "integrations", None)
        return mod.jev_key_source(integrations.state_file) if integrations else None

    def set_jev_key(self, key):
        """Save the key the user typed (None or empty removes it)."""
        key = key.strip() if isinstance(key, str) else None
        if not self.live:
            self.jev_key_demo = bool(key)
            return
        from . import mod
        integrations = getattr(self.gateway, "integrations", None)
        if integrations is None:
            raise RuntimeError("The key can't be saved right now")
        try:
            mod.set_jev_key(integrations.state_file, key or None)
        except OSError as error:
            raise RuntimeError(f"Couldn't save the key ({type(error).__name__})") from None

    def jev_compact_notes(self):
        """What Jev compaction still needs, as notes in the log: the mod, and a key."""
        from . import mod
        integrations = getattr(self.gateway, "integrations", None)
        if integrations is None:
            return
        if self.mod_installed is False or not self.gateway.manager.meta.get("jevModInstalled"):
            self.notify("log", "Jev compaction needs the Claude Code mod: Settings → Install (or update) it")
        if not mod.jev_key_present(integrations.state_file):
            self.notify("log", f"Jev compaction needs an OpenRouter key: paste it in Settings, under Jev compaction")

    def note_mod_installed(self, installed):
        """Both plugins are there (or not): the Jev compaction is only asked for when they are."""
        self.mod_installed = installed
        manager = getattr(self.gateway, "manager", None)
        if manager is None or installed is None or bool(manager.meta.get("jevModInstalled")) == installed:
            return
        with manager.lock:
            manager.meta["jevModInstalled"] = installed
            manager.save()

    def mod_check(self):
        from . import mod
        self.mod_checked = time.time()
        self.note_mod_installed(mod.installed())
        if self.mod_installed is False:  # uninstalled: the status line command is wanted again
            self.set_mod_seen(0.0)
        self.notify("changed", None)

    def note_mod(self):
        """The mod reported: remember it (it feeds the usage live after every turn)."""
        now = time.time()
        self.mod_seen = now
        if self.mod_installed is None:
            self.mod_installed = True  # at least limit-status is; a look-up says whether jev-compact is too
        manager = getattr(self.gateway, "manager", None)
        if manager is not None and now - float(manager.meta.get("modSeenAt") or 0) > 60:
            self.set_mod_seen(now)

    def set_mod_seen(self, when):
        manager = getattr(self.gateway, "manager", None)
        if manager is None:
            return
        with manager.lock:
            was = float(manager.meta.get("modSeenAt") or 0) > 0
            manager.meta["modSeenAt"] = when
            manager.save()
        if was != (when > 0) and getattr(self.gateway, "integrations", None):
            self.gateway.integrations.apply_afk()  # puts the user's own status line back, or ours

    def mod_action(self, action):
        from . import mod
        if action == "checkMod":
            threading.Thread(target=self.mod_check, daemon=True, name="mod-check").start()
            return
        if not self.live or self.mod_busy == "installing":
            return
        self.mod_busy = "installing"
        self.notify("changed", None)

        def work():
            state_file = self.gateway.integrations.state_file if getattr(self.gateway, "integrations", None) else None
            error = mod.install(state_file) if state_file else "LimitSwitcher isn't ready yet; try again in a moment"
            # Settings has room for a few words; the reason goes to the log (shown as a note)
            self.mod_busy = ("Claude Code not found" if error and "isn't installed" in error else "Install failed · see log") if error else None
            if error:
                self.notify("log", "Claude Code mod: " + error)
            self.mod_checked = 0.0
            if not error:
                self.note_mod_installed(mod.installed())
            if not error:
                with self.gateway.manager.lock:
                    self.gateway.manager.meta["modStatePath"] = str(state_file)
                    self.gateway.manager.save()
                self.notify("log", "Claude Code mod installed: open sessions pick it up after /reload-plugins")
            self.notify("changed", None)
        threading.Thread(target=work, daemon=True, name="mod-install").start()

    def afk_enabled(self):
        return bool(self.gateway.manager.meta.get("afk")) if self.live else self.afk

    def statusline_limits(self):
        return self.gateway.manager.claude_limits() if self.live else None

    def statusline(self, body):
        """Live Claude usage from Claude Code's status line; returns the line to show there."""
        if not self.live:
            return None
        session = str(body.get("session") or "")[:100] or None
        model = body.get("model") if isinstance(body.get("model"), str) else None
        effort = body.get("effort") if isinstance(body.get("effort"), str) else None
        line = self.gateway.manager.statusline(body.get("rate_limits"), session, model=model, effort=effort)
        if body.get("source") == "mod":  # the mod feeds the usage; the line itself is the status line's
            self.note_mod()
            self.gateway.manager.note_mod_session(session)
            return None
        # Without the mod the usage still comes in (no API calls needed), but nothing of LimitSwitcher
        # shows in Claude Code (the user's own status line, if any, is unchanged): installing the
        # mod is the person's choice to see the line.
        if time.time() - float(self.gateway.manager.meta.get("modSeenAt") or 0) > 14 * 24 * 3600:
            return None
        return line

    def compaction_request(self, body):
        """For a session's mod: the Jev compaction to run before it goes on, if one is due."""
        if not self.live or body.get("source") != "mod":
            return None
        return self.gateway.manager.compaction_request(str(body.get("session") or "")[:100])

    def compacted_context(self, body):
        """For a session's status line: its context after a Jev compaction, if it had one."""
        if not self.live or body.get("source") == "mod":
            return None
        return self.gateway.manager.compacted_context(str(body.get("session") or "")[:100] or None)

    def compaction_done(self, body):
        """A session's mod finished (or gave up on) the compaction it was asked for."""
        if not self.live:
            return False
        if body.get("outcome") == "running":  # one the session started itself (/jevcompact)
            started = self.gateway.manager.compaction_started(str(body.get("session") or "")[:100] or None,
                                                              str(body.get("id") or "")[:40])
            if started:
                self.notify("changed", None)
            return started
        saved = body.get("saved") if type(body.get("saved")) is int else None
        done = self.gateway.manager.compaction_done(str(body.get("session") or "")[:100], str(body.get("id") or ""),
                                                    str(body.get("outcome") or ""), saved, body.get("reason"))
        if done:
            self.notify("changed", None)
        return done

    def afk_limit(self, body):
        """A Claude Code session hit a usage limit (from the AFK hook): what should it do?"""
        if not self.live or body.get("provider") != "claude":
            return {"action": "stop"}
        answer = self.gateway.manager.claude_limit(
            str(body.get("session") or "")[:100], body.get("contextTokens") if type(body.get("contextTokens")) is int else None)
        self.notify("changed", None)
        return answer

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        with self.operations:
            self.gateway.close()


def account_view(account, active):
    """JSON shape the dashboard and tray use for one account."""
    view = asdict(account)
    view.pop("usage", None)
    view.update(active=active, eligible=account.eligible, windows=account.windows(),
                headroom=account.headroom, renewsAt=account.renews_at,
                name=account.email or account.alias.split(" · ")[-1].replace(" (synthetic)", ""))
    return view


def make_server(controller, port=0):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, body, mime="application/json"):
            raw = json.dumps(body).encode() if mime == "application/json" else body
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            try:
                self.end_headers()
                self.wfile.write(raw)
            except ConnectionError:
                pass

        def authorized(self):
            if self.headers.get("Host") != self.server.expected_host:
                self.respond(403, {"error": "Invalid local host"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.server.expected_host:
                self.respond(403, {"error": "Cross-origin request rejected"})
                return False
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.respond(401, {"error": "Open the private launch URL to connect"})
                return False
            return True

        def do_GET(self):
            if self.headers.get("Host") != self.server.expected_host:
                self.respond(403, {"error": "Invalid local host"})
                return
            path = self.path.split("?")[0]
            if path in ("/assets/codex.png", "/assets/claude.png", "/assets/switcher.png"):
                self.respond(200, (ASSETS / path.lstrip("/")).read_bytes(), "image/png")
                return
            assets = {"/menu": ("menu.html", "text/html; charset=utf-8"), "/menu.js": ("menu.js", "text/javascript; charset=utf-8"),
                      "/menu.css": ("menu.css", "text/css; charset=utf-8")}  # the macOS panel
            if path in assets:
                name, mime = assets[path]
                self.respond(200, (ASSETS / name).read_bytes(), mime)
                return
            if path != "/api/state" or not self.authorized():
                if path != "/api/state":
                    self.respond(404, {"error": "Unknown route"})
                return
            from urllib.parse import parse_qs, urlsplit
            try:
                after = int(parse_qs(urlsplit(self.path).query).get("after", ["-1"])[0])
            except ValueError:
                self.respond(400, {"error": "Invalid revision"})
                return
            with controller.condition:
                controller.condition.wait_for(lambda: controller.revision != after or controller.closed, timeout=20)
            self.respond(200, controller.snapshot())

        def do_POST(self):
            app_token = secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)
            if self.path == "/api/statusline" and not app_token:  # Claude Code's status line script: live usage, narrow token
                # (with the app's own token it's the Settings switch, like any other action; the Mac full view uses it)
                if self.headers.get("Host") != self.server.expected_host or not secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + self.server.hook_token):
                    self.respond(403, {"error": "Forbidden"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(size)) if 0 < size <= 8192 else {}
                    body = body if isinstance(body, dict) else {}
                    line = controller.statusline(body)
                    self.respond(200, {"line": line, "parts": getattr(line, "parts", None),
                                       "rate_limits": controller.statusline_limits(),
                                       "compact": controller.compaction_request(body),
                                       "compacted": controller.compacted_context(body)})
                except (ValueError, RuntimeError, OSError) as error:
                    self.respond(200, {"line": None, "error": str(error)})
                return
            if self.path == "/api/compaction":  # the mod: a Jev compaction started by hand, or one it ran is done
                if self.headers.get("Host") != self.server.expected_host or not secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + self.server.hook_token):
                    self.respond(403, {"error": "Forbidden"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(size)) if 0 < size <= 4096 else {}
                    self.respond(200, {"ok": controller.compaction_done(body if isinstance(body, dict) else {})})
                except (ValueError, RuntimeError, OSError) as error:
                    self.respond(200, {"ok": False, "error": str(error)})
                return
            if self.path == "/api/afk":  # the Claude Code AFK hook, with its own narrow token
                if self.headers.get("Host") != self.server.expected_host or not secrets.compare_digest(
                        self.headers.get("Authorization", ""), "Bearer " + self.server.hook_token):
                    self.respond(403, {"error": "Forbidden"})
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(size)) if 0 < size <= 4096 else {}
                    self.respond(200, controller.afk_limit(body if isinstance(body, dict) else {}))
                except (ValueError, RuntimeError, OSError) as error:
                    self.respond(200, {"action": "stop", "error": str(error)})
                return
            if not self.authorized():
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 4096:
                    raise ValueError("Request too large")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                if self.path == "/api/shutdown":
                    self.respond(200, {"ok": True})
                    # A host (the tray) can supply its own quit; otherwise stop serving.
                    threading.Thread(target=getattr(self.server, "quit", self.server.shutdown), daemon=True).start()
                    return
                if self.path == "/api/show":  # the app was opened again: the host shows its window
                    show = getattr(self.server, "show", None)
                    if show:
                        show()
                    self.respond(200, {"shown": bool(show)})
                    return
                if not self.path.startswith("/api/"):
                    raise ValueError("Unknown route")
                controller.action(self.path[5:], body)
                self.respond(202, {"ok": True})
            except ValueError as error:
                self.respond(400, {"error": str(error)})
            except RuntimeError as error:
                self.respond(409, {"error": str(error)})

    server = LocalServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.expected_host = f"127.0.0.1:{server.server_port}"
    server.launch_url = f"http://{server.expected_host}/#token={token}"
    server.hook_token = secrets.token_urlsafe(32)
    server.hook_url = f"http://{server.expected_host}/api/afk"

    return server


def write_url_file(path, url):
    """Record the tokenised launch URL so a local script can request a clean shutdown."""
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(url, encoding="utf-8")


def clear_url_file(path, url):
    """Remove the launch URL on exit, unless a newer instance has replaced it."""
    try:
        target = Path(path) if path else None
        if target and target.read_text(encoding="utf-8") == url:
            target.unlink()
    except OSError:
        pass
