"""Tests for plugins and trusted commands: the manifest's rules, blessing, the programs they may run, the checks on
what is run, the limits, and the model's and the user's ways of running them."""

import copy
import io
import json
import os
import re
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakeapi import FakeAPI, call, reply  # noqa: E402
from test_hoplon import G, Proc, Tmp, bless_path, make_home, write_json  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CMDS = {
    "queue": {"describe": "jobs of one user", "argv": ["fakeq", "-u", "{user}", "--fmt={fmt}"],
              "params": {"user": {"pattern": "^[a-z_][a-z0-9_-]{0,31}$", "describe": "a user name"},
                         "fmt": {"enum": ["short", "long"]}},
              "read_only": True, "min_interval": 0},
    "job": {"describe": "one job", "argv": ["fakeq", "job", "{id}"],
            "params": {"id": {"int": [1, 99999]}}, "read_only": True, "min_interval": 0},
    "kill": {"describe": "stops a job", "argv": ["fakeq", "kill", "{id}"],
             "params": {"id": {"int": [1, 99999]}}, "min_interval": 0},
    "slow": {"describe": "never ends", "argv": ["slowq"], "read_only": True, "timeout": 1, "min_interval": 0},
    "big": {"describe": "too much output", "argv": ["bigq"], "read_only": True, "max_output": 1000, "min_interval": 0},
    "leak": {"describe": "prints a secret", "argv": ["leakq"], "read_only": True, "min_interval": 0},
    "cached": {"describe": "cached", "argv": ["fakeq", "cached"], "read_only": True, "cache": 60, "min_interval": 0},
    "paced": {"describe": "paced", "argv": ["fakeq", "paced"], "read_only": True, "min_interval": 30},
}


def program(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)


class PluginBase(Tmp):
    """A plugin called 'fake' whose programs are shell scripts in a private directory that log what they get."""

    def setUp(self):
        super().setUp()
        self.bin = self.path("bin")
        os.mkdir(self.bin, 0o755)
        os.chmod(self.bin, 0o755)
        self.log = self.path("calls.log")
        program(os.path.join(self.bin, "fakeq"),
                f'for a in "$@"; do printf "ARG:%s\\n" "$a"; done >> {self.log}\n'
                f'/usr/bin/env >> {self.log}\necho "---" >> {self.log}\n'
                'echo "result $*"\necho "warning" >&2\nexit 3\n')
        program(os.path.join(self.bin, "slowq"), "/bin/sleep 30\n")
        program(os.path.join(self.bin, "bigq"), "/usr/bin/yes aaaaaaaaaaaaaaaaaaaa | /usr/bin/head -c 5000000\n")
        program(os.path.join(self.bin, "leakq"), "echo 'TOKEN=abcdefghijklmnop1234'\n")
        self.manifest = {"name": "fake", "description": "a fake plugin", "search": [self.bin], "env": ["FAKE_ALLOWED"],
                         "commands": copy.deepcopy(CMDS)}

    def plugin_dir(self, parent=None, name="fake"):
        d = os.path.join(parent or self.path("plugins"), name)
        os.makedirs(d, exist_ok=True)
        write_json(os.path.join(d, "plugin.json"), self.manifest)
        return d

    def load(self, **top):
        saved, self.manifest = self.manifest, dict(self.manifest, **top)
        try:
            return G.Plugin("fake", self.plugin_dir(self.path("loose")))
        finally:
            self.manifest = saved

    def calls(self):
        """What the fake program saw: a list of {args, env} for each run."""
        if not os.path.exists(self.log):
            return []
        runs = []
        for chunk in open(self.log).read().split("---\n"):
            if chunk.strip():
                lines = chunk.splitlines()
                runs.append({"args": [x[4:] for x in lines if x.startswith("ARG:")],
                             "env": dict(x.split("=", 1) for x in lines if "=" in x and not x.startswith("ARG:"))})
        return runs


def bless_plugin(home, path):
    G.Config(home).set_blessed(os.path.abspath(path), {"kind": "plugin", "name": os.path.basename(path),
                                                       "hash": G.hash_skill(path)[0]})


# ---------------------------------------------------------------- the manifest

class TestManifest(PluginBase):
    def refused(self, why, **top):
        pl = self.load(**top)
        self.assertIsNotNone(pl.error, why)
        self.assertIn(why, pl.error)

    def cmd(self, name="queue", **change):
        self.manifest["commands"][name].update(change)
        return {}

    def test_a_good_manifest_loads(self):
        pl = self.load()
        self.assertIsNone(pl.error)
        self.assertEqual(pl.commands["queue"].tool, "fake__queue")
        self.assertEqual(G.Plugin("fake", self.plugin_dir(self.path("again"))).hash, pl.hash)

    def test_the_slurm_plugin_in_the_repo_is_valid(self):
        pl = G.Plugin("slurm", os.path.join(REPO, "plugins", "slurm"))
        self.assertIsNone(pl.error)
        self.assertEqual(set(pl.commands), {"down_nodes", "nodes", "partitions", "queue", "queue_user",
                                            "queue_partition", "job", "node", "jobs_history_user"})
        self.assertTrue(all(c.read_only for c in pl.commands.values()))
        for c in pl.commands.values():          # resolved against a fake search directory: Slurm isn't installed here
            for prog in ("sinfo", "squeue", "scontrol", "sacct"):
                program(os.path.join(self.bin, prog), "true\n")
            c.search = [self.bin]
            c.resolve()
        job = pl.commands["job"]
        self.assertEqual(job.check({"id": "123_4"}), {"id": "123_4"})
        for bad in ("12;ls", "1 2", "", "x", "123_", "-1"):
            with self.assertRaises(ValueError, msg=bad):
                job.check({"id": bad})

    def test_name_rules(self):
        self.refused("name", name="Fake")
        self.refused("name", name="other")                      # not the directory's name
        self.manifest["commands"] = {"Bad": self.manifest["commands"]["queue"]}
        self.refused("commands", name="fake")
        self.assertIsNotNone(self.load(commands={}).error)

    def test_unknown_keys_anywhere(self):
        self.refused("unknown field(s): extra", extra=1)
        self.manifest["commands"]["queue"]["extra"] = 1
        self.refused("commands.queue: unknown field(s): extra")
        del self.manifest["commands"]["queue"]["extra"]
        self.manifest["commands"]["queue"]["params"]["user"]["extra"] = 1
        self.refused("commands.queue.params.user: unknown field(s): extra")

    def test_argv_rules(self):
        for argv, why in [([], "argv"), ("fakeq", "argv"), ([1], "argv"), (["a/b"], "argv[0]"),
                          (["../fakeq"], "argv[0]"), (["{user}"], "argv[0]"), (["fakeq", "{nope}"], "undeclared"),
                          (["fakeq", "{user"], "single '{'"), (["fakeq", "a}"], "single '}'"),
                          (["fakeq", "{user}", "{fmt}", "{"], "single '{'")]:
            self.manifest["commands"]["queue"]["argv"] = argv
            self.refused(why)

    def test_unused_param_and_literal_braces(self):
        self.manifest["commands"]["queue"]["argv"] = ["fakeq", "{user}"]
        self.refused("not used in argv: fmt")
        self.manifest["commands"]["queue"]["argv"] = ["fakeq", "{user}", "{fmt}", "{{literal}}"]
        pl = self.load()
        self.assertIsNone(pl.error)
        c = pl.commands["queue"]
        c.binary = "/bin/fakeq"
        self.assertEqual(c.build({"user": "al", "fmt": "long"}), ["/bin/fakeq", "al", "long", "{literal}"])
        self.manifest["commands"]["queue"]["argv"] = ["/abs/fakeq", "{user}", "{fmt}"]
        self.assertIsNone(self.load().error)

    def test_param_rules(self):
        p = self.manifest["commands"]["queue"]["params"]
        for spec, why in [({}, "exactly one"), ({"pattern": "a", "enum": ["a"]}, "exactly one"),
                          ({"pattern": "[a-z]*"}, "empty string"), ({"pattern": "("}, "regular expression"),
                          ({"pattern": 5}, "regular expression"), ({"enum": []}, "enum"), ({"enum": [1]}, "enum"),
                          ({"int": [5, 1]}, "[min, max]"), ({"int": [1]}, "[min, max]"),
                          ({"int": ["a", "b"]}, "[min, max]"), ({"pattern": "a", "describe": 5}, "describe"),
                          ({"pattern": "^[a-z-]{1,9}$"}, "option"), ({"pattern": ".+"}, "option"),
                          ({"enum": ["ok", "--all"]}, "option"), ({"int": [-1, 5]}, "from 0 up")]:
            p["user"] = spec
            self.refused(why)

    def test_option_like_values_are_refused_whatever_the_pattern(self):
        rule = G.TrustedParam({"pattern": "^[a-z]{1,9}$"}, "p")
        rule.rx = re.compile(".+")                     # as if a pattern had slipped through the load-time check
        for value in ("-M", "--format=%x", "-"):
            with self.assertRaises(ValueError):
                rule.check(value)
        self.assertEqual(rule.check("alice"), "alice")

    def test_limits_search_env_and_pane(self):
        for key, bad in [("timeout", 0), ("timeout", 121), ("max_output", 2000000), ("min_interval", -1),
                         ("cache", 99999), ("timeout", "5"), ("timeout", True)]:
            self.setUp()
            self.manifest["commands"]["queue"][key] = bad
            self.refused(key)
        self.setUp()
        self.manifest["commands"]["queue"]["read_only"] = "yes"
        self.refused("read_only")
        self.setUp()
        self.refused("search", search=["relative/dir"])
        self.refused("search", search=[])
        self.refused("env", env=["PATH"])
        self.refused("env", env=["LD_PRELOAD"])
        self.refused("env", env=["1X"])
        self.refused("pane", pane="a/b")
        self.refused("pane", pane="missing.py")

    def test_defaults(self):
        c = self.load().commands["slow"]
        self.manifest["commands"]["x"] = {"argv": ["fakeq"]}
        x = self.load().commands["x"]
        self.assertEqual((x.read_only, x.timeout, x.max_output, x.min_interval, x.cache), (False, 15, 65536, 2, 0))
        self.assertEqual(c.timeout, 1)

    def test_pane_is_reported(self):
        d = self.plugin_dir(self.path("with-pane"))
        self.manifest["pane"] = "pane.py"
        write_json(os.path.join(d, "plugin.json"), self.manifest)
        open(os.path.join(d, "pane.py"), "w").write("print(1)\n")
        pl = G.Plugin("fake", d)
        self.assertEqual((pl.error, pl.pane), (None, "pane.py"))

    def test_values_are_checked_whatever_the_pattern(self):
        c = self.load().commands["queue"]
        self.assertEqual(c.check({"user": "alice", "fmt": "long"}), {"user": "alice", "fmt": "long"})
        for bad in [{"user": "alice"}, {"user": "alice", "fmt": "long", "x": "1"}, {"user": 5, "fmt": "long"},
                    {"user": "a;ls", "fmt": "long"}, {"user": "alice", "fmt": "medium"},
                    {"user": "a" * 40, "fmt": "long"},
                    {"user": "$(id)", "fmt": "long"}, {"user": None, "fmt": "long"}, []]:
            with self.assertRaises(ValueError, msg=str(bad)):
                c.check(bad)
        self.manifest["commands"]["queue"]["params"]["user"] = {"pattern": "[^-].*"}  # allows almost anything but...
        c = self.load().commands["queue"]
        for bad in ("a\nb", "a\x00b", "a\rb", "x" * 257):
            with self.assertRaises(ValueError, msg=repr(bad)):
                c.check({"user": bad, "fmt": "long"})
        self.assertEqual(self.load().commands["job"].check({"id": 7}), {"id": "7"})
        for bad in (True, 0, 100000, "1.5", "1e3", " 7"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.load().commands["job"].check({"id": bad})

    def test_tool_name_and_schema(self):
        self.manifest["name"] = "my-plug"
        pl = G.Plugin("my-plug", self.plugin_dir(self.path("x"), "my-plug"))
        self.assertIsNone(pl.error)
        self.assertEqual(pl.commands["queue"].tool, "my_plug__queue")
        schema = pl.commands["queue"].schema("node1")["function"]
        self.assertEqual(schema["parameters"]["required"], ["user", "fmt"])
        self.assertEqual(schema["parameters"]["properties"]["fmt"]["enum"], ["short", "long"])
        self.assertEqual(schema["parameters"]["properties"]["user"]["pattern"], "^[a-z_][a-z0-9_-]{0,31}$")
        self.assertEqual(schema["parameters"]["properties"]["user"]["description"], "a user name")
        self.assertIn("(runs `fakeq -u {user} --fmt={fmt}` on node1; read-only)", schema["description"])
        self.assertIn("the user is asked first", pl.commands["kill"].schema("h")["function"]["description"])


    def test_a_usage_section_goes_to_the_model_and_the_blessing_screen(self):
        self.cmd(usage=["ExitCode is code:signal.", "An empty answer is not an error."])
        pl = self.load()
        self.assertIsNone(pl.error)
        desc = pl.commands["queue"].schema("h")["function"]["description"]
        self.assertTrue(desc.endswith("\n\nUsage:\nExitCode is code:signal.\nAn empty answer is not an error."))
        self.assertIn("      | An empty answer is not an error.", pl.summary())
        self.assertNotIn("Usage:", pl.commands["kill"].schema("h")["function"]["description"])
        before = pl.hash
        self.cmd(usage="One line\nand another")                  # a string, split into lines
        pl = self.load()
        self.assertEqual(pl.commands["queue"].usage, ["One line", "and another"])
        self.assertNotEqual(pl.hash, before)                       # covered by the blessing

    def test_a_usage_section_is_short_and_plain(self):
        for bad, why in (([" x" * 700], "at most"), (["l"] * 21, "at most"), (["\x1b[2Jfake"], "control or hidden"),
                         (["a\u202eb"], "control or hidden"), ("x\ty", "control or hidden"), (5, "list of lines")):
            with self.subTest(bad=bad):
                self.cmd(usage=bad)
                self.refused(why)


# ---------------------------------------------------------------- the program

class TestBinary(PluginBase):
    def test_resolved_from_search_never_from_path(self):
        pl = self.load()
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            pl.resolve()
        self.assertEqual(pl.commands["queue"].binary, os.path.join(os.path.realpath(self.bin), "fakeq"))
        other = self.path("elsewhere")
        os.mkdir(other, 0o755)
        program(os.path.join(other, "nothere"), "true\n")
        self.manifest["commands"]["x"] = {"argv": ["nothere"]}
        pl = self.load()
        with mock.patch.dict(os.environ, {"PATH": other}):
            self.assertEqual(pl.resolve(), ["x"])          # not installed here: only its command is left out
        self.assertNotIn("x", pl.commands)
        self.assertIn("queue", pl.commands)
        self.manifest["commands"] = {"x": {"argv": ["nothere"]}}
        with self.assertRaises(G.HoplonError) as cm:
            self.load().resolve()
        self.assertIn("none of its programs", str(cm.exception))

    def test_group_writable_directory_is_refused(self):
        os.chmod(self.bin, 0o775)
        with self.assertRaises(G.HoplonError) as cm:
            self.load().resolve()
        self.assertIn("writable by group or others", str(cm.exception))

    def test_writable_program_is_refused(self):
        os.chmod(os.path.join(self.bin, "fakeq"), 0o775)
        with self.assertRaises(G.HoplonError):
            self.load().resolve()

    def test_not_executable_is_refused(self):
        os.chmod(os.path.join(self.bin, "fakeq"), 0o644)
        with self.assertRaises(G.HoplonError) as cm:
            self.load().resolve()
        self.assertIn("not an executable", str(cm.exception))

    def test_absolute_argv0(self):
        self.manifest["commands"] = {"x": {"argv": [os.path.join(self.bin, "fakeq")], "read_only": True}}
        pl = self.load()
        pl.resolve()
        self.assertTrue(pl.commands["x"].binary.endswith("/fakeq"))

    def test_the_environment_is_minimal(self):
        c = self.load().commands["queue"]
        with mock.patch.dict(os.environ, {"FAKE_ALLOWED": "yes", "FAKE_OTHER": "no", "PATH": "/home/me/bin",
                                          "LD_PRELOAD": "/x", "HOME": "/home/me"}):
            env = c.env()
        self.assertEqual(env["PATH"], self.bin)
        self.assertEqual(sorted(env), ["FAKE_ALLOWED", "HOME", "LANG", "LOGNAME", "PATH", "USER"])
        self.assertEqual((env["FAKE_ALLOWED"], env["HOME"], env["LANG"]), ("yes", "/home/me", "C.UTF-8"))


# ---------------------------------------------------------------- blessing and enabling

class TestBlessing(PluginBase):
    def setUp(self):
        super().setUp()
        self.home, _ = make_home(self.tmp, "http://127.0.0.1:1/v1")
        self.cfg = G.Config(self.home)
        self.dir = self.plugin_dir(os.path.join(self.home, "plugins"))

    def engine(self, names=("fake",)):
        notes = []

        class Ui(G.PlainUI):
            performs = True
            notice = notes.append

        ep = G.Endpoint({"name": "t", "base_url": "http://127.0.0.1:1/v1", "model": "m"}, "t.json")
        return G.Engine(self.cfg, ep, G.Session(self.cfg), Ui(), client=object(), plugins=names), notes

    def test_unblessed_is_not_enabled(self):
        eng, notes = self.engine()
        self.assertEqual(eng.trusted, {})
        self.assertIn("unblessed", notes[0])
        self.assertEqual([t["function"]["name"] for t in eng.tools()], [t["function"]["name"] for t in G.TOOLS])

    def test_blessed_is_enabled_and_changes_are_refused(self):
        bless_plugin(self.home, self.dir)
        eng, notes = self.engine()
        self.assertEqual((notes, sorted(eng.trusted)[:2]), ([], ["fake__big", "fake__cached"]))
        names = [t["function"]["name"] for t in eng.tools()]
        self.assertIn("fake__queue", names)
        self.assertIn("# Trusted commands", eng.system()["content"])
        self.manifest["commands"]["queue"]["argv"] = ["fakeq", "-x", "{user}", "--fmt={fmt}"]
        write_json(os.path.join(self.dir, "plugin.json"), self.manifest)
        eng2, notes = self.engine()
        self.assertEqual(eng2.trusted, {})
        self.assertIn("changed", notes[0])
        self.manifest = json.load(open(os.path.join(self.dir, "plugin.json")))
        open(os.path.join(self.dir, "extra.txt"), "w").write("x")          # any file of the directory counts
        self.assertIn("changed", self.engine()[1][0])

    def test_nothing_enabled_no_tools_no_prompt(self):
        bless_plugin(self.home, self.dir)
        eng, _ = self.engine(())
        self.assertEqual(eng.trusted, {})
        self.assertNotIn("Trusted commands", eng.system()["content"])

    def test_a_program_in_a_group_writable_directory_refuses_the_plugin(self):
        bless_plugin(self.home, self.dir)
        os.chmod(self.bin, 0o770)
        eng, notes = self.engine()
        self.assertEqual(eng.trusted, {})
        self.assertIn("writable by group or others", notes[0])

    def test_tool_name_collisions(self):
        bless_plugin(self.home, self.dir)
        eng, _ = self.engine(())
        eng.register([{"name": "fake__queue", "description": "mine"}])
        with self.assertRaises(G.HoplonError) as cm:
            eng.set_plugin("fake", True)
        self.assertIn("already has the name fake__queue", str(cm.exception))
        eng2, _ = self.engine()
        with self.assertRaises(G.HoplonError):
            eng2.register([{"name": "fake__queue", "description": "mine"}])
        self.assertEqual(eng2.register([{"name": "other", "description": "x"}]), ["other"])

    def test_config_profile_and_listing(self):
        bless_plugin(self.home, self.dir)
        write_json(os.path.join(self.home, "config.json"),
                   {"default_plugins": ["fake"], "plugin_dirs": [self.path("more")]})
        cfg = G.Config(self.home)
        self.assertEqual((cfg.default_plugins, cfg.plugin_dirs[1]), (["fake"], self.path("more")))
        prof = G.Profile({"name": "p", "plugins": ["fake"]}, "p.json")
        self.assertEqual(prof.plugins, ["fake"])
        self.assertIn("plugins:  fake", prof.summary()[0])
        with self.assertRaises(G.HoplonError):
            G.Profile({"name": "p", "plugins": "fake"}, "p.json")
        rows = G.plugin_rows(self.cfg, ["fake"])
        self.assertEqual((rows[0]["name"], rows[0]["status"], rows[0]["enabled"], rows[0]["blessed"]),
                         ("fake", "blessed", True, True))
        self.assertEqual(rows[0]["commands"][0]["name"], "queue")

    def test_bless_shows_the_commands(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertTrue(G.bless(self.cfg, self.dir, lambda q: True))
        text = out.getvalue()
        self.assertIn("Plugin fake", text)
        self.assertIn("[asks every time]", text)
        self.assertIn("fakeq -u {user} --fmt={fmt}", text)
        self.assertIn("program: " + os.path.realpath(self.bin), text)
        self.assertIn("your PATH is never used", text)
        self.assertEqual(self.cfg.blessed()[self.dir]["kind"], "plugin")
        self.assertEqual(G.plugin_status(self.cfg, G.find_plugins(self.cfg)["fake"]), "blessed")

    def test_bless_refuses_a_bad_manifest(self):
        self.manifest["commands"]["queue"]["argv"] = ["fakeq", "{nope}"]
        write_json(os.path.join(self.dir, "plugin.json"), self.manifest)
        with self.assertRaises(G.HoplonError) as cm:
            G.bless(self.cfg, self.dir, lambda q: True)
        self.assertIn("undeclared", str(cm.exception))


# ---------------------------------------------------------------- running, in process

class Ui(G.PlainUI):
    performs = True

    def __init__(self):
        super().__init__(io.StringIO())
        self.results, self.autos = [], []
        self.answer = "decline"

    def notice(self, msg):
        pass

    def show_auto(self, p):
        self.autos.append(p)

    def decide(self, pending):
        for p in pending:
            p.decision = self.answer

    def result(self, p, status, note, **extra):
        self.results.append((p.id, status, note, extra))


class TestRunning(PluginBase):
    def setUp(self):
        super().setUp()
        self.home, _ = make_home(self.tmp, "http://127.0.0.1:1/v1")
        self.cfg = G.Config(self.home)
        bless_plugin(self.home, self.plugin_dir(os.path.join(self.home, "plugins")))
        self.ui = Ui()
        ep = G.Endpoint({"name": "t", "base_url": "http://127.0.0.1:1/v1", "model": "m"}, "t.json")
        self.eng = G.Engine(self.cfg, ep, G.Session(self.cfg), self.ui, client=object(), plugins=["fake"])
        self.assertEqual(self.ui.results, [])
        self.assertTrue(self.eng.trusted)

    def audit(self):
        with open(os.path.join(self.home, "audit.log")) as f:
            return [json.loads(x) for x in f if '"trusted_run"' in x]

    def model(self, name, **args):
        p = self.eng.make_proposal({"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}})
        self.eng.decide([p])
        return p, self.eng.perform(p)

    def test_read_only_runs_without_asking_and_arguments_stay_whole(self):
        self.ui.answer = "never asked"
        p, told = self.model("fake__queue", user="alice", fmt="long")
        self.assertEqual((p.kind, p.auto, p.decision), ("trusted", True, "accept"))
        self.assertEqual(self.ui.autos, [p])
        self.assertEqual(p.event()["argv"], "fakeq -u alice --fmt=long")
        self.assertEqual({k: v for k, v in p.event().items() if k in ("plugin", "command", "params", "read_only")},
                         {"plugin": "fake", "command": "queue", "params": {"user": "alice", "fmt": "long"},
                          "read_only": True})
        self.assertTrue(told.startswith("exit 3. Output:\nresult -u alice --fmt=long\n[stderr]\nwarning"), told)
        self.assertEqual(self.calls()[0]["args"], ["-u", "alice", "--fmt=long"])
        self.assertEqual(self.ui.results[0][1:3][0], "error")          # exit 3
        self.assertEqual(self.ui.results[0][3]["exit"], 3)
        rec = self.audit()[0]
        self.assertEqual([rec[k] for k in ("plugin", "command", "source", "exit", "cached", "rate_limited")],
                         ["fake", "queue", "model", 3, False, False])
        self.assertEqual(rec["params"], {"user": "alice", "fmt": "long"})
        self.assertTrue(rec["binary"].endswith("/fakeq"))
        self.assertGreater(rec["bytes"], 10)

    def test_awkward_values_are_refused_and_nothing_runs(self):
        for bad in ("alice bob", "a;ls", "$(id)", "`id`", "a\nb", "A"):
            p, told = self.model("fake__queue", user=bad, fmt="long")
            self.assertEqual((p.kind, p.auto), ("invalid", False))
            self.assertTrue(told.startswith("error: parameter 'user'"), told)
        p, told = self.model("fake__queue", user="alice")
        self.assertIn("missing parameter 'fmt'", told)
        p, told = self.model("fake__queue", user="alice", fmt="long", extra="1")
        self.assertIn("unknown parameter 'extra'", told)
        self.assertEqual(self.calls(), [])

    def test_a_loose_value_stays_one_argument(self):
        self.manifest["commands"]["queue"]["params"]["user"] = {"pattern": "[ -,.-~][ -~]{0,39}"}   # no leading '-'
        write_json(os.path.join(self.home, "plugins", "fake", "plugin.json"), self.manifest)
        bless_plugin(self.home, os.path.join(self.home, "plugins", "fake"))
        self.eng.set_plugin("fake", True)
        evil = "a b; touch /tmp/x $(id) 'q' \"d\" *"
        self.model("fake__queue", user=evil, fmt="short")
        self.assertEqual(self.calls()[0]["args"], ["-u", evil, "--fmt=short"])

    def test_not_read_only_asks_every_time(self):
        p, told = self.model("fake__kill", id=5)
        self.assertEqual((p.kind, p.auto, p.decision, told), ("trusted", False, "decline", "declined by the user"))
        self.assertEqual(self.calls(), [])
        self.ui.answer = "accept"
        p, told = self.model("fake__kill", id="5")
        self.assertEqual((p.decision, self.calls()[0]["args"]), ("accept", ["kill", "5"]))
        self.assertTrue(told.startswith("exit 3"))

    def test_plain_ui_offers_nothing(self):
        self.eng.ui = G.PlainUI()
        self.assertEqual(self.eng.trusted_tools(), {})
        self.assertNotIn("fake__queue", [t["function"]["name"] for t in self.eng.tools()])
        p = self.eng.make_proposal({"id": "c", "function": {"name": "fake__queue", "arguments": "{}"}})
        self.assertEqual(p.kind, "invalid")

    def test_environment_seen_by_the_program(self):
        with mock.patch.dict(os.environ, {"FAKE_ALLOWED": "yes", "FAKE_OTHER": "no", "SECRET_THING": "x"}):
            self.model("fake__queue", user="al", fmt="long")
        env = self.calls()[0]["env"]
        self.assertEqual(env["PATH"], self.bin)
        self.assertEqual(env["FAKE_ALLOWED"], "yes")
        self.assertNotIn("FAKE_OTHER", env)
        self.assertNotIn("SECRET_THING", env)
        self.assertEqual(env["LANG"], "C.UTF-8")

    def test_cache_returns_the_last_result(self):
        r1 = self.eng.run_user("fake", "cached", {})
        r2 = self.eng.run_user("fake", "cached", {})
        self.assertEqual((r1["cached"], r2["cached"], r2["output"]), (None, 0, r1["output"]))
        self.assertEqual(len(self.calls()), 1)
        p, told = self.model("fake__cached")
        self.assertIn("(cached 0 s ago)", told)
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(self.audit()[-1]["cached"])
        self.eng.cached[("fake", "cached", ())] = (time.monotonic() - 100, 0, "old", "")
        self.assertIsNone(self.eng.run_user("fake", "cached", {})["cached"])        # too old: really run
        self.assertEqual(len(self.calls()), 2)

    def test_min_interval_refuses_too_soon(self):
        r1 = self.eng.run_user("fake", "paced", {})
        self.assertIsNone(r1["error"])
        r2 = self.eng.run_user("fake", "paced", {})
        self.assertRegex(r2["error"], r"^rate limited: try again in \d+ s$")
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(self.audit()[-1]["rate_limited"])
        p, told = self.model("fake__paced")
        self.assertTrue(told.startswith("error: not run: rate limited"), told)
        self.assertEqual(self.ui.results[-1][1], "error")
        self.eng.last_run[("fake", "paced")] -= 31
        self.assertIsNone(self.eng.run_user("fake", "paced", {})["error"])

    def test_global_cap_per_minute(self):
        for i in range(G.TRUSTED_PER_MINUTE):
            self.assertIsNone(self.eng.run_user("fake", "job", {"id": i + 1})["error"], i)
        r = self.eng.run_user("fake", "job", {"id": 99})
        self.assertRegex(r["error"], r"^rate limited: try again in \d+ s$")
        self.assertEqual(len(self.calls()), G.TRUSTED_PER_MINUTE)
        self.eng.runs = G.collections.deque(t - 61 for t in self.eng.runs)       # a minute later
        self.assertIsNone(self.eng.run_user("fake", "job", {"id": 99})["error"])

    def test_timeout_kills_the_program(self):
        started = time.monotonic()
        r = self.eng.run_user("fake", "slow", {})
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((r["exit"], r["note"]), (None, "killed after 1 s"))
        p, told = self.model("fake__slow")
        self.assertTrue(told.startswith("killed after 1 s"), told)

    def test_output_cap_stops_the_program(self):
        r = self.eng.run_user("fake", "big", {})
        self.assertIsNone(r["error"])
        self.assertIn("output cut at 1000 bytes", r["note"])
        self.assertLessEqual(len(r["output"]), 1000)
        self.assertTrue(r["output"].startswith("aaaa"))

    def test_a_secret_in_the_output_is_masked(self):
        p, told = self.model("fake__leak")
        self.assertNotIn("abcdefghijklmnop1234", told)
        self.assertIn("[REDACTED]", told)
        self.assertNotIn("abcdefghijklmnop1234", json.dumps(self.ui.results))
        self.assertTrue(self.audit()[-1]["redacted"])

    def test_the_endpoint_key_is_masked(self):
        self.eng.client = mock.Mock(_key="supersecretendpointkey")
        program(os.path.join(self.bin, "leakq"), "echo 'key is supersecretendpointkey'\n")
        # the program changed since hoplon started, so it is refused; a fresh engine reads the new identity
        ep = G.Endpoint({"name": "t", "base_url": "http://127.0.0.1:1/v1", "model": "m"}, "t.json")
        eng = G.Engine(self.cfg, ep, G.Session(self.cfg), self.ui, client=mock.Mock(_key="supersecretendpointkey"),
                       plugins=["fake"])
        self.assertEqual(eng.run_user("fake", "leak", {})["output"].strip(), "key is [REDACTED endpoint key]")

    def test_a_program_that_changed_is_refused(self):
        self.assertIsNone(self.eng.run_user("fake", "job", {"id": 1})["error"])
        time.sleep(0.01)
        program(os.path.join(self.bin, "fakeq"), "echo changed\n")
        r = self.eng.run_user("fake", "job", {"id": 2})
        self.assertEqual(r["error"], "the program changed since hoplon started; restart hoplon")
        self.assertEqual(len(self.calls()), 1)
        p, told = self.model("fake__job", id=3)
        self.assertIn("error: not run: the program changed", told)
        os.unlink(os.path.join(self.bin, "fakeq"))
        self.assertIn("gone", self.eng.run_user("fake", "job", {"id": 2})["error"])

    def test_the_user_runs_with_the_same_checks(self):
        r = self.eng.run_user("fake", "kill", {"id": 7})          # not read-only: the user decided by clicking
        self.assertEqual((r["exit"], self.calls()[0]["args"]), (3, ["kill", "7"]))
        self.assertEqual(self.audit()[0]["source"], "user")
        with self.assertRaises(G.HoplonError) as cm:
            self.eng.run_user("fake", "job", {"id": "1;ls"})
        self.assertEqual(cm.exception.kind, "bad_params")
        with self.assertRaises(G.HoplonError) as cm:
            self.eng.run_user("fake", "nope", {})
        self.assertEqual(cm.exception.kind, "unknown_command")
        self.eng.set_plugin("fake", False)
        with self.assertRaises(G.HoplonError):
            self.eng.run_user("fake", "job", {"id": 1})

    def test_tty_shows_what_ran(self):
        p = self.eng.make_proposal({"id": "c", "function": {"name": "fake__queue",
                                                            "arguments": json.dumps({"user": "alice", "fmt": "long"})}})
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            ui = G.TtyUI(out=io.StringIO())
            ui.show_auto(p)
            ui.result(p, "ok", "exit 0 in 5 ms", output="line\x1b[2J\n", exit=0)
        text = err.getvalue()
        self.assertIn("↳ [p1] ▲ runs live · fake.queue(user=alice, fmt=long): fakeq -u alice --fmt=long", text)
        self.assertIn("\\x1b[2J", text)
        self.assertNotIn("\x1b", text)


# ---------------------------------------------------------------- over JSON

class TestJson(PluginBase):
    def start(self, api, args=("--plugin", "fake"), bless=True):
        home, ep = make_home(self.tmp, api.url)
        bless_path(home, ep)
        d = self.plugin_dir(os.path.join(home, "plugins"))
        if bless:
            bless_plugin(home, d)
        os.makedirs(self.path("wd"), exist_ok=True)
        self.home = home
        return Proc(home, self.path("wd"), ["-e", "test"] + list(args))

    def audit(self):
        with open(os.path.join(self.home, "audit.log")) as f:
            return [json.loads(x) for x in f]

    def test_the_prompt_says_trusted_commands_read_the_live_system(self):
        with FakeAPI([]) as api:
            g = self.start(api)
            g.send({"type": "system"})
            system = g.until("system")["system"]
            g.close()
        self.assertIn("# Trusted commands", system)
        where = system.split("# Where things are", 1)[1]
        self.assertIn("Trusted commands (plugin__command tools) run on this host too", where)
        self.assertIn("what the user shares with you and what trusted commands return", where)

    def test_hello_and_plugins_event(self):
        with FakeAPI([]) as api:
            g = self.start(api)
            hello = g.until("hello")
            self.assertIn("plugins", hello["features"])
            self.assertEqual(hello["plugins"], ["fake"])
            ev = g.until("plugins")
            (row,) = ev["plugins"]
            self.assertEqual((row["name"], row["enabled"], row["blessed"], row["pane"], row["dir"]),
                             ("fake", True, True, None, os.path.join(self.home, "plugins", "fake")))
            queue = next(c for c in row["commands"] if c["name"] == "queue")
            self.assertEqual(queue["read_only"], True)
            self.assertEqual(queue["argv"], ["fakeq", "-u", "{user}", "--fmt={fmt}"])
            self.assertEqual(queue["params"]["fmt"], {"enum": ["short", "long"], "describe": ""})
            g.send({"type": "plugins"})
            self.assertEqual(g.until("plugins")["plugins"][0]["name"], "fake")
            g.close()

    def test_model_calls_a_read_only_tool(self):
        turns = [call("fake__queue", user="alice", fmt="short"), reply("done")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "who is queued?"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p["auto"], p["argv"]), ("trusted", True, "fakeq -u alice --fmt=short"))
            r = g.until("result")
            self.assertEqual((r["status"], r["exit"]), ("error", 3))
            self.assertIn("result -u alice --fmt=short", r["output"])
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        first = api.requests[0]["body"]
        names = [t["function"]["name"] for t in first["tools"]]
        self.assertIn("fake__queue", names)
        self.assertIn("# Trusted commands", first["messages"][0]["content"])
        self.assertTrue(api.requests[-1]["body"]["messages"][-1]["content"].startswith("exit 3."))
        self.assertEqual(self.calls()[0]["env"]["PATH"], self.bin)

    def test_not_enabled_offers_no_tools(self):
        with FakeAPI([call("fake__queue", user="a", fmt="short"), reply("ok")]) as api:
            g = self.start(api, args=())
            g.send({"type": "user", "text": "x"})
            g.until("done")
            g.close()
        self.assertFalse(any("fake__" in t["function"]["name"] for t in api.requests[0]["body"]["tools"]))
        self.assertIn("unknown tool", api.requests[-1]["body"]["messages"][-1]["content"])
        self.assertEqual(self.calls(), [])

    def test_unblessed_plugin_is_refused_with_a_notice(self):
        with FakeAPI([]) as api:
            g = self.start(api, bless=False)
            n = g.until("notice")
            self.assertIn("unblessed", n["message"])
            self.assertEqual(g.until("hello")["plugins"], [])
            g.close()

    def test_not_read_only_asks_through_a_decision(self):
        with FakeAPI([call("fake__kill", id=4), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "stop it"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p.get("auto"), p["read_only"]), ("trusted", None, False))
            self.assertEqual(self.calls(), [])
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            self.assertEqual(g.until("result")["exit"], 3)
            g.until("done")
            self.assertEqual(self.calls()[0]["args"], ["kill", "4"])
            g.close()

    def test_declined_runs_nothing(self):
        with FakeAPI([call("fake__kill", id=4), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "stop it"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()
        self.assertEqual(self.calls(), [])
        self.assertEqual(api.requests[-1]["body"]["messages"][-1]["content"], "declined by the user")

    def test_the_user_runs_a_command(self):
        with FakeAPI([]) as api:
            g = self.start(api)
            g.send({"type": "run", "id": "r1", "plugin": "fake", "command": "job", "params": {"id": "42"}})
            ran = g.until("ran")
            self.assertEqual((ran["id"], ran["exit"], ran["cached"]), ("r1", 3, False))
            self.assertIn("result job 42", ran["output"])
            self.assertIsInstance(ran["ms"], int)
            g.send({"type": "run", "id": 2, "plugin": "fake", "command": "job", "params": {"id": "4;2"}})
            bad = g.until("ran")
            self.assertEqual(bad["id"], 2)
            self.assertIn("parameter 'id'", bad["error"])
            self.assertNotIn("output", bad)
            g.send({"type": "run", "id": 3, "plugin": "nope", "command": "job", "params": {}})
            self.assertIn("no command", g.until("ran")["error"])
            g.send({"type": "run", "plugin": "fake", "command": "job", "params": {"id": "1"}})
            self.assertEqual(g.until("error")["code"], "bad_message")
            g.close()
        runs = [e for e in self.audit() if e["event"] == "trusted_run"]
        self.assertEqual([r["source"] for r in runs], ["user"])
        self.assertEqual(len(self.calls()), 1)

    def test_run_needs_an_enabled_plugin_and_can_be_switched(self):
        with FakeAPI([]) as api:
            g = self.start(api, args=())
            g.send({"type": "run", "id": 1, "plugin": "fake", "command": "job", "params": {"id": "1"}})
            self.assertIn("no command", g.until("ran")["error"])
            g.send({"type": "plugin", "name": "fake", "on": True})
            self.assertTrue(next(r for r in g.until("plugins")["plugins"] if r["name"] == "fake")["enabled"])
            g.send({"type": "run", "id": 2, "plugin": "fake", "command": "job", "params": {"id": "1"}})
            self.assertEqual(g.until("ran")["exit"], 3)
            g.send({"type": "plugin", "name": "nope", "on": True})
            self.assertEqual(g.until("error")["code"], "unknown_plugin")
            g.send({"type": "plugin", "name": "fake", "on": False})
            g.until("plugins")
            g.send({"type": "plugin", "name": "fake"})
            self.assertEqual(g.until("error")["code"], "bad_message")
            g.close()

    def test_run_is_busy_while_a_decision_is_pending(self):
        with FakeAPI([call("fake__kill", id=4), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "stop it"})
            p = g.until("proposal")
            g.send({"type": "run", "id": 9, "plugin": "fake", "command": "job", "params": {"id": "1"}})
            self.assertEqual(g.until("error")["code"], "busy")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
