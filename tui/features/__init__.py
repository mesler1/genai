"""hoplon-tui's features: each in a module of its own, registered here.

The conversation pane (conversation.py) is the core: the conversation, the cards, the approval and run flows. What it
can do without is a Feature, so it can be changed, or switched off, in one place. A feature gets the Model (to add
notes, send messages to hoplon, queue actions) or the App (to do something on screen) and nothing more, and the same
checks as everywhere apply: it can't answer a question or approve anything.

Switching one off: {"disable": ["copy", "model"]} in $XDG_CONFIG_HOME/hoplon/tui.json (default
~/.config/hoplon/tui.json), or HOPLON_TUI_DISABLE=copy,model in the environment. A disabled feature has no command, no
help line and no menu item, its hoplon events are ignored, and the socket refuses the requests it would have taken.
"""

import importlib
import json
import os

BUILTIN = ("model", "copy", "decline", "raw", "ssh", "files", "sandbox", "edits", "plugins", "work", "fixtures", "leftovers")


class Feature:
    """One feature of the conversation pane, in a module of its own. A subclass sets what it takes part in and
    overrides the methods it needs; the rest do nothing. One is made per conversation pane, so it may keep its own
    state. The core's helpers (clean, Note, menu_item ...) are on self.core: a feature can't import conversation.py,
    which may be running as the program itself."""

    name = ""          # how config refers to it
    commands = {}      # "/cmd": "help text" for /help, in order; the core's come first
    to_hoplon = ()      # of those, the commands that must wait while hoplon is replying (like the core's to_hoplon)
    needs = None       # a feature hoplon must list in hello["features"], or the command says "this hoplon can't ..."
    cant = ""          # what completes "this hoplon can't ..." when needs is missing
    events = ()        # hoplon event types it handles
    requests = {}      # socket request types it handles -> the roles that may send them, e.g. {"copy": ("menu",)}
    actions = ()       # kinds of Model.actions tuples (action[0]) it performs in the App
    card_actions = ()  # words of `menu ACTION ID` (a command card's tmux menu) it handles
    core = None        # the conversation module, set by load()

    def command(self, m, cmd, arg):
        """One of its commands, as typed (a menu sends the command a user would have typed). m is the Model."""

    def event(self, m, ev):
        """A hoplon event of one of its types."""

    def request(self, m, role, msg):
        """A socket request of one of its types, from a pane in one of the roles it allows (None where the Model is
        driven directly). msg is untrusted: check it, then act as the user's command would."""

    def error(self, m, ev):
        """A hoplon error event. Return True only for the failure of one of its own requests, which it has then dealt
        with; any other is left to the core."""
        return False

    def action(self, app, action):
        """One of its Model.actions, to be done on screen. app is the App."""

    def card_action(self, app, what, card):
        """A choice from a command card's menu that is one of its card_actions. card is a command proposal."""

    def help(self, m):
        """More /help lines, after the commands."""

    def poll(self, app):
        """Once per App loop iteration, for things that have to be watched (such as hoplon-ssh's handshake)."""

    def run_started(self, m, card):
        """A command card was typed into the shell pane to run."""

    def exited(self, m):
        """hoplon has exited."""

    def snapshot(self, m, view):
        """May add to or adjust the info panel's view dict before it is sent."""

    def card_menu(self, m, card):
        """Extra items for a command card's tmux menu: [(name, key, action_word)]. The word comes back as
        `menu ACTION ID`, and must be one of its card_actions."""
        return []

    def turn_menu(self, m, turn):
        """Extra items for the conversation's right-click menu: [(name, key, request_args...)]. The args are fixed
        words or numbers, run as `request.py ARGS`. turn is 0 where the click wasn't on a turn."""
        return []


def disabled_names(environ=None):
    """The features switched off, and a warning to show once if the config file couldn't be used. No file is fine; a
    malformed one is ignored (every feature stays on), so a typo can't stop the program."""
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "hoplon", "tui.json")
    names, warning = set(), None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        off = data.get("disable", []) if isinstance(data, dict) else None
        if not isinstance(off, list) or not all(isinstance(n, str) for n in off):
            raise ValueError('it should look like {"disable": ["copy"]}')
        names.update(off)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        warning = f"{path} was ignored, so every feature is on: {e}"
    names.update(n.strip() for n in (env.get("HOPLON_TUI_DISABLE") or "").split(",") if n.strip())
    return names, warning


def load(disabled=(), core=None):
    """The built-in features, in order, except those named in disabled."""
    out = []
    for name in BUILTIN:
        if name not in disabled:
            feature = importlib.import_module(f"{__name__}.{name}").FEATURE()
            feature.core = core
            out.append(feature)
    return out
