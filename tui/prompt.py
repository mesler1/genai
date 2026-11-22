#!/usr/bin/env python3
"""genai-tui prompt pane: where you type, below your shell.

Typing, history, paste and Tab completion happen here. What you send, and your answers to
approval and run questions, go to the conversation pane through genai-tui's private pipe
($GENAI_TUI_STATE/requests); the conversation pane owns genai and decides everything. It tells
this pane what to show in $GENAI_TUI_STATE/prompt.json. Double-tap ~ for a fullscreen shell.
Standard library only; Python 3.10 or newer.
"""

import curses
import json
import locale
import os
import subprocess
import sys
import time

from conversation import A, D, Q, R, SPIN, W, Editor, complete, edit_key, fit, width

STATE = os.environ.get("GENAI_TUI_STATE", "")
SHELL = os.environ.get("GENAI_TUI_SHELL", "")
TILDE_GAP = 0.4            # seconds between the two taps of ~~
SETTLE = 0.3               # keys this soon after a question appears are typing, not an answer


def send(msg):
    try:
        fd = os.open(os.path.join(STATE, "requests"), os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(fd, (json.dumps(msg) + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def tmux(*args):
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def shell_fullscreen():
    """The shell pane, zoomed to the whole window; ~~ there (or Ctrl-b z) comes back."""
    if SHELL:
        tmux("select-pane", "-t", SHELL)
        tmux("resize-pane", "-Z", "-t", SHELL)


class Prompt:
    def __init__(self, scr):
        self.scr, self.ed = scr, Editor()
        self.view, self.mtime, self.height, self.title = {}, None, 0, None
        self.paste, self.tilde, self.ctrl_c = None, 0.0, 0.0
        self.asked_at = 0.0
        curses.raw()
        curses.use_default_colors()
        self.roles = {"": 0}
        if curses.has_colors():
            quiet = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
            for i, (role, fg) in enumerate([(Q, quiet), (A, curses.COLOR_BLUE), (W, curses.COLOR_YELLOW),
                                            (D, curses.COLOR_GREEN), (R, curses.COLOR_RED)], 1):
                curses.init_pair(i, fg, -1)
                self.roles[role] = curses.color_pair(i)
        self.roles["bold"] = curses.A_BOLD
        self.roles["key"] = self.roles[A] | curses.A_BOLD | curses.A_REVERSE
        self.roles["question"] = self.roles[A] | curses.A_BOLD
        scr.keypad(True)
        scr.timeout(50)

    def load(self):
        try:
            m = os.stat(os.path.join(STATE, "prompt.json")).st_mtime_ns
            if m == self.mtime:
                return
            with open(os.path.join(STATE, "prompt.json"), encoding="utf-8") as f:
                view = json.load(f)
            if view.get("mode") in ("approval", "ask") and view.get("lines") != self.view.get("lines"):
                self.asked_at = time.time()
            self.view, self.mtime = view, m
        except (OSError, ValueError):
            pass

    def lines(self, w):
        """What to draw: content lines, then the hint line, and the cursor (or None)."""
        v = self.view
        if v.get("mode") in ("approval", "ask"):
            return [[(t, r) for t, r in segs] for segs in v.get("lines", [])], None
        rows, cur = self.ed.view(w)
        return [[(r[:2], A), (r[2:], "")] for r in rows[-8:]], (min(cur[0], 7), cur[1])

    def draw(self):
        h, w = self.scr.getmaxyx()
        content, cur = self.lines(w)
        want = min(max(len(content) + 2, 3), 14)
        if want != self.height:            # grow for a long question, shrink back for the input line
            self.height = want
            tmux("resize-pane", "-t", os.environ.get("TMUX_PANE", ""), "-y", str(want))
        self.scr.erase()
        for y, segs in enumerate(content[:max(h - 2, 1)]):
            x = 0
            for text, role in segs:
                try:
                    self.scr.addstr(y, x, fit(text, max(w - x, 1)), self.roles.get(role, 0))
                except curses.error:
                    pass
                x += width(text)
        v = self.view
        hints = v.get("hints") or "Enter send · Alt+Enter new line · ↑ earlier · ~~ shell fullscreen · /help"
        if v.get("mode", "input") == "input" and v.get("busy"):
            hints = f"{SPIN[int(time.time() * 10) % len(SPIN)]} genai is replying · type ahead, Enter queues it"
        try:
            self.scr.addstr(h - 1, 0, fit(hints, w - 1), self.roles[Q])
        except curses.error:
            pass
        title = "you" + (f" · {v['status']}" if v.get("status") else "")
        if title != self.title:
            self.title = title
            tmux("set", "-p", "-t", os.environ.get("TMUX_PANE", ""), "@genai_title", title)
        if cur is not None:
            curses.curs_set(1)
            try:
                self.scr.move(cur[0], min(cur[1], w - 1))
            except curses.error:
                pass
        else:
            curses.curs_set(0)
        self.scr.refresh()

    def read_escape(self):
        self.scr.timeout(0)
        seq = ""
        try:
            while len(seq) < 8:
                try:
                    c = self.scr.get_wch()
                except curses.error:
                    break
                seq += c if isinstance(c, str) else ""
                if seq in ("\r", "\n") or (len(seq) > 1 and seq[-1].isalpha() or seq.endswith("~")):
                    break
        finally:
            self.scr.timeout(50)
        return seq

    def key(self, k):
        ed = self.ed
        if self.paste is not None:
            self.paste += k if isinstance(k, str) else ""
            if self.paste.endswith("\x1b[201~"):
                ed.insert(self.paste[:-6].replace("\r\n", "\n").replace("\r", "\n"))
                self.paste = None
            return
        asking = self.view.get("mode") in ("approval", "ask")
        if k == "\x1b":
            seq = self.read_escape()
            if seq in ("\r", "\n") and not asking:
                ed.insert("\n")
            elif seq == "[200~":
                self.paste = ""
            elif seq == "" and asking:
                self.answer("esc")
            return
        if k in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            send({"type": "scroll", "up": k == curses.KEY_PPAGE})
            return
        if asking:
            if isinstance(k, str) and k.isprintable() and time.time() - self.asked_at >= SETTLE:
                self.answer(k.lower())
            return
        if k == "~":
            now = time.time()
            if now - self.tilde < TILDE_GAP and ed.text[ed.pos - 1:ed.pos] == "~":
                ed.backspace()
                self.tilde = 0.0
                shell_fullscreen()
                return
            self.tilde = now
        if k in ("\n", "\r", curses.KEY_ENTER):
            if ed.text.endswith("\\") and ed.pos == len(ed.text):
                ed.backspace()
                ed.insert("\n")
            elif ed.text.strip():
                text = ed.take()
                if not send({"type": "input", "text": text}):
                    ed.insert(text)                 # the conversation pane isn't listening: keep it
            return
        if k == "\x04" and not ed.text:
            send({"type": "quit"})
            return
        if k == "\x03":
            now = time.time()
            if ed.text:
                ed.take()
            elif now - self.ctrl_c < 2:
                send({"type": "quit"})
            self.ctrl_c = now
            return
        if k == "\t":
            ed.text = complete(ed.text)
            ed.pos = len(ed.text)
            return
        edit_key(ed, k)

    def answer(self, k):
        """An answer to the question showing. One that settles it puts the input box back at once, so whatever
        is typed next is text, never an answer to the next question before it has been seen."""
        send({"type": "key", "key": k})
        if k in ("y", "n", "esc"):
            self.view = dict(self.view, mode="input", lines=[], hints="")

    def run(self):
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
        try:
            while True:
                self.load()
                self.draw()
                try:
                    k = self.scr.get_wch()
                except curses.error:
                    continue
                if k != curses.KEY_RESIZE:
                    self.key(k)
        finally:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()


def main():
    if not STATE:
        print("prompt.py is started by genai-tui", file=sys.stderr)
        return 2
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: Prompt(scr).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
