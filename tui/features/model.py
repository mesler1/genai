"""/model: pick the model from those the server offers, as a drop-down, or switch to one by name."""

import re

from . import Feature, shown_model

MODEL_MENU = 20               # models in the /model drop-down; the rest by name
# A model name from the server, as hoplon accepts it. Checked again here before one goes into a tmux menu, whose
# item names are formats: no '#', ',', '}', quotes or spaces can get through.
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+=-]{0,199}")
MODEL_ID_CHARS = re.compile(r"[^A-Za-z0-9._:/@+=()-]")     # what an alias's label may show beside it in the menu
ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}")         # an endpoint's alias, as hoplon accepts it


class ModelPicker(Feature):
    name = "model"
    commands = {"/model": "[NAME]: pick the model from those the server offers (a drop-down), or switch to NAME"}
    to_hoplon = ("/model",)
    needs = "models"
    cant = "list or switch models"
    events = ("models", "model")
    requests = {"model": ("menu",), "models": ("menu",)}
    actions = ("model_menu",)

    def __init__(self):
        self.models = []           # what /model offers, as hoplon last listed them: the endpoint's aliases first
        self.labels = []           # how each is shown
        self.picking = False       # /model asked for that list, to show it as a drop-down

    def command(self, m, cmd, arg):
        if arg:
            m.send({"type": "model", "model": arg})
        else:
            self.picking = True
            m.send({"type": "models"})

    def event(self, m, ev):
        clean = self.core.clean
        if ev.get("type") == "models":
            aliases = ev.get("aliases") if isinstance(ev.get("aliases"), dict) else {}
            aliases = {k: v for k, v in sorted(aliases.items()) if isinstance(k, str) and ALIAS.fullmatch(k)
                       and isinstance(v, str) and MODEL_ID.fullmatch(v)}
            ids = [x["id"] for x in ev.get("models") or []
                   if isinstance(x, dict) and isinstance(x.get("id"), str) and MODEL_ID.fullmatch(x["id"])]
            self.models = list(aliases) + ids
            self.labels = [shown_model(v, k) for k, v in aliases.items()] + ids
            ep = (m.hello or {}).get("endpoint", {})
            current = ep.get("alias") or ep.get("model")
            if not self.models:
                m.note("the server lists no models hoplon can use", self.core.R)
            elif self.picking and m.shell:
                m.actions.append(("model_menu",))
            else:
                for name, label in zip(self.models, self.labels):
                    m.note(("* " if name == current else "  ") + clean(label))
                m.note("/model NAME switches")
            self.picking = False
        else:
            ep = ev.get("endpoint")
            if isinstance(ep, dict) and m.hello is not None:
                m.hello = {**m.hello, "endpoint": ep}
            ctx = ev.get("context") or {}
            if isinstance(ctx.get("used"), int) and isinstance(ctx.get("limit"), int):
                m.context = (ctx["used"], ctx["limit"])
            ep = ep if isinstance(ep, dict) else {}
            alias = ep.get("alias") if isinstance(ep.get("alias"), str) else None
            m.note(f"model: {clean(shown_model(str(ep.get('model', '?')), alias))} from your next message", self.core.D)

    def request(self, m, role, msg):
        if msg.get("type") == "models":            # a click on the header's model
            m.command("/model")
        elif type(msg.get("n")) is int and 0 <= msg["n"] < len(self.models):
            m.command(f"/model {self.models[msg['n']]}")      # the menu sent a position; the name is ours

    def action(self, app, action):
        if app.shell:
            ep = (app.m.hello or {}).get("endpoint", {})
            self.menu(app.shell, self.models, ep.get("alias") or ep.get("model"), self.labels)

    def menu(self, shell, models, current, labels=None):
        """The /model drop-down: tmux's menu, in the middle of the screen. Names are checked against MODEL_ID first,
        and each item sends back only its position. An alias shows its model beside it, cut to safe characters."""
        labels = labels or models
        items = []
        for i, mid in enumerate(models[:MODEL_MENU]):
            if MODEL_ID.fullmatch(mid):
                label = labels[i] if i < len(labels) else mid
                label = mid if label == mid else f"{mid}  {MODEL_ID_CHARS.sub('', label[len(mid):])}"
                items += [("• " if mid == current else "  ") + label, str((i + 1) % 10) if i < 10 else "",
                          self.core.menu_item("model", str(i))]
        if len(models) > MODEL_MENU:
            items += ["", f"-  {len(models) - MODEL_MENU} more: /model NAME", "", ""]
        items += ["", "Cancel", "q", ""]
        shell.popup_menu("-x", "C", "-y", "C", "-T", " model · ↑↓ Enter ", *items)


FEATURE = ModelPicker
