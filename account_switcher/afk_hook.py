"""Claude Code StopFailure hook (installed by LimitSwitcher while AFK is on).

Runs in the background (asyncRewake). On a usage limit it asks the running app what to do:
  continue -> print the continuation note and exit 2, which wakes the Claude session
  wait     -> sleep until an account should have headroom again, then ask again
  anything else, or the app not running -> exit 0 and leave the session as it is
Standard library only; it must not import the app.
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

LIMIT = 6 * 3600 - 120   # stay inside the hook timeout set in settings.json
NOTE = "The usage limit was reached, so the session moved to another account. Continue exactly where you left off."


SHELLS = {"sh", "bash", "zsh", "dash", "fish", "cmd", "powershell", "pwsh", "python", "python3", "pythonw", "py", "env"}
CHECK = 15   # seconds between looks at whether Claude Code is still there while waiting


def owner():
    """(the process that started this hook, a function: is it still running?). That is Claude Code
    (the first parent that is not a shell). The hook waits for hours and is not stopped when its
    session is closed, so without this it would outlive the session and still ask the app to
    continue it. (None, None) when it can't be told: then it never gives up on its own."""
    try:
        path = Path(__file__).with_name("processes.py")
        spec = importlib.util.spec_from_file_location("_afk_processes", path)
        processes = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(processes)
        family = processes.family()
        pid = os.getpid()
        while pid in family:
            pid = family[pid][0]
            if pid <= 1 or pid not in family:
                return None, None
            if family[pid][1] not in SHELLS:
                break
        else:
            return None, None
        return pid, lambda: pid in (processes.family() or {pid: None})
    except Exception:
        return None, None


def wait(seconds, running):
    """Sleep up to `seconds`; False as soon as Claude Code is gone."""
    end = time.time() + seconds
    while time.time() < end:
        if running is not None and not running():
            return False
        time.sleep(max(0.0, min(CHECK, end - time.time())))
    return running is None or running()


TAIL = 512 * 1024   # the end of the transcript is enough to find the last reply's usage


def context_tokens(path):
    """The size of the session's context: what the last reply read (input + cached). A new account
    has none of it cached, so the next turn pays for all of it. None when it can't be told."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TAIL))
            lines = handle.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            try:
                usage = json.loads(line)["message"]["usage"]
                total = sum(int(usage.get(key) or 0) for key in
                            ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if total > 0:  # the limit's own error reply reports none
                return total
    except OSError:
        pass
    return None


def stamp(path):
    """The session's last real message (a prompt, a reply, a tool result), or None when it can't
    be told. Only these say the session went on: Claude Code also appends notices to the transcript
    while the hook waits (the account switch, a compaction's log line, its own wait being
    cancelled), and taking those for the session going on swallowed the continue."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TAIL))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError, ValueError):
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant"):
            continue  # notices, summaries, snapshots, titles
        if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isApiErrorMessage"):
            continue  # not the conversation going on (the limit's own error reply is one)
        return entry.get("uuid") or line
    return None


def main(argv):
    if len(argv) < 2:
        return 0
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("error") != "rate_limit":
        return 0
    opener = build_opener(ProxyHandler({}))  # the app is on loopback: never via a proxy
    deadline = time.time() + LIMIT
    _, running = owner()
    transcript = event.get("transcript_path") if isinstance(event.get("transcript_path"), str) else None
    tokens = context_tokens(transcript) if transcript else None
    started = None   # the transcript's state once the wait has begun (see below)
    while time.time() < deadline:
        if running is not None and not running():
            return 0  # Claude Code was closed: nothing to continue
        try:
            with open(argv[1], encoding="utf-8") as handle:
                state = json.load(handle)
            body = json.dumps({"provider": "claude", "session": str(event.get("session_id") or ""),
                               "contextTokens": tokens, "window": os.environ.get("LIMITSWITCHER_WINDOW") or None}).encode()
            request = Request(state["url"], data=body, method="POST",
                              headers={"Authorization": "Bearer " + state["token"], "Content-Type": "application/json"})
            with opener.open(request, timeout=180) as response:
                answer = json.load(response)
        except (OSError, ValueError, KeyError, TypeError):
            return 0  # the app is not running (or not answering): leave the session alone
        action = answer.get("action") if isinstance(answer, dict) else None
        if action == "continue":
            if started is not None and stamp(transcript) != started:
                return 0  # a new message since the wait began: the session went on by itself (Claude Code's own wait, or you), no second wake
            sys.stderr.write(str(answer.get("message") or NOTE))
            return 2
        if action == "wait":
            if answer.get("restamp"):
                started = None  # the app rewrote the transcript (a Jev compaction): look at it afresh
            if started is None and transcript:
                time.sleep(3)  # Claude Code may still be writing the limit's own entries
                started = stamp(transcript)
            seconds = answer.get("seconds")
            seconds = seconds if isinstance(seconds, (int, float)) else 600
            if not wait(max(5.0, min(float(seconds), deadline - time.time())), running):
                return 0
            continue
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
