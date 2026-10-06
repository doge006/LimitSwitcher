"""The optional Claude Code mod: installing it and checking on it.

It is two plugins from this repository's marketplace, installed and checked together:
  mods/limitswitcher feeds Claude Code's live usage to this app after every turn, and runs the
                     Jev compaction the app asks for right before it swaps a session's account
                     (named limit-status until 1.3.8: installing removes that one)
  mods/jev-compact   the compaction itself (Claude Code skips a plugin's own compaction hook when
                     that plugin starts the compaction, so it can't live in limitswitcher)
They are installed with Claude Code's own plugin commands; nothing but those commands and their
one setting each (where this app's files are) is written.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import processes
from .version import REPO

MARKETPLACE = "limitswitcher"
PLUGIN = "limitswitcher"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
JEV_ID = f"jev-compact@{MARKETPLACE}"
OLD_PLUGIN_ID = f"limit-status@{MARKETPLACE}"  # the same plugin under its name before 1.3.8
ENV_FILE = ".env"     # in the data folder: OPENROUTER_API_KEY=... for the Jev compaction
TIMEOUT = 90          # installing fetches the repository
QUIET = 20            # listing what is installed


def _run(args, timeout, stdin_text=None):
    """Runs `claude plugin <args>`. A command that outlives `timeout` is ended with everything it started
    (on Windows `claude` is a shim: ending only it would leave the real process running, and a few of
    those add up)."""
    cli = shutil.which("claude")
    if cli is None:
        raise FileNotFoundError("Claude Code isn't installed (no `claude` command found)")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen([cli, "plugin", *args], stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
    try:
        out, err = process.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        processes.end_tree(process.pid)
        try:
            process.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError):
            pass
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, out, err)


def installed():
    """True when both plugins are there, False when either is missing (an install from before the
    Jev compaction, or from before the rename, has only limit-status: Settings then offers the install again); None when it
    can't be told (no `claude` command, or an old one)."""
    try:
        done = _run(["list", "--json"], QUIET)
        if done.returncode != 0:
            return None
        ids = {p.get("id") for p in json.loads(done.stdout or "[]") if isinstance(p, dict)}
        return PLUGIN_ID in ids and JEV_ID in ids
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def env_file(state_file):
    """The .env file the Jev compaction reads its key from: beside the state file, in the data folder."""
    return Path(state_file).with_name(ENV_FILE)


def settings(state_file):
    """Each plugin's settings: where the app's local address file is, where the key file is."""
    return {PLUGIN_ID: {"statePath": str(state_file)}, JEV_ID: {"envFile": str(env_file(state_file))}}


def install(state_file, source=REPO):
    """Install (or update) both plugins. Returns None when done, else a short reason it didn't work."""
    try:
        added = _run(["marketplace", "add", source], TIMEOUT)
        if added.returncode != 0 and "already" not in (added.stdout + added.stderr).lower():
            return _why(added)
        _run(["marketplace", "update", MARKETPLACE], TIMEOUT)  # the catalog as it is now
        for plugin, values in settings(state_file).items():
            key, value = next(iter(values.items()))
            done = _run(["install", plugin, "--config", f"{key}={value}"], TIMEOUT)
            there = done.returncode == 0 or "already" in (done.stdout + done.stderr).lower()
            # Installing again changes nothing once it is there: an update is what brings in a newer version.
            updated = _run(["update", plugin], TIMEOUT)
            if not there and updated.returncode != 0:
                return _why(done)
        _run(["uninstall", OLD_PLUGIN_ID], TIMEOUT)  # the same plugin under its old name, if it is there
        return None
    except FileNotFoundError as error:
        return str(error)
    except (OSError, subprocess.SubprocessError) as error:
        return f"Couldn't run Claude Code ({type(error).__name__})"


def configure(state_file):
    """Tell the installed plugins where this app's files are (they move when the data folder does).
    Returns None when done, else a short reason."""
    try:
        for plugin, values in settings(state_file).items():
            done = _run(["configure", plugin, "--values-stdin"], QUIET, json.dumps(values))
            if done.returncode != 0:
                return _why(done)
        return None
    except FileNotFoundError as error:
        return str(error)
    except (OSError, subprocess.SubprocessError) as error:
        return f"Couldn't run Claude Code ({type(error).__name__})"


KEY_LINE = re.compile(r"^\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(['\"]?)(\S+?)\1\s*$", re.M)


def jev_key_source(state_file, claude_root=None):
    """Where the Jev compaction finds its OpenRouter key (the same order it looks): "env" (the
    environment), "settings" (Claude Code's settings.json env block), "file" (the .env file in the
    data folder), or None. Only where it is; this never returns the key."""
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        return "env"
    from . import claude_hooks
    try:
        path = (Path(claude_root) if claude_root else claude_hooks.settings_path()) / "settings.json"
        block = json.loads(path.read_text(encoding="utf-8")).get("env")
        if isinstance(block, dict) and str(block.get("OPENROUTER_API_KEY") or "").strip():
            return "settings"
    except (OSError, ValueError, AttributeError):
        pass
    try:
        if KEY_LINE.search(env_file(state_file).read_text(encoding="utf-8")) is not None:
            return "file"
    except (OSError, ValueError):
        pass
    return None


def jev_key_present(state_file, claude_root=None):
    """An OpenRouter key is set where the Jev compaction looks for one. Only whether it's there."""
    return jev_key_source(state_file, claude_root) is not None


def set_jev_key(state_file, key):
    """Save (or, with None, remove) the OpenRouter key in the .env file in the data folder, the
    file the Jev compaction reads. The file is private to the user and its other lines are kept.
    Raises ValueError for something that can't be a key."""
    from .vault import atomic_write
    path = env_file(state_file)
    if key is not None:
        key = key.strip().strip("'\"")
        if not key or len(key) > 300 or any(ch.isspace() or not ch.isprintable() for ch in key):
            raise ValueError("That doesn't look like an OpenRouter key")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        lines = []
    lines = [line for line in lines if not KEY_LINE.match(line)]
    if key is not None:
        lines.append("OPENROUTER_API_KEY=" + key)
    if not lines:
        try:
            path.unlink()
        except OSError:
            pass
        return
    atomic_write(path, ("\n".join(lines) + "\n").encode("utf-8"), private=True)


def _why(done):
    text = " ".join((done.stderr or done.stdout or "").split())
    return (text[:160] + "…") if len(text) > 160 else (text or "Claude Code refused the install")
