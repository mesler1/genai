"""Tests for `hoplon --remote-config`: the resolved endpoint and profile as one JSON line, with nothing secret in it."""

import base64
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fakeapi import FakeAPI, reply  # noqa: E402
from test_hoplon import G, SCRIPT, Proc, Tmp, bless_path, make_home  # noqa: E402


class TestRemoteConfigExport(Tmp):
    def run_export(self, home, args=()):
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home))
        return subprocess.run([sys.executable, SCRIPT, "--remote-config"] + list(args), env=env, cwd=self.tmp,
                              capture_output=True, text=True)

    def setup_home(self, **extra):
        home, path = make_home(self.tmp, "https://llm.example/v1", key_env="SECRET_KEY_VAR", proxy="http://px:3128",
                               headers={"X-Team": "blue-secret"}, ca_bundle="/etc/ca.pem", **extra)
        bless_path(home, path)
        return home, path

    def test_exports_without_key_headers_or_proxy(self):
        home, _ = self.setup_home()
        r = self.run_export(home, ["-e", "test"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 1)
        out = json.loads(r.stdout)
        self.assertEqual((out["endpoint_name"], out["base_path"], out["profile"]), ("test", "/v1", None))
        self.assertEqual(out["endpoint"], {"name": "test", "base_url": "https://llm.example/v1", "model": "fake-model",
                                           "context_limit": 100000})
        for secret in ("SECRET_KEY_VAR", "blue-secret", "px:3128", "ca.pem"):
            self.assertNotIn(secret, r.stdout)

    def test_default_endpoint_and_profile_rules(self):
        home, _ = self.setup_home()
        ppath = os.path.join(home, "profiles", "p.json")
        os.makedirs(os.path.dirname(ppath))
        with open(ppath, "w") as f:
            json.dump({"name": "p", "endpoint": "test", "persona": "be brief"}, f)
        bless_path(home, ppath)
        out = json.loads(self.run_export(home, ["-p", "p"]).stdout)      # the profile names the endpoint
        self.assertEqual((out["endpoint_name"], out["profile"]["persona"]), ("test", "be brief"))
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"default_endpoint": "test"}, f)
        self.assertEqual(json.loads(self.run_export(home).stdout)["endpoint_name"], "test")

    def test_unblessed_and_changed_files_are_refused(self):
        home, path = make_home(self.tmp, "https://llm.example/v1")
        r = self.run_export(home, ["-e", "test"])
        self.assertEqual((r.returncode, r.stdout), (3, ""))
        self.assertIn("not blessed", r.stderr)
        bless_path(home, path)
        with open(path, "a") as f:
            f.write(" ")
        r = self.run_export(home, ["-e", "test"])
        self.assertEqual((r.returncode, r.stdout), (3, ""))
        self.assertIn("changed since it was blessed", r.stderr)

    def test_no_endpoint_is_refused(self):
        home, _ = self.setup_home()
        r = self.run_export(home)
        self.assertEqual(r.returncode, 3)
        self.assertIn("no endpoint is configured", r.stderr)

    def test_a_session_is_refused(self):
        home, _ = self.setup_home()
        self.assertEqual(self.run_export(home, ["-e", "test", "-S", "x"]).returncode, 2)

    def item(self, home, kind, name, files):
        d = os.path.join(home, {"skill": "skills", "command_set": "commands"}[kind], name)
        os.makedirs(d)
        for rel, text in files.items():
            with open(os.path.join(d, rel), "w") as f:
                f.write(text)
        return d

    def bless_dir(self, home, kind, d):
        G.Config(home).set_blessed(d, {"kind": kind, "name": os.path.basename(d), "hash": G.hash_skill(d)[0]})

    def test_skills_and_command_sets_in_use_go_with_their_bytes(self):
        home, _ = self.setup_home()
        a = self.item(home, "skill", "a", {"SKILL.md": "# A\n", "notes.txt": "more"})
        self.item(home, "skill", "unblessed", {"SKILL.md": "# U\n"})
        b = self.item(home, "skill", "b", {"SKILL.md": "# B\n"})
        pl = self.item(home, "command_set", "pl", {"commands.json": json.dumps(
            {"name": "pl", "commands": {"x": {"describe": "x", "argv": ["true"], "access": "read", "network": "none"}}})})
        for kind, d in (("skill", a), ("skill", b), ("command_set", pl)):
            self.bless_dir(home, kind, d)
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"default_skills": ["a", "unblessed", "missing"], "default_commands": ["pl"]}, f)
        r = self.run_export(home, ["-e", "test", "-k", "b"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([x["name"] for x in out["skills"]], ["a", "b"])
        self.assertIn("skill 'unblessed' is not sent to the remote host: unblessed", r.stderr)
        self.assertIn("skill 'missing' is not sent to the remote host: not found", r.stderr)
        sa = out["skills"][0]
        self.assertEqual(base64.b64decode(sa["files"]["notes.txt"]), b"more")
        self.assertEqual(sa["hash"], G.hash_skill(a)[0])
        self.assertEqual(out["command_sets"][0]["hash"], G.hash_skill(pl)[0])
        self.assertEqual(out["command_sets"][0]["auto"], [])
        with open(os.path.join(home, "audit.log")) as f:
            rec = [json.loads(x) for x in f if "remote_config_export" in x][-1]
        self.assertEqual(rec["skills"], {"a": sa["hash"], "b": out["skills"][1]["hash"]})
        with open(os.path.join(b, "SKILL.md"), "a") as f:          # changed since blessing: left out
            f.write("x")
        r = self.run_export(home, ["-e", "test", "-k", "b"])
        self.assertEqual([x["name"] for x in json.loads(r.stdout)["skills"]], ["a"])
        self.assertIn("skill 'b' is not sent to the remote host: changed", r.stderr)


class TestRemoteConfigLine(Tmp):
    """remote_config on the remote host: the skills and command sets are made from the bytes sent, hashed again."""

    def line(self, **extra):
        msg = {"type": "config", "token": "ab" * 16,
               "endpoint": {"name": "e", "base_url": "http://127.0.0.1:4000/v1", "model": "m"}, "profile": None}
        msg.update(extra)
        return json.dumps(msg)

    @staticmethod
    def entry(name, files):
        data = {k: v.encode() for k, v in files.items()}
        return {"name": name, "hash": G.hash_snapshot(data),
                "files": {k: base64.b64encode(v).decode() for k, v in data.items()}}

    def parse(self, **extra):
        return G.remote_config(self.line(**extra), G.Audit(self.path("audit.log")))

    def test_items_are_made_from_the_bytes_sent(self):
        plug = {"commands.json": json.dumps({"name": "pl", "commands": {"x": {"describe": "d", "argv": ["true"], "access": "read", "network": "none"}}})}
        _, _, offered = self.parse(skills=[self.entry("s", {"SKILL.md": "# S\nbody"})], command_sets=[self.entry("pl", plug)])
        self.assertEqual(offered["skills"]["s"].text(), "# S\nbody")
        self.assertEqual(list(offered["command_sets"]["pl"].commands), ["x"])
        wplug = {"commands.json": json.dumps({"name": "w", "commands": {
            "x": {"describe": "d", "argv": ["true"], "access": "write", "network": "none"},
            "y": {"describe": "d", "argv": ["true"], "access": "write", "network": "none"}}})}
        _, _, got = self.parse(command_sets=[dict(self.entry("w", wplug), auto=["x"])])
        self.assertEqual((got["command_sets"]["w"].commands["x"].auto, got["command_sets"]["w"].commands["y"].auto),
                         (True, False))
        self.assertEqual(self.parse()[2], {"skills": {}, "command_sets": {}, "session": None})
        with open(self.path("audit.log")) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["skills"], {"s": offered["skills"]["s"].hash})

    def test_refusals(self):
        good = self.entry("s", {"SKILL.md": "# S"})
        tampered = dict(good, files={"SKILL.md": base64.b64encode(b"# other").decode()})
        badname = self.entry("pl", {"commands.json": json.dumps({"name": "other", "commands": {}})})
        for extra, why in (({"skills": [tampered]}, "don't match the hash"),
                           ({"skills": [good, good]}, "distinct"),
                           ({"skills": [dict(good, files={"../x": "eA=="})]}, "bad file"),
                           ({"skills": [dict(good, files={"SKILL.md": "%%%"})]}, "not base64"),
                           ({"skills": [self.entry("s", {"README": "x"})]}, "no SKILL.md"),
                           ({"skills": [dict(good, extra=1)]}, "exactly"),
                           ({"skills": {"s": good}}, "must be a list"),
                           ({"command_sets": [dict(badname, auto="x")]}, "'auto' must be a list"),
                           ({"skills": [dict(good, auto=[])]}, "exactly"),
                           ({"command_sets": [badname]}, "command set 'pl'")):
            with self.assertRaises(G.HoplonError) as e:
                self.parse(**extra)
            self.assertIn(why, str(e.exception), extra)
            self.assertEqual(e.exception.kind, "bad_config")

    def test_a_session_is_checked_for_shape(self):
        self.assertIsNone(self.parse()[2].get("session"))
        self.assertEqual(self.parse(session={"name": "tui-a"})[2]["session"], {"name": "tui-a", "data": None})
        for bad in ({"name": "../x"}, {"name": "a", "extra": 1}, "tui-a", {"name": "a", "data": {"messages": "x"}},
                    {"name": "a", "data": {"coding": "yes"}}, {"name": "a", "data": {"history": [1]}}):
            with self.assertRaises(G.HoplonError, msg=bad) as e:
                self.parse(session=bad)
            self.assertEqual(e.exception.kind, "bad_config")

    def test_a_remote_session_is_resumed_from_the_line_and_saved_back_not_here(self):
        """Remote mode keeps the conversation on the user's machine: hoplon on the host gets it in the config line,
        sends every save back as session_save, and writes no session file of its own."""
        with FakeAPI([reply("second answer")]) as api:
            home, path = make_home(self.tmp, api.url)
            g = Proc(home, self.tmp, ["--config-stdin"])
            earlier = [{"role": "user", "content": "EARLIER-QUESTION"}, {"role": "assistant", "content": "first answer"}]
            g.send({"type": "config", "token": "ab" * 16, "profile": None,
                    "endpoint": {"name": "e", "base_url": api.url, "model": "fake-model"},
                    "session": {"name": "tui-abc123", "data": {"messages": earlier, "history": earlier,
                                                               "coding": False, "cwd": self.tmp, "skills": [],
                                                               "created": "2026-11-27T10:00:00"}}})
            hello = g.until("hello")
            self.assertEqual((hello["session"], hello["resumed"]), ("tui-abc123", True))
            g.send({"type": "user", "text": "NEW-QUESTION"})
            saved = g.until("session_save")["session"]
            g.until("done")
            g.close()
            sent = json.dumps(api.requests[-1])
            self.assertIn("EARLIER-QUESTION", sent)
            saved = [e for e in g.events if e["type"] == "session_save"][-1]["session"]
            self.assertEqual([m.get("content") for m in saved["history"]],
                             ["EARLIER-QUESTION", "first answer", "NEW-QUESTION", "second answer"])
            self.assertEqual(saved["id"], "tui-abc123")
            self.assertFalse(os.path.exists(os.path.join(home, "sessions", "tui-abc123.json")))
            self.assertEqual(os.listdir(os.path.join(home, "sessions")) if os.path.isdir(os.path.join(home, "sessions"))
                             else [], [])

    def test_a_remote_session_is_not_resumed_by_hoplon_here(self):
        home, _ = make_home(self.tmp, "https://llm.example/v1")
        os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
        with open(os.path.join(home, "sessions", "tui-r.json"), "w") as f:
            json.dump({"id": "tui-r", "remote": {"dest": "me@devbox", "as": None, "dir": None}, "messages": []}, f)
        r = subprocess.run([sys.executable, SCRIPT, "-e", "test", "-S", "tui-r", "hi"], cwd=self.tmp,
                           env=dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home)), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ran on me@devbox, through hoplon-tui: resume it there with hoplon-tui --remote me@devbox -S tui-r",
                      r.stderr)
        rows = G.list_sessions(G.Config(home))
        self.assertEqual([x["remote"] for x in rows], ["me@devbox"])

    def test_hello_names_the_audit_log(self):
        with FakeAPI([]) as api:
            home, path = make_home(self.tmp, api.url)
            bless_path(home, path)
            g = Proc(home, self.tmp, ["-e", "test"])
            self.assertEqual(g.until("hello")["audit"], os.path.join(home, "audit.log"))
            g.close()


if __name__ == "__main__":
    unittest.main()
