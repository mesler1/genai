# hoplon_core/commands.py: command sets and their scoped commands (a handler: the scoped request).
#
# CommandSet, TrustedCommand and TrustedParam: a blessed manifest of programs and the parameters each accepts,
# checked value by value; and run_program, which runs one with the environment it is given, a time limit and an
# output cap.
#
# Uses: base, files

# --------------------------------------------------------------------------
# Command sets and scoped commands: programs you blessed once, with exactly the inputs they may take

COMMAND_SET_KEYS = {"name", "description", "search", "env", "pane", "commands"}
COMMAND_KEYS = {"describe", "usage", "argv", "params", "access", "network", "timeout", "max_output", "min_interval",
                "cache"}
NETWORK_MAX = 16             # destinations one command may declare
DESTINATION_MAX = 100        # characters of one
USAGE_MAX = 1200            # characters of a scoped command's usage section, all lines together
USAGE_LINES = 20
GUIDE_MAX = 6000            # characters of a command set's GUIDE.md, sent to the model while the set is on
PARAM_KEYS = {"pattern", "enum", "int", "path", "host", "describe"}
PATH_RULE_KEYS = {"under", "files", "new"}
HOST_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*")
SET_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
COMMAND_NAME = re.compile(r"[a-z][a-z0-9_]{0,30}")
# Programs that run a command as another user (root unless -u says otherwise). A scoped command that starts with one
# is shown as running as that user, asks every time (reads too) unless you let it run without asking when blessing,
# and must never ask for a password: it runs with no terminal, so sudoers (or doas.conf, or polkit for run0) has to
# allow exactly that command without one. Only the options below are accepted, so none can start a shell, edit a
# file, keep the environment or change directory.
ESCALATE = {"sudo": {"quiet": ("-n", "--non-interactive"), "user": ("-u", "--user"), "other": ("-H", "--set-home")},
            "doas": {"quiet": ("-n",), "user": ("-u",), "other": ()},
            "run0": {"quiet": ("--no-ask-password",), "user": ("-u", "--user"), "other": ()}}
ESCALATE_NO = ("pkexec", "su", "sudoedit", "runuser")      # can't run without asking for a password, or a shell


def escalation(argv, where):
    """Whom a scoped command's argv runs as, through sudo, doas or run0, and the program it runs: ("root", a user
    name, or "a user given per call" (a placeholder), the program's absolute path); (None, None) for a plain
    command. ValueError for what can't run safely here."""
    prog = os.path.basename(argv[0])
    if prog in ESCALATE_NO:
        raise ValueError(f"{where}argv[0]: {prog} can't run here; use sudo -n, with a sudoers rule for exactly this "
                         "command")
    if prog not in ESCALATE:
        return None, None
    opts, user, quiet, i = ESCALATE[prog], "root", False, 1
    while i < len(argv) and argv[i].startswith("-"):
        a = argv[i]
        i += 1
        if a == "--":
            break
        name, eq, val = a.partition("=")
        if a in opts["quiet"]:
            quiet = True
        elif name in opts["user"]:
            if not eq:
                val = argv[i] if i < len(argv) else ""
                i += 1
            if not val:
                raise ValueError(f"{where}argv: {prog} {name} needs a user")
            user = "a user given per call" if "{" in val else val
        elif a not in opts["other"]:
            allowed = ", ".join(opts["quiet"] + opts["user"] + opts["other"])
            raise ValueError(f"{where}argv: {prog} {a} is not allowed in a scoped command (only {allowed}, each "
                             "written on its own)")
    if not quiet:
        raise ValueError(f"{where}argv: {prog} needs {opts['quiet'][0]}: scoped commands run with no terminal, so it "
                         "must never ask for a password (allow exactly this command without one)")
    if i >= len(argv) or not argv[i].startswith("/") or "{" in argv[i]:
        raise ValueError(f"{where}argv: the program {prog} runs must follow its options as an absolute path, such as "
                         "/usr/bin/systemctl, so it matches your sudoers rule exactly")
    return user, argv[i]
TEMPLATE = re.compile(r"\{\{|\}\}|\{([A-Za-z0-9_]+)\}|[{}]")
DEFAULT_SEARCH = ("/usr/local/bin", "/usr/bin", "/bin")
PARAM_CAP = 256                # characters of one parameter value
TRUSTED_PER_MINUTE = 30        # real runs across all command sets; more are refused until the minute is up
TRUSTED_CACHE = 256            # results kept for repeated calls


def show_argv(argv):
    """An argument list as one readable line, quoted where needed. For people: nothing is run through a shell."""
    return " ".join(a if re.fullmatch(r"[A-Za-z0-9_@%+=:,./{}-]+", a) else shlex.quote(a) for a in argv)


def _text(data, key, where, default=""):
    v = data.get(key, default)
    if not isinstance(v, str) or len(v) > 1000 or "\0" in v:
        raise ValueError(f"{where}{key} must be text of at most 1000 characters")
    return v


def _usage(v, where):
    """A command's usage section: a list of lines (or one string) of plain text, short enough to send with the
    command every time. No control characters: it goes to the model, and to the blessing screen."""
    lines = [v] if isinstance(v, str) else v
    if not isinstance(lines, list) or not all(isinstance(ln, str) for ln in lines):
        raise ValueError(f"{where} must be a list of lines of text")
    lines = [ln for part in lines for ln in part.split("\n")]
    if len(lines) > USAGE_LINES or sum(len(ln) for ln in lines) > USAGE_MAX:
        raise ValueError(f"{where} must be at most {USAGE_LINES} lines and {USAGE_MAX} characters")
    if any(exact(ln)[1] or one_line(ln)[1] for ln in lines):
        raise ValueError(f"{where} must not hold control or hidden characters")
    return [ln.rstrip() for ln in lines]


def _number(data, key, where, default, lo, hi):
    v = data.get(key, default)
    if type(v) is not int or not lo <= v <= hi:
        raise ValueError(f"{where}{key} must be a whole number from {lo} to {hi}")
    return v


def _object(data, allowed, where):
    if not isinstance(data, dict):
        raise ValueError(f"{where} must be an object")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"{where}: unknown field(s): {', '.join(unknown)}")
    return data


def _strings(value, where):
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and "\0" not in x for x in value):
        raise ValueError(f"{where} must be a non-empty list of strings")
    return value


def split_template(text, where):
    """An argument split into (is_placeholder, text) parts. '{{' and '}}' are literal braces; any other brace
    that is not part of a {name} placeholder is an error."""
    parts, pos = [], 0
    for m in TEMPLATE.finditer(text):
        parts.append((False, text[pos:m.start()]))
        if m.group(1):
            parts.append((True, m.group(1)))
        elif m.group(0) in ("{{", "}}"):
            parts.append((False, m.group(0)[0]))
        else:
            raise ValueError(f"{where}: a single '{m.group(0)}' is not allowed (write it doubled to mean a brace)")
        pos = m.end()
    parts.append((False, text[pos:]))
    return parts


class TrustedParam:
    """What one placeholder accepts: a path inside the directories or among the files the set names, a host from
    its list, a whole number in a range, a list of values, or, as the last resort, a regular expression the whole
    value must match. A value that doesn't fit is refused, never cleaned. No value may start with '-': one that
    did could be read by the program as an option (another cluster, an output file), not as data."""

    def __init__(self, data, where):
        _object(data, PARAM_KEYS, where)
        kinds = [k for k in ("path", "host", "int", "enum", "pattern") if k in data]
        if len(kinds) != 1:
            raise ValueError(f"{where}: needs exactly one of path, host, int, enum, pattern")
        self.kind, self.rule = kinds[0], data[kinds[0]]
        self.describe = _text(data, "describe", where + ".")
        if self.kind == "pattern":
            try:
                self.rx = re.compile(self.rule)
            except (re.error, TypeError):
                raise ValueError(f"{where}.pattern is not a valid regular expression") from None
            if self.rx.fullmatch(""):
                raise ValueError(f"{where}.pattern matches the empty string")
            if any(self.rx.fullmatch(v) for v in ("-", "-a", "-1", "--a", "--a=b")):
                raise ValueError(f"{where}.pattern accepts a value starting with '-', which a program reads as an option")
        elif self.kind == "enum":
            _strings(self.rule, where + ".enum")
            if any(v.startswith("-") for v in self.rule):
                raise ValueError(f"{where}.enum has a value starting with '-', which a program reads as an option")
        elif self.kind == "host":
            _strings(self.rule, where + ".host")
            if not all(len(h) <= 253 and HOST_NAME.fullmatch(h) for h in self.rule):
                raise ValueError(f"{where}.host must be a list of host names (letters, digits, '-' and '.')")
        elif self.kind == "path":
            _object(self.rule, PATH_RULE_KEYS, where + ".path")
            under, files = self.rule.get("under", []), self.rule.get("files", [])
            for key, v in (("under", under), ("files", files)):
                if v:
                    _strings(v, f"{where}.path.{key}")
                if v and not all((x.startswith("/") or x.startswith("~/")) and "\0" not in x and "\n" not in x
                                 for x in v):
                    raise ValueError(f"{where}.path.{key} must be absolute paths (or start with ~/)")
            if not (under or files):
                raise ValueError(f"{where}.path needs 'under' (directories) or 'files' (exact files), or both")
            if not isinstance(self.rule.get("new", False), bool) or (self.rule.get("new") and not under):
                raise ValueError(f"{where}.path.new must be true or false, and true needs 'under'")
        elif not (isinstance(self.rule, list) and len(self.rule) == 2 and all(type(n) is int for n in self.rule)
                  and 0 <= self.rule[0] <= self.rule[1]):
            raise ValueError(f"{where}.int must be [min, max], from 0 up")

    def check(self, value):
        """The value as a string, or ValueError saying why it is refused."""
        if self.kind == "int" and type(value) is int:
            value = str(value)
        if not isinstance(value, str):
            raise ValueError("must be a string")
        if len(value) > PARAM_CAP or "\0" in value or "\n" in value or "\r" in value:
            raise ValueError(f"must be at most {PARAM_CAP} characters, on one line")
        if value.startswith("-"):          # whatever the pattern says: it would be an option, not data
            raise ValueError("must not start with '-'")
        if self.kind == "pattern" and not self.rx.fullmatch(value):
            raise ValueError(f"does not match {self.rule}")
        if self.kind == "enum" and value not in self.rule:
            raise ValueError(f"must be one of: {', '.join(self.rule)}")
        if self.kind == "int" and not (re.fullmatch(r"[0-9]{1,18}", value)
                                       and self.rule[0] <= int(value) <= self.rule[1]):
            raise ValueError(f"must be a whole number from {self.rule[0]} to {self.rule[1]}")
        if self.kind == "host" and value not in self.rule:
            raise ValueError(f"must be one of the hosts: {', '.join(self.rule)}")
        if self.kind == "path":
            return self.check_path(value)
        return value

    def check_path(self, value):
        """The real, absolute location: '..' and symlinks resolved first, then checked against the directories and
        files the set allows, with the file tools' rules (hidden and secret-looking names refused unless listed
        in files). The program gets this resolved path. It opens the file after this check, so a symlink swapped
        in between can still get past it: only the sandbox closes that."""
        real = os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(value)))
        if real in {os.path.realpath(os.path.expanduser(f)) for f in self.rule.get("files", [])}:
            if not os.path.exists(real):
                raise ValueError("does not exist")
            return real
        for d in self.rule.get("under", []):
            top = os.path.realpath(os.path.expanduser(d))
            if real != top and not real.startswith(top.rstrip("/") + "/"):
                continue
            if is_hidden(os.path.relpath(real, top).split("/")) and real != top:
                raise ValueError("is hidden or looks like it holds secrets; the set must list it in 'files'")
            if not os.path.exists(real):
                if not self.rule.get("new"):
                    raise ValueError("does not exist")
                if not os.path.isdir(os.path.dirname(real)):
                    raise ValueError("may be new, but its directory must exist")
            return real
        where = ", ".join(self.rule.get("under", []) + self.rule.get("files", []))
        raise ValueError(f"must be inside {where}, after resolving '..' and symlinks")

    def schema(self):
        s = {"type": "string"}
        if self.kind == "pattern":
            s["pattern"] = self.rule
        elif self.kind in ("enum", "host"):
            s["enum"] = self.rule
        elif self.kind == "int":
            s["pattern"] = "^[0-9]{1,18}$"
        text = self.describe
        if self.kind == "int":
            text = f"{text} (a whole number from {self.rule[0]} to {self.rule[1]})".strip()
        elif self.kind == "path":
            allowed = ([f"under {', '.join(self.rule['under'])}"] if self.rule.get("under") else []) + \
                      ([f"one of {', '.join(self.rule['files'])}"] if self.rule.get("files") else [])
            text = (f"{text} (a path {' or '.join(allowed)}; "
                    + ("it may be new" if self.rule.get("new") else "it must exist") + ")").strip()
        if text:
            s["description"] = text
        return s

    def rules(self):
        return {self.kind: self.rule, "describe": self.describe}


def trusted_binary(argv0, search):
    """Where a scoped command's program is: argv[0] if it is a path, else the first search directory that has it.
    The user's PATH is never used. The file and its directory must belong to you or root and not be writable
    by group or others, or whoever can write there could swap the program. So must every directory above it, up
    to /, except that one with the sticky bit (/tmp) may be writable: nobody else can rename what is yours or
    root's in it. Returns (path, identity)."""
    paths = [argv0] if os.path.isabs(argv0) else [os.path.join(d, argv0) for d in search]
    path = next((p for p in paths if os.path.exists(p)), None)
    if path is None:
        raise HoplonError(f"{argv0}: not found in {', '.join(search)}", EXIT_USAGE, "not_found")
    real = os.path.realpath(path)
    st = os.stat(real)
    if not stat.S_ISREG(st.st_mode) or not os.access(real, os.X_OK):
        raise HoplonError(f"{real}: not an executable file")
    for p in dict.fromkeys([real, os.path.dirname(real), os.path.dirname(path)]):
        s = os.stat(p)
        if s.st_mode & 0o022 or s.st_uid not in (0, os.geteuid()):
            raise HoplonError(f"{p}: writable by group or others, or owned by another user, so the program there "
                             "could be replaced; refused")
    d = os.path.dirname(real)
    while d != "/":
        d = os.path.dirname(d)
        s = os.stat(d)
        if s.st_uid not in (0, os.geteuid()) or (s.st_mode & 0o022 and not s.st_mode & stat.S_ISVTX):
            raise HoplonError(f"{d}, above {real}: writable by group or others, or owned by another user, so the "
                             "directories below it could be swapped and the program with them; refused")
    return real, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


class TrustedCommand:
    """One command of a command set: a fixed argument list with named placeholders, and what each accepts."""

    def __init__(self, cset, name, data, search, env):
        where = f"commands.{name}"
        _object(data, COMMAND_KEYS, where)
        self.set, self.name, self.search, self.env_names = cset, name, search, env
        w = where + "."
        self.describe = _text(data, "describe", w)
        self.usage = _usage(data.get("usage", []), w + "usage")
        self.argv = _strings(data.get("argv"), w + "argv")
        self.access = data.get("access")
        if self.access not in ("read", "write"):
            raise ValueError(f'{w}access must be "read" (it only looks) or "write" (it changes something)')
        self.read_only = self.access == "read"
        # Where it reaches over the network: "none", "any", or a list of destinations. Like "read", it is the set's
        # own claim: hoplon shows it at blessing and asks before a command that uses the network, but can't check it.
        self.network = data.get("network")
        if not (self.network in ("none", "any") or (
                isinstance(self.network, list) and 1 <= len(self.network) <= NETWORK_MAX
                and all(isinstance(d, str) and d.strip() and len(d) <= DESTINATION_MAX and not hidden_found(d)
                        for d in self.network))):
            raise ValueError(f'{w}network must be "none" (it never uses the network), a list of where it connects '
                             f'(hosts, or a short description such as "the Slurm controller"), or "any"')
        self.timeout = _number(data, "timeout", w, 15, 1, 120)
        self.max_output = _number(data, "max_output", w, 65536, 1, 1048576)
        self.min_interval = _number(data, "min_interval", w, 2, 0, 3600)
        self.cache = _number(data, "cache", w, 0, 0, 3600)
        params = data.get("params", {})
        if not isinstance(params, dict) or not all(COMMAND_NAME.fullmatch(k) for k in params):
            raise ValueError(f"{w}params must be an object named by parameter names (a-z, 0-9, _)")
        self.params = {k: TrustedParam(v, f"{w}params.{k}") for k, v in params.items()}
        for k, p in self.params.items():
            if p.kind != "host":
                continue
            if self.network == "none":
                raise ValueError(f'{w}network: params.{k} is a host, so it can\'t be "none"')
            if isinstance(self.network, list) and set(p.rule) - set(self.network):
                raise ValueError(f"{w}network must list every host params.{k} allows: "
                                 f"{', '.join(sorted(set(p.rule) - set(self.network)))}")
        if self.read_only and any(p.kind == "path" and p.rule.get("new") for p in self.params.values()):
            raise ValueError(f'{w}params: a path that may be new needs "access": "write"')
        first = self.argv[0]
        if not (re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", first) or (first.startswith("/") and "{" not in first)):
            raise ValueError(f"{w}argv[0] must be a program name without '/' or placeholders, or an absolute path")
        self.parts = [split_template(a, f"{w}argv[{i}]") for i, a in enumerate(self.argv)]
        # None, or whom it runs as (sudo, doas, run0), and then the program it runs, checked like argv[0]
        self.as_user, self.target = escalation(self.argv, w)
        # It runs without asking if it only reads, as you, with no network; anything else only if you chose so when
        # blessing (allow_auto).
        self.auto = self.plain()
        used = {text for parts in self.parts[1:] for is_param, text in parts if is_param}
        if used - set(self.params):
            raise ValueError(f"{w}argv uses undeclared placeholder(s): {', '.join(sorted(used - set(self.params)))}")
        if set(self.params) - used:
            raise ValueError(f"{w}params not used in argv: {', '.join(sorted(set(self.params) - used))}")
        self.tool = f"{cset.replace('-', '_')}__{name}"        # at most 31 + 2 + 31 characters
        self.binary = self.identity = self.target_identity = None

    def plain(self):
        """Does it run without asking unless you choose otherwise: a read, as you, that uses no network?"""
        return self.read_only and not self.as_user and self.network == "none"

    def reaches(self):
        """Where it connects, as shown to people: "", "ANY host", or the destinations."""
        return ("" if self.network == "none" else "ANY host" if self.network == "any" else
                ", ".join(one_line(d)[0] for d in self.network))

    def resolve(self):
        self.binary, self.identity = trusted_binary(self.argv[0], self.search)
        if self.target:                 # what sudo runs: your sudoers rule allows it, and hoplon checks it too
            real, self.target_identity = trusted_binary(self.target, [])
            if real != self.target:
                raise HoplonError(f"{self.target} is a link to {real}: name the program itself, so the sudoers "
                                 "rule and hoplon mean the same file")

    def changed(self):
        """Why the program may not run now, or None. It must be the very file found at load time."""
        try:
            st = os.stat(self.binary)
        except OSError:
            return "the program is gone since hoplon started; restart hoplon"
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != self.identity:
            return "the program changed since hoplon started; restart hoplon"
        if self.target:
            try:
                st = os.stat(self.target)
            except OSError:
                return f"{self.target} is gone since hoplon started; restart hoplon"
            if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns) != self.target_identity:
                return f"{self.target} changed since hoplon started; restart hoplon"
        return None

    def check(self, args):
        """The arguments as {name: string}, or ValueError. Every parameter is required; none is added or cleaned."""
        if not isinstance(args, dict):
            raise ValueError("params must be an object")
        for k in sorted(set(args) - set(self.params)):
            raise ValueError(f"unknown parameter '{one_line(str(k))[0]}'")
        values = {}
        for k, spec in self.params.items():
            if k not in args:
                raise ValueError(f"missing parameter '{k}'")
            try:
                values[k] = spec.check(args[k])
            except ValueError as e:
                raise ValueError(f"parameter '{k}' {e}") from None
        return values

    def build(self, values):
        """The argument list to run: each placeholder becomes its value, and one argument stays one argument."""
        return [self.binary] + ["".join(values[t] if is_param else t for is_param, t in parts)
                                for parts in self.parts[1:]]

    def shown(self, values=None):
        """The command as shown to people: the program's name, then the values (or the {placeholders})."""
        if values is None:
            return show_argv(self.argv)
        return show_argv([self.argv[0]] + self.build(values)[1:])

    def env(self):
        user = whoami()[0]
        env = {"PATH": ":".join(self.search), "LANG": "C.UTF-8",
               "HOME": os.environ.get("HOME") or os.path.expanduser("~"), "USER": user, "LOGNAME": user}
        env.update({n: os.environ[n] for n in self.env_names if n in os.environ})
        return env

    def schema(self, host):
        how = ("read-only" if self.read_only else "changes something") + \
            (f"; runs as {self.as_user}" if self.as_user else "") + \
            (f"; connects to {self.reaches()}" if self.reaches() else "") + \
            ("" if self.auto and self.plain() else
             "; runs without asking, as the user chose" if self.auto else "; the user is asked first")
        props = {k: p.schema() for k, p in self.params.items()}
        text = f"{self.describe} (runs `{self.shown()}` on {host}; {how})".strip()
        if self.usage:
            text += "\n\nUsage:\n" + "\n".join(self.usage)
        return _tool(self.tool, text, props, list(props))

    def info(self):
        return {"name": self.name, "describe": self.describe, "usage": self.usage,
                "params": {k: p.rules() for k, p in self.params.items()},
                "access": self.access, "auto": self.auto, "argv": self.argv, "as": self.as_user,
                "network": self.network}


def read_guide(raw):
    """A command set's GUIDE.md: when to use which command, for the model. Plain text, short, and covered by the
    blessing like every file of the set; "" when there is none."""
    if raw is None:
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("must be UTF-8 text") from None
    if len(text) > GUIDE_MAX:
        raise ValueError(f"must be at most {GUIDE_MAX} characters; put broader know-how in a skill")
    if hidden_found(text):
        raise ValueError("must not hold control or hidden characters")
    return text.strip()


class CommandSet:
    """A command set, read once like a skill: its hash and manifest come from one snapshot of the directory, so editing
    the files afterwards changes nothing until the next load. error is set when the manifest is refused."""

    def __init__(self, name, path, files=None):
        """files: the command set's bytes as sent by hoplon-tui from your own machine (remote mode); path is then only a
        label, and nothing is read here."""
        self.name, self.path = name, path
        self.error = self.hash = self.pane = None
        self.description, self.commands, self.files, self.guide = "", {}, {}, ""
        try:
            self.files = snapshot_skill(path) if files is None else files
            self.hash = hash_snapshot(self.files)
        except (HoplonError, OSError) as e:
            self.error = str(e)
            return
        try:
            self.parse(self.files)
        except ValueError as e:
            self.error, self.commands = f"commands.json: {e}", {}
            return
        try:
            self.guide = read_guide(self.files.get("GUIDE.md"))
        except ValueError as e:
            self.error, self.commands = f"GUIDE.md: {e}", {}

    def parse(self, files):
        data = _object(json.loads(files.get("commands.json", b"").decode("utf-8")), COMMAND_SET_KEYS, "commands.json")
        if data.get("name") != self.name or not SET_NAME.fullmatch(data.get("name", "")):
            raise ValueError("name must be lowercase letters, digits and '-', and the same as the directory's name")
        self.description = _text(data, "description", "")
        search = data.get("search", list(DEFAULT_SEARCH))
        _strings(search, "search")
        if not all(os.path.isabs(d) for d in search):
            raise ValueError("search must list absolute directories")
        env = data.get("env", [])
        if env:
            bad = [n for n in _strings(env, "env")
                   if not ENV_NAME_RE.fullmatch(n) or n == "PATH" or n.startswith("LD_")]
            if bad:
                raise ValueError(f"env: not allowed: {', '.join(bad)} (PATH and LD_* are never passed on)")
        pane = data.get("pane")
        if pane is not None and not (isinstance(pane, str) and NAME_RE.fullmatch(pane) and pane in files):
            raise ValueError("pane must be the name of a file in the command set directory")
        self.pane = pane
        cmds = data.get("commands")
        if not isinstance(cmds, dict) or not cmds or not all(COMMAND_NAME.fullmatch(k) for k in cmds):
            raise ValueError("commands must be an object of commands named a-z, 0-9, _")
        self.commands = {k: TrustedCommand(self.name, k, v, search, env) for k, v in cmds.items()}

    def tools(self):
        return {c.tool: c for c in self.commands.values()}

    def allow_auto(self, names):
        """The commands the user chose, when blessing, to run without asking (writes, any that use the network, and any
        run as another user); reads that run as you with no network always do."""
        names = set(names) if isinstance(names, list) else set()
        for c in self.commands.values():
            c.auto = c.plain() or c.name in names

    def writes(self):
        return [c for c in self.commands.values() if not c.read_only and not c.as_user and c.network == "none"]

    def networked(self):
        return [c for c in self.commands.values() if c.network != "none" and not c.as_user]

    def escalated(self):
        return [c for c in self.commands.values() if c.as_user]

    def askers(self):
        """The commands that ask every time unless the user chooses otherwise when blessing."""
        return [c for c in self.commands.values() if not c.plain()]

    def resolve(self):
        """Find every command's program now, refusing the command set if one can't be trusted. A program that isn't
        installed here (a cluster without accounting has no sacct) only leaves out its commands; the names of
        those are returned."""
        missing = []
        for c in list(self.commands.values()):
            try:
                c.resolve()
            except HoplonError as e:
                if e.kind != "not_found":
                    raise HoplonError(f"command set '{self.name}', command {c.name}: {e}", EXIT_USAGE, "bad_commands") from None
                missing.append(c.name)
                del self.commands[c.name]
            except OSError as e:
                raise HoplonError(f"command set '{self.name}', command {c.name}: {e}", EXIT_USAGE, "bad_commands") from None
        if not self.commands:
            raise HoplonError(f"command set '{self.name}': none of its programs is installed here", EXIT_USAGE, "bad_commands")
        return missing

    def summary(self):
        """The review screen for blessing: every command with what it may run, and the programs it would use."""
        c0 = next(iter(self.commands.values()))
        lines = [f"  set:      {one_line(self.name)[0]}", f"  about:    {one_line(self.description)[0]}"]
        if self.pane:
            lines.append(f"  pane:     {one_line(self.pane)[0]} (for hoplon-tui; hoplon does not run it)")
        lines.append(f"  search:   {', '.join(one_line(d)[0] for d in c0.search)} (your PATH is never used)")
        lines.append(f"  env:      {', '.join(c0.env_names) or 'none'} passed on, besides PATH, LANG, HOME, USER "
                     "and LOGNAME")
        groups = (("read: the set says these only look, and they run without asking (hoplon can't check that: "
                   "make sure each one only looks)",
                   [c for c in self.commands.values() if c.plain()]),
                  ("write: these change something, and ask every time unless you choose otherwise below",
                   self.writes()),
                  ("[!] NETWORK: these connect to other machines, and ask every time, reads too, unless you choose "
                   "otherwise below", self.networked()),
                  ("[!] AS ANOTHER USER (sudo, doas, run0): these ask every time, reads too, unless you choose "
                   "otherwise below", self.escalated()))
        for title, cmds in groups:
            if not cmds:
                continue
            lines.append(f"  {title}")
            lines += self.command_lines(cmds)
        if self.guide:
            lines.append("  guide (GUIDE.md, sent to the model whenever the set is on):")
            lines += [f"    | {one_line(ln)[0]}" for ln in self.guide.splitlines()]
        lines.append("  [!] these commands run on this host as you, with no shell, and the read ones that use no "
                     "network without asking: their output goes to the model and the endpoint like any file the model reads")
        lines.append("  [!] 'read' and 'write' are the set's own labels: hoplon checks which program runs and with "
                     "what arguments, not what it does. A 'read' command that changes something runs without "
                     "asking, live, whatever mode you are in")
        if self.networked() or any(c.network != "none" for c in self.escalated()):
            lines.append("  [!] the network a command declares is the set's own claim too: a command runs on this host "
                         "with your network, so it can reach anything you can. What it sends there leaves "
                         "this machine without going through the endpoint you blessed")
        if self.escalated():
            lines.append("  [!] the ones marked AS ANOTHER USER run with that user's rights (root can read and change "
                         "anything); allow exactly these commands, with -n, in sudoers or doas.conf, and nothing more")
        return "\n".join(lines)

    def command_lines(self, cmds):
        lines = []
        for c in cmds:
            flag = (f"[runs as {c.as_user}, {c.access}, asks every time]" if c.as_user else
                    f"[{c.access}, asks every time]" if c.network != "none" else
                    "runs without asking" if c.read_only else "[asks every time]")
            lines.append(f"  command {one_line(c.name)[0]}: {one_line(c.describe)[0]}  ({flag})")
            lines.append(f"    runs:    {one_line(c.shown())[0]}")
            lines.append(f"    network: {'[!] ' + c.reaches() if c.reaches() else 'none (the set says)'}")
            for k, p in c.params.items():
                lines.append(f"    {k}: {p.kind} {one_line(json.dumps(p.rule))[0]}  {one_line(p.describe)[0]}".rstrip())
            if c.usage:
                lines.append("    usage (sent to the model with the command):")
                lines += [f"      | {ln}" for ln in c.usage]
            lines.append(f"    limits:  {c.timeout} s, {c.max_output} bytes of output, at most one run per "
                         f"{c.min_interval} s, results kept {c.cache} s")
            try:
                lines.append(f"    program: {one_line(trusted_binary(c.argv[0], c.search)[0])[0]}")
            except (HoplonError, OSError) as e:
                lines.append(f"    program: [!] {one_line(str(e))[0]} (the command set can't be used until this is fixed)")
        return lines


def find_command_sets(cfg):
    seen = {}
    for d in cfg.command_dirs:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            p = os.path.join(d, n)
            if n not in seen and os.path.isfile(os.path.join(p, "commands.json")):
                seen[n] = CommandSet(n, os.path.abspath(p))
    return seen


def command_set_status(cfg, cset):
    return "invalid" if cset.hash is None else bless_status(cfg, "command_set", cset.path, cset.hash)


def command_set_rows(cfg, active=(), found=None):
    """Every command set found, for listings and the command sets message. found: the command sets your own machine sent (remote
    mode), which are the only ones there, and blessed by being sent."""
    rows = []
    for n, pl in sorted((find_command_sets(cfg) if found is None else found).items()):
        st = command_set_status(cfg, pl) if found is None else ("invalid" if pl.error else "blessed")
        rows.append({"name": n, "path": pl.path, "dir": pl.path, "description": pl.description or pl.error or "",
                     "status": st, "blessed": st == "blessed", "enabled": n in active, "active": n in active,
                     "error": pl.error, "pane": pl.pane, "guide": bool(pl.guide), "commands": [c.info() for c in pl.commands.values()]})
    return rows


def run_program(argv, env, cwd, timeout, cap):
    """Run argv with no shell, no stdin and its own process group. Returns (exit code, stdout, stderr, note); the
    exit code is None when the program was killed. Output past cap bytes (both streams together) is dropped and
    ends the run."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                            env=env, start_new_session=True)
    bufs, size, over = [bytearray(), bytearray()], [0], threading.Event()

    def drain(stream, buf):
        for chunk in iter(lambda: stream.read1(65536), b""):
            room = max(cap - size[0], 0)
            buf.extend(chunk[:room])
            size[0] += min(len(chunk), room)
            if len(chunk) > room:
                over.set()
                return

    readers = [threading.Thread(target=drain, args=(s, b), daemon=True)
               for s, b in zip((proc.stdout, proc.stderr), bufs)]
    for r in readers:
        r.start()
    deadline, killed = time.monotonic() + timeout, False
    try:
        while proc.poll() is None:
            if over.is_set() or time.monotonic() > deadline:
                killed = True
                break
            over.wait(0.02)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)       # the program, and anything it left running
        except OSError:
            pass
        proc.wait()
        for r in readers:
            r.join(5)
        proc.stdout.close()
        proc.stderr.close()
    note = f"output cut at {cap} bytes" if over.is_set() else ""
    if killed and not over.is_set():
        note = f"killed after {timeout} s"
    return (None if killed else proc.returncode), bytes(bufs[0]), bytes(bufs[1]), note
