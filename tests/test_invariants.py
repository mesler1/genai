"""Invariants the approval gate keeps for every kind of proposal, checked over the whole table (PROPOSAL_KINDS),
so a kind added later is held to them too. SECURITY.md describes the rules; these tests hold the parts that
must not depend on any one kind's code. Run: python3 -m unittest discover -s tests"""

import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_coding_guard import Guard, Ui  # noqa: E402
from test_genai import G  # noqa: E402


def proposal(kind, n=1):
    """A proposal of this kind with the fields the gate itself reads; the handlers are not reached."""
    return G.Proposal(f"p{n}", kind, f"c{n}", command="true", path="a.txt", steps=[], tool="t", goal="g",
                      items=["i"], summary="s", reason="r")


class TestTable(unittest.TestCase):
    def test_every_tool_genai_offers_has_a_kind(self):
        offered = [t["function"]["name"] for t in G.TOOLS + [G.SEARCH_TOOL, G.RUN_TOOL, G.WORK_PLAN_TOOL, G.FINISH_TOOL]]
        self.assertEqual(sorted(set(offered)), sorted(G.INTRINSICS))
        self.assertEqual(sorted(G.BY_TOOL), sorted(G.INTRINSICS))

    def test_every_kind_names_methods_that_exist(self):
        for k in G.PROPOSAL_KINDS.values():
            with self.subTest(kind=k.name):
                self.assertTrue(callable(getattr(G.Engine, k.build, None)), k.build)
                self.assertTrue(callable(getattr(G.Engine, k.perform, None)), k.perform)

    def test_a_tool_not_in_the_table_is_invalid(self):
        eng = mock.Mock(external={}, counter=0)
        eng.trusted_tools.return_value = {}
        p = G.Engine.make_proposal(eng, {"id": "c1", "function": {"name": "rm_rf", "arguments": "{}"}})
        self.assertEqual((p.kind, p.error), ("invalid", "unknown tool 'rm_rf'"))


class TestNothingRunsUnaccepted(unittest.TestCase):
    """A kind's handler is reached without an accept only if the kind is shown (never run) or answered by the
    wrapper (which carries its own answer)."""

    def test_undecided_and_declined_never_reach_the_handler(self):
        for k in G.PROPOSAL_KINDS.values():
            if k.shown or k.answered:
                continue
            for decision in (None, "decline", "ok", "yes"):
                with self.subTest(kind=k.name, decision=decision):
                    p = proposal(k.name)
                    p.decision = decision
                    with mock.patch.object(G.Engine, k.perform, side_effect=AssertionError("ran")):
                        self.assertEqual(G.Engine.perform(mock.Mock(), p), "declined by the user")

    def test_shown_kinds_never_run_anything(self):
        for k in G.PROPOSAL_KINDS.values():
            if k.shown:
                with self.subTest(kind=k.name):
                    self.assertFalse(k.answered or k.bulk)
                    self.assertIn("not run", G.Engine.perform(mock.Mock(), proposal(k.name)))


class TestGate(Guard):
    def setUp(self):
        super().setUp()
        self.proj = self.mkdir(self.home, "proj")
        with open(os.path.join(self.proj, "a.txt"), "w") as f:
            f.write("hello\n")

    def logged(self, eng):
        records = []
        eng.audit.log = lambda event, **f: records.append((event, f))
        return records

    def test_every_automatic_approval_is_audited_before_it_is_accepted(self):
        eng, ui = self.engine(self.proj, coding="--coding")
        records = self.logged(eng)
        kinds = [k for k in G.PROPOSAL_KINDS.values() if not k.shown]
        props = [proposal(k.name, n) for n, k in enumerate(kinds, 1)]
        for p in props:
            p.auto = True
        seen = []

        def show_auto(p):
            self.assertIsNone(seen.append(p.id))
            self.assertIn(("proposal", p.id, True),
                          [(e, f.get("id"), f.get("auto")) for e, f in records if e == "proposal"])
        ui.show_auto = show_auto
        eng.decide(props)
        self.assertEqual(seen, [p.id for p in props])
        self.assertTrue(all(p.decision == "accept" for p in props))

    def test_every_proposal_is_audited_whether_or_not_it_asks(self):
        eng, ui = self.engine(self.proj, coding="--coding")
        records = self.logged(eng)
        props = [proposal(k, n) for n, k in enumerate(G.PROPOSAL_KINDS, 1)]
        ui.decide = lambda pending: None
        eng.decide(props)
        self.assertEqual(sorted(f["id"] for e, f in records if e == "proposal"), sorted(p.id for p in props))

    def test_end_of_input_declines_every_kind_that_asks(self):
        eng, _ = self.engine(self.proj, coding="--coding")
        self.logged(eng)
        ui = G.JsonUI(inp=io.StringIO(""), out=io.StringIO())
        eng.ui = ui
        asking = [k for k in G.PROPOSAL_KINDS.values() if not k.shown]
        props = [proposal(k.name, n) for n, k in enumerate(asking, 1)]
        eng.decide(props)
        for p in props:
            with self.subTest(kind=p.kind):
                if p.kind == "external":
                    self.assertNotIn(p.decision, ("ok", "accept"))
                else:
                    self.assertEqual(p.decision, "decline")

    def test_a_real_call_of_each_automatic_file_kind_is_audited(self):
        eng, ui = self.engine(self.proj, coding="--coding")
        records = self.logged(eng)
        for name, args in (("read_file", {"path": "a.txt"}), ("list_dir", {}),
                           ("search_files", {"pattern": "hello"})):
            with self.subTest(tool=name):
                p, out = self.run_call(eng, name, **args)
                self.assertTrue(p.auto)
                rec = [f for e, f in records if e == "proposal" and f["id"] == p.id]
                self.assertEqual(len(rec), 1)
                self.assertTrue(rec[0]["auto"])


if __name__ == "__main__":
    unittest.main()
