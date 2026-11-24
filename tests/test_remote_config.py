"""Tests for `genai --remote-config`: the resolved endpoint and profile as one JSON line, with nothing secret in it."""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fakeapi import FakeAPI  # noqa: E402
from test_genai import SCRIPT, Proc, Tmp, bless_path, make_home  # noqa: E402


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

    def test_session_and_skills_are_refused(self):
        home, _ = self.setup_home()
        self.assertEqual(self.run_export(home, ["-e", "test", "-S", "x"]).returncode, 2)
        self.assertEqual(self.run_export(home, ["-e", "test", "-k", "x"]).returncode, 2)

    def test_hello_names_the_audit_log(self):
        with FakeAPI([]) as api:
            home, path = make_home(self.tmp, api.url)
            bless_path(home, path)
            g = Proc(home, self.tmp, ["-e", "test"])
            self.assertEqual(g.until("hello")["audit"], os.path.join(home, "audit.log"))
            g.close()


if __name__ == "__main__":
    unittest.main()
