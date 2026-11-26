#!/usr/bin/env python3
"""hoplon-tui prompt pane: where you type, below your shell.

Typing, history, paste and Tab completion happen here. What you send, and your answers to
approval and run questions, go to the conversation pane over hoplon-tui's private socket (see
ipc.py); the conversation pane owns hoplon and decides everything, and pushes back what this pane
should show. Every answer names the question it answers. Ctrl-` (a tmux binding, set by hoplon-tui) shows
your shell above the conversation, or hides it again.
Standard library only; Python 3.10 or newer.
"""

import curses
import locale
import os
import queue
import subprocess
import sys
import time

import palette
from conversation import A, D, Q, R, SPIN, W, Editor, complete, edit_key, fit, width
from ipc import Client

STATE = os.environ.get("HOPLON_TUI_STATE", "")
SHELL = os.environ.get("HOPLON_TUI_SHELL", "")
SETTLE = 0.3               # keys this soon after a question appears are typing, not an answer


def tmux(*args):
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


class Prompt:
    def __init__(self, scr, link):
        self.scr, self.ed, self.link = scr, Editor(), link
        self.view, self.height, self.title = {}, 0, None
        self.paste, self.ctrl_c = None, 0.0
        self.asked_at = 0.0
        curses.raw()
        self.roles = palette.roles(curses)
        self.roles["bold"] = curses.A_BOLD
        self.roles["key"] = self.roles[A] | curses.A_BOLD | curses.A_REVERSE
        self.roles["question"] = self.roles[A] | curses.A_BOLD
        scr.keypad(True)
        scr.timeout(50)

    def load(self):
        """Take whatever the conversation pane has pushed since the last look."""
        while True:
            try:
                msg = self.link.inbox.get_nowait()
            except queue.Empty:
                return
            if msg is None:                    # the conversation pane has gone; hoplon-tui is closing
                self.view = dict(self.view, mode="input", hints="the conversation has ended")
                continue
            if msg.get("type") == "insert":
                self.insert(msg.get("text"))
                continue
            view = msg.get("view") if msg.get("type") == "view" else None
            if not isinstance(view, dict):
                continue
            if view.get("mode") in ("approval", "ask") and view.get("lines") != self.view.get("lines"):
                self.asked_at = time.time()
            self.view = view

    def insert(self, text):
        """A path from the file tree, at the cursor: only into the input box, never as an answer, and only text
        that is printable."""
        if (self.view.get("mode") not in ("approval", "ask") and isinstance(text, str) and 0 < len(text) <= 4096
                and text.isprintable()):
            self.ed.insert(text)

    def send(self, msg):
        return self.link.send(msg)

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
        hints = v.get("hints") or "Enter send · Alt+Enter new line · ↑ earlier · Ctrl-` shell · /help"
        if v.get("mode", "input") == "input" and v.get("running"):
            hints = f"{SPIN[int(time.time() * 10) % len(SPIN)]} a sandboxed run is going · Ctrl-C or /stop stops it"
        elif v.get("mode", "input") == "input" and v.get("busy"):
            doing = v.get("doing") if isinstance(v.get("doing"), str) else "hoplon is replying"
            hints = f"{SPIN[int(time.time() * 10) % len(SPIN)]} {doing} · type ahead, Enter queues it"
        try:
            self.scr.addstr(h - 1, 0, fit(hints, w - 1), self.roles[Q])
        except curses.error:
            pass
        title = "you" + (f" · {v['status']}" if v.get("status") else "")
        if title != self.title:
            self.title = title
            tmux("set", "-p", "-t", os.environ.get("TMUX_PANE", ""), "@hoplon_title", title)
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
            self.send({"type": "scroll", "up": k == curses.KEY_PPAGE})
            return
        if asking and k == "\x03" and self.view.get("halt"):
            self.send({"type": "stop"})             # the turn stops; what waits for an answer is declined
            return
        if asking:
            if isinstance(k, str) and k.isprintable() and time.time() - self.asked_at >= SETTLE:
                self.answer(k.lower())
            return
        if k in ("\n", "\r", curses.KEY_ENTER):
            if ed.text.endswith("\\") and ed.pos == len(ed.text):
                ed.backspace()
                ed.insert("\n")
            elif ed.text.strip():
                text = ed.take()
                if not self.send({"type": "input", "text": text}):
                    ed.insert(text)                 # the conversation pane isn't listening: keep it
            return
        if k == "\x04" and not ed.text:
            self.send({"type": "quit"})
            return
        if k == "\x03":
            now = time.time()
            if ed.text:
                ed.take()
            elif self.view.get("running"):
                self.send({"type": "stop"})       # stops the sandboxed run; it doesn't count towards quitting
                now = 0.0
            elif self.view.get("halt") and now - self.ctrl_c >= 2:
                self.send({"type": "stop"})       # stops hoplon's turn; a second Ctrl-C soon after still quits
            elif now - self.ctrl_c < 2:
                self.send({"type": "quit"})
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
        self.send({"type": "key", "key": k, "q": self.view.get("q")})
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
        print("prompt.py is started by hoplon-tui", file=sys.stderr)
        return 2
    try:
        link = Client(STATE, "prompt")
    except OSError as e:
        print(f"prompt.py: can't reach the conversation pane: {e}", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: Prompt(scr, link).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
