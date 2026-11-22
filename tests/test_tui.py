"""Tests for the TUI's conversation pane (tui/conversation.py). Run: python3 -m unittest discover -s tests"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from fakeapi import FakeAPI, call, reply  # noqa: E402


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


C = load("conversation", os.path.join(REPO, "tui", "conversation.py"))
G = load("genai_for_tui", os.path.join(REPO, "genai"))


def text_of(block, w=60):
    return "\n".join("".join(t for t, _ in ln) for ln in block.lines(w))


class TestText(unittest.TestCase):
    def test_clean_makes_control_and_invisible_characters_visible(self):
        self.assertEqual(C.clean("a\x1b[2Jb\tc‮d​e\n"), "a\\x1b[2Jb    c\\u202ed\\u200be\n")
        self.assertEqual(C.clean("\x9b"), "\\x9b")

    def test_wrap_counts_wide_characters_and_breaks_long_words(self):
        self.assertEqual(C.wrap("你好你好", 4), ["你好", "你好"])
        self.assertEqual(C.wrap("abcdefghij", 4), ["abcd", "efgh", "ij"])
        self.assertEqual(C.wrap("one two three", 7), ["one two", "three"])
        self.assertTrue(all(C.width(ln) <= 5 for ln in C.wrap("x" * 3 + " 你好你好你 y", 5)))

    def test_markdown_styles_and_fences(self):
        lines = C.markdown("Use **sbatch** and `squeue`\n```\nraw **not bold**\n```\n- item", 40)
        self.assertIn(("sbatch", "bold"), lines[0])
        self.assertIn(("squeue", "code"), lines[0])
        self.assertEqual("".join(t for t, _ in lines[1]), "│ raw **not bold**")   # fenced: kept as written
        self.assertEqual("".join(t for t, _ in lines[2]), "• item")

    def test_completion(self):
        self.assertEqual(C.complete("/he"), "/help ")
        self.assertEqual(C.complete("/sk"), "/skill")            # /skill and /skills agree that far
        self.assertEqual(C.complete("hello"), "hello")


class TestEditor(unittest.TestCase):
    def test_multiline_editing_and_history(self):
        e = C.Editor()
        e.insert("first")
        self.assertEqual(e.take(), "first")
        e.insert("line one")
        e.insert("\n")
        e.insert("two")
        e.up()
        self.assertEqual(e.pos, 3)                               # same column, line above
        e.up()                                                   # top line: previous message
        self.assertEqual(e.text, "first")
        e.down()
        self.assertEqual(e.text, "line one\ntwo")                # the draft comes back
        e.kill_word()
        self.assertEqual(e.text, "line one\n")
        e.insert("x y")
        e.kill_back()
        self.assertEqual(e.text, "line one\n")

    def test_view_wraps_and_places_the_cursor(self):
        e = C.Editor()
        e.insert("abcdefghij")
        rows, cur = e.view(8)
        self.assertEqual(rows, ["› abcde", "  fghij"])
        self.assertEqual(cur, (1, 7))                            # just after the last character
        e.insert("\x1b")
        self.assertIn("\\x1b", e.view(40)[0][0])                  # pasted control characters show escaped


class TestModel(unittest.TestCase):
    def hello(self, m, **extra):
        m.handle(dict({"type": "hello", "version": "1", "endpoint": {"name": "e", "model": "m", "base_url": "u"},
                       "sandbox": None}, **extra))

    def test_reply_streams_into_one_block(self):
        m = C.Model()
        self.hello(m)
        m.submit("hi")
        self.assertEqual(m.outbox, [{"type": "user", "text": "hi"}])
        self.assertTrue(m.busy)
        for chunk in ("Hel", "lo"):
            m.handle({"type": "text", "text": chunk})
        m.handle({"type": "done", "reason": "reply", "context": {"used": 10, "limit": 100}})
        self.assertFalse(m.busy)
        self.assertEqual(m.context, (10, 100))
        self.assertEqual(sum(isinstance(b, C.Reply) for b in m.blocks), 1)
        self.assertIn("Hello", text_of(m.blocks[-1]))

    def test_messages_typed_while_busy_are_queued(self):
        m = C.Model()
        m.submit("one")
        m.submit("two")
        self.assertEqual(len(m.outbox), 1)
        m.handle({"type": "done", "reason": "reply", "context": {}})
        self.assertEqual(m.outbox[-1], {"type": "user", "text": "two"})
        self.assertTrue(m.busy)

    def test_approvals_go_in_order_and_cards_follow_results(self):
        m = C.Model()
        m.submit("go")
        m.handle({"type": "proposal", "id": "p1", "kind": "write", "path": "a", "content": "x", "exists": False})
        m.handle({"type": "proposal", "id": "p2", "kind": "read", "path": ".env", "secret": True})
        m.handle({"type": "proposal", "id": "p3", "kind": "read", "path": "b", "auto": True})
        m.handle({"type": "proposal", "id": "p4", "kind": "command", "command": "rm -rf ~"})
        self.assertEqual([c.id for c in m.pending], ["p1", "p2"])     # auto and command cards don't wait
        m.decide(True)
        m.decide(False)
        self.assertEqual(m.outbox[1:], [{"type": "decision", "id": "p1", "decision": "accept"},
                                        {"type": "decision", "id": "p2", "decision": "decline"}])
        m.handle({"type": "result", "id": "p1", "status": "ok", "detail": "wrote a"})
        self.assertEqual((m.cards["p1"].state, m.cards["p2"].state), ("done", "declined"))
        self.assertIn("secrets", text_of(m.cards["p2"]))
        self.assertIn("$ rm -rf ~", text_of(m.cards["p4"]))
        self.assertIn("genai never runs it", text_of(m.cards["p4"]))

    def test_model_text_is_escaped_everywhere(self):
        m = C.Model()
        m.handle({"type": "text", "text": "evil\x1b]0;title\x07"})
        m.handle({"type": "proposal", "id": "p1", "kind": "edit", "path": "a\x1b[2J", "diff": "+x\x1b[31m",
                  "reason": "r‮"})
        shown = text_of(m.blocks[0]) + text_of(m.blocks[1])
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("‮", shown)
        self.assertIn("\\x1b", shown)

    def test_commands(self):
        m = C.Model()
        m.submit("/coding on")
        m.submit("/skill my skill on")
        m.submit("/skill x")
        m.submit("//not a command")
        self.assertEqual(m.outbox[:2], [{"type": "coding", "on": True},
                                        {"type": "skill", "name": "my skill", "on": True}])
        self.assertIn("usage: /skill", text_of(m.blocks[0]))
        self.assertEqual(m.outbox[2], {"type": "user", "text": "/not a command"})
        m.submit("/coding off")                                   # busy now: waits
        self.assertEqual(len(m.outbox), 3)
        m.submit("/quit")
        self.assertTrue(m.quit)

    def test_failed_turn_ends_busy(self):
        m = C.Model()
        m.submit("hi")
        m.handle({"type": "error", "code": "api", "message": "endpoint error"})
        self.assertFalse(m.busy)
        m.submit("again")
        self.assertTrue(m.busy)


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class TestInTmux(unittest.TestCase):
    """The pane for real: genai --json against the fake endpoint, typed into through tmux."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="genai-tui-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sock = f"genai-tui-test-{os.getpid()}"
        self.addCleanup(self.tmux, "kill-server")

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, *args], capture_output=True, text=True).stdout

    def screen(self):
        return self.tmux("capture-pane", "-p", "-t", "t")

    def wait_for(self, text, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if text in self.screen():
                return
            time.sleep(0.1)
        self.fail(f"{text!r} never appeared:\n{self.screen()}")

    def type(self, text, enter=True):
        self.tmux("send-keys", "-t", "t", "-l", text)
        if enter:
            self.tmux("send-keys", "-t", "t", "Enter")

    def start(self, api):
        home = os.path.join(self.tmp, "cfg", "genai")
        os.makedirs(os.path.join(home, "endpoints"))
        ep = os.path.join(home, "endpoints", "test.json")
        with open(ep, "w") as f:
            json.dump({"name": "test", "base_url": api.url, "model": "fake-model", "context_limit": 100000}, f)
        cfg = G.Config(home)
        cfg.ensure()
        cfg.set_blessed(ep, {"kind": "endpoint", "name": "test", "hash": G.hash_file(ep)})
        self.wd = os.path.join(self.tmp, "wd")
        os.makedirs(self.wd)
        cmd = (f"env XDG_CONFIG_HOME={os.path.dirname(home)} LANG=C.UTF-8 TERM=xterm-256color "
               f"{sys.executable} {os.path.join(REPO, 'tui', 'conversation.py')} -e test; sleep 30")
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "t", "-x", "90", "-y", "30", "-c", self.wd, cmd)
        self.wait_for("Type a message")

    def test_conversation_and_approvals(self):
        turns = [reply("Hello **there**."),
                 call("write_file", path="new.txt", content="one\n", reason="make it"),
                 call("write_file", path="other.txt", content="two\n", reason="and this"),
                 reply("Made one file."),
                 call("propose_command", command="sbatch run.sbatch", reason="resubmit")]
        with FakeAPI(turns) as api:
            self.start(api)
            self.type("hi")
            self.wait_for("● Hello there.")
            self.type("make files")
            self.wait_for("Allow p1: create new.txt?")
            self.tmux("send-keys", "-t", "t", "y")
            self.wait_for("Allow p2: create other.txt?")
            self.tmux("send-keys", "-t", "t", "n")
            self.wait_for("Made one file.")
            self.assertTrue(os.path.exists(os.path.join(self.wd, "new.txt")))
            self.assertFalse(os.path.exists(os.path.join(self.wd, "other.txt")))
            self.type("resubmit")
            self.wait_for("$ sbatch run.sbatch")
            self.type("/coding on")
            self.wait_for("coding mode is on")
            tools = [m["content"] for m in api.requests[-1]["body"]["messages"] if m["role"] == "tool"]
            self.assertIn("declined by the user", tools)

    def test_alt_enter_adds_a_line_and_paste_does_not_send(self):
        with FakeAPI([reply("got it")]) as api:
            self.start(api)
            self.type("first", enter=False)
            self.tmux("send-keys", "-t", "t", "M-Enter")
            self.tmux("set-buffer", "-b", "p", "pasted\nlines")
            self.tmux("paste-buffer", "-p", "-b", "p", "-t", "t")
            time.sleep(0.5)
            self.assertEqual(api.requests, [])                    # nothing sent yet
            self.tmux("send-keys", "-t", "t", "Enter")
            self.wait_for("got it")
            sent = [m for m in api.requests[0]["body"]["messages"] if m["role"] == "user"][-1]["content"]
            self.assertIn("first\npasted\nlines", sent)


if __name__ == "__main__":
    unittest.main()
