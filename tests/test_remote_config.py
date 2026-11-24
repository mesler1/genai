"""Tests for `genai --remote-config`: the resolved endpoint and profile as one JSON line, with nothing secret in it."""

import base64
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fakeapi import FakeAPI  # noqa: E402
from test_genai import G, SCRIPT, Proc, Tmp, bless_path, make_home  # noqa: E402


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
        d = os.path.join(home, kind + "s", name)
        os.makedirs(d)
        for rel, text in files.items():
            with open(os.path.join(d, rel), "w") as f:
                f.write(text)
        return d

    def bless_dir(self, home, kind, d):
        G.Config(home).set_blessed(d, {"kind": kind, "name": os.path.basename(d), "hash": G.hash_skill(d)[0]})

    def test_skills_and_plugins_in_use_go_with_their_bytes(self):
        home, _ = self.setup_home()
        a = self.item(home, "skill", "a", {"SKILL.md": "# A\n", "notes.txt": "more"})
        self.item(home, "skill", "unblessed", {"SKILL.md": "# U\n"})
        b = self.item(home, "skill", "b", {"SKILL.md": "# B\n"})
        pl = self.item(home, "plugin", "pl", {"plugin.json": json.dumps(
            {"name": "pl", "commands": {"x": {"describe": "x", "argv": ["true"], "read_only": True}}})})
        for kind, d in (("skill", a), ("skill", b), ("plugin", pl)):
            self.bless_dir(home, kind, d)
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"default_skills": ["a", "unblessed", "missing"], "default_plugins": ["pl"]}, f)
        r = self.run_export(home, ["-e", "test", "-k", "b"])
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([x["name"] for x in out["skills"]], ["a", "b"])
        self.assertIn("skill 'unblessed' is not sent to the remote host: unblessed", r.stderr)
        self.assertIn("skill 'missing' is not sent to the remote host: not found", r.stderr)
        sa = out["skills"][0]
        self.assertEqual(base64.b64decode(sa["files"]["notes.txt"]), b"more")
        self.assertEqual(sa["hash"], G.hash_skill(a)[0])
        self.assertEqual(out["plugins"][0]["hash"], G.hash_skill(pl)[0])
        with open(os.path.join(home, "audit.log")) as f:
            rec = [json.loads(x) for x in f if "remote_config_export" in x][-1]
        self.assertEqual(rec["skills"], {"a": sa["hash"], "b": out["skills"][1]["hash"]})
        with open(os.path.join(b, "SKILL.md"), "a") as f:          # changed since blessing: left out
            f.write("x")
        r = self.run_export(home, ["-e", "test", "-k", "b"])
        self.assertEqual([x["name"] for x in json.loads(r.stdout)["skills"]], ["a"])
        self.assertIn("skill 'b' is not sent to the remote host: changed", r.stderr)


class TestRemoteConfigLine(Tmp):
    """remote_config on the remote host: the skills and plugins are made from the bytes sent, hashed again."""

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
        plug = {"plugin.json": json.dumps({"name": "pl", "commands": {"x": {"describe": "d", "argv": ["true"]}}})}
        _, _, offered = self.parse(skills=[self.entry("s", {"SKILL.md": "# S\nbody"})], plugins=[self.entry("pl", plug)])
        self.assertEqual(offered["skills"]["s"].text(), "# S\nbody")
        self.assertEqual(list(offered["plugins"]["pl"].commands), ["x"])
        self.assertEqual(self.parse()[2], {"skills": {}, "plugins": {}})
        with open(self.path("audit.log")) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["skills"], {"s": offered["skills"]["s"].hash})

    def test_refusals(self):
        good = self.entry("s", {"SKILL.md": "# S"})
        tampered = dict(good, files={"SKILL.md": base64.b64encode(b"# other").decode()})
        badname = self.entry("pl", {"plugin.json": json.dumps({"name": "other", "commands": {}})})
        for extra, why in (({"skills": [tampered]}, "don't match the hash"),
                           ({"skills": [good, good]}, "distinct"),
                           ({"skills": [dict(good, files={"../x": "eA=="})]}, "bad file"),
                           ({"skills": [dict(good, files={"SKILL.md": "%%%"})]}, "not base64"),
                           ({"skills": [self.entry("s", {"README": "x"})]}, "no SKILL.md"),
                           ({"skills": [dict(good, extra=1)]}, "exactly"),
                           ({"skills": {"s": good}}, "must be a list"),
                           ({"plugins": [badname]}, "plugin 'pl'")):
            with self.assertRaises(G.GenaiError) as e:
                self.parse(**extra)
            self.assertIn(why, str(e.exception), extra)
            self.assertEqual(e.exception.kind, "bad_config")

    def test_hello_names_the_audit_log(self):
        with FakeAPI([]) as api:
            home, path = make_home(self.tmp, api.url)
            bless_path(home, path)
            g = Proc(home, self.tmp, ["-e", "test"])
            self.assertEqual(g.until("hello")["audit"], os.path.join(home, "audit.log"))
            g.close()


if __name__ == "__main__":
    unittest.main()
