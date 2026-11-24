"""/raw and /system: what was sent to the endpoint and what came back, and the system prompt, in a pager."""

import curses
import json
import os
import shutil
import tempfile

from . import Feature


def pretty(text):
    """JSON text indented for reading, or the text as it is if it isn't JSON."""
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return text


def safe_lines(text):
    """Text for the pager: control and invisible characters shown as escapes, as everywhere else."""
    return "\n".join(clean(ln) for ln in text.split("\n"))


class Raw(Feature):
    name = "raw"
    commands = {
        "/raw": "[N|all]: what was sent to the endpoint and what came back, for the last turn, turn N, or all kept",
        "/system": "the system prompt and tool definitions sent with every request",
    }
    events = ("trace", "system")
    requests = {"raw": ("menu",), "system": ("menu",)}
    actions = ("show",)

    def command(self, m, cmd, arg):
        if m.busy and not m.pending:
            m.note(f"{cmd} has to wait until genai finishes this reply", self.core.W)
        elif cmd == "/system":
            m.send({"type": "system"})
        else:
            self.raw(m, arg)

    def raw(self, m, arg):
        """Ask genai for a turn's exchanges with the endpoint: the latest turn that had any, turn N, or all."""
        if arg == "all":
            m.send({"type": "trace", "from": 1, "to": 1 << 30})
            return
        if arg and not arg.isdigit():
            m.note("usage: /raw, /raw N (a turn) or /raw all", self.core.R)
            return
        turn = int(arg) if arg else max(m.exchanges, default=None)
        if m.busy and m.pending and not arg:
            m.send({"type": "trace"})               # this turn hasn't ended: its latest exchange
        elif turn is None or turn not in m.exchanges:
            m.note("nothing has been sent to the endpoint yet" if turn is None else
                   f"turn {turn} has no exchanges with the endpoint", self.core.R)
        else:
            lo, hi = m.exchanges[turn]
            m.send({"type": "trace", "from": lo, "to": hi})

    def event(self, m, ev):
        m.actions.append(("show", ev))

    def request(self, m, role, msg):
        if msg.get("type") == "system":
            m.command("/system")
        elif type(msg.get("turn")) is int or msg.get("turn") in ("all", "last"):
            m.command(f"/raw {'' if msg['turn'] == 'last' else msg['turn']}".strip())

    def action(self, app, action):
        self.show(app, action[1])

    def show(self, app, ev):
        """A trace or system answer from genai, full screen in a pager. The file holds the whole conversation, so
        it goes in genai-tui's private directory (or a private one of its own) and is gone when the pager closes."""
        text = self.render_system(ev) if ev.get("type") == "system" else self.render_trace(ev, app.m.exchanges)
        where = app.state_dir or tempfile.mkdtemp(prefix="genai-tui-raw-")
        fd, path = tempfile.mkstemp(prefix="raw-", suffix=".txt", dir=where)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            curses.endwin()
            pager = ["less", "-+F", "-i", path] if shutil.which("less") else ["more", path]
            app.run_here(pager, full=True)
        finally:
            os.unlink(path)
            if not app.state_dir:
                os.rmdir(where)
            app.scr.refresh()

    def turn_menu(self, m, turn):
        this = [("What was sent and received for this turn", "r", "raw", str(turn))] if turn in m.exchanges else []
        return this + [
            ("What was sent and received last", "l", "raw", "last"),
            ("Everything genai still has (last 30)", "a", "raw", "all"),
            ("The system prompt and tools", "s", "system")]

    def safe_lines(self, text):
        """Text for the pager: control and invisible characters shown as escapes, as everywhere else."""
        return "\n".join(self.core.clean(ln) for ln in text.split("\n"))

    def render_trace(self, ev, turns):
        """A trace event as text for a pager. turns maps a turn to its [first, last] exchange, to name them."""
        exs = ev.get("exchanges") or []
        named = {n: t for t, (a, b) in turns.items() for n in range(a, b + 1)}
        kept = ev.get("kept")
        out = ["genai: what was sent to the endpoint, and what came back",
               "Exactly as sent and received, except your API key and the endpoint's own header values. Request bodies",
               "are indented here for reading; they were sent as compact JSON. q leaves, / searches.", ""]
        if not exs:
            out.append("genai no longer has these exchanges" + (f" (it keeps the last 30: {kept[0]}–{kept[1]})" if kept
                                                                else ""))
        for ex in exs:
            n = ex.get("n")
            status = ex.get("status", "?") + (f": {ex['error']}" if ex.get("error") else "")
            took = f" · {ex['ms'] / 1000:.1f} s" if isinstance(ex.get("ms"), int) else ""
            turn = f" · turn {named[n]}" if n in named else ""
            out += ["═" * 78, f"Exchange {n}{turn} · {ex.get('time', '')}{took} · {status}", "═" * 78, "",
                    f"{ex.get('method', 'POST')} {ex.get('url', '')}"]
            out += [f"{k}: {v}" for k, v in (ex.get("headers") or {}).items()]
            body = ex.get("request") or ""
            out += ["", f"── Request body ({len(body.encode())} bytes) " + "─" * 40, pretty(body), ""]
            resp = ex.get("response") or []
            how = "streamed, " + f"{len(resp)} lines" if ex.get("stream") else "one body"
            out += [f"── Response: HTTP {ex.get('http', '?')}, {how} " + "─" * 40]
            out += resp if ex.get("stream") else [pretty("".join(resp))]
            if ex.get("message") is not None:
                out += ["", "── The reply as genai put it together " + "─" * 40,
                        json.dumps(ex["message"], indent=2, ensure_ascii=False)]
            if ex.get("usage"):
                out += ["", f"usage: {json.dumps(ex['usage'])}"]
            out.append("")
        return self.safe_lines("\n".join(out))

    def render_system(self, ev):
        out = ["genai: the system prompt and tools sent with every request",
               "Your conversation follows the system prompt in each request. q leaves, / searches.", "",
               f"model: {ev.get('model', '?')}", f"params: {json.dumps(ev.get('params') or {})}", "",
               "── System prompt " + "─" * 60, ev.get("system", ""), "",
               "── Tools " + "─" * 68, json.dumps(ev.get("tools") or [], indent=2, ensure_ascii=False)]
        return self.safe_lines("\n".join(out))


FEATURE = Raw
