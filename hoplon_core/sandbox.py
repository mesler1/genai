# hoplon_core/sandbox.py: the model's sandbox and work sessions (a handler: the run request).
#
# Sandbox runs a command offline in podman (or the host-OS sandbox), on Copy: a throwaway copy of the project.
# Work is a work session's private repository and its branch in yours. Fixtures are the files a sandbox may read.
# Also run_in_shell (the REPL's :run, once the user has confirmed it) and coding_refusal.
#
# Uses: base

# --------------------------------------------------------------------------
# Sandbox runner: commands the model runs itself, in a container, on a throwaway copy of the project

SANDBOX_KEYS = {"engine", "image", "network", "timeout", "memory", "pids"}
WORK_KEYS = {"test", "steps_per_item", "time_limit", "planner"}
WORK_STEPS = 20                # model steps one plan item may take in a work session, by default
WORK_STEPS_MAX = 500
WORK_TIME = 7200               # seconds a work session may run unattended, by default
WORK_TIME_MAX = 86400
WORK_CLEAR_FRACTION = 0.4      # a work session drops old file contents and output once its context is this full
WORK_CLEAR_AGAIN = 0.1         # ... and again only after it has grown by this much more (keeps the prompt cacheable)
WORK_KEEP_OUTPUTS = 4          # tool results kept in full when it does
WORK_NOTES_MAX = 2000          # characters of handoff notes kept per finished item
COMMIT_TITLE = 64              # characters of a work commit's title, after "N. "
SANDBOX_TIMEOUT = 300          # seconds a sandboxed command may run before it is killed
SANDBOX_TMP = "1g"             # size of the sandbox's /tmp, which is also its HOME
SANDBOX_MEMORY = "4g"
SANDBOX_PIDS = 1024
SANDBOX_MASKS = 500            # files and directories one run may hide; more refuses the run
SANDBOX_SCAN = 50000           # entries checked for secrets; a larger project refuses the run
# A virtual environment (a directory with a pyvenv.cfg) is scanned too, except for the parts that hold installed
# packages: a pyvenv.cfg written beside a secret can't hide it from the scan, only what is in these.
VENV_PARTS = {"lib", "lib64", "Lib", "include", "Include", "bin", "Scripts", "share"}
SCAN_SKIP = {"node_modules", "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
# With no image, a run uses this host's OS: an empty root holding only mount points, with these mounted read-only
# from the host, and the top-level links (/bin -> usr/bin and the like) as the host has them.
HOST_RO = ("/usr", "/etc")
HOST_LINKS = ("/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
HOST_OS = "this host's OS (/usr and /etc, read-only)"


def host_root(parent):
    """Make an empty root for a run on this host's OS in parent; returns (root, the podman flags that mount it)."""
    root = os.path.join(parent, "root")
    for d in ("proc", "sys", "dev", "run", "tmp", "var/tmp", "work", "fixtures") + tuple(p.lstrip("/") for p in HOST_RO):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    flags = [f"--volume={d}:{d}:ro" for d in HOST_RO]
    for d in HOST_LINKS:
        if os.path.islink(d):
            os.symlink(os.readlink(d), root + d)
        elif os.path.isdir(d):
            os.makedirs(root + d, exist_ok=True)
            flags.append(f"--volume={d}:{d}:ro")
    return root, flags


def box_name(conf):
    """What a sandbox's runs use: its image, or this host's OS."""
    return conf["image"] or HOST_OS


_GUARD_EXIT = 97
# Runs first inside the container: every hidden path must really be empty, or the command doesn't run.
_GUARD = ('for p in "$@"; do if [ -d "$p" ]; then [ -z "$(ls -A "$p")" ] || { echo "$HOPLON_GUARD $p"; exit 97; }; '
          'elif [ -s "$p" ]; then echo "$HOPLON_GUARD $p"; exit 97; fi; done; '
          'c=$HOPLON_CMD; unset HOPLON_CMD HOPLON_GUARD; exec /bin/sh -c "$c"')


FIXTURES_FILES = 200            # files one samples folder may hold
FIXTURES_BYTES = 64 * 1024 * 1024
FIXTURES_ASK = ("Use these samples for this job? Test output may send their contents to the model endpoint.")


class Fixtures:
    """Real samples for the model's runs: a private folder per project and job, outside the project, so neither git,
    the work session's copy nor the model's file tools reach it. The user fills it; nothing is used until they approve
    exactly what is there, after the secret checks. Approved, it is mounted read-only at /fixtures in each sandboxed
    run, and checked before each one: if anything changed, the approval is gone. The approval lasts this process only;
    the folder lasts until /fixtures clear, or the end of a work session that used it."""

    def __init__(self, root, project, job):
        self.root, self.project = root, os.path.realpath(project)
        base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(self.project) or "root")[:40]
        self.group = os.path.join(root, f"{base}-{hashlib.sha256(self.project.encode()).hexdigest()[:8]}")
        self.job = re.sub(r"[^A-Za-z0-9._-]", "_", job)[:80]
        self.dir = os.path.join(self.group, self.job)
        self.approved = None          # the digest the user approved, this process only
        self.used_in_work = False

    def problem(self):
        """Why the root can't hold samples, or None."""
        real, home = os.path.realpath(self.root), os.path.realpath(os.path.expanduser("~"))
        if real == home or home.startswith(real + os.sep):
            return f"fixtures_dir {self.root} is or holds your home directory"
        if real == self.project or real.startswith(self.project + os.sep):
            return f"fixtures_dir {self.root} is inside this project, where git and the model's file tools reach it"
        if any(c in self.dir for c in ":,"):
            return "the samples folder's path contains ':' or ',', which podman can't mount"
        return None

    def ensure(self):
        problem = self.problem()
        if problem:
            raise HoplonError(problem, EXIT_USAGE, "fixtures")
        for d in (self.root, self.group, self.dir):
            ensure_dir(d)
            os.chmod(d, 0o700)
        state_directory(self.dir)
        meta = os.path.join(self.group, self.job + ".json")
        if not os.path.exists(meta):
            write_private(meta, json.dumps({"project": self.project, "job": self.job, "created": now(),
                                            "pid": os.getpid()}) + "\n")
        return self.dir

    def scan(self, values=()):
        """What the folder holds: [{name, size, sha256, flag}], flag saying why a file can't be used, or None."""
        out, total = [], 0
        if not os.path.isdir(self.dir):
            return out
        for top, dirs, files in os.walk(self.dir):
            dirs.sort()
            for d in list(dirs):
                if os.path.islink(os.path.join(top, d)):
                    dirs.remove(d)
                    out.append({"name": os.path.relpath(os.path.join(top, d), self.dir), "size": 0, "sha256": "",
                                "flag": "a symbolic link"})
            for f in sorted(files):
                path = os.path.join(top, f)
                rel = os.path.relpath(path, self.dir)
                row = {"name": rel, "size": 0, "sha256": "", "flag": None}
                out.append(row)
                if len(out) > FIXTURES_FILES:
                    raise HoplonError(f"the samples folder has more than {FIXTURES_FILES} files", EXIT_USAGE, "fixtures")
                if not is_plain_file(path):
                    row["flag"] = "not a regular file"
                    continue
                with open(path, "rb") as fh:
                    data = fh.read(FIXTURES_BYTES + 1)
                total += len(data)
                if total > FIXTURES_BYTES:
                    raise HoplonError(f"the samples come to more than {FIXTURES_BYTES // 2**20} MB", EXIT_USAGE,
                                      "fixtures")
                row["size"], row["sha256"] = len(data), hashlib.sha256(data).hexdigest()
                text = data.decode("utf-8", "replace")
                counts = redact(text, values)[1]
                if secret_name(f):
                    row["flag"] = "its name looks like a secret's"
                elif counts:
                    row["flag"] = f"it looks like it holds a secret ({redaction_note(counts)})"
                elif any(c in rel for c in ":,"):
                    row["flag"] = "its name contains ':' or ','"
        return out

    @staticmethod
    def digest(rows):
        return hashlib.sha256(json.dumps([[r["name"], r["size"], r["sha256"]] for r in rows]).encode()).hexdigest()

    def add(self, paths, cwd):
        """Copy files into the folder, each under its own name. Returns the names."""
        self.ensure()
        names = []
        for p in paths:
            src = os.path.join(cwd, os.path.expanduser(p))
            if not is_plain_file(src):
                raise HoplonError(f"{p}: not a regular file (only files can be added; links are refused)", EXIT_USAGE,
                                  "fixtures")
            name = os.path.basename(src)
            dst = os.path.join(self.dir, name)
            if os.path.islink(dst):
                os.unlink(dst)
            shutil.copyfile(src, dst, follow_symlinks=False)
            os.chmod(dst, 0o600)
            names.append(name)
        self.approved = None if names else self.approved
        return names

    def approve(self, digest, values=()):
        rows = self.scan(values)
        if not rows:
            raise HoplonError("the samples folder is empty: add files first", EXIT_USAGE, "fixtures")
        bad = [r for r in rows if r["flag"]]
        if bad:
            raise HoplonError(f"{bad[0]['name']} can't be used: {bad[0]['flag']}. Scrub or replace it, or remove it "
                              "from the folder", EXIT_USAGE, "fixtures")
        if digest != self.digest(rows):
            raise HoplonError("the samples changed since they were listed: look again before approving", EXIT_USAGE,
                              "fixtures")
        self.approved = digest
        return rows

    def mount(self, values=()):
        """The folder to mount for a run, after checking it is what the user approved; None if nothing is approved.
        Raises HoplonError (and forgets the approval) if it changed."""
        if not self.approved:
            return None
        rows = self.scan(values)
        if self.digest(rows) != self.approved or any(r["flag"] for r in rows):
            self.approved = None
            raise HoplonError("the samples changed since you approved them, so the run was not started: approve them "
                              "again with /fixtures use", EXIT_USAGE, "fixtures")
        return self.dir

    def clear(self):
        self.approved = None
        if os.path.isdir(self.dir):
            shutil.rmtree(self.dir)
        meta = os.path.join(self.group, self.job + ".json")
        if os.path.exists(meta):
            os.unlink(meta)
        try:
            os.rmdir(self.group)
        except OSError:
            pass


def parse_sandbox(data, path):
    """A profile's "sandbox" object, checked. Without an image, runs use this host's OS (HOST_RO); with one, the
    project's tools and dependencies have to be in it already, since runs are offline."""
    if not isinstance(data, dict):
        raise HoplonError(f"{path}: 'sandbox' must be an object", EXIT_PROFILE)
    unknown = set(data) - SANDBOX_KEYS
    if unknown:
        raise HoplonError(f"{path}: unknown sandbox fields: {', '.join(sorted(unknown))}", EXIT_PROFILE)
    box = {"engine": data.get("engine", "auto"), "image": data.get("image"), "network": data.get("network", False),
           "timeout": data.get("timeout", SANDBOX_TIMEOUT), "memory": data.get("memory", SANDBOX_MEMORY),
           "pids": data.get("pids", SANDBOX_PIDS)}
    if box["engine"] not in ("auto", "podman"):
        raise HoplonError(f"{path}: sandbox 'engine' must be \"auto\" or \"podman\" (apptainer is not supported yet)",
                         EXIT_PROFILE)
    if box["image"] is not None and (not isinstance(box["image"], str)
                                     or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,255}", box["image"])):
        raise HoplonError(f"{path}: sandbox 'image' must be a container image name, or left out to use this host's OS",
                         EXIT_PROFILE)
    if not isinstance(box["network"], bool):
        raise HoplonError(f"{path}: sandbox 'network' must be true or false", EXIT_PROFILE)
    if type(box["timeout"]) is not int or not 1 <= box["timeout"] <= 3600:
        raise HoplonError(f"{path}: sandbox 'timeout' must be seconds, from 1 to 3600", EXIT_PROFILE)
    if not isinstance(box["memory"], str) or not re.fullmatch(r"[0-9]+[kmg]?", box["memory"]):
        raise HoplonError(f"{path}: sandbox 'memory' must be a size such as \"4g\"", EXIT_PROFILE)
    if type(box["pids"]) is not int or not 16 <= box["pids"] <= 100000:
        raise HoplonError(f"{path}: sandbox 'pids' must be a number of processes, from 16 to 100000", EXIT_PROFILE)
    return box


def is_plain_file(path):
    """A regular file, not a directory, a link or anything else of that name."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def run_in_shell(command, show):
    """Run a command in the user's shell, on this machine, as them: the REPL's :run, once the user has confirmed it.
    Output goes to show as it comes; up to 8 times OUTPUT_CAP of it is kept. ^C kills it. Returns (exit, output with
    ANSI codes removed), or None when ^C came before it started."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    captured, proc = [], None
    try:
        proc = subprocess.Popen([shell, "-c", command], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for chunk in iter(lambda: proc.stdout.read1(4096), b""):
            show(chunk)
            if sum(len(c) for c in captured) < OUTPUT_CAP * 8:
                captured.append(chunk)
        rc = proc.wait()
    except KeyboardInterrupt:
        if proc is None:
            return None
        proc.kill()
        rc = proc.wait()
    return rc, strip_ansi(b"".join(captured).decode("utf-8", "replace"))


class Sandbox:
    """Runs one command in a podman container: the project is a copy-on-write overlay whose changes are
    discarded, files that look like secrets are hidden, there is no network unless switched on, nothing from
    the home directory, a time limit, and memory and process limits where the host allows them."""

    running = False            # a container is running now: --json mode turns SIGINT into Ctrl-C only then
    stop = threading.Event()   # set by a stop message (--json) to stop the run going on now, as Ctrl-C does

    def __init__(self, conf, root, notice):
        self.conf, self.root, self.notice = conf, os.path.realpath(root), notice
        self.podman = shutil.which("podman")
        self.network = conf["network"]      # the switch; starts as the profile says, never saved with a session
        self.limits = True                  # False once the host refused memory and process limits
        self.cache = {}                     # (path, identity) -> (looks secret, digest, holds a value)
        self.excused = dict                 # -> the not-secret digests (Config.not_secret), set by the Engine
        self.counter = 0

    def problem(self):
        """Why the sandbox can't run anything here, or None."""
        if not self.podman:
            return "podman is not installed"
        if any(c in self.root for c in ":,"):
            return "the project's path contains ':' or ',', which podman can't mount"
        refused = coding_refusal(self.root)
        if refused:     # even with --coding-anywhere: the copy would hold all of it, and nothing here asks
            return f"this is {refused[0]}; run hoplon in a project directory"
        return None

    def info(self):
        return {"engine": "podman", "image": self.conf["image"], "uses": box_name(self.conf), "network": self.network,
                "timeout": self.conf["timeout"], "memory": self.conf.get("memory"), "pids": self.conf.get("pids"),
                "problem": self.problem()}

    def masks(self, values=()):
        """Files and directories to hide in the sandbox, relative to the project: secret-looking names, and
        files whose contents look like secrets. Dependency caches and a virtual environment's packages (VENV_PARTS)
        are not scanned; a .git directory only has its config checked. Raises HoplonError if the project is too big to check."""
        files, dirs, seen, venvs = [], [], 0, set()
        stack = [""]
        while stack:
            rel = stack.pop()
            try:
                with os.scandir(os.path.join(self.root, rel)) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue                    # unreadable here is unreadable in the sandbox too
            for e in entries:
                seen += 1
                if seen > SANDBOX_SCAN:
                    raise HoplonError(f"the project has more than {SANDBOX_SCAN} files, too many to check for "
                                     "secrets before a sandboxed run", kind="sandbox")
                r = f"{rel}/{e.name}" if rel else e.name
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir():
                        if secret_name(e.name):
                            dirs.append(r)
                        elif e.name == ".git":
                            if self.secret_file(os.path.join(e.path, "config"), values):
                                files.append(r + "/config")
                        elif e.name not in SCAN_SKIP and not (e.name in VENV_PARTS and rel in venvs):
                            stack.append(r)
                            if is_plain_file(os.path.join(e.path, "pyvenv.cfg")):
                                venvs.add(r)
                    elif e.is_file() and (secret_name(e.name) or self.secret_file(e.path, values)):
                        files.append(r)
                except OSError:
                    continue
        return files, dirs

    def secret_file(self, path, values):
        try:
            st = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode):
                return False
            key = (path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, tuple(values))
            if key not in self.cache:
                with open(path, "rb") as f:
                    text = f.read(READ_CAP).decode("utf-8", "replace")
                self.cache[key] = (looks_secret(text), text_digest(text), any(v and v in text for v in values))
            looks, digest, holds = self.cache[key]
            return holds or (looks and digest not in self.excused())
        except OSError:
            return False

    def argv(self, name, command, files, dirs, empty, guard, fixtures=None, host=None):
        """host: (root, mount flags) from host_root, for a sandbox with no image."""
        net = ["--network=none", "--http-proxy=false"] if not self.network else []
        limits = [f"--memory={self.conf['memory']}", f"--pids-limit={self.conf['pids']}"] if self.limits else []
        mounts = [f"--volume={self.root}:/work:O"]
        mounts += [f"--volume=/dev/null:/work/{f}:ro" for f in files]
        mounts += [f"--volume={empty}:/work/{d}:ro" for d in dirs]
        mounts += [f"--volume={fixtures}:/fixtures:ro"] if fixtures else []
        if host:
            mounts, base, pull = host[1] + mounts, ["--rootfs", host[0]], []
        else:
            base, pull = [self.conf["image"]], ["--pull=never"]
        return ([self.podman, "--log-level=error", "run", "--rm", f"--name={name}"] + pull + ["--init",
                 "--userns=keep-id", f"--user={os.getuid()}:{os.getgid()}", "--read-only", f"--tmpfs=/tmp:rw,size={SANDBOX_TMP},mode=1777",
                 "--cap-drop=all", "--security-opt=no-new-privileges", "--workdir=/work",
                 "--env=HOME=/tmp", f"--env=HOPLON_CMD={command}", f"--env=HOPLON_GUARD={guard}"]
                + net + limits + mounts + base + ["/bin/sh", "-c", _GUARD, "hoplon-guard"]
                + [f"/work/{p}" for p in files + dirs])

    def run(self, command, values=(), cap=OUTPUT_CAP * 8, timeout=None, fixtures=None):
        """Run command; returns (ran, exit code or None, output, note). ran is False when the command never
        started; the exit code is None when it was killed at the time limit (the sandbox's, or timeout seconds if
        that is sooner). Ctrl-C stops the container and is raised again."""
        limit = max(1, min(self.conf["timeout"], int(timeout))) if timeout is not None else self.conf["timeout"]
        deadline = time.monotonic() + limit     # one deadline: the scan for secrets and a retry come out of it
        problem = self.problem()
        if problem:
            return False, None, "", problem
        files, dirs = self.masks(values)
        bad = [p for p in files + dirs if any(c in p for c in ":,")]
        if bad:
            return False, None, "", f"can't hide {show_exact(bad[0])}: its name contains ':' or ','"
        if len(files) + len(dirs) > SANDBOX_MASKS:
            return False, None, "", f"more than {SANDBOX_MASKS} files look like secrets, too many to hide"
        self.counter += 1
        name = f"hoplon-{os.getpid()}-{self.counter}-{secrets.token_hex(6)}"
        guard = "hoplon-guard-" + secrets.token_hex(6)
        with tempfile.TemporaryDirectory(prefix="hoplon-empty-") as empty, \
                tempfile.TemporaryDirectory(prefix="hoplon-root-") as scratch:
            host = None if self.conf["image"] else host_root(scratch)
            os.chmod(empty, 0o500)
            for attempt in (1, 2):
                rc, out = self._podman(self.argv(name, command, files, dirs, empty, guard, fixtures, host), name,
                                       cap, deadline)
                if attempt == 1 and self.limits and rc in (125, 126) and "cgroup" in out:
                    self.limits = False
                    self.notice("this host doesn't let podman limit memory or processes (cgroups aren't delegated "
                                "to you); sandboxed commands are limited only by time and the size of /tmp")
                    continue
                break
        hidden = len(files) + len(dirs)
        if rc == _GUARD_EXIT and out.startswith(guard):
            return False, None, "", (f"refused: {out[len(guard):].strip()} should have been hidden in the sandbox but "
                              "wasn't, so the command did not run")
        if rc == "timeout":
            return True, None, out, f"killed after {limit} s"
        if rc == "late":
            return False, None, "", f"not run: the {limit} s time limit was used up before it could start"
        if rc in (125, 126, 127) and out.startswith("Error:"):
            hint = (f" (podman pull {self.conf['image']} first)" if self.conf["image"] and "image not known" in out
                    else "")
            return False, None, "", f"the sandbox could not start: {one_line(out.strip())[0]}{hint}"
        return True, rc, out, f"{hidden} secret-looking path{'s' if hidden != 1 else ''} hidden" if hidden else ""

    def _podman(self, argv, name, cap, deadline=None):
        chunks, size, proc = [], [0], None

        def drain():
            for chunk in iter(lambda: proc.stdout.read1(65536), b""):
                if size[0] < cap:
                    chunks.append(chunk)
                size[0] += len(chunk)

        deadline = deadline or time.monotonic() + self.conf["timeout"]
        if time.monotonic() >= deadline:
            return "late", ""
        try:                                # everything from here on, so a Ctrl-C at any point kills the container
            Sandbox.running = True
            if Sandbox.stop.is_set():       # a stop that came while the run was being set up
                raise KeyboardInterrupt
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            while True:                     # in slices, so a stop message can end it as Ctrl-C does
                try:
                    rc = proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if Sandbox.stop.is_set():
                        raise KeyboardInterrupt
                    if time.monotonic() >= deadline:
                        self._kill(name, proc)
                        rc = "timeout"
                        break
        except BaseException:              # Ctrl-C, a stop, or hoplon exiting on a signal
            self._kill(name, proc)
            raise
        finally:
            Sandbox.running = False
        reader.join(5)
        proc.stdout.close()
        out = strip_ansi(b"".join(chunks).decode("utf-8", "replace"))
        if size[0] > cap:
            out += f"\n[... {size[0] - cap} more bytes of output not kept ...]"
        return rc, out

    def _kill(self, name, proc):
        """Kill the container by name (proc is None if Ctrl-C came before podman's process was known). A second
        Ctrl-C is ignored until it is done: one that interrupted the kill would leave the container running."""
        main = threading.current_thread() is threading.main_thread()
        before = signal.signal(signal.SIGINT, signal.SIG_IGN) if main else None
        try:
            subprocess.run([self.podman, "--log-level=error", "kill", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, check=False)
            if proc is not None:
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        finally:
            if main:
                signal.signal(signal.SIGINT, before)




# --------------------------------------------------------------------------
# Work sessions: a private git repository the model works in

# Every git command hoplon runs for a work session: hooks, fsmonitor and signing off, so nothing in the copy or a
# repository's config can make git run a program; external diff and textconv drivers off for the same reason, and
# no bare repository found by searching. Filter drivers from your global and system config stay on: you set them
# up, for every repository. The copy has no config of its own that could name another. (A repository with Git LFS
# files is refused: the copy can't reach the LFS store.)
GIT_SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false",
            "-c", "diff.external=", "-c", "core.pager=cat", "-c", "safe.bareRepository=explicit")


def run_git(args, cwd, env=None, ok=(0,)):
    """git on the host: (exit code, output). GIT_* variables from the environment are dropped, so only what hoplon
    sets here decides which repository is used. Raises HoplonError for an exit code not in ok, and for an argument git
    can't be given (a NUL)."""
    full = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    full.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}, **(env or {}))
    try:
        r = subprocess.run(["git", *GIT_SAFE, *args], cwd=cwd, env=full, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors="replace", timeout=120)
    except FileNotFoundError:
        raise HoplonError("git is not installed", EXIT_USAGE, "work_refused") from None
    except subprocess.TimeoutExpired:
        raise HoplonError(f"git {args[0]} took more than 120 s", EXIT_USAGE, "work") from None
    except (ValueError, OSError) as e:
        raise HoplonError(f"git {args[0]} could not run: {e}", EXIT_USAGE, "work") from None
    if r.returncode not in ok:
        raise HoplonError(f"git {args[0]} failed: {one_line((r.stderr or r.stdout).strip())[0]}", EXIT_USAGE, "work")
    return r.returncode, r.stdout


def git_line(out):
    """A path git printed: its line, without the newline but with any spaces it ends with."""
    return out.rstrip("\n")


def state_home():
    return os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")


def fixtures_home():
    return os.path.join(state_home(), "hoplon", "fixtures")


def work_home():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "hoplon", "work")


class Copy:
    """A work session's copy of the project: a new git repository outside it, holding your last commit and nothing
    else (no history, no other branches), so neither the model nor the sandbox can read what that commit doesn't
    have. hoplon touches your repository twice: it reads that commit into the copy at the start, and fetches each
    commit made in the copy into the branch hoplon/work-N, which only hoplon writes. The model reaches the copy only
    through hoplon's file tools, which never write .git, and the sandbox runs on a throwaway overlay of it.
    The copy's files are in DIR/tree and its git directory in DIR/git, beside them: the sandbox mounts only the
    files, so it can't read the commit's objects, which hold every file the sandbox hides as a secret. The tree has
    no .git while the session runs (hoplon names the git directory in every command), so git in the sandbox finds no
    repository rather than a broken one; a copy kept at the end gets one pointing at DIR/git, for you to look at."""

    def __init__(self, top, home, branch, base, sub):
        self.top, self.home, self.branch, self.base, self.sub = top, home, branch, base, sub
        self.path = os.path.join(home, "tree")      # the copy's files: where the work is, and what you're shown
        self.gitdir = os.path.join(home, "git")
        self.published = base       # what the branch in your repository has (base: it doesn't exist yet)

    @property
    def wd(self):
        """The working directory inside the copy: the same place in the project that hoplon was started in."""
        return os.path.join(self.path, self.sub) if self.sub != "." else self.path

    @classmethod
    def create(cls, cwd):
        """A new copy of the repository cwd is in, from its last commit: (Copy, untracked file count). Refuses
        outside a repository, before the first commit, and with uncommitted changes to tracked files."""
        try:
            rc, out = run_git(["rev-parse", "--show-toplevel"], cwd, ok=(0, 128))
        except HoplonError as e:
            raise HoplonError(str(e), EXIT_USAGE, "work_refused") from None
        if rc:
            raise HoplonError("a work session needs a git repository: this directory isn't in one", EXIT_USAGE,
                             "work_refused")
        top = os.path.realpath(git_line(out))
        sub = os.path.relpath(os.path.realpath(cwd), top)
        if sub != "." and run_git(["ls-files", "-z", "--", sub], top)[1] == "":
            raise HoplonError("nothing in this directory is committed, so the copy wouldn't have it: cd to a "
                             "directory with committed files, or commit some here first", EXIT_USAGE, "work_refused")
        rc, head = run_git(["rev-parse", "--verify", "-q", "HEAD^{commit}"], top, ok=(0, 1))
        if rc:
            raise HoplonError("the repository has no commits yet: commit something first", EXIT_USAGE, "work_refused")
        head = head.strip()
        # In your own checkout, with your own config: git status there runs what yours does, nothing the model wrote.
        _, status = run_git(["status", "--porcelain", "--untracked-files=all"], top)
        lines = status.splitlines()
        changed = [ln for ln in lines if not ln.startswith("??")]
        if changed:
            raise HoplonError(f"the repository has uncommitted changes ({len(changed)} file"
                             f"{'s' if len(changed) != 1 else ''}): commit or stash them first, so the work starts "
                             "from what you have", EXIT_USAGE, "work_refused")
        lfs = [p for p in run_git(["ls-files", "-z", "--", ":(attr:filter=lfs)"], top)[1].split("\0") if p]
        if lfs:             # the copy can't reach your LFS store: checking these out there fails
            raise HoplonError(f"work sessions don't support Git LFS yet, and {len(lfs)} file"
                             f"{'s' if len(lfs) != 1 else ''} here {'are' if len(lfs) != 1 else 'is'} stored with it "
                             f"({show_exact(lfs[0])}{', …' if len(lfs) > 1 else ''})", EXIT_USAGE, "work_refused")
        if run_git(["show-ref", "--verify", "-q", "refs/heads/hoplon"], top, ok=(0, 1))[0] == 0:
            raise HoplonError("the repository has a branch named hoplon, so no branch hoplon/work-N can be made beside "
                             "it: rename that branch first", EXIT_USAGE, "work_refused")
        fmt = git_line(run_git(["rev-parse", "--show-object-format"], top)[1]) or "sha1"
        name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(top)) or "project"
        n = cls.free(top)
        try:
            home = os.path.realpath(tempfile.mkdtemp(prefix=f"{name}-{n}-", dir=ensure_dir(work_home())))  # ours
        except OSError as e:
            raise HoplonError(f"can't make the copy under {work_home()}: {e.strerror or e}", EXIT_USAGE,
                             "work_refused") from None
        try:
            run_git(["init", "-q", f"--object-format={fmt}", f"--separate-git-dir={home}/git", "--", f"{home}/tree"],
                    home)
            os.remove(os.path.join(home, "tree", ".git"))  # it would name a host path the sandbox can't reach
            c = cls(top, home, f"hoplon/work-{n}", head, sub)
            c.git(["fetch", "-q", "--depth", "1", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                   "--", top, head])
            c.git(["checkout", "-q", "-b", c.branch, head])
            c.record()
        except BaseException as e:          # a failure, a timeout or Ctrl-C: no half-made copy is left
            shutil.rmtree(home, ignore_errors=True)
            if isinstance(e, HoplonError) and os.path.exists(home):
                raise HoplonError(f"{e} (and the half-made copy at {home} could not be removed)", e.code,
                                 e.kind) from None
            raise
        return c, sum(1 for ln in lines if ln.startswith("??"))

    @staticmethod
    def free(top, n=1):
        """The first n from n on for which a branch hoplon/work-n can be made in your repository: none of that name,
        and none below it (hoplon/work-n/x)."""
        _, out = run_git(["for-each-ref", "--format=%(refname)", "refs/heads/hoplon/"], top)
        taken = {r.split("/")[3] for r in out.split("\n") if r.count("/") >= 3}
        while f"work-{n}" in taken:
            n += 1
        return n

    def on_branch(self):
        """What the branch in your repository points at now, or None if there is no such branch."""
        rc, out = run_git(["rev-parse", "--verify", "-q", f"refs/heads/{self.branch}^{{commit}}"], self.top,
                          ok=(0, 1))
        return out.strip() if rc == 0 else None

    def git(self, args, ok=(0,), env=None):
        return run_git(args, self.path, dict({"GIT_DIR": self.gitdir, "GIT_WORK_TREE": self.path}, **(env or {})), ok)

    def commit(self, message):
        """Commit everything in the copy: the short hash, or None if nothing changed. publish puts it on the branch."""
        message = plain_text(message)
        self.git(["add", "-A"])
        if self.git(["diff", "--cached", "--quiet", "--no-ext-diff"], ok=(0, 1))[0] == 0:
            return None
        who = {}
        if self.git(["config", "user.email"], ok=(0, 1))[0]:     # no identity configured: commit as hoplon
            who = {"GIT_AUTHOR_NAME": "hoplon", "GIT_AUTHOR_EMAIL": "hoplon@localhost",
                   "GIT_COMMITTER_NAME": "hoplon", "GIT_COMMITTER_EMAIL": "hoplon@localhost"}
        self.git(["commit", "-q", "--no-verify", "-m", message], env=who)
        return self.git(["rev-parse", "--short", "HEAD"])[1].strip()

    def publish(self):
        """Fetch the copy's commits into the branch in your repository. The branch is made by the first one; if a
        branch of that name has turned up since the session started, the next free name is used. Each fetch must
        add to what the branch had, so a branch that was moved or deleted meanwhile is an error, not overwritten;
        so is one found changed when there is nothing new to add, since the copy is then all that has the work.
        Signals wait for it: git killed halfway could leave a lock file in your repository. A branch already at
        the copy's commit counts as published: a fetch that finished just before a signal ended hoplon, say."""
        head = self.head()
        with signals_held():
            now = self.on_branch()
            if now == head:
                self.published = head
                return
            if self.published == self.base:
                if head == self.base:       # nothing made yet: no branch, whatever has that name
                    return
                if now is not None:
                    self.branch = f"hoplon/work-{self.free(self.top, int(self.branch.rsplit('-', 1)[1]) + 1)}"
                    self.git(["branch", "-q", "-m", self.branch])
            elif now != self.published:
                raise HoplonError(f"the branch {self.branch} was moved or deleted outside the session: its commits are "
                                 f"still in the copy at {self.path}", EXIT_USAGE, "work")
            run_git(["fetch", "-q", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                     "--no-auto-maintenance", "--", self.gitdir, f"{head}:refs/heads/{self.branch}"], self.top)
            self.published = head

    def summary(self):
        """(commits as "hash subject" lines, diff stat) since the session started."""
        _, log = self.git(["log", "--reverse", "--format=%h %s", f"{self.base}..HEAD"])
        _, stat = self.git(["diff", "--no-ext-diff", "--no-textconv", "--stat", self.base, "HEAD"])
        return log.splitlines(), stat.rstrip()

    def head(self):
        return self.git(["rev-parse", "HEAD"])[1].strip()

    def missing(self, paths):
        """Which of these paths (from the copy's top), files the model wrote that are still there, the last commit
        doesn't have: files git ignores, and files inside a submodule's directory, which the copy only holds as
        a commit id."""
        here = sorted(p for p in paths if os.path.isfile(os.path.join(self.path, p)))
        if not here:
            return []
        _, out = self.git(["ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *(f":(literal){p}" for p in here)])
        have = set(out.split("\0"))
        return [p for p in here if p not in have]

    def keep(self):
        """A copy left for you: give its files a .git, so git works there as in any checkout."""
        try:
            with open(os.path.join(self.path, ".git"), "x") as f:
                f.write("gitdir: ../git\n")
        except OSError:
            pass                    # the model's own .git can't be there (no tool writes one), but don't fail on it

    def record(self):
        """Say whose copy this is, beside it (the model's tools only reach tree/), so leftovers can tell whether its
        session is still running and whether the branch already has its commits."""
        with open(os.path.join(self.home, "hoplon.json"), "x", encoding="utf-8") as f:
            json.dump({"project": self.top, "branch": self.branch, "pid": os.getpid(), "created": now()}, f)
        remember_project(self.top)

    def remove(self):
        try:
            shutil.rmtree(self.home)
        except OSError as e:
            raise HoplonError(f"{self.home}: {e.strerror or e}", EXIT_USAGE, "work") from None


class Work:
    """A work session in progress: the copy, the task, the approved plan items and the limits."""

    def __init__(self, tree, task, steps, seconds, test, home):
        self.tree, self.task, self.steps, self.seconds, self.test = tree, task, steps, seconds, test
        self.home = home            # the Workdir hoplon goes back to when the session ends
        self.goal = None
        self.items = None           # the approved plan: [{"n", "title", "status", "commit"}]
        self.current = 0            # the item being worked on (0: no plan yet)
        self.used = 0
        self.since = time.monotonic()
        # Each item runs in a fresh context; the planner's context waits meanwhile, and gets a stuck item back.
        self.role = "plan"          # whose context is being sent: "plan", "item" or "review" (the planner, again)
        self.planner = None         # the planner's messages while an item's context runs
        self.reports = []           # finished items, for the next item's context: {"n", "summary", "notes", "commit"}
        self.reviewed = set()       # items the planner has already reviewed in this stretch
        self.swap = None            # (role, hint): a context swap due after the current step's tool results
        self.cleared_at = 0         # tokens in use when old output was last dropped
        self.planner_ep = None      # (Endpoint, Client) the planner's contexts go to, if not the session's own
        self.before = None          # what the session put aside, to be put back when it ends: approved files,
                                    # coding mode, the conversation if the planner is elsewhere, queued attachments
        self.written = set()        # paths written or edited in the copy, from its top: checked at the end, so a
                                    # file git ignores isn't lost with the copy

    def fresh(self, renew=False):
        """A new item or a fresh attempt: a new budget of steps. renew: the user has just spoken (a message, or the
        plan's approval), so the time without them starts again too; finishing an item doesn't renew it."""
        self.used = 0
        self.reviewed = set()
        if renew:
            self.since = time.monotonic()

    def stuck(self):
        """The current item used its steps, and the planner hasn't reviewed it yet in this stretch."""
        return (self.role == "item" and self.current and self.used >= self.steps
                and self.current not in self.reviewed and time.monotonic() - self.since < self.seconds)

    def limit(self):
        """Why the session must pause now, or None."""
        what = f"item {self.current}" if self.current else "the plan"
        if self.used >= self.steps:
            return f"{what} has used its {self.steps} model steps without being finished"
        if time.monotonic() - self.since >= self.seconds:
            return f"the session has run {self.seconds // 60} min without you"
        return None

    def done(self):
        return bool(self.items) and all(it["status"] == "done" for it in self.items)

    def left(self):
        """Seconds the session may still run without the user: a sandboxed run is cut off when they are up."""
        return self.seconds - (time.monotonic() - self.since)

    def info(self):
        return {"branch": self.tree.branch, "base": self.tree.base[:12], "path": self.tree.path,
                "steps_per_item": self.steps, "time_limit": self.seconds, "test": self.test,
                "current": self.current, "items": self.items, "role": self.role,
                "planner": ({"endpoint": self.planner_ep[0].name, "model": self.planner_ep[0].model}
                            if self.planner_ep else None)}


def short_title(text, n=COMMIT_TITLE):
    """text cut to n characters at a word, for a commit title."""
    if len(text) <= n:
        return text
    cut = text[:n - 1].rsplit(" ", 1)[0] if " " in text[:n - 1] else text[:n - 1]
    return cut.rstrip(" ,;:.-\u2014") + "\u2026"


def item_brief(work, hint=None):
    """The first message of an item's fresh context: the task, the item, and what earlier items left for it."""
    it = work.items[work.current - 1]
    parts = [f"Work session. The task:\n\n{work.task}",
             f"You are working on item {it['n']} of {len(work.items)}: {it['title']}",
             "This is a fresh context: the conversations about earlier items are gone, but their changes are in the "
             "working directory, committed. Read the files you need before you change them."]
    if work.reports:
        lines = []
        for r in work.reports:
            lines.append(f"- {r['n']}. {r['summary']} ({r['commit']})")
            if r["notes"]:
                lines += ["    " + ln for ln in r["notes"].splitlines()]
        parts.append("Finished so far, with the notes each left for later items:\n" + "\n".join(lines))
    if hint:
        parts.append("For this attempt:\n" + hint)
    parts.append(f"When the item is done and tested, call finish_item with n={it['n']}, and leave notes for the "
                 "items after it.")
    return "\n\n".join(parts)


def prune_bulk(msgs, upto):
    """Drop the bodies of bulky messages before index upto: file contents, command output, and whole files or texts
    the model wrote with write_file or edit_file. They are on disk or can be run again. Returns how many."""
    n = 0
    for m in msgs[:upto]:
        if m.get("_pruned"):
            continue
        if m.get("_bulk"):
            if m.get("role") == "tool":
                m["content"] = "[contents dropped to save room; read again if needed]"
            else:
                first = (m.get("content") or "").split("\n\n[", 1)[0]
                m["content"] = first + "\n\n[attached contents dropped to save room; ask for them again if needed]"
            m["_pruned"] = True
            n += 1
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            calls, changed = json.loads(json.dumps(m["tool_calls"])), False     # history shares the old ones
            for tc in calls:
                f = tc.get("function") or {}
                if f.get("name") not in ("write_file", "edit_file"):
                    continue
                try:
                    a = json.loads(f.get("arguments") or "{}")
                except ValueError:
                    continue
                if not isinstance(a, dict):
                    continue
                for k in ("content", "old_text", "new_text"):
                    if isinstance(a.get(k), str) and len(a[k]) > 200:
                        a[k] = "[dropped to save room; the file is on disk]"
                        changed = True
                f["arguments"] = json.dumps(a, ensure_ascii=False)
            if changed:
                m["tool_calls"] = calls
                m["_pruned"] = True
                n += 1
    return n

# Offered only in coding mode, when the profile gives a sandbox that can run here.
def work_limits(steps, seconds, where):
    """A work session's limits, checked: model steps per plan item, and seconds unattended."""
    if steps is not None and (type(steps) is not int or not 1 <= steps <= WORK_STEPS_MAX):
        raise HoplonError(f"{where}: steps per item must be a number from 1 to {WORK_STEPS_MAX}", EXIT_USAGE,
                         "bad_message")
    if seconds is not None and (type(seconds) is not int or not 60 <= seconds <= WORK_TIME_MAX):
        raise HoplonError(f"{where}: the time limit must be seconds, from 60 to {WORK_TIME_MAX}", EXIT_USAGE,
                         "bad_message")
    return steps, seconds


def parse_work(data, path):
    """A profile's "work" object, checked: the defaults for work sessions, and the command that must pass before a
    plan item counts as done."""
    if not isinstance(data, dict):
        raise HoplonError(f"{path}: 'work' must be an object", EXIT_PROFILE)
    unknown = set(data) - WORK_KEYS
    if unknown:
        raise HoplonError(f"{path}: unknown work fields: {', '.join(sorted(unknown))}", EXIT_PROFILE)
    test = data.get("test")
    if test is not None and (not isinstance(test, str) or not test.strip() or "\n" in test):
        raise HoplonError(f"{path}: work 'test' must be one command line", EXIT_PROFILE)
    try:
        steps, seconds = work_limits(data.get("steps_per_item"), data.get("time_limit"), path)
    except HoplonError as e:
        raise HoplonError(str(e), EXIT_PROFILE) from None
    planner = data.get("planner")
    if planner is not None:
        if not isinstance(planner, dict) or not planner or set(planner) - {"endpoint", "model"}:
            raise HoplonError(f"{path}: work 'planner' must be an object with 'model', 'endpoint' or both",
                             EXIT_PROFILE)
        ep, model = planner.get("endpoint"), planner.get("model")
        if ep is not None and (not isinstance(ep, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", ep)):
            raise HoplonError(f"{path}: work planner 'endpoint' must be an endpoint's name", EXIT_PROFILE)
        if model is not None and (not isinstance(model, str) or not MODEL_ID.fullmatch(model)):
            raise HoplonError(f"{path}: work planner 'model' must be a model's name", EXIT_PROFILE)
        planner = {"endpoint": ep, "model": model}
    return {"test": test, "steps_per_item": steps or WORK_STEPS, "time_limit": seconds or WORK_TIME,
            "planner": planner}


# --------------------------------------------------------------------------
# Where coding mode refuses to run, and the projects hoplon has worked in (for hoplon --leftovers)

CODING_REFUSED_UNDER_HOME = (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store", ".config/gcloud")


CODING_RISKS = {     # what --coding-anywhere exposes, by the kind of place; shown when it is used
    "home": "every file under your home directory that is not hidden (documents, downloads, every project)",
    "above": "every file under here that is not hidden: your whole home directory, and other people's and the "
             "system's files where you can read them",
    "config": "your endpoint files, every saved session and the audit log; a key file here is read like any other",
    "credentials": "the credentials kept here; only the ones hoplon recognises as secrets will still ask",
}


def coding_refusal(cwd):
    """Why coding mode is refused in this directory, as (phrase, kind of place), or None. A project directory
    under your home is fine; these are the places that hold much more than one project. Real paths, compared
    by component."""
    def inside(path, root):
        return path == root or path.startswith(root.rstrip("/") + "/")
    here = os.path.realpath(cwd)
    home = os.path.realpath(os.path.expanduser("~"))
    if here == home:
        return "your home directory", "home"
    if inside(home, here):
        return "a directory that contains your home directory", "above"
    if inside(here, os.path.realpath(config_home())):
        return "hoplon's own config directory", "config"
    for d in CODING_REFUSED_UNDER_HOME:
        if inside(here, os.path.realpath(os.path.join(home, d))):     # ~/.ssh may be a link to elsewhere
            return f"~/{d}, which holds credentials", "credentials"
    return None


def projects_file():
    return os.path.join(state_home(), "hoplon", "projects.json")


def remember_project(top):
    """Note a repository a work session ran in, so leftovers can look for its hoplon/work-N branches later."""
    path = projects_file()
    try:
        known = json.load(open(path, encoding="utf-8"))
        known = [k for k in known if isinstance(k, str)] if isinstance(known, list) else []
    except (OSError, ValueError):
        known = []
    if top in known:
        return
    try:
        ensure_dir(os.path.dirname(path))
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(known + [top], f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        pass                        # only the listing is poorer for it
