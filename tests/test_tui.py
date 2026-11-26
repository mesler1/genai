"""Tests for the TUI's conversation pane (tui/conversation.py). Run: python3 -m unittest discover -s tests"""

import importlib.machinery
import importlib.util
import json
import os
import queue
import re
import socket
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

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
sys.path.insert(0, os.path.join(REPO, "tui"))
C_PROMPT = load("prompt", os.path.join(REPO, "tui", "prompt.py"))
IPC = load("ipc", os.path.join(REPO, "tui", "ipc.py"))
REC = load("recorder", os.path.join(REPO, "tui", "recorder.py"))
RAW = C.Model().feature("raw")
F = importlib.import_module("features")
G = load("hoplon_for_tui", os.path.join(REPO, "hoplon"))


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


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


class TestApprovalDisplay(unittest.TestCase):
    def test_undiffable_overwrite_shows_replacement_and_mode(self):
        card = C.Card({"kind": "write", "exists": True, "undiffable": "binary",
                       "diff": "(cannot diff)", "mode": "0755", "content": "replacement"})
        self.assertIn("replacement", card.full_text())
        self.assertIn("0755 (executable)", card.full_text())

    def test_diff_keeps_content_that_looks_like_headers(self):
        card = C.Card({"kind": "edit", "diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n---remove\n+++add"})
        self.assertIn("---remove\n+++add", card.full_text())
        self.assertNotIn("--- a/x", card.full_text())


class TestShellDispatch(unittest.TestCase):
    def test_commands_are_shown_exactly_as_they_run(self):
        for cmd in ("make \\\n  all", "printf 'a\\nb'", "grep -E '\\bfoo\\b' x"):
            self.assertEqual(C.command_text(cmd), cmd)
        self.assertEqual(C.command_text("a\tb"), "a⇥b")
        self.assertEqual(C.unsafe_chars("echo 'ok' \\x1b"), [])
        for bad in ("echo \x1b[2J", "echo \u202eevil", "rm \u200b-rf", "a\u2028b"):
            self.assertTrue(C.unsafe_chars(bad), repr(bad))
            with self.assertRaisesRegex(C.Busy, "control or invisible"):
                C.Shell("%1").type(bad)

    def test_bracketed_paste_unknown_on_older_tmux(self):
        shell = C.Shell("%1")
        shell.primary_prompt = lambda: True
        for flag, ok in (("1", True), ("", True), ("0", False)):         # tmux 3.2 and 3.3 report nothing
            with mock.patch.object(shell, "tmux", return_value=f"123 bash 0 {flag} /work\n"):
                if ok:
                    self.assertEqual(shell.ready(), ("123", "/work"))
                else:
                    self.assertRaises(C.Busy, shell.ready)
        shell.primary_prompt = lambda: False                               # a continuation prompt, or a program
        with mock.patch.object(shell, "tmux", return_value="123 bash 0  /work\n"):
            self.assertRaises(C.Busy, shell.ready)

    def test_failed_paste_never_sends_enter(self):
        shell = C.Shell("%1")
        with mock.patch.object(shell, "ready", return_value=("123", "/work")), \
                mock.patch.object(shell, "tmux", side_effect=["", "", subprocess.CalledProcessError(1, "paste")]) as tmux:
            with self.assertRaises(subprocess.CalledProcessError):
                shell.type("echo reviewed", expected=("123", "/work"))
        self.assertNotIn(mock.call("send-keys", "-t", "%1", "Enter", check=True), tmux.call_args_list)

    def test_changed_directory_prevents_paste(self):
        shell = C.Shell("%1")
        with mock.patch.object(shell, "ready", return_value=("123", "/another")), \
                mock.patch.object(shell, "tmux") as tmux:
            with self.assertRaisesRegex(C.Busy, "changed since review"):
                shell.type("echo reviewed", expected=("123", "/work"))
        tmux.assert_not_called()


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

    def test_clear_asks_hoplon_to_start_over(self):
        m = C.Model()
        self.hello(m, features=["clear"])
        m.submit("hi")
        m.handle({"type": "proposal", "id": "p1", "kind": "command", "command": "ls"})
        m.handle({"type": "done", "reason": "reply", "context": {"used": 900, "limit": 1000}})
        m.outbox.clear()
        m.submit("/clear")
        self.assertEqual(m.outbox, [{"type": "clear"}])
        m.handle({"type": "cleared", "dropped": 3, "context": {"used": 40, "limit": 1000}})
        self.assertEqual((m.cards, m.context), ({}, (40, 1000)))
        self.assertEqual(len(m.blocks), 1)
        self.assertIn("context cleared", text_of(m.blocks[0]))
        old = C.Model()
        self.hello(old)                                      # a hoplon without "clear": only the screen clears
        old.submit("/clear")
        self.assertNotIn({"type": "clear"}, old.outbox)
        self.assertIn("only the screen", text_of(old.blocks[-1]))

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

    def test_decline_a_command(self):
        m = C.Model()
        self.hello(m)
        m.submit("go")
        for i in (1, 2):
            m.handle({"type": "proposal", "id": f"p{i}", "kind": "command", "command": f"rm -rf build{i}"})
        m.handle({"type": "done", "reason": "command", "context": {"used": 1, "limit": 9}})
        m.submit("/decline")                                   # the latest
        self.assertEqual((m.cards["p2"].state, m.cards["p1"].state), ("declined", "shown"))
        self.assertEqual((m.cards["p2"].key, m.keyed(1).id), (None, "p1"))   # Alt+1 moves to what's still open
        self.assertIn("p2 · command", text_of(m.cards["p2"]))
        self.assertNotIn("/run p2", text_of(m.cards["p2"]))
        m.submit("/decline p2")
        self.assertIn("p2 is declined", text_of(m.blocks[-1]))
        m.submit("/decline p1")
        m.run_started(m.cards["p1"])                             # run after all: the model isn't told
        m.outbox.clear()
        m.submit("ok, something else")
        self.assertEqual([x["type"] for x in m.outbox], ["attach", "user"])
        self.assertEqual(m.outbox[0]["label"], "declined p2")
        self.assertIn("did not run it:\nrm -rf build2", m.outbox[0]["text"])
        self.assertNotIn("decline", m.outbox[0])

    def test_copy_a_message_and_its_reply(self):
        m = C.Model()
        self.hello(m)
        m.shell = object()
        for text, answer in (("first\nquestion", "one"), ("second", "two\x1b[2J")):
            m.submit(text)
            m.handle({"type": "text", "text": "Here is "})
            m.handle({"type": "text", "text": answer})
            m.handle({"type": "proposal", "id": f"p{m.turn}", "kind": "read", "path": "x", "auto": True})
            m.handle({"type": "text", "text": "and more"})
            m.handle({"type": "done", "reason": "reply", "context": {"used": 1, "limit": 9}})
        self.assertEqual(m.feature("copy").turn_text(m, 1), "> first\n> question\n\nHere is one\n\nand more\n")
        self.assertEqual(m.feature("copy").turn_text(m, 2),
                         "> second\n\nHere is two\\x1b[2J\n\nand more\n")                 # escaped, as shown
        self.assertIsNone(m.feature("copy").turn_text(m, 3))
        m.submit("/copy")
        self.assertEqual(m.actions[-1][:2], ("copy", 2))
        m.submit("/copy 1")
        self.assertEqual(m.actions[-1], ("copy", 1, m.feature("copy").turn_text(m, 1)))
        m.submit("/copy 7")
        self.assertIn("turn 7 isn't on screen", text_of(m.blocks[-1]))

    def test_newest_commands_get_alt_keys(self):
        m = C.Model()
        self.hello(m)
        for i in range(1, 12):
            m.handle({"type": "proposal", "id": f"p{i}", "kind": "command", "command": f"echo {i}"})
            m.handle({"type": "proposal", "id": f"r{i}", "kind": "read", "path": "x", "auto": True})
        self.assertEqual([m.keyed(n).id for n in (1, 2, 9)], ["p11", "p10", "p3"])
        self.assertIsNone(m.cards["p2"].key)
        self.assertIsNone(m.keyed(10))
        head = text_of(m.cards["p11"])
        self.assertIn("p11 · command · Alt+1", head)
        self.assertIn("click it, press Alt+1, or /run p11", head)

    def test_model_picker(self):
        m = C.Model()
        self.hello(m, features=["models"])
        m.shell = object()
        m.submit("/model")
        self.assertEqual(m.outbox[-1], {"type": "models"})
        m.handle({"type": "models", "current": "m", "models": [{"id": "coder-model"}, {"id": "x#{evil}"},
                                                                 {"id": "llama"}, "junk"]})
        self.assertEqual((m.feature("model").models, m.actions[-1]), (["coder-model", "llama"], ("model_menu",)))
        m.request({"type": "model", "n": 1})                       # the menu sends a position, never a name
        self.assertEqual(m.outbox[-1], {"type": "model", "model": "llama"})
        m.request({"type": "model", "n": 7})
        self.assertEqual(m.outbox[-1], {"type": "model", "model": "llama"})       # out of range: nothing
        m.handle({"type": "model", "endpoint": {"name": "e", "model": "llama", "base_url": "u"},
                  "context": {"used": 5, "limit": 8000}})
        self.assertEqual((m.snapshot()["session"]["model"], m.context), ("llama", (5, 8000)))
        self.assertIn("model: llama", text_of(m.blocks[-1]))
        old = C.Model()
        self.hello(old)
        old.submit("/model")
        self.assertIn("can't list or switch models", text_of(old.blocks[-1]))

    def test_model_menu_items_carry_only_positions(self):
        sh = C.Shell("%1")
        with mock.patch.object(C.subprocess, "Popen") as popen:
            C.Model().feature("model").menu(sh, ["coder-model", "bad#{x}"] + [f"m{i}" for i in range(25)], "coder-model")
        argv = popen.call_args[0][0]
        self.assertIn("• coder-model", argv)
        self.assertNotIn("bad#{x}", " ".join(argv))
        self.assertIn("-  7 more: /model NAME", argv)
        picks = [a for a in argv if "request.py" in a]
        self.assertEqual(len(picks), 19)
        self.assertTrue(all(re.search(r"model [0-9]+\\?\"$", a) for a in picks), picks[:2])

    def test_remote_banner_says_where_commands_run(self):
        m = C.Model()
        m.shell_where = "OUTSIDE any sandbox, directly on this host"
        self.hello(m, remote=True, host="login1", user="svc")
        banner = text_of(m.blocks[0], 120)
        self.assertIn("Commands you approve run ON ANOTHER HOST, login1, over ssh", banner)
        self.assertNotIn("directly on this host", banner)

    def test_trusted_command_card_shows_what_ran_and_its_output(self):
        m = C.Model()
        self.hello(m)
        m.submit("nodes?")
        m.handle({"type": "proposal", "id": "p1", "kind": "trusted", "auto": True, "plugin": "slurm",
                  "command": "down_nodes", "read_only": True, "argv": "sinfo -R -h", "reason": "down nodes"})
        card = m.cards["p1"]
        self.assertEqual(card.state, "running")
        m.handle({"type": "result", "id": "p1", "status": "ok", "output": "cn042|down*\x1b[2J", "exit": 0})
        text = text_of(card, 80)
        self.assertIn("p1 · slurm · down_nodes · read-only", text)
        self.assertIn("$ sinfo -R -h", text)
        self.assertIn("│ cn042|down*\\x1b[2J", text)                     # output escaped, as everywhere

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
        m.handle({"type": "proposal", "id": "p5", "kind": "read", "path": ".env", "digest": "0123456789abcdef"})
        m.decide(True)                                   # hoplon's digest goes back with the answer, unchanged
        self.assertEqual(m.outbox[-1], {"type": "decision", "id": "p5", "decision": "accept",
                                        "digest": "0123456789abcdef"})
        self.assertEqual((m.cards["p1"].state, m.cards["p2"].state), ("done", "declined"))
        self.assertIn("secrets", text_of(m.cards["p2"]))
        self.assertIn("$ rm -rf ~", text_of(m.cards["p4"]))
        self.assertIn("hoplon never runs it", text_of(m.cards["p4"]))

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
        self.assertTrue(any("usage: /skill" in text_of(b) for b in m.blocks))
        self.assertEqual(m.outbox[2], {"type": "user", "text": "/not a command"})
        m.submit("/coding off")                                   # busy now: waits
        self.assertEqual(len(m.outbox), 3)
        m.submit("/quit")
        self.assertTrue(m.quit)

    def test_run_and_share_need_the_shell_pane(self):
        m = C.Model()
        m.handle({"type": "proposal", "id": "p1", "kind": "command", "command": "squeue"})
        m.submit("/run")
        self.assertIsNone(m.ask)
        self.assertIn("hoplon-tui", text_of(m.blocks[-1]))
        m.shell = True
        m.submit("/run p9")
        self.assertIn("no command proposal to run with id p9", text_of(m.blocks[-1]))
        m.submit("/run")
        self.assertEqual((m.ask["kind"], m.ask["command"]), ("run", "squeue"))
        m.submit("/share")
        self.assertEqual(m.actions, [("share", "last")])

    def test_share_attaches_then_says_so(self):
        m = C.Model()
        m.share("output of p1 (exit 0)", "line one\nline two", "Here is the output of p1.")
        self.assertEqual(m.outbox, [{"type": "attach", "label": "output of p1 (exit 0)", "text": "line one\nline two"},
                                    {"type": "user", "text": "Here is the output of p1."}])
        self.assertIn("[attached: output of p1 (exit 0), 2 lines]", text_of(m.blocks[-1]))

    def test_queued_share_stays_with_its_own_message(self):
        m = C.Model()
        m.submit("first")
        m.submit("older queued prompt")
        m.share("shell output", "reviewed", "share")
        self.assertEqual(m.outbox, [{"type": "user", "text": "first"}])
        m.handle({"type": "done"})
        self.assertEqual(m.outbox[-1], {"type": "user", "text": "older queued prompt"})
        m.handle({"type": "done"})
        self.assertEqual(m.outbox[-2:], [{"type": "attach", "label": "shell output", "text": "reviewed"},
                                         {"type": "user", "text": "share"}])

    def test_reviewed_context_stays_with_the_next_submitted_prompt(self):
        m = C.Model()
        m.submit("first")
        m.submit("older queued prompt")
        m.attach("context", "reviewed")
        m.submit("explain that context")
        m.handle({"type": "done"})
        self.assertEqual(m.outbox[-1], {"type": "user", "text": "older queued prompt"})
        m.handle({"type": "done"})
        self.assertEqual(m.outbox[-2:], [{"type": "attach", "label": "context", "text": "reviewed"},
                                         {"type": "user", "text": "explain that context"}])

    def test_review_buffer_matches_hoplon(self):
        buf = C.review_buffer("ls", 2, "OUTSIDE any sandbox, directly on this host", "x")
        self.assertIn("I ran this command OUTSIDE any sandbox, directly on this host (exit 2):\n```\nls\n```\nOutput:", buf)
        self.assertEqual(C.strip_comments("# a\nkeep\n#b\n"), "keep")
        self.assertEqual(C.strip_comments("# header\n# more\nline\n## A heading\n# kept\n", top_only=True),
                         "line\n## A heading\n# kept")

    def test_session_destination_confirmation_is_forwarded(self):
        args = C.parse_args(["-S", "saved", "--allow-session-endpoint-change"])
        self.assertEqual(C.hoplon_args(args), ["-S", "saved", "--allow-session-endpoint-change"])

    def test_options_split_between_the_tui_and_hoplon(self):
        a = C.parse_args(["-p", "hpc", "-S", "night", "-k", "slurm", "-k", "draft", "--coding", "--no-panel"])
        self.assertEqual(C.hoplon_args(a), ["-p", "hpc", "-S", "night", "-k", "slurm", "-k", "draft", "--coding"])
        self.assertEqual(C.hoplon_args(C.parse_args(["--coding", "--coding-anywhere"])), ["--coding-anywhere"])
        with self.assertRaises(SystemExit):
            C.parse_args(["--json"])                              # hoplon's own wrapper flags aren't options here

    def test_panel_snapshot_and_requests(self):
        m = C.Model()
        m.handle({"type": "hello", "version": "1", "endpoint": {"name": "e", "model": "m"}, "sandbox": {"name": "podman"},
                  "features": ["skills"]})
        self.assertEqual(m.outbox, [{"type": "skills"}])            # asked for quietly, for the panel
        m.handle({"type": "skills", "skills": [{"name": "slurm", "status": "blessed", "active": False}]})
        self.assertEqual(len(m.blocks), 1)                         # not printed in the conversation
        m.handle({"type": "proposal", "id": "p1", "kind": "read", "path": "a\x1b[2Jb"})
        snap = m.snapshot()
        self.assertEqual(snap["session"]["sandbox"], "podman")
        self.assertEqual(snap["skills"][0]["name"], "slurm")
        self.assertEqual(snap["proposals"][0]["title"], "read a\\x1b[2Jb")
        m.outbox = []
        for bad in ({"type": "skill", "name": "x"}, {"type": "skill", "name": "a\nb", "on": True},
                    {"type": "decision", "id": "p1", "decision": "accept"}, {"type": "coding", "on": "yes"}, "junk"):
            m.request(bad)
        self.assertEqual(m.outbox, [])                             # the panel can't approve or send anything odd
        m.request({"type": "coding", "on": True})
        m.request({"type": "skill", "name": "slurm", "on": True})
        self.assertEqual(m.outbox, [{"type": "coding", "on": True}, {"type": "skill", "name": "slurm", "on": True}])

    def test_share_and_mark_commands(self):
        m = C.Model()
        m.shell = True
        for line in ("/share", "/share mark", "/share all", "/mark", "/share 12", "/share #3", "/commands",
                     "/share bogus"):
            m.submit(line)
        self.assertEqual(m.actions, [("share", "last"), ("share", "mark"), ("share", "all"), ("mark",),
                                     ("share_cmd", 12), ("share_cmd", 3), ("commands",)])
        self.assertIn("usage: /share", text_of(m.blocks[-1]))
        m.actions = []
        for msg in ({"type": "share", "what": "mark"}, {"type": "command", "n": 4}, {"type": "command", "n": "4"},
                    {"type": "command", "n": True}, {"type": "mark", "on": True}, {"type": "mark", "on": False}):
            m.request(msg)
        self.assertEqual(m.actions, [("share", "mark"), ("share_cmd", 4), ("mark_line",), ("unmark",)])


PLAN_STEPS = [{"n": 1, "kind": "read", "path": "a.txt", "why": "look"},
              {"n": 2, "kind": "edit", "path": "a.txt", "why": "fix"},
              {"n": 3, "kind": "command", "command": "make test", "why": "check"},
              {"n": 4, "kind": "command", "command": "rm -rf build", "why": "clean"}]


class TestSandboxRuns(unittest.TestCase):
    RUNNER = {"engine": "podman", "image": "dev:1", "network": False, "timeout": 300, "problem": None}

    def model(self, **runner):
        m = C.Model()
        m.handle({"type": "hello", "version": "1", "endpoint": {"name": "e", "model": "m", "base_url": "u"},
                  "sandbox": None, "coding": True, "runner": dict(self.RUNNER, **runner)})
        return m

    def test_banner_says_the_model_runs_things_offline(self):
        self.assertIn("offline", text_of(self.model().blocks[0], 200))
        self.assertIn("WITH the network", text_of(self.model(network=True).blocks[0], 200))
        self.assertIn("can't run here: podman is not installed",
                      text_of(self.model(problem="podman is not installed").blocks[0], 200))

    def test_an_offline_run_shows_its_command_then_the_end_of_its_output(self):
        m = self.model()
        m.submit("test it")
        m.handle({"type": "proposal", "id": "p1", "kind": "sandbox", "command": "make test\x1b[2J", "reason": "check",
                  "network": False, "image": "dev:1", "auto": True})
        card = m.cards["p1"]
        self.assertEqual((card.state, m.pending), ("running", []))
        self.assertIn("sandbox · offline", text_of(card))
        self.assertIn("$ make test\\x1b[2J", text_of(card))
        m.handle({"type": "result", "id": "p1", "status": "error", "detail": "exit 1 in 2.0 s", "exit": 1,
                  "output": "".join(f"line {i}\n" for i in range(20)) + "\x1b]0;t\x07"})
        shown = text_of(card, 80)
        self.assertEqual(card.state, "error")
        self.assertIn("13 earlier lines of output", shown)
        self.assertIn("line 19", shown)
        self.assertNotIn("line 11\n", shown)
        self.assertNotIn("\x1b", shown)
        self.assertIn("line 0", card.full_text())                 # v shows it all
        self.assertIn("exit 1", shown)

    def test_an_online_run_asks_and_says_why(self):
        m = self.model(network=True)
        m.submit("go")
        m.handle({"type": "proposal", "id": "p1", "kind": "sandbox", "command": "pip install x", "reason": "r",
                  "network": True, "image": "dev:1"})
        self.assertEqual([c.id for c in m.pending], ["p1"])
        self.assertIn("sandbox · ONLINE", text_of(m.cards["p1"]))
        m.decide(True)
        self.assertEqual(m.outbox[-1], {"type": "decision", "id": "p1", "decision": "accept"})

    def test_network_command_and_panel(self):
        m = self.model()
        m.submit("/network")
        self.assertIn("sandbox network is off", text_of(m.blocks[-1], 200))
        m.submit("/network on")
        self.assertEqual(m.outbox[-1], {"type": "network", "on": True})
        m.submit("/network maybe")
        self.assertEqual(m.outbox[-1], {"type": "network", "on": True})
        m.handle({"type": "network", "runner": dict(self.RUNNER, network=True), "available": True})
        self.assertIn("ON: each sandboxed run asks", text_of(m.blocks[-1], 200))
        snap = m.snapshot()
        self.assertEqual(snap["runner"]["network"], True)
        PANEL = load("panel", os.path.join(REPO, "tui", "panel.py"))
        lines, _ = PANEL.view(snap, 42, None)
        self.assertIn("model's runs  ⊡ sandboxed · ONLINE", "\n".join("".join(t for t, _ in ln) for ln in lines))
        m.submit("go")
        m.submit("/network off")
        self.assertNotEqual(m.outbox[-1], {"type": "network", "on": False})   # waits until the reply ends


class TestSandboxStatus(unittest.TestCase):
    """The SANDBOX panel section, tmux's status line, a running card's clock, and stopping a run."""
    RUNNER = TestSandboxRuns.RUNNER

    def model(self, sandbox=None, runner=RUNNER, features=("interrupt",), remote=False):
        m = C.Model()
        m.handle({"type": "hello", "version": "1", "endpoint": {"name": "e", "model": "m", "base_url": "u"},
                  "sandbox": sandbox, "coding": True, "runner": runner, "features": list(features), "remote": remote,
                  "host": "h", "user": "u", "uid": 1000, "cwd": "/w"})
        m.outbox.clear()
        return m

    def panel(self, m):
        PANEL = load("panel", os.path.join(REPO, "tui", "panel.py"))
        lines, _ = PANEL.view(m.snapshot(), 42, None)
        return "\n".join("".join(t for t, _ in ln) for ln in lines)

    def start(self, m, pid="p3"):
        m.submit("test it")
        m.handle({"type": "proposal", "id": pid, "kind": "sandbox", "command": "make", "reason": "r",
                  "network": False, "image": "dev:1", "auto": True})
        return m.cards[pid]

    def test_panel_states(self):
        self.assertIn("hoplon itself ⊡ in the podman sandbox", self.panel(self.model(sandbox={"name": "podman"})))
        text = self.panel(self.model())
        self.assertIn("hoplon itself ▲ this host", text)
        self.assertIn("model's runs  ⊡ sandboxed · offline\n              dev:1", text)
        self.assertIn("model's runs  none", self.panel(self.model(runner=None)))
        self.assertIn("model's runs  can't run: no podman",
                      self.panel(self.model(runner=dict(self.RUNNER, problem="no podman"))))
        m = self.model()
        card = self.start(m)
        card.started -= 12
        text = self.panel(m)
        self.assertIn("model's runs  ⊡ RUNNING p3 · 12s", text)
        self.assertIn("Ctrl-C or /stop stops it", text)
        m.handle({"type": "result", "id": "p3", "status": "ok", "detail": "exit 0"})
        self.assertIn("model's runs  ⊡ sandboxed", self.panel(m))

    def test_tmux_status_holds_only_fixed_words_and_a_checked_id(self):
        self.assertEqual(C.Model().tmux_status(), "")
        self.assertIn("⊡ model's runs sandboxed", self.model().tmux_status())
        self.assertIn("▲ no sandbox", self.model(runner=None).tmux_status())
        self.assertIn("⊡ sandbox", self.model(sandbox={"name": "podman"}).tmux_status())
        waiting = self.model(runner=None)
        waiting.submit("go")
        waiting.handle({"type": "proposal", "id": "p1", "kind": "write", "path": "a", "content": "x", "reason": "r",
                        "digest": "d"})
        self.assertTrue(waiting.tmux_status().startswith(" #[fg=black,bg=yellow,bold] ⏸ waiting for you"))
        waiting.decide(False)                                   # answered: no longer waiting
        self.assertNotIn("waiting for you", waiting.tmux_status())
        m = self.model()
        card = self.start(m)
        card.started -= 5
        self.assertIn("⏵ sandbox p3 5s", m.tmux_status())
        m2 = self.model()
        self.start(m2, pid="#[fg=red]x")                       # not an id hoplon would send: left out
        status = m2.tmux_status()
        self.assertNotIn("x", status.replace("fg=yellow", "").replace("#[default]", "").replace("box", ""))
        self.assertIn("⏵ sandbox 0s", status)

    def test_running_card_shows_its_clock_and_ends_stopped(self):
        m = self.model()
        card = self.start(m)
        self.assertIn("running 0s · Ctrl-C (or /stop) stops it", text_of(card, 80))
        card.started -= 7
        self.assertIn("running 7s", text_of(card, 80))
        m.handle({"type": "result", "id": "p3", "status": "stopped", "detail": "the container was killed"})
        self.assertEqual(card.state, "stopped")
        self.assertNotIn("running", text_of(card, 80))
        self.assertIn("■ stopped", text_of(card, 80))

    def test_an_approved_online_run_is_running(self):
        m = self.model(runner=dict(self.RUNNER, network=True))
        m.submit("go")
        m.handle({"type": "proposal", "id": "p1", "kind": "sandbox", "command": "pip install x", "reason": "r",
                  "network": True, "image": "dev:1"})
        self.assertIsNone(m.running_card())
        m.decide(True)
        self.assertIs(m.running_card(), m.cards["p1"])

    def test_stop_asks_for_an_interrupt_only_when_it_can(self):
        m = self.model()
        m.submit("/stop")
        self.assertIn("nothing is running", text_of(m.blocks[-1]))
        self.start(m)
        m.submit("/stop")                                     # works while hoplon is busy
        self.assertEqual(m.actions, [("interrupt",)])
        self.assertIn("stopping the sandboxed run p3", text_of(m.blocks[-1]))
        m = self.model(features=())
        self.start(m)
        m.submit("/stop")
        self.assertEqual(m.actions, [])
        self.assertIn("can't stop a sandboxed run", text_of(m.blocks[-1], 80))
        m = self.model(remote=True)
        card = self.start(m)
        m.submit("/stop")
        self.assertEqual(m.actions, [])
        self.assertIn("isn't available in remote mode yet", text_of(m.blocks[-1], 120))
        self.assertIn("can't be stopped from here in remote mode", text_of(card, 120))

    def test_stop_message_works_here_and_over_ssh(self):
        for remote in (False, True):
            m = self.model(features=("interrupt", "stop"), remote=remote)
            card = self.start(m)
            self.assertTrue(card.stoppable)
            self.assertIn("Ctrl-C (or /stop) stops it", text_of(card, 120))
            m.submit("/stop")
            self.assertEqual((m.outbox[-1], m.actions), ({"type": "stop"}, []))
            self.assertIn("stopping the sandboxed run p3", text_of(m.blocks[-1]))
            m.handle({"type": "error", "code": "not_running", "message": "nothing is running in the sandbox"})
            self.assertTrue(m.busy)                           # the run had just ended; the turn goes on
            self.assertIn("nothing is running in the sandbox now", text_of(m.blocks[-1]))

    def test_stop_with_nothing_in_the_sandbox_points_at_the_shell_pane(self):
        m = self.model(features=("interrupt", "stop"))
        m.submit("/stop")
        self.assertEqual(m.outbox, [])
        self.assertIn("A command in the shell pane stops with Ctrl-C there", text_of(m.blocks[-1], 200))

    def test_a_run_ends_when_hoplon_exits(self):
        m = self.model()
        card = self.start(m)
        m.exit(1)
        self.assertIsNone(m.running_card())
        self.assertEqual((card.state, card.detail), ("error", "hoplon exited before it finished"))
        self.assertNotIn("RUNNING", self.panel(m))
        self.assertNotIn("⏵", m.tmux_status())

    def test_prompt_pane_ctrl_c_stops_a_run(self):
        p = C_PROMPT.Prompt.__new__(C_PROMPT.Prompt)
        sent = []
        p.send = lambda msg: sent.append(msg) or True
        p.view, p.ctrl_c, p.paste = {"running": True}, 0.0, None
        p.ed = C.Editor()
        p.key("\x03")
        p.key("\x03")
        self.assertEqual(sent, [{"type": "stop"}, {"type": "stop"}])   # never a quit while a run is going
        self.assertIn("stop", IPC.ROLES["prompt"])

    def test_sandbox_command_prints_the_details(self):
        m = self.model(runner=dict(self.RUNNER, memory="4g", pids=1024))
        card = self.start(m)
        m.submit("/sandbox")
        text = "\n".join(text_of(b, 200) for b in m.blocks)
        self.assertIn("hoplon: not sandboxed", text)
        self.assertIn("sandbox/run hoplon runs it in a podman sandbox", text)
        self.assertIn("image dev:1, offline, 300s time limit, 4g memory, 1024 processes", text)
        self.assertIn(f"RUNNING {card.id}", text)
        m = self.model(sandbox={"name": "podman"}, runner=None)
        m.submit("/sandbox")
        text = "\n".join(text_of(b, 200) for b in m.blocks)
        self.assertIn("hoplon: in the podman sandbox", text)
        self.assertIn("the model's sandbox: none", text)


class TestEditsFeature(unittest.TestCase):
    def model(self, edits=None, features=("edits",)):
        m = C.Model()
        m.handle({"type": "hello", "version": "1", "endpoint": {"name": "e", "model": "m", "base_url": "u"},
                  "sandbox": None, "features": list(features), "edits": edits or {"mode": "ask", "files": []}})
        m.outbox.clear()
        return m

    def panel(self, m):
        PANEL = load("panel", os.path.join(REPO, "tui", "panel.py"))
        lines, _ = PANEL.view(m.snapshot(), 42, None)
        return "\n".join("".join(t for t, _ in ln) for ln in lines)

    def test_commands_send_and_show(self):
        m = self.model()
        m.submit("/edits")
        self.assertIn("every edit and write asks", text_of(m.blocks[-1], 200))
        self.assertNotIn("EDITS", self.panel(m))
        m.submit("/edits approved-files")
        self.assertEqual(m.outbox[-1], {"type": "edits", "mode": "approved-files"})
        m.handle({"type": "edits", "mode": "approved-files", "files": []})
        self.assertIn("none yet", text_of(m.blocks[-1], 200))
        m.handle({"type": "edits", "mode": "approved-files", "files": ["src/a.py", "b\x1b[2J"]})
        text = self.panel(m)
        self.assertIn("EDITS", text)
        self.assertIn("  src/a.py", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("/edits reset forgets them", text)
        m.submit("/edits reset")
        self.assertEqual(m.outbox[-1], {"type": "edits", "reset": True})
        m.submit("/edits maybe")
        self.assertIn("usage: /edits", text_of(m.blocks[-1], 200))
        m = self.model(features=())
        m.submit("/edits ask")
        self.assertEqual(m.outbox, [])
        self.assertIn("this hoplon can't", text_of(m.blocks[-1], 200))

    def test_an_auto_applied_edit_is_shown_in_full(self):
        m = self.model({"mode": "approved-files", "files": ["a.txt"]})
        m.submit("go")
        m.handle({"type": "proposal", "id": "p2", "kind": "edit", "path": "a.txt", "diff": "@@ -1 +1 @@\n-two\n+three\n",
                  "reason": "r", "auto": True, "approved_file": True})
        card = m.cards["p2"]
        self.assertEqual((card.state, m.pending), ("accepted", []))
        shown = text_of(card, 100)
        self.assertIn("+three", shown)
        self.assertIn("applied without asking", shown)
        m.handle({"type": "result", "id": "p2", "status": "ok", "detail": "edited a.txt"})
        self.assertEqual(card.state, "done")


class TestWorkFeature(unittest.TestCase):
    ITEMS = [{"n": 1, "title": "Core model", "status": "todo", "commit": None},
             {"n": 2, "title": "Curses \x1b[2J front end", "status": "todo", "commit": None}]

    def model(self, features=("work",)):
        m = C.Model()
        m.handle({"type": "hello", "version": "1", "endpoint": {"name": "e", "model": "m", "base_url": "u"},
                  "sandbox": None, "features": list(features)})
        m.outbox.clear()
        return m

    def panel(self, m):
        PANEL = load("panel", os.path.join(REPO, "tui", "panel.py"))
        lines, _ = PANEL.view(m.snapshot(), 42, None)
        return "\n".join("".join(t for t, _ in ln) for ln in lines)

    def shown(self, m, n=12):
        return "\n".join(text_of(b, 200) for b in m.blocks[-n:])

    def test_a_session_from_start_to_end(self):
        m = self.model()
        m.submit("/work make a game")
        self.assertEqual(m.outbox, [{"type": "work", "task": "make a game"}])
        self.assertTrue(m.busy)
        m.handle({"type": "work", "state": "started", "branch": "hoplon/work-1", "base": "abc1234def", "path": "/x",
                  "steps_per_item": 30, "time_limit": 3600, "test": None, "current": None, "items": [],
                  "role": "plan", "planner": {"endpoint": "e", "model": "big"}})
        self.assertIn("work session on branch hoplon/work-1", self.shown(m))
        self.assertIn("plans with big on e", self.shown(m))
        m.handle({"type": "proposal", "id": "p1", "kind": "work_plan", "goal": "a game",
                  "reason": "a game", "items": ["Core model", "Curses front end"], "digest": "d"})
        card = m.cards["p1"]
        self.assertEqual(m.pending, [card])
        self.assertEqual(card.title(), "work plan · 2 items")
        self.assertIn(" 2. Curses front end", text_of(card, 100))
        self.assertEqual(text_of(card, 100).count("a game"), 1)
        m.decide(True)
        self.assertEqual(m.outbox[-1]["id"], "p1")
        m.handle({"type": "work", "state": "plan", "items": self.ITEMS, "current": 1, "role": "item"})
        m.handle({"type": "proposal", "id": "p2", "kind": "write", "path": "a.py", "content": "x", "auto": True,
                  "work": True})
        self.assertIn("(work session)", text_of(m.cards["p2"], 100))
        self.assertIn("WORK", self.panel(m))
        self.assertIn("0 of 2 done · on item 1", self.panel(m))
        done = [dict(self.ITEMS[0], status="done", commit="1234567890"), self.ITEMS[1]]
        m.handle({"type": "proposal", "id": "p3", "kind": "finish", "n": 1, "summary": "core", "notes": "n",
                  "auto": True})
        self.assertIn("finish item 1: core  (work session)", text_of(m.cards["p3"], 100))
        m.handle({"type": "work", "state": "item", "items": done, "current": 2, "role": "item"})
        self.assertIn("item 1 committed as 1234567: Core model", self.shown(m))
        self.assertIn("✓  1. Core model  1234567", self.panel(m))
        self.assertIn("next, item 2 of 2", self.shown(m))
        self.assertNotIn("\x1b", self.panel(m))
        m.handle({"type": "work", "state": "context", "role": "review", "item": 2, "why": "the item used its steps"})
        self.assertIn("the planner, to review item 2", self.shown(m))
        self.assertIn("1 of 2 done · planner reviews item 2", self.panel(m))
        m.handle({"type": "done", "reason": "work_limit"})
        self.assertIn("paused at a limit", self.shown(m))
        self.assertFalse(m.busy)
        m.submit("/work")
        self.assertIn("1 of 2 done", self.shown(m))
        m.submit("/work end")
        self.assertEqual(m.outbox[-1], {"type": "work_end"})
        m.handle({"type": "work", "state": "ended", "why": "ended", "branch": "hoplon/work-1", "base": "abc1234def",
                  "commits": ["1234567 1. core"], "stat": " 1 file changed", "unfinished": "89abcdef",
                  "commands": ["git merge hoplon/work-1"], "items": done})
        text = self.shown(m)
        self.assertIn("the work is on branch hoplon/work-1", text)
        self.assertIn("committed as 89abcde", text)
        self.assertIn("git merge hoplon/work-1", text)
        self.assertNotIn("WORK", self.panel(m))
        m.submit("/work")
        self.assertIn("the last work session ended", self.shown(m, 1))

    def test_refusals_and_waiting(self):
        m = self.model()
        m.submit("/work end")
        self.assertIn("no work session is on", self.shown(m, 1))
        m.submit("/work t")
        m.handle({"type": "error", "code": "work_refused", "message": "uncommitted changes"})
        self.assertIn("no work session: uncommitted changes", self.shown(m, 1))
        self.assertFalse(m.busy)
        m.submit("hi")
        m.outbox.clear()
        m.submit("/work t")
        self.assertEqual(m.outbox, [])
        self.assertIn("has to wait", self.shown(m, 1))
        m = self.model(features=())
        m.submit("/work t")
        self.assertEqual(m.outbox, [])
        self.assertIn("this hoplon can't run work sessions", self.shown(m, 1))

    def test_work_end_and_stop_reach_a_busy_hoplon_that_can_halt(self):
        m = self.model(features=("work", "stop", "halt"))
        m.submit("/work t")
        m.handle({"type": "work", "state": "started", "branch": "hoplon/work-1", "base": "abc", "items": []})
        self.assertTrue(m.busy)
        m.stop()
        self.assertEqual(m.outbox[-1], {"type": "stop"})
        self.assertIn("stopping hoplon's turn", self.shown(m, 1))
        m.submit("/work end")
        self.assertEqual(m.outbox[-1], {"type": "work_end"})
        m.handle({"type": "work", "state": "ended", "why": "ended", "branch": "hoplon/work-1", "path": "/s/copy",
                  "problems": ["what was left could not be committed: git add failed"], "commits": "not a list"})
        text = self.shown(m)
        self.assertIn("could not be committed", text)
        self.assertIn("the copy is still at /s/copy", text)

    def test_a_finish_test_runs_with_a_clock_and_stops(self):
        m = self.model(features=("work", "stop"))
        m.handle({"type": "proposal", "id": "p1", "kind": "finish", "n": 1, "summary": "s", "test": "make test",
                  "auto": True})
        self.assertIs(m.running_card(), m.cards["p1"])
        m.stop()
        self.assertEqual(m.outbox[-1], {"type": "stop"})

    def test_odd_event_fields_are_shown_safely(self):
        m = self.model()
        m.handle({"type": "work", "state": "started", "branch": "b\nFAKE", "items": "nope", "time_limit": "x"})
        m.handle({"type": "work", "state": "plan", "current": "1", "role": "item",
                  "items": [{"n": 1, "title": "one\n✓  2. fake", "status": "todo"}, "junk", 3]})
        panel = self.panel(m)
        self.assertIn("one⏎✓  2. fake", panel)
        self.assertIn("b⏎FAKE", panel)
        self.assertNotIn("\n✓  2. fake", panel)
        m.handle({"type": "work", "state": "item", "current": 1, "items": [{"n": "1", "status": "done"}]})
        self.assertIn("✓  ?. ", self.panel(m))
        m.handle({"type": "proposal", "id": "p1", "kind": "work_plan", "goal": "g", "items": ["a\nb"], "digest": "d"})
        self.assertIn(" 1. a⏎b", text_of(m.cards["p1"], 100))


    def test_a_path_with_a_line_break_stays_on_one_row(self):
        m = self.model()
        m.handle({"type": "proposal", "id": "p1", "kind": "write", "path": "a\nb.py", "content": "x", "auto": True,
                  "work": True})
        self.assertEqual(m.cards["p1"].title(), "create a⏎b.py")
        m.handle({"type": "proposal", "id": "p2", "kind": ["odd"], "items": 3, "digest": "d"})
        self.assertEqual(m.cards["p2"].title(), "['odd']")

    def test_a_stopped_turn_leaves_nothing_waiting_or_running(self):
        m = self.model(features=("work", "halt", "stop"))
        m.busy = True
        m.handle({"type": "proposal", "id": "p1", "kind": "work_plan", "goal": "g", "reason": "g",
                  "items": ["a"], "digest": "d"})
        m.handle({"type": "proposal", "id": "p2", "kind": "finish", "n": 1, "summary": "s", "auto": True,
                  "test": "make test"})
        self.assertTrue(m.pending and m.running_card())
        m.handle({"type": "done", "reason": "stopped"})
        self.assertEqual(m.pending, [])
        self.assertIsNone(m.running_card())
        self.assertEqual((m.cards["p1"].state, m.cards["p2"].state), ("stopped", "stopped"))
        m.handle({"type": "proposal", "id": "p3", "kind": "write", "path": "x", "content": "x", "auto": True})
        m.handle({"type": "result", "id": "p3", "status": "skipped", "detail": "not run"})
        self.assertEqual(m.cards["p3"].state, "stopped")

    def test_odd_work_fields_dont_break_the_panel(self):
        m = self.model()
        m.handle({"type": "work", "state": "started", "branch": "b", "items": [], "role": "plan"})
        m.handle({"type": "work", "state": "context", "role": ["item"], "item": 1, "why": "w"})
        m.handle({"type": "work", "state": "item", "items": [{"n": 1, "title": "t", "status": ["done"]}],
                  "current": 1})
        self.assertIn("WORK", self.panel(m))
        m.submit("/work")
        self.assertIn("1. t", self.shown(m))

    def test_how_a_session_ended_is_told_apart(self):
        m = self.model()
        m.handle({"type": "work", "state": "started", "branch": "hoplon/work-1", "base": "abc", "items": []})
        m.handle({"type": "work", "state": "ended", "why": "ended", "branch": None, "path": "/s/copy/tree",
                  "problems": ["1 file the model wrote is not in its commits"], "commits": ["abc one"]})
        text = self.shown(m)
        self.assertIn("the work is not on a branch; it is in the copy at /s/copy/tree", text)
        self.assertNotIn("nothing was changed", text)
        m.submit("/work")
        self.assertIn("the work is in the copy at /s/copy/tree", self.shown(m, 1))
        m.handle({"type": "work", "state": "started", "branch": "hoplon/work-2", "base": "abc", "items": []})
        m.handle({"type": "work", "state": "ended", "why": ["odd"], "branch": None, "path": None})
        self.assertIn("nothing was changed, so no branch was made", self.shown(m, 1))

    def test_work_end_reaches_a_session_still_starting(self):
        m = self.model(features=("work", "stop", "halt"))
        m.submit("/work t")
        m.submit("/work end")                           # hoplon hasn't said started yet
        self.assertEqual(m.outbox[-1], {"type": "work_end"})
        m.handle({"type": "error", "code": "not_running", "message": "no work session is on"})
        self.assertIn("no work session is on", self.shown(m, 1))
        self.assertNotIn("sandbox", self.shown(m, 1))

    def test_hoplon_exiting_mid_session_says_where_the_work_is(self):
        m = self.model()
        m.handle({"type": "work", "state": "started", "branch": "hoplon/work-1", "base": "abc", "path": "/s/c/tree",
                  "items": []})
        m.exit(1)
        self.assertIn("the copy is at /s/c/tree", self.shown(m))
        self.assertNotIn("WORK", self.panel(m))
        m.submit("/work end")
        self.assertIn("no work session is on", self.shown(m, 1))

    def test_a_failed_finish_test_shows_its_output(self):
        m = self.model()
        m.handle({"type": "proposal", "id": "p1", "kind": "finish", "n": 1, "summary": "s", "test": "make test",
                  "auto": True})
        m.handle({"type": "result", "id": "p1", "status": "error", "detail": "the test command failed (exit 1)",
                  "output": "FAILED test_core"})
        self.assertIn("FAILED test_core", text_of(m.cards["p1"], 100))

    def test_ctrl_c_in_the_prompt_pane_stops_a_turn_even_during_a_question(self):
        p = C_PROMPT.Prompt.__new__(C_PROMPT.Prompt)
        sent = []
        p.send = lambda msg: sent.append(msg) or True
        p.view, p.ctrl_c, p.paste, p.asked_at = {"mode": "approval", "halt": True}, 0.0, None, 0.0
        p.ed = C.Editor()
        p.key("\x03")
        p.view = {"mode": "input", "busy": True, "halt": True}
        p.key("\x03")
        p.key("\x03")
        self.assertEqual(sent, [{"type": "stop"}, {"type": "stop"}, {"type": "quit"}])


class TestPluginsFeature(unittest.TestCase):
    ROWS = [{"name": "slurm", "description": "cluster", "enabled": True, "blessed": True, "status": "blessed",
             "pane": "slurm.py", "dir": "/opt/hoplon/plugins/slurm",
             "commands": [{"name": "queue", "describe": "the queue", "read_only": True, "params": {}},
                          {"name": "cancel", "describe": "cancel a job", "read_only": False, "params": {}}]},
            {"name": "off", "description": "not on", "enabled": False, "blessed": True, "pane": None, "dir": "/x",
             "commands": [{"name": "x", "read_only": True}]}]

    def model(self, remote=False):
        m = C.Model()
        m.handle({"type": "hello", "version": "1", "endpoint": {"name": "e", "model": "m", "base_url": "u"},
                  "sandbox": None, "features": ["plugins"], "remote": remote})
        m.handle({"type": "plugins", "plugins": self.ROWS})
        m.outbox.clear()
        return m

    def notes(self, m, n=6):
        return "\n".join(text_of(b, 200) for b in m.blocks[-n:])

    def test_list_switch_and_pane(self):
        m = self.model()
        m.submit("/plugins")
        self.assertIn("[on ] slurm: cluster · pane: /plugins slurm pane", self.notes(m))
        self.assertIn("[off] off: not on", self.notes(m))
        m.submit("/plugins off on")
        self.assertEqual(m.outbox[-1], {"type": "plugin", "name": "off", "on": True})
        m.submit("/plugins slurm pane")
        self.assertEqual(m.actions[-1], ("plugin_pane", "slurm", "/opt/hoplon/plugins/slurm/slurm.py"))
        m.submit("/plugins off pane")
        self.assertIn("switch it on first", self.notes(m, 1))
        m.submit("/plugins nope on")
        self.assertIn("no plugin named nope", self.notes(m, 1))
        m = self.model(remote=True)
        m.submit("/plugins slurm pane")
        self.assertIn("don't run in remote mode yet", self.notes(m, 1))

    def test_a_pane_runs_its_own_read_only_commands_and_hears_the_answer(self):
        m = self.model()
        f = m.feature("plugins")
        f.request(m, "plugin:slurm", {"type": "run", "id": "s1", "command": "queue", "params": {}})
        self.assertEqual(m.outbox[-1], {"type": "run", "id": "pl1", "plugin": "slurm", "command": "queue",
                                        "params": {}})
        m.handle({"type": "ran", "id": "pl1", "exit": 0, "output": "1|a\n"})
        self.assertEqual(m.panes[-1], ("plugin:slurm", {"type": "ran", "id": "s1", "exit": 0, "output": "1|a\n"}))
        m.handle({"type": "ran", "id": "pl1", "exit": 0, "output": "again"})       # answered once only
        self.assertEqual(len(m.panes), 1)
        m.panes.clear()
        for role, msg, why in (("plugin:slurm", {"type": "run", "id": 2, "command": "cancel"}, "only read-only"),
                               ("plugin:slurm", {"type": "run", "id": 3, "command": "x"}, "no such command"),
                               ("plugin:slurm", {"type": "run", "id": 4, "command": "queue", "params": {"a": 1}},
                                "no such command")):
            f.request(m, role, msg)
            self.assertIn(why, m.panes[-1][1]["error"])
        f.request(m, "plugin:off", {"type": "run", "id": 5, "command": "x"})
        self.assertEqual(m.panes[-1], ("plugin:off", {"type": "refused", "why": "this plugin is not on"}))
        self.assertEqual(len([o for o in m.outbox if o["type"] == "run"]), 1)

    def test_insert_and_attach(self):
        m = self.model()
        f = m.feature("plugins")
        f.request(m, "plugin:slurm", {"type": "insert", "text": "12345\x1b[2J"})
        self.assertEqual(m.panes[-1], ("prompt", {"type": "insert", "text": C.clean("12345\x1b[2J")}))
        f.request(m, "plugin:slurm", {"type": "attach", "label": "job 7", "text": "JobId=7"})
        self.assertEqual(m.actions[-1], ("plugin_attach", "slurm", "job 7", "JobId=7"))
        f.request(m, "plugin:slurm", {"type": "attach", "text": "x" * 70000})
        self.assertEqual(len(m.actions), 1)

    def test_help_and_panel_list_the_tools(self):
        m = self.model()
        m.submit("/help")
        text = "\n".join(text_of(b, 200) for b in m.blocks)
        self.assertIn("slurm: queue (read-only) the queue", text)
        self.assertIn("slurm: cancel (asks) cancel a job", text)
        self.assertNotIn("off: x", text)
        PANEL = load("panel", os.path.join(REPO, "tui", "panel.py"))
        lines, _ = PANEL.view(m.snapshot(), 50, None)
        shown = "\n".join("".join(t for t, _ in ln) for ln in lines)
        self.assertIn("PLUGINS", shown)
        self.assertIn("slurm · /plugins slurm pane", shown)
        self.assertIn("  queue, cancel", shown)


class TestPlan(unittest.TestCase):
    def plan_model(self):
        m = C.Model()
        m.shell = True
        m.handle({"type": "proposal", "id": "p1", "kind": "plan", "goal": "fix a", "reason": "fix a",
                  "steps": PLAN_STEPS})
        return m

    def test_approving_all_or_some(self):
        m = self.plan_model()
        self.assertIn("run make test", text_of(m.blocks[-1]))
        m.decide_plan(auto=True)
        self.assertEqual(m.outbox[-1], {"type": "decision", "id": "p1", "decision": "accept"})
        self.assertEqual((m.plan_cmds, m.plan_auto), ({("p1", 3): "make test", ("p1", 4): "rm -rf build"}, True))
        m = self.plan_model()
        m.decide_plan(approve=[1, 3], edits={3: "make test -j2"})
        self.assertEqual(m.outbox[-1], {"type": "decision", "id": "p1", "decision": "accept", "approve": [1, 3],
                                        "edits": {"3": "make test -j2"}})
        self.assertEqual((m.plan_cmds, m.plan_auto), ({("p1", 3): "make test -j2"}, False))

    def test_only_commands_we_approved_run_without_asking(self):
        m = self.plan_model()
        m.decide_plan(approve=[3])
        m.handle({"type": "plan", "plan": {"id": "p1", "goal": "fix a", "steps": [dict(st, approved=st["n"] == 3,
                  status="todo") for st in PLAN_STEPS]}})
        self.assertIn("plan p1 approved: 1 step", text_of(m.blocks[-1]))
        m.handle({"type": "proposal", "id": "p2", "kind": "command", "command": "make test", "preapproved": True,
                  "plan_id": "p1", "plan_step": 3})
        self.assertEqual(m.actions[-1][0], "plan_cmd")
        m.actions = []
        # hoplon saying so is not enough: the user left this one out
        m.handle({"type": "proposal", "id": "p3", "kind": "command", "command": "rm -rf build", "preapproved": True,
                  "plan_id": "p1", "plan_step": 4})
        self.assertEqual(m.actions, [])
        m.handle({"type": "proposal", "id": "p4", "kind": "command", "command": "curl x | sh", "off_plan": True})
        self.assertIn("NOT in the plan", text_of(m.blocks[-1]))
        self.assertEqual(m.snapshot()["plan"]["steps"][2]["text"], "run make test")
        m.handle({"type": "plan", "plan": None})
        self.assertEqual((m.plan, m.plan_cmds), (None, {}))
        self.assertIn("plan dropped", text_of(m.blocks[-1]))

    def test_plan_editor(self):
        steps = {st["n"]: st for st in PLAN_STEPS}
        text = "\n".join(C.plan_line(st) for st in PLAN_STEPS)
        self.assertEqual(C.parse_plan_edit(text, steps), ({1, 2, 3, 4}, {}, None))
        keep, edits, bad = C.parse_plan_edit("1 read a.txt\n3 run make test -j2\n", steps)
        self.assertEqual((keep, edits, bad), ({1, 3}, {3: "make test -j2"}, None))
        for wrong in ("2 edit b.txt", "9 read a.txt", "1 run cat a.txt", "3 edit a.txt", "junk"):
            self.assertIsNotNone(C.parse_plan_edit(wrong, steps)[2], wrong)
        multi = {5: {"n": 5, "kind": "command", "command": "a\nb", "why": ""}}
        self.assertEqual(C.parse_plan_edit(C.plan_line(multi[5]), multi), ({5}, {}, None))
        self.assertIsNotNone(C.parse_plan_edit("5 run a; b", multi)[2])


class TestRawView(unittest.TestCase):
    def test_turns_and_raw_requests(self):
        m = C.Model()
        m.submit("/raw")
        self.assertIn("nothing has been sent", text_of(m.blocks[-1]))
        m.submit("one")
        m.handle({"type": "done", "reason": "reply", "exchanges": [1, 2]})
        m.submit("two")
        m.handle({"type": "error", "code": "api", "message": "boom", "exchanges": [3, 3]})
        self.assertEqual(m.exchanges, {1: [1, 2], 2: [3, 3]})
        self.assertEqual([b.turn for b in m.blocks if isinstance(b, C.UserMsg)], [1, 2])
        m.outbox = []
        for line in ("/raw", "/raw 1", "/raw all", "/system", "/raw 9", "/raw x"):
            m.submit(line)
        self.assertEqual(m.outbox, [{"type": "trace", "from": 3, "to": 3}, {"type": "trace", "from": 1, "to": 2},
                                    {"type": "trace", "from": 1, "to": 1 << 30}, {"type": "system"}])
        self.assertIn("usage: /raw", text_of(m.blocks[-1]))
        m.outbox = []
        m.request({"type": "raw", "turn": 1})
        m.request({"type": "raw", "turn": "last"})
        m.request({"type": "raw", "turn": "1; rm"})
        self.assertEqual(m.outbox, [{"type": "trace", "from": 1, "to": 2}, {"type": "trace", "from": 3, "to": 3}])
        m.handle({"type": "trace", "exchanges": []})
        self.assertEqual(m.actions[-1][0], "show")

    def test_raw_waits_for_a_reply_to_finish(self):
        m = C.Model()
        m.submit("go")
        m.outbox = []
        m.submit("/raw")
        self.assertEqual(m.outbox, [])
        m.handle({"type": "proposal", "id": "p1", "kind": "write", "path": "x", "content": "y"})
        m.submit("/raw")                                   # hoplon answers while a proposal waits
        self.assertEqual(m.outbox, [{"type": "trace"}])

    def test_rendered_exchange(self):
        ev = {"type": "trace", "exchanges": [{
            "n": 3, "time": "t", "method": "POST", "url": "https://e/v1/chat/completions", "ms": 1500,
            "headers": {"Authorization": "Bearer (your API key, not shown)"}, "http": 200, "stream": True,
            "request": json.dumps({"messages": [{"role": "user", "content": "hi\u202e"}]}),
            "response": ['data: {"choices":[{"delta":{"content":"he\u001b[2Jllo"}}]}', "data: [DONE]"],
            "status": "ok", "message": {"role": "assistant", "content": "hello"}, "usage": {"total_tokens": 9}}]}
        text = RAW.render_trace(ev, {2: [3, 3]})
        self.assertIn("Exchange 3 · turn 2 · t · 1.5 s · ok", text)
        self.assertIn("Authorization: Bearer (your API key, not shown)", text)
        self.assertIn('"content": "hi\\u202e"', text)          # escaped, never raw
        self.assertIn("streamed, 2 lines", text)
        self.assertIn('"total_tokens": 9', text)
        self.assertNotIn("\u202e", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("no longer has", RAW.render_trace({"exchanges": [], "kept": [5, 34]}, {}))
        sys_text = RAW.render_system({"system": "You are hoplon", "tools": [{"name": "t"}], "model": "m"})
        self.assertIn("You are hoplon", sys_text)


def mark(kind, *args, nonce="0123456789abcdef"):
    return f"\x1b]6973;{nonce};{kind}{''.join(';' + a for a in args)}\x07".encode()


def done(rc, cmd, nonce="0123456789abcdef"):
    import base64
    return mark("D", str(rc), "1" if cmd is not None else "0", base64.b64encode((cmd or "").encode()).decode(), "1",
                nonce=nonce)


class TestRecorder(unittest.TestCase):
    """What the shell pane printed, split into commands by the prompt hook's markers (tui/recorder.py)."""

    def test_commands_and_their_output(self):
        r = REC.Recorder()
        r.feed(b"motd from bashrc\r\n" + mark("H") + b"$ " + done(0, None))   # the first prompt: nothing ran
        r.feed(b"make\r\n" + mark("S") + b"building\r\nok\r\n" + done(0, "make") + b"$ ")
        r.feed(b"\r\n" + done(130, None))                                     # Ctrl-C at an empty prompt
        r.feed(mark("S") + b"secret stuff\r\n" + done(1, None))               # typed with a leading space
        self.assertEqual([(c.n, c.cmd, c.in_history, c.rc, c.text()) for c in r.finished()],
                         [(1, "make", True, 0, "building\nok"), (2, "", False, 1, "secret stuff")])
        self.assertEqual(REC.label(r.get(2)), "(a command kept out of your history)")
        self.assertEqual(r.done, 2)

    def remote_session(self, r, sid="5" * 32, dest="me@hpc1"):
        """The local shell announces a hoplon-ssh session, and the remote shell asks for its key."""
        import base64
        r.feed(mark("H") + mark("S") + mark("X", sid, base64.b64encode(dest.encode()).decode()))
        r.feed(mark("R", sid, nonce="ssh"))
        return r.take_handshake()

    def rdone(self, rc, cmd, key, cwd="/scratch/me", host="login1\x1b[2J"):
        import base64
        b = lambda t: base64.b64encode(t.encode()).decode()   # noqa: E731
        return mark("D", str(rc), "1" if cmd else "0", b(cmd or ""), "1", b(cwd), b(host), nonce=key)

    def test_hoplon_ssh_follows_the_remote_shell_with_its_own_key(self):
        r = REC.Recorder()
        key = self.remote_session(r)
        self.assertRegex(key, r"^[0-9a-f]{32}$")
        self.assertIsNone(r.take_handshake())                          # typed once
        self.assertIsNone(r.remote_prompt())                           # not until the remote prompt comes
        r.feed(b"Welcome to hpc1\r\n" + self.rdone(0, None, key))
        self.assertEqual(r.remote_prompt(), ("login1[2J", "/scratch/me"))   # what the other host says, printable
        r.feed(b"make\r\n" + mark("S", nonce=key) + b"built\r\n" + self.rdone(2, "make", key))
        c = r.last()
        self.assertEqual((c.host, c.cmd, c.rc, c.text()), ("login1[2J", "make", 2, "built"))
        self.assertEqual(REC.label(c), "login1[2J: make")
        r.feed(mark("S", nonce=key) + b"long job\r\n")
        self.assertIsNone(r.remote_prompt())                           # busy there: nothing can be sent
        self.assertEqual(r.running().text(), "long job")
        r.feed(b"Connection closed\r\n" + done(255, "hoplon-ssh hpc1"))     # ssh ended: the local prompt is back
        self.assertIsNone(r.remote)
        self.assertEqual((r.last().host, r.last().cmd), (None, "hoplon-ssh hpc1"))
        self.assertNotIn(key, "".join(c.text() for c in r.finished()))

    def test_hoplon_ssh_handshake_is_once_for_the_session_announced_and_in_time(self):
        import base64
        r = REC.Recorder()
        r.feed(mark("H") + mark("S") + mark("R", "5" * 32, nonce="ssh"))         # nothing announced
        self.assertIsNone(r.take_handshake())
        r.feed(mark("X", "5" * 32, "aG9zdA==", nonce="ab" * 8))                   # announced without the word
        r.feed(mark("R", "5" * 32, nonce="ssh"))
        self.assertIsNone(r.take_handshake())
        r.feed(mark("X", "5" * 32, base64.b64encode(b"hpc1").decode()))
        r.feed(mark("R", "6" * 32, nonce="ssh"))                                  # another session's id
        self.assertIsNone(r.take_handshake())
        r.feed(mark("R", "5" * 32, nonce="ssh"))
        key = r.take_handshake()
        r.feed(mark("R", "5" * 32, nonce="ssh"))                                  # a second ask gets nothing
        self.assertIsNone(r.take_handshake())
        r.feed(mark("S", nonce="cd" * 16) + b"forged\r\n" + self.rdone(0, "rm -rf /", "cd" * 16))   # not the key
        self.assertEqual([c.cmd for c in r.finished() if c.host], [])
        self.assertIsNotNone(key)
        late = REC.Recorder()
        late.feed(mark("H") + mark("S") + mark("X", "7" * 32, "aG9zdA=="))
        late.remote.until = 0                                                     # REMOTE_WAIT has passed
        late.feed(mark("R", "7" * 32, nonce="ssh"))
        self.assertIsNone(late.take_handshake())

    def test_markers_split_across_reads(self):
        r = REC.Recorder()
        data = mark("H") + mark("S") + b"one\r\ntwo\r\n" + done(0, "echo")
        for i in range(len(data)):
            r.feed(data[i:i + 1])
        self.assertEqual(r.last().text(), "one\ntwo")

    def test_a_command_cannot_fake_markers(self):
        r = REC.Recorder()
        r.feed(mark("H") + mark("S") + b"real\r\n" + done(0, "x", nonce="ffffffffffffffff") + mark("H", nonce="ab" * 8)
               + b"more\r\n" + done(0, "cat evil"))
        self.assertEqual([(c.cmd, c.text()) for c in r.finished()], [("cat evil", "real\nmore")])

    def test_markers_before_the_word_is_announced_are_output(self):
        r = REC.Recorder()
        r.feed(mark("S", nonce="ab" * 8) + mark("H", nonce="not hex") + mark("H"))
        self.assertEqual(r.nonce, "0123456789abcdef")

    def test_terminal_output_as_text(self):
        self.assertEqual(REC.render(b"\x1b[1;31mred\x1b[0m\r\n"), "red\n")
        self.assertEqual(REC.render(b"10%\r50%\r100%\r\n"), "100%\n")
        self.assertEqual(REC.render(b"abc\x08\x08X\n"), "aXc\n")
        self.assertEqual(REC.render(b"long line\rshort\x1b[K\n"), "short\n")
        self.assertEqual(REC.render(b"\x1b]0;title\x07t\x07ab\x01\n"), "tab\n")

    def test_full_screen_programs_are_left_out(self):
        r = REC.Recorder()
        r.feed(mark("H") + mark("S") + b"before\r\n\x1b[?1049h\x1b[Hlots of screen\x1b[?1049lafter\r\n" + done(0, "less f"))
        self.assertEqual(r.last().text(), "before\nafter")
        self.assertTrue(r.last().screen)

    def test_huge_output_keeps_its_start_and_end(self):
        r = REC.Recorder()
        r.feed(mark("H") + mark("S"))
        for i in range(40000):
            r.feed(f"line {i:05d} {'x' * 30}\r\n".encode())
        r.feed(done(0, "spam"))
        text = r.last().text()
        self.assertTrue(text.startswith("line 00000"))
        self.assertTrue(text.endswith("line 39999 " + "x" * 30))
        self.assertIn("bytes of output not kept", text)
        trimmed = REC.trim(text)
        self.assertIn("lines left out here", trimmed)
        self.assertLess(trimmed.count("\n"), 400)

    def test_failed_turn_ends_busy(self):
        m = C.Model()
        m.submit("hi")
        m.handle({"type": "error", "code": "api", "message": "endpoint error"})
        self.assertFalse(m.busy)
        m.submit("again")
        self.assertTrue(m.busy)


class TestFeatures(unittest.TestCase):
    """tui/features: the registry, and switching features off."""

    def setUp(self):
        self.config = tempfile.mkdtemp(prefix="hoplon-config-")
        self.addCleanup(shutil.rmtree, self.config, True)
        os.makedirs(os.path.join(self.config, "hoplon"))

    def environ(self, **extra):
        return dict({"XDG_CONFIG_HOME": self.config}, **extra)

    def write_config(self, text):
        with open(os.path.join(self.config, "hoplon", "tui.json"), "w") as f:
            f.write(text)

    def model(self, environ):
        names, warning = F.disabled_names(environ)
        return C.Model(F.load(names, C.CORE)), warning

    def test_all_built_in_features_load(self):
        on = F.load(core=C.CORE)
        self.assertEqual([f.name for f in on], ["model", "copy", "decline", "raw", "ssh", "files", "sandbox", "edits", "plugins", "work"])
        m = C.Model()
        self.assertEqual([f.name for f in m.features], [f.name for f in on])
        self.assertEqual(list(m.commands())[-10:], ["/model", "/copy", "/decline", "/raw", "/system", "/files", "/sandbox", "/edits", "/plugins", "/work"])
        self.assertEqual(C.feature_requests(on), {"panel": {"models"}, "menu": {"model", "copy", "raw", "system"},
                          "files": {"list", "view", "attach", "insert"}})

    def test_environment_disables_a_feature(self):
        m, warning = self.model(self.environ(HOPLON_TUI_DISABLE="copy, model"))
        self.assertIsNone(warning)
        self.assertEqual([f.name for f in m.features], ["decline", "raw", "ssh", "files", "sandbox", "edits", "plugins", "work"])
        m.submit("/copy")
        self.assertIn("unknown command /copy (/help lists them)", text_of(m.blocks[-1]))
        m.submit("/help")
        helped = "\n".join(text_of(b) for b in m.blocks)
        self.assertNotIn("/copy", helped.replace("unknown command /copy", ""))
        self.assertNotIn("/model", helped)
        self.assertIn("/raw", helped)
        m.handle({"type": "models", "models": [{"id": "x"}]})                # its events are ignored too
        self.assertEqual(m.actions, [])
        self.assertEqual(C.complete("/mo", C.command_list(m.features)), "/mo")

    def test_config_file_disables_a_feature(self):
        self.write_config('{"disable": ["decline", "raw"]}')
        m, warning = self.model(self.environ())
        self.assertIsNone(warning)
        self.assertEqual([f.name for f in m.features], ["model", "copy", "ssh", "files", "sandbox", "edits", "plugins", "work"])
        m.submit("/decline")
        self.assertIn("unknown command /decline", text_of(m.blocks[-1]))

    def test_no_config_file_means_everything_is_on(self):
        names, warning = F.disabled_names(self.environ())
        self.assertEqual((names, warning), (set(), None))

    def test_a_malformed_config_is_ignored_with_one_warning(self):
        for text in ("{not json", '{"disable": "copy"}', '["copy"]', '{"disable": [1]}'):
            self.write_config(text)
            m, warning = self.model(self.environ())
            self.assertEqual(len(m.features), 10, text)
            self.assertIn("was ignored, so every feature is on", warning)

    def test_the_hub_refuses_the_requests_of_a_disabled_feature(self):
        names, _ = F.disabled_names(self.environ(HOPLON_TUI_DISABLE="copy"))
        d = tempfile.mkdtemp(prefix="hoplon-ipc-")
        os.chmod(d, 0o700)
        self.addCleanup(shutil.rmtree, d, True)
        hub = IPC.Hub(d, C.feature_requests(F.load(names, C.CORE)))
        self.addCleanup(hub.close)
        menu = IPC.Client(d, "menu")
        self.addCleanup(menu.close)
        menu.send({"type": "copy", "turn": 1})
        menu.send({"type": "model", "n": 0})
        got = []
        end = time.time() + 2
        while len(got) < 2 and time.time() < end:
            try:
                got.append(hub.inbox.get(timeout=0.1))
            except Exception:
                pass
        self.assertEqual(got, [("hello", "menu"), ("menu", {"type": "model", "n": 0})])

    def test_features_cannot_be_given_the_power_to_answer(self):
        roles = {r: set(k) for r, k in IPC.ROLES.items()}
        for role, kind in (("prompt", "copy"), ("menu", "key"), ("panel", "decision"), ("menu", "share"),
                           ("root", "copy")):
            with self.assertRaises(ValueError):
                IPC.allow(roles, role, [kind])
        self.assertEqual(roles, IPC.ROLES)

    def test_a_model_request_from_a_role_it_does_not_allow_does_nothing(self):
        m = C.Model()
        m.request({"type": "copy", "turn": 1}, "panel")
        self.assertEqual((m.blocks, m.actions), ([], []))

    def test_menu_items_from_features_keep_only_fixed_words_and_numbers(self):
        sh = C.Shell("%1")
        m = C.Model()
        m.shell = object()
        m.add(C.UserMsg("hi"))
        m.blocks[-1].turn = m.turn = 1
        m.exchanges[1] = [1, 1]
        here = [f.turn_menu(m, 1) for f in m.features]
        self.assertTrue(any(here))
        evil = [[("Copy #{x}", "c", "copy", "1"), ("ok", "c", "copy", "1; rm"), ("ok", "cc", "copy", "1"),
                 ("fine", "f", "copy", "7")]]
        with mock.patch.object(C.subprocess, "Popen") as popen:
            sh.turn_menu(1, here + evil)
            argv = popen.call_args[0][0]
            sh.menu("p1", "here", extra=[("Decline it", "d", "decline"), ("bad#", "x", "decline"),
                                         ("ok", "k", "rm -rf"), ("ok", "k", "$(x)")])
            card = popen.call_args[0][0]
        self.assertIn("Copy this message and its reply", argv)
        self.assertIn("fine", argv)
        self.assertNotIn("Copy #{x}", argv)
        self.assertNotIn("ok", argv)
        picks = [a for a in argv if "request.py" in a]
        self.assertTrue(all(re.search(r"request.py (copy [0-9]+|raw (last|all|[0-9]+)|system)\\?\"$", a) for a in picks),
                        picks)
        self.assertIn("Decline it", card)
        self.assertEqual(sum("request.py" in a for a in card), 5)         # run, edit, prompt, copy, decline
        self.assertNotIn("bad#", card)


class TestFilesFeature(unittest.TestCase):
    """tui/features/files.py: what the file tree pane may ask for, and how hoplon's answers come back."""

    def model(self, **extra):
        m = C.Model()
        m.handle({"type": "hello", "version": "1", "endpoint": {"name": "e", "model": "m", "base_url": "u"},
                  "sandbox": None, "features": ["files", "read"]} | extra)
        return m

    def panes(self, m):
        out, m.panes = m.panes, []
        return out

    def test_a_listing_is_asked_for_and_answered_for_the_same_path(self):
        m = self.model()
        m.request({"type": "list", "path": "src"}, "files")
        self.assertEqual(m.outbox, [{"type": "files", "path": "src"}])
        m.handle({"type": "files", "path": "elsewhere", "entries": []})          # nobody asked for that one
        self.assertEqual(self.panes(m), [])
        entries = [{"name": "app.py", "kind": "file"}]
        m.handle({"type": "files", "path": "src", "entries": entries, "truncated": True})
        self.assertEqual(self.panes(m), [("files", {"type": "listing", "path": "src", "entries": entries,
                                                    "truncated": True})])

    def test_a_refused_listing_or_read_is_attributed_by_its_path(self):
        m = self.model()
        m.request({"type": "list", "path": "a/b"}, "files")
        m.request({"type": "view", "path": "x.txt"}, "files")
        m.handle({"type": "error", "code": "bad_path", "message": "cannot list a/b: no such directory"})
        self.assertEqual(self.panes(m), [("files", {"type": "listing", "path": "a/b", "error": "no such directory"})])
        m.handle({"type": "error", "code": "bad_path", "message": "cannot read x.txt: too big", "path": "x.txt"})
        self.assertIn("cannot view x.txt: cannot read x.txt: too big", text_of(m.blocks[-1]))
        n = len(m.blocks)
        m.handle({"type": "error", "code": "bad_path", "message": "cannot list zzz: no"})        # not ours
        m.handle({"type": "error", "code": "bad_path", "message": "cannot read q: no", "path": "q"})
        m.handle({"type": "error", "code": "bad_message", "message": "cannot list a/b: x"})
        self.assertEqual(len(m.blocks), n + 3)                                   # the core reported each one

    def test_view_and_attach_read_the_file_and_remember_why(self):
        m = self.model()
        m.request({"type": "view", "path": "README.md"}, "files")
        m.request({"type": "attach", "path": "src/app.py"}, "files")
        self.assertEqual(m.outbox, [{"type": "read", "path": "README.md"}, {"type": "read", "path": "src/app.py"}])
        m.handle({"type": "file", "path": "src/app.py", "text": "x = 1\n", "size": 6, "secret": False, "redacted": {}})
        m.handle({"type": "file", "path": "README.md", "text": "hi", "size": 2, "secret": False, "redacted": {}})
        m.handle({"type": "file", "path": "other", "text": "hi", "size": 2, "secret": False, "redacted": {}})
        self.assertEqual([a[:2] for a in m.actions], [("file", "attach"), ("file", "view")])

    def test_view_shows_cleaned_text_with_a_warning_for_secrets(self):
        app, feature = mock.Mock(), self.model().feature("files")
        feature.view(app, {"path": "a.env", "size": 12, "secret": True, "redacted": {"api_key": 1},
                           "text": "KEY=\x1b[31mred\x07\n"})
        shown = app.view_text.call_args[0][0]
        self.assertTrue(shown.startswith("a.env · 12 bytes\nhoplon thinks this file holds secrets; masked: 1 api_key"))
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\x07", shown)
        self.assertIn("\\x1b[31mred\\x07", shown)

    def test_attach_opens_a_review_buffer_and_attaches_what_is_saved(self):
        app, feature = mock.Mock(), self.model().feature("files")
        app.m = C.Model()
        app.edit.return_value = "File a.env:\n```\nKEY=1\n```"
        feature.attach(app, {"path": "a.env", "size": 5, "secret": True, "redacted": {"api_key": 1}, "text": "KEY=1"})
        buffer = app.edit.call_args[0][0]
        self.assertTrue(buffer.startswith("# hoplon thinks this file holds secrets"))
        self.assertIn("# EVERYTHING below is sent to the model as saved", buffer)
        self.assertIn("File a.env:\n```\nKEY=1\n```", buffer)
        self.assertEqual(app.m.next_attachments, [{"type": "attach", "label": "file a.env",
                                                   "text": "File a.env:\n```\nKEY=1\n```"}])
        app.edit.return_value = " \n"
        feature.attach(app, {"path": "b", "text": "x"})
        self.assertEqual(len(app.m.next_attachments), 1)                         # an empty save sends nothing

    def test_insert_goes_to_the_prompt_pane_and_bad_paths_are_ignored(self):
        m = self.model()
        m.request({"type": "insert", "path": "src/app.py"}, "files")
        self.assertEqual(self.panes(m), [("prompt", {"type": "insert", "text": "src/app.py"})])
        self.assertEqual(m.actions, [("insert",)])                # and the input box gets the focus
        m.actions.clear()
        for bad in ("../x", "/etc/passwd", "a/../b", "a//b", "a\0b", "", "x" * 4097, 7, None, "./a", "a/"):
            for kind in ("list", "view", "attach", "insert"):
                m.request({"type": kind, "path": bad}, "files")
        self.assertEqual((m.outbox, self.panes(m), m.actions), ([], [], []))

    def test_a_hoplon_without_files_says_so(self):
        m = self.model(features=[])
        m.request({"type": "list", "path": "."}, "files")
        self.assertEqual((m.outbox, self.panes(m)),
                         ([], [("files", {"type": "unavailable", "reason": "this hoplon can't list files"})]))
        m.submit("/files")
        self.assertIn("this hoplon can't list files", text_of(m.blocks[-1]))

    def test_the_files_command_shows_or_hides_the_pane(self):
        m = self.model()
        m.submit("/files")
        self.assertEqual(m.actions, [("files",)])
        self.assertIn("/files", m.commands())

    def test_a_disabled_files_feature_has_no_command_and_the_hub_refuses_its_requests(self):
        m = C.Model(F.load({"files"}, C.CORE))
        m.submit("/files")
        self.assertIn("unknown command /files", text_of(m.blocks[-1]))
        d = tempfile.mkdtemp(prefix="hoplon-ipc-")
        os.chmod(d, 0o700)
        self.addCleanup(shutil.rmtree, d, True)
        hub = IPC.Hub(d, C.feature_requests(m.features))
        self.addCleanup(hub.close)
        client = IPC.Client(d, "files")
        self.addCleanup(client.close)
        client.send({"type": "list", "path": "."})
        self.assertEqual(hub.inbox.get(timeout=2), ("hello", "files"))
        with self.assertRaises(queue.Empty):
            hub.inbox.get(timeout=0.5)

    def test_the_files_role_can_never_answer(self):
        roles = {r: set(k) for r, k in IPC.ROLES.items()}
        self.assertEqual(roles["files"], set())
        for kind in ("decision", "answer", "key", "input", "hello"):
            with self.assertRaises(ValueError):
                IPC.allow(roles, "files", [kind])

    def test_the_prompt_pane_inserts_only_plain_text_in_input_mode(self):
        pane = C_PROMPT.Prompt.__new__(C_PROMPT.Prompt)
        pane.ed, pane.view = C.Editor(), {"mode": "input"}
        for text in ("src/a.py", "x\x1b[2Jy", "a\nb", "", "y" * 4097, 5):
            pane.insert(text)
        self.assertEqual(pane.ed.text, "src/a.py")
        pane.view = {"mode": "ask"}
        pane.insert("more")
        self.assertEqual(pane.ed.text, "src/a.py")


class TestSocket(unittest.TestCase):
    """tui/ipc.py: the private socket between the panes."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hoplon-ipc-")
        os.chmod(self.dir, 0o700)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.hub = IPC.Hub(self.dir)
        self.addCleanup(self.hub.close)

    def received(self, n, timeout=2):
        out, end = [], time.time() + timeout
        while len(out) < n and time.time() < end:
            try:
                out.append(self.hub.inbox.get(timeout=0.1))
            except Exception:
                pass
        return out

    def test_socket_is_private(self):
        self.assertEqual(os.stat(IPC.socket_path(self.dir)).st_mode & 0o777, 0o600)

    def test_each_role_sends_only_its_own_messages(self):
        panel = IPC.Client(self.dir, "panel")
        self.addCleanup(panel.close)
        panel.send({"type": "key", "key": "y", "q": "approve:p1"})        # a panel may not answer
        panel.send({"type": "input", "text": "hi"})
        panel.send({"type": "coding", "on": True})
        self.assertEqual(self.received(2), [("hello", "panel"), ("panel", {"type": "coding", "on": True})])
        self.assertEqual(self.received(1, timeout=0.3), [])

    def test_a_plugin_pane_may_only_run_insert_and_attach(self):
        self.hub.plugins = True
        pane = IPC.Client(self.dir, "plugin:slurm")
        self.addCleanup(pane.close)
        for kind in ("input", "key", "decision", "coding", "skill", "share", "run"):
            pane.send({"type": kind})
        self.assertEqual(self.received(2), [("hello", "plugin:slurm"), ("plugin:slurm", {"type": "run"})])
        self.assertEqual(self.received(1, timeout=0.3), [])

    def test_plugin_panes_need_the_plugins_feature(self):
        for on, role in ((False, "plugin:slurm"), (True, "plugin:Bad"), (True, "plugin:")):
            self.hub.plugins = on
            c = IPC.Client(self.dir, role)
            self.addCleanup(c.close)
            c.send({"type": "run"})
            self.assertEqual(self.received(1, timeout=0.3), [], role)

    def test_unknown_role_and_bad_lines_end_the_connection(self):
        import socket
        for first in (b'{"type": "hello", "role": "root"}\n', b"not json\n", b"x" * (IPC.MAX_LINE + 10)):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(IPC.socket_path(self.dir))
            c.settimeout(3)
            try:
                c.sendall(first)
            except OSError:
                pass
            try:
                self.assertEqual(c.recv(10), b"")                  # closed by the hub
            except ConnectionResetError:
                pass
            c.close()
        self.assertEqual(self.received(1, timeout=0.3), [])

    def test_complete_oversize_line_is_rejected_before_decoding(self):
        sender, receiver = socket.socketpair()
        with sender, receiver, mock.patch.object(IPC, "MAX_LINE", 16):
            sender.sendall(IPC.encode({"text": "a" * 32}))
            sender.shutdown(socket.SHUT_WR)
            self.assertEqual(list(IPC.messages(receiver)), [])

    def test_incomplete_handshake_reserves_a_slot_until_deadline(self):
        with mock.patch.object(IPC, "MAX_CLIENTS", 1), mock.patch.object(IPC, "HELLO_TIMEOUT", 0.5):
            with socket.socket(socket.AF_UNIX) as first, socket.socket(socket.AF_UNIX) as excess:
                first.settimeout(2)
                excess.settimeout(2)
                first.connect(IPC.socket_path(self.dir))
                excess.connect(IPC.socket_path(self.dir))
                self.assertEqual(excess.recv(1), b"")
                self.assertEqual(first.recv(1), b"")
            client = IPC.Client(self.dir, "menu")
            self.addCleanup(client.close)
            self.assertEqual(self.received(1), [("hello", "menu")])

    def test_views_reach_the_right_role(self):
        prompt, panel = IPC.Client(self.dir, "prompt"), IPC.Client(self.dir, "panel")
        self.addCleanup(prompt.close)
        self.addCleanup(panel.close)
        self.received(2)
        self.hub.send("prompt", {"type": "view", "view": {"mode": "input"}})
        self.assertEqual(prompt.inbox.get(timeout=2), {"type": "view", "view": {"mode": "input"}})
        self.assertTrue(panel.inbox.empty())


class TmuxCase(unittest.TestCase):
    """Helpers for driving a private tmux server; no tests of its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hoplon-tui-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sock = f"hoplon-tui-test-{os.getpid()}"
        self.addCleanup(self.tmux, "kill-server")

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, *args], capture_output=True, text=True).stdout

    def screen(self):
        return self.tmux("capture-pane", "-p", "-t", "t")

    def wait_for(self, text, timeout=10, pane=None):
        look = (lambda: self.screen(pane)) if pane else self.screen
        end = time.time() + timeout
        while time.time() < end:
            if text in look():
                return
            time.sleep(0.1)
        self.fail(f"{text!r} never appeared:\n{look()}")

    def type(self, text, enter=True):
        self.tmux("send-keys", "-t", "t", "-l", text)
        if enter:
            self.tmux("send-keys", "-t", "t", "Enter")

    def config(self, api):
        """A config home with the fake endpoint blessed, and an empty working directory."""
        home = os.path.join(self.tmp, "cfg", "hoplon")
        os.makedirs(os.path.join(home, "endpoints"))
        ep = os.path.join(home, "endpoints", "test.json")
        with open(ep, "w") as f:
            json.dump({"name": "test", "base_url": api.url, "model": "fake-model", "context_limit": 100000}, f)
        cfg = G.Config(home)
        cfg.ensure()
        cfg.set_blessed(ep, {"kind": "endpoint", "name": "test", "hash": G.hash_file(ep)})
        self.wd = os.path.join(self.tmp, "wd")
        os.makedirs(self.wd)
        return home


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class TestInTmux(TmuxCase):
    """The pane for real: hoplon --json against the fake endpoint, typed into through tmux."""

    def start(self, api):
        home = self.config(api)
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


@unittest.skipUnless(shutil.which("tmux") and shutil.which("script"), "needs tmux and script")
class TestHoplonTui(TmuxCase):
    """tui/hoplon-tui itself: the conversation pane above a real bash pane, started through a pseudo-terminal."""

    def start(self, api, editor="true", path=None, args="-e test", keep_home=False):
        home = self.home if keep_home else self.config(api)      # keep_home: the test made self.home with config()
        if not args:        # hoplon then finds the endpoint the way a bare `hoplon` does
            conf = dict(G.STARTER_CONFIG, default_endpoint="test")
            G.write_private(os.path.join(home, "config.json"), json.dumps(conf))
        self.runtime = tempfile.mkdtemp(prefix="gr-", dir="/tmp")      # short: socket paths are limited
        os.chmod(self.runtime, 0o700)
        self.addCleanup(shutil.rmtree, self.runtime, True)
        if path is None:
            path = os.path.join(self.tmp, "test-bin")
            os.mkdir(path)
            pager = os.path.join(path, "less")
            with open(pager, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(pager, 0o700)
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home), HOME=self.tmp, LANG="C.UTF-8",
                   TERM="xterm-256color", HOPLON_TUI_SOCKET=self.sock, EDITOR=editor, VISUAL=editor,
                   XDG_RUNTIME_DIR=self.runtime, PATH=f"{path}:{os.environ['PATH']}" if path else os.environ["PATH"])
        if path and os.path.exists(os.path.join(path, "less")):
            env["HOPLON_TUI_PAGER"] = os.path.join(path, "less")      # the test's pager stands in for hoplon-tui's
        env.pop("TMUX", None)
        env["HOPLON_TUI_SPLASH"] = "0"         # the splash holds the screen before tmux attaches
        self.proc = subprocess.Popen(["script", "-qfc", f"stty cols 110 rows 40; {REPO}/tui/hoplon-tui {args}",
                                      "/dev/null"], cwd=self.wd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (self.tmux("kill-server"), self.proc.wait(10)))
        end = time.time() + 10
        while time.time() < end and not {"hoplon", "you", "shell"} <= set(self.panes()):
            time.sleep(0.1)
        self.wait_for("Type a message")

    def panes(self):
        out = self.tmux("list-panes", "-a", "-F", "#{pane_id} #{@hoplon_title}")
        return {t.split(" ")[0]: p for p, _, t in (ln.partition(" ") for ln in out.splitlines())}

    def screen(self, pane=None):
        """One pane, or the conversation and the prompt pane together."""
        names = [pane] if pane else ["hoplon", "you"]
        return "\n".join(self.tmux("capture-pane", "-p", "-t", self.panes()[n]) for n in names)

    def type(self, text, enter=True):
        you = self.panes()["you"]
        self.tmux("send-keys", "-t", you, "-l", text)
        if enter:
            self.tmux("send-keys", "-t", you, "Enter")

    def key(self, k):
        """An answer typed in the prompt pane, after the pause it needs (keys right as a question appears are
        taken as typing)."""
        time.sleep(C_PROMPT.SETTLE + 0.1)
        self.tmux("send-keys", "-t", self.panes()["you"], k)

    def at_terminal(self, keys):
        """Keys typed at the terminal itself, so they reach tmux (and its menus) first."""
        self.proc.stdin.write(keys.encode())
        self.proc.stdin.flush()

    def click(self, text, pane="hoplon", button=0, hold=0.3):
        """A click on the first row of a pane showing text, at the terminal as a mouse sends it: the press, the
        button held a moment (long enough for a menu to open), then the release."""
        y = next(i for i, ln in enumerate(self.screen(pane).splitlines()) if text in ln)
        top, left = (int(v) for v in self.tmux("display", "-p", "-t", self.panes()[pane],
                                                 "#{pane_top} #{pane_left}").split())
        at = f"{button};{left + 5};{top + y + 1}"
        self.at_terminal(f"\x1b[<{at}M")
        time.sleep(hold)
        self.at_terminal(f"\x1b[<{at}m")

    def shell(self, command):
        self.tmux("send-keys", "-t", self.panes()["shell"], command, "Enter")
        time.sleep(0.4)

    def sent(self, api):
        return [m for m in api.requests[-1]["body"]["messages"] if m["role"] == "user"][-1]["content"]

    def test_a_sandboxed_run_shows_it_is_running_and_ctrl_c_stops_it(self):
        from test_sandbox_runner import FAKE_PODMAN
        path = os.path.join(self.tmp, "test-bin")
        os.mkdir(path)
        for name, body in (("less", "#!/bin/sh\nexit 0\n"), ("podman", FAKE_PODMAN)):
            with open(os.path.join(path, name), "w") as f:
                f.write(body)
            os.chmod(os.path.join(path, name), 0o700)
        log = os.path.join(self.tmp, "podman.log")
        plain = self.config

        def config(api):                                   # and a blessed coding profile with a sandbox
            home = plain(api)
            prof = os.path.join(home, "profiles", "coder.json")
            os.makedirs(os.path.dirname(prof), exist_ok=True)
            with open(prof, "w") as f:
                json.dump({"name": "coder", "endpoint": "test", "coding": True, "sandbox": {"image": "img:1"}}, f)
            G.Config(home).set_blessed(prof, {"kind": "profile", "name": "coder", "hash": G.hash_file(prof)})
            return home
        self.config = config
        turns = [call("run_in_sandbox", command="echo started; sleep 30", reason="t"), reply("it was stopped")]
        with FakeAPI(turns) as api, mock.patch.dict(os.environ, {"FAKE_PODMAN_LOG": log}):
            self.start(api, path=path, args="-p coder")
            self.assertIn("⊡ model's runs sandboxed", self.tmux("show", "-gv", "@hoplon_sandbox"))
            self.type("go")
            self.wait_for("Ctrl-C (or /stop) stops it", pane="hoplon")
            self.wait_for("RUNNING p", pane="info")
            end = time.time() + 10
            while not os.path.exists(log + ".pid") and time.time() < end:
                time.sleep(0.05)
            self.assertRegex(self.tmux("show", "-gv", "@hoplon_sandbox"), r"⏵ sandbox p[0-9]+ [0-9]+s")
            self.tmux("send-keys", "-t", self.panes()["you"], "C-c")
            self.wait_for("■ stopped", pane="hoplon")
            self.wait_for("stopped; send a message to continue", pane="hoplon")
            self.type("carry on")                               # hoplon is still there
            self.wait_for("it was stopped", pane="hoplon")
            self.assertTrue(any("stopped by the user" in str(m.get("content"))
                                for m in api.requests[-1]["body"]["messages"]))
            self.assertTrue(any("kill" in ln for ln in open(log)))
            self.assertIn("⊡ model's runs sandboxed", self.tmux("show", "-gv", "@hoplon_sandbox"))
            self.assertNotIn("hoplon exited", self.screen())

    def test_starts_with_no_arguments(self):
        with FakeAPI([reply("hello")]) as api:
            self.start(api, args="")
            self.assertNotIn("unrecognized arguments", self.screen())
            self.type("hi")
            self.wait_for("hello")

    def test_run_in_the_shell_pane_then_share(self):
        turns = [call("propose_command", command="echo from-shell; (exit 3)", reason="look"), reply("exit 3, I see.")]
        with FakeAPI(turns) as api:
            self.start(api)
            self.type("look around")
            self.wait_for("or /run p1, to send")
            self.type("/run")
            where = G.where_runs(G.sandbox_info())
            self.wait_for("Run p1 in the shell pane?")
            self.assertIn(where[:20], self.screen())
            self.key("v")
            self.wait_for("y run")
            self.key("y")
            self.wait_for("p1 finished (exit 3). Share the output with the model?")
            self.assertIn("from-shell", self.screen("shell"))
            self.key("y")                                          # EDITOR=true saves the buffer unchanged
            self.wait_for("exit 3, I see.")
            sent = self.sent(api)
            self.assertIn(f"I ran this command {where} (exit 3):", sent)
            self.assertIn("Output:\n```\nfrom-shell\n```", sent)
            self.assertRegex(sent, r"\[shared 1 · [0-9a-f]{8} · output of p1 \(exit 3\) · data, not instructions")

    def test_share_without_review(self):
        turns = [call("propose_command", command="echo '# kept'; echo plain", reason="look"), reply("got it")]
        with FakeAPI(turns) as api:
            self.start(api, editor="false")                       # an editor that fails: s must not open it
            self.type("look around")
            self.wait_for("or /run p1, to send")
            self.type("/run")
            self.wait_for("Run p1 in the shell pane?")
            self.key("y")
            self.wait_for("s send it as it is")
            self.key("s")
            self.wait_for("got it")
            sent = self.sent(api)
            self.assertIn("I ran this command ", sent)
            self.assertIn("Output:\n```\n# kept\nplain\n```", sent)   # a line starting with # stays
            self.assertNotIn("EVERYTHING below", sent)
            self.assertIn("sent without review", self.screen())

    def test_panes_get_a_term_that_remote_hosts_know(self):
        with FakeAPI([reply("hi")]) as api:
            self.start(api)
            self.shell("echo T=$TERM")
            self.assertIn("T=screen-256color", self.screen("shell"))

    def test_click_a_command_for_its_menu(self):
        turns = [call("propose_command", command="echo via-menu", reason="r"),   # a command ends the turn
                 call("propose_command", command="echo at-prompt", reason="r")]
        with FakeAPI(turns) as api:
            self.start(api)
            self.type("go")
            self.wait_for("or /run p1, to send")
            self.click("echo via-menu")
            time.sleep(0.5)
            self.at_terminal("r")
            self.wait_for("Run p1 in the shell pane?")
            self.assertNotIn("via-menu", self.screen("shell"))
            self.key("v")
            self.wait_for("y run")
            self.key("y")
            self.wait_for("p1 finished (exit 0). Share the output with the model?")
            self.assertIn("via-menu", self.screen("shell"))
            self.assertEqual(self.window()[0], ["shell", "hoplon", "you"])     # shown, to watch it run
            self.key("n")
            self.type("again")
            self.wait_for("or /run p2, to send")
            self.at_terminal("\x1b2")                              # Alt+2: p1, now that p2 has Alt+1
            time.sleep(0.5)
            self.at_terminal("c")
            self.wait_for("p1 copied")
            self.assertEqual(self.tmux("show-buffer", "-b", "hoplon-copy"), "echo via-menu")
            self.click("echo at-prompt")
            time.sleep(0.5)
            self.at_terminal("c")                                  # "Copy it"
            self.wait_for("p2 copied")
            self.assertEqual(self.tmux("show-buffer", "-b", "hoplon-copy"), "echo at-prompt")
            self.click("echo at-prompt")
            time.sleep(0.5)
            self.at_terminal("p")
            self.wait_for("Paste p2 in the shell pane?")
            self.key("v")
            self.wait_for("y paste")
            self.key("y")
            self.wait_for("p2 is at the shell prompt")
            time.sleep(0.3)
            last = [ln for ln in self.screen("shell").splitlines() if ln.strip()][-1]
            self.assertTrue(last.endswith("echo at-prompt"), last)   # typed, not run

    def test_info_panel_switches_coding_and_skills(self):
        with FakeAPI([reply("hi")]) as api:
            home = os.path.join(self.tmp, "cfg", "hoplon")
            os.makedirs(os.path.join(home, "skills", "slurm"))
            with open(os.path.join(home, "skills", "slurm", "SKILL.md"), "w") as f:
                f.write("# Slurm help\n")
            self.start(api)
            sk = os.path.join(home, "skills", "slurm")
            G.Config(home).set_blessed(sk, {"kind": "skill", "name": "slurm", "hash": G.hash_skill(sk)[0]})
            self.tmux("send-keys", "-t", self.panes()["info"], "r")
            self.wait_for("[ ] slurm", pane="info")
            self.assertIn("▲ this host, no sandbox" if not G.sandbox_info() else "⊡ inside", self.screen("info"))
            self.tmux("send-keys", "-t", self.panes()["info"], "c")
            self.wait_for("coding mode is on")
            self.wait_for("[on]", pane="info")
            self.tmux("send-keys", "-t", self.panes()["info"], " ")
            self.wait_for("[x] slurm", pane="info")
            self.type("/panel")
            time.sleep(0.5)
            self.assertNotIn("info", [t.split(" ")[0] for t in
                                      self.tmux("list-panes", "-t", "hoplon:0", "-F", "#{@hoplon_title}").split()])

    def test_click_a_shell_command_in_the_panel_to_share_it(self):
        with FakeAPI([reply("got it")]) as api:
            self.start(api)
            self.shell("echo first-out")
            self.shell("printf 'a\\nb\\n'; (exit 2)")
            self.wait_for("✗ printf", pane="info")
            self.type("/commands")
            self.wait_for("/share N reviews one")
            self.assertIn("1 ✓ echo first-out", self.screen())
            self.click("echo first-out", pane="info")
            self.wait_for("Command 1 in the shell pane: echo first-out (exit 0, 1 line)")
            self.key("y")
            self.wait_for("got it")
            self.assertIn("```\necho first-out\n```\nOutput:\n```\nfirst-out\n```", self.sent(api))

    def test_switch_model_from_the_drop_down(self):
        with FakeAPI([reply("from fake"), reply("from coder")],
                     models=[{"id": "fake-model"}, {"id": "coder-model", "max_model_len": 65536}]) as api:
            self.start(api)
            self.type("hi")
            self.wait_for("from fake")
            self.type("/model")
            time.sleep(0.5)
            env = dict(os.environ, HOPLON_TUI_STATE=self.runtime_state())
            subprocess.run([sys.executable, os.path.join(REPO, "tui", "request.py"), "model", "1"], env=env, check=True)
            self.wait_for("model: coder-model from your next message")
            self.wait_for("coder-model ▾", pane="info")
            self.type("and now?")
            self.wait_for("from coder")
            self.assertEqual([r["body"]["model"] for r in api.requests if r.get("body")], ["fake-model", "coder-model"])

    def fake_ssh(self):
        """A PATH with an ssh that runs the remote command here through sh, as sshd runs it through your login
        shell, in a "remote" home of its own; with no command, a plain shell hoplon-tui can't follow."""
        bindir = os.path.join(self.tmp, "ssh-bin")
        remote = os.path.join(self.tmp, "remote-home")
        os.makedirs(bindir)
        os.makedirs(remote)
        named = os.path.join(self.tmp, "named")                       # a shell tmux reports as "ssh"
        os.makedirs(named)
        os.symlink(shutil.which("bash"), os.path.join(named, "ssh"))
        with open(os.path.join(bindir, "ssh"), "w") as f:
            f.write(f'#!/bin/sh\nfor a; do last=$a; done\ncd {remote}\n'
                    f'case $last in exec*) exec env HOME={remote} sh -c "$last" ;; esac\n'
                    f'exec env HOME={remote} {named}/ssh --norc -i\n')
        with open(os.path.join(bindir, "less"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        for n in ("ssh", "less"):
            os.chmod(os.path.join(bindir, n), 0o700)
        return bindir, remote

    def test_hoplon_ssh_runs_and_shares_commands_on_the_other_host(self):
        turns = [call("propose_command", command="echo on-$PWD", reason="where am I"), reply("seen it")]
        with FakeAPI(turns) as api:
            bindir, remote = self.fake_ssh()
            self.start(api, path=bindir)
            self.shell("hoplon-ssh -p 2222 me@hpc1")
            self.wait_for("following the shell on the other host")
            self.wait_for("ON ANOTHER HOST", pane="info")                # the panel says where commands run now
            time.sleep(0.5)
            shown = self.screen("shell")
            self.assertNotRegex(shown, r"[0-9a-f]{32}")                  # the key was typed with echo off
            self.type("where am I?")
            self.wait_for("/run p1, to send")
            self.type("/run")
            self.wait_for("ON ANOTHER HOST")
            self.assertIn(f":{remote}", self.screen())                   # host:directory, as the remote reports
            self.key("y")
            self.wait_for("Share the output")
            self.assertIn(f"on-{remote}", self.screen("shell"))
            self.key("y")                                                 # EDITOR=true sends it as shown
            self.wait_for("seen it")
            sent = self.sent(api)
            self.assertIn("I ran this command ON ANOTHER HOST, ", sent)
            self.assertIn(f"on-{remote}", sent)
            self.wait_for(": echo on-$PWD", pane="info")
            self.shell("exit")                                            # back here: commands run here again
            time.sleep(0.5)
            self.type("/run p1")
            self.wait_for("It runs OUTSIDE any sandbox")                # here again, not on the other host
            self.key("n")

    def test_plain_ssh_is_not_followed_and_says_how(self):
        turns = [call("propose_command", command="hostname", reason="r")]
        with FakeAPI(turns) as api:
            bindir, _ = self.fake_ssh()
            self.start(api, path=bindir)
            self.shell("ssh hpc1")
            time.sleep(0.5)
            self.type("go")
            self.wait_for("/run p1, to send")
            self.type("/run")
            self.wait_for("Run p1 in the shell pane?")
            self.key("y")
            self.wait_for("hoplon-ssh [ssh options] HOST")               # refused, and told how
            self.assertNotIn("hostname\n", self.screen("shell"))

    def test_decline_from_the_menu(self):
        turns = [call("propose_command", command="echo not-wanted", reason="r"), reply("fine, something else")]
        with FakeAPI(turns) as api:
            self.start(api)
            self.type("go")
            self.wait_for("or /run p1, to send")
            env = dict(os.environ, HOPLON_TUI_STATE=self.runtime_state())
            subprocess.run([sys.executable, os.path.join(REPO, "tui", "request.py"), "menu", "decline", "p1"],
                           env=env, check=True)
            self.wait_for("you declined it")
            self.type("then what?")
            self.wait_for("fine, something else")
            self.assertIn("I declined the command you proposed as p1", self.sent(api))
            self.assertNotIn("not-wanted", self.screen("shell"))

    def test_copy_a_turn_from_the_menu_and_by_command(self):
        with FakeAPI([reply("the answer")]) as api:
            self.start(api)
            self.type("the question")
            self.wait_for("the answer")
            env = dict(os.environ, HOPLON_TUI_STATE=self.runtime_state())
            subprocess.run([sys.executable, os.path.join(REPO, "tui", "request.py"), "copy", "1"], env=env, check=True)
            self.wait_for("turn 1 copied")
            self.assertEqual(self.tmux("show-buffer", "-b", "hoplon-copy"), "> the question\n\nthe answer\n")
            self.tmux("delete-buffer", "-b", "hoplon-copy")
            self.type("/copy")
            end = time.time() + 5
            while time.time() < end and not self.tmux("show-buffer", "-b", "hoplon-copy"):
                time.sleep(0.1)
            self.assertIn("the answer", self.tmux("show-buffer", "-b", "hoplon-copy"))

    def test_raw_view_from_the_menu_and_by_command(self):
        bindir = tempfile.mkdtemp(prefix="hoplon-tui-bin-")
        self.addCleanup(shutil.rmtree, bindir, True)
        seen = os.path.join(bindir, "seen")
        with open(os.path.join(bindir, "less"), "w") as f:                 # a pager that keeps what it was shown
            f.write(f'#!/bin/sh\nfor a; do last=$a; done\ncat "$last" >> {seen}\n')
        os.chmod(os.path.join(bindir, "less"), 0o700)
        with FakeAPI([reply("hello there")]) as api:
            self.start(api, path=bindir)
            self.type("say hi")
            self.wait_for("hello there")
            self.click("hello there", button=2)                          # right-click the reply
            time.sleep(0.5)
            self.at_terminal("r")                                         # "What was sent and received for this turn"
            end = time.time() + 10
            while time.time() < end and not os.path.exists(seen):
                time.sleep(0.1)
            time.sleep(0.3)
            shown = slurp(seen)
            self.assertIn("Exchange 1 · turn 1", shown)
            self.assertIn('"content": "say hi"', shown)
            self.assertIn("data: ", shown)
            self.type("/system")
            end = time.time() + 10
            while time.time() < end and "system prompt and tools" not in slurp(seen):
                time.sleep(0.1)
            self.assertIn("You are hoplon", slurp(seen))
            self.assertEqual([n for n in os.listdir(self.runtime_state()) if n.startswith("raw-")], [])

    def test_the_slurm_console_runs_its_commands_through_hoplon(self):
        with FakeAPI([reply("ok")]) as api:
            home = self.config(api)
            fake = os.path.join(self.tmp, "slurm-bin")
            os.mkdir(fake, 0o755)
            nodes = "c001|batch*|idle|0/32/0/32|192000|none\nc002|batch*|drained|0/0/32/32|192000|bad DIMM\n"
            for prog, body in (("sinfo", f'case "$*" in *-N*) printf "{nodes}";; *-s*) echo "batch*|up|1:00:00|2|0/1/1/2|0/32/32/64";; esac'),
                               ("squeue", 'echo "7|batch|train|alice|RUNNING|1:00|1|c001"'),
                               ("scontrol", 'echo "NodeName=$3 State=DRAIN Reason=bad_DIMM"')):
                with open(os.path.join(fake, prog), "w") as f:
                    f.write("#!/bin/sh\n" + body + "\n")
                os.chmod(os.path.join(fake, prog), 0o755)
            pdir = os.path.join(home, "plugins", "slurm")
            shutil.copytree(os.path.join(REPO, "plugins", "slurm"), pdir)
            with open(os.path.join(pdir, "plugin.json")) as f:
                manifest = json.load(f)
            manifest["search"] = [fake]
            with open(os.path.join(pdir, "plugin.json"), "w") as f:
                json.dump(manifest, f)
            G.Config(home).set_blessed(pdir, {"kind": "plugin", "name": "slurm", "hash": G.hash_skill(pdir)[0]})
            G.write_private(os.path.join(home, "config.json"), json.dumps(dict(G.STARTER_CONFIG, default_plugins=["slurm"])))
            self.home = home
            self.start(api, args="-e test --no-panel", keep_home=True)
            self.type("/plugins slurm pane")
            end = time.time() + 15
            while time.time() < end and "slurm" not in self.panes():
                time.sleep(0.2)
            self.wait_for("bad DIMM", pane="slurm", timeout=15)
            self.wait_for("running 1", pane="slurm")
            self.tmux("send-keys", "-t", self.panes()["slurm"], "Down", "Enter")
            self.wait_for("NodeName=c002", pane="slurm")
            self.tmux("send-keys", "-t", self.panes()["slurm"], "Escape")
            self.tmux("send-keys", "-t", self.panes()["slurm"], "i")
            self.wait_for("c002", pane="you")

    def test_the_file_tree_browses_views_attaches_and_inserts(self):
        bindir = tempfile.mkdtemp(prefix="hoplon-tui-bin-")
        self.addCleanup(shutil.rmtree, bindir, True)
        seen = os.path.join(bindir, "seen")
        with open(os.path.join(bindir, "less"), "w") as f:                 # a pager that keeps what it was shown
            f.write(f'#!/bin/sh\nfor a; do last=$a; done\ncat "$last" >> {seen}\n')
        os.chmod(os.path.join(bindir, "less"), 0o700)
        with FakeAPI([reply("ok")]) as api:
            self.start(api, path=bindir, args="-e test --no-panel")
            os.makedirs(os.path.join(self.wd, "src"))
            for name, text in (("src/app.py", "print('hi')\n"), ("README.md", "# Title\nsome words\n"),
                               (".env", "API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789\n")):
                with open(os.path.join(self.wd, name), "w") as f:
                    f.write(text)
            os.symlink("README.md", os.path.join(self.wd, "link"))
            self.type("/files")
            end = time.time() + 10
            while time.time() < end and "README.md" not in self.screen("files"):
                self.tmux("send-keys", "-t", self.panes()["files"], "r")      # it listed the empty directory at start
                time.sleep(0.5)
            self.wait_for("README.md", pane="files")
            self.wait_for("src/", pane="files")
            self.assertRegex(self.screen("files"), r"⚠ \.env")
            self.assertRegex(self.screen("files"), r"↪ link")
            send = lambda *keys: self.tmux("send-keys", "-t", self.panes()["files"], *keys)
            send("Enter")                                                     # src/ is selected first: open it
            self.wait_for("app.py", pane="files")
            send("Down", "Down", "Down", "Down")                              # app.py, .env, link, README.md
            send("i")
            self.wait_for("README.md", pane="you")
            send("Enter")                                                     # view it
            end = time.time() + 10
            while time.time() < end and not os.path.exists(seen):
                time.sleep(0.1)
            time.sleep(0.3)
            self.assertIn("README.md · 19 bytes", slurp(seen))
            self.assertIn("some words", slurp(seen))
            send("a")
            self.wait_for("attached file README.md")

    def test_one_click_in_the_file_tree_acts_once(self):
        """A mouse click is a press and a release; only one of them may count, or a folder opens and shuts again."""
        with FakeAPI([reply("ok")]) as api:
            self.start(api, args="-e test --no-panel")
            os.makedirs(os.path.join(self.wd, "src"))
            with open(os.path.join(self.wd, "src", "app.py"), "w") as f:
                f.write("print('hi')\n")
            self.type("/files")
            end = time.time() + 10
            while time.time() < end and "src/" not in self.screen("files"):
                self.tmux("send-keys", "-t", self.panes()["files"], "r")
                time.sleep(0.5)
            self.wait_for("src/", pane="files")
            self.click("src/", pane="files", hold=0.05)                      # selected first: a click opens it
            self.wait_for("app.py", pane="files")
            time.sleep(0.5)
            self.assertIn("app.py", self.screen("files"))
            self.click("src/", pane="files", hold=0.05)                      # and one more shuts it
            time.sleep(0.5)
            self.assertNotIn("app.py", self.screen("files"))

    def runtime_state(self):
        return next(os.path.join(self.runtime, d) for d in os.listdir(self.runtime) if d.startswith("hoplon-tui."))

    def test_an_approved_plan_runs_its_command_on_its_own(self):
        steps = [{"kind": "read", "path": "a.txt", "why": "look"}, {"kind": "edit", "path": "a.txt", "why": "fix"},
                 {"kind": "command", "command": "cat a.txt", "why": "check"}]
        turns = [call("propose_plan", goal="fix a", steps=steps), call("read_file", path="a.txt", reason="r"),
                 call("edit_file", path="a.txt", old_text="old", new_text="planned-new", reason="r"),
                 call("propose_command", command="cat a.txt", reason="r"), reply("all good"),
                 call("propose_command", command="cat a.txt", reason="repeat")]
        with FakeAPI(turns) as api:
            self.start(api)
            with open(os.path.join(self.wd, "a.txt"), "w") as f:
                f.write("old\n")
            self.type("fix a")
            self.wait_for("Approve plan p1 (3 steps, 1 command)?")
            self.key("a")
            self.wait_for("press a again")
            self.key("a")
            self.wait_for("Allow p3: edit a.txt?")                     # the read didn't ask; the edit does
            self.assertIn("step 2 of the plan", self.screen())
            end = time.time() + 5                                       # tmux's status line says so too
            while "waiting for you" not in self.tmux("show", "-gv", "@hoplon_sandbox") and time.time() < end:
                time.sleep(0.1)
            self.assertIn("⏸ waiting for you", self.tmux("show", "-gv", "@hoplon_sandbox"))
            self.key("y")
            self.wait_for("p4 finished (exit 0). Share the output")    # run without another question
            self.assertIn("planned-new", self.screen("shell"))
            self.wait_for("✓  3 run cat a.txt", pane="info")
            self.key("y")
            self.wait_for("all good")
            self.assertIn("planned-new", self.sent(api))
            self.type("check again")
            self.wait_for("or /run p5, to send")
            self.wait_for("step 3 of the plan, already run once", pane="hoplon")
            self.assertEqual(self.screen("shell").count("cat a.txt"), 1)

    def test_a_mark_survives_resizing(self):
        with FakeAPI([reply("seen it")]) as api:
            self.start(api)
            self.show_shell()
            sh = self.panes()["shell"]
            long = "x" * 150                                          # wraps in the shell pane, then won't
            for c in ("echo before-mark", f"echo {long}", "echo in-context"):
                self.shell(c)
            y = next(i for i, ln in enumerate(self.screen("shell").splitlines()) if ln.endswith("echo in-context"))
            top = int(self.tmux("display", "-p", "-t", sh, "#{pane_top}"))
            self.at_terminal(f"\x1b[<2;5;{top + y + 1}M")
            time.sleep(0.5)
            self.at_terminal("m")
            time.sleep(0.5)
            self.tmux("resize-window", "-t", "hoplon:0", "-x", "220")
            time.sleep(0.5)
            self.type("/share mark")
            self.wait_for("Share 2 lines of shell history from the mark with the model?")
            self.key("y")
            self.wait_for("attached 2 lines")
            self.type("what happened?")
            self.wait_for("seen it")
            sent = self.sent(api)
            self.assertIn("echo in-context\nin-context", sent)
            self.assertNotIn("xxxx", sent)

    def test_right_click_marks_shell_context(self):
        with FakeAPI([reply("seen it")]) as api:
            self.start(api)
            self.show_shell()                                       # it has to be on screen to be clicked
            sh = self.panes()["shell"]
            for c in ("echo before-mark", "echo in-context"):
                self.tmux("send-keys", "-t", sh, c, "Enter")
                time.sleep(0.3)
            self.click("echo in-context", pane="shell", button=2)            # held, then released on the same spot
            time.sleep(0.5)
            self.at_terminal("m")                                   # "Mark start of context here"
            time.sleep(0.5)
            self.type("/share mark")
            self.wait_for("Share 2 lines of shell history from the mark with the model?")
            self.key("y")
            self.wait_for("attached 2 lines of shell history")
            self.type("what happened?")
            self.wait_for("seen it")
            sent = self.sent(api)
            self.assertIn("echo in-context\nin-context", sent)
            self.assertNotIn("before-mark", sent)

    def window(self):
        """Titles of the visible panes, top to bottom, and which one is active."""
        rows = self.tmux("list-panes", "-t", "hoplon:0", "-F", "#{pane_top} #{pane_left} #{pane_active} #{@hoplon_title}")
        rows = sorted((int(t), int(lf), a, title.split(" ")[0]) for t, lf, a, title in
                      (ln.split(" ", 3) for ln in rows.splitlines()))
        return [r[3] for r in rows if r[1] == 0], next(r[3] for r in rows if r[2] == "1")

    def show_shell(self):
        self.at_terminal("\x00")             # Ctrl-` as most terminals send it
        time.sleep(0.6)

    def test_ctrl_backtick_shows_and_hides_the_shell_from_any_pane(self):
        with FakeAPI([reply("hello")]) as api:
            self.start(api)
            self.assertEqual(self.window(), (["hoplon", "you"], "you"))          # the shell starts hidden
            self.type("hi")
            self.wait_for("● hello")
            self.type("a `` b", enter=False)                                  # backticks are only text now
            self.at_terminal("\x00")                                          # Ctrl-`, sent as NUL
            time.sleep(0.6)
            self.assertEqual(self.window(), (["shell", "hoplon", "you"], "shell"))   # above the conversation
            self.shell("echo ``x")
            self.assertIn("echo ``x", self.screen("shell"))
            self.at_terminal("\x00")                                          # from the shell: hides it
            time.sleep(0.6)
            self.assertEqual(self.window(), (["hoplon", "you"], "you"))
            self.assertIn("a `` b", self.screen("you"))                        # the input is untouched
            self.at_terminal("\x1b[96;5u")                                    # Ctrl-` reported in full
            time.sleep(0.6)
            self.assertEqual(self.window()[0], ["shell", "hoplon", "you"])

    def test_an_answer_must_name_the_question_showing(self):
        with FakeAPI([call("write_file", path="x.txt", content="x\n", reason="r"), reply("done")]) as api:
            self.start(api)
            self.type("go")
            self.wait_for("Allow p1: create x.txt?")
            state = [d for d in os.listdir(self.runtime) if d.startswith("hoplon-tui.")][0]
            c = IPC.Client(os.path.join(self.runtime, state), "prompt")
            self.addCleanup(c.close)
            for q in (None, "approve:p2", "ask:1"):                    # stale, early or made up
                c.send({"type": "key", "key": "y", "q": q})
            time.sleep(0.5)
            self.assertFalse(os.path.exists(os.path.join(self.wd, "x.txt")))
            c.send({"type": "key", "key": "y", "q": "approve:p1"})
            self.wait_for("● done")
            self.assertTrue(os.path.exists(os.path.join(self.wd, "x.txt")))

    def test_a_key_typed_as_a_question_appears_is_not_an_answer(self):
        with FakeAPI([call("write_file", path="x.txt", content="x\n", reason="r"), reply("done")]) as api:
            self.start(api)
            self.type("go")
            self.wait_for("Allow p1: create x.txt?")
            self.tmux("send-keys", "-t", self.panes()["you"], "y")      # at once: typing, not an answer
            time.sleep(0.5)
            self.assertFalse(os.path.exists(os.path.join(self.wd, "x.txt")))
            self.assertIn("Allow p1", self.screen("you"))
            self.key("y")
            self.wait_for("● done")
            self.assertTrue(os.path.exists(os.path.join(self.wd, "x.txt")))

    def test_busy_shell_is_never_typed_into(self):
        with FakeAPI([call("propose_command", command="echo should-not-run", reason="r")]) as api:
            self.start(api)
            self.type("go")
            self.wait_for("or /run p1, to send")
            self.tmux("send-keys", "-t", self.panes()["shell"], "sleep 5", "Enter")
            time.sleep(0.5)
            self.type("/run p1")
            self.wait_for("Run p1")
            self.key("y")
            self.wait_for("the shell pane is busy (sleep is running)")
            self.wait_for("/shell shows it")
            self.assertNotIn("should-not-run", self.screen("shell"))

    def test_edit_first_and_share_your_own_command(self):
        editor = os.path.join(tempfile.mkdtemp(prefix="hoplon-tui-ed-"), "ed")
        with open(editor, "w") as f:
            f.write("#!/bin/sh\nsed -i 's/echo original/echo edited/' \"$1\"\n")
        os.chmod(editor, 0o700)
        turns = [call("propose_command", command="echo original", reason="r"), reply("thanks")]
        with FakeAPI(turns) as api:
            self.start(api, editor=editor)
            self.type("go")
            self.wait_for("or /run p1, to send")
            self.type("/run")
            self.wait_for("Run p1")
            self.key("e")
            self.wait_for("Run p1 (edited)")
            self.wait_for("$ echo edited")                 # all of it is in the question: y runs it
            self.key("y")
            self.wait_for("Share the output")
            self.key("n")
            self.assertIn("edited", self.screen("shell"))
            self.assertNotIn("original", self.screen("shell"))
            self.tmux("send-keys", "-t", self.panes()["shell"], "echo by-hand", "Enter")
            time.sleep(0.5)
            self.type("/share")
            self.wait_for("Command 2 in the shell pane: echo by-hand (exit 0, 1 line)")
            self.key("y")
            self.wait_for("thanks")
            self.assertIn("```\necho by-hand\n```\nOutput:\n```\nby-hand\n```", self.sent(api))


if __name__ == "__main__":
    unittest.main()
