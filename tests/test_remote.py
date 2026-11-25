"""Tests for genai-tui's remote mode: the bootstrap, the key staying here, the launcher's options, the banner.

Run: python3 -m unittest tests.test_remote
The end-to-end tests run the remote command locally (remote.json's "ssh" is `sh -c`), with a thread standing in for
`ssh -R`: a TCP port that forwards to the key proxy's Unix socket. The real-ssh test needs the container from
tests/remote_sshd.sh and is skipped without it.
"""

import base64
import importlib.machinery
import importlib.util
import json
import os
import queue
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tui"))
from fakeapi import FakeAPI, call, reply  # noqa: E402
from test_genai import Tmp, bless_path, make_home, write_json  # noqa: E402
import test_genai  # noqa: E402
import test_tui  # noqa: E402

import remote  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("conversation", os.path.join(REPO, "tui", "conversation.py"))
C = importlib.util.module_from_spec(importlib.util.spec_from_loader("conversation", _loader))
_loader.exec_module(C)

KEY = "sk-never-leaves-this-machine"
KEYPROXY = os.path.join(REPO, "tui", "keyproxy.py")
GENAI = os.path.join(REPO, "genai")


class Forwarder:
    """What `ssh -R 127.0.0.1:0:SOCKET` does: a loopback TCP port whose connections go to a Unix socket."""

    def __init__(self, sock_path):
        self.path = sock_path
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(16)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                a, _ = self.srv.accept()
            except OSError:
                return
            b = socket.socket(socket.AF_UNIX)
            b.connect(self.path)
            for src, dst in ((a, b), (b, a)):
                threading.Thread(target=self.pump, args=(src, dst), daemon=True).start()

    @staticmethod
    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self):
        self.srv.close()


class TestCommand(unittest.TestCase):
    def test_plain(self):
        info = {"python": "python3.11"}
        cmd = remote.bootstrap_command(info, 123, ["--coding"])
        words = shlex.split(cmd)
        self.assertEqual(words[0], "python3.11")
        self.assertEqual(words[1], "-c")
        self.assertEqual(words[2], remote.BOOTSTRAP)
        self.assertEqual(words[3:], ["123", "--json", "--config-stdin", "--coding"])

    def test_dir_and_user_are_quoted(self):
        info = {"python": "python3", "dir": "/srv/my project; rm -rf ~", "as": "svc"}
        cmd = remote.bootstrap_command(info, 5, [])
        self.assertTrue(cmd.startswith("cd '/srv/my project; rm -rf ~' && sudo -n -H -u svc -- python3 -c "), cmd)
        self.assertEqual(shlex.split(cmd)[:2], ["cd", "/srv/my project; rm -rf ~"])
        self.assertTrue(cmd.endswith(" 5 --json --config-stdin"))

    def test_destination_checks(self):
        ok = remote.check_destination
        self.assertIsNone(ok("me@host", "svc", "/srv/x"))
        for bad in ("", "-oProxyCommand=x", "a b", "a\nb"):
            self.assertIsNotNone(ok(bad, None, None), bad)
        self.assertIsNotNone(ok("h", "x y", None))
        self.assertIsNotNone(ok("h", "-u", None))
        self.assertIsNotNone(ok("h", None, "a\nb"))


class RemoteCase(Tmp):
    """A blessed endpoint with a key, a key proxy in front of FakeAPI, and a forwarder on loopback."""

    turns = [reply("hello from the remote side")]

    def setUp(self):
        super().setUp()
        self.api = FakeAPI(list(self.turns))
        self.api.__enter__()
        self.addCleanup(self.api.__exit__)
        self.home, path = make_home(self.tmp, self.api.url, key_env="REMOTE_TEST_KEY")
        bless_path(self.home, path)
        self.env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(self.home), REMOTE_TEST_KEY=KEY)
        self.state = self.path("state")
        os.mkdir(self.state, 0o700)
        self.workdir = self.path("work")
        os.mkdir(self.workdir)
        out = subprocess.run([sys.executable, GENAI, "--remote-config", "-e", "test", *self.prepare()], env=self.env,
                             check=True, capture_output=True, text=True).stdout
        with open(os.path.join(self.state, "export.json"), "w") as f:
            f.write(out)
        self.token = "c0" * 32
        self.kp = subprocess.Popen([sys.executable, KEYPROXY, "test", os.path.join(self.state, "kp.sock")],
                                   env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_proxy)
        self.kp.stdin.write((self.token + "\n").encode())
        self.kp.stdin.flush()
        self.assertEqual(self.kp.stdout.readline().strip(), b"ready")
        self.fwd = Forwarder(os.path.join(self.state, "kp.sock"))
        self.addCleanup(self.fwd.close)

    def prepare(self):
        """Set up more of this machine's genai home before the export; returns more --remote-config options."""
        return []

    def stop_proxy(self):
        self.kp.stdin.close()
        self.kp.wait(10)
        self.kp.stdout.close()
        self.kp.stderr.close()

    def write_info(self, ssh=("sh", "-c"), **kw):
        remote.write(self.state, "testhost", kw.get("as_user"), kw.get("directory", self.workdir), self.fwd.port,
                     "python3", self.token, [])
        path = os.path.join(self.state, "remote.json")
        info = remote.load(path)
        info["ssh"] = list(ssh)
        info["python"] = sys.executable          # no PATH lookup of "python3" in the test
        return info

    def child(self, info, args=()):
        """genai started the remote way, with its own empty home (the remote host's), and no ssh."""
        self.remote_home = self.path("remote-home")
        env = dict(os.environ, XDG_CONFIG_HOME=self.remote_home)
        for k in ("REMOTE_TEST_KEY", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            env.pop(k, None)
        with mock.patch.dict(os.environ, env, clear=True):
            c = C.Child(list(args), info)
        self.addCleanup(c.close)
        return c

    @staticmethod
    def until(child, *types, timeout=30):
        end = time.time() + timeout
        seen = []
        while time.time() < end:
            try:
                ev = child.events.get(timeout=0.5)
            except queue.Empty:
                continue
            seen.append(ev)
            if ev["type"] in types or ev["type"] == "_exit":
                return ev, seen
        raise AssertionError(f"timed out waiting for {types}; saw {seen}")


class TestEndToEnd(RemoteCase):
    def test_a_turn_works_and_the_key_stays_here(self):
        info = self.write_info()
        c = self.child(info)
        hello, seen = self.until(c, "hello")
        self.assertEqual(hello["type"], "hello", seen)
        self.assertTrue(hello["remote"])
        self.assertEqual(hello["cwd"], os.path.realpath(self.workdir))
        self.assertEqual(hello["audit"], os.path.join(self.remote_home, "genai", "audit.log"))
        # the same message a person's typing sends: read after the source and the config line, not swallowed by them
        c.send({"type": "user", "text": "hi"})
        done, seen = self.until(c, "done")
        self.assertEqual(done["type"], "done", seen)
        self.assertEqual("".join(e["text"] for e in seen if e["type"] == "text"), "hello from the remote side")

        up = self.api.requests[0]
        h = {k.lower(): v for k, v in up["headers"].items()}
        self.assertEqual(h["authorization"], f"Bearer {KEY}")          # added by the proxy, here
        self.assertNotIn("x-genai-token", h)
        self.assertNotIn(self.token, json.dumps(up))

        c.close()
        everything = json.dumps(list(c.events.queue)) + "\n".join(c.err)
        self.assertNotIn(KEY, everything)
        for root, _, files in os.walk(self.remote_home):         # the remote host's genai home and audit log
            for name in files:
                with open(os.path.join(root, name), errors="replace") as f:
                    text = f.read()
                self.assertNotIn(KEY, text, name)
                self.assertNotIn(self.token, text, name)
        with open(os.path.join(self.state, "remote.json")) as f:
            blob = f.read()
        self.assertNotIn(KEY, blob)
        self.assertEqual(os.stat(os.path.join(self.state, "remote.json")).st_mode & 0o777, 0o600)

    def test_the_config_has_no_key_proxy_or_headers_and_points_at_the_tunnel(self):
        cfg = self.write_info()["config"]
        self.assertEqual(cfg["endpoint"]["base_url"], f"http://127.0.0.1:{self.fwd.port}/v1")
        for k in ("key_env", "key_file", "proxy", "headers"):
            self.assertNotIn(k, cfg["endpoint"])
        self.assertEqual(cfg["token"], self.token)

    def test_a_wrong_token_gets_nothing_through(self):
        info = self.write_info()
        info["config"]["token"] = "d1" * 32
        c = self.child(info)
        self.until(c, "hello")
        c.send({"type": "user", "text": "hi"})
        ev, seen = self.until(c, "error", "done")
        self.assertEqual(ev["type"], "error", seen)
        self.assertEqual(self.api.requests, [])

    def test_the_bootstrap_reads_exactly_the_source(self):
        """Whatever follows the source on stdin is left for the program, line for line."""
        src = b"import sys\nprint(sys.stdin.readline().strip(), sys.argv[1:])\n"
        info = {"python": sys.executable}
        cmd = remote.bootstrap_command(info, len(src), []).replace(" --json --config-stdin", " one two")
        r = subprocess.run(["sh", "-c", cmd], input=src + b"first line\nsecond\n", capture_output=True)
        self.assertEqual(r.stdout.decode().strip(), "first line ['one', 'two']", r.stderr)


class TestPluginsAndSkills(RemoteCase):
    """A plugin and a skill blessed on this machine are sent in the config line; the plugin's commands run on the
    remote host (here: the same machine, with a home of its own), and nothing blessed only there can be used."""

    turns = [call("rp__who", user="alice"), reply("done")]

    def prepare(self):
        self.bin = self.path("rbin")
        os.mkdir(self.bin, 0o755)
        prog = os.path.join(self.bin, "whoq")
        with open(prog, "w") as f:
            f.write('#!/bin/sh\necho "ran on the remote host for $1 as HOME=$HOME"\n')
        os.chmod(prog, 0o755)
        pdir = os.path.join(self.home, "plugins", "rp")
        os.makedirs(pdir)
        with open(os.path.join(pdir, "plugin.json"), "w") as f:
            json.dump({"name": "rp", "description": "remote plugin", "search": [self.bin],
                       "commands": {"who": {"describe": "who", "argv": ["whoq", "{user}"], "read_only": True,
                                            "params": {"user": {"pattern": "[a-z]+"}}, "min_interval": 0},
                                    "gone": {"describe": "not installed", "argv": ["nosuchprogq"], "read_only": True}}},
                      f)
        sdir = os.path.join(self.home, "skills", "rs")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "SKILL.md"), "w") as f:
            f.write("# Remote skill\n\nSAY-PINEAPPLE\n")
        cfg = test_genai.G.Config(self.home)
        for kind, d in (("plugin", pdir), ("skill", sdir)):
            cfg.set_blessed(d, {"kind": kind, "name": os.path.basename(d), "hash": test_genai.G.hash_skill(d)[0]})
        return ["--plugin", "rp", "-k", "rs"]

    def test_a_plugin_command_runs_remotely_and_the_skill_is_used(self):
        c = self.child(self.write_info())
        hello, seen = self.until(c, "hello")
        self.assertEqual((hello["plugins"], hello["skills"]), (["rp"], ["rs"]), seen)
        self.assertTrue(any("gone left out" in e.get("message", "") for e in seen if e["type"] == "notice"), seen)
        c.send({"type": "user", "text": "who?"})
        res, seen = self.until(c, "result")
        self.assertEqual(res.get("exit"), 0, seen)
        self.assertIn("ran on the remote host for alice", res["output"])
        self.assertIn("HOME=", res["output"])
        self.until(c, "done")
        system = self.api.requests[0]["body"]["messages"][0]["content"]
        self.assertIn("SAY-PINEAPPLE", system)
        with open(os.path.join(self.remote_home, "genai", "audit.log")) as f:
            rec = next(json.loads(x) for x in f if '"remote_config"' in x)
        self.assertEqual(set(rec["plugins"]), {"rp"})
        self.assertTrue(rec["skills"]["rs"].startswith("sha256:"))

    def test_only_what_was_sent_can_be_switched_on(self):
        c = self.child(self.write_info())
        self.until(c, "hello")
        c.send({"type": "plugin", "name": "rp", "on": False})
        ev, seen = self.until(c, "plugins")
        ev, seen = self.until(c, "plugins")         # the one after hello came first
        self.assertFalse(ev["plugins"][0]["enabled"], seen)
        c.send({"type": "plugin", "name": "rp", "on": True})
        ev, _ = self.until(c, "plugins")
        self.assertEqual([(r["name"], r["enabled"], r["status"]) for r in ev["plugins"]], [("rp", True, "blessed")])
        for kind, name in (("plugin", "slurm"), ("skill", "hpc")):
            c.send({"type": kind, "name": name, "on": True})
            ev, seen = self.until(c, "error")
            self.assertEqual(ev["code"], "remote", seen)
            self.assertIn("wasn't sent from your machine", ev["message"])
        c.send({"type": "skill", "name": "rs", "on": False})
        ev, seen = self.until(c, "skills")
        while ev["type"] == "skills" and ev["skills"][0]["active"]:
            ev, seen = self.until(c, "skills")
        self.assertEqual([(r["name"], r["active"]) for r in ev["skills"]], [("rs", False)])

    def test_a_changed_file_in_the_config_line_is_refused(self):
        info = self.write_info()
        info["config"]["skills"][0]["files"]["SKILL.md"] = base64.b64encode(b"# tampered\n").decode()
        c = self.child(info)
        ev, seen = self.until(c, "hello", "error")
        msgs = json.dumps(seen) + "\n".join(c.err)
        self.assertNotEqual(ev["type"], "hello", seen)
        self.assertIn("don't match the hash blessed on your machine", msgs)


class TestOptions(unittest.TestCase):
    """conversation.py --check is what the launcher runs, in your terminal, before tmux starts."""

    def check(self, *args):
        r = subprocess.run([sys.executable, os.path.join(REPO, "tui", "conversation.py"), "--check", *args],
                           capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr

    def test_remote_options_are_printed_for_the_launcher(self):
        rc, out, _ = self.check("--remote", "me@host", "--dir", "/srv/x", "--as", "svc", "-p", "coder", "--coding",
                                "--", "-J", "bastion", "-p", "2222")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), ["dest=me@host", "as=svc", "dir=/srv/x", "profile=coder", "endpoint=",
                                            "ssh=-J", "ssh=bastion", "ssh=-p", "ssh=2222"])

    def test_without_remote_nothing_is_printed(self):
        self.assertEqual(self.check("--coding"), (0, "", ""))

    def test_mistakes_are_refused(self):
        for args, why in ((["--dir", "/x"], "--dir needs --remote"), (["--as", "u"], "--as needs --remote"),
                          (["--", "-J", "b"], "needs --remote"),
                          (["--remote", "h", "-S", "x"], "can't use -S"),
                          (["--remote", "h", "-k", "a\nb"], "without control characters"),
                          (["--remote=-oProxyCommand=x"], "--remote needs a host"),
                          (["--remote", "h", "--as", "a b"], "not a user name"),
                          (["--remote"], "expected one argument")):
            rc, _, err = self.check(*args)
            self.assertEqual(rc, 2, args)
            self.assertIn(why, err, args)

    def test_skills_go_to_the_launcher_not_to_the_remote_genai(self):
        rc, out, _ = self.check("--remote", "h", "-k", "hpc", "-k", "bash-scripts")
        self.assertEqual(rc, 0)
        self.assertEqual([x for x in out.splitlines() if x.startswith("skill=")], ["skill=hpc", "skill=bash-scripts"])
        self.assertEqual(C.genai_args(C.parse_args(["--remote", "h", "-k", "hpc"])), [])
        self.assertEqual(C.genai_args(C.parse_args(["-k", "hpc"])), ["-k", "hpc"])

    def test_remote_genai_gets_no_endpoint_or_profile_options(self):
        a = C.parse_args(["--remote", "h", "-e", "x", "-p", "y", "--coding"])
        self.assertEqual(C.genai_args(a), ["--coding"])
        self.assertEqual(C.genai_args(C.parse_args(["-e", "x"])), ["-e", "x"])


class TestBanner(unittest.TestCase):
    HELLO = {"version": "1", "endpoint": {"name": "e", "model": "m", "base_url": "http://127.0.0.1:5/v1"},
             "remote": True, "host": "hpc1", "user": "svc", "uid": 1001, "cwd": "/srv/proj",
             "audit": "/home/svc/.config/genai/audit.log"}

    def text(self, **over):
        b = C.Banner(dict(self.HELLO, **over))
        return "\n".join("".join(t for t, _ in ln) for ln in b.lines(100)), b

    def test_says_where_genai_runs_and_its_audit_log(self):
        text, _ = self.text()
        self.assertIn("genai runs ON hpc1 AS svc in /srv/proj", text)
        self.assertIn("/home/svc/.config/genai/audit.log", text)

    def test_root_is_red(self):
        self.assertEqual(C.genai_where(dict(self.HELLO, user="root", uid=0))[1], C.R)
        self.assertEqual(C.genai_where(self.HELLO)[1], C.W)
        self.assertIsNone(C.genai_where(dict(self.HELLO, remote=False)))

    def test_a_local_genai_says_nothing_about_a_host(self):
        text, _ = self.text(remote=False)
        self.assertNotIn("genai runs ON", text)


SSH_PORT = int(os.environ.get("GENAI_TEST_SSH_PORT", "2222"))
SSH_KEY = os.path.join(os.environ.get("GENAI_TEST_SSH_DIR") or os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
                                                                          "genai-sshd-test"), "key")


def sshd_container_up():
    """podman, the image tests/remote_sshd.sh builds, and its sshd answering on the published port."""
    if not (shutil.which("podman") and shutil.which("ssh") and os.path.exists(SSH_KEY)):
        return False
    if subprocess.run(["podman", "image", "exists", "localhost/genai-sshd-test"], capture_output=True).returncode:
        return False
    try:
        socket.create_connection(("127.0.0.1", SSH_PORT), timeout=2).close()
    except OSError:
        return False
    return True


@unittest.skipUnless(sshd_container_up(), "needs the container: tests/remote_sshd.sh up")
class TestRealSsh(RemoteCase):
    """The same turn over a real ssh master and `-R` forward, to the SLES container, as `me` and as `svc`."""

    def setUp(self):
        super().setUp()
        self.opts = ["-p", str(SSH_PORT), "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes", "-o", "LogLevel=ERROR"]
        self.cm = os.path.join(self.state, "cm")
        subprocess.run(["ssh", "-M", "-S", self.cm, "-o", "ControlPersist=no", "-a", "-x", "-fN", *self.opts,
                        "me@127.0.0.1"], check=True)
        self.addCleanup(subprocess.run, ["ssh", "-S", self.cm, "-O", "exit", "me@127.0.0.1"], capture_output=True)

    def run_turn(self, as_user):
        out = subprocess.run(["ssh", "-S", self.cm, "-O", "forward", "-R",
                              f"127.0.0.1:0:{os.path.join(self.state, 'kp.sock')}", "me@127.0.0.1"],
                             check=True, capture_output=True, text=True).stdout
        port = int(out.strip().splitlines()[-1])
        remote.write(self.state, "me@127.0.0.1", as_user, "/tmp", port, "python3.11", self.token, self.opts)
        info = remote.load(os.path.join(self.state, "remote.json"))
        c = self.child(info)
        hello, seen = self.until(c, "hello", timeout=60)
        self.assertEqual(hello["type"], "hello", seen)
        self.assertEqual((hello["remote"], hello["user"]), (True, as_user or "me"))
        c.send({"type": "user", "text": "hi"})
        done, seen = self.until(c, "done", timeout=60)
        self.assertEqual(done["type"], "done", seen)
        h = {k.lower(): v for k, v in self.api.requests[0]["headers"].items()}
        self.assertEqual(h["authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, json.dumps(seen))

    def test_as_the_login_user(self):
        self.run_turn(None)

    def test_as_another_user_through_sudo(self):
        self.run_turn("svc")


REAL_HOST = os.environ.get("GENAI_TEST_REMOTE_HOST", "")
REAL_IMAGE = os.environ.get("GENAI_TEST_REMOTE_IMAGE", "docker.io/library/alpine:3")


@unittest.skipUnless(REAL_HOST and shutil.which("ssh"),
                     "set GENAI_TEST_REMOTE_HOST to an ssh destination (your ~/.ssh/config applies)")
class TestRealHost(RemoteCase):
    """A turn, and a sandboxed run, on a real host of yours over a real ssh master and -R forward. The work is
    done in a directory made with mktemp there and removed afterwards; genai there uses your ~/.config/genai as a real
    remote run does, so its audit log records the run. For the sandboxed run the host needs rootless podman and
    GENAI_TEST_REMOTE_IMAGE (default alpine:3) pulled there."""

    turns = [reply("hello from a real host"),
             call("run_in_sandbox", command="cat a.txt; cat .env; touch new.txt; id -un", reason="t"),
             reply("ran it"),
             call("run_in_sandbox", command="echo started; sleep 60", reason="t"),
             reply("after the stop")]

    def setUp(self):
        super().setUp()
        prof = {"name": "real", "endpoint": "test", "coding": True,
                "sandbox": {"image": REAL_IMAGE, "timeout": 120}}
        bless_path(self.home, write_json(os.path.join(self.home, "profiles", "real.json"), prof))
        out = subprocess.run([sys.executable, GENAI, "--remote-config", "-e", "test", "-p", "real"], env=self.env,
                             check=True, capture_output=True, text=True).stdout
        with open(os.path.join(self.state, "export.json"), "w") as f:
            f.write(out)
        self.cm = os.path.join(self.state, "cm")
        self.opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        subprocess.run(["ssh", "-M", "-S", self.cm, "-o", "ControlPersist=no", "-a", "-x", "-fN", *self.opts,
                        REAL_HOST], check=True, timeout=60)
        self.addCleanup(subprocess.run, ["ssh", "-S", self.cm, "-O", "exit", REAL_HOST], capture_output=True)
        self.there = self.ssh("mktemp -d /tmp/genai-test-XXXXXXXX").strip()
        self.assertRegex(self.there, r"^/tmp/genai-test-[A-Za-z0-9]{8}$")
        self.addCleanup(self.ssh, f"rm -rf {shlex.quote(self.there)}")
        self.ssh(f"cd {shlex.quote(self.there)} && echo hi > a.txt && echo API_TOKEN=abcdefghijklmnop12345 > .env")

    def ssh(self, command, check=True):
        r = subprocess.run(["ssh", "-S", self.cm, *self.opts, REAL_HOST, command], capture_output=True, text=True,
                           timeout=120)
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def start(self):
        out = subprocess.run(["ssh", "-S", self.cm, "-O", "forward", "-R",
                              f"127.0.0.1:0:{os.path.join(self.state, 'kp.sock')}", REAL_HOST],
                             check=True, capture_output=True, text=True).stdout
        port = int(out.strip().splitlines()[-1])
        remote.write(self.state, REAL_HOST, None, self.there, port, "python3", self.token, self.opts)
        c = self.child(remote.load(os.path.join(self.state, "remote.json")))
        hello, seen = self.until(c, "hello", timeout=90)
        self.assertEqual(hello["type"], "hello", seen)
        self.assertEqual((hello["remote"], hello["cwd"]), (True, self.there))
        return c, hello

    def test_a_turn_then_a_sandboxed_run(self):
        c, hello = self.start()
        c.send({"type": "user", "text": "hi"})
        done, seen = self.until(c, "done", timeout=90)
        self.assertEqual(done["type"], "done", seen)
        self.assertEqual("".join(e.get("text", "") for e in seen if e.get("type") == "text"), "hello from a real host")
        h = {k.lower(): v for k, v in self.api.requests[0]["headers"].items()}
        self.assertEqual(h["authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, json.dumps(seen))
        if hello.get("runner", {}).get("problem") or not self.ssh("command -v podman", check=False).strip():
            self.skipTest("no podman on the host")
        if subprocess.run(["ssh", "-S", self.cm, *self.opts, REAL_HOST,
                           f"podman image exists {shlex.quote(REAL_IMAGE)}"], timeout=60).returncode:
            self.skipTest(f"pull {REAL_IMAGE} on the host first")
        c.send({"type": "user", "text": "run it"})
        result, seen = self.until(c, "result", timeout=180)
        self.assertEqual(result["type"], "result", seen)
        self.assertEqual((result["status"], result.get("exit")), ("ok", 0), result)
        self.assertIn("hi", result["output"])
        self.assertNotIn("abcdefghijklmnop12345", result["output"])       # .env is hidden in the container
        done, seen = self.until(c, "done", timeout=90)
        self.assertEqual(done["type"], "done", seen)
        self.assertEqual(self.ssh(f"ls -A {shlex.quote(self.there)}").split(), [".env", "a.txt"])  # nothing kept
        self.assertIn("stop", hello["features"])                # and a run can be stopped over ssh
        c.send({"type": "user", "text": "a long one"})
        prop, seen = self.until(c, "proposal", timeout=90)
        self.assertEqual(prop["type"], "proposal", seen)
        deadline = time.monotonic() + 60
        while not self.ssh("podman ps -q --filter name=genai-", check=False).strip():
            self.assertLess(time.monotonic(), deadline, "the container never started")
            time.sleep(0.5)
        started = time.monotonic()
        c.send({"type": "stop"})
        result, seen = self.until(c, "result", timeout=60)
        self.assertEqual((result["type"], result.get("status")), ("result", "stopped"), seen)
        self.assertLess(time.monotonic() - started, 30)
        done, seen = self.until(c, "done", timeout=60)
        self.assertEqual(done.get("reason"), "stopped", seen)
        self.assertEqual(self.ssh("podman ps -aq --filter name=genai-", check=False).strip(), "")


@unittest.skipUnless(sshd_container_up() and shutil.which("tmux") and shutil.which("script"),
                     "needs the container (tests/remote_sshd.sh up), tmux and script")
class TestLauncher(test_tui.TmuxCase):
    """tui/genai-tui --remote itself, against the container: ssh master, key proxy, forward, banner, shell pane."""
    start, panes, screen, type, shell = (test_tui.TestGenaiTui.start, test_tui.TestGenaiTui.panes,
                                         test_tui.TestGenaiTui.screen, test_tui.TestGenaiTui.type,
                                         test_tui.TestGenaiTui.shell)

    def test_as_svc_in_tmp(self):
        opts = (f"-p {SSH_PORT} -i {SSH_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                "-o IdentitiesOnly=yes -o LogLevel=ERROR")
        with FakeAPI([reply("hello from afar")]) as api:
            self.start(api, args=f"-e test --remote me@127.0.0.1 --as svc --dir /tmp -- {opts}")
            self.wait_for("genai runs ON")
            self.assertRegex(self.screen(), r"genai runs ON \S+ AS svc in /tmp")
            self.assertIn("audit log there: /home/svc/.config/genai/audit.log", self.screen())
            self.type("hi")
            self.wait_for("hello from afar")
            self.assertEqual(len(api.requests), 1)
            status = self.tmux("display", "-p", "-t", "t", "#{status-right}") or ""
            self.assertIn("genai on me@127.0.0.1", self.tmux("show", "-gv", "status-right"), status)
            self.wait_for("following the shell on the other host")              # the shell pane is a genai-ssh session
            self.shell("echo who-$(id -un)-in-$PWD")
            time.sleep(1)
            self.assertIn("who-svc-in-/tmp", self.screen("shell"))
            self.shell("exit")
            self.wait_for("disconnected from me@127.0.0.1", pane="shell")
            self.assertIn("Enter reconnects, Ctrl-D closes this pane", self.screen("shell"))

    def refused(self, *args):
        """Run the launcher up to its first error (no terminal needed: it stops before tmux starts)."""
        with FakeAPI([]) as api:
            home = self.config(api)
        runtime = tempfile.mkdtemp(prefix="gr-", dir="/tmp")
        self.addCleanup(shutil.rmtree, runtime, True)
        opts = ["-p", str(SSH_PORT), "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "IdentitiesOnly=yes", "-o", "LogLevel=ERROR"]
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(home), XDG_RUNTIME_DIR=runtime, GENAI_TUI_SOCKET=self.sock)
        env.pop("TMUX", None)
        r = subprocess.run([os.path.join(REPO, "tui", "genai-tui"), "-e", "test", "--remote", "me@127.0.0.1", *args,
                            "--", *opts], env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(os.listdir(runtime), [], "the state directory was not removed")
        return r

    def test_as_needs_passwordless_sudo(self):
        r = self.refused("--as", "root")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--as needs sudo to root without a password on me@127.0.0.1 (sudo -n)", r.stderr)

    def test_dir_must_exist_as_that_user(self):
        r = self.refused("--as", "svc", "--dir", "/home/me/nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("/home/me/nope is not a directory on me@127.0.0.1 (as svc)", r.stderr)

    def test_unblessed_endpoint_stops_before_any_connection(self):
        r = self.refused("-e", "nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("connecting to", r.stderr)


if __name__ == "__main__":
    unittest.main()
