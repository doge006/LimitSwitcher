"""The `claude` wrapper for "Separate accounts per window" (see claude_router.py).

While the setting is on, LimitSwitcher puts a `claude` command first on PATH that runs this. A new
interactive Claude Code window asks the running app to register it, then starts as an ordinary Claude
Code window (~/.claude, its settings, plugins and login) with two variables more:
  ANTHROPIC_BASE_URL    the app's router, with the window's id in the path: the router puts the
                        window's account on its requests (and passes them on unchanged otherwise)
  LIMITSWITCHER_WINDOW  the window's id, for its status line, hooks and mod to say which window they're in
A window already started with an ANTHROPIC_BASE_URL of its own keeps it: the router forwards there.

Everything else goes straight to the real `claude`, unchanged: subcommands (auth, mcp, update, ...),
print mode (-p), a window already registered, the app not running, or the setting off.

    python window.py <state file> [claude arguments...]     macOS, Linux: becomes `claude` (exec)
    python window.py <state file> --env <file>              Windows: writes `set` lines for claude.cmd

Standard library only; it must not import the app (it runs with every `claude`).
"""
import json
import os
import shutil
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


def ask(state_file, pid):
    """The app's answer: {"ANTHROPIC_BASE_URL", "LIMITSWITCHER_WINDOW"} for this window, or None
    (the app isn't running, or the setting is off)."""
    try:
        with open(state_file, encoding="utf-8") as handle:
            state = json.load(handle)
        body = json.dumps({"pid": pid, "cwd": os.getcwd(), "upstream": os.environ.get("ANTHROPIC_BASE_URL") or None}).encode()
        url = state["url"].rsplit("/", 1)[0] + "/window"
        request = Request(url, data=body, method="POST",
                          headers={"Authorization": "Bearer " + state["token"], "Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=ASK_TIMEOUT) as response:  # loopback: never via a proxy
            answer = json.load(response)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(answer, dict):
        return None
    window, base = answer.get("window"), answer.get("baseUrl")
    if not (isinstance(window, str) and window.isalnum() and isinstance(base, str) and base.startswith("http://127.0.0.1:")):
        return None
    return {"ANTHROPIC_BASE_URL": base, "LIMITSWITCHER_WINDOW": window}


def write_env(state_file, path):
    """Windows: the window's variables as `set` lines claude.cmd calls before it runs the real
    `claude` (this process is gone by then; the window's process is the .cmd's)."""
    if os.environ.get("LIMITSWITCHER_WINDOW"):
        return 0
    env = ask(state_file, os.getppid())
    if env:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("".join(f'set "{k}={v}"\r\n' for k, v in env.items()))
    return 0


def main(argv):
    if len(argv) < 2:
        return 2
    state_file, args = argv[1], argv[2:]
    if args[:1] == ["--env"] and len(args) == 2:
        return write_env(state_file, args[1])
    claude = real_claude(os.environ.get("LIMITSWITCHER_WRAPPER_DIR") or "")  # set by the wrapper: the folder to skip
    if claude is None:
        sys.stderr.write("LimitSwitcher: Claude Code (claude) isn't installed, or isn't on PATH\n")
        return 127
    env = dict(os.environ)
    env.pop("LIMITSWITCHER_WRAPPER_DIR", None)
    if interactive(args) and not env.get("LIMITSWITCHER_WINDOW"):
        env.update(ask(state_file, os.getpid()) or {})  # exec keeps this pid: the app knows the window by it
    os.execve(claude, [claude, *args], env)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
