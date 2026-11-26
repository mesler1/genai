"""Tests for work sessions: the model plans, then edits, tests and commits in a copy of its own, and the user
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
from fakeapi import FakeAPI, call, calls, reply, slow  # noqa: E402
from test_genai import G, SCRIPT, Proc, bless_path, make_home, write_json  # noqa: E402
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

    def start(self, api, work=None, sandbox=None, context_limit=100000, planner_api=None, coding=True,
              session=None, params=None, planner_model="big-model"):
        if os.path.exists(self.path("home")):
            subprocess.run(["rm", "-rf", self.path("home")], check=True)
        home, ep = make_home(self.tmp, api.url, context_limit=context_limit)
        bless_path(home, ep)
        if planner_api:
            bless_path(home, write_json(os.path.join(home, "endpoints", "big.json"),
                                        {"name": "big", "base_url": planner_api.url, "model": planner_model,
                                         "context_limit": 100000}))
        prof = {"name": "coder", "endpoint": "test", "coding": coding, "sandbox": sandbox or {"image": "img:1"}}
        if work is not None:
            prof["work"] = work
        if params:
            prof["params"] = params
        bless_path(home, write_json(os.path.join(home, "profiles", "coder.json"), prof))
        self.home = home
        return Proc(home, self.path("wd"), ["-p", "coder"] + (["-S", session] if session else []), env=self.env)

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

    def test_a_git_failure_at_the_end_keeps_the_copy(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"), reply("pausing")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            started = g.until("work")
            self.approve(g)
            g.until("done")
            open(os.path.join(os.path.dirname(started["path"]), "git", "index.lock"), "w").close()  # git add fails
            g.send({"type": "work_end"})
            ended = self.ended(g)
            g.close()
        self.assertTrue(ended["problems"] and "could not be committed" in ended["problems"][0])
        self.assertEqual((ended["branch"], ended["path"], ended["removed"]), (None, started["path"], False))
        self.assertTrue(os.path.exists(os.path.join(started["path"], "b.txt")))     # the work is still there
        self.assertNotIn("genai/work", git(root, "branch"))                       # nothing was committed

    def test_the_repositorys_own_filter_drivers_never_run_in_the_copy_but_global_ones_do(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 *[call("write_file", path=f"new.{ext}", content="x\n", reason="r") for ext in ("txt", "e", "o", "g")],
                 call("finish_item", n=1, summary="b"), reply("done")]
        attrs = "*.txt filter=evil\n*.e filter=x=y\n*.o filter=ob\n*.g filter=glob\n"
        root = self.repo({"a.txt": "hi\n", "f.e": "e\n", "f.o": "o\n", "f.g": "g\n", ".gitattributes": attrs})

        def driver(name):
            mark = self.path(f"ran-{name.replace('=', '_')}")
            return mark, f'[filter "{name}"]\n\tclean = "pwd >> {mark}; cat"\n\tsmudge = "pwd >> {mark}; cat"\n'
        marks = {}
        for name in ("evil", "x=y"):        # in the repository's own config
            marks[name], text = driver(name)
            with open(os.path.join(root, ".git", "config"), "a") as f:
                f.write(text)
        marks["ob"], text = driver("ob")    # included only on a work branch, so only the copy would see it
        with open(self.path("ob.cfg"), "w") as f:
            f.write(text)
        git(root, "config", "includeIf.onbranch:genai/**.path", self.path("ob.cfg"))
        with FakeAPI(turns) as api:
            g = self.start(api)
            marks["glob"], text = driver("glob")    # your global config: XDG_CONFIG_HOME/git/config
            os.makedirs(os.path.join(os.path.dirname(self.home), "git"), exist_ok=True)
            with open(os.path.join(os.path.dirname(self.home), "git", "config"), "w") as f:
                f.write(text)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            ended = self.ended(g)
            g.close()

        def where(name):
            if not os.path.exists(marks[name]):
                return []
            with open(marks[name]) as f:
                return f.read().split()
        self.assertEqual(len(ended["commits"]), 1)
        for name in ("evil", "x=y", "ob"):
            self.assertFalse([d for d in where(name) if "genai/work" in d], name)   # never in the copy
        self.assertTrue([d for d in where("glob") if "genai/work" in d])          # yours runs there too

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


    # -- what the second audit found

    def wait_requests(self, api, n):
        deadline = time.monotonic() + 20
        while len(api.requests) < n:
            self.assertLess(time.monotonic(), deadline, "the request never came")
            time.sleep(0.05)

    def test_approved_files_dont_skip_the_work_sessions_rules(self):
        turns = [call("edit_file", path="a.txt", old_text="hi", new_text="one", reason="r"),
                 call("edit_file", path="a.txt", old_text="one", new_text="two", reason="r"), reply("ok")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "edits", "mode": "approved-files"})
            g.until("edits")
            g.send({"type": "work", "task": "t"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            p = g.until("proposal")
            self.assertIsNone(p.get("auto"))            # no plan yet: it asks, approved file or not
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()

    def test_edits_reset_during_a_session_isnt_undone_when_it_ends(self):
        turns = [call("edit_file", path="a.txt", old_text="hi", new_text="one", reason="r"), reply("ok"),
                 call("propose_work_plan", goal="g", items=["one"]), reply("pausing"),
                 call("edit_file", path="a.txt", old_text="one", new_text="two", reason="r"), reply("ok")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "edits", "mode": "approved-files"})
            g.until("edits")
            g.send({"type": "user", "text": "edit"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            git(self.path("wd"), "commit", "-qam", "one")
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "edits", "reset": True})
            self.assertEqual(g.until("edits")["files"], [])
            g.send({"type": "work_end"})
            self.ended(g)
            self.assertEqual(g.until("edits")["files"], [])
            g.send({"type": "user", "text": "edit again"})
            p = g.until("proposal")
            self.assertIsNone(p.get("auto"))            # forgotten: it asks
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()

    def test_anything_under_a_pyvenv_cfg_directory_asks(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="conf/pyvenv.cfg/x.txt", content="x\n", reason="r"), reply("ok")]
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

    def test_the_saved_session_is_the_one_from_before_the_work_session(self):
        plans = [call("propose_work_plan", goal="g", items=["one"])]
        works = [reply("EARLIER-ANSWER"), reply("pausing")]
        self.repo()
        with FakeAPI(works) as api, FakeAPI(plans) as big:
            g = self.start(api, {"planner": {"endpoint": "big"}}, planner_api=big, coding=False, session="named")
            g.send({"type": "user", "text": "EARLIER-TEXT"})
            g.until("done")
            g.send({"type": "work", "task": "WORK-TASK"})
            self.approve(g)
            g.until("done")
            path = os.path.join(self.home, "sessions", "named.json")
            with open(path) as f:
                during = json.load(f)
            g.send({"type": "work_end"})
            self.ended(g)
            with open(path) as f:
                after = json.load(f)
            g.close()
        for saved in (during, after):
            self.assertFalse(saved["coding"])
            self.assertIn("EARLIER-TEXT", json.dumps(saved["messages"]))
            self.assertNotIn("WORK-TASK", json.dumps(saved["messages"]))

    def test_a_hidden_file_read_names_the_planner_it_would_go_to(self):
        plans = [call("read_file", path=".notes", reason="r"), reply("ok")]
        self.repo({"a.txt": "hi\n", ".notes": "plain\n"})
        with FakeAPI([]) as api, FakeAPI(plans) as big:
            g = self.start(api, {"planner": {"endpoint": "big"}}, planner_api=big)
            g.send({"type": "work", "task": "t"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()
        self.assertEqual(p.get("to"), big.url)

    def test_an_item_that_replans_doesnt_hand_its_conversation_to_the_planner(self):
        plans = [call("propose_work_plan", goal="g", items=["one"]), reply("reviewed")]
        works = [call("propose_work_plan", goal="g2", items=["two"]),
                 call("read_file", path="a.txt", reason="r"), call("read_file", path="a.txt", reason="r")]
        self.repo()
        with FakeAPI(works) as api, FakeAPI(plans) as big:
            g = self.start(api, {"steps_per_item": 2, "planner": {"endpoint": "big"}}, planner_api=big)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            self.approve(g)                 # the item's new plan
            g.until("done")
            g.close()
        review = json.dumps(big.requests[-1]["body"]["messages"])
        self.assertIn("used its budget", review)
        self.assertIn("replaced the plan", review)
        self.assertNotIn("This is a fresh context", review)        # the item's own brief stays with the item

    def test_attachments_queued_before_a_planner_elsewhere_wait_for_the_conversation(self):
        plans = [reply("which way?"), reply("ok")]
        works = [reply("got it")]
        self.repo()
        with FakeAPI(works) as api, FakeAPI(plans) as big:
            g = self.start(api, {"planner": {"endpoint": "big"}}, planner_api=big)
            g.send({"type": "attach", "label": "notes", "text": "ATTACHED-TEXT"})
            g.until("attached")
            g.send({"type": "work", "task": "t"})
            g.until("done")
            g.send({"type": "user", "text": "go"})
            g.until("done")
            g.send({"type": "work_end"})
            self.ended(g)
            g.send({"type": "user", "text": "and now"})
            g.until("done")
            g.close()
        self.assertNotIn("ATTACHED-TEXT", json.dumps([r.get("body") for r in big.requests]))
        self.assertIn("ATTACHED-TEXT", json.dumps(api.requests[-1]["body"]["messages"]))

    def test_a_file_git_ignores_keeps_the_copy(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="gen.txt", content="g\n", reason="r"), reply("pausing")]
        self.repo({"a.txt": "hi\n", ".gitignore": "gen.txt\n"})
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "work_end"})
            ended = self.ended(g)
            g.close()
        self.assertFalse(ended["removed"])
        self.assertIn("1 file the model wrote is not in its commits (git ignores it", ended["problems"][0])
        self.assertTrue(os.path.exists(os.path.join(ended["path"], "gen.txt")))

    def test_a_stop_while_the_model_writes_tool_calls_ends_the_turn(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 slow(call("write_file", path="b.txt", content="x\n", reason="r"), 3), reply("never")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            self.wait_requests(api, 2)
            time.sleep(0.5)
            g.send({"type": "stop"})
            self.assertEqual(g.until("done")["reason"], "stopped")
            g.close()
        self.assertEqual(len(api.requests), 2)
        self.assertFalse([e for e in g.events if e["type"] == "proposal" and e.get("kind") == "write"])

    def test_a_stop_while_the_model_writes_rollover_notes_pauses_the_session(self):
        big = "x" * 4000 + "\n"
        turns = ([call("propose_work_plan", goal="g", items=["one"])]
                 + [call("read_file", path="big.txt", reason="r")] * 5 + [slow(reply("NOTES: slow"), 3)])
        self.repo({"big.txt": big})
        with FakeAPI(turns) as api:
            g = self.start(api, context_limit=8000)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            self.wait_requests(api, 7)
            time.sleep(0.5)
            g.send({"type": "stop"})
            self.assertEqual(g.until("done")["reason"], "stopped")
            g.send({"type": "work"})            # still running, and listening
            g.until("error")
            g.close()
        self.assertIn("nearly full", json.dumps(api.requests[-1]["body"]["messages"][-1:]))

    def test_what_comes_after_finish_in_the_same_reply_is_not_run(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="x\n", reason="r"),
                 calls(("finish_item", {"n": 1, "summary": "b"}),
                       ("write_file", {"path": "late.txt", "content": "late\n", "reason": "r"})),
                 reply("done")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            ended = self.ended(g)
            g.close()
        self.assertEqual((ended["why"], ended["unfinished"]), ("done", None))
        self.assertNotIn("late.txt", git(root, "ls-tree", "-r", "--name-only", "genai/work-1"))
        skipped = [e for e in g.events if e["type"] == "result" and e["status"] == "skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertTrue(any("came after finish_item" in t for t in self.tool_replies(api)))

    def test_plan_items_cant_hold_a_unicode_line_separator(self):
        turns = [call("propose_work_plan", goal="g", items=["one\u2028two"]), reply("ok")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            g.until("done")
            g.close()
        self.assertIn("each item must be one line", self.tool_replies(api)[0])

    def test_a_planner_on_the_same_endpoint_keeps_the_profiles_params(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]), reply("pausing")]
        self.repo()
        models = [{"id": "fake-model"}, {"id": "big-model"}]
        with FakeAPI(turns, models=models) as api:
            g = self.start(api, {"planner": {"model": "big-model"}}, params={"temperature": 0.25})
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.close()
        first = next(r["body"] for r in api.requests if r.get("body"))
        self.assertEqual((first["model"], first.get("temperature")), ("big-model", 0.25))

    def test_a_planner_endpoint_set_to_auto_uses_the_model_it_lists(self):
        plans = [reply("planning")]
        self.repo()
        with FakeAPI([]) as api, FakeAPI(plans, models=[{"id": "listed-model"}]) as big:
            g = self.start(api, {"planner": {"endpoint": "big"}}, planner_api=big, planner_model="auto")
            g.send({"type": "work", "task": "t"})
            self.assertEqual(g.until("work")["planner"]["model"], "listed-model")
            g.until("done")
            g.close()
        self.assertEqual(big.requests[-1]["body"]["model"], "listed-model")

    def test_a_planner_model_that_cant_be_checked_refuses_the_session(self):
        self.repo()
        with FakeAPI([], models_status=500) as api:
            g = self.start(api, {"planner": {"model": "big-model"}})
            g.send({"type": "work", "task": "t"})
            err = g.until("error")
            g.close()
        self.assertEqual(err["code"], "work_refused")
        self.assertIn("could not check", err["message"])


    # -- the copy

    def test_the_copy_holds_your_last_commit_and_nothing_else(self):
        turns = [reply("looking")]
        root = self.repo({"old.txt": "password = hunter2hunter2\n"})
        old = git(root, "rev-parse", "HEAD").strip()
        git(root, "rm", "-q", "old.txt")
        with open(os.path.join(root, "a.txt"), "w") as f:
            f.write("hi\n")
        git(root, "add", "a.txt")
        git(root, "commit", "-qm", "two")
        git(root, "branch", "other", old)
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            started = g.until("work")
            g.until("done")
            gitdir = os.path.join(os.path.dirname(started["path"]), "git")
            self.assertEqual(git(root, "--git-dir", gitdir, "rev-list", "--all").split(),
                             [git(root, "rev-parse", "HEAD").strip()])
            self.assertNotEqual(subprocess.run(["git", "--git-dir", gitdir, "cat-file", "-e", old],
                                               capture_output=True).returncode, 0)
            local = git(root, "--git-dir", gitdir, "config", "--local", "--list", "--name-only").split()
            self.assertFalse([k for k in local if not k.startswith("core.")])   # no remote, no filters: none of yours
            self.assertNotIn("genai/work", git(root, "branch"))     # nothing is on a branch until an item is done
            g.close()

    def test_a_branch_moved_outside_the_session_is_not_overwritten(self):
        turns = [call("propose_work_plan", goal="g", items=["one", "two"]),
                 call("write_file", path="b.txt", content="b\n", reason="r"),
                 call("finish_item", n=1, summary="b"),
                 call("write_file", path="c.txt", content="c\n", reason="r"), reply("pausing")]
        root = self.repo()
        base = git(root, "rev-parse", "HEAD").strip()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            self.assertEqual(git(root, "show", "genai/work-1:b.txt"), "b\n")     # on the branch already
            git(root, "update-ref", "refs/heads/genai/work-1", base)
            g.send({"type": "work_end"})
            ended = self.ended(g)
            g.close()
        self.assertEqual(git(root, "rev-parse", "genai/work-1").strip(), base)
        self.assertIn("was moved or deleted outside the session", ended["problems"][0])
        self.assertFalse(ended["removed"])
        self.assertTrue(os.path.exists(os.path.join(ended["path"], "c.txt")))

    def test_a_branch_name_taken_meanwhile_moves_to_the_next(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="b.txt", content="b\n", reason="r"), reply("pausing")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.assertEqual(g.until("work")["branch"], "genai/work-1")
            self.approve(g)
            g.until("done")
            git(root, "branch", "genai/work-1")
            g.send({"type": "work_end"})
            ended = self.ended(g)
            g.close()
        self.assertEqual(ended["branch"], "genai/work-2")
        self.assertEqual(git(root, "show", "genai/work-2:b.txt"), "b\n")
        self.assertEqual(git(root, "rev-parse", "genai/work-1").strip(), git(root, "rev-parse", "main").strip())


    # -- what the third audit found

    def test_the_sandbox_mounts_the_copys_files_but_not_its_git_directory(self):
        turns = [reply("looking")]
        self.repo({"a.txt": "hi\n", "service.key": "opaque\n"})
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            started = g.until("work")
            g.until("done")
            tree = started["path"]
            self.assertFalse(os.path.lexists(os.path.join(tree, ".git")))     # not even a pointer to it
            self.assertTrue(os.path.isdir(os.path.join(os.path.dirname(tree), "git")))   # beside the files
            g.close()

    def test_ending_with_a_problem_still_puts_the_commits_on_the_branch(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="gen.txt", content="g\n", reason="r"),
                 call("write_file", path="b.txt", content="b\n", reason="r"), reply("pausing")]
        root = self.repo({"a.txt": "hi\n", ".gitignore": "gen.txt\n"})
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "work_end"})
            ended = self.ended(g)
            g.close()
        self.assertEqual((ended["branch"], ended["removed"]), ("genai/work-1", False))    # the copy has gen.txt
        self.assertEqual(git(root, "show", "genai/work-1:b.txt"), "b\n")
        self.assertTrue(ended["commands"])

    def test_a_file_written_inside_a_submodule_keeps_the_copy(self):
        turns = [call("propose_work_plan", goal="g", items=["one"]),
                 call("write_file", path="lib/new.txt", content="n\n", reason="r"), reply("pausing")]
        root = self.repo()
        git(root, "update-index", "--add", "--cacheinfo", f"160000,{git(root, 'rev-parse', 'HEAD').strip()},lib")
        git(root, "commit", "-qm", "a submodule")
        os.mkdir(os.path.join(root, "lib"))             # as a submodule that isn't checked out leaves it
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "work_end"})
            ended = self.ended(g)
            g.close()
        self.assertFalse(ended["removed"])
        self.assertIn("lib/new.txt", ended["problems"][0])
        self.assertTrue(os.path.exists(os.path.join(ended["path"], "lib", "new.txt")))
        self.assertTrue(git(ended["path"], "rev-parse", "HEAD").strip())   # a kept copy: plain git works in it

    def test_a_branch_deleted_when_nothing_new_is_left_keeps_the_copy(self):
        turns = [call("propose_work_plan", goal="g", items=["one", "two"]),
                 call("write_file", path="b.txt", content="b\n", reason="r"),
                 call("finish_item", n=1, summary="b"), reply("pausing")]
        root = self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            git(root, "branch", "-D", "genai/work-1")
            g.send({"type": "work_end"})
            ended = self.ended(g)
            g.close()
        self.assertIn("was moved or deleted outside the session", ended["problems"][0])
        self.assertEqual((ended["branch"], ended["commands"], ended["removed"]), (None, [], False))
        self.assertTrue(os.path.exists(os.path.join(ended["path"], "b.txt")))

    def test_edits_ask_during_a_session_forgets_the_approved_files(self):
        turns = [call("edit_file", path="a.txt", old_text="hi", new_text="one", reason="r"), reply("ok"),
                 call("propose_work_plan", goal="g", items=["one"]), reply("pausing"),
                 call("edit_file", path="a.txt", old_text="one", new_text="two", reason="r"), reply("ok")]
        self.repo()
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "edits", "mode": "approved-files"})
            g.until("edits")
            g.send({"type": "user", "text": "edit"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            git(self.path("wd"), "commit", "-qam", "one")
            g.send({"type": "work", "task": "t"})
            self.approve(g)
            g.until("done")
            g.send({"type": "edits", "mode": "ask"})
            g.until("edits")
            g.send({"type": "edits", "mode": "approved-files"})
            g.until("edits")
            g.send({"type": "work_end"})
            self.ended(g)
            self.assertEqual(g.until("edits")["files"], [])
            g.send({"type": "user", "text": "edit again"})
            p = g.until("proposal")
            self.assertIsNone(p.get("auto"))
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()

    def test_a_clear_during_the_session_is_saved_as_cleared(self):
        works = [reply("EARLIER-ANSWER"), reply("looking")]
        self.repo()
        with FakeAPI(works) as api:
            g = self.start(api, session="named")
            g.send({"type": "user", "text": "EARLIER-TEXT"})
            g.until("done")
            g.send({"type": "work", "task": "t"})
            g.until("done")
            g.send({"type": "clear"})
            g.until("cleared")
            with open(os.path.join(self.home, "sessions", "named.json")) as f:
                saved = json.load(f)
            g.close()
        self.assertNotIn("EARLIER-TEXT", json.dumps(saved["messages"]))


class TestCopy(unittest.TestCase):
    """The copy and the branch, with git alone: no model, no sandbox."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = d.name
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_ENV, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                   XDG_STATE_HOME=os.path.join(self.tmp, "state"))
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def repo(self, *init):
        root = os.path.join(self.tmp, "repo")
        os.makedirs(root)
        git(root, "init", "-q", "-b", "main", *init)
        with open(os.path.join(root, "a.txt"), "w") as f:
            f.write("a\n")
        git(root, "add", "a.txt")
        git(root, "commit", "-qm", "a")
        return root

    def item(self, c, name):
        with open(os.path.join(c.path, name), "w") as f:
            f.write(name)
        self.assertTrue(c.commit(name))

    def test_a_fetch_that_finished_before_a_signal_counts_as_published(self):
        root = self.repo()
        c, _ = G.Copy.create(root)
        self.addCleanup(c.remove)
        self.item(c, "b")
        git(root, "fetch", "-q", c.gitdir, f"{c.head()}:refs/heads/{c.branch}")   # done, but not yet recorded
        c.publish()
        self.assertEqual((c.branch, c.published), ("genai/work-1", c.head()))
        self.assertNotIn("genai/work-2", git(root, "branch"))
        self.item(c, "c")
        git(root, "fetch", "-q", c.gitdir, f"{c.head()}:refs/heads/{c.branch}")
        c.publish()                                                         # no false "moved or deleted"
        self.assertEqual(git(root, "rev-parse", c.branch).strip(), c.head())

    def test_branch_names_in_the_way(self):
        root = self.repo()
        git(root, "branch", "genai/work-1/x")
        c, _ = G.Copy.create(root)
        self.addCleanup(c.remove)
        self.assertEqual(c.branch, "genai/work-2")      # genai/work-1 can't be made beside genai/work-1/x
        git(root, "branch", "-D", "genai/work-1/x")
        git(root, "branch", "genai")
        with self.assertRaisesRegex(G.GenaiError, "a branch named genai"):
            G.Copy.create(root)

    def test_a_repository_with_git_lfs_files_is_refused(self):
        root = self.repo()
        with open(os.path.join(root, ".gitattributes"), "w") as f:
            f.write("*.bin filter=lfs diff=lfs merge=lfs -text\n")
        with open(os.path.join(root, "big.bin"), "w") as f:
            f.write("version https://git-lfs.github.com/spec/v1\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "lfs")
        with self.assertRaisesRegex(G.GenaiError, "don't support Git LFS yet, and 1 file here is stored with it "
                                                  r"\(big.bin\)"):
            G.Copy.create(root)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "state", "genai", "work")) if os.path.exists(
            os.path.join(self.tmp, "state", "genai", "work")) else [], [])

    def test_a_sha256_repository(self):
        root = self.repo("--object-format=sha256")
        c, _ = G.Copy.create(root)
        self.addCleanup(c.remove)
        self.item(c, "b")
        c.publish()
        self.assertEqual(len(git(root, "rev-parse", c.branch).strip()), 64)

    def test_the_copys_files_have_no_git_until_it_is_kept(self):
        root = self.repo()
        c, _ = G.Copy.create(root)
        self.addCleanup(c.remove)
        self.assertFalse(os.path.lexists(os.path.join(c.path, ".git")))    # git in the sandbox finds no repository
        self.item(c, "b")
        c.publish()                 # fetched from the git directory itself
        self.assertEqual(git(root, "rev-parse", c.branch).strip(), c.head())
        c.keep()
        self.assertEqual(git(c.path, "rev-parse", "HEAD").strip(), c.head())

    def test_a_copy_that_cant_be_made_is_a_refusal(self):
        root = self.repo()
        with mock.patch("tempfile.mkdtemp", side_effect=OSError(28, "No space left on device")):
            with self.assertRaisesRegex(G.GenaiError, "can't make the copy under .*No space left") as cm:
                G.Copy.create(root)
        self.assertEqual(cm.exception.kind, "work_refused")

    def test_each_copy_gets_a_directory_of_its_own(self):
        root = self.repo()
        a, _ = G.Copy.create(root)
        self.addCleanup(a.remove)
        with mock.patch.object(G.Copy, "free", return_value=1):
            b, _ = G.Copy.create(root)
        self.addCleanup(b.remove)
        self.assertNotEqual(a.home, b.home)


class TestWorkParts(unittest.TestCase):
    def test_a_pyvenv_cfg_directory_doesnt_count_as_a_virtual_environment(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "conf", "pyvenv.cfg"))
            os.makedirs(os.path.join(root, "conf", "lib"))
            with open(os.path.join(root, "conf", "lib", "k.pem"), "w") as f:
                f.write("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
            box = G.Sandbox(G.parse_sandbox({"image": "i:1"}, "p.json"), root, print)
            self.assertEqual(box.masks(), (["conf/lib/k.pem"], []))

    def test_a_signal_waits_for_the_end_of_signals_held(self):
        script = (f"import importlib.machinery, importlib.util, os, signal, sys, time\n"
                  f"l = importlib.machinery.SourceFileLoader('g', {SCRIPT!r})\n"
                  "s = importlib.util.spec_from_loader('g', l); G = importlib.util.module_from_spec(s); l.exec_module(G)\n"
                  "G.exit_on_signals()\n"
                  "try:\n"
                  "    with G.signals_held():\n"
                  "        os.kill(os.getpid(), signal.SIGTERM)\n"
                  "        time.sleep(0.2)\n"
                  "        print('finished', flush=True)\n"
                  "except SystemExit as e:\n"
                  "    print('exit', e.code, flush=True)\n")
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.split(), ["finished", "exit", str(128 + signal.SIGTERM)])

    def test_work_end_stops_a_session_that_is_still_starting(self):
        import queue
        ui = G.JsonUI(inp=None, out=open(os.devnull, "w"))
        self.addCleanup(ui.out.close)
        ui.engine = mock.Mock(work=None, halt=None)
        ui.lines, ui.busy = queue.Queue(), True
        self.assertFalse(ui.halt("end"))                # an ordinary turn: work_end waits for it
        ui.starting_work = True
        self.assertTrue(ui.halt("end"))
        self.assertEqual(ui.engine.halt, "end")

    def test_a_message_read_as_a_stop_comes_in_is_kept(self):
        import queue
        ui = G.JsonUI(inp=None, out=open(os.devnull, "w"))
        self.addCleanup(ui.out.close)
        ui.engine = mock.Mock(halt="stop")
        ui.lines = queue.Queue()
        ui.lines.put('{"type": "user", "text": "later"}\n')
        p = G.Proposal("p1", "read", "c1", path="x", reason="r")
        ui.decide([p])
        self.assertEqual(p.decision, "decline")
        self.assertEqual(ui.read(), {"type": "user", "text": "later"})

    def test_a_sandboxed_run_is_cut_off_at_the_sessions_time(self):
        w = G.Work(None, "t", 5, 60, None, None)
        w.since -= 100
        e = mock.Mock(work=w)
        p = G.Proposal("p1", "sandbox", "c1", command="sleep 30", network=False, reason="r")
        self.assertIn("min without you", G.Engine.run_sandboxed(e, p))
        e.box.run.assert_not_called()
        w.since += 90
        e.box.run.return_value = (True, 0, "", "")
        e.secrets.return_value = []
        G.Engine.run_sandboxed(e, p)
        self.assertLessEqual(e.box.run.call_args.kwargs["timeout"], 51)

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
                G.Copy.create(os.path.join(root, "new"))

    def test_a_pyvenv_cfg_doesnt_hide_a_secret_from_the_scan(self):
        with tempfile.TemporaryDirectory() as root:
            for rel, text in {"conf/pyvenv.cfg": "home = /usr\n", "conf/keys.txt": "password = hunter2hunter2\n",
                              "conf/lib/pkg.pem": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n"}.items():
                os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w") as f:
                    f.write(text)
            box = G.Sandbox(G.parse_sandbox({"image": "i:1"}, "p.json"), root, print)
            self.assertEqual(box.masks(), (["conf/keys.txt"], []))

    def test_the_repl_queues_attachments_where_a_session_sets_them_aside(self):
        ui = G.PlainUI(out=open(os.devnull, "w"))
        self.addCleanup(ui.out.close)
        r = G.Repl(mock.Mock(ui=ui))
        r.attachments.append(("file notes.txt", "x"))
        self.assertEqual(ui.queue, [("file notes.txt", "x")])
        ui.queue = []                   # what start_work does with a planner elsewhere
        self.assertEqual(r.attachments, [])

    def test_ctrl_c_at_a_question_answers_the_tool_call(self):
        s = G.Session.__new__(G.Session)
        s.messages = [{"role": "user", "content": "go"},
                      {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}, {"id": "c2"}]},
                      {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
        s.history = list(s.messages)
        r = G.Repl.__new__(G.Repl)
        r.e, r.turns = mock.Mock(session=s, client=None), []
        r.e.user_turn.side_effect = KeyboardInterrupt
        r.e.ui.queue = []
        with mock.patch("builtins.print"):
            r.send("go on")
        self.assertEqual(s.messages[-1], {"role": "tool", "tool_call_id": "c2",
                                          "content": "stopped: the user interrupted before deciding"})

    def test_a_superscript_digit_isnt_a_step_count(self):
        r = G.Repl.__new__(G.Repl)
        r.e = mock.Mock(work=None)
        with mock.patch("builtins.print"):
            r.do_work("--steps ² a task")
        r.e.start_work.assert_called_once_with("--steps ² a task", None, None)

    def test_secret_file_names_are_hidden_from_the_file_tools_too(self):
        for name in ("service.key", "id_rsa", "conf/.env", "certs/store.p12"):
            self.assertTrue(G.is_hidden(name.split("/")), name)
        self.assertFalse(G.is_hidden(["src", "keys.py"]))

    def test_a_stop_while_a_run_is_set_up_stops_it_before_podman(self):
        box = G.Sandbox(G.parse_sandbox({"image": "i:1"}, "p.json"), tempfile.gettempdir(), print)
        G.Sandbox.stop.set()
        self.addCleanup(G.Sandbox.stop.clear)
        with mock.patch("subprocess.Popen") as popen, mock.patch.object(box, "_kill"):
            with self.assertRaises(KeyboardInterrupt):
                box._podman(["podman", "run"], "n", 100, time.monotonic() + 60)
        popen.assert_not_called()

    def test_a_run_whose_time_is_up_doesnt_start(self):
        box = G.Sandbox(G.parse_sandbox({"image": "i:1"}, "p.json"), tempfile.gettempdir(), print)
        with mock.patch("subprocess.Popen") as popen:
            self.assertEqual(box._podman(["podman", "run"], "n", 100, time.monotonic() - 1), ("late", ""))
        popen.assert_not_called()

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
