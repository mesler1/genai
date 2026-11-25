#!/usr/bin/env python3
"""genai-tui's pager: a text file genai-tui wrote, shown in a popup until you close it.

genai-tui cleans what it shows before writing it, so the file holds no control characters; this pager draws it
as plain text all the same (each line through clean()), never through the terminal raw. Esc or q closes it,
which less can't do: in less, Esc starts other commands and waits for the next key.
Keys: ↑↓ j k, PgUp PgDn space b, Home End g G, the mouse wheel; / searches (case-insensitive), n and N find the
next and the previous match. Long lines wrap. Standard library only; Python 3.10 or newer.

Usage: pager.py FILE
"""

import curses
import locale
import os
import sys

from conversation import A, Q, clean, cut, width

HINT = "Esc or q closes · ↑↓ PgUp PgDn scroll · / searches, n N next"


def rows(lines, w):
    """The file's lines wrapped to w cells, as (line number, piece) for each screen row."""
    out = []
    w = max(w, 1)
    for n, line in enumerate(lines):
        rest = clean(line).replace("\n", " ")
        if not rest:
            out.append((n, ""))
        while rest:
            piece = cut(rest, w)
            out.append((n, piece))
            rest = rest[len(piece):]
    return out


def find(lines, term, start, step):
    """The next line from start (step 1) or before it (step -1) holding term, any case; None if there is none."""
    term = term.lower()
    n = len(lines)
    for i in range(1, n + 1):
        k = (start + step * i) % n
        if term in clean(lines[k]).lower():
            return k
    return None


class Pager:
    def __init__(self, scr, lines):
        self.scr, self.lines = scr, lines or [""]
        self.top, self.term, self.note, self.hit = 0, "", "", None
        curses.curs_set(0)
        curses.use_default_colors()
        self.roles = {"": 0, Q: curses.A_DIM, A: curses.A_BOLD}
        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_BLUE, -1)
            self.roles[A] = curses.color_pair(1) | curses.A_BOLD
        curses.mousemask(getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON5_PRESSED", 0x200000))
        scr.keypad(True)

    def view(self):
        h, w = self.scr.getmaxyx()
        return rows(self.lines, w - 1), max(h - 1, 1), w

    def draw(self):
        view, page, w = self.view()
        self.top = max(min(self.top, len(view) - page), 0)
        self.scr.erase()
        for y, (n, piece) in enumerate(view[self.top:self.top + page]):
            role = A if self.hit == n else ""
            try:
                self.scr.addstr(y, 0, piece, self.roles[role])
            except curses.error:
                pass
        end = min(self.top + page, len(view))
        where = "all" if len(view) <= page else f"{100 * end // len(view)}%"
        status = f" {self.note or HINT}  ·  {where} "
        try:
            self.scr.addstr(page, 0, cut(status, w - 1), curses.A_REVERSE)
        except curses.error:
            pass
        self.scr.refresh()

    def ask(self, prompt):
        """A line typed on the status row: the text, or None if Esc cancelled it."""
        _, page, w = self.view()
        text = ""
        curses.curs_set(1)
        try:
            while True:
                shown = prompt + text
                self.scr.move(page, 0)
                self.scr.clrtoeol()
                self.scr.addstr(page, 0, cut(shown, w - 1))
                self.scr.move(page, min(width(shown), w - 1))
                k = self.scr.get_wch()
                if k in ("\n", "\r", curses.KEY_ENTER):
                    return text
                if k == "\x1b":
                    return None
                if k in ("\x7f", "\b", curses.KEY_BACKSPACE):
                    text = text[:-1]
                elif isinstance(k, str) and k.isprintable() and len(text) < 200:
                    text += k
        finally:
            curses.curs_set(0)

    def jump(self, step):
        """Show the next (1) or previous (-1) line matching the search, if there is one."""
        if not self.term:
            self.note = "nothing to find: / searches"
            return
        view, _, _ = self.view()
        here = self.hit if self.hit is not None else (view[self.top][0] if view else 0) - (step == 1)
        k = find(self.lines, self.term, here, step)
        if k is None:
            self.note, self.hit = f"not found: {clean(self.term)}", None
            return
        self.hit, self.note = k, ""
        self.top = next(i for i, (n, _) in enumerate(view) if n == k)

    def key(self, k):
        """Act on a key; False when the pager should close."""
        view, page, _ = self.view()
        self.note = ""
        if k in ("q", "Q", "\x1b"):
            return False
        moves = {curses.KEY_DOWN: 1, "j": 1, "\n": 1, curses.KEY_UP: -1, "k": -1,
                 curses.KEY_NPAGE: page - 1, " ": page - 1, "f": page - 1,
                 curses.KEY_PPAGE: -(page - 1), "b": -(page - 1)}
        if k in moves:
            self.top += moves[k]
        elif k in (curses.KEY_HOME, "g"):
            self.top = 0
        elif k in (curses.KEY_END, "G"):
            self.top = len(view)
        elif k == "/":
            term = self.ask("/")
            if term:
                self.term, self.hit = term, None
                self.jump(1)
        elif k == "n":
            self.jump(1)
        elif k == "N":
            self.jump(-1)
        elif k == curses.KEY_MOUSE:
            try:
                _, _, _, _, b = curses.getmouse()
            except curses.error:
                return True
            if b & getattr(curses, "BUTTON4_PRESSED", 0):
                self.top -= 3
            elif b & getattr(curses, "BUTTON5_PRESSED", 0x200000):
                self.top += 3
        self.top = max(self.top, 0)
        return True

    def run(self):
        while True:
            self.draw()
            try:
                k = self.scr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                return
            if k != curses.KEY_RESIZE and not self.key(k):
                return


def main(argv):
    if len(argv) != 1:
        print(__doc__.split("\n\n")[-1].strip(), file=sys.stderr)
        return 2
    try:
        with open(argv[0], encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError as e:
        print(f"pager.py: {e}", file=sys.stderr)
        return 1
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")      # so Esc on its own closes at once, not after a second
    for stale in ("LINES", "COLUMNS"):           # curses prefers them to the pane's real size; readline exports
        os.environ.pop(stale, None)              # them once, and they go stale as soon as the pane is resized
    curses.wrapper(lambda scr: Pager(scr, lines).run())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
