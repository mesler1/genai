"""/work: a work session. The model plans, you approve the plan, then it codes and tests each item on a branch of its
own, in a copy of the repository, and commits it. Shown as notes in the conversation and a WORK section in the info
panel; the branch is yours to review and merge when it ends."""

from . import Feature


def items_of(ev):
    """The plan's items from an event, keeping only well-formed ones: genai sends a list of objects."""
    items = ev.get("items")
    return [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []


def number(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None

class Work(Feature):
    name = "work"
    commands = {"/work": "TASK: the model plans, then codes, tests and commits each item on a branch of its own "
                         "· /work shows it · /work end stops it, even mid-reply"}
    needs = "work"
    cant = "run work sessions"
    events = ("work",)

    def __init__(self):
        self.info = None           # the session's last state from genai, while one is on
        self.last = None           # how the last one ended, for /work after it

    def command(self, m, cmd, arg):
        core = self.core
        if not arg:
            self.show(m)
        elif arg == "end" and self.info is None:
            m.note("no work session is on", core.Q)
        elif arg == "end" and m.exited is None and (not m.busy or m.can_halt()):
            if m.busy:
                m.note("ending the work session: genai stops its turn first, then commits what is there", core.W)
            m.send({"type": "work_end"})
        elif m.busy or m.exited is not None:
            m.note("/work has to wait until genai finishes this reply (Ctrl-C stops a long one)", core.W)
        elif self.info is not None:
            m.note("a work session is on: send a message to go on with it, or /work end", core.W)
        else:
            m.turn += 1
            m.add(core.UserMsg(f"/work {arg}"))
            m.busy = True
            m.send({"type": "work", "task": arg})

    def show(self, m):
        core, w = self.core, self.info
        if w is None:
            m.note("no work session is on · /work TASK starts one" if self.last is None else
                   f"the last work session ended: {self.last}", core.Q)
            return
        m.note(f"work session on branch {core.one_row(w.get('branch', '?'))} · {self.status(w)}", core.A)
        for it in items_of(w):
            m.note(self.item_line(it, w.get("current")))
        m.note("/work end stops it; what's committed stays on the branch", core.Q)

    def status(self, w):
        items = items_of(w)
        done = sum(1 for it in items if it.get("status") == "done")
        cur, role = number(w.get("current")), w.get("role")
        if not items:
            return "planning"
        doing = {"review": f"planner reviews item {cur}", "plan": "planning"}.get(role, f"on item {cur}")
        return f"{done} of {len(items)} done" + (f" · {doing}" if cur else "")

    def item_line(self, it, current):
        row = self.core.one_row
        mark = {"done": "✓", "left out": "✗"}.get(it.get("status"), "▸" if it.get("n") == current else "·")
        commit = f"  {row(it['commit'])[:7]}" if it.get("commit") else ""
        n = number(it.get("n"))
        return f"{mark} {n if n is not None else '?':>2}. {row(it.get('title', ''))}{commit}"

    def event(self, m, ev):
        core = self.core
        clean, state = core.clean, ev.get("state")
        if state == "ended":
            self.ended(m, ev)
            return
        self.info = {**(self.info or {}), **{k: v for k, v in ev.items() if k not in ("type", "state")}}
        if state == "context":
            self.info["role"] = ev.get("role")
            self.info["current"] = ev.get("item")
        if state == "started":
            mins = (number(ev.get("time_limit")) or 0) // 60
            test = f"tests with {clean(str(ev['test']))}" if ev.get("test") else "no test command"
            m.note(f"work session on branch {clean(str(ev.get('branch', '?')))}, a copy of your last commit "
                   f"({clean(str(ev.get('base', '')))[:7]}). Your own files are not touched.", core.A)
            m.note(f"  {ev.get('steps_per_item', '?')} model steps per item · {mins} min without you · {test}",
                   core.Q)
            pl = ev.get("planner")
            if isinstance(pl, dict):
                m.note(f"  plans with {clean(str(pl.get('model', '?')))} on {clean(str(pl.get('endpoint', '?')))}",
                       core.Q)
        elif state == "plan":
            m.note(f"plan approved: {len(items_of(ev))} items", core.D)
        elif state == "item":
            row = core.one_row
            done = [it for it in items_of(ev) if it.get("status") == "done"]
            if done:
                last = max(done, key=lambda it: number(it.get("n")) or 0)
                commit = f" as {row(last['commit'])[:7]}" if last.get("commit") else ""
                m.note(f"item {number(last.get('n')) or '?'} committed{commit}: {row(last.get('title', ''))}", core.D)
            items = items_of(ev)
            nxt = number(ev.get("current"))
            if nxt and 0 < nxt <= len(items):
                m.note(f"next, item {nxt} of {len(items)}: {row(items[nxt - 1].get('title', ''))}", core.Q)
        elif state == "context":
            who = (f"item {ev.get('item')}" if ev.get("role") == "item"
                   else f"the planner, to review item {ev.get('item')}")
            m.note(f"a fresh context for {who} ({clean(str(ev.get('why', '')))})", core.Q)

    def ended(self, m, ev):
        core, clean = self.core, self.core.clean
        why = ev.get("why")
        head = {"done": "work session done", "ended": "work session ended"}.get(why, f"work session ended ({clean(str(why))})")
        self.info = None
        for p in ev.get("problems") if isinstance(ev.get("problems"), list) else []:
            m.note(f"  {clean(str(p))}", core.R)
        if ev.get("path"):
            m.note(f"  nothing was deleted: the copy is still at {core.one_row(ev['path'])}", core.W)
        if not ev.get("branch"):
            self.last = f"{head}; nothing was changed"
            m.note(f"{head}: nothing was changed, so the branch was deleted", core.Q)
            return
        branch = clean(str(ev["branch"]))
        self.last = f"{head}, on branch {branch}"
        m.note(f"{head}: the work is on branch {branch}", core.D if why == "done" else core.W)
        if ev.get("unfinished"):
            m.note(f"  changes not yet finished were committed as {clean(str(ev['unfinished']))[:7]}", core.W)
        for line in ev.get("commits") if isinstance(ev.get("commits"), list) else []:
            m.note(f"  {core.one_row(line)}")
        for line in clean(str(ev.get("stat") or "")).rstrip("\n").split("\n")[-1:]:
            if line.strip():
                m.note(f"  {line.strip()}", core.Q)
        if isinstance(ev.get("commands"), list) and ev["commands"]:
            m.note("  review and merge it yourself:", core.Q)
            for c in ev["commands"]:
                m.note(f"    {core.one_row(c)}")

    def error(self, m, ev):
        if ev.get("code") == "work_refused":
            m.note(f"no work session: {self.core.clean(ev.get('message', ''))}", self.core.R)
            if m.busy and not m.pending:
                m.busy = False
                m.next_queued()
            return True
        return False

    def snapshot(self, m, view):
        w = self.info
        if w is None:
            return
        view["work"] = {"branch": self.core.one_row(w.get("branch", "")), "status": self.status(w),
                        "items": [self.item_line(it, w.get("current")) for it in items_of(w)]}

    def help(self, m):
        m.note("A work session runs in a copy of your repository; each item is tested and committed there, and the "
               "branch is yours to review and merge. The model's edits there don't ask; the plan does.")


FEATURE = Work
