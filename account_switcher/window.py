"""The `claude` wrapper for "Separate accounts per window" (see profiles.py).

While the setting is on, LimitSwitcher puts a `claude` command first on PATH that runs this. A new
interactive Claude Code window asks the running app for a profile of its own (a config folder with
one account's login) and starts the real `claude` with CLAUDE_CONFIG_DIR pointing at it, so the app
can later switch that window, and only that window, to another account.

Everything else goes straight to the real `claude`, unchanged: subcommands (auth, mcp, update, ...),
print mode (-p), a CLAUDE_CONFIG_DIR already set, the app not running, or no account free.

    python window.py <state file> [claude arguments...]

Standard library only; it must not import the app (it runs with every `claude`).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
from urllib.request import ProxyHandler, Request, build_opener

PASS = {"auth", "mcp", "config", "update", "upgrade", "doctor", "install", "migrate-installer", "setup-token",
        "plugin", "plugins", "--version", "-v", "-h", "--help"}
ASK_TIMEOUT = 4


def real_claude(skip):
    """The real `claude` on PATH, past our own folder."""
    skip = os.path.normcase(os.path.realpath(skip)) if skip else None
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or os.path.normcase(os.path.realpath(entry)) == skip:
            continue
        found = shutil.which("claude", path=entry)
        if found:
            return found
    return None


def interactive(args):
    if args and args[0] in PASS:
        return False
    return not any(a in ("-p", "--print") or a.startswith("--print=") for a in args)


def ask(state_file):
    """The app's answer: a profile folder for this window, or None (share the main login)."""
    try:
        with open(state_file, encoding="utf-8") as handle:
            state = json.load(handle)
        body = json.dumps({"pid": os.getpid(), "cwd": os.getcwd()}).encode()
        url = state["url"].rsplit("/", 1)[0] + "/window"
        request = Request(url, data=body, method="POST",
                          headers={"Authorization": "Bearer " + state["token"], "Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=ASK_TIMEOUT) as response:  # loopback: never via a proxy
            answer = json.load(response)
    except (OSError, ValueError, KeyError, TypeError):
        return None  # the app isn't running (or not answering)
    folder = answer.get("configDir") if isinstance(answer, dict) else None
    return folder if isinstance(folder, str) and os.path.isdir(folder) else None


def main(argv):
    if len(argv) < 2:
        return 2
    state_file, args = argv[1], argv[2:]
    claude = real_claude(os.environ.get("LIMITSWITCHER_WRAPPER_DIR") or "")  # set by the wrapper: the folder to skip
    if claude is None:
        sys.stderr.write("LimitSwitcher: Claude Code (claude) isn't installed, or isn't on PATH\n")
        return 127
    env = dict(os.environ)
    env.pop("LIMITSWITCHER_WRAPPER_DIR", None)
    if interactive(args) and not env.get("CLAUDE_CONFIG_DIR"):
        folder = ask(state_file)
        if folder:
            env["CLAUDE_CONFIG_DIR"] = folder
    if sys.platform != "win32":
        os.execve(claude, [claude, *args], env)  # same process: the app knows the window by this pid
    # Windows: no exec. This process stays for the window's life (its pid is the one the app watches);
    # Ctrl+C belongs to Claude Code, which shares the console.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    command = [claude, *args]
    if claude.lower().endswith((".cmd", ".bat")):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *command]
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
