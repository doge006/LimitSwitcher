"""The Claude Code router (claude_router.py): a window's requests go on to Anthropic unchanged but
for the login, and its answers come back as they arrive."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
import threading
import time
import unittest

os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
from account_switcher import claude_router
from account_switcher.claude_router import ClaudeRouter


class Upstream:
    """A fake Anthropic: records what it gets; answers per path."""

    def __init__(self):
        self.seen = []
        self.connections = set()
        self.refuse = set()  # tokens answered with 401
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                upstream.connections.add(self.client_address)
                upstream.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
                token = (self.headers.get("Authorization") or "")[7:]
                if token in upstream.refuse:
                    raw = b'{"type":"error","error":{"type":"authentication_error","message":"expired"}}'
                    self.send_response(401)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    return self.wfile.write(raw)
                if self.path.startswith("/v1/messages"):  # an event stream, in pieces, without a length
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("anthropic-ratelimit-unified-5h-utilization", "0.25")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for n in range(3):
                        event = f"event: delta\ndata: {json.dumps({'n': n, 'token': token})}\n\n".encode()
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                        self.wfile.flush()
                        time.sleep(0.05)
                    self.wfile.write(b"0\r\n\r\n")
                    return
                raw = json.dumps({"token": token, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Windows:
    """The app's side, as the router asks it."""

    def __init__(self):
        self.accounts = {"w1": "acct-b", "w2": None}
        self.upstreams = {}
        self.tokens = {"acct-b": "at-b"}
        self.renewed = []
        self.failed = []

    def window_route(self, window_id):
        return self.accounts.get(window_id), self.upstreams.get(window_id)

    def window_token(self, account_id, renew):
        if renew:
            self.renewed.append(account_id)
            self.tokens[account_id] += "+"
        if self.tokens[account_id] is None:
            raise RuntimeError("Login expired; sign in again")
        return self.tokens[account_id]

    def window_login_failed(self, window_id, account_id, error):
        self.failed.append((window_id, account_id, str(error)))


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.windows = Windows()
        self.router = ClaudeRouter(self.windows, port=0, upstream=self.upstream.base)
        self.router.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=10)

    def tearDown(self):
        self.connection.close()
        self.router.close()
        self.upstream.close()

    def post(self, window, path, token="at-main", body=b'{"messages":[]}', headers=None):
        prefix = f"/{self.router.secret}/{window}"
        sent = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20"}
        sent.update(headers or {})
        if token is None:
            del sent["Authorization"]
        self.connection.request("POST", prefix + path, body=body, headers=sent)
        response = self.connection.getresponse()
        return response, response.read()

    def test_a_window_on_its_own_account_goes_with_that_login(self):
        response, body = self.post("w1", "/v1/count_tokens")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body), {"token": "at-b", "path": "/v1/count_tokens"})
        seen = self.upstream.seen[-1]["headers"]
        self.assertEqual(seen["anthropic-beta"], "oauth-2025-04-20")  # everything else as it came
        self.assertEqual(self.upstream.seen[-1]["body"], b'{"messages":[]}')
        self.assertEqual([k for k in seen if k.lower() == "authorization"], ["Authorization"])  # one, never two

    def test_a_window_on_the_main_account_sends_its_own_login(self):
        _, body = self.post("w2", "/v1/count_tokens")
        self.assertEqual(json.loads(body)["token"], "at-main")
        _, body = self.post("unknown", "/v1/count_tokens")  # a window the app doesn't know (yet): the same
        self.assertEqual(json.loads(body)["token"], "at-main")

    def test_an_api_key_goes_as_it_came(self):
        response, body = self.post("w1", "/v1/count_tokens", token=None, headers={"x-api-key": "sk-ant-api"})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.upstream.seen[-1]["headers"].get("x-api-key"), "sk-ant-api")
        self.assertEqual(json.loads(body)["token"], "")

    def test_an_event_stream_comes_through_piece_by_piece(self):
        self.connection.request("POST", f"/{self.router.secret}/w1/v1/messages?beta=true", body=b"{}",
                                headers={"Authorization": "Bearer at-main"})
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
        self.assertEqual(response.getheader("anthropic-ratelimit-unified-5h-utilization"), "0.25")
        first = response.read1(65536)
        self.assertIn(b'"n": 0', first)  # the first event arrives before the last is sent
        self.assertNotIn(b'"n": 2', first)
        rest = response.read()
        self.assertIn(b'"n": 2', rest)
        self.assertIn(b'"token": "at-b"', first)
        _, body = self.post("w1", "/v1/count_tokens")  # the same connection, again
        self.assertEqual(json.loads(body)["token"], "at-b")

    def test_connections_to_anthropic_are_kept_and_reused(self):
        for _ in range(5):
            self.post("w1", "/v1/count_tokens")
            self.post("w2", "/v1/count_tokens")
        self.assertEqual(len(self.upstream.connections), 1)
        for _ in range(2):  # a stream ends cleanly too, and its connection is reused
            self.connection.request("POST", f"/{self.router.secret}/w1/v1/messages", body=b"{}",
                                    headers={"Authorization": "Bearer at-main"})
            self.connection.getresponse().read()
        self.assertEqual(len(self.upstream.connections), 1)

    def test_a_refused_login_is_renewed_once_and_the_request_sent_again(self):
        self.upstream.refuse.add("at-b")
        response, body = self.post("w1", "/v1/count_tokens")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["token"], "at-b+")
        self.assertEqual(self.windows.renewed, ["acct-b"])
        self.assertEqual(self.upstream.seen[-1]["body"], b'{"messages":[]}')  # the same request

    def test_a_login_that_cant_be_used_goes_with_the_sessions_own(self):
        self.windows.tokens["acct-b"] = None
        _, body = self.post("w1", "/v1/count_tokens")
        self.assertEqual(json.loads(body)["token"], "at-main")
        self.assertEqual(self.windows.failed[0][:2], ("w1", "acct-b"))

    def test_a_window_with_its_own_base_url_goes_there(self):
        other = Upstream()
        try:
            self.windows.upstreams["w2"] = other.base + "/proxy/prefix"
            _, body = self.post("w2", "/v1/count_tokens?x=1")
            self.assertEqual(json.loads(body)["path"], "/proxy/prefix/v1/count_tokens?x=1")
            self.assertEqual(other.seen[-1]["headers"]["Host"], other.base[len("http://"):])
        finally:
            other.close()

    def test_other_programs_without_the_secret_get_nothing(self):
        self.connection.request("POST", "/wrong/w1/v1/messages", body=b"{}", headers={"Authorization": "Bearer at-main"})
        response = self.connection.getresponse()
        response.read()
        self.assertEqual(response.status, 403)
        self.assertEqual(self.upstream.seen, [])

    def test_anthropic_unreachable_is_an_error_claude_code_retries(self):
        self.router.upstream = "http://127.0.0.1:9"
        response, body = self.post("w2", "/v1/messages")
        self.assertEqual(response.status, 502)
        self.assertEqual(json.loads(body)["type"], "error")

    def test_an_idle_connection_from_claude_code_lets_its_thread_go(self):
        self.post("w1", "/v1/count_tokens")
        before = threading.active_count()
        with unittest.mock.patch.object(self.router.server.RequestHandlerClass, "timeout", 0.3):
            connection = http.client.HTTPConnection("127.0.0.1", self.router.port, timeout=10)
            connection.request("POST", f"/{self.router.secret}/w2/v1/count_tokens", body=b"{}",
                               headers={"Authorization": "Bearer at-main"})
            connection.getresponse().read()
            self.assertEqual(threading.active_count(), before + 1)  # its thread, waiting for the next request
            time.sleep(0.8)
            self.assertEqual(threading.active_count(), before)  # idle: closed, the thread gone
            connection.close()


if __name__ == "__main__":
    unittest.main()
