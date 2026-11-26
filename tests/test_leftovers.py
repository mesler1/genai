"""Tests for leftovers: everything hoplon makes for sandboxes and jobs is found again, with whether it is likely stale
and why, the exact command that removes it, and a clean that removes only what is stale and safe to remove. Every
place it looks is pointed into a temp directory here (TMPDIR, XDG_RUNTIME_DIR, TMUX_TMPDIR, XDG_STATE_HOME, a fake
podman on PATH), so the tests never see, or touch, the real ones."""

import io
import json
import os
import socket
import stat
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakeapi import FakeAPI  # noqa: E402
from test_hoplon import G, Proc, Tmp, bless_path, make_home  # noqa: E402

FAKE_PODMAN = """#!/bin/sh
case "$1 $2" in
  "ps -a") cat "$FAKE_PODMAN_PS" ;;
  "rm -f") echo "$3" >> "$FAKE_PODMAN_RM" ;;
esac
"""


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


class Places(Tmp):
    """A tree with one of each kind of leftover, in temp directories named as the environment would name them."""

    def setUp(self):
        super().setUp()
        self.state, self.tmpdir = self.path("state"), self.path("tmp")
        self.runtime, self.tmux = self.path("run"), self.path("tmuxdir")
        self.bin = self.path("bin")
        for d in (self.state, self.tmpdir, self.runtime, self.tmux, self.bin):
            os.makedirs(d)
        os.makedirs(os.path.join(self.tmux, f"tmux-{os.getuid()}"))
        with open(os.path.join(self.bin, "podman"), "w") as f:
            f.write(FAKE_PODMAN)
        os.chmod(os.path.join(self.bin, "podman"), 0o755)
        self.ps = self.path("ps.json")
        self.rm = self.path("rm.log")
        self.write(self.ps, "[]")
        self.env = {"XDG_STATE_HOME": self.state, "TMPDIR": self.tmpdir, "XDG_RUNTIME_DIR": self.runtime,
                    "TMUX_TMPDIR": self.tmux, "PATH": self.bin + os.pathsep + os.environ["PATH"],
                    "FAKE_PODMAN_PS": self.ps, "FAKE_PODMAN_RM": self.rm}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        G.tempfile.tempdir = None                       # gettempdir() reads TMPDIR again
        self.addCleanup(setattr, G.tempfile, "tempdir", None)
        self.cfg = G.Config(self.path("home", "hoplon"))
        self.cfg.ensure()

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def old(self, path, hours=5):
        t = time.time() - hours * 3600
        os.utime(path, (t, t), follow_symlinks=False)

    def repo(self, name):
        top = self.path(name)
        os.makedirs(top)
        git(top, "init", "-q", "-b", "main")
        self.write(os.path.join(top, "a.txt"), "a\n")
        git(top, "add", "-A")
        git(top, "commit", "-q", "-m", "a")
        return os.path.realpath(top)

    def by(self, items, kind):
        return [i for i in items if i["kind"] == kind]


class TestScan(Places):
    def test_samples_folders_are_stale_once_their_hoplon_has_exited(self):
        proj = self.repo("proj")
        gone = G.Fixtures(self.cfg.fixtures_dir, proj, "s1")
        gone.add([], proj) if False else gone.ensure()
        meta = os.path.join(gone.group, "s1.json")
        rec = json.load(open(meta))
        json.dump(dict(rec, pid=dead_pid()), open(meta, "w"))
        live = G.Fixtures(self.cfg.fixtures_dir, proj, "s2")
        live.ensure()
        with mock.patch.object(G, "hoplon_alive", lambda pid: pid == os.getpid()):
            items = {i["path"]: i for i in self.by(G.leftovers(self.cfg), "fixtures")}
        self.assertIn("has exited", items[gone.dir]["stale"])
        self.assertTrue(items[gone.dir]["safe"])
        self.assertEqual(items[gone.dir]["remove"], f"rm -rf -- {gone.dir} {meta}")
        self.assertIsNone(items[live.dir]["stale"])
        self.assertFalse(items[live.dir]["safe"])

    def copy(self, proj, name, branch, pid, extra_commit=False):
        home = os.path.join(self.state, "hoplon", "work", name)
        os.makedirs(home)
        git(home, "clone", "-q", "--bare", proj, "git")
        if extra_commit:
            tree = os.path.join(home, "tree")
            git(home, "clone", "-q", os.path.join(home, "git"), tree)
            self.write(os.path.join(tree, "b.txt"), "b\n")
            git(tree, "add", "-A")
            git(tree, "commit", "-q", "-m", "b")
            git(tree, "push", "-q", "origin", "HEAD:main")
        self.write(os.path.join(home, "hoplon.json"),
                   json.dumps({"project": proj, "branch": branch, "pid": pid, "created": G.now()}))
        return home

    def test_work_copies_say_whether_removing_them_loses_commits(self):
        proj = self.repo("proj")
        git(proj, "branch", "hoplon/work-1")
        saved = self.copy(proj, "proj-1-aaa", "hoplon/work-1", dead_pid())
        unsaved = self.copy(proj, "proj-2-bbb", "hoplon/work-2", dead_pid(), extra_commit=True)
        unknown = os.path.join(self.state, "hoplon", "work", "proj-3-ccc")
        os.makedirs(unknown)
        items = {i["path"]: i for i in self.by(G.leftovers(self.cfg), "work")}
        self.assertTrue(items[saved]["safe"])
        self.assertIn("has ended", items[saved]["stale"])
        self.assertFalse(items[unsaved]["safe"])
        self.assertIn("HEAD:refs/heads/hoplon/work-2-kept", items[unsaved]["note"])
        self.assertFalse(items[unknown]["safe"])
        self.assertIn("look in it before removing it", items[unknown]["note"])

    def test_work_branches_merged_are_stale_and_unmerged_are_your_work(self):
        proj = self.repo("proj")
        git(proj, "branch", "hoplon/work-1")
        git(proj, "checkout", "-q", "-b", "hoplon/work-2")
        self.write(os.path.join(proj, "c.txt"), "c\n")
        git(proj, "add", "-A")
        git(proj, "commit", "-q", "-m", "c")
        git(proj, "checkout", "-q", "main")
        G.remember_project(proj)
        items = {i["path"]: i for i in self.by(G.leftovers(self.cfg), "branch")}
        merged, mine = items[proj + "#hoplon/work-1"], items[proj + "#hoplon/work-2"]
        self.assertEqual(merged["stale"], "merged into your current branch")
        self.assertEqual(merged["remove"], f"git -C {proj} branch -d hoplon/work-1")
        self.assertIsNone(mine["stale"])
        self.assertIn("your work to review", mine["note"])

    def test_containers_temp_files_and_tui_folders(self):
        dead = dead_pid()
        json.dump([{"Names": [f"hoplon-{dead}-1-0123456789ab"], "Created": time.time() - 99, "State": "exited"},
                   {"Names": [f"hoplon-{os.getpid()}-2-0123456789ab"], "Created": time.time(), "State": "running"},
                   {"Names": ["hoplon-db"], "Created": 0, "State": "running"}], open(self.ps, "w"))
        for name in ("hoplon-sh-abc", "hoplon-x1y2z3.txt", "hoplon-notes"):
            self.write(os.path.join(self.tmpdir, name), "x")
            self.old(os.path.join(self.tmpdir, name))
        self.write(os.path.join(self.tmpdir, "hoplon-sh-new"), "x")
        os.makedirs(os.path.join(self.tmpdir, "hoplon-empty-q"))
        self.old(os.path.join(self.tmpdir, "hoplon-empty-q"))
        os.makedirs(os.path.join(self.tmpdir, "hoplon-test-q"))
        dead_tui = os.path.join(self.runtime, "hoplon-tui.AAAA")
        live_tui = os.path.join(self.runtime, "hoplon-tui.BBBB")
        os.makedirs(dead_tui)
        os.makedirs(live_tui)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(os.path.join(live_tui, "sock"))
        srv.listen(1)
        self.addCleanup(srv.close)
        gone = socket.socket(socket.AF_UNIX)
        gone.bind(os.path.join(self.tmux, f"tmux-{os.getuid()}", "hoplon-tui-123"))
        gone.close()
        with mock.patch.object(G, "hoplon_alive", lambda pid: pid == os.getpid()):
            items = G.leftovers(self.cfg)
        names = {os.path.basename(i["path"].split(":", 1)[-1]): i for i in items}
        self.assertIn("has exited", names[f"hoplon-{dead}-1-0123456789ab"]["stale"])
        self.assertEqual(names[f"hoplon-{dead}-1-0123456789ab"]["remove"],
                         f"podman rm -f hoplon-{dead}-1-0123456789ab")
        self.assertIsNone(names[f"hoplon-{os.getpid()}-2-0123456789ab"]["stale"])
        self.assertNotIn("hoplon-db", names)                       # not one of ours, by its name
        self.assertTrue(names["hoplon-sh-abc"]["stale"])
        self.assertIn("may hold what you typed", names["hoplon-sh-abc"]["what"])
        self.assertTrue(names["hoplon-x1y2z3.txt"]["stale"])
        self.assertIsNone(names["hoplon-sh-new"]["stale"])          # too new to call stale
        self.assertTrue(names["hoplon-empty-q"]["stale"])
        self.assertNotIn("hoplon-notes", names)
        self.assertNotIn("hoplon-test-q", names)
        self.assertEqual(names["hoplon-tui.AAAA"]["stale"], "no hoplon-tui is listening in it")
        self.assertIsNone(names["hoplon-tui.BBBB"]["stale"])
        self.assertIn("socket file is left", names["hoplon-tui-123"]["stale"])

    def test_clean_removes_only_what_is_stale_and_safe_now(self):
        proj = self.repo("proj")
        keep = self.copy(proj, "proj-2-bbb", "hoplon/work-2", dead_pid(), extra_commit=True)
        for name in ("hoplon-sh-a", "hoplon-sh-b"):
            self.write(os.path.join(self.tmpdir, name), "x")
            self.old(os.path.join(self.tmpdir, name))
        items = G.leftovers(self.cfg)
        ids = [i["id"] for i in items]
        logged = []
        audit = mock.Mock(log=lambda e, **f: logged.append((e, f)))
        os.remove(os.path.join(self.tmpdir, "hoplon-sh-b"))         # gone between the listing and the clean
        done = G.clean_leftovers(self.cfg, ids, audit, "test")
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "hoplon-sh-a")))
        self.assertTrue(os.path.isdir(keep))                          # it may hold commits: never removed by clean
        self.assertEqual(len(done["removed"]), 1)
        self.assertEqual({s["why"] for s in done["skipped"]}, {"it needs a look first", "already gone"})
        self.assertEqual([e for e, _ in logged], ["leftover_removed"])

    def test_alike_stale_ones_are_one_entry_with_one_command(self):
        for n in range(5):
            self.write(os.path.join(self.tmpdir, f"hoplon-sh-{n}"), "x")
            self.old(os.path.join(self.tmpdir, f"hoplon-sh-{n}"))
        text = "\n".join(G.leftover_lines(G.leftovers(self.cfg), "hoplon --clean-stale"))
        self.assertIn("✗ 5 × a captured :sh session", text)
        self.assertEqual(text.count("remove: rm -rf --"), 1)
        self.assertIn("hoplon --clean-stale removes the 5 stale ones", text)
        self.assertIn("Nothing left over", G.leftover_lines([], "x")[0])


class TestCopyRecord(Places):
    def test_a_work_copy_says_whose_it_is(self):
        proj = self.repo("proj")
        copy, _ = G.Copy.create(proj)
        self.addCleanup(copy.remove)
        rec = json.load(open(os.path.join(copy.home, "hoplon.json")))
        self.assertEqual((rec["project"], rec["branch"], rec["pid"]), (proj, copy.branch, os.getpid()))
        self.assertEqual(json.load(open(G.projects_file())), [proj])
        self.assertFalse(os.path.exists(os.path.join(copy.path, "hoplon.json")))   # not where the model looks


class TestCommands(Places):
    def run_hoplon(self, *args, stdin=None):
        env = dict(os.environ, XDG_CONFIG_HOME=self.path("home"))
        return subprocess.run([sys.executable, G.__file__, *args], env=env, cwd=self.tmp, text=True,
                              capture_output=True, stdin=stdin or subprocess.DEVNULL, timeout=60)

    def test_list_as_text_and_json_and_clean_needs_a_terminal(self):
        self.write(os.path.join(self.tmpdir, "hoplon-sh-a"), "x")
        self.old(os.path.join(self.tmpdir, "hoplon-sh-a"))
        r = self.run_hoplon("--leftovers")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("likely stale: left over, 5 h old", r.stdout)
        r = self.run_hoplon("--leftovers", "--json")
        self.assertEqual([i["kind"] for i in json.loads(r.stdout)["items"]], ["temp"])
        r = self.run_hoplon("--clean-stale")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("needs a terminal", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "hoplon-sh-a")))

    def test_the_json_message(self):
        self.write(os.path.join(self.tmpdir, "hoplon-sh-a"), "x")
        self.old(os.path.join(self.tmpdir, "hoplon-sh-a"))
        home, ep = make_home(self.path("j"), "http://127.0.0.1:9/v1")
        bless_path(home, ep)
        with FakeAPI([]):
            g = Proc(home, self.tmp, ["-e", "test"], env=self.env)
            hello = g.until("hello")
            self.assertIn("leftovers", hello["features"])
            g.send({"type": "leftovers"})
            items = g.until("leftovers")["items"]
            self.assertEqual([i["kind"] for i in items], ["temp"])
            for bad in ({"action": "clean"}, {"action": "nuke", "ids": ["x"]}, {"ids": [1]}):
                g.send(dict(bad, type="leftovers"))
                self.assertEqual(g.until("error")["code"], "bad_message")
            g.send({"type": "leftovers", "action": "clean", "ids": [items[0]["id"]]})
            ev = g.until("leftovers")
            self.assertEqual((ev["removed"], ev["items"]), ([items[0]["id"]], []))
            g.close()
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "hoplon-sh-a")))

    def test_repl(self):
        self.write(os.path.join(self.tmpdir, "hoplon-sh-a"), "x")
        self.old(os.path.join(self.tmpdir, "hoplon-sh-a"))
        ep = G.Endpoint({"name": "t", "base_url": "https://llm.invalid/v1", "model": "m"}, "t.json")
        eng = G.Engine(self.cfg, ep, G.Session(self.cfg), G.PlainUI())
        repl, out = G.Repl(eng), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()):
            repl.command(":leftovers")
            with mock.patch.object(G, "ask_yes", return_value=False):
                repl.command(":leftovers clean")
            self.assertTrue(os.path.exists(os.path.join(self.tmpdir, "hoplon-sh-a")))
            with mock.patch.object(G, "ask_yes", return_value=True):
                repl.command(":leftovers clean")
            repl.command(":leftovers sometimes")
        text = out.getvalue()
        self.assertIn(":leftovers clean removes the 1 stale one", text)
        self.assertIn("Removed 1.", text)
        self.assertIn("usage: :leftovers", text)
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, "hoplon-sh-a")))


if __name__ == "__main__":
    unittest.main()
