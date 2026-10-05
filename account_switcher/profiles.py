"""Per-window accounts (prototype): a Claude Code window that keeps its own account.

A profile is a Claude Code config folder of its own (CLAUDE_CONFIG_DIR) inside LimitSwitcher's
data folder, holding one saved account's login. A window started with it uses that account,
whatever the app switches the rest of the machine to. Settings, CLAUDE.md, plugins, skills and
session history are linked to ~/.claude, so the window works like any other (and /resume sees
every window's sessions); only the login differs.

Claude's refresh tokens are single-use: once one copy of a login renews, every other copy of it
is signed out. So:
- an account is in one place at a time: never in a profile while every other window uses it,
  and in at most one profile (the folder is named after the account);
- the window owns its tokens: the app takes them over as Claude Code renews them, and never
  renews them itself;
- the app never switches the machine to an account a profile holds, and a usage limit in a
  profile window stays in that window (the hook and the status line say which folder they're in).

    python -m account_switcher.profiles list
    python -m account_switcher.profiles open you@example.com     # a new terminal on that account
    python -m account_switcher.profiles env you@example.com      # CLAUDE_CONFIG_DIR=... for your own launcher
    python -m account_switcher.profiles remove you@example.com
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from .providers import Claude, _read_json
from .vault import Vault, atomic_write

FOLDER = "profiles"
PREFIX = "claude-"
# Linked to ~/.claude, when there: everything but the login. settings.json carries LimitSwitcher's
# own hook and status line too; they tell the app which folder they run in (see web.py).
SHARED = ("settings.json", "CLAUDE.md", "agents", "commands", "skills", "plugins", "output-styles",
          "projects", "todos", "history.jsonl")


def root(vault_root):
    return Path(vault_root) / FOLDER


def folder(vault_root, account_id):
    # The real path: Claude Code names its Keychain item (macOS) after it.
    return (root(vault_root) / (PREFIX + account_id)).resolve()


def existing(vault_root):
    """{account id: folder} for every profile there is."""
    try:
        entries = sorted(root(vault_root).iterdir())
    except OSError:
        return {}
    return {p.name[len(PREFIX):]: p.resolve() for p in entries if p.is_dir() and p.name.startswith(PREFIX)}


def same_folder(a, b):
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def provider(directory, keychain=None):
    return Claude(config_dir=directory, keychain=keychain)


def environment(directory):
    return {"CLAUDE_CONFIG_DIR": str(directory)}


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


def prepare(vault, account_id, live_identity=None, home=None, keychain=None):
    """Make (or reuse) the account's profile and put its login there; returns the folder.
    live_identity: the account every other window uses now, which can't also have a profile."""
    meta = (vault.load_meta().get("accounts") or {}).get(account_id)
    if meta is None:
        raise ValueError("Unknown account")
    if meta.get("provider") != "claude":
        raise ValueError("Only Claude Code accounts can have their own window for now")
    if live_identity and live_identity == meta.get("identity"):
        raise RuntimeError("Every other window uses this account now: switch them to another account first, "
                           "or one of the two copies of its login will be signed out")
    home = Path(home) if home else Path.home()
    directory = folder(vault.root, account_id)
    directory.mkdir(parents=True, exist_ok=True)
    _seed(directory, home)
    claude = provider(directory, keychain)
    current = claude.read_live()
    if current is None or current.identity != meta["identity"]:
        secret = vault.read_secret(account_id)
        if secret is None:
            raise RuntimeError("This account's saved login is missing; sign in again")
        claude.write_live(secret)
    # else: the window's own login is the newest (Claude Code renews it there); keep it
    return directory


def remove(vault, account_id, keychain=None):
    """Delete a profile; its newest login goes back to the saved copy first. The links go, never
    what they point at."""
    directory = existing(vault.root).get(account_id)
    if directory is None:
        return False
    claude = provider(directory, keychain)
    login = claude.read_live()
    meta = (vault.load_meta().get("accounts") or {}).get(account_id) or {}
    if login is not None and login.identity == meta.get("identity"):
        vault.write_secret(account_id, login.secret)
    claude.forget()
    for name in SHARED:  # the links themselves; rmtree must never walk into ~/.claude
        path = directory / name
        if os.path.islink(path):
            os.unlink(path)
        elif _is_link(path):
            os.rmdir(path)  # a junction: removes the link only
    shutil.rmtree(directory, ignore_errors=True)
    return True


def open_window(directory, title="Claude Code"):
    """A new terminal window running `claude` on the profile."""
    env = environment(directory)
    if sys.platform == "darwin":
        # Like "Add account": a .command file Terminal runs by itself (it has the user's PATH).
        script = directory / "Open window.command"
        script.write_text("#!/bin/sh\n" + "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
                          + f"printf '\\033]0;%s\\007' {shlex.quote(title)}\nexec claude\n")
        script.chmod(0o700)
        return subprocess.Popen(["/usr/bin/open", "-a", "Terminal", str(script)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not shutil.which("claude"):
        raise RuntimeError("Claude Code (claude) not found on PATH")
    if sys.platform == "win32":
        return subprocess.Popen(["cmd.exe", "/k", f'title {title.replace("&", "and")} && claude'],
                                env=dict(os.environ, **env), creationflags=subprocess.CREATE_NEW_CONSOLE)
    for terminal in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        if shutil.which(terminal):
            dash = "--" if terminal == "gnome-terminal" else "-e"
            return subprocess.Popen([terminal, dash, "claude"], env=dict(os.environ, **env))
    raise RuntimeError("No terminal found; run `claude` with " + " ".join(f"{k}={v}" for k, v in env.items()))


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


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m account_switcher.profiles",
                                     description="Claude Code windows that keep their own account.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the saved Claude accounts and which have their own window")
    for name, text in (("open", "open a terminal on the account"), ("env", "print the variable that selects it"),
                       ("remove", "delete the account's profile (the account stays saved)")):
        commands.add_parser(name, help=text).add_argument("account", help="email, name or id")
    args = parser.parse_args(argv)
    vault = Vault()
    meta = vault.load_meta()
    profiles = existing(vault.root)
    if args.command == "list":
        live = Claude().read_live()
        for account_id, m in sorted((meta.get("accounts") or {}).items(), key=lambda kv: kv[1].get("email") or ""):
            if m.get("provider") != "claude":
                continue
            where = "own window: " + str(profiles[account_id]) if account_id in profiles else \
                "every other window" if live and live.identity == m.get("identity") else "saved"
            print(f"{m.get('email') or account_id:40} {where}")
        return 0
    account_id = _find(meta, args.account)
    if args.command == "remove":
        print("Removed" if remove(vault, account_id) else "That account has no profile")
        return 0
    live = Claude().read_live()
    try:
        directory = prepare(vault, account_id, live_identity=live.identity if live else None)
    except (RuntimeError, ValueError) as error:
        raise SystemExit(str(error))
    if args.command == "env":
        for key, value in environment(directory).items():
            print(f"{key}={value}")
        return 0
    email = meta["accounts"][account_id].get("email") or account_id
    open_window(directory, f"Claude Code · {email}")
    print(f"Opened a window on {email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
