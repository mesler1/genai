"""/plugins: the plugins genai offers (trusted commands), switched on and off, and each one's own pane.

A plugin can declare a "pane": a program in its directory, blessed with the rest of it, that shows something (the
Slurm console shows nodes and jobs). genai-tui runs it out of process, in a tmux pane of its own, never inside the
conversation pane, which holds the API key's way in (the key proxy's token, in remote mode) and every answer to genai.
The pane connects to the private socket as "plugin:NAME" (tui/ipc.py) and may only:

  run     one of its own plugin's read-only commands, through genai, with genai's checks, cache and rate limits;
          the answer ("ran") comes back to that pane alone
  insert  put text in the input box (where you see it before anything is sent)
  attach  offer text for your next message, which opens in your editor first, like shell context

A command that isn't read-only is never run from a pane: the model's own calls of it ask you, and so must a pane's.
"""

import os
import re
import shlex
import sys

from . import Feature

MAX_TEXT = 65536           # characters a pane may insert or offer to attach
PANE_NAME = re.compile(r"[A-Za-z0-9._-]+")


class Plugins(Feature):
    name = "plugins"
    commands = {"/plugins": "list plugins (trusted commands); /plugins NAME on|off switches one, /plugins NAME pane "
                            "shows or hides its pane"}
    needs = "plugins"
    cant = "use plugins"
    events = ("plugins", "ran")
    actions = ("plugin_pane", "plugin_attach")

    def __init__(self):
        self.rows = None           # genai's plugins event, as received
        self.asked = {}            # our run id -> (the pane role that asked, the pane's own id for it)
        self.n = 0
        self.panes = {}            # plugin name -> tmux pane id, once started

    # -- what genai said

    def plugin(self, name):
        return next((r for r in self.rows or [] if isinstance(r, dict) and r.get("name") == name), None)

    def command_of(self, name, command):
        row = self.plugin(name)
        cmds = row.get("commands") if row else None
        return next((c for c in cmds or [] if isinstance(c, dict) and c.get("name") == command), None)

    def event(self, m, ev):
        if ev.get("type") == "plugins":
            if isinstance(ev.get("plugins"), list):
                self.rows = ev["plugins"]
            return
        asked = self.asked.pop(ev.get("id"), None) if isinstance(ev.get("id"), str) else None
        if asked:                                          # a ran: only the pane that asked hears the answer
            m.panes.append((asked[0], dict(ev, id=asked[1])))

    # -- /plugins

    def command(self, m, cmd, arg):
        c = self.core
        words = arg.split()
        if not words:
            self.list(m)
            return
        name, what = words[0], (words[1] if len(words) > 1 else "")
        row = self.plugin(name)
        if row is None:
            m.note(f"no plugin named {c.clean(name)} (/plugins lists them)", c.R)
        elif what in ("on", "off") and len(words) == 2:
            if m.busy:
                m.note("wait until genai has replied, then switch it", c.W)
                return
            m.send({"type": "plugin", "name": name, "on": what == "on"})
        elif what == "pane" and len(words) == 2:
            self.pane(m, row)
        else:
            m.note("usage: /plugins, /plugins NAME on|off, /plugins NAME pane", c.R)

    def list(self, m):
        c = self.core
        if self.rows is None:
            m.send({"type": "plugins"})
            m.note("asking genai for its plugins; /plugins again shows them")
            return
        if not self.rows:
            m.note("no plugins found (see the README: Plugins)")
        for r in self.rows:
            if not isinstance(r, dict):
                continue
            state = "on " if r.get("enabled") else "off"
            status = "" if r.get("blessed") else f" ({c.clean(str(r.get('status', '?')))})"
            pane = " · pane: /plugins %s pane" % c.clean(str(r.get("name"))) if r.get("pane") else ""
            m.note(f"[{state}] {c.clean(str(r.get('name', '?')))}{status}: {c.clean(str(r.get('description', '')))}"
                   f"{pane}", c.D if r.get("enabled") else "")

    def pane(self, m, row):
        c, name = self.core, row.get("name")
        pane, d = row.get("pane"), row.get("dir")
        if not row.get("enabled"):
            m.note(f"switch it on first: /plugins {c.clean(name)} on", c.W)
        elif not pane:
            m.note(f"plugin {c.clean(name)} has no pane", c.W)
        elif (m.hello or {}).get("remote"):
            m.note("plugin panes don't run in remote mode yet", c.W)
        elif not (isinstance(pane, str) and PANE_NAME.fullmatch(pane) and isinstance(d, str) and os.path.isabs(d)):
            m.note(f"plugin {c.clean(name)}: genai gave no usable pane", c.R)
        else:
            m.actions.append(("plugin_pane", name, os.path.join(d, pane)))

    def help(self, m):
        on = [r for r in self.rows or [] if isinstance(r, dict) and r.get("enabled")]
        for r in on:
            for cmd in r.get("commands") or []:
                if isinstance(cmd, dict):
                    how = "read-only" if cmd.get("read_only") else "asks"
                    m.note(f"  {self.core.clean(str(r.get('name')))}: {self.core.clean(str(cmd.get('name')))} "
                           f"({how}) {self.core.clean(str(cmd.get('describe', '')))}")

    def snapshot(self, m, view):
        view["plugins"] = [{"name": self.core.clean(str(r.get("name", "?"))),
                            "commands": [self.core.clean(str(c.get("name", "?"))) for c in r.get("commands") or []
                                         if isinstance(c, dict)],
                            "pane": bool(r.get("pane"))}
                           for r in self.rows or [] if isinstance(r, dict) and r.get("enabled")]

    # -- from a plugin's pane

    def request(self, m, role, msg):
        c, name = self.core, role.split(":", 1)[1]
        row = self.plugin(name)
        if not row or not row.get("enabled"):
            m.panes.append((role, {"type": "refused", "why": "this plugin is not on"}))
            return
        kind = msg.get("type")
        if kind == "run":
            command, params = msg.get("command"), msg.get("params", {})
            spec = self.command_of(name, command) if isinstance(command, str) else None
            if spec is None or not isinstance(params, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in params.items()):
                m.panes.append((role, {"type": "ran", "id": msg.get("id"), "error": "no such command"}))
                return
            if not spec.get("read_only"):
                m.panes.append((role, {"type": "ran", "id": msg.get("id"),
                                       "error": "only read-only commands run from a pane"}))
                return
            self.n += 1
            rid = f"pl{self.n}"
            own = msg.get("id") if type(msg.get("id")) in (str, int) else None
            self.asked[rid] = (role, own)
            m.send({"type": "run", "id": rid, "plugin": name, "command": command, "params": params})
        elif kind == "insert" and isinstance(msg.get("text"), str) and 0 < len(msg["text"]) <= MAX_TEXT:
            m.panes.append(("prompt", {"type": "insert", "text": c.clean(msg["text"])}))
        elif kind == "attach" and isinstance(msg.get("text"), str) and 0 < len(msg["text"]) <= MAX_TEXT:
            label = msg.get("label") if isinstance(msg.get("label"), str) else "output"
            m.actions.append(("plugin_attach", name, c.clean(label)[:80], msg["text"]))

    # -- on screen

    def action(self, app, action):
        if action[0] == "plugin_attach":
            _, name, label, text = action
            kept = app.edit(f"# From the {name} plugin's pane. EVERYTHING below is sent to the model as saved.\n"
                            f"# Remove anything you don't want it to see.\n{label}:\n```\n{text}\n```\n",
                            f"{name}: {label}", "sent to the model exactly as saved; nothing is added", "send nothing",
                            top_only=True)
            if kept is None or not kept.strip():
                app.m.note("nothing sent")
            else:
                app.m.attach(f"{name}: {label}", kept)
            return
        _, name, path = action
        if not app.shell or not app.state_dir:
            app.m.note("plugin panes need genai-tui", self.core.R)
            return
        pid = self.panes.get(name)
        if pid and app.shell.tmux("display", "-p", "-t", pid, "#{pane_id}").strip() == pid:
            app.toggle_pane(pid, f"plugin {name}", ["-l", "60"], f"{name} pane")
            return
        run = (f"{shlex.quote(sys.executable)} {shlex.quote(path)}" if path.endswith(".py") else shlex.quote(path))
        cmd = (f"{run}; rc=$?; if [ $rc -ne 0 ]; then echo; echo \"the {name} pane ended with code $rc. "
               f"Press Enter.\"; read -r _; fi")
        tui = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        me = os.environ.get("TMUX_PANE", "")
        pid = app.shell.tmux("split-window", "-d", "-h", "-f", "-l", "60", "-t", me,
                             "-e", f"GENAI_TUI_STATE={app.state_dir}", "-e", f"GENAI_TUI_PLUGIN={name}",
                             "-e", f"PYTHONPATH={tui}", "-c", os.path.dirname(path), "-P", "-F", "#{pane_id}",
                             cmd).strip()
        if not pid:
            app.m.note(f"could not start the {name} pane", self.core.R)
            return
        self.panes[name] = pid
        app.shell.tmux("set", "-p", "-t", pid, "@genai_title", f"{name} · /plugins {name} pane hides it")


FEATURE = Plugins
