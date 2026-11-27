"""Tests for edits "approved-files": a further edit or write to a file you approved one for applies without asking.
Run: python3 -m unittest discover -s tests"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_hoplon as T  # noqa: E402
from fakeapi import FakeAPI, call, reply  # noqa: E402
from test_approvals import Approvals  # noqa: E402

SECRET = "id AKIAIOSFODNN7EXAMPLE end"


def edit(path, old, new):
    return call("edit_file", path=path, old_text=old, new_text=new, reason="r")


class Edits(Approvals):
    def start(self, api, *args, on=True):
        g = super().start(api, *args)
        self.hello = g.until("hello")
        if on:
            g.send({"type": "edits", "mode": "approved-files"})
            self.assertEqual(g.until("edits")["mode"], "approved-files")
        return g

    def turn(self, g, answers=()):
        """One user turn. answers: "accept" or "decline", one per proposal that asks, in order. Returns the
        proposal events of the turn."""
        g.send({"type": "user", "text": "go"})
        answers, props = list(answers), []
        while True:
            ev = g.until("proposal", "done", "error")
            if ev["type"] != "proposal":
                return props
            props.append(ev)
            if not ev.get("auto"):
                g.send({"type": "decision", "id": ev["id"], "digest": ev["digest"], "decision": answers.pop(0)})

    def audit(self):
        home = os.path.join(self.tmp, "home", "hoplon")
        with open(os.path.join(home, "audit.log")) as f:
            return [json.loads(x) for x in f]


class TestApprovedFiles(Edits):
    def test_second_edit_to_the_same_file_applies_without_asking(self):
        turns = [edit("a.txt", "one", "two"), reply("ok"),
                 edit("a.txt", "two", "three"), reply("ok"),
                 edit("b.txt", "bee", "BEE"), reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            self.put("a.txt", "one\n")
            self.put("b.txt", "bee\n")
            (p,) = self.turn(g, ["accept"])
            self.assertFalse(p.get("auto"))
            self.assertEqual(self.get("a.txt"), "two\n")
            (p,) = self.turn(g)
            self.assertTrue(p["auto"] and p["approved_file"])
            self.assertIn("+three", p["diff"])                 # still shown with its diff
            self.assertEqual(self.get("a.txt"), "three\n")
            (p,) = self.turn(g, ["decline"])                  # another file asks
            self.assertFalse(p.get("auto"))
            self.assertEqual(self.get("b.txt"), "bee\n")
            g.close()
        kinds = [e["event"] for e in self.audit()]
        self.assertIn("edits", kinds)
        self.assertIn("edits_remember", kinds)
        self.assertIn({"decision": "accept", "by": "approved_file"},
                      [{k: e.get(k) for k in ("decision", "by")} for e in self.audit() if e["event"] == "decision"])
        ops = [e for e in self.audit() if e["event"] == "file_op"]
        self.assertEqual([bool(e.get("approved_file")) for e in ops], [False, True])

    def test_off_by_default(self):
        turns = [edit("a.txt", "one", "two"), reply("ok"), edit("a.txt", "two", "three"), reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api, on=False)
            self.assertEqual(self.hello["edits"], {"mode": "ask", "files": []})
            self.assertIn("edits", self.hello["features"])
            self.put("a.txt", "one\n")
            self.turn(g, ["accept"])
            (p,) = self.turn(g, ["accept"])
            self.assertFalse(p.get("auto"))
            g.close()

    def test_a_decline_takes_the_file_out_of_the_set(self):
        turns = [edit("a.txt", "one", "two"), reply("ok"), edit("a.txt", "two", "x"), reply("ok"),
                 edit("a.txt", "two", "y"), reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            self.put("a.txt", "one\n")
            self.turn(g, ["accept"])
            self.assertEqual([e for e in g.events if e["type"] == "edits"][-1]["files"], ["a.txt"])
            g.send({"type": "edits", "reset": True})          # reset, then approve again by hand
            self.assertEqual(g.until("edits")["files"], [])
            (p,) = self.turn(g, ["decline"])
            self.assertFalse(p.get("auto"))
            (p,) = self.turn(g, ["accept"])
            self.assertFalse(p.get("auto"))
            self.assertEqual(self.get("a.txt"), "y\n")
            g.close()

    def test_declining_an_approved_file_forgets_it(self):
        turns = [edit("a.txt", "one", "two"), reply("ok"),
                 edit("a.txt", "two", SECRET), reply("ok"),
                 edit("a.txt", "two", "three"), reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            self.put("a.txt", "one\n")
            self.turn(g, ["accept"])
            (p,) = self.turn(g, ["decline"])                  # looks like a secret: asks, and is declined
            self.assertFalse(p.get("auto"))
            (p,) = self.turn(g, ["accept"])                  # so the file asks again
            self.assertFalse(p.get("auto"))
            g.close()
        self.assertIn("edits_forget", [e["event"] for e in self.audit()])

    def test_secret_hidden_and_new_files_always_ask(self):
        turns = [edit("a.txt", "one", "two"), reply("ok"),
                 edit("a.txt", "two", SECRET), reply("ok"),
                 edit(".cfg", "x", "y"), reply("ok"), edit(".cfg", "y", "z"), reply("ok"),
                 call("write_file", path="new.txt", content="hi\n", reason="r"), reply("ok"),
                 call("write_file", path="new.txt", content="hello\n", reason="r"), reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            self.put("a.txt", "one\n")
            self.put(".cfg", "x\n")
            self.turn(g, ["accept"])
            (p,) = self.turn(g, ["decline"])                 # the new text looks like a secret
            self.assertFalse(p.get("auto"))
            self.turn(g, ["accept"])
            (p,) = self.turn(g, ["accept"])                  # hidden
            self.assertFalse(p.get("auto"))
            (p,) = self.turn(g, ["accept"])                  # a new file
            self.assertFalse(p.get("auto"))
            (p,) = self.turn(g)                              # now it exists, and was approved
            self.assertTrue(p.get("auto"))
            self.assertEqual(self.get("new.txt"), "hello\n")
            g.close()

    def test_a_new_process_asks_again(self):
        turns = [edit("a.txt", "one", "two"), reply("ok")]
        with FakeAPI(turns + [edit("a.txt", "two", "three"), reply("ok")]) as api:
            g = self.start(api)
            self.put("a.txt", "one\n")
            self.turn(g, ["accept"])
            g.close()
            g = T.Proc(os.path.join(self.tmp, "home", "hoplon"), self.wd, ["-e", "test"])
            g.until("hello")
            g.send({"type": "edits", "mode": "approved-files"})
            g.until("edits")
            (p,) = self.turn(g, ["accept"])
            self.assertFalse(p.get("auto"))
            g.close()

    def test_bad_messages(self):
        with FakeAPI([]) as api:
            g = self.start(api, on=False)
            g.send({"type": "edits", "mode": "always"})
            self.assertEqual(g.until("error")["code"], "bad_message")
            g.close()


class TestProfileAndRepl(T.Tmp):
    def test_profile_field(self):
        p = T.G.Profile({"name": "p", "edits": "approved-files"}, "p.json")
        self.assertEqual(p.edits, "approved-files")
        self.assertIn("edits: approved-files", p.summary()[0])
        self.assertEqual(T.G.Profile({"name": "p"}, "p.json").edits, "ask")
        with self.assertRaises(T.G.HoplonError):
            T.G.Profile({"name": "p", "edits": "yes"}, "p.json")

    def test_repl_command(self):
        import io
        from unittest import mock
        cfg = T.G.Config(self.path("home", "hoplon"))
        cfg.ensure()
        ep = T.G.Endpoint({"name": "t", "base_url": "https://llm.invalid/v1", "model": "m"}, "t.json")
        eng = T.G.Engine(cfg, ep, T.G.Session(cfg), T.G.PlainUI())
        repl, out = T.G.Repl(eng), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()):
            repl.command(":edits")
            repl.command(":edits approved-files")
            eng.approved_files.add("src/a.py")
            repl.command(":edits")
            repl.command(":edits reset")
            repl.command(":edits sometimes")
            repl.command(":edits ask")
        text = out.getvalue()
        self.assertIn("edits: ask", text)
        self.assertIn("applied without asking: src/a.py", text)
        self.assertIn("applied without asking: none yet", text)
        self.assertIn("usage: :edits", text)
        self.assertEqual(eng.edits, "ask")



class TestOwnPlaces(unittest.TestCase):
    """An approved file never makes an edit in hoplon's own config or state, or where coding mode is refused, apply
    without asking: the model could raise its own limits or turn things on there (found by an AISVS C9 audit)."""

    def test_own_places_always_ask(self):
        import tempfile
        from unittest import mock
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="hoplon-own-"))
        cfg_home, state, home = (os.path.join(tmp, d) for d in ("cfg", "state", "home"))
        for d in (cfg_home + "/hoplon", state + "/hoplon", home + "/.ssh", home + "/proj"):
            os.makedirs(d)
        os.symlink(cfg_home + "/hoplon", home + "/proj/link")
        env = {"XDG_CONFIG_HOME": cfg_home, "XDG_STATE_HOME": state, "HOME": home}
        with mock.patch.dict(os.environ, env):
            eng = type("E", (), {"cfg": type("C", (), {"home": os.path.join(tmp, "other")})()})()
            own = lambda p: T.G.Engine.own_place(eng, p)        # noqa: E731
            for path in (cfg_home + "/hoplon/config.json", cfg_home + "/hoplon/blessed.json",
                         state + "/hoplon/audit.log", tmp + "/other/config.json", home + "/.ssh/config",
                         home + "/.bashrc", home + "/proj/link/config.json"):
                self.assertTrue(own(path), path)
            self.assertFalse(own(home + "/proj/a.txt"))


if __name__ == "__main__":
    unittest.main()
