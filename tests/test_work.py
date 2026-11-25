"""Tests for work sessions: the model plans, then edits, tests and commits on a git worktree of its own, and the user
reviews the branch. A fake podman runs sandboxed commands on the host (see test_sandbox_runner); git is real.
Run: python3 -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakeapi import FakeAPI, call, reply  # noqa: E402
from test_genai import G, Proc, bless_path, make_home, write_json  # noqa: E402
from test_sandbox_runner import FakePodman  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_ENV, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout


class WorkTest(FakePodman):
    def setUp(self):
        super().setUp()
        self.env.update(XDG_STATE_HOME=self.path("state"), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def repo(self, files=None):
        root = self.project(files or {"a.txt": "hi\n"})
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "start")
        return root

    def start(self, api, work=None, sandbox=None, context_limit=100000, planner_api=None):
        if os.path.exists(self.path("home")):
            subprocess.run(["rm", "-rf", self.path("home")], check=True)
        home, ep = make_home(self.tmp, api.url, context_limit=context_limit)
        bless_path(home, ep)
        if planner_api:
            bless_path(home, write_json(os.path.join(home, "endpoints", "big.json"),
                                        {"name": "big", "base_url": planner_api.url, "model": "big-model",
                                         "context_limit": 100000}))
        prof = {"name": "coder", "endpoint": "test", "coding": True, "sandbox": sandbox or {"image": "img:1"}}
        if work is not None:
            prof["work"] = work
        bless_path(home, write_json(os.path.join(home, "profiles", "coder.json"), prof))
        self.home = home
        return Proc(home, self.path("wd"), ["-p", "coder"], env=self.env)

    def audit(self):
        with open(os.path.join(self.home, "audit.log")) as f:
            return [json.loads(x) for x in f]

    def approve(self, g):
        p = g.until("proposal")
        self.assertEqual(p["kind"], "work_plan")
        g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
        return p

    def test_a_session_from_plan_to_branch(self):
        turns = [call("propose_work_plan", goal="fix a", items=["change hi to fixed", "add b"]),
                 call("edit_file", path="a.txt", old_text="hi", new_text="fixed", reason="r"),
                 call("run_in_sandbox", command="grep fixed a.txt", reason="check"),
                 call("finish_item", n=1, summary="a.txt says fixed"),
                 call("write_file", path="b.txt", content="new\n", reason="r"),
                 call("finish_item", n=2, summary="add b.txt"),
                 reply("Both done.")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api, {"test": "test -f a.txt"})
            g.until("hello")
            g.send({"type": "work", "task": "fix a"})
            started = g.until("work")
            self.assertEqual((started["state"], started["branch"], started["test"]),
                             ("started", "genai/work-1", "test -f a.txt"))
            self.approve(g)
            edit = g.until("proposal")
            self.assertEqual((edit["kind"], edit.get("auto"), edit.get("work")), ("edit", True, True))
            r = g.until("result")
            while "exit" not in r:
                r = g.until("result")
            self.assertEqual(r["exit"], 0)                            # the sandbox run sees the edit
            ended = g.until("work")
            while ended["state"] != "ended":
                ended = g.until("work")
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        self.assertEqual(ended["why"], "done")
        self.assertEqual(ended["branch"], "genai/work-1")
        self.assertEqual([c.split(" ", 1)[1] for c in ended["commits"]], ["1. a.txt says fixed", "2. add b.txt"])
        self.assertIn("git merge genai/work-1", ended["commands"])
        with open(os.path.join(root, "a.txt")) as f:
            self.assertEqual(f.read(), "hi\n")                       # your own files are untouched
        self.assertFalse(os.path.exists(os.path.join(root, "b.txt")))
        self.assertEqual(git(root, "show", "genai/work-1:a.txt"), "fixed\n")
        self.assertEqual(git(root, "show", "genai/work-1:b.txt"), "new\n")
        self.assertEqual(git(root, "rev-parse", "--abbrev-ref", "HEAD").strip(), "main")
        self.assertEqual(os.listdir(self.path("state", "genai", "work")), [])     # the copy is gone
        self.assertEqual(len(git(root, "worktree", "list").splitlines()), 1)
        mounts = [a for c in self.podman_calls() for a in c if a.endswith(":/work:O")]
        self.assertTrue(mounts and all(self.path("state") in m for m in mounts))  # the copy, as an overlay
        events = [e["event"] for e in self.audit()]
        for e in ("work_start", "work_plan", "work_test", "work_item", "work_end"):
            self.assertIn(e, events)
        tools = [t["function"]["name"] for t in api.requests[0]["body"]["tools"]]
        self.assertIn("finish_item", tools)
        self.assertNotIn("propose_command", tools)
        self.assertIn("# This is a work session", api.requests[0]["body"]["messages"][0]["content"])

    def test_finish_needs_a_passing_test_and_a_change(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("finish_item", n=1, summary="nothing yet"),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("finish_item", n=1, summary="b"),
                 reply("stuck")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api, {"test": "test -f c.txt"})
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        tools = [m["content"] for r in api.requests for m in r["body"]["messages"] if m["role"] == "tool"]
        self.assertTrue(any("not finished: the test command failed" in t for t in tools))
        self.assertFalse(any("committed as" in t for t in tools))

    def test_nothing_changed_is_not_finished(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]), call("finish_item", n=1, summary="s"), reply("?")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "work_end"})
            ended = g.until("work")
            g.close()
        self.assertIn("no files changed", api.requests[2]["body"]["messages"][-1]["content"])
        self.assertEqual((ended["state"], ended["branch"]), ("ended", None))      # no commits: the branch is gone
        self.assertNotIn("genai/work-1", git(self.path("wd"), "branch"))

    def test_the_step_budget_pauses_and_a_message_renews_it(self):
        reads = [call("read_file", path="a.txt", reason="r")] * 5
        turns = [call("propose_work_plan", goal="g", items=["one"])] + reads + [reply("ok")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api, {"steps_per_item": 2})
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            self.assertEqual(g.until("done")["reason"], "work_limit")
            self.assertEqual(len(api.requests), 5)          # the plan, item 1's two steps, the planner's two
            g.send({"type": "user", "text": "go on"})
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        self.assertIn("work_pause", [e["event"] for e in self.audit()])

    def test_steps_can_be_overridden_for_one_session(self):
        turns = [call("propose_work_plan", goal="g", items=["one"])] + [call("read_file", path="a.txt", reason="r")] * 2
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api, {"steps_per_item": 50})
            g.send({"type": "work", "task": "t", "steps_per_item": 1})
            self.assertEqual(g.until("work")["steps_per_item"], 1)
            self.approve(g)
            self.assertEqual(g.until("done")["reason"], "work_limit")
            g.close()

    def test_refused_with_uncommitted_changes_or_outside_git(self):
        root = self.repo()
        with open(os.path.join(root, "a.txt"), "a") as f:
            f.write("more\n")
        with FakeAPI([]) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            err = g.until("error")
            self.assertEqual(err["code"], "work_refused")
            self.assertIn("uncommitted changes", err["message"])
            g.close()
        subprocess.run(["rm", "-rf", os.path.join(root, ".git")], check=True)
        with FakeAPI([]) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.assertIn("needs a git repository", g.until("error")["message"])
            g.close()

    def test_git_is_never_written_and_hidden_files_ask(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path=".git", content="gitdir: /tmp/evil\n", reason="r"),
                 call("write_file", path=".env", content="X=1\n", reason="r"),
                 reply("ok")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            p = g.until("proposal")
            while p["kind"] != "write":
                p = g.until("proposal")
            self.assertEqual(p["path"], ".env")                 # .git never became a proposal
            self.assertIsNone(p.get("auto"))                    # a hidden file asks
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()
        tool = api.requests[2]["body"]["messages"][-1]["content"]
        self.assertIn("nothing under .git can be written", tool)

    def test_a_hook_in_the_repository_never_runs(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("finish_item", n=1, summary="b"), reply("done")]
        root = self.repo()
        marker = self.path("hook-ran")
        for hook in ("pre-commit", "post-checkout", "post-commit"):
            path = os.path.join(root, ".git", "hooks", hook)
            with open(path, "w") as f:
                f.write(f"#!/bin/sh\ntouch {marker}\n")
            os.chmod(path, 0o755)
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            ended = g.until("work")
            while ended["state"] != "ended":
                ended = g.until("work")
            g.close()
        self.assertEqual(len(ended["commits"]), 1)
        self.assertFalse(os.path.exists(marker))

    def test_exiting_mid_session_commits_the_work_to_the_branch(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"), reply("pausing")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.close()
        self.assertEqual(git(root, "show", "genai/work-1:b.txt"), "x\n")
        self.assertEqual(os.listdir(self.path("state", "genai", "work")), [])

    def test_each_item_starts_in_a_fresh_context_with_the_notes(self):
        turns = [call("propose_work_plan", goal="g", items=["make b", "make c"]),
                 call("write_file", path="b.txt", content="b\n", reason="r"),
                 call("finish_item", n=1, summary="Add b.txt with the letter b in it, as the first of several "
                      "files the task needs", notes="b.txt holds one letter; c.txt should match"),
                 call("write_file", path="c.txt", content="c\n", reason="r"),
                 call("finish_item", n=2, summary="add c.txt"),
                 reply("done")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "the task"})
            self.approve(g)
            ctx = g.until("work")
            while ctx["state"] != "context":
                ctx = g.until("work")
            self.assertEqual((ctx["role"], ctx["item"]), ("item", 1))
            g.until("done")
            g.close()
        msgs = [r["body"]["messages"] for r in api.requests]
        item1, item2 = msgs[1], msgs[3]
        self.assertEqual([m["role"] for m in item1], ["system", "user"])      # nothing of the planning
        self.assertIn("item 1 of 2: make b", item1[1]["content"])
        self.assertIn("the task", item1[1]["content"])
        self.assertEqual([m["role"] for m in item2], ["system", "user"])
        self.assertIn("item 2 of 2: make c", item2[1]["content"])
        self.assertIn("c.txt should match", item2[1]["content"])              # the handoff
        log = git(root, "log", "--format=%s%n%b", "-1", "genai/work-1~1")
        title = log.splitlines()[0]
        self.assertTrue(title.startswith("1. Add b.txt") and title.endswith("\u2026") and len(title) <= 67, title)
        self.assertIn("Plan item: make b", log)
        self.assertIn("c.txt should match", log)
        self.assertIn("work_context", [e["event"] for e in self.audit()])

    def test_a_stuck_item_goes_back_to_the_planner(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("read_file", path="b.txt", reason="polish"),
                 call("retry_item", n=1, hint="b.txt is written: just finish it"),
                 call("finish_item", n=1, summary="b"),
                 reply("done")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api, {"steps_per_item": 2})
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        review = api.requests[3]["body"]
        self.assertIn("used its budget of 2 model steps", review["messages"][-1]["content"])
        self.assertIn("b.txt", review["messages"][-1]["content"])           # what the attempt changed
        self.assertIn("propose_work_plan", json.dumps(review["messages"]))    # the planner's own context
        self.assertIn("retry_item", [t["function"]["name"] for t in review["tools"]])
        retry = api.requests[4]["body"]["messages"]
        self.assertEqual([m["role"] for m in retry], ["system", "user"])
        self.assertIn("just finish it", retry[1]["content"])
        self.assertNotIn("retry_item", [t["function"]["name"] for t in api.requests[4]["body"]["tools"]])
        events = [e["event"] for e in self.audit()]
        self.assertIn("work_retry", events)
        self.assertIn("work_item", events)

    def test_old_output_is_dropped_as_the_context_fills(self):
        big = "x" * 4000 + "\n"
        turns = ([call("propose_work_plan", goal="g", items=["one"])]
                 + [call("read_file", path="big.txt", reason="r")] * 6 + [reply("ok")])
        self.repo({"big.txt": big})
        with FakeAPI(turns) as api:
            g = self.start(api, context_limit=20000)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.close()
        last = api.requests[-1]["body"]["messages"]
        results = [m["content"] for m in last if m["role"] == "tool"]
        self.assertTrue(any("dropped to save room" in r for r in results))
        self.assertTrue(any(big.strip() in r for r in results))            # the latest are kept in full
        self.assertIn("work_clear", [e["event"] for e in self.audit()])

    def test_an_item_that_fills_the_context_goes_on_in_a_fresh_one(self):
        big = "x" * 4000 + "\n"
        turns = ([call("propose_work_plan", goal="g", items=["one"])]
                 + [call("read_file", path="big.txt", reason="r")] * 5 + [reply("NOTES: big.txt is all x"), reply("ok")])
        self.repo({"big.txt": big})
        with FakeAPI(turns) as api:
            g = self.start(api, context_limit=8000)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        asked = [r for r in api.requests if "nearly full" in json.dumps(r["body"]["messages"][-1:])]
        self.assertEqual(len(asked), 1)
        self.assertNotIn("tools", asked[0]["body"])                          # notes only, nothing run
        fresh = api.requests[-1]["body"]["messages"]
        self.assertEqual([m["role"] for m in fresh], ["system", "user"])
        self.assertIn("NOTES: big.txt is all x", fresh[1]["content"])


    def test_a_planner_endpoint_plans_and_reviews(self):
        plans = [call("propose_work_plan", goal="g", items=["one"]),
                 call("finish_item", n=1, summary="b is there"), reply("done")]
        works = [call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("read_file", path="b.txt", reason="polish")]
        self.repo()
        with FakeAPI(works) as api, FakeAPI(plans) as big:
            g = self.start(api, {"steps_per_item": 2, "planner": "big"}, planner_api=big)
            g.send({"type": "work", "task": "t"})
            self.assertEqual(g.until("work")["planner"], "big")
            self.approve(g)
            ended = g.until("work")
            while ended["state"] != "ended":
                ended = g.until("work")
            g.close()
        self.assertEqual(len(big.requests), 3)          # the plan; then the review, which finishes; then the summary
        self.assertEqual(len(api.requests), 2)          # item 1's two steps
        self.assertTrue(all(r["body"]["model"] == "big-model" for r in big.requests))
        self.assertIn("used its budget", big.requests[1]["body"]["messages"][-1]["content"])
        self.assertEqual(ended["why"], "done")
        start = next(e for e in self.audit() if e["event"] == "work_start")
        self.assertEqual(start["planner"]["model"], "big-model")

    def test_an_unknown_planner_endpoint_refuses_the_session(self):
        self.repo()
        with FakeAPI([]) as api:
            g = self.start(api, {"planner": "nope"})
            g.send({"type": "work", "task": "t"})
            err = g.until("error")
            g.close()
        self.assertEqual(err["code"], "work_refused")
        self.assertIn("planner endpoint", err["message"])
        self.assertEqual(git(self.path("wd"), "branch").split(), ["*", "main"])     # nothing was made


class TestWorkProfile(unittest.TestCase):
    def test_work_needs_a_sandbox_and_checks_its_fields(self):
        with self.assertRaisesRegex(G.GenaiError, "needs a 'sandbox'"):
            G.Profile({"name": "p", "work": {}}, "p.json")
        with self.assertRaisesRegex(G.GenaiError, "steps per item"):
            G.Profile({"name": "p", "sandbox": {"image": "i:1"}, "work": {"steps_per_item": 0}}, "p.json")
        with self.assertRaisesRegex(G.GenaiError, "unknown work fields"):
            G.Profile({"name": "p", "sandbox": {"image": "i:1"}, "work": {"steps": 3}}, "p.json")
        p = G.Profile({"name": "p", "sandbox": {"image": "i:1"}, "work": {"test": "make test"}}, "p.json")
        self.assertEqual(p.work, {"test": "make test", "steps_per_item": 20, "time_limit": 7200, "planner": None})
        self.assertIn("20 steps per plan item", p.summary()[0])
        with self.assertRaisesRegex(G.GenaiError, "endpoint's name"):
            G.Profile({"name": "p", "sandbox": {"image": "i:1"}, "work": {"planner": "../x"}}, "p.json")
        p = G.Profile({"name": "p", "sandbox": {"image": "i:1"}, "work": {"planner": "big"}}, "p.json")
        self.assertIn("plans with endpoint big, which also sees the project", p.summary()[0])


if __name__ == "__main__":
    unittest.main()
