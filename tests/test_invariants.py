"""Invariants the approval gate keeps for every kind of proposal, checked over the whole table (PROPOSAL_KINDS),
so a kind added later is held to them too. SECURITY.md describes the rules; these tests hold the parts that
must not depend on any one kind's code. Run: python3 -m unittest discover -s tests"""

import io
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_coding_guard import Guard, Ui  # noqa: E402
from test_hoplon import G  # noqa: E402


def proposal(kind, n=1):
    """A proposal of this kind with the fields the gate itself reads; the handlers are not reached."""
    return G.Proposal(f"p{n}", kind, f"c{n}", command="true", path="a.txt", steps=[], tool="t", goal="g",
                      items=["i"], summary="s", reason="r",
                      what="w", files=["f.log"], suggest="", folder="/f", names=["s"], about=[""],
                      exists=False, lines=1, content="x\n", diff="", size=1, set="s", argv="true", access="read")


def source(*parts):
    """hoplon's source: the parts it is joined from (hoplon_core/), all of them unless named."""
    root = os.path.join(os.path.dirname(os.path.abspath(G.__file__)), "hoplon_core")
    out = []
    for name in parts or G.PARTS:
        with open(os.path.join(root, f"{name}.py")) as f:
            out.append(f.read())
    return "\n".join(out)


class TestTable(unittest.TestCase):
    def test_every_tool_hoplon_offers_has_a_kind(self):
        offered = [t["function"]["name"] for t in G.TOOLS + [G.SEARCH_TOOL, G.RUN_TOOL, G.WORK_PLAN_TOOL, G.FINISH_TOOL, G.RETRY_TOOL,
                                                    G.SAMPLES_TOOL, G.SKILLS_TOOL]]
        self.assertEqual(sorted(set(offered)), sorted(G.INTRINSICS))
        self.assertEqual(sorted(G.BY_TOOL), sorted(G.INTRINSICS))

    def test_every_kind_names_methods_that_exist(self):
        for k in G.PROPOSAL_KINDS.values():
            with self.subTest(kind=k.name):
                self.assertTrue(callable(getattr(G.Engine, k.build, None)), k.build)
                self.assertTrue(callable(getattr(G.Engine, k.perform, None)), k.perform)

    def test_every_kind_names_the_request_it_makes_or_none(self):
        # A kind that acts outside hoplon makes one of the four gate requests. The kinds that make none are listed
        # here by name, so a new kind has to be placed on purpose.
        none = {"command", "samples", "skills", "plan", "work_plan", "finish", "retry", "external"}
        for k in G.PROPOSAL_KINDS.values():
            with self.subTest(kind=k.name):
                if k.name in none:
                    self.assertIsNone(k.request)
                else:
                    self.assertIn(k.request, G.GATE_REQUESTS)
        self.assertEqual(sorted({k.request for k in G.PROPOSAL_KINDS.values()} - {None}), sorted(G.GATE_REQUESTS))

    def test_every_request_names_a_handler_that_exists(self):
        for name, (what, handler) in G.GATE_REQUESTS.items():
            with self.subTest(request=name):
                cls, meth = handler.split(".")
                self.assertTrue(callable(vars(getattr(G, cls)).get(meth)), handler)     # its own, not inherited
                self.assertTrue(what)

    def test_every_rule_is_known_and_used(self):
        used = [r for k in G.PROPOSAL_KINDS.values() for r in k.rules]
        self.assertLessEqual(set(used), set(G.AUTO_RULES))
        self.assertEqual(set(used), set(G.AUTO_RULES))      # a rule no kind lists is dead, or a kind forgot it
        for k in G.PROPOSAL_KINDS.values():
            if k.shown or k.answered:
                with self.subTest(kind=k.name):
                    self.assertEqual(k.rules, ())           # nothing to approve: shown, or the wrapper answers

    def test_only_grant_makes_a_proposal_automatic(self):
        # Anywhere else, setting .auto on a proposal would be an approval no rule accounts for and no record says.
        src = source()
        grant = re.search(r"\n    def grant\(self, p, rule\):\n(?:        .*\n|\n)+", src).group(0)
        rest = src.replace(grant, "")
        hits = [m.group(0) for m in re.finditer(r"\b(\w+)\.auto\s*(?:,[^=\n]*)?=(?!=)[^\n]*", rest)
                if m.group(1) not in ("c", "self")]   # TrustedCommand.auto (c, self) is what a set allows, not a grant
        self.assertEqual(hits, [])
        self.assertIn("self.auto = False", src)          # a Proposal starts asking

    def test_standing_is_the_one_place_a_rule_approves(self):
        # grant() is called once, in decide, with what standing() answered; and standing() names every rule.
        src = source()
        self.assertEqual(src.count("self.grant("), 1)
        decide = src[src.index("    def decide(self, props):"):src.index("    def match_plan(self, p):")]
        self.assertIn("rule = self.standing(p)\n                if rule:\n                    self.grant(p, rule)", decide)
        standing = src[src.index("    def standing(self, p):"):src.index("    def grant(self, p, rule):")]
        code = standing[standing.index('"""', standing.index('"""') + 3) + 3:]      # after the docstring
        for rule in G.AUTO_RULES:
            with self.subTest(rule=rule):
                self.assertIn(f'"{rule}"', code)

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

    def test_every_automatic_approval_is_audited_with_its_rule_before_it_is_accepted(self):
        eng, ui = self.engine(self.proj, coding="--coding")
        records = self.logged(eng)
        props = [(proposal(k.name, n), rule) for n, (k, rule) in
                 enumerate(((k, r) for k in G.PROPOSAL_KINDS.values() for r in k.rules), 1)]
        rules = {p.id: rule for p, rule in props}
        seen = []

        def show_auto(p):
            self.assertIsNone(seen.append(p.id))
            self.assertIn(("proposal", p.id, True),
                          [(e, f.get("id"), f.get("auto")) for e, f in records if e == "proposal"])
            self.assertIn(("decision", p.id, "accept", p.rule),
                          [(e, f.get("id"), f.get("decision"), f.get("by")) for e, f in records])
        ui.show_auto = show_auto
        with mock.patch.object(eng, "standing", lambda p: rules[p.id]):     # decide grants what standing answers
            eng.decide([p for p, _ in props])
        self.assertEqual(seen, [p.id for p, _ in props])
        self.assertTrue(all(p.decision == "accept" and p.rule == rule for p, rule in props))

    def test_a_rule_approves_only_the_kinds_that_list_it(self):
        eng, _ = self.engine(self.proj, coding="--coding")
        for k in G.PROPOSAL_KINDS.values():
            for rule in set(G.AUTO_RULES) - set(k.rules):
                with self.subTest(kind=k.name, rule=rule):
                    p = proposal(k.name)
                    with self.assertRaises(RuntimeError):
                        eng.grant(p, rule)
                    self.assertFalse(p.auto)

    def test_end_of_input_at_the_terminal_declines_every_kind_that_asks(self):
        eng, _ = self.engine(self.proj, coding="--coding")
        self.logged(eng)
        eng.ui = G.TtyUI(out=io.StringIO())
        asking = [k for k in G.PROPOSAL_KINDS.values() if not k.shown and k.name != "external"]
        props = [proposal(k.name, n) for n, k in enumerate(asking, 1)]
        with mock.patch("sys.stdin", io.StringIO("")), mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("sys.stderr", io.StringIO()):
            with mock.patch.object(eng, "standing", lambda p: None):     # none is approved by a rule
                eng.decide(props)
        for p in props:
            with self.subTest(kind=p.kind):
                self.assertEqual(p.decision, "decline")

    def test_every_proposal_is_audited_whether_or_not_it_asks(self):
        eng, ui = self.engine(self.proj, coding="--coding")
        records = self.logged(eng)
        props = [proposal(k, n) for n, k in enumerate(G.PROPOSAL_KINDS, 1)]
        ui.decide = lambda pending: None
        with mock.patch.object(eng, "standing", lambda p: None):     # none is approved by a rule
            eng.decide(props)
        self.assertEqual(sorted(f["id"] for e, f in records if e == "proposal"), sorted(p.id for p in props))

    def test_end_of_input_declines_every_kind_that_asks(self):
        eng, _ = self.engine(self.proj, coding="--coding")
        self.logged(eng)
        ui = G.JsonUI(inp=io.StringIO(""), out=io.StringIO())
        eng.ui = ui
        asking = [k for k in G.PROPOSAL_KINDS.values() if not k.shown]
        props = [proposal(k.name, n) for n, k in enumerate(asking, 1)]
        with mock.patch.object(eng, "standing", lambda p: None):     # none is approved by a rule
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
                self.assertIn({"id": p.id, "decision": "accept", "by": "coding_read"},
                              [f for e, f in records if e == "decision"])


if __name__ == "__main__":
    unittest.main()


class TestFrontCommands(unittest.TestCase):
    """The commands both front ends have are one table: each is checked and done in one place."""

    # A form of each command as the REPL takes it. Its fields must be ones the JSON check accepts, so the two
    # front ends can't drift into taking different things.
    FORMS = {"coding": "on", "edits": "reset", "network": "", "fixtures": "add a.txt", "leftovers": "",
             "skills": "", "skill": "x on", "command_sets": "", "command_set": "x off", "models": "",
             "model": "m", "clear": "", "compact": "-e"}

    def test_each_repl_form_is_a_message_json_accepts(self):
        self.assertEqual(sorted(self.FORMS), sorted(G.FRONT_COMMANDS))
        for name, c in G.FRONT_COMMANDS.items():
            with self.subTest(command=name):
                m = c.words(self.FORMS[name])
                self.assertIsNotNone(m)
                self.assertTrue(c.check(m), m)
                if name != "model":         # any word names a model
                    self.assertIsNone(c.words("no such form at all"))

    def test_neither_front_end_handles_a_shared_command_itself(self):
        src = source()
        repl = src[src.index("    def command(self, line):"):src.index("    def do_attach(self, path):")]
        js = src[src.index("def run_json("):src.index("def running_bundle(")]
        for name in G.FRONT_COMMANDS:
            with self.subTest(command=name):
                self.assertNotIn(f't == "{name}"', js)
                if name != "model":         # :model alone shows the model, which is the REPL's own
                    self.assertNotIn(f'cmd == "{name}"', repl)
        self.assertIn("if t in FRONT_COMMANDS:", js)
        self.assertIn("if cmd in FRONT_COMMANDS", repl)
        self.assertIn("json_command(FRONT_COMMANDS[t]", js)
        self.assertIn("repl_command(FRONT_COMMANDS[cmd]", repl)
