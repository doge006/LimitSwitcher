"""Separate accounts per window: the `claude` wrapper, opening a window, and windows from before the router.

Since 1.4 a window with an account of its own is an ordinary Claude Code window on ~/.claude: the
wrapper (window.py) points it at the app's router (claude_router.py), which puts that account's login
on its requests. This module installs the wrapper, opens "New window" terminals and reads a window's
session title for the Windows list.

Before 1.4 such a window had a config folder of its own (CLAUDE_CONFIG_DIR, `profiles/window-<id>` in
the data folder) with links to ~/.claude, which Claude Code's own saves could break on Windows (its
settings, and so its plugins, then drifted apart). What is left of that here: the app takes each such
folder's newest login back and deletes the folder once its window has closed (LiveAccounts._retire_profiles).

    python -m account_switcher.profiles list     # the saved Claude accounts, and where each is in use
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

from .providers import Claude, _read_json
from .vault import Vault, atomic_write

FOLDER = "profiles"
PREFIX = "window-"
INFO = "window.json"
PID_FILE = "window.pid"
# What a folder from before 1.4 linked to ~/.claude: only the links go when it is deleted.
SHARED = ("settings.json", "CLAUDE.md", "agents", "commands", "skills", "plugins", "output-styles",
          "projects", "todos", "history.jsonl", "keybindings.json", "ide", "file-history", "plans")
SEED = ".claude.seed.json"  # the ~/.claude.json the folder started from: what the window changed goes back on removal
KEEP_OWN = {"oauthAccount"}  # the folder's own login, never copied back


def root(vault_root):
    return Path(vault_root) / FOLDER


def existing(vault_root):
    """{window id: folder} for every profile there is."""
    try:
        entries = sorted(root(vault_root).iterdir())
    except OSError:
        return {}
    # (Claude Code's locks sit beside a folder as window-<id>.lock: not profiles)
    return {p.name: p.resolve() for p in entries if p.is_dir() and re.fullmatch(PREFIX + "[0-9a-f]{8}", p.name)}


def key(path):
    """A folder as the app compares them (what a hook's CLAUDE_CONFIG_DIR says, however spelled)."""
    return os.path.normcase(os.path.realpath(str(path)))


def provider(directory, keychain=None):
    return Claude(config_dir=directory, keychain=keychain)


def info(directory):
    """{"pid", "cwd", "started", "how"} of the window that has this profile."""
    data = _read_json(Path(directory) / INFO) or {}
    try:
        data["pid"] = int((Path(directory) / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        pass
    return data


def set_info(directory, **fields):
    data = _read_json(Path(directory) / INFO) or {}
    data.update(fields)
    atomic_write(Path(directory) / INFO, json.dumps(data, indent=2).encode())


def alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000 | 0x100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_link(path):
    """A symlink, or a junction (Windows; os.path.islink doesn't see those before Python 3.12)."""
    if os.path.islink(path):
        return True
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return False


def _merge(main, seed, own):
    """What the window changed in its .claude.json since `seed`, onto `main` (~/.claude.json now),
    where the main copy hasn't changed that same thing since: folder trust and a folder's allowed
    tools and MCP servers (per folder, under "projects"), and the rest key by key. Returns
    whether anything changed."""
    changed = False
    for name, value in own.items():
        if name in KEEP_OWN or value == seed.get(name):
            continue
        if name == "projects" and isinstance(value, dict) and isinstance(main.get(name, {}), dict):
            before, now = seed.get(name) or {}, dict(main.get(name) or {})
            for folder, entry in value.items():
                if entry != before.get(folder) and now.get(folder) == before.get(folder):
                    now[folder] = entry
                    changed = True
            main[name] = now
        elif main.get(name) == seed.get(name):
            main[name] = value
            changed = True
    return changed


def keep_changes(directory, home=None):
    """Before a profile goes: what its window changed in .claude.json (a folder trusted, an MCP
    server added, a setting from /config) is written to ~/.claude.json, so it isn't lost."""
    directory = Path(directory)
    seed, own = _read_json(directory / SEED), _read_json(directory / ".claude.json")
    if not isinstance(seed, dict) or not isinstance(own, dict):
        return False
    home = Path(home or info(directory).get("home") or Path.home())
    path = home / ".claude.json"
    main = _read_json(path)
    if not isinstance(main, dict):
        return False
    if not _merge(main, seed, own):
        return False
    atomic_write(path, json.dumps(main, indent=2).encode())
    return True


def remove(directory, keychain=None):
    """Delete a 1.3.x window folder (its login taken back first: LiveAccounts._retire_profiles). The links go,
    never what they point at."""
    directory = Path(directory)
    try:
        keep_changes(directory)
    except OSError:
        pass  # never keeps the folder (and the account) from being freed
    provider(directory, keychain).forget()
    for name in SHARED:  # the links themselves; rmtree must never walk into ~/.claude
        path = directory / name
        if os.path.islink(path):
            os.unlink(path)
        elif _is_link(path):
            os.rmdir(path)  # a junction: removes the link only
    shutil.rmtree(directory, ignore_errors=True)


def open_window(env, folder, window_id, title="Claude Code"):
    """A new terminal window running `claude` with `env` (the router's address and the window's id),
    in the home folder like a terminal opened by hand. Returns what tells when it has closed:
    {"pid"} or, on macOS (Terminal runs the script), {"pidFile", "script"}."""
    home = str(Path.home())
    if sys.platform == "darwin":
        # Like "Add account": a .command file Terminal runs by itself (it has the user's PATH). Its
        # shell writes its pid, then becomes `claude`.
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        script, pid_file = folder / f"{window_id}.command", folder / f"{window_id}.pid"
        script.write_text("#!/bin/sh\necho $$ > " + shlex.quote(str(pid_file)) + "\n"
                          + "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
                          + f"cd {shlex.quote(home)}\n"
                          + f"printf '\\033]0;%s\\007' {shlex.quote(title)}\nexec claude\n")
        script.chmod(0o700)
        subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"pidFile": str(pid_file), "script": str(script)}
    if not shutil.which("claude"):
        raise RuntimeError("Claude Code (claude) not found on PATH")
    if sys.platform == "win32":
        # cmd stays for the window's life: it is the process the app watches (and highlights)
        process = subprocess.Popen(["cmd.exe", "/k", f'title {title.replace("&", "and")} && claude'],
                                   env=dict(os.environ, **env), cwd=home, creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        terminal = next((t for t in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm") if shutil.which(t)), None)
        if terminal is None:
            raise RuntimeError("No terminal found; run `claude` with " + " ".join(f"{k}={v}" for k, v in env.items()))
        process = subprocess.Popen([terminal, "--" if terminal == "gnome-terminal" else "-e", "claude"], env=dict(os.environ, **env), cwd=home)
    return {"pid": process.pid}


# ---------- a window's session title ----------
_titles = {}  # transcript path -> what was read of it so far


def session_title(transcript):
    """The title of a Claude Code session, from its transcript: its name from /rename, else the
    title Claude Code gave it, else its first prompt. Only what was added since the last call is
    read (a status line asks every second or so); None until there is one."""
    try:
        size = os.path.getsize(transcript)
    except (OSError, TypeError, ValueError):
        return None
    seen = _titles.get(transcript)
    if seen is None or size < seen["offset"]:  # new, or rewritten (a Jev compaction)
        seen = _titles[transcript] = {"offset": 0, "custom": None, "ai": None, "summary": None, "first": None}
        if len(_titles) > 64:  # sessions come and go
            _titles.pop(next(iter(_titles)))
    if size > seen["offset"]:
        try:
            with open(transcript, "rb") as f:
                f.seek(seen["offset"])
                chunk = f.read(size - seen["offset"])
        except OSError:
            return _best_title(seen)
        end = chunk.rfind(b"\n") + 1  # whole lines only: the last one may still be being written
        seen["offset"] += end
        for line in chunk[:end].splitlines():
            _read_title_line(seen, line)
    return _best_title(seen)


_TITLE_LINE = re.compile(rb'"type":\s*"(custom-title|ai-title|summary)"')
_USER_LINE = re.compile(rb'"type":\s*"user"')


def _read_title_line(seen, line):
    if _TITLE_LINE.search(line):
        try:
            entry = json.loads(line)
        except ValueError:
            return
        for kind, key, slot in (("custom-title", "customTitle", "custom"), ("ai-title", "aiTitle", "ai"),
                                ("summary", "summary", "summary")):
            if entry.get("type") == kind and isinstance(entry.get(key), str) and entry[key].strip():
                seen[slot] = entry[key].strip()
    elif seen["first"] is None and _USER_LINE.search(line):
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if entry.get("isMeta") or entry.get("isSidechain"):
            return
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):
            content = next((part.get("text") for part in content if isinstance(part, dict) and part.get("type") == "text"), None)
        text = content.strip() if isinstance(content, str) else ""
        if text and not text.startswith(("<", "Caveat:")):  # slash commands and their output aren't prompts
            seen["first"] = " ".join(text.split())[:120]


def _best_title(seen):
    return seen["custom"] or seen["ai"] or seen["summary"] or seen["first"]


# ---------- the `claude` wrapper (the setting) ----------
# Arguments that never start a window: they go straight to the real `claude` (window.py has the same list).
PASS = ("auth", "mcp", "config", "update", "upgrade", "doctor", "install", "migrate-installer", "setup-token",
        "plugin", "plugins", "--version", "-v", "-h", "--help", "-p", "--print")


def wrapper_dir(vault_root):
    return Path(vault_root) / "bin"


def install_wrapper(vault_root, python, state_file):
    """Write the `claude` wrapper into the app's bin folder; returns that folder. (On Windows the
    folder goes first on the user's PATH: add_to_path.) If the app's Python is gone (uninstalled),
    the wrapper steps aside and runs the real `claude`.

    No process of the wrapper's stays: on macOS and Linux it becomes `claude` (exec); on Windows its
    Python only registers the window and writes the two variables to set into a file the .cmd reads,
    then the .cmd runs the real `claude` itself."""
    folder = wrapper_dir(vault_root)
    folder.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).with_name("window.py")
    if sys.platform == "win32":
        skip = "".join(f'if /i "%~1"=="{arg}" goto plain\r\n' for arg in PASS)
        text = ("@echo off\r\n"
                "setlocal\r\n"
                f'if not exist "{python}" goto plain\r\n'
                'if defined LIMITSWITCHER_WINDOW goto plain\r\n'
                + skip +
                'set "LIMITSWITCHER_ENV=%TEMP%\\limitswitcher-%RANDOM%%RANDOM%.cmd"\r\n'
                f'"{python}" -B "{script}" "{state_file}" --env "%LIMITSWITCHER_ENV%"\r\n'
                'if exist "%LIMITSWITCHER_ENV%" call "%LIMITSWITCHER_ENV%"\r\n'
                'if exist "%LIMITSWITCHER_ENV%" del "%LIMITSWITCHER_ENV%"\r\n'
                'set "LIMITSWITCHER_ENV="\r\n'
                ":plain\r\n"
                'set "PATH=%PATH:' + str(folder) + ';=%"\r\n'
                "claude %*\r\n"
                "exit /b %ERRORLEVEL%\r\n")
        target = folder / "claude.cmd"
    else:
        q = shlex.quote
        text = ("#!/bin/sh\n"
                f"if [ -x {q(str(python))} ]; then\n"
                f'  LIMITSWITCHER_WRAPPER_DIR={q(str(folder))} exec {q(str(python))} -B {q(str(script))} {q(str(state_file))} "$@"\n'
                "fi\n"
                f'PATH=$(printf %s "$PATH" | sed "s#{folder}:##")\n'
                'exec claude "$@"\n')
        target = folder / "claude"
    target.write_bytes(text.encode("utf-8"))  # as written: Windows text mode would double the \r of \r\n
    if sys.platform != "win32":
        target.chmod(0o755)
    return folder


def uninstall_wrapper(vault_root):
    shutil.rmtree(wrapper_dir(vault_root), ignore_errors=True)


def on_path(folder):
    return any(entry and key(entry) == key(folder) for entry in os.environ.get("PATH", "").split(os.pathsep))


def add_to_path(folder, add=True):
    """Windows: put the folder first on (or take it off) the user's PATH, for terminals opened from now
    on. Returns whether anything changed."""
    if sys.platform != "win32":
        return False
    import winreg
    import ctypes
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as handle:
        try:
            value, kind = winreg.QueryValueEx(handle, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        entries = [e for e in value.split(";") if e]
        kept = [e for e in entries if os.path.normcase(os.path.expandvars(e).rstrip("\\")) != os.path.normcase(str(folder))]
        wanted = ([str(folder)] + kept) if add else kept
        if wanted == entries:
            return False
        winreg.SetValueEx(handle, "Path", 0, kind, ";".join(wanted))
    # Tell Explorer (which starts new terminals) that the environment changed.
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, ctypes.byref(result))
    return True


# ---------- command line ----------
def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m account_switcher.profiles",
                                     description="Claude Code windows with an account of their own.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the saved Claude accounts, and where each is in use")
    parser.parse_args(argv)
    vault = Vault()
    meta = vault.load_meta()
    windows = _read_json(vault.root / "windows.json") or {}
    live = Claude().read_live()
    held = {}
    for window_id, window in sorted(windows.items(), key=lambda kv: kv[1].get("started") or 0):
        if isinstance(window, dict) and window.get("account"):
            held.setdefault(window["account"], []).append(f"window {window_id}")
    for account_id, m in sorted((meta.get("accounts") or {}).items(), key=lambda kv: kv[1].get("email") or ""):
        if m.get("provider") == "claude":
            where = held.get(account_id, []) + (["main"] if live and live.identity == m.get("identity") else [])
            print(f"{m.get('email') or account_id:40} {', '.join(where) or 'free'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
