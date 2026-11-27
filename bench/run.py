#!/usr/bin/env python3
"""hoplon's benchmark: a few fixed tasks, each run as a work session over JSON, to check a prompt or model change
against real runs instead of guessing.

    bench/run.py run -p PROFILE [-e ENDPOINT] [-t TASK ...] [-o results.json]
    bench/run.py compare before.json after.json

A task is a directory under bench/tasks/: task.md (the task the model gets; its first line is a title), repo/ (the
project's files, committed as the start), and check.sh (run afterwards on the session's branch, in the profile's
sandbox, offline; it never reaches the model, so it may hold tests the model can't see). The profile must have a
"sandbox", as any work session needs (and may have a "work" section), and you bless it as usual: the runner uses your own hoplon
configuration and changes nothing in it.

For each task the runner copies repo/ to a new git repository, starts `hoplon --json -p PROFILE` there, sends the
task, approves the work plan (and any new plan), declines anything else that asks, and nudges the model to go on when
a turn ends before the session does, up to --nudges times. It records, per task: whether the check passed, the
session's end, items done, steps per item, retries, fresh contexts, model requests and generated tokens, wall time,
and the diff size. Every run costs real model requests on the endpoint you name.
"""

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(HERE, "tasks")
HOPLON = os.path.join(os.path.dirname(HERE), "hoplon")
NUDGE = ("Go on with the approved plan on your own; nobody is here to answer questions. Decide for yourself, "
         "and finish the items.")
GIT_ENV = {"GIT_AUTHOR_NAME": "bench", "GIT_AUTHOR_EMAIL": "bench@localhost", "GIT_COMMITTER_NAME": "bench",
           "GIT_COMMITTER_EMAIL": "bench@localhost", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
           "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


class BenchError(Exception):
    pass


def load_tasks(root=TASKS, names=None):
    """{name: task directory}, every one checked to have task.md, repo/ and check.sh."""
    found = sorted(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)))
    names = names or found
    out = {}
    for n in names:
        d = os.path.join(root, n)
        if n not in found:
            raise BenchError(f"no task named {n!r} in {root} (there are: {', '.join(found)})")
        missing = [f for f in ("task.md", "repo", "check.sh") if not os.path.exists(os.path.join(d, f))]
        if missing:
            raise BenchError(f"task {n}: missing {', '.join(missing)}")
        out[n] = d
    return out


def git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_ENV)
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


def make_repo(task_dir, where):
    """The task's repo/ as a fresh git repository on main, with one start commit."""
    root = os.path.join(where, "repo")
    shutil.copytree(os.path.join(task_dir, "repo"), root, symlinks=False)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", "start")
    return root


def sandbox_of(profile, endpoint, hoplon, env):
    """The profile's sandbox (engine and image), as hoplon itself resolves the profile."""
    args = [sys.executable, hoplon, "--remote-config", "-p", profile] + (["-e", endpoint] if endpoint else [])
    r = subprocess.run(args, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise BenchError(f"hoplon could not use profile {profile!r}: {r.stderr.strip() or r.stdout.strip()}")
    prof = json.loads(r.stdout).get("profile") or {}
    if not prof.get("sandbox"):
        raise BenchError(f"profile {profile!r} needs a \"sandbox\": work sessions test in it")
    sb = prof["sandbox"]
    engine = sb.get("engine", "auto")
    if engine == "auto":
        engine = next((e for e in ("podman", "docker") if shutil.which(e, path=env.get("PATH"))), None)
        if not engine:
            raise BenchError("neither podman nor docker is on PATH, for the checks")
    return engine, sb["image"]


class Session:
    """One hoplon --json process, read on a thread so a wait can time out."""

    def __init__(self, args, cwd, env):
        self.p = subprocess.Popen(args, cwd=cwd, env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.stderr = []
        threading.Thread(target=lambda: self.stderr.extend(self.p.stderr), daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                pass
        self.q.put(None)

    def send(self, obj):
        try:
            self.p.stdin.write(json.dumps(obj) + "\n")
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def next(self, deadline):
        try:
            return self.q.get(timeout=max(0.1, deadline - time.monotonic()))
        except queue.Empty:
            return "timeout"

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()


def drive(s, task_text, nudges, limit):
    """Run one work session to its end, answering as an absent user would. Returns what it saw."""
    r = {"end": None, "plans": 0, "items": [], "retries": 0, "fresh_contexts": 0, "requests": 0, "tokens": 0,
         "declined": [], "nudges": 0, "errors": [], "branch": None, "commits": []}
    deadline = time.monotonic() + limit
    steps, ended, started = 0, False, False
    while True:
        ev = s.next(deadline)
        if ev == "timeout":
            r["end"] = "timeout"
            break
        if ev is None:
            r["end"] = r["end"] or "hoplon exited"
            break
        t = ev.get("type")
        if t == "hello":
            s.send({"type": "work", "task": task_text})
        elif t == "proposal":
            if ev.get("kind") == "work_plan" and ev.get("digest"):
                r["plans"] += 1
                s.send({"type": "decision", "id": ev["id"], "digest": ev["digest"], "decision": "accept"})
            else:
                if ev.get("digest") and not ev.get("auto"):
                    r["declined"].append(ev.get("kind"))
                    s.send({"type": "decision", "id": ev["id"], "digest": ev["digest"], "decision": "decline"})
                if ev.get("kind") not in ("finish", "retry"):
                    steps += 1          # a step of the item: what the model proposed, done or declined
        elif t == "generated":
            r["requests"] += 1
            r["tokens"] += ev.get("tokens") or 0
        elif t == "work":
            state = ev.get("state")
            started = started or state == "started"
            if state == "item":
                r["items"].append(steps)
                steps = 0
            elif state == "context":
                r["retries" if ev.get("role") == "review" else "fresh_contexts"] += 1
            elif state == "ended":
                ended = True
                r.update(end=ev.get("why"), branch=ev.get("branch"), commits=ev.get("commits") or [])
        elif t == "error":
            r["errors"].append(f"{ev.get('code')}: {ev.get('message')}"[:300])
            if ev.get("code") == "work_refused":
                r["end"] = "refused"
                break
        elif t == "done":
            if ended:
                break
            if not started:
                r["end"] = "not started"
                break
            if r["nudges"] >= nudges:
                r["end"] = "gave up"
                break
            r["nudges"] += 1
            s.send({"type": "user", "text": NUDGE})
    r["unfinished_steps"] = steps
    return r


def check(engine, image, task_dir, root, branch, env, timeout=600):
    """Run the task's check.sh on the session's branch, in the sandbox image, offline. (passed, output tail)."""
    if not branch:
        return False, "no branch: the session changed nothing"
    git(root, "checkout", "-q", branch)
    args = [engine, "run", "--rm", "--network=none", "--env=BENCH=/bench", f"--volume={task_dir}:/bench:ro",
            f"--volume={root}:/work:O", "--workdir=/work", image, "/bin/sh", "-c", 'sh "$BENCH/check.sh"',
            "bench-check"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, "the check timed out"
    return r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def diff_size(root, branch):
    if not branch:
        return {"files": 0, "added": 0, "removed": 0}
    out = git(root, "diff", "--numstat", "main", branch)
    rows = [ln.split("\t") for ln in out.splitlines() if ln]
    num = [(int(a) if a.isdigit() else 0, int(b) if b.isdigit() else 0) for a, b, _ in rows]
    return {"files": len(rows), "added": sum(a for a, _ in num), "removed": sum(b for _, b in num)}


def run_task(name, task_dir, opts, env, sandbox):
    with open(os.path.join(task_dir, "task.md"), encoding="utf-8") as f:
        text = f.read().strip()
    where = tempfile.mkdtemp(prefix=f"hoplon-bench-{name}-")
    try:
        root = make_repo(task_dir, where)
        args = [sys.executable, opts.hoplon, "--json", "-p", opts.profile] + (["-e", opts.endpoint] if opts.endpoint
                                                                             else [])
        t0 = time.monotonic()
        s = Session(args, root, env)
        try:
            r = drive(s, text, opts.nudges, opts.time_limit)
        finally:
            s.close()
        r["seconds"] = round(time.monotonic() - t0, 1)
        passed, out = check(*sandbox, task_dir, root, r["branch"], env)
        r.update(task=name, title=text.splitlines()[0].lstrip("# ").strip(), passed=passed, check_output=out,
                 diff=diff_size(root, r["branch"]))
        if not r["branch"] and s.stderr:
            r["stderr"] = "".join(s.stderr)[-800:]
        return r
    finally:
        if opts.keep:
            print(f"  kept {where}", file=sys.stderr)
        else:
            shutil.rmtree(where, ignore_errors=True)


def table(results):
    head = ("task", "check", "end", "items", "steps/item", "retries", "fresh", "requests", "tokens", "time", "diff")
    rows = [head]
    for r in results:
        steps = "/".join(str(n) for n in r["items"]) or "-"
        d = r["diff"]
        rows.append((r["task"], "pass" if r["passed"] else "FAIL", str(r["end"]), str(len(r["items"])), steps,
                     str(r["retries"]), str(r["fresh_contexts"]), str(r["requests"]), str(r["tokens"]),
                     f"{r['seconds']:.0f}s", f"{d['files']}f +{d['added']} -{d['removed']}"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(head))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows)


def compare(a, b):
    """Two result files side by side, task by task."""
    by = [{r["task"]: r for r in x["results"]} for x in (a, b)]
    lines = [f"before: {a.get('label', '?')}", f"after:  {b.get('label', '?')}", ""]
    for name in sorted(set(by[0]) | set(by[1])):
        x, y = by[0].get(name), by[1].get(name)
        if not (x and y):
            lines.append(f"{name}: only in {'before' if x else 'after'}")
            continue

        def f(r):
            return (f"{'pass' if r['passed'] else 'FAIL'}, {len(r['items'])} items, {sum(r['items'])} steps, "
                    f"{r['retries']} retries, {r['tokens']} tokens, {r['seconds']:.0f}s")
        mark = "" if x["passed"] == y["passed"] else ("  <- now passes" if y["passed"] else "  <- now FAILS")
        lines += [f"{name}:{mark}", f"  before: {f(x)}", f"  after:  {f(y)}"]
    for label, x in (("before", a), ("after", b)):
        rs = x["results"]
        lines.append(f"{label}: {sum(r['passed'] for r in rs)}/{len(rs)} passed, "
                     f"{sum(r['tokens'] for r in rs)} tokens, {sum(r['seconds'] for r in rs):.0f}s")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the tasks as work sessions")
    r.add_argument("-p", "--profile", required=True, help="a blessed profile with a \"sandbox\"")
    r.add_argument("-e", "--endpoint", help="the endpoint (default: the profile's)")
    r.add_argument("-t", "--task", action="append", help="run only this task (repeatable)")
    r.add_argument("-o", "--out", help="write the results here as JSON, for compare")
    r.add_argument("--label", help="what this run is, for compare (default: the profile, endpoint and hoplon commit)")
    r.add_argument("--nudges", type=int, default=3, help="times to tell the model to go on (default 3)")
    r.add_argument("--time-limit", type=int, default=3600, help="seconds per task before it is stopped (3600)")
    r.add_argument("--hoplon", default=HOPLON, help="the hoplon to run (default: this repository's)")
    r.add_argument("--tasks", default=TASKS, help="the tasks directory")
    r.add_argument("--keep", action="store_true", help="keep each task's repository, to look at the branch")
    c = sub.add_parser("compare", help="compare two result files")
    c.add_argument("before")
    c.add_argument("after")
    opts = ap.parse_args(argv)
    try:
        if opts.cmd == "compare":
            with open(opts.before) as f, open(opts.after) as g:
                print(compare(json.load(f), json.load(g)))
            return 0
        tasks = load_tasks(opts.tasks, opts.task)
        env = dict(os.environ)
        sandbox = sandbox_of(opts.profile, opts.endpoint, opts.hoplon, env)
        try:
            commit = git(os.path.dirname(os.path.abspath(opts.hoplon)), "rev-parse", "--short", "HEAD").strip()
        except (subprocess.CalledProcessError, OSError):
            commit = "?"
        label = opts.label or f"{opts.profile}{'/' + opts.endpoint if opts.endpoint else ''} at {commit}"
        print(f"{len(tasks)} task(s) with {label}; every one makes real model requests.", file=sys.stderr)
        results = []
        for name, d in tasks.items():
            print(f"- {name} ...", file=sys.stderr, flush=True)
            results.append(run_task(name, d, opts, env, sandbox))
            res = results[-1]
            print(f"  {'pass' if res['passed'] else 'FAIL'} ({res['end']}, {res['seconds']:.0f}s)", file=sys.stderr)
        print(table(results))
        if opts.out:
            with open(opts.out, "w") as f:
                json.dump({"label": label, "results": results}, f, indent=1)
        return 0 if all(r["passed"] for r in results) else 1
    except BenchError as e:
        print(f"bench: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
