# hoplon_core/gate.py: the gate: the one part that decides whether anything happens.
#
# GATE_REQUESTS (the four things that cross out of hoplon), AUTO_RULES (the only ways one is approved without
# asking), Proposal, the proposal kinds, and Gate, which turns a tool call into a proposal, decides it (asks, approves
# by a rule through standing and grant, or refuses) and performs what was accepted through the handlers: files.py,
# commands.py and sandbox.py. (The fourth request, what goes to the model, is sent by the turn loop through
# client.py.) This part touches nothing itself; tests/test_parts.py holds it to that.
#
# Uses: base, files, commands, sandbox

# --------------------------------------------------------------------------
# Proposals

def parse_plan(args):
    """propose_plan's arguments, checked: a goal and 1 to PLAN_STEPS numbered steps."""
    goal, steps = args.get("goal"), args.get("steps")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal is required")
    if not isinstance(steps, list) or not 1 <= len(steps) <= PLAN_STEPS:
        raise ValueError(f"steps must be a list of 1 to {PLAN_STEPS} steps")
    out = []
    for n, st in enumerate(steps, 1):
        if not isinstance(st, dict) or st.get("kind") not in ("read", "edit", "write", "command"):
            raise ValueError(f"step {n}: kind must be read, edit, write or command")
        step = {"n": n, "kind": st["kind"], "why": str(st.get("why", ""))}
        if st["kind"] == "command":
            if not isinstance(st.get("command"), str) or not st["command"].strip():
                raise ValueError(f"step {n}: a command step needs the exact command")
            step["command"] = st["command"]
        else:
            if not isinstance(st.get("path"), str):
                raise ValueError(f"step {n}: a {st['kind']} step needs a path")
            if st["path"] not in (".", "./", ""):
                split_rel(st["path"])
            step["path"] = st["path"]
        out.append(step)
    return {"goal": goal, "steps": out, "reason": goal}


def plan_text(plan):
    """The approved plan for the model: each step, whether it is approved, and how far it has got."""
    lines = [f"Goal: {plan['goal']}"]
    for st in plan["steps"]:
        what = f"run exactly: {st['command']}" if st["kind"] == "command" else f"{st['kind']} {st['path']}"
        note = {"todo": "", "done": " (done)", "left out": " (LEFT OUT by the user: don't do it)"}[st["status"]]
        if st.get("edited"):
            note += " (the user changed this command; propose it exactly as written here)"
        lines.append(f"{st['n']}. {what}{note}")
    lines.append("Reads in the plan need no approval. Edits and writes are still shown to the user. Anything outside "
                 "the plan is asked about and flagged; to change course, say why or propose a new plan.")
    return "\n".join(lines)


def work_text(work):
    """The approved work plan for the model: each item and how far it has got."""
    lines = [f"Goal: {work.goal}"]
    for it in work.items:
        note = {"done": " (done)", "todo": " (CURRENT)" if it["n"] == work.current else ""}[it["status"]]
        lines.append(f"{it['n']}. {it['title']}{note}")
    return "\n".join(lines)


def audit_step(st):
    return {"n": st["n"], "kind": st["kind"], **({"path": st["path"]} if "path" in st else
                                                 command_fingerprint(st["command"]))}


class Proposal:
    def __init__(self, pid, kind, call_id, **fields):
        self.id, self.kind, self.call_id = pid, kind, call_id
        self.fields = fields
        self.auto = False
        self.decision = None
        self.result = None
        self.error = None          # set when the request is invalid and never shown for a decision
        self.reply = None          # what the model is told instead of "error: ..." (the user still sees error)
        self.stopped = False       # the user pressed Ctrl-C while it ran: the turn ends
        self.digest = None         # set when a JSON front end is asked to decide it
        self.rule = None           # the rule that approved it without asking (AUTO_RULES), set only by Engine.grant

    def event(self):
        ev = {"type": "proposal", "id": self.id, "kind": self.kind}
        ev.update({k: v for k, v in self.fields.items() if not k.startswith("_")})
        if self.auto:
            ev["auto"] = True
        return ev


# The gate: what crosses from the model's side to yours is one of four requests, and each is carried out by one
# handler. Every proposal kind names the request it makes, or none: a kind with none acts on nothing outside hoplon
# (it is only shown, the front end answers it, or it moves a work session along). See #44.
GATE_REQUESTS = {
    "run": ("a command in the model's sandbox, on a throwaway copy of the project", "Sandbox.run"),
    "scoped": ("a command from a blessed command set, on the live system", "Gate.run_trusted"),
    "edit": ("a change to a file in the project: an edit or a write", "Workdir.write"),
    "model": ("what goes to the model endpoint, including what the file tools read for it", "Client.chat"),
}

# The only rules that may approve a proposal without asking. Each kind lists the ones that apply to it, and
# Engine.grant refuses any other; every approval by a rule is logged as a decision "by" that rule.
AUTO_RULES = {
    "offline_run": "a sandboxed run with the network off",
    "coding_read": "coding mode: a read, listing or search of what isn't hidden or secret-looking",
    "plan_read": "a read or listing the approved plan names",
    "plain_command": "a scoped command that says read, uses no network and runs as you",
    "blessed_auto": "a scoped command you let run without asking when you blessed its set",
    "work_copy": "an edit or write to a work session's copy, on its own branch",
    "approved_file": "an edit or write to a file you already approved one for (edits: approved-files)",
    "work_step": "finishing or retrying a work item, which only moves the work session along",
}


class Kind:
    """One kind of proposal: the tool that asks for it and the Engine methods that build it from a call and carry it
    out once decided. request: the gate request it makes (GATE_REQUESTS), or None. rules: the AUTO_RULES that may
    approve it without asking. bulk: its accepted result is bulky (compaction may drop it later). shown: only shown
    to the user, never asked about or run, and it ends the turn. answered: the wrapper answers it (ok, declined or
    error), so hoplon records no accept or decline and the handler runs whatever the answer. Every other kind runs
    only if accepted, by the user or by one of its rules."""

    def __init__(self, name, tool, build, perform, request=None, rules=(), bulk=False, shown=False, answered=False):
        self.name, self.tool, self.build, self.perform = name, tool, build, perform
        self.request, self.rules = request, tuple(rules)
        self.bulk, self.shown, self.answered = bulk, shown, answered


PROPOSAL_KINDS = {k.name: k for k in (       # every kind of proposal there is; a call for any other tool is invalid
    Kind("command", "propose_command", "build_command", "perform_command", shown=True),
    Kind("samples", "request_samples", "build_samples", "perform_samples", shown=True),
    Kind("skills", "suggest_skills", "build_skills", "perform_skills"),
    Kind("sandbox", "run_in_sandbox", "build_sandbox", "run_sandboxed", "run", ("offline_run",), bulk=True),
    Kind("search", "search_files", "search_proposal", "perform_file", "model", ("coding_read",), bulk=True),
    Kind("read", "read_file", "build_read", "perform_file", "model", ("coding_read", "plan_read"), bulk=True),
    Kind("list", "list_dir", "build_read", "perform_file", "model", ("coding_read", "plan_read"), bulk=True),
    Kind("write", "write_file", "build_write", "perform_file", "edit", ("work_copy", "approved_file")),
    Kind("edit", "edit_file", "build_edit", "perform_file", "edit", ("work_copy", "approved_file")),
    Kind("plan", "propose_plan", "build_plan", "perform_plan", answered=True),
    Kind("work_plan", "propose_work_plan", "build_work_plan", "perform_work_plan"),
    Kind("finish", "finish_item", "build_finish", "perform_finish", rules=("work_step",)),
    Kind("retry", "retry_item", "build_retry", "perform_retry", rules=("work_step",)),
    Kind("external", None, "build_external", "perform_external", bulk=True, answered=True),
    Kind("trusted", None, "build_trusted", "perform_trusted", "scoped", ("plain_command", "blessed_auto"), bulk=True),
)}
BY_TOOL = {k.tool: k for k in PROPOSAL_KINDS.values() if k.tool}


UNOFFERED = {"search_files": "is only available in coding mode",
             "propose_work_plan": "is only available in a work session",
             "finish_item": "is only available in a work session",
             "retry_item": "is only for reviewing an item that used its steps",
             "propose_command": "is not available in a work session: run_in_sandbox runs commands there",
             "propose_plan": "is not available in a work session: propose_work_plan plans one",
             "suggest_skills": "is not available now: no skill is left to suggest (each one is on, or the user "
                               "declined it), or nobody is there to answer"}


class Halted(Exception):
    """The user asked the turn to stop (Engine.halt), while the model's reply was coming in."""
class Gate:
    """The gate's half of Engine: building proposals from tool calls, the policies that decide whether each
    one asks, is approved by a rule or is refused, and performing what was accepted through the handlers
    (files.py, commands.py, sandbox.py). Engine (turn.py) is a Gate."""

    def set_coding(self, on, source=":coding"):
        if self.work and not on:
            raise HoplonError("coding mode stays on during a work session; end the session first", EXIT_USAGE,
                             "work")
        refused = coding_refusal(os.getcwd()) if on else None
        if refused and not self.anywhere:
            raise HoplonError(f"coding mode is refused in {refused[0]}; cd into a project first "
                             "(--coding-anywhere overrides this, and says what that exposes)", EXIT_USAGE)
        self.coding_override = refused[0] if refused else None
        if refused:
            self.ui.notice(f"[!] --coding-anywhere: coding mode is ON in {refused[0]}, where it is normally "
                           f"refused. Without asking you, the model can now read {CODING_RISKS[refused[1]]}. "
                           f"All of it goes to {self.endpoint.base_url}. Something the model reads can tell it "
                           "what to read next. Hidden files, files that look like secrets, and every write "
                           "still ask.")
        self.session.coding = on
        self.session.cwd = os.getcwd()
        self.coding_from = source if on else None
        self.audit.log("coding", on=on, source=source, cwd=os.getcwd(), override=self.coding_override)

    def set_command_set(self, name, on):
        """Offer a command set's scoped commands to the model and the user from the next request on, or stop. Only a
        blessed command set whose programs can be trusted is turned on."""
        if on:
            pl = (find_command_sets(self.cfg) if self.offered is None else self.offered["command_sets"]).get(name)
            if not pl:
                raise HoplonError(f"command set '{name}' not found", EXIT_USAGE, "unknown_commands")
            st = command_set_status(self.cfg, pl) if self.offered is None else "blessed"
            if st != "blessed":
                raise HoplonError(f"command set '{name}' is {st}; read it, then: hoplon --bless {pl.path}", EXIT_USAGE,
                                 "not_blessed")
            if pl.error:
                raise HoplonError(f"command set '{name}': {pl.error}", EXIT_USAGE, "bad_commands")
            clash = sorted(set(pl.tools()) & set(self.external))
            if clash:
                raise HoplonError(f"command set '{name}': a registered tool already has the name {clash[0]}", EXIT_USAGE,
                                 "bad_commands")
            if self.offered is None:
                pl.allow_auto(self.cfg.blessed().get(pl.path, {}).get("auto", []))
            missing = pl.resolve()
            if missing:
                self.ui.notice(f"command set '{name}': {', '.join(missing)} left out, as the program isn't installed here")
            self.command_sets = [p for p in self.command_sets if p.name != name] + [pl]
        else:
            self.command_sets = [p for p in self.command_sets if p.name != name]
        self.trusted = {t: c for p in self.command_sets for t, c in p.tools().items()}
        self.session.usage = None   # the tools changed, so the last measured size no longer holds
        self.audit.log("command_set", name=name, on=on)

    def register(self, tools):
        added = []
        for t in tools:
            if not isinstance(t, dict):
                raise HoplonError("each registered tool must be an object")
            name, desc, params = t.get("name"), t.get("description"), t.get("parameters", {"type": "object"})
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                raise HoplonError(f"bad tool name {name!r}")
            if name in INTRINSICS or name in self.trusted:
                raise HoplonError(f"tool '{name}' collides with an intrinsic or command set tool")
            if not isinstance(desc, str) or not isinstance(params, dict):
                raise HoplonError(f"tool '{name}' needs a description and a parameters object")
            self.external[name] = {"type": "function",
                                   "function": {"name": name, "description": desc, "parameters": params}}
            added.append(name)
        self.audit.log("register_tools", tools=added)
        return added

    def can_run(self):
        """Is run_in_sandbox offered? Coding mode, a sandbox that works here, and someone to see what runs."""
        return bool(self.box and self.session.coding and self.ui.performs and not self.box.problem())

    def set_edits(self, mode, source=":edits"):
        """"ask": every edit and write asks. "approved-files": a further edit or write to a file you approved one for,
        in this process, applies without asking (still shown, logged and bound to the file as it was)."""
        if mode not in EDITS_MODES:
            raise HoplonError(f"edits must be one of: {', '.join(EDITS_MODES)}", EXIT_USAGE, "bad_message")
        self.edits = mode
        self.audit.log("edits", mode=mode, source=source)
        if mode == "ask" and (self.approved_files or self.work and self.work.before and self.work.before["files"]):
            # off forgets them, those a work session set aside too: on again starts from none
            self.reset_approved_files(source)
        if mode == "approved-files":
            self.ui.notice("[!] edits: approved-files: a further edit or write to a file you approved one for applies "
                           "without asking, for the rest of this session; :edits reset forgets those files")

    def reset_approved_files(self, source=":edits reset"):
        """Forget the approved files: those set aside during a work session too, so they don't come back after."""
        put_aside = self.work.before["files"] if self.work and self.work.before else set()
        self.audit.log("edits_reset", files=sorted(self.approved_files | put_aside), source=source)
        self.approved_files.clear()
        put_aside.clear()

    def edits_info(self):
        return {"mode": self.edits, "files": sorted(self.approved_files)}

    @staticmethod
    def file_key(path):
        try:
            return "/".join(split_rel(path))
        except ValueError:
            return None

    def own_place(self, path):
        """Is this file where an edit must always ask: hoplon's own config or state (whatever it says about
        approved files), or a place coding mode is refused, such as ~/.ssh or your home directory itself?"""
        real = os.path.realpath(path)
        for root in (self.cfg.home, config_home(), os.path.join(state_home(), "hoplon")):
            root = os.path.realpath(root)
            if real == root or real.startswith(root.rstrip("/") + "/"):
                return True
        return coding_refusal(os.path.dirname(real)) is not None

    def auto_edit(self, p):
        """Would this edit or write apply without asking? Only with edits "approved-files", for a file already in the
        approved set, and never for a new file, a hidden or secret-looking one, or a whole replacement."""
        if self.edits != "approved-files" or p.kind not in ("edit", "write") or not self.ui.performs:
            return False
        key = self.file_key(p.fields.get("path"))
        if key is None or key not in self.approved_files or is_hidden(key.split("/")):
            return False
        if self.own_place(os.path.join(self.wd.root, key)):
            return False
        f = p.fields
        if p.kind == "write" and (not f.get("exists") or f.get("undiffable")):
            return False
        new = f.get("_content") if p.kind == "edit" else f.get("content")
        try:
            old = self.wd.read(f["path"])
        except (ValueError, OSError):
            return False
        excused = self.cfg.not_secret()
        return not any(self.secret_text(t, excused) for t in (old, new or ""))

    def work_edit(self, p):
        """Does this edit or write apply without asking, in a work session? Yes, in the copy, while an item of the
        approved plan is being worked on or the planner reviews it (both are held to the steps and the time limit),
        unless the file is hidden, or it or the new content looks secret. Before the plan is approved, every edit asks. So does a pyvenv.cfg, or anything
        under a directory of that name, which would change what the sandbox's secret scan looks at. In a work
        session these rules are the only ones: edits "approved-files" doesn't apply in the copy."""
        w = self.work
        if not w or not self.ui.performs or not w.items or not w.current or w.role not in ("item", "review"):
            return False
        key = self.file_key(p.fields.get("path"))
        if (key is None or is_hidden(key.split("/")) or p.fields.get("undiffable")
                or "pyvenv.cfg" in key.split("/")):
            return False
        new = p.fields.get("_content") if p.kind == "edit" else p.fields.get("content")
        texts = [new or ""]
        if p.kind == "edit" or p.fields.get("exists"):
            try:
                texts.append(self.wd.read(p.fields["path"]))
            except (ValueError, OSError):
                return False
        excused = self.cfg.not_secret()
        return not any(self.secret_text(t, excused) for t in texts)

    def set_network(self, on, source=":network"):
        if not self.box:
            raise HoplonError("there is no sandbox: the profile has no 'sandbox'", EXIT_USAGE, "no_sandbox")
        if on and self.work:
            raise HoplonError("the network stays off during a work session, whose tests run without asking; end the "
                             "session first", EXIT_USAGE, "work")
        self.box.network = on
        self.audit.log("network", on=on, source=source)
        if on:
            self.ui.notice("[!] the sandbox's network is ON for this session: code running there could send your "
                           "project anywhere, so each sandboxed command now asks first")

    def can_search(self):
        return self.read_allowed([])

    def trusted_tools(self):
        """The scoped commands on offer: only where someone can see what runs, as with run_in_sandbox."""
        return dict(self.trusted) if self.ui.performs else {}

    # -- proposals

    def make_proposal(self, tc):
        self.counter += 1
        pid = f"p{self.counter}"
        name = tc["function"]["name"]
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            p = Proposal(pid, "invalid", tc["id"], tool=name)
            p.error = "arguments were not a JSON object"
            return p
        reason = str(args.get("reason", ""))
        try:
            kind = BY_TOOL.get(name) or (PROPOSAL_KINDS["external"] if name in self.external else
                                         PROPOSAL_KINDS["trusted"] if name in self.trusted_tools() else None)
            if kind is None:
                raise ValueError(f"unknown tool '{name}'")
            if name not in {t["function"]["name"] for t in self.tools()}:     # a tool not offered at this point
                raise ValueError(f"{name} {UNOFFERED.get(name, 'is not available now')}")
            return getattr(Gate, kind.build)(self, pid, tc["id"], args, reason, name)
        except (ValueError, OSError) as e:
            p = Proposal(pid, "invalid", tc["id"], tool=name)
            p.error = str(e)
            if isinstance(e, (DiskError, OSError)):
                try:
                    parts = split_rel(args.get("path", "."))
                except ValueError:
                    parts = [".hidden"]
                if not self.read_allowed(parts):    # on-disk facts are for the user, not the model
                    p.error = f"{name} of {show_exact(str(args.get('path')))} failed, and the model was only told " \
                              f"it was declined: {e}"
                    p.reply = "declined by the user"
            return p

    # Builders: one per kind (PROPOSAL_KINDS). Each turns a call's arguments into a proposal, deciding whether it
    # is automatic, or raises ValueError (or OSError) for a call that is not valid.

    def build_command(self, pid, call_id, args, reason, name):
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("command is required")
        p = Proposal(pid, "command", call_id, command=cmd, reason=reason)
        self.commands.append(p)
        return p

    def build_samples(self, pid, call_id, args, reason, name):
        what, files, suggest = args.get("what"), args.get("files"), args.get("suggest")
        if self.work or not self.can_run():
            raise ValueError("request_samples is not available now")
        if not isinstance(what, str) or not what.strip():
            raise ValueError("what is required")
        if not isinstance(files, list) or not files or not all(isinstance(f, str) and f.strip() for f in files):
            raise ValueError("files must list the file names your tests will read")
        if suggest is not None and not isinstance(suggest, str):
            raise ValueError("suggest must be a command, as text")
        fx = self.fixtures_store()
        return Proposal(pid, "samples", call_id, what=what, files=files[:20], suggest=suggest or "", reason=reason,
                        folder=fx.dir)

    def build_skills(self, pid, call_id, args, reason, name):
        names = args.get("names")
        if not self.can_suggest_skills(index_only=True):
            raise ValueError("suggest_skills is not available now")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ValueError("names must list skills from 'Skills you can suggest'")
        names = list(dict.fromkeys(names))
        if len(names) > SKILL_SUGGEST_MAX:
            raise ValueError(f"suggest at most {SKILL_SUGGEST_MAX} skills at a time")
        on, index = {s.name for s in self.skills}, dict(self.skill_index)
        for n in names:
            if n not in index:
                raise ValueError(f"'{one_line(n)[0][:40]}' is not in the list of skills you can suggest")
            if n in on:
                raise ValueError(f"skill {n} is on already")
            if n in self.declined_skills:
                raise ValueError(f"the user declined skill {n}; don't suggest it again unless they ask")
        return Proposal(pid, "skills", call_id, names=names, about=[index[n] for n in names], reason=reason)

    def build_sandbox(self, pid, call_id, args, reason, name):
        cmd = args.get("command")
        if not self.can_run():
            raise ValueError("run_in_sandbox is not available now")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError("command is required")
        p = Proposal(pid, "sandbox", call_id, command=cmd, reason=reason, network=self.box.network,
                     image=self.box.conf["image"])
        return p

    def build_read(self, pid, call_id, args, reason, name):
        """read_file and list_dir."""
        path = args.get("path", ".")
        parts = split_rel(path) if not (name == "list_dir" and path in (".", "./", "")) else []
        kind = "read" if name == "read_file" else "list"
        p = Proposal(pid, kind, call_id, path=path, reason=reason)
        if kind == "read":
            st = self.wd.stat(path)
            p.fields["size"] = st.st_size if st else None
        if not self.read_allowed(parts) and kind == "read" and any(secret_name(x) for x in parts):    # named like one: warned as content would be
            p.fields["secret"] = True
            p.fields["to"] = to = self.active()[0].base_url
            self.ui.notice(f"{path} is named like a file that holds secrets, so reading it needs your OK; allowing "
                           f"sends it to {to}")
        return p

    def no_git(self, path):
        """In a work session, the copy's .git is hoplon's alone: no file tool may write it."""
        if getattr(self, "work", None) and ".git" in split_rel(path):
            raise ValueError("in a work session, nothing under .git can be written: hoplon keeps the copy's git")

    def build_write(self, pid, call_id, args, reason, name):
        path, content = args.get("path"), args.get("content")
        split_rel(path)
        Gate.no_git(self, path)
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        state, previous = self.wd.snapshot(path)
        exists = state is not None
        fields = {"path": path, "exists": exists, "content": content, "reason": reason, "_state": state,
                  "lines": content.count("\n") + (0 if content.endswith("\n") or not content else 1)}
        if exists:
            try:
                fields["diff"] = unified_diff(self.wd.decode(previous, WRITE_CAP), content, path)
            except ValueError as e:
                fields.update(diff=f"(cannot diff the existing file: {e})", undiffable=str(e),
                              mode=f"{stat.S_IMODE(state[0][2]):04o}")
        return Proposal(pid, "write", call_id, **fields)

    def build_edit(self, pid, call_id, args, reason, name):
        path, old, new = args.get("path"), args.get("old_text"), args.get("new_text")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ValueError("old_text and new_text are required")
        parts = split_rel(path)
        Gate.no_git(self, path)
        try:
            state, previous = self.wd.snapshot(path)
            if state is None:
                raise FileNotFoundError(f"{path} does not exist")
            current = self.wd.decode(previous, WRITE_CAP)
            n = current.count(old)
            if n != 1:
                raise ValueError(f"old_text appears {n} times in {path}; it must appear exactly once")
        except (ValueError, OSError) as e:
            if self.read_allowed(parts):
                raise
            # The model may not learn anything about this file, so it only hears "declined".
            p = Proposal(pid, "invalid", call_id, tool=name)
            p.error = f"edit of {show_exact(path)} not applied, and the model was only told it was declined: {e}"
            p.reply = "declined by the user"
            return p
        updated = current.replace(old, new, 1)
        return Proposal(pid, "edit", call_id, path=path, diff=unified_diff(current, updated, path),
                        reason=reason, _content=updated, _state=state)

    def build_plan(self, pid, call_id, args, reason, name):
        return Proposal(pid, "plan", call_id, **parse_plan(args))

    def build_work_plan(self, pid, call_id, args, reason, name):
        if not self.work:
            raise ValueError("propose_work_plan is only available in a work session")
        goal, items = args.get("goal"), args.get("items")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal is required")
        if (not isinstance(items, list) or not 1 <= len(items) <= PLAN_STEPS
                or not all(isinstance(x, str) and x.strip() for x in items)):
            raise ValueError(f"items must be a list of 1 to {PLAN_STEPS} lines of text")
        items = [plain_text(x).strip() for x in items]
        if any(len(x.splitlines()) != 1 or "\t" in x for x in items):     # any line break, U+2028 too
            raise ValueError("each item must be one line of text")
        goal = plain_text(goal).strip()
        return Proposal(pid, "work_plan", call_id, goal=goal, items=items, reason=goal)

    def build_finish(self, pid, call_id, args, reason, name):
        if not self.work:
            raise ValueError("finish_item is only available in a work session")
        if not self.work.items:
            raise ValueError("there is no approved plan yet: propose one with propose_work_plan")
        n, summary = args.get("n"), args.get("summary")
        if type(n) is not int or not isinstance(summary, str) or not summary.strip():
            raise ValueError("n (the item's number) and summary are required")
        if not self.work.current:
            raise ValueError("every item is done: reply with a short summary of the work")
        if n != self.work.current:
            raise ValueError(f"item {n} is not the current item: finish item {self.work.current} first")
        notes = args.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise ValueError("notes must be text")
        summary = plain_text(summary).strip()
        if not summary:
            raise ValueError("n (the item's number) and summary are required")
        p = Proposal(pid, "finish", call_id, n=n, summary=one_line(summary)[0][:200], reason=summary,
                     test=self.work.test, notes=plain_text(notes or "").strip()[:WORK_NOTES_MAX])
        return p

    def build_retry(self, pid, call_id, args, reason, name):
        w = self.work
        if not w or w.role != "review":
            raise ValueError("retry_item is only for reviewing an item that used its steps")
        n, hint = args.get("n"), args.get("hint")
        if type(n) is not int or not isinstance(hint, str) or not hint.strip():
            raise ValueError("n (the item's number) and hint are required")
        if n != w.current:
            raise ValueError(f"item {n} is not the one under review: that is item {w.current}")
        p = Proposal(pid, "retry", call_id, n=n, hint=plain_text(hint).strip()[:WORK_NOTES_MAX], reason=hint)
        return p

    def build_external(self, pid, call_id, args, reason, name):
        return Proposal(pid, "external", call_id, tool=name, args=args)

    def build_trusted(self, pid, call_id, args, reason, name):
        cmd = self.trusted[name]
        values = cmd.check(args)
        p = Proposal(pid, "trusted", call_id, set=cmd.set, command=cmd.name, params=values,
                     argv=cmd.shown(values), access=cmd.access, reason=cmd.describe, _cmd=cmd,
                     **({"as_user": cmd.as_user} if cmd.as_user else {}),
                     **({"network": cmd.reaches()} if cmd.reaches() else {}))
        return p

    def search_proposal(self, pid, call_id, args, reason, name="search_files"):
        if not self.can_search():
            raise ValueError("search_files is only available in coding mode")
        pattern, path, glob = args.get("pattern"), args.get("path", "."), args.get("glob")
        if not isinstance(pattern, str) or not pattern or "\n" in pattern or len(pattern) > 200:
            raise ValueError("pattern must be one line of text, at most 200 characters")
        if not isinstance(path, str) or (glob is not None and (not isinstance(glob, str) or len(glob) > 100)):
            raise ValueError("path and glob must be strings")
        parts = split_rel(path) if path not in ("", ".", "./") else []
        if is_hidden(parts):
            raise ValueError("hidden directories are not searched")
        fields = {"path": path or ".", "pattern": pattern, "reason": reason}
        if glob:
            fields["glob"] = glob
        if args.get("ignore_case") is True:
            fields["ignore_case"] = True
        return Proposal(pid, "search", call_id, **fields)

    def search(self, p):
        """Run an automatic search_files: (what the model gets, a note for the user)."""
        f = p.fields
        fold = (lambda t: t.casefold()) if f.get("ignore_case") else (lambda t: t)
        needle, excused = fold(f["pattern"]), self.cfg.not_secret()
        hits, files, skipped, stopped = [], 0, {}, None
        deadline = time.monotonic() + SEARCH_SECONDS
        for rel in self.wd.walk(f["path"]):
            if f.get("glob") and not fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], f["glob"]):
                continue
            if files == SEARCH_FILES:
                stopped = f"after {SEARCH_FILES} files"
                break
            if time.monotonic() > deadline:
                stopped = f"after {SEARCH_SECONDS} seconds"
                break
            files += 1
            try:
                text = self.wd.read(rel)
            except (ValueError, OSError):
                skipped["binary, large or unreadable"] = skipped.get("binary, large or unreadable", 0) + 1
                continue
            if needle not in fold(text):
                continue
            if self.secret_text(text, excused):
                skipped["looking like secrets"] = skipped.get("looking like secrets", 0) + 1
                continue
            for n, line in enumerate(text.split("\n"), 1):
                at = fold(line).find(needle)
                if at >= 0:
                    start = max(0, min(at - SEARCH_LINE // 4, len(line) - SEARCH_LINE))     # the match stays in view
                    hits.append(f"{rel}:{n}: {'…' if start else ''}{line[start:start + SEARCH_LINE]}"
                                f"{'…' if start + SEARCH_LINE < len(line) else ''}")
                    if len(hits) == SEARCH_MATCHES:
                        stopped = f"at {SEARCH_MATCHES} matches"
                        break
            if stopped:
                break
        out, counts = redact("\n".join(hits), self.secrets())
        tail = [f"(stopped {stopped}; narrow it with path or glob)"] if stopped else []
        tail += [f"(skipped {n} file{'' if n == 1 else 's'} {why})" for why, n in sorted(skipped.items())]
        result = "\n".join(([out] if out else ["(no matches)"]) + tail)
        return result, f"{len(hits)} matches in {files} files" + (f", stopped {stopped}" if stopped else "")

    def files(self, path):
        """One directory as a file tree shows it: each entry, whether it looks like it holds secrets, and
        whether reading it would ask first. Contents are read only to classify them; none of this reaches
        the model. secret is None when hoplon would not read the file at all (too big, binary, unreadable)."""
        parts = split_rel(path) if path not in ("", ".", "./") else []
        rows = []
        entries, truncated = self.wd.list(path)
        excused = self.cfg.not_secret()
        for name, kind, size in entries:
            sub = parts + [name]
            row = {"name": name, "kind": kind, "size": size, "hidden": is_hidden(sub)}
            if kind == "file":
                try:
                    row["secret"] = self.secret_text(self.wd.read("/".join(sub)), excused)
                except (ValueError, OSError):
                    row["secret"] = None
                row["asks"] = not self.read_allowed(sub) or row["secret"] is not False
            rows.append(row)
        return rows, truncated

    def view(self, path):
        """A file for the user's viewer: read through the same checks as the model's reads, with the endpoint key
        masked. It goes to the wrapper only, never into the session. Returns (text, size, secret, redacted)."""
        text = self.wd.read(path)
        secret = self.secret_text(text)
        shown, counts = redact(text, self.secrets())
        self.audit.log("user_read", path=path, chars=len(text), secret=secret)
        return shown, len(text.encode()), secret, counts, text_digest(text)

    def standing(self, p):
        """The one place that answers "is this already approved, and by what?": the rule (AUTO_RULES) that approves
        p without asking, or None, and p asks. decide() calls it once for each proposal, after match_plan has held
        the proposal to an approved plan. SECURITY.md describes each rule.

        offline_run: a sandboxed run with the network off. coding_read: in coding mode, a read, listing or search
        that isn't hidden. plan_read: a read or listing an approved plan names. plain_command and blessed_auto: a
        scoped command its set, or your blessing, lets run. work_copy: an edit in a work session's copy.
        approved_file: with edits approved-files, a further edit to a file you approved one for. work_step: a work
        session's own finish or retry. A read is read now, so a file that looks secret asks even so."""
        k, f = p.kind, p.fields
        if k == "sandbox":
            return None if f["network"] else "offline_run"
        if k == "search":
            return "coding_read"
        if k in ("finish", "retry"):
            return "work_step"
        if k == "trusted":
            cmd = f["_cmd"]
            return None if not cmd.auto else "plain_command" if cmd.plain() else "blessed_auto"
        if k in ("read", "list"):
            parts = split_rel(f["path"]) if f["path"] not in (".", "./", "") else []
            rule = ("coding_read" if self.read_allowed(parts) else
                    "plan_read" if f.get("plan_step") and not is_hidden(parts) else None)
            return rule if rule and (k == "list" or self.read_now(p)) else None
        if k in ("edit", "write"):
            if self.work:
                return "work_copy" if self.work_edit(p) else None
            return "approved_file" if self.auto_edit(p) else None
        return None

    def grant(self, p, rule):
        """Approve p without asking, by one of the rules its kind allows (Kind.rules). decide() logs each one."""
        if rule not in PROPOSAL_KINDS[p.kind].rules:
            raise RuntimeError(f"rule {rule!r} may not approve a {p.kind} proposal")
        p.auto, p.rule = True, rule

    def read_now(self, proposal):
        """Read a file that a rule would let the model read, so what is sent is exactly what was checked for secrets.
        False if it looks secret, so it asks. A read that fails (a missing, binary or oversized file) is approved
        too and reports its error: there is nothing secret to ask about."""
        path = proposal.fields["path"]
        if proposal.fields.get("secret"):       # named like a file that holds secrets
            return False
        try:
            text = self.wd.read(path)
        except (ValueError, OSError) as e:
            proposal.fields["_read_error"] = e
            return True
        if self.secret_text(text):
            proposal.fields["secret"] = True
            proposal.fields["to"] = to = self.active()[0].base_url     # where it goes: a planner may be elsewhere
            self.ui.notice(f"{path} looks like it contains secrets, so reading it needs your OK; allowing sends it "
                           f"to {to}")
            return False
        proposal.fields["_read_text"] = text
        return True

    def remember_not_secret(self, path, text):
        """You said this file holds no secrets: don't ask about this exact text again. Not when it holds the endpoint
        key, which always counts."""
        if any(k and k in text for k in self.secrets()):
            self.ui.notice(f"{path} holds the endpoint's key, so it will still ask")
            return
        digest = text_digest(text)
        where = os.path.join(self.wd.root, path)
        self.cfg.set_not_secret(digest, where)
        self.audit.log("not_secret", path=where, sha256=digest)
        self.ui.notice(f"{path}: you said it holds no secrets, so it won't ask again until the file changes")

    def mark_not_secret(self, path, sha256, source):
        """You said this file holds no secrets, from your own command (:not_secret, or hoplon-tui's files pane), not
        the model's. sha256 is of the text you looked at: if the file has changed since, nothing is marked. Only a
        file caught by its contents can be marked: never a name that is a secret by definition (.env, id_rsa, *.key),
        never anything in .git, never a file holding the endpoint key. Raises HoplonError (kind not_secret)."""
        def no(why):
            raise HoplonError(f"{show_exact(path)} not marked: {why}", kind="not_secret")
        try:
            parts = split_rel(path)
        except ValueError as err:
            no(str(err))
        if any(secret_name(p) for p in parts):
            no("its name says it holds secrets, so the sandbox always hides it")
        if ".git" in parts:
            no("files in .git can't be marked")
        try:
            text = self.wd.read("/".join(parts))
        except (ValueError, OSError) as err:
            no(str(err))
        if any(k and k in text for k in self.secrets()):
            no("it holds the endpoint's key")
        if not looks_secret(text):
            no("nothing in it looks like a secret, so there is nothing to mark")
        digest = text_digest(text)
        if digest != sha256:
            no("it changed since you looked at it; look again")
        where = os.path.join(self.wd.root, *parts)
        self.cfg.set_not_secret(digest, where)
        self.audit.log("not_secret", path=where, sha256=digest, source=source)
        return {"path": "/".join(parts), "sha256": digest}

    def read_allowed(self, parts):
        """Would reading this path be allowed without asking? (coding mode, not hidden)"""
        return bool(self.session.coding and not is_hidden(parts) and self.ui.performs)

    def decide(self, props):
        for p in props:
            if not p.error:
                self.match_plan(p)
                if p.kind in ("edit", "write") and self.work:
                    p.fields["in_copy"] = True      # it lands in the copy, asked or not: the card says so
                rule = self.standing(p)
                if rule:
                    self.grant(p, rule)
                    if rule in ("work_copy", "approved_file"):
                        p.fields["work" if rule == "work_copy" else "approved_file"] = True
        for p in props:
            p.decision = None
            if p.error:
                self.audit.log("invalid_call", id=p.id, tool=p.fields.get("tool"), error=p.error)
                if p.reply:
                    self.ui.notice(f"[{p.id}] {p.error}")
                continue
            rec = {k: v for k, v in p.event().items() if k not in ("type", "content", "diff", "command", "steps")}
            if p.kind in ("command", "sandbox"):
                rec.update(command_fingerprint(p.fields["command"]))
            if p.kind == "plan":
                rec["steps"] = [audit_step(st) for st in p.fields["steps"]]
            self.audit.log("proposal", **rec)
        live = [p for p in props if not p.error]
        for p in live:
            if p.auto:          # every approval by a rule, in the same record a user's decision gets
                self.audit.log("decision", id=p.id, decision="accept", by=p.rule)
        for p in live:
            if p.kind == "samples":
                self.ui.show_samples(p)
            elif PROPOSAL_KINDS[p.kind].shown:
                self.ui.show_command(p)
            elif p.auto:
                p.decision = "accept"
                self.ui.show_auto(p)
        if any(PROPOSAL_KINDS[p.kind].shown for p in live):
            self.notify("command")      # once, however many commands were shown
        pending = [p for p in live if not PROPOSAL_KINDS[p.kind].shown and not p.auto]
        ep = self.active()[0] if self.work else None
        for p in pending:       # a read for a planner elsewhere: the question says where it goes
            if ep and ep is not self.endpoint and p.kind in ("read", "list", "search") and "to" not in p.fields:
                p.fields["to"] = ep.base_url
                self.ui.notice(f"allowing {p.id} sends what it reads to the planner, at {ep.base_url}")
        if pending:
            self.notify("approval", count=len(pending), kinds=sorted({p.kind for p in pending}))
            self.ui.decide(pending)
        for p in pending:
            if p.kind != "external":    # the wrapper's answer (ok, declined, error) is its result, not a decision
                p.decision = p.decision if p.decision in ("accept", "decline") else "decline"
                self.audit.log("decision", id=p.id, decision=p.decision)
                if p.kind == "skills" and p.decision != "accept":
                    self.declined_skills.update(p.fields["names"])     # the model may not suggest them again
            if p.kind in ("edit", "write") and self.edits == "approved-files":
                key = self.file_key(p.fields.get("path"))
                if p.decision == "accept" and key and key not in self.approved_files:
                    self.approved_files.add(key)
                    self.audit.log("edits_remember", path=key, id=p.id)
                    self.ui.edits(self.edits_info())
                elif p.decision != "accept" and key in self.approved_files:
                    self.approved_files.discard(key)        # a decline: that file asks again
                    self.audit.log("edits_forget", path=key, id=p.id)
                    self.ui.edits(self.edits_info())
            if p.kind == "plan" and p.decision == "accept":
                self.adopt_plan(p)

    def match_plan(self, p):
        """Hold a proposal to the approved plan. A planned read is labelled with its step, and standing() approves it
        (unless the file is hidden or looks secret); a planned command, word for word as approved, is marked
        preapproved, for the wrapper to run without asking again; a planned edit or write is labelled, and still
        asks. A command, edit or write the
        plan doesn't name is flagged off_plan. Each approved command step is preapproved once; proposed again, it is
        marked plan_repeat and asks. hoplon still runs nothing itself."""
        if not self.plan or p.kind not in ("command", "read", "list", "edit", "write"):
            return
        steps = [st for st in self.plan["steps"] if st["approved"]]
        if p.kind == "command":
            same = [st for st in steps if st["kind"] == "command" and st["command"] == p.fields["command"]]
            hit = next((st for st in same if st["status"] == "todo"), None)
            if hit is None and same:            # run once already: in the plan, but it asks again
                p.fields["plan_step"], p.fields["plan_repeat"] = same[0]["n"], True
                self.audit.log("plan_repeat", id=p.id, step=same[0]["n"])
                return
        else:
            try:
                where = "/".join(split_rel(p.fields["path"])) if p.fields["path"] not in (".", "./", "") else ""
            except ValueError:
                return
            kinds = ("read",) if p.kind in ("read", "list") else ("edit", "write")
            hit = next((st for st in steps if st["kind"] in kinds and st["_where"] == where), None)
        if hit is None:
            if p.kind in ("command", "edit", "write"):
                p.fields["off_plan"] = True
            return
        p.fields["plan_step"] = hit["n"]
        if p.kind == "command":
            p.fields["preapproved"] = True
            p.fields["plan_id"] = self.plan["id"]
            hit["status"] = "done"
        self.audit.log("plan_match", id=p.id, step=hit["n"], kind=p.kind)
        self.ui.plan(self.plan)

    def adopt_plan(self, p):
        approve = getattr(p, "approve", None)
        edits = getattr(p, "edits", None) or {}
        steps = []
        for st in p.fields["steps"]:
            st = dict(st)
            st["approved"] = approve is None or st["n"] in approve
            if st["kind"] == "command" and st["approved"] and edits.get(st["n"]):
                st["command"], st["edited"] = edits[st["n"]], True
            st["status"] = "todo" if st["approved"] else "left out"
            st["_where"] = "/".join(split_rel(st["path"])) if st.get("path") not in (None, ".", "./", "") else ""
            steps.append(st)
        self.plan = {"id": p.id, "goal": p.fields["goal"], "steps": steps}
        self.audit.log("plan", id=p.id, approved=[st["n"] for st in steps if st["approved"]],
                       edited=[st["n"] for st in steps if st.get("edited")])
        self.ui.plan(self.plan)

    def clear_plan(self):
        if self.plan:
            self.audit.log("plan_cleared", id=self.plan["id"])
        self.plan = None
        self.ui.plan(None)

    def perform(self, p):
        """Carry out a decided proposal and return what the model is told. A kind the wrapper answers runs whatever
        the answer; any other runs only if accepted."""
        if p.error:
            return p.reply or f"error: {p.error}"
        kind = PROPOSAL_KINDS[p.kind]
        if not (kind.shown or kind.answered) and p.decision != "accept":
            return "declined by the user"
        return getattr(Gate, kind.perform)(self, p)

    def perform_command(self, p):
        return "shown to the user, not run. If they run it, they may send you the output."

    def perform_skills(self, p):
        """Switch them on, carried by this call's result. A skill that can't be (its blessing changed since) is
        reported, and the others still go on."""
        p.carry, failed = {}, []
        for n in p.fields["names"]:
            try:
                self.set_skill(n, True, source="suggestion", carry=False)
            except HoplonError as e:
                failed.append(f"{n} ({e})")
                continue
            s = next(s for s in self.skills if s.name == n)
            p.carry[n] = self.skill_block(s, "switched on by the user at your suggestion")
        self.ui.skills_changed(self.skill_rows())
        head = (f"switched on: {', '.join(p.carry)}." if p.carry else "none was switched on.") + \
            (f" Not switched on: {'; '.join(failed)}." if failed else "")
        return head + "".join(p.carry.values())

    def perform_samples(self, p):
        return ("shown to the user, not run: nothing runs for a sample request. Wait for them: once they have put the samples in the folder and approved them, "
                "run_in_sandbox sees them read-only at /fixtures.")

    def fixtures_store(self):
        """This job's samples folder (not created until something is added)."""
        if getattr(self, "fixtures", None) is None:
            root = os.path.realpath(self.wd.root if not self.work else self.work.home.root)
            self.fixtures = Fixtures(self.cfg.fixtures_dir, root, self.session.id or f"run-{os.getpid()}")
        return self.fixtures

    def fixtures_info(self):
        fx = self.fixtures_store()
        try:
            rows = fx.scan(self.secrets())
            problem = fx.problem()
        except (HoplonError, OSError) as e:
            rows, problem = [], str(e)
        return {"dir": fx.dir, "files": rows, "digest": Fixtures.digest(rows), "approved": bool(fx.approved),
                "problem": problem, "ask": FIXTURES_ASK}

    def fixtures_action(self, action, source, paths=(), cwd=None, digest=None):
        """add, use (approve exactly what was listed), drop (forget the approval) or clear (delete the folder)."""
        fx = self.fixtures_store()
        if action == "add":
            names = fx.add(paths, cwd or os.getcwd())
            self.audit.log("fixtures_add", files=names, dir=fx.dir, source=source)
        elif action == "use":
            rows = fx.approve(digest, self.secrets())
            self.audit.log("fixtures_use", digest=digest, files=[[r["name"], r["size"], r["sha256"]] for r in rows],
                           source=source)
        elif action == "drop":
            fx.approved = None
            self.audit.log("fixtures_drop", source=source)
        elif action == "clear":
            fx.clear()
            self.audit.log("fixtures_clear", dir=fx.dir, source=source)
        else:
            raise HoplonError("fixtures: unknown action", EXIT_USAGE, "bad_message")
        return self.fixtures_info()

    def perform_plan(self, p):
        if p.decision != "accept" or not self.plan or self.plan["id"] != p.id:
            return "declined by the user: the plan was not approved. Ask what they want instead."
        return "The user approved this plan. Carry it out step by step, without asking first.\n" + plan_text(self.plan)

    def perform_work_plan(self, p):
        w = self.work
        if p.decision != "accept" or not w:
            return "declined by the user: the plan was not approved. Stop and ask what they want instead."
        w.goal = p.fields["goal"]
        w.items = [{"n": n, "title": t, "status": "todo", "commit": None} for n, t in enumerate(p.fields["items"], 1)]
        w.current = 1
        w.reports = []          # a new plan numbers its items afresh; what was done is committed on the branch
        w.fresh(renew=True)
        if w.role == "item":
            # An item's context replanned: the planner's context stays its own (an item's conversation doesn't go to
            # a planner elsewhere), and is told of the new plan.
            w.planner = (w.planner or []) + [{"role": "user", "content": "An item's context replaced the plan, and the user "
                                      "approved the new one:\n" + work_text(w)}]
        else:
            w.role = "plan"     # the planner's context, or the first plan's: it plans from now on
        w.swap = ("item", None, "the plan was approved")
        self.audit.log("work_plan", id=p.id, items=len(w.items))
        self.ui.work({"state": "plan", **w.info()})
        return ("The user approved this plan. Work through it item by item, without asking; call finish_item when "
                "each is done.\n" + work_text(w))

    def perform_retry(self, p):
        w = self.work
        w.swap = ("item", p.fields["hint"], "the planner asked for a fresh attempt")
        self.audit.log("work_retry", id=p.id, item=p.fields["n"])
        self.ui.result(p, "ok", f"a fresh attempt at item {p.fields['n']} starts now")
        return f"A fresh attempt at item {p.fields['n']} starts now, with your hint."

    def perform_finish(self, p):
        """Finish the current item: the test command must pass in the sandbox, then everything is committed."""
        w, n = self.work, p.fields["n"]
        if w.test and self.box.network:     # set_network refuses this; checked again, as the test doesn't ask
            self.ui.result(p, "error", f"item {n} not finished: the sandbox's network is on")
            return "not finished: the sandbox's network is on, and the test only runs offline. Ask the user."
        if w.test and w.left() < 1:
            why = f"the work session has run {w.seconds // 60} min without you"
            self.ui.result(p, "skipped", f"item {n} not finished: {why}")
            return f"not finished: {why}, so the test did not run"
        if w.test:
            try:
                ran, rc, out, note = self.box.run(w.test, self.secrets(), timeout=w.left())
            except KeyboardInterrupt:
                p.stopped = True
                self.ui.result(p, "stopped", "the test command was stopped")
                return "stopped by the user before the test command finished"
            except HoplonError as e:
                ran, rc, out, note = False, None, "", str(e)
            out = clip(redact(out, self.secrets())[0])
            let = self.box.let_through if ran and isinstance(getattr(self.box, "let_through", None), list) else []
            self.audit.log("work_test", id=p.id, item=n, exit=rc, ran=ran, **({"let_through": let} if let else {}))
            if not ran or rc != 0:
                why = f"the test command could not run: {note}" if not ran else \
                    (f"the test command failed (exit {rc})" if rc is not None else f"the test command was {note}")
                self.ui.result(p, "error", f"item {n} not finished: {why}", output=out, exit=rc)
                return f"not finished: {why}. Output:\n" + (out if out.strip() else "(none)")
        try:
            summary, notes = p.fields["summary"], p.fields["notes"]
            title = short_title(summary)
            body = [summary] if title != summary else []
            body.append(f"Plan item: {w.items[n - 1]['title']}")
            if notes:
                body.append("Notes for later items:\n" + notes)
            sha = w.tree.commit(f"{n}. {title}\n\n" + "\n\n".join(body))
        except HoplonError as e:
            self.ui.result(p, "error", f"item {n} not committed: {e}")
            return f"error: not committed: {e}"
        if sha is None:
            self.ui.result(p, "error", f"item {n} not finished: nothing changed since the last item")
            return "not finished: no files changed since the last item. Make the changes first."
        w.items[n - 1].update(status="done", commit=sha)
        w.reports.append({"n": n, "summary": p.fields["summary"], "notes": p.fields["notes"], "commit": sha})
        self.audit.log("work_item", id=p.id, item=n, commit=sha)
        nxt = next((it for it in w.items if it["status"] == "todo"), None)
        w.current = nxt["n"] if nxt else 0
        w.fresh()
        if nxt:
            w.swap = ("item", None, f"item {n} was finished")
        self.ui.result(p, "ok", f"item {n} committed as {sha}")
        try:
            w.tree.publish()
        except HoplonError as e:     # it's committed in the copy; the end of the session tries again
            self.ui.notice(f"[!] item {n} is committed in the copy, but the branch couldn't be updated: {e}")
        self.ui.work({"state": "item", **w.info()})
        if nxt is None:
            return f"Item {n} committed as {sha}. Every item is done: reply with a short summary of the work."
        return f"Item {n} committed as {sha}. Next: item {nxt['n']}: {nxt['title']}"

    def perform_external(self, p):
        status = p.decision if p.decision in ("ok", "declined", "error") else "declined"
        out, counts = redact(str(p.result or ""), self.secrets())
        self.audit.log("external_result", id=p.id, tool=p.fields["tool"], status=status,
                       chars=len(out), redacted=counts)
        return f"{status}" + (f":\n{clip(out)}" if out else "")

    def perform_file(self, p):
        """read, list, search, write and edit, through the working directory's checks."""
        try:
            if p.kind == "read":
                if p.auto and "_read_error" in p.fields:
                    raise p.fields["_read_error"]
                text = p.fields["_read_text"] if p.auto else self.wd.read(p.fields["path"])
                result = text
                note = f"read {len(text)} characters"
                if p.fields.get("_not_secret") and not p.auto:
                    self.remember_not_secret(p.fields["path"], text)
                if self.secret_text(text):
                    self.ui.notice(f"warning: {p.fields['path']} looks like it contains secrets; it was sent as is")
            elif p.kind == "search":
                result, note = self.search(p)
            elif p.kind == "list":
                rows, truncated = self.wd.list(p.fields["path"])
                lines = [f"{n}/" if k == "dir" else f"{n}  ({s} bytes)" if k == "file" else f"{n}  [{k}]"
                         for n, k, s in rows]
                if truncated:
                    lines.append(f"... listing stopped after {LIST_CAP} entries")
                result = "\n".join(lines) or "(empty directory)"
                note = f"listed {len(rows)} entries"
            elif p.kind == "write":
                n = self.wd.write(p.fields["path"], p.fields["content"], expected=p.fields["_state"])
                result = note = f"wrote {p.fields['path']} ({n} bytes)"
            else:
                self.wd.write(p.fields["path"], p.fields["_content"], expected=p.fields["_state"])
                result = note = f"edited {p.fields['path']}"
        except StaleApproval:
            self.audit.log("file_op", id=p.id, kind=p.kind, path=p.fields["path"], status="stale")
            self.ui.result(p, "error", f"{p.fields['path']} changed after you reviewed it, so nothing was written")
            return (f"not applied: {p.fields['path']} changed since the user reviewed your proposal; "
                    "propose the change again")
        except (ValueError, OSError) as e:
            self.audit.log("file_op", id=p.id, kind=p.kind, path=p.fields.get("path"), status="error", error=str(e))
            self.ui.result(p, "error", str(e))
            return f"error: {e}"
        self.audit.log("file_op", id=p.id, kind=p.kind, path=p.fields.get("path"), status="ok", auto=p.auto,
                       **({"approved_file": True} if p.fields.get("approved_file") else {}))
        key = self.file_key(p.fields["path"]) if self.work and p.kind in ("write", "edit") else None
        if key:
            sub = self.work.tree.sub
            self.work.written.add(key if sub == "." else f"{sub}/{key}")
        self.ui.result(p, "ok", note)
        step = p.fields.get("plan_step")
        if step and self.plan:
            for st in self.plan["steps"]:
                if st["n"] == step:
                    st["status"] = "done"
            self.ui.plan(self.plan)
        return result

    def run_sandboxed(self, p):
        """Run an accepted run_in_sandbox call. The model gets the exit code and the output, redacted and clipped;
        the user sees the same."""
        where = "online" if p.fields["network"] else "offline"
        left = self.work.left() if self.work else None
        if left is not None and left < 1:
            why = f"the work session has run {self.work.seconds // 60} min without you"
            self.ui.result(p, "skipped", f"not run: {why}")
            return f"not run: {why}"
        started = time.monotonic()
        try:
            fx = self.fixtures_store()
            mount = fx.mount(self.secrets())
            if mount and self.work:
                fx.used_in_work = True
            ran, rc, out, note = self.box.run(p.fields["command"], self.secrets(), timeout=left, fixtures=mount)
        except KeyboardInterrupt:
            p.stopped = True
            self.audit.log("sandbox_exit", id=p.id, exit=None, stopped=True)
            self.ui.result(p, "stopped", "the container was killed")
            return "stopped by the user before it finished"
        except HoplonError as e:
            ran, rc, out, note = False, None, "", str(e)
        secs = time.monotonic() - started
        out, counts = redact(out, self.secrets())
        out = clip(out)
        let = self.box.let_through if ran and isinstance(getattr(self.box, "let_through", None), list) else []
        self.audit.log("sandbox_exit", id=p.id, exit=rc, network=p.fields["network"], seconds=round(secs, 1),
                       chars=len(out), redacted=counts, note=note, **({"let_through": let} if let else {}))
        if not ran:
            self.ui.result(p, "error", f"not run: {note}", output="", exit=None)
            return f"error: not run: {note}"
        head = f"exit {rc}" if rc is not None else note
        extra = [note] if note and rc is not None else []
        if counts:
            extra.append(f"redacted {redaction_note(counts)}")
        self.ui.result(p, "ok" if rc == 0 else "error", "; ".join([f"{head} in {secs:.1f} s"] + extra),
                       output=out, exit=rc)
        return f"{head} (sandbox, {where}, {secs:.1f} s). Output:\n" + (out if out.strip() else "(none)")

    def perform_trusted(self, p):
        """Run an accepted scoped command for the model: the exit code and the output, redacted and clipped."""
        r = self.run_trusted(p.fields["_cmd"], p.fields["params"], "model")
        if r["error"]:
            self.ui.result(p, "error", f"not run: {r['error']}", output="", exit=None)
            return f"error: not run: {r['error']}"
        rc, note = r["exit"], r["note"]
        head = f"exit {rc}" if rc is not None else note
        extra = [note] if note and rc is not None else []
        if r["cached"] is not None:
            extra.append(f"cached {r['cached']} s ago")
        if r["redacted"]:
            extra.append(f"redacted {redaction_note(r['redacted'])}")
        self.ui.result(p, "ok" if rc == 0 else "error", "; ".join([f"{head} in {r['ms']} ms"] + extra),
                       output=r["output"], exit=rc)
        return head + (f" ({'; '.join(extra)})" if extra else "") + ". Output:\n" \
            + (r["output"] if r["output"].strip() else "(none)")

    def run_shell(self, p, command, show):
        """The REPL's :run: a command proposal, as the user edited and then confirmed it, run in their shell. Each
        chunk of output goes to show as it comes. Returns (exit, output with ANSI codes removed), or None when ^C came
        before it started."""
        self.audit.log("run", id=p.id, **command_fingerprint(command), edited=command != p.fields["command"],
                       sandbox=self.sandbox)
        ran = run_in_shell(command, show)
        if ran is not None:
            self.audit.log("run_exit", id=p.id, exit=ran[0], sandbox=self.sandbox)
        return ran

    def run_user(self, cset, command, params):
        """A scoped command the user asked for (a wrapper's run message): the same checks and limits as the
        model's. Raises HoplonError when it can't be asked for; otherwise returns run_trusted's result."""
        cmd = next((c for p in self.command_sets if p.name == cset for c in p.commands.values() if c.name == command),
                   None)
        if not cmd:
            raise HoplonError(f"no command {one_line(str(cset))[0]}.{one_line(str(command))[0]} in an enabled command set",
                             EXIT_USAGE, "unknown_command")
        if cmd.as_user and not cmd.auto:
            raise HoplonError(f"{one_line(cset)[0]}.{one_line(command)[0]} runs as {cmd.as_user}, so it asks every time: "
                             "it can't be run from here", EXIT_USAGE, "asks")
        try:
            values = cmd.check(params)
        except ValueError as e:
            raise HoplonError(str(e), EXIT_USAGE, "bad_params") from None
        return self.run_trusted(cmd, values, "user")

    def run_trusted(self, cmd, values, source):
        """Run a scoped command whose parameters are already checked. Refused, not run, when the program changed,
        when the same command ran less than its min_interval ago, or after 30 real runs in a minute; a result
        younger than the command's cache time is returned again instead. Returns {error, exit, output, note,
        cached (seconds, or None), ms, redacted}."""
        res = {"error": None, "exit": None, "output": "", "note": "", "cached": None, "ms": 0, "redacted": {}}
        limited = False

        def finish():
            self.audit.log("trusted_run", set=cmd.set, command=cmd.name, params=values, source=source,
                           exit=res["exit"], ms=res["ms"], bytes=len(res["output"].encode()),
                           cached=res["cached"] is not None, rate_limited=limited, binary=cmd.binary,
                           error=res["error"], redacted=res["redacted"])
            return res

        key, now_ = (cmd.set, cmd.name), time.monotonic()
        ckey = key + (tuple(sorted(values.items())),)
        problem = cmd.changed()
        if problem:
            res["error"] = problem
            return finish()
        hit = self.cached.get(ckey)
        if cmd.cache and hit and now_ - hit[0] < cmd.cache:
            res.update(exit=hit[1], output=hit[2], note=hit[3], cached=int(now_ - hit[0]))
            return finish()
        while self.runs and now_ - self.runs[0] >= 60:
            self.runs.popleft()
        wait = max(cmd.min_interval - (now_ - self.last_run[key]), 0) if key in self.last_run else 0
        if len(self.runs) >= TRUSTED_PER_MINUTE:
            wait = max(wait, 60 - (now_ - self.runs[0]))
        if wait > 0:
            limited = True
            res["error"] = f"rate limited: try again in {int(wait) + 1} s"
            return finish()
        self.last_run[key] = now_
        self.runs.append(now_)
        try:
            rc, out, err, note = run_program(cmd.build(values), cmd.env(), os.getcwd(), cmd.timeout, cmd.max_output)
        except OSError as e:
            res["error"] = f"could not start the program: {e.strerror}"
            return finish()
        text = strip_ansi(out.decode("utf-8", "replace"))
        if err.strip():
            text += ("" if text.endswith("\n") or not text else "\n") + "[stderr]\n" \
                + strip_ansi(err.decode("utf-8", "replace"))
        text, counts = redact(text, self.secrets())
        res.update(exit=rc, output=clip(text), note=note, redacted=counts, ms=int((time.monotonic() - now_) * 1000))
        if cmd.cache:
            self.cached.pop(ckey, None)
            self.cached[ckey] = (now_, rc, res["output"], note)
            while len(self.cached) > TRUSTED_CACHE:
                del self.cached[next(iter(self.cached))]
        return finish()

    # -- sharing output and compaction

    def share(self, source, text, ref=None):
        """Record that reviewed output is being sent to the model."""
        self.audit.log("shared", source=source, ref=ref, chars=len(text),
                       sha256=hashlib.sha256(text.encode("utf-8", "replace")).hexdigest())

    def secret_text(self, text, excused=None):
        """Does this text look like it holds secrets? The endpoint key always counts; the patterns don't for text you
        said holds none (not-secret.json, matched by its sha256, so any change asks again)."""
        if any(k and k in text for k in self.secrets()):
            return True
        if not looks_secret(text):
            return False
        return text_digest(text) not in (self.cfg.not_secret() if excused is None else excused)

    def secrets(self):
        """Exact values to mask in text bound for the model: the endpoint keys."""
        w = getattr(self, "work", None)
        return [getattr(self.client, "_key", None)] + ([w.planner_ep[1]._key] if w and w.planner_ep else [])
