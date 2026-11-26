"""Offline tests for how file proposals are approved and applied. Run: python3 -m unittest discover -s tests"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_hoplon as T  # noqa: E402
from fakeapi import FakeAPI, call, calls, reply  # noqa: E402

GENERIC = "declined by the user"


class Approvals(T.Tmp):
    def start(self, api, *args):
        home, path = T.make_home(self.tmp, api.url)
        T.bless_path(home, path)
        self.wd = self.path("wd")
        os.makedirs(self.wd, exist_ok=True)
        return T.Proc(home, self.wd, ["-e", "test"] + list(args))

    def put(self, name, text):
        with open(os.path.join(self.wd, name), "w") as f:
            f.write(text)

    def get(self, name):
        with open(os.path.join(self.wd, name)) as f:
            return f.read()

    def tool_results(self, api):
        return [m["content"] for m in api.requests[-1]["body"]["messages"] if m["role"] == "tool"]

    def edit_once(self, turn, *args, setup=()):
        """Run one edit proposal that cannot be previewed; return (tool result, events)."""
        self.setUp()                                    # a fresh directory and config per run
        with FakeAPI([turn, reply("ok")]) as api:
            g = self.start(api, *args)
            for name, text in setup:
                self.put(name, text)
            g.send({"type": "user", "text": "edit"})
            g.until("done")
            g.close()
            self.assertFalse([e for e in g.events if e["type"] == "proposal"])
            (result,) = self.tool_results(api)
            return result, g.events


class TestEditDisclosure(Approvals):
    def mismatches(self, path):
        return [call("edit_file", path=path, old_text="nope", new_text="x", reason="r"),     # not found
                call("edit_file", path=path, old_text="a", new_text="x", reason="r")]        # ambiguous

    def test_hidden_file_with_coding_off_gives_only_the_generic_result(self):
        for turn in self.mismatches(".env"):
            result, events = self.edit_once(turn, setup=[(".env", "TOKEN=aaa\n")])
            self.assertEqual(result, GENERIC)
            notes = [e["message"] for e in events if e["type"] == "notice"]
            self.assertTrue(any("not applied" in n and "appears" in n for n in notes), notes)  # the user is told

    def test_missing_hidden_file_gives_the_same_result(self):
        result, _ = self.edit_once(call("edit_file", path=".nope", old_text="a", new_text="b", reason="r"))
        self.assertEqual(result, GENERIC)

    def test_plain_file_with_coding_off_gives_only_the_generic_result(self):
        result, _ = self.edit_once(self.mismatches("a.txt")[1], setup=[("a.txt", "aaa\n")])
        self.assertEqual(result, GENERIC)

    def test_hidden_file_with_coding_on_gives_only_the_generic_result(self):
        result, _ = self.edit_once(self.mismatches(".env")[0], "--coding", setup=[(".env", "TOKEN=aaa\n")])
        self.assertEqual(result, GENERIC)

    def test_authorized_read_keeps_the_detail(self):
        result, _ = self.edit_once(self.mismatches("a.txt")[1], "--coding", setup=[("a.txt", "aaa\n")])
        self.assertIn("appears 3 times in a.txt", result)
        result, _ = self.edit_once(self.mismatches("a.txt")[0], "--coding", setup=[("a.txt", "aaa\n")])
        self.assertIn("appears 0 times in a.txt", result)

    def test_edit_that_can_be_previewed_still_asks(self):
        with FakeAPI([call("edit_file", path=".env", old_text="aaa", new_text="bbb", reason="r"), reply("ok")]) as api:
            g = self.start(api)
            self.put(".env", "TOKEN=aaa\n")
            g.send({"type": "user", "text": "edit"})
            p = g.until("proposal")
            self.assertEqual(p["kind"], "edit")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            g.close()
            self.assertEqual(self.get(".env"), "TOKEN=bbb\n")


class TestStaleApprovals(Approvals):
    def test_two_writes_to_one_path_second_is_refused(self):
        turns = [calls(("write_file", {"path": "x.txt", "content": "first\n", "reason": "r"}),
                       ("write_file", {"path": "x.txt", "content": "second\n", "reason": "r"})),
                 reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "go"})
            p1, p2 = g.until("proposal"), g.until("proposal")
            for p in (p1, p2):
                g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            g.close()
            self.assertEqual(self.get("x.txt"), "first\n")
            first, second = self.tool_results(api)
            self.assertIn("wrote x.txt", first)
            self.assertEqual(second, "not applied: x.txt changed since the user reviewed your proposal; "
                                     "propose the change again")
            results = [e for e in g.events if e["type"] == "result"]
            self.assertEqual([r["status"] for r in results], ["ok", "error"])
            self.assertIn("changed after you reviewed it", results[1]["detail"])

    def test_write_to_file_modified_after_preview_is_refused(self):
        with FakeAPI([call("write_file", path="x.txt", content="mine\n", reason="r"), reply("ok")]) as api:
            g = self.start(api)
            self.put("x.txt", "old\n")
            g.send({"type": "user", "text": "go"})
            p = g.until("proposal")
            self.put("x.txt", "edited in an editor\n")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            g.close()
            self.assertEqual(self.get("x.txt"), "edited in an editor\n")
            self.assertIn("changed since the user reviewed", self.tool_results(api)[0])

    def test_new_file_that_appears_after_preview_is_refused(self):
        with FakeAPI([call("write_file", path="x.txt", content="mine\n", reason="r"), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "go"})
            p = g.until("proposal")
            self.put("x.txt", "someone else\n")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            g.close()
            self.assertEqual(self.get("x.txt"), "someone else\n")
            self.assertIn("changed since the user reviewed", self.tool_results(api)[0])

    def test_edit_of_file_modified_after_preview_is_refused(self):
        with FakeAPI([call("edit_file", path="x.txt", old_text="one", new_text="two", reason="r"), reply("ok")]) as api:
            g = self.start(api)
            self.put("x.txt", "one\n")
            g.send({"type": "user", "text": "go"})
            p = g.until("proposal")
            self.put("x.txt", "one\nplus a line\n")           # old_text still matches once; the file differs
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            g.close()
            self.assertEqual(self.get("x.txt"), "one\nplus a line\n")
            self.assertIn("changed since the user reviewed", self.tool_results(api)[0])

    def test_unchanged_target_is_applied(self):
        with FakeAPI([call("write_file", path="x.txt", content="new\n", reason="r"), reply("ok")]) as api:
            g = self.start(api)
            self.put("x.txt", "old\n")
            g.send({"type": "user", "text": "go"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            g.until("done")
            g.close()
            self.assertEqual(self.get("x.txt"), "new\n")


if __name__ == "__main__":
    unittest.main()
