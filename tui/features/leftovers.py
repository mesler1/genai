"""/leftovers: what hoplon made for sandboxes and jobs that is still there (samples folders, work session copies,
hoplon/work-N branches, sandbox containers, temp files, hoplon-tui's own folders and tmux servers), which are likely
stale and why, and the exact command that removes each. /leftovers clean has the core ask before hoplon removes the
stale ones it can remove safely (a feature can't build or answer an approval). In remote mode it is that host's."""

from . import Feature


def size(n):
    if not isinstance(n, (int, float)):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def age(s):
    if not isinstance(s, (int, float)):
        return ""
    for span, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if s >= span:
            return f"{s // span:.0f} {unit} old"
    return f"{s:.0f} s old"


class Leftovers(Feature):
    name = "leftovers"
    commands = {"/leftovers": "[clean]: what hoplon made for sandboxes and jobs that is still there, what is likely "
                              "stale, and how to remove it; clean removes the stale ones after asking"}
    to_hoplon = ("/leftovers",)
    needs = "leftovers"
    cant = "list its leftovers"
    events = ("leftovers",)

    def __init__(self):
        self.then = None            # "show", or "ask" (the user typed /leftovers clean)

    def command(self, m, cmd, arg):
        if arg not in ("", "clean"):
            m.note("usage: /leftovers or /leftovers clean", self.core.R)
            return
        m.send({"type": "leftovers"})
        self.then = "ask" if arg == "clean" else "show"

    def event(self, m, ev):
        then, self.then = self.then, None
        if not then:
            return
        c = self.core
        for key, label in (("removed", "removed"),):
            if isinstance(ev.get(key), list) and ev[key]:
                m.note(f"{label} {len(ev[key])}", c.D)
        for f in ev.get("failed") or []:
            if isinstance(f, dict):
                m.note(f"couldn't remove {c.clean(str(f.get('id')))}: {c.clean(str(f.get('error')))}", c.R)
        items = [i for i in ev.get("items") or [] if isinstance(i, dict)]
        self.show(m, items)
        safe = [i for i in items if i.get("safe") and isinstance(i.get("id"), str)]
        if then == "ask":
            if safe:
                m.ask_approval("leftovers_clean", {"ids": [i["id"] for i in safe]})
            else:
                m.note("nothing to remove safely", c.Q)

    def show(self, m, items):
        c = self.core
        if not items:
            m.note("nothing left over: no samples folders, work copies, hoplon/work-N branches, sandbox containers, "
                   "temp files or hoplon-tui folders", c.D)
            return
        stale = [i for i in items if i.get("stale")]
        m.note(f"left over: {len(items)} ({len(stale)} likely stale)")
        groups = {}
        for it in items:
            key = (it.get("kind"), it.get("what"), it.get("stale")) if it.get("stale") and it.get("safe") else id(it)
            groups.setdefault(key, []).append(it)
        for group in groups.values():
            it = group[0]
            head = c.clean(str(it.get("what", "?")))
            facts = ", ".join(x for x in (size(it.get("bytes")), age(it.get("age"))) if x)
            mark = "✗" if it.get("stale") else "·"
            m.note(f"  {mark} {f'{len(group)} × ' if len(group) > 1 else ''}{head}"
                   + (f"  [{facts}]" if facts and len(group) == 1 else ""), c.W if it.get("stale") else "")
            for g in group[:5]:
                m.note(f"      {c.clean(str(g.get('path', '')))}", c.Q)
            if len(group) > 5:
                m.note(f"      … {len(group) - 5} more", c.Q)
            if it.get("stale"):
                m.note(f"      likely stale: {c.clean(str(it['stale']))}", c.Q)
            if it.get("note"):
                m.note(f"      ! {c.clean(str(it['note']))}", c.R)
            if it.get("stale") and it.get("remove") and len(group) == 1:
                m.note(f"      remove: {c.clean(str(it['remove']))}")
        safe = sum(1 for i in stale if i.get("safe"))
        if safe:
            m.note(f"/leftovers clean removes the {safe} stale one{'s' if safe != 1 else ''} it can remove safely, "
                   "after asking" + (f"; {len(stale) - safe} need a look first (marked !)" if len(stale) > safe else ""))


FEATURE = Leftovers
