"""Tests for command sets and scoped commands: the manifest's rules, blessing, the programs they may run, the checks on
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
              "access": "read", "min_interval": 0},
    "job": {"describe": "one job", "argv": ["fakeq", "job", "{id}"],
            "params": {"id": {"int": [1, 99999]}}, "access": "read", "min_interval": 0},
    "kill": {"describe": "stops a job", "argv": ["fakeq", "kill", "{id}"],
             "params": {"id": {"int": [1, 99999]}}, "access": "write", "min_interval": 0},
    "slow": {"describe": "never ends", "argv": ["slowq"], "access": "read", "timeout": 1, "min_interval": 0},
    "big": {"describe": "too much output", "argv": ["bigq"], "access": "read", "max_output": 1000, "min_interval": 0},
    "leak": {"describe": "prints a secret", "argv": ["leakq"], "access": "read", "min_interval": 0},
    "cached": {"describe": "cached", "argv": ["fakeq", "cached"], "access": "read", "cache": 60, "min_interval": 0},
    "paced": {"describe": "paced", "argv": ["fakeq", "paced"], "access": "read", "min_interval": 30},
}


def program(path, body):
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)


class CommandSetBase(Tmp):
    """A command set called 'fake' whose programs are shell scripts in a private directory that log what they get."""

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
        self.manifest = {"name": "fake", "description": "a fake command set", "search": [self.bin], "env": ["FAKE_ALLOWED"],
                         "commands": copy.deepcopy(CMDS)}

    def set_dir(self, parent=None, name="fake"):
        d = os.path.join(parent or self.path("commands"), name)
        os.makedirs(d, exist_ok=True)
        write_json(os.path.join(d, "commands.json"), self.manifest)
        return d

    def load(self, **top):
        saved, self.manifest = self.manifest, dict(self.manifest, **top)
        try:
            return G.CommandSet("fake", self.set_dir(self.path("loose")))
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


def bless_set(home, path):
    G.Config(home).set_blessed(os.path.abspath(path), {"kind": "command_set", "name": os.path.basename(path),
                                                       "hash": G.hash_skill(path)[0]})


# ---------------------------------------------------------------- the manifest

class TestManifest(CommandSetBase):
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
        self.assertEqual(G.CommandSet("fake", self.set_dir(self.path("again"))).hash, pl.hash)

    def test_the_slurm_command_set_in_the_repo_is_valid(self):
        pl = G.CommandSet("slurm", os.path.join(REPO, "commands", "slurm"))
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

    def test_sudo_doas_and_run0_are_seen_and_checked(self):
        """A command run through sudo, doas or run0 runs as another user: it must never ask for a password, may use
        only a few options, and names its program by absolute path, so a sudoers rule can match it exactly."""
        ok = [(["sudo", "-n", "/usr/bin/systemctl", "restart", "nginx"], "root"),
              (["sudo", "--non-interactive", "-u", "www", "/usr/bin/id"], "www"),
              (["sudo", "-n", "--user=www", "-H", "/usr/bin/id"], "www"),
              (["sudo", "-n", "-u", "{user}", "/usr/bin/id"], "a user given per call"),
              (["doas", "-n", "/usr/bin/id"], "root"),
              (["run0", "--no-ask-password", "/usr/bin/id"], "root"),
              (["sudo", "-n", "--", "/usr/bin/id"], "root"),
              (["fakeq", "sudo"], None)]
        for argv, who in ok:
            self.assertEqual(G.escalation(argv, ""), who, argv)
        for argv, why in [(["sudo", "/usr/bin/id"], "needs -n"), (["doas", "/usr/bin/id"], "needs -n"),
                          (["sudo", "-n", "-s"], "-s is not allowed"), (["sudo", "-ns", "/usr/bin/id"], "-ns"),
                          (["sudo", "-n", "-E", "/usr/bin/id"], "-E is not allowed"),
                          (["sudo", "-n", "-i"], "-i is not allowed"), (["sudo", "-n", "-e", "/etc/x"], "-e"),
                          (["sudo", "-n", "id"], "absolute path"), (["sudo", "-n"], "absolute path"),
                          (["sudo", "-n", "{user}"], "absolute path"), (["sudo", "-n", "-u"], "needs a user"),
                          (["pkexec", "/usr/bin/id"], "pkexec can't run here"), (["su", "-c", "id"], "su can't"),
                          (["sudoedit", "/etc/x"], "sudoedit can't")]:
            with self.assertRaises(ValueError, msg=argv) as e:
                G.escalation(argv, "")
            self.assertIn(why, str(e.exception))

    def test_a_sudo_command_without_n_refuses_the_set(self):
        self.manifest["commands"]["queue"] = {"describe": "who", "argv": ["fakeq", "/usr/bin/id"], "access": "read"}
        self.manifest["commands"]["queue"]["argv"][0] = "sudo"
        program(os.path.join(self.bin, "sudo"), "exit 0\n")
        self.refused("needs -n")

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

    def test_path_and_host_rules(self):
        p = self.manifest["commands"]["queue"]["params"]
        for spec, why in [({"path": {}}, "needs 'under'"), ({"path": {"under": ["rel/dir"]}}, "absolute"),
                          ({"path": {"files": ["x"]}}, "absolute"), ({"path": {"under": ["/d"], "new": "yes"}}, "new"),
                          ({"path": {"files": ["/f"], "new": True}}, "needs 'under'"),
                          ({"path": {"under": ["/d"], "other": 1}}, "other"), ({"path": "/d"}, "path"),
                          ({"host": ["ok", "bad host"]}, "host names"), ({"host": ["-x"]}, "host names"),
                          ({"host": []}, "host")]:
            with self.subTest(spec=spec):
                self.setUp()
                self.manifest["commands"]["queue"]["params"]["user"] = spec
                self.refused(why)
        self.setUp()
        self.manifest["commands"]["queue"]["params"]["user"] = {"path": {"under": ["/tmp"], "new": True}}
        self.refused('needs "access": "write"')                    # a read command may not create files

    def test_a_path_is_resolved_then_checked(self):
        top, out = self.path("data"), self.path("outside")
        for d in (top, os.path.join(top, "sub"), os.path.join(top, ".hidden"), out):
            os.makedirs(d, exist_ok=True)
        for f in ("a.txt", "sub/b.txt", ".hidden/c", "id_rsa", "server.key"):
            open(os.path.join(top, f), "w").close()
        open(os.path.join(out, "secret.txt"), "w").close()
        os.symlink(os.path.join(out, "secret.txt"), os.path.join(top, "link"))
        os.symlink(os.path.join(top, "sub"), os.path.join(top, "sublink"))
        rule = G.TrustedParam({"path": {"under": [top], "files": [os.path.join(top, "id_rsa")]}}, "p")
        real = os.path.realpath
        self.assertEqual(rule.check(os.path.join(top, "a.txt")), real(os.path.join(top, "a.txt")))
        self.assertEqual(rule.check(os.path.join(top, "sublink", "b.txt")), real(os.path.join(top, "sub", "b.txt")))
        self.assertEqual(rule.check(os.path.join(top, "id_rsa")), real(os.path.join(top, "id_rsa")))   # listed
        with mock.patch("os.getcwd", return_value=top):
            self.assertEqual(rule.check("sub/b.txt"), real(os.path.join(top, "sub", "b.txt")))      # relative
        for value, why in ((os.path.join(top, "..", "outside", "secret.txt"), "must be inside"),
                           (os.path.join(top, "link"), "must be inside"),                 # a symlink out
                           ("/etc/passwd", "must be inside"), (os.path.join(top, ".hidden", "c"), "hidden"),
                           (os.path.join(top, "server.key"), "secrets"), (os.path.join(top, "nope"), "does not exist"),
                           (top + "x", "must be inside")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as cm:
                    rule.check(value)
                self.assertIn(why, str(cm.exception))
        new = G.TrustedParam({"path": {"under": [top], "new": True}}, "p")
        self.assertEqual(new.check(os.path.join(top, "fresh.txt")), real(os.path.join(top, "fresh.txt")))
        with self.assertRaises(ValueError) as cm:
            new.check(os.path.join(top, "nodir", "fresh.txt"))
        self.assertIn("directory must exist", str(cm.exception))
        self.assertIn(f"a path under {top}", new.schema()["description"])

    def test_a_host_is_one_of_the_list(self):
        rule = G.TrustedParam({"host": ["login1", "login2.example.org"], "describe": "a login node"}, "p")
        self.assertEqual(rule.check("login2.example.org"), "login2.example.org")
        with self.assertRaises(ValueError):
            rule.check("login3")
        self.assertEqual(rule.schema(), {"type": "string", "enum": ["login1", "login2.example.org"],
                                         "description": "a login node"})

    def test_a_path_reaches_the_program_resolved(self):
        top = self.path("logs")
        os.makedirs(os.path.join(top, "real"))
        open(os.path.join(top, "real", "x.log"), "w").close()
        os.symlink(os.path.join(top, "real"), os.path.join(top, "alias"))
        self.manifest["commands"]["tail"] = {"describe": "a log", "argv": ["fakeq", "tail", "{file}"], "access": "read",
                                             "params": {"file": {"path": {"under": [top]}}}}
        pl = self.load()
        self.assertIsNone(pl.error)
        cmd = pl.commands["tail"]
        values = cmd.check({"file": os.path.join(top, "alias", "x.log")})
        self.assertEqual(values, {"file": os.path.realpath(os.path.join(top, "real", "x.log"))})

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
        self.manifest["commands"]["queue"]["access"] = "yes"
        self.refused("access must be")
        self.setUp()
        del self.manifest["commands"]["queue"]["access"]
        self.refused("access must be")
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
        self.manifest["commands"]["x"] = {"argv": ["fakeq"], "access": "write"}
        x = self.load().commands["x"]
        self.assertEqual((x.auto, x.timeout, x.max_output, x.min_interval, x.cache), (False, 15, 65536, 2, 0))
        self.assertEqual(c.timeout, 1)

    def test_pane_is_reported(self):
        d = self.set_dir(self.path("with-pane"))
        self.manifest["pane"] = "pane.py"
        write_json(os.path.join(d, "commands.json"), self.manifest)
        open(os.path.join(d, "pane.py"), "w").write("print(1)\n")
        pl = G.CommandSet("fake", d)
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
        pl = G.CommandSet("my-plug", self.set_dir(self.path("x"), "my-plug"))
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

    def test_a_guide_is_read_with_the_set_and_checked(self):
        d = self.set_dir(self.path("guided"))
        with open(os.path.join(d, "GUIDE.md"), "w") as f:
            f.write("# Fake\n\nUse queue before job.\n")
        pl = G.CommandSet("fake", d)
        self.assertIsNone(pl.error)
        self.assertEqual(pl.guide, "# Fake\n\nUse queue before job.")
        self.assertIn("    | Use queue before job.", pl.summary())
        self.assertNotEqual(pl.hash, self.load().hash)              # the blessing covers it
        for text, why in (("x" * 6001, "at most 6000"), ("a\x1b[2Jb", "hidden"), ("a\u202eb", "hidden")):
            with open(os.path.join(d, "GUIDE.md"), "w") as f:
                f.write(text)
            self.assertIn(why, G.CommandSet("fake", d).error or "", text[:5])
        with open(os.path.join(d, "GUIDE.md"), "wb") as f:
            f.write(b"\xff\xfe")
        self.assertIn("GUIDE.md: must be UTF-8", G.CommandSet("fake", d).error)

    def test_the_slurm_guide_is_valid(self):
        pl = G.CommandSet("slurm", os.path.join(REPO, "commands", "slurm"))
        self.assertIsNone(pl.error)
        self.assertIn("queue_user", pl.guide)
        named = set(re.findall(r"`([a-z_]+)`", pl.guide)) - {"squeue", "sinfo"}
        self.assertEqual(named - set(pl.commands), set())         # every command it names exists

    def test_a_usage_section_is_short_and_plain(self):
        for bad, why in (([" x" * 700], "at most"), (["l"] * 21, "at most"), (["\x1b[2Jfake"], "control or hidden"),
                         (["a\u202eb"], "control or hidden"), ("x\ty", "control or hidden"), (5, "list of lines")):
            with self.subTest(bad=bad):
                self.cmd(usage=bad)
                self.refused(why)


# ---------------------------------------------------------------- the program

class TestBinary(CommandSetBase):
    def test_resolved_from_search_never_from_path(self):
        pl = self.load()
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            pl.resolve()
        self.assertEqual(pl.commands["queue"].binary, os.path.join(os.path.realpath(self.bin), "fakeq"))
        other = self.path("elsewhere")
        os.mkdir(other, 0o755)
        program(os.path.join(other, "nothere"), "true\n")
        self.manifest["commands"]["x"] = {"argv": ["nothere"], "access": "read"}
        pl = self.load()
        with mock.patch.dict(os.environ, {"PATH": other}):
            self.assertEqual(pl.resolve(), ["x"])          # not installed here: only its command is left out
        self.assertNotIn("x", pl.commands)
        self.assertIn("queue", pl.commands)
        self.manifest["commands"] = {"x": {"argv": ["nothere"], "access": "read"}}
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
        self.manifest["commands"] = {"x": {"argv": [os.path.join(self.bin, "fakeq")], "access": "read"}}
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

class TestBlessing(CommandSetBase):
    def setUp(self):
        super().setUp()
        self.home, _ = make_home(self.tmp, "http://127.0.0.1:1/v1")
        self.cfg = G.Config(self.home)
        self.dir = self.set_dir(os.path.join(self.home, "commands"))

    def engine(self, names=("fake",)):
        notes = []

        class Ui(G.PlainUI):
            performs = True
            notice = notes.append

        ep = G.Endpoint({"name": "t", "base_url": "http://127.0.0.1:1/v1", "model": "m"}, "t.json")
        return G.Engine(self.cfg, ep, G.Session(self.cfg), Ui(), client=object(), command_sets=names), notes

    def test_unblessed_is_not_enabled(self):
        eng, notes = self.engine()
        self.assertEqual(eng.trusted, {})
        self.assertIn("unblessed", notes[0])
        self.assertEqual([t["function"]["name"] for t in eng.tools()], [t["function"]["name"] for t in G.TOOLS])

    def test_blessed_is_enabled_and_changes_are_refused(self):
        bless_set(self.home, self.dir)
        eng, notes = self.engine()
        self.assertEqual((notes, sorted(eng.trusted)[:2]), ([], ["fake__big", "fake__cached"]))
        names = [t["function"]["name"] for t in eng.tools()]
        self.assertIn("fake__queue", names)
        self.assertIn("# Scoped commands", eng.system()["content"])
        self.manifest["commands"]["queue"]["argv"] = ["fakeq", "-x", "{user}", "--fmt={fmt}"]
        write_json(os.path.join(self.dir, "commands.json"), self.manifest)
        eng2, notes = self.engine()
        self.assertEqual(eng2.trusted, {})
        self.assertIn("changed", notes[0])
        self.manifest = json.load(open(os.path.join(self.dir, "commands.json")))
        open(os.path.join(self.dir, "extra.txt"), "w").write("x")          # any file of the directory counts
        self.assertIn("changed", self.engine()[1][0])

    def test_nothing_enabled_no_tools_no_prompt(self):
        bless_set(self.home, self.dir)
        eng, _ = self.engine(())
        self.assertEqual(eng.trusted, {})
        self.assertNotIn("Scoped commands", eng.system()["content"])

    def test_a_program_in_a_group_writable_directory_refuses_the_command_set(self):
        bless_set(self.home, self.dir)
        os.chmod(self.bin, 0o770)
        eng, notes = self.engine()
        self.assertEqual(eng.trusted, {})
        self.assertIn("writable by group or others", notes[0])

    def test_tool_name_collisions(self):
        bless_set(self.home, self.dir)
        eng, _ = self.engine(())
        eng.register([{"name": "fake__queue", "description": "mine"}])
        with self.assertRaises(G.HoplonError) as cm:
            eng.set_command_set("fake", True)
        self.assertIn("already has the name fake__queue", str(cm.exception))
        eng2, _ = self.engine()
        with self.assertRaises(G.HoplonError):
            eng2.register([{"name": "fake__queue", "description": "mine"}])
        self.assertEqual(eng2.register([{"name": "other", "description": "x"}]), ["other"])

    def test_config_profile_and_listing(self):
        bless_set(self.home, self.dir)
        write_json(os.path.join(self.home, "config.json"),
                   {"default_commands": ["fake"], "command_dirs": [self.path("more")]})
        cfg = G.Config(self.home)
        self.assertEqual((cfg.default_commands, cfg.command_dirs[1]), (["fake"], self.path("more")))
        prof = G.Profile({"name": "p", "commands": ["fake"]}, "p.json")
        self.assertEqual(prof.command_sets, ["fake"])
        self.assertIn("commands: fake", prof.summary()[0])
        with self.assertRaises(G.HoplonError):
            G.Profile({"name": "p", "commands": "fake"}, "p.json")
        rows = G.command_set_rows(self.cfg, ["fake"])
        self.assertEqual((rows[0]["name"], rows[0]["status"], rows[0]["enabled"], rows[0]["blessed"]),
                         ("fake", "blessed", True, True))
        self.assertEqual(rows[0]["commands"][0]["name"], "queue")

    def test_bless_shows_the_commands(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertTrue(G.bless(self.cfg, self.dir, lambda q: True))
        text = out.getvalue()
        self.assertIn("Command set fake", text)
        self.assertIn("[asks every time]", text)
        self.assertIn("fakeq -u {user} --fmt={fmt}", text)
        self.assertIn("program: " + os.path.realpath(self.bin), text)
        self.assertIn("your PATH is never used", text)
        self.assertEqual(self.cfg.blessed()[self.dir]["kind"], "command_set")
        self.assertEqual(G.command_set_status(self.cfg, G.find_command_sets(self.cfg)["fake"]), "blessed")

    def test_reads_and_writes_are_listed_apart_and_a_write_may_be_let_run(self):
        asked, out = [], io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertTrue(G.bless(self.cfg, self.dir, lambda q: asked.append(q) or True))
        text = out.getvalue()
        reads, writes = text.index("read: these only look"), text.index("write: these change something")
        self.assertLess(reads, text.index("command queue:"))
        self.assertLess(text.index("command queue:"), writes)
        self.assertLess(writes, text.index("command kill:"))
        self.assertEqual(len(asked), 2)                       # the set, then its one write command
        self.assertIn("Let the model run kill (fakeq kill {id}) without asking you?", asked[1])
        self.assertEqual(self.cfg.blessed()[self.dir]["auto"], ["kill"])
        eng, _ = self.engine()
        kill = eng.trusted["fake__kill"]
        self.assertTrue(kill.auto)
        self.assertIn("runs without asking, as the user chose", kill.schema("h")["function"]["description"])
        self.assertEqual(G.command_set_rows(self.cfg, ["fake"])[0]["commands"][2]["name"], "kill")

    def test_a_write_asks_unless_the_user_said_otherwise(self):
        answers = iter([True, False])
        with mock.patch.object(sys, "stdout", io.StringIO()):
            G.bless(self.cfg, self.dir, lambda q: next(answers))
        self.assertEqual(self.cfg.blessed()[self.dir]["auto"], [])
        eng, _ = self.engine()
        self.assertFalse(eng.trusted["fake__kill"].auto)
        self.assertTrue(eng.trusted["fake__queue"].auto)
        self.assertIn("the user is asked first", eng.trusted["fake__kill"].schema("h")["function"]["description"])
        with mock.patch.object(sys, "stdout", io.StringIO()):     # declined: nothing is recorded, nothing more asked
            self.assertFalse(G.bless(self.cfg, self.dir, lambda q: False))

    def sudo_set(self):
        program(os.path.join(self.bin, "sudo"), f'for a in "$@"; do printf "ARG:%s\\n" "$a"; done >> {self.log}\n'
                f'echo "---" >> {self.log}\necho "as root: $*"\n')
        self.manifest["commands"]["status"] = {"describe": "a service's state", "access": "read", "min_interval": 0,
                                               "argv": ["sudo", "-n", "/usr/bin/systemctl", "status", "{unit}"],
                                               "params": {"unit": {"enum": ["nginx", "cron"]}}}
        write_json(os.path.join(self.dir, "commands.json"), self.manifest)

    def test_a_command_run_as_root_is_listed_apart_and_asks_even_to_read(self):
        self.sudo_set()
        asked, out = [], io.StringIO()
        answers = iter([True, False, False])               # the set; kill: no; status: no
        with mock.patch.object(sys, "stdout", out):
            self.assertTrue(G.bless(self.cfg, self.dir, lambda q: asked.append(q) or next(answers)))
        text = out.getvalue()
        self.assertIn("[!] AS ANOTHER USER (sudo, doas, run0)", text)
        self.assertLess(text.index("AS ANOTHER USER"), text.index("command status:"))
        self.assertIn("[runs as root, read, asks every time]", text)
        self.assertIn("allow exactly these commands, with -n, in sudoers", text)
        self.assertEqual(len(asked), 3)
        self.assertIn("Let the model run status (sudo -n /usr/bin/systemctl status {unit}) AS ROOT without asking",
                      asked[2])
        eng, _ = self.engine()
        status = eng.trusted["fake__status"]
        self.assertEqual((status.as_user, status.auto), ("root", False))
        self.assertIn("read-only; runs as root; the user is asked first",
                      status.schema("h")["function"]["description"])
        row = [c for c in G.command_set_rows(self.cfg, ["fake"])[0]["commands"] if c["name"] == "status"][0]
        self.assertEqual((row["as"], row["auto"]), ("root", False))
        self.assertIsNone([c for c in G.command_set_rows(self.cfg, ["fake"])[0]["commands"]
                           if c["name"] == "queue"][0]["as"])
        with self.assertRaises(G.HoplonError) as e:                # a pane can't run it either
            eng.run_user("fake", "status", {"unit": "nginx"})
        self.assertIn("runs as root, so it asks every time", str(e.exception))

    def test_a_command_run_as_root_may_be_let_run_when_blessing(self):
        self.sudo_set()
        with mock.patch.object(sys, "stdout", io.StringIO()):
            G.bless(self.cfg, self.dir, lambda q: True)
        self.assertEqual(sorted(self.cfg.blessed()[self.dir]["auto"]), ["kill", "status"])
        eng, _ = self.engine()
        self.assertTrue(eng.trusted["fake__status"].auto)
        self.assertIn("runs as root; runs without asking, as the user chose",
                      eng.trusted["fake__status"].schema("h")["function"]["description"])
        r = eng.run_user("fake", "status", {"unit": "cron"})
        self.assertEqual(r["output"], "as root: -n /usr/bin/systemctl status cron\n")

    def test_the_guide_goes_to_the_model_while_the_set_is_on(self):
        with open(os.path.join(self.dir, "GUIDE.md"), "w") as f:
            f.write("Use queue before job.\n")
        bless_set(self.home, self.dir)
        eng, _ = self.engine()
        system = eng.system()["content"]
        self.assertIn("# Command set: fake\n\nThe user blessed this guide", system)
        self.assertIn("Use queue before job.", system)
        eng.set_command_set("fake", False)
        self.assertNotIn("Use queue before job.", eng.system()["content"])

    def test_bless_refuses_a_bad_manifest(self):
        self.manifest["commands"]["queue"]["argv"] = ["fakeq", "{nope}"]
        write_json(os.path.join(self.dir, "commands.json"), self.manifest)
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


class TestRunning(CommandSetBase):
    def setUp(self):
        super().setUp()
        self.home, _ = make_home(self.tmp, "http://127.0.0.1:1/v1")
        self.cfg = G.Config(self.home)
        bless_set(self.home, self.set_dir(os.path.join(self.home, "commands")))
        self.ui = Ui()
        ep = G.Endpoint({"name": "t", "base_url": "http://127.0.0.1:1/v1", "model": "m"}, "t.json")
        self.eng = G.Engine(self.cfg, ep, G.Session(self.cfg), self.ui, client=object(), command_sets=["fake"])
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
        self.assertEqual({k: v for k, v in p.event().items() if k in ("set", "command", "params", "access")},
                         {"set": "fake", "command": "queue", "params": {"user": "alice", "fmt": "long"},
                          "access": "read"})
        self.assertTrue(told.startswith("exit 3. Output:\nresult -u alice --fmt=long\n[stderr]\nwarning"), told)
        self.assertEqual(self.calls()[0]["args"], ["-u", "alice", "--fmt=long"])
        self.assertEqual(self.ui.results[0][1:3][0], "error")          # exit 3
        self.assertEqual(self.ui.results[0][3]["exit"], 3)
        rec = self.audit()[0]
        self.assertEqual([rec[k] for k in ("set", "command", "source", "exit", "cached", "rate_limited")],
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
        write_json(os.path.join(self.home, "commands", "fake", "commands.json"), self.manifest)
        bless_set(self.home, os.path.join(self.home, "commands", "fake"))
        self.eng.set_command_set("fake", True)
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

    def test_a_read_run_as_root_asks_and_says_so(self):
        program(os.path.join(self.bin, "sudo"), 'echo "as root: $*"\n')
        self.manifest["commands"]["status"] = {"describe": "a service's state", "access": "read", "min_interval": 0,
                                               "argv": ["sudo", "-n", "/usr/bin/systemctl", "status", "{unit}"],
                                               "params": {"unit": {"enum": ["nginx"]}}}
        write_json(os.path.join(self.home, "commands", "fake", "commands.json"), self.manifest)
        bless_set(self.home, os.path.join(self.home, "commands", "fake"))
        self.eng.set_command_set("fake", True)
        p, told = self.model("fake__status", unit="nginx")
        self.assertEqual((p.kind, p.auto, p.decision, told), ("trusted", False, "decline", "declined by the user"))
        self.assertEqual(p.event()["as_user"], "root")
        self.assertNotIn("as_user", self.model("fake__queue", user="a", fmt="short")[0].event())
        self.ui.answer = "accept"
        p, told = self.model("fake__status", unit="nginx")
        self.assertIn("as root: -n /usr/bin/systemctl status nginx", told)

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
                       command_sets=["fake"])
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
        self.eng.set_command_set("fake", False)
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

class TestJson(CommandSetBase):
    def start(self, api, args=("--commands", "fake"), bless=True):
        home, ep = make_home(self.tmp, api.url)
        bless_path(home, ep)
        d = self.set_dir(os.path.join(home, "commands"))
        if bless:
            bless_set(home, d)
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
        self.assertIn("# Scoped commands", system)
        where = system.split("# Where things are", 1)[1]
        self.assertIn("Scoped commands (set__command tools) run on this host too", where)
        self.assertIn("what the user shares with you and what scoped commands return", where)

    def test_hello_and_command_sets_event(self):
        with FakeAPI([]) as api:
            g = self.start(api)
            hello = g.until("hello")
            self.assertIn("command_sets", hello["features"])
            self.assertEqual(hello["command_sets"], ["fake"])
            ev = g.until("command_sets")
            (row,) = ev["command_sets"]
            self.assertEqual((row["name"], row["enabled"], row["blessed"], row["pane"], row["dir"]),
                             ("fake", True, True, None, os.path.join(self.home, "commands", "fake")))
            queue = next(c for c in row["commands"] if c["name"] == "queue")
            self.assertEqual((queue["access"], queue["auto"]), ("read", True))
            self.assertEqual(queue["argv"], ["fakeq", "-u", "{user}", "--fmt={fmt}"])
            self.assertEqual(queue["params"]["fmt"], {"enum": ["short", "long"], "describe": ""})
            g.send({"type": "command_sets"})
            self.assertEqual(g.until("command_sets")["command_sets"][0]["name"], "fake")
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
        self.assertIn("# Scoped commands", first["messages"][0]["content"])
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

    def test_unblessed_command_set_is_refused_with_a_notice(self):
        with FakeAPI([]) as api:
            g = self.start(api, bless=False)
            n = g.until("notice")
            self.assertIn("unblessed", n["message"])
            self.assertEqual(g.until("hello")["command_sets"], [])
            g.close()

    def test_not_read_only_asks_through_a_decision(self):
        with FakeAPI([call("fake__kill", id=4), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "stop it"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p.get("auto"), p["access"]), ("trusted", None, "write"))
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
            g.send({"type": "run", "id": "r1", "set": "fake", "command": "job", "params": {"id": "42"}})
            ran = g.until("ran")
            self.assertEqual((ran["id"], ran["exit"], ran["cached"]), ("r1", 3, False))
            self.assertIn("result job 42", ran["output"])
            self.assertIsInstance(ran["ms"], int)
            g.send({"type": "run", "id": 2, "set": "fake", "command": "job", "params": {"id": "4;2"}})
            bad = g.until("ran")
            self.assertEqual(bad["id"], 2)
            self.assertIn("parameter 'id'", bad["error"])
            self.assertNotIn("output", bad)
            g.send({"type": "run", "id": 3, "set": "nope", "command": "job", "params": {}})
            self.assertIn("no command", g.until("ran")["error"])
            g.send({"type": "run", "set": "fake", "command": "job", "params": {"id": "1"}})
            self.assertEqual(g.until("error")["code"], "bad_message")
            g.close()
        runs = [e for e in self.audit() if e["event"] == "trusted_run"]
        self.assertEqual([r["source"] for r in runs], ["user"])
        self.assertEqual(len(self.calls()), 1)

    def test_run_needs_an_enabled_command_set_and_can_be_switched(self):
        with FakeAPI([]) as api:
            g = self.start(api, args=())
            g.send({"type": "run", "id": 1, "set": "fake", "command": "job", "params": {"id": "1"}})
            self.assertIn("no command", g.until("ran")["error"])
            g.send({"type": "command_set", "name": "fake", "on": True})
            self.assertTrue(next(r for r in g.until("command_sets")["command_sets"] if r["name"] == "fake")["enabled"])
            g.send({"type": "run", "id": 2, "set": "fake", "command": "job", "params": {"id": "1"}})
            self.assertEqual(g.until("ran")["exit"], 3)
            g.send({"type": "command_set", "name": "nope", "on": True})
            self.assertEqual(g.until("error")["code"], "unknown_commands")
            g.send({"type": "command_set", "name": "fake", "on": False})
            g.until("command_sets")
            g.send({"type": "command_set", "name": "fake"})
            self.assertEqual(g.until("error")["code"], "bad_message")
            g.close()

    def test_run_is_busy_while_a_decision_is_pending(self):
        with FakeAPI([call("fake__kill", id=4), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "stop it"})
            p = g.until("proposal")
            g.send({"type": "run", "id": 9, "set": "fake", "command": "job", "params": {"id": "1"}})
            self.assertEqual(g.until("error")["code"], "busy")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
