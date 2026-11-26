"""The step limit counts only model steps nobody decided anything in: automatic reads run on their own until
max_steps, but a turn where you approve or decline each step goes on as long as you do."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakeapi import FakeAPI, call, reply  # noqa: E402
from test_hoplon import G, Proc, Tmp, bless_path, make_home  # noqa: E402


class TestSteps(Tmp):
    def start(self, api, max_steps, *args):
        home, path = make_home(self.tmp, api.url)
        bless_path(home, path)
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"max_steps": max_steps}, f)
        wd = self.path("wd")
        os.makedirs(wd, exist_ok=True)
        with open(os.path.join(wd, "a.txt"), "w") as f:
            f.write("a\n")
        return Proc(home, wd, ["-e", "test"] + list(args))

    def test_unattended_steps_pause_at_max_steps(self):
        turns = [call("read_file", path="a.txt", reason="r") for _ in range(5)] + [reply("done")]
        with FakeAPI(turns) as api:
            g = self.start(api, 3, "--coding")
            g.send({"type": "user", "text": "read it a lot"})
            self.assertEqual(g.until("done")["reason"], "step_limit")
            notice = [e for e in g.events if e["type"] == "notice" and "steps in a row" in e.get("message", "")]
            self.assertIn("max_steps", notice[0]["message"])
            g.close()
        self.assertEqual(len(api.requests), 3)

    def test_steps_you_decide_have_no_limit(self):
        turns = [call("write_file", path=f"f{n}.txt", content="x\n", reason="r") for n in range(5)] + [reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api, 2)
            g.send({"type": "user", "text": "write five"})
            for n in range(5):
                p = g.until("proposal")
                g.send({"type": "decision", "id": p["id"], "digest": p["digest"],
                        "decision": "accept" if n % 2 else "decline"})
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        self.assertEqual(len(api.requests), 6)

    def test_the_setting_is_checked(self):
        home = self.path("cfg", "hoplon")
        os.makedirs(home)
        for bad in (0, 1001, "50", 2.5, True):
            with open(os.path.join(home, "config.json"), "w") as f:
                json.dump({"max_steps": bad}, f)
            with self.subTest(bad=bad), self.assertRaisesRegex(G.HoplonError, "max_steps"):
                G.Config(home)
        os.remove(os.path.join(home, "config.json"))
        self.assertEqual(G.Config(home).max_steps, 50)


if __name__ == "__main__":
    unittest.main()
