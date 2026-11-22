#!/usr/bin/env python3
"""genai-tui conversation pane: a coding-agent front end for `genai --json`.

One scrolling conversation with the model's reply streaming in, an input box at the bottom,
slash commands, and approval prompts that take the input box's place while genai waits.
genai decides what needs approval and enforces every rule; this program shows proposals and
returns the user's answers, nothing more.

Usage: conversation.py [genai options such as -p PROFILE -e ENDPOINT -S SESSION]
Standard library only; Python 3.10 or newer.
"""

import curses
import json
import locale
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

GENAI = os.environ.get("GENAI_BIN") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                    "genai")
CARD_BODY_LINES = 12
INPUT_MAX_LINES = 8

COMMANDS = {
    "/help": "show this help",
    "/coding": "on|off: let the model read and list files here without asking",
    "/skills": "list skills; * marks the ones in use",
    "/skill": "NAME on|off: use a blessed skill from the next message on, or stop",
    "/compact": "shrink the conversation when the context is getting full",
    "/context": "show how full the context is",
    "/clear": "clear the screen (the conversation itself is kept)",
    "/quit": "leave (also Ctrl-D on an empty input)",
}

# --------------------------------------------------------------------------
# Text: what is safe to draw, and how wide it is

_INVISIBLE = ({0x00AD, 0xFEFF} | set(range(0x200B, 0x2010)) | set(range(0x202A, 0x202F))
              | set(range(0x2060, 0x2065)) | set(range(0x2066, 0x206A)))


def clean(text):
    """Text from the model or a file, as it is drawn: tabs become spaces, and control, bidi and
    invisible characters become visible escapes, so nothing can move the cursor or hide."""
    out = []
    for ch in str(text):
        o = ord(ch)
        if ch == "\n":
            out.append(ch)
        elif ch == "\t":
            out.append("    ")
        elif o < 0x20 or 0x7F <= o < 0xA0:
            out.append(f"\\x{o:02x}")
        elif o in _INVISIBLE or unicodedata.category(ch) in ("Cs", "Co", "Cn"):
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    return "".join(out)


def cells(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def width(s):
    return sum(cells(c) for c in s)


def cut(s, w):
    """The longest start of s that fits in w cells (at least one character, so wrapping always moves)."""
    out, n = [], 0
    for c in s:
        if n + cells(c) > w and out:
            break
        out.append(c)
        n += cells(c)
    return "".join(out)


def fit(s, w):
    """s cut to w cells, with … when something was dropped."""
    if width(s) <= w:
        return s
    return cut(s, max(w - 1, 1)) + "…"


def wrap(text, w):
    """Word-wrap to w cells; words longer than a line are broken."""
    w = max(w, 1)
    lines = []
    for para in text.split("\n"):
        line, lw = "", 0
        for word in re.split(r"(\s+)", para):
            if not word:
                continue
            ww = width(word)
            if lw + ww <= w:
                line, lw = line + word, lw + ww
                continue
            if word.isspace():
                lines.append(line)
                line, lw = "", 0
                continue
            if line:
                lines.append(line.rstrip())
            while width(word) > w:
                part = cut(word, w)
                lines.append(part)
                word = word[len(part):]
            line, lw = word, width(word)
        lines.append(line)
    return lines


def inline(text, base=""):
    """Styled segments for one line of Markdown: **bold** and `code`; everything else as base."""
    segs = []
    for part in re.split(r"(\*\*[^*\n]+\*\*|`[^`\n]+`)", text):
        if len(part) > 4 and part.startswith("**") and part.endswith("**"):
            segs.append((part[2:-2], "bold"))
        elif len(part) > 2 and part.startswith("`") and part.endswith("`"):
            segs.append((part[1:-1], "code"))
        elif part:
            segs.append((part, base))
    return segs


def wrap_segs(segs, w):
    """Word-wrap styled segments to w cells, keeping each word's style."""
    w = max(w, 1)
    lines, lw = [[]], 0
    for text, role in segs:
        for word in re.split(r"(\s+)", text):
            if not word:
                continue
            ww = width(word)
            if lw + ww <= w:
                lines[-1].append((word, role))
                lw += ww
                continue
            if word.isspace():
                lines.append([])
                lw = 0
                continue
            if lw:
                lines.append([])
            while width(word) > w:
                part = cut(word, w)
                lines[-1].append((part, role))
                lines.append([])
                word = word[len(part):]
            lines[-1].append((word, role))
            lw = width(word)
    return lines


def markdown(text, w):
    """The model's Markdown as styled lines: headings, bullets, **bold**, `code` and fenced code blocks."""
    out, fenced = [], False
    for raw in text.split("\n"):
        if raw.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            out += [[("│ ", "quiet"), (part, "code")] for part in (wrap(raw, w - 2) if raw else [""])]
            continue
        head = re.match(r"#{1,6}\s+(.*)", raw)
        bullet = re.match(r"(\s*)[-*+]\s+(.*)", raw)
        if head:
            out += wrap_segs([(head.group(1), "bold")], w)
        elif bullet:
            pad = bullet.group(1) + "• "
            for i, ln in enumerate(wrap_segs(inline(bullet.group(2)), w - width(pad))):
                out.append([(pad if i == 0 else " " * width(pad), "quiet" if i == 0 else "")] + ln)
        else:
            out += wrap_segs(inline(raw), w)
    return out


# --------------------------------------------------------------------------
# The conversation, as blocks that each draw themselves

Q, A, W, D, R = "quiet", "accent", "waiting", "done", "risk"


class Block:
    def __init__(self):
        self.version = 0

    def changed(self):
        self.version += 1

    def lines(self, w):
        """A list of lines; each line is a list of (text, role) segments."""
        raise NotImplementedError


def para(text, role, w, first="", rest="", first_role=None):
    """Wrap text under a prefix: first on the first line, rest (same width) after."""
    out = []
    for i, ln in enumerate(wrap(text, w - width(first))):
        out.append([(first, first_role or role), (ln, role)] if i == 0 else [(rest, role), (ln, role)])
    return out


class Gap(Block):
    def lines(self, w):
        return [[]]


class Note(Block):
    def __init__(self, text, role=Q, prefix="  "):
        super().__init__()
        self.text, self.role, self.prefix = text, role, prefix

    def lines(self, w):
        return para(self.text, self.role, w, self.prefix, " " * width(self.prefix))


class UserMsg(Block):
    def __init__(self, text, queued=False):
        super().__init__()
        self.text, self.queued = text, queued

    def lines(self, w):
        out = [[]] + para(clean(self.text), "", w, "› ", "  ", first_role=A)
        if self.queued:
            out.append([("  queued: sends when the current reply ends", Q)])
        return out


class Reply(Block):
    def __init__(self):
        super().__init__()
        self.text = ""

    def add(self, chunk):
        self.text += chunk
        self.changed()

    def lines(self, w):
        body = markdown(clean(self.text).strip("\n"), w - 2)
        return [[]] + [[("● " if i == 0 else "  ", D)] + ln for i, ln in enumerate(body)]


class Banner(Block):
    def __init__(self, hello):
        super().__init__()
        self.h = hello

    def lines(self, w):
        h, ep = self.h, self.h.get("endpoint") or {}
        rows = [(f"genai {h.get('version', '?')}", A),
                (f"{ep.get('name', '?')}: {ep.get('model', '?')} at {ep.get('base_url', '?')}", "")]
        prof = h.get("profile")
        if prof:
            rows.append((f"profile {prof.get('name')}" + (f": {prof['description']}" if prof.get("description") else ""), ""))
        if h.get("session"):
            rows.append((f"session {h['session']}" + (" (resumed)" if h.get("resumed") else ""), ""))
        if h.get("coding"):
            rows.append((f"coding mode is on ({h.get('coding_from') or 'session'}): the model reads files here "
                         "without asking, except hidden and secret-looking ones", W))
            if h.get("coding_override"):
                rows.append((f"this is {h['coding_override']}; --coding-anywhere allowed it", R))
        sb = h.get("sandbox")
        rows.append(("commands are only shown: you run them yourself, "
                     + (f"inside the {sb['name']} sandbox" if sb else "OUTSIDE any sandbox, directly on this host"),
                     "" if sb else W))
        if h.get("skills"):
            rows.append((f"skills: {', '.join(h['skills'])}", ""))
        rows.append(("Type a message and press Enter. /help lists commands.", Q))
        out = []
        for text, role in rows:
            out += para(clean(text), role, w, "  ", "  ")
        return out


class Card(Block):
    """A proposal. Its state changes as it is decided and performed."""
    STATE_ROLE = {"waiting": W, "accepted": D, "done": D, "declined": Q, "error": R, "shown": A, "auto": Q}

    def __init__(self, ev):
        super().__init__()
        self.ev, self.state, self.detail = ev, "waiting", ""

    @property
    def id(self):
        return self.ev.get("id", "?")

    def title(self):
        ev, k = self.ev, self.ev.get("kind")
        path = clean(ev.get("path", ""))
        return {"read": f"read {path}", "list": f"list {path or '.'}", "edit": f"edit {path}",
                "write": f"{'overwrite' if ev.get('exists') else 'create'} {path}",
                "command": "command", "external": f"tool {clean(ev.get('tool', ''))}"}.get(k, str(k))

    def body(self):
        """(text, role) lines inside the card."""
        ev, k, out = self.ev, self.ev.get("kind"), []
        if ev.get("reason"):
            out += [(ln, Q) for ln in clean(ev["reason"]).split("\n")]
        if ev.get("secret"):
            out.append(("this file looks like it contains secrets", R))
        if ev.get("flagged"):
            out.append(("hidden or control characters are shown as \\x.. or \\u.... escapes", R))
        if k == "command":
            for i, ln in enumerate(clean(ev.get("command", "")).split("\n")):
                out.append((("$ " if i == 0 else "  ") + ln, ""))
        elif k in ("edit", "write") and ev.get("diff"):
            for ln in clean(ev["diff"]).rstrip("\n").split("\n"):
                if ln.startswith(("+++", "---")):
                    continue
                out.append((ln, D if ln.startswith("+") else R if ln.startswith("-") else Q if ln.startswith("@") else ""))
        elif k == "write":
            out += [("+" + ln, D) for ln in clean(ev.get("content", "")).split("\n")]
        elif k == "read" and ev.get("size") is not None:
            out.append((f"{ev['size']} bytes", Q))
        elif k == "external":
            out.append((clean(json.dumps(ev.get("args", {}), ensure_ascii=False)), ""))
        return out

    def full_text(self):
        return "\n".join(t for t, _ in self.body())

    def lines(self, w):
        if self.state == "auto":
            return [[("  ↳ ", Q), (fit(f"{self.title()}  (coding mode: no approval needed)", w - 4), Q)]]
        role = self.STATE_ROLE.get(self.state, Q)
        inner = max(w - 4, 8)
        head = f"─ {self.id} · {self.title()} "
        tail = f" {self.state} ─"
        head = fit(head, w - 2 - width(tail) - 1)
        fill = max(w - 2 - width(head) - width(tail), 1)
        out = [[], [("╭" + head + "─" * fill + tail + "╮", role)]]
        body = self.body()
        shown = body if len(body) <= CARD_BODY_LINES + 1 else body[:CARD_BODY_LINES]
        rows = [(fit(t, inner), r) for t, r in shown]
        if len(shown) < len(body):
            rows.append((f"… {len(body) - len(shown)} more lines (v views it all)", Q))
        if self.ev.get("kind") == "command" and self.state == "shown":
            rows.append(("run it in your shell if you want to; genai never runs it", A))
        if self.detail:
            rows.append((fit(clean(self.detail), inner), R if self.state == "error" else Q))
        for t, r in rows:
            out.append([("│ ", role), (t, r), (" " * (inner - width(t)) + " │", role)])
        out.append([("╰" + "─" * (w - 2) + "╯", role)])
        return out


# --------------------------------------------------------------------------
# State: genai's events in, the user's messages out. No curses here, so it can be tested.

class Model:
    def __init__(self):
        self.blocks = []
        self.cards = {}
        self.pending = []          # cards waiting for a decision, oldest first
        self.reply = None          # the reply being streamed
        self.busy = False          # a turn is running
        self.queued = []           # messages typed while busy
        self.outbox = []           # messages for genai
        self.hello = None
        self.coding = False
        self.context = None        # (used, limit)
        self.exited = None         # genai's exit code once it has gone
        self.quit = False

    def add(self, block):
        self.blocks.append(block)
        return block

    def note(self, text, role=Q):
        self.add(Note(text, role))

    def send(self, msg):
        self.outbox.append(msg)

    # -- from genai

    def handle(self, ev):
        t = ev.get("type")
        if t != "text":
            self.reply = None
        if t == "hello":
            self.hello, self.coding = ev, bool(ev.get("coding"))
            self.add(Banner(ev))
        elif t == "text":
            if self.reply is None:
                self.reply = self.add(Reply())
            self.reply.add(ev.get("text", ""))
        elif t == "proposal":
            card = self.add(Card(ev))
            self.cards[card.id] = card
            if ev.get("auto"):
                card.state = "auto"
            elif ev.get("kind") == "command":
                card.state = "shown"
            elif ev.get("kind") == "invalid":
                card.state = "error"
                card.detail = f"the model asked for a tool that does not exist: {ev.get('tool', '?')}"
            else:
                self.pending.append(card)
        elif t == "result":
            card = self.cards.get(ev.get("id"))
            if card:
                card.state = "done" if ev.get("status") == "ok" else "error"
                card.detail = ev.get("detail") or ""
                card.changed()
        elif t == "done":
            self.busy = False
            self.context = ((ev.get("context") or {}).get("used"), (ev.get("context") or {}).get("limit"))
            if ev.get("reason") == "step_limit":
                self.note("genai stopped after its step limit; send a message to continue", W)
            self.next_queued()
        elif t == "context":
            self.context = (ev.get("used"), ev.get("limit"))
        elif t == "error":
            self.note(f"error: {ev.get('message', '')}", R)
            if self.busy and not self.pending and ev.get("code") not in ("busy", "unknown_id", "bad_message",
                                                                         "unknown_type"):
                self.busy = False          # a failed turn ends without a done event
                self.next_queued()
        elif t == "notice":
            self.note(clean(ev.get("message", "")), W)
        elif t == "coding":
            self.coding = bool(ev.get("on"))
            self.note(f"coding mode is {'on' if self.coding else 'off'}", W if self.coding else Q)
        elif t == "skills":
            rows = ev.get("skills") or []
            if not rows:
                self.note("no skills found (genai --list-skills shows where they are looked for)")
            for r in rows:
                self.note(f"{'*' if r.get('active') else ' '} {clean(r.get('name', '')):20} {r.get('status', ''):10} "
                          f"{clean(r.get('description', ''))}", "" if r.get("active") else Q)
            if any(r.get("status") != "blessed" for r in rows):
                self.note("genai --list-skills shows the command that blesses each one")
        elif t == "compacted":
            self.note(f"compacted: {ev.get('before')} → {ev.get('after')} tokens", D)
        elif t == "attached":
            self.note(f"attached {clean(ev.get('label', ''))} ({ev.get('chars')} characters)")

    def exit(self, code, err=""):
        self.exited, self.busy, self.pending = code, False, []
        for ln in err.strip().splitlines()[-5:]:
            self.note(clean(ln), R)
        self.note(f"genai exited (code {code}). /quit or Ctrl-D to leave.", R)

    # -- from the user

    def submit(self, text):
        if not text.strip():
            return
        if text.startswith("/") and not text.startswith("//"):
            self.command(text.strip())
            return
        if text.startswith("//"):
            text = text[1:]
        if self.exited is not None:
            self.note("genai is not running", R)
        elif self.busy:
            self.queued.append(self.add(UserMsg(text, queued=True)))
        else:
            self.add(UserMsg(text))
            self.busy = True
            self.send({"type": "user", "text": text})

    def next_queued(self):
        if self.queued and not self.busy:
            msg = self.queued.pop(0)
            msg.queued = False
            msg.changed()
            self.blocks.remove(msg)
            self.add(msg)
            self.busy = True
            self.send({"type": "user", "text": msg.text})

    def command(self, line):
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        to_genai = cmd in ("/coding", "/skills", "/skill", "/compact")
        if to_genai and (self.busy or self.exited is not None):
            self.note(f"{cmd} has to wait until genai finishes this reply", W)
        elif cmd == "/help":
            for c, d in COMMANDS.items():
                self.note(f"{c:9} {d}")
            self.note("Enter sends · Alt+Enter or \\ then Enter adds a line · ↑↓ earlier messages · "
                      "PgUp/PgDn scroll · // starts a message with /")
        elif cmd == "/coding":
            if arg in ("on", "off"):
                self.send({"type": "coding", "on": arg == "on"})
            else:
                self.note(f"coding mode is {'on' if self.coding else 'off'} (/coding on|off)")
        elif cmd == "/skills":
            self.send({"type": "skills"})
        elif cmd == "/skill":
            name, _, state = arg.rpartition(" ")
            if not name or state not in ("on", "off"):
                self.note("usage: /skill NAME on|off", R)
            else:
                self.send({"type": "skill", "name": name, "on": state == "on"})
        elif cmd == "/compact":
            self.send({"type": "compact"})
        elif cmd == "/context":
            used, limit = self.context or (None, None)
            self.note(f"context: {used}/{limit} tokens" if used is not None else "context: not measured yet")
        elif cmd == "/clear":
            self.blocks = []
        elif cmd in ("/quit", "/exit"):
            self.quit = True
        else:
            self.note(f"unknown command {cmd} (/help lists them)", R)

    def decide(self, accept):
        if not self.pending:
            return
        card = self.pending.pop(0)
        card.state = "accepted" if accept else "declined"
        card.changed()
        self.send({"type": "decision", "id": card.id, "decision": "accept" if accept else "decline"})


def complete(text):
    """Tab on a partial /command: the completion, or text unchanged when there is none or several."""
    if not text.startswith("/") or " " in text:
        return text
    hits = [c for c in COMMANDS if c.startswith(text)]
    if len(hits) == 1:
        return hits[0] + " "
    return os.path.commonprefix(hits) if hits else text


# --------------------------------------------------------------------------
# The input box: a small multi-line editor with history

class Editor:
    def __init__(self):
        self.text, self.pos = "", 0
        self.history, self.hist_i, self.draft = [], None, ""

    def insert(self, s):
        self.text = self.text[:self.pos] + s + self.text[self.pos:]
        self.pos += len(s)

    def backspace(self):
        if self.pos:
            self.text = self.text[:self.pos - 1] + self.text[self.pos:]
            self.pos -= 1

    def delete(self):
        self.text = self.text[:self.pos] + self.text[self.pos + 1:]

    def line_bounds(self):
        start = self.text.rfind("\n", 0, self.pos) + 1
        end = self.text.find("\n", self.pos)
        return start, len(self.text) if end < 0 else end

    def home(self):
        self.pos = self.line_bounds()[0]

    def end(self):
        self.pos = self.line_bounds()[1]

    def left(self):
        self.pos = max(self.pos - 1, 0)

    def right(self):
        self.pos = min(self.pos + 1, len(self.text))

    def kill_back(self):          # Ctrl-U: to the start of the line
        start = self.line_bounds()[0]
        self.text, self.pos = self.text[:start] + self.text[self.pos:], start

    def kill_forward(self):       # Ctrl-K: to the end of the line
        end = self.line_bounds()[1]
        self.text = self.text[:self.pos] + self.text[end:]

    def kill_word(self):          # Ctrl-W: the word before the cursor
        m = re.search(r"\S*\s*$", self.text[:self.pos])
        start = m.start() if m else self.pos
        self.text, self.pos = self.text[:start] + self.text[self.pos:], start

    def up(self):
        """Up a line, or to the previous message when already on the first line."""
        start, _ = self.line_bounds()
        if start:
            prev = self.text.rfind("\n", 0, start - 1) + 1
            self.pos = min(prev + (self.pos - start), start - 1)
        elif self.history and self.hist_i != 0:
            if self.hist_i is None:
                self.draft, self.hist_i = self.text, len(self.history)
            self.hist_i -= 1
            self.text = self.history[self.hist_i]
            self.pos = len(self.text)

    def down(self):
        start, end = self.line_bounds()
        if end < len(self.text):
            nxt_end = self.text.find("\n", end + 1)
            nxt_end = len(self.text) if nxt_end < 0 else nxt_end
            self.pos = min(end + 1 + (self.pos - start), nxt_end)
        elif self.hist_i is not None:
            self.hist_i += 1
            if self.hist_i >= len(self.history):
                self.text, self.hist_i = self.draft, None
            else:
                self.text = self.history[self.hist_i]
            self.pos = len(self.text)

    def take(self):
        text = self.text
        if text.strip() and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.text, self.pos, self.hist_i, self.draft = "", 0, None, ""
        return text

    def view(self, w):
        """Wrapped input lines and the cursor's (row, column), with the › prompt."""
        rows, cur, offset = [], (0, 2), 0
        for i, line in enumerate(self.text.split("\n")):
            prefix = "› " if i == 0 else "  "
            shown = clean(line)
            chunks = [""] if not shown else []
            rest = shown
            while rest:
                part = cut(rest, max(w - 3, 1))
                chunks.append(part)
                rest = rest[len(part):]
            if offset <= self.pos <= offset + len(line):
                col = width(clean(line[:self.pos - offset]))
                row_in = min(col // max(w - 3, 1), len(chunks) - 1)
                col -= sum(width(c) for c in chunks[:row_in])
                cur = (len(rows) + row_in, 2 + col)
            for j, c in enumerate(chunks):
                rows.append((prefix if j == 0 else "  ") + c)
            offset += len(line) + 1
        return rows, cur


# --------------------------------------------------------------------------
# genai as a child process

class Child:
    def __init__(self, args):
        cmd = [GENAI] if os.environ.get("GENAI_BIN") else [sys.executable, GENAI]
        self.p = subprocess.Popen(cmd + ["--json"] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.events = queue.Queue()
        self.err = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                ev = {"type": "error", "message": f"not JSON from genai: {line.strip()[:200]}"}
            self.events.put(ev)
        self.events.put({"type": "_exit", "code": self.p.wait(), "err": "\n".join(self.err)})

    def _read_err(self):
        for line in self.p.stderr:
            self.err.append(line.rstrip("\n"))

    def send(self, msg):
        try:
            self.p.stdin.write(json.dumps(msg) + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            self.p.kill()


# --------------------------------------------------------------------------
# The screen

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class App:
    def __init__(self, scr, child):
        self.scr, self.child = scr, child
        self.m, self.ed = Model(), Editor()
        self.scroll = 0            # lines up from the bottom; 0 follows new output
        self.cache = {}
        self.last_total = 0
        self.ctrl_c = 0.0
        self.paste = None          # text of a bracketed paste in progress
        curses.curs_set(1)
        curses.use_default_colors()
        self.roles = {"": 0}
        if curses.has_colors():
            quiet = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
            for i, (role, fg) in enumerate([(Q, quiet), (A, curses.COLOR_BLUE), (W, curses.COLOR_YELLOW),
                                            (D, curses.COLOR_GREEN), (R, curses.COLOR_RED)], 1):
                curses.init_pair(i, fg, -1)
                self.roles[role] = curses.color_pair(i)
        self.roles["bold"] = curses.A_BOLD
        self.roles["code"] = self.roles[A]
        curses.raw()               # Ctrl-C, Ctrl-S and friends arrive as keys instead of signals
        scr.keypad(True)
        scr.timeout(50)

    # -- drawing

    def transcript(self, w):
        out = []
        for b in self.m.blocks:
            key = (id(b), b.version, w)
            if key not in self.cache:
                self.cache[key] = b.lines(w)
            out += self.cache[key]
        if len(self.cache) > 4 * len(self.m.blocks) + 64:
            self.cache = {}
        return out

    def put(self, y, x, segs, w):
        for text, role in segs:
            if x >= w:
                return
            try:
                self.scr.addstr(y, x, fit(text, w - x) if width(text) > w - x else text, self.roles.get(role, 0))
            except curses.error:
                pass
            x += width(text)

    def bottom(self, w):
        """The input box or the approval prompt, then the hint line: (lines, cursor or None, hints)."""
        if self.m.pending:
            c = self.m.pending[0]
            more = f"  ({len(self.m.pending)} waiting)" if len(self.m.pending) > 1 else ""
            lines = [[(f"Allow {c.id}: {c.title()}?", W), (more, Q)]]
            if c.ev.get("secret"):
                ep = (self.m.hello or {}).get("endpoint") or {}
                lines.append([(f"It looks like it holds secrets; allowing sends it to {ep.get('base_url', 'the endpoint')}", R)])
            lines.append([("  y", A), (" yes   ", ""), ("n", A), (" no   ", ""), ("v", A), (" view it all", "")])
            return lines, None, "y allow · n or Esc decline · v view in full"
        rows, cur = self.ed.view(w)
        if len(rows) > INPUT_MAX_LINES:
            top = min(max(cur[0] - INPUT_MAX_LINES + 1, 0), len(rows) - INPUT_MAX_LINES)
            rows, cur = rows[top:top + INPUT_MAX_LINES], (cur[0] - top, cur[1])
        lines = [[(r[:2], A), (r[2:], "")] for r in rows]
        if self.m.exited is not None:
            hints = "genai has exited · /quit or Ctrl-D to leave"
        elif self.m.busy:
            hints = f"{SPIN[int(time.time() * 10) % len(SPIN)]} working · type ahead, Enter queues it"
        else:
            hints = "Enter send · Alt+Enter new line · ↑ earlier · /help"
        return lines, cur, hints

    def draw(self):
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        lines, cur, hints = self.bottom(w)
        status = []
        if self.m.coding:
            status.append("coding on")
        if self.m.context and self.m.context[0] is not None and self.m.context[1]:
            used, limit = self.m.context
            status.append(f"ctx {used / 1000:.1f}k/{limit / 1000:.0f}k ({100 * used // limit}%)")
        area = max(h - len(lines) - 2, 1)
        body = self.transcript(w)
        if self.scroll and len(body) > self.last_total:
            self.scroll += len(body) - self.last_total       # stay put while scrolled up
        self.last_total = len(body)
        self.scroll = max(0, min(self.scroll, len(body) - area))
        end = len(body) - self.scroll
        for i, segs in enumerate(body[max(end - area, 0):end]):
            self.put(i, 0, segs, w)
        rule = "─" * w
        if self.scroll:
            rule = fit(f"── ↓ {self.scroll} more lines below (PgDn) ", w) + "─" * w
        self.put(area, 0, [(rule[:w], Q)], w)
        for i, segs in enumerate(lines):
            self.put(area + 1 + i, 0, segs, w)
        right = " · ".join(status)
        self.put(h - 1, 0, [(fit(hints, max(w - width(right) - 2, 1)), Q)], w)
        if right and width(right) < w - 4:
            self.put(h - 1, w - width(right) - 1, [(right, Q)], w)
        if cur is not None:
            curses.curs_set(1)
            try:
                self.scr.move(area + 1 + cur[0], min(cur[1], w - 1))
            except curses.error:
                pass
        else:
            curses.curs_set(0)
        self.scr.refresh()

    # -- input

    def key(self, k):
        m, ed = self.m, self.ed
        if self.paste is not None:
            self.paste += k if isinstance(k, str) else ""
            if self.paste.endswith("\x1b[201~"):
                ed.insert(self.paste[:-6].replace("\r\n", "\n").replace("\r", "\n"))
                self.paste = None
            return
        if k == "\x1b":
            seq = self.read_escape()
            if seq in ("\r", "\n"):
                ed.insert("\n")
            elif seq == "[200~":
                self.paste = ""
            elif m.pending and seq == "":
                m.decide(False)
            return
        if m.pending:
            if k in ("y", "Y"):
                m.decide(True)
            elif k in ("n", "N"):
                m.decide(False)
            elif k in ("v", "V"):
                self.view(m.pending[0])
            elif k in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                self.page(k)
            return
        if k in ("\n", "\r", curses.KEY_ENTER):
            if ed.text.endswith("\\") and ed.pos == len(ed.text):
                ed.backspace()
                ed.insert("\n")
            else:
                m.submit(ed.take())
                self.scroll = 0
        elif k == "\x04":
            if not ed.text:
                m.quit = True
            else:
                ed.delete()
        elif k == "\x03":
            now = time.time()
            if ed.text:
                ed.take()
            elif now - self.ctrl_c < 2:
                m.quit = True
            else:
                m.note("Ctrl-C again (or Ctrl-D) to quit" + (". genai's reply can't be interrupted yet." if m.busy else ""))
            self.ctrl_c = now
        elif k in ("\x7f", "\b", curses.KEY_BACKSPACE):
            ed.backspace()
        elif k == curses.KEY_DC:
            ed.delete()
        elif k in (curses.KEY_LEFT, "\x02"):
            ed.left()
        elif k in (curses.KEY_RIGHT, "\x06"):
            ed.right()
        elif k in (curses.KEY_HOME, "\x01"):
            ed.home()
        elif k in (curses.KEY_END, "\x05"):
            ed.end()
        elif k == curses.KEY_UP:
            ed.up()
        elif k == curses.KEY_DOWN:
            ed.down()
        elif k == "\x15":
            ed.kill_back()
        elif k == "\x0b":
            ed.kill_forward()
        elif k == "\x17":
            ed.kill_word()
        elif k == "\t":
            ed.text = complete(ed.text)
            ed.pos = len(ed.text)
        elif k == "\x0c":
            self.scr.clear()
        elif k in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.page(k)
        elif isinstance(k, str) and (k >= " " or k == "\n"):
            ed.insert(k)

    def read_escape(self):
        """The rest of an escape sequence, read without waiting: '' for a lone Esc."""
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

    def page(self, k):
        h, _ = self.scr.getmaxyx()
        step = max(h - 6, 1)
        self.scroll = self.scroll + step if k == curses.KEY_PPAGE else max(self.scroll - step, 0)

    def view(self, card):
        """Show a proposal in full in a pager: cleaned text, so nothing in it reaches the terminal raw."""
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(f"{card.id}: {card.title()}\n\n{card.full_text()}\n")
            path = f.name
        curses.endwin()
        try:
            subprocess.call(["less", "-S", path] if shutil.which("less") else ["more", path])
        finally:
            os.unlink(path)
            self.scr.refresh()

    # -- loop

    def run(self):
        sys.stdout.write("\x1b[?2004h")        # bracketed paste: a pasted newline doesn't send
        sys.stdout.flush()
        try:
            while not self.m.quit:
                self.pump()
                self.draw()
                try:
                    k = self.scr.get_wch()
                except curses.error:
                    continue
                if k == curses.KEY_RESIZE:
                    self.cache = {}
                    continue
                self.key(k)
                self.pump()
        finally:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()

    def pump(self):
        while True:
            try:
                ev = self.child.events.get_nowait()
            except queue.Empty:
                break
            if ev.get("type") == "_exit":
                self.m.exit(ev["code"], ev.get("err", ""))
            else:
                self.m.handle(ev)
        for msg in self.m.outbox:
            self.child.send(msg)
        self.m.outbox = []


def main(argv):
    if "--json" in argv:
        print("conversation.py adds --json itself; pass only genai's other options", file=sys.stderr)
        return 2
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    child = Child(argv)
    try:
        curses.wrapper(lambda scr: App(scr, child).run())
    finally:
        child.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
