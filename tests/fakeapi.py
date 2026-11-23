"""A scripted OpenAI-compatible endpoint on loopback, for offline tests."""

import http.server
import json
import threading


def reply(text):
    return {"content": text}


def call(name, **args):
    return {"tool_calls": [(name, args)]}


def calls(*pairs):
    return {"tool_calls": list(pairs)}


class FakeAPI:
    """Serves the scripted turns in order. Each turn is reply(...) or call(...)."""

    def __init__(self, turns, stream=True, usage=None, status=200, redirect=None, error_message="nope", models=None):
        self.turns = list(turns)
        self.models = models if models is not None else [{"id": "fake-model"}]
        self.requests = []
        self.stream = stream
        self.usage = usage
        self.status = status
        self.redirect = redirect
        self.error_message = error_message
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                api.requests.append({"path": self.path, "headers": dict(self.headers)})
                self._json(200, {"object": "list", "data": api.models})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n))
                api.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if api.redirect:
                    self.send_response(307)
                    self.send_header("Location", api.redirect)
                    self.end_headers()
                    return
                if api.status != 200:
                    self._json(api.status, {"error": {"message": api.error_message}})
                    return
                turn = api.turns.pop(0) if api.turns else reply("(script exhausted)")
                if body.get("stream"):
                    self._sse(turn)
                else:
                    self._json(200, {"choices": [{"message": api._message(turn)}], "usage": api.usage})

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _sse(self, turn):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(obj):
                    self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")

                text = turn.get("content") or ""
                for i in range(0, len(text), 7):
                    send({"choices": [{"index": 0, "delta": {"content": text[i:i + 7]}}]})
                for idx, (name, args) in enumerate(turn.get("tool_calls", [])):
                    raw = json.dumps(args)
                    send({"choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": idx, "id": f"call_{idx}", "type": "function",
                         "function": {"name": name, "arguments": raw[: len(raw) // 2]}}]}}]})
                    send({"choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": idx, "function": {"arguments": raw[len(raw) // 2:]}}]}}]})
                if api.usage:
                    send({"choices": [], "usage": api.usage})
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @staticmethod
    def _message(turn):
        msg = {"role": "assistant", "content": turn.get("content")}
        if turn.get("tool_calls"):
            msg["tool_calls"] = [{"id": f"call_{i}", "type": "function",
                                  "function": {"name": n, "arguments": json.dumps(a)}}
                                 for i, (n, a) in enumerate(turn["tool_calls"])]
        return msg

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()
