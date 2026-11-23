"""Tests for where coding mode is allowed, secret-looking files in coding mode, and what the model
may learn about the disk before an approval. Run: python3 -m unittest discover -s tests"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_genai import G, Tmp, make_home  # noqa: E402

GENERIC = "declined by the user"


class Ui(G.TtyUI):
    """A terminal front end that records what it is told instead of printing it."""

    def __init__(self):
        super().__init__(out=open(os.devnull, "w"))
        self.notices = []

    def notice(self, msg):
        self.notices.append(msg)

    def show_auto(self, p):
        pass

    def result(self, p, status, note):
        pass


class Guard(Tmp):
    def setUp(self):
        super().setUp()
        self.home = self.path("userhome")
        os.makedirs(self.home)
        self.xdg = self.path("xdg")
        env = mock.patch.dict(os.environ, {"HOME": self.home, "XDG_CONFIG_HOME": self.xdg})
        env.start()
        self.addCleanup(env.stop)
        self.cwd0 = os.getcwd()
        self.addCleanup(os.chdir, self.cwd0)
        self.gh, _ = make_home(self.tmp, "http://127.0.0.1:1/v1")
        self.cfg = G.Config(self.gh)

    def mkdir(self, *p):
        d = os.path.join(*p)
        os.makedirs(d, exist_ok=True)
        return d

    def engine(self, cwd, coding=None, session_coding=False, resumed=False, ui=None, anywhere=False):
        os.chdir(cwd)
        sess = G.Session(self.cfg)
        sess.coding, sess.resumed, sess.cwd = session_coding, resumed, cwd
        ui = ui or Ui()
        ep = G.Endpoint({"name": "t", "base_url": "http://127.0.0.1:1/v1", "model": "m"}, "t.json")
        return G.Engine(self.cfg, ep, sess, ui, client=object(), coding=coding, anywhere=anywhere), ui

    def run_call(self, eng, name, **args):
        """Propose, decide and perform one call; return (proposal, what the model is told)."""
        p = eng.make_proposal({"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}})
        eng.decide([p])
        return p, eng.perform(p)


class TestWhere(Guard):
    def refused(self, cwd):
        eng, _ = self.engine(cwd)
        with self.assertRaises(G.GenaiError) as cm:
            eng.set_coding(True)
        return str(cm.exception)

    def test_refused_places(self):
        places = [self.home, os.path.dirname(self.home), "/",
                  os.path.join(self.xdg, "genai"), os.path.join(self.xdg, "genai", "sessions")]
        for d in (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store", ".config/gcloud",
                  ".ssh/keys", ".config/gcloud/configurations"):
            places.append(os.path.join(self.home, d))
        for d in places:
            self.mkdir(d)
            self.assertIn("coding mode is refused", self.refused(d), d)

    def test_message_names_the_rule(self):
        self.assertIn("home directory", self.refused(self.home))
        self.assertIn("~/.ssh", self.refused(self.mkdir(self.home, ".ssh")))
        self.assertIn("config directory", self.refused(self.mkdir(self.xdg, "genai")))

    def test_allowed_places(self):
        for d in (os.path.join(self.home, ".sshkeys"), os.path.join(self.home, "project"),
                  os.path.join(self.home, ".config", "other"), os.path.join(self.xdg, "genai-x"),
                  self.path("elsewhere")):
            self.mkdir(d)
            eng, _ = self.engine(d)
            eng.set_coding(True)
            self.assertTrue(eng.session.coding, d)

    def test_component_not_string_prefix(self):
        os.rename(self.home, self.path("alice"))
        self.mkdir(self.path("al"))
        with mock.patch.dict(os.environ, {"HOME": self.path("alice")}):
            self.assertIsNone(G.coding_refusal(self.path("al")))

    def test_every_refusal_has_a_stated_risk(self):
        for d in (self.home, os.path.dirname(self.home), self.mkdir(self.xdg, "genai"), self.mkdir(self.home, ".aws")):
            self.assertIn(G.coding_refusal(d)[1], G.CODING_RISKS, d)

    def test_real_path_is_compared(self):
        ssh = self.mkdir(self.home, ".ssh")
        link = self.path("innocent")
        os.symlink(ssh, link)
        self.assertIn("coding mode is refused", self.refused(link))

    def test_flag_errors_but_profile_gets_a_notice(self):
        ssh = self.mkdir(self.home, ".ssh")
        with self.assertRaises(G.GenaiError):
            self.engine(ssh, coding="--coding")
        eng, ui = self.engine(ssh, coding="profile coder")
        self.assertFalse(eng.session.coding)
        self.assertTrue(any("asks for coding mode" in n and "~/.ssh" in n for n in ui.notices), ui.notices)

    def test_resumed_session_is_checked(self):
        ssh = self.mkdir(self.home, ".ssh")
        eng, ui = self.engine(ssh, session_coding=True, resumed=True)
        self.assertFalse(eng.session.coding)
        self.assertIsNone(eng.coding_from)
        self.assertTrue(any("refused" in n for n in ui.notices), ui.notices)
        proj = self.mkdir(self.home, "proj")
        eng, ui = self.engine(proj, session_coding=True, resumed=True)
        self.assertTrue(eng.session.coding)
        self.assertEqual(ui.notices, [])


class TestOverride(Guard):
    """--coding-anywhere turns coding mode on where it is refused, and says what that exposes."""

    def test_refusal_names_the_override(self):
        eng, _ = self.engine(self.home)
        with self.assertRaises(G.GenaiError) as cm:
            eng.set_coding(True)
        self.assertIn("--coding-anywhere", str(cm.exception))

    def test_override_turns_it_on_and_states_the_risk(self):
        for d, phrase, risk in ((self.home, "your home directory", "every file under your home directory"),
                                (self.mkdir(self.home, ".ssh"), "~/.ssh", "credentials kept here"),
                                (self.mkdir(self.xdg, "genai"), "config directory", "every saved session")):
            eng, ui = self.engine(d, coding="--coding-anywhere", anywhere=True)
            self.assertTrue(eng.session.coding, d)
            self.assertEqual(eng.coding_from, "--coding-anywhere")
            self.assertIn(phrase, eng.coding_override)
            warning = [n for n in ui.notices if "--coding-anywhere" in n]
            self.assertEqual(len(warning), 1, ui.notices)
            for part in (phrase, risk, "normally refused", "http://127.0.0.1:1/v1", "every write still ask"):
                self.assertIn(part, " ".join(warning[0].split()), d)

    def test_override_is_logged(self):
        self.engine(self.home, coding="--coding-anywhere", anywhere=True)
        with open(os.path.join(self.gh, "audit.log")) as f:
            recs = [json.loads(line) for line in f]
        self.assertEqual([r["override"] for r in recs if r.get("event") == "coding"], ["your home directory"])

    def test_no_warning_where_it_was_allowed_anyway(self):
        eng, ui = self.engine(self.mkdir(self.home, "proj"), coding="--coding-anywhere", anywhere=True)
        self.assertTrue(eng.session.coding)
        self.assertIsNone(eng.coding_override)
        self.assertEqual(ui.notices, [])

    def test_a_profile_cannot_override(self):
        eng, ui = self.engine(self.home, coding="profile coder")
        self.assertFalse(eng.session.coding)
        self.assertIsNone(eng.coding_override)

    def test_resumed_session_needs_the_flag_again(self):
        eng, ui = self.engine(self.home, session_coding=True, resumed=True)
        self.assertFalse(eng.session.coding)
        eng, ui = self.engine(self.home, coding="--coding-anywhere", session_coding=True, resumed=True, anywhere=True)
        self.assertTrue(eng.session.coding)
        self.assertTrue(any("--coding-anywhere" in n for n in ui.notices), ui.notices)

    def test_secret_looking_file_still_asks(self):
        ssh = self.mkdir(self.home, ".ssh")
        with open(os.path.join(ssh, "id_rsa"), "w") as f:
            f.write("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n")
        eng, ui = self.engine(ssh, coding="--coding-anywhere", anywhere=True)
        p = eng.make_proposal({"id": "c1", "function": {"name": "read_file", "arguments": json.dumps({"path": "id_rsa"})}})
        self.assertFalse(p.auto)
        self.assertTrue(p.fields.get("secret"))


class TestSecrets(Guard):
    def setUp(self):
        super().setUp()
        self.proj = self.mkdir(self.home, "proj")
        for name, text in (("key.txt", "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"),
                           ("conf.txt", "api_key=abcdef1234567890abcdef\n"), ("notes.txt", "just some notes\n")):
            with open(os.path.join(self.proj, name), "w") as f:
                f.write(text)

    def test_auto_read_sends_the_classified_snapshot(self):
        eng, _ = self.engine(self.proj, session_coding=True)
        call = {"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "notes.txt"}'}}
        proposal = eng.make_proposal(call)
        self.assertTrue(proposal.auto)
        with open(os.path.join(self.proj, "notes.txt"), "w") as f:
            f.write("token=example-new-token")
        proposal.decision = "accept"
        self.assertEqual(eng.perform(proposal), "just some notes\n")

    def test_secret_looking_file_needs_approval(self):
        eng, ui = self.engine(self.proj, session_coding=True)
        for name in ("key.txt", "conf.txt"):
            with mock.patch.object(G, "ask_yes", return_value=False) as ask:
                p, told = self.run_call(eng, "read_file", path=name, reason="r")
            ask.assert_called_once()
            self.assertFalse(p.auto)
            self.assertTrue(p.event()["secret"])
            self.assertEqual(told, GENERIC)
        self.assertTrue(any("key.txt looks like it contains secrets" in n for n in ui.notices), ui.notices)

    def test_approved_secret_file_is_read(self):
        eng, _ = self.engine(self.proj, session_coding=True)
        with mock.patch.object(G, "ask_yes", return_value=True):
            _, told = self.run_call(eng, "read_file", path="conf.txt", reason="r")
        self.assertIn("api_key=", told)

    def test_ordinary_file_is_read_without_asking(self):
        eng, ui = self.engine(self.proj, session_coding=True)
        with mock.patch.object(G, "ask_yes") as ask:
            p, told = self.run_call(eng, "read_file", path="notes.txt", reason="r")
        ask.assert_not_called()
        self.assertTrue(p.auto)
        self.assertNotIn("secret", p.event())
        self.assertEqual(told, "just some notes\n")
        self.assertEqual(ui.notices, [])


class TestDiskFacts(Guard):
    def setUp(self):
        super().setUp()
        self.proj = self.mkdir(self.home, "proj")
        with open(os.path.join(self.proj, "plainfile"), "w") as f:
            f.write("x\n")
        os.symlink("/etc", os.path.join(self.proj, "link"))

    def calls(self):
        return [("read_file", {"path": "plainfile/x", "reason": "r"}),
                ("read_file", {"path": "link/passwd", "reason": "r"}),
                ("write_file", {"path": "plainfile/x", "content": "c", "reason": "r"}),
                ("write_file", {"path": "link/x", "content": "c", "reason": "r"})]

    def test_model_hears_only_declined_when_reads_need_approval(self):
        eng, ui = self.engine(self.proj)
        for name, args in self.calls():
            with mock.patch.object(G, "ask_yes") as ask:
                p, told = self.run_call(eng, name, **args)
            ask.assert_not_called()
            self.assertEqual(told, GENERIC, (name, args))
            self.assertIn("symlink or not a directory", p.error)       # the user sees why
        self.assertEqual(len(ui.notices), len(self.calls()))

    def test_list_dir_asks_first_and_declined_is_generic(self):
        eng, _ = self.engine(self.proj)
        with mock.patch.object(G, "ask_yes", return_value=False) as ask:
            _, told = self.run_call(eng, "list_dir", path="plainfile/x", reason="r")
        ask.assert_called_once()
        self.assertEqual(told, GENERIC)

    def test_hidden_path_in_coding_mode_is_still_generic(self):
        eng, _ = self.engine(self.proj, session_coding=True)
        with open(os.path.join(self.proj, ".d"), "w") as f:
            f.write("x")
        _, told = self.run_call(eng, "read_file", path=".d/x", reason="r")
        self.assertEqual(told, GENERIC)

    def test_real_error_when_coding_mode_is_on(self):
        eng, _ = self.engine(self.proj, session_coding=True)
        for name, args in self.calls():
            p, told = self.run_call(eng, name, **args)
            self.assertIn("symlink or not a directory", told, (name, args))
            self.assertTrue(told.startswith("error: "))

    def test_argument_errors_still_reach_the_model(self):
        eng, _ = self.engine(self.proj)
        for name, args, text in (("read_file", {"path": "/etc/passwd"}, "absolute"),
                                 ("read_file", {"path": "../x"}, ".."),
                                 ("read_file", {"path": ""}, "non-empty"),
                                 ("write_file", {"path": "a"}, "content"),
                                 ("nope", {}, "unknown tool")):
            _, told = self.run_call(eng, name, **args)
            self.assertIn(text, told)


if __name__ == "__main__":
    unittest.main()
