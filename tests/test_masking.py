"""Offline tests for masking output bound for the model: speed, the endpoint key, hidden characters, reply size.
Run: python3 -m unittest discover -s tests"""

import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "hoplon")
sys.path.insert(0, HERE)
from fakeapi import FakeAPI, reply  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("hoplon_mod", SCRIPT)
_spec = importlib.util.spec_from_loader("hoplon_mod", _loader)
G = importlib.util.module_from_spec(_spec)
_loader.exec_module(G)

KEY = "Zx9Q7mK2pL8vN4tR6wY1"
BIG = 256 * 1024


def adversarial(n=BIG):
    pk = "-----BEGIN PRIVATE KEY-----\n"
    return {
        "a.": "a." * (n // 2), "a-": "a-" * (n // 2), "a+": "a+" * (n // 2), "a:": "a:" * (n // 2),
        "a=": "a=" * (n // 2), "a.b://": "a.b://" * (n // 6), "eyJ repeated": "eyJ" * (n // 3),
        "eyJ run": "eyJ" + "a" * n, "eyJ-": "eyJ-" * (n // 4), "bearer repeated": "bearer " * (n // 7),
        "bearer run": "bearer " + "a" * n, "begin repeated": pk * (n // len(pk)),
        "password repeated": "password" * (n // 8), "one run": "a" * n, "password run": "password=" + "a" * n,
        "scheme run": "a." * (n // 2) + "://u:p@h", "github_pat_": "github_pat_" * (n // 11) + "-",
        "sk-": "sk-" * (n // 3) + " ", "xox": "xoxb-" * (n // 5) + " ",
    }


class TestSpeed(unittest.TestCase):
    def test_redact_is_fast_on_adversarial_input(self):
        for name, text in adversarial().items():
            t = time.monotonic()
            G.redact(text)
            self.assertLess(time.monotonic() - t, 5, name)

    def test_strip_ansi_is_fast_on_adversarial_input(self):
        n = BIG
        for name, text in {"esc[": "\x1b[" * (n // 2), "esc[;": "\x1b[" + "9;" * (n // 2), "esc]": "\x1b]" + "a" * n,
                           "esc] repeated": "\x1b]a" * (n // 3), "esc": "\x1b" * n, "esc[ space": "\x1b[" + " " * n,
                           "esc[ repeated": "\x1b[1;" * (n // 4)}.items():
            t = time.monotonic()
            G.strip_ansi(text)
            self.assertLess(time.monotonic() - t, 5, name)


class TestRealisticPatterns(unittest.TestCase):
    CASES = [
        ("private key", "x\n-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\ny", "x\n[REDACTED private key]\ny"),
        ("aws key id", "id AKIAIOSFODNN7EXAMPLE end", "id [REDACTED aws key id] end"),
        ("github token", "t=ghp_" + "a1" * 18, "t=[REDACTED github token]"),
        ("github token", "github_pat_" + "A_b" * 15 + " x", "[REDACTED github token] x"),
        ("slack token", "xoxb-1234567890-abcdefghij", "[REDACTED slack token]"),
        ("api key", "key sk-" + "x" * 30 + ".", "key [REDACTED api key]."),
        ("jwt", "Authorization: eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", "Authorization: [REDACTED jwt]"),
        ("bearer token", "Authorization: Bearer abcdefghijklmnop12345", "Authorization: Bearer [REDACTED]"),
        ("url password", "git clone https://bob:hunter22@example.com/x", "git clone https://bob:[REDACTED]@example.com/x"),
        ("url password", "postgres+ssl://u:p%40ss@db:5432/x", "postgres+ssl://u:[REDACTED]@db:5432/x"),
        ("secret assignment", "DB_PASSWORD=swordfish", "DB_PASSWORD=[REDACTED]"),
        ("secret assignment", "export AWS_SECRET_ACCESS_KEY='abcd1234'", "export AWS_SECRET_ACCESS_KEY='[REDACTED]'"),
        ("secret assignment", 'my.api-key: "tok_12345"', 'my.api-key: "[REDACTED]"'),
        ("secret assignment", "GITHUB_TOKEN = ghx", "GITHUB_TOKEN = ghx"),            # under 4 characters: kept
        ("secret assignment", "x_token_value=abcdef", "x_token_value=[REDACTED]"),
        ("secret assignment", "passwd:abcd", "passwd:[REDACTED]"),
    ]

    def test_each_kind_still_masks(self):
        for kind, text, want in self.CASES:
            out, counts = G.redact(text)
            self.assertEqual(out, want, text)
            self.assertEqual(bool(counts), want != text, text)
            if want != text:
                self.assertIn(kind, counts, text)
        self.assertEqual({k for k, _, w in self.CASES if w}, {k for k, _ in G._REDACTIONS})

    def test_unterminated_private_key_header_is_left_alone(self):
        text = "-----BEGIN PRIVATE KEY-----\nabc"
        self.assertEqual(G.redact(text), (text, {}))

    def test_two_keys_and_text_after_the_last(self):
        k = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        out, counts = G.redact(f"{k}\nmid\n{k}\ntail -----BEGIN PRIVATE KEY-----")
        self.assertEqual(out, "[REDACTED private key]\nmid\n[REDACTED private key]\ntail -----BEGIN PRIVATE KEY-----")
        self.assertEqual(counts, {"private key": 2})


class TestEndpointKey(unittest.TestCase):
    def test_masked_under_an_unknown_variable_name(self):
        text = f"HPC_LLM_CRED={KEY}\nagain {KEY}"
        self.assertEqual(G.redact(text)[0], text)                 # patterns alone do not know it
        out, counts = G.redact(text, [KEY])
        self.assertNotIn(KEY, out)
        self.assertEqual(out, "HPC_LLM_CRED=[REDACTED endpoint key]\nagain [REDACTED endpoint key]")
        self.assertEqual(counts, {"endpoint key": 2})

    def test_short_empty_and_missing_values_are_ignored(self):
        text = "abcdefg hello abcdefg"
        for secrets in ([""], [None], ["abcdefg"], ["a"], [], None):
            self.assertEqual(G.redact(text, secrets or ()), (text, {}), secrets)

    def test_eight_characters_is_enough(self):
        self.assertEqual(G.redact("k=12345678!", ["12345678"])[0], "k=[REDACTED endpoint key]!")

    def test_engine_secrets_with_and_without_a_key(self):
        eng = types.SimpleNamespace(client=types.SimpleNamespace(_key=KEY))
        self.assertEqual(G.Engine.secrets(eng), [KEY])
        self.assertEqual(G.redact("a", G.Engine.secrets(types.SimpleNamespace(client=object()))), ("a", {}))

    def test_json_attach(self):
        out = io.StringIO()
        ui = G.JsonUI(io.StringIO(), out)
        ui.queue_attach({"text": f"HPC_LLM_CRED={KEY}"})                  # no engine yet
        self.assertIn(KEY, ui.queue[0][1])
        ui.engine = types.SimpleNamespace(secrets=lambda: [KEY])
        ui.queue_attach({"text": f"HPC_LLM_CRED={KEY}", "label": "env"})
        self.assertNotIn(KEY, ui.queue[1][1])
        self.assertIn("[REDACTED endpoint key]", ui.queue[1][1])
        self.assertIn('"endpoint key": 1', out.getvalue())

    def test_external_tool_result(self):
        tmp = tempfile.mkdtemp(prefix="hoplon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        eng = types.SimpleNamespace(audit=G.Audit(os.path.join(tmp, "audit.log")),
                                    secrets=lambda: [KEY])
        p = G.Proposal("e1", "external", "tc1", tool="job_details", args={})
        p.decision, p.result = "ok", f"env: HPC_LLM_CRED={KEY}"
        out = G.Engine.perform(eng, p)
        self.assertNotIn(KEY, out)
        self.assertIn("[REDACTED endpoint key]", out)


class Editor(unittest.TestCase):
    """review_output with the editor replaced by a function that records the buffer and returns it."""

    def review(self, lead, text, key=None):
        seen = {}

        def edit(buf, title, what, cancel):
            seen["buf"], seen["title"] = buf, title
            return buf

        eng = types.SimpleNamespace(share=lambda *a: None, secrets=lambda: [key])
        with mock.patch.object(G, "edit_text", edit):
            kept = G.review_output(eng, "run", lead, text, "Output of: x")
        return kept, seen

    def test_escapes_and_zero_width_are_visible_and_noted(self):
        kept, seen = self.review(["I ran this:", "```", "echo hi", "```", "Output:"], "ok\x1b[2Jgone\u200b!")
        self.assertEqual(kept, seen["buf"])
        for bad in ("\x1b", "\u200b"):
            self.assertNotIn(bad, kept)
        self.assertIn("\\x1b[2J", kept)
        self.assertIn("\\u200b", kept)
        self.assertIn("Hidden or control characters", seen["title"])

    def test_tag_characters_are_visible(self):
        if not G.exact("\U000e0041")[1]:
            self.skipTest("exact() does not flag tag characters in this tree")
        kept, seen = self.review(["lead"], "a\U000e0041b")
        self.assertNotIn("\U000e0041", kept)
        self.assertIn("Hidden or control characters", seen["title"])

    def test_lead_is_escaped_too(self):
        kept, seen = self.review(["I ran this\x1b[31m:", "```", "echo \u202e", "```", "Output:"], "plain")
        self.assertNotIn("\x1b", kept)
        self.assertNotIn("\u202e", kept)
        self.assertIn("Hidden or control characters", seen["title"])

    def test_clean_output_has_no_hidden_note_and_keeps_newlines_and_tabs(self):
        kept, seen = self.review(["lead"], "a\tb\nc")
        self.assertIn("a\tb\nc", kept)
        self.assertNotIn("Hidden or control", seen["title"])

    def test_key_and_patterns_masked_before_the_editor(self):
        kept, seen = self.review(["lead"], f"HPC_LLM_CRED={KEY}\nDB_PASSWORD=swordfish", KEY)
        self.assertNotIn(KEY, kept)
        self.assertNotIn("swordfish", kept)
        self.assertIn("endpoint key", seen["title"])


class TestRunColours(unittest.TestCase):
    def test_colour_codes_are_removed_from_run_output(self):
        tmp = tempfile.mkdtemp(prefix="hoplon-test-")
        self.addCleanup(shutil.rmtree, tmp, True)
        p = G.Proposal("c1", "command", "tc1", command="ls", reason="r")
        bufs = []
        eng = types.SimpleNamespace(commands=[p], sandbox=None, audit=G.Audit(os.path.join(tmp, "audit.log")),
                                    secrets=lambda: [], share=lambda *a: None, user_turn=lambda *a: None,
                                    ui=G.PlainUI())

        def edit(text, title, what, cancel):
            bufs.append(text)
            return text

        proc = mock.Mock()
        proc.stdout.read1.side_effect = [b"\x1b[31mred\x1b[0m plain\r\nbell\x07\n", b""]
        proc.wait.return_value = 0
        with mock.patch.object(G, "edit_text", edit), mock.patch.object(G.subprocess, "Popen", return_value=proc), \
                mock.patch.object(G, "ask_yes", return_value=True), \
                mock.patch.object(sys, "stdout", io.TextIOWrapper(io.BytesIO())):
            G.Repl(eng).do_run("")
        shown = bufs[-1]
        self.assertIn("red plain", shown)
        self.assertNotIn("\x1b[31m", shown)
        self.assertNotIn("\\x1b", shown)                      # removed, not shown as an escape
        self.assertIn("\\x07", shown)                         # but other control characters are shown


class TestReplyCap(unittest.TestCase):
    def client(self, api, stream):
        ep = G.Endpoint({"name": "t", "base_url": api.url, "model": "m", "allow_http": True,
                         "params": {"stream": stream}}, "t.json")
        return G.Client(ep)

    def test_oversized_non_streaming_reply(self):
        with FakeAPI([reply("x" * 5000)]) as api, mock.patch.object(G, "REPLY_CAP", 1000):
            with self.assertRaises(G.HoplonError) as cm:
                self.client(api, False).chat([{"role": "user", "content": "hi"}], None, lambda t: None)
        self.assertEqual((cm.exception.code, cm.exception.kind), (G.EXIT_API, "api"))
        self.assertIn("exceeded", str(cm.exception))
        self.assertIn("nothing was performed", str(cm.exception))

    def test_oversized_streamed_reply(self):
        with FakeAPI([reply("x" * 5000)]) as api, mock.patch.object(G, "REPLY_CAP", 1000):
            with self.assertRaises(G.HoplonError) as cm:
                self.client(api, True).chat([{"role": "user", "content": "hi"}], None, lambda t: None)
        self.assertEqual((cm.exception.code, cm.exception.kind), (G.EXIT_API, "api"))
        self.assertIn("exceeded", str(cm.exception))

    def test_oversized_single_line(self):
        r = io.BytesIO(b"data: " + b"x" * 5000 + b"\n")
        with mock.patch.object(G, "REPLY_CAP", 1000), self.assertRaises(G.HoplonError):
            G.Client._stream(object(), r, lambda t: None)

    def test_replies_under_the_cap_still_work(self):
        for stream in (False, True):
            with FakeAPI([reply("hello there")]) as api:
                msg, _ = self.client(api, stream).chat([{"role": "user", "content": "hi"}], None, lambda t: None)
            self.assertEqual(msg["content"], "hello there", stream)


if __name__ == "__main__":
    unittest.main()
