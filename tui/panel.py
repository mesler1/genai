#!/usr/bin/env python3
"""hoplon-tui info panel: the session, context use, skills and proposals, beside the conversation.

The conversation pane pushes what to show over hoplon-tui's private socket (see ipc.py), and this
pane sends requests back the same way. It connects in the "panel" role, which may only switch
coding mode and skills, ask for the skill list, or ask to share a shell command's output (which you
then review): it can never approve anything or reach hoplon.
Standard library only; Python 3.10 or newer.
"""

import curses
import locale
import os
import queue
import sys

import palette
from conversation import A, D, Q, R, STATE_MARK, W, clean, fit, one_row, width
from ipc import Client

STATE = os.environ.get("HOPLON_TUI_STATE", "")


def bar(used, limit, w):
    if not limit:
        return [("not measured yet", Q)]
    frac = min(max(used / limit, 0), 1)
    n = max(w - 18, 4)
    full = round(frac * n)
    role = R if frac >= 0.9 else W if frac >= 0.75 else D
    return [("█" * full, role), ("░" * (n - full), Q), (f" {100 * frac:3.0f}% {used / 1000:.1f}k/{limit / 1000:.0f}k", "")]


def view(st, w, sel):
    """The panel as lines of (text, role) segments, and which skill each line toggles (or "coding")."""
    lines, acts = [], []

    def add(segs, act=None):
        lines.append(segs)
        acts.append(act)

    def head(title):
        if lines:
            add([])
        add([(title, A)])

    if not st:
        add([("waiting for the conversation…", Q)])
        return lines, acts
    h = st.get("session") or {}        # endpoint, model, profile, session and coding are in the header
    head("WHERE")
    # What is live and what is isolated: each line names one thing and marks it, ⊡ isolated or ▲ live.
    L, V = 14, w - 14
    run, going = st.get("runner"), st.get("running")
    if going:
        add([("model's runs  ", Q), ("⊡ ", D),
             (fit(f"RUNNING {clean(going.get('id', '?'))} · {going.get('secs', 0)}s", V - 2), W)])
        add([(" " * L, ""), (fit("Ctrl-C or /stop stops it" if going.get("stoppable")
                                 else "can't be stopped in remote mode yet", V), Q)])
    elif run and run.get("problem"):
        add([("model's runs  ", Q), (fit(f"can't run: {clean(run['problem'])}", V), R)])
    elif run:
        add([("model's runs  ", Q), ("⊡ sandboxed · ", D),
             (fit("ONLINE" if run.get("network") else "offline", V - 14), W if run.get("network") else D)])
        add([(" " * L, ""), (fit(clean(run.get("image") or "?"), V), Q)])
        add([(" " * L, ""), (fit("each run asks · /network" if run.get("network") else "/network switches", V), Q)])
    else:
        add([("model's runs  ", Q), (fit("none: they're only proposed", V), Q)])
    where = h.get("shell_where")
    if not where:
        add([("your commands ", Q), (fit("you run them yourself", V), Q)])
    elif where.startswith("inside"):
        add([("your commands ", Q), ("⊡ ", D), (fit(clean(where), V - 2), D)])
    else:
        text = "this host, no sandbox" if where == "OUTSIDE any sandbox, directly on this host" else clean(where)
        add([("your commands ", Q), (fit("▲ " + text, V), W)])
    gw = h.get("hoplon_where")
    if gw:                                   # remote mode: ON host AS user in dir, red as root
        add([("hoplon itself ", Q), (fit("▲ " + clean(gw[0].removeprefix("hoplon runs ")), V), R if gw[1] == R else W)])
    elif h.get("sandbox"):
        add([("hoplon itself ", Q), (fit(f"⊡ in the {clean(h['sandbox'])} sandbox", V), D)])
    else:
        add([("hoplon itself ", Q), (fit("▲ this host", V), "")])      # the usual way to run it: not a fault
    work, edits = st.get("work"), st.get("edits") or {}
    if work:
        add([("project files ", Q), ("⊡ ", D), (fit("copy " + one_row(work.get("branch", "")), V - 2), D)])
    elif edits.get("mode") == "approved-files":
        add([("project files ", Q), (fit("▲ yours, some edits don't ask", V), W)])
    else:
        add([("project files ", Q), (fit("▲ yours, edits ask", V), "")])
    fx = st.get("fixtures")
    if fx and fx.get("files"):
        n = fx["files"]
        add([("samples       ", Q), (fit(f"⊡ {n} file{'s' if n != 1 else ''} at /fixtures" if fx.get("approved")
                                         else f"{n} file{'s' if n != 1 else ''}, not approved · /fixtures use", V),
                                     D if fx.get("approved") else Q)])
    add([(fit("/sandbox shows the details", w), Q)])
    head("CONTEXT")
    ctx = st.get("context") or [None, None]
    add(bar(ctx[0] or 0, ctx[1], w) if ctx[0] is not None else [("not measured yet", Q)])
    if st.get("shared"):
        n = st["shared"]
        add([(fit(f"{n} shared, sent with your next message · /shared", w), D)])
    if st.get("busy"):
        add([("… hoplon is replying", W)])
    plan = st.get("plan")
    if plan:
        head("PLAN")
        add([(fit(clean(plan.get("goal", "")), w), "")])
        for step in (plan.get("steps") or [])[:14]:
            status = step.get("status")
            mark, role = {"done": ("✓", D), "left out": ("✗", Q)}.get(status, ("·", ""))
            add([(f"{mark} ", role), (f"{step.get('n', '?'):>2} ", Q),
                 (fit(clean(step.get("text", "")), w - 6), Q if status == "left out" else "")])
        add([("commands run on their own" if plan.get("auto") else "each planned command asks once",
              W if plan.get("auto") else Q)])
        add([("/plan clear drops it", Q)])
    work = st.get("work")
    if work:
        head("WORK")
        add([(fit(one_row(work.get("branch", "")), w), A)])
        add([(fit(one_row(work.get("status", "")), w), "")])
        for line in (work.get("items") or [])[:14]:
            add([(fit(one_row(line), w), D if str(line).startswith("✓") else Q)])
        add([("/work end stops it", Q)])
    edits = st.get("edits") or {}
    if edits.get("mode") == "approved-files":
        head("EDITS")
        add([(fit("applied without asking:", w), W)])
        for f in (edits.get("files") or [])[:10]:
            add([(fit("  " + clean(f), w), "")])
        if not edits.get("files"):
            add([("  none yet", Q)])
        elif len(edits["files"]) > 10:
            add([(f"  … {len(edits['files']) - 10} more (/edits)", Q)])
        add([(fit("/edits reset forgets them", w), Q)])
    head("SKILLS")
    skills = st.get("skills")
    if skills is None:
        add([("r loads the list", Q)])
    elif not skills:
        add([("none found", Q)])
    for i, s in enumerate(skills or []):
        mark = ">" if i == sel else " "
        blessed = s.get("status") == "blessed"
        box = "[x]" if s.get("active") else "[ ]"
        add([(mark, A), (box + " ", D if s.get("active") else ("" if blessed else Q)),
             (fit(clean(s.get("name", "")), w - 16), "" if blessed else Q),
             (" " + ("" if blessed else s.get("status", "")), R if s.get("status") == "changed" else Q)], i)
        if i == sel and not blessed:
            add([(fit(f"  bless: hoplon --bless {clean(s.get('path', ''))}", w), Q)], i)
    plugins = st.get("plugins") or []
    if plugins:
        head("PLUGINS")
        for pl in plugins[:8]:
            add([(fit(clean(pl.get("name", "?")) + (" · /plugins %s pane" % clean(pl.get("name", "?"))
                                                    if pl.get("pane") else ""), w), D)])
            add([(fit("  " + ", ".join(clean(c) for c in pl.get("commands") or []), w), Q)])
    head("COMMANDS")
    cmds = st.get("commands") or []
    if not cmds:
        add([("none run in the shell yet", Q)])
    for c in cmds:
        if c.get("running"):
            add([(f"{c.get('n', '?'):>3} ", Q), ("running…", A)])
            continue
        ok = c.get("rc") == 0
        info = f" {c.get('lines', 0)}L {c.get('ago', '')}"
        cmd = fit(clean(c.get("cmd", "")), max(w - 6 - width(info), 4))
        add([(f"{c.get('n', '?'):>3} ", Q), ("✓ " if ok else "✗ ", D if ok else R), (cmd, ""),
             (" " * max(w - 6 - width(cmd) - width(info), 0) + info, Q)], ("cmd", c.get("n")))
    if cmds:
        add([("click one to share it", Q)])
    head("PROPOSALS")
    props = st.get("proposals") or []
    if not props:
        add([("none yet", Q)])
    for p in props[-12:]:
        role = {"waiting": W, "error": R, "declined": Q, "done": D, "ran": D, "running": A}.get(p.get("state"), "")
        title = fit(clean(p.get("title", "")), max(w - 20, 4))
        status = clean(STATE_MARK.get(p.get("state"), "·") + " " + p.get("state", "")
                       + (f" · {p['detail']}" if p.get("detail") else ""))
        add([(f"{clean(p.get('id', '?')):4} ", Q), (title, ""), (" ", ""), (fit(status, max(w - 6 - width(title), 1)), role)])
    return lines, acts


class Panel:
    def __init__(self, scr, link):
        self.scr, self.link = scr, link
        self.st, self.sel, self.msg = None, 0, ""
        curses.curs_set(0)
        self.roles = palette.roles(curses)
        self.roles[A] |= curses.A_BOLD
        # The release too, though a click acts on the press: without it, ncurses holds the press back until the next
        # input arrives.
        curses.mousemask(getattr(curses, "BUTTON1_PRESSED", 0) | getattr(curses, "BUTTON1_RELEASED", 0))
        curses.mouseinterval(0)
        scr.keypad(True)
        scr.timeout(100)
        self.acts = []

    def refresh_state(self):
        while True:
            try:
                msg = self.link.inbox.get_nowait()
            except queue.Empty:
                return
            if msg is None:
                self.msg = "the conversation has ended"
            elif msg.get("type") == "view" and isinstance(msg.get("view"), dict):
                self.st = msg["view"]

    def request(self, msg):
        return self.link.send(msg)

    def draw(self):
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        lines, self.acts = view(self.st, w - 1, self.sel)
        for y, segs in enumerate(lines[:h - 1]):
            x = 0
            for text, role in segs:
                try:
                    self.scr.addstr(y, x, fit(text, max(w - 1 - x, 1)), self.roles.get(role, 0))
                except curses.error:
                    pass
                x += width(text)
        hint = self.msg or "c coding · ↑↓ space skill · r refresh"
        try:
            self.scr.addstr(h - 1, 0, fit(hint, w - 1), self.roles[Q])
        except curses.error:
            pass
        self.scr.refresh()

    def skills(self):
        return (self.st or {}).get("skills") or []

    def act(self, what):
        st = self.st or {}
        if what == "coding":
            ok = self.request({"type": "coding", "on": not st.get("coding")})
        elif isinstance(what, tuple) and what[0] == "cmd" and type(what[1]) is int:
            ok = self.request({"type": "command", "n": what[1]})
        elif isinstance(what, int) and what < len(self.skills()):
            s = self.skills()[what]
            if s.get("status") != "blessed" and not s.get("active"):
                self.msg = "bless it first (the command is shown)"
                return
            ok = self.request({"type": "skill", "name": s.get("name", ""), "on": not s.get("active")})
        else:
            return
        self.msg = "" if ok else "the conversation isn't listening"

    def key(self, k):
        n = len(self.skills())
        if k in (curses.KEY_UP, "k"):
            self.sel = max(self.sel - 1, 0)
        elif k in (curses.KEY_DOWN, "j"):
            self.sel = min(self.sel + 1, max(n - 1, 0))
        elif k in (" ", "\n", "\r", curses.KEY_ENTER):
            self.act(self.sel)
        elif k == "c":
            self.act("coding")
        elif k == "r":
            self.msg = "" if self.request({"type": "skills"}) else "the conversation isn't listening"
        elif k == curses.KEY_MOUSE:
            try:
                _, _, y, _, b = curses.getmouse()
            except curses.error:
                return
            if not b & curses.BUTTON1_PRESSED:                                   # the release: a click acts once
                return
            if 0 <= y < len(self.acts) and self.acts[y] is not None:
                if type(self.acts[y]) is int:
                    self.sel = self.acts[y]
                self.act(self.acts[y])

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
        print("panel.py is started by hoplon-tui", file=sys.stderr)
        return 2
    try:
        link = Client(STATE, "panel")
    except OSError as e:
        print(f"panel.py: can't reach the conversation pane: {e}", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: Panel(scr, link).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
