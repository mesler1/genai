"""Subagents: genai's delegate tool, as the conversation pane shows it.

A subagent is a separate model run that genai starts for the model, in coding mode: it can only read, list and search
the working directory (and run commands in the offline sandbox), and its report goes back to the model. Its card shows
the task and, while it works, its step, the tokens it has used and what it is doing; when it ends, `v` on the card
shows the report the model got. The info panel lists the turn's subagents under AGENTS. Nothing here can start,
approve or stop one: genai does that (Ctrl-C in its pane stops one, as for a sandboxed run).
"""

from . import Feature

SHOWN = 6                  # subagents listed in the info panel


class Agents(Feature):
    name = "agents"
    needs = "agents"
    cant = "run subagents"
    events = ("agent",)

    def __init__(self):
        self.rows = {}     # subagent id -> its latest agent event, in the order they started

    def event(self, m, ev):
        aid = ev.get("id")
        if not isinstance(aid, str):
            return
        self.rows[aid] = ev
        while len(self.rows) > SHOWN:
            self.rows.pop(next(iter(self.rows)))
        card = m.cards.get(ev.get("proposal"))
        if card and ev.get("state") == "running":
            card.detail = self.progress(ev)
            card.changed()

    def progress(self, ev):
        c = self.core
        return (f"step {self.num(ev.get('step'))}/{self.num(ev.get('steps'))} · {self.num(ev.get('tokens'))} tokens"
                f" · {c.clean(str(ev.get('doing', '')))}")

    @staticmethod
    def num(v):
        return v if type(v) is int else "?"

    def snapshot(self, m, view):
        view["agents"] = [{"id": self.core.clean(aid), "state": self.core.clean(str(ev.get("state", "?"))),
                           "detail": self.progress(ev)} for aid, ev in self.rows.items()]


FEATURE = Agents
