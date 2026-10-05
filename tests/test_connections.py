"""Kept-open connections for the usage checks (account_switcher/connections.py)."""
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from account_switcher import connections  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like the real APIs

    def log_message(self, *args):
        pass

    def answer(self, status, payload, close=False):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        if close:
            self.close_connection = True

    def do_GET(self):
        self.server.seen.append((self.command, self.path, self.client_address[1], self.headers.get("Authorization")))
        if self.path == "/limited":
            self.answer(429, {"error": "slow down"})
        elif self.path == "/close":
            self.answer(200, {"ok": True}, close=True)
        elif self.path == "/drop":  # says nothing, then closes: as a server drops an idle connection
            self.answer(200, {"path": self.path})
            self.close_connection = True
        else:
            self.answer(200, {"path": self.path})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.seen.append((self.command, self.path, self.client_address[1], self.rfile.read(length)))
        self.answer(200, {"posted": True})


class Server:
    def __init__(self, context=None):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.seen = []
        self.httpd.daemon_threads = True
        if context is not None:
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def get(url, headers=None):
    with connections.urlopen(Request(url, headers=headers or {}), timeout=5) as response:
        return response.status, json.loads(response.read())


class ConnectionsTest(unittest.TestCase):
    def setUp(self):
        connections.close_all()
        connections._sessions.clear()
        self.env = mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"})
        self.env.start()
        self.server = Server()
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        connections.close_all()
        self.server.close()
        self.env.stop()

    def test_checks_one_after_another_share_one_connection(self):
        for i in range(3):
            self.assertEqual(get(f"{self.base}/usage?n={i}", {"Authorization": "Bearer x"}), (200, {"path": f"/usage?n={i}"}))
        ports = {port for _, _, port, _ in self.server.httpd.seen}
        self.assertEqual(len(ports), 1, "every check went over the same connection")
        self.assertEqual(self.server.httpd.seen[0][3], "Bearer x")

    def test_an_error_status_raises_httperror_with_its_body(self):
        with self.assertRaises(HTTPError) as caught:
            get(f"{self.base}/limited")
        self.assertEqual(caught.exception.code, 429)
        self.assertIn(b"slow down", caught.exception.read())
        get(f"{self.base}/after")  # the connection is still good after an error status
        self.assertEqual(len({port for _, _, port, _ in self.server.httpd.seen}), 1)

    def test_a_connection_the_server_closed_is_replaced_without_failing_the_check(self):
        get(f"{self.base}/drop")
        import time
        time.sleep(0.2)  # the server has closed its side by now
        self.assertEqual(get(f"{self.base}/second"), (200, {"path": "/second"}))
        ports = [port for _, _, port, _ in self.server.httpd.seen]
        self.assertEqual(len(ports), 2, "the request reached the server once")
        self.assertNotEqual(ports[0], ports[1])

    def test_a_response_that_closes_the_connection_is_not_kept(self):
        get(f"{self.base}/close")
        get(f"{self.base}/next")
        ports = [port for _, _, port, _ in self.server.httpd.seen]
        self.assertNotEqual(ports[0], ports[1])

    def test_a_post_goes_on_a_new_connection_and_is_never_kept(self):
        """Renewing a login spends a single-use refresh token: never on a connection that may be stale."""
        get(f"{self.base}/usage")
        request = Request(f"{self.base}/token", data=b'{"refresh_token":"r"}', method="POST",
                          headers={"Content-Type": "application/json"})
        with connections.urlopen(request, timeout=5) as response:
            self.assertEqual(json.loads(response.read()), {"posted": True})
        get(f"{self.base}/usage")
        ports = [port for _, _, port, _ in self.server.httpd.seen]
        self.assertNotEqual(ports[1], ports[0])
        self.assertEqual(ports[2], ports[0], "the GET connection was kept through the POST")
        self.assertEqual(self.server.httpd.seen[1][3], b'{"refresh_token":"r"}')

    def test_an_unreachable_host_is_a_urlerror(self):
        self.server.close()
        with self.assertRaises(URLError):
            get(f"{self.base}/usage")

    def test_a_connection_idle_too_long_is_not_used(self):
        get(f"{self.base}/a")
        with mock.patch.object(connections, "IDLE_LIMIT", -1):
            get(f"{self.base}/b")
        ports = [port for _, _, port, _ in self.server.httpd.seen]
        self.assertNotEqual(ports[0], ports[1])

    def test_the_providers_send_their_requests_this_way(self):
        from account_switcher import providers
        self.assertIs(providers.urlopen, connections.urlopen)
        status, body = providers._http("GET", f"{self.base}/usage", {"Accept": "application/json"})
        self.assertEqual((status, body), (200, {"path": "/usage"}))
        with self.assertRaises(providers.ProviderError) as caught:
            providers._http("GET", f"{self.base}/limited", {})
        self.assertTrue(caught.exception.rate_limited)


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
class HttpsTest(unittest.TestCase):
    """Over TLS: the connection is kept, and a new one resumes the session (no certificate again)."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp()
        cls.cert, cls.key = os.path.join(cls.folder, "cert.pem"), os.path.join(cls.folder, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost", "-keyout", cls.key, "-out", cls.cert],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.folder, ignore_errors=True)

    def setUp(self):
        connections.close_all()
        connections._sessions.clear()
        self.env = mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"})
        self.env.start()
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(self.cert, self.key)
        self.server = Server(server_context)
        self.client = ssl.create_default_context(cafile=self.cert)
        self.base = f"https://localhost:{self.server.port}"

    def tearDown(self):
        connections.close_all()
        self.server.close()
        self.env.stop()

    def fetch(self, path):
        with connections.urlopen(Request(self.base + path), timeout=5, context=self.client) as response:
            return json.loads(response.read())

    def test_kept_open_and_resumed(self):
        before = dict(connections.stats)
        self.assertEqual(self.fetch("/one"), {"path": "/one"})
        self.assertEqual(self.fetch("/two"), {"path": "/two"})
        self.assertEqual(len({port for _, _, port, _ in self.server.httpd.seen}), 1)
        self.assertEqual(connections.stats["reused"] - before["reused"], 1)
        connections.close_all()  # as if the server had dropped it while idle
        self.assertEqual(self.fetch("/three"), {"path": "/three"})
        self.assertEqual(connections.stats["resumed"] - before["resumed"], 1, "the new connection resumed the TLS session")

    def test_an_untrusted_certificate_still_fails(self):
        with self.assertRaises(URLError) as caught:
            with connections.urlopen(Request(self.base + "/x"), timeout=5, context=ssl.create_default_context()):
                pass
        self.assertIsInstance(caught.exception.reason, ssl.SSLCertVerificationError)


if __name__ == "__main__":
    unittest.main()
