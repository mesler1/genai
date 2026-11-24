#!/usr/bin/env python3
"""keyproxy: lets a remote genai use your endpoint without the API key ever leaving this machine.

genai-tui's remote mode runs genai on an internal host over ssh. The endpoint's key must not go there, so
genai-tui starts this program locally. It listens on a Unix socket in genai-tui's private (0700) state
directory, and `ssh -R 127.0.0.1:0:SOCKET` forwards a port on the remote host to it. The remote genai sends
plain-HTTP model requests to http://127.0.0.1:PORT/v1; this program checks the token, adds the key and the
endpoint's own headers, and forwards the request over the endpoint's normal TLS and proxy settings (the same
opener genai itself builds), streaming the reply back as it arrives.

    keyproxy.py ENDPOINT SOCKET        the token is the first line of stdin (32-128 hex characters)

ENDPOINT is resolved and blessing-checked exactly like `genai -e ENDPOINT`: an unblessed or changed endpoint
is refused (exit 3). After one line `ready` on stdout it serves until stdin closes or SIGTERM, then removes
the socket and exits 0. The token comes on stdin so it never shows in `ps`.

Allowed, and nothing else (anything else is 404 and is never forwarded):
    GET  /v1/models            ->  BASE_URL/models
    POST /v1/chat/completions  ->  BASE_URL/chat/completions    (JSON object body, at most 16 MiB)
Every request needs the right X-Genai-Token (401 otherwise). Of the client's headers only Content-Type and
Accept are forwarded; Authorization, Cookie, Proxy-*, Host and the token itself are dropped.

What a root user on the remote host can do: use the token while the tunnel is up, so send chat and model-list
requests to your endpoint and spend your quota, through this fixed allowlist. What they cannot do: read the
key (it is only in this process), reach any other URL or path on the endpoint, change its headers, or use the
proxy once genai-tui exits. The reply size is capped like genai's own.

Requests are logged to genai's local audit log as `proxy_request` (method, path, status, bytes, ms); the key
and token are never logged. Standard library only.
"""

import hmac
import http.server
import importlib.machinery
import importlib.util
import json
import os
import re
import signal
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request

MAX_BODY = 16 * 1024 * 1024
MAX_ERROR = 64 * 1024
MAX_CONCURRENT = 8
CHUNK = 16 * 1024
TOKEN_RE = re.compile(r"[0-9a-fA-F]{32,128}")
ROUTES = {("GET", "/v1/models"): "/models", ("POST", "/v1/chat/completions"): "/chat/completions"}
EXIT_OK, EXIT_USAGE, EXIT_PROFILE = 0, 2, 3


def load_genai():
    """Import the genai script (no .py suffix) that sits at the repository root."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "genai")
    loader = importlib.machinery.SourceFileLoader("genai_mod", path)
    spec = importlib.util.spec_from_loader("genai_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Failure(Exception):
    def __init__(self, message, code=EXIT_USAGE):
        super().__init__(message)
        self.code = code


class Proxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, path, genai, client, token, audit):
        self.genai, self.client, self.token, self.audit = genai, client, token.encode(), audit
        self.base = client.p.base_url
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        super().__init__(path, Handler)

    def handle_error(self, request, client_address):
        pass                                   # a client that vanished is not news, and details could leak


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"              # the reply ends when the connection closes: nothing to buffer
    timeout = 30                               # seconds a client may take to send its request
    server_version = "keyproxy"

    def log_message(self, *a):
        pass

    def address_string(self):
        return "local"

    def setup(self):
        super().setup()
        self.bytes_out = 0
        self.status = 0

    # ---- replies

    def fail(self, code, message):
        data = json.dumps({"error": {"message": message}}).encode()
        self.status = code
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            pass
        self.bytes_out = len(data)

    # ---- every method lands here

    def do_any(self):
        self.serve(self.command)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_any

    def serve(self, method):
        started = time.time()
        path = self.path
        try:
            self.handle_request(method, path)
        finally:
            ms = int(1000 * (time.time() - started))
            self.server.audit.log("proxy_request", method=method, path=path[:200], status=self.status,
                                  bytes=self.bytes_out, ms=ms)

    def handle_request(self, method, path):
        got = self.headers.get("X-Genai-Token", "")
        if not hmac.compare_digest(got.encode("utf-8", "replace"), self.server.token):
            return self.fail(401, "missing or wrong token")
        target = ROUTES.get((method, path))
        if target is None:
            return self.fail(404, "not found")
        if not self.server.slots.acquire(blocking=False):
            return self.fail(503, "too many requests at once")
        try:
            body = self.read_body() if method == "POST" else None
            if body is not False:
                self.forward(method, target, body)
        finally:
            self.server.slots.release()

    def read_body(self):
        """The request body, or False after replying with an error."""
        raw = self.headers.get("Content-Length", "")
        if not raw.isascii() or not raw.isdigit():
            return self.fail(411, "Content-Length is required") or False
        n = int(raw)
        if n > MAX_BODY:
            return self.fail(413, "request body too large") or False
        data = self.rfile.read(n)
        if len(data) != n:
            return self.fail(400, "short request body") or False
        try:
            ok = isinstance(json.loads(data), dict)
        except (ValueError, RecursionError):
            ok = False
        if not ok:
            return self.fail(400, "the body must be a JSON object") or False
        return data

    def forward(self, method, target, body):
        srv = self.server
        g = srv.genai
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "User-Agent": f"genai/{g.VERSION}"}
        for name in ("Content-Type", "Accept"):
            v = self.headers.get(name)
            if v and v.isprintable() and len(v) <= 200:
                headers[name] = v
        headers.update(srv.client.p.headers)
        key = srv.client._key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(srv.base + target, data=body, headers=headers, method=method)
        try:
            r = srv.client.opener.open(req, timeout=srv.client.p.timeout)
        except urllib.error.HTTPError as e:
            data = e.read(MAX_ERROR)
            return self.relay(e.code, e.headers.get("Content-Type"), [data])
        except g.GenaiError:
            return self.fail(502, "the endpoint answered with a redirect, which is not followed")
        except (urllib.error.URLError, OSError, ValueError):
            return self.fail(502, "cannot reach the endpoint")
        with r:
            return self.relay(r.status, r.headers.get("Content-Type"), self.chunks(r))

    def chunks(self, r):
        """The upstream body as it arrives, cut off at genai's reply cap."""
        left = self.server.genai.REPLY_CAP
        try:
            while left > 0:
                data = r.read1(min(CHUNK, left))
                if not data:
                    return
                left -= len(data)
                yield data
        except (OSError, ValueError):
            return                              # upstream broke mid-reply; the short body is what the client sees

    def relay(self, code, ctype, parts):
        self.status = code
        try:
            self.send_response(code)
            if ctype and ctype.isprintable() and len(ctype) <= 200:
                self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            for data in parts:
                self.wfile.write(data)
                self.wfile.flush()
                self.bytes_out += len(data)
        except OSError:
            pass                                # the client went away


def check_socket_path(path):
    parent = os.path.dirname(os.path.abspath(path))
    try:
        st = os.stat(parent)
    except OSError as e:
        raise Failure(f"{parent}: {e.strerror}")
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Failure(f"{parent} must be owned by you and closed to group and others (chmod 700)")
    if os.path.lexists(path):
        raise Failure(f"{path} already exists")


def read_token():
    line = sys.stdin.buffer.readline(512).decode("ascii", "replace").strip()
    if not TOKEN_RE.fullmatch(line):
        raise Failure("the token (first line of stdin) must be 32 to 128 hex characters")
    return line


def start(argv):
    if len(argv) != 2 or argv[0].startswith("-"):
        raise Failure("usage: keyproxy.py ENDPOINT SOCKET   (the token is the first line of stdin)")
    name, path = argv
    g = load_genai()
    try:
        cfg = g.Config()
        endpoint = g.select_endpoint(cfg, name)
        client = g.Client(endpoint)
    except g.GenaiError as e:
        raise Failure(str(e).splitlines()[0], EXIT_PROFILE)
    token = read_token()
    check_socket_path(path)
    old = os.umask(0o177)
    try:
        return Proxy(os.path.abspath(path), g, client, token, cfg.audit)
    except OSError as e:
        raise Failure(f"cannot listen on {path}: {e.strerror}")
    finally:
        os.umask(old)


def main(argv=None):
    try:
        proxy = start(sys.argv[1:] if argv is None else argv)
    except Failure as e:
        print(f"keyproxy: {e}", file=sys.stderr, flush=True)
        return e.code
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    def watch_stdin():
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        stop.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    print("ready", flush=True)
    try:
        while not stop.wait(1):
            pass
    finally:
        proxy.shutdown()
        proxy.server_close()
        try:
            os.unlink(proxy.server_address)
        except OSError:
            pass
    return EXIT_OK


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)        # a daemon thread may still be blocked reading stdin, which aborts a normal exit
