"""bench/run.py: the benchmark runner drives a work session as an absent user would, then checks the branch."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bench"))
import run as B  # noqa: E402
from fakeapi import FakeAPI, call, reply  # noqa: E402
from test_hoplon import SCRIPT, bless_path, make_home, write_json  # noqa: E402
from test_sandbox_runner import FAKE_PODMAN, FakePodman  # noqa: E402

# The checks run as "podman run ... --volume=TASK:/bench:ro --volume=COPY:/work:O ... /bin/sh -c CMD": here,
# on the host, with BENCH pointing at the task. Anything else is a sandboxed run, as in the work tests.
CHECK_MODE = r'''
_bench = [a for a in sys.argv[1:] if a.startswith("--volume=") and a.endswith(":/bench:ro")]
if _bench:
    _task = _bench[0][len("--volume="):-len(":/bench:ro")]
    _root = [a for a in sys.argv[1:] if a.endswith(":/work:O")][0][len("--volume="):-len(":/work:O")]
    with open(os.environ["FAKE_PODMAN_LOG"], "a") as f:
        f.write(json.dumps(["check"] + sys.argv[1:]) + "\n")
    sys.exit(subprocess.run(["/bin/sh", os.path.join(_task, "check.sh")], cwd=_root,
                            env=dict(os.environ, BENCH=_task)).returncode)
'''


class TestBench(FakePodman):
    def setUp(self):
        super().setUp()
        head, body = FAKE_PODMAN.split("\n", 2)[:2], FAKE_PODMAN.split("\n", 2)[2]
        with open(os.path.join(self.bin, "podman"), "w") as f:
            f.write("\n".join(head) + "\n" + CHECK_MODE + body)
        self.tasks = self.path("tasks")
        t = os.path.join(self.tasks, "fix-a")
        os.makedirs(os.path.join(t, "repo"))
        with open(os.path.join(t, "repo", "a.txt"), "w") as f:
            f.write("hi\n")
        with open(os.path.join(t, "task.md"), "w") as f:
            f.write("# Fix a\n\nMake a.txt say fixed.\n")
        with open(os.path.join(t, "check.sh"), "w") as f:
            f.write('test "$(cat a.txt)" = fixed && test -f "$BENCH/task.md"\n')

    def bench_env(self, api):
        home, ep = make_home(self.tmp, api.url)
        bless_path(home, ep)
        bless_path(home, write_json(os.path.join(home, "profiles", "coder.json"),
                                    {"name": "coder", "endpoint": "test", "sandbox": {"image": "img:1"}}))
        return dict(os.environ, **self.env, XDG_CONFIG_HOME=os.path.dirname(home),
                    XDG_STATE_HOME=self.path("state"), HOME=self.tmp, GIT_CONFIG_GLOBAL=os.devnull,
                    GIT_CONFIG_NOSYSTEM="1", HTTPS_PROXY="http://127.0.0.1:9")

    def run_bench(self, api, *extra):
        out, err = io.StringIO(), io.StringIO()
        results = self.path("results.json")
        with mock.patch.dict(os.environ, self.bench_env(api), clear=True), redirect_stdout(out), redirect_stderr(err):
            code = B.main(["run", "-p", "coder", "--tasks", self.tasks, "--hoplon", SCRIPT, "-o", results,
                           "--time-limit", "120", *extra])
        with open(results) as f:
            return code, json.load(f), out.getvalue(), err.getvalue()

    def test_a_task_runs_to_a_branch_that_passes_its_check(self):
        turns = [reply("Let me plan."),         # a turn that ends early: the runner nudges
                 call("propose_work_plan", goal="fix a", items=["make a.txt say fixed"]),
                 call("write_file", path="a.txt", content="fixed\n", reason="r"),
                 call("finish_item", n=1, summary="a.txt says fixed"),
                 reply("Done.")]
        with FakeAPI(turns) as api:
            code, res, out, err = self.run_bench(api, "--label", "test run")
        r = res["results"][0]
        self.assertEqual(code, 0, (r, err))
        self.assertEqual(res["label"], "test run")
        self.assertEqual((r["task"], r["title"], r["passed"], r["end"]), ("fix-a", "Fix a", True, "done"))
        self.assertEqual((r["plans"], r["nudges"], r["items"], r["declined"]), (1, 1, [1], []))
        self.assertEqual(r["diff"], {"files": 1, "added": 1, "removed": 1})
        self.assertEqual(r["requests"], 5)
        self.assertIn("fix-a", out)
        self.assertIn("pass", out)
        self.assertTrue(any(c[0] == "check" and "--network=none" in c for c in self.podman_calls()))

    def test_a_session_that_changes_nothing_fails_and_gives_up_after_the_nudges(self):
        with FakeAPI([reply("I can't.")] * 3) as api:
            code, res, _, _ = self.run_bench(api, "--nudges", "2")
        r = res["results"][0]
        self.assertEqual((code, r["passed"], r["end"], r["nudges"]), (1, False, "gave up", 2))
        self.assertIn("no branch", r["check_output"])

    def test_whatever_else_asks_is_declined_and_counts_as_a_step(self):
        class Fake:
            def __init__(self, events):
                self.events, self.sent = list(events), []

            def next(self, deadline):
                return self.events.pop(0) if self.events else None

            def send(self, obj):
                self.sent.append(obj)
        s = Fake([{"type": "hello"}, {"type": "work", "state": "started"},
                  {"type": "proposal", "id": "p1", "kind": "work_plan", "digest": "d1"},
                  {"type": "proposal", "id": "p2", "kind": "skills", "digest": "d2"},
                  {"type": "proposal", "id": "p3", "kind": "write", "auto": True},
                  {"type": "work", "state": "context", "role": "review"},
                  {"type": "work", "state": "item"}, {"type": "generated", "tokens": 40},
                  {"type": "work", "state": "ended", "why": "done", "branch": "b", "commits": ["c 1. x"]},
                  {"type": "done", "reason": "reply"}])
        r = B.drive(s, "the task", 3, 60)
        self.assertEqual(s.sent[0], {"type": "work", "task": "the task"})
        self.assertEqual([m.get("decision") for m in s.sent[1:]], ["accept", "decline"])
        self.assertEqual((r["declined"], r["items"], r["retries"], r["tokens"], r["end"], r["branch"]),
                         (["skills"], [2], 1, 40, "done", "b"))
        s = Fake([{"type": "hello"}, {"type": "error", "code": "work_refused", "message": "not a git repository"}])
        self.assertEqual(B.drive(s, "t", 3, 60)["end"], "refused")

    def test_tasks_and_compare(self):
        with self.assertRaisesRegex(B.BenchError, "no task named 'nope'"):
            B.load_tasks(self.tasks, ["nope"])
        os.makedirs(os.path.join(self.tasks, "broken"))
        with self.assertRaisesRegex(B.BenchError, "broken: missing task.md, repo, check.sh"):
            B.load_tasks(self.tasks)
        self.assertEqual(sorted(B.load_tasks(B.TASKS)), ["parser-fix", "read-first", "refactor", "roguelike"])
        row = {"task": "t", "passed": True, "end": "done", "items": [3, 4], "retries": 0, "fresh_contexts": 1,
               "requests": 9, "tokens": 1200, "seconds": 61.0, "diff": {"files": 2, "added": 10, "removed": 3}}
        self.assertIn("t     pass   done  2      3/4", B.table([row]))
        text = B.compare({"label": "a", "results": [row]},
                         {"label": "b", "results": [dict(row, passed=False, tokens=900)]})
        self.assertIn("t:  <- now FAILS", text)
        self.assertIn("after:  FAIL, 2 items, 7 steps, 0 retries, 900 tokens, 61s", text)
        self.assertIn("before: 1/1 passed, 1200 tokens", text)


if __name__ == "__main__":
    unittest.main()
