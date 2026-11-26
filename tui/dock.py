"""The right column's tabs: the info panel and any plugin panes take turns in one spot.

Only the active tab's pane is on screen; the others are parked in windows of their own, where they keep
running and keep their scrollback. Switching swaps the chosen pane into the active one's place. With more
than one tab, the active pane's top border shows the tab strip (the option @hoplon_tabs, which hoplon-tui's
pane-border-format prefers to the pane's title); a click on that border opens a menu of the tabs.
"""
import re

import palette

NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")    # "info", or a plugin's name: shown in tmux formats, so checked


class Dock:
    def __init__(self, tmux, me, environ=None):
        self.tmux, self.me, self.environ = tmux, me, environ
        self.tabs = []        # [name, pane id, width], in the order they were added
        self.active = None    # the name of the tab that is on screen, or would be when the column is shown

    # -- what is where

    def tab(self, name):
        return next((t for t in self.tabs if t[0] == name), None)

    def names(self):
        return [t[0] for t in self.tabs]

    def window(self, pane):
        return self.tmux("display", "-p", "-t", pane, "#{window_id}").strip()

    def alive(self, pane):
        return self.tmux("display", "-p", "-t", pane, "#{pane_id}").strip() == pane

    def prune(self):
        """Forget tabs whose pane has gone (a plugin's pane program ended)."""
        self.tabs = [t for t in self.tabs if self.alive(t[1])]
        if self.active not in self.names():
            self.active = self.tabs[0][0] if self.tabs else None

    def shown(self):
        t = self.tab(self.active) if self.active else None
        return bool(t) and self.window(t[1]) == self.window(self.me)

    # -- changing it

    def add(self, name, pane, width, select=True):
        """A new tab for a pane that already exists (anywhere); shown at once unless select is False."""
        if not NAME.fullmatch(name) or self.tab(name):
            return False
        self.tabs.append([name, pane, width])
        if self.active is None:
            self.active = name
        if select:
            self.select(name)
        else:
            self.titles()
        return True

    def select(self, name):
        """Put that tab on screen, in place of the one there, or show the column with it."""
        self.prune()
        t = self.tab(name)
        if not t:
            return False
        cur = self.tab(self.active)
        if cur and cur[1] != t[1] and self.shown():
            self.tmux("swap-pane", "-d", "-s", t[1], "-t", cur[1])
            self.tmux("resize-pane", "-t", t[1], "-x", str(t[2]))
        elif not (cur and cur[1] == t[1] and self.shown()):
            self.tmux("join-pane", "-d", "-h", "-f", "-l", str(t[2]), "-s", t[1], "-t", self.me)
        self.active = name
        self.titles()
        return True

    def hide(self):
        if self.shown():
            t = self.tab(self.active)
            self.tmux("break-pane", "-d", "-s", t[1], "-n", f"tab {t[0]}")

    def show(self):
        self.prune()
        return self.select(self.active) if self.active else False

    def toggle(self):
        if self.shown():
            self.hide()
        else:
            self.show()

    def next(self):
        """The tab after the active one, round to the first."""
        self.prune()
        if not self.tabs:
            return False
        names = self.names()
        i = names.index(self.active) if self.active in names else -1
        return self.select(names[(i + 1) % len(names)])

    def remove(self, name):
        """Take a tab out of the column; its pane stays parked, running. Another tab takes its place on screen."""
        t = self.tab(name)
        if not t:
            return
        if name == self.active and self.shown():
            others = [x[0] for x in self.tabs if x[0] != name]
            if others:
                self.select(others[0])
            else:
                self.hide()
        self.tabs.remove(t)
        self.tmux("set", "-pu", "-t", t[1], "@hoplon_tabs")
        if self.active == name:
            self.active = self.tabs[0][0] if self.tabs else None
        self.titles()

    # -- how it looks

    def strip(self):
        """The tab strip for the active pane's border: the active tab bronze, the others muted."""
        T = lambda *a, **k: palette.tmux(*a, environ=self.environ, **k)
        out = []
        for name in self.names():
            if name == self.active:
                on = T('iron', 'bronze', bold=True) if palette.enabled(self.environ) else "#[reverse]"
                out.append(f"{on} {name} #[default]")
            else:
                out.append(f"{T('muted')} {name} #[default]")
        return " ".join(out) + f" {T('dim')}click: tabs#[default]"

    def titles(self):
        """The strip on the active tab's pane when there are two or more; the plain title otherwise."""
        for name, pane, _ in self.tabs:
            if name == self.active and len(self.tabs) > 1:
                self.tmux("set", "-p", "-t", pane, "@hoplon_tabs", self.strip())
            else:
                self.tmux("set", "-pu", "-t", pane, "@hoplon_tabs")
