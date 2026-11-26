"""Tests for run_in_sandbox: the profile's sandbox, what it hides, the podman command it builds, the network
switch, and the loop. A fake podman runs the command on the host, so these need no containers; TestRealPodman
runs the real thing when podman and an image are present. Run: python3 -m unittest discover -s tests"""

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakeapi import FakeAPI, call, reply  # noqa: E402
from test_hoplon import G, Proc, Tmp, bless_path, make_home, write_json  # noqa: E402

IMAGE = os.environ.get("HOPLON_TEST_SANDBOX_IMAGE", "registry.suse.com/bci/bci-base:15.6")

# Stands in for podman: logs its arguments, then runs the guard and the command on the host, in the project, with
# /work meaning the project. It mounts nothing, so a path that should be hidden is still there and the guard trips.
FAKE_PODMAN = r'''#!/usr/bin/env python3
import json, os, signal, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FAKE_PODMAN_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
pidfile = os.environ["FAKE_PODMAN_LOG"] + ".pid"
if "kill" in args:                       # stops the fake container, as the real one would
    try:
        with open(pidfile) as f:
            os.kill(int(f.read()), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    sys.exit(0)
if os.environ.get("FAKE_PODMAN_CGROUP") and any(a.startswith("--memory") for a in args):
    print("Error: runc create failed: unable to apply cgroup configuration: permission denied")
    sys.exit(126)
if os.environ.get("FAKE_PODMAN_NOIMAGE"):
    print("Error: x: image not known")
    sys.exit(125)
i = args.index("/bin/sh")
env, root = dict(os.environ), None
for a in args[:i]:
    if a.startswith("--env="):
        k, _, v = a[6:].partition("=")
        env[k] = v
    if a.startswith("--volume=") and a.endswith(":/work:O"):
        root = a[len("--volume="):-len(":/work:O")]
paths = [root + p[len("/work"):] for p in args[i + 4:]]
child = subprocess.Popen(["/bin/sh", "-c", args[i + 2], "hoplon-guard"] + paths, cwd=root, env=env)
with open(pidfile, "w") as f:
    f.write(str(os.getpid()))
def stop(*_):
    child.kill()
    sys.exit(137)
signal.signal(signal.SIGTERM, stop)
sys.exit(child.wait())
'''


def conf(**kw):
    d = {"image": "img:1"}
    d.update(kw)
    return G.parse_sandbox(d, "p.json")


class FakePodman(Tmp):
    def setUp(self):
        super().setUp()
        self.bin = self.path("bin")
        os.makedirs(self.bin)
        podman = os.path.join(self.bin, "podman")
        with open(podman, "w") as f:
            f.write(FAKE_PODMAN)
        os.chmod(podman, 0o755)
        self.log = self.path("podman.log")
        self.env = {"PATH": self.bin + os.pathsep + os.environ["PATH"], "FAKE_PODMAN_LOG": self.log}

    def podman_calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(x) for x in f]

    def project(self, files):
        root = self.path("wd")
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w") as f:
                f.write(text)
        os.makedirs(root, exist_ok=True)
        return root


# ---------------------------------------------------------------- configuration

class TestSandboxConfig(unittest.TestCase):
    def test_defaults_and_validation(self):
        c = conf()
        self.assertEqual((c["engine"], c["network"], c["timeout"]), ("auto", False, G.SANDBOX_TIMEOUT))
        for bad in ({"image": None}, {"image": "-x"}, {"image": "a b"}, {"network": "yes"}, {"timeout": 0},
                    {"timeout": 7200}, {"timeout": True}, {"engine": "apptainer"}, {"memory": "lots"},
                    {"pids": 1}, {"mounts": []}):
            with self.assertRaises(G.HoplonError, msg=bad):
                conf(**bad)
        with self.assertRaises(G.HoplonError):
            G.Profile({"name": "p", "sandbox": "podman"}, "p.json")

    def test_summary_says_what_it_grants(self):
        text, _ = G.Profile({"name": "p", "coding": True, "sandbox": {"image": "img:1"}}, "p.json").summary()
        self.assertIn("throwaway copy", text)
        self.assertNotIn("network ON", text)
        text, _ = G.Profile({"name": "p", "sandbox": {"image": "img:1", "network": True}}, "p.json").summary()
        self.assertIn("[!] the sandbox starts with the network ON", text)

    def test_problem_when_podman_is_missing(self):
        with mock.patch.object(G.shutil, "which", return_value=None):
            self.assertEqual(G.Sandbox(conf(), ".", print).problem(), "podman is not installed")

    def test_refused_where_coding_mode_is_refused_even_with_coding_anywhere(self):
        home = os.path.realpath(tempfile.mkdtemp(prefix="hoplon-test-"))
        self.addCleanup(shutil.rmtree, home, True)
        for d in ("proj", ".ssh/keys", ".config/hoplon"):
            os.makedirs(os.path.join(home, d))
        env = {"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")}
        with mock.patch.dict(os.environ, env), mock.patch.object(G.shutil, "which", return_value="/usr/bin/podman"):
            for where, phrase in (("", "your home directory"), ("..", "contains your home directory"),
                                  (".ssh/keys", "holds credentials"), (".config/hoplon", "config directory")):
                problem = G.Sandbox(conf(), os.path.join(home, where), print).problem()
                self.assertIn(phrase, problem or "", where)
            self.assertIsNone(G.Sandbox(conf(), os.path.join(home, "proj"), print).problem())


# ---------------------------------------------------------------- what is hidden

class TestMasks(FakePodman):
    def test_secret_names_and_contents_are_hidden(self):
        root = self.project({
            ".env": "X=1\n", ".env.local": "X=1\n", "src/app.py": "print(1)\n", "deploy/server.key": "binary\n",
            "conf/settings.ini": "password = hunter2hunter2\n", ".aws/credentials": "k\n",
            ".git/config": "[core]\n", ".git/HEAD": "ref: x\n", "README.md": "Run make test.\n",
            ".venv/pyvenv.cfg": "home = /usr\n", ".venv/lib/key.pem": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
            "node_modules/x/.env": "X=1\n", "tests/fixture.txt": "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"})
        os.symlink("/etc/passwd", os.path.join(root, "link"))
        files, dirs = G.Sandbox(conf(), root, print).masks()
        self.assertEqual(sorted(files), [".env", ".env.local", "conf/settings.ini", "deploy/server.key",
                                         "tests/fixture.txt"])
        self.assertEqual(dirs, [".aws"])

    def test_git_config_with_a_credential_is_hidden(self):
        root = self.project({".git/config": '[remote "o"]\n\turl = https://me:ghp_abcdefghijklmnopqrstuvwxyz0123456789@x/r\n'})
        self.assertEqual(G.Sandbox(conf(), root, print).masks(), ([".git/config"], []))

    def test_the_endpoint_key_is_a_secret_too(self):
        root = self.project({"notes.txt": "the key is abcdef0123456789xyz\n"})
        box = G.Sandbox(conf(), root, print)
        self.assertEqual(box.masks(), ([], []))
        self.assertEqual(box.masks(["abcdef0123456789xyz"]), (["notes.txt"], []))

    def test_too_big_to_check_refuses(self):
        root = self.project({f"f{i}": "" for i in range(5)})
        with mock.patch.object(G, "SANDBOX_SCAN", 3), self.assertRaises(G.HoplonError):
            G.Sandbox(conf(), root, print).masks()


# ---------------------------------------------------------------- the podman command

class TestArgv(FakePodman):
    def argv(self, box):
        return box.argv("n", "make test", [".env"], [".aws"], "/empty", "g")

    def box(self, **kw):
        with mock.patch.dict(os.environ, self.env):
            return G.Sandbox(conf(**kw), "/p", print)

    def test_offline(self):
        a = self.argv(self.box())
        for flag in ("--network=none", "--http-proxy=false", "--pull=never", "--read-only", "--cap-drop=all",
                     "--userns=keep-id", "--security-opt=no-new-privileges", "--volume=/p:/work:O",
                     "--volume=/dev/null:/work/.env:ro", "--volume=/empty:/work/.aws:ro", "--env=HOPLON_CMD=make test",
                     "--env=HOME=/tmp", "--memory=4g", "--pids-limit=1024"):
            self.assertIn(flag, a)
        self.assertEqual(a[-2:], ["/work/.env", "/work/.aws"])
        self.assertFalse(any(x.startswith("--volume=" + os.path.expanduser("~")) for x in a))

    def test_online_and_without_limits(self):
        box = self.box(network=True)
        box.limits = False
        a = self.argv(box)
        self.assertFalse(any(x.startswith(("--network", "--http-proxy", "--memory", "--pids")) for x in a))


# ---------------------------------------------------------------- running, with a fake podman

class TestRun(FakePodman):
    def box(self, root, **kw):
        with mock.patch.dict(os.environ, self.env):
            return G.Sandbox(conf(**kw), root, self.notices.append)

    def setUp(self):
        super().setUp()
        self.notices = []

    def run_box(self, box, cmd):
        with mock.patch.dict(os.environ, self.env):
            return box.run(cmd)

    def test_runs_and_reports(self):
        root = self.project({"a.txt": "hi\n"})
        self.assertEqual(self.run_box(self.box(root), "cat a.txt; exit 3"), (True, 3, "hi\n", ""))

    def test_guard_refuses_when_a_secret_is_still_there(self):
        root = self.project({".env": "X=1\n"})          # the fake mounts nothing, so .env is still readable
        ran, rc, out, note = self.run_box(self.box(root), "cat .env")
        self.assertFalse(ran)
        self.assertIn("should have been hidden", note)
        self.assertEqual(out, "")

    def test_cgroup_refusal_falls_back_to_no_limits_once(self):
        root = self.project({"a.txt": "hi\n"})
        self.env["FAKE_PODMAN_CGROUP"] = "1"
        box = self.box(root)
        self.assertEqual(self.run_box(box, "echo ok")[:3], (True, 0, "ok\n"))
        self.assertEqual(self.run_box(box, "echo ok")[:3], (True, 0, "ok\n"))
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(len(self.podman_calls()), 3)

    def test_missing_image_says_how_to_fix(self):
        root = self.project({})
        self.env["FAKE_PODMAN_NOIMAGE"] = "1"
        ran, _, _, note = self.run_box(self.box(root), "true")
        self.assertFalse(ran)
        self.assertIn("podman pull img:1", note)

    def test_time_limit_kills_it(self):
        root = self.project({})
        ran, rc, _, note = self.run_box(self.box(root, timeout=1), "sleep 5")
        self.assertEqual((ran, rc, note), (True, None, "killed after 1 s"))
        self.assertTrue(any("kill" in c for c in self.podman_calls()))


# ---------------------------------------------------------------- end to end

class TestSandboxLoop(FakePodman):
    def start(self, api, sandbox, coding=True):
        home, ep = make_home(self.tmp, api.url)
        bless_path(home, ep)
        prof = {"name": "coder", "endpoint": "test", "coding": coding, "sandbox": sandbox}
        bless_path(home, write_json(os.path.join(home, "profiles", "coder.json"), prof))
        self.project({"a.txt": "hi\n"})
        self.home = home
        return Proc(home, self.path("wd"), ["-p", "coder"], env=self.env)

    def audit(self):
        with open(os.path.join(self.home, "audit.log")) as f:
            return [json.loads(x) for x in f]

    def test_offline_run_needs_no_approval_and_the_model_sees_the_output(self):
        turns = [call("run_in_sandbox", command="cat a.txt; echo SECRET=abcdefghijklmnop12345; exit 2", reason="t"),
                 reply("fixed")]
        with FakeAPI(turns) as api:
            g = self.start(api, {"image": "img:1"})
            hello = g.until("hello")
            self.assertEqual(hello["runner"]["network"], False)
            g.send({"type": "user", "text": "test it"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p["auto"], p["network"]), ("sandbox", True, False))
            r = g.until("result")
            self.assertEqual((r["status"], r["exit"]), ("error", 2))
            self.assertIn("hi", r["output"])
            self.assertNotIn("abcdefghijklmnop12345", r["output"])
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        first, second = api.requests[0]["body"], api.requests[1]["body"]
        self.assertIn("run_in_sandbox", [t["function"]["name"] for t in first["tools"]])
        self.assertIn("The sandbox is offline", first["messages"][0]["content"])
        self.assertNotIn("You still cannot run anything", first["messages"][0]["content"])
        tool = second["messages"][-1]
        self.assertTrue(tool["content"].startswith("exit 2 (sandbox, offline"))
        self.assertIn("hi", tool["content"])
        self.assertNotIn("abcdefghijklmnop12345", tool["content"])
        self.assertIn("--network=none", self.podman_calls()[0])
        events = {e["event"] for e in self.audit()}
        self.assertTrue({"sandbox_exit"} <= events)
        self.assertFalse(any("cat a.txt" in json.dumps(e) for e in self.audit()))   # commands by fingerprint only

    def test_not_offered_outside_coding_mode(self):
        with FakeAPI([call("run_in_sandbox", command="true", reason="t"), reply("ok")]) as api:
            g = self.start(api, {"image": "img:1"}, coding=False)
            g.send({"type": "user", "text": "x"})
            g.until("done")
            g.close()
        body = api.requests[0]["body"]
        self.assertNotIn("run_in_sandbox", [t["function"]["name"] for t in body["tools"]])
        self.assertIn("not available", api.requests[1]["body"]["messages"][-1]["content"])
        self.assertEqual(self.podman_calls(), [])

    def test_network_switch_makes_each_run_ask(self):
        with FakeAPI([call("run_in_sandbox", command="echo online", reason="t"), reply("ok")]) as api:
            g = self.start(api, {"image": "img:1"})
            g.send({"type": "network", "on": True})
            ev = g.until("network")
            self.assertEqual((ev["runner"]["network"], ev["available"]), (True, True))
            g.send({"type": "user", "text": "x"})
            p = g.until("proposal")
            self.assertEqual((p["kind"], p.get("auto"), p["network"]), ("sandbox", None, True))
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "decline"})
            g.until("done")
            g.close()
        self.assertEqual(self.podman_calls(), [])
        self.assertIn("online, so the user is asked", api.requests[0]["body"]["messages"][0]["content"])
        self.assertEqual(api.requests[1]["body"]["messages"][-1]["content"], "declined by the user")

    def test_network_on_and_accepted_runs_online(self):
        with FakeAPI([call("run_in_sandbox", command="echo online", reason="t"), reply("ok")]) as api:
            g = self.start(api, {"image": "img:1", "network": True})
            g.send({"type": "user", "text": "x"})
            p = g.until("proposal")
            g.send({"type": "decision", "id": p["id"], "digest": p["digest"], "decision": "accept"})
            self.assertEqual(g.until("result")["exit"], 0)
            g.until("done")
            g.close()
        self.assertNotIn("--network=none", self.podman_calls()[0])


class TestInterrupt(FakePodman):
    """In --json mode SIGINT stops a sandboxed run, like Ctrl-C in the REPL, and is ignored the rest of the time."""

    def test_sigint_kills_the_container_and_ends_the_turn_stopped(self):
        turns = [call("run_in_sandbox", command="echo started; sleep 30", reason="t"), reply("second")]
        with FakeAPI(turns) as api:
            home, ep = make_home(self.tmp, api.url)
            bless_path(home, ep)
            prof = {"name": "coder", "endpoint": "test", "coding": True, "sandbox": {"image": "img:1"}}
            bless_path(home, write_json(os.path.join(home, "profiles", "coder.json"), prof))
            self.project({"a.txt": "hi\n"})
            g = Proc(home, self.path("wd"), ["-p", "coder"], env=self.env)
            hello = g.until("hello")
            self.assertIn("interrupt", hello["features"])
            self.assertEqual(hello["runner"]["image"], "img:1")
            g.p.send_signal(signal.SIGINT)               # idle: ignored
            time.sleep(0.3)
            self.assertIsNone(g.p.poll())
            g.send({"type": "user", "text": "go"})
            g.until("proposal")
            deadline = time.time() + 10
            while not os.path.exists(self.log + ".pid") and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)
            g.p.send_signal(signal.SIGINT)
            r = g.until("result")
            self.assertEqual(r["status"], "stopped")
            self.assertEqual(g.until("done")["reason"], "stopped")
            self.assertTrue(any("kill" in c for c in self.podman_calls()))
            g.send({"type": "user", "text": "again"})    # hoplon is still there
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        self.assertTrue(any("stopped by the user" in str(m.get("content")) for m in api.requests[1]["body"]["messages"]))


    def test_stop_message_does_the_same_over_any_channel(self):
        turns = [call("run_in_sandbox", command="echo started; sleep 30", reason="t"), reply("second")]
        with FakeAPI(turns) as api:
            home, ep = make_home(self.tmp, api.url)
            bless_path(home, ep)
            prof = {"name": "coder", "endpoint": "test", "coding": True, "sandbox": {"image": "img:1"}}
            bless_path(home, write_json(os.path.join(home, "profiles", "coder.json"), prof))
            self.project({"a.txt": "hi\n"})
            g = Proc(home, self.path("wd"), ["-p", "coder"], env=self.env)
            self.assertIn("stop", g.until("hello")["features"])
            g.send({"type": "stop"})                     # idle: said so, and nothing else happens
            self.assertEqual(g.until("error")["code"], "not_running")
            g.send({"type": "user", "text": "go"})
            g.until("proposal")
            deadline = time.time() + 10
            while not os.path.exists(self.log + ".pid") and time.time() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)
            started = time.monotonic()
            g.send({"type": "stop"})                     # read while hoplon is busy with the run
            r = g.until("result")
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(r["status"], "stopped")
            self.assertEqual(g.until("done")["reason"], "stopped")
            self.assertTrue(any("kill" in c for c in self.podman_calls()))
            g.send({"type": "user", "text": "again"})
            self.assertEqual(g.until("done")["reason"], "reply")
            g.close()
        self.assertTrue(any("stopped by the user" in str(m.get("content")) for m in api.requests[1]["body"]["messages"]))


class TestKill(unittest.TestCase):
    """Ctrl-C at any point of a run kills the container, and a second one can't interrupt the kill."""

    def box(self):
        box = G.Sandbox(conf(), ".", print)
        box.podman = "/usr/bin/podman"
        return box

    def test_a_second_ctrl_c_does_not_interrupt_the_kill(self):
        def raises(signum, frame):
            raise KeyboardInterrupt
        before = signal.signal(signal.SIGINT, raises)
        self.addCleanup(signal.signal, signal.SIGINT, before)
        calls = []

        def podman_kill(argv, **kw):
            calls.append(argv)
            os.kill(os.getpid(), signal.SIGINT)         # the second Ctrl-C, while the kill runs
            time.sleep(0.05)
        proc = mock.Mock()
        with mock.patch.object(G.subprocess, "run", side_effect=podman_kill):
            self.box()._kill("hoplon-1", proc)            # returns: the SIGINT was ignored
        self.assertEqual(calls[0][-2:], ["kill", "hoplon-1"])
        proc.wait.assert_called()
        self.assertIs(signal.getsignal(signal.SIGINT), raises)   # and Ctrl-C works again afterwards

    def test_ctrl_c_as_podman_starts_still_kills_it_by_name(self):
        killed = []
        with mock.patch.object(G.subprocess, "Popen", side_effect=KeyboardInterrupt), \
                mock.patch.object(G.Sandbox, "_kill", lambda self, name, proc: killed.append((name, proc))):
            with self.assertRaises(KeyboardInterrupt):
                self.box()._podman(["podman", "run"], "hoplon-2", 100)
        self.assertEqual(killed, [("hoplon-2", None)])
        self.assertFalse(G.Sandbox.running)                # a later stray SIGINT is ignored again


class TestTerminal(unittest.TestCase):
    def test_shows_the_command_then_the_end_of_its_output(self):
        p = G.Proposal("p1", "sandbox", "c1", command="make test\x1b[2J", reason="check", network=False, image="i")
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            ui = G.TtyUI(out=io.StringIO())
            ui.show_auto(p)
            ui.result(p, "error", "exit 1 in 0.2 s", output="".join(f"line {i}\n" for i in range(50)), exit=1)
        text = err.getvalue()
        self.assertIn("sandbox, offline", text)
        self.assertIn("\\x1b[2J", text)                          # escaped, never sent to the terminal
        self.assertNotIn("\x1b", text)
        self.assertIn("10 earlier lines", text)
        self.assertIn("| line 49", text)
        self.assertNotIn("| line 9\n", text)
        self.assertIn("[p1] error: exit 1 in 0.2 s", text)


@unittest.skipUnless(shutil.which("podman") and subprocess.run(["podman", "image", "exists", IMAGE],
                                                                capture_output=True).returncode == 0,
                     f"needs podman and the image {IMAGE}")
class TestRealPodman(Tmp):
    def test_the_sandbox_holds(self):
        root = self.path("wd")
        for rel, text in {"a.txt": "hi\n", ".env": "API_KEY=sk-abcdefghijklmnopqrstuv\n",
                          ".aws/credentials": "k\n"}.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w") as f:
                f.write(text)
        box = G.Sandbox(conf(image=IMAGE, timeout=60), root, lambda m: None)
        ran, rc, out, _ = box.run("cat a.txt; cat .env; ls -A .aws; echo new > b.txt; echo more >> a.txt; "
                                  "ls -A $HOME | wc -l; (getent hosts example.com >/dev/null && echo NET) || echo NONET")
        self.assertTrue(ran, out)
        self.assertEqual((rc, out), (0, "hi\n0\nNONET\n"))
        self.assertEqual(sorted(os.listdir(root)), [".aws", ".env", "a.txt"])     # writes were thrown away
        with open(os.path.join(root, "a.txt")) as f:
            self.assertEqual(f.read(), "hi\n")


if __name__ == "__main__":
    unittest.main()
