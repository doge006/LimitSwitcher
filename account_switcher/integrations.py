"""What the app sets up in Codex and Claude Code while it runs, and takes down on quit.

- Codex: the local router (codex_proxy.py) and the config lines pointing Codex at it. Port
  and path secret are kept across restarts, so sessions that are already open reconnect
  after an update or restart (Codex retries a refused connection).
- Claude Code: the AFK hook (claude_hooks.py) while AFK is on, and the small file that tells
  the hook how to reach the app.
- Windows: start with Windows (on by default), because Codex's requests go through the app.
Quit undoes the Codex and Claude changes and writes the chosen Codex account into
~/.codex/auth.json, so Codex keeps working, on that account, without the app.
"""
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time

from . import claude_hooks, codex_config
from .codex_proxy import DEFAULT_PORT, CodexProxy, ThreadState
from .vault import atomic_write

MOD_FRESH = 14 * 24 * 3600  # the mod counts as in use this long after it last reported

log = logging.getLogger("account_switcher.integrations")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "LimitSwitcher"
OLD_RUN_NAMES = ("LimitSwitch", "AccountSwitcher")  # before the renames
THREADS = "codex-threads"   # encrypted: which account made which item, checkpoint texts


class RoutedAccounts:
    """What the router needs from the account manager."""

    def __init__(self, manager, provider="codex"):
        self.manager, self.provider = manager, provider

    def route(self):
        return self.manager.route(self.provider)

    def credentials(self, account_id):
        return self.manager.credentials(account_id)

    def limit_hit(self, account_id, resets_at):
        return self.manager.limit_hit(account_id, resets_at)

    def turn_start(self, account_id):
        return self.manager.turn_start(account_id)

    def usable(self, account_id):
        return self.manager.usable(account_id)

    def observe(self, account_id, windows):
        self.manager.observe(account_id, windows)


def codex_home_path(codex_home=None):
    return Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_present(codex_home=None):
    return codex_home_path(codex_home).exists() or shutil.which("codex") is not None


# Apps that run their own Codex app-server (named in the notice when one started too early).
EDITORS = {"code": "VS Code", "code - insiders": "VS Code Insiders", "cursor": "Cursor", "windsurf": "Windsurf",
           "codium": "VSCodium", "codex": "the Codex app", "chatgpt": "the ChatGPT app", "electron": "an app"}


class CodexServerWatch:
    """Stops Codex's shared background server once it is idle.

    Sessions attach to that server if it is running, even with auto-start off, and it keeps
    the settings it started with, so its sessions would skip the router (no switching) and
    it opens a console window for every command on Windows. Once it is gone, each session
    runs in its own terminal and goes through the router. It is only stopped while no
    session has written anything for a while, so no turn in progress is cut off."""

    QUIET = 90      # seconds without session activity that count as idle
    EVERY = 60

    def __init__(self, codex_home=None, notify=lambda *_: None, cli="codex", routed_since=None):
        self.home = codex_home_path(codex_home)
        self.notify, self.cli = notify, cli
        self.stopped = threading.Event()
        self.tried_at = None
        # When Codex's settings started pointing at the router: Codex processes older than this
        # read them before, so they talk to ChatGPT directly on the login they started with.
        self.routed_since = routed_since
        self.told = set()  # Codex sessions already mentioned

    @property
    def socket(self):
        return self.home / "app-server-control" / "app-server-control.sock"

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="codex-server-watch").start()

    def close(self):
        self.stopped.set()

    def _loop(self):
        while not self.stopped.is_set():
            for step in (self.check, self.check_older):
                try:
                    step()
                except Exception:
                    log.debug("codex watch", exc_info=True)
            self.stopped.wait(self.EVERY)

    def older_codex(self):
        """Codex processes that started before the router: [(Process, kind)], kind "broker" (the
        Claude Code Codex plugin's app-server broker) or "session" (a Codex window)."""
        if not self.routed_since or self.home != codex_home_path():
            return []  # processes can't be told apart by their Codex folder: only the usual one's
        from . import processes
        found, servers = [], []
        for process in processes.listing({"node", "codex"}):
            if process.started >= self.routed_since - 2:
                continue
            command = process.command.replace("\\", "/")
            if process.name == "node" and "app-server-broker" in command and " serve" in command:
                found.append((process, "broker"))
            elif process.name == "codex" and "app-server" in command:
                servers.append(process)
            elif process.name == "codex" and not any(word in command for word in (" login", " mcp", "exec ")):
                found.append((process, "session"))
        if servers:  # an editor's or app's own Codex (VS Code, Cursor, the Codex app), not the plugin's
            brokers = {p.pid for p, kind in found if kind == "broker"}
            tree = processes.family()
            for server in servers:
                owner, pid, seen = None, server.parent, set()
                while pid in tree and pid not in seen and pid not in brokers:
                    seen.add(pid)
                    parent, name = tree[pid]
                    if name in EDITORS and owner is None:
                        owner = EDITORS[name]
                    pid = parent
                if pid in brokers:
                    continue  # the plugin's: handled with its broker
                server.owner = owner or "an editor or app"
                found.append((server, "embedded"))
        return found

    def check_older(self, quiet=None):
        """Codex processes older than the router skip it, so they can't switch accounts.
        - The Claude Code Codex plugin keeps one broker (and its codex app-server) running between
          jobs, and starts a new one when that one isn't there. So once no Codex session has
          written anything for a while (no job running), end the old one: the plugin's next job
          starts a fresh one, which goes through the router.
        - A Codex window can't be restarted for the user: say so once."""
        older = self.older_codex()
        if not older:
            return False
        brokers = [p for p, kind in older if kind == "broker"]
        ended = False
        if brokers and time.time() - self.last_activity() >= (self.QUIET if quiet is None else quiet):
            from . import processes
            for broker in brokers:
                processes.end_tree(broker.pid)
            ended = True
            self.notify("log", "Restarted the Claude Code Codex plugin's background Codex (it started before LimitSwitcher), "
                               "so its jobs switch accounts too")
        for process, kind in older:
            if kind in ("session", "embedded") and process.pid not in self.told:
                self.told.add(process.pid)
                when = time.strftime("%H:%M", time.localtime(process.started))
                if kind == "session":
                    self.notify("log", f"A Codex window opened at {when}, before LimitSwitcher, doesn't go through it, so "
                                       "it won't switch accounts. Restart that Codex session to fix it.")
                else:
                    self.notify("log", f"Codex in {process.owner} started at {when}, before LimitSwitcher, so it doesn't go "
                                       f"through it and won't switch accounts. Reload {process.owner} (or restart its "
                                       "Codex) to fix it.")
        return ended

    @property
    def pid_file(self):
        return self.home / "app-server-daemon" / "server.pid"

    def marker(self):
        """Something identifying the running server, or None. Only existence and lstat are
        used: on Windows the socket is a special file that a normal stat cannot open."""
        stamps = []
        for path in (self.socket, self.pid_file):
            try:
                stamps.append(os.lstat(path).st_mtime)
            except OSError:
                continue
        return tuple(stamps) or None

    def running(self):
        marker = self.marker()
        return marker is not None and marker != self.tried_at  # a server we already stopped is gone

    def last_activity(self):
        """Newest write to a session log in the last two day folders (sessions/YYYY/MM/DD)."""
        root = self.home / "sessions"
        days = []
        try:
            for year in sorted(p for p in root.iterdir() if p.is_dir())[-2:]:
                for month in sorted(p for p in year.iterdir() if p.is_dir())[-2:]:
                    days += [p for p in month.iterdir() if p.is_dir()]
        except OSError:
            return 0.0
        newest = 0.0
        for day in sorted(days)[-2:]:
            try:
                newest = max([newest] + [f.stat().st_mtime for f in day.iterdir()])
            except OSError:
                continue
        return newest

    def check(self, quiet=None):
        if not self.running() or time.time() - self.last_activity() < (self.QUIET if quiet is None else quiet):
            return False
        self.tried_at = self.marker()
        codex = shutil.which(self.cli)
        if not codex:
            return False
        done = subprocess.run([codex, "app-server", "daemon", "stop"], capture_output=True, text=True, timeout=120,
                              stdin=subprocess.DEVNULL, env=dict(os.environ, CODEX_HOME=str(self.home)),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode == 0:
            self.notify("log", "Stopped Codex's shared background server; Codex sessions now go through the app")
            return True
        return False


def stamp_of(path):
    """A change marker for a file: its time and its size (two writes within one clock tick, such as
    a truncate and the rewrite, share a time but not a size)."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


class Integrations:
    SETTINGS_EVERY = 20  # seconds between checks that Claude Code's settings still have our status line
    def __init__(self, gateway, hook_url, hook_token, codex_home=None, claude_root=None, upstream=None):
        self.gateway = gateway
        self.manager = gateway.manager
        self.root = Path(self.manager.vault.root)
        self.hook_url, self.hook_token = hook_url, hook_token
        self.codex_home, self.claude_root = codex_home, claude_root
        self.upstream = upstream
        self.proxy = None
        self.watch = None

    @property
    def state_file(self):
        return self.root / "afk-hook.json"

    # ---------- start / stop ----------
    def refresh_mod_config(self):
        """The mod finds this app through a path saved in its settings: when the data folder moved (the
        AccountSwitcher to LimitSwitcher rename), tell it the new one."""
        if not self.mod_in_use():
            return
        path = str(self.state_file)
        if self.manager.meta.get("modStatePath") == path:
            return
        from . import mod
        error = mod.configure(self.state_file)
        if error is None:
            with self.manager.lock:
                self.manager.meta["modStatePath"] = path
                self.manager.save()
        else:
            log.warning("could not point the Claude Code Status mod at %s: %s", path, error)

    def start(self):
        self.apply_afk()
        self.keep_claude_settings()
        threading.Thread(target=self.refresh_mod_config, daemon=True, name="mod-config").start()
        if codex_present(self.codex_home):
            self.start_codex()
        meta = self.manager.meta
        if "startWithWindows" not in meta:  # first run: on (Settings turns it off)
            meta["startWithWindows"] = True
            self.manager.save()
        if meta["startWithWindows"]:
            set_start_with_windows(True)

    def start_codex(self):
        settings_path = self.root / "router.json"
        try:
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        port = saved.get("port") if isinstance(saved.get("port"), int) else DEFAULT_PORT
        secret = saved.get("secret") if isinstance(saved.get("secret"), str) else secrets.token_urlsafe(18)
        kwargs = {"upstream": self.upstream} if self.upstream else {}
        vault = self.manager.vault
        state = ThreadState(load=lambda: vault.read_secret(THREADS), save=lambda data: vault.write_secret(THREADS, data))
        proxy = CodexProxy(RoutedAccounts(self.manager), port=port, secret=secret, state=state, **kwargs)
        try:
            base_url = proxy.start()
            codex_config.apply(base_url, self.codex_home)
        except Exception as error:
            proxy.close()
            self.manager.notify("log", f"Codex routing is off: {error}")
            log.warning("codex routing failed: %s", error)
            return
        atomic_write(settings_path, json.dumps({"port": proxy.port, "secret": proxy.secret}).encode())
        self.proxy = proxy
        self.manager.enable_routing("codex")
        try:  # unchanged settings (left by a run that ended abruptly) count from when they were written
            routed_since = os.stat(codex_config.config_path(self.codex_home)).st_mtime
        except (OSError, AttributeError):
            routed_since = time.time()
        self.watch = CodexServerWatch(self.codex_home, self.manager.notify, routed_since=routed_since)
        self.watch.start()

        def after_swap(provider):
            """Right after a Codex switch, stop what would stay on the old account once it is quiet."""
            if provider == "codex":
                for step in (self.watch.check, self.watch.check_older):
                    threading.Thread(target=step, kwargs={"quiet": 30}, daemon=True).start()
        self.manager.on_swap = after_swap

    def keep_claude_settings(self):
        """Claude Code (updating itself, /config) and other tools rewrite settings.json, which can
        drop our status line. Check whenever the file changes and put it back: without it there
        is no live Claude usage. Only a stat every 20 s while nothing changes."""
        self.settings_stop = threading.Event()
        path = (Path(self.claude_root) if self.claude_root else claude_hooks.settings_path()) / "settings.json"

        def loop():
            seen = None
            while not self.settings_stop.wait(self.SETTINGS_EVERY):
                stamp = stamp_of(path)
                if stamp == seen:
                    continue
                seen = stamp
                if not claude_hooks.statusline_installed(self.claude_root) and self.statusline_wanted():
                    log.warning("Claude Code's status line was not ours any more (settings.json rewritten); restoring it")
                    self.apply_afk()
                    # caught mid-write (invalid for a moment), it couldn't be put back: look again next time
                    seen = stamp_of(path) if claude_hooks.statusline_installed(self.claude_root) else None

        threading.Thread(target=loop, daemon=True, name="claude-settings-watch").start()

    def stop(self):
        if getattr(self, "settings_stop", None):
            self.settings_stop.set()
        if self.watch:
            self.watch.close()
        if self.proxy:
            try:
                self.manager.disable_routing("codex")
            finally:
                try:
                    codex_config.restore(self.codex_home)
                except OSError as error:
                    log.warning("could not restore Codex config: %s", error)
                self.proxy.close()
                self.proxy = None
        try:
            claude_hooks.uninstall(self.claude_root)
        except OSError as error:
            log.warning("could not remove the Claude hook: %s", error)
        try:
            claude_hooks.uninstall_statusline(self.state_file, self.claude_root)
        except OSError as error:
            log.warning("could not restore the Claude status line: %s", error)
        try:
            claude_hooks.restore_auto_continue(self.state_file, self.claude_root)
        except OSError as error:
            log.warning("could not restore Claude Code's automatic continue: %s", error)
        try:
            self.state_file.unlink()
        except OSError:
            pass

    # ---------- AFK ----------
    def write_state(self, statusline=None):
        """What the hook and the status line script need: where the app is, their token, and
        the user's own status line command (which the script keeps showing)."""
        atomic_write(self.state_file, json.dumps({"url": self.hook_url, "token": self.hook_token,
                                                   "statusline": statusline}).encode())

    def mod_in_use(self):
        """The Claude Code Status mod has reported lately (it feeds the usage and draws the line)."""
        return time.time() - float(self.manager.meta.get("modSeenAt") or 0) < MOD_FRESH

    def statusline_wanted(self):
        """Ours in Claude Code's status line: while the mod is in use (installing it is the person's
        choice to see the line; the mod feeds the usage, the status line shows it), or around the
        user's own one (which stays unchanged). Otherwise Claude Code's status line is left alone:
        ours would show an empty line there."""
        return self.mod_in_use() or \
            claude_hooks.own_statusline(self.state_file, self.claude_root) is not None

    def apply_afk(self):
        previous = None
        try:
            # Live Claude usage from Claude Code's status line: no tokens, no API calls.
            if self.statusline_wanted():
                previous = claude_hooks.install_statusline(self.state_file, self.claude_root)
            else:
                claude_hooks.uninstall_statusline(self.state_file, self.claude_root)
        except (OSError, ValueError) as error:
            log.warning("could not install Claude Code's status line: %s", error)
            self.manager.notify("log", f"Couldn't update Claude Code's status line: {error}")
        self.write_state(previous)
        try:
            # The hook reports Claude's usage limits: needed for Auto swap and for AFK.
            if self.manager.meta.get("afk") or self.manager.meta.get("autoSwap"):
                claude_hooks.install(self.state_file, self.claude_root)
            else:
                claude_hooks.uninstall(self.claude_root)
        except (OSError, ValueError) as error:
            log.warning("could not update Claude Code's hook: %s", error)
            self.manager.notify("log", f"Couldn't update Claude Code's settings for AFK: {error}")
        try:
            # Claude Code's own wait is turned ON (see claude_hooks.AUTO_CONTINUE): off, a limit opens a
            # dialog that blocks the hook's continue until someone answers it.
            if self.manager.meta.get("afk"):
                claude_hooks.enable_auto_continue(self.state_file, self.claude_root)
            else:
                claude_hooks.restore_auto_continue(self.state_file, self.claude_root)
        except (OSError, ValueError) as error:
            log.warning("could not update Claude Code's automatic continue: %s", error)


def launcher():
    """Command that starts the app quietly (the installed exe, or pythonw running LimitSwitcher.pyw)."""
    from .version import launcher as command
    return command()


LAUNCH_AGENT = "com.accountswitcher.app"


def launch_agent_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT}.plist"


def set_start_with_windows(enabled):
    """Start at sign-in: a Run entry on Windows, a LaunchAgent (login item) on macOS."""
    if sys.platform == "darwin":
        return _set_launch_agent(enabled)
    if sys.platform != "win32":
        return
    import winreg
    try:
        # CreateKeyEx: a fresh Windows profile may not have the Run key yet (it opens it when it does).
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, launcher())
            for name in (OLD_RUN_NAMES if enabled else (RUN_NAME,) + OLD_RUN_NAMES):
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    except OSError as error:
        log.warning("start with Windows: %s", error)


def _set_launch_agent(enabled):
    import plistlib
    path = launch_agent_path()
    if not enabled:
        try:
            path.unlink()
        except OSError:
            pass
        return
    python = Path(sys.executable)
    script = Path(__file__).resolve().parent.parent / "LimitSwitcher.pyw"
    arguments = [str(python), str(script)]
    app = os.environ.get("ACCOUNT_SWITCHER_APP")
    if app:  # started as the app: log in as the app (its launcher, quietly)
        for name in ("LimitSwitcher", "AccountSwitcher"):
            if (Path(app) / "Contents" / "MacOS" / name).exists():
                arguments = [str(Path(app) / "Contents" / "MacOS" / name), "--at-login"]
                break
    plist = {"Label": LAUNCH_AGENT, "ProgramArguments": arguments, "RunAtLoad": True,
             "ProcessType": "Interactive", "WorkingDirectory": str(script.parent)}
    from .vault import data_dir
    log_file = str(data_dir() / "app.log")  # a failed start at login is not silent either
    plist.update(StandardOutPath=log_file, StandardErrorPath=log_file)
    data = plistlib.dumps(plist)
    try:
        if not path.exists() or path.read_bytes() != data:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, data)
    except OSError as error:
        log.warning("start at login: %s", error)
