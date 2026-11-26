"""/fixtures: real samples for the model's sandboxed runs. hoplon keeps them in a private folder outside the project
and mounts them read-only at /fixtures, but only once you approve exactly what is in it, after its secret checks. This
lists the folder, copies files in from the shell pane's directory, and has the core ask you to approve them (a feature
can't build or answer an approval). The model asks for samples with a "samples requested" card; it never makes them."""

import os

from . import Feature

USAGE = "usage: /fixtures, /fixtures add FILE…, /fixtures use, /fixtures drop or /fixtures clear"


class Fixtures(Feature):
    name = "fixtures"
    commands = {"/fixtures": "[add FILE…|use|drop|clear]: real samples, read-only at /fixtures in the model's "
                             "sandboxed runs once you approve them; add copies files from the shell pane's directory"}
    to_hoplon = ("/fixtures",)
    needs = "fixtures"
    cant = "keep samples for the model's runs"
    events = ("fixtures",)
    actions = ("fixtures_add",)

    def __init__(self):
        self.info = None       # hoplon's last fixtures event
        self.then = None       # what to do with the next one: "show", or "ask" (the user typed /fixtures use)

    def command(self, m, cmd, arg):
        words = arg.split()
        what = words[0] if words else ""
        if what not in ("", "add", "use", "drop", "clear") or (what == "add") != (len(words) > 1):
            m.note(USAGE, self.core.R)
            return
        if what == "add":
            m.actions.append(("fixtures_add", words[1:]))
            return
        if what == "clear":
            m.ask_approval("fixtures_clear", None)
            return
        if what == "drop":
            m.send({"type": "fixtures", "action": "drop"})
        else:
            m.send({"type": "fixtures"})
        self.then = "ask" if what == "use" else "show"

    def action(self, app, action):
        """/fixtures add: the paths are the shell pane's, so they are resolved in its directory."""
        c, m, paths = self.core, app.m, action[1]
        cwd = None
        if m.remote():
            if not all(p.startswith("/") for p in paths):
                m.note("in remote mode, give each file's full path on that host", c.R)
                return
        elif app.shell:
            try:
                (_, cwd), _ = app.shell.context()
            except (c.Busy, OSError, c.subprocess.SubprocessError) as e:
                m.note(f"the shell pane's directory is unavailable ({e}); give full paths", c.R)
                if not all(p.startswith("/") for p in paths):
                    return
        m.send({"type": "fixtures", "action": "add", "paths": paths, "cwd": cwd or os.getcwd()})
        self.then = "show"

    def event(self, m, ev):
        self.info = ev
        then, self.then = self.then, None
        if then:
            self.show(m, ev)
        if then == "ask":
            files = ev.get("files") if isinstance(ev.get("files"), list) else []
            if not files:
                m.note("nothing to approve: /fixtures add FILE… copies files in", self.core.W)
            elif any(isinstance(r, dict) and r.get("flag") for r in files):
                m.note("not asking: scrub or replace the files marked ✗, then /fixtures use again", self.core.R)
            else:
                m.ask_approval("fixtures_use", ev)          # the core builds and answers approvals

    def show(self, m, ev):
        c = self.core
        m.note(f"samples folder: {c.clean(str(ev.get('dir', '?')))}"
               + (f"  ({c.clean(str(ev['problem']))})" if ev.get("problem") else ""), c.R if ev.get("problem") else "")
        files = ev.get("files") if isinstance(ev.get("files"), list) else []
        for r in files[:30]:
            if not isinstance(r, dict):
                continue
            flag = r.get("flag")
            m.note(f"  {'✗' if flag else '·'} {c.clean(str(r.get('name', '?')))}  {c.clean(str(r.get('size', '?')))} "
                   "bytes" + (f"  — can't be used: {c.clean(str(flag))}" if flag else ""), c.R if flag else "")
        if len(files) > 30:
            m.note(f"  … {len(files) - 30} more")
        if not files:
            m.note("  empty: /fixtures add FILE… copies files in from the shell pane's directory", c.Q)
        m.note("approved: the model's sandboxed runs see them read-only at /fixtures" if ev.get("approved") else
               "not approved: /fixtures use checks them and asks", c.D if ev.get("approved") else c.Q)

    def snapshot(self, m, view):
        ev = self.info or {}
        files = ev.get("files") if isinstance(ev.get("files"), list) else []
        view["fixtures"] = {"files": len(files), "approved": bool(ev.get("approved"))} if self.info else None


FEATURE = Fixtures
