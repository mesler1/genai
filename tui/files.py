#!/usr/bin/env python3
"""genai-tui files pane: the working directory as a tree, beside the conversation.

This pane never reads the disk itself. It connects to the conversation pane's private socket (see ipc.py)
in the "files" role and asks for one directory's entries at a time ("list"). The role may only ask the
conversation pane to show a file in the pager ("view"), attach a file to your next message once you have
reviewed it in your editor ("attach"), or put a path in the input box ("insert"): it can never approve
anything or reach genai directly. Everything it receives is treated as untrusted: entries are validated,
names are cleaned before they are drawn, and at most MAX_ENTRIES are kept per directory.
Standard library only; Python 3.10 or newer.
"""

import curses
import locale
import os
import queue
import sys

from conversation import A, D, Q, R, W, clean, fit, width
from ipc import Client

STATE = os.environ.get("GENAI_TUI_STATE", "")
MAX_ENTRIES = 2000
KINDS = ("file", "dir", "symlink", "other")
HINT = "↑↓ move · →← open/close · Enter view · a attach · i insert path · . hidden · r refresh"


def human(n):
    """A size in a few cells: 512, 4.0K, 12K, 1.2M."""
    if n < 1024:
        return str(n)
    n = float(n)
    for unit in "KMGT":
        n /= 1024
        if n < 1024 or unit == "T":
            return f"{n:.1f}{unit}" if n < 10 else f"{n:.0f}{unit}"


def valid_path(p):
    """A relative path as the pane sends it: "." or components joined with "/", never absolute or containing "..". """
    return isinstance(p, str) and (p == "." or (bool(p) and not p.startswith("/")
                                                 and all(c not in ("", ".", "..") for c in p.split("/"))))


def entry_of(raw):
    """One received entry made safe, or None if it is malformed."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\0" in name:
        return None
    kind = raw.get("kind") if raw.get("kind") in KINDS else "other"
    size = raw.get("size")
    return {"name": name, "kind": kind, "size": size if type(size) is int and size >= 0 else None,
            "hidden": raw.get("hidden") is True or name.startswith("."),
            "secret": raw.get("secret") if raw.get("secret") in (True, False) else None,
            "asks": raw.get("asks") is True}


def label(text):
    """A name or message as one drawable line (clean() lets newlines through, and a file name may hold one)."""
    return clean(text).replace("\n", "\\n")


def sort_key(e):
    return (e["kind"] != "dir", e["name"].lower(), e["name"])


def join(parent, name):
    return name if parent == "." else f"{parent}/{name}"


def parent_of(path):
    return "." if "/" not in path else path.rsplit("/", 1)[0]


class Tree:
    """What the pane knows, without curses: listings by path, which directories are open, what is selected.

    key() and click() return the messages to send to the conversation pane (a list, often empty); anything
    for the hint line is left in note."""

    def __init__(self):
        self.listings = {}       # path -> {"entries": [...], "truncated": bool}
        self.errors = {}         # path -> message
        self.pending = {"."}     # paths asked for and not yet answered
        self.expanded = set()
        self.show_hidden = True
        self.sel = None          # path of the selected entry
        self.unavailable = None
        self.ended = False
        self.page = 10
        self.note = ""

    def start(self):
        return [{"type": "list", "path": "."}]

    def receive(self, msg):
        """Take one message from the inbox (None means the conversation has ended)."""
        if msg is None:
            self.ended = True
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "unavailable":
            reason = msg.get("reason")
            self.unavailable = reason if isinstance(reason, str) else "files are not available"
        elif msg.get("type") == "listing":
            path = msg.get("path")
            if not valid_path(path) or path not in self.pending:
                return
            self.pending.discard(path)
            self.unavailable = None
            if isinstance(msg.get("error"), str):
                self.errors[path] = msg["error"]
                self.listings.pop(path, None)
                return
            entries = msg.get("entries")
            if not isinstance(entries, list):
                return
            seen = {}
            for raw in entries[:MAX_ENTRIES]:
                e = entry_of(raw)
                if e and e["name"] not in seen:
                    seen[e["name"]] = e
            self.errors.pop(path, None)
            self.listings[path] = {"entries": sorted(seen.values(), key=sort_key),
                                   "truncated": msg.get("truncated") is True or len(entries) > MAX_ENTRIES}

    def ask(self, path):
        self.pending.add(path)
        self.errors.pop(path, None)
        return {"type": "list", "path": path}

    # -- what is on screen

    def walk(self, parent=".", depth=0):
        """The visible rows as dicts: entry, loading, error or more."""
        out = []
        lst = self.listings.get(parent)
        if lst is None:
            if parent in self.errors:
                out.append({"t": "error", "depth": depth, "text": self.errors[parent]})
            else:
                out.append({"t": "loading", "depth": depth})
            return out
        for e in lst["entries"]:
            if e["hidden"] and not self.show_hidden:
                continue
            path = join(parent, e["name"])
            out.append({"t": "entry", "depth": depth, "e": e, "path": path})
            if e["kind"] == "dir" and path in self.expanded:
                out += self.walk(path, depth + 1)
        if lst["truncated"]:
            out.append({"t": "more", "depth": depth})
        return out

    def entries(self):
        return [r for r in self.walk() if r["t"] == "entry"]

    def rows(self, w):
        """(segments, ref) per row; segments are (text, role). ref is None for rows that can't be selected,
        else {"path", "kind", "name"}."""
        out = []
        for r in self.walk():
            pad = "  " * r["depth"]
            if r["t"] == "loading":
                out.append(([(pad + "  …", Q)], None))
            elif r["t"] == "error":
                out.append(([(pad + "  ", ""), (fit(label(r["text"]), max(w - len(pad) - 2, 1)), R)], None))
            elif r["t"] == "more":
                out.append(([(pad + "  … more not listed", Q)], None))
            else:
                e = r["e"]
                out.append((self.entry_row(r, w), {"path": r["path"], "kind": e["kind"], "name": e["name"]}))
        return out

    def entry_row(self, r, w):
        e, pad = r["e"], "  " * r["depth"]
        kind = e["kind"]
        name_role = Q if e["hidden"] or kind in ("symlink", "other") else ""
        size = human(e["size"]) if kind == "file" and e["size"] is not None and w >= 28 else ""
        limit = w - (6 if size else 0)
        if kind == "dir":
            mark, tail = [("▾ " if r["path"] in self.expanded else "▸ ", Q if e["hidden"] else A)], "/"
        elif kind == "symlink":
            mark, tail = [("↪ ", Q)], " (not followed)"
        elif e["secret"]:
            mark, tail = [("⚠ ", R)], ""
        elif e["asks"]:
            mark, tail = [("⚿ ", W)], ""
        else:
            mark, tail = [("  ", "")], ""
        room = max(limit - width(pad) - 2 - width(tail), 1)
        segs = [(pad, "")] + mark + [(fit(label(e["name"]), room), name_role)]
        if tail:
            segs.append((tail, Q))
        if size:
            used = sum(width(t) for t, _ in segs)
            segs.append((" " * max(w - used - len(size), 1) + size, Q))
        return segs

    # -- selection and keys

    def current(self):
        """The selected entry row, or None. A selection that is no longer visible falls back to the first row."""
        rows = self.entries()
        for r in rows:
            if r["path"] == self.sel:
                return r
        self.sel = rows[0]["path"] if rows else None
        return rows[0] if rows else None

    def move(self, to):
        rows = self.entries()
        cur = self.current()
        if cur is None:
            return
        i = [r["path"] for r in rows].index(cur["path"])
        self.sel = rows[min(max(to(i), 0), len(rows) - 1)]["path"]

    def expand(self, row):
        if row["e"]["kind"] != "dir" or row["path"] in self.expanded:
            return []
        self.expanded.add(row["path"])
        if row["path"] in self.listings or row["path"] in self.pending:
            return []
        return [self.ask(row["path"])]

    def collapse(self, row):
        if row["e"]["kind"] == "dir" and row["path"] in self.expanded:
            self.expanded.discard(row["path"])
            return True
        return False

    def file_action(self, row, kind):
        """view or attach: only for plain files; a symlink or other entry is never sent."""
        k = row["e"]["kind"]
        if k == "file":
            return [{"type": kind, "path": row["path"]}]
        self.note = ("symlinks are not followed" if k == "symlink"
                     else f"only plain files can be {'viewed' if kind == 'view' else 'attached'}")
        return []

    def key(self, k):
        self.note = ""
        cur = self.current()
        if k in (curses.KEY_UP, "k"):
            self.move(lambda i: i - 1)
        elif k in (curses.KEY_DOWN, "j"):
            self.move(lambda i: i + 1)
        elif k == curses.KEY_PPAGE:
            self.move(lambda i: i - self.page)
        elif k == curses.KEY_NPAGE:
            self.move(lambda i: i + self.page)
        elif k == curses.KEY_HOME:
            self.move(lambda i: 0)
        elif k == curses.KEY_END:
            self.move(lambda i: 1 << 30)
        elif k == ".":
            self.show_hidden = not self.show_hidden
        elif k == "r":
            return [self.ask(p) for p in ["."] + sorted(self.expanded) if p == "." or p in self.listings]
        elif cur is None:
            return []
        elif k in (curses.KEY_RIGHT, "l"):
            return self.expand(cur)
        elif k in ("\n", "\r", curses.KEY_ENTER):
            if cur["e"]["kind"] != "dir":
                return self.file_action(cur, "view")
            if cur["path"] in self.expanded:
                self.collapse(cur)
                return []
            return self.expand(cur)
        elif k in (curses.KEY_LEFT, "h"):
            if not self.collapse(cur) and "/" in cur["path"]:
                self.sel = parent_of(cur["path"])
        elif k == "a":
            return self.file_action(cur, "attach")
        elif k == "i":
            return [{"type": "insert", "path": cur["path"]}]
        return []

    def click(self, index, w=80):
        """A click on row index: select it; clicking the selected row again opens a directory or views a file."""
        rows = self.rows(w)
        self.note = ""
        if not 0 <= index < len(rows) or rows[index][1] is None:
            return []
        ref = rows[index][1]
        if ref["path"] != self.sel:
            self.sel = ref["path"]
            return []
        return self.key("\n")


class Pane:
    def __init__(self, scr, link):
        self.scr, self.link = scr, link
        self.tree, self.msg, self.top, self.seen = Tree(), "", 0, None
        curses.curs_set(0)
        curses.use_default_colors()
        self.roles = {"": 0}
        if curses.has_colors():
            quiet = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
            for i, (role, fg) in enumerate([(Q, quiet), (A, curses.COLOR_BLUE), (W, curses.COLOR_YELLOW),
                                            (D, curses.COLOR_GREEN), (R, curses.COLOR_RED)], 1):
                curses.init_pair(i, fg, -1)
                self.roles[role] = curses.color_pair(i)
        self.roles[A] |= curses.A_BOLD
        self.wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
        self.wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)
        curses.mousemask(getattr(curses, "BUTTON1_PRESSED", 0) | getattr(curses, "BUTTON1_CLICKED", 0)
                         | self.wheel_up | self.wheel_down)
        curses.mouseinterval(0)
        scr.keypad(True)
        scr.timeout(100)
        self.send(self.tree.start())

    def send(self, msgs):
        for m in msgs:
            if not self.link.send(m):
                self.msg = "the conversation isn't listening"
                return
        self.msg = ""

    def refresh_state(self):
        while True:
            try:
                msg = self.link.inbox.get_nowait()
            except queue.Empty:
                return
            self.tree.receive(msg)

    def draw(self):
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        self.tree.page = max(h - 3, 1)
        rows = [] if self.tree.unavailable else self.tree.rows(w - 1)
        self.tree.current()
        sel = next((i for i, (_, ref) in enumerate(rows) if ref and ref["path"] == self.tree.sel), 0)
        room = max(h - 2, 1)
        if sel != self.seen:  # keep the selection in view, but let the wheel scroll away from it
            self.top, self.seen = min(max(self.top, sel - room + 1), sel), sel
        self.top = min(self.top, max(len(rows) - room, 0))
        self.put(0, [("FILES", A)], w)
        if self.tree.unavailable:
            self.put(1, [(fit(label(self.tree.unavailable), w - 1), Q)], w)
        for y, (segs, ref) in enumerate(rows[self.top:self.top + room], 1):
            self.put(y, segs, w, curses.A_REVERSE if ref and ref["path"] == self.tree.sel else 0)
        hint = self.msg or self.tree.note or ("the conversation has ended" if self.tree.ended else HINT)
        self.put(h - 1, [(fit(hint, w - 1), Q)], w)
        self.scr.refresh()

    def put(self, y, segs, w, attr=0):
        x = 0
        if attr:
            try:
                self.scr.addstr(y, 0, " " * max(w - 1, 0), attr)
            except curses.error:
                pass
        for text, role in segs:
            try:
                self.scr.addstr(y, x, fit(text, max(w - 1 - x, 1)), self.roles.get(role, 0) | attr)
            except curses.error:
                pass
            x += width(text)

    def key(self, k):
        if k == curses.KEY_MOUSE:
            try:
                _, _, y, _, bstate = curses.getmouse()
            except curses.error:
                return
            if self.wheel_up and bstate & self.wheel_up:
                self.top = max(self.top - 3, 0)
            elif self.wheel_down and bstate & self.wheel_down:
                self.top += 3
            else:
                self.send(self.tree.click(y - 1 + self.top, self.scr.getmaxyx()[1] - 1))
            return
        self.send(self.tree.key(k))

    def run(self):
        while True:
            self.refresh_state()
            self.draw()
            try:
                k = self.scr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                continue
            if k != curses.KEY_RESIZE:
                self.key(k)


def main():
    if not STATE:
        print("files.py is started by genai-tui", file=sys.stderr)
        return 2
    try:
        link = Client(STATE, "files")
    except OSError as e:
        print(f"files.py: can't reach the conversation pane: {e}", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: Pane(scr, link).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
