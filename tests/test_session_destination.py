"""Tests for replaying saved context only to its approved destination."""

import json
import os
import subprocess

from test_genai import FakeAPI, SCRIPT, Tmp, bless_path, make_home, reply


class TestSessionDestination(Tmp):
    def invoke(self, home, *args):
        return subprocess.run([SCRIPT, "--json", "-e", "test", "-S", "saved", *args],
                              cwd=self.tmp, env={**os.environ, "XDG_CONFIG_HOME": os.path.dirname(home)},
                              input="", capture_output=True, text=True, timeout=10)

    def test_same_name_new_url_requires_explicit_opt_in(self):
        with FakeAPI([reply("first")]) as old, FakeAPI([reply("second")]) as new:
            home, path = make_home(self.tmp, old.url)
            bless_path(home, path)
            first = self.invoke(home, "original context")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            with open(path) as f:
                endpoint = json.load(f)
            endpoint["base_url"] = new.url
            with open(path, "w") as f:
                json.dump(endpoint, f)
            bless_path(home, path)

            denied = self.invoke(home, "continue")
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("session_endpoint_changed", denied.stdout)
            self.assertEqual(new.requests, [])

            approved = self.invoke(home, "--allow-session-endpoint-change", "continue")
            self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
            self.assertIn("original context", json.dumps(new.requests[0]["body"]["messages"]))

    def test_another_model_on_the_same_server_needs_no_consent(self):
        with FakeAPI([reply("first"), reply("second")]) as api:
            home, path = make_home(self.tmp, api.url)
            bless_path(home, path)
            self.assertEqual(self.invoke(home, "original context").returncode, 0)
            with open(path) as f:
                endpoint = json.load(f)
            endpoint.update(model="bigger-model", base_url=api.url.rstrip("/") + "/")
            with open(path, "w") as f:
                json.dump(endpoint, f)
            bless_path(home, path)
            again = self.invoke(home, "continue")
            self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
            self.assertEqual(api.requests[-1]["body"]["model"], "bigger-model")

    def test_legacy_session_requires_opt_in(self):
        home, path = make_home(self.tmp, "https://local.example/v1")
        bless_path(home, path)
        os.mkdir(os.path.join(home, "sessions"), 0o700)
        with open(os.path.join(home, "sessions", "saved.json"), "w") as f:
            json.dump({"endpoint": "test", "messages": [{"role": "user", "content": "saved"}]}, f)
        denied = self.invoke(home)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("before its destination was recorded", denied.stdout)
