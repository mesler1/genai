"""Tests for /fixtures: real samples for the model's sandboxed runs, kept outside the project, used only once the user
approves exactly what is there, after the secret checks, and mounted read-only at /fixtures. Also request_samples,
the model's way to ask for them. A fake podman stands in for containers (see test_sandbox_runner)."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakeapi import FakeAPI, call, reply  # noqa: E402
from test_hoplon import G, Proc, Tmp, bless_path, make_home, write_json  # noqa: E402
from test_sandbox_runner import FakePodman  # noqa: E402


class TestStore(Tmp):
    def setUp(self):
        super().setUp()
        self.proj = self.path("proj")
        os.makedirs(self.proj)
        self.fx = G.Fixtures(self.path("state", "fixtures"), self.proj, "s1")

    def put(self, name, text):
        src = self.path(name)
        with open(src, "w") as f:
            f.write(text)
        return self.fx.add([name], self.tmp)

    def test_outside_the_project_private_and_named_by_project_and_job(self):
        self.put("app.log", "GET / 200\n")
        self.assertFalse(self.fx.dir.startswith(self.proj))
        self.assertTrue(self.fx.dir.endswith(os.path.join("fixtures", f"proj-{G.hashlib.sha256(os.path.realpath(self.proj).encode()).hexdigest()[:8]}", "s1")))
        self.assertEqual(os.stat(self.fx.dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.fx.dir, "app.log")).st_mode & 0o777, 0o600)
        meta = json.load(open(os.path.join(self.fx.group, "s1.json")))
        self.assertEqual((meta["project"], meta["job"], meta["pid"]), (os.path.realpath(self.proj), "s1", os.getpid()))

    def test_a_root_inside_the_project_or_holding_home_is_refused(self):
        for root in (os.path.join(self.proj, ".hoplon"), os.path.dirname(os.path.expanduser("~"))):
            fx = G.Fixtures(root, self.proj, "s1")
            with self.assertRaises(G.HoplonError):
                fx.ensure()

    def test_approval_is_for_exactly_what_was_listed(self):
        self.put("app.log", "GET / 200\n")
        listed = G.Fixtures.digest(self.fx.scan())
        self.put("more.log", "GET /x 404\n")
        with self.assertRaisesRegex(G.HoplonError, "changed since they were listed"):
            self.fx.approve(listed)
        self.fx.approve(G.Fixtures.digest(self.fx.scan()))
        self.assertEqual(self.fx.mount(), self.fx.dir)

    def test_a_change_after_approval_forgets_it_and_stops_the_run(self):
        self.put("app.log", "GET / 200\n")
        self.fx.approve(G.Fixtures.digest(self.fx.scan()))
        with open(os.path.join(self.fx.dir, "app.log"), "a") as f:
            f.write("more\n")
        with self.assertRaisesRegex(G.HoplonError, "changed since you approved"):
            self.fx.mount()
        self.assertIsNone(self.fx.approved)
        self.assertIsNone(self.fx.mount())

    def test_secret_looking_samples_are_flagged_with_the_reason_not_emptied(self):
        self.put("app.log", "user=bob password=hunter2222\n")
        self.put("id_rsa", "x\n")
        rows = {r["name"]: r["flag"] for r in self.fx.scan()}
        self.assertIn("looks like it holds a secret", rows["app.log"])
        self.assertIn("name looks like a secret", rows["id_rsa"])
        with self.assertRaisesRegex(G.HoplonError, "can't be used"):
            self.fx.approve(G.Fixtures.digest(self.fx.scan()))

    def test_links_are_refused(self):
        os.symlink("/etc/passwd", self.path("link"))
        with self.assertRaisesRegex(G.HoplonError, "not a regular file"):
            self.fx.add(["link"], self.tmp)
        self.put("a.log", "x\n")
        os.symlink("/etc/passwd", os.path.join(self.fx.dir, "planted"))
        self.assertEqual({r["name"]: r["flag"] for r in self.fx.scan()}["planted"], "not a regular file")

    def test_clear_removes_the_folder_and_its_record(self):
        self.put("app.log", "x\n")
        self.fx.clear()
        self.assertFalse(os.path.exists(self.fx.dir))
        self.assertFalse(os.path.exists(self.fx.group))

    def test_the_mount_is_read_only_at_fixtures(self):
        box = G.Sandbox(G.parse_sandbox({"image": "img:1"}, "p"), self.proj, lambda m: None)
        argv = box.argv("n", "true", [], [], "/e", "g", "/samples/dir")
        self.assertIn("--volume=/samples/dir:/fixtures:ro", argv)
        self.assertFalse(any("/fixtures" in a for a in box.argv("n", "true", [], [], "/e", "g")))


class TestRepl(Tmp):
    """Plain hoplon, without hoplon-tui: the same folder, checks and approval through :fixtures."""

    def test_add_use_and_clear(self):
        import io
        from unittest import mock
        proj = self.path("proj")
        os.makedirs(proj)
        with open(self.path("proj", "app.log"), "w") as f:
            f.write("GET / 200\n")
        with open(self.path("proj", "creds.log"), "w") as f:
            f.write("password=hunter2222\n")
        cfg = G.Config(self.path("home", "hoplon"))
        cfg.ensure()
        cfg.fixtures_dir = self.path("fx")
        ep = G.Endpoint({"name": "t", "base_url": "https://llm.invalid/v1", "model": "m"}, "t.json")
        cwd = os.getcwd()
        os.chdir(proj)
        try:
            eng = G.Engine(cfg, ep, G.Session(cfg), G.PlainUI())
            repl, out = G.Repl(eng), io.StringIO()
            with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()):
                repl.command(":fixtures add creds.log")
                with mock.patch.object(G, "ask_yes", return_value=True) as asked:
                    repl.command(":fixtures use")           # a flagged file: it never asks
                self.assertFalse(asked.called)
                repl.command(":fixtures drop")
                os.remove(os.path.join(eng.fixtures.dir, "creds.log"))
                repl.command(":fixtures add app.log")
                with mock.patch.object(G, "ask_yes", return_value=True) as asked:
                    repl.command(":fixtures use")
                self.assertEqual(asked.call_args[0][0], G.FIXTURES_ASK)
                self.assertEqual(eng.fixtures.mount(), eng.fixtures.dir)
                repl.command(":fixtures sometimes")
                with mock.patch.object(G, "ask_yes", return_value=True):
                    repl.command(":fixtures clear")
        finally:
            os.chdir(cwd)
        text = out.getvalue()
        self.assertIn("can't be used: it looks like it holds a secret", text)
        self.assertIn("approved: the model's sandboxed runs see them read-only at /fixtures", text)
        self.assertIn("usage: :fixtures", text)
        self.assertTrue(eng.fixtures.dir.startswith(self.path("fx")))
        self.assertFalse(os.path.exists(eng.fixtures.dir))


class TestLoop(FakePodman):
    def start(self, api, fixtures_dir=None):
        home, ep = make_home(self.tmp, api.url)
        bless_path(home, ep)
        prof = {"name": "coder", "endpoint": "test", "coding": True, "sandbox": {"image": "img:1"}}
        bless_path(home, write_json(os.path.join(home, "profiles", "coder.json"), prof))
        if fixtures_dir:
            cfg = json.load(open(os.path.join(home, "config.json"))) if os.path.exists(
                os.path.join(home, "config.json")) else {}
            write_json(os.path.join(home, "config.json"), dict(cfg, fixtures_dir=fixtures_dir))
        self.project({"a.txt": "hi\n"})
        self.home = home
        env = dict(self.env, XDG_STATE_HOME=self.path("xdg-state"))
        return Proc(home, self.path("wd"), ["-p", "coder"], env=env)

    def audit(self):
        with open(os.path.join(self.home, "audit.log")) as f:
            return [json.loads(x) for x in f]

    def test_the_model_asks_the_user_adds_and_approves_and_runs_see_them(self):
        with open(self.path("app.log"), "w") as f:
            f.write("GET / 200\n")
        turns = [call("request_samples", what="200 lines of the app log", files=["app.log"],
                      suggest="tail -n 200 /var/log/app.log > app.log", reason="the parser needs real lines"),
                 call("run_in_sandbox", command="true", reason="t"), reply("works")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.until("hello")
            g.send({"type": "user", "text": "write a parser"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p["files"]), ("samples", ["app.log"]))
            self.assertIn(os.path.join(self.path("xdg-state"), "hoplon", "fixtures"), p["folder"])
            self.assertEqual(g.until("done")["reason"], "command")     # its turn ends there, as after a command
            g.send({"type": "fixtures", "action": "add", "paths": ["app.log"], "cwd": self.tmp})
            listed = g.until("fixtures")
            self.assertEqual([r["name"] for r in listed["files"]], ["app.log"])
            self.assertFalse(listed["approved"])
            self.assertIn("may send their contents to the model endpoint", listed["ask"])
            g.send({"type": "fixtures", "action": "use", "digest": listed["digest"]})
            self.assertTrue(g.until("fixtures")["approved"])
            g.send({"type": "user", "text": "they are there"})
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        tools = [t["function"]["name"] for t in api.requests[0]["body"]["tools"]]
        self.assertIn("request_samples", tools)
        self.assertIn("ask with request_samples", api.requests[0]["body"]["messages"][0]["content"])
        tool = [m for m in api.requests[1]["body"]["messages"] if m["role"] == "tool"][-1]
        self.assertIn("Wait for them", tool["content"])
        self.assertIn("sees them read-only in /fixtures. Commit only invented data",
                      api.requests[1]["body"]["messages"][0]["content"])
        run = [c for c in self.podman_calls() if "run" in c][0]
        self.assertIn(f"--volume={listed['dir']}:/fixtures:ro", run)
        events = [e["event"] for e in self.audit()]
        self.assertTrue({"fixtures_add", "fixtures_use"} <= set(events))
        self.assertFalse(listed["dir"].startswith(self.path("wd")))

    def test_without_approval_runs_get_no_fixtures(self):
        with FakeAPI([call("run_in_sandbox", command="true", reason="t"), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "fixtures", "action": "add", "paths": ["a.txt"], "cwd": self.path("wd")})
            g.until("fixtures")
            g.send({"type": "user", "text": "x"})
            g.until("done")
            g.close()
        self.assertFalse(any("/fixtures" in a for c in self.podman_calls() for a in c))

    def test_the_root_can_be_set_in_config(self):
        with FakeAPI([]) as api:
            g = self.start(api, fixtures_dir=self.path("mine"))
            g.send({"type": "fixtures"})
            self.assertTrue(g.until("fixtures")["dir"].startswith(self.path("mine")))
            g.close()

    def test_a_root_in_the_project_is_refused(self):
        with FakeAPI([]) as api:
            g = self.start(api, fixtures_dir=self.path("wd", "samples"))
            g.send({"type": "fixtures", "action": "add", "paths": ["a.txt"], "cwd": self.path("wd")})
            self.assertIn("inside this project", g.until("error")["message"])
            g.close()

    def test_bad_messages_are_refused(self):
        with FakeAPI([]) as api:
            g = self.start(api)
            for msg in ({"action": "add"}, {"action": "use"}, {"action": "nuke"}, {"action": "add", "paths": [1]}):
                g.send(dict(msg, type="fixtures"))
                self.assertEqual(g.until("error")["code"], "bad_message")
            g.close()


if __name__ == "__main__":
    unittest.main()
