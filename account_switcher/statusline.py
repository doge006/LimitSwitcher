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

It runs every second or so, once per open session, so its start-up is kept small: a plain socket
instead of urllib (which pulls in http.client, email, ssl), and no pathlib or subprocess unless
they're needed. A run is about 15 ms of CPU, 6 of them beyond starting Python.
"""
import os
import sys
import time

def _json_functions():
    """(loads, dumps). The json module's regex machinery costs about as much to import as the rest of
    this script costs to run, so its C scanner and encoder are used directly; if this Python's
    build doesn't have them in the form expected, the real json module is."""
    try:
        from _json import encode_basestring_ascii, make_encoder, make_scanner

        class Context:
            strict, object_hook, object_pairs_hook, parse_float, parse_int = True, None, None, float, int
            parse_constant = {"-Infinity": float("-inf"), "Infinity": float("inf"), "NaN": float("nan")}.__getitem__

        scan = make_scanner(Context)

        def refuse(value):
            raise TypeError(f"{type(value).__name__} is not JSON serializable")

        encode = make_encoder(None, refuse, encode_basestring_ascii, None, ":", ",", False, False, True)

        def loads(text):
            if isinstance(text, (bytes, bytearray)):
                text = text.decode("utf-8")
            text = text.strip()
            try:
                value, end = scan(text, 0)
            except StopIteration:
                raise ValueError("not JSON") from None
            if end != len(text):
                raise ValueError("not JSON")
            return value

        def dumps(value):
            return "".join(encode(value, 0))

        if loads(dumps({"a": [1, 2.5, None, True, "\u00e9\n"]})) != {"a": [1, 2.5, None, True, "\u00e9\n"]}:
            raise ValueError("the C helpers don't round-trip")
        return loads, dumps
    except Exception:
        import json
        return json.loads, json.dumps


loads, dumps = _json_functions()

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
        body = dumps({"rate_limits": limits if isinstance(limits, dict) else None,
                           "session": str(data.get("session_id") or "")[:100],
                           "model": model.get("display_name") or model.get("id"),
                           "effort": effort.get("level")}).encode()
        status, payload = post(state["url"].rsplit("/", 1)[0] + "/statusline", state["token"], body, 0.6)
        if status >= 400:
            return None, None  # the app refused (a token from before it restarted): nothing, not an old line
        answer = loads(payload)
        if not isinstance(answer, dict):
            return None, None
        limits = answer.get("rate_limits")
        line = answer.get("line")
        parts = answer.get("parts") if isinstance(answer.get("parts"), list) else None
        shown = (paint(parts) if parts else line) if line else None
        remember(cache, shown)
        return shown, limits if isinstance(limits, dict) else None
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return recall(cache), None  # the app is busy or gone: the last line for a moment, not a blank one


def connect(name, port, timeout):
    """A connected TCP socket. The C module directly: socket.py pulls in enum and selectors."""
    try:
        import _socket
    except ImportError:
        import socket
        return socket.create_connection((name, port), timeout)
    connection = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect((name, port))
    except BaseException:
        connection.close()
        raise
    return connection


def post(url, token, body, timeout):
    """POST `body` (JSON bytes) to the app's loopback address: (status, response body). A plain
    socket and HTTP/1.0: the app closes the connection after its answer."""
    host, _, path = url.partition("://")[2].partition("/")
    name, _, port = host.partition(":")
    head = (f"POST /{path} HTTP/1.0\r\nHost: {host}\r\nAuthorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode("ascii")
    deadline = time.monotonic() + timeout
    chunks = []
    connection = connect(name, int(port or 80), timeout)
    try:
        connection.sendall(head + body)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise OSError("the app took too long")
            connection.settimeout(left)
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        connection.close()
    answer = b"".join(chunks)
    header, _, payload = answer.partition(b"\r\n\r\n")
    return int(header.split(b" ", 2)[1]), payload


def remember(cache, line):
    if cache is None:
        return
    try:
        if not line:
            return
        try:  # the same line again: nothing to write until it is getting old
            with open(cache, encoding="utf-8") as handle:
                saved = loads(handle.read())
            if saved["line"] == line and time.time() - saved["at"] < CACHE_FOR / 3:
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        with open(cache, "w", encoding="utf-8") as handle:
            handle.write(dumps({"at": time.time(), "line": line}))
    except OSError:
        pass


def recall(cache, lasts=CACHE_FOR):
    try:
        if not cache:
            return None
        with open(cache, encoding="utf-8") as handle:
            saved = loads(handle.read())
        return saved["line"] if saved and (lasts is None or time.time() - saved["at"] < lasts) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


MARKER = "\x1b[2m⇄ LimitSwitcher\x1b[0m"  # dim: shown at the end of the user's own line while the app runs


def tokens_text(count):
    return f"{count / 1_000_000:.1f}M" if count >= 1_000_000 else f"{round(count / 1000)}k" if count >= 1000 else str(count)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def context_part(data):
    """The session's context: how much is used and what's left of the window ("ctx 183k/82% left").
    None until Claude Code reports it (before the first reply). The count and the percentage come
    from the same figure (the current context, from current_usage) so they move together; the
    percentage Claude Code reports separately is only the fallback (it can trail the count)."""
    window = data.get("context_window")
    if not isinstance(window, dict):
        return None
    usage, size = window.get("current_usage"), window.get("context_window_size")
    used = left = None
    if isinstance(usage, dict):
        parts = [usage.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        if any(_number(v) for v in parts):
            used = sum(v for v in parts if _number(v))
    if used is None:
        used = window.get("total_input_tokens")
    if not _number(used) or used <= 0:
        return None
    if _number(size) and size > 0 and (isinstance(usage, dict) and used != window.get("total_input_tokens")):
        left = 100 - used * 100 / size
    elif _number(window.get("remaining_percentage")):
        left = window["remaining_percentage"]
    elif _number(size) and size > 0:
        left = 100 - used * 100 / size
    text = "ctx " + tokens_text(int(used))
    if left is not None:
        text += f"/{max(0, min(100, int(left)))}% left"
    return text


def context_painted(data):
    """The same, coloured by how much is left: "ctx 183k/82% left" with the count and the
    percentage in that colour, "ctx" blue and the rest grey (the count alone when the percentage
    is unknown)."""
    text = context_part(data)
    if text is None:
        return None
    count, _, left = text.partition("/")
    label, _, number = count.partition(" ")
    if not left:
        return paint([{"t": label + " ", "c": "label"}, {"t": number}])
    color = left_color(int(left.split("%")[0]))
    return paint([{"t": label + " ", "c": "label"}, {"t": number, "c": color}, {"t": "/", "c": "dim"}, {"t": left.split(" ")[0], "c": color},
                  {"t": " left", "c": "dim"}])


def run_previous(command, raw):
    """The user's own status line command: its output, unchanged."""
    import subprocess  # only when there is a status line of the user's own to run
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
        data = loads(raw or b"{}")
    except ValueError:
        data = {}
    try:
        with open(argv[1], encoding="utf-8") as handle:
            state = loads(handle.read())
    except (OSError, ValueError, IndexError):
        state = {}
    cache = os.path.join(os.path.dirname(argv[1]), "statusline-cache.json") if len(argv) > 1 else None
    line, limits = report(state, data if isinstance(data, dict) else {}, cache) if state.get("url") else (None, None)
    previous = state.get("statusline")
    if previous:
        if limits and isinstance(data, dict):
            # The app's freshest numbers for this account: a session that has been idle for a
            # while still carries the numbers from its last reply.
            fresh = dict(data.get("rate_limits") if isinstance(data.get("rate_limits"), dict) else {})
            for key, window in limits.items():
                fresh[key] = dict(fresh.get(key) if isinstance(fresh.get(key), dict) else {}, **window)
            raw = dumps(dict(data, rate_limits=fresh)).encode("utf-8")
        output = run_previous(previous, raw)
        write(with_marker(output) if line else output)  # the marker only while the app answers
    else:
        extra = context_painted(data) if isinstance(data, dict) else None
        ctx_cache = None
        if cache and isinstance(data, dict):
            session = "".join(c for c in str(data.get("session_id") or "")[:60] if c.isalnum() or c in "-_")
            ctx_cache = os.path.join(os.path.dirname(cache), f"statusline-ctx-{session}.json") if session else None
        if extra:
            remember(ctx_cache, extra)
        else:
            # A run without the figures (a usage limit, a compaction, until the next reply): the
            # session's last ones, however long it waits, not a gap
            extra = recall(ctx_cache, None)
        if line or extra:
            pieces = [line] if line else []
            if extra:
                pieces.append(extra)
            write((paint([{"t": " · ", "c": "dim"}]).join(pieces) + "\n").encode("utf-8"))
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
