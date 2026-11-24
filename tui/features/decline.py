"""/decline: say no to a proposed command without running it, and tell the model with the next message."""

from . import Feature


class Decline(Feature):
    name = "decline"
    commands = {"/decline": "[ID]: decline a proposed command (the latest by default); the model hears with your next "
                            "message"}
    card_actions = ("decline",)

    def command(self, m, cmd, arg):
        self.decline(m, arg)

    def decline(self, m, arg):
        """/decline [ID], or Decline in a command's menu: the card says so, and the model hears with the next
        message, so it doesn't assume the command ran."""
        cards = [c for c in m.cards.values() if c.ev.get("kind") == "command" and c.state == "shown"]
        card = m.cards.get(arg) if arg else (cards[-1] if cards else None)
        if not card or card.ev.get("kind") != "command":
            m.note("no command proposal to decline" + (f" with id {arg}" if arg else ""), self.core.R)
        elif card.state != "shown":
            m.note(f"{card.id} is {card.state}; only a command that hasn't run can be declined", self.core.R)
        else:
            card.state, card.detail = "declined", "you declined it; the model hears with your next message"
            card.changed()
            m.next_attachments.append({"type": "attach", "label": f"declined {card.id}", "decline": card.id,
                                       "text": f"I declined the command you proposed as {card.id}, and did not "
                                               f"run it:\n{card.ev.get('command', '')}"})
            m.number_commands()
            m.note(f"{card.id} declined", self.core.Q)

    def run_started(self, m, card):
        """A declined command is run after all: the model is no longer told it was declined."""
        m.next_attachments = [a for a in m.next_attachments if a.get("decline") != card.id]

    def card_menu(self, m, card):
        return [("Decline it (the model hears with your next message)", "d", "decline")]

    def card_action(self, app, what, card):
        self.decline(app.m, card.id)


FEATURE = Decline
