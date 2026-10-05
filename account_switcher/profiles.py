"""Separate accounts per window: each Claude Code window with a login of its own.

A profile is a Claude Code config folder (CLAUDE_CONFIG_DIR) inside LimitSwitcher's data folder,
`profiles/window-<id>`, holding one account's login. A window started with it uses that account, and
switching it rewrites only that folder's login (the window picks it up on its next request, like a
switch today). Settings, CLAUDE.md, plugins, skills and session history are linked to ~/.claude, so
the window works like any other and /resume sees every window's sessions; only the login differs.

Where profiles come from:
- the setting "Separate accounts per window": a `claude` wrapper first on PATH (window.py) asks the
  app for a profile each time a new interactive window starts;
- "Own window" on an account's card, or `python -m account_switcher.profiles open EMAIL`.
`window.json` in the folder says which process is the window; once it has ended, the app takes the
login back and deletes the folder, which frees the account.

Claude's refresh tokens are single-use: once one copy of a login renews, every other copy of it is
signed out. So an account is in one place at a time (the main login every other window shares, or one
profile); the window owns its tokens (the app takes them over as Claude Code renews them and never
renews them itself); and a limit in a profile window is handled for that window alone (the hook and
the status line send their CLAUDE_CONFIG_DIR).

    python -m account_switcher.profiles list
    python -m account_switcher.profiles open you@example.com     # a new terminal on that account
    python -m account_switcher.profiles env you@example.com      # CLAUDE_CONFIG_DIR=... for your own launcher
    python -m account_switcher.profiles remove window-1a2b3c4d
"""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time

from .providers import Claude, _read_json
from .vault import Vault, atomic_write

FOLDER = "profiles"
PREFIX = "window-"
INFO = "window.json"
PID_FILE = "window.pid"   # written by the macOS launcher script: the shell that becomes `claude`
# Linked to ~/.claude, when there: everything but the login. settings.json carries LimitSwitcher's
# own hook and status line too; they say which folder they run in (see web.py).
SHARED = ("settings.json", "CLAUDE.md", "agents", "commands", "skills", "plugins", "output-styles",
          "projects", "todos", "history.jsonl")


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


def environment(directory):
    return {"CLAUDE_CONFIG_DIR": str(directory)}


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


def _link(source, target):
    """A link at `target` to `source`: a symlink, else (Windows without Developer Mode) a junction
    for a folder or a copy of a file. Returns how, for the log."""
    try:
        os.symlink(source, target, target_is_directory=source.is_dir())
        return "linked"
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            raise
    if source.is_dir():
        import _winapi
        _winapi.CreateJunction(str(source), str(target))
        return "junction"
    shutil.copy2(source, target)
    return "copied"


def _seed(directory, home):
    """A new profile starts with the user's own Claude Code setup: ~/.claude.json (trusted folders,
    MCP servers, onboarding done) without its login, and links to the shared parts of ~/.claude."""
    claude_home = home / ".claude"
    config = directory / ".claude.json"
    if not config.exists():
        base = _read_json(home / ".claude.json") or {}
        base.pop("oauthAccount", None)
        atomic_write(config, json.dumps(base, indent=2).encode())
    done = []
    for name in SHARED:
        source, target = claude_home / name, directory / name
        if not source.exists() or target.exists() or _is_link(target):
            continue
        try:
            done.append(f"{name} ({_link(source, target)})")
        except OSError as error:
            done.append(f"{name} (not shared: {error})")
    return done


def create(vault, account_id, home=None, keychain=None, **window):
    """A new profile with this account's login; returns its folder. The caller makes sure the
    account isn't in use anywhere else. window: pid, cwd, how (for window.json)."""
    meta = (vault.load_meta().get("accounts") or {}).get(account_id)
    if meta is None:
        raise ValueError("Unknown account")
    if meta.get("provider") != "claude":
        raise ValueError("Only Claude Code accounts can have a window of their own for now")
    secret = vault.read_secret(account_id)
    if secret is None:
        raise RuntimeError("This account's saved login is missing; sign in again")
    home = Path(home) if home else Path.home()
    # The real path: Claude Code names its Keychain item (macOS) after it.
    directory = (root(vault.root) / (PREFIX + secrets.token_hex(4))).resolve()
    directory.mkdir(parents=True)
    _seed(directory, home)
    set_info(directory, started=time.time(), **{k: v for k, v in window.items() if v is not None})
    provider(directory, keychain).write_live(secret)
    return directory


def remove(directory, keychain=None):
    """Delete a profile (take its login back first: LiveAccounts.sync_profiles). The links go,
    never what they point at."""
    directory = Path(directory)
    provider(directory, keychain).forget()
    for name in SHARED:  # the links themselves; rmtree must never walk into ~/.claude
        path = directory / name
        if os.path.islink(path):
            os.unlink(path)
        elif _is_link(path):
            os.rmdir(path)  # a junction: removes the link only
    shutil.rmtree(directory, ignore_errors=True)


def open_window(directory, title="Claude Code"):
    """A new terminal window running `claude` on the profile; records which process is the window."""
    env = environment(directory)
    if sys.platform == "darwin":
        # Like "Add account": a .command file Terminal runs by itself (it has the user's PATH). Its
        # shell writes its pid, then becomes `claude`.
        script = directory / "Open window.command"
        script.write_text("#!/bin/sh\necho $$ > " + shlex.quote(str(directory / PID_FILE)) + "\n"
                          + "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
                          + f"printf '\\033]0;%s\\007' {shlex.quote(title)}\nexec claude\n")
        script.chmod(0o700)
        subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if not shutil.which("claude"):
        raise RuntimeError("Claude Code (claude) not found on PATH")
    if sys.platform == "win32":
        # cmd stays for the window's life: it is the process the app watches (and highlights)
        process = subprocess.Popen(["cmd.exe", "/k", f'title {title.replace("&", "and")} && claude'],
                                   env=dict(os.environ, **env), creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        terminal = next((t for t in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm") if shutil.which(t)), None)
        if terminal is None:
            raise RuntimeError("No terminal found; run `claude` with " + " ".join(f"{k}={v}" for k, v in env.items()))
        process = subprocess.Popen([terminal, "--" if terminal == "gnome-terminal" else "-e", "claude"], env=dict(os.environ, **env))
    set_info(directory, pid=process.pid)


# ---------- the `claude` wrapper (the setting) ----------
def wrapper_dir(vault_root):
    return Path(vault_root) / "bin"


def install_wrapper(vault_root, python, state_file):
    """Write the `claude` wrapper into the app's bin folder; returns that folder. (On Windows the
    folder goes first on the user's PATH: add_to_path.) If the app's Python is gone (uninstalled),
    the wrapper steps aside and runs the real `claude`."""
    folder = wrapper_dir(vault_root)
    folder.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).with_name("window.py")
    if sys.platform == "win32":
        text = ("@echo off\r\n"
                f'if not exist "{python}" goto plain\r\n'
                'set "LIMITSWITCHER_WRAPPER_DIR=%~dp0."\r\n'
                f'"{python}" -B "{script}" "{state_file}" %*\r\n'
                "exit /b %ERRORLEVEL%\r\n"
                ":plain\r\n"
                'set "PATH=%PATH:' + str(folder) + ';=%"\r\n'
                "claude %*\r\n")
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
    target.write_text(text, encoding="utf-8")
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
def _find(meta, query):
    accounts = {i: m for i, m in (meta.get("accounts") or {}).items() if m.get("provider") == "claude"}
    q = query.strip().lower()
    hits = [i for i, m in accounts.items() if q in (i.lower(), (m.get("email") or "").lower(),
                                                    (m.get("label") or "").lower(), str(m.get("identity")).lower())]
    if len(hits) != 1:
        known = ", ".join(sorted(m.get("email") or i for i, m in accounts.items())) or "none saved yet"
        raise SystemExit(f"No single Claude account matches {query!r} (accounts: {known})")
    return hits[0]


def _held(vault):
    """{account identity: where it's in use} for the main login and every profile."""
    held = {}
    live = Claude().read_live()
    if live:
        held[live.identity] = "every other window"
    for name, directory in existing(vault.root).items():
        login = provider(directory).read_live()
        if login:
            held[login.identity] = name
    return held


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m account_switcher.profiles",
                                     description="Claude Code windows that keep their own account.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the saved Claude accounts, and where each is in use")
    for name, text in (("open", "open a terminal on the account"), ("env", "print the variable that selects it")):
        commands.add_parser(name, help=text).add_argument("account", help="email, name or id")
    commands.add_parser("remove", help="delete a profile (close its window first)").add_argument("window", help="window-<id>")
    args = parser.parse_args(argv)
    vault = Vault()
    meta = vault.load_meta()
    if args.command == "list":
        held = _held(vault)
        for account_id, m in sorted((meta.get("accounts") or {}).items(), key=lambda kv: kv[1].get("email") or ""):
            if m.get("provider") == "claude":
                print(f"{m.get('email') or account_id:40} {held.get(m.get('identity'), 'free')}")
        return 0
    if args.command == "remove":
        directory = existing(vault.root).get(args.window)
        if directory is None:
            raise SystemExit("No such profile")
        login = provider(directory).read_live()
        account_id = next((i for i, m in (meta.get("accounts") or {}).items() if login and m.get("identity") == login.identity), None)
        if account_id:
            vault.write_secret(account_id, login.secret)  # its newest tokens
        remove(directory)
        print("Removed")
        return 0
    account_id = _find(meta, args.account)
    where = _held(vault).get(meta["accounts"][account_id]["identity"])
    if where:
        raise SystemExit(f"That account is in use ({where}); one copy of its login would be signed out")
    directory = create(vault, account_id, how="manual" if args.command == "env" else "terminal")
    if args.command == "env":
        for k, value in environment(directory).items():
            print(f"{k}={value}")
        return 0
    email = meta["accounts"][account_id].get("email") or account_id
    open_window(directory, f"Claude Code · {email}")
    print(f"Opened a window on {email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
