"""Claude Code status line (installed by LimitSwitcher while it runs).

Claude Code runs the status line command after each reply and passes it the session's data,
including the live 5-hour and weekly usage of the signed-in account (rate_limits). That is how
the app follows Claude usage live: no tokens, no API calls. This script
  1. hands rate_limits to the running app (loopback, a fraction of a second at most), then
  2. prints the status line: the user's own status line command if they had one (same input,
     except rate_limits holds the app's freshest numbers; its output unchanged), else the app's
     compact line (account and what's left).
The app also sets refreshInterval, so idle sessions re-run it and stay current.
Standard library only; it must not import the app. When the app isn't running it still runs
the user's own command, so their status line never breaks.
"""
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

# Bright colours, so they read on dark and light terminals alike.
ANSI = {"dim": "90", "label": "94", "good": "92", "warn": "93", "bad": "91"}
CACHE_FOR = 180  # seconds: the last line stands in while the app is busy (so the line doesn't blink out)


def paint(parts):
    """The coloured pieces ([{"t": text, "c": colour name}]) as one string with ANSI colours."""
    return "".join(f"\x1b[{ANSI[p['c']]}m{p['t']}\x1b[0m" if p.get("c") in ANSI else p["t"] for p in parts)


def left_color(left):
    return "good" if left > 30 else "warn" if left > 10 else "bad"


def report(state, data, cache=None):
    limits = data.get("rate_limits")
    try:
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        effort = data.get("effort") if isinstance(data.get("effort"), dict) else {}
        body = json.dumps({"rate_limits": limits if isinstance(limits, dict) else None,
                           "session": str(data.get("session_id") or "")[:100],
                           "model": model.get("display_name") or model.get("id"),
                           "effort": effort.get("level")}).encode()
        request = Request(state["url"].rsplit("/", 1)[0] + "/statusline", data=body, method="POST",
                          headers={"Authorization": "Bearer " + state["token"], "Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=0.6) as response:
            answer = json.load(response)
        if not isinstance(answer, dict):
            return None, None
        limits = answer.get("rate_limits")
        line = answer.get("line")
        parts = answer.get("parts") if isinstance(answer.get("parts"), list) else None
        shown = (paint(parts) if parts else line) if line else None
        remember(cache, shown)
        return shown, limits if isinstance(limits, dict) else None
    except HTTPError:
        return None, None  # the app refused (a token from before it restarted): nothing, not an old line
    except (OSError, ValueError, KeyError, TypeError):
        return recall(cache), None  # the app is busy or gone: the last line for a moment, not a blank one


def remember(cache, line):
    if cache is None:
        return
    try:
        if line:
            Path(cache).write_text(json.dumps({"at": time.time(), "line": line}), encoding="utf-8")
    except OSError:
        pass


def recall(cache):
    try:
        saved = json.loads(Path(cache).read_text(encoding="utf-8")) if cache else None
        return saved["line"] if saved and time.time() - saved["at"] < CACHE_FOR else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


MARKER = "\x1b[2m⇄ LimitSwitcher\x1b[0m"  # dim: shown at the end of the user's own line while the app runs


def tokens_text(count):
    return f"{count / 1_000_000:.1f}M" if count >= 1_000_000 else f"{round(count / 1000)}k" if count >= 1000 else str(count)


def context_part(data):
    """The session's context: how much is used and what's left of the window ("ctx 183k · 82% left").
    None until Claude Code reports it (before the first reply)."""
    window = data.get("context_window")
    if not isinstance(window, dict):
        return None
    used, left = window.get("total_input_tokens"), window.get("remaining_percentage")
    if not isinstance(used, (int, float)) or isinstance(used, bool) or used <= 0:
        return None
    text = "ctx " + tokens_text(int(used))
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        text += f" · {max(0, min(100, int(left)))}% left"
    return text


def context_painted(data):
    """The same, coloured by how much is left: "ctx 183k · 82% left" with the count and the
    percentage in that colour, "ctx" blue and the rest grey (the count alone when the percentage
    is unknown)."""
    text = context_part(data)
    if text is None:
        return None
    count, _, left = text.partition(" · ")
    label, _, number = count.partition(" ")
    if not left:
        return paint([{"t": label + " ", "c": "label"}, {"t": number}])
    color = left_color(int(left.split("%")[0]))
    return paint([{"t": label + " ", "c": "label"}, {"t": number, "c": color}, {"t": " · ", "c": "dim"}, {"t": left.split(" ")[0], "c": color},
                  {"t": " left", "c": "dim"}])


def run_previous(command, raw):
    """The user's own status line command: its output, unchanged."""
    try:
        done = subprocess.run(command, shell=True, input=raw, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return b""
    return done.stdout


def with_marker(output):
    """The user's status line with the marker after its last line (they may print several)."""
    text = output.rstrip(b"\r\n")
    ending = output[len(text):] or b"\n"
    return text + (b"  " if text else b"") + MARKER.encode("utf-8") + ending


def main(argv):
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    try:
        with open(argv[1], encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError, IndexError):
        state = {}
    cache = str(Path(argv[1]).with_name("statusline-cache.json")) if len(argv) > 1 else None
    line, limits = report(state, data if isinstance(data, dict) else {}, cache) if state.get("url") else (None, None)
    previous = state.get("statusline")
    if previous:
        if limits and isinstance(data, dict):
            # The app's freshest numbers for this account: a session that has been idle for a
            # while still carries the numbers from its last reply.
            fresh = dict(data.get("rate_limits") if isinstance(data.get("rate_limits"), dict) else {})
            for key, window in limits.items():
                fresh[key] = dict(fresh.get(key) if isinstance(fresh.get(key), dict) else {}, **window)
            raw = json.dumps(dict(data, rate_limits=fresh)).encode("utf-8")
        output = run_previous(previous, raw)
        write(with_marker(output) if line else output)  # the marker only while the app answers
    elif line:
        extra = context_painted(data) if isinstance(data, dict) else None
        write((line + (paint([{"t": " · ", "c": "dim"}]) + extra if extra else "") + "\n").encode("utf-8"))
    return 0


def write(data):
    """UTF-8 bytes to stdout (Windows would otherwise use its old code page for a pipe)."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        sys.stdout.write(data.decode("utf-8", "replace"))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
