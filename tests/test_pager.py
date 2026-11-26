"""Tests for hoplon-tui's pager (tui/pager.py): wrapping, search, and that Esc or q closes it at once.
Run: python3 -m unittest discover -s tests"""

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tui"))


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


P = load("pager", os.path.join(REPO, "tui", "pager.py"))
C = load("conversation_for_pager", os.path.join(REPO, "tui", "conversation.py"))


class TestText(unittest.TestCase):
    def test_lines_wrap_and_control_characters_stay_escaped(self):
        rows = P.rows(["short", "x" * 25, "", "a\x1b[2Jb"], 10)
        self.assertEqual(rows, [(0, "short"), (1, "x" * 10), (1, "x" * 10), (1, "x" * 5), (2, ""),
                                (3, "a\\x1b[2Jb")])

    def test_search_finds_next_and_previous_any_case_and_wraps(self):
        lines = ["one", "Needle", "two", "needle again", "three"]
        self.assertEqual(P.find(lines, "NEEDLE", -1, 1), 1)
        self.assertEqual(P.find(lines, "needle", 1, 1), 3)
        self.assertEqual(P.find(lines, "needle", 3, 1), 1)          # wraps round
        self.assertEqual(P.find(lines, "needle", 3, -1), 1)
        self.assertIsNone(P.find(lines, "missing", 0, 1))

    def test_pager_is_ours_unless_one_is_set(self):
        argv = C.pager_argv("/tmp/x.txt")
        self.assertEqual((argv[0], os.path.basename(argv[1]), argv[2]), (sys.executable, "pager.py", "/tmp/x.txt"))
        os.environ["HOPLON_TUI_PAGER"] = "less -R"
        try:
            self.assertEqual(C.pager_argv("/tmp/x.txt"), ["less", "-R", "/tmp/x.txt"])
        finally:
            del os.environ["HOPLON_TUI_PAGER"]


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class TestInTmux(unittest.TestCase):
    """The pager for real, in a private tmux server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hoplon-pager-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sock = f"hoplon-pager-test-{os.getpid()}"
        self.addCleanup(self.tmux, "kill-server")
        self.file = os.path.join(self.tmp, "view.txt")
        with open(self.file, "w") as f:
            f.write("".join(f"line {i}\n" for i in range(1, 201)) + "the needle is here\n")

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.sock, *args], capture_output=True, text=True).stdout

    def start(self, env=""):
        self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "p", "-x", "80", "-y", "20",
                  f"env {env}LANG=C.UTF-8 TERM=screen-256color {sys.executable} {REPO}/tui/pager.py {self.file}; "
                  "echo CLOSED; sleep 30")
        self.wait("line 1")

    def screen(self):
        return self.tmux("capture-pane", "-p", "-t", "p")

    def wait(self, text, timeout=5, line=False):
        """Until text is on the screen; with line, as a whole line ("line 1" is not "line 190")."""
        end = time.time() + timeout
        while time.time() < end:
            screen = self.screen()
            if (text in [ln.rstrip() for ln in screen.splitlines()]) if line else (text in screen):
                return
            time.sleep(0.05)
        self.fail(f"{text!r} never appeared:\n{self.screen()}")

    def test_esc_closes_at_once(self):
        self.start()
        self.assertIn("Esc or q closes", self.screen())
        began = time.time()
        self.tmux("send-keys", "-t", "p", "Escape")
        self.wait("CLOSED", timeout=2)
        self.assertLess(time.time() - began, 1.0)

    def test_q_closes_and_search_moves_to_the_match(self):
        self.start()
        self.tmux("send-keys", "-t", "p", "/", "needle", "Enter")
        self.wait("the needle is here")
        self.assertNotIn("line 1\n", self.screen())
        self.tmux("send-keys", "-t", "p", "g")
        self.wait("line 1", line=True)                  # the top again, not just "line 190" at the match
        self.tmux("send-keys", "-t", "p", "q")
        self.wait("CLOSED")

    def test_stale_lines_and_columns_in_the_environment_are_ignored(self):
        self.start("LINES=24 COLUMNS=80 ")           # readline exports these, and children inherit them
        self.tmux("send-keys", "-t", "p", "/", "needle", "Enter")
        self.wait("the needle is here")                  # in the 20-row pane, not on a 24th row it doesn't have

    def test_esc_in_the_search_prompt_cancels_it_and_keeps_the_pager(self):
        self.start()
        self.tmux("send-keys", "-t", "p", "/", "nee", "Escape")
        time.sleep(0.3)
        self.assertNotIn("CLOSED", self.screen())
        self.assertIn("Esc or q closes", self.screen())
        self.tmux("send-keys", "-t", "p", "Escape")
        self.wait("CLOSED", timeout=2)


if __name__ == "__main__":
    unittest.main()
