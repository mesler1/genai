"""Offline tests for genai. Run: python3 -m unittest discover -s tests"""

import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "genai")
sys.path.insert(0, HERE)
from fakeapi import FakeAPI, call, calls, reply  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("genai_mod", SCRIPT)
_spec = importlib.util.spec_from_loader("genai_mod", _loader)
G = importlib.util.module_from_spec(_spec)
_loader.exec_module(G)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="genai-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *p):
        return os.path.join(self.tmp, *p)


# ---------------------------------------------------------------- path check

class TestWorkdir(Tmp):
    def setUp(self):
        super().setUp()
        self.root = self.path("wd")
        os.makedirs(self.path("wd", "sub"))
        os.makedirs(self.path("outside"))
        with open(self.path("outside", "secret"), "w") as f:
            f.write("s3cret")
        with open(self.path("wd", "a.txt"), "w") as f:
            f.write("hello\n")
        self.wd = G.Workdir(self.root)

    def test_rejects_dotdot_and_absolute(self):
        for bad in ("../outside/secret", "sub/../../outside/secret", "/etc/passwd", "~/x", "", "a\0b"):
            with self.assertRaises(ValueError, msg=bad):
                self.wd.read(bad)

    def test_rejects_symlinked_file(self):
        os.symlink(self.path("outside", "secret"), self.path("wd", "link"))
        with self.assertRaises(ValueError):
            self.wd.read("link")
        with self.assertRaises(ValueError):
            self.wd.write("link", "x")
        self.assertEqual(open(self.path("outside", "secret")).read(), "s3cret")

    def test_rejects_symlinked_directory_component(self):
        os.symlink(self.path("outside"), self.path("wd", "ld"))
        with self.assertRaises(ValueError):
            self.wd.read("ld/secret")
        with self.assertRaises(ValueError):
            self.wd.write("ld/new", "x")
        with self.assertRaises(ValueError):
            self.wd.list("ld")
        self.assertFalse(os.path.exists(self.path("outside", "new")))

    def test_read_write_list(self):
        self.assertEqual(self.wd.read("a.txt"), "hello\n")
        self.assertEqual(self.wd.read("./a.txt"), "hello\n")
        self.wd.write("sub/deeper/b.sh", "echo hi\n")
        self.assertEqual(open(self.path("wd", "sub", "deeper", "b.sh")).read(), "echo hi\n")
        entries, truncated = self.wd.list(".")
        self.assertFalse(truncated)
        names = [n for n, _, _ in entries]
        self.assertEqual(names, ["a.txt", "sub"])
        self.assertEqual([x for x in os.listdir(self.path("wd", "sub", "deeper")) if x.startswith(".genai")], [])

    def test_write_keeps_mode(self):
        p = self.path("wd", "x.sh")
        with open(p, "w") as f:
            f.write("old")
        os.chmod(p, 0o750)
        self.wd.write("x.sh", "new")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o750)
        self.assertEqual(open(p).read(), "new")

    def test_write_stages_under_a_private_directory(self):
        target = self.path("wd", "a.txt")
        os.chmod(target, 0o600)
        fsync = os.fsync

        def check_staging(fd):
            (stage,) = [name for name in os.listdir(self.root) if name.startswith(".genai-tmp-")]
            self.assertEqual(stat.S_IMODE(os.stat(self.path("wd", stage)).st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(os.fstat(fd).st_mode), 0o600)
            return fsync(fd)

        with mock.patch.object(G.os, "fsync", side_effect=check_staging):
            self.wd.write("a.txt", "replacement")
        self.assertEqual(self.wd.read("a.txt"), "replacement")

    def test_rejects_changes_during_staging(self):
        expected, _ = self.wd.snapshot("a.txt")
        fsync = os.fsync

        def concurrent_update(fd):
            with open(self.path("wd", "a.txt"), "w") as f:
                f.write("concurrent update")
            return fsync(fd)

        with mock.patch.object(G.os, "fsync", side_effect=concurrent_update):
            with self.assertRaises(G.StaleApproval):
                self.wd.write("a.txt", "replacement", expected=expected)
        self.assertEqual(self.wd.read("a.txt"), "concurrent update")

    def test_new_file_publication_refuses_a_concurrent_creator(self):
        link = os.link

        def concurrent_create(*args, **kwargs):
            with open(self.path("wd", "new.txt"), "x") as f:
                f.write("concurrent update")
            return link(*args, **kwargs)

        with mock.patch.object(G.os, "link", side_effect=concurrent_create):
            with self.assertRaises(G.StaleApproval):
                self.wd.write("new.txt", "replacement", expected=None)
        self.assertEqual(self.wd.read("new.txt"), "concurrent update")

    def test_existing_file_snapshot_is_bounded(self):
        with mock.patch.object(G, "SNAPSHOT_CAP", 4):
            with self.assertRaisesRegex(G.DiskError, "overwrite limit"):
                self.wd.snapshot("a.txt")

    def test_directory_listing_is_bounded(self):
        with mock.patch.object(G, "LIST_CAP", 1):
            entries, truncated = self.wd.list(".")
        self.assertEqual(len(entries), 1)
        self.assertTrue(truncated)

    def test_binary_fifo_and_caps(self):
        with open(self.path("wd", "bin"), "wb") as f:
            f.write(b"\x00\x01\x02")
        with self.assertRaises(ValueError):
            self.wd.read("bin")
        os.mkfifo(self.path("wd", "fifo"))
        with self.assertRaises(ValueError):
            self.wd.read("fifo")
        with open(self.path("wd", "big"), "w") as f:
            f.write("x" * (G.READ_CAP + 1))
        with self.assertRaises(ValueError):
            self.wd.read("big")
        with self.assertRaises(ValueError):
            self.wd.write("huge", "x" * (G.WRITE_CAP + 1))

    def test_hidden(self):
        self.assertTrue(G.is_hidden(G.split_rel(".env")))
        self.assertTrue(G.is_hidden(G.split_rel("a/.git/config")))
        self.assertFalse(G.is_hidden(G.split_rel("./a/b")))


# ---------------------------------------------------------------- display and redaction

class TestExactAndRedact(unittest.TestCase):
    def test_exact_flags_controls_and_bidi(self):
        disp, flagged = G.exact("ls\x1b[2J ‮gnp.exe")
        self.assertTrue(flagged)
        self.assertNotIn("\x1b", disp)
        self.assertNotIn("‮", disp)
        self.assertIn("\\x1b", disp)
        self.assertIn("\\u202e", disp)
        self.assertEqual(G.exact("echo 'a\tb'\nls"), ("echo 'a\tb'\nls", False))

    def test_redact(self):
        text = ("token=abcd1234efgh\nAuthorization: Bearer abcdefghijklmnopqrstuv\n"
                "AKIAABCDEFGHIJKLMNOP ghp_" + "a" * 36 + " https://u:pw123@host/x\n"
                "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n")
        out, counts = G.redact(text)
        for secret in ("abcd1234efgh", "abcdefghijklmnopqrstuv", "AKIAABCDEFGHIJKLMNOP", "pw123", "MIIE"):
            self.assertNotIn(secret, out)
        self.assertIn("token=[REDACTED]", out)
        self.assertGreaterEqual(sum(counts.values()), 6)
        self.assertEqual(G.redact("nothing to see")[1], {})

    def test_clip(self):
        out = G.clip("a" * 100 + "b" * 100, cap=60)
        self.assertTrue(out.startswith("a" * 40) and out.endswith("b" * 20))
        self.assertIn("characters cut", out)


# ---------------------------------------------------------------- endpoints and blessing

def make_home(root, url, name="test", **extra):
    home = os.path.join(root, "home", "genai")
    os.makedirs(os.path.join(home, "endpoints"))
    prof = {"name": name, "base_url": url, "model": "fake-model", "context_limit": 100000}
    prof.update(extra)
    path = os.path.join(home, "endpoints", f"{name}.json")
    with open(path, "w") as f:
        json.dump(prof, f)
    return home, path


def bless_path(home, path):
    cfg = G.Config(home)
    h = G.hash_skill(path)[0] if os.path.isdir(path) else G.hash_file(path)
    kind = "skill" if os.path.isdir(path) else ("endpoint" if "base_url" in json.load(open(path)) else "profile")
    name = os.path.basename(path) if kind == "skill" else json.load(open(path))["name"]
    cfg.set_blessed(os.path.abspath(path), {"kind": kind, "name": name, "hash": h})


class TestEndpoints(Tmp):
    def prof(self, **kw):
        d = {"name": "x", "base_url": "https://llm.example/v1", "model": "m"}
        d.update(kw)
        return G.Endpoint(d, "x.json")

    def test_https_rules(self):
        self.prof()
        self.prof(base_url="http://127.0.0.1:8000/v1")
        self.prof(base_url="http://localhost/v1")
        with self.assertRaises(G.GenaiError):
            self.prof(base_url="http://llm.example/v1")
        self.prof(base_url="http://llm.example/v1", allow_http=True)
        with self.assertRaises(G.GenaiError):
            self.prof(base_url="https://user:pw@llm.example/v1")

    def test_strict_fields(self):
        with self.assertRaises(G.GenaiError):
            self.prof(key_command="pass show x")
        with self.assertRaises(G.GenaiError):
            self.prof(headers={"Authorization": "Bearer x"})
        with self.assertRaises(G.GenaiError):
            self.prof(key_env="A", key_file="/b")
        with self.assertRaises(G.GenaiError):
            self.prof(params={"model": "other"})

    def test_key_file_mode(self):
        kf = self.path("key")
        with open(kf, "w") as f:
            f.write("k\n")
        os.chmod(kf, 0o644)
        with self.assertRaises(G.GenaiError):
            self.prof(key_file=kf).key()
        os.chmod(kf, 0o600)
        self.assertEqual(self.prof(key_file=kf).key(), "k")

    def test_key_env(self):
        os.environ["GENAI_TEST_KEY"] = "abc"
        self.addCleanup(os.environ.pop, "GENAI_TEST_KEY", None)
        self.assertEqual(self.prof(key_env="GENAI_TEST_KEY").key(), "abc")
        with self.assertRaises(G.GenaiError):
            self.prof(key_env="GENAI_TEST_KEY_UNSET").key()

    def test_selection_requires_blessing(self):
        home, path = make_home(self.tmp, "https://llm.example/v1")
        cfg = G.Config(home)
        with self.assertRaises(G.GenaiError) as e:
            G.select_endpoint(cfg, None)
        self.assertEqual(e.exception.code, G.EXIT_PROFILE)
        with self.assertRaises(G.GenaiError):
            G.select_endpoint(cfg, "test")           # unblessed
        bless_path(home, path)
        self.assertEqual(G.select_endpoint(cfg, "test").name, "test")
        with open(path, "a") as f:
            f.write(" ")
        with self.assertRaises(G.GenaiError):
            G.select_endpoint(cfg, "test")           # changed

    def test_blessed_shared_endpoint_found_by_name(self):
        home, _ = make_home(self.tmp, "https://llm.example/v1")
        shared = self.path("shared.json")
        with open(shared, "w") as f:
            json.dump({"name": "team", "base_url": "https://team.example/v1", "model": "m"}, f)
        bless_path(home, shared)
        self.assertEqual(G.select_endpoint(G.Config(home), "team").base_url, "https://team.example/v1")

    def test_example_endpoint_is_valid(self):
        p = G.Endpoint.load(os.path.join(os.path.dirname(HERE), "endpoints", "example.json"))
        self.assertTrue(p.base_url.startswith("https://"))
        self.assertEqual(p.key_env, "GENAI_API_KEY")


class TestSkills(Tmp):
    def test_hash_changes_and_symlinks(self):
        d = self.path("slurm")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w") as f:
            f.write("# Slurm help\nUse squeue.\n")
        h1, _ = G.hash_skill(d)
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("x")
        self.assertNotEqual(h1, G.hash_skill(d)[0])
        os.symlink("/etc/hostname", os.path.join(d, "link"))
        with self.assertRaises(G.GenaiError):
            G.hash_skill(d)

    def test_unblessed_skill_skipped(self):
        home = self.path("home", "genai")
        d = os.path.join(home, "skills", "slurm")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w") as f:
            f.write("# Slurm help\n")
        cfg = G.Config(home)
        notes = []
        self.assertEqual(G.load_skills(cfg, ["slurm", "missing"], notes.append), [])
        self.assertEqual(len(notes), 2)
        bless_path(home, d)
        self.assertEqual([s.name for s in G.load_skills(cfg, ["slurm"], notes.append)], ["slurm"])
        with open(os.path.join(d, "SKILL.md"), "a") as f:
            f.write("edit")
        self.assertEqual(G.load_skills(cfg, ["slurm"], notes.append), [])
        self.assertIn("changed", notes[-1])

    def test_repl_skill_command(self):
        home = self.path("home", "genai")
        d = os.path.join(home, "skills", "slurm")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w") as f:
            f.write("# Slurm help\nSLURM-MARKER\n")
        cfg = G.Config(home)
        cfg.ensure()
        ep = G.Endpoint({"name": "t", "base_url": "https://llm.example/v1", "model": "m"}, "t.json")
        eng = G.Engine(cfg, ep, G.Session(cfg), G.PlainUI())
        repl, out = G.Repl(eng), io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            repl.command(":skill slurm on")
            self.assertEqual(eng.skills, [])                        # unblessed: refused
            bless_path(home, d)
            repl.command(":skill slurm on")
            self.assertIn("SLURM-MARKER", eng.system()["content"])
            repl.command(":skills")
            repl.command(":skill slurm")
            repl.command(":skill slurm off")
        self.assertEqual(eng.skills, [])
        self.assertNotIn("SLURM-MARKER", eng.system()["content"])
        text = out.getvalue()
        self.assertIn("is unblessed", text)
        self.assertRegex(text, r"(?m)^\* slurm +blessed")
        self.assertIn("usage: :skill NAME on|off", text)
        self.assertRegex(text, r"(?m)^\* slurm .*\n {6}" + re.escape(d) + "$")   # the path, under its line



class TestFirstRun(Tmp):
    def run_genai(self, *args):
        env = dict(os.environ, XDG_CONFIG_HOME=self.path("xdg"))
        return subprocess.run([sys.executable, SCRIPT, *args], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30)

    def test_creates_config_tree(self):
        r = self.run_genai("--list-skills", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["skills"], [])        # stdout stays clean JSON
        self.assertIn("genai: created", r.stderr)
        home = self.path("xdg", "genai")
        for d in ("", "endpoints", "profiles", "skills", "sessions"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(home, d)).st_mode), 0o700, d)
        conf = os.path.join(home, "config.json")
        self.assertEqual(stat.S_IMODE(os.stat(conf).st_mode), 0o600)
        data = json.load(open(conf))
        self.assertIsNone(data["default_profile"])                    # only the user sets it
        self.assertEqual({k for k in data if not k.startswith("_")},
                         {"default_profile", "default_endpoint", "default_skills",
                          "endpoint_dirs", "profile_dirs", "skill_dirs"})
        self.assertEqual(G.Config(home).default_profile, None)        # the starter file loads cleanly

    def test_config_typo_warns_and_bad_type_fails(self):
        home = self.path("xdg", "genai")
        os.makedirs(home)
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"default_profle": "x", "_note": "mine"}, f)
        r = self.run_genai("--list-skills")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("unknown setting(s) ignored: default_profle", r.stderr)
        self.assertNotIn("_note", r.stderr)
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"skill_dirs": "/opt/skills"}, f)
        r = self.run_genai("--list-skills")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("skill_dirs must be a list", r.stderr)

    def test_list_skills_shows_the_path_to_bless(self):
        self.run_genai("--list-skills")
        home = self.path("xdg", "genai")
        for name in ("slurm", "my notes"):
            os.makedirs(os.path.join(home, "skills", name))
            with open(os.path.join(home, "skills", name, "SKILL.md"), "w") as f:
                f.write(f"# {name} help\n")
        bless_path(home, os.path.join(home, "skills", "slurm"))
        lines = self.run_genai("--list-skills").stdout.splitlines()
        self.assertEqual(lines[1], "    to bless: genai --bless " + shlex.quote(os.path.join(home, "skills", "my notes")))
        self.assertEqual(lines[3], "    " + os.path.join(home, "skills", "slurm"))
        self.assertTrue(lines[0].startswith("my notes ") and lines[2].startswith("slurm "))

    def test_second_run_is_quiet_and_keeps_existing(self):
        self.run_genai("--list-skills")
        home = self.path("xdg", "genai")
        os.chmod(os.path.join(home, "skills"), 0o750)
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"default_profile": "mine"}, f)
        shutil.rmtree(os.path.join(home, "sessions"))
        r = self.run_genai("--list-skills")
        self.assertNotIn("genai: created", r.stderr)
        self.assertTrue(os.path.isdir(os.path.join(home, "sessions")))      # missing piece restored
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(home, "skills")).st_mode), 0o750)
        self.assertEqual(json.load(open(os.path.join(home, "config.json"))), {"default_profile": "mine"})


class TestBundledSkills(unittest.TestCase):
    def test_bundled_skills_are_valid(self):
        root = os.path.join(os.path.dirname(HERE), "skills")
        names = sorted(os.listdir(root))
        self.assertIn("genai-profile", names)
        self.assertIn("genai-endpoint", names)
        self.assertIn("genai-skill", names)
        for n in names:
            G.hash_skill(os.path.join(root, n))
            s = G.Skill(n, os.path.join(root, n))
            self.assertTrue(s.description(), n)
            self.assertLess(len(s.text().splitlines()), 150, n)

# ---------------------------------------------------------------- end to end

class Proc:
    """genai --json as a subprocess, driven line by line."""

    def __init__(self, home, cwd, args):
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home), HTTPS_PROXY="http://127.0.0.1:9",
                   https_proxy="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9", http_proxy="http://127.0.0.1:9")
        env.pop("NO_PROXY", None)
        env.pop("no_proxy", None)
        self.p = subprocess.Popen([sys.executable, SCRIPT, "--json"] + args, cwd=cwd, env=env, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.events = []

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def until(self, *types):
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise AssertionError(f"EOF waiting for {types}; stderr: {self.p.stderr.read()}")
            ev = json.loads(line)
            self.events.append(ev)
            if ev["type"] in types:
                return ev

    def close(self):
        self.p.stdin.close()
        rest = self.p.stdout.read()
        self.events += [json.loads(x) for x in rest.splitlines() if x.strip()]
        rc = self.p.wait(10)
        self.p.stdout.close()
        self.p.stderr.close()
        return rc


class TestEndToEnd(Tmp):
    def start(self, api, *args):
        home, path = make_home(self.tmp, api.url)
        bless_path(home, path)
        self.home = home
        self.wd = self.path("wd")
        os.makedirs(self.wd, exist_ok=True)
        return Proc(home, self.wd, ["-e", "test"] + list(args))

    def audit(self):
        with open(os.path.join(self.home, "audit.log")) as f:
            return [json.loads(x) for x in f]

    def test_no_endpoint_sends_nothing(self):
        with FakeAPI([]) as api:
            home, _ = make_home(self.tmp, api.url)
            r = subprocess.run([sys.executable, SCRIPT, "hello"], env=dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home)),
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, G.EXIT_PROFILE)
            self.assertIn("no endpoint is configured", r.stderr)
            self.assertEqual(api.requests, [])

    def test_hello_and_plain_reply(self):
        with FakeAPI([reply("Hello there, human.")]) as api:
            g = self.start(api)
            hello = g.until("hello")
            self.assertEqual(hello["protocol"], G.PROTOCOL)
            self.assertEqual(hello["endpoint"]["base_url"], api.url)
            g.send({"type": "user", "text": "hi"})
            done = g.until("done")
            self.assertEqual(done["reason"], "reply")
            self.assertEqual("".join(e["text"] for e in g.events if e["type"] == "text"), "Hello there, human.")
            self.assertEqual(g.close(), 0)
            self.assertEqual(api.requests[0]["path"], "/v1/chat/completions")
            self.assertEqual(api.requests[0]["body"]["model"], "fake-model")

    def test_write_accept_then_edit_decline(self):
        turns = [call("write_file", path="rotate.sh", content="echo one\n", reason="make script"),
                 call("edit_file", path="rotate.sh", old_text="one", new_text="two", reason="fix"),
                 reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "write it"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p["path"], p["exists"]), ("write", "rotate.sh", False))
            self.assertFalse(os.path.exists(os.path.join(self.wd, "rotate.sh")))
            g.send({"type": "decision", "id": p["id"], "decision": "accept"})
            self.assertEqual(g.until("result")["status"], "ok")
            p2 = g.until("proposal")
            self.assertEqual(p2["kind"], "edit")
            self.assertIn("-echo one", p2["diff"])
            g.send({"type": "decision", "id": p2["id"], "decision": "decline"})
            g.until("done")
            g.close()
            self.assertEqual(open(os.path.join(self.wd, "rotate.sh")).read(), "echo one\n")
            tool_msgs = [m for m in api.requests[-1]["body"]["messages"] if m["role"] == "tool"]
            self.assertIn("wrote rotate.sh", tool_msgs[0]["content"])
            self.assertEqual(tool_msgs[1]["content"], "declined by the user")
            events = [a["event"] for a in self.audit()]
            self.assertIn("proposal", events)
            self.assertIn("file_op", events)

    def test_eof_means_decline(self):
        with FakeAPI([call("write_file", path="x", content="y", reason="r")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "go"})
            g.until("proposal")
            g.close()
            self.assertFalse(os.path.exists(os.path.join(self.wd, "x")))

    def test_unknown_id_and_type_rejected(self):
        with FakeAPI([call("read_file", path="a", reason="r"), reply("fine")]) as api:
            g = self.start(api)
            g.send({"type": "bogus"})
            self.assertEqual(g.until("error")["code"], "unknown_type")
            g.send({"type": "user", "text": "go"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": "p999", "decision": "accept"})
            self.assertEqual(g.until("error")["code"], "unknown_id")
            g.send({"type": "decision", "id": p["id"], "decision": "decline"})
            g.until("done")
            g.close()

    def test_command_proposal_ends_turn(self):
        turns = [call("propose_command", command="scontrol show job 48211", reason="why pending"), reply("later")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "why pending?"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p["command"]), ("command", "scontrol show job 48211"))
            self.assertEqual(g.until("done")["reason"], "command")
            g.close()
            self.assertEqual(len(api.requests), 1)
            self.assertEqual(len(api.turns), 1)

    def test_hidden_command_text_is_flagged(self):
        with FakeAPI([call("propose_command", command="ls ‮; rm -rf x", reason="r")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "x"})
            p = g.until("proposal")
            self.assertTrue(p["flagged"])
            self.assertIn("\\u202e", p["display"])
            g.close()

    def test_path_escape_is_refused_without_asking(self):
        with FakeAPI([call("read_file", path="../../etc/passwd", reason="r"), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "x"})
            g.until("done")
            g.close()
            self.assertFalse([e for e in g.events if e["type"] == "proposal"])
            tool = [m for m in api.requests[-1]["body"]["messages"] if m["role"] == "tool"][0]
            self.assertIn("'..' is not allowed", tool["content"])

    def test_coding_mode_auto_reads_but_hidden_asks(self):
        turns = [calls(("read_file", {"path": "a.txt", "reason": "r"}),
                       ("read_file", {"path": ".env", "reason": "r"}),
                       ("write_file", {"path": "b.txt", "content": "x", "reason": "r"})),
                 reply("done")]
        with FakeAPI(turns) as api:
            g = self.start(api, "--coding")
            with open(os.path.join(self.wd, "a.txt"), "w") as f:
                f.write("visible")
            with open(os.path.join(self.wd, ".env"), "w") as f:
                f.write("TOKEN=abc")
            g.send({"type": "user", "text": "x"})
            auto = g.until("proposal")
            self.assertTrue(auto.get("auto"))
            p_env, p_write = g.until("proposal"), g.until("proposal")
            self.assertFalse(p_env.get("auto"))
            self.assertEqual(p_write["kind"], "write")
            # batch: decide in reverse order
            g.send({"type": "decision", "id": p_write["id"], "decision": "decline"})
            g.send({"type": "decision", "id": p_env["id"], "decision": "decline"})
            g.until("done")
            g.close()
            tools = [m["content"] for m in api.requests[-1]["body"]["messages"] if m["role"] == "tool"]
            self.assertEqual(tools, ["visible", "declined by the user", "declined by the user"])

    def test_coding_refused_in_home(self):
        with FakeAPI([]) as api:
            home, path = make_home(self.tmp, api.url)
            bless_path(home, path)
            fake_home = self.path("fakehome")
            os.makedirs(fake_home)
            env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home), HOME=fake_home)
            r = subprocess.run([sys.executable, SCRIPT, "-e", "test", "--coding", "hi"], cwd=fake_home, env=env,
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, G.EXIT_USAGE)
            self.assertIn("coding mode is refused", r.stderr)

    def test_wrapper_turns_coding_on_and_off(self):
        turns = [call("read_file", path="a.txt", reason="r"), reply("done"),
                 call("read_file", path="a.txt", reason="r"), reply("done")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            with open(os.path.join(self.wd, "a.txt"), "w") as f:
                f.write("visible")
            hello = g.until("hello")
            self.assertIn("set_coding", hello["features"])
            g.send({"type": "coding", "on": "yes"})
            self.assertEqual(g.until("error")["code"], "bad_message")
            g.send({"type": "coding", "on": True})
            ev = g.until("coding")
            self.assertEqual((ev["on"], ev["coding_from"]), (True, "wrapper"))
            g.send({"type": "user", "text": "x"})
            self.assertTrue(g.until("proposal").get("auto"))
            g.until("done")
            g.send({"type": "coding", "on": False})
            self.assertEqual((g.until("coding")["on"]), False)
            g.send({"type": "user", "text": "x"})
            p = g.until("proposal")
            self.assertFalse(p.get("auto"))
            g.send({"type": "decision", "id": p["id"], "decision": "decline"})
            g.until("done")
            g.close()
            system = [r["body"]["messages"][0]["content"] for r in api.requests]
            self.assertEqual(["# Coding mode is on" in t for t in system], [True, True, False, False])
            coding = [a for a in self.audit() if a["event"] == "coding"]
            self.assertEqual([(a["on"], a["source"]) for a in coding], [(True, "wrapper"), (False, "wrapper")])

    def test_wrapper_coding_refused_in_home(self):
        with FakeAPI([]) as api:
            fake_home = self.path("fakehome")
            os.makedirs(fake_home)
            with mock.patch.dict(os.environ, HOME=fake_home):
                home, path = make_home(self.tmp, api.url)
                bless_path(home, path)
                g = Proc(home, fake_home, ["-e", "test"])
            g.send({"type": "coding", "on": True})
            err = g.until("error")
            self.assertEqual(err["code"], "coding_refused")
            self.assertIn("coding mode is refused", err["message"])
            self.assertFalse(g.until("coding")["on"])
            g.close()

    def test_coding_change_refused_while_proposals_pending(self):
        with FakeAPI([call("write_file", path="x", content="y", reason="r"), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "go"})
            p = g.until("proposal")
            g.send({"type": "coding", "on": True})
            g.send({"type": "skill", "name": "slurm", "on": True})
            self.assertEqual(g.until("error")["code"], "busy")
            self.assertEqual(g.until("error")["code"], "busy")
            g.send({"type": "decision", "id": p["id"], "decision": "decline"})
            g.until("done")
            g.close()
            self.assertFalse(any(e["type"] == "coding" for e in g.events))

    def test_wrapper_sees_each_exchange_as_sent_and_received(self):
        turns = [call("write_file", path="x", content="y", reason="r"), reply("all done")]
        with FakeAPI(turns) as api:
            home, path = make_home(self.tmp, api.url, key_env="GENAI_TRACE_KEY", headers={"X-Team": "hush-hush"})
            bless_path(home, path)
            self.wd = self.path("wd")
            os.makedirs(self.wd)
            with mock.patch.dict(os.environ, GENAI_TRACE_KEY="sk-very-secret"):
                g = Proc(home, self.wd, ["-e", "test"])
            self.assertIn("trace", g.until("hello")["features"])
            g.send({"type": "system"})
            sysev = g.until("system")
            self.assertIn("You are genai", sysev["system"])
            self.assertIn("propose_command", [t["function"]["name"] for t in sysev["tools"]])
            g.send({"type": "user", "text": "make x"})
            p = g.until("proposal")
            g.send({"type": "trace"})                                 # allowed while a proposal waits
            ex = g.until("trace")["exchanges"]
            self.assertEqual([e["n"] for e in ex], [1])
            g.send({"type": "decision", "id": p["id"], "decision": "decline"})
            done = g.until("done")
            self.assertEqual(done["exchanges"], [1, 2])
            g.send({"type": "trace", "from": 1, "to": 2})
            tr = g.until("trace")
            g.close()
            first, second = tr["exchanges"]
            self.assertEqual(json.loads(first["request"])["messages"][-1], {"role": "user", "content": "make x"})
            self.assertEqual(first["message"]["tool_calls"][0]["function"]["name"], "write_file")
            self.assertTrue(any(line.startswith("data:") for line in second["response"]))
            self.assertEqual((second["status"], second["http"], second["message"]["content"]), ("ok", 200, "all done"))
            self.assertEqual(second["headers"]["Authorization"], "Bearer (your API key, not shown)")
            self.assertEqual(second["headers"]["X-Team"], "(set in the endpoint; not shown)")
            raw = json.dumps(tr)
            self.assertNotIn("sk-very-secret", raw)
            self.assertNotIn("hush-hush", raw)

    def test_plan_is_approved_in_advance_and_enforced(self):
        steps = [{"kind": "read", "path": "a.txt", "why": "see it"},
                 {"kind": "edit", "path": "a.txt", "why": "fix it"},
                 {"kind": "command", "command": "make test", "why": "check"},
                 {"kind": "command", "command": "rm -rf build", "why": "clean"}]
        turns = [call("propose_plan", goal="fix a", steps=steps),
                 call("read_file", path="a.txt", reason="step 1"),
                 call("edit_file", path="a.txt", old_text="old", new_text="new", reason="step 2"),
                 call("propose_command", command="make test -j2", reason="step 3"),
                 call("propose_command", command="rm -rf build", reason="left out"),
                 call("write_file", path="b.txt", content="x", reason="not planned"), reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            with open(os.path.join(self.wd, "a.txt"), "w") as f:
                f.write("old\n")
            self.assertIn("plan", g.until("hello")["features"])
            g.send({"type": "user", "text": "fix a"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p["goal"], [s["n"] for s in p["steps"]]), ("plan", "fix a", [1, 2, 3, 4]))
            g.send({"type": "decision", "id": p["id"], "decision": "accept", "approve": [1, 2, 3],
                    "edits": {"3": "make test -j2"}})
            plan = g.until("plan")["plan"]
            self.assertEqual([(s["n"], s["status"]) for s in plan["steps"]],
                             [(1, "todo"), (2, "todo"), (3, "todo"), (4, "left out")])
            self.assertEqual(plan["steps"][2]["command"], "make test -j2")
            read = g.until("proposal")
            self.assertEqual((read["kind"], read.get("auto"), read["plan_step"]), ("read", True, 1))   # no question
            edit = g.until("proposal")
            self.assertEqual((edit["kind"], edit.get("auto"), edit["plan_step"]), ("edit", None, 2))   # still asks
            g.send({"type": "decision", "id": edit["id"], "decision": "accept"})
            cmd = g.until("proposal")
            self.assertEqual((cmd["command"], cmd.get("preapproved"), cmd["plan_step"]), ("make test -j2", True, 3))
            g.until("done")
            g.send({"type": "user", "text": "go on"})
            cmd = g.until("proposal")
            self.assertEqual((cmd["command"], cmd.get("preapproved"), cmd.get("off_plan")), ("rm -rf build", None, True))
            g.until("done")
            g.send({"type": "user", "text": "and?"})
            w = g.until("proposal")
            self.assertTrue(w.get("off_plan"))
            g.send({"type": "decision", "id": w["id"], "decision": "decline"})
            g.until("done")
            g.send({"type": "plan"})
            last = g.until("plan")["plan"]
            g.send({"type": "plan", "clear": True})
            self.assertIsNone(g.until("plan")["plan"])
            g.close()
            self.assertEqual([s["status"] for s in last["steps"]], ["done", "done", "done", "left out"])
            plan_result = next(m for m in api.requests[1]["body"]["messages"] if m["role"] == "tool")["content"]
            self.assertIn("The user approved this plan", plan_result)
            self.assertIn("3. run exactly: make test -j2", plan_result)
            self.assertIn("4. run exactly: rm -rf build (LEFT OUT", plan_result)
            self.assertIn("# The plan the user approved", api.requests[-1]["body"]["messages"][0]["content"])
            events = [a["event"] for a in self.audit()]
            self.assertIn("plan", events)
            self.assertEqual(events.count("plan_match"), 3)
            logged = next(a for a in self.audit() if a["event"] == "proposal" and a["kind"] == "plan")
            self.assertNotIn("rm -rf build", json.dumps(logged))          # commands are fingerprinted, as always

    def test_a_malformed_plan_choice_declines_it(self):
        steps = [{"kind": "command", "command": "make", "why": "w"}]
        with FakeAPI([call("propose_plan", goal="g", steps=steps), reply("ok")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "go"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "decision": "accept", "approve": [1, "x"]})
            self.assertEqual(g.until("error")["code"], "bad_message")
            g.until("done")
            g.close()
            tool = next(m for m in api.requests[-1]["body"]["messages"] if m["role"] == "tool")
            self.assertIn("declined by the user", tool["content"])
            self.assertNotIn("The plan the user approved", api.requests[-1]["body"]["messages"][0]["content"])

    def test_plan_reads_of_secret_or_hidden_files_still_ask(self):
        steps = [{"kind": "read", "path": "creds.txt", "why": "w"}, {"kind": "read", "path": ".env", "why": "w"}]
        turns = [call("propose_plan", goal="g", steps=steps), call("read_file", path="creds.txt", reason="r"),
                 call("read_file", path=".env", reason="r"), reply("ok")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            with open(os.path.join(self.wd, "creds.txt"), "w") as f:
                f.write("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n")
            with open(os.path.join(self.wd, ".env"), "w") as f:
                f.write("A=1\n")
            g.send({"type": "user", "text": "go"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "decision": "accept"})
            for _ in range(2):
                r = g.until("proposal")
                self.assertFalse(r.get("auto"))
                g.send({"type": "decision", "id": r["id"], "decision": "decline"})
            g.until("done")
            g.close()

    def test_repl_picks_plan_steps(self):
        p = types.SimpleNamespace(id="p1")
        steps = [{"n": 1}, {"n": 2}, {"n": 3}]
        for ans, want in (("a", ("accept", None)), ("", ("decline", None)), ("n", ("decline", None)),
                          ("1,3", ("accept", [1, 3])), ("2 9", ("accept", [2])), ("1; rm", ("decline", None))):
            with mock.patch("builtins.input", return_value=ans):
                self.assertEqual(G.pick_steps(p, steps), want, ans)

    def test_bad_plans_are_refused(self):
        for args in ({"goal": "", "steps": [{"kind": "read", "path": "a", "why": ""}]}, {"goal": "g", "steps": []},
                     {"goal": "g", "steps": [{"kind": "delete", "path": "a"}]},
                     {"goal": "g", "steps": [{"kind": "command", "why": "no command"}]},
                     {"goal": "g", "steps": [{"kind": "edit", "path": "../outside", "why": ""}]},
                     {"goal": "g", "steps": [{"kind": "read", "path": "a", "why": ""}] * (G.PLAN_STEPS + 1)}):
            with self.assertRaises(ValueError):
                G.parse_plan(args)

    def test_wrapper_turns_skills_on_and_off(self):
        with FakeAPI([reply("one"), reply("two")]) as api:
            g = self.start(api)
            skills = os.path.join(self.home, "skills")
            for name in ("slurm", "draft"):
                os.makedirs(os.path.join(skills, name))
                with open(os.path.join(skills, name, "SKILL.md"), "w") as f:
                    f.write(f"# {name} help\n{name.upper()}-MARKER\n")
            bless_path(self.home, os.path.join(skills, "slurm"))
            hello = g.until("hello")
            self.assertEqual(hello["skills"], [])
            self.assertIn("skills", hello["features"])
            g.send({"type": "skills"})
            rows = {r["name"]: r for r in g.until("skills")["skills"]}
            self.assertEqual({n: (r["status"], r["active"]) for n, r in rows.items()},
                             {"slurm": ("blessed", False), "draft": ("unblessed", False)})
            g.send({"type": "skill", "name": "nope", "on": True})
            self.assertEqual(g.until("error")["code"], "unknown_skill")
            g.until("skills")
            g.send({"type": "skill", "name": "draft", "on": True})
            err = g.until("error")
            self.assertEqual(err["code"], "not_blessed")
            self.assertIn("genai --bless", err["message"])
            self.assertFalse({r["name"]: r["active"] for r in g.until("skills")["skills"]}["draft"])
            g.send({"type": "skill", "name": "slurm"})
            self.assertEqual(g.until("error")["code"], "bad_message")
            g.send({"type": "skill", "name": "slurm", "on": True})
            self.assertTrue({r["name"]: r["active"] for r in g.until("skills")["skills"]}["slurm"])
            g.send({"type": "user", "text": "a"})
            g.until("done")
            g.send({"type": "skill", "name": "slurm", "on": False})
            self.assertFalse({r["name"]: r["active"] for r in g.until("skills")["skills"]}["slurm"])
            g.send({"type": "user", "text": "b"})
            g.until("done")
            g.close()
            systems = [r["body"]["messages"][0]["content"] for r in api.requests if "body" in r]
            self.assertIn("SLURM-MARKER", systems[0])
            self.assertNotIn("SLURM-MARKER", systems[1])
            self.assertFalse(any("DRAFT-MARKER" in s for s in systems))
            self.assertEqual([(a["name"], a["on"]) for a in self.audit() if a["event"] == "skill"],
                             [("slurm", True), ("slurm", False)])

    def test_files_marks_secrets_for_the_tree(self):
        with FakeAPI([]) as api:
            g = self.start(api)
            files = {"plain.txt": "hello\n", "notes.txt": "password = hunter2xyz\n", ".env": "TOKEN=abcdefgh\n",
                     ".hidden.txt": "nothing here\n", "sub/inner.txt": "x\n"}
            for rel, text in files.items():
                os.makedirs(os.path.dirname(os.path.join(self.wd, rel)), exist_ok=True)
                with open(os.path.join(self.wd, rel), "w") as f:
                    f.write(text)
            with open(os.path.join(self.wd, "blob.bin"), "wb") as f:
                f.write(b"\0\1\2")
            os.symlink("/etc/hostname", os.path.join(self.wd, "link"))
            self.assertIn("files", g.until("hello")["features"])

            g.send({"type": "files"})
            ev = g.until("files")
            rows = {r["name"]: r for r in ev["entries"]}
            self.assertEqual(ev["path"], ".")
            self.assertEqual([r["name"] for r in ev["entries"]], sorted(rows))
            self.assertEqual({n: (r["kind"], r.get("secret"), r["hidden"]) for n, r in rows.items()},
                             {"plain.txt": ("file", False, False), "notes.txt": ("file", True, False),
                              ".env": ("file", True, True), ".hidden.txt": ("file", False, True),
                              "blob.bin": ("file", None, False), "sub": ("dir", None, False),
                              "link": ("symlink", None, False)})
            self.assertTrue(all(r["asks"] for r in rows.values() if r["kind"] == "file"))   # coding off

            g.send({"type": "coding", "on": True})
            g.until("coding")
            g.send({"type": "files", "path": "."})
            rows = {r["name"]: r for r in g.until("files")["entries"]}
            self.assertEqual({n: r["asks"] for n, r in rows.items() if r["kind"] == "file"},
                             {"plain.txt": False, "notes.txt": True, ".env": True, ".hidden.txt": True,
                              "blob.bin": True})

            g.send({"type": "files", "path": "sub"})
            self.assertEqual([r["name"] for r in g.until("files")["entries"]], ["inner.txt"])
            for bad in ("..", "/etc", "link", "missing"):
                g.send({"type": "files", "path": bad})
                self.assertEqual(g.until("error")["code"], "bad_path", bad)
            g.send({"type": "files", "path": 3})
            self.assertEqual(g.until("error")["code"], "bad_message")
            g.close()
            self.assertEqual(api.requests, [])          # classifying files sends nothing to the endpoint

    def test_register_tools_relay(self):
        turns = [call("job_details", job="48211"), reply("thanks")]
        with FakeAPI(turns) as api:
            g = self.start(api)
            g.send({"type": "register_tools", "tools": [{"name": "read_file", "description": "x"}]})
            self.assertEqual(g.until("error")["code"], "bad_tools")
            g.send({"type": "register_tools", "tools": [{"name": "job_details", "description": "Show a job",
                    "parameters": {"type": "object", "properties": {"job": {"type": "string"}}}}]})
            self.assertEqual(g.until("registered")["tools"], ["job_details"])
            g.send({"type": "user", "text": "job?"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p["tool"], p["args"]), ("external", "job_details", {"job": "48211"}))
            g.send({"type": "result", "id": p["id"], "status": "ok", "output": "State=PENDING token=supersecret1"})
            g.until("done")
            g.close()
            self.assertIn("job_details", [t["function"]["name"] for t in api.requests[0]["body"]["tools"]])
            tool = [m for m in api.requests[-1]["body"]["messages"] if m["role"] == "tool"][0]["content"]
            self.assertIn("State=PENDING", tool)
            self.assertNotIn("supersecret1", tool)

    def test_attach_is_redacted_and_sent_with_next_message(self):
        with FakeAPI([reply("seen")]) as api:
            g = self.start(api)
            g.send({"type": "attach", "label": "console", "text": "ok\npassword=hunter22\n", "ref": "p3"})
            att = g.until("attached")
            self.assertEqual(att["redacted"], {"secret assignment": 1})
            g.send({"type": "user", "text": "what happened?"})
            g.until("done")
            g.close()
            content = api.requests[0]["body"]["messages"][-1]["content"]
            self.assertIn("[console (re p3)]", content)
            self.assertNotIn("hunter22", content)

    def test_redirect_not_followed(self):
        with FakeAPI([], redirect="http://127.0.0.1:1/steal") as api:
            g = self.start(api)
            g.send({"type": "user", "text": "hi"})
            err = g.until("error")
            self.assertIn("redirect", err["message"])
            g.close()
            self.assertEqual(len(api.requests), 1)

    def test_env_proxy_ignored(self):
        # Proc sets HTTP(S)_PROXY to a dead port; the request must still reach the fake API.
        with FakeAPI([reply("direct")]) as api:
            g = self.start(api)
            g.send({"type": "user", "text": "hi"})
            self.assertEqual(g.until("done", "error")["type"], "done")
            g.close()

    def test_non_streaming_and_usage(self):
        with FakeAPI([reply("x")], usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}) as api:
            home, path = make_home(self.tmp, api.url, params={"stream": False})
            bless_path(home, path)
            os.makedirs(self.path("wd"))
            g = Proc(home, self.path("wd"), ["-e", "test"])
            g.send({"type": "user", "text": "hi"})
            done = g.until("done")
            g.close()
            self.assertFalse(api.requests[0]["body"]["stream"])
            self.assertGreaterEqual(done["context"]["used"], 12)

    def test_context_limit_refuses_and_compact_prunes(self):
        turns = [call("read_file", path="big.txt", reason="r"), reply("one"), reply("two"), reply("three"),
                 reply("after")]
        with FakeAPI(turns) as api:
            home, path = make_home(self.tmp, api.url, context_limit=6000)
            bless_path(home, path)
            wd = self.path("wd")
            os.makedirs(wd)
            with open(os.path.join(wd, "big.txt"), "w") as f:
                f.write("word " * 2000)                     # ~2500 tokens
            g = Proc(home, wd, ["-e", "test", "-S", "s1"])
            g.send({"type": "user", "text": "read it"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "decision": "accept"})
            g.until("done")
            for text in ("t2", "t3"):
                g.send({"type": "user", "text": text})
                g.until("done")
            big = "q " * 6000                               # ~3000 tokens: over the limit with the file
            sent_before = len(api.requests)
            g.send({"type": "user", "text": big})
            self.assertEqual(g.until("error", "done")["code"], "context_full")
            self.assertEqual(len(api.requests), sent_before)
            g.send({"type": "compact"})
            c = g.until("compacted")
            self.assertGreaterEqual(c["pruned"], 1)
            self.assertLess(c["after"], c["before"])
            self.assertIsNone(c["summary"])                 # pruning alone was enough
            self.assertEqual(len(api.requests), sent_before)
            g.send({"type": "user", "text": big})
            self.assertEqual(g.until("done", "error")["type"], "done")
            g.close()
            sess = json.load(open(os.path.join(home, "sessions", "s1.json")))
            self.assertTrue(any("word word" in (m.get("content") or "") for m in sess["history"]))
            self.assertFalse(any("word word" in (m.get("content") or "") for m in sess["messages"]))
            self.assertEqual([m["role"] for m in sess["messages"]].count("user"), 4)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(home, "sessions", "s1.json")).st_mode), 0o600)

    def test_session_resume_closes_pending(self):
        with FakeAPI([call("write_file", path="x", content="y", reason="r"), reply("resumed fine")]) as api:
            g = self.start(api, "-S", "job")
            g.send({"type": "user", "text": "go"})
            g.until("proposal")
            g.p.kill()
            g.p.wait()
            g.p.stdin.close(); g.p.stdout.close(); g.p.stderr.close()
            g2 = Proc(self.home, self.wd, ["-e", "test", "-S", "job"])
            self.assertTrue(g2.until("hello")["resumed"])
            g2.send({"type": "user", "text": "again"})
            g2.until("done")
            g2.close()
            msgs = api.requests[-1]["body"]["messages"]
            roles = [m["role"] for m in msgs]
            self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"])

    def test_sandbox_indicator(self):
        os.environ["GENAI_SANDBOX"] = "testbox"
        self.addCleanup(os.environ.pop, "GENAI_SANDBOX", None)
        self.assertEqual(G.sandbox_info()["name"], "testbox")
        self.assertIn("inside the testbox sandbox", G.where_runs(G.sandbox_info()))
        self.assertIn("OUTSIDE", G.where_runs(None))
        with FakeAPI([]) as api:
            g = self.start(api)
            self.assertEqual(g.until("hello")["sandbox"]["name"], "testbox")
            g.close()

    def repl_run(self, sandbox_env):
        """Drive the REPL in a pseudo-terminal through :run, with EDITOR=true (keeps text as is)."""
        import pty
        import select
        turns = [call("propose_command", command="echo sandbox-check", reason="test"), reply("got it")]
        with FakeAPI(turns) as api:
            home, path = make_home(self.tmp, api.url)
            bless_path(home, path)
            wd = self.path("wd")
            os.makedirs(wd, exist_ok=True)
            env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home), EDITOR="true", VISUAL="true",
                       SHELL="/bin/sh", TERM="dumb")
            env.pop("GENAI_SANDBOX", None)
            env.update(sandbox_env)
            master, slave = pty.openpty()
            proc = subprocess.Popen([sys.executable, SCRIPT, "-e", "test"], cwd=wd, env=env,
                                    stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            out = b""

            def expect(token, timeout=10):
                nonlocal out
                end = time.time() + timeout
                while token.encode() not in out:
                    r, _, _ = select.select([master], [], [], max(0, end - time.time()))
                    if not r:
                        raise AssertionError(f"timed out waiting for {token!r}; got:\n{out.decode(errors='replace')}")
                    try:
                        out += os.read(master, 4096)
                    except OSError:
                        raise AssertionError(f"pty closed waiting for {token!r}; got:\n{out.decode(errors='replace')}")

            try:
                expect("> ")
                os.write(master, b"say something\n..\n")
                expect("Proposed command")
                os.write(master, b":run\n")
                expect("Run it?")
                os.write(master, b"y\n")
                expect("got it")
                os.write(master, b":quit\n")
                proc.wait(10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                os.close(master)
            sent = api.requests[-1]["body"]["messages"][-1]["content"]
            return out.decode(errors="replace"), sent

    def test_run_reports_outside_sandbox(self):
        out, sent = self.repl_run({"GENAI_SANDBOX": "none"})
        if os.path.exists("/run/.containerenv") or os.path.exists("/.dockerenv"):
            self.skipTest("running inside a container; 'outside' can't be shown here")
        self.assertIn("OUTSIDE any sandbox", out)
        self.assertIn("sandbox-check", out)
        self.assertIn("ran OUTSIDE any sandbox", out)
        self.assertIn("OUTSIDE any sandbox", sent)
        self.assertIn("sandbox-check", sent)

    def test_run_reports_inside_sandbox(self):
        out, sent = self.repl_run({"GENAI_SANDBOX": "testbox"})
        self.assertIn("sandbox: commands you :run or :sh execute inside the testbox sandbox", out)
        self.assertIn("ran inside the testbox sandbox", out)
        self.assertIn("inside the testbox sandbox", sent)
        runs = [a for a in self.audit_at(self.path("home", "genai")) if a["event"] == "run"]
        self.assertEqual(runs[0]["sandbox"]["name"], "testbox")

    def audit_at(self, home):
        with open(os.path.join(home, "audit.log")) as f:
            return [json.loads(x) for x in f]

    def test_plain_mode_performs_nothing(self):
        turns = [calls(("write_file", {"path": "x", "content": "y", "reason": "r"}),
                       ("propose_command", {"command": "ls -la", "reason": "look"}))]
        with FakeAPI(turns) as api:
            home, path = make_home(self.tmp, api.url)
            bless_path(home, path)
            wd = self.path("wd")
            os.makedirs(wd)
            r = subprocess.run([sys.executable, SCRIPT, "-e", "test", "do it"], cwd=wd, capture_output=True,
                               text=True, stdin=subprocess.DEVNULL,
                               env=dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home)))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("ls -la", r.stdout)
            self.assertIn("declined: no terminal", r.stderr)
            self.assertIn("non-interactive", api.requests[0]["body"]["messages"][0]["content"])
            self.assertFalse(os.path.exists(os.path.join(wd, "x")))


# ---------------------------------------------------------------- profiles (personas)

def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    return path


class TestPersonaProfiles(Tmp):
    def prof(self, **kw):
        d = {"name": "p"}
        d.update(kw)
        return G.Profile(d, "p.json")

    def test_validation(self):
        p = self.prof(endpoint="work-llm", persona="Be terse.", skills=["a"], params={"temperature": 0.1}, coding=True)
        self.assertTrue(p.coding)
        self.assertIsNone(self.prof().skills)                     # not set: default_skills apply
        for bad in ({"coding": "yes"}, {"params": {"model": "x"}}, {"skills": "a"}, {"endpoint": "../x"},
                    {"persona": 3}, {"temperature": 1}):
            with self.assertRaises(G.GenaiError, msg=bad):
                self.prof(**bad)
        with self.assertRaises(G.GenaiError) as e:
            self.prof(base_url="https://x/v1")
        self.assertIn("looks like an endpoint", str(e.exception))
        with self.assertRaises(G.GenaiError) as e:
            G.Endpoint({"name": "x", "base_url": "https://x/v1", "model": "m", "persona": "hi"}, "x.json")
        self.assertIn("belong in a profile", str(e.exception))

    def test_bless_shows_persona_and_coding(self):
        home = self.path("home", "genai")
        os.makedirs(home)
        path = write_json(self.path("coder.json"), {"name": "coder", "endpoint": "e", "coding": True,
                                                   "persona": "Be terse.\u202e hidden"})
        asked = []
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(G.bless(G.Config(home), path, lambda q: asked.append(q) or True))
        text = out.getvalue()
        self.assertIn("| Be terse.", text)
        self.assertIn("\\u202e", text)                           # hidden characters are shown escaped
        self.assertIn("grants coding mode", text)
        self.assertIn("coding mode it grants", asked[0])
        self.assertEqual(G.Config(home).blessed()[path]["kind"], "profile")

    def setup_home(self, api, profile, config=None):
        home, ep = make_home(self.tmp, api.url, params={"temperature": 0.9, "top_p": 0.5})
        bless_path(home, ep)
        skill = self.path("home", "genai", "skills", "terse")
        os.makedirs(skill)
        with open(os.path.join(skill, "SKILL.md"), "w") as f:
            f.write("# Terse\n\nSKILL-MARKER\n")
        bless_path(home, skill)
        if profile is not None:
            bless_path(home, write_json(os.path.join(home, "profiles", "coder.json"), profile))
        if config:
            write_json(os.path.join(home, "config.json"), config)
        self.home = home
        self.wd = self.path("wd")
        os.makedirs(self.wd, exist_ok=True)
        return home

    def test_profile_picks_endpoint_persona_skills_params(self):
        prof = {"name": "coder", "description": "Careful", "endpoint": "test", "persona": "PERSONA-MARKER",
                "skills": ["terse"], "params": {"temperature": 0.1}, "coding": True}
        with FakeAPI([reply("ok")]) as api:
            home = self.setup_home(api, prof)
            g = Proc(home, self.wd, ["-p", "coder"])
            hello = g.until("hello")
            self.assertEqual(hello["endpoint"]["name"], "test")
            self.assertEqual(hello["profile"], {"name": "coder", "description": "Careful"})
            self.assertTrue(hello["coding"])
            self.assertEqual(hello["coding_from"], "profile coder")
            g.send({"type": "user", "text": "hi"})
            g.until("done")
            g.close()
            body = api.requests[-1]["body"]
            system = body["messages"][0]["content"]
            self.assertIn("PERSONA-MARKER", system)
            self.assertIn("SKILL-MARKER", system)
            self.assertLess(system.index("You are genai"), system.index("PERSONA-MARKER"))   # core rules first
            self.assertEqual((body["temperature"], body["top_p"]), (0.1, 0.5))             # profile over endpoint

    def test_endpoint_flag_overrides_profile(self):
        with FakeAPI([reply("ok")]) as api:
            home = self.setup_home(api, {"name": "coder", "endpoint": "elsewhere"})
            g = Proc(home, self.wd, ["-p", "coder", "-e", "test"])
            self.assertEqual(g.until("hello")["endpoint"]["name"], "test")
            g.close()

    def test_defaults_from_config(self):
        with FakeAPI([reply("ok")]) as api:
            home = self.setup_home(api, {"name": "coder", "persona": "PERSONA-MARKER"},
                                   config={"default_profile": "coder", "default_endpoint": "test",
                                           "default_skills": ["terse"]})
            g = Proc(home, self.wd, [])
            hello = g.until("hello")
            self.assertEqual((hello["profile"]["name"], hello["endpoint"]["name"]), ("coder", "test"))
            self.assertFalse(hello["coding"])
            g.send({"type": "user", "text": "hi"})
            g.until("done")
            g.close()
            system = api.requests[-1]["body"]["messages"][0]["content"]
            self.assertIn("SKILL-MARKER", system)                 # profile has no "skills": defaults apply
            self.assertIn("PERSONA-MARKER", system)

    def test_profile_skills_replace_defaults(self):
        with FakeAPI([reply("ok")]) as api:
            home = self.setup_home(api, {"name": "coder", "endpoint": "test", "skills": []},
                                   config={"default_skills": ["terse"]})
            g = Proc(home, self.wd, ["-p", "coder"])
            g.until("hello")
            g.send({"type": "user", "text": "hi"})
            g.until("done")
            g.close()
            self.assertNotIn("SKILL-MARKER", api.requests[-1]["body"]["messages"][0]["content"])

    def run_genai(self, home, *args, cwd=None, **env):
        return subprocess.run([sys.executable, SCRIPT, *args], cwd=cwd or self.wd,
                              env=dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home), **env),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_unblessed_or_changed_profile_sends_nothing(self):
        with FakeAPI([]) as api:
            home = self.setup_home(api, None)
            path = write_json(os.path.join(home, "profiles", "coder.json"), {"name": "coder", "endpoint": "test"})
            r = self.run_genai(home, "-p", "coder", "hi")
            self.assertEqual(r.returncode, G.EXIT_PROFILE)
            self.assertIn("profile 'coder'", r.stderr)
            self.assertIn("not blessed", r.stderr)
            bless_path(home, path)
            write_json(path, {"name": "coder", "endpoint": "test", "persona": "sneaky"})
            r = self.run_genai(home, "-p", "coder", "hi")
            self.assertIn("changed since it was blessed", r.stderr)
            self.assertEqual(api.requests, [])

    def test_wrong_kind_hint(self):
        with FakeAPI([]) as api:
            home = self.setup_home(api, None)
            r = self.run_genai(home, "-p", "test", "hi")
            self.assertEqual(r.returncode, G.EXIT_PROFILE)
            self.assertIn("use -e test", r.stderr)

    def test_profile_coding_off_in_home(self):
        with FakeAPI([reply("ok")]) as api:
            home = self.setup_home(api, {"name": "coder", "endpoint": "test", "coding": True})
            fake_home = self.path("fakehome")
            os.makedirs(fake_home)
            r = self.run_genai(home, "--json", "-p", "coder", cwd=fake_home, HOME=fake_home)
            events = [json.loads(x) for x in r.stdout.splitlines()]
            self.assertEqual(r.returncode, 0, r.stderr)
            hello = next(e for e in events if e["type"] == "hello")
            self.assertFalse(hello["coding"])
            self.assertTrue(any("asks for coding mode" in e.get("message", "") for e in events if e["type"] == "notice"))

    def test_lists(self):
        with FakeAPI([]) as api:
            home = self.setup_home(api, {"name": "coder", "endpoint": "test", "description": "Careful", "coding": True})
            write_json(os.path.join(home, "profiles", "draft.json"), {"name": "draft"})
            r = self.run_genai(home, "--list-profiles", "--json")
            rows = {x["name"]: x for x in json.loads(r.stdout)["profiles"]}
            self.assertEqual(rows["coder"]["status"], "blessed")
            self.assertIn("grants coding mode", rows["coder"]["description"])
            self.assertEqual(rows["draft"]["status"], "unblessed")
            r = self.run_genai(home, "--list-endpoints")
            self.assertIn("test", r.stdout)
            self.assertIn("blessed", r.stdout)

    def test_bundled_coder_profile_is_valid(self):
        p = G.Profile.load(os.path.join(os.path.dirname(HERE), "profiles", "coder.json"))
        self.assertIsNone(p.endpoint)        # uses default_endpoint, or -e
        self.assertTrue(os.path.isdir(os.path.join(os.path.dirname(HERE), "skills", p.skills[0])))


if __name__ == "__main__":
    unittest.main()
