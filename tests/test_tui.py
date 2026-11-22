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
sys.path.insert(0, os.path.join(REPO, "tui"))
C_PROMPT = load("prompt", os.path.join(REPO, "tui", "prompt.py"))
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
        self.assertIn("genai-tui", text_of(m.blocks[-1]))
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

    def test_review_buffer_matches_genai(self):
        buf = C.review_buffer("ls", 2, "OUTSIDE any sandbox, directly on this host", "x")
        self.assertIn("I ran this command OUTSIDE any sandbox, directly on this host (exit 2):\n```\nls\n```\nOutput:", buf)
        self.assertEqual(C.strip_comments("# a\nkeep\n#b\n"), "keep")

    def test_options_split_between_the_tui_and_genai(self):
        a = C.parse_args(["-p", "hpc", "-S", "night", "-k", "slurm", "-k", "draft", "--coding", "--no-panel"])
        self.assertEqual(C.genai_args(a), ["-p", "hpc", "-S", "night", "-k", "slurm", "-k", "draft", "--coding"])
        self.assertEqual(C.genai_args(C.parse_args(["--coding", "--coding-anywhere"])), ["--coding-anywhere"])
        with self.assertRaises(SystemExit):
            C.parse_args(["--json"])                              # genai's own wrapper flags aren't options here

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
        for line in ("/share", "/share mark", "/share all", "/mark", "/share bogus"):
            m.submit(line)
        self.assertEqual(m.actions, [("share", "last"), ("share", "mark"), ("share", "all"), ("mark",)])
        self.assertIn("usage: /share", text_of(m.blocks[-1]))
        m.request({"type": "share", "what": "mark"})
        self.assertEqual(m.actions[-1], ("share", "mark"))

    def test_failed_turn_ends_busy(self):
        m = C.Model()
        m.submit("hi")
        m.handle({"type": "error", "code": "api", "message": "endpoint error"})
        self.assertFalse(m.busy)
        m.submit("again")
        self.assertTrue(m.busy)


class TmuxCase(unittest.TestCase):
    """Helpers for driving a private tmux server; no tests of its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="genai-tui-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sock = f"genai-tui-test-{os.getpid()}"
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
        return home


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class TestInTmux(TmuxCase):
    """The pane for real: genai --json against the fake endpoint, typed into through tmux."""

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
class TestGenaiTui(TmuxCase):
    """tui/genai-tui itself: the conversation pane above a real bash pane, started through a pseudo-terminal."""

    def start(self, api, editor="true"):
        home = self.config(api)
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home), HOME=self.tmp, LANG="C.UTF-8",
                   TERM="xterm-256color", GENAI_TUI_SOCKET=self.sock, EDITOR=editor, VISUAL=editor)
        env.pop("TMUX", None)
        self.proc = subprocess.Popen(["script", "-qfc", f"stty cols 110 rows 40; {REPO}/tui/genai-tui -e test",
                                      "/dev/null"], cwd=self.wd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (self.tmux("kill-server"), self.proc.wait(10)))
        end = time.time() + 10
        while time.time() < end and not {"genai", "you", "shell"} <= set(self.panes()):
            time.sleep(0.1)
        self.wait_for("Type a message")

    def panes(self):
        out = self.tmux("list-panes", "-a", "-F", "#{pane_id} #{@genai_title}")
        return {t.split(" ")[0]: p for p, _, t in (ln.partition(" ") for ln in out.splitlines())}

    def screen(self, pane=None):
        """One pane, or the conversation and the prompt pane together."""
        names = [pane] if pane else ["genai", "you"]
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

    def click(self, text):
        """A left click (press and release, as a mouse sends them) on the first conversation row showing text."""
        y = next(i for i, ln in enumerate(self.screen("genai").splitlines()) if text in ln)
        xy = [f"{33 + 4:02x}", f"{33 + y:02x}"]
        self.tmux("send-keys", "-t", self.panes()["genai"], "-H", "1b", "5b", "4d", "20", *xy, "1b", "5b", "4d", "23", *xy)

    def sent(self, api):
        return [m for m in api.requests[-1]["body"]["messages"] if m["role"] == "user"][-1]["content"]

    def test_run_in_the_shell_pane_then_share(self):
        turns = [call("propose_command", command="echo from-shell; (exit 3)", reason="look"), reply("exit 3, I see.")]
        with FakeAPI(turns) as api:
            self.start(api)
            self.type("look around")
            self.wait_for("click it, or /run p1")
            self.type("/run")
            where = G.where_runs(G.sandbox_info())
            self.wait_for("Run p1 in the shell pane?")
            self.assertIn(where[:20], self.screen())
            self.key("y")
            self.wait_for("p1 finished (exit 3). Share the output with the model?")
            self.assertIn("from-shell", self.screen("shell"))
            self.key("y")                                          # EDITOR=true saves the buffer unchanged
            self.wait_for("exit 3, I see.")
            sent = self.sent(api)
            self.assertIn(f"I ran this command {where} (exit 3):", sent)
            self.assertIn("Output:\n```\nfrom-shell\n```", sent)
            self.assertIn("[output of p1 (exit 3)]", sent)

    def test_click_a_command_for_its_menu(self):
        turns = [call("propose_command", command="echo via-menu", reason="r"),   # a command ends the turn
                 call("propose_command", command="echo at-prompt", reason="r")]
        with FakeAPI(turns) as api:
            self.start(api)
            self.type("go")
            self.wait_for("click it, or /run p1")
            self.click("echo via-menu")
            time.sleep(0.5)
            self.at_terminal("r")                                  # "Run it in the shell pane"
            self.wait_for("p1 finished (exit 0). Share the output with the model?")
            self.assertIn("via-menu", self.screen("shell"))
            self.key("n")
            self.type("again")
            self.wait_for("click it, or /run p2")
            self.click("echo at-prompt")
            time.sleep(0.5)
            self.at_terminal("c")                                  # "Copy it"
            self.wait_for("p2 copied")
            self.assertEqual(self.tmux("show-buffer", "-b", "genai-copy"), "echo at-prompt")
            self.click("echo at-prompt")
            time.sleep(0.5)
            self.at_terminal("p")                                  # "Put it at the shell prompt"
            self.wait_for("p2 is at the shell prompt")
            time.sleep(0.3)
            last = [ln for ln in self.screen("shell").splitlines() if ln.strip()][-1]
            self.assertTrue(last.endswith("echo at-prompt"), last)   # typed, not run

    def test_info_panel_switches_coding_and_skills(self):
        with FakeAPI([reply("hi")]) as api:
            home = os.path.join(self.tmp, "cfg", "genai")
            os.makedirs(os.path.join(home, "skills", "slurm"))
            with open(os.path.join(home, "skills", "slurm", "SKILL.md"), "w") as f:
                f.write("# Slurm help\n")
            self.start(api)
            sk = os.path.join(home, "skills", "slurm")
            G.Config(home).set_blessed(sk, {"kind": "skill", "name": "slurm", "hash": G.hash_skill(sk)[0]})
            self.tmux("send-keys", "-t", self.panes()["info"], "r")
            self.wait_for("[ ] slurm", pane="info")
            self.assertIn("run OUTSIDE any sandbox" if not G.sandbox_info() else "run in", self.screen("info"))
            self.tmux("send-keys", "-t", self.panes()["info"], "c")
            self.wait_for("coding mode is on")
            self.wait_for("[on]", pane="info")
            self.tmux("send-keys", "-t", self.panes()["info"], " ")
            self.wait_for("[x] slurm", pane="info")
            self.type("/panel")
            time.sleep(0.5)
            self.assertNotIn("info", [t.split(" ")[0] for t in
                                      self.tmux("list-panes", "-t", "genai:0", "-F", "#{@genai_title}").split()])

    def test_right_click_marks_shell_context(self):
        with FakeAPI([reply("seen it")]) as api:
            self.start(api)
            sh = self.panes()["shell"]
            for c in ("echo before-mark", "echo in-context"):
                self.tmux("send-keys", "-t", sh, c, "Enter")
                time.sleep(0.3)
            y = next(i for i, ln in enumerate(self.screen("shell").splitlines()) if ln.endswith("echo in-context"))
            top = int(self.tmux("display", "-p", "-t", sh, "#{pane_top}"))
            self.at_terminal(f"\x1b[<2;5;{top + y + 1}M")              # a right-click, as the terminal reports it
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

    def test_layout_and_double_backtick(self):
        with FakeAPI([reply("hello")]) as api:
            self.start(api)
            tops = {t.split(" ")[0]: int(top) for top, _, t in
                    (ln.partition(" ") for ln in self.tmux("list-panes", "-F", "#{pane_top} #{@genai_title}").splitlines())}
            self.assertLess(tops["genai"], tops["shell"])
            self.assertLess(tops["shell"], tops["you"])                # the prompt is below the shell
            self.assertEqual(self.tmux("display", "-p", "-t", "genai", "#{@genai_title}").split()[0], "you")
            self.type("hi")
            self.wait_for("● hello")
            zoomed = lambda: self.tmux("display", "-p", "-t", "genai", "#{window_zoomed_flag}").strip()
            self.type("`", enter=False)
            time.sleep(0.8)                                            # however slowly it is typed
            self.type("`", enter=False)
            time.sleep(0.5)
            self.assertEqual(zoomed(), "1")                            # the shell, fullscreen
            self.tmux("send-keys", "-t", self.panes()["shell"], "-l", "``")
            time.sleep(1)
            self.assertEqual(zoomed(), "0")
            self.assertEqual(self.tmux("display", "-p", "-t", "genai", "#{@genai_title}").split()[0], "you")
            self.assertNotIn("`", self.screen("you").splitlines()[0])  # the taps didn't stay in the input
            self.type("a ``code`` b", enter=False)                     # not on an empty input: just text
            time.sleep(0.3)
            self.assertEqual(zoomed(), "0")
            self.assertIn("a ``code`` b", self.screen("you"))
            self.tmux("send-keys", "-t", "genai", "C-u")
            sh = self.panes()["shell"]
            self.tmux("send-keys", "-t", sh, "-l", "echo ``x")          # mid-command in the shell: typed as is
            time.sleep(1)
            self.assertEqual(zoomed(), "0")
            self.assertIn("echo ``x", self.screen("shell"))

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
            self.wait_for("click it, or /run p1")
            self.tmux("send-keys", "-t", self.panes()["shell"], "sleep 5", "Enter")
            time.sleep(0.5)
            self.type("/run p1")
            self.wait_for("Run p1")
            self.key("y")
            self.wait_for("the shell pane is busy (sleep is running)")
            self.assertNotIn("should-not-run", self.screen("shell"))

    def test_edit_first_and_share_your_own_command(self):
        editor = os.path.join(tempfile.mkdtemp(prefix="genai-tui-ed-"), "ed")
        with open(editor, "w") as f:
            f.write("#!/bin/sh\nsed -i 's/echo original/echo edited/' \"$1\"\n")
        os.chmod(editor, 0o700)
        turns = [call("propose_command", command="echo original", reason="r"), reply("thanks")]
        with FakeAPI(turns) as api:
            self.start(api, editor=editor)
            self.type("go")
            self.wait_for("click it, or /run p1")
            self.type("/run")
            self.wait_for("Run p1")
            self.key("e")
            self.wait_for("Run p1 (edited)")
            self.key("y")
            self.wait_for("Share the output")
            self.key("n")
            self.assertIn("edited", self.screen("shell"))
            self.assertNotIn("original", self.screen("shell"))
            self.tmux("send-keys", "-t", self.panes()["shell"], "echo by-hand", "Enter")
            time.sleep(0.5)
            self.type("/share")
            self.wait_for("Last command in the shell pane: echo by-hand (exit 0)")
            self.key("y")
            self.wait_for("thanks")
            self.assertIn("```\necho by-hand\n```\nOutput:\n```\nby-hand\n```", self.sent(api))


if __name__ == "__main__":
    unittest.main()
