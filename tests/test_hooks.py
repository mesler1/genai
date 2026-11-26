"""Offline tests for the notify hook. Run: python3 -m unittest discover -s tests"""

import contextlib
import io
import json
import os
import select
import subprocess
import sys
import threading
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_hoplon as T  # noqa: E402
from fakeapi import FakeAPI, call, calls, reply  # noqa: E402

G, SCRIPT = T.G, T.SCRIPT
KEY_VAR = "HOPLON_TEST_KEY"
KEY_VALUE = "key-value-r7x2"

# What a test hook does: append what it saw (one tab-separated line) to $HOOK_OUT, then make noise.
RECORDER = """#!/bin/sh
line=$(cat)
if [ -t 1 ]; then t=tty; else t=notty; fi
printf '%s\\t%s\\t%s\\t%s\\n' "$line" "${HOPLON_TEST_KEY-unset}" "${OTHER_VAR-unset}" "$t" >> "$HOOK_OUT"
echo HOOKNOISE-q7
echo HOOKNOISE-q7 >&2
"""


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class HookCase(T.Tmp):
    """A temp config home with a blessed endpoint, and helpers for installing and blessing a hook."""

    def setUp(self):
        super().setUp()
        self.api = FakeAPI([])
        self.api.__enter__()
        self.addCleanup(self.api.__exit__)
        patch = mock.patch.dict(os.environ, {KEY_VAR: KEY_VALUE, "OTHER_VAR": "ordinary",
                                             "HOOK_OUT": self.path("hook.out"), "HOME": self.tmp})
        patch.start()
        self.addCleanup(patch.stop)
        self.home, self.epath = T.make_home(self.tmp, self.api.url, key_env=KEY_VAR)
        self.cfg = G.Config(self.home)
        self.cfg.set_blessed(self.epath, {"kind": "endpoint", "name": "test", "hash": G.hash_file(self.epath)})
        self.hook = os.path.join(self.home, "hooks", "notify")
        self.out = self.path("hook.out")
        self.wd = self.path("wd")
        os.makedirs(self.wd)

    def install(self, text=RECORDER, mode=0o700, bless=True):
        os.makedirs(os.path.dirname(self.hook), mode=0o700, exist_ok=True)
        with open(self.hook, "w") as f:
            f.write(text)
        os.chmod(self.hook, mode)
        if bless:
            self.bless()

    def bless(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(G.bless(self.cfg, self.hook, lambda q: True))

    def records(self):
        """The recorder's lines as (json, key variable, other variable, tty), in file order."""
        try:
            with open(self.out) as f:
                rows = [ln.rstrip("\n").split("\t") for ln in f]
        except FileNotFoundError:
            return []
        return [(json.loads(r[0]), r[1], r[2], r[3]) for r in rows]

    def wait_records(self, n, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            recs = self.records()
            if len(recs) >= n:
                return recs
            time.sleep(0.05)
        self.fail(f"expected {n} hook runs, saw {len(self.records())}")

    def settle(self):
        """Give a background hook that must NOT have run a moment to (wrongly) run."""
        time.sleep(0.4)

    def audit(self):
        try:
            with open(self.cfg.audit.path) as f:
                return [json.loads(x) for x in f]
        except FileNotFoundError:
            return []

    def env(self, **extra):
        e = dict(os.environ, XDG_CONFIG_HOME=os.path.dirname(self.home), HOME=self.tmp, TERM="dumb",
                 HOOK_OUT=self.out, OTHER_VAR="ordinary", **{KEY_VAR: KEY_VALUE})
        e.pop("HOPLON_SANDBOX", None)
        e.update(extra)
        return e

    def pty_run(self, args, steps=(), timeout=20, **env):
        """Run the CLI on a pseudo-terminal, so the terminal UI is chosen. steps: (token to wait for, text to
        send). Returns (everything printed, exit code)."""
        import pty
        self.api.requests.clear()
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, SCRIPT, "-e", "test"] + list(args), cwd=self.wd,
                                env=self.env(**env), stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        out = b""
        end = time.time() + timeout

        def pump(token=None):
            nonlocal out
            while token is None or token.encode() not in out:
                r, _, _ = select.select([master], [], [], max(0, end - time.time()))
                if not r:
                    raise AssertionError(f"timed out waiting for {token!r}; got:\n{out.decode(errors='replace')}")
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    return
                out += chunk

        try:
            for token, text in steps:
                pump(token)
                os.write(master, text.encode())
            pump()
            rc = proc.wait(max(1, end - time.time()))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)
        return out.decode(errors="replace").replace("\r\n", "\n"), rc

    def stub_engine(self, session="nightly"):
        """Just enough of an Engine for a Notifier."""
        notices = []
        ep = G.Endpoint({"name": "work-llm", "base_url": "https://llm.example/v1", "model": "m",
                         "key_env": KEY_VAR}, "x.json")
        eng = SimpleNamespace(cfg=self.cfg, ui=SimpleNamespace(notice=notices.append), endpoint=ep,
                              session=SimpleNamespace(id=session), sandbox=None, audit=self.cfg.audit)
        eng.notices = notices
        return eng


class TestBlessHook(HookCase):
    def run_bless(self, answer=True):
        buf = io.StringIO()
        asked = []
        with contextlib.redirect_stdout(buf):
            ok = G.bless(self.cfg, self.hook, lambda q: asked.append(q) or answer)
        return ok, buf.getvalue(), asked

    def test_shown_and_recorded(self):
        self.install(bless=False)
        ok, out, asked = self.run_bless()
        self.assertTrue(ok)
        self.assertEqual(len(asked), 1)
        self.assertIn(self.hook, out)
        self.assertIn("a hook: a program hoplon will run as you when it is waiting for you", out)
        self.assertIn(f"{len(RECORDER.encode())} bytes", out)
        self.assertIn("    | #!/bin/sh", out)
        self.assertNotIn("not executable", out)
        entry = self.cfg.blessed()[self.hook]
        self.assertEqual((entry["kind"], entry["hash"]), ("hook", G.hash_file(self.hook)))
        self.assertTrue(any(a["event"] == "bless" and a["kind"] == "hook" for a in self.audit()))

    def test_declined_records_nothing(self):
        self.install(bless=False)
        ok, out, _ = self.run_bless(answer=False)
        self.assertFalse(ok)
        self.assertNotIn(self.hook, self.cfg.blessed())
        self.assertIn("Not blessed", out)

    def test_refused_cases(self):
        os.makedirs(os.path.dirname(self.hook))
        real = self.path("real-hook")
        with open(real, "w") as f:
            f.write(RECORDER)
        os.chmod(real, 0o700)
        os.symlink(real, self.hook)
        with self.assertRaisesRegex(G.HoplonError, "regular file"):
            self.run_bless()
        os.unlink(self.hook)
        self.install(mode=0o770, bless=False)
        with self.assertRaisesRegex(G.HoplonError, "writable by group or others"):
            self.run_bless()
        os.chmod(self.hook, 0o702)
        with self.assertRaisesRegex(G.HoplonError, "writable by group or others"):
            self.run_bless()
        with open(self.hook, "wb") as f:
            f.write(b"#!/bin/sh\n\xff\xfe\n")
        os.chmod(self.hook, 0o700)
        with self.assertRaisesRegex(G.HoplonError, "UTF-8"):
            self.run_bless()
        self.assertNotIn(self.hook, self.cfg.blessed())

    def test_missing_file_is_refused(self):
        with self.assertRaises(G.HoplonError):
            self.run_bless()

    def test_not_executable_is_blessed_with_a_note(self):
        self.install(mode=0o600, bless=False)
        ok, out, _ = self.run_bless()
        self.assertTrue(ok)
        self.assertIn("will not run until you: chmod u+x", out)
        self.assertEqual(G.hook_problem(self.cfg, self.hook), "is not executable (chmod u+x)")

    def test_hidden_characters_are_listed_and_escaped(self):
        self.install("#!/bin/sh\n# \x1b[2J ‮\n", bless=False)
        ok, out, _ = self.run_bless()
        self.assertTrue(ok)
        self.assertIn("hidden or control characters: U+001B x1, U+202E x1", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("‮", out)
        self.assertIn("\\x1b", out)

    def test_cli_blesses_the_hook(self):
        """--bless PATH on a terminal goes through the same flow."""
        self.install(bless=False)
        import pty
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, SCRIPT, "--bless", self.hook], env=self.env(), stdin=slave,
                                stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        buf = b""
        end = time.time() + 15
        sent = False
        try:
            while time.time() < end:
                r, _, _ = select.select([master], [], [], 0.2)
                if r:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                if not sent and b"[y/N]" in buf:
                    os.write(master, b"y\n")
                    sent = True
            proc.wait(5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)
        self.assertIn("Blessed.", buf.decode(errors="replace"))
        self.assertEqual(self.cfg.blessed()[self.hook]["kind"], "hook")


class TestNotifierChecks(HookCase):
    """The checks made at start and at each run, with a stand-in engine."""

    def fire(self, eng, event="reply", **fields):
        G.Notifier(eng).fire(event, **fields)

    def start(self, eng):
        n = G.Notifier.start(eng)
        return n

    def test_no_file_is_silent(self):
        eng = self.stub_engine()
        self.assertIsNone(G.Notifier.start(eng))
        self.assertEqual(eng.notices, [])

    def test_not_blessed_is_not_run_and_said_once(self):
        self.install(bless=False)
        eng = self.stub_engine()
        self.assertIsNone(G.Notifier.start(eng))
        self.assertEqual(len(eng.notices), 1)
        self.assertIn("is not blessed", eng.notices[0])
        self.assertIn(f"hoplon --bless {self.hook}", eng.notices[0])
        self.settle()
        self.assertEqual(self.records(), [])

    def test_edited_after_blessing(self):
        self.install()
        with open(self.hook, "a") as f:
            f.write("# changed\n")
        eng = self.stub_engine()
        self.assertIsNone(G.Notifier.start(eng))
        self.assertEqual(len(eng.notices), 1)
        self.assertIn("changed since it was blessed", eng.notices[0])
        self.settle()
        self.assertEqual(self.records(), [])

    def test_group_writable_after_blessing(self):
        self.install()
        os.chmod(self.hook, 0o770)
        eng = self.stub_engine()
        self.assertIsNone(G.Notifier.start(eng))
        self.assertIn("writable by group or others", eng.notices[0])
        self.settle()
        self.assertEqual(self.records(), [])

    def test_replaced_by_symlink(self):
        self.install()
        real = self.path("real-hook")
        os.rename(self.hook, real)
        os.symlink(real, self.hook)
        eng = self.stub_engine()
        self.assertIsNone(G.Notifier.start(eng))
        self.assertIn("not a regular file", eng.notices[0])
        self.settle()
        self.assertEqual(self.records(), [])

    def test_not_executable(self):
        self.install()
        os.chmod(self.hook, 0o600)
        eng = self.stub_engine()
        self.assertIsNone(G.Notifier.start(eng))
        self.assertIn("not executable", eng.notices[0])

    def test_change_during_the_session_stops_it_with_one_notice(self):
        self.install()
        eng = self.stub_engine()
        n = G.Notifier.start(eng)
        self.assertIsNotNone(n)
        n.fire("reply")
        self.wait_records(1)
        with open(self.hook, "a") as f:
            f.write("# edited mid-session\n")
        for _ in range(3):
            n.fire("reply")
        self.settle()
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(eng.notices), 1)
        self.assertIn("changed since it was blessed", eng.notices[0])
        self.assertIn("rest of this session", eng.notices[0])
        self.assertEqual(len([a for a in self.audit() if a["event"] == "hook"]), 1)

    def test_a_hook_that_cannot_start_is_ignored_once(self):
        self.install("#!/nonexistent/interpreter\n")
        eng = self.stub_engine()
        n = G.Notifier.start(eng)
        self.assertIsNotNone(n)
        n.fire("reply")
        n.fire("reply")
        self.assertEqual(len(eng.notices), 1)
        self.assertIn("could not be started", eng.notices[0])
        self.assertEqual([a for a in self.audit() if a["event"] == "hook"], [])


class TestNotifierRuns(HookCase):
    def test_the_line_for_each_event(self):
        self.install()
        eng = self.stub_engine()
        n = G.Notifier.start(eng)
        n.fire("approval", count=2, kinds=["edit", "write"])
        n.fire("command")
        n.fire("reply")
        recs = {r[0]["event"]: r[0] for r in self.wait_records(3)}
        base = {"session": "nightly", "cwd": os.getcwd(), "endpoint": "work-llm", "sandbox": None,
                "version": G.VERSION}
        self.assertEqual(recs["approval"], {"event": "approval", "count": 2, "kinds": ["edit", "write"], **base})
        self.assertEqual(recs["command"], {"event": "command", **base})
        self.assertEqual(recs["reply"], {"event": "reply", **base})
        self.assertNotIn("llm.example", json.dumps(list(recs.values())))

    def test_json_is_ascii_and_session_may_be_null(self):
        self.install()
        eng = self.stub_engine(session=None)
        os.makedirs(self.path("café"))
        with mock.patch("os.getcwd", return_value=self.path("café")):
            G.Notifier.start(eng).fire("reply")
        self.wait_records(1)
        with open(self.out) as f:
            line = f.read().split("\t")[0]
        self.assertTrue(line.isascii())
        self.assertIn("\\u00e9", line)
        self.assertIsNone(json.loads(line)["session"])

    def test_key_variable_is_removed_and_others_are_kept(self):
        self.install()
        eng = self.stub_engine()
        G.Notifier.start(eng).fire("reply")
        (rec,) = self.wait_records(1)
        self.assertEqual(rec[1], "unset")
        self.assertEqual(rec[2], "ordinary")
        self.assertEqual(rec[3], "notty")
        self.assertNotIn(KEY_VALUE, json.dumps(rec[0]))

    def test_hook_past_the_timeout_is_killed_with_its_children(self):
        child = self.path("child.pid")
        self.install(f"#!/bin/sh\nsleep 60 &\necho $! > '{child}'\necho $$ >> '{child}'\nwait\n")
        eng = self.stub_engine()
        before = threading.active_count()
        with mock.patch.object(G, "HOOK_TIMEOUT", 1):
            t0 = time.time()
            G.Notifier.start(eng).fire("reply")
            self.assertLess(time.time() - t0, 0.9)            # hoplon did not wait for it
            end = time.time() + 5
            while time.time() < end and not (os.path.exists(child) and len(open(child).read().split()) == 2):
                time.sleep(0.05)
            pids = [int(x) for x in open(child).read().split()]
            self.assertTrue(all(alive(p) for p in pids))
            end = time.time() + 8
            while time.time() < end and any(alive(p) for p in pids):
                time.sleep(0.1)
            self.assertFalse(any(alive(p) for p in pids), "the hook and its child should have been killed")
            while time.time() < end and threading.active_count() > before:
                time.sleep(0.05)


class TestCli(HookCase):
    """The whole program, on a pseudo-terminal."""

    SEEDS = ("MODELTEXT-zq91", "CMDTEXT-zq91", "PATHNAME-zq91", "FILECONTENT-zq91", "OLDTEXT-zq91", "NEWTEXT-zq91")

    def hook_audit(self):
        return [a for a in self.audit() if a["event"] == "hook"]

    def test_approval_command_and_reply(self):
        self.install()
        with open(os.path.join(self.wd, "a.txt"), "w") as f:
            f.write("OLDTEXT-zq91\n")
        self.api.turns[:] = [
            calls(("write_file", {"path": "PATHNAME-zq91.txt", "content": "FILECONTENT-zq91\n", "reason": "r"}),
                  ("edit_file", {"path": "a.txt", "old_text": "OLDTEXT-zq91", "new_text": "NEWTEXT-zq91",
                                 "reason": "r"})),
            call("propose_command", command="echo CMDTEXT-zq91", reason="MODELTEXT-zq91")]
        out, rc = self.pty_run(["go"], [("Accept p1?", "n\n"), ("Accept p2?", "n\n")])
        self.assertEqual(rc, 0, out)
        self.wait_records(2)
        self.settle()       # a turn that ends with a proposed command fires "command", not "reply" as well
        self.assertEqual(sorted(r[0]["event"] for r in self.records()), ["approval", "command"])
        self.api.turns[:] = [reply("MODELTEXT-zq91")]
        out2, rc = self.pty_run(["go"])
        self.assertEqual(rc, 0, out2)
        out += out2
        recs = {r[0]["event"]: r for r in self.wait_records(3)}
        self.assertEqual(set(recs), {"approval", "command", "reply"})
        sb = G.sandbox_info()
        base = {"session": None, "cwd": self.wd, "endpoint": "test", "sandbox": sb, "version": G.VERSION}
        self.assertEqual(recs["approval"][0], {"event": "approval", "count": 2, "kinds": ["edit", "write"], **base})
        self.assertEqual(recs["command"][0], {"event": "command", **base})
        self.assertEqual(recs["reply"][0], {"event": "reply", **base})
        with open(self.out) as f:
            seen = f.read()
        for s in self.SEEDS + (KEY_VALUE, self.api.url):
            self.assertNotIn(s, seen)
        self.assertEqual({r[1] for r in recs.values()}, {"unset"})
        self.assertEqual({r[2] for r in recs.values()}, {"ordinary"})
        self.assertNotIn("HOOKNOISE", out)
        runs = self.hook_audit()
        self.assertEqual(sorted(a["when"] for a in runs), ["approval", "command", "reply"])
        ap = [a for a in runs if a["when"] == "approval"][0]
        self.assertEqual({k: v for k, v in ap.items() if k not in ("ts", "pid")},
                         {"event": "hook", "hook": "notify", "when": "approval", "count": 2,
                          "kinds": ["edit", "write"]})
        self.assertEqual(set(runs[0]) - {"ts", "pid", "event", "hook", "when", "count", "kinds"}, set())

    def test_automatic_reads_do_not_fire_approval(self):
        self.install()
        with open(os.path.join(self.wd, "a.txt"), "w") as f:
            f.write("hello\n")
        self.api.turns[:] = [call("read_file", path="a.txt", reason="r"), reply("MODELTEXT-zq91")]
        out, rc = self.pty_run(["--coding", "go"])
        self.assertEqual(rc, 0, out)
        recs = self.wait_records(1)
        self.settle()
        self.assertEqual([r[0]["event"] for r in self.records()], ["reply"])
        self.assertEqual(len(recs), 1)

    def test_a_mixed_batch_counts_only_what_asks(self):
        self.install()
        with open(os.path.join(self.wd, "a.txt"), "w") as f:
            f.write("hello\n")
        self.api.turns[:] = [
            calls(("read_file", {"path": "a.txt", "reason": "r"}),
                  ("write_file", {"path": "b.txt", "content": "x\n", "reason": "r"})), reply("done")]
        out, rc = self.pty_run(["--coding", "go"], [("Accept p2?", "n\n")])
        self.assertEqual(rc, 0, out)
        self.wait_records(2)
        ap = [r[0] for r in self.records() if r[0]["event"] == "approval"]
        self.assertEqual([(a["count"], a["kinds"]) for a in ap], [(1, ["write"])])

    def test_banner_and_reply_in_the_repl(self):
        self.install()
        self.api.turns[:] = [reply("MODELTEXT-zq91")]
        out, rc = self.pty_run([], [("> ", "hi\n..\n"), ("MODELTEXT-zq91", ":quit\n")])
        self.assertEqual(rc, 0, out)
        short = self.hook.replace(self.tmp, "~")      # HOME is the temp dir
        self.assertIn(f"  hook: notify (runs {short} when hoplon waits for you)", out)
        self.assertEqual(out.count("hook: notify"), 1)
        self.wait_records(1)
        self.assertEqual([r[0]["event"] for r in self.records()], ["reply"])
        self.assertNotIn("HOOKNOISE", out)

    def test_banner_in_one_shot(self):
        self.install()
        self.api.turns[:] = [reply("ok")]
        out, rc = self.pty_run(["go"])
        self.assertIn("hook: notify (runs", out)

    def test_unblessed_hook_one_notice_and_no_run(self):
        self.install(bless=False)
        with open(os.path.join(self.wd, "a.txt"), "w") as f:
            f.write("x\n")
        self.api.turns[:] = [call("write_file", path="b.txt", content="x\n", reason="r"), reply("ok")]
        out, rc = self.pty_run(["go"], [("Accept p1?", "n\n")])
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count("is not blessed"), 1)
        self.assertIn(f"hoplon --bless {self.hook}", out)
        self.assertNotIn("hook: notify", out)
        self.settle()
        self.assertEqual(self.records(), [])
        self.assertEqual(self.hook_audit(), [])

    def test_edited_hook_one_notice_and_no_run(self):
        self.install()
        with open(self.hook, "a") as f:
            f.write("# edited\n")
        self.api.turns[:] = [reply("ok")]
        out, rc = self.pty_run(["go"])
        self.assertEqual(out.count("changed since it was blessed"), 1)
        self.settle()
        self.assertEqual(self.records(), [])

    def test_a_slow_hook_does_not_delay_hoplon(self):
        pidfile = self.path("slow.pid")
        self.install(f"#!/bin/sh\necho $$ > '{pidfile}'\nexec sleep 30\n")
        self.api.turns[:] = [reply("ok")]
        t0 = time.time()
        out, rc = self.pty_run(["go"])
        took = time.time() - t0
        self.assertEqual(rc, 0, out)
        end = time.time() + 5
        while time.time() < end and not os.path.exists(pidfile):
            time.sleep(0.05)
        pid = int(open(pidfile).read())
        self.addCleanup(self._kill_group, pid)
        self.assertLess(took, 8)

    @staticmethod
    def _kill_group(pid):
        try:
            os.killpg(pid, 9)
        except OSError:
            pass

    def test_not_active_in_json_mode(self):
        self.install()
        self.api.turns[:] = [call("propose_command", command="echo hi", reason="r"), reply("ok")]
        p = T.Proc(self.home, self.wd, ["-e", "test"])
        hello = p.until("hello")
        self.assertNotIn("hook", json.dumps(hello))
        p.send({"type": "user", "text": "go"})
        p.until("done")
        p.close()
        self.settle()
        self.assertEqual(self.records(), [])
        self.assertEqual(self.hook_audit(), [])

    def test_not_active_when_output_is_piped(self):
        self.install()
        self.api.turns[:] = [reply("ok")]
        r = subprocess.run([sys.executable, SCRIPT, "-e", "test", "go"], cwd=self.wd, env=self.env(),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("hook", r.stdout + r.stderr)
        self.settle()
        self.assertEqual(self.records(), [])
        self.assertEqual(self.hook_audit(), [])

    def test_no_hook_file_changes_nothing(self):
        self.api.turns[:] = [call("propose_command", command="echo hi", reason="r")]
        out, rc = self.pty_run(["go"])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("hook", out.lower())
        self.assertNotIn("notify", out.lower())
        self.assertEqual(self.hook_audit(), [])
        self.assertFalse(os.path.exists(os.path.dirname(self.hook)))
        self.api.turns[:] = [reply("ok")]
        out, rc = self.pty_run([], [("> ", ":quit\n")])
        self.assertEqual(rc, 0, out)
        self.assertNotIn("hook", out.lower())



class TestAlertsSwitch(unittest.TestCase):
    """alerts: false (config.json) or :alerts off keeps the notify hook quiet."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hoplon-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def engine(self, alerts=None):
        home = os.path.join(self.tmp, "home", "hoplon")
        cfg = G.Config(home)
        cfg.ensure()
        if alerts is not None:
            with open(os.path.join(home, "config.json"), "w") as f:
                json.dump({"alerts": alerts}, f)
            cfg = G.Config(home)
        ep = G.Endpoint({"name": "t", "base_url": "https://llm.invalid/v1", "model": "m"}, "t.json")
        eng = G.Engine(cfg, ep, G.Session(cfg), G.PlainUI())
        eng.notifier = mock.Mock()
        return eng

    def test_on_by_default_off_in_config_and_switched_by_command(self):
        eng = self.engine()
        eng.notify("reply")
        self.assertEqual(eng.notifier.fire.call_count, 1)
        quiet = self.engine(alerts=False)
        quiet.notify("reply")
        quiet.notifier.fire.assert_not_called()
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            G.Repl(quiet).command(":alerts on")
            G.Repl(quiet).command(":alerts sometimes")
        quiet.notify("approval", count=1, kinds=["edit"])
        self.assertEqual(quiet.notifier.fire.call_count, 1)
        self.assertIn("alerts: on", out.getvalue())
        self.assertIn("usage: :alerts", out.getvalue())
        with self.assertRaisesRegex(G.HoplonError, "alerts must be true or false"):
            self.engine(alerts="no")


if __name__ == "__main__":
    unittest.main()
