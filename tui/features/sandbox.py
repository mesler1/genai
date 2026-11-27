"""/sandbox: where hoplon runs, where your commands run, and the model's sandbox, in full."""

from . import Feature


class SandboxInfo(Feature):
    name = "sandbox"
    commands = {"/sandbox": "where hoplon and your commands run, and the model's sandbox: its image, network, limits, "
                            "and the run going on now"}

    def command(self, m, cmd, arg):
        c = self.core
        clean, note = c.clean, m.note
        h = m.hello
        if h is None:
            note("hoplon hasn't said hello yet", c.W)
            return
        where = c.hoplon_where(h)
        if where:
            note(where[0], where[1])
        sb = h.get("sandbox")
        if sb:
            name = clean(sb.get("name", "?") if isinstance(sb, dict) else sb)
            note(f"hoplon: in the {name} sandbox (it says so in its hello: an indicator, not a guarantee)", c.D)
        else:
            note("hoplon: not sandboxed: it runs directly on " + ("that host" if m.remote() else "this machine")
                 + ", as you")
            if not m.remote():
                note("  sandbox/run hoplon runs it in a podman sandbox; sandbox/run shell opens a shell in that one")
        note("your commands: " + (f"run {clean(m.shell_where)} when you approve them" if m.shell_where
                                  else "shown for you to run in your own shell"))
        r = m.runner
        if not r:
            note("the model's sandbox: none (the profile has no 'sandbox')", c.Q)
        elif r.get("problem"):
            note(f"the model's sandbox can't run here: {clean(r['problem'])}", c.R)
        else:
            parts = [clean(r.get("engine") or "podman"), (f"image {clean(r['image'])}" if r.get("image") else "this host's OS (/usr and /etc, read-only)"),
                     "network ON (each run asks)" if r.get("network") else "offline"]
            for key, label in (("timeout", "s time limit"), ("memory", " memory"), ("pids", " processes")):
                if r.get(key) is not None:
                    parts.append(f"{clean(r[key])}{label}")
            note("the model's sandbox: " + ", ".join(parts), c.W if r.get("network") else c.D)
            note("  it runs on a throwaway copy of the project, with secret-looking files hidden"
                 + ("" if m.coding else "; it is used in coding mode (/coding on)"))
            note("  /network on|off switches its network for this session")
        card = m.running_card()
        if card:
            note(f"RUNNING {clean(card.id)} for {card.elapsed()}s: "
                 + ("Ctrl-C or /stop stops it" if card.stoppable else
                    "it can't be stopped from here in remote mode yet; it ends at its time limit"), c.W)


FEATURE = SandboxInfo
