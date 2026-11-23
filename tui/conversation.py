#!/usr/bin/env python3
"""genai-tui conversation pane: a coding-agent front end for `genai --json`.

One scrolling conversation with the model's reply streaming in, an input box at the bottom,
slash commands, and approval prompts that take the input box's place while genai waits.
genai decides what needs approval and enforces every rule; this program shows proposals and
returns the user's answers, nothing more.

Usage: conversation.py [genai options such as -p PROFILE -e ENDPOINT -S SESSION]
Standard library only; Python 3.10 or newer.
"""

import argparse
import curses
import json
import locale
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipc import Hub  # noqa: E402
from recorder import Recorder, ago, fifo_path, label, trim  # noqa: E402

GENAI = os.environ.get("GENAI_BIN") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                    "genai")
CARD_BODY_LINES = 12
INPUT_MAX_LINES = 8

COMMANDS = {
    "/help": "show this help",
    "/coding": "on|off: let the model read and list files here without asking",
    "/skills": "list skills; * marks the ones in use",
    "/skill": "NAME on|off: use a blessed skill from the next message on, or stop",
    "/run": "[ID]: review and run a proposed command in the shell pane (the latest by default)",
    "/share": "[N|mark|all]: review shell output, then send it: the last command, command N, from the mark, or all",
    "/commands": "list the commands run in the shell pane, numbered for /share N (also in the info panel)",
    "/mark": "shell context starts at the shell's current prompt (or right-click a line in the shell)",
    "/shell": "show the shell and move there (`` on its empty prompt hides it again)",
    "/panel": "show or hide the info panel on the right",
    "/plan": "[clear]: show the approved plan, or drop it so everything asks again",
    "/raw": "[N|all]: what was sent to the endpoint and what came back, for the last turn, turn N, or all kept",
    "/system": "the system prompt and tool definitions sent with every request",
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
        elif o in _INVISIBLE or unicodedata.category(ch) in ("Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    return "".join(out)


def command_text(command):
    return clean(command.replace("\\", "\\\\").replace("\t", "\\t"))


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
    turn = 0                       # the turn it belongs to (0: before the first message)

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
    def __init__(self, text, queued=False, attached=None):
        super().__init__()
        self.text, self.queued, self.attached = text, queued, attached

    def lines(self, w):
        out = [[]] + para(clean(self.text), "", w, "› ", "  ", first_role=A)
        if self.attached:
            out.append([("  ", Q), (fit(f"[attached: {clean(self.attached)}]", w - 2), Q)])
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


def step_text(st):
    """One plan step, escaped: what it does to which file, or the exact command."""
    if st.get("kind") == "command":
        return "run " + command_text(st.get("command", "")).replace("\n", "\\n")
    return f"{st.get('kind', '?')} {clean(st.get('path', ''))}"


def plan_line(st):
    """A step as a line in the plan editor."""
    if st.get("kind") == "command":
        cmd = st.get("command", "")
        if "\n" in cmd:
            return f"{st.get('n')} run (a {cmd.count(chr(10)) + 1}-line command: keep or delete this line; it can't be changed here)"
        return f"{st.get('n')} run {cmd}"
    return f"{st.get('n')} {st.get('kind')} {st.get('path', '')}"


def parse_plan_edit(text, steps):
    """What the user kept of a plan in their editor: (step numbers kept, {number: changed command}, problem)."""
    keep, edits = set(), {}
    for ln in text.split("\n"):
        if not ln.strip():
            continue
        m = re.fullmatch(r"([0-9]{1,3}) (read|edit|write|run) (.+)", ln)
        st = steps.get(int(m.group(1))) if m else None
        if st and ln == plan_line(st):
            keep.add(st["n"])
            continue
        if not st or (m.group(2) == "run") != (st.get("kind") == "command") or \
                (m.group(2) != "run" and m.group(2) != st.get("kind")):
            return set(), {}, f"this line isn't a step of the plan: {clean(ln)[:60]}"
        if st.get("kind") == "command":
            if "\n" in st.get("command", ""):
                return set(), {}, f"step {st['n']} is a multi-line command; keep its line as it was, or delete it"
            edits[st["n"]] = m.group(3)
        elif m.group(3) != st.get("path"):
            return set(), {}, f"step {st['n']}'s path was changed; leave the step out instead"
        keep.add(st["n"])
    return keep, edits, None


class Card(Block):
    """A proposal. Its state changes as it is decided and performed."""
    STATE_ROLE = {"waiting": W, "accepted": D, "done": D, "declined": Q, "error": R, "shown": A, "auto": Q,
                  "running": A, "ran": D, "cancelled": Q}

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
                "command": "command", "external": f"tool {clean(ev.get('tool', ''))}",
                "plan": f"plan · {len(ev.get('steps') or [])} steps"}.get(k, str(k))

    def body(self):
        """(text, role) lines inside the card."""
        ev, k, out = self.ev, self.ev.get("kind"), []
        if ev.get("preapproved"):
            out.append((f"step {ev.get('plan_step')} of the plan you approved", D))
        elif ev.get("plan_step"):
            out.append((f"step {ev.get('plan_step')} of the plan", Q))
        elif ev.get("off_plan"):
            out.append(("NOT in the plan you approved", W))
        if ev.get("reason"):
            out += [(ln, A if k == "plan" else Q) for ln in clean(ev["reason"]).split("\n")]
        if ev.get("secret"):
            out.append(("this file looks like it contains secrets", R))
        if ev.get("flagged"):
            out.append(("hidden or control characters are shown as \\x.. or \\u.... escapes", R))
        if k == "command":
            for i, ln in enumerate(command_text(ev.get("command", "")).split("\n")):
                out.append((("$ " if i == 0 else "  ") + ln, ""))
        elif k == "write" and ev.get("undiffable"):
            out.append(("The existing file will be replaced entirely: " + clean(ev["undiffable"]), W))
            mode = ev.get("mode")
            if mode:
                executable = " (executable)" if int(mode, 8) & 0o111 else ""
                out.append((f"The replacement keeps mode {mode}{executable}.", W))
            out.extend(("+" + ln, D) for ln in clean(ev.get("content", "")).split("\n"))
        elif k in ("edit", "write") and ev.get("diff"):
            diff = clean(ev["diff"]).rstrip("\n").split("\n")
            if len(diff) >= 2 and diff[0].startswith("--- a/") and diff[1].startswith("+++ b/"):
                diff = diff[2:]
            for ln in diff:
                out.append((ln, D if ln.startswith("+") else R if ln.startswith("-") else Q if ln.startswith("@") else ""))
        elif k == "write":
            out += [("+" + ln, D) for ln in clean(ev.get("content", "")).rstrip("\n").split("\n")]
        elif k == "read" and ev.get("size") is not None:
            out.append((f"{ev['size']} bytes", Q))
        elif k == "external":
            out.append((clean(json.dumps(ev.get("args", {}), ensure_ascii=False)), ""))
        elif k == "plan":
            for st in ev.get("steps") or []:
                out.append((f"{st.get('n', '?'):>2}. {step_text(st)}", ""))
                out.append((f"    {clean(st.get('why', ''))}", Q))
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
            rows.append((f"click it, or /run {self.id}, to send it to your shell; genai never runs it", A))
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
        self.shell = False         # a shell pane is there (inside genai-tui)
        self.skills = None         # genai's skill list, once asked for
        self.show_skills = False   # print the next skill list in the conversation (the user typed /skills)
        self.ask = None            # a question of ours in place of the input box: run or share
        self.actions = []          # things for the screen to do: ("share", "last"), ("focus_shell",)
        self.recent = []           # the shell pane's latest commands, for the info panel
        self.turn = 0              # turns sent to genai so far
        self.exchanges = {}        # turn: [first, last] exchange with the endpoint, for /raw
        self.plan = None           # the approved plan, as genai last reported it
        self.plan_cmds = {}        # (plan id, step number) -> approved command, consumed before execution
        self.plan_context = None   # execution pane and directory reviewed for automatic runs
        self.plan_auto = False     # run those commands as they come, without asking again

    def add(self, block):
        if "turn" not in vars(block):
            block.turn = self.turn
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
            if "skills" in (ev.get("features") or []):
                self.send({"type": "skills"})          # for the info panel; not printed
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
                if ev.get("preapproved") and self.command_authorized(card) and self.shell:
                    self.actions.append(("plan_cmd", card))
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
        elif t in ("trace", "system"):
            self.actions.append(("show", ev))
        elif t == "plan":
            was, self.plan = self.plan, ev.get("plan")
            if self.plan is None:
                self.plan_cmds, self.plan_auto, self.plan_context = {}, False, None
                if was:
                    self.note("plan dropped: everything asks again", W)
            elif not was or was.get("id") != self.plan.get("id"):
                n = sum(1 for st in self.plan.get("steps") or [] if st.get("approved"))
                self.note(f"plan {self.plan.get('id')} approved: {n} step{'s' if n != 1 else ''}"
                          + ("; planned commands run on their own" if self.plan_auto else
                             "; each planned command asks once") + " · /plan shows it", D)
        elif t == "done":
            if ev.get("exchanges"):
                self.exchanges[self.turn] = ev["exchanges"]
            self.busy = False
            self.context = ((ev.get("context") or {}).get("used"), (ev.get("context") or {}).get("limit"))
            if ev.get("reason") == "step_limit":
                self.note("genai stopped after its step limit; send a message to continue", W)
            self.next_queued()
        elif t == "context":
            self.context = (ev.get("used"), ev.get("limit"))
        elif t == "error":
            if ev.get("exchanges"):
                self.exchanges[self.turn] = ev["exchanges"]
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
            self.skills = rows
            if not self.show_skills:
                return
            self.show_skills = False
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
            masked = ev.get("redacted") or {}
            if masked:
                self.note("genai masked " + ", ".join(f"{n} {clean(k)}" for k, n in masked.items())
                          + " in what you shared", W)

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
        self.say(text)

    def say(self, text, attached=None):
        if self.exited is not None:
            self.note("genai is not running", R)
        elif self.busy:
            self.queued.append(self.add(UserMsg(text, queued=True, attached=attached)))
        else:
            self.turn += 1
            self.add(UserMsg(text, attached=attached))
            self.busy = True
            self.send({"type": "user", "text": text})

    def share(self, label, text, message):
        """Send reviewed output: as an attachment, so genai masks secrets in it, then a message about it."""
        self.send({"type": "attach", "label": label, "text": text})
        self.say(message, attached=f"{label}, {text.count(chr(10)) + 1} lines")

    def next_queued(self):
        if self.queued and not self.busy:
            msg = self.queued.pop(0)
            self.turn += 1
            msg.queued, msg.turn = False, self.turn
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
            self.show_skills = True
            self.send({"type": "skills"})
        elif cmd == "/skill":
            name, _, state = arg.rpartition(" ")
            if not name or state not in ("on", "off"):
                self.note("usage: /skill NAME on|off", R)
            else:
                self.note(f"skill {clean(name)}: {state} from your next message", Q)
                self.send({"type": "skill", "name": name, "on": state == "on"})
        elif cmd == "/compact":
            self.send({"type": "compact"})
        elif cmd in ("/run", "/share", "/shell", "/mark", "/commands") and not self.shell:
            self.note(f"{cmd} needs the shell pane: start genai with genai-tui", R)
        elif cmd == "/run":
            cards = [c for c in self.cards.values() if c.ev.get("kind") == "command"]
            card = self.cards.get(arg) if arg else (cards[-1] if cards else None)
            if not card or card.ev.get("kind") != "command":
                self.note("no command proposal to run" + (f" with id {arg}" if arg else ""), R)
            else:
                self.ask = {"kind": "run", "card": card, "command": card.ev.get("command", ""), "edited": False}
        elif cmd == "/share":
            if re.fullmatch(r"#?[0-9]{1,9}", arg):
                self.actions.append(("share_cmd", int(arg.lstrip("#"))))
            elif arg not in ("", "mark", "all"):
                self.note("usage: /share, /share N, /share mark or /share all", R)
            else:
                self.actions.append(("share", arg or "last"))
        elif cmd == "/commands":
            self.actions.append(("commands",))
        elif cmd == "/mark":
            self.actions.append(("mark",))
        elif cmd == "/shell":
            self.actions.append(("focus_shell",))
        elif cmd == "/panel":
            self.actions.append(("panel",))
        elif cmd in ("/raw", "/system") and self.busy and not self.pending:
            self.note(f"{cmd} has to wait until genai finishes this reply", W)
        elif cmd == "/system":
            self.send({"type": "system"})
        elif cmd == "/plan":
            if arg == "clear" and self.busy:
                self.note("/plan clear has to wait until genai finishes this reply", W)
            elif arg == "clear":
                self.plan_cmds, self.plan_auto, self.plan_context = {}, False, None
                self.send({"type": "plan", "clear": True})
            elif arg:
                self.note("usage: /plan or /plan clear", R)
            elif not self.plan:
                self.note("no plan approved")
            else:
                self.note(f"plan {self.plan.get('id')}: {clean(self.plan.get('goal', ''))}", A)
                for st in self.plan.get("steps") or []:
                    mark = {"done": "✓", "todo": "·", "left out": "✗"}.get(st.get("status"), "?")
                    self.note(f"  {mark} {st.get('n', '?'):>2}. {step_text(st)}"
                              + (" (you changed it)" if st.get("edited") else "")
                              + (" (left out)" if st.get("status") == "left out" else ""),
                              D if st.get("status") == "done" else Q if st.get("status") == "left out" else "")
        elif cmd == "/raw":
            self.raw(arg)
        elif cmd == "/context":
            used, limit = self.context or (None, None)
            self.note(f"context: {used}/{limit} tokens" if used is not None else "context: not measured yet")
        elif cmd == "/clear":
            self.blocks = []
        elif cmd in ("/quit", "/exit"):
            self.quit = True
        else:
            self.note(f"unknown command {cmd} (/help lists them)", R)

    def raw(self, arg):
        """Ask genai for a turn's exchanges with the endpoint: the latest turn that had any, turn N, or all."""
        if arg == "all":
            self.send({"type": "trace", "from": 1, "to": 1 << 30})
            return
        if arg and not arg.isdigit():
            self.note("usage: /raw, /raw N (a turn) or /raw all", R)
            return
        turn = int(arg) if arg else max(self.exchanges, default=None)
        if self.busy and self.pending and not arg:
            self.send({"type": "trace"})               # this turn hasn't ended: its latest exchange
        elif turn is None or turn not in self.exchanges:
            self.note("nothing has been sent to the endpoint yet" if turn is None else
                      f"turn {turn} has no exchanges with the endpoint", R)
        else:
            lo, hi = self.exchanges[turn]
            self.send({"type": "trace", "from": lo, "to": hi})

    def snapshot(self):
        """What the info panel shows. Proposal titles and details are escaped here and again where drawn."""
        h = self.hello or {}
        ep, sb, prof = h.get("endpoint") or {}, h.get("sandbox"), h.get("profile") or {}
        return {
            "session": {"endpoint": ep.get("name"), "model": ep.get("model"), "profile": prof.get("name"),
                        "session": h.get("session"), "sandbox": sb.get("name") if sb else None},
            "coding": self.coding, "busy": self.busy, "context": list(self.context or (None, None)),
            "skills": self.skills,
            "proposals": [{"id": c.id, "title": c.title(), "state": c.state, "detail": clean(c.detail)}
                          for c in self.cards.values() if c.state != "auto"],
            "commands": self.recent,
            "plan": self.plan and {"goal": clean(self.plan.get("goal", "")), "auto": self.plan_auto,
                                   "steps": [{"n": st.get("n"), "text": step_text(st), "status": st.get("status")}
                                             for st in self.plan.get("steps") or []]},
        }

    def request(self, msg):
        """A request from the info panel, checked and turned into the command a user would type."""
        t = msg.get("type") if isinstance(msg, dict) else None
        if t == "coding" and isinstance(msg.get("on"), bool):
            self.command(f"/coding {'on' if msg['on'] else 'off'}")
        elif t == "skill" and isinstance(msg.get("name"), str) and isinstance(msg.get("on"), bool) \
                and msg["name"].strip() and "\n" not in msg["name"]:
            self.command(f"/skill {msg['name']} {'on' if msg['on'] else 'off'}")
        elif t == "skills" and not self.busy and self.exited is None:
            self.send({"type": "skills"})
        elif t == "raw" and (type(msg.get("turn")) is int or msg.get("turn") in ("all", "last")):
            self.command(f"/raw {'' if msg['turn'] == 'last' else msg['turn']}".strip())
        elif t == "system":
            self.command("/system")
        elif t == "share" and msg.get("what") in ("mark", "all", "last") and self.shell:
            self.actions.append(("share", msg["what"]))
        elif t == "command" and type(msg.get("n")) is int and self.shell:
            self.actions.append(("share_cmd", msg["n"]))
        elif t == "mark" and isinstance(msg.get("on"), bool) and self.shell:
            self.actions.append(("mark_line",) if msg["on"] else ("unmark",))

    def attach(self, label, text):
        """Shell context the user reviewed: it goes with their next message, so they can ask about it."""
        self.send({"type": "attach", "label": label, "text": text})
        self.note(f"attached {label}; it goes with your next message", D)

    def command_authorized(self, card):
        ev = card.ev
        key = ev.get("plan_id"), ev.get("plan_step")
        return key in self.plan_cmds and self.plan_cmds[key] == ev.get("command")

    def decide_plan(self, approve=None, edits=None, auto=False, context=None):
        """Approve the plan showing: every step, or those in approve, with commands changed as in edits
        ({number: command}). Planned commands then ask once each, or with auto run as they come."""
        card = self.pending.pop(0)
        steps = {st.get("n"): st for st in card.ev.get("steps") or []}
        keep = set(steps) if approve is None else set(approve)
        edits = edits or {}
        self.plan_cmds = {(card.id, n): edits.get(n, st.get("command")) for n, st in steps.items()
                          if n in keep and st.get("kind") == "command"}
        self.plan_auto, self.plan_context = auto, context
        card.state = "accepted"
        card.detail = ("all steps" if approve is None else f"steps {', '.join(map(str, sorted(keep)))}") + \
            (f"; you changed step {', '.join(map(str, sorted(edits)))}" if edits else "") + \
            ("; commands run on their own" if auto else "")
        card.changed()
        msg = {"type": "decision", "id": card.id, "decision": "accept"}
        if approve is not None:
            msg["approve"] = sorted(keep)
        if edits:
            msg["edits"] = {str(n): c for n, c in edits.items()}
        self.send(msg)

    def decide(self, accept):
        if not self.pending:
            return
        card = self.pending.pop(0)
        card.state = "accepted" if accept else "declined"
        card.changed()
        self.send({"type": "decision", "id": card.id, "decision": "accept" if accept else "decline"})


def edit_key(ed, k):
    """The editing keys both input boxes share. True if k was one of them."""
    keys = {"\x7f": ed.backspace, "\b": ed.backspace, curses.KEY_BACKSPACE: ed.backspace, curses.KEY_DC: ed.delete,
            curses.KEY_LEFT: ed.left, "\x02": ed.left, curses.KEY_RIGHT: ed.right, "\x06": ed.right,
            curses.KEY_HOME: ed.home, "\x01": ed.home, curses.KEY_END: ed.end, "\x05": ed.end,
            curses.KEY_UP: ed.up, curses.KEY_DOWN: ed.down, "\x15": ed.kill_back, "\x0b": ed.kill_forward,
            "\x17": ed.kill_word}
    if k in keys:
        keys[k]()
    elif isinstance(k, str) and (k >= " " or k == "\n"):
        ed.insert(k)
    else:
        return False
    return True


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
# Your editor, for reviewing a command or output before it goes anywhere

EDITOR_HEADER = """# {title}
# Lines starting with '#' are removed. What is left is {what}.
# Save an empty file to {cancel}.
"""


def editor_argv(path):
    """The user's editor, with swap, backup and history files off for vim, as genai does: the text may hold secrets."""
    argv = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    base = os.path.basename(argv[0])
    if base in ("vim", "nvim", "gvim"):
        argv += ["-n", "-i", "NONE", "--cmd", "set nobackup nowritebackup noundofile"]
    elif base == "vi":
        argv += ["-n"]
    return argv + [path]


def strip_comments(text):
    return "\n".join(ln for ln in text.split("\n") if not ln.startswith("#")).strip("\n")


def review_buffer(command, rc, where, output):
    """What /run and /share open for review: the same layout as genai's own :run."""
    return ("# EVERYTHING below is sent to the model as saved, the command included.\n"
            "# Remove anything you don't want it to see, such as a secret in the command.\n"
            f"I ran this command {where} (exit {rc}):\n```\n{command}\n```\nOutput:\n```\n{output}\n```\n")


# --------------------------------------------------------------------------
# The raw view: what went to the endpoint and what came back

def pretty(text):
    """JSON text indented for reading, or the text as it is if it isn't JSON."""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return text


def safe_lines(text):
    """Text for the pager: control and invisible characters shown as escapes, as everywhere else."""
    return "\n".join(clean(ln) for ln in text.split("\n"))


def render_trace(ev, turns):
    """A trace event as text for a pager. turns maps a turn to its [first, last] exchange, to name them."""
    exs = ev.get("exchanges") or []
    named = {n: t for t, (a, b) in turns.items() for n in range(a, b + 1)}
    kept = ev.get("kept")
    out = ["genai: what was sent to the endpoint, and what came back",
           "Exactly as sent and received, except your API key and the endpoint's own header values. Request bodies",
           "are indented here for reading; they were sent as compact JSON. q leaves, / searches.", ""]
    if not exs:
        out.append("genai no longer has these exchanges" + (f" (it keeps the last 30: {kept[0]}–{kept[1]})" if kept
                                                            else ""))
    for ex in exs:
        n = ex.get("n")
        status = ex.get("status", "?") + (f": {ex['error']}" if ex.get("error") else "")
        took = f" · {ex['ms'] / 1000:.1f} s" if isinstance(ex.get("ms"), int) else ""
        turn = f" · turn {named[n]}" if n in named else ""
        out += ["═" * 78, f"Exchange {n}{turn} · {ex.get('time', '')}{took} · {status}", "═" * 78, "",
                f"{ex.get('method', 'POST')} {ex.get('url', '')}"]
        out += [f"{k}: {v}" for k, v in (ex.get("headers") or {}).items()]
        body = ex.get("request") or ""
        out += ["", f"── Request body ({len(body.encode())} bytes) " + "─" * 40, pretty(body), ""]
        resp = ex.get("response") or []
        how = "streamed, " + f"{len(resp)} lines" if ex.get("stream") else "one body"
        out += [f"── Response: HTTP {ex.get('http', '?')}, {how} " + "─" * 40]
        out += resp if ex.get("stream") else [pretty("".join(resp))]
        if ex.get("message") is not None:
            out += ["", "── The reply as genai put it together " + "─" * 40,
                    json.dumps(ex["message"], indent=2, ensure_ascii=False)]
        if ex.get("usage"):
            out += ["", f"usage: {json.dumps(ex['usage'])}"]
        out.append("")
    return safe_lines("\n".join(out))


def render_system(ev):
    out = ["genai: the system prompt and tools sent with every request",
           "Your conversation follows the system prompt in each request. q leaves, / searches.", "",
           f"model: {ev.get('model', '?')}", f"params: {json.dumps(ev.get('params') or {})}", "",
           "── System prompt " + "─" * 60, ev.get("system", ""), "",
           "── Tools " + "─" * 68, json.dumps(ev.get("tools") or [], indent=2, ensure_ascii=False)]
    return safe_lines("\n".join(out))


# --------------------------------------------------------------------------
# The shell pane, through tmux

class Busy(Exception):
    pass


def menu_item(*args):
    """The tmux command for a menu item: it runs request.py, which sends the choice over the socket. A menu
    command is a tmux format, so it is quoted for tmux, and carries only fixed words and checked ids."""
    cmd = shlex.join([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "request.py"), *args])
    return 'run-shell -b "' + cmd.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("#", "##") + '"'


class Shell:
    """The shell pane genai-tui opened beside the conversation. What each command printed comes from the
    recorder (tui/recorder.py); this reads the screen and its history only for shell context, by logical line:
    a line wrapped onto several rows counts once, so resizing the pane doesn't move a mark."""

    def __init__(self, pane):
        self.pane = pane
        self.primary_prompt = None

    @classmethod
    def from_env(cls):
        pane = os.environ.get("GENAI_TUI_SHELL")
        return cls(pane) if pane and os.environ.get("TMUX") else None

    def tmux(self, *args, data=None, check=False):
        r = subprocess.run(["tmux", *args], input=data, capture_output=True, text=True, timeout=5, check=check)
        return r.stdout

    def opt(self, name):
        return self.tmux("show", "-gv", name).strip()

    def context(self):
        """The live pane process and working directory used for dispatch."""
        raw = self.tmux("display", "-p", "-t", self.pane,
                        "#{pane_pid} #{pane_current_command} #{pane_in_mode} #{bracket_paste_flag} #{pane_current_path}",
                        check=True)
        fields = raw.removesuffix("\n").split(" ", 4)
        if len(fields) != 5 or not fields[0].isdigit():
            raise Busy("the shell pane's execution context is unavailable")
        pid, command, mode, paste, cwd = fields
        if not cwd:
            raise Busy("the shell pane's working directory is unavailable")
        return (pid, cwd), (command, mode, paste)

    def execution_where(self):
        """Describe the shell pane itself, independently of the genai child."""
        try:
            (pid, _), _ = self.context()
        except (Busy, OSError, subprocess.SubprocessError):
            return "with unverified isolation"
        root = f"/proc/{pid}/root"
        if os.path.exists(f"{root}/run/.containerenv"):
            return "inside the podman sandbox"
        if os.path.exists(f"{root}/.dockerenv"):
            return "inside the docker sandbox"
        return "OUTSIDE any sandbox, directly on this host"

    def ready(self):
        context, (command, mode, paste) = self.context()
        if mode == "1":
            raise Busy("it is in copy mode; press q there")
        if command != "bash":
            raise Busy(f"{command or 'something'} is running")
        if paste != "1" or self.primary_prompt is None or not self.primary_prompt():
            raise Busy("Bash is not at its primary prompt with bracketed input enabled")
        return context

    def type(self, command, enter=True, expected=None):
        """Type only into a Bash pane accepting bracketed input."""
        if any(unicodedata.category(ch) in ("Cc", "Cs") and ch not in "\n\t" for ch in command):
            raise Busy("the command contains terminal control characters")
        context = self.ready()
        if expected is not None and context != expected:
            raise Busy("the shell or working directory changed since review")
        self.tmux("load-buffer", "-b", "genai-run", "-", data=command, check=True)
        if self.ready() != context:
            raise Busy("the shell changed before paste")
        self.tmux("send-keys", "-t", self.pane, "C-e", "C-u", check=True)
        self.tmux("paste-buffer", "-p", "-d", "-b", "genai-run", "-t", self.pane, check=True)
        if enter:
            if self.ready() != context:
                raise Busy("the shell changed before Enter")
            self.tmux("send-keys", "-t", self.pane, "Enter", check=True)

    def lines(self, upto=None):
        """The pane's history and screen as logical lines, to the cursor's line, or to row upto counted from the
        oldest line of history."""
        if upto is None:
            end = self.tmux("display", "-p", "-t", self.pane, "#{cursor_y}").strip() or "0"
        else:
            hs = int(self.tmux("display", "-p", "-t", self.pane, "#{history_size}").strip() or 0)
            end = str(upto - hs)
        out = self.tmux("capture-pane", "-p", "-J", "-t", self.pane, "-S", "-", "-E", end)
        return [ln.rstrip() for ln in out.removesuffix("\n").split("\n")]

    def show(self, focus=False):
        """Bring the shell pane into view above the conversation (it runs hidden), and go there if asked."""
        toggle = os.environ.get("GENAI_TUI_TOGGLE")
        if toggle:
            subprocess.run([toggle, "show"] + (["--focus"] if focus else []), capture_output=True, timeout=5)
        elif focus:
            self.tmux("select-pane", "-t", self.pane)

    def focus(self):
        self.show(focus=True)

    def copy(self, text):
        """To tmux's buffer and, through the terminal (OSC 52), the system clipboard where the terminal allows it."""
        self.tmux("set-buffer", "-w", "-b", "genai-copy", "--", text)

    def menu(self, pid, where):
        """tmux's own menu at the mouse, for a command card. Menu titles, names and commands are tmux formats, so
        none of them carries text from the model: only the proposal id, checked here, and fixed words."""
        if not re.fullmatch(r"p[0-9]{1,9}", pid):
            return
        me = os.environ.get("TMUX_PANE", "")

        def pick(what):
            return menu_item("menu", what, pid)
        # display-menu returns only when the menu closes, so don't wait for it.
        self.menus = [p for p in getattr(self, "menus", []) if p.poll() is None]
        self.menus.append(subprocess.Popen(["tmux", "display-menu", "-t", me, "-x", "M", "-y", "M", "-T", f" {pid}: runs {where} ",
                  "Run it in the shell pane", "r", pick("run"),
                  "Edit it, then run", "e", pick("edit"),
                  "Put it at the shell prompt (you press Enter)", "p", pick("prompt"),
                  "Copy it", "c", pick("copy"),
                  "", "Cancel", "q", ""],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    def raw_menu(self, turn):
        """Right-click in the conversation: tmux's menu for looking behind it. Only fixed words and a number."""
        me = os.environ.get("TMUX_PANE", "")
        pick = menu_item
        items = (["What was sent and received for this turn", "r", pick("raw", str(turn))] if turn else []) + [
            "What was sent and received last", "l", pick("raw", "last"),
            "Everything genai still has (last 30)", "a", pick("raw", "all"),
            "The system prompt and tools", "s", pick("system"),
            "", "Cancel", "q", ""]
        self.menus = [p for p in getattr(self, "menus", []) if p.poll() is None]
        self.menus.append(subprocess.Popen(["tmux", "display-menu", "-t", me, "-x", "M", "-y", "M", "-T",
                                            f" raw view{f' · turn {turn}' if turn else ''} ", *items],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    def title(self, text):
        self.tmux("set", "-p", "-t", os.environ.get("TMUX_PANE", ""), "@genai_title", text)


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
    def __init__(self, scr, child, shell=None):
        self.scr, self.child, self.shell = scr, child, shell
        self.m, self.ed = Model(), Editor()
        self.m.shell = shell is not None
        self.shell_where = shell.execution_where() if shell else "OUTSIDE any sandbox, directly on this host"
        self.plan_review = None
        self.running = None        # a /run command in the shell pane: card, command, commands done before it
        self.rows = []             # the block drawn on each screen row of the conversation
        self.state_dir = os.environ.get("GENAI_TUI_STATE") if shell else None
        self.panel = os.environ.get("GENAI_TUI_PANEL") if shell else None
        self.prompt = os.environ.get("GENAI_TUI_PROMPT") if shell else None   # input happens in that pane
        self.hub = Hub(self.state_dir) if self.state_dir else None   # the other panes connect here
        fifo = fifo_path(self.state_dir) if self.state_dir else None
        self.rec = Recorder(fifo if fifo and os.path.exists(fifo) else None)   # each shell command and its output
        if shell:
            shell.primary_prompt = self.rec.at_primary_prompt
        self.ctx_line = None       # where shell context starts, in logical lines (see Shell.lines)
        self.sent = {}             # the last view sent to each role
        self.ask_obj, self.ask_serial = None, 0
        self.mouse = 0
        for name in ("BUTTON1_PRESSED", "BUTTON1_CLICKED", "BUTTON3_PRESSED", "BUTTON4_PRESSED", "BUTTON5_PRESSED"):
            self.mouse |= getattr(curses, name, 0)
        curses.mousemask(self.mouse)
        curses.mouseinterval(0)
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
        """Every line of the conversation, and the block each line belongs to."""
        out, owners = [], []
        for b in self.m.blocks:
            key = (id(b), b.version, w)
            if key not in self.cache:
                self.cache[key] = b.lines(w)
            out += self.cache[key]
            owners += [b] * len(self.cache[key])
        if len(self.cache) > 4 * len(self.m.blocks) + 64:
            self.cache = {}
        return out, owners

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
        ask = self.m.ask
        if ask and not self.m.pending:
            if ask["kind"] == "run":
                c = ask["card"]
                where = self.where()
                cwd = clean(ask["context"][1]) if ask.get("context") else "shown in the full preview"
                lines = [[(f"Run {c.id}{' (edited)' if ask['edited'] else ''} in the shell pane? ", A),
                          (f"It runs {where}, in your shell, as you.", R if where.startswith("OUTSIDE") else "")],
                         [(f"Directory: {cwd}", Q)]]
                if ask.get("context"):
                    lines.append([("  y", A), (" run   ", ""), ("v", A), (" view again   ", ""),
                                  ("e", A), (" edit   ", ""), ("n", A), (" cancel", "")])
                    return lines, None, "y run · v view again · e edit · n or Esc cancel"
                lines.append([("  v", A), (" review full command   ", ""), ("e", A), (" edit   ", ""),
                              ("n", A), (" cancel", "")])
                return lines, None, "v or y review full command · e edit · n or Esc cancel"
            lines = [[(ask["question"], A)],
                     [("  y", A), (" review it in your editor, then send   ", ""), ("n", A), (" keep it to yourself", "")]]
            return lines, None, "y review and send · n or Esc don't share"
        if self.m.pending and self.m.pending[0].ev.get("kind") == "plan":
            c = self.m.pending[0]
            n = len(c.ev.get("steps") or [])
            cmds = sum(1 for st in c.ev.get("steps") or [] if st.get("kind") == "command")
            lines = [[(f"Approve plan {c.id} ({n} steps, {cmds} command{'s' if cmds != 1 else ''})? ", W),
                      ("Planned reads won't ask again; edits still will.", Q)],
                     [("  y", A), (" approve: each planned command asks once   ", ""),
                      ("a", A), (" review plan for automatic commands", "")],
                     [("  e", A), (" choose steps or change commands first   ", ""), ("n", A), (" no   ", ""),
                      ("v", A), (" view it all", "")]]
            if self.plan_review and self.plan_review[0] == c.id:
                lines[1][-1] = (" press a again to approve automatic runs", "")
            if cmds and self.where().startswith("OUTSIDE"):
                lines.append([(f"  Commands run {self.where()}, in your shell, as you.", R)])
            return lines, None, "y approve · a review automatic runs · e edit · n or Esc no · v view"
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
            hints = "Enter send · Alt+Enter new line · ↑ earlier · /help" + (" · /run · /share" if self.shell else "")
        return lines, cur, hints

    def draw(self):
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        lines, cur, hints = self.bottom(w)
        if self.prompt:
            lines, cur, hints = [], None, ""
        status = []
        if self.m.coding:
            status.append("coding on")
        if self.m.context and self.m.context[0] is not None and self.m.context[1]:
            used, limit = self.m.context
            status.append(f"ctx {used / 1000:.1f}k/{limit / 1000:.0f}k ({100 * used // limit}%)")
        area = max(h - len(lines) - 2, 1) if not self.prompt else max(h - (1 if self.scroll else 0), 1)
        body, owners = self.transcript(w)
        if self.scroll and len(body) > self.last_total:
            self.scroll += len(body) - self.last_total       # stay put while scrolled up
        self.last_total = len(body)
        self.scroll = max(0, min(self.scroll, len(body) - area))
        end = len(body) - self.scroll
        self.rows = owners[max(end - area, 0):end]
        for i, segs in enumerate(body[max(end - area, 0):end]):
            self.put(i, 0, segs, w)
        rule = "─" * w
        if self.scroll:
            rule = fit(f"── ↓ {self.scroll} more lines below (PgDn) ", w) + "─" * w
        if not self.prompt or self.scroll:
            self.put(area, 0, [(rule[:w], Q)], w)
        if self.prompt:
            self.scr.refresh()
            curses.curs_set(0)
            return
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
            elif m.ask and seq == "":
                self.answer("n")
            return
        if k == curses.KEY_MOUSE:
            self.click()
            return
        if self.prompt and isinstance(k, str) and k.isprintable() and not (m.pending or m.ask):
            self.shell.tmux("select-pane", "-t", self.prompt)    # typing belongs in the prompt pane
            self.shell.tmux("send-keys", "-t", self.prompt, "-l", k)
            return
        if m.ask and not m.pending:
            if isinstance(k, str):
                self.answer(k.lower())
            return
        if m.pending and m.pending[0].ev.get("kind") == "plan":
            if k in ("y", "Y"):
                m.decide_plan()
            elif k in ("a", "A"):
                self.review_auto_plan(m.pending[0])
            elif k in ("e", "E"):
                self.edit_plan()
            elif k in ("n", "N"):
                m.decide(False)
            elif k in ("v", "V"):
                self.view(m.pending[0])
            elif k in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                self.page(k)
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
        elif k == "\t":
            ed.text = complete(ed.text)
            ed.pos = len(ed.text)
        elif k == "\x0c":
            self.scr.clear()
        elif k in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.page(k)
        else:
            edit_key(ed, k)

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

    def prompt_view(self):
        """What the prompt pane shows: the input box, or our question or genai's approval, with hints."""
        lines, cur, hints = self.bottom(200)
        status = []
        if self.m.coding:
            status.append("coding on")
        if self.m.context and self.m.context[0] is not None and self.m.context[1]:
            used, limit = self.m.context
            status.append(f"context {100 * used // limit}%")
        mode = "input" if cur is not None else ("approval" if self.m.pending else "ask")
        if self.m.ask is not self.ask_obj:
            self.ask_obj, self.ask_serial = self.m.ask, self.ask_serial + 1
        if mode != "input":
            lines = [[(t, "question" if i == 0 and r in (A, W) else ("key" if t.strip() in ("y", "n", "v", "e", "a") else r))
                      for t, r in segs] for i, segs in enumerate(lines)]
        return {"mode": mode, "lines": lines if mode != "input" else [], "hints": "" if mode == "input" else hints,
                "busy": self.m.busy, "status": " · ".join(status), "exited": self.m.exited is not None,
                "q": self.question()}

    def question(self):
        """An id for the question showing in the prompt pane. An answer must name it, so an answer meant for a
        question that has since gone (or not yet come) is ignored, however late or early it arrives."""
        if self.m.pending:
            return f"approve:{self.m.pending[0].id}"
        if self.m.ask:
            return f"ask:{self.ask_serial}"
        return None

    def publish(self):
        """Send the prompt pane and the info panel what changed, as soon as it changed."""
        if not self.hub:
            return
        views = {"panel": self.m.snapshot()}
        if self.prompt:
            views["prompt"] = self.prompt_view()
        for role, view in views.items():
            text = json.dumps(view, ensure_ascii=False, sort_keys=True)
            if text != self.sent.get(role):
                self.sent[role] = text
                self.hub.send(role, {"type": "view", "view": view})

    def from_prompt(self, msg):
        """What the prompt pane sends: typed messages, answers, scrolling, quitting."""
        t = msg.get("type") if isinstance(msg, dict) else None
        if t == "input" and isinstance(msg.get("text"), str):
            self.m.submit(msg["text"])
            self.scroll = 0
        elif t == "key" and isinstance(msg.get("key"), str) and len(msg["key"]) <= 3:
            if msg.get("q") != self.question() or self.question() is None:
                return True                                # an answer to a question that isn't showing
            k = msg["key"]
            if k == "esc":
                if self.m.pending:
                    self.m.decide(False)
                elif self.m.ask:
                    self.answer("n")
            elif self.m.pending or self.m.ask:
                self.key(k)
        elif t == "scroll":
            self.page(curses.KEY_PPAGE if msg.get("up") else curses.KEY_NPAGE)
        elif t == "quit":
            self.m.quit = True
        else:
            return False
        return True

    def toggle_panel(self):
        if not self.panel:
            self.m.note("there is no info panel in this session", R)
            return
        me = os.environ.get("TMUX_PANE", "")
        here = self.shell.tmux("display", "-p", "-t", me, "#{window_id}").strip()
        there = self.shell.tmux("display", "-p", "-t", self.panel, "#{window_id}").strip()
        if here == there:
            self.shell.tmux("break-pane", "-d", "-s", self.panel, "-n", "panel")
        else:
            self.shell.tmux("join-pane", "-d", "-h", "-f", "-l", "42", "-s", self.panel, "-t", me)

    def click(self):
        """The wheel scrolls; a click on a command card opens its menu; a right-click anywhere opens the raw view's."""
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return
        if b & getattr(curses, "BUTTON4_PRESSED", 0):
            self.scroll += 3
        elif b & getattr(curses, "BUTTON5_PRESSED", 0x200000):
            self.scroll = max(self.scroll - 3, 0)
        elif not b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON3_PRESSED):
            return                                  # a release, or a move: only a press opens the menu
        elif b & curses.BUTTON3_PRESSED and self.shell:
            block = self.rows[y] if 0 <= y < len(self.rows) else None
            turn = getattr(block, "turn", 0)
            self.shell.raw_menu(turn if turn in self.m.exchanges else 0)
        elif 0 <= y < len(self.rows) and not self.m.pending and not self.m.ask:
            card = self.rows[y]
            if isinstance(card, Card) and card.ev.get("kind") == "command":
                if not self.shell:
                    self.m.note(f"{card.id}: start genai with genai-tui to send commands to a shell pane", R)
                    return
                self.shell.menu(card.id, self.where())

    def menu_action(self, what, pid):
        """A choice from a command card's menu, sent by request.py."""
        card = self.m.cards.get(pid)
        if not card or card.ev.get("kind") != "command":
            return
        command = card.ev.get("command", "")
        if what == "run":
            self.m.ask = {"kind": "run", "card": card, "command": command, "edited": False}
        elif what == "edit":
            self.m.ask = {"kind": "run", "card": card, "command": command, "edited": False}
            self.answer("e")
        elif what == "prompt":
            try:
                self.shell.type(command, enter=False)
                self.shell.focus()
                self.m.note(f"{card.id} is at the shell prompt; press Enter there to run it, then /share")
            except (Busy, OSError, subprocess.SubprocessError) as e:
                self.m.note(f"{card.id}: could not use the shell pane ({e})", R)
        elif what == "copy":
            self.shell.copy(command)
            self.m.note(f"{card.id} copied (tmux buffer, and your clipboard if the terminal allows it)")

    def where(self):
        return self.shell_where

    def review_context(self):
        try:
            return self.shell.ready()
        except (Busy, OSError, subprocess.SubprocessError) as e:
            self.m.note(f"the shell pane is busy ({e})", R)
            return None

    def review_run(self, ask):
        context = self.review_context()
        if context and self.view_text(f"{ask['card'].id}: runs {self.where()}\n"
                                      f"Directory: {clean(context[1])}\n\n"
                                      f"{command_text(ask['command'])}\n"):
            self.m.ask = {**ask, "context": context}

    def review_auto_plan(self, card):
        if not self.shell or not any(st["kind"] == "command" for st in card.ev.get("steps", [])):
            self.m.note("this plan has no commands that can run in a shell pane", R)
            return
        context = self.review_context()
        if not context:
            self.plan_review = None
            return
        if self.plan_review == (card.id, context):
            self.plan_review = None
            self.m.decide_plan(auto=True, context=context)
            return
        if self.view_text(f"{card.id}: automatic commands run {self.where()}\n"
                          f"Directory: {clean(context[1])}\n\n{card.full_text()}\n"):
            self.plan_review = (card.id, context)
        else:
            self.plan_review = None

    def answer(self, k):
        """A key while one of our questions (run, share) is showing."""
        ask, m = self.m.ask, self.m
        if ask["kind"] == "run":
            card = ask["card"]
            if k == "v" or (k == "y" and not ask.get("context")):
                self.review_run(ask)
            elif k == "y":
                m.ask = None
                self.start_run(card, ask["command"], context=ask["context"])
            elif k == "e":
                reason = " ".join(clean(card.ev.get("reason", "")).split())
                edited = self.edit(ask["command"], f"Proposed command [{card.id}]: {reason}",
                                   "the command that will run, as you", "cancel")
                if edited is None or not edited.strip():
                    m.ask = None
                    m.note(f"{card.id}: cancelled")
                else:
                    m.ask = {**ask, "command": edited, "edited": edited != card.ev.get("command"), "context": None}
            elif k in ("n", "\x1b"):
                m.ask = None
                m.note(f"{card.id}: not run")
        else:
            m.ask = None
            if k == "y":
                if ask.get("context"):
                    text = ("# EVERYTHING below is sent to the model as saved. Remove anything you don't want it to "
                            f"see.\nMy shell session{ask['from']}:\n```\n{ask['output']}\n```\n")
                else:
                    text = review_buffer(ask["command"], ask["rc"], self.where(), ask["output"])
                kept = self.edit(text, ask["label"], "sent to the model exactly as saved; nothing is added",
                                 "send nothing")
                if kept is None or not kept.strip():
                    m.note("nothing sent")
                elif ask.get("context"):
                    m.attach(ask["label"], kept)
                else:
                    m.share(f"output of {ask['what']} (exit {ask['rc']})", kept, f"Here is the output of {ask['what']}.")

    def start_run(self, card, command, context):
        done = self.rec.done
        try:
            self.shell.type(command, expected=context)
        except Busy as e:
            self.m.note(f"{card.id} not run: the shell pane is busy ({e}). When it is back at a prompt, /run "
                        f"{card.id} again.", R)
            return
        except (OSError, subprocess.SubprocessError) as e:
            self.m.note(f"{card.id} not run: tmux failed ({e})", R)
            return
        self.shell.show()                                # so the command can be watched as it runs
        card.state, card.detail = "running", "typed into the shell pane"
        card.changed()
        self.running = {"card": card, "command": command, "done": done}

    def poll_run(self):
        """Has the command from /run finished? Then offer to share its output."""
        r = self.running
        if not r or self.rec.done <= r["done"]:
            return
        self.running = None
        c, card = self.rec.last(), r["card"]
        card.state, card.detail = "ran", f"exit {c.rc}"
        card.changed()
        self.m.ask = {"kind": "share", "label": f"{card.id} finished (exit {c.rc})", "what": card.id,
                      "question": f"{card.id} finished (exit {c.rc}). Share the output with the model?",
                      "command": r["command"], "rc": c.rc, "output": self.output_of(c)}

    def output_of(self, c):
        out = trim(c.text())
        if c.screen:
            out += "\n[it ran a full-screen program; what that drew is not included]"
        return out

    def share_command(self, c, what):
        """/share, /share N, or a click in the info panel: one command from the shell pane and its output."""
        if c is None:
            self.m.note("nothing has run in the shell pane yet" if what == "last" else
                        f"no command {what} in the shell pane (/commands lists them)", R)
            return
        cmd = c.cmd if c.in_history else "(not shown: I kept it out of my shell history)"
        name = fit(clean(label(c)), 40)
        n = c.lines()
        label_ = f"Command {c.n} in the shell pane: {name} (exit {c.rc}, {n} line{'s' if n != 1 else ''})"
        self.m.ask = {"kind": "share", "label": label_, "what": f"command {c.n}", "command": cmd, "rc": c.rc,
                      "output": self.output_of(c), "question": f"{label_}. Share the output with the model?"}

    def list_commands(self):
        cmds = self.rec.finished()
        if not cmds:
            self.m.note("nothing has run in the shell pane yet")
            return
        for c in cmds[-20:]:
            n = c.lines()
            self.m.note(f"{c.n:>3} {'✓' if c.rc == 0 else '✗'} {fit(clean(label(c)), 32):32} exit {c.rc} · "
                        f"{n} line{'s' if n != 1 else ''} · {ago(c.ended)}", "" if c.rc == 0 else W)
        self.m.note("/share N reviews one, then sends it; so does a click on it in the info panel")

    def recent(self):
        """The info panel's COMMANDS: the latest first, with what a click shares."""
        rows = [{"n": c.n, "cmd": clean(label(c)), "rc": c.rc, "lines": c.lines(), "ago": ago(c.ended),
                 "screen": c.screen} for c in reversed(self.rec.finished()[-10:])]
        cur = self.rec.running()
        if cur:
            rows.insert(0, {"n": cur.n, "cmd": "", "rc": None, "lines": None, "ago": "", "running": True})
        return rows

    def share_context(self, what):
        """Shell history for the model to refer to: everything, or from the line the user marked."""
        if what == "mark" and self.ctx_line is None:
            self.m.note("no mark yet: right-click a line in the shell pane and choose \"Mark start of "
                        "context here\", or /mark", R)
            return
        try:
            lines = self.shell.lines()
        except (OSError, subprocess.SubprocessError) as e:
            self.m.note(f"tmux failed ({e})", R)
            return
        end = max(len(lines) - self.rec.ps1_lines, 0)        # the current prompt isn't context
        out = "\n".join(lines[(self.ctx_line if what == "mark" else 0):end]).strip("\n")
        if not out.strip():
            self.m.note("the shell pane has nothing " + ("after the mark" if what == "mark" else "to share") + " yet", R)
            return
        n = out.count("\n") + 1
        label_ = f"{n} lines of shell history" + (" from the mark" if what == "mark" else "")
        self.m.ask = {"kind": "share", "context": True, "label": label_, "output": out,
                      "from": " from the line I marked" if what == "mark" else "",
                      "question": f"Share {label_} with the model? It goes with your next message."}

    def mark_here(self):
        """/mark: shell context starts at the shell's current prompt."""
        try:
            self.ctx_line = max(len(self.shell.lines()) - self.rec.ps1_lines, 0)
        except (OSError, subprocess.SubprocessError) as e:
            self.m.note(f"tmux failed ({e})", R)
            return
        self.m.note("shell context starts at the shell's current prompt; /share mark sends it from there")

    def mark_line(self):
        """The right-click menu marked a line: turn its row number into a logical line now, before a resize could
        move it."""
        try:
            raw = self.shell.opt("@genai_ctx_mark")
            if not raw.isdigit():
                return
            self.ctx_line = len(self.shell.lines(upto=int(raw))) - 1
            self.shell.tmux("display-message", "-t", self.shell.pane, "genai: shell context starts at this line")
        except (OSError, subprocess.SubprocessError) as e:
            self.m.note(f"tmux failed ({e})", R)

    def edit(self, text, title, what, cancel):
        """Open text in the user's editor; what they save, without # lines. None if the editor failed."""
        fd, path = tempfile.mkstemp(prefix="genai-tui-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(EDITOR_HEADER.format(title=" ".join(title.split()), what=what, cancel=cancel) + text)
            curses.endwin()
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()
            try:
                rc = self.run_here(editor_argv(path))
            except OSError as e:
                self.m.note(f"could not start your editor: {e}", R)
                return None
            if rc != 0:
                self.m.note(f"your editor exited with code {rc}; nothing was used", R)
                return None
            with open(path, encoding="utf-8", errors="replace") as f:
                return strip_comments(f.read())
        finally:
            os.unlink(path)
            sys.stdout.write("\x1b[?2004h")
            sys.stdout.flush()
            self.scr.refresh()

    def edit_plan(self):
        """The plan in the user's editor: delete a step's line to leave it out, change a command to approve that
        instead. Paths can't be changed here (the model chose them; leave the step out instead)."""
        card = self.m.pending[0]
        steps = {st.get("n"): st for st in card.ev.get("steps") or []}
        text = "\n".join(f"# {st.get('n')}. {clean(st.get('why', ''))}\n{plan_line(st)}" for st in steps.values())
        edited = self.edit(text, f"Plan {card.id}: {clean(card.ev.get('goal', ''))}",
                           "the steps you approve. Delete a line to leave that step out; change a command after "
                           "'run' to approve your version instead. Paths can't be changed", "decline the plan")
        if edited is None:
            return                                          # the editor failed: the question stays
        keep, edits, bad = parse_plan_edit(edited, steps)
        if bad:
            self.m.note(f"{card.id}: {bad}; nothing decided yet", R)
        elif not keep:
            self.m.decide(False)
            self.m.note(f"{card.id}: every step was left out, so the plan was declined")
        else:                                               # after editing, each planned command asks once
            self.m.decide_plan(approve=None if keep == set(steps) else sorted(keep), edits=edits)

    def plan_cmd(self, card):
        """genai marked a command as one the user approved in the plan, and our own record agrees."""
        if self.m.ask or self.m.pending or self.running or not self.m.command_authorized(card):
            return                                          # it stays a card: click it or /run it
        if self.m.plan_auto and self.m.plan_context:
            ev = card.ev
            self.m.plan_cmds.pop((ev.get("plan_id"), ev.get("plan_step")))
            self.start_run(card, ev["command"], context=self.m.plan_context)
        else:
            self.m.ask = {"kind": "run", "card": card, "command": card.ev.get("command", ""), "edited": False}

    def run_here(self, argv, full=False):
        """Run an editor or pager where the user can use it: in a tmux popup when typing happens in the prompt
        pane, else in this pane. Returns its exit code."""
        if not self.prompt:
            return subprocess.call(argv)
        cmd = shlex.join(argv).replace("#", "##")        # a popup's command is a tmux format
        size = ["-w", "100%", "-h", "100%"] if full else ["-w", "90%", "-h", "85%"]
        return subprocess.call(["tmux", "display-popup", "-E", *size, cmd])

    def show_raw(self, ev):
        """A trace or system answer from genai, full screen in a pager. The file holds the whole conversation, so
        it goes in genai-tui's private directory (or a private one of its own) and is gone when the pager closes."""
        text = render_system(ev) if ev.get("type") == "system" else render_trace(ev, self.m.exchanges)
        where = self.state_dir or tempfile.mkdtemp(prefix="genai-tui-raw-")
        fd, path = tempfile.mkstemp(prefix="raw-", suffix=".txt", dir=where)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            curses.endwin()
            pager = ["less", "-+F", "-i", path] if shutil.which("less") else ["more", path]
            self.run_here(pager, full=True)
        finally:
            os.unlink(path)
            if not self.state_dir:
                os.rmdir(where)
            self.scr.refresh()

    def page(self, k):
        h, _ = self.scr.getmaxyx()
        step = max(h - 6, 1)
        self.scroll = self.scroll + step if k == curses.KEY_PPAGE else max(self.scroll - step, 0)

    def view(self, card):
        """Show a proposal in full in a pager: cleaned text, so nothing in it reaches the terminal raw."""
        return self.view_text(f"{card.id}: {card.title()}\n\n{card.full_text()}\n")

    def view_text(self, text):
        fd, path = tempfile.mkstemp(prefix="genai-preview-", suffix=".txt", dir=self.state_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        curses.endwin()
        try:
            pager = ["less", "-+F", path] if shutil.which("less") else ["more", path]
            return self.run_here(pager) == 0
        except OSError as e:
            self.m.note(f"could not open the full preview: {e}", R)
            return False
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
            if self.hub:
                self.hub.close()

    def pump(self):
        self.poll_run()
        while self.hub:
            try:
                role, msg = self.hub.inbox.get_nowait()
            except queue.Empty:
                break
            if role == "hello":
                self.sent.pop(msg, None)                  # a pane (re)connected: send it everything
            elif role == "prompt":
                self.from_prompt(msg)
            elif role == "panel":
                self.m.request(msg)
            elif role == "menu" and msg.get("type") in ("share", "mark", "raw", "system"):
                self.m.request(msg)
            elif role == "menu" and msg.get("type") == "menu" and isinstance(msg.get("id"), str):
                if msg.get("action") in ("run", "edit", "prompt", "copy"):
                    self.menu_action(msg["action"], msg["id"])
        for action in self.m.actions:
            if action == ("share", "last"):
                self.share_command(self.rec.last(), "last")
            elif action[0] == "share_cmd":
                self.share_command(self.rec.get(action[1]), action[1])
            elif action[0] == "commands":
                self.list_commands()
            elif action[0] == "show":
                self.show_raw(action[1])
            elif action[0] == "plan_cmd":
                self.plan_cmd(action[1])
            elif action[0] == "mark_line":
                self.mark_line()
            elif action[0] == "unmark":
                self.ctx_line = None
            elif action[0] == "share":
                self.share_context(action[1])
            elif action[0] == "mark":
                self.mark_here()
            elif action[0] == "focus_shell":
                self.shell.focus()
            elif action[0] == "panel":
                self.toggle_panel()
        self.m.actions = []
        while True:
            try:
                ev = self.child.events.get_nowait()
            except queue.Empty:
                break
            if ev.get("type") == "_exit":
                self.m.exit(ev["code"], ev.get("err", ""))
            else:
                self.m.handle(ev)
                if ev.get("type") == "hello" and self.shell:
                    epn = (ev.get("endpoint") or {})
                    self.shell.title(f"genai · {epn.get('name', '?')}: {epn.get('model', '?')}")
        for msg in self.m.outbox:
            self.child.send(msg)
        self.m.outbox = []
        if self.shell:
            self.m.recent = self.recent()
        self.publish()


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="genai-tui", description="genai as a coding agent in tmux: the conversation, your shell below it and "
        "an info panel beside it. The model only proposes; you approve, and you run commands in your own shell.")
    ap.add_argument("-p", "--profile", help="profile (persona) name")
    ap.add_argument("-e", "--endpoint", help="endpoint name (overrides the profile's)")
    ap.add_argument("-S", "--session", help="keep the conversation in a named session (created or resumed)")
    ap.add_argument("-k", "--skill", action="append", default=[], help="use a blessed skill (repeatable)")
    ap.add_argument("--coding", action="store_true", help="let the model read and list files here without asking")
    ap.add_argument("--coding-anywhere", action="store_true",
                    help="--coding, also where it is normally refused; genai says what that exposes")
    ap.add_argument("--no-panel", action="store_true", help="start without the info panel (/panel shows it)")
    ap.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def genai_args(a):
    """The options genai itself gets; everything else is the TUI's."""
    out = []
    for flag, value in (("-p", a.profile), ("-e", a.endpoint), ("-S", a.session)):
        if value:
            out += [flag, value]
    for k in a.skill:
        out += ["-k", k]
    if a.coding_anywhere:
        out.append("--coding-anywhere")
    elif a.coding:
        out.append("--coding")
    return out


def main(argv):
    args = parse_args(argv)
    if args.check:
        return 0
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    child = Child(genai_args(args))
    try:
        curses.wrapper(lambda scr: App(scr, child, Shell.from_env()).run())
    finally:
        child.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
