# hoplon_core/config.py: configuration: profiles, endpoints, skills, blessing, remote config and the notify hook.
#
# Config and Profile, finding and loading named items, blessing (a hash of exactly what was approved), what goes to
# a remote host with a session, and Notifier, which runs the user's own notify hook.
#
# Uses: base, client, commands, sandbox

# --------------------------------------------------------------------------
# Config, blessing, skills, endpoints and profiles

# Every setting hoplon reads from config.json, with its default. Keys starting with "_" are comments.
STARTER_CONFIG = {
    "_help": "default_profile: profile (persona) used when -p is not given. "
             "default_endpoint: endpoint used when neither -e nor the profile names one. "
             "default_skills: skills used when the profile doesn't list its own (-k adds more). "
             "default_commands: command sets (scoped commands) used when the profile doesn't list its own "
             "(--commands adds more). "
             "endpoint_dirs, profile_dirs, skill_dirs, command_dirs: extra directories to search, after the ones here. "
             "fixtures_dir: where /fixtures keeps real samples for the model's test runs (default: "
             "~/.local/state/hoplon/fixtures); outside any project. "
             "max_steps: model steps in a row that run without you deciding anything before the turn pauses "
             "(default 50; a step where you approve or decline something starts the count again). "
             "alerts: false keeps the notify hook quiet (:alerts on|off switches it for a session). "
             "Endpoints, profiles, skills and command sets must be blessed: hoplon --bless PATH",
    "default_profile": None,
    "default_endpoint": None,
    "default_skills": [],
    "default_commands": [],
    "endpoint_dirs": [],
    "profile_dirs": [],
    "skill_dirs": [],
    "command_dirs": [],
    "fixtures_dir": None,
    "max_steps": STEP_LIMIT,
    "alerts": True,
}


class Config:
    def __init__(self, home=None):
        self.home = state_directory(home or config_home())
        data = read_state_json(os.path.join(self.home, "config.json"), {}) or {}
        if not isinstance(data, dict):
            raise HoplonError("config.json must be a JSON object")
        unknown = sorted(k for k in data if k not in STARTER_CONFIG and not k.startswith("_"))
        if unknown:
            eprint(f"hoplon: warning: config.json: unknown setting(s) ignored: {', '.join(unknown)} "
                   f"(known: {', '.join(k for k in STARTER_CONFIG if not k.startswith('_'))})")
        for k in ("default_profile", "default_endpoint"):
            v = data.get(k)
            if v is not None and not (isinstance(v, str) and NAME_RE.fullmatch(v)):
                raise HoplonError(f"config.json: {k} must be a name or null")
        for k in ("default_skills", "default_commands", "endpoint_dirs", "profile_dirs", "skill_dirs", "command_dirs"):
            v = data.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise HoplonError(f"config.json: {k} must be a list of strings")
        fx = data.get("fixtures_dir")
        if fx is not None and not (isinstance(fx, str) and fx.strip()):
            raise HoplonError("config.json: fixtures_dir must be a directory or null")
        self.fixtures_dir = os.path.abspath(os.path.expanduser(fx)) if fx else fixtures_home()
        if not isinstance(data.get("alerts", True), bool):
            raise HoplonError("config.json: alerts must be true or false")
        self.alerts = data.get("alerts", True)
        steps = data.get("max_steps", STEP_LIMIT)
        if type(steps) is not int or not 1 <= steps <= 1000:
            raise HoplonError("config.json: max_steps must be a whole number from 1 to 1000")
        self.max_steps = steps
        self.default_profile = data.get("default_profile")
        self.default_endpoint = data.get("default_endpoint")
        self.default_skills = list(data.get("default_skills", []))
        self.default_commands = list(data.get("default_commands", []))

        def dirs(name):
            return [os.path.join(self.home, name)] + [os.path.expanduser(p) for p in data.get(name[:-1] + "_dirs", [])]
        self.endpoint_dirs, self.profile_dirs, self.skill_dirs = dirs("endpoints"), dirs("profiles"), dirs("skills")
        self.command_dirs = dirs("commands")
        self.blessed_path = os.path.join(self.home, "blessed.json")
        self.not_secret_path = os.path.join(self.home, "not-secret.json")
        self.sessions = os.path.join(self.home, "sessions")
        state_directory(self.sessions)
        if os.path.islink(self.sessions):
            raise HoplonError(f"unsafe hoplon state directory {self.sessions}: symlinks are refused")
        self.audit = Audit(os.path.join(self.home, "audit.log"))

    def ensure(self):
        """First run: create the config tree (0700) and a starter config.json. Existing
        directories and files are left as they are; a failure is a warning, not an error."""
        fresh = not os.path.exists(self.home)
        try:
            missing, d = [], os.path.abspath(self.home)
            while not os.path.lexists(d):
                missing.append(d)
                d = os.path.dirname(d)
            for d in reversed(missing):                       # ~/.config too, whatever the umask
                os.mkdir(d, 0o700)
            state_directory(self.home)
            for d in (self.endpoint_dirs[0], self.profile_dirs[0], self.skill_dirs[0], self.command_dirs[0],
                      self.sessions):
                if not os.path.isdir(d):
                    os.mkdir(d, 0o700)
            conf = os.path.join(self.home, "config.json")
            if not os.path.exists(conf):
                write_private(conf, json.dumps(STARTER_CONFIG, indent=2) + "\n")
        except OSError as e:
            eprint(f"hoplon: warning: cannot create {self.home}: {e}")
            return
        if fresh:
            eprint(f"hoplon: created {self.home} (config.json, endpoints/, profiles/, skills/, commands/, sessions/)")

    def blessed(self):
        b = read_state_json(self.blessed_path, {}) or {}
        return b if isinstance(b, dict) else {}

    def set_blessed(self, path, entry):
        b = self.blessed()
        b[path] = entry
        write_private(self.blessed_path, json.dumps(b, indent=2, sort_keys=True) + "\n")

    def not_secret(self):
        """Contents you said hold no secrets though they look as if they do: sha256 of the text -> where you saw it.
        Only that exact text is excused; the endpoint key always counts."""
        n = read_state_json(self.not_secret_path, {}) or {}
        return n if isinstance(n, dict) else {}

    def set_not_secret(self, digest, path):
        n = self.not_secret()
        n[digest] = {"path": path, "when": datetime.datetime.now().isoformat(timespec="seconds")}
        write_private(self.not_secret_path, json.dumps(n, indent=2, sort_keys=True) + "\n")


class Skill:
    """A skill, read once. Its hash and every byte used later come from this one snapshot;
    nothing reopens the files, so editing them afterwards changes nothing until the next load."""

    def __init__(self, name, path, files=None):
        """files: the skill's bytes as sent by hoplon-tui from your own machine (remote mode); path is then only a
        label, and nothing is read here."""
        self.name, self.path = name, path
        try:
            self.files = snapshot_skill(path) if files is None else files
            self.hash = hash_snapshot(self.files)
        except (HoplonError, OSError):
            self.files, self.hash = {}, None

    def text(self):
        return self.files.get("SKILL.md", b"").decode("utf-8", "replace")

    def description(self):
        for line in self.text().splitlines():
            if line.strip():
                return line.strip().lstrip("#").strip()
        return ""


def find_skills(cfg):
    seen = {}
    for d in cfg.skill_dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if n not in seen and os.path.isfile(os.path.join(p, "SKILL.md")):
                seen[n] = Skill(n, os.path.abspath(p))
    return seen


def skill_status(cfg, skill):
    return bless_status(cfg, "skill", skill.path, skill.hash)


def shown_name(name):
    """A skill's name as listings show it: as it is when it is a usable name, else escaped and cut short."""
    if SKILL_NAME.fullmatch(name):
        return name
    text = one_line(name)[0]
    return text[:48] + ("…" if len(text) > 48 else "")


def shown_description(skill):
    """A skill's description as listings show it: one line, escaped, at most SKILL_DESC_MAX characters."""
    text = one_line(skill.description())[0]
    return text[:SKILL_DESC_MAX] + ("…" if len(text) > SKILL_DESC_MAX else "")


def skill_rows(cfg, active=None, found=None):
    """Every skill found, for listings. active: names in use, marked when given. found: the skills your own machine
    sent (remote mode), which are the only ones there, and blessed by being sent. A skill whose name isn't usable
    is listed as "bad name", escaped; it can't be blessed or switched on. Every name and description here is escaped
    and capped, so a listing can't be faked or broken by what a skill's files say."""
    rows = []
    for n, s in sorted((find_skills(cfg) if found is None else found).items()):
        bad = not SKILL_NAME.fullmatch(n)
        row = {"name": shown_name(n), "path": s.path, "description": shown_description(s),
               "status": "bad name" if bad else skill_status(cfg, s) if found is None else "blessed"}
        if active is not None:
            row["active"] = n in active
        rows.append(row)
    return rows


def skill_lines(rows, mark=False):
    """A skill listing for people: one line per skill, then its full path, which is what --bless takes.
    An unblessed or changed skill gets the whole command. mark: start each line with * if it is in use."""
    out = []
    for r in rows:
        lead = ("* " if r.get("active") else "  ") if mark else ""
        out.append(f"{lead}{one_line(r['name'])[0]:20} {r['status']:10} {one_line(r['description'])[0]}")
        path = one_line(shlex.quote(r["path"]))[0]
        if r["status"] == "bad name":
            out.append(f"{' ' * len(lead)}    {path}: rename the directory to use it ({SKILL_NAME_RULE})")
            continue
        out.append(f"{' ' * len(lead)}    " + (path if r["status"] == "blessed" else f"to bless: hoplon --bless {path}"))
    return out


def load_skills(cfg, names, notice):
    """Return the usable skills among names; skip and report the rest."""
    found, out = find_skills(cfg), []
    for n in names:
        s = found.get(n) if SKILL_NAME.fullmatch(n) else None
        if not s:
            notice(f"skill '{shown_name(n)}' not found; skipped")
            continue
        st = skill_status(cfg, s)
        if st != "blessed":
            notice(f"skill '{n}' is {st}; skipped (read it, then: hoplon --bless {s.path})")
            continue
        out.append(s)
    return out


class Profile:
    """A persona: which endpoint, what to tell the model, which skills, sampling params, coding mode, and the
    sandbox the model may run commands in."""

    def __init__(self, data, path):
        self.path, self.data = path, data
        if not isinstance(data, dict):
            raise HoplonError(f"{path}: a profile must be a JSON object", EXIT_PROFILE)
        unknown = set(data) - PROFILE_KEYS
        if unknown:
            hint = " (this looks like an endpoint; endpoints go under endpoints/)" if "base_url" in data else ""
            raise HoplonError(f"{path}: unknown profile fields: {', '.join(sorted(unknown))}{hint}", EXIT_PROFILE)
        if not isinstance(data.get("name"), str) or not NAME_RE.fullmatch(data["name"]):
            raise HoplonError(f"{path}: 'name' is required (letters, digits, '.', '_', '-')", EXIT_PROFILE)
        self.name = data["name"]
        self.description = data.get("description", "")
        self.endpoint = data.get("endpoint")
        self.model = data.get("model")            # an alias of the endpoint, or the server's model name
        if self.model is not None and not (isinstance(self.model, str) and MODEL_ID.fullmatch(self.model)):
            raise HoplonError(f"{path}: 'model' must be a model name or one of the endpoint's aliases", EXIT_PROFILE)
        self.persona = data.get("persona", "")
        self.skills = data.get("skills")          # None: the profile leaves skills to default_skills
        self.command_sets = data.get("commands")       # likewise for default_commands
        self.params = data.get("params", {})
        self.coding = data.get("coding", False)
        if self.endpoint is not None and not (isinstance(self.endpoint, str) and NAME_RE.fullmatch(self.endpoint)):
            raise HoplonError(f"{path}: 'endpoint' must be an endpoint name", EXIT_PROFILE)
        if not isinstance(self.description, str) or not isinstance(self.persona, str):
            raise HoplonError(f"{path}: 'description' and 'persona' must be text", EXIT_PROFILE)
        if self.skills is not None and not (isinstance(self.skills, list) and all(isinstance(x, str) for x in self.skills)):
            raise HoplonError(f"{path}: 'skills' must be a list of skill names", EXIT_PROFILE)
        if self.command_sets is not None and not (isinstance(self.command_sets, list)
                                             and all(isinstance(x, str) for x in self.command_sets)):
            raise HoplonError(f"{path}: 'commands' must be a list of command set names", EXIT_PROFILE)
        if not isinstance(self.params, dict):
            raise HoplonError(f"{path}: 'params' must be an object", EXIT_PROFILE)
        for k in ("model", "messages", "tools"):
            if k in self.params:
                raise HoplonError(f"{path}: params may not set '{k}'", EXIT_PROFILE)
        if type(self.params.get("stream", True)) is not bool:
            raise HoplonError(f"{path}: params' 'stream' must be true or false", EXIT_PROFILE)
        if not isinstance(self.coding, bool):
            raise HoplonError(f"{path}: 'coding' must be true or false", EXIT_PROFILE)
        self.edits = data.get("edits", "ask")
        if self.edits not in EDITS_MODES:
            raise HoplonError(f"{path}: 'edits' must be \"ask\" or \"approved-files\"", EXIT_PROFILE)
        self.sandbox = parse_sandbox(data["sandbox"], path) if "sandbox" in data else None
        self.work = parse_work(data["work"], path) if "work" in data else None
        if self.work and not self.sandbox:
            raise HoplonError(f"{path}: 'work' needs a 'sandbox': a work session tests in it", EXIT_PROFILE)
        self.layout = data.get("layout", {})       # for hoplon-tui: which of its panes start shown
        if not (isinstance(self.layout, dict) and set(self.layout) <= set(LAYOUT_PANES)
                and all(v in ("shown", "hidden") for v in self.layout.values())):
            raise HoplonError(f"{path}: 'layout' must map {', '.join(LAYOUT_PANES)} to \"shown\" or \"hidden\"",
                              EXIT_PROFILE)

    @classmethod
    def load(cls, path):
        return cls.parse(read_bytes_or_missing(path, "profile"), path)

    @classmethod
    def parse(cls, raw, path):
        return cls(parse_json_bytes(raw, path), os.path.abspath(path))

    def summary(self):
        """The review screen. One-line fields are escaped; the persona carries its own warning.
        Returns (text, flagged), where flagged covers the one-line fields."""
        flags = []

        def one(v):
            d, f = one_line(str(v))
            flags.append(f)
            return d

        lines = [f"  profile:  {one(self.name)}"]
        if self.description:
            lines.append(f"  about:    {one(self.description)}")
        lines.append(f"  endpoint: {one(self.endpoint) if self.endpoint else '(none; -e or default_endpoint decides)'}")
        if self.model:
            lines.append(f"  model:    {one(self.model)} (a model or alias of the endpoint, instead of its own model)")
        if self.skills is None:
            lines.append("  skills:   (not set; default_skills apply)")
        else:
            lines.append(f"  skills:   {', '.join(one(k) for k in self.skills) or 'none'}")
        if self.command_sets:
            lines.append(f"  commands: {', '.join(one(k) for k in self.command_sets)} (scoped commands; each command set "
                         "must be blessed on its own)")
        if self.params:
            lines.append(f"  params:   {json.dumps(self.params, sort_keys=True)}")
        if self.coding:
            lines.append("  [!] grants coding mode: the model reads and lists files in your working directory "
                         "without asking (writes still ask)")
        if self.edits == "approved-files":
            lines.append("  [!] edits: approved-files: after you approve an edit or write to a file, further edits to "
                         "it apply without asking, for the rest of the session")
        if self.sandbox:
            b = self.sandbox
            lines.append(f"  sandbox:  podman, {'image ' + one(b['image']) if b['image'] else HOST_OS}, {b['timeout']} s limit: in coding mode the model "
                         "runs commands on a throwaway copy of the project, without asking")
            if b["network"]:
                lines.append("  [!] the sandbox starts with the network ON: code the model writes could send your "
                             "project anywhere, so each run asks first")
        if self.work:
            w = self.work
            test = f"test command {one(w['test'])}" if w["test"] else "no test command"
            lines.append(f"  work:     {w['steps_per_item']} steps per plan item, {w['time_limit'] // 60} min unattended, "
                         f"{test}")
            pl = w["planner"]
            if pl and pl["endpoint"]:
                lines.append(f"            plans with endpoint {one(pl['endpoint'])}"
                             + (f", model {one(pl['model'])}" if pl["model"] else "") + ", which also sees the project")
            elif pl:
                lines.append(f"            plans with model {one(pl['model'])}, on the same endpoint")
        if self.persona:
            lines.append("  persona (sent to the model with every request):")
            lines += ["    | " + ln for ln in show_exact(self.persona).splitlines()]
        else:
            lines.append("  persona:  (none)")
        return "\n".join(lines), any(flags)


KINDS = {"endpoint": Endpoint, "profile": Profile}


def kind_dirs(cfg, kind):
    return cfg.endpoint_dirs if kind == "endpoint" else cfg.profile_dirs


def _locate(cfg, kind, name):
    for d in kind_dirs(cfg, kind):
        p = os.path.join(d, name + ".json")
        if os.path.exists(p):
            return os.path.abspath(p)
    for path, entry in sorted(cfg.blessed().items()):
        if entry.get("kind") == kind and entry.get("name") == name and os.path.exists(path):
            return path
    return None


def find_named(cfg, kind, name):
    """Find an endpoint or profile by name: in its directories first, then wherever one was blessed."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise HoplonError(f"bad {kind} name '{name}'", EXIT_PROFILE)
    path = _locate(cfg, kind, name)
    if path:
        return path
    other = "profile" if kind == "endpoint" else "endpoint"
    hint = ""
    if _locate(cfg, other, name):
        flag = "-p" if other == "profile" else "-e"
        hint = f"\n  There is {'an' if other == 'endpoint' else 'a'} {other} by that name: use {flag} {name} " \
               f"(or default_{other} in config.json)."
    raise HoplonError(f"no {kind} named '{name}'. Nothing was sent.{hint}", EXIT_PROFILE)


def load_named(cfg, kind, path):
    """Read an endpoint or profile file once; return the parsed object and its status.
    The hash and the parse both come from the same bytes."""
    raw = read_bytes_or_missing(path, kind)
    return KINDS[kind].parse(raw, path), bless_status(cfg, kind, path, hash_bytes(raw))


def select_named(cfg, kind, name):
    """Load and check a blessed endpoint or profile."""
    path = find_named(cfg, kind, name)
    obj, st = load_named(cfg, kind, path)
    if st == "unblessed":
        raise HoplonError(f"{kind} '{name}' ({path}) is not blessed. Nothing was sent.\n"
                         f"  Review it, then: hoplon --bless {path}", EXIT_PROFILE)
    if st != "blessed":
        raise HoplonError(f"{kind} '{name}' ({path}) changed since it was blessed. Nothing was sent.\n"
                         f"  Review it again, then: hoplon --bless {path}", EXIT_PROFILE)
    return obj


REMOTE_ITEM_CAP = 1 << 20      # bytes of one skill or command set sent to a remote hoplon


def remote_items(raw, kind, bad):
    """The skills or command sets in a config line: {name: Skill or Command set}, made from the bytes your machine sent. Each
    one's hash is computed again here from those bytes and must be the one blessed there."""
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise bad(f"'{kind}s' must be a list")
    out = {}
    for item in raw:
        keys = {"name", "hash", "files"} | ({"auto"} if kind == "command_set" else set())
        if not isinstance(item, dict) or not {"name", "hash", "files"} <= set(item) <= keys:
            raise bad(f"each {kind} must have exactly 'name', 'hash' and 'files'"
                      + (" (and optionally 'auto')" if kind == "command_set" else ""))
        if not isinstance(item.get("auto", []), list) or not all(isinstance(x, str) for x in item.get("auto", [])):
            raise bad(f"{kind} '{one_line(str(item['name']))[0]}': 'auto' must be a list of command names")
        name, files = item["name"], item["files"]
        if (not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in out
                or (kind == "skill" and not SKILL_NAME.fullmatch(name))):
            raise bad(f"{kind} names must be distinct names (letters, digits, . _ -)"
                      + (f"; a skill's: {SKILL_NAME_RULE}" if kind == "skill" else ""))
        if not isinstance(files, dict) or not files:
            raise bad(f"{kind} '{name}': 'files' must be an object of relative paths")
        data, size = {}, 0
        for rel, b64 in files.items():
            parts = rel.split("/") if isinstance(rel, str) else [""]
            if not isinstance(b64, str) or "\0" in rel or any(p in ("", ".", "..") for p in parts):
                raise bad(f"{kind} '{name}': bad file {one_line(str(rel))[0]!r}")
            try:
                data[rel] = base64.b64decode(b64, validate=True)
            except ValueError:
                raise bad(f"{kind} '{name}': a file is not base64") from None
            size += len(data[rel])
        if size > REMOTE_ITEM_CAP:
            raise bad(f"{kind} '{name}' is over {REMOTE_ITEM_CAP} bytes")
        if hash_snapshot(data) != item["hash"]:
            raise bad(f"{kind} '{name}': its files don't match the hash blessed on your machine")
        label = f"{name} (from your machine)"
        if kind == "skill":
            if "SKILL.md" not in data:
                raise bad(f"skill '{name}' has no SKILL.md")
            out[name] = Skill(name, label, data)
        else:
            out[name] = CommandSet(name, label, data)
            if out[name].error:
                raise bad(f"command set '{name}': {out[name].error}")
            out[name].allow_auto(item.get("auto", []))
    return out


def remote_config(line, audit):
    """Parse the config line hoplon-tui sends first with --config-stdin: an endpoint, a profile (or null), the
    session token, and the skills and command sets to use. hoplon-tui checked the user's blessed files on their machine,
    so nothing here is looked up or blessed on this host; the audit log records what arrived, with hashes. The key
    stays on the user's machine: the endpoint is a loopback port that ssh forwards to a proxy there, which adds the
    key when the token matches. Returns the endpoint, the profile and {"skills": {...}, "command_sets": {...}}."""
    def bad(why):
        return HoplonError(f"bad config from the wrapper: {why}. Nothing was sent.", EXIT_PROFILE, "bad_config")

    try:
        msg = json.loads(line)
    except ValueError:
        raise bad("the first line is not JSON") from None
    if not isinstance(msg, dict) or msg.get("type") != "config":
        raise bad("the first line must be a config message")
    ep, prof, token = msg.get("endpoint"), msg.get("profile"), msg.get("token")
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise bad("'token' must be 32 to 128 hex characters")
    if not isinstance(ep, dict) or (prof is not None and not isinstance(prof, dict)):
        raise bad("'endpoint' and 'profile' must be objects")
    if "key_env" in ep or "key_file" in ep or ep.get("proxy") is not None:
        raise bad("the endpoint may not name a key or a proxy here: the key stays on your machine")
    try:
        endpoint = Endpoint(ep, "config endpoint")
        persona = Profile(prof, "config profile") if prof is not None else None
    except HoplonError as e:
        raise bad(str(e)) from None
    m = REMOTE_URL.fullmatch(endpoint.base_url)
    if not m or not 0 < int(m.group(1)) < 65536:
        raise bad("base_url must be http://127.0.0.1:PORT, optionally with a path")
    offered = {"skills": remote_items(msg.get("skills"), "skill", bad),
               "command_sets": remote_items(msg.get("command_sets"), "command_set", bad),
               "session": remote_session(msg.get("session"), bad)}
    endpoint.token = token
    digest = lambda o: hash_bytes(json.dumps(o, sort_keys=True).encode())
    audit.log("remote_config", endpoint=endpoint.name, model=endpoint.model, endpoint_sha256=digest(ep),
              profile=persona.name if persona else None, profile_sha256=digest(prof) if persona else None,
              skills={n: s.hash for n, s in offered["skills"].items()},
              command_sets={n: p.hash for n, p in offered["command_sets"].items()})
    return endpoint, persona, offered


def remote_session(sess, bad):
    """The session hoplon-tui keeps for this conversation on the user's machine, or None (not saved): {"name", "data"},
    data None for a new one. Checked for shape; the user's machine checked that it was this host's."""
    if sess is None:
        return None
    if not isinstance(sess, dict) or set(sess) - {"name", "data"} or not isinstance(sess.get("name"), str) \
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", sess["name"]):
        raise bad("'session' must be {name, data}, with a session name")
    data = sess.get("data")
    if data is None:
        return {"name": sess["name"], "data": None}
    ok = (isinstance(data, dict) and isinstance(data.get("coding", False), bool)
          and isinstance(data.get("cwd", ""), str)
          and isinstance(data.get("skills", []), list) and all(isinstance(x, str) for x in data.get("skills", []))
          and isinstance(data.get("created", ""), str)
          and all(isinstance(data.get(k, []), list) and all(isinstance(m, dict) for m in data.get(k, []))
                  for k in ("messages", "history")))
    if not ok:
        raise bad("'session.data' is not a saved session")
    messages = data.get("messages", [])
    return {"name": sess["name"], "data": {"coding": data.get("coding", False), "cwd": data.get("cwd") or os.getcwd(),
                                           "skills": data.get("skills", []), "messages": messages,
                                           "history": data.get("history", list(messages)),
                                           "created": data.get("created") or now()}}


# Endpoint fields that never travel to a remote host: where the key is, the proxy (the key proxy uses the endpoint's
# own proxy, here, and a remote one would dial it from there), headers (they may hold secrets and are added here by
# the key proxy) and the local CA bundle path (the remote leg is plain HTTP over the tunnel).
REMOTE_STRIP = ("key_env", "key_file", "proxy", "headers", "ca_bundle")


def export_items(cfg, kind, names):
    """The blessed skills or command sets among names, with their bytes (base64) and hash, for a remote hoplon. One that
    is missing, not blessed, refused or too big is left out, and hoplon says so on stderr."""
    found, out = (find_skills(cfg) if kind == "skill" else find_command_sets(cfg)), []
    for n in dict.fromkeys(names):
        item = found.get(n)
        st = None if item is None else (skill_status(cfg, item) if kind == "skill" else command_set_status(cfg, item))
        why = ("not found" if item is None else f"{st} (hoplon --bless {item.path})" if st != "blessed" else
               getattr(item, "error", None) or
               (f"over {REMOTE_ITEM_CAP} bytes" if sum(map(len, item.files.values())) > REMOTE_ITEM_CAP else None))
        if why:
            eprint(f"hoplon: {kind} '{n}' is not sent to the remote host: {why}")
            continue
        out.append({"name": n, "hash": item.hash,
                    "files": {rel: base64.b64encode(b).decode() for rel, b in sorted(item.files.items())}})
        if kind == "command_set":
            out[-1]["auto"] = cfg.blessed().get(item.path, {}).get("auto", [])
    return out


def export_remote(cfg, profile_name, endpoint_name, skills=(), command_sets=()):
    """What hoplon-tui's remote mode needs from this machine's configuration, resolved and checked exactly as a
    normal start would (default profile and endpoint, default skills and command sets, blessing, changed files), and
    nothing secret: the skills and command sets in use go with their bytes, which the remote hoplon checks against the
    hashes blessed here."""
    pname = profile_name or cfg.default_profile
    persona = select_named(cfg, "profile", pname) if pname else None
    endpoint = select_endpoint(cfg, endpoint_name or (persona.endpoint if persona else None) or cfg.default_endpoint)
    path = urllib.parse.urlsplit(endpoint.base_url).path
    if not re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*", path):
        raise HoplonError(f"endpoint '{endpoint.name}': the path of base_url ({path!r}) can't be used in remote mode "
                         "(letters, digits and . _ ~ - only)", EXIT_PROFILE)
    skills = export_items(cfg, "skill", (persona.skills if persona and persona.skills is not None
                                         else cfg.default_skills) + list(skills))
    sets = export_items(cfg, "command_set", (persona.command_sets if persona and persona.command_sets is not None
                                           else cfg.default_commands) + list(command_sets))
    cfg.audit.log("remote_config_export", endpoint=endpoint.name, profile=persona.name if persona else None,
                  skills={s["name"]: s["hash"] for s in skills}, command_sets={p["name"]: p["hash"] for p in sets})
    return {"endpoint": {k: v for k, v in endpoint.data.items() if k not in REMOTE_STRIP},
            "profile": persona.data if persona else None, "endpoint_name": endpoint.name, "base_path": path,
            "destination_id": endpoint.destination_id(),
            "skills": skills, "command_sets": sets}


def select_endpoint(cfg, name):
    if not name:
        raise HoplonError("no endpoint is configured. Nothing was sent.\n"
                         "  Add one to ~/.config/hoplon/endpoints/ and bless it, then use -e NAME, a profile that "
                         "names it, or default_endpoint in config.json.", EXIT_PROFILE)
    return select_named(cfg, "endpoint", name)


def list_named(cfg, kind):
    found = {}
    for d in kind_dirs(cfg, kind):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if n.endswith(".json") and n[:-5] not in found:
                found[n[:-5]] = os.path.abspath(os.path.join(d, n))
    for path, entry in sorted(cfg.blessed().items()):
        if entry.get("kind") == kind and entry.get("name") not in found and os.path.exists(path):
            found[entry.get("name")] = path
    rows = []
    for name, path in sorted(found.items()):
        try:
            obj, st = load_named(cfg, kind, path)
            if kind == "endpoint":
                desc = f"{obj.model} at {obj.base_url}"
            else:
                desc = (f"[{obj.endpoint}] " if obj.endpoint else "") + obj.description + \
                       (" (grants coding mode)" if obj.coding else "")
        except HoplonError as e:
            desc, st = str(e), "invalid"
        rows.append({"name": name, "path": path, "status": st, "description": desc})
    return rows


def bless(cfg, path, ask):
    """Show what is being blessed and record its hash if the user confirms."""
    path = os.path.abspath(os.path.expanduser(path))
    if is_hook_path(cfg, path):
        return bless_hook(cfg, ask)
    if os.path.isdir(path):
        files = snapshot_skill(path)
        cset = CommandSet(os.path.basename(path), path) if "commands.json" in files else None
        if cset and cset.error:
            raise HoplonError(f"{path}: {cset.error}")
        if not cset and "SKILL.md" not in files:
            raise HoplonError(f"{path}: a skill needs a SKILL.md, and a command set a commands.json")
        if not cset and not SKILL_NAME.fullmatch(os.path.basename(path)):
            raise HoplonError(f"{one_line(path)[0]}: a skill's name (its directory's) must be {SKILL_NAME_RULE}; "
                             "rename it, then bless it", EXIT_USAGE, "bad_name")
        h = hash_snapshot(files)
        print(f"{'Command set' if cset else 'Skill'} {one_line(os.path.basename(path))[0]} at {one_line(path)[0]}")
        for f in sorted(files):
            print(f"  {one_line(f)[0]} ({len(files[f])} bytes)")
        print(f"  hash: {h}")
        for f in sorted(files):
            try:
                text = files[f].decode("utf-8")
            except UnicodeDecodeError:
                print(f"  {one_line(f)[0]}: binary (not UTF-8); it cannot be shown or checked for hidden characters")
                continue
            found = hidden_found(text)
            if found:
                print(f"  [!] {one_line(f)[0]} has hidden or control characters: {found}")
        if any(one_line(x)[1] for x in [path, *files]):
            print("  [!] the name or a file name above had characters that were escaped; read carefully")
        if cset:
            print(cset.summary())
        entry = {"kind": "command_set" if cset else "skill", "name": os.path.basename(path), "hash": h}
        question = ("Have you read every command above, and may the model and you run them as shown?" if cset
                    else "Have you read every file above, and do you want to use this skill?")
    else:
        raw = read_regular(path)
        h = hash_bytes(raw)
        data = parse_json_bytes(raw, path)
        if isinstance(data, dict) and "base_url" in data:
            ep = Endpoint(data, path)
            text, flagged = ep.summary()
            print(f"Endpoint at {one_line(path)[0]}\n{text}\n  hash:     {h}")
            print("Everything you send to this endpoint (prompts, files, the key) goes to the URL above.")
            if flagged:
                print("  [!] some fields above had control or hidden characters that were escaped; "
                      "this file may be trying to mislead you")
            entry = {"kind": "endpoint", "name": ep.name, "hash": h}
            question = "Send your prompts, files and key to this endpoint?"
        else:
            prof = Profile(data, path)
            text, flagged = prof.summary()
            print(f"Profile at {one_line(path)[0]}\n{text}\n  hash:     {h}")
            if flagged:
                print("  [!] some fields above had control or hidden characters that were escaped; "
                      "this file may be trying to mislead you")
            print("The persona is sent to the model as instructions. The endpoint and skills it names "
                  "must be blessed on their own.")
            entry = {"kind": "profile", "name": prof.name, "hash": h}
            question = "Have you read the persona above, and do you want to use this profile" + \
                       (", including the coding mode it grants?" if prof.coding else "?")
    if not ask(question):
        print("Not blessed.")
        return False
    if os.path.isdir(path) and cset and cset.askers():
        print("Write commands, commands that use the network, and commands that run as another user ask you every "
              "time. You can let some of them run without asking; every run is still logged as trusted_run.")
        entry["auto"] = [c.name for c in cset.askers()
                         if ask(f"Let the model run {one_line(c.name)[0]} ({one_line(c.shown())[0]})"
                                + (f" AS {one_line(c.as_user)[0].upper()}" if c.as_user else "")
                                + (f", CONNECTING TO {c.reaches()}," if c.reaches() else "") + " without asking you?")]
    entry["blessed_at"] = now()
    cfg.set_blessed(path, entry)
    cfg.audit.log("bless", path=path, kind=entry["kind"], hash=h, **({"auto": entry["auto"]} if "auto" in entry else {}))
    print("Blessed.")
    return True


# --------------------------------------------------------------------------
# The notify hook: a program of yours, run when hoplon waits for you (see HOOKS.md)

def hook_path(cfg):
    return os.path.abspath(os.path.join(cfg.home, "hooks", "notify"))


def is_hook_path(cfg, path):
    """Is this the config home's hooks/notify? Judged by where it is, not by what it holds."""
    want = hook_path(cfg)
    return (os.path.basename(path) == "notify"
            and os.path.realpath(os.path.dirname(path)) == os.path.realpath(os.path.dirname(want)))


def hook_mode_problem(path):
    """Why this file may not be a hook (not a regular file, not yours, writable by others), or None."""
    try:
        st = os.lstat(path)
    except OSError as e:
        return e.strerror
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file (a symlink is not allowed)"
    if st.st_uid != os.getuid():
        return "is not owned by you"
    if st.st_mode & 0o022:
        return "is writable by group or others"
    return None


def hook_problem(cfg, path):
    """Why the notify hook must not run right now, or None. The bytes are read once and hashed."""
    why = hook_mode_problem(path)
    if why:
        return why
    try:
        if not os.lstat(path).st_mode & 0o100:
            return "is not executable (chmod u+x)"
        h = hash_file(path)
    except (HoplonError, OSError):
        return "cannot be read"
    entry = cfg.blessed().get(path)
    if not entry or entry.get("kind") != "hook":
        return "is not blessed"
    return None if h == entry.get("hash") else "changed since it was blessed"


def bless_hook(cfg, ask):
    """Show the notify hook, whole, and record its hash if the user confirms."""
    path = hook_path(cfg)
    why = hook_mode_problem(path)
    if why:
        raise HoplonError(f"{path}: a hook {why}; it cannot be blessed")
    raw = read_regular(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HoplonError(f"{path}: a hook must be a UTF-8 text file, so that it can be shown; it cannot be blessed")
    h = hash_bytes(raw)
    print(f"Hook at {one_line(path)[0]}\n  a hook: a program hoplon will run as you when it is waiting for you\n"
          f"  size:     {len(raw)} bytes\n  hash:     {h}\n{margin(text)}")
    found = hidden_found(text)
    if found:
        print(f"  [!] the hook has hidden or control characters: {found}")
    if not os.lstat(path).st_mode & 0o100:
        print("  note: it is not executable, so it will not run until you: chmod u+x " + shlex.quote(path))
    if not ask("Have you read the whole hook above, and do you want hoplon to run it as you?"):
        print("Not blessed.")
        return False
    cfg.set_blessed(path, {"kind": "hook", "name": "notify", "hash": h, "blessed_at": now()})
    cfg.audit.log("bless", path=path, kind="hook", hash=h)
    print("Blessed.")
    return True


def short_path(path):
    home = os.path.expanduser("~")
    shown = "~" + path[len(home):] if home != "/" and path.startswith(home + "/") else path
    return one_line(shown)[0]


class Notifier:
    """Runs the notify hook. It only observes: hoplon never waits for it and never reads its answer."""

    def __init__(self, engine):
        self.e, self.path, self.dead = engine, hook_path(engine.cfg), False

    @classmethod
    def start(cls, engine):
        """The notifier for this run, or None. A hook that is present but not usable is reported once."""
        path = hook_path(engine.cfg)
        if not os.path.lexists(path):
            return None
        why = hook_problem(engine.cfg, path)
        if why:
            engine.ui.notice(f"the notify hook {short_path(path)} {why}, so it will not run. "
                             f"Review it, then: hoplon --bless {shlex.quote(path)}")
            return None
        return cls(engine)

    def fire(self, event, **fields):
        if self.dead:
            return
        why = hook_problem(self.e.cfg, self.path)
        if why:
            self.dead = True
            self.e.ui.notice(f"the notify hook {why}; it is not run for the rest of this session")
            return
        e = self.e
        line = json.dumps({"event": event, **fields, "session": e.session.id, "cwd": os.getcwd(),
                           "endpoint": e.endpoint.name, "sandbox": e.sandbox, "version": VERSION},
                          ensure_ascii=True)
        env = {k: v for k, v in os.environ.items() if k != e.endpoint.key_env}
        try:
            proc = subprocess.Popen([self.path], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=env, cwd=e.cfg.home if os.path.isdir(e.cfg.home)
                                    else "/", start_new_session=True, close_fds=True)
        except OSError as err:
            self.dead = True
            e.ui.notice(f"the notify hook could not be started ({err.strerror}); it is ignored")
            return
        try:        # a short line into an empty pipe does not block; done here so a quick exit still delivers it
            proc.stdin.write(line.encode() + b"\n")
            proc.stdin.close()
        except OSError:
            pass
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()
        e.audit.log("hook", hook="notify", when=event, **fields)

    @staticmethod
    def _reap(proc):
        """Wait for the hook; past the time limit kill its whole process group. Nothing here may raise."""
        try:
            try:
                proc.wait(HOOK_TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        except Exception:
            pass


def new_endpoint(cfg, ask=input, out=print, client=None):
    """hoplon --new-endpoint: ask for an endpoint's name, address, key variable, model, context size and aliases,
    check it as an endpoint, and write it to the config's endpoints/ with private permissions. It never blesses:
    the user reads what it wrote and blesses it. client(endpoint) gives the server's model list (for tests).
    Returns the path written, or None."""
    def question(text, check, default=None):
        while True:
            try:
                ans = ask(f"{text}{f' [{default}]' if default else ''}: ").strip()
            except EOFError:
                raise HoplonError("stopped: nothing was written", EXIT_USAGE) from None
            ans = ans or (default or "")
            why = check(ans)
            if why is None:
                return ans
            out(f"  {why}")

    def yes(text):
        try:
            return ask(f"{text} [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:        # Ctrl-D stops at every question, this one too
            raise HoplonError("stopped: nothing was written", EXIT_USAGE) from None

    folder = cfg.endpoint_dirs[0]
    out("A new endpoint: where hoplon sends your prompts. Nothing is written until the end, and it is never blessed "
        "for you. Ctrl-D stops.")
    name = question("Name (used with -e NAME)", lambda v: "letters, digits, '.', '_' and '-'" if not NAME_RE.fullmatch(v)
                    else f"{folder}/{v}.json already exists" if os.path.lexists(os.path.join(folder, v + ".json"))
                    else None)

    def url_ok(v):
        try:
            u = urllib.parse.urlsplit(v)
            bad = u.scheme not in ("https", "http") or not u.hostname or u.username or u.password or u.port == 0
        except ValueError:      # an unmatched '[' or a port that isn't a number
            bad = True
        return "an http(s) address without credentials, usually ending in /v1" if bad else None
    data = {"name": name, "base_url": question("The server's address (the OpenAI-compatible base URL)", url_ok)}
    host = urllib.parse.urlsplit(data["base_url"]).hostname
    if data["base_url"].startswith("http:") and not _is_loopback(host):
        if not yes(f"  [!] plain HTTP to {host}: prompts, files and the key would travel unencrypted. Allow it?"):
            raise HoplonError("stopped: use an https address; nothing was written", EXIT_USAGE)
        data["allow_http"] = True

    def env_ok(v):
        if not v:
            return None
        if not ENV_NAME_RE.fullmatch(v) or len(v) > 64:
            return "the variable's name (letters, digits, underscore), never the key itself"
        return None
    key_env = question("The environment variable that holds the API key (its name, not the key; empty: no key)",
                       env_ok)
    sends = "a request"
    if key_env:
        data["key_env"] = key_env
        if os.environ.get(key_env):
            sends = f"a request with your key (${key_env})"
        else:
            out(f"  ${key_env} is not set in this shell, so the server's model list can't be read with it")
    listed = []
    # Nothing is sent before blessing unless you say so here: the address is the one you just typed.
    if yes(f"List the models {data['base_url']} has? That sends {sends} there now, before you bless the endpoint"):
        probe = Endpoint({**data, "model": "auto"}, os.path.join(folder, name + ".json"))
        try:
            listed = (client or Client)(probe).model_list()
        except HoplonError as e:
            out(f"  could not list the server's models: {one_line(str(e))[0]}")
    for i, (mid, n) in enumerate(listed[:30], 1):
        out(f"  {i:2}. {mid}" + (f"  ({n} tokens)" if n else ""))
    if len(listed) > 30:
        out(f"  ... and {len(listed) - 30} more, by name")

    def pick(v, empty_ok=False):
        if not v:
            return None if empty_ok else "a model"
        if v.isdigit() and listed:      # with no list, a number is a model name like any other
            return None if 1 <= int(v) <= min(len(listed), 30) else "a number from the list"
        return None if MODEL_ID.fullmatch(v) else "a model name, as the server lists it"

    def chosen(v):
        return listed[int(v) - 1][0] if v.isdigit() and listed else v
    data["model"] = chosen(question("Model (a number from the list, or its name)" if listed else "Model name", pick))
    found = dict(listed).get(data["model"])
    limit = question("The model's context size in tokens",
                     lambda v: None if v.isdigit() and 1000 <= int(v) <= 10_000_000 else "a whole number of tokens",
                     str(found or 32000))
    data["context_limit"] = int(limit)
    if yes("Give its models short names (aliases such as low, medium, high), for profiles and :model?"):
        aliases = {}
        for alias in ("low", "medium", "high"):
            v = question(f"  Model for '{alias}' (number or name; empty: none)", lambda v: pick(v, True))
            if v:
                aliases[alias] = chosen(v)
        if aliases:
            data["aliases"] = aliases
    path = os.path.join(folder, name + ".json")
    Endpoint(data, path)             # checked as hoplon will check it
    text = json.dumps(data, indent=2) + "\n"
    os.makedirs(folder, mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise HoplonError(f"{path} appeared meanwhile; nothing was written", EXIT_USAGE) from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    out(f"Wrote {path}:\n{text}")
    out(f"Read it, then bless it (only you can): hoplon --bless {path}")
    out(f"Then try it: hoplon -e {name} \"hello\"")
    return path
