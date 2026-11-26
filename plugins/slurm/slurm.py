#!/usr/bin/env python3
"""The Slurm console: hoplon-tui's pane for the slurm plugin. Nodes by state (down and drained in red, with their
reasons), partitions, how many jobs are in each state, and your own jobs.

hoplon-tui starts it with /plugins slurm pane, in a tmux pane of its own. It runs nothing itself: it asks the
conversation pane over hoplon-tui's private socket, in the "plugin:slurm" role, to run the plugin's read-only commands
through hoplon (the same checks, cache and rate limits as the model's calls), and shows what comes back. It can also
put a job id or node name in the input box, and offer details for your next message, which you review in your editor
first. Everything it receives is cleaned before it is drawn.

Keys: ↑↓ choose a node or job · Enter (or a click) its details · i insert its name · a attach what is shown ·
/ filter · r refresh · Esc back · q hides nothing (use /plugins slurm pane).
Standard library only; Python 3.10 or newer. hoplon-tui puts its own directory on PYTHONPATH.
"""

import curses
import locale
import os
import pwd
import queue
import sys

from conversation import A, D, Q, R, W, clean, fit, width
from ipc import Client

STATE = os.environ.get("HOPLON_TUI_STATE", "")
ROLE = "plugin:slurm"
BAD = ("down", "drain", "fail", "not_responding", "unk", "maint", "reboot", "inval")
HINT = "↑↓ choose · Enter details · i insert · a attach · / filter · r refresh"


def rows(output, n):
    """The '|'-separated lines of a command's output with n fields each; the rest are skipped."""
    out = []
    for line in (output or "").splitlines():
        parts = line.split("|")
        if len(parts) >= n:
            out.append([p.strip() for p in parts[:n - 1]] + ["|".join(parts[n - 1:]).strip()])
    return out


def bad(state):
    return any(b in state.lower() for b in BAD)


def parse_nodes(output):
    """sinfo -N lists a node once per partition: one entry per node, partitions joined."""
    nodes = {}
    for name, part, state, cpus, mem, reason in rows(output, 6):
        n = nodes.setdefault(name, {"name": name, "partitions": [], "state": state, "cpus": cpus, "mem": mem,
                                    "reason": "" if reason == "none" else reason})
        if part not in n["partitions"]:
            n["partitions"].append(part)
    return sorted(nodes.values(), key=lambda n: (not bad(n["state"]), n["state"], n["name"]))


def parse_partitions(output):
    return [{"name": p, "avail": a, "limit": lim, "nodes": f} for p, a, lim, _total, f, _cpus in rows(output, 6)]


def parse_queue(output, user):
    jobs = [{"id": i, "partition": p, "name": n, "user": u, "state": s, "time": t, "nodes": d, "reason": r}
            for i, p, n, u, s, t, d, r in rows(output, 8)]
    counts = {}
    for j in jobs:
        counts[j["state"]] = counts.get(j["state"], 0) + 1
    return counts, [j for j in jobs if j["user"] == user]


def matches(text, flt):
    return not flt or flt.lower() in text.lower()


def view(st, w, sel, flt):
    """The pane as lines of (text, role) segments, and what each line selects: ("node", name), ("job", id) or None."""
    lines, acts = [], []

    def add(segs, act=None):
        lines.append(segs)
        acts.append(act)

    def head(title):
        if lines:
            add([])
        add([(title, A)])

    if st.get("detail"):
        title, text = st["detail"]
        add([(fit(clean(title), w), A)])
        for ln in text.splitlines():
            add([(fit(clean(ln), w), "")])
        return lines, acts
    for name in ("nodes", "partitions", "queue"):
        if st.get("errors", {}).get(name):
            add([(fit(f"{name}: {clean(st['errors'][name])}", w), R)])
    nodes = [n for n in st.get("nodes") or [] if matches(n["name"] + " " + n["state"] + " " + n["reason"], flt)]
    head(f"NODES ({len(st.get('nodes') or [])})" + (f" matching {clean(flt)}" if flt else ""))
    if st.get("nodes") is None:
        add([("loading…", Q)])
    by_state = {}
    for n in st.get("nodes") or []:
        by_state[n["state"]] = by_state.get(n["state"], 0) + 1
    if by_state:
        add([(fit(" · ".join(f"{k} {v}" for k, v in sorted(by_state.items())), w), Q)])
    for n in nodes:
        if not bad(n["state"]) and not flt:
            continue                                  # the healthy ones are in the counts above; / shows them
        mark = ">" if sel == len(lines) else " "
        reason = f"  {n['reason']}" if n["reason"] else ""
        add([(mark, A), (fit(clean(n["name"]), 16).ljust(16), ""),
             (fit(clean(n["state"]), 12).ljust(12), R if bad(n["state"]) else D),
             (fit(clean(reason), max(w - 30, 1)), Q)], ("node", n["name"]))
    head("PARTITIONS")
    for p in st.get("partitions") or []:
        add([(fit(f" {clean(p['name']):14} {clean(p['avail']):6} {clean(p['limit']):12} nodes a/i/o/t "
                  f"{clean(p['nodes'])}", w), "" if p["avail"] == "up" else R)])
    counts, mine = st.get("queue") or ({}, None)
    head("QUEUE")
    if mine is None:
        add([("loading…", Q)])
    else:
        add([(fit(" " + (" · ".join(f"{clean(k).lower()} {v}" for k, v in sorted(counts.items())) or "empty"), w),
              "")])
        head(f"MY JOBS ({clean(st.get('user', '?'))})")
        shown = [j for j in mine if matches(" ".join(j.values()), flt)]
        if not shown:
            add([(" none", Q)])
        for j in shown:
            mark = ">" if sel == len(lines) else " "
            add([(mark, A), (fit(clean(j["id"]), 12).ljust(12), ""),
                 (fit(clean(j["state"]), 10).ljust(10), W if j["state"] == "PENDING" else D),
                 (fit(f"{clean(j['name'])} {clean(j['time'])} {clean(j['reason'])}", max(w - 23, 1)), Q)],
                ("job", j["id"]))
    return lines, acts


class Console:
    def __init__(self, link, user):
        self.link, self.st = link, {"user": user, "errors": {}}
        self.sel, self.flt, self.msg, self.n = None, "", "", 0
        self.waiting = {}          # our run id -> what the answer is for
        self.acts = []

    def run(self, command, purpose, **params):
        self.n += 1
        rid = f"s{self.n}"
        self.waiting[rid] = purpose
        if not self.link.send({"type": "run", "id": rid, "command": command, "params": params}):
            self.msg = "the conversation isn't listening"

    def refresh(self):
        for command in ("nodes", "partitions", "queue"):
            self.run(command, command)

    def handle(self, msg):
        if msg.get("type") == "refused":
            self.msg = clean(str(msg.get("why", "refused")))
            return
        if msg.get("type") != "ran":
            return
        purpose = self.waiting.pop(msg.get("id"), None)
        if purpose is None:
            return
        out = msg.get("output") if isinstance(msg.get("output"), str) else ""
        err = msg.get("error") or (None if msg.get("exit") == 0 else f"exit {msg.get('exit')}: {out[:200]}")
        if isinstance(purpose, tuple):                # details of a node or job
            self.st["detail"] = (f"{purpose[0]} {purpose[1]}", str(err) if msg.get("error") else out)
            return
        self.st["errors"][purpose] = str(err) if err else None
        if msg.get("cached"):
            self.msg = f"{purpose}: cached (hoplon keeps results a while)"
        if err:
            return
        if purpose == "nodes":
            self.st["nodes"] = parse_nodes(out)
        elif purpose == "partitions":
            self.st["partitions"] = parse_partitions(out)
        elif purpose == "queue":
            self.st["queue"] = parse_queue(out, self.st["user"])

    def selected(self):
        if self.sel is not None and 0 <= self.sel < len(self.acts):
            return self.acts[self.sel]
        return None

    def move(self, step, w):
        _, self.acts = view(self.st, w, self.sel, self.flt)
        idx = [i for i, a in enumerate(self.acts) if a]
        if not idx:
            self.sel = None
            return
        if self.sel not in idx:
            self.sel = idx[0] if step > 0 else idx[-1]
        else:
            self.sel = idx[max(0, min(len(idx) - 1, idx.index(self.sel) + step))]

    def details(self):
        act = self.selected()
        if act:
            self.run(act[0], act, **({"name": act[1]} if act[0] == "node" else {"id": act[1]}))
            self.st["detail"] = (f"{act[0]} {act[1]}", "loading…")

    def key(self, k, w, ask=None):
        """One key. ask(prompt) reads a line, for the filter. Returns False to stop."""
        if k in (curses.KEY_UP, "k"):
            self.move(-1, w)
        elif k in (curses.KEY_DOWN, "j"):
            self.move(1, w)
        elif k in ("\n", "\r", curses.KEY_ENTER):
            self.details()
        elif k == "\x1b":
            self.st.pop("detail", None)
        elif k == "r":
            self.refresh()
        elif k == "/" and ask:
            self.flt = ask("filter: ")
        elif k == "i":
            act = self.selected()
            if act:
                self.link.send({"type": "insert", "text": act[1]})
        elif k == "a":
            d = self.st.get("detail")
            if d:
                self.link.send({"type": "attach", "label": f"Slurm {d[0]}", "text": d[1]})
            else:
                text = "\n".join("".join(t for t, _ in ln) for ln in view(self.st, 200, None, self.flt)[0])
                self.link.send({"type": "attach", "label": "Slurm overview", "text": text})
        return True


class Screen:
    def __init__(self, scr, console):
        self.scr, self.c = scr, console
        curses.curs_set(0)
        curses.use_default_colors()
        self.roles = {"": 0}
        if curses.has_colors():
            quiet = 8 if curses.COLORS >= 16 else curses.COLOR_WHITE
            for i, (role, fg) in enumerate([(Q, quiet), (A, curses.COLOR_BLUE), (W, curses.COLOR_YELLOW),
                                            (D, curses.COLOR_GREEN), (R, curses.COLOR_RED)], 1):
                curses.init_pair(i, fg, -1)
                self.roles[role] = curses.color_pair(i)
        curses.mousemask(getattr(curses, "BUTTON1_PRESSED", 0) | getattr(curses, "BUTTON1_CLICKED", 0))
        scr.keypad(True)
        scr.timeout(200)

    def ask(self, prompt):
        h, w = self.scr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        self.scr.addstr(h - 1, 0, " " * (w - 1))
        self.scr.addstr(h - 1, 0, prompt)
        try:
            text = self.scr.getstr(h - 1, len(prompt), 60).decode("utf-8", "replace")
        finally:
            curses.noecho()
            curses.curs_set(0)
        return clean(text.strip())

    def draw(self):
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        lines, self.c.acts = view(self.c.st, w - 1, self.c.sel, self.c.flt)
        for y, segs in enumerate(lines[:h - 1]):
            x = 0
            for text, role in segs:
                try:
                    self.scr.addstr(y, x, fit(text, max(w - 1 - x, 1)), self.roles.get(role, 0))
                except curses.error:
                    pass
                x += width(text)
        try:
            self.scr.addstr(h - 1, 0, fit(self.c.msg or HINT, w - 1), self.roles[Q])
        except curses.error:
            pass
        self.scr.refresh()

    def loop(self):
        self.c.refresh()
        while True:
            while True:
                try:
                    msg = self.c.link.inbox.get_nowait()
                except queue.Empty:
                    break
                if msg is None:
                    self.c.msg = "the conversation has ended"
                else:
                    self.c.handle(msg)
            self.draw()
            try:
                k = self.scr.get_wch()
            except (curses.error, KeyboardInterrupt):
                continue
            if k == curses.KEY_MOUSE:
                try:
                    _, _, y, _, _ = curses.getmouse()
                except curses.error:
                    continue
                if 0 <= y < len(self.c.acts) and self.c.acts[y]:
                    self.c.sel = y
                    self.c.details()
            elif k != curses.KEY_RESIZE:
                self.c.msg = ""
                self.c.key(k, self.scr.getmaxyx()[1] - 1, self.ask)


def main():
    if not STATE:
        print("slurm.py is started by hoplon-tui: /plugins slurm pane", file=sys.stderr)
        return 2
    try:
        link = Client(STATE, ROLE)
    except OSError as e:
        print(f"slurm.py: can't reach the conversation pane: {e}", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    user = pwd.getpwuid(os.geteuid()).pw_name
    curses.wrapper(lambda scr: Screen(scr, Console(link, user)).loop())
    return 0


if __name__ == "__main__":
    sys.exit(main())
