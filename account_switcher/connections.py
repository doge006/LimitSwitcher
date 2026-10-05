"""Kept-open HTTPS connections for the usage checks (a development note: urlopen opened a new
connection, with a full TLS handshake, for every check).

A usage check is a request of about 1 KB and an answer of about 1 KB, but a new TLS connection
also carries the server's certificate chain and the key exchange, several KB more. The checks
come every minute or so per account, so most of what the app sent and received was handshakes.
Here each host keeps one connection open between checks, and when the server has closed it in
the meantime, the next connection resumes the TLS session (no certificate chain again).

Behaves like urllib's urlopen for what the providers use: a response with .status and .read()
that works as a context manager, HTTPError for an error status, URLError when the host can't be
reached. The system's proxy settings are followed (CONNECT through an HTTPS proxy).
A POST (renewing a login, whose refresh token is single-use) always goes on a new connection and
is never sent twice. Standard library only.
"""
import http.client
import io
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

from . import tls

IDLE_LIMIT = 240   # seconds: a connection unused this long is closed rather than tried (servers drop them sooner or later)
# A reused connection that fails like this was closed by the server while idle: the request never
# reached it, so it is sent again on a new connection.
STALE = (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError, ConnectionAbortedError,
         BrokenPipeError, ssl.SSLEOFError, ssl.SSLZeroReturnError)

_lock = threading.Lock()
_idle = {}      # (scheme, host, port, proxy) -> (connection, when it was last used)
_sessions = {}  # (host, port) -> the last TLS session with it, to resume
stats = {"opened": 0, "reused": 0, "resumed": 0}  # for tests and app.log


class _Https(http.client.HTTPSConnection):
    """An HTTPS connection that resumes the host's last TLS session when it can."""

    def connect(self):
        http.client.HTTPConnection.connect(self)  # TCP (and the proxy tunnel, if any)
        key = self._key()
        session = _sessions.get(key)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=key[0], session=session)
        except (ssl.SSLError, ValueError):
            if session is None:
                raise
            _sessions.pop(key, None)  # the server no longer knows it: a full handshake
            http.client.HTTPConnection.connect(self)
            self.sock = self._context.wrap_socket(self.sock, server_hostname=key[0])
        if self.sock.session_reused:
            stats["resumed"] += 1

    def _key(self):
        """The server's (host, port), not the proxy's when tunnelling."""
        return (self._tunnel_host, self._tunnel_port or 443) if self._tunnel_host else (self.host, self.port)

    def remember_session(self, sock):
        """After a response has been read on `sock` (TLS 1.3 sends its session ticket after the
        handshake; http.client lets go of the socket of a response that closes the connection)."""
        session = getattr(sock, "session", None) if sock is not None else None
        if session is not None:
            _sessions[self._key()] = session


class Response:
    """What the caller reads: status, headers and the whole body (already read)."""

    def __init__(self, status, reason, headers, body, url):
        self.status, self.reason, self.headers, self._body, self.url = status, reason, headers, body, url

    def read(self, amount=None):
        return self._body if amount is None else self._body[:amount]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _proxy_for(scheme, host):
    """(proxy host, proxy port, Proxy-Authorization or None) from the system settings, or None."""
    try:
        if proxy_bypass(host):
            return None
        proxy = getproxies().get(scheme)
    except Exception:
        return None
    if not proxy:
        return None
    parts = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if not parts.hostname:
        return None
    auth = None
    if parts.username is not None:
        from base64 import b64encode
        from urllib.parse import unquote
        pair = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        auth = "Basic " + b64encode(pair.encode()).decode("ascii")
    return parts.hostname, parts.port or 8080, auth


def _new(scheme, host, port, proxy, timeout, context):
    stats["opened"] += 1
    if proxy:
        if scheme == "https":
            connection = _Https(proxy[0], proxy[1], timeout=timeout, context=context or tls.context())
            connection.set_tunnel(host, port, headers={"Proxy-Authorization": proxy[2]} if proxy[2] else None)
        else:
            connection = http.client.HTTPConnection(proxy[0], proxy[1], timeout=timeout)
        return connection
    if scheme == "https":
        return _Https(host, port, timeout=timeout, context=context or tls.context())
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _take(key):
    with _lock:
        entry = _idle.pop(key, None)
    if entry is None:
        return None
    connection, used = entry
    if time.monotonic() - used > IDLE_LIMIT:
        connection.close()
        return None
    return connection


def _keep(key, connection):
    with _lock:
        previous = _idle.get(key)
        _idle[key] = (connection, time.monotonic())
    if previous is not None and previous[0] is not connection:
        previous[0].close()


def close_all():
    with _lock:
        entries = list(_idle.values())
        _idle.clear()
    for connection, _ in entries:
        connection.close()


def urlopen(request, timeout=None, context=None):
    """Send a urllib Request; returns a Response (see the module notes)."""
    url = request.full_url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise URLError(f"unsupported URL: {url}")
    host, port = parts.hostname, parts.port or (443 if scheme == "https" else 80)
    proxy = _proxy_for(scheme, host)
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    method = request.get_method()
    body = request.data
    headers = {k: v for k, v in request.header_items()}
    if proxy and scheme == "http":  # a plain proxy takes the whole URL
        target = url
        headers.setdefault("Host", parts.netloc)
        if proxy[2]:
            headers["Proxy-Authorization"] = proxy[2]
    headers.setdefault("Accept-Encoding", "identity")
    headers.setdefault("User-Agent", "Python-urllib")
    key = (scheme, host, port, proxy)
    reusable = method == "GET"
    connection = _take(key) if reusable else None
    reused = connection is not None
    while True:
        if connection is None:
            connection = _new(scheme, host, port, proxy, timeout, context)
        elif timeout is not None:
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
        try:
            connection.request(method, target, body=body, headers=headers)
            sock = connection.sock
            response = connection.getresponse()
            data = response.read()
        except STALE as error:
            connection.close()
            if reused:  # idle too long on the server's side: once more on a new connection
                connection, reused = None, False
                continue
            raise URLError(error)
        except (OSError, http.client.HTTPException) as error:  # unreachable, timed out, a bad certificate
            connection.close()
            raise URLError(error)
        break
    if reused:
        stats["reused"] += 1
    if isinstance(connection, _Https):
        connection.remember_session(sock)
    if reusable and not response.will_close:
        _keep(key, connection)
    else:
        connection.close()
    if response.status >= 400:
        raise HTTPError(url, response.status, response.reason, response.headers, io.BytesIO(data))
    return Response(response.status, response.reason, response.headers, data, url)
