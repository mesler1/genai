"""/shared: what you sent from the shell pane with hoplon-share (and /share context, and plugin text you attached),
waiting to go with your next message. Nothing here starts a turn: share several, then ask about them all.
/shared lists them, /shared drop N takes one back, /shared clear takes them all back."""

from . import Feature

MAX_NOTE = 200


class Shared(Feature):
    name = "shared"
    commands = {"/shared": "[drop N|clear]: what goes with your next message (hoplon-share from the shell pane)"}
    requests = {"piped": ("pipe",)}

    def request(self, m, role, msg):
        c = self.core
        text, note, cmd, rc = msg.get("text"), msg.get("note"), msg.get("command"), msg.get("rc")
        if not isinstance(text, str):
            return
        note = " ".join(note.split())[:MAX_NOTE] if isinstance(note, str) else ""
        cmd = " ".join(cmd.split())[:MAX_NOTE] if isinstance(cmd, str) and cmd.strip() else None
        rc = rc if isinstance(rc, int) and not isinstance(rc, bool) else None
        what = f"`{cmd}`" if cmd else "piped output"
        label = (f"{note} — " if note else "") + what + (f" (exit {rc})" if rc is not None else
                                                          "" if cmd else " (exit code unknown)")
        if isinstance(msg.get("cwd"), str):
            label += f" in {' '.join(msg['cwd'].split())[:MAX_NOTE]}"
        body = c.trim(text.rstrip("\n"))
        m.next_attachments.append({"type": "attach", "label": "shared from the shell pane: " + label, "text": body})
        n = body.count("\n") + 1 if body else 0
        m.note(f"shared {c.clean(label)} · {n} line{'s' if n != 1 else ''}; goes with your next message "
               f"({len(m.next_attachments)} waiting, /shared)", c.D)

    def command(self, m, cmd, arg):
        c, words = self.core, arg.split()
        waiting = m.next_attachments
        if not words:
            if not waiting:
                m.note("nothing waiting: cmd 2>&1 | hoplon-share --note \"…\" in the shell pane queues output here")
                return
            m.note(f"{len(waiting)} waiting for your next message:")
            for i, a in enumerate(waiting, 1):
                n = a["text"].count("\n") + 1 if a.get("text") else 0
                m.note(f"  {i}. {c.fit(c.clean(str(a.get('label', '?'))), 70)} · {n} line{'s' if n != 1 else ''}")
            m.note("/shared drop N takes one back; /shared clear takes them all back", c.Q)
        elif words == ["clear"]:
            n = len(waiting)
            waiting.clear()
            m.note(f"took back {n}; nothing waits for your next message" if n else "nothing was waiting")
        elif len(words) == 2 and words[0] == "drop" and words[1].isdigit() and 1 <= int(words[1]) <= len(waiting):
            a = waiting.pop(int(words[1]) - 1)
            m.note(f"took back {c.fit(c.clean(str(a.get('label', '?'))), 70)}; {len(waiting)} still waiting")
        else:
            m.note("usage: /shared, /shared drop N (from /shared's list) or /shared clear", c.R)

    def snapshot(self, m, view):
        view["shared"] = len(m.next_attachments)


FEATURE = Shared
