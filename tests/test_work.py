"""Tests for work sessions: the model plans, then edits, tests and commits on a git worktree of its own, and the user
reviews the branch. A fake podman runs sandboxed commands on the host (see test_sandbox_runner); git is real.
Run: python3 -m unittest discover -s tests"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

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

    def start(self, api, work=None, sandbox=None, context_limit=100000, planner_api=None, coding=True):
        if os.path.exists(self.path("home")):
            subprocess.run(["rm", "-rf", self.path("home")], check=True)
        home, ep = make_home(self.tmp, api.url, context_limit=context_limit)
        bless_path(home, ep)
        if planner_api:
            bless_path(home, write_json(os.path.join(home, "endpoints", "big.json"),
                                        {"name": "big", "base_url": planner_api.url, "model": "big-model",
                                         "context_limit": 100000}))
        prof = {"name": "coder", "endpoint": "test", "coding": coding, "sandbox": sandbox or {"image": "img:1"}}
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
            g = self.start(api, {"steps_per_item": 2, "planner": {"endpoint": "big"}}, planner_api=big)
            g.send({"type": "work", "task": "t"})
            self.assertEqual(g.until("work")["planner"], {"endpoint": "big", "model": "big-model"})
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

    def test_a_planner_model_on_the_same_endpoint(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("finish_item", n=1, summary="b"), reply("done")]
        self.repo()
        models = [{"id": "fake-model"}, {"id": "big-model", "max_model_len": 50000}]
        with FakeAPI(turns, models=models) as api:
            g = self.start(api, {"planner": {"model": "big-model"}})
            g.send({"type": "work", "task": "t"})
            self.assertEqual(g.until("work")["planner"], {"endpoint": "test", "model": "big-model"})
            self.approve(g)
            g.until("done")
            g.close()
        used = [r["body"]["model"] for r in api.requests if r.get("body")]
        self.assertEqual(used, ["big-model", "fake-model", "fake-model", "fake-model"])   # the plan, then the item

    def test_a_planner_model_the_server_doesnt_list_refuses_the_session(self):
        self.repo()
        with FakeAPI([]) as api:
            g = self.start(api, {"planner": {"model": "typo-model"}})
            g.send({"type": "work", "task": "t"})
            err = g.until("error")
            g.close()
        self.assertEqual(err["code"], "work_refused")
        self.assertIn("typo-model", err["message"])

    def test_an_unknown_planner_endpoint_refuses_the_session(self):
        self.repo()
        with FakeAPI([]) as api:
            g = self.start(api, {"planner": {"endpoint": "nope"}})
            g.send({"type": "work", "task": "t"})
            err = g.until("error")
            g.close()
        self.assertEqual(err["code"], "work_refused")
        self.assertIn("planner endpoint", err["message"])
        self.assertEqual(git(self.path("wd"), "branch").split(), ["*", "main"])     # nothing was made

    # -- what the audit before 1.1.0 found

    def ended(self, g):
        ev = g.until("work")
        while ev["state"] != "ended":
            ev = g.until("work")
        return ev

    def tool_replies(self, api):
        return [m["content"] for r in api.requests for m in r["body"]["messages"] if m["role"] == "tool"]

    def test_a_git_failure_at_the_end_keeps_the_copy_and_the_branch(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"), reply("pausing")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            started = g.until("work")
            self.approve(g)
            g.until("done")
            gitdir = os.path.join(root, ".git", "worktrees", os.path.basename(started["path"]))
            open(os.path.join(gitdir, "index.lock"), "w").close()       # so git add fails
            g.send({"type": "work_end"})
            ended = self.ended(g)
            g.close()
        self.assertTrue(ended["problems"] and "could not be committed" in ended["problems"][0])
        self.assertEqual((ended["branch"], ended["path"], ended["removed"]), ("genai/work-1", started["path"], False))
        self.assertTrue(os.path.exists(os.path.join(started["path"], "b.txt")))     # the work is still there
        self.assertIn("genai/work-1", git(root, "branch"))

    def test_a_filter_driver_in_the_repository_config_never_runs(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("finish_item", n=1, summary="b"), reply("done")]
        root = self.repo({"a.txt": "hi\n", ".gitattributes": "*.txt filter=evil\n"})
        marker = self.path("filter-ran")
        for key in ("clean", "smudge"):
            git(root, "config", f"filter.evil.{key}", f"touch {marker}; cat")
        git(root, "config", "filter.evil.required", "true")
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            ended = self.ended(g)
            g.close()
        self.assertEqual(len(ended["commits"]), 1)
        self.assertFalse(os.path.exists(marker))

    def test_writes_ask_until_the_plan_is_approved(self):
        turns = [call("write_file", path="b.txt", content="x\n", reason="early"), reply("ok")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            p = g.until("proposal")
            self.assertEqual(p["kind"], "write")
            self.assertIsNone(p.get("auto"))                    # no plan yet: it asks
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()

    def test_a_pyvenv_cfg_asks_in_a_work_session(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="conf/pyvenv.cfg", content="home = /usr\n", reason="r"), reply("ok")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            p = g.until("proposal")
            self.assertIsNone(p.get("auto"))
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()

    def test_the_network_stays_off_during_a_session(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]), reply("pausing")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "network", "on": True})
            err = g.until("error")
            self.assertFalse(g.until("network")["runner"]["network"])
            g.close()
        self.assertIn("network stays off during a work session", err["message"])

    def test_a_tool_the_session_doesnt_offer_is_refused(self):
        turns = [call("propose_command", command="rm -rf ~", reason="r"), reply("ok")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            g.until("done")
            g.close()
        self.assertIn("propose_command is not available in a work session", self.tool_replies(api)[0])
        self.assertNotIn("command", [e.get("kind") for e in g.events if e["type"] == "proposal"])

    def test_coding_mode_goes_back_to_off_after_a_refusal_and_after_the_session(self):
        root = self.repo()
        with open(os.path.join(root, "a.txt"), "a") as f:
            f.write("dirty\n")
        with FakeAPI([reply("ok")]) as api:
            g = self.start(api, coding=False)
            self.assertFalse(g.until("hello")["coding"])
            g.send({"type": "work", "task": "t"})
            g.until("error")
            g.close()
        coding = [e["on"] for e in g.events if e["type"] == "coding"]
        self.assertEqual(coding, [True, False])
        git(root, "checkout", "a.txt")
        with FakeAPI([call("propose_work_plan", goal="g", items=["one"]), reply("pausing")]) as api:
            g = self.start(api, coding=False)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "work_end"})
            self.ended(g)
            g.close()
        self.assertEqual([e["on"] for e in g.events if e["type"] == "coding"], [True, False])

    def test_approved_files_are_set_aside_for_the_session(self):
        turns = [call("write_file", path="a.txt", content="mine\n", reason="r"), reply("ok"),
                 call("propose_work_plan", goal="g", items=["one"]), reply("pausing")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "edits", "mode": "approved-files"})
            g.until("edits")
            g.send({"type": "user", "text": "write a"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            git(root, "commit", "-qam", "mine")
            g.send({"type": "edits"})
            before = g.until("edits")["files"]
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "edits"})
            during = g.until("edits")["files"]
            g.send({"type": "work_end"})
            self.ended(g)
            g.send({"type": "edits"})
            after = g.until("edits")["files"]
            g.close()
        self.assertEqual((before, during, after), (["a.txt"], [], ["a.txt"]))

    def test_a_planner_elsewhere_starts_from_the_task_alone(self):
        plans = [call("propose_work_plan", goal="g", items=["one"]), reply("pausing")]
        works = [reply("SECRET-CHAT-ANSWER"), reply("back again")]
        self.repo()
        with FakeAPI(works) as api, FakeAPI(plans) as big:
            g = self.start(api, {"planner": {"endpoint": "big"}}, planner_api=big)
            g.send({"type": "user", "text": "PRIVATE-EARLIER-TEXT"})
            g.until("done")
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "work_end"})
            self.ended(g)
            g.send({"type": "user", "text": "and now?"})
            g.until("done")
            g.close()
        self.assertNotIn("PRIVATE-EARLIER-TEXT", json.dumps([r["body"] for r in big.requests]))
        self.assertIn("PRIVATE-EARLIER-TEXT", json.dumps(api.requests[-1]["body"]["messages"]))   # back after

    def test_a_secret_read_names_where_it_would_go(self):
        plans = [call("read_file", path="notes.txt", reason="r"), reply("ok")]
        self.repo({"notes.txt": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"})
        with FakeAPI([]) as api, FakeAPI(plans) as big:
            g = self.start(api, {"planner": {"endpoint": "big"}}, planner_api=big)
            g.send({"type": "work", "task": "t"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()
        self.assertEqual((p["secret"], p["to"]), (True, big.url))

    def test_clear_drops_the_planners_waiting_context_too(self):
        turns = [call("propose_work_plan", goal="PLANNED-GOAL", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"), reply("pausing"),
                 call("read_file", path="b.txt", reason="r"), call("read_file", path="b.txt", reason="r"),
                 reply("what now?")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api, {"steps_per_item": 2})
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "clear"})
            g.until("cleared")
            g.send({"type": "user", "text": "go on"})
            g.until("done")
            g.close()
        review = api.requests[-1]["body"]["messages"]
        self.assertIn("used its budget", review[-1]["content"])
        self.assertEqual([m["role"] for m in review], ["system", "user"])     # the planner's old context is gone

    def test_finish_with_every_item_done_is_refused(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("finish_item", n=1, summary="b"), call("finish_item", n=0, summary="again"), reply("done")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            self.assertEqual(self.ended(g)["why"], "done")
            g.close()
        self.assertIn("every item is done", self.tool_replies(api)[-1])

    def test_plan_items_are_one_line_and_odd_characters_dont_break_a_commit(self):
        turns = [call("propose_work_plan", goal="g", items=["one\n2. fake item"]),
                 call("propose_work_plan", goal="g\x00", items=["one\x07"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("finish_item", n=1, summary="b\x00\ud800c", notes="n\x00"), reply("done")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            p = self.approve(g)
            ended = self.ended(g)
            g.close()
        self.assertIn("each item must be one line", self.tool_replies(api)[0])
        self.assertEqual((p["goal"], p["items"]), ("g", ["one"]))
        self.assertEqual(len(ended["commits"]), 1)
        self.assertIn("b?c", git(root, "log", "-1", "--format=%s", "genai/work-1"))

    def test_a_new_plan_drops_the_old_plans_reports(self):
        turns = [call("propose_work_plan", goal="g", items=["a", "b"]),
                 call("write_file", path="a.txt", content="a\n", reason="r"),
                 call("finish_item", n=1, summary="a", notes="OLD-NOTE"),
                 call("propose_work_plan", goal="g2", items=["c"]), reply("pausing")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            p = g.until("proposal")
            while p["kind"] != "work_plan":
                p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            g.close()
        brief = api.requests[-1]["body"]["messages"][1]["content"]
        self.assertIn("item 1 of 1: c", brief)
        self.assertNotIn("OLD-NOTE", brief)

    # -- stopping a session

    def test_stop_ends_the_turn_and_the_session_pauses(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("run_in_sandbox", command="sleep 30", reason="r"), reply("never")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            p = g.until("proposal")
            while p["kind"] != "sandbox":
                p = g.until("proposal")
            t = time.monotonic()
            g.send({"type": "stop"})
            self.assertEqual(g.until("done")["reason"], "stopped")
            self.assertLess(time.monotonic() - t, 20)
            g.send({"type": "work"})            # a bad message: genai is listening again
            g.until("error")
            g.close()
        self.assertEqual(len(api.requests), 2)      # never asked the model again
        self.assertIn("halt", [e["event"] for e in self.audit()])

    def test_work_end_mid_turn_stops_and_ends_the_session(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("finish_item", n=1, summary="b"), reply("done")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api, {"test": "sleep 30"})
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            p = g.until("proposal")
            while p["kind"] != "finish":
                p = g.until("proposal")
            g.send({"type": "work_end"})            # while the finish test runs
            self.assertEqual(g.until("done")["reason"], "stopped")
            ended = self.ended(g)
            g.close()
        self.assertEqual((ended["why"], ended["branch"]), ("ended", "genai/work-1"))
        self.assertEqual(git(root, "show", "genai/work-1:b.txt"), "x\n")    # committed as unfinished
        self.assertEqual(os.listdir(self.path("state", "genai", "work")), [])

    def test_sigterm_mid_turn_commits_the_work_and_removes_the_copy(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 call("run_in_sandbox", command="sleep 30", reason="r"), reply("never")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            p = g.until("proposal")
            while p["kind"] != "sandbox":
                p = g.until("proposal")
            g.p.send_signal(signal.SIGTERM)
            rc = g.close()
        self.assertEqual(rc, 128 + signal.SIGTERM)
        self.assertEqual(git(root, "show", "genai/work-1:b.txt"), "x\n")
        self.assertEqual(os.listdir(self.path("state", "genai", "work")), [])
        self.assertEqual(len(git(root, "worktree", "list").splitlines()), 1)


class TestWorkParts(unittest.TestCase):
    def test_finishing_an_item_doesnt_renew_the_time_without_the_user(self):
        w = G.Work(None, "t", 5, 60, None, None)
        w.since -= 100
        w.used = 3
        w.fresh()
        self.assertEqual(w.used, 0)
        self.assertIn("min without you", w.limit())
        w.fresh(renew=True)
        self.assertIsNone(w.limit())

    def test_plain_text_and_git_paths(self):
        self.assertEqual(G.plain_text("a\x00b\x1b[1m\ud800\n\tc"), "ab[1m?\n\tc")
        self.assertEqual(G.git_line("/p/ends in space \n"), "/p/ends in space ")

    def test_git_refuses_a_nul_without_crashing(self):
        with self.assertRaisesRegex(G.GenaiError, "could not run"):
            G.run_git(["log", "a\x00b"], os.getcwd())

    def test_a_directory_with_nothing_committed_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            git(root, "init", "-q")
            with open(os.path.join(root, "a"), "w") as f:
                f.write("a")
            git(root, "add", "a")
            git(root, "commit", "-qm", "a")
            os.makedirs(os.path.join(root, "new"))
            with self.assertRaisesRegex(G.GenaiError, "nothing in this directory is committed"):
                G.Worktree.create(os.path.join(root, "new"))

    def test_a_pyvenv_cfg_doesnt_hide_a_secret_from_the_scan(self):
        with tempfile.TemporaryDirectory() as root:
            for rel, text in {"conf/pyvenv.cfg": "home = /usr\n", "conf/keys.txt": "password = hunter2hunter2\n",
                              "conf/lib/pkg.pem": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n"}.items():
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w") as f:
                    f.write(text)
            box = G.Sandbox(G.parse_sandbox({"image": "i:1"}, "p.json"), root, print)
            self.assertEqual(box.masks(), (["conf/keys.txt"], []))

    def test_ctrl_c_while_starting_work_in_the_repl_doesnt_exit(self):
        r = G.Repl.__new__(G.Repl)
        r.e = mock.Mock(work=None)
        r.e.start_work.side_effect = KeyboardInterrupt
        with mock.patch("builtins.print"):
            r.do_work("a task")
        r.e.start_work.assert_called_once()


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
        box = {"image": "i:1"}
        for bad in ("big", {}, {"endpoint": "../x"}, {"model": "a b"}, {"model": "m", "key": "k"}):
            with self.subTest(planner=bad), self.assertRaisesRegex(G.GenaiError, "planner"):
                G.Profile({"name": "p", "sandbox": box, "work": {"planner": bad}}, "p.json")
        p = G.Profile({"name": "p", "sandbox": box, "work": {"planner": {"endpoint": "big"}}}, "p.json")
        self.assertIn("plans with endpoint big, which also sees the project", p.summary()[0])
        p = G.Profile({"name": "p", "sandbox": box, "work": {"planner": {"model": "m2"}}}, "p.json")
        self.assertIn("plans with model m2, on the same endpoint", p.summary()[0])


if __name__ == "__main__":
    unittest.main()
