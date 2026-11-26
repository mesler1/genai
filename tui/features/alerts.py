"""/alerts: whether the terminal bell rings when a decision starts waiting: once per card, so you notice from another
window (most terminals turn the bell into a notification or a highlighted window). tmux's status line shows
"⏸ waiting for you" either way; that never makes a sound. (hoplon's notify hook doesn't run under hoplon-tui.)

Off from the start: {"alerts": false} in ~/.config/hoplon/tui.json, or HOPLON_TUI_ALERTS=off, which wins.
{"disable": ["alerts"]} silences it too, and takes the /alerts command away."""

from . import Feature, tui_setting


class Alerts(Feature):
    name = "alerts"
    commands = {"/alerts": "[on|off]: ring the terminal bell when a decision starts waiting"}

    def __init__(self):
        self.on = tui_setting("alerts", True, bool, env="HOPLON_TUI_ALERTS")
        self.rung = set()           # cards the bell has rung for: once each, as it starts to wait

    def command(self, m, cmd, arg):
        if arg not in ("", "on", "off"):
            m.note("usage: /alerts, /alerts on or /alerts off", self.core.R)
            return
        if arg:
            self.on = arg == "on"
        m.note("alerts: on: the bell rings when a decision starts waiting" if self.on else
               "alerts: off: no bell; the header still shows ⏸ waiting for you"
               + ("" if arg else " ({\"alerts\": true} in tui.json, or /alerts on, turns it back on)"))

    def poll(self, app):
        fresh = {c.id for c in app.m.pending} - self.rung
        if fresh:
            self.rung |= fresh
            if self.on:
                app.bell()


FEATURE = Alerts
