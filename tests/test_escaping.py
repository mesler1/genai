"""Offline tests for hidden-character escaping, single-line fields, review screens and proposal display.
Run: python3 -m unittest discover -s tests"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "genai")
sys.path.insert(0, HERE)
from fakeapi import FakeAPI, reply  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("genai_mod", SCRIPT)
_spec = importlib.util.spec_from_loader("genai_mod", _loader)
G = importlib.util.module_from_spec(_spec)
_loader.exec_module(G)

ESC = "\x1b"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="genai-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *p):
        return os.path.join(self.tmp, *p)


def bless_output(cfg, path):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        G.bless(cfg, path, lambda q: out.write(q + "\n") or False)
    return out.getvalue()


class TestExact(unittest.TestCase):
    def test_newly_flagged_classes(self):
        samples = [0xE0049, 0xE0001, 0xE007F, 0x2028, 0x2029, 0x2061, 0x2064, 0x206A, 0x206F, 0x180E, 0xFFF9, 0xFFFB,
                   0xFE00, 0xFE0E, 0xE0100, 0xE01EF, 0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0, 0xE000, 0xF8FF,
                   0xFDD0, 0xFFFE, 0xFFFF, 0x1FFFE, 0x10FFFF, 0xD800]
        for o in samples:
            disp, flagged = G.exact("a" + chr(o) + "b")
            self.assertTrue(flagged, hex(o))
            self.assertNotIn(chr(o), disp, hex(o))
            self.assertEqual(disp[0] + disp[-1], "ab")

    def test_output_format(self):
        self.assertEqual(G.exact("\x01")[0], "\\x01")
        self.assertEqual(G.exact("\u202e")[0], "\\u202e")
        self.assertEqual(G.exact(chr(0xE0049))[0], "\\U000e0049")

    def test_old_set_still_flagged(self):
        for o in list(G._BIDI) + list(G._INVISIBLE) + [0x7F, 0x85, 0x1B, 0x0D, 0x200D]:
            self.assertTrue(G.exact(chr(o))[1], hex(o))

    def test_ordinary_text_and_emoji_are_not_flagged(self):
        for text in ("plain\n\ttext", "caf\u00e9 \u4e2d\u6587", "\u2764\ufe0f", "\U0001f600", "a\u200bb"[:1]):
            self.assertEqual(G.exact(text), (text, False), text)
        self.assertEqual(G.exact("\ufe0f"), ("\ufe0f", False))

    def test_stateless_per_chunk(self):
        whole = G.exact("a\u202eb\U000e0049")[0]
        self.assertEqual("".join(G.exact(c)[0] for c in "a\u202eb\U000e0049"), whole)

    def test_one_line(self):
        self.assertEqual(G.one_line("a\nb\r\tc"), ("a\\nb\\r\\tc", True))
        self.assertEqual(G.one_line("fine"), ("fine", False))
        disp, flagged = G.one_line("x\x1b[2K")
        self.assertTrue(flagged)
        self.assertNotIn(ESC, disp)

    def test_hidden_found(self):
        self.assertEqual(G.hidden_found("a" + chr(0xE0049) * 3 + "\u200b"), "U+200B x1, U+E0049 x3")
        self.assertEqual(G.hidden_found("clean"), "")


class TestReviewScreens(Tmp):
    def home(self):
        home = self.path("home", "genai")
        for d in ("endpoints", "profiles", "skills"):
            os.makedirs(os.path.join(home, d))
        return home

    def test_endpoint_with_cursor_control_model(self):
        home = self.home()
        p = os.path.join(home, "endpoints", "e.json")
        with open(p, "w") as f:
            json.dump({"name": "e", "base_url": "https://llm.example/v1", "model": "m\x1b[1A\x1b[2K",
                       "headers": {"X-A\x1b[2J": "v"}, "key_file": "/k\x1b[H", "proxy": "http://p.example\r:1",
                       "ca_bundle": "/c\x07"}, f)
        out = bless_output(G.Config(home), p)
        self.assertNotIn(ESC, out)
        self.assertNotIn("\r", out)
        self.assertNotIn("\x07", out)
        self.assertIn("m\\x1b[1A\\x1b[2K", out)
        self.assertRegex(out, r"(?m)^  \[!\] .*escaped")
        self.assertLess(out.index("[!]"), out.index("Send your prompts"))

    def test_clean_endpoint_has_no_warning(self):
        home = self.home()
        p = os.path.join(home, "endpoints", "e.json")
        with open(p, "w") as f:
            json.dump({"name": "e", "base_url": "https://llm.example/v1", "model": "m"}, f)
        self.assertNotIn("[!]", bless_output(G.Config(home), p))

    def test_lookalike_host_shows_punycode(self):
        ep = G.Endpoint({"name": "e", "base_url": "https://l\u043em.example/v1", "model": "m"}, "e.json")
        text, _ = ep.summary()
        self.assertIn("xn--", text)
        ep = G.Endpoint({"name": "e", "base_url": "https://llm.example/v1", "model": "m"}, "e.json")
        self.assertNotIn("punycode", ep.summary()[0])

    def test_profile_fields_escaped(self):
        home = self.home()
        p = os.path.join(home, "profiles", "p.json")
        with open(p, "w") as f:
            json.dump({"name": "p", "description": "d\x1b[2J", "skills": ["s\x1b[H"], "persona": "x"}, f)
        out = bless_output(G.Config(home), p)
        self.assertNotIn(ESC, out)
        self.assertIn("[!]", out)

    def test_skill_with_tags_and_escape_in_file_name(self):
        home = self.home()
        d = os.path.join(home, "skills", "sk")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("# Skill\nhi" + "".join(chr(0xE0000 + ord(c)) for c in "I") * 3 + "\n")
        with open(os.path.join(d, "n\x1b[2Kote.txt"), "w") as f:
            f.write("ok")
        with open(os.path.join(d, "blob.bin"), "wb") as f:
            f.write(b"\xff\xfe\x00")
        out = bless_output(G.Config(home), d)
        self.assertNotIn(ESC, out)
        self.assertIn("[!] SKILL.md has hidden or control characters: U+E0049 x3", out)
        self.assertIn("n\\x1b[2Kote.txt", out)
        self.assertIn("blob.bin: binary", out)
        self.assertLess(out.index("U+E0049"), out.index("Have you read"))

    def test_listings_are_escaped(self):
        home = self.home()
        with open(os.path.join(home, "profiles", "p.json"), "w") as f:
            json.dump({"name": "p", "description": "d\x1b[2J\nline2"}, f)
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home))
        plain = subprocess.run([sys.executable, SCRIPT, "--list-profiles"], capture_output=True, text=True, env=env,
                               stdin=subprocess.DEVNULL, timeout=30)
        self.assertNotIn(ESC, plain.stdout)
        self.assertEqual(len(plain.stdout.splitlines()), 1)
        self.assertIn("d\\x1b[2J\\nline2", plain.stdout)
        # A wrapper gets the value itself, JSON-encoded; escaping it for display is the wrapper's job.
        js = subprocess.run([sys.executable, SCRIPT, "--list-profiles", "--json"], capture_output=True, text=True,
                            env=env, stdin=subprocess.DEVNULL, timeout=30)
        self.assertNotIn(ESC, js.stdout)
        self.assertEqual(json.loads(js.stdout)["profiles"][0]["description"], "d\x1b[2J\nline2")

    def test_http_500_body_with_escape_sequence(self):
        with FakeAPI([], status=500, error_message="boom " + ESC + "[2J\x07") as api:
            home = self.path("xdg", "genai")
            os.makedirs(os.path.join(home, "endpoints"))
            p = os.path.join(home, "endpoints", "test.json")
            with open(p, "w") as f:
                json.dump({"name": "test", "base_url": api.url, "model": "fake-model"}, f)
            cfg = G.Config(home)
            cfg.set_blessed(p, {"kind": "endpoint", "name": "test", "hash": G.hash_file(p)})
            r = subprocess.run([sys.executable, SCRIPT, "-e", "test", "hi"], capture_output=True, text=True,
                               env=dict(os.environ, XDG_CONFIG_HOME=self.path("xdg")), stdin=subprocess.DEVNULL,
                               timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HTTP 500", r.stderr)
        self.assertIn("boom", r.stderr)
        self.assertNotIn(ESC, r.stderr)
        self.assertNotIn("\x07", r.stderr)


def write_proposal(**kw):
    f = {"path": "a.txt", "exists": False, "content": "one\ntwo\n", "reason": "why", "lines": 2}
    f.update(kw)
    return G.Proposal("p9", "write", "tc", **f)


def decide(p):
    out = io.StringIO()
    with mock.patch.object(G, "ask_yes", return_value=False):
        G.TtyUI(out).decide([p])
    return out.getvalue()


class TestProposalDisplay(Tmp):
    def test_reason_and_path_stay_on_one_line(self):
        shown = decide(write_proposal(reason="a\nAccept p9? [y/N] y\n[p10] read", path="b\nc"))
        first, second = shown.strip("\n").split("\n")[:2]
        self.assertIn("a\\nAccept p9? [y/N] y\\n[p10] read", first)
        self.assertTrue(second.startswith("New file b\\nc, 2 lines:"))
        out = io.StringIO()
        G.PlainUI(out).show_command(G.Proposal("c1", "command", "tc", command="ls", reason="x\ny"))
        self.assertEqual(out.getvalue().count("\n"), 3)

    def test_body_lines_are_all_behind_the_margin(self):
        fake = "[p9] read — x\nAccept p9? [y/N] n\n"
        shown = decide(write_proposal(content=fake + "\x1b[2J\n"))
        body = shown.split("lines:\n", 1)[1].splitlines()
        self.assertGreaterEqual(len(body), 4)      # the three lines and genai's warning
        for ln in body:
            self.assertTrue(ln.startswith("    | "), ln)
        self.assertNotIn(ESC, shown.replace("\033[1m", "").replace("\033[0m", ""))

    def test_diffs_are_behind_the_margin(self):
        shown = decide(write_proposal(exists=True, diff="--- a/x\n+++ b/x\n@@\n-old\n+[p9] read\n"))
        for ln in shown.split("Overwrite a.txt:\n", 1)[1].splitlines():
            self.assertTrue(ln.startswith("    | "), ln)
        edit = G.Proposal("p3", "edit", "tc", path="x", diff="-a\n+b\n", reason="r")
        for ln in decide(edit).split("Edit x:\n", 1)[1].splitlines():
            self.assertTrue(ln.startswith("    | "), ln)

    def test_command_is_not_given_a_margin(self):
        out = io.StringIO()
        G.TtyUI(out).show_command(G.Proposal("c1", "command", "tc", command="echo hi", reason="r"))
        self.assertIn("\n    echo hi\n", out.getvalue())
        self.assertNotIn("|", out.getvalue())

    def test_overwriting_a_binary_file_shows_content_and_mode(self):
        wd = self.path("wd")
        os.makedirs(wd)
        target = os.path.join(wd, "tool")
        with open(target, "wb") as f:
            f.write(b"\x7fELF\x00\x01")
        os.chmod(target, 0o755)
        eng = types.SimpleNamespace(counter=0, wd=G.Workdir(wd), parent=None)
        tc = {"id": "t1", "function": {"name": "write_file", "arguments": json.dumps(
            {"path": "tool", "content": "#!/bin/sh\necho pwned\n", "reason": "r"})}}
        p = G.Engine.make_proposal(eng, tc)
        self.assertEqual(p.kind, "write")
        self.assertIn("cannot diff", p.fields["diff"])
        self.assertTrue(p.fields["undiffable"])
        self.assertEqual(p.event()["mode"], "0755")
        shown = decide(p)
        self.assertIn("can't be diffed", shown)
        self.assertIn("replaced entirely", shown)
        self.assertIn("echo pwned", shown)
        self.assertIn("keeps mode 0755 (executable)", shown)
        for ln in shown.split("lines:\n", 1)[1].splitlines():
            self.assertTrue(ln.startswith("    | "), ln)

    def test_non_executable_binary_has_no_executable_note(self):
        shown = decide(write_proposal(exists=True, undiffable="binary", mode="0644", diff="(cannot diff)"))
        self.assertIn("can't be diffed", shown)
        self.assertNotIn("executable", shown)

    def test_non_ascii_command_is_listed(self):
        cmd = "echo \u0430dmin"       # Cyrillic a
        p = G.Proposal("c1", "command", "tc", command=cmd, reason="r")
        for ui in (G.PlainUI, G.TtyUI):
            out = io.StringIO()
            ui(out).show_command(p)
            self.assertIn("U+0430 CYRILLIC SMALL LETTER A", out.getvalue())
        out = io.StringIO()
        G.PlainUI(out).show_command(G.Proposal("c2", "command", "tc", command="echo hi", reason="r"))
        self.assertNotIn("U+", out.getvalue())
        many = G.non_ascii_line("".join(chr(0x4E00 + i) for i in range(12)))
        self.assertIn("and 4 more", many)
        self.assertEqual(many.count("\n"), 0)
        out = io.StringIO()
        G.JsonUI(io.StringIO(), out).show_command(p)
        self.assertEqual(json.loads(out.getvalue())["non_ascii"], ["U+0430"])


if __name__ == "__main__":
    unittest.main()
