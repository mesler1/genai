"""/files: the working directory as a tree beside the conversation (tui/files.py), and what it can ask for: a listing,
a file in the pager, a file attached to your next message, its path in the input box, or a file marked ⚠ shown to you
and, if you say so, marked as holding no secrets (the core asks; hoplon refuses it if the file changed since).

The files pane never reads the disk. Listings and files come from hoplon, which applies its own checks (inside the
directory, nothing that holds credentials without a warning), and nothing reaches the model but an attachment you have
reviewed in your editor."""

from . import Feature

MAX_PATH = 4096


def valid_path(p):
    """A path as the files pane asks for it: "." or components joined by "/", relative, with no "..", no NUL."""
    return (isinstance(p, str) and 0 < len(p) <= MAX_PATH and "\0" not in p
            and (p == "." or (not p.startswith("/") and all(c not in ("", ".", "..") for c in p.split("/")))))


class Files(Feature):
    name = "files"
    commands = {"/files": "show or hide the file tree on the left"}
    needs = "files"
    cant = "list files"
    events = ("files", "file", "not_secret")
    requests = {"list": ("files",), "view": ("files",), "attach": ("files",), "insert": ("files",),
                "not_secret": ("files",)}
    actions = ("files", "file", "insert")

    def __init__(self):
        self.listing = []          # paths hoplon was asked to list, oldest first: its answers come in that order
        self.reading = []          # (path, "view" or "attach") of the files hoplon was asked to read

    def command(self, m, cmd, arg):
        m.actions.append(("files",))

    def request(self, m, role, msg):
        kind, path = msg.get("type"), msg.get("path")
        if not valid_path(path):
            return
        features = (m.hello or {}).get("features") or []
        if m.hello is None:
            features = ["files", "read"]         # the pane starts with hoplon; its hello is on the way, and in order
        if kind == "insert":
            m.panes.append(("prompt", {"type": "insert", "text": path}))
            m.actions.append(("insert",))             # and go there, to type around it
        elif kind == "list":
            if "files" in features:
                self.listing.append(path)
                m.send({"type": "files", "path": path})
            else:
                m.panes.append(("files", {"type": "unavailable", "reason": "this hoplon can't list files"}))
        elif kind == "not_secret" and "not_secret" not in features:
            m.note("this hoplon can't mark files as holding no secrets", self.core.R)
        elif "read" in features:
            self.reading.append((path, kind))
            m.send({"type": "read", "path": path})
        else:
            m.note(f"this hoplon can't read files, so it can't {kind} {self.core.clean(path)}", self.core.R)

    def event(self, m, ev):
        path = ev.get("path")
        if ev.get("type") == "not_secret":
            for x in ev.get("marked") if isinstance(ev.get("marked"), list) else []:
                if isinstance(x, dict) and isinstance(x.get("path"), str):
                    m.note(f"{self.core.clean(x['path'])}: marked as holding no secrets, until it changes")
                    parent = x["path"].rsplit("/", 1)[0] if "/" in x["path"] else "."
                    self.listing.append(parent)          # so the tree drops its ⚠
                    m.send({"type": "files", "path": parent})
        elif ev.get("type") == "files":
            if path in self.listing:             # an answer to something we asked for, and only that
                self.listing.remove(path)
                m.panes.append(("files", {"type": "listing", "path": path, "entries": ev.get("entries"),
                                          "truncated": ev.get("truncated") is True}))
        else:
            purpose = next((p for f, p in self.reading if f == path), None)
            if purpose:
                self.reading.remove((path, purpose))
                m.actions.append(("file", purpose, ev))

    def error(self, m, ev):
        """hoplon refused a listing or a read of ours. True if so, and nothing else is: other errors are the core's."""
        if ev.get("code") != "bad_path":
            return False
        path, message = ev.get("path"), str(ev.get("message", ""))
        if path is not None:
            purpose = next((p for f, p in self.reading if f == path), None)
            if purpose:
                self.reading.remove((path, purpose))
                m.note(f"cannot {purpose} {self.core.clean(path)}: {self.core.clean(message)}", self.core.R)
                return True
            return False
        for p in self.listing:                   # a listing error names the path only in its message
            if message.startswith(f"cannot list {p}: "):
                self.listing.remove(p)
                m.panes.append(("files", {"type": "listing", "path": p, "error": message[len(f"cannot list {p}: "):]}))
                return True
        return False

    def action(self, app, action):
        if action[0] == "files":
            app.toggle_pane(app.files, "files", app.FILES_JOIN, "file tree")
        elif action[0] == "insert":
            if app.prompt and app.shell:
                app.shell.tmux("select-pane", "-t", app.prompt)
        elif action[1] == "view":
            self.view(app, action[2])
        elif action[1] == "not_secret":
            ev = action[2]
            if not ev.get("secret") or not isinstance(ev.get("sha256"), str):
                app.m.note(f"{self.core.clean(str(ev.get('path')))}: hoplon doesn't take it for a secret now")
                return
            self.view(app, ev)                          # you see it first, then the core asks
            app.m.ask_approval("not_secret", ev)
        else:
            self.attach(app, action[2])

    def view(self, app, ev):
        """The file in the pager: cleaned, so nothing in it reaches the terminal raw."""
        head = [f"{ev.get('path')} · {ev.get('size')} bytes"]
        if ev.get("secret"):
            head.append("hoplon thinks this file holds secrets; masked: " + self.masked(ev))
        text = "\n".join(head) + "\n\n" + str(ev.get("text", ""))
        app.view_text("\n".join(self.core.clean(ln) for ln in text.split("\n")))

    def masked(self, ev):
        counts = ev.get("redacted") if isinstance(ev.get("redacted"), dict) else {}
        return ", ".join(f"{n} {self.core.clean(k)}" for k, n in counts.items()) or "nothing it recognised"

    def attach(self, app, ev):
        """The file in your editor first, like shell context: what you save goes with your next message."""
        path = str(ev.get("path"))
        warn = ("# hoplon thinks this file holds secrets (masked: " + self.masked(ev) + "). Check it before saving.\n"
                if ev.get("secret") else "")
        text = (warn + "# EVERYTHING below is sent to the model as saved. Remove anything you don't want it to see.\n"
                f"File {path}:\n```\n{ev.get('text', '')}\n```\n")
        kept = app.edit(text, f"file {path}", "sent to the model exactly as saved; nothing is added", "send nothing",
                        top_only=True)
        if kept is None or not kept.strip():
            app.m.note("nothing sent")
        else:
            app.m.attach(f"file {path}", kept)


FEATURE = Files
