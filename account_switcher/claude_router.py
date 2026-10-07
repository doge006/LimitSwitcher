"""Local router for Claude Code: separate accounts per window, with every window an ordinary one.

A window started by the `claude` wrapper (window.py) has ANTHROPIC_BASE_URL pointing here, with the
window's id in the path. Everything else about it is Claude Code as usual: ~/.claude, its settings,
plugins, history and its own login. Each request goes on to Anthropic unchanged, except for the login:
- a window on the main account: the login Claude Code sent (its own, which it keeps renewing);
- a window given an account of its own: that account's access token, kept fresh by the app.
So switching a window changes only where its next request is billed, and nothing on disk.

Cheap by design: one thread per open connection (Claude Code keeps one or two per window, and an idle
one closes after IDLE seconds), bodies streamed through in 64 KB pieces (a request is held only while
it is sent, for the one retry a renewed login needs), and the connections to Anthropic are kept open
and reused, TLS session included, so a request costs no handshake. Nothing is logged or stored here.
The path carries a random secret, so other local programs cannot borrow the logins.
"""
from http.server import BaseHTTPRequestHandler
import http.client
import json
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

from . import connections, tls
from .local_http import LocalServer

UPSTREAM = "https://api.anthropic.com"
DEFAULT_PORT = 47831
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
              "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
MAX_BODY = 64 * 1024 * 1024
CHUNK = 65536
TIMEOUT = 600        # an answer can think for minutes before its next byte
IDLE = 120           # seconds an idle connection from Claude Code is kept (Claude Code opens a new one after)
POOL_IDLE = 240      # seconds an idle connection to Anthropic is kept (its servers drop them sooner or later)
POOL_SIZE = 8        # idle connections kept per upstream
NO_BODY = {204, 304}


def error_body(message, kind="api_error"):
    """An error in Anthropic's own shape, so Claude Code shows (and retries) it as one of theirs."""
    return json.dumps({"type": "error", "error": {"type": kind, "message": message}}).encode()


class Upstreams:
    """Kept-open connections to each upstream (the system's proxy settings followed, as in
    connections.py): a request takes one, and gives it back once its answer has been read."""

    def __init__(self):
        self.lock = threading.Lock()
        self.idle = {}  # (scheme, host, port, proxy) -> [(connection, when it was given back)]

    @staticmethod
    def key(url):
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host, port = parts.hostname, parts.port or (443 if scheme == "https" else 80)
        return scheme, host, port, connections._proxy_for(scheme, host)

    def take(self, key):
        """(connection, reused?)"""
        now = time.monotonic()
        with self.lock:
            stack = self.idle.get(key) or []
            while stack:
                connection, since = stack.pop()
                if now - since < POOL_IDLE:
                    return connection, True
                connection.close()
        scheme, host, port, proxy = key
        return connections._new(scheme, host, port, proxy, TIMEOUT, tls.context()), False

    def give(self, key, connection):
        with self.lock:
            stack = self.idle.setdefault(key, [])
            if len(stack) < POOL_SIZE:
                stack.append((connection, time.monotonic()))
                return
        connection.close()

    def close(self):
        with self.lock:
            stacks, self.idle = list(self.idle.values()), {}
        for stack in stacks:
            for connection, _ in stack:
                connection.close()


class ClaudeRouter:
    """windows: what the router needs from the app (see LiveAccounts):
      window_route(window id)            -> (account id or None for the session's own login, upstream URL or None)
      window_token(account id, renew)    -> that account's access token (renewed first when renew is true)
      window_login_failed(window id, account id, error) -> the account's login can't be used: the request
                                            then goes with the session's own login
    """

    def __init__(self, windows, port=DEFAULT_PORT, secret=None, upstream=None):
        self.windows = windows
        self.port = port
        self.secret = secret or secrets.token_hex(12)
        self.upstream = (upstream or UPSTREAM).rstrip("/")
        self.server = None
        self.pool = Upstreams()

    def base_url(self, window_id):
        return f"http://127.0.0.1:{self.port}/{self.secret}/{window_id}"

    def start(self):
        router = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = IDLE

            def log_message(self, *_):
                pass

            def do_GET(self):
                router.handle(self)

            do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

        for port in [self.port] + list(range(self.port + 1, self.port + 20)):
            try:
                self.server = LocalServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        else:
            raise RuntimeError("No free local port for the Claude Code router")
        self.server.daemon_threads = True
        self.port = self.server.server_port
        # Sleeps until a connection comes (the default wakes twice a second, forever)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 3600}, daemon=True,
                         name="claude-router").start()
        return self.port

    def close(self):
        server, self.server = self.server, None
        if server:
            stopper = threading.Thread(target=server.shutdown, daemon=True)
            stopper.start()
            try:
                socket.create_connection(("127.0.0.1", server.server_port), timeout=1).close()  # wake it
            except OSError:
                pass
            stopper.join(5)
            server.server_close()
        self.pool.close()

    # ---------- one request ----------
    def handle(self, h):
        prefix = "/" + self.secret + "/"
        if not h.path.startswith(prefix):
            return self.reply(h, 403, error_body("Unknown route", "permission_error"))
        window_id, _, rest = h.path[len(prefix):].partition("/")
        try:
            body = self.read_body(h)
        except ValueError as error:
            return self.reply(h, 413, error_body(str(error), "request_too_large"))
        try:
            account, upstream = self.windows.window_route(window_id)
        except Exception:
            account, upstream = None, None
        base = (upstream or self.upstream).rstrip("/")
        parts = urlsplit(base)
        target = (parts.path or "") + "/" + rest
        headers = {k: v for k, v in h.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Host"] = parts.netloc
        own = headers.get("Authorization") or headers.get("authorization")
        # Only a Claude login is swapped (a Bearer token); an API key (x-api-key) goes as it came.
        swap = account is not None and isinstance(own, str) and own.lower().startswith("bearer ")
        renew = False
        while True:
            if swap:
                try:
                    token = self.windows.window_token(account, renew)
                except Exception as error:
                    self.failed(window_id, account, error)
                    swap, token = False, None
                if token:
                    for name in [k for k in headers if k.lower() == "authorization"]:
                        del headers[name]
                    headers["Authorization"] = "Bearer " + token
            if not swap and own:
                for name in [k for k in headers if k.lower() == "authorization"]:
                    del headers[name]
                headers["Authorization"] = own
            key = self.pool.key(base)
            response, connection = self.send(key, h.command, target, headers, body)
            if response is None:
                return self.reply(h, 502, error_body(f"LimitSwitcher couldn't reach Anthropic: {connection}", "api_error"))
            if response.status == 401 and swap and not renew:
                response.read()  # small: an error
                self.done(key, connection, response)
                renew = True  # the saved login's token was refused early: renew it once and send again
                continue
            if response.status == 401 and swap:
                self.failed(window_id, account, "refused after renewal")
            return self.relay(h, key, connection, response)

    def failed(self, window_id, account, error):
        try:
            self.windows.window_login_failed(window_id, account, error)
        except Exception:
            pass

    def send(self, key, method, target, headers, body):
        """(response, connection) or (None, the error). A kept-open connection the server has closed
        meanwhile fails before any answer: the request is sent again on a new one."""
        connection, reused = self.pool.take(key)
        scheme, host, port, proxy = key
        if proxy and scheme == "http":  # a plain proxy takes the whole URL
            target = f"http://{host}:{port}{target}"
        while True:
            try:
                connection.request(method, target, body=body if body or method not in ("GET", "HEAD") else None,
                                   headers=headers)
                sock = connection.sock
                response = connection.getresponse()
            except connections.STALE as error:
                connection.close()
                if reused:
                    connection, reused = connections._new(scheme, host, port, proxy, TIMEOUT, tls.context()), False
                    continue
                return None, error
            except (OSError, http.client.HTTPException) as error:
                connection.close()
                return None, error
            if isinstance(connection, connections._Https):
                connection.remember_session(sock)
            return response, connection

    def done(self, key, connection, response):
        """The answer has been read whole: the connection goes back for the next request."""
        if response.will_close:
            connection.close()
        else:
            self.pool.give(key, connection)

    def relay(self, h, key, connection, response):
        """The answer, as it arrives: an event stream goes through piece by piece."""
        status = response.status
        length = response.getheader("Content-Length")
        bodiless = h.command == "HEAD" or status in NO_BODY or 100 <= status < 200
        chunked = not bodiless and length is None
        h.send_response(status, response.reason)
        for name, value in response.getheaders():
            if name.lower() not in HOP_BY_HOP:
                h.send_header(name, value)
        if length is not None:
            h.send_header("Content-Length", length)
        if chunked:
            h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        whole = False
        try:
            if bodiless:
                pass
            elif chunked and response.chunked:
                whole = self.relay_chunks(h, response)  # as it came, framing and all
            else:
                while True:
                    piece = response.read1(CHUNK)
                    if not piece:
                        break
                    h.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece) if chunked else piece)
                if chunked:
                    h.wfile.write(b"0\r\n\r\n")
                whole = True
            whole = whole or bodiless
        except (OSError, http.client.HTTPException, ValueError):
            h.close_connection = True  # the window went away (or Anthropic did) mid-answer
        if whole:
            response.close()  # read to its end: the connection is free (http.client keeps its socket)
            self.done(key, connection, response)
        else:
            connection.close()

    @staticmethod
    def relay_chunks(h, response):
        """An event stream's bytes straight through, chunk framing included: one write per read from
        the network, and only the chunk sizes looked at, to see where the answer ends (http.client
        would decode each event and the router re-frame it). True once the last chunk has come."""
        raw = response.fp
        need, line, trailer = 0, b"", False
        while True:
            data = raw.read1(CHUNK)
            if not data:
                return False  # cut off before the end
            h.wfile.write(data)
            i, n = 0, len(data)
            while i < n:
                if need:  # inside a chunk (its bytes and the CRLF after them)
                    take = min(need, n - i)
                    i += take
                    need -= take
                    continue
                j = data.find(b"\n", i)
                if j < 0:
                    line += data[i:]
                    if len(line) > 4096:
                        raise ValueError("bad chunk line")
                    break
                text, line, i = (line + data[i:j]).strip(), b"", j + 1
                if trailer:
                    if not text:
                        return True  # the empty line after the last chunk (and its trailers): done
                    continue
                size = int(text.split(b";", 1)[0], 16)
                if size:
                    need = size + 2
                else:
                    trailer = True

    def read_body(self, h):
        if h.headers.get("Transfer-Encoding", "").lower() == "chunked":
            parts, size = [], 0
            while True:
                length = int(h.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if length == 0:
                    while h.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass  # trailers
                    break
                size += length
                if size > MAX_BODY:
                    raise ValueError("Request too large")
                parts.append(h.rfile.read(length))
                h.rfile.readline()
            return b"".join(parts)
        length = int(h.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("Request too large")
        return h.rfile.read(length) if length else b""

    def reply(self, h, status, data):
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
