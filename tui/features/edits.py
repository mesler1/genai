"""/edits: let a further edit to a file you approved apply without asking ("approved-files"), and see or clear the files
that do. genai keeps the set, per session and never saved; this shows it."""

from . import Feature


class Edits(Feature):
    name = "edits"
    commands = {"/edits": "[ask|approved-files|reset]: approved-files applies a further edit to a file you approved "
                          "one for without asking; reset forgets those files"}
    to_genai = ("/edits",)
    needs = "edits"
    cant = "apply edits to approved files without asking"
    events = ("edits",)

    def __init__(self):
        self.info = None           # {"mode": ..., "files": [...]}, as genai last said
        self.show = False          # print the next edits event (the user typed /edits)

    def current(self, m):
        info = self.info if self.info is not None else (m.hello or {}).get("edits")
        return info if isinstance(info, dict) else {"mode": "ask", "files": []}

    def command(self, m, cmd, arg):
        if arg == "reset":
            m.send({"type": "edits", "reset": True})
        elif arg in ("ask", "approved-files"):
            m.send({"type": "edits", "mode": arg})
        elif arg:
            m.note("usage: /edits, /edits ask, /edits approved-files or /edits reset", self.core.R)
            return
        else:
            self.note(m)
            return
        self.show = True

    def note(self, m):
        c, info = self.core, self.current(m)
        if info.get("mode") != "approved-files":
            m.note("edits: every edit and write asks (/edits approved-files to let further edits to a file you "
                   "approved apply without asking)")
            return
        files = [c.clean(f) for f in info.get("files") or [] if isinstance(f, str)]
        m.note("edits: approved-files: further edits to these apply without asking (still shown and logged): "
               + (", ".join(files) or "none yet") + " · /edits reset forgets them", c.W)

    def event(self, m, ev):
        self.info = {"mode": ev.get("mode"), "files": ev.get("files") if isinstance(ev.get("files"), list) else []}
        if self.show:
            self.show = False
            self.note(m)

    def snapshot(self, m, view):
        info = self.current(m)
        view["edits"] = {"mode": info.get("mode"),
                         "files": [self.core.clean(f) for f in info.get("files") or [] if isinstance(f, str)]}


FEATURE = Edits
