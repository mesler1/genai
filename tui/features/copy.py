"""/copy: your message and the reply to it, to tmux's buffer and the clipboard."""

from . import Feature


class Copy(Feature):
    name = "copy"
    commands = {"/copy": "[N]: copy your message and the reply to it, for the last turn or turn N (or right-click it)"}
    requests = {"copy": ("menu",)}
    actions = ("copy",)

    def turn_text(self, m, turn):
        """Your message for a turn and the replies that followed it, as text to copy (escaped, as shown), or None."""
        core = self.core
        asked = [b for b in m.blocks if isinstance(b, core.UserMsg) and b.turn == turn and not b.queued]
        if not turn or not asked:
            return None
        said = core.clean(asked[0].text).strip("\n")
        parts = ["\n".join("> " + ln if ln else ">" for ln in said.split("\n"))]
        parts += [core.clean(b.text).strip("\n") for b in m.blocks
                  if isinstance(b, core.Reply) and b.turn == turn and b.text.strip()]
        return "\n\n".join(parts) + "\n"

    def command(self, m, cmd, arg):
        if arg and not arg.isdigit():
            m.note("usage: /copy or /copy N (a turn)", self.core.R)
            return
        turns = [b.turn for b in m.blocks if isinstance(b, self.core.UserMsg) and not b.queued]
        turn = int(arg) if arg else max(turns, default=0)
        text = self.turn_text(m, turn)
        if text is None:
            m.note("nothing to copy yet" if not arg else f"turn {turn} isn't on screen", self.core.R)
        elif not m.shell:
            m.note("/copy needs tmux: start hoplon with hoplon-tui", self.core.R)
        else:
            m.actions.append(("copy", turn, text))

    def request(self, m, role, msg):
        if type(msg.get("turn")) is int:
            m.command(f"/copy {msg['turn']}")

    def action(self, app, action):
        app.shell.copy(action[2])
        n = action[2].count("\n")
        app.m.note(f"turn {action[1]} copied: your message and the reply, {n} lines (tmux buffer, and your "
                   "clipboard if the terminal allows it)")

    def turn_menu(self, m, turn):
        return [("Copy this message and its reply", "c", "copy", str(turn))] if self.turn_text(m, turn) else []


FEATURE = Copy
