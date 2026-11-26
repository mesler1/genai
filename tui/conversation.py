#!/usr/bin/env python3
"""hoplon-tui conversation pane: a coding-agent front end for `hoplon --json`.

One scrolling conversation with the model's reply streaming in, an input box at the bottom,
slash commands, and approval prompts that take the input box's place while hoplon waits.
hoplon decides what needs approval and enforces every rule; this program shows proposals and
returns the user's answers, nothing more.

Usage: conversation.py [hoplon options such as -p PROFILE -e ENDPOINT -S SESSION]
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
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import palette  # noqa: E402
from features import disabled_names, load as load_features, tui_setting  # noqa: E402
from ipc import Hub  # noqa: E402
from recorder import Recorder, ago, fifo_path, label, trim  # noqa: E402
import remote  # noqa: E402

HOPLON = os.environ.get("HOPLON_BIN") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                    "hoplon")
CARD_BODY_LINES = 12
OUTPUT_LINES = 8               # last lines of a sandboxed run's output shown in its card
INPUT_MAX_LINES = 8
CMD_LINES = 4                  # lines of a command shown in a run question; a longer one is viewed in full first
MENU_KEYS = 9                  # Alt+1 .. Alt+9 open the menus of the newest command proposals, Alt+1 the newest

COMMANDS = {
    "/help": "show this help",
    "/coding": "on|off: let the model read and list files here without asking",
    "/network": "[on|off]: the sandbox's network, for this session (on: each sandboxed run asks)",
    "/skills": "list skills; * marks the ones in use",
    "/skill": "NAME on|off: use a blessed skill from the next message on, or stop",
    "/run": "[ID]: review and run a proposed command in the shell pane (the latest by default)",
    "/share": "[N|mark|all]: send shell output, reviewed first or as it is: the last command, command N, from the "
              "mark, or all",
    "/commands": "list the commands run in the shell pane, numbered for /share N (also in the info panel)",
    "/mark": "shell context starts at the shell's current prompt (or right-click a line in the shell)",
    "/shell": "show the shell and move there (Ctrl-` hides it again)",
    "/panel": "show or hide the info panel on the right",
    "/plan": "[clear]: show the approved plan, or drop it so everything asks again",
    "/stop": "stop the sandboxed run going on now, or hoplon's turn (Ctrl-C on an empty input does the same)",
    "/compact": "shrink the conversation when the context is getting full",
    "/context": "show how full the context is",
    "/clear": "start over: the model forgets this conversation, and the screen clears (the saved session keeps it)",
    "/quit": "leave (also Ctrl-D on an empty input)",
}
# The commands of the features are in tui/features. Of the core's own, these wait while hoplon is replying:
TO_HOPLON = ("/coding", "/skills", "/skill", "/compact", "/network", "/clear")

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



def one_row(text):
    """clean(text) for a place that is one row on screen: a line break shows as ⏎ instead of starting a new row."""
    return clean(text).replace("\n", "⏎")

def command_text(command):
    """A command as it is shown for review: exactly as it runs, backslashes and all, with tabs as ⇥. Characters
    that would show as escapes are never sent to the shell (see unsafe_chars), so an escape on screen can't stand
    for something else."""
    return clean(command.replace("\t", "⇥"))


def unsafe_chars(command):
    """Control, bidi and invisible characters: clean() shows them as escapes, so the command shown would not be
    the command typed. Such a command is never sent to the shell pane."""
    return [ch for ch in command if ch not in "\n\t" and (ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0 or ord(ch) in _INVISIBLE
            or unicodedata.category(ch) in ("Cf", "Cs", "Co", "Cn", "Zl", "Zp"))]


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
    def __init__(self, text, queued=False, attachments=()):
        super().__init__()
        self.text, self.queued, self.attachments = text, queued, tuple(attachments)

    def lines(self, w):
        out = [[]] + para(clean(self.text), "", w, "› ", "  ", first_role=A)
        for attachment in self.attachments:
            label = clean(attachment["label"])
            lines = attachment["text"].count("\n") + 1
            out.append([("  ", Q), (fit(f"[attached: {label}, {lines} lines]", w - 2), Q)])
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


def hoplon_where(h):
    """Where hoplon itself runs, from its hello, when that is another host: (text, role), red for root. Else None."""
    if not h.get("remote"):
        return None
    user, root = clean(str(h.get("user", "?"))), h.get("uid") == 0 or h.get("user") == "root"
    return (f"hoplon runs ON {clean(str(h.get('host', '?')))} AS {user} in {clean(str(h.get('cwd', '?')))}"
            + (" (root)" if root else ""), R if root else W)


class Banner(Block):
    def __init__(self, hello, shell_where=None):
        super().__init__()
        self.h, self.shell_where = hello, shell_where

    def lines(self, w):
        h, ep = self.h, self.h.get("endpoint") or {}
        rows = [(f"◆ hoplon {h.get('version', '?')}", A),
                (f"{ep.get('name', '?')}: {ep.get('model', '?')} at {ep.get('base_url', '?')}", "")]
        where = hoplon_where(h)
        if where:
            rows.append(where)
            rows.append((f"it keeps its audit log there: {h.get('audit') or '?'}", ""))
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
        run = h.get("runner")
        if run and h.get("coding") and not run.get("problem"):
            rows.append((f"The model runs builds and tests itself in a podman sandbox ({run.get('image')}), on a "
                         "throwaway copy of the project, " + ("WITH the network: each run asks first." if
                                                             run.get("network") else "offline."), W))
        elif run and run.get("problem"):
            rows.append((f"The profile's sandbox can't run here: {run['problem']}.", R))
        if self.shell_where:
            rows.append((f"Commands you approve run {self.shell_where}.", W))
        else:
            rows.append(("Commands are shown for you to run in your own shell.", W))
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


MARK = {R: "✗", W: "⚠", D: "✓"}      # notes: the symbol before one in that role (a plain note has none)
RUNS = ("sandbox", "finish")     # kinds of card that run something in the sandbox: a clock, and /stop stops them
STATE_MARK = {"waiting": "◆", "shown": "▸", "running": "…", "accepted": "✓", "done": "✓", "ran": "✓",
              "declined": "⊘", "cancelled": "⊘", "error": "✗", "auto": "↳", "stopped": "■"}
# A proposal id, as it may go into a tmux format (the status line): hoplon's are p1, p2 ...
PROPOSAL_ID = re.compile(r"[a-z]{1,4}[0-9]{1,6}")


class Card(Block):
    """A proposal. Its state changes as it is decided and performed."""
    STATE_ROLE = {"waiting": W, "accepted": D, "done": D, "declined": Q, "error": R, "shown": A, "auto": Q,
                  "running": A, "ran": D, "cancelled": Q, "stopped": W}

    def __init__(self, ev):
        super().__init__()
        self.ev, self.state, self.detail = ev, "waiting", ""
        self.output = None         # a sandboxed run's output, as the model got it
        self.key = None            # N when Alt+N opens this command's menu
        self.started = None        # time.monotonic() when a sandboxed run started
        self.stoppable = True      # /stop can stop it (in remote mode, only a hoplon that takes the stop message)
        self.shown_secs = None     # the elapsed seconds last drawn

    def start(self, stoppable=True):
        self.state, self.started, self.stoppable = "running", time.monotonic(), stoppable
        self.changed()

    def elapsed(self):
        return int(time.monotonic() - self.started) if self.started is not None else 0

    @property
    def id(self):
        return self.ev.get("id", "?")

    def title(self):
        """One row: every field in it is shown with line breaks as ⏎."""
        ev, k = self.ev, self.ev.get("kind")
        row = lambda v: one_row(str(v))     # noqa: E731
        count = lambda v: len(v) if isinstance(v, list) else "?"     # noqa: E731
        path = row(ev.get("path", ""))
        return {"read": f"read {path}", "list": f"list {path or '.'}", "edit": f"edit {path}",
                "search": f"search {row(ev.get('pattern', ''))!r} in {path or '.'}"
                          + (f" ({row(ev['glob'])})" if ev.get("glob") else ""),
                "write": f"{'overwrite' if ev.get('exists') else 'create'} {path}",
                "command": "command", "external": f"tool {row(ev.get('tool', ''))}",
                "samples": "samples requested",
                "sandbox": f"sandbox · {'ONLINE' if ev.get('network') else 'offline'}",
                "trusted": f"{row(ev.get('plugin', '?'))} · {row(ev.get('command', '?'))}"
                           + (" · read-only" if ev.get("read_only") else ""),
                "plan": f"plan · {count(ev.get('steps'))} steps",
                "work_plan": f"work plan · {count(ev.get('items'))} items",
                "finish": f"finish item {row(ev.get('n', '?'))}: {row(ev.get('summary', ''))}",
                "retry": f"the planner retries item {row(ev.get('n', '?'))}"}.get(k if isinstance(k, str) else None,
                                                                                 row(k))

    def body(self, full=False):
        """(text, role) lines inside the card. A sandboxed run's output is cut to its last lines unless full."""
        ev, k, out = self.ev, self.ev.get("kind"), []
        if ev.get("preapproved"):
            out.append((f"step {ev.get('plan_step')} of the plan you approved", D))
        elif ev.get("plan_repeat"):
            out.append((f"step {ev.get('plan_step')} of the plan, already run once: it asks again", Q))
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
        if k == "samples":
            out += [(ln, "") for ln in clean(str(ev.get("what", ""))).split("\n")]
            files = ev.get("files") if isinstance(ev.get("files"), list) else []
            out.append(("files its tests will read in /fixtures: " + ", ".join(one_row(str(f)) for f in files[:20]), D))
            if ev.get("suggest"):
                out.append(("you could make them with:  " + one_row(str(ev["suggest"])), ""))
            out += [("Make them in your shell, scrubbing anything private,", A),
                    ("then /fixtures add FILE… and /fixtures use.", A),
                    ("The model never runs anything for this.", Q)]
        if k in ("command", "sandbox"):
            for i, ln in enumerate(command_text(ev.get("command", "")).split("\n")):
                out.append((("$ " if i == 0 else "  ") + ln, ""))
        elif k == "trusted":                             # a plugin's trusted command: what runs, as hoplon shows it
            out.append(("$ " + command_text(str(ev.get("argv", ""))).replace("\n", " "), ""))
        if k in RUNS and self.state == "running":
            self.shown_secs = self.elapsed()
            out.append((f"running {self.shown_secs}s · " + ("Ctrl-C (or /stop) stops it" if self.stoppable else
                        "it can't be stopped from here in remote mode yet"), A))
        if k in ("sandbox", "trusted", "finish") and self.output is not None:
            lines = clean(self.output).rstrip("\n").split("\n") if self.output.strip() else ["(no output)"]
            if not full and len(lines) > OUTPUT_LINES:
                out.append((f"… {len(lines) - OUTPUT_LINES} earlier lines of output (v views it all)", Q))
                lines = lines[-OUTPUT_LINES:]
            out += [("│ " + ln, Q) for ln in lines]
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
        elif k == "work_plan":
            if ev.get("goal") != ev.get("reason"):              # hoplon sends the goal as the reason too
                out.append((clean(str(ev.get("goal", ""))), A))
            items = ev.get("items") if isinstance(ev.get("items"), list) else []
            out += [(f"{n:>2}. {one_row(it)}", "") for n, it in enumerate(items, 1)]
        elif k == "finish" and ev.get("notes"):
            out += [("notes for later items:", Q)] + [("  " + ln, Q) for ln in clean(str(ev["notes"])).split("\n")]
        elif k == "retry" and ev.get("hint"):
            out += [("  " + ln, Q) for ln in clean(str(ev["hint"])).split("\n")]
        elif k == "plan":
            for st in ev.get("steps") or []:
                out.append((f"{st.get('n', '?'):>2}. {step_text(st)}", ""))
                out.append((f"    {clean(st.get('why', ''))}", Q))
        return out

    def full_text(self):
        return "\n".join(t for t, _ in self.body(full=True))

    def place(self):
        """Where approving this acts: ▲ live, ⊡ isolated, an arrow where it crosses from the model to your side."""
        k = self.ev.get("kind")
        if k in ("edit", "write"):
            return "⊡ the copy" if self.ev.get("work") or self.ev.get("in_copy") else "⊡→▲ your files"
        return {"command": "⊡→▲ your shell", "trusted": "▲ runs live", "sandbox": "⊡ a throwaway run",
                "samples": "▲→⊡ from your side"}.get(k if isinstance(k, str) else None, "")

    def lines(self, w):
        if self.state == "auto":
            why = "work session" if self.ev.get("work") or self.ev.get("kind") in ("finish", "retry") else \
                "coding mode: no approval needed"
            return [[("  ↳ ", Q), (fit(f"{self.title()}  ({why})", w - 4), Q)]]
        role = self.STATE_ROLE.get(self.state, Q)
        inner = max(w - 4, 8)
        place = self.place()
        head = f"─ {self.id} · {f'{place} · ' if place else ''}{self.title()}{f' · Alt+{self.key}' if self.key else ''} "
        tail = f" {STATE_MARK.get(self.state, '·')} {self.state} ─"
        head = fit(head, w - 2 - width(tail) - 1)
        fill = max(w - 2 - width(head) - width(tail), 1)
        out = [[], [("╭" + head + "─" * fill + tail + "╮", role)]]
        body = self.body()
        shown = body if len(body) <= CARD_BODY_LINES + 1 else body[:CARD_BODY_LINES]
        rows = [(fit(t, inner), r) for t, r in shown]
        if len(shown) < len(body):
            rows.append((f"… {len(body) - len(shown)} more lines (v views it all)", Q))
        if self.ev.get("kind") == "command" and self.state == "shown":
            how = f"click it, press Alt+{self.key}," if self.key else "click it,"
            rows.append((f"{how} or /run {self.id}, to send it to your shell; hoplon never runs it", A))
        if self.detail:
            rows.append((fit(clean(self.detail), inner), R if self.state == "error" else Q))
        for t, r in rows:
            out.append([("│ ", role), (t, r), (" " * (inner - width(t)) + " │", role)])
        out.append([("╰" + "─" * (w - 2) + "╯", role)])
        return out


# --------------------------------------------------------------------------
# State: hoplon's events in, the user's messages out. No curses here, so it can be tested.

class Model:
    def __init__(self, features=None):
        self.features = load_features(core=CORE) if features is None else features   # see tui/features
        self.blocks = []
        self.cards = {}
        self.pending = []          # cards waiting for a decision, oldest first
        self.reply = None          # the reply being streamed
        self.busy = False          # a turn is running
        self.queued = []           # messages typed while busy
        self.outbox = []           # messages for hoplon
        self.next_attachments = [] # reviewed shell context for the next prompt the user submits
        self.hello = None
        self.coding = False
        self.runner = None         # the profile's sandbox, from hello and network events
        self.context = None        # (used, limit)
        self.exited = None         # hoplon's exit code once it has gone
        self.quit = False
        self.shell = False         # a shell pane is there (inside hoplon-tui)
        self.shell_where = None
        self.skills = None         # hoplon's skill list, once asked for
        self.show_skills = False   # print the next skill list in the conversation (the user typed /skills)
        self.ask = None            # a question of ours in place of the input box: run or share
        self.actions = []          # things for the screen to do: ("share", "last"), ("focus_shell",)
        self.panes = []            # messages for the other panes: (role, message)
        self.recent = []           # the shell pane's latest commands, for the info panel
        self.turn = 0              # turns sent to hoplon so far
        self.exchanges = {}        # turn: [first, last] exchange with the endpoint, for /raw
        self.plan = None           # the approved plan, as hoplon last reported it
        self.plan_cmds = {}        # (plan id, step number) -> approved command, consumed before execution
        self.plan_context = None   # execution pane and directory reviewed for automatic runs
        self.plan_auto = False     # run those commands as they come, without asking again

    def add(self, block):
        if "turn" not in vars(block):
            block.turn = self.turn
        self.blocks.append(block)
        return block

    def note(self, text, role=Q):
        self.add(Note(text, role, MARK[role] + " " if role in MARK else "  "))

    def number_commands(self):
        """Alt+1 for the newest command proposal, Alt+2 for the one before, up to MENU_KEYS."""
        cmds = [c for c in self.cards.values() if c.ev.get("kind") == "command"]
        live = [c for c in cmds if c.state != "declined"]          # a declined one keeps /run, not a key
        keys = {id(c): i + 1 for i, c in enumerate(reversed(live)) if i < MENU_KEYS}
        for c in cmds:
            key = keys.get(id(c))
            if c.key != key:
                c.key = key
                c.changed()

    def keyed(self, n):
        return next((c for c in self.cards.values() if c.ev.get("kind") == "command" and c.key == n), None)

    def send(self, msg):
        self.outbox.append(msg)

    def ask_approval(self, kind, ev):
        """An approval question for a feature's request. Only the core builds and answers these. fixtures_use offers
        exactly the digest hoplon listed; hoplon refuses it if the files changed since, and checks again before runs."""
        if kind == "fixtures_use" and isinstance(ev, dict) and isinstance(ev.get("digest"), str):
            self.ask = {"kind": kind, "digest": ev["digest"],
                        "question": clean(str(ev.get("ask") or "Use these samples for this job?"))}
        elif kind == "fixtures_clear":
            self.ask = {"kind": kind, "question": "Delete the samples folder and everything in it?"}
        elif kind == "leftovers_clean" and isinstance(ev, dict) and isinstance(ev.get("ids"), list) and ev["ids"]:
            n = len(ev["ids"])
            self.ask = {"kind": kind, "ids": [i for i in ev["ids"] if isinstance(i, str)],
                        "question": f"Remove the {n} stale leftover{'s' if n != 1 else ''} marked ✗ "
                                    "(not those marked !)?"}

    def feature(self, name):
        return next((f for f in self.features if f.name == name), None)

    def commands(self):
        """Every command there is, for /help and completion: the core's first, then each feature's."""
        return command_list(self.features)

    def takes(self, role, kind):
        """Does some feature take this kind of request from this role?"""
        return any(role in f.requests.get(kind, ()) for f in self.features)

    def run_started(self, card):
        """A command was typed into the shell pane to run, so features that held it back let go of it."""
        for f in self.features:
            f.run_started(self, card)

    # -- from hoplon

    def handle(self, ev):
        t = ev.get("type")
        if t != "text":
            self.reply = None
        if t == "hello":
            self.hello, self.coding, self.runner = ev, bool(ev.get("coding")), ev.get("runner")
            if ev.get("remote") and isinstance(ev.get("host"), str):
                self.shell_where = remote_where(clean(ev["host"]))   # the shell pane follows hoplon to that host
            self.add(Banner(ev, self.shell_where))
            if "skills" in (ev.get("features") or []):
                self.send({"type": "skills"})          # for the info panel; not printed
        elif t == "text":
            if self.reply is None:
                self.reply = self.add(Reply())
            self.reply.add(ev.get("text", ""))
        elif t == "proposal":
            card = self.add(Card(ev))
            self.cards[card.id] = card
            if ev.get("auto") and (ev.get("kind") == "sandbox" or (ev.get("kind") == "finish" and ev.get("test"))):
                card.start(self.can_stop())
            elif ev.get("auto") and ev.get("kind") == "trusted":
                card.state = "running"
            elif ev.get("auto") and ev.get("approved_file"):
                card.state = "accepted"                 # shown in full: an edit that didn't ask
                card.detail = "applied without asking: you approved an edit to this file earlier (/edits)"
            elif ev.get("auto"):
                card.state = "auto"
            elif ev.get("kind") == "command":
                card.state = "shown"
                self.number_commands()
                if ev.get("preapproved") and self.command_authorized(card) and self.shell:
                    self.actions.append(("plan_cmd", card))
            elif ev.get("kind") == "samples":
                card.state = "shown"                    # nothing to approve: you make them, then /fixtures
            elif ev.get("kind") == "invalid":
                card.state = "error"
                card.detail = f"the model asked for a tool that does not exist: {ev.get('tool', '?')}"
            else:
                self.pending.append(card)
        elif t == "result":
            card = self.cards.get(ev.get("id"))
            if card:
                card.state = {"ok": "done", "stopped": "stopped", "skipped": "stopped"}.get(ev.get("status"), "error")
                card.detail = ev.get("detail") or ""
                if isinstance(ev.get("output"), str):
                    card.output = ev["output"]
                card.changed()
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
            self.retire("the turn ended before you answered")
            self.context = ((ev.get("context") or {}).get("used"), (ev.get("context") or {}).get("limit"))
            if ev.get("reason") == "step_limit":
                self.note("hoplon paused: many steps in a row ran without you deciding anything (max_steps); "
                          "send a message to continue", W)
            elif ev.get("reason") == "work_limit":
                self.note("the work session paused at a limit: send a message to go on, or /work end", W)
            self.next_queued()
        elif t == "context":
            self.context = (ev.get("used"), ev.get("limit"))
        elif t == "error" and any(f.error(self, ev) for f in self.features):
            pass                           # a feature's own request failed, and it has said so
        elif t == "error" and ev.get("code") == "not_running":
            self.note("nothing is running in the sandbox now")     # it ended as the stop was sent
        elif t == "error":
            if ev.get("exchanges"):
                self.exchanges[self.turn] = ev["exchanges"]
            self.note(f"error: {ev.get('message', '')}", R)
            if self.busy and not self.pending and ev.get("code") not in ("busy", "unknown_id", "bad_message",
                                                                         "unknown_type", "not_running"):
                self.busy = False          # a failed turn ends without a done event
                self.next_queued()
        elif t == "notice":
            self.note(clean(ev.get("message", "")), W)
        elif t == "coding":
            self.coding = bool(ev.get("on"))
            self.note(f"coding mode is {'on' if self.coding else 'off'}", W if self.coding else Q)
        elif t == "network":
            self.runner = ev.get("runner")
            self.note(self.network_text(), W if (self.runner or {}).get("network") else Q)
        elif t == "skills":
            rows = ev.get("skills") or []
            self.skills = rows
            if not self.show_skills:
                return
            self.show_skills = False
            if not rows:
                self.note("no skills found (hoplon --list-skills shows where they are looked for)")
            for r in rows:
                self.note(f"{'*' if r.get('active') else ' '} {clean(r.get('name', '')):20} {r.get('status', ''):10} "
                          f"{clean(r.get('description', ''))}", "" if r.get("active") else Q)
            if any(r.get("status") != "blessed" for r in rows):
                self.note("hoplon --list-skills shows the command that blesses each one")
        elif t == "compacted":
            self.note(f"compacted: {ev.get('before')} → {ev.get('after')} tokens", D)
        elif t == "cleared":
            self.blocks, self.cards, self.reply = [], {}, None
            ctx = ev.get("context") or {}
            if isinstance(ctx.get("used"), int) and isinstance(ctx.get("limit"), int):
                self.context = (ctx["used"], ctx["limit"])
            self.note(f"context cleared: the model starts over ({ev.get('dropped', 0)} messages dropped)", D)
        elif t == "attached":
            masked = ev.get("redacted") or {}
            if masked:
                self.note("hoplon masked " + ", ".join(f"{n} {clean(k)}" for k, n in masked.items())
                          + " in what you shared", W)
        else:
            for f in self.features:
                if t in f.events:
                    f.event(self, ev)

    def remote(self):
        return bool((self.hello or {}).get("remote"))

    def running_card(self):
        """The sandboxed run going on now, if any."""
        return next((c for c in reversed(list(self.cards.values()))
                     if c.ev.get("kind") in RUNS and c.state == "running"), None)

    def can_stop(self):
        features = (self.hello or {}).get("features") or []
        return "stop" in features or ("interrupt" in features and not self.remote())

    def can_halt(self):
        """hoplon stops a whole turn on the stop message, not only a sandboxed run."""
        return "halt" in ((self.hello or {}).get("features") or [])

    def stop(self):
        """/stop and Ctrl-C: ask hoplon to stop the sandboxed run: the stop message, which works over ssh too, or
        from an older hoplon here, a SIGINT to the hoplon child. A hoplon that can halt stops the whole turn."""
        card = self.running_card()
        if not card and self.busy and self.can_halt() and self.exited is None:
            self.note("stopping hoplon's turn: its reply ends at the next words, and what is waiting is declined…", W)
            self.send({"type": "stop"})
        elif not card:
            self.note("nothing is running in the sandbox. A command in the shell pane stops with Ctrl-C there "
                      "(/shell shows it).")
        elif "stop" in ((self.hello or {}).get("features") or []):
            self.note(f"stopping the sandboxed run {clean(card.id)}…", W)
            self.send({"type": "stop"})
        elif self.remote():
            self.note("stopping a sandboxed run isn't available in remote mode yet; it ends at its time limit", W)
        elif "interrupt" not in ((self.hello or {}).get("features") or []):
            self.note("this hoplon can't stop a sandboxed run; it ends at its time limit", R)
        else:
            self.note(f"stopping the sandboxed run {clean(card.id)}…", W)
            self.actions.append(("interrupt",))

    def tmux_status(self):
        """The sandbox part of tmux's status-right, after a "waiting for you" while a card waits for a decision. It
        is a tmux format, so only fixed words, a checked proposal id and a number go in."""
        if self.hello is None:
            return ""
        wait = f" {palette.tmux('iron', 'brass', bold=True)} ⏸ waiting for you #[default] │" if self.pending else ""
        return wait + self.sandbox_status()

    def shell_title(self):
        """The shell pane's title: whether what you type there is live. A tmux format, so the host is reduced to
        the characters a host name has."""
        hints = " · Ctrl-` hides it · right-click: shell context"
        where = self.shell_where or ""
        if where.startswith("inside"):
            return f"{palette.tmux('patina', bold=True)}⊡ your shell, {where}#[default]{hints}"
        if where.startswith("ON ANOTHER HOST"):
            host, how = (self.hello or {}).get("host"), ", over ssh"
        elif where.startswith("OUTSIDE"):
            host, how = socket.gethostname(), ""
        else:
            return f"your shell, isolation unverified{hints}"
        host = re.sub(r"[^A-Za-z0-9._@-]", "", str(host or "")) or "?"
        return f"{palette.tmux('brass', bold=True)}▲ LIVE: your shell on {host}{how}#[default]{hints}"

    def sandbox_status(self):
        card = self.running_card()
        if card and PROPOSAL_ID.fullmatch(card.id):
            return f" {palette.tmux('patina', bold=True)}⏵ sandbox {card.id} {card.elapsed()}s#[default] │"
        if card:
            return f" {palette.tmux('patina', bold=True)}⏵ sandbox {card.elapsed()}s#[default] │"
        if self.hello.get("sandbox"):
            return f" {palette.tmux('patina')}⊡ sandbox#[default] │"
        if self.runner and not self.runner.get("problem"):
            return f" {palette.tmux('patina')}⊡ model's runs sandboxed#[default] │"
        return f" {palette.tmux('brass')}▲ no sandbox#[default] │"

    def network_text(self):
        r = self.runner
        if not r:
            return "no sandbox: the profile has no 'sandbox'"
        if r.get("problem"):
            return f"the profile's sandbox can't run here: {clean(r['problem'])}"
        return (f"sandbox network is {'ON: each sandboxed run asks first' if r.get('network') else 'off'}"
                + ("" if self.coding else " (the sandbox is used in coding mode)") + " · /network on|off")

    def retire(self, why):
        """The turn is over (it was stopped, or hoplon has gone): nothing still waits for an answer, and nothing
        still runs, so no card asks or keeps its clock going, and Ctrl-C quits again."""
        for card in self.pending:
            card.state, card.detail = "stopped", why
            card.changed()
        self.pending = []
        for card in self.cards.values():
            if card.ev.get("kind") in RUNS and card.state == "running":
                card.state, card.detail = "stopped", card.detail or "stopped"
                card.changed()

    def exit(self, code, err=""):
        self.exited, self.busy = code, False
        for card in self.cards.values():
            if card.ev.get("kind") in RUNS and card.state == "running":
                card.state, card.detail = "error", "hoplon exited before it finished"
                card.changed()
        self.retire("hoplon exited")
        for f in self.features:
            f.exited(self)
        for ln in err.strip().splitlines()[-5:]:
            self.note(clean(ln), R)
        self.note(f"hoplon exited (code {code}). /quit or Ctrl-D to leave.", R)

    # -- from the user

    def submit(self, text):
        if not text.strip():
            return
        if text.startswith("/") and not text.startswith("//"):
            self.command(text.strip())
            return
        if text.startswith("//"):
            text = text[1:]
        attachments, self.next_attachments = self.next_attachments, []
        self.say(text, attachments=attachments)

    def say(self, text, attachments=()):
        if self.exited is not None:
            self.note("hoplon is not running", R)
        elif self.busy:
            self.queued.append(self.add(UserMsg(text, queued=True, attachments=attachments)))
        else:
            self.turn += 1
            self.send_turn(self.add(UserMsg(text, attachments=attachments)))

    def share(self, label, text, message):
        """Send reviewed output: as an attachment, so hoplon masks secrets in it, then a message about it."""
        self.say(message, attachments=[{"type": "attach", "label": label, "text": text}])

    def next_queued(self):
        if self.queued and not self.busy:
            msg = self.queued.pop(0)
            self.turn += 1
            msg.queued, msg.turn = False, self.turn
            msg.changed()
            self.blocks.remove(msg)
            self.add(msg)
            self.send_turn(msg)

    def send_turn(self, msg):
        self.busy = True
        for attachment in msg.attachments:
            self.send({k: v for k, v in attachment.items() if k != "decline"})
        self.send({"type": "user", "text": msg.text})

    def command(self, line):
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        feature = next((f for f in self.features if cmd in f.commands), None)
        to_hoplon = cmd in TO_HOPLON or (feature is not None and cmd in feature.to_hoplon)
        if to_hoplon and (self.busy or self.exited is not None):
            self.note(f"{cmd} has to wait until hoplon finishes this reply", W)
        elif cmd == "/help":
            for c, d in self.commands().items():
                self.note(f"{c:9} {d}")
            self.note("Enter sends · Alt+Enter or \\ then Enter adds a line · ↑↓ earlier messages · "
                      "PgUp/PgDn scroll · // starts a message with /")
            self.note("In the shell pane, hoplon-ssh [ssh options] HOST connects so commands on that host can be "
                      "followed and /run can send them there (plain ssh can't be)")
            self.note("Alt+1 … Alt+9 open the menu of a command proposal, Alt+1 the newest (as its card says); "
                      "↑↓ and Enter, or the letter shown, choose")
            for f in self.features:
                f.help(self)
        elif cmd == "/coding":
            if arg in ("on", "off"):
                self.send({"type": "coding", "on": arg == "on"})
            else:
                self.note(f"coding mode is {'on' if self.coding else 'off'} (/coding on|off)")
        elif cmd == "/network":
            if arg in ("on", "off"):
                self.send({"type": "network", "on": arg == "on"})
            elif arg:
                self.note("usage: /network, /network on or /network off", R)
            else:
                self.note(self.network_text())
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
        elif feature and feature.needs and feature.needs not in ((self.hello or {}).get("features") or []):
            self.note(f"this hoplon can't {feature.cant}", R)
        elif feature:
            feature.command(self, cmd, arg)
        elif cmd == "/compact":
            self.send({"type": "compact"})
        elif cmd in ("/run", "/share", "/shell", "/mark", "/commands") and not self.shell:
            self.note(f"{cmd} needs the shell pane: start hoplon with hoplon-tui", R)
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
        elif cmd == "/stop":
            self.stop()
        elif cmd == "/plan":
            if arg == "clear" and self.busy:
                self.note("/plan clear has to wait until hoplon finishes this reply", W)
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
        elif cmd == "/context":
            used, limit = self.context or (None, None)
            self.note(f"context: {used}/{limit} tokens" if used is not None else "context: not measured yet")
        elif cmd == "/clear":
            if "clear" in ((self.hello or {}).get("features") or []):
                self.send({"type": "clear"})
            else:
                self.blocks = []
                self.note("this hoplon can't clear its context; only the screen was cleared", W)
        elif cmd in ("/quit", "/exit"):
            self.quit = True
        else:
            self.note(f"unknown command {cmd} (/help lists them)", R)

    def snapshot(self):
        """What the info panel shows. Proposal titles and details are escaped here and again where drawn."""
        h = self.hello or {}
        ep, sb, prof = h.get("endpoint") or {}, h.get("sandbox"), h.get("profile") or {}
        run = self.running_card()
        view = {
            "session": {"endpoint": ep.get("name"), "model": ep.get("model"), "profile": prof.get("name"),
                        "session": h.get("session"), "sandbox": sb.get("name") if sb else None,
                        "shell_where": self.shell_where, "hoplon_where": hoplon_where(h)},
            "coding": self.coding, "busy": self.busy, "context": list(self.context or (None, None)),
            "runner": self.runner and {"network": bool(self.runner.get("network")), "problem": self.runner.get("problem"),
                                       "image": self.runner.get("image")},
            "running": run and {"id": clean(run.id), "secs": run.elapsed(), "stoppable": run.stoppable},
            "skills": self.skills,
            "proposals": [{"id": c.id, "title": c.title(), "state": c.state, "detail": clean(c.detail)}
                          for c in self.cards.values() if c.state != "auto"],
            "commands": self.recent,
            "plan": self.plan and {"goal": clean(self.plan.get("goal", "")), "auto": self.plan_auto,
                                   "steps": [{"n": st.get("n"), "text": step_text(st), "status": st.get("status")}
                                             for st in self.plan.get("steps") or []]},
        }
        for f in self.features:
            f.snapshot(self, view)
        return view

    def request(self, msg, role=None):
        """A request from the info panel or a menu, checked and turned into the command a user would type. role is
        the pane it came from: the Hub has already refused kinds that role may not send."""
        t = msg.get("type") if isinstance(msg, dict) else None
        if t == "coding" and isinstance(msg.get("on"), bool):
            self.command(f"/coding {'on' if msg['on'] else 'off'}")
        elif t == "skill" and isinstance(msg.get("name"), str) and isinstance(msg.get("on"), bool) \
                and msg["name"].strip() and "\n" not in msg["name"]:
            self.command(f"/skill {msg['name']} {'on' if msg['on'] else 'off'}")
        elif t == "skills" and not self.busy and self.exited is None:
            self.send({"type": "skills"})
        elif t == "share" and msg.get("what") in ("mark", "all", "last") and self.shell:
            self.actions.append(("share", msg["what"]))
        elif t == "command" and type(msg.get("n")) is int and self.shell:
            self.actions.append(("share_cmd", msg["n"]))
        elif t == "mark" and isinstance(msg.get("on"), bool) and self.shell:
            self.actions.append(("mark_line",) if msg["on"] else ("unmark",))
        else:
            for f in self.features:
                if t in f.requests and (role is None or role in f.requests[t]):
                    f.request(self, role, msg)

    def attach(self, label, text):
        """Shell context the user reviewed: it goes with their next message, so they can ask about it."""
        self.next_attachments.append({"type": "attach", "label": label, "text": text})
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
        msg = answer(card, "decision", decision="accept")
        if approve is not None:
            msg["approve"] = sorted(keep)
        if edits:
            msg["edits"] = {str(n): c for n, c in edits.items()}
        self.send(msg)

    def decide(self, accept, not_secret=False):
        """not_secret: you allowed a secret-looking read and said it holds none, so hoplon won't ask about it again
        until the file changes."""
        if not self.pending:
            return
        card = self.pending.pop(0)
        if accept and card.ev.get("kind") == "sandbox":
            card.start(self.can_stop())            # hoplon runs it as soon as it has the answer
        else:
            card.state = "accepted" if accept else "declined"
        card.changed()
        extra = {"not_secret": True} if accept and not_secret and card.ev.get("secret") else {}
        self.send(answer(card, "decision", decision="accept" if accept else "decline", **extra))


def answer(card, kind, **fields):
    """A decision or result for hoplon, echoing the digest of the proposal the card shows (hoplon refuses an answer
    whose digest is missing or different)."""
    msg = {"type": kind, "id": card.id, **fields}
    if card.ev.get("digest"):
        msg["digest"] = card.ev["digest"]
    return msg


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


def command_list(features):
    """The core's commands, then each feature's, with their help."""
    out = dict(COMMANDS)
    for f in features:
        out.update(f.commands)
    return out


def configured_features():
    """The features that are on, as the config file and HOPLON_TUI_DISABLE say, and a warning if the file was no good."""
    disabled, warning = disabled_names()
    return load_features(disabled, CORE), warning


def feature_requests(features):
    """{role: kinds} of the requests the features take, for the Hub: what is not here, it refuses."""
    out = {}
    for f in features:
        for kind, roles in f.requests.items():
            for role in roles:
                out.setdefault(role, set()).add(kind)
    return out


def complete(text, commands=None):
    """Tab on a partial /command: the completion, or text unchanged when there is none or several. commands are
    those there are (the core's, and those of the features that are on) unless given."""
    if not text.startswith("/") or " " in text:
        return text
    if commands is None:
        commands = command_list(configured_features()[0])
    hits = [c for c in commands if c.startswith(text)]
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
# For text that may hold '#' lines of its own (output, a file): only the comment lines on top are ours.
EDITOR_HEADER_TOP = EDITOR_HEADER.replace("# Lines starting with '#' are removed.", "# The '#' lines up here are removed.")


def pager_argv(path):
    """How to show a text file hoplon-tui wrote: its own pager (tui/pager.py: Esc or q closes it), or the program in
    HOPLON_TUI_PAGER (for example less) if you set one."""
    mine = os.environ.get("HOPLON_TUI_PAGER", "").strip()
    if mine:
        return shlex.split(mine) + [path]
    return [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "pager.py"), path]


def editor_argv(path):
    """The user's editor, with swap, backup and history files off for vim, as hoplon does: the text may hold secrets."""
    argv = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    base = os.path.basename(argv[0])
    if base in ("vim", "nvim", "gvim"):
        argv += ["-n", "-i", "NONE", "--cmd", "set nobackup nowritebackup noundofile"]
    elif base == "vi":
        argv += ["-n"]
    return argv + [path]


def strip_comments(text, top_only=False):
    """Text without its '#' lines: all of them, or with top_only just the ones it starts with (the header)."""
    lines = text.split("\n")
    if top_only:
        while lines and lines[0].startswith("#"):
            lines.pop(0)
        return "\n".join(lines).strip("\n")
    return "\n".join(ln for ln in lines if not ln.startswith("#")).strip("\n")


def remote_where(host):
    return f"ON ANOTHER HOST, {host}, over ssh, OUTSIDE any sandbox"


def review_buffer(command, rc, where, output):
    """What /run and /share open for review: the same layout as hoplon's own :run."""
    return ("# EVERYTHING below is sent to the model as saved, the command included.\n"
            "# Remove anything you don't want it to see, such as a secret in the command.\n"
            f"I ran this command {where} (exit {rc}):\n```\n{command}\n```\nOutput:\n```\n{output}\n```\n")


class Core:
    """The helpers of this module, for the features (tui/features): they can't import it, as it may be the program
    itself. Names are looked up when used, so they see the module as it is."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None


CORE = Core()


# --------------------------------------------------------------------------
# The shell pane, through tmux

class Busy(Exception):
    pass


def menu_text_ok(name, key):
    """A menu item's name and key come from fixed text in a feature, but they are tmux formats too: no '#', which
    starts one, and nothing unprintable."""
    return (isinstance(name, str) and isinstance(key, str) and "#" not in name and name.isprintable()
            and len(name) <= 80 and len(key) <= 1 and key.isprintable())


def menu_item(*args):
    """The tmux command for a menu item: it runs request.py, which sends the choice over the socket. A menu
    command is a tmux format, so it is quoted for tmux, and carries only fixed words and checked ids."""
    cmd = shlex.join([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "request.py"), *args])
    return 'run-shell -b "' + cmd.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("#", "##") + '"'


class Shell:
    """The shell pane hoplon-tui opened beside the conversation. What each command printed comes from the
    recorder (tui/recorder.py); this reads the screen and its history only for shell context, by logical line:
    a line wrapped onto several rows counts once, so resizing the pane doesn't move a mark."""

    def __init__(self, pane):
        self.pane = pane
        self.primary_prompt = None
        self.remote_prompt = None      # (host, directory) while a hoplon-ssh shell is at its prompt, from the recorder

    @classmethod
    def from_env(cls):
        pane = os.environ.get("HOPLON_TUI_SHELL")
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

    def execution_where(self, pid=None):
        """Describe the shell pane itself, independently of the hoplon child."""
        if pid is None:
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
        remote = self.remote_prompt() if self.remote_prompt else None
        if remote:
            # A hoplon-ssh session's shell, at its prompt: its markers carry the key only it was given (recorder.py),
            # so that is the proof, whatever runs ssh here (ssh itself, or a wrapper).
            if paste == "0":
                raise Busy("the shell on the other host has bracketed input turned off")
            return context[0], f"{remote[0]}:{remote[1]}"
        if command == "ssh":
            raise Busy("ssh is running, and hoplon-tui can't follow it; to send commands to another host, connect "
                       "with hoplon-ssh [ssh options] HOST instead")
        if command != "bash":
            raise Busy(f"{command or 'something'} is running")
        # tmux before 3.4 can't report bracketed paste (an empty format). Our shellrc turns it on, and the recorder's
        # prompt marker proves that shellrc is what is at the prompt; a 0 from a newer tmux means it was turned off.
        if paste == "0" or self.primary_prompt is None or not self.primary_prompt():
            raise Busy("Bash is not at its primary prompt with bracketed input enabled")
        return context

    def type(self, command, enter=True, expected=None):
        """Type only into a Bash pane accepting bracketed input."""
        if unsafe_chars(command):
            raise Busy("the command contains control or invisible characters (shown as escapes); "
                       "copy it and check it yourself")
        context = self.ready()
        if expected is not None and context != expected:
            raise Busy("the shell or working directory changed since review")
        self.tmux("load-buffer", "-b", "hoplon-run", "-", data=command, check=True)
        if self.ready() != context:
            raise Busy("the shell changed before paste")
        self.tmux("send-keys", "-t", self.pane, "C-e", "C-u", check=True)
        self.tmux("paste-buffer", "-p", "-d", "-b", "hoplon-run", "-t", self.pane, check=True)
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
        toggle = os.environ.get("HOPLON_TUI_TOGGLE")
        if toggle:
            subprocess.run([toggle, "show"] + (["--focus"] if focus else []), capture_output=True, timeout=5)
        elif focus:
            self.tmux("select-pane", "-t", self.pane)

    def focus(self):
        self.show(focus=True)

    def copy(self, text):
        """To tmux's buffer and, through the terminal (OSC 52), the system clipboard where the terminal allows it."""
        self.tmux("set-buffer", "-w", "-b", "hoplon-copy", "--", text)

    def popup_menu(self, *args):
        """tmux's display-menu in this pane, without waiting for it to close. Titles, names and commands are tmux
        formats: callers pass only fixed words and checked numbers and ids."""
        self.menus = [p for p in getattr(self, "menus", []) if p.poll() is None]
        self.menus.append(subprocess.Popen(["tmux", "display-menu", "-t", os.environ.get("TMUX_PANE", ""), *args],
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL))

    def menu(self, pid, where, at="M", extra=()):
        """tmux's own menu at the mouse, for a command card. Menu titles, names and commands are tmux formats, so
        none of them carries text from the model: only the proposal id, checked here, and fixed words. extra is
        what features add: (name, key, action_word) each."""
        if not re.fullmatch(r"p[0-9]{1,9}", pid):
            return

        def pick(what):
            return menu_item("menu", what, pid)
        items = []
        for name, key, what in extra:
            if menu_text_ok(name, key) and re.fullmatch(r"[a-z]{1,12}", what):
                items += [name, key, pick(what)]
        self.popup_menu("-x", at, "-y", at, "-T", f" {pid}: runs {where} ",
                        "Run it in the shell pane", "r", pick("run"),
                        "Edit it, then run", "e", pick("edit"),
                        "Put it at the shell prompt (you press Enter)", "p", pick("prompt"),
                        "Copy it", "c", pick("copy"),
                        *items,
                        "", "Cancel", "q", "")

    def turn_menu(self, turn, extra):
        """Right-click in the conversation: tmux's menu for what the features offer on a turn (extra: a list of
        (name, key, request_args...) from each of them). Only fixed words and numbers."""
        groups = []
        for group in extra:
            items = []
            for name, key, *args in group:
                if menu_text_ok(name, key) and args and all(re.fullmatch(r"[a-z]{1,12}|[0-9]{1,9}", a) for a in args):
                    items += [name, key, menu_item(*args)]
            if items:
                groups.append(items)
        items = [x for i, g in enumerate(groups) for x in (g if i == 0 else ["", *g])]
        self.popup_menu("-x", "M", "-y", "M", "-T", f" turn {turn} " if turn else " raw view ",
                        *items, *([""] if items else []), "Cancel", "q", "")

    def title(self, text):
        self.tmux("set", "-p", "-t", os.environ.get("TMUX_PANE", ""), "@hoplon_title", text)


# --------------------------------------------------------------------------
# hoplon as a child process

class Child:
    def __init__(self, args, remote_info=None):
        """hoplon --json ARGS, here, or (remote_info is remote.json) on the remote host over the ssh master. Remote,
        what hoplon reads first is its own source, then the config line, then the protocol (see remote.py); a
        thread writes it all so a slow or dead ssh can't freeze the screen."""
        self.writes = None
        if remote_info:
            with open(HOPLON, "rb") as f:
                source = f.read()
            cmd = remote_info["ssh"] + [remote.bootstrap_command(remote_info, len(source), args)]
            args = []
            self.writes = queue.Queue()
            self.writes.put(source)
            self.writes.put(json.dumps(remote_info["config"]).encode() + b"\n")
        else:
            cmd = ([HOPLON] if os.environ.get("HOPLON_BIN") else [sys.executable, HOPLON]) + ["--json"]
        self.p = subprocess.Popen(cmd + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.events = queue.Queue()
        self.err = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        if self.writes:
            self.writer = threading.Thread(target=self._write, daemon=True)
            self.writer.start()

    def _write(self):
        out = self.p.stdin.buffer
        while True:
            data = self.writes.get()
            if data is None:
                break
            try:
                out.write(data)
                out.flush()
            except (OSError, ValueError):
                break
        try:
            self.p.stdin.close()
        except (OSError, ValueError):
            pass

    def _read(self):
        for line in self.p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                ev = {"type": "error", "message": f"not JSON from hoplon: {line.strip()[:200]}"}
            self.events.put(ev)
        self.events.put({"type": "_exit", "code": self.p.wait(), "err": "\n".join(self.err)})

    def _read_err(self):
        for line in self.p.stderr:
            self.err.append(line.rstrip("\n"))

    def send(self, msg):
        if self.writes:
            self.writes.put((json.dumps(msg) + "\n").encode())
            return
        try:
            self.p.stdin.write(json.dumps(msg) + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def interrupt(self):
        """Ctrl-C for hoplon: a SIGINT, which stops a sandboxed run (hoplon ignores it otherwise). Only when hoplon is
        our own child here; over ssh there is no way to send it one yet."""
        if self.writes or self.p.poll() is not None:
            return False
        try:
            os.kill(self.p.pid, signal.SIGINT)
        except OSError:
            return False
        return True

    def close(self):
        try:
            if self.writes:
                self.writes.put(None)
                self.writer.join(5)
            else:
                self.p.stdin.close()
            self.p.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            try:            # still busy: SIGTERM lets hoplon end a work session, committing it, before it exits
                self.p.terminate()
                self.p.wait(30)
            except (OSError, subprocess.TimeoutExpired):
                self.p.kill()


# --------------------------------------------------------------------------
# The screen

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class App:
    def __init__(self, scr, child, shell=None):
        self.scr, self.child, self.shell = scr, child, shell
        on, warning = configured_features()
        self.m, self.ed = Model(on), Editor()
        if warning:
            self.m.note(warning, W)
        self.m.shell = shell is not None
        self.m.shell_where = shell.execution_where() if shell else None
        self.plan_review = None
        self.running = None        # a /run command in the shell pane: card, command, commands done before it
        self.rows = []             # the block drawn on each screen row of the conversation
        self.state_dir = os.environ.get("HOPLON_TUI_STATE") if shell else None
        self.panel = os.environ.get("HOPLON_TUI_PANEL") if shell else None
        self.files = os.environ.get("HOPLON_TUI_FILES") if shell else None
        self.prompt = os.environ.get("HOPLON_TUI_PROMPT") if shell else None   # input happens in that pane
        # the other panes connect here; a feature that is off has no requests, so the Hub refuses them
        self.hub = Hub(self.state_dir, feature_requests(on)) if self.state_dir else None
        if self.hub:
            self.hub.plugins = any(f.name == "plugins" for f in on)
        fifo = fifo_path(self.state_dir) if self.state_dir else None
        self.rec = Recorder(fifo if fifo and os.path.exists(fifo) else None)   # each shell command and its output
        if shell:
            shell.primary_prompt = self.rec.at_primary_prompt
            shell.remote_prompt = self.rec.remote_prompt
        self.ctx_line = None       # where shell context starts, in logical lines (see Shell.lines)
        self.sent = {}             # the last view sent to each role
        self.ask_obj, self.ask_serial = None, 0
        self.mouse = 0
        for name in ("BUTTON1_RELEASED", "BUTTON1_CLICKED", "BUTTON3_RELEASED", "BUTTON3_CLICKED", "BUTTON4_PRESSED",
                     "BUTTON5_PRESSED"):
            self.mouse |= getattr(curses, name, 0)
        curses.mousemask(self.mouse)
        curses.mouseinterval(0)
        self.scroll = 0            # lines up from the bottom; 0 follows new output
        self.cache = {}
        self.last_total = 0
        self.ctrl_c = 0.0
        self.status = None         # the sandbox part of tmux's status line, as last set
        self.shell_title = None    # the shell pane's title, as last set
        self.paste = None          # text of a bracketed paste in progress
        curses.curs_set(1)
        self.roles = palette.roles(curses)
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
                here = ask.get("context") or ask.get("peek")
                cwd = clean(here[1]) if here else "the shell pane's (it is busy)"
                verb = "Paste" if ask.get("paste_only") else "Run"
                lines = [[(f"{verb} {c.id}{' (edited)' if ask['edited'] else ''} in the shell pane? ", A),
                          (f"It runs {where}, in your shell, as you.", R if "OUTSIDE" in where else "")],
                         [(f"in {cwd}", Q)]]
                shown = command_text(ask["command"]).split("\n")
                lines += [[("    $ " if i == 0 else "      ", Q), (fit(ln, w - 6), "")]
                          for i, ln in enumerate(shown[:CMD_LINES])]
                if ask.get("context") or self.fits(ask["command"]):
                    lines.append([("  y", A), (f" {verb.lower()}   ", ""), ("v", A), (" view in full   ", ""),
                                  ("e", A), (" edit   ", ""), ("n", A), (" cancel", "")])
                    return lines, None, f"y {verb.lower()} · v view in full · e edit · n or Esc cancel"
                lines.append([("  It doesn't all fit here: ", W), ("y", A), (" or ", ""), ("v", A),
                              (" view it in full first   ", ""), ("e", A), (" edit   ", ""), ("n", A), (" cancel", "")])
                return lines, None, "y or v view the whole command first · e edit · n or Esc cancel"
            if ask["kind"] in ("fixtures_use", "fixtures_clear", "leftovers_clean"):
                yes = {"fixtures_use": "use them", "fixtures_clear": "delete it"}.get(ask["kind"], "remove them")
                return ([[(ask["question"], A)], [("  y", A), (f" {yes}   ", ""), ("n", A), (" no", "")]], None,
                        f"y {yes} · n or Esc no")
            lines = [[(ask["question"], A)],
                     [("  y", A), (" review it in your editor, then send   ", ""), ("s", A), (" send it as it is   ", ""),
                      ("n", A), (" keep it to yourself", "")]]
            return lines, None, "y review, then send · s send as it is · n or Esc don't share"
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
            if cmds and "OUTSIDE" in self.where():
                lines.append([(f"  Commands run {self.where()}, in your shell, as you.", R)])
            return lines, None, "y approve · a review automatic runs · e edit · n or Esc no · v view"
        if self.m.pending:
            c = self.m.pending[0]
            more = f"  ({len(self.m.pending)} waiting)" if len(self.m.pending) > 1 else ""
            lines = [[(f"Allow {c.id}: {c.title()}?", W), (more, Q)]]
            if c.ev.get("kind") == "trusted":
                lines.append([("  $ ", A), (fit(command_text(str(c.ev.get("argv", ""))).replace("\n", " "), w - 6), "")])
            if c.ev.get("kind") == "sandbox":
                lines.append([("  $ ", A), (fit(command_text(c.ev.get("command", "")).replace("\n", "⏎"), w - 6), "")])
                lines.append([("  The network is on: on a throwaway copy, but it could send what it reads anywhere.", R)])
            if c.ev.get("kind") == "work_plan":
                lines.append([("  Once approved, each item is coded, tested and committed on the session's branch "
                               "without asking.", Q)])
            if c.ev.get("secret"):
                ep = (self.m.hello or {}).get("endpoint") or {}
                to = c.ev.get("to") if isinstance(c.ev.get("to"), str) else ep.get("base_url", "the endpoint")
                lines.append([(f"It looks like it holds secrets; allowing sends it to {clean(str(to))}", R)])
                lines.append([("  y", A), (" yes   ", ""), ("a", A), (" not secrets   ", ""), ("n", A), (" no   ", ""),
                              ("v", A), (" view it all", "")])
                return lines, None, "y allow · a not secrets (until it changes) · n no · v view"
            elif isinstance(c.ev.get("to"), str):
                lines.append([(f"  Allowing sends it to the planner, at {one_row(c.ev['to'])}", W)])
            lines.append([("  y", A), (" yes   ", ""), ("n", A), (" no   ", ""), ("v", A), (" view it all", "")])
            return lines, None, "y allow · n or Esc decline · v view in full"
        rows, cur = self.ed.view(w)
        if len(rows) > INPUT_MAX_LINES:
            top = min(max(cur[0] - INPUT_MAX_LINES + 1, 0), len(rows) - INPUT_MAX_LINES)
            rows, cur = rows[top:top + INPUT_MAX_LINES], (cur[0] - top, cur[1])
        lines = [[(r[:2], A), (r[2:], "")] for r in rows]
        if self.m.exited is not None:
            hints = "hoplon has exited · /quit or Ctrl-D to leave"
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
        if m.pending and k == "\x03" and m.busy and m.can_halt():
            m.stop()                                   # the turn stops; what waits for you is declined
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
            elif k in ("a", "A") and m.pending[0].ev.get("secret"):
                m.decide(True, not_secret=True)
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
            elif m.running_card():
                m.stop()
                now = 0.0                              # stopping a run doesn't count towards quitting
            elif m.busy and m.can_halt() and now - self.ctrl_c >= 2:
                m.stop()                               # stopping the turn: a second Ctrl-C soon after still quits
            elif now - self.ctrl_c < 2:
                m.quit = True
            else:
                m.note("Ctrl-C again (or Ctrl-D) to quit" + (". hoplon's reply can't be interrupted yet." if m.busy else ""))
            self.ctrl_c = now
        elif k == "\t":
            ed.text = complete(ed.text, self.m.commands())
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
        """What the prompt pane shows: the input box, or our question or hoplon's approval, with hints."""
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
                "busy": self.m.busy, "running": self.m.running_card() is not None,
                "halt": self.m.busy and self.m.can_halt() and self.m.exited is None,
                "status": " · ".join(status), "exited": self.m.exited is not None,
                "q": self.question()}

    def question(self):
        """An id for the question showing in the prompt pane. An answer must name it, so an answer meant for a
        question that has since gone (or not yet come) is ignored, however late or early it arrives."""
        if self.m.pending:
            pid = self.m.pending[0].id
            reviewed = self.plan_review and self.plan_review[0] == pid
            return f"approve:{pid}:reviewed" if reviewed else f"approve:{pid}"
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
            self.sent.pop("prompt", None)
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
        elif t == "stop":
            self.m.stop()
        else:
            return False
        return True

    def toggle_panel(self):
        self.toggle_pane(self.panel, "panel", ["-l", "42"], "info panel")

    def toggle_pane(self, pane, name, join, what):
        """Show a side pane beside this one, or break it out to a window of its own again."""
        if not pane:
            self.m.note(f"there is no {what} in this session", R)
            return
        me = os.environ.get("TMUX_PANE", "")
        here = self.shell.tmux("display", "-p", "-t", me, "#{window_id}").strip()
        there = self.shell.tmux("display", "-p", "-t", pane, "#{window_id}").strip()
        if here == there:
            self.shell.tmux("break-pane", "-d", "-s", pane, "-n", name)
        else:
            self.shell.tmux("join-pane", "-d", "-h", "-f", *join, "-s", pane, "-t", me)

    def click(self):
        """The wheel scrolls; a click on a command card opens its menu; a right-click anywhere opens the raw view's.
        A menu opens when the button is released: tmux closes a menu that a program opened at the next mouse event
        other than a press, so one opened on the press would close again on the release."""
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return
        if b & getattr(curses, "BUTTON4_PRESSED", 0):
            self.scroll += 3
        elif b & getattr(curses, "BUTTON5_PRESSED", 0x200000):
            self.scroll = max(self.scroll - 3, 0)
        elif not b & (curses.BUTTON1_RELEASED | curses.BUTTON1_CLICKED | curses.BUTTON3_RELEASED | curses.BUTTON3_CLICKED):
            return                                  # a press, or a move: only the release opens the menu
        elif b & (curses.BUTTON3_RELEASED | curses.BUTTON3_CLICKED) and self.shell:
            block = self.rows[y] if 0 <= y < len(self.rows) else None
            turn = getattr(block, "turn", 0)
            here = [f.turn_menu(self.m, turn) for f in self.m.features]
            # the menu is about the turn if a feature has something to offer for it in particular
            specific = any(len(h) > len(f.turn_menu(self.m, 0)) for f, h in zip(self.m.features, here))
            self.shell.turn_menu(turn if specific else 0, here)
        elif 0 <= y < len(self.rows) and not self.m.pending and not self.m.ask:
            card = self.rows[y]
            if isinstance(card, Card) and card.ev.get("kind") == "command":
                if not self.shell:
                    self.m.note(f"{card.id}: start hoplon with hoplon-tui to send commands to a shell pane", R)
                    return
                self.shell.menu(card.id, self.where(), extra=self.card_items(card))

    def open_menu(self, n):
        """Alt+N, through tmux: the menu of the command proposal its card shows Alt+N on, in the middle of the
        screen, to be used with the keys."""
        if type(n) is not int or not 1 <= n <= MENU_KEYS or self.m.pending or self.m.ask:
            return
        card = self.m.keyed(n)
        if not card:
            self.m.note(f"no command proposal has Alt+{n}", Q)
        elif not self.shell:
            self.m.note(f"{card.id}: start hoplon with hoplon-tui to send commands to a shell pane", R)
        else:
            self.shell.menu(card.id, self.where(), at="C", extra=self.card_items(card))

    def card_items(self, card):
        """What the features add to a command card's menu."""
        return [item for f in self.m.features for item in f.card_menu(self.m, card)]

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
            self.m.ask = {"kind": "run", "card": card, "command": command, "edited": False, "paste_only": True}
        elif what == "copy":
            self.shell.copy(command)
            self.m.note(f"{card.id} copied (tmux buffer, and your clipboard if the terminal allows it)")
        else:
            for f in self.m.features:
                if what in f.card_actions:
                    f.card_action(self, what, card)

    def where(self):
        """Where a command typed into the shell pane now runs: here, or on the host of a hoplon-ssh session."""
        host = self.rec.remote_host() if self.shell else None
        return remote_where(host) if host else self.m.shell_where or "OUTSIDE any sandbox, directly on this host"

    def ran_where(self, c):
        """Where a command the recorder saw ran."""
        return remote_where(c.host) if c.host else self.m.shell_where or "OUTSIDE any sandbox, directly on this host"

    def fits(self, command):
        """Is all of the command visible in the question? Then reading it there is the review."""
        rows = command_text(command).split("\n")
        if len(rows) > CMD_LINES:
            return False
        w = self.scr.getmaxyx()[1]
        if self.prompt:
            try:
                w = int(self.shell.tmux("display", "-p", "-t", self.prompt, "#{pane_width}").strip() or w)
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
        return all(width(r) <= w - 6 for r in rows)

    def peek(self, ask):
        """The shell's directory, to show with a run question; None while the shell is busy."""
        try:
            return self.shell.ready()
        except (Busy, OSError, subprocess.SubprocessError):
            return None

    def review_context(self):
        try:
            context = self.shell.ready()
            self.m.shell_where = self.shell.execution_where(context[0])
            return context
        except (Busy, OSError, subprocess.SubprocessError) as e:
            self.m.note(f"the shell pane is busy ({e}), so nothing was typed there. /shell shows it; when it is back "
                        f"at its prompt, try again.", R)
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
            if k == "y" and not ask.get("context") and self.fits(ask["command"]):
                context = self.review_context()             # all of it is on screen: that was the review
                if context:
                    m.ask = None
                    if ask.get("paste_only"):
                        self.paste_run(card, ask["command"], context)
                    else:
                        self.start_run(card, ask["command"], context=context)
            elif k == "v" or (k == "y" and not ask.get("context")):
                self.review_run(ask)
            elif k == "y":
                m.ask = None
                if ask.get("paste_only"):
                    self.paste_run(card, ask["command"], ask["context"])
                else:
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
        elif ask["kind"] == "leftovers_clean":
            m.ask = None
            if k == "y":
                m.send({"type": "leftovers", "action": "clean", "ids": ask["ids"]})
                lo = m.feature("leftovers")
                if lo:
                    lo.then = "show"
            else:
                m.note("leftovers: nothing removed")
        elif ask["kind"] in ("fixtures_use", "fixtures_clear"):
            m.ask = None
            fx = next((f for f in m.features if f.name == "fixtures"), None)
            if k != "y":
                m.note("samples: not approved" if ask["kind"] == "fixtures_use" else "samples folder: kept")
            elif ask["kind"] == "fixtures_use":
                m.send({"type": "fixtures", "action": "use", "digest": ask["digest"]})
            else:
                m.send({"type": "fixtures", "action": "clear"})
            if fx and k == "y":
                fx.then = "show"
        else:
            m.ask = None
            if k in ("y", "s"):                             # y: review it in your editor first; s: send it as it is
                if ask.get("context"):
                    text = ("# EVERYTHING below is sent to the model as saved. Remove anything you don't want it to "
                            f"see.\nMy shell session{ask['from']}:\n```\n{ask['output']}\n```\n")
                else:
                    text = review_buffer(ask["command"], ask["rc"], ask.get("where") or self.where(), ask["output"])
                if k == "s":                                # the same text, without the editor's # lines on top
                    kept = text.split("\n", 1 if ask.get("context") else 2)[-1].strip("\n")
                    m.note(f"{ask['label']}: sent without review (hoplon still masks the secrets it recognises)", Q)
                else:
                    kept = self.edit(text, ask["label"], "sent to the model exactly as saved; nothing is added",
                                     "send nothing", top_only=True)
                if kept is None or not kept.strip():
                    m.note("nothing sent")
                elif ask.get("context"):
                    m.attach(ask["label"], kept)
                else:
                    m.share(f"output of {ask['what']} (exit {ask['rc']})", kept, f"Here is the output of {ask['what']}.")

    def paste_run(self, card, command, context):
        try:
            self.shell.type(command, enter=False, expected=context)
            self.shell.focus()
        except (Busy, OSError, subprocess.SubprocessError) as e:
            self.m.note(f"{card.id}: could not use the shell pane ({e})", R)
            return
        self.m.note(f"{card.id} is at the shell prompt; press Enter there to run it, then /share")

    def start_run(self, card, command, context):
        done = self.rec.done
        try:
            self.shell.type(command, expected=context)
        except Busy as e:
            self.m.note(f"{card.id} not run: the shell pane is busy ({e}). /shell shows it; when it is back at its "
                        f"prompt, /run {card.id} again.", R)
            return
        except (OSError, subprocess.SubprocessError) as e:
            self.m.note(f"{card.id} not run: tmux failed ({e})", R)
            return
        self.shell.show()                                # so the command can be watched as it runs
        self.m.run_started(card)
        card.state, card.detail = "running", "typed into the shell pane"
        self.m.number_commands()
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
                      "command": r["command"], "rc": c.rc, "output": self.output_of(c), "where": self.ran_where(c)}

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
                      "output": self.output_of(c), "question": f"{label_}. Share the output with the model?",
                      "where": self.ran_where(c)}

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
            raw = self.shell.opt("@hoplon_ctx_mark")
            if not raw.isdigit():
                return
            self.ctx_line = len(self.shell.lines(upto=int(raw))) - 1
            self.shell.tmux("display-message", "-t", self.shell.pane, "hoplon: shell context starts at this line")
        except (OSError, subprocess.SubprocessError) as e:
            self.m.note(f"tmux failed ({e})", R)

    def edit(self, text, title, what, cancel, top_only=False):
        """Open text in the user's editor; what they save, without # lines (with top_only, only those on top, so
        a shared file or output keeps its own). None if the editor failed."""
        fd, path = tempfile.mkstemp(prefix="hoplon-tui-", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                header = EDITOR_HEADER_TOP if top_only else EDITOR_HEADER
                f.write(header.format(title=" ".join(title.split()), what=what, cancel=cancel) + text)
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
                return strip_comments(f.read(), top_only)
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
        """hoplon marked a command as one the user approved in the plan, and our own record agrees."""
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

    def page(self, k):
        h, _ = self.scr.getmaxyx()
        step = max(h - 6, 1)
        self.scroll = self.scroll + step if k == curses.KEY_PPAGE else max(self.scroll - step, 0)

    def view(self, card):
        """Show a proposal in full in a pager: cleaned text, so nothing in it reaches the terminal raw."""
        return self.view_text(f"{card.id}: {card.title()}\n\n{card.full_text()}\n")

    def view_text(self, text):
        fd, path = tempfile.mkstemp(prefix="hoplon-preview-", suffix=".txt", dir=self.state_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        curses.endwin()
        try:
            return self.run_here(pager_argv(path)) == 0
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

    def bell(self):
        """Ring the terminal bell (the alerts feature decides when)."""
        try:
            curses.beep()
        except curses.error:
            pass

    def pump(self):
        self.poll_run()
        for f in self.m.features:
            f.poll(self)
        while self.hub:
            try:
                role, msg = self.hub.inbox.get_nowait()
            except queue.Empty:
                break
            if role == "hello":
                self.sent.pop(msg, None)                  # a pane (re)connected: send it everything
            elif role == "prompt":
                self.from_prompt(msg)
            elif role in ("panel", "files", "pipe"):        # the Hub let through only what the role may send
                self.m.request(msg, role)
            elif role.startswith("plugin:") and self.m.feature("plugins"):
                self.m.feature("plugins").request(self.m, role, msg)
            elif role == "menu" and (msg.get("type") in ("share", "mark") or self.m.takes(role, msg.get("type"))):
                self.m.request(msg, role)
            elif role == "menu" and msg.get("type") == "menu" and msg.get("action") == "open":
                self.open_menu(msg.get("key"))
            elif role == "menu" and msg.get("type") == "menu" and isinstance(msg.get("id"), str):
                if msg.get("action") in ("run", "edit", "prompt", "copy") or any(
                        msg.get("action") in f.card_actions for f in self.m.features):
                    self.menu_action(msg["action"], msg["id"])
        for action in self.m.actions:
            if action == ("share", "last"):
                self.share_command(self.rec.last(), "last")
            elif action[0] == "share_cmd":
                self.share_command(self.rec.get(action[1]), action[1])
            elif action[0] == "commands":
                self.list_commands()
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
            elif action[0] == "interrupt":
                if not self.child.interrupt():
                    self.m.note("hoplon couldn't be sent Ctrl-C", R)
            else:
                for f in self.m.features:
                    if action[0] in f.actions:
                        f.action(self, action)
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
                    self.shell.title(f"hoplon · {epn.get('name', '?')}: {epn.get('model', '?')}")
        for msg in self.m.outbox:
            self.child.send(msg)
        self.m.outbox = []
        for role, msg in self.m.panes:
            if self.hub:
                self.hub.send(role, msg)
        self.m.panes = []
        if self.shell:
            self.m.recent = self.recent()
            ask = self.m.ask
            if ask and ask.get("kind") == "run" and "peek" not in ask:
                ask["peek"] = self.peek(ask)
        card = self.m.running_card()
        if card and card.shown_secs != card.elapsed():
            card.changed()                             # its elapsed time, once a second
        if self.shell:
            status = self.m.tmux_status()
            if status != self.status:
                self.status = status
                self.shell.tmux("set", "-g", "@hoplon_sandbox", status)
            title = self.m.shell_title()
            if title != self.shell_title:
                self.shell_title = title
                self.shell.tmux("set", "-p", "-t", self.shell.pane, "@hoplon_title", title)
        self.publish()


def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="hoplon-tui", description="hoplon as a coding agent in tmux: the conversation, your shell below it and "
        "an info panel beside it. The model only proposes; you approve, and you run commands in your own shell.")
    ap.add_argument("-p", "--profile", help="profile (persona) name")
    ap.add_argument("-e", "--endpoint", help="endpoint name (overrides the profile's)")
    ap.add_argument("-S", "--session", help="the session to keep the conversation in, created or resumed "
                                            "(default: a new one with a made-up name, shown when you quit)")
    ap.add_argument("--no-save", action="store_true",
                    help="keep nothing of this conversation on disk: no session, nothing to resume "
                         "(\"save_sessions\": false in tui.json makes this the default)")
    ap.add_argument("-k", "--skill", action="append", default=[], help="use a blessed skill (repeatable)")
    ap.add_argument("--allow-session-endpoint-change", action="store_true",
                    help="allow sending saved context to a changed or unrecorded destination")
    ap.add_argument("--coding", action="store_true", help="let the model read and list files here without asking")
    ap.add_argument("--coding-anywhere", action="store_true",
                    help="--coding, also where it is normally refused; hoplon says what that exposes")
    ap.add_argument("--no-panel", action="store_true", help="start without the info panel (/panel shows it)")
    ap.add_argument("--files", action="store_true", help="start with the file tree shown (/files shows or hides it)")
    ap.add_argument("--remote", metavar="[USER@]HOST",
                    help="run hoplon on that host over ssh, keeping this screen and the API key here. Options for ssh "
                         "go after a lone --, such as: -- -J bastion")
    ap.add_argument("--dir", metavar="PATH", help="with --remote: the directory on that host to work in")
    ap.add_argument("--as", dest="as_user", metavar="USER",
                    help="with --remote: run hoplon and the shell there as USER, through sudo -n (no password)")
    ap.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    argv, ssh_opts = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    args = ap.parse_args(argv)
    args.ssh_opts = ssh_opts
    for name, given in (("--dir", args.dir), ("--as", args.as_user), ("ssh options after --", ssh_opts)):
        if given and not args.remote:
            ap.error(f"{name} needs --remote")
    if args.no_save and args.session:
        ap.error("--no-save and -S can't go together: -S keeps the conversation in that session")
    if args.remote:
        if args.session:
            ap.error("--remote can't use -S yet: a saved session would stay on the host hoplon runs on")
        if any(not k or re.search(r"[\x00-\x1f\x7f]", k) for k in args.skill):
            ap.error("-k needs a skill name without control characters")
        why = remote.check_destination(args.remote, args.as_user, args.dir)
        if why:
            ap.error(why)
    return args


def hoplon_args(a):
    """The options hoplon itself gets; everything else is the TUI's. Remote, the endpoint and profile travel in
    the config line instead (the launcher resolved them here, with -e and -p)."""
    out = []
    for flag, value in (("-p", a.profile), ("-e", a.endpoint), ("-S", a.session)):
        if value and not a.remote:
            out += [flag, value]
    for k in a.skill if not a.remote else ():      # remote, the skills travel in the config line too
        out += ["-k", k]
    if a.allow_session_endpoint_change and not a.remote:
        out.append("--allow-session-endpoint-change")
    if a.coding_anywhere:
        out.append("--coding-anywhere")
    elif a.coding:
        out.append("--coding")
    return out


def sessions_dir(environ=None):
    """Where hoplon keeps saved sessions (its sessions/NAME.json)."""
    envs = os.environ if environ is None else environ
    base = envs.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "hoplon", "sessions")


def fresh_session(where, pick=None):
    """A session name nobody has used here yet: tui- and six letters or digits."""
    import secrets
    pick = pick or (lambda: "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(6)))
    while True:
        name = "tui-" + pick()
        if not os.path.exists(os.path.join(where, name + ".json")):
            return name


def main(argv):
    args = parse_args(argv)
    if args.check:
        save = not args.no_save and (args.session or tui_setting("save_sessions", True, bool))
        if not args.remote and save:  # a local conversation is kept in a session, so it can be resumed
            where = sessions_dir()
            if not args.session:
                args.session = fresh_session(where)
                print(f"new_session={args.session}")
            print(f"session_file={os.path.join(where, args.session + '.json')}")
        if args.remote:           # what the launcher needs, one per line (values have no newlines: checked)
            for k, v in (("dest", args.remote), ("as", args.as_user), ("dir", args.dir), ("profile", args.profile),
                         ("endpoint", args.endpoint)):
                print(f"{k}={v or ''}")
            for k in args.skill:
                print(f"skill={k}")
            for o in args.ssh_opts:
                print(f"ssh={o}")
        return 0
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    info = None
    if args.remote:
        try:
            info = remote.load(os.environ.get("HOPLON_TUI_REMOTE", ""))
        except (OSError, ValueError) as e:
            print(f"hoplon-tui: --remote needs the launcher (hoplon-tui) to set up the connection first: {e}",
                  file=sys.stderr)
            return 2
    child = Child(hoplon_args(args), info)
    try:
        curses.wrapper(lambda scr: App(scr, child, Shell.from_env()).run())
    finally:
        child.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
