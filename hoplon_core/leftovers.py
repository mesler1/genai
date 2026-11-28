# hoplon_core/leftovers.py: everything sandboxes and work sessions left behind: listed, aged and removed on request.
#
# Copies, branches, containers, fixtures and temp files that hoplon made, with whether each is still in use, and
# the cleanup that removes the stale ones when the user asks (hoplon --leftovers).
#
# Uses: base, sandbox

# --------------------------------------------------------------------------
# Leftovers: everything hoplon makes for sandboxes and jobs, what is likely stale, and how to remove it

LEFTOVER_TEMP_AGE = 3600        # a temp file whose maker can't be known is likely stale after this many seconds
LEFTOVER_WALK = 50000           # entries counted for a size, at most
CONTAINER_NAME = re.compile(r"hoplon-(\d+)-\d+-[0-9a-f]{12}\Z")
GUARD_NAME = re.compile(r"hoplon-guard-[0-9a-f]{12}\Z")


def hoplon_alive(pid):
    """Whether a hoplon is running as this pid (a reused pid running something else counts as gone)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False                # not ours, so not a hoplon of ours
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"hoplon" in f.read()
    except OSError:
        return True                 # no /proc: trust the pid


def tree_size(path):
    total, n = 0, 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    for dirpath, dirs, files in os.walk(path):
        for name in files + dirs:
            n += 1
            if n > LEFTOVER_WALK:
                return total
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def socket_live(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def sh(*words):
    return " ".join(shlex.quote(w) for w in words)


def leftover(kind, what, path, age, stale, remove, *, safe=True, note="", owner=""):
    return {"id": f"{kind}:{hashlib.sha256(path.encode()).hexdigest()[:12]}", "kind": kind, "what": what,
            "path": path, "bytes": tree_size(path) if os.path.exists(path) else None,
            "age": max(0, int(age)) if age is not None else None, "owner": owner, "stale": stale,
            "safe": bool(stale) and safe, "remove": remove, "note": note}


def mtime_age(path):
    try:
        return time.time() - os.lstat(path).st_mtime
    except OSError:
        return None


def created_age(iso, path):
    try:
        return time.time() - datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return mtime_age(path)


def leftover_fixtures(root):
    out = []
    if not os.path.isdir(root):
        return out
    for group in sorted(os.listdir(root)):
        gpath = os.path.join(root, group)
        if not os.path.isdir(gpath) or os.path.islink(gpath):
            continue
        for job in sorted(os.listdir(gpath)):
            jpath = os.path.join(gpath, job)
            if not os.path.isdir(jpath) or os.path.islink(jpath):
                continue
            meta = os.path.join(gpath, job + ".json")
            try:
                m = json.load(open(meta, encoding="utf-8"))
                m = m if isinstance(m, dict) else {}
            except (OSError, ValueError):
                m = {}
            pid = m.get("pid")
            live = hoplon_alive(pid)
            stale = None if live else (f"the hoplon that made it (pid {pid}) has exited" if pid else
                                       "no record of the hoplon that made it")
            out.append(leftover("fixtures", f"samples for {m.get('project') or group}", jpath,
                                created_age(m.get("created"), jpath), stale,
                                sh("rm", "-rf", "--", jpath, meta), owner=f"pid {pid}" if pid else ""))
    return out


def copy_heads(home, rec):
    """The copy's last commit, and whether your repository's branch already has it (so removing loses nothing)."""
    gitdir = os.path.join(home, "git")
    rc, head = run_git(["--git-dir", gitdir, "rev-parse", "--verify", "-q", "HEAD"], home, ok=(0, 1, 128))
    head = head.strip() if rc == 0 else None
    project, branch = rec.get("project"), rec.get("branch")
    if not head or not (isinstance(project, str) and isinstance(branch, str) and os.path.isdir(project)):
        return head, False
    rc, _ = run_git(["merge-base", "--is-ancestor", head, f"refs/heads/{branch}"], project, ok=(0, 1, 128))
    return head, rc == 0


def leftover_copies(base):
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        home = os.path.join(base, name)
        if not os.path.isdir(home) or os.path.islink(home):
            continue
        try:
            rec = json.load(open(os.path.join(home, "hoplon.json"), encoding="utf-8"))
            rec = rec if isinstance(rec, dict) else {}
        except (OSError, ValueError):
            rec = {}
        pid, project, branch = rec.get("pid"), rec.get("project"), rec.get("branch")
        if hoplon_alive(pid):
            out.append(leftover("work", "work session copy, in use", home, created_age(rec.get("created"), home),
                                None, "", owner=f"pid {pid}"))
            continue
        try:
            _, saved = copy_heads(home, rec)
        except HoplonError:
            saved = False
        stale = f"its work session (pid {pid}) has ended" if pid else "no record of its work session"
        note = "" if saved else (
            f"it may hold commits that {branch} in {project} doesn't have: to keep them, run "
            f"{sh('git', '-C', project, 'fetch', os.path.join(home, 'git'), 'HEAD:refs/heads/' + branch + '-kept')}"
            if project and branch else "hoplon can't tell whether it holds work you want: look in it before removing it")
        out.append(leftover("work", f"work session copy of {project or name}" + (f" ({branch})" if branch else ""),
                            home, created_age(rec.get("created"), home), stale, sh("rm", "-rf", "--", home),
                            safe=saved, note=note, owner=f"pid {pid}" if pid else ""))
    return out


def leftover_branches(projects, live_branches):
    out = []
    for top in projects:
        if not os.path.isdir(top):
            continue
        try:
            _, refs = run_git(["for-each-ref", "--format=%(refname:short) %(committerdate:unix) %(objectname)",
                               "refs/heads/hoplon/"], top)
        except HoplonError:
            continue
        for line in refs.splitlines():
            parts = line.split()
            if len(parts) != 3 or not re.fullmatch(r"hoplon/work-\d+", parts[0]):
                continue
            branch, when, obj = parts
            age = time.time() - int(when) if when.isdigit() else None
            if (top, branch) in live_branches:
                out.append(leftover("branch", f"{branch} in {top}, in use", top + "#" + branch, age, None, ""))
                continue
            try:
                merged = run_git(["merge-base", "--is-ancestor", obj, "HEAD"], top, ok=(0, 1, 128))[0] == 0
            except HoplonError:
                merged = False
            item = leftover("branch", f"{branch} in {top}", top + "#" + branch, age,
                            "merged into your current branch" if merged else None,
                            sh("git", "-C", top, "branch", "-d" if merged else "-D", branch),
                            note="" if merged else f"not merged: your work to review (git -C {shlex.quote(top)} "
                                                   f"log {branch}), then merge it or delete it")
            item["bytes"] = None
            out.append(item)
    return out


def leftover_containers():
    if not shutil.which("podman"):
        return []
    try:
        r = subprocess.run(["podman", "ps", "-a", "--format", "json"], capture_output=True, text=True, timeout=15)
        rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    out = []
    for c in rows if isinstance(rows, list) else []:
        names = c.get("Names") if isinstance(c, dict) else None
        name = names[0] if isinstance(names, list) and names and isinstance(names[0], str) else None
        if not name:
            continue
        m = CONTAINER_NAME.match(name)
        created = c.get("Created")
        age = time.time() - created if isinstance(created, (int, float)) else None
        if m:
            pid = int(m.group(1))
            stale = None if hoplon_alive(pid) else f"the hoplon that started it (pid {pid}) has exited"
        elif GUARD_NAME.match(name):
            stale = None if age is not None and age < 600 else "a secret-file check that didn't finish"
        else:
            continue
        item = leftover("container", f"sandbox container ({c.get('State', '?')})", "podman:" + name, age, stale,
                        sh("podman", "rm", "-f", name))
        item["bytes"] = None
        out.append(item)
    return out


def leftover_temp(tmp):
    out = []
    try:
        names = sorted(os.listdir(tmp))
    except OSError:
        return out
    kinds = (("hoplon-empty-", True, "an empty folder hidden files are covered with during a run"),
             ("hoplon-sbx-", True, "a scratch root from sandbox/run"),
             ("hoplon-sh-", False, "a captured :sh session: it may hold what you typed and saw"),
             ("hoplon-raw-", False, "a raw-output file for the pager"),
             ("hoplon-", False, "an editor buffer: it may hold a command, plan or text you reviewed"))
    uid = os.getuid()
    for name in names:
        path = os.path.join(tmp, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if st.st_uid != uid:
            continue
        for prefix, is_dir, what in kinds:
            if not name.startswith(prefix) or stat.S_ISDIR(st.st_mode) != is_dir:
                continue
            if prefix == "hoplon-" and not name.endswith(".txt"):
                break
            age = time.time() - st.st_mtime
            stale = f"left over, {age // 3600:.0f} h old" if age >= LEFTOVER_TEMP_AGE else None
            out.append(leftover("temp", what, path, age, stale, sh("rm", "-rf", "--", path)))
            break
    return out


def leftover_tui(runtime, tmux_dir):
    out = []
    for base, pattern, what in ((runtime, "hoplon-tui.", "hoplon-tui's private folder"),):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if not name.startswith(pattern) or not os.path.isdir(path) or os.path.islink(path):
                continue
            if os.lstat(path).st_uid != os.getuid():
                continue
            live = socket_live(os.path.join(path, "sock"))
            out.append(leftover("tui", what + (", in use" if live else ""), path, mtime_age(path),
                                None if live else "no hoplon-tui is listening in it", sh("rm", "-rf", "--", path)))
    try:
        names = sorted(os.listdir(tmux_dir))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(tmux_dir, name)
        if not re.fullmatch(r"hoplon-tui-\d+", name):
            continue
        live = socket_live(path)
        out.append(leftover("tui", "hoplon-tui's tmux server" + (", running" if live else ""), path, mtime_age(path),
                            None if live else "the tmux server is gone; only its socket file is left",
                            sh("rm", "-f", "--", path) if not live else sh("tmux", "-L", name, "kill-server")))
    return out


def leftovers(cfg, extra_projects=()):
    """Everything hoplon made for sandboxes and jobs that is still there: [item], each with what it is, where, its
    size and age, why it is likely stale (or None while in use), and the command that removes it."""
    copies = leftover_copies(work_home())
    projects, live = [], set()
    try:
        known = json.load(open(projects_file(), encoding="utf-8"))
        projects = [k for k in known if isinstance(k, str)] if isinstance(known, list) else []
    except (OSError, ValueError):
        pass
    for home in (c["path"] for c in copies):
        try:
            rec = json.load(open(os.path.join(home, "hoplon.json"), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("project"), str):
            if rec["project"] not in projects:
                projects.append(rec["project"])
            if hoplon_alive(rec.get("pid")) and isinstance(rec.get("branch"), str):
                live.add((rec["project"], rec["branch"]))
    projects += [p for p in extra_projects if p and p not in projects]
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    runtime = runtime if runtime and os.path.isdir(runtime) else "/tmp"
    tmux_dir = os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}")
    items = (leftover_fixtures(cfg.fixtures_dir) + copies + leftover_branches(projects, live) + leftover_containers()
             + leftover_temp(tempfile.gettempdir()) + leftover_tui(runtime, tmux_dir))
    seen, out = set(), []
    for it in items:
        if it["id"] not in seen:
            seen.add(it["id"])
            out.append(it)
    return out


def remove_leftover(it):
    """Remove one stale, safe leftover; the error text, or None."""
    kind, path = it["kind"], it["path"]
    try:
        if kind == "branch":
            top, branch = path.rsplit("#", 1)
            run_git(["branch", "-d", "--", branch], top)
        elif kind == "container":
            r = subprocess.run(["podman", "rm", "-f", path.split(":", 1)[1]], capture_output=True, text=True,
                               timeout=30)
            if r.returncode != 0:
                return one_line(r.stderr.strip() or f"podman exited {r.returncode}")[0]
        elif os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
            if kind == "fixtures":
                meta = path + ".json"
                if os.path.exists(meta):
                    os.unlink(meta)
                try:
                    os.rmdir(os.path.dirname(path))
                except OSError:
                    pass
        elif os.path.lexists(path):
            os.unlink(path)
    except (OSError, subprocess.SubprocessError, HoplonError) as e:
        return str(e)
    return None


def clean_leftovers(cfg, ids, audit, source):
    """Remove the listed leftovers that are still stale and safe when looked at again now."""
    want = set(ids)
    removed, failed, skipped = [], [], []
    for it in leftovers(cfg):
        if it["id"] not in want:
            continue
        want.discard(it["id"])
        if not it["safe"]:
            skipped.append({"id": it["id"], "why": "in use" if not it["stale"] else "it needs a look first"})
            continue
        err = remove_leftover(it)
        (failed.append({"id": it["id"], "error": err}) if err else removed.append(it["id"]))
        audit.log("leftover_removed" if not err else "leftover_failed", kind=it["kind"], path=it["path"],
                  source=source, **({"error": err} if err else {}))
    skipped += [{"id": i, "why": "already gone"} for i in sorted(want)]
    return {"removed": removed, "failed": failed, "skipped": skipped}


def human_bytes(n):
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_age(s):
    if s is None:
        return ""
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if s >= size:
            return f"{s // size:.0f} {unit}"
    return f"{s:.0f} s"


def leftover_item_lines(it, lines):
    facts = ", ".join(x for x in (human_bytes(it["bytes"]), human_age(it["age"]) and human_age(it["age"]) + " old")
                      if x)
    lines.append(f"  {'✗' if it['stale'] else '·'} {show_exact(it['what'])}" + (f"  [{facts}]" if facts else ""))
    lines.append(f"      {show_exact(it['path'])}")
    if it["stale"]:
        lines.append(f"      likely stale: {show_exact(it['stale'])}")
    if it["note"]:
        lines.append(f"      ! {show_exact(it['note'])}")
    if it["remove"] and it["stale"]:
        lines.append(f"      remove: {show_exact(it['remove'])}")


def leftover_lines(items, clean_hint):
    if not items:
        return ["Nothing left over: no samples folders, work copies, hoplon/work-N branches, sandbox containers, "
                "temp files or hoplon-tui folders."]
    stale = [i for i in items if i["stale"]]
    lines = [f"Left over: {len(items)} ({len(stale)} likely stale)"]
    groups = {}
    for it in items:            # many alike (old tmux sockets, say) are one entry, with one command for all
        key = (it["kind"], it["what"], it["stale"], it["note"]) if it["stale"] and it["safe"] else it["id"]
        groups.setdefault(key, []).append(it)
    for group in groups.values():
        if len(group) > 3:
            it = group[0]
            ages = [g["age"] for g in group if g["age"] is not None]
            span = f"{human_age(min(ages))}–{human_age(max(ages))} old" if ages else ""
            lines.append(f"  ✗ {len(group)} × {show_exact(it['what'])}" + (f"  [{span}]" if span else ""))
            lines += [f"      {show_exact(g['path'])}" for g in group]
            lines.append(f"      likely stale: {show_exact(it['stale'])}")
            words = shlex.split(it["remove"])
            cut = len(words) - 1        # the command is the same but for its last word, the path
            lines.append(f"      remove: {show_exact(sh(*words[:cut], *(shlex.split(g['remove'])[-1] for g in group)))}")
            continue
        for it in group:
            leftover_item_lines(it, lines)
    safe = [i for i in stale if i["safe"]]
    if safe:
        lines.append(f"{clean_hint} removes the {len(safe)} stale one{'s' if len(safe) != 1 else ''} it can remove "
                     "safely, after asking." + (f" {len(stale) - len(safe)} need a look first (marked !)."
                                                if len(stale) > len(safe) else ""))
    elif stale:
        lines.append("Each stale one needs a look first (marked !): use its remove command when you are sure.")
    return lines


def git_top_here():
    try:
        rc, out = run_git(["rev-parse", "--show-toplevel"], os.getcwd(), ok=(0, 128))
        return os.path.realpath(out.strip()) if rc == 0 and out.strip() else None
    except HoplonError:
        return None


def leftovers_command(cfg, args):
    items = leftovers(cfg, [git_top_here()])
    if args.leftovers and args.json:
        print(json.dumps({"type": "leftovers", "items": items}))
        return EXIT_OK
    if args.json:
        raise HoplonError("--clean-stale asks first, so not with --json: wrappers send the leftovers message",
                          EXIT_USAGE)
    print("\n".join(leftover_lines(items, "hoplon --clean-stale")))
    if not args.clean_stale:
        return EXIT_OK
    safe = [i for i in items if i["safe"]]
    if not safe:
        print("Nothing to remove safely.")
        return EXIT_OK
    if not sys.stdin.isatty():
        raise HoplonError("--clean-stale needs a terminal: it asks before removing anything", EXIT_USAGE)
    if not ask_yes(f"Remove the {len(safe)} stale one{'s' if len(safe) != 1 else ''} marked ✗ (not those marked !)?"):
        print("Nothing removed.")
        return EXIT_OK
    done = clean_leftovers(cfg, [i["id"] for i in safe], cfg.audit, "--clean-stale")
    print(f"Removed {len(done['removed'])}." + "".join(f"\n  couldn't remove {f['id']}: {show_exact(f['error'])}"
                                                    for f in done["failed"]))
    return EXIT_OK if not done["failed"] else EXIT_ERROR
