"""Tests for tui/keyproxy.py, the local proxy that keeps the API key off the remote host.

Run: python3 -m unittest tests.test_keyproxy   (or discover -s tests)
"""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fakeapi import FakeAPI, reply  # noqa: E402
from test_hoplon import Tmp, bless_path, make_home  # noqa: E402

PROXY = os.path.join(os.path.dirname(HERE), "tui", "keyproxy.py")
TOKEN = "ab" * 24
KEY = "sk-very-secret-key-123"


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=20):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class ProxyCase(Tmp):
    """A blessed endpoint on a FakeAPI, and a keyproxy in front of it."""

    turns = [reply("hello from the fake model")]
    api_args = {}

    def setUp(self):
        super().setUp()
        self.api = FakeAPI(list(self.turns), **self.api_args)
        self.api.__enter__()
        self.addCleanup(self.api.__exit__)
        self.home, self.epath = make_home(self.tmp, self.api.url, key_env="KP_TEST_KEY",
                                          headers={"X-Team": "blue"})
        bless_path(self.home, self.epath)
        self.sockdir = self.path("run")
        os.mkdir(self.sockdir, 0o700)
        self.sock = os.path.join(self.sockdir, "key.sock")
        self.proc = None

    def env(self):
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(self.home), KP_TEST_KEY=KEY)
        return env

    def spawn(self, args=None, stdin=TOKEN + "\n", env=None):
        self.proc = subprocess.Popen([sys.executable, PROXY] + (args or ["test", self.sock]), env=env or self.env(),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop)
        self.proc.stdin.write(stdin.encode())
        self.proc.stdin.flush()
        return self.proc

    def start(self):
        self.spawn()
        line = self.proc.stdout.readline().decode().strip()
        self.assertEqual(line, "ready", self.proc.poll())

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            p.kill()
        if p:
            p.wait()
            for f in (p.stdin, p.stdout, p.stderr):
                try:
                    f.close()
                except OSError:
                    pass

    def finish(self):
        """Close stdin, wait, and return (exit code, stdout, stderr)."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        rc = self.proc.wait(timeout=15)
        return rc, self.proc.stdout.read().decode(), self.proc.stderr.read().decode()

    def request(self, method, path, body=None, headers=None, token=TOKEN, raw=None):
        c = UnixConnection(self.sock)
        h = dict(headers or {})
        if token is not None:
            h["X-Hoplon-Token"] = token
        if raw is None and body is not None:
            raw = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=raw, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, r.getheader("Content-Type"), data


class TestForwarding(ProxyCase):
    def test_models_adds_key_and_drops_token(self):
        self.start()
        status, _, data = self.request("GET", "/v1/models",
                                       headers={"Authorization": "Bearer attacker", "Cookie": "a=b"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["data"][0]["id"], "fake-model")
        up = self.api.requests[-1]
        self.assertEqual(up["path"], "/v1/models")
        h = {k.lower(): v for k, v in up["headers"].items()}
        self.assertEqual(h["authorization"], f"Bearer {KEY}")
        self.assertEqual(h["x-team"], "blue")
        self.assertNotIn("x-hoplon-token", h)
        self.assertNotIn("cookie", h)
        self.assertNotIn(TOKEN, json.dumps(up))

    def test_chat_streams_sse(self):
        self.start()
        status, ctype, data = self.request("POST", "/v1/chat/completions",
                                           {"model": "fake-model", "stream": True, "messages": []})
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/event-stream")
        events = [json.loads(ln[6:]) for ln in data.decode().splitlines() if ln.startswith("data: {")]
        text = "".join(e["choices"][0]["delta"]["content"] for e in events)
        self.assertEqual(text, "hello from the fake model")
        self.assertTrue(data.endswith(b"data: [DONE]\n\n"))
        up = self.api.requests[-1]
        self.assertEqual(up["body"]["messages"], [])
        h = {k.lower(): v for k, v in up["headers"].items()}
        self.assertEqual(h["authorization"], f"Bearer {KEY}")
        self.assertNotIn("x-hoplon-token", h)

    def test_non_streamed_chat(self):
        self.start()
        status, ctype, data = self.request("POST", "/v1/chat/completions", {"model": "m", "messages": []})
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(json.loads(data)["choices"][0]["message"]["content"], "hello from the fake model")

    def test_audit_logged_without_secrets(self):
        self.start()
        self.request("GET", "/v1/models")
        self.finish()
        with open(os.path.join(self.home, "audit.log")) as f:
            text = f.read()
        recs = [json.loads(ln) for ln in text.splitlines()]
        rec = [r for r in recs if r["event"] == "proxy_request"][-1]
        self.assertEqual((rec["method"], rec["path"], rec["status"]), ("GET", "/v1/models", 200))
        self.assertGreater(rec["bytes"], 0)
        self.assertNotIn(KEY, text)
        self.assertNotIn(TOKEN, text)


class TestStreaming(Tmp):
    """Chunks must reach the client while the upstream is still sending, not after it finishes."""

    def test_incremental(self):
        import http.server
        import threading

        gate = threading.Event()

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: first\n\n")
                self.wfile.flush()
                gate.wait(15)
                self.wfile.write(b"data: second\n\n")

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        home, path = make_home(self.tmp, f"http://127.0.0.1:{srv.server_address[1]}/v1")
        bless_path(home, path)
        sockdir = self.path("run")
        os.mkdir(sockdir, 0o700)
        sock = os.path.join(sockdir, "k.sock")
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home))
        p = subprocess.Popen([sys.executable, PROXY, "test", sock], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        p.stdin.write((TOKEN + "\n").encode())
        p.stdin.flush()
        self.assertEqual(p.stdout.readline().strip(), b"ready")
        c = UnixConnection(sock)
        c.request("POST", "/v1/chat/completions", body=b"{}", headers={"X-Hoplon-Token": TOKEN})
        r = c.getresponse()
        self.assertEqual(r.fp.readline(), b"data: first\n")     # arrives while upstream is still open
        gate.set()
        self.assertIn(b"data: second", r.read())
        c.close()
        p.stdin.close()


class TestRejections(ProxyCase):
    def test_token(self):
        self.start()
        for tok in (None, "", "0" * 48, TOKEN[:-1]):
            status, _, _ = self.request("GET", "/v1/models", token=tok)
            self.assertEqual(status, 401, tok)
        status, _, _ = self.request("GET", "/nope", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(self.api.requests, [])

    def test_other_paths_and_methods(self):
        self.start()
        cases = [("GET", "/"), ("GET", "/v1"), ("GET", "/models"), ("GET", "/v1/chat/completions"),
                 ("POST", "/v1/models"), ("POST", "/v1/completions"), ("GET", "/v1/models/"),
                 ("GET", "/v1/../v1/models"), ("DELETE", "/v1/models"), ("PUT", "/v1/chat/completions"),
                 ("GET", "/v1/files"), ("GET", "http://evil.example/v1/models")]
        for method, path in cases:
            status, _, data = self.request(method, path, raw=b"{}" if method in ("POST", "PUT") else None)
            self.assertEqual(status, 404, (method, path))
            self.assertIn("error", json.loads(data))
        self.assertEqual(self.api.requests, [])

    def test_query_string_refused(self):
        self.start()
        self.assertEqual(self.request("GET", "/v1/models?x=1")[0], 404)
        self.assertEqual(self.request("POST", "/v1/chat/completions?x=1", {})[0], 404)
        self.assertEqual(self.api.requests, [])

    def test_oversized_body(self):
        self.start()
        c = UnixConnection(self.sock)
        c.putrequest("POST", "/v1/chat/completions")
        c.putheader("X-Hoplon-Token", TOKEN)
        c.putheader("Content-Length", str(16 * 1024 * 1024 + 1))
        c.endheaders()
        self.assertEqual(c.getresponse().status, 413)
        c.close()
        self.assertEqual(self.api.requests, [])

    def test_bad_bodies(self):
        self.start()
        for raw in (b"not json", b"[1,2]", b'"s"', b"", b"{" * 100000):
            self.assertEqual(self.request("POST", "/v1/chat/completions", raw=raw)[0], 400, raw[:20])
        c = UnixConnection(self.sock)
        c.putrequest("POST", "/v1/chat/completions")
        c.putheader("X-Hoplon-Token", TOKEN)
        c.endheaders()
        self.assertEqual(c.getresponse().status, 411)
        c.close()
        self.assertEqual(self.api.requests, [])


class TestUpstreamErrors(ProxyCase):
    api_args = {"status": 500, "error_message": "boom"}

    def test_500_passed_through(self):
        self.start()
        status, ctype, data = self.request("POST", "/v1/chat/completions", {"messages": []})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(data)["error"]["message"], "boom")


class TestUnreachable(ProxyCase):
    def test_502_without_secrets(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                               # nothing listens on this port now
        url = f"http://127.0.0.1:{port}/v1"
        with open(self.epath) as f:
            data = json.load(f)
        data["base_url"] = url
        with open(self.epath, "w") as f:
            json.dump(data, f)
        bless_path(self.home, self.epath)
        self.start()
        status, _, body = self.request("GET", "/v1/models")
        self.assertEqual(status, 502)
        for secret in (KEY, TOKEN, str(port)):
            self.assertNotIn(secret.encode(), body)


class TestStartup(ProxyCase):
    def test_socket_mode_and_cleanup_on_eof(self):
        self.start()
        self.assertEqual(os.stat(self.sock).st_mode & 0o777, 0o600)
        rc, out, err = self.finish()
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.sock))
        for secret in (KEY, TOKEN):
            self.assertNotIn(secret, out + err)

    def test_sigterm_stops_and_cleans(self):
        self.start()
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=15), 0)
        self.assertFalse(os.path.exists(self.sock))

    def test_unblessed_refused(self):
        with open(self.epath, "a") as f:
            f.write("\n")                                       # changed since it was blessed
        self.spawn()
        rc, out, err = self.finish()
        self.assertEqual(rc, 3)
        self.assertEqual(out, "")
        self.assertIn("blessed", err)
        self.assertNotIn(KEY, err)
        self.assertFalse(os.path.exists(self.sock))

    def test_unknown_endpoint(self):
        self.spawn(["nosuch", self.sock])
        self.assertEqual(self.finish()[0], 3)

    def test_missing_key_env(self):
        env = self.env()
        del env["KP_TEST_KEY"]
        self.spawn(env=env)
        self.assertEqual(self.finish()[0], 3)

    def test_token_in_argv_not_accepted(self):
        for args in (["--token", TOKEN, "test", self.sock], ["test", self.sock, TOKEN]):
            self.spawn(args)
            rc, out, err = self.finish()
            self.assertEqual(rc, 2, args)
            self.assertIn("usage", err)
            self.assertFalse(os.path.exists(self.sock))

    def test_bad_tokens(self):
        for tok in ("", "short", "z" * 40, "a" * 129):
            self.spawn(stdin=tok + "\n")
            rc, out, err = self.finish()
            self.assertEqual(rc, 2, tok)
            self.assertEqual(out, "")

    def test_existing_socket_refused(self):
        with open(self.sock, "w") as f:
            f.write("x")
        self.spawn()
        rc, _, err = self.finish()
        self.assertEqual(rc, 2)
        self.assertIn("already exists", err)
        with open(self.sock) as f:
            self.assertEqual(f.read(), "x")                    # left alone

    def test_insecure_directory_refused(self):
        for mode in (0o750, 0o705, 0o770):
            os.chmod(self.sockdir, mode)
            self.spawn()
            rc, _, err = self.finish()
            self.assertEqual(rc, 2, oct(mode))
            self.assertIn("chmod 700", err)
            self.assertFalse(os.path.exists(self.sock))

    def test_too_many_concurrent_requests(self):
        # Hold 8 connections open mid-request (headers promise a body that never comes); the 9th is refused.
        self.start()
        held = []
        for _ in range(8):
            c = UnixConnection(self.sock)
            c.putrequest("POST", "/v1/chat/completions")
            c.putheader("X-Hoplon-Token", TOKEN)
            c.putheader("Content-Length", "10")
            c.endheaders()
            held.append(c)
        time.sleep(1)
        status = self.request("GET", "/v1/models")[0]
        for c in held:
            c.close()
        self.assertEqual(status, 503)


if __name__ == "__main__":
    unittest.main()
