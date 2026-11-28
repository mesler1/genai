# hoplon_core/front.py: the front ends: they show proposals and ask the gate; nothing the model proposes runs here.
#
# PlainUI, TtyUI and JsonUI present proposals; Repl is the terminal REPL and run_json the JSON Lines protocol;
# FRONT_COMMANDS is the table of commands both have. Also the editor review, the pager, and main.
#
# Uses: base, commands, sandbox, config, leftovers, gate, turn

# --------------------------------------------------------------------------
# Front ends

class PlainUI:
    """No terminal, not JSON: print text and command proposals; perform nothing."""
    performs = False

    def __init__(self, out=sys.stdout):
        self.out = out
        self._open = False
        self._flagged = False
        self.queue = []          # the REPL's attachments waiting for the next message: (label, text)

    def text(self, chunk):
        # Escaping is per character, so a chunk boundary can't split or hide anything.
        disp, flagged = exact(chunk)
        self._flagged = self._flagged or flagged
        self.out.write(disp)
        self.out.flush()
        self._open = True

    def text_end(self):
        if self._open:
            self.out.write("\n")
            self.out.flush()
            if self._flagged:      # stderr, so `hoplon ... > file` keeps only the reply
                eprint("  [!] hidden or control characters were escaped above")
            self._open = False
            self._flagged = False

    def notice(self, msg):
        eprint(f"hoplon: {one_line(msg)[0]}")

    def progress(self, report):
        pass                    # the REPL shows the reply's text as it comes; hoplon-tui shows the rest

    def generated(self, stats):
        pass

    def skills_changed(self, rows):
        on = [one_line(r["name"])[0] for r in rows if r.get("active")]
        self.notice(f"skills on: {', '.join(on) or 'none'}")

    def context(self, used, limit):
        pass

    def show_samples(self, p):
        f = p.fields
        self.out.write(f"\n[samples requested {p.id}] {one_line(f['what'])[0]}\n"
                       f"    files: {', '.join(one_line(x)[0] for x in f['files'])}\n"
                       + (f"    suggested: {show_exact(f['suggest'])}\n" if f["suggest"] else ""))
        self.out.flush()

    def show_command(self, p):
        self.out.write(f"\n[proposed command {p.id}] {one_line(p.fields['reason'])[0]}\n"
                       f"    {show_exact(p.fields['command'])}\n")
        note = non_ascii_line(p.fields["command"])
        if note:
            self.out.write(note + "\n")
        self.out.flush()

    def show_auto(self, p):
        pass

    def edits(self, info):
        pass

    def decide(self, pending):
        for p in pending:
            p.decision = "decline" if p.kind != "external" else "declined"
            what = p.fields.get("path") or p.fields.get("tool") or p.fields.get("goal") or p.fields.get("argv")
            self.notice(f"{p.kind} proposal {p.id} ({one_line(str(what))[0]}) declined: no terminal")

    def result(self, p, status, note, **extra):
        pass

    def plan(self, plan):
        pass

    def coding(self, on, source, override=None):
        self.notice(f"coding mode is {'on' if on else 'off'} ({source or 'as before the work session'})")

    def work(self, ev):
        pass


def place(kind, fields):
    """Where a proposal acts, as hoplon-tui's cards say it: ▲ live, ⊡ isolated, an arrow where it crosses to the
    user's side."""
    if kind in ("edit", "write"):
        return "⊡ the copy" if fields.get("work") or fields.get("in_copy") else "⊡→▲ your files"
    return {"command": "⊡→▲ your shell", "trusted": "▲ runs live", "sandbox": "⊡ a throwaway run"}.get(kind, "")


def tagged(kind, fields):
    t = place(kind, fields)
    return f"{t} · " if t else ""


class TtyUI(PlainUI):
    """A human at a terminal: ask about each file proposal."""
    performs = True

    def __init__(self, out=sys.stdout):
        super().__init__(out)

    def show_samples(self, p):
        f = p.fields
        self.out.write(f"\n\033[1m▲→⊡ samples requested [{p.id}]\033[0m: {one_line(f['reason'])[0]}\n"
                       f"{margin(f['what'])}\n"
                       f"    files your tests will read in /fixtures: {', '.join(one_line(x)[0] for x in f['files'])}\n"
                       + (f"    you could make them with:  {show_exact(f['suggest'])}\n" if f["suggest"] else "")
                       + "\nMake them in your shell (scrub anything private), then :fixtures add FILE… and "
                         ":fixtures use. The model never runs anything for this.\n")
        self.out.flush()

    def show_command(self, p):
        cmd = show_exact(p.fields["command"])
        note = non_ascii_line(p.fields["command"])
        self.out.write(f"\n\033[1m{tagged(p.kind, p.fields)}Proposed command [{p.id}]\033[0m: {one_line(p.fields['reason'])[0]}\n\n    {cmd}\n"
                       + (note + "\n" if note else "") +
                       "\nPaste it into your shell, or type :run to review and run it.\n")
        self.out.flush()

    def show_auto(self, p):
        if p.kind == "trusted":
            f = p.fields
            args = ", ".join(f"{k}={v}" for k, v in f["params"].items())
            who = (f" AS {one_line(f['as_user'])[0].upper()}" if f.get("as_user") else "") + \
                (f", connecting to {one_line(f['network'])[0]}" if f.get("network") else "")
            eprint(f"  \u21b3 [{p.id}] {tagged(p.kind, f)}{one_line(f['set'])[0]}.{one_line(f['command'])[0]}({show_exact(args)}){who}: "
                   f"{show_exact(f['argv'])}")
        elif p.kind == "sandbox":
            eprint(f"  [{p.id}] {tagged(p.kind, p.fields)}sandbox, offline: {one_line(p.fields['reason'])[0]}\n    $ {show_exact(p.fields['command'])}")
        elif p.kind == "search":
            eprint(f"  (coding mode) search {show_exact(p.fields['pattern'])} in {one_line(p.fields['path'])[0]}")
        elif p.kind == "finish":
            eprint(f"  [{p.id}] finish item {p.fields['n']}: {one_line(p.fields['summary'])[0]}")
        elif p.kind == "retry":
            eprint(f"  [{p.id}] try item {p.fields['n']} again: {one_line(p.fields['hint'])[0]}")
        elif p.fields.get("work"):
            what = "edit" if p.kind == "edit" else "write"
            eprint(f"  [{p.id}] {tagged(p.kind, p.fields)}(work session) {what} {one_line(p.fields['path'])[0]}")
        elif p.fields.get("approved_file"):
            what = "Edit" if p.kind == "edit" else "Overwrite"
            self.out.write(f"\n\033[1m[{p.id}] {tagged(p.kind, p.fields)}{p.kind}\033[0m — "
                           f"{one_line(p.fields.get('reason', ''))[0]}\n"
                           f"{what} {one_line(p.fields['path'])[0]} (without asking: you approved an edit to it "
                           f"earlier; :edits ask stops that):\n{margin(p.fields.get('diff') or '(no changes)')}\n")
            self.out.flush()
        else:
            eprint(f"  (coding mode) {p.kind} {one_line(p.fields['path'])[0]}")

    def decide(self, pending):
        for p in pending:
            f = p.fields
            path = one_line(str(f.get("path", "")))[0]
            if p.kind == "read":
                size = "missing" if f.get("size") is None else f"{f['size']} bytes"
                head = f"Read {path} ({size})?"
            elif p.kind == "list":
                head = f"List directory {path}?"
            elif p.kind == "write":
                if f["exists"] and f.get("undiffable"):
                    mode = f.get("mode")
                    keep = f"keeps mode {mode} (executable)" if mode and int(mode, 8) & 0o111 else "keeps its mode"
                    head = (f"Overwrite {path}:\n  the existing file can't be diffed: {one_line(f['undiffable'])[0]}\n"
                            f"  it will be replaced entirely, and {keep}\n"
                            f"  the new content, {f['lines']} lines:\n{margin(f['content'])}")
                elif f["exists"]:
                    head = f"Overwrite {path}:\n{margin(f.get('diff') or '(no changes)')}"
                else:
                    head = f"New file {path}, {f['lines']} lines:\n{margin(f['content'])}"
            elif p.kind == "edit":
                head = f"Edit {path}:\n{margin(f['diff'])}"
            elif p.kind == "sandbox":
                head = (f"Run in the sandbox, with the network ON:\n    $ {show_exact(f['command'])}\n"
                        "It works on a throwaway copy of the project, but it could send what it reads anywhere.")
            elif p.kind == "trusted":
                head = (f"Run {one_line(f['set'])[0]}.{one_line(f['command'])[0]} (a scoped command that "
                        + ("its set says only reads" if f.get("access") == "read" else "changes something")
                        + (f", AS {one_line(f['as_user'])[0].upper()}" if f.get("as_user") else "")
                        + (f", CONNECTING TO {one_line(f['network'])[0]}" if f.get("network") else "")
                        + f"):\n    $ {show_exact(f['argv'])}")
            elif p.kind == "plan":
                self.out.write(f"\n\033[1m[{p.id}] plan\033[0m — {one_line(f['goal'])[0]}\n{margin(plan_steps(f['steps']))}\n"
                               "Planned reads won't ask again; a planned command proposed exactly as shown is "
                               "marked as approved; edits still ask.\n")
                self.out.flush()
                p.decision, p.approve = pick_steps(p, f["steps"])
                continue
            elif p.kind == "work_plan":
                items = "\n".join(f"{n:>2}. {t}" for n, t in enumerate(f["items"], 1))
                self.out.write(f"\n\033[1m[{p.id}] work plan\033[0m — {one_line(f['goal'])[0]}\n{margin(items)}\n"
                               "Once approved, the model works through the items on its branch without asking, "
                               "committing each one.\n")
                self.out.flush()
                p.decision = "accept" if ask_yes(f"Approve {p.id} and start?") else "decline"
                continue
            elif p.kind == "skills":
                rows = "\n".join(f"{one_line(n)[0]}: {one_line(d)[0]}" if d else one_line(n)[0]
                                 for n, d in zip(f["names"], f["about"]))
                head = f"Switch on these skills?\n{margin(rows)}"
            else:
                head = f"Call {p.kind}?"
            self.out.write(f"\n\033[1m[{p.id}] {tagged(p.kind, f)}{p.kind}\033[0m — {one_line(f.get('reason', ''))[0]}\n"
                           f"{head}\n")
            self.out.flush()
            p.decision = "accept" if ask_yes(f"Accept {p.id}?") else "decline"
            if p.decision == "accept" and f.get("secret") and ask_yes(
                    "Does it hold no secrets after all? (it won't ask again until the file changes)"):
                f["_not_secret"] = True

    def work(self, ev):
        state = ev["state"]
        if state == "started":
            test = f"test command: {show_exact(ev['test'])}" if ev["test"] else "no test command"
            eprint(f"hoplon: work session on branch {ev['branch']}, a copy of your last commit ({ev['base'][:7]}). "
                   f"Your own files are not touched.\n  limits: {ev['steps_per_item']} model steps per plan item, "
                   f"{ev['time_limit'] // 60} min without you; {test}. :work end stops it."
                   + (f"\n  plans with {one_line(ev['planner']['model'])[0]} on endpoint "
                      f"{one_line(ev['planner']['endpoint'])[0]}" if ev.get("planner") else ""))
        elif state == "item" and ev["current"]:
            it = ev["items"][ev["current"] - 1]
            eprint(f"hoplon: next, item {it['n']} of {len(ev['items'])}: {one_line(it['title'])[0]}")
        elif state == "context":
            who = f"item {ev['item']}" if ev["role"] == "item" else f"the planner, to review item {ev['item']}"
            eprint(f"hoplon: a fresh context for {who} ({ev['why']})")
        elif state == "ended":
            eprint(f"hoplon: work session {'done' if ev['why'] == 'done' else 'ended'}.")
            if not ev["branch"]:
                eprint(f"  the work is not on a branch; it is in the copy at {one_line(ev['path'])[0]}" if ev["path"]
                       else "  nothing was changed, so no branch was made")
                return
            if ev["unfinished"]:
                eprint(f"  changes not yet finished were committed as {ev['unfinished']}")
            eprint(f"  branch {ev['branch']}:\n" + margin("\n".join(ev["commits"]) + "\n" + ev["stat"]))
            eprint("  review and merge it yourself:\n" + "\n".join(f"    {c}" for c in ev["commands"]))

    def result(self, p, status, note, output=None, **extra):
        if output:
            lines = output.rstrip("\n").splitlines()
            if len(lines) > TTY_OUTPUT_LINES:
                eprint(f"    | ... {len(lines) - TTY_OUTPUT_LINES} earlier lines (the model sees them)")
            eprint(margin("\n".join(lines[-TTY_OUTPUT_LINES:])))
        eprint(f"  [{p.id}] {status}: {one_line(note)[0]}")


def plan_steps(steps):
    return "\n".join(f"{st['n']:>2}. " + (f"run   {st['command']}" if st["kind"] == "command" else
                                          f"{st['kind']:5} {st['path']}") + f"\n      {st['why']}" for st in steps)


def pick_steps(p, steps):
    """Ask which steps of a plan to approve: (decision, approved step numbers or None for all)."""
    try:
        ans = input(f"Approve {p.id}? [a]ll, n[one], or step numbers such as 1,2,4: ").strip().lower()
    except EOFError:
        return "decline", None
    if ans in ("a", "all", "y", "yes"):
        return "accept", None
    nums = {int(x) for x in re.findall(r"[0-9]+", ans)} & {st["n"] for st in steps}
    return ("accept", sorted(nums)) if nums and re.fullmatch(r"[0-9 ,]+", ans) else ("decline", None)


def choosable_models(engine):
    """The server's models that a name can choose: ([(id, context or None)], error text or None). A model named
    like one of the endpoint's aliases is left out, as that name chooses the alias's model instead."""
    try:
        listed = engine.client.model_list()
    except HoplonError as e:
        return [], str(e)
    return [m for m in listed if m[0] not in engine.endpoint.aliases], None


def plan_choice(p, msg):
    """A wrapper's choice of steps (approve: [numbers], default all) and edited commands (edits: {number: command}).
    False if either is malformed."""
    numbers = {st["n"]: st for st in p.fields["steps"]}
    approve, edits = msg.get("approve"), msg.get("edits", {})
    if approve is not None and (not isinstance(approve, list) or not approve
                                or not all(type(n) is int and n in numbers for n in approve)):
        return False
    if not isinstance(edits, dict):
        return False
    p.approve, p.edits = (sorted(set(approve)) if approve is not None else None), {}
    for key, cmd in edits.items():
        n = int(key) if str(key).isdecimal() else None
        if n not in numbers or numbers[n]["kind"] != "command" or not isinstance(cmd, str) or not cmd.strip():
            return False
        p.edits[n] = cmd
    return True


HALT = object()     # put in JsonUI.lines to wake a read() when a turn is asked to stop


class JsonUI:
    """JSON Lines over stdio for wrappers and TUIs."""
    performs = True

    def __init__(self, inp=sys.stdin, out=sys.stdout):
        self.inp, self.out = inp, out
        self.queue = []          # attachments waiting for the next user message
        self.engine = None
        self.lines = None        # stdin's lines, read by a thread of their own once reading starts
        self.busy = False        # a turn is going on (run_json sets it, under lock)
        self.starting_work = False   # that turn is starting a work session (it may not be on yet)
        self.lock = threading.Lock()
        self.again = []          # messages read while a stop came in, handled next

    def emit(self, ev):
        self.out.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.out.flush()

    def error(self, message, code="error", **extra):
        ev = {"type": "error", "code": code, "message": message}
        ev.update(extra)
        self.emit(ev)

    def text(self, chunk):
        self.emit({"type": "text", "text": chunk})

    def text_end(self):
        pass

    def progress(self, report):
        self.emit(dict(report, type="progress"))

    def skills_changed(self, rows):
        self.emit({"type": "skills", "skills": rows})

    def generated(self, stats):
        self.emit({"type": "generated", "tokens": stats["tokens"], "estimated": stats["estimated"],
                   "seconds": round(stats["seconds"], 2)})

    def notice(self, msg):
        self.emit({"type": "notice", "message": msg})

    def context(self, used, limit):
        """Before each model request, so a front end's gauge keeps up during a long turn or a work session."""
        self.emit({"type": "context", "used": used, "limit": limit})

    def show_samples(self, p):
        self.emit(p.event())

    def show_command(self, p):
        ev = p.event()
        disp, flagged = exact(p.fields["command"])
        if flagged:
            ev["display"], ev["flagged"] = disp, True
        if non_ascii_chars(p.fields["command"]):
            ev["non_ascii"] = [f"U+{ord(c):04X}" for c in non_ascii_chars(p.fields["command"])]
        self.emit(ev)

    def show_auto(self, p):
        self.emit(p.event())

    def edits(self, info):
        self.emit({"type": "edits", **info})

    def coding(self, on, source, override=None):
        self.emit({"type": "coding", "on": on, "coding_from": source if on else None,
                   "coding_override": override if on else None})

    def work(self, ev):
        self.emit({"type": "work", **ev})

    def plan(self, plan):
        self.emit({"type": "plan", "plan": None if plan is None else {
            "id": plan["id"], "goal": plan["goal"],
            "steps": [{k: v for k, v in st.items() if not k.startswith("_")} for st in plan["steps"]]}})

    def decide(self, pending):
        """Emit every blocking proposal, then wait for decisions in any order.
        No answer, EOF, or an unknown ID means decline. A decision must echo the proposal's digest; one that
        doesn't is refused and the proposal still waits (see digest)."""
        for p in pending:
            ev = p.event()
            for key in ("content", "diff", "path", "undiffable"):
                if isinstance(ev.get(key), str):
                    disp, flagged = exact(ev[key])
                    if flagged:
                        ev.setdefault("flagged", []).append(key)
            if p.kind == "plan" and any(exact(st.get("command") or st.get("path") or "")[1] for st in ev["steps"]):
                ev.setdefault("flagged", []).append("steps")
            if p.kind == "work_plan" and any(exact(t)[1] for t in ev["items"] + [ev["goal"]]):
                ev.setdefault("flagged", []).append("items")
            ev["digest"] = p.digest = digest(ev)
            self.emit(ev)
        waiting = {p.id: p for p in pending}
        while waiting:
            msg = self.read()
            if msg is None or (self.engine and self.engine.halt):
                if msg is not None and msg.get("type") != "halt":
                    self.again.append(msg)      # not lost: it is handled after the turn
                break
            t = msg.get("type")
            if t == "halt":
                continue
            pid = msg.get("id")
            if t in ("decision", "result") and pid in waiting and msg.get("digest") != waiting[pid].digest:
                self.error(f"{pid}: that answer is for a proposal other than the one waiting (its digest is "
                           f"{'missing' if 'digest' not in msg else 'different'}); it still waits", "digest_mismatch")
            elif t == "decision" and pid in waiting and waiting[pid].kind != "external":
                p = waiting.pop(pid)
                p.decision = msg.get("decision") if msg.get("decision") in ("accept", "decline") else "decline"
                if p.decision == "accept" and msg.get("not_secret") is True and p.fields.get("secret"):
                    p.fields["_not_secret"] = True
                if p.kind == "plan" and p.decision == "accept" and not plan_choice(p, msg):
                    p.decision = "decline"
                    self.error(f"{pid}: approve must list step numbers, and edits map a command step's number to "
                               "its new command; the plan was declined", "bad_message")
            elif t == "result" and pid in waiting and waiting[pid].kind == "external":
                p = waiting.pop(pid)
                p.decision = msg.get("status") if msg.get("status") in ("ok", "declined", "error") else "declined"
                p.result = msg.get("output", "")
            elif t in ("decision", "result"):
                self.error(f"no pending proposal {pid!r} of that kind", "unknown_id")
            elif t == "attach":
                self.queue_attach(msg)
            elif t in ("trace", "system"):
                inspect(self.engine, self, msg)
            elif t == "stop":
                self.error("nothing is running in the sandbox", "not_running")
            else:
                self.error(f"'{t}' is not allowed while proposals are pending", "busy")
        for p in waiting.values():
            p.decision = "declined" if p.kind == "external" else "decline"
            p.result = ""

    def queue_attach(self, msg):
        text = msg.get("text")
        if not isinstance(text, str):
            self.error("attach needs 'text'", "bad_message")
            return
        red, counts = redact(text, self.engine.secrets() if self.engine else ())
        red = clip(red)
        label = str(msg.get("label") or "attachment")
        self.queue.append((label, red, msg.get("ref")))
        self.emit({"type": "attached", "label": label, "chars": len(red), "redacted": counts})

    def result(self, p, status, note, **extra):
        ev = {"type": "result", "id": p.id, "status": status, "detail": note}
        ev.update(extra)
        self.emit(ev)

    def halt(self, why):
        """Ask the turn going on to stop (why: "stop"), or to stop and end the work session after it ("end"): any
        sandboxed run ends now, a model reply as its next words arrive, and a wait for decisions declines what is
        left. False if no turn is going on, so the message waits for read() instead."""
        with self.lock:
            if not self.busy:
                return False
            e = self.engine
            if why == "end" and not (e and (e.work or self.starting_work)):
                return False
            if e and e.halt != "end":
                e.halt = why
            Sandbox.stop.set()      # a run going on ends, and so does one being set up (cleared when a turn starts)
        self.lines.put(HALT)        # wakes a wait for decisions
        return True

    def listen(self):
        """Read stdin on a thread of its own, so stop and work_end are seen while hoplon is busy: they stop the turn
        (see halt). Every other line waits in order for read(). At the end of input, a turn still going on stops
        and its work session ends: nobody is left to answer it."""
        for line in iter(self.inp.readline, ""):
            try:
                msg = json.loads(line)
            except ValueError:
                msg = None
            t = msg.get("type") if isinstance(msg, dict) else None
            if t == "stop" and (self.halt("stop") or Sandbox.running):
                Sandbox.stop.set()
                continue
            if t == "work_end" and self.halt("end"):
                continue
            self.lines.put(line)
        self.halt("end")
        self.lines.put("")

    def start_listening(self):
        if self.lines is None:
            self.lines = queue.Queue()
            threading.Thread(target=self.listen, daemon=True).start()

    def read(self):
        if self.again:
            return self.again.pop(0)
        self.start_listening()
        while True:
            line = self.lines.get()
            if line is HALT:
                return {"type": "halt"}
            if not line:
                self.lines.put("")      # stdin has ended: so does every later read
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                    raise ValueError("a message must be an object with a 'type'")
                return msg
            except ValueError as e:
                self.error(f"bad message: {e}", "bad_message")


# --------------------------------------------------------------------------
# Editor review

EDITOR_HEADER = """# {title}
# Lines starting with '#' are removed. What is left is {what}.
# Save an empty file to {cancel}.
"""


def editor_cmd():
    ed = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    argv = shlex.split(ed)
    base = os.path.basename(argv[0])
    if base in ("vi", "vim", "nvim", "gvim"):
        argv += ["-n", "-i", "NONE", "--cmd", "set nobackup nowritebackup noundofile"] if base != "vi" else ["-n"]
    return argv


def edit_text(text, title, what, cancel):
    """Open text in $EDITOR in a private temp file; return what the user saved, minus # lines."""
    fd, path = tempfile.mkstemp(prefix="hoplon-", suffix=".txt")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(EDITOR_HEADER.format(title=title, what=what, cancel=cancel) + text)
        rc = subprocess.call(editor_cmd() + [path])
        if rc != 0:
            eprint(f"hoplon: editor exited with {rc}; cancelled")
            return ""
        with open(path, encoding="utf-8", errors="replace") as f:
            kept = [ln for ln in f.read().splitlines() if not ln.startswith("#")]
        return "\n".join(kept).strip("\n")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def review_output(engine, source, lead, text, title):
    """Redact, clip and open output in the editor, with `lead` lines above it in the same buffer.
    Returns exactly what the user saved, which is exactly what is sent, inside a marked block (shared_block)."""
    red, counts = redact(text, getattr(engine, "secrets", lambda: ())())
    buf, flagged = exact("\n".join(lead) + "\n```\n" + clip(red) + "\n```")
    if counts:
        title += f"\n# Masked in the output: {redaction_note(counts)}"
    if flagged:
        title += ("\n# Hidden or control characters were found; they are shown as \\x.. or \\u.... escapes"
                  " and are sent that way.")
    title += ("\n# EVERYTHING below is sent to the model as saved, the command included."
              "\n# Remove anything you don't want it to see, such as a secret in the command.")
    kept = edit_text(buf, title, "sent to the model exactly as saved, marked as shared", "send nothing")
    if not kept.strip():
        print("Nothing sent.")
        return None
    engine.share(source, kept)
    return kept


# --------------------------------------------------------------------------
# Commands both front ends have
#
# One row per command that the REPL (:NAME) and the JSON protocol ({"type": NAME}) both offer. A row turns REPL
# words into the JSON message's fields, checks the fields once for both, does it, and gives the JSON reply; the REPL
# prints the same reply as text. Where hoplon-tui asks the user itself (deleting samples, cleaning leftovers,
# approving samples), the REPL's `ask` step asks and fills in what the wrapper would have sent.

class Front:
    """Who is asking: the REPL (source ":NAME") or the wrapper, whether this is remote mode, and the problems an
    action reported while still replying (error(text, kind)), which the front end shows with the reply."""
    def __init__(self, repl, name="", remote=False):
        self.repl, self.remote, self.problems = repl, remote, []
        self.source = f":{name}" if repl else "wrapper"

    def error(self, text, kind):
        self.problems.append((text, kind))


class FrontCommand:
    """name: the REPL's :NAME and the JSON message's type. words: REPL text after :NAME -> the message's fields, or
    None if it isn't one of the forms. check: fields -> True if well formed. act: (engine, fields, front) -> the
    reply's fields, or None for no reply. show: (engine, fields, reply) -> the REPL's lines. usage: the REPL's
    forms; bad: the JSON error when check fails. reply: the reply's type, when it isn't the name. ask: REPL only,
    (engine, fields) -> fields, or None to stop: it asks what hoplon-tui asks before sending. partial: the REPL
    shows the reply even when the action reported a problem, and both front ends show the problem after the reply,
    not before."""
    def __init__(self, name, words, check, act, show, usage, bad="", reply="", ask=None, partial=False):
        self.name, self.words, self.check, self.act, self.show = name, words, check, act, show
        self.usage, self.bad, self.reply, self.ask, self.partial = usage, bad, reply or name, ask, partial


def on_off(text, empty=None):
    return {"on": {"on": True}, "off": {"on": False}, "": empty}.get(text)


def context_now(e):
    return {"used": e.used(), "limit": e.active()[0].context_limit}


def coding_act(e, m, front):
    try:
        e.set_coding(m["on"], front.source)
        e.session.save()
    except HoplonError as err:
        front.error(str(err), "coding_refused")
    return {"on": e.session.coding, "coding_from": e.coding_from,
            "coding_override": e.coding_override if e.session.coding else None}


def edits_act(e, m, front):
    if m.get("reset"):
        e.reset_approved_files(":edits reset" if front.repl else front.source)
    if m.get("mode") is not None:
        e.set_edits(m["mode"], front.source)
    return e.edits_info()


def edits_show(e, m, r):
    return [f"edits: {r['mode']}" + ("" if r["mode"] == "ask" else "; applied without asking: "
                                     + (", ".join(one_line(f)[0] for f in r["files"]) or "none yet"))]


def network_act(e, m, front):
    try:
        if m.get("on") is not None:
            e.set_network(m["on"], front.source)
    except HoplonError as err:
        front.error(str(err), err.kind)
    return {"runner": e.box.info() if e.box else None, "available": e.can_run()}


def box_line(e):
    """The profile's sandbox in a line: what it runs on, its network, and whether the model runs commands there."""
    b = e.box
    problem = b.problem()
    if problem:
        return f"the profile's sandbox can't run here: {problem}"
    state = "the model runs commands there itself, without asking" if e.can_run() else "it is used in coding mode"
    return (f"podman sandbox ({one_line(box_name(b.conf))[0]}), network {'ON: each run asks' if b.network else 'off'}"
            f"; {state}")


def fixtures_words(text):
    words = shlex.split(text) if text else []
    what = words[0] if words else None
    if what not in (None, "add", "use", "drop", "clear"):
        return None
    m = {"action": what} if what else {}
    if what == "add":
        m.update(paths=words[1:], cwd=os.getcwd())
    elif len(words) > 1:
        return None
    return m


def fixtures_check(m):
    action, paths, digest = m.get("action"), m.get("paths") or [], m.get("digest")
    return not (action not in (None, "add", "use", "drop", "clear") or not isinstance(paths, list)
                or not all(isinstance(x, str) and x for x in paths) or (action == "add") != bool(paths)
                or (action == "use" and not isinstance(digest, str)) or not isinstance(m.get("cwd", ""), str))


def fixtures_ask(e, m):
    if m.get("action") == "clear" and not ask_yes("Delete the samples folder and everything in it?"):
        return None
    if m.get("action") == "use":
        info = e.fixtures_info()
        print("\n".join(fixtures_lines(info)))
        if not info["files"] or any(r["flag"] for r in info["files"]) or not ask_yes(FIXTURES_ASK):
            print("Not approved." if info["files"] else "Nothing to approve.")
            return None
        m = dict(m, digest=info["digest"])
    return m


def fixtures_act(e, m, front):
    action = m.get("action")
    try:
        if action:
            return e.fixtures_action(action, front.source, m.get("paths") or [], m.get("cwd"), m.get("digest"))
    except (HoplonError, OSError) as err:
        front.error(f"fixtures: {err}", getattr(err, "kind", None) or "fixtures")
    return e.fixtures_info()


def fixtures_lines(info):
    out = [f"samples folder: {info['dir']}" + (f"  ({info['problem']})" if info["problem"] else "")]
    for r in info["files"]:
        out.append(f"  {'✗' if r['flag'] else '·'} {show_exact(r['name'])}  {r['size']} bytes"
                   + (f"  — can't be used: {r['flag']}" if r["flag"] else ""))
    if not info["files"]:
        out.append("  empty: :fixtures add FILE… copies files in")
    out.append("approved: the model's sandboxed runs see them read-only at /fixtures" if info["approved"] else
               "not approved: :fixtures use checks them and asks")
    return out


NOT_SECRET_LINES = 12      # lines shown per file before :not_secret asks


def not_secret_words(text):
    """:not_secret PATH…: paths from where you are, globs expanded here (each file is still shown and asked about)."""
    out = []
    for w in shlex.split(text) if text else []:
        out += sorted(glob.glob(w, recursive=True)) if glob.has_magic(w) else [w]
    return {"marks": [{"path": p, "sha256": ""} for p in out], "cwd": os.getcwd()} if out else None


def not_secret_check(m):
    marks = m.get("marks")
    return (isinstance(marks, list) and bool(marks) and len(marks) <= SANDBOX_MASKS
            and all(isinstance(x, dict) and isinstance(x.get("path"), str) and isinstance(x.get("sha256"), str)
                    for x in marks))


def not_secret_ask(e, m):
    """Show the lines that look secret in each file, and ask about each; only the files you say y to are sent, each
    with the hash of the text you saw."""
    marks = []
    for p in (x["path"] for x in m["marks"]):
        rel = os.path.relpath(os.path.join(m["cwd"], p), e.wd.root)
        if rel.startswith(".."):
            print(f"{show_exact(p)}: not in the project, skipped")
            continue
        try:
            _, size, secret, counts, _ = e.view(rel)            # the same checks, and an audit record
            text = e.wd.read(rel)                               # your own terminal: the real lines, to judge them
        except (ValueError, OSError) as err:
            print(f"{show_exact(rel)}: {err}, skipped")
            continue
        if not secret:
            print(f"{show_exact(rel)}: hoplon doesn't take it for a secret, skipped")
            continue
        lines = text.splitlines()
        found = [(i, ln) for i, ln in enumerate(lines, 1) if looks_secret(ln)] \
            or list(enumerate(lines, 1))                        # a secret over several lines, such as a key
        print(f"\n{show_exact(rel)} · {size} bytes · " + ", ".join(f"{n} {k}" for k, n in counts.items()))
        for i, ln in found[:NOT_SECRET_LINES]:
            print(f"  {i:>5} | {show_exact(ln.strip())[:200]}")
        if len(found) > NOT_SECRET_LINES:
            print(f"        … and {len(found) - NOT_SECRET_LINES} more lines")
        if ask_yes("Holds no real secrets? (it will ask again if the file changes)"):
            marks.append({"path": rel, "sha256": text_digest(text)})
    if not marks:
        print("Nothing marked.")
        return None
    return {"marks": marks}


def not_secret_act(e, m, front):
    marked = []
    for x in m["marks"]:
        try:
            marked.append(e.mark_not_secret(x["path"], x["sha256"], front.source))
        except HoplonError as err:
            front.error(str(err), err.kind)
    return {"marked": marked}


def not_secret_show(e, m, r):
    return [f"{show_exact(x['path'])}: marked as holding no secrets, until it changes" for x in r["marked"]]


def leftovers_ask(e, m):
    if m.get("action") != "clean":
        return m
    items = leftovers(e.cfg, [e.wd.root])
    print("\n".join(leftover_lines(items, ":leftovers clean")))
    safe = [i["id"] for i in items if i["safe"]]
    if not safe:
        return None
    if not ask_yes(f"Remove the {len(safe)} stale one{'s' if len(safe) != 1 else ''} marked ✗ (not those marked !)?"):
        print("Nothing removed.")
        return None
    return dict(m, ids=safe)


def leftovers_act(e, m, front):
    done = clean_leftovers(e.cfg, m["ids"], e.audit, front.source) if m.get("action") else {}
    return {"items": leftovers(e.cfg, [e.wd.root]), **done}


def leftovers_show(e, m, r):
    if not m.get("action"):
        return leftover_lines(r["items"], ":leftovers clean")
    return [f"Removed {len(r['removed'])}." + "".join(f"\n  couldn't remove {f['id']}: {show_exact(f['error'])}"
                                                     for f in r["failed"])]


def named_on_off(text):
    name, _, state = text.partition(" ")
    got = on_off(state.strip())
    return dict(got, name=name) if name and got else None


def switch(what, key, sent, turn, rows):
    """Switch a skill or command set on or off, and reply with the listing. In remote mode only what the user's own
    machine sent can be used: anything else is refused here, before the engine."""
    def act(e, m, front):
        if front.remote and m["name"] not in e.offered[key]:
            front.error(f"{what} '{one_line(m['name'])[0]}' wasn't sent from your machine: in remote mode, only the "
                        f"{what}s hoplon-tui sent ({sent}) can be used", "remote")
        else:
            try:
                turn(e, m["name"], m["on"])
            except HoplonError as err:
                front.error(str(err), err.kind)
        return {key: rows(e)}
    return act


def turn_skill(e, name, on):
    e.set_skill(name, on)
    e.session.save()


def command_set_lines(rows):
    out = []
    for r in rows:
        out.append(f"{'*' if r['active'] else ' '} {one_line(r['name'])[0]:20} {r['status']:10} "
                   f"{one_line(r['description'])[0]}")
        path = one_line(shlex.quote(r["path"]))[0]
        out.append("    " + (path if r["blessed"] else f"to bless: hoplon --bless {path}"))
    return out or ["no command sets found"]


def models_act(e, m, front):
    listed, err = choosable_models(e)
    reply = None
    if listed or e.endpoint.aliases or not err:      # the aliases, even when the server lists nothing
        reply = {"current": e.endpoint.model, "aliases": e.endpoint.aliases,
                 "models": [{"id": mid, "context": n} for mid, n in listed]}
    if err:
        front.error(f"could not list the server's models: {err}" if front.repl else err, "models")
    return reply


def models_show(e, m, r):
    ep, out = e.endpoint, []
    for k in sorted(r["aliases"]):
        out.append(f" {'*' if r['aliases'][k] == r['current'] else ' '} {k}  ({r['aliases'][k]}, an alias)")
    for row in r["models"]:
        mark = "*" if row["id"] == r["current"] and not ep.alias_of() else " "
        out.append(f" {mark} {row['id']}" + (f"  ({row['context']} tokens)" if row["context"] else ""))
    return out


def model_act(e, m, front):
    e.set_model(m["model"], front.source)
    return {"endpoint": e.endpoint.info(), "context": {"used": e.used(), "limit": e.endpoint.context_limit}}


def compact_act(e, m, front):
    edit = (lambda s: edit_text(s, "Compaction summary", "what the model keeps of older turns", "drop the summary")) \
        if m.get("edit") and front.repl else None
    r = e.compact(edit_summary=edit)
    return {"pruned": r["pruned"], "before": r["before"], "after": r["after"], "summary": r["summary"]}


def compact_show(e, m, r):
    out = [f"pruned {r['pruned']} items: {r['before']} -> {r['after']} tokens (estimated)"]
    if r["summary"]:
        out.append(f"\nSummary of older turns (sent instead of them):\n{show_exact(r['summary'])}\n")
    return out


def nothing(text):
    return {} if not text else None


FRONT_COMMANDS = {c.name: c for c in [
    FrontCommand("coding", on_off, lambda m: isinstance(m.get("on"), bool), coding_act,
                 lambda e, m, r: [f"coding mode is {'ON' if r['on'] else 'off'}"],
                 ":coding on|off", "coding needs 'on': true or false"),
    FrontCommand("edits", lambda t: {"": {}, "reset": {"reset": True}}.get(t, {"mode": t} if t in EDITS_MODES else None),
                 lambda m: (m.get("mode") is None or m["mode"] in EDITS_MODES)
                 and (m.get("reset") is None or isinstance(m["reset"], bool)),
                 edits_act, edits_show, ":edits, :edits ask, :edits approved-files or :edits reset",
                 "edits takes 'mode': \"ask\" or \"approved-files\", and/or 'reset': true"),
    FrontCommand("network", lambda t: on_off(t, {}), lambda m: m.get("on") is None or isinstance(m["on"], bool),
                 network_act, lambda e, m, r: [box_line(e) if e.box else "no sandbox: the profile has no 'sandbox'"],
                 ":network, :network on or :network off", "network needs 'on': true or false, or nothing to ask"),
    FrontCommand("fixtures", fixtures_words, fixtures_check, fixtures_act, lambda e, m, r: fixtures_lines(r),
                 ":fixtures, :fixtures add FILE…, :fixtures use, :fixtures drop or :fixtures clear",
                 "fixtures takes 'action' (add with 'paths' and 'cwd', use with the listing's 'digest', drop or "
                 "clear), or nothing to list", ask=fixtures_ask),
    FrontCommand("leftovers", lambda t: {"": {}, "clean": {"action": "clean"}}.get(t),
                 lambda m: m.get("action") in (None, "clean") and isinstance(m.get("ids") or [], list)
                 and all(isinstance(x, str) and x for x in m.get("ids") or [])
                 and (m.get("action") == "clean") == bool(m.get("ids")),
                 leftovers_act, leftovers_show, ":leftovers or :leftovers clean",
                 "leftovers takes nothing to list, or 'action': 'clean' with the listing's 'ids'", ask=leftovers_ask),
    FrontCommand("not_secret", not_secret_words, not_secret_check, not_secret_act, not_secret_show,
                 ":not_secret PATH…", "not_secret needs 'marks': a list of {'path', 'sha256'} from file events",
                 ask=not_secret_ask, partial=True),
    FrontCommand("skills", nothing, lambda m: True, lambda e, m, front: {"skills": e.skill_rows()},
                 lambda e, m, r: skill_lines(r["skills"], mark=True) or ["no skills found"], ":skills", ""),
    FrontCommand("skill", named_on_off, lambda m: isinstance(m.get("name"), str) and isinstance(m.get("on"), bool),
                 switch("skill", "skills", "the profile's, default_skills and -k", turn_skill,
                        lambda e: e.skill_rows()),
                 lambda e, m, r: [f"skill {one_line(m['name'])[0]} is {'on' if m['on'] else 'off'} from your next "
                                  "message"],
                 ":skill NAME on|off", "skill needs 'name' and 'on': true or false", reply="skills"),
    FrontCommand("command_sets", nothing, lambda m: True,
                 lambda e, m, front: {"command_sets": e.command_set_rows()},
                 lambda e, m, r: command_set_lines(r["command_sets"]), ":command_sets", ""),
    FrontCommand("command_set", named_on_off,
                 lambda m: isinstance(m.get("name"), str) and isinstance(m.get("on"), bool),
                 switch("command set", "command_sets", "the profile's and default_commands",
                        lambda e, name, on: e.set_command_set(name, on), lambda e: e.command_set_rows()),
                 lambda e, m, r: [f"command set {one_line(m['name'])[0]} is {'on' if m['on'] else 'off'} from your "
                                  "next message"],
                 ":command_set NAME on|off", "command_set needs 'name' and 'on': true or false",
                 reply="command_sets"),
    FrontCommand("models", nothing, lambda m: True, models_act, models_show, ":models", partial=True),
    FrontCommand("model", lambda t: {"model": t} if t else None, lambda m: isinstance(m.get("model"), str),
                 model_act, lambda e, m, r: [f"model: {one_line(e.endpoint.shown_model())[0]} from your next message"],
                 ":model NAME (:model alone shows it)", "model needs 'model': a name from the models message"),
    FrontCommand("clear", nothing, lambda m: True,
                 lambda e, m, front: {"dropped": e.clear(), "context": context_now(e)},
                 lambda e, m, r: [f"context cleared: the model starts over ({r['dropped']} messages dropped; "
                                  f"{'the saved session keeps them' if e.session.path else 'they were not saved'})"],
                 ":clear", "", reply="cleared"),
    FrontCommand("compact", lambda t: {"": {}, "-e": {"edit": True}}.get(t), lambda m: True, compact_act,
                 compact_show, ":compact or :compact -e", "", reply="compacted"),
]}


def json_command(c, engine, ui, msg, remote):
    """One of the shared commands, from the JSON protocol: check it, do it, reply."""
    if not c.check(msg):
        ui.error(c.bad, "bad_message")
        return
    front = Front(False, remote=remote)
    try:
        reply = c.act(engine, msg, front)
    except HoplonError as err:
        ui.error(str(err), err.kind)
        return
    for text, kind in front.problems if not c.partial else ():
        ui.error(text, kind)
    if reply is not None:
        ui.emit({"type": c.reply, **reply})
    for text, kind in front.problems if c.partial else ():
        ui.error(text, kind)


def repl_command(c, engine, text):
    """One of the shared commands, from the REPL: the same check and action as JSON, printed for a person. A problem
    is printed instead of the state, unless the row shows both."""
    m = c.words(text)
    if m is not None and c.ask:
        m = c.ask(engine, m)
        if m is None:
            return
    if m is None or not c.check(m):
        print(f"usage: {c.usage}")
        return
    front = Front(True, c.name)
    reply = c.act(engine, m, front)
    if reply is not None and (c.partial or not front.problems):
        print("\n".join(c.show(engine, m, reply)))
    for text, _ in front.problems:
        print(f"hoplon: {exact(text)[0]}", file=sys.stderr if c.partial else sys.stdout)


# --------------------------------------------------------------------------
# REPL

HELP = """Type a message; a line with only '..' sends it (Ctrl-D also sends).
Commands (on an empty buffer):
  :run [ID]       review, confirm and run a proposed command (latest by default)
  :sh             open a subshell; :capture on|off to share its output afterwards
  :attach PATH    attach a file to your next message
  :coding on|off  let the model read and list files here without asking
  :network [on|off]  the sandbox's network, for this session (on: each sandboxed run asks)
  :fixtures [add FILE…|use|drop|clear]  real samples, read-only at /fixtures in sandboxed runs once you approve them
  :not_secret PATH…  shows each file that looks secret and asks: y marks it as holding none, so the sandbox
                  stops hiding it and reads stop asking, until the file changes
  :alerts [on|off]  whether the notify hook runs when hoplon waits for you
  :leftovers [clean]  what hoplon made for sandboxes and jobs that is still there, what is stale, how to remove it
  :edits [ask|approved-files|reset]  approved-files: a further edit to a file you approved applies without asking
  :plan [clear]   show the approved plan and how far it has got, or drop it
  :work TASK      a work session: the model plans, then codes and tests on a git branch of its own without
                  asking; you review and merge the branch. --steps N, --minutes M first override the limits.
                  :work shows how far it has got, :work end stops it
  :compact [-e]   prune old file contents and output; summarize if still large
  :context        show how full the context is
  :raw [N|all]    what was sent to the endpoint and what came back: the last turn, turn N, or all kept
  :system         the system prompt and tool definitions sent with every request
  :models         list the models the server offers   :model NAME   use one of them from now on
  :clear          start over: the model forgets this conversation (the saved session keeps it)
  :skills         list skills, marking the ones in use
  :skill NAME on|off  use a blessed skill from the next message on, or stop using it
  :command_sets   list command sets, marking the ones in use
  :command_set NAME on|off  offer a blessed command set's scoped commands from the next message on, or stop
  :bless PATH     bless a skill, endpoint or profile
  :sandbox        where hoplon and your commands run: in a sandbox or on this host
  :help           this text            :quit         leave"""


HOOK_BANNER = "hook: notify (runs {} when hoplon waits for you)"


def pretty_json(text):
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return text


def trace_text(exchanges, named, kept):
    """Exchanges with the endpoint as text for reading, as hoplon-tui's /raw shows them. named maps an exchange
    number to its turn."""
    out = ["hoplon: what was sent to the endpoint, and what came back",
           "Exactly as sent and received, except your API key and the endpoint's own header values. Request bodies",
           "are indented here for reading; they were sent as compact JSON.", ""]
    if not exchanges:
        out.append("hoplon no longer has these exchanges" + (f" (it keeps the last {TRACE_KEEP}: {kept[0]}–{kept[1]})"
                                                            if kept else ""))
    for ex in exchanges:
        n = ex["n"]
        status = ex.get("status", "?") + (f": {ex['error']}" if ex.get("error") else "")
        took = f" · {ex['ms'] / 1000:.1f} s" if isinstance(ex.get("ms"), int) else ""
        turn = f" · turn {named[n]}" if n in named else ""
        out += ["═" * 78, f"Exchange {n}{turn} · {ex.get('time', '')}{took} · {status}", "═" * 78, "",
                f"{ex.get('method', 'POST')} {ex.get('url', '')}"]
        out += [f"{k}: {v}" for k, v in (ex.get("headers") or {}).items()]
        body = ex.get("request") or ""
        out += ["", f"── Request body ({len(body.encode())} bytes) " + "─" * 40, pretty_json(body), ""]
        resp = ex.get("response") or []
        how = f"streamed, {len(resp)} lines" if ex.get("stream") else "one body"
        out += [f"── Response: HTTP {ex.get('http', '?')}, {how} " + "─" * 40]
        out += resp if ex.get("stream") else [pretty_json("".join(resp))]
        if ex.get("message") is not None:
            out += ["", "── The reply as hoplon put it together " + "─" * 40,
                    json.dumps(ex["message"], indent=2, ensure_ascii=False)]
        if ex.get("usage"):
            out += ["", f"usage: {json.dumps(ex['usage'])}"]
        out.append("")
    return "\n".join(out)


def page(text):
    """Show text in $PAGER (less by default) when stdout is a terminal, or print it. Control characters are
    escaped line by line first. The text holds the conversation, so its file is private and removed after."""
    text = "\n".join(exact(ln)[0] for ln in text.split("\n")) + "\n"
    argv = shlex.split(os.environ.get("PAGER") or "less")
    if not (sys.stdout.isatty() and argv and shutil.which(argv[0])):
        sys.stdout.write(text)
        return
    fd, path = tempfile.mkstemp(prefix="hoplon-raw-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        subprocess.run(argv + [path])
    finally:
        os.unlink(path)


def sandbox_work_host():
    """The host path of /work, as sandbox/run reported it. Display only, so it is escaped."""
    return one_line(os.environ.get("HOPLON_SANDBOX_WORK_HOST", "").strip())[0]


class Repl:
    def __init__(self, engine):
        self.e = engine
        self.capture = False
        self.turns = []          # for :raw: each turn's [first, last] exchange with the endpoint, or None

    @property
    def attachments(self):
        """Attachments waiting for the next message. They live on the UI, as hoplon-tui's do, so a work session with
        the planner elsewhere sets them aside the same way."""
        return self.e.ui.queue

    @attachments.setter
    def attachments(self, value):
        self.e.ui.queue = value

    def in_sandbox(self):
        """Is hoplon itself running in a sandbox? Then the prompt and banner say so."""
        return bool(getattr(self.e, "sandbox", None))

    def banner(self):
        p = self.e.endpoint
        print(f"hoplon {VERSION} — {one_line(p.name)[0]}: {one_line(p.shown_model())[0]} at {one_line(p.base_url)[0]}")
        if self.in_sandbox():
            name = one_line(self.e.sandbox["name"])[0]
            host = sandbox_work_host()
            line = (f"IN THE {name.upper()} SANDBOX: /work" + (f" = {host}" if host else "") +
                    ", no host home (:sandbox for details)")
            print(f"\033[1m{line}\033[0m" if sys.stdout.isatty() else line)
        if os.geteuid() == 0:
            print(f"  [!] running as root on {one_line(socket.gethostname())[0]}: "
                  "every command it proposes would run as root")
        if self.e.profile:
            d = self.e.profile.description
            print(f"profile {one_line(self.e.profile.name)[0]}" + (f": {one_line(d)[0]}" if d else ""))
        s = self.e.session
        if s.id:
            print(f"session {s.id}" + (" (resumed)" if s.resumed else ""))
        if s.coding:
            print(f"coding mode is ON ({self.e.coding_from or 'session'}): "
                  "the model can read and list files here without asking")
            if self.e.coding_override:
                print(f"  [!] this is {self.e.coding_override}; --coding-anywhere allowed it")
        print(f"sandbox: commands you :run or :sh execute {where_runs(self.e.sandbox)}")
        if self.e.box:
            print("  " + box_line(self.e))
        if self.e.notifier:
            print("  " + HOOK_BANNER.format(short_path(self.e.notifier.path)))
        print("Type :help for commands. A line with only '..' sends.")

    def do_sandbox(self):
        sb = self.e.sandbox
        print(f"hoplon runs {where_runs(sb)}")
        if sb:
            host = sandbox_work_host()
            print(f"  working directory: {one_line(os.getcwd())[0]}"
                  + (f" (/work is {host} on the host, writable)" if host else ""))
            print("  the repo is at /opt/hoplon, read-only; the host's /usr and /etc are read-only; "
                  "nothing else from your home")
            print(f"  config: {one_line(short_path(self.e.cfg.home))[0]} (the sandbox's own, not your real one)")
            print("  changes outside /work and the config directory are discarded when it exits")
            try:
                host_name = urllib.parse.urlsplit(self.e.endpoint.base_url).hostname
                socket.getaddrinfo(host_name, None)
                print(f"  network: the endpoint's host {one_line(host_name)[0]} resolves, so the network looks available")
            except (OSError, ValueError, TypeError):
                print("  network: the endpoint's host does not resolve here, so the network looks off")
            print("  sandbox/run shell opens a shell in it (from the host)")
        else:
            print("  sandbox/run hoplon runs hoplon in a podman sandbox instead (see docs/sandbox.md)")
        if self.e.box:
            print("  profile sandbox: " + box_line(self.e))

    def run(self):
        try:
            import readline  # noqa: F401  (line editing and in-memory history)
        except ImportError:
            pass
        self.banner()
        buf = []
        while True:
            try:
                mark = "(sandbox) " if self.in_sandbox() else ""
                line = input(mark + ("> " if not buf else ". "))
            except EOFError:
                print()
                if buf:
                    self.send("\n".join(buf))
                    buf = []
                    continue
                return EXIT_OK
            except KeyboardInterrupt:
                print("^C (buffer cleared)" if buf else "^C (:quit to leave)")
                buf = []
                continue
            if not buf and line.startswith(":"):
                if self.command(line) == "quit":
                    return EXIT_OK
                continue
            if line.strip() == "..":
                if buf:
                    self.send("\n".join(buf))
                buf = []
                continue
            buf.append(line)

    def send(self, text, attachments=None):
        att = attachments if attachments is not None else self.attachments
        self.attachments = []
        before = getattr(getattr(self.e, "client", None), "exchanges", 0)
        try:
            self.e.user_turn(text, att)
        except KeyboardInterrupt:
            self.e.session.close_dangling("stopped: the user interrupted before deciding")
            print("\n(interrupted)")
        except HoplonError as err:
            print(f"hoplon: {exact(str(err))[0]}")
        finally:
            after = getattr(getattr(self.e, "client", None), "exchanges", 0)
            self.turns.append([before + 1, after] if after > before else None)

    def do_raw(self, arg):
        trace = list(getattr(self.e.client, "trace", []))
        kept = [trace[0]["n"], trace[-1]["n"]] if trace else None
        named = {n: t for t, r in enumerate(self.turns, 1) if r for n in range(r[0], r[1] + 1)}
        if arg == "all":
            lo, hi = 1, 1 << 30
        elif arg and not arg.isdecimal():
            print("usage: :raw, :raw N (a turn) or :raw all")
            return
        else:
            had = [(t, r) for t, r in enumerate(self.turns, 1) if r]
            hit = next((r for t, r in had if t == int(arg)), None) if arg else (had[-1][1] if had else None)
            if hit is None:
                print("nothing has been sent to the endpoint yet" if not arg else
                      f"turn {arg} has no exchanges with the endpoint")
                return
            lo, hi = hit
        page(trace_text([ex for ex in trace if lo <= ex["n"] <= hi], named, kept))

    def do_system(self):
        page("\n".join(["hoplon: the system prompt and tools sent with every request",
                         "Your conversation follows the system prompt in each request.", "",
                         f"model: {self.e.endpoint.shown_model()}",
                         f"params: {json.dumps(getattr(self.e.client, 'params', {}))}", "",
                         "── System prompt " + "─" * 60, self.e.system()["content"], "",
                         "── Tools " + "─" * 68, json.dumps(self.e.tools(), indent=2, ensure_ascii=False)]))

    def do_work(self, arg):
        """:work TASK starts a work session (--steps N and --minutes M before the task override the limits),
        :work end ends it, and :work alone shows how far it has got."""
        w = self.e.work
        if arg == "end":
            self.e.end_work("ended")
            return
        if not arg:
            if not w:
                print("no work session; :work TASK starts one")
            else:
                print(f"work session on {w.tree.branch}: " + (show_exact(work_text(w)) if w.items else "no plan yet"))
            return
        steps = secs = None
        words = arg.split()
        while len(words) >= 2 and words[0] in ("--steps", "--minutes") and words[1].isdecimal():
            if words[0] == "--steps":
                steps = int(words[1])
            else:
                secs = int(words[1]) * 60
            words = words[2:]
        task = " ".join(words) if words != arg.split() else arg
        if not task:
            print("usage: :work [--steps N] [--minutes M] TASK, :work end, or :work")
            return
        try:
            self.e.start_work(task, steps, secs)
        except KeyboardInterrupt:       # as in any other turn: the session pauses, hoplon goes on
            self.e.session.close_dangling("stopped: the user interrupted before deciding")
            print("\n(interrupted; the work session pauses: send a message to go on, or :work end)")

    def command(self, line):
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        try:
            if cmd in FRONT_COMMANDS and not (cmd == "model" and not arg):
                repl_command(FRONT_COMMANDS[cmd], self.e, arg)
            elif cmd in ("q", "quit", "exit"):
                return "quit"
            elif cmd == "help":
                print(HELP)
            elif cmd == "sandbox":
                self.do_sandbox()
            elif cmd == "run":
                self.do_run(arg)
            elif cmd == "sh":
                self.do_sh()
            elif cmd == "capture":
                self.capture = arg == "on"
                print(f":sh capture is {'on' if self.capture else 'off'}")
            elif cmd == "attach":
                self.do_attach(arg)
            elif cmd == "plan":
                if arg == "clear":
                    self.e.clear_plan()
                    print("plan dropped: everything asks again")
                elif not self.e.plan:
                    print("no plan approved")
                else:
                    print(show_exact(plan_text(self.e.plan)))
            elif cmd == "work":
                self.do_work(arg)
            elif cmd == "alerts":
                if arg in ("on", "off"):
                    self.e.set_alerts(arg == "on", ":alerts")
                elif arg:
                    print("usage: :alerts, :alerts on or :alerts off")
                    return None
                print(f"alerts: {'on' if self.e.alerts else 'off'}" + ("" if self.e.notifier else
                      " (there is no notify hook, so there is nothing to hear either way)"))
            elif cmd == "model":
                print(f"model: {one_line(self.e.endpoint.shown_model())[0]} (:models lists the others, "
                      ":model NAME switches)")
            elif cmd == "raw":
                self.do_raw(arg)
            elif cmd == "system":
                self.do_system()
            elif cmd == "context":
                print(f"context: {self.e.used()}/{self.e.active()[0].context_limit} tokens (estimated)")
            elif cmd == "bless":
                if bless(self.e.cfg, arg, ask_yes):
                    self.e.reload_skills()
            else:
                print(f"unknown command :{cmd} (try :help)")
        except HoplonError as err:
            print(f"hoplon: {exact(str(err))[0]}")

    def do_attach(self, path):
        data = read_attachment(path)
        self.attachments.append((f"file {path}", data))
        print(f"attached {one_line(path)[0]} ({len(data)} characters) to your next message")

    def do_run(self, pid):
        props = [p for p in self.e.commands if not pid or p.id == pid]
        if not props:
            print("no command proposal to run" + (f" with id {pid}" if pid else ""))
            return
        p = props[-1]
        reason = " ".join(exact(p.fields["reason"])[0].split())
        edited = edit_text(p.fields["command"], f"Proposed command [{p.id}]: {reason}",
                           "the command that will run, as you", "cancel")
        if not edited.strip():
            print("Cancelled.")
            return
        where = where_runs(self.e.sandbox)
        print(f"\nThis will run {where}, in your shell, as you:\n\n    {show_exact(edited)}\n")
        if not ask_yes("Run it?"):
            print("Cancelled.")
            return
        def show(chunk):
            sys.stdout.buffer.write(chunk)
            sys.stdout.flush()
        ran = self.e.run_shell(p, edited, show)
        if ran is None:
            return
        rc, out = ran
        print(f"\n[exit {rc}; ran {where}]")
        kept = review_output(self.e, "run", [f"I ran this command {where} (exit {rc}):", "```", edited, "```", "Output:"],
                             out, f"Output of: {' '.join(exact(edited)[0].split())} (exit {rc})")
        if kept:
            self.send("", self.attachments + [(f"the output of :run (exit {rc}), as I reviewed it", kept)])

    def do_sh(self):
        shell = os.environ.get("SHELL") or "/bin/sh"
        self.e.audit.log("sh", capture=self.capture, sandbox=self.e.sandbox)
        print(f"[subshell runs {where_runs(self.e.sandbox)}]")
        if not self.capture:
            print("Subshell; exit to come back. Output is not captured (:capture on to share it).")
            subprocess.call([shell])
            return
        if not shutil.which("script"):
            print("capture needs the 'script' program; opening the subshell without capture")
            subprocess.call([shell])
            return
        fd, log = tempfile.mkstemp(prefix="hoplon-sh-")
        os.close(fd)
        os.chmod(log, 0o600)
        try:
            print("Subshell with capture; exit to come back and review what to share.")
            subprocess.call(["script", "-q", "-c", shell, log])
            with open(log, encoding="utf-8", errors="replace") as f:
                text = strip_ansi(f.read())
        finally:
            os.unlink(log)
        kept = review_output(self.e, "sh", [f"Output from my shell session, which ran {where_runs(self.e.sandbox)}:"],
                             text, "Output of: :sh session")
        if kept:
            self.send("", self.attachments + [("the output of my :sh session, as I reviewed it", kept)])


def read_attachment(path):
    try:
        with open(os.path.expanduser(path), "rb") as f:
            data = f.read(ATTACH_CAP + 1)
    except OSError as e:
        raise HoplonError(f"cannot read {path}: {e.strerror}", EXIT_USAGE)
    if len(data) > ATTACH_CAP:
        raise HoplonError(f"{path} is larger than {ATTACH_CAP} bytes", EXIT_USAGE)
    if b"\0" in data[:8192]:
        raise HoplonError(f"{path} looks binary; refused", EXIT_USAGE)
    text = data.decode("utf-8", "replace")
    if looks_secret(text):
        eprint(f"hoplon: warning: {one_line(path)[0]} looks like it contains secrets; it is sent as is "
               "(files are not redacted)")
    return text


# --------------------------------------------------------------------------
# JSON mode driver

def inspect(engine, ui, msg):
    """trace and system: what was sent to the endpoint and what came back, or what the next request will
    carry besides the conversation. Read only, so allowed while proposals are pending."""
    if msg.get("type") == "system":
        ui.emit({"type": "system", "system": engine.system()["content"], "tools": engine.tools(),
                 "model": engine.endpoint.model, "params": getattr(engine.client, "params", {})})
        return
    trace = list(getattr(engine.client, "trace", []))
    lo, hi = msg.get("from"), msg.get("to")
    if type(lo) is not int or type(hi) is not int:
        lo = hi = trace[-1]["n"] if trace else 0
    ui.emit({"type": "trace", "from": lo, "to": hi, "kept": [trace[0]["n"], trace[-1]["n"]] if trace else None,
             "exchanges": [{k: v for k, v in ex.items() if k != "started"} for ex in trace if lo <= ex["n"] <= hi]})


def run_json(engine, ui, first_prompt, attachments, remote=False, after_hello=()):
    ui.engine = engine

    # SIGINT stops a sandboxed run, as Ctrl-C does in the REPL, and is ignored otherwise: the default would end
    # hoplon, and with it the conversation, wherever it happened to arrive.
    def on_sigint(signum, frame):
        if Sandbox.running:
            raise KeyboardInterrupt
    signal.signal(signal.SIGINT, on_sigint)
    user, uid = whoami()
    ran = running_bundle()
    ui.emit({"type": "hello", "protocol": PROTOCOL, "version": VERSION, "bundle": ran,
             "host": socket.gethostname(), "user": user, "uid": uid, "remote": remote,
             "features": ["proposals", "decisions", "attach", "register_tools", "compact", "context", "coding",
                          "fixtures", "leftovers", "not_secret",
                          "set_coding", "skills", "files", "read", "trace", "system", "plan", "network", "clear",
                          "models", "command_sets", "interrupt", "edits", "stop", "digest", "work", "halt", "progress"],
             "endpoint": engine.endpoint.info(),
             "profile": dict({"name": engine.profile.name, "description": engine.profile.description},
                             **({"layout": engine.profile.layout} if engine.profile.layout else {}))
             if engine.profile else None,
             "session": engine.session.id,
             "resumed": engine.session.resumed, "coding": engine.session.coding,
             "coding_from": engine.coding_from if engine.session.coding else None,
             "coding_override": engine.coding_override if engine.session.coding else None, "cwd": os.getcwd(),
             "sandbox": engine.sandbox, "audit": engine.audit.path,
             "runner": engine.box.info() if engine.box else None, "edits": engine.edits_info(),
             "skills": [s.name for s in engine.skills],
             "command_sets": [p.name for p in engine.command_sets],
             "tools": list(INTRINSICS)})
    engine.audit.log("start", mode="json", bundle=ran, endpoint=engine.endpoint.info(),
                     profile=engine.profile.name if engine.profile else None, session=engine.session.id,
                     sandbox=engine.sandbox)
    for n in after_hello:
        ui.notice(n)
    ui.emit({"type": "command_sets", "command_sets": engine.command_set_rows()})

    def turn(text, att, start=None):
        first = getattr(engine.client, "exchanges", 0) + 1
        with ui.lock:
            engine.halt, ui.busy, ui.starting_work = None, True, start is not None
            Sandbox.stop.clear()        # a stop from now on ends this turn's runs, even one about to start
        try:
            reason = start() if start else engine.user_turn(text, att)
            ui.emit({"type": "done", "reason": reason, "context": {"used": engine.used(),
                                                                    "limit": engine.active()[0].context_limit},
                     "exchanges": exchanges(first)})
        except HoplonError as err:
            ui.error(str(err), err.kind, exchanges=exchanges(first))
        finally:
            with ui.lock:
                ui.busy, ui.starting_work, why, engine.halt = False, False, engine.halt, None
        if why == "end" and engine.work:        # work_end came during the turn
            try:
                engine.end_work("ended")
            except HoplonError as err:
                ui.error(str(err), err.kind)

    def exchanges(first):
        """The numbers of this turn's exchanges with the endpoint, for the trace message; None if there were none."""
        last = getattr(engine.client, "exchanges", 0)
        return [first, last] if last >= first else None

    ui.start_listening()        # now, so a stop or the end of input is seen during a first turn too
    if first_prompt is not None:
        turn(first_prompt, attachments)
    while True:
        msg = ui.read()
        if msg is None:
            return EXIT_OK
        t = msg.get("type")
        if t in FRONT_COMMANDS:
            json_command(FRONT_COMMANDS[t], engine, ui, msg, remote)
        elif t == "user":
            text = msg.get("text")
            if not isinstance(text, str):
                ui.error("user message needs 'text'", "bad_message")
                continue
            att = [(f"{lbl}" + (f" (re {ref})" if ref else ""), body) for lbl, body, ref in ui.queue]
            for lbl, body, ref in ui.queue:
                engine.share("attach", body, ref)
            ui.queue = []
            turn(text, att)
        elif t == "work":
            task, steps, secs = msg.get("task"), msg.get("steps_per_item"), msg.get("time_limit")
            if not isinstance(task, str) or not task.strip():
                ui.error("work needs 'task': what the session is to do", "bad_message")
                continue
            turn(None, (), lambda: engine.start_work(task, steps, secs, "wrapper"))
        elif t == "work_end":
            try:
                engine.end_work("ended")
            except HoplonError as err:
                ui.error(str(err), err.kind)
        elif t == "attach":
            ui.queue_attach(msg)
        elif t == "register_tools":
            try:
                names = engine.register(msg.get("tools") or [])
                ui.emit({"type": "registered", "tools": names})
            except HoplonError as err:
                ui.error(str(err), "bad_tools")
        elif t == "stop":
            ui.error("nothing is running", "not_running")
        elif t == "halt":
            pass            # a stop that came as a turn ended
        elif t == "run":
            rid = msg.get("id")
            if type(rid) not in (int, str):
                ui.error("run needs 'id': a string or a number, which the answer repeats", "bad_message")
                continue
            try:
                r = engine.run_user(msg.get("set"), msg.get("command"), msg.get("params", {}))
            except HoplonError as err:
                r = {"error": str(err)}
            if r["error"]:
                ui.emit({"type": "ran", "id": rid, "error": r["error"]})
            else:
                ui.emit({"type": "ran", "id": rid, "exit": r["exit"], "output": r["output"], "note": r["note"],
                         "cached": r["cached"] is not None, "ms": r["ms"], "redacted": r["redacted"]})
        elif t == "files":
            path = msg.get("path", ".")
            if not isinstance(path, str):
                ui.error("files needs 'path' as a string", "bad_message")
                continue
            try:
                entries, truncated = engine.files(path)
                ui.emit({"type": "files", "path": path, "entries": entries, "truncated": truncated})
            except (ValueError, OSError) as err:
                ui.error(f"cannot list {path}: {err}", "bad_path")
        elif t == "read":
            path = msg.get("path")
            if not isinstance(path, str):
                ui.error("read needs 'path' as a string", "bad_message")
                continue
            try:
                text, size, secret, counts, digest = engine.view(path)
                ui.emit({"type": "file", "path": path, "text": text, "size": size, "secret": secret,
                         "redacted": counts, "sha256": digest})
            except (ValueError, OSError) as err:
                ui.error(f"cannot read {path}: {err}", "bad_path", path=path)
        elif t in ("trace", "system"):
            inspect(engine, ui, msg)
        elif t == "plan":
            if msg.get("clear") is True:
                engine.clear_plan()
            else:
                ui.plan(engine.plan)
        elif t in ("decision", "result"):
            ui.error(f"no pending proposal {msg.get('id')!r}", "unknown_id")
        else:
            ui.error(f"unknown message type '{t}'", "unknown_type")


# --------------------------------------------------------------------------
# Main

def running_bundle():
    """The sha256 of the bundle that is running: as the hoplon script made it, or as remote mode's bootstrap received
    it."""
    return BUNDLE_SHA256


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="hoplon", description="Ask a model; it proposes, you act.")
    ap.add_argument("prompt", nargs="*", help="question (omit for the interactive REPL)")
    ap.add_argument("-p", "--profile", help="profile (persona) name")
    ap.add_argument("-e", "--endpoint", help="endpoint name (overrides the profile's)")
    ap.add_argument("-f", "--file", action="append", default=[], help="attach a file (repeatable)")
    ap.add_argument("-k", "--skill", action="append", default=[], help="use a blessed skill (repeatable)")
    ap.add_argument("-S", "--session", help="keep the conversation in a named session (created or resumed)")
    ap.add_argument("--allow-session-endpoint-change", action="store_true",
                    help="allow this invocation to send saved context to a changed or unrecorded destination")
    ap.add_argument("--commands", action="append", default=[], help="use a blessed command set (repeatable)")
    ap.add_argument("--coding", action="store_true", help="let the model read and list files here without asking")
    ap.add_argument("--coding-anywhere", action="store_true",
                    help="--coding, also where it is normally refused (your home directory, credential "
                         "directories); prints what that exposes")
    ap.add_argument("--json", action="store_true", help="JSON Lines over stdio, for wrappers")
    ap.add_argument("--config-stdin", action="store_true",
                    help="with --json: read the endpoint, profile and session token from the first line of stdin "
                         "(for hoplon-tui's remote mode)")
    ap.add_argument("--remote-config", action="store_true",
                    help="print the resolved endpoint, profile, skills and command sets as one JSON line, without any key "
                         "(for hoplon-tui's remote mode; takes -e, -p, -k and --commands)")
    ap.add_argument("--bless", metavar="PATH",
                    help="review and bless a skill or command set directory, endpoint or profile file")
    ap.add_argument("--new-endpoint", action="store_true",
                    help="ask for an endpoint's details and write it to your config, for you to bless")
    ap.add_argument("--list-skills", action="store_true", help="list skills (with --json: as JSON)")
    ap.add_argument("--list-commands", action="store_true", help="list command sets (with --json: as JSON)")
    ap.add_argument("--list-endpoints", action="store_true", help="list endpoints (with --json: as JSON)")
    ap.add_argument("--list-profiles", action="store_true", help="list profiles (with --json: as JSON)")
    ap.add_argument("--list-sessions", action="store_true", help="list saved sessions (with --json: as JSON)")
    ap.add_argument("--leftovers", action="store_true",
                    help="list what hoplon made for sandboxes and jobs that is still there, what is likely stale, and "
                         "how to remove it (with --json: as JSON)")
    ap.add_argument("--clean-stale", action="store_true",
                    help="remove the stale leftovers that can be removed safely, after showing them and asking")
    ap.add_argument("--bundle", action="store_true",
                    help="write hoplon as the one script remote mode sends to stdout, and its sha256 to stderr")
    ap.add_argument("--version", action="version", version=f"hoplon {VERSION} (protocol {PROTOCOL})")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.bundle:
        eprint("hoplon: --bundle is answered by the hoplon script, alone: hoplon --bundle")
        return EXIT_USAGE
    try:
        if args.config_stdin and not args.json:
            raise HoplonError("--config-stdin needs --json", EXIT_USAGE)
        if args.config_stdin and (args.endpoint or args.profile or args.session or args.skill):
            raise HoplonError("--config-stdin takes the endpoint and profile from stdin: not with -e, -p, -S or -k",
                             EXIT_USAGE)
        cfg = Config()
        cfg.ensure()
        if args.bless:
            if not sys.stdin.isatty():
                raise HoplonError("blessing needs a terminal: it asks you to confirm you have read it", EXIT_USAGE)
            return EXIT_OK if bless(cfg, args.bless, ask_yes) else EXIT_ERROR
        if args.new_endpoint:
            if not sys.stdin.isatty():
                raise HoplonError("--new-endpoint needs a terminal: it asks you questions", EXIT_USAGE)
            return EXIT_OK if new_endpoint(cfg) else EXIT_ERROR
        if args.remote_config:
            if args.session:
                raise HoplonError("--remote-config takes only -e, -p, -k and --commands", EXIT_USAGE)
            print(json.dumps(export_remote(cfg, args.profile, args.endpoint, args.skill, args.commands)))
            return EXIT_OK
        if args.list_skills:
            rows = skill_rows(cfg)
            if args.json:
                print(json.dumps({"type": "skills", "skills": rows}))
            else:
                print("\n".join(skill_lines(rows)))
            return EXIT_OK
        if args.list_commands:
            rows = command_set_rows(cfg)
            if args.json:
                print(json.dumps({"type": "command_sets", "command_sets": rows}))
            else:
                print("\n".join(skill_lines(rows)))
            return EXIT_OK
        for kind, wanted in (("endpoint", args.list_endpoints), ("profile", args.list_profiles)):
            if wanted:
                rows = list_named(cfg, kind)
                if args.json:
                    print(json.dumps({"type": kind + "s", kind + "s": rows}))
                else:
                    for r in rows:
                        print(f"{one_line(r['name'])[0]:20} {r['status']:10} {one_line(r['description'])[0]}")
                return EXIT_OK
        if args.leftovers or args.clean_stale:
            return leftovers_command(cfg, args)
        if args.list_sessions:
            rows = list_sessions(cfg)
            if args.json:
                print(json.dumps({"type": "sessions", "sessions": rows}))
            else:
                for r in rows:
                    print(f"{one_line(r['id'])[0]:28} {one_line(r['endpoint'] or '-')[0]:12} "
                          f"{one_line(r['profile'] or '-')[0]:12} {one_line(r['updated'] or '')[0]:26} "
                          + (f"[on {one_line(r['remote'])[0]}] " if r.get("remote") else "")
                          + f"{one_line(r['first'])[0]}")
            return EXIT_OK

        session = Session.open(cfg, args.session)
        notices = []
        after_hello = []
        offered = None
        if args.config_stdin:
            endpoint, persona, offered = remote_config(sys.stdin.readline(), cfg.audit)
            if offered["session"]:
                session.resume_from(offered["session"]["name"], offered["session"]["data"])
        else:
            pname = args.profile or (session.profile if session.resumed else cfg.default_profile)
            persona = select_named(cfg, "profile", pname) if pname else None
            endpoint = select_endpoint(cfg, args.endpoint or (persona.endpoint if persona else None)
                                       or session.endpoint or cfg.default_endpoint)
        destination_id = endpoint.destination_id()
        if args.config_stdin:           # hoplon-tui checked the real endpoint against the session, on your machine
            session.destination_id = destination_id
        if session.messages and session.destination_id != destination_id:
            reason = ("has a different destination" if session.destination_id else
                      "was saved before its destination was recorded")
            msg = f"session '{session.id}' {reason}. Saved context would go to {endpoint.base_url}."
            if not args.allow_session_endpoint_change:
                raise HoplonError(msg + " To approve this transfer, rerun with --allow-session-endpoint-change.",
                                 EXIT_USAGE, "session_endpoint_changed")
            notices.append(msg)
            cfg.audit.log("session_endpoint_change", session=session.id, endpoint=endpoint.info())
        session.destination_id = destination_id
        if session.resumed and args.profile and session.profile != args.profile:
            notices.append(f"session '{session.id}' was used with profile '{session.profile or '-'}', "
                           f"now '{args.profile}'")
        if args.config_stdin:
            base = list(offered["skills"])      # what your machine sent is what it had on
        elif not session.resumed:
            base = persona.skills if persona and persona.skills is not None else cfg.default_skills
        else:
            base = persona.skills if args.profile and persona and persona.skills else []
        session.endpoint, session.profile = endpoint.name, persona.name if persona else None
        if session.coding and os.path.realpath(session.cwd) != os.path.realpath(os.getcwd()):
            session.coding = False
            notices.append(f"coding mode was on for {session.cwd}, not here; it is now off")
        for k in base + args.skill:
            if k not in session.skills:
                session.skills.append(k)
        if args.config_stdin:
            command_sets = list(offered["command_sets"])
            if args.commands:
                notices.append("--commands is ignored in remote mode: the command sets come from your machine")
        else:
            command_sets = list(dict.fromkeys((persona.command_sets if persona and persona.command_sets is not None
                                          else cfg.default_commands) + args.commands))

        prompt = " ".join(args.prompt) if args.prompt else None
        if args.json:
            ui = JsonUI()
            if offered and offered["session"]:
                session.relay = lambda data: ui.emit({"type": "session_save", "session": data})
        elif sys.stdin.isatty() and sys.stdout.isatty():
            ui = TtyUI()
        else:
            ui = PlainUI()
        attachments = [(f"file {f}", read_attachment(f)) for f in args.file]
        coding = ("--coding-anywhere" if args.coding_anywhere else "--coding" if args.coding else
                  f"profile {persona.name}" if persona and persona.coding else None)
        engine = Engine(cfg, endpoint, session, ui, coding=coding, profile=persona, anywhere=args.coding_anywhere,
                        command_sets=command_sets, offered=offered)
        if isinstance(ui, TtyUI):       # hooks only run for a person at the terminal, the UI chosen above
            engine.notifier = Notifier.start(engine)
        for n in notices:
            ui.notice(n)
        if session.resumed and session.coding and engine.coding_from == "session":
            ui.notice("coding mode is on for this session")

        exit_on_signals()
        if args.json:
            try:
                return run_json(engine, ui, prompt, attachments, args.config_stdin, after_hello)
            finally:
                engine.leave_work()
        cfg.audit.log("start", mode="repl" if prompt is None else "oneshot", bundle=running_bundle(),
                      endpoint=endpoint.info(),
                      profile=persona.name if persona else None,
                      session=session.id, sandbox=engine.sandbox)
        if prompt is None:
            if not isinstance(ui, TtyUI):
                raise HoplonError("no prompt given and no terminal for the REPL", EXIT_USAGE)
            repl = Repl(engine)
            repl.attachments = attachments
            try:
                return repl.run()
            finally:
                engine.leave_work()
        if isinstance(ui, TtyUI):
            eprint(f"hoplon: {(one_line(persona.name)[0] + ' on ') if persona else ''}{one_line(endpoint.name)[0]} "
                   f"({one_line(endpoint.model)[0]} at {one_line(endpoint.base_url)[0]}); "
                   f"commands run {where_runs(engine.sandbox)}")
            if engine.notifier:
                eprint("hoplon: " + HOOK_BANNER.format(short_path(engine.notifier.path)))
        engine.user_turn(prompt, attachments)
        return EXIT_OK
    except HoplonError as err:
        if args.json:
            print(json.dumps({"type": "error", "code": err.kind, "message": str(err)}), flush=True)
        else:
            eprint(f"hoplon: {exact(str(err))[0]}")
        return err.code
    except KeyboardInterrupt:
        eprint("\nhoplon: interrupted")
        return EXIT_INTERRUPT


if __name__ == "__main__":
    sys.exit(main())
