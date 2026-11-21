"""Tests for the sandbox hooks in sandbox/run and for sandbox/hooks/modules.sh. podman is a fake
that logs what it was given. Run: python3 -m unittest discover -s tests"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUN = os.path.join(ROOT, "sandbox", "run")
MODULES = os.path.join(ROOT, "sandbox", "hooks", "modules.sh")
BASH = shutil.which("bash") or "/bin/bash"

FAKE_PODMAN = """#!/bin/sh
[ "$1" = run ] || exit 0
{
  echo "RUN"
  for a in "$@"; do echo "ARG:$a"; done
  for v in $FAKE_LOG_VARS; do eval "printf 'ENV:%s=%s\\n' $v \\"\\${$v-<unset>}\\""; done
} >> "$FAKE_LOG"
exit 0
"""


def write(path, text, mode=0o755):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        self.bin = self.p("fakebin")
        self.home = self.p("home")
        self.state = self.p("state")
        self.hooks = os.path.join(self.state, "hooks")
        self.log = self.p("podman.log")
        for d in (self.bin, self.home, self.hooks):
            os.makedirs(d)
        write(os.path.join(self.bin, "podman"), FAKE_PODMAN)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)

    def hook(self, name, lines=(), body=None, mode=0o755, directory=None):
        text = body if body is not None else (
            "#!/bin/sh\ncat <<'HOOK_EOF'\n" + "\n".join(lines) + "\nHOOK_EOF\n")
        path = os.path.join(directory or self.hooks, name)
        write(path, text, mode)
        return path

    def run_sandbox(self, *args, mode="selfcheck", env=None, log_vars=""):
        e = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], HOME=self.home,
                 GENAI_SANDBOX_STATE=self.state, FAKE_LOG=self.log, FAKE_LOG_VARS=log_vars)
        for k in ("GENAI_SANDBOX_HOOKS", "GENAI_SANDBOX_RO", "GENAI_KEY_ENV"):
            e.pop(k, None)
        e.update(env or {})
        return subprocess.run(["bash", RUN, mode, *args], env=e, capture_output=True, text=True, timeout=60)

    def podman_runs(self):
        """One (args, env) pair per podman run."""
        if not os.path.exists(self.log):
            return []
        runs = []
        with open(self.log) as f:
            for line in f.read().splitlines():
                if line == "RUN":
                    runs.append(([], {}))
                elif line.startswith("ARG:"):
                    runs[-1][0].append(line[4:])
                elif line.startswith("ENV:"):
                    k, _, v = line[4:].partition("=")
                    runs[-1][1][k] = v
        return runs

    def ok(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        runs = self.podman_runs()
        self.assertEqual(len(runs), 1, r.stderr)
        return runs[0]

    def refused(self, r, text=None):
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.podman_runs(), [], "podman was started")
        if text:
            self.assertIn(text, r.stderr)

    def tree(self, *rel):
        """Make directories under the temp dir and return the first."""
        for r in rel:
            os.makedirs(self.p(r), exist_ok=True)
        return self.p(rel[0])


class TestHooksDirectory(SandboxCase):
    def test_no_hooks_directory_runs_as_before(self):
        shutil.rmtree(self.hooks)
        r = self.run_sandbox()
        args, _ = self.ok(r)
        self.assertNotIn("hook", r.stderr)
        self.assertEqual([a for a in args if a.startswith(self.tmp)], [])

    def test_empty_hooks_directory(self):
        r = self.run_sandbox()
        self.ok(r)
        self.assertNotIn("hook", r.stderr)

    def test_override_directory(self):
        other = self.p("other-hooks")
        os.makedirs(other)
        d = self.tree("mods/a")
        self.hook("a", [f"ro {d}"], directory=other)
        self.hook("ignored", ["ro /nonexistent-ignored"])
        r = self.run_sandbox(env={"GENAI_SANDBOX_HOOKS": other})
        args, _ = self.ok(r)
        self.assertIn(f"{d}:{d}:ro", args)
        self.assertNotIn("ignored", r.stderr)

    def test_hooks_run_in_name_order_with_the_mode(self):
        seen = self.p("order")
        for name in ("20-b", "10-a", "5-z"):
            self.hook(name, body=f"#!/bin/sh\necho \"{name} $1 $#\" >> {seen}\n")
        for mode in ("selfcheck", "tests"):
            self.run_sandbox(mode=mode)
        with open(seen) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ["10-a selfcheck 1", "20-b selfcheck 1", "5-z selfcheck 1",
                                 "10-a tests 1", "20-b tests 1", "5-z tests 1"])

    def test_hook_gets_no_input(self):
        out = self.p("stdin")
        self.hook("a", body=f"#!/bin/sh\ncat > {out}\n")
        self.ok(self.run_sandbox())
        with open(out) as f:
            self.assertEqual(f.read(), "")

    def test_unknown_mode_runs_no_hooks(self):
        seen = self.p("ran")
        self.hook("a", body=f"#!/bin/sh\ntouch {seen}\n")
        r = self.run_sandbox(mode="nonsense")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(seen))

    def test_report_line_per_hook(self):
        d = self.tree("mods/a")
        self.hook("a", [f"ro {d}", "# a comment", "", "env FOO_HOME"], )
        self.hook("b", [])
        r = self.run_sandbox(env={"FOO_HOME": d})
        self.ok(r)
        lines = [ln for ln in r.stderr.splitlines() if ln.startswith("sandbox/run: hook ")]
        self.assertEqual(lines, [f"sandbox/run: hook a: ro {d}; env FOO_HOME",
                                 "sandbox/run: hook b: no requests"])

    def test_hook_stderr_is_passed_on(self):
        self.hook("a", body="#!/bin/sh\necho hello from the hook >&2\n")
        r = self.run_sandbox()
        self.ok(r)
        self.assertIn("hello from the hook", r.stderr)


class TestHookChecks(SandboxCase):
    def test_nonzero_exit_refused(self):
        self.hook("a", body="#!/bin/sh\necho 'ro /nonexistent'\nexit 3\n")
        self.refused(self.run_sandbox(), "hook a failed")

    def test_unknown_request_refused_and_shown_safely(self):
        self.hook("a", ["mount /etc"])
        self.refused(self.run_sandbox(), "unknown request")
        self.hook("a", ["rw /tmp"])
        self.refused(self.run_sandbox(), "unknown request")
        self.hook("a", ["ro /a /b"])
        self.refused(self.run_sandbox(), "unknown request")
        self.hook("a", ["ro", "path"])
        self.refused(self.run_sandbox(), "unknown request")
        self.hook("a", body="#!/bin/sh\nprintf 'bad\\033[31mred\\n'\n")
        r = self.run_sandbox()
        self.refused(r, "unknown request")
        self.assertNotIn("\x1b", r.stderr)

    def test_group_writable_hook_refused(self):
        self.hook("a", [], mode=0o775)
        self.refused(self.run_sandbox(), "writable by group or others")

    def test_other_writable_hook_refused(self):
        self.hook("a", [], mode=0o757)
        self.refused(self.run_sandbox(), "writable by group or others")

    def test_group_writable_directory_refused(self):
        self.hook("a", [])
        os.chmod(self.hooks, 0o775)
        self.refused(self.run_sandbox(), "hooks directory")

    def test_hooks_path_that_is_a_file_refused(self):
        shutil.rmtree(self.hooks)
        write(self.hooks, "x", 0o644)
        self.refused(self.run_sandbox(), "hooks directory")

    def test_symlinked_hook_is_checked_at_its_target(self):
        target = self.hook("real", [], directory=self.tmp, mode=0o775)
        os.symlink(target, os.path.join(self.hooks, "a"))
        self.refused(self.run_sandbox(), "writable by group or others")
        os.chmod(target, 0o755)
        self.ok(self.run_sandbox())

    def test_symlink_to_a_directory_refused(self):
        os.symlink(self.tmp, os.path.join(self.hooks, "a"))
        self.refused(self.run_sandbox(), "not a regular file")

    def test_broken_symlink_refused(self):
        os.symlink(self.p("missing"), os.path.join(self.hooks, "a"))
        self.refused(self.run_sandbox(), "cannot resolve")

    def test_non_executable_file_ignored_with_a_note(self):
        seen = self.p("ran")
        self.hook("a", body=f"#!/bin/sh\ntouch {seen}\n", mode=0o644)
        r = self.run_sandbox()
        self.ok(r)
        self.assertIn("hook a ignored: not executable", r.stderr)
        self.assertFalse(os.path.exists(seen))

    def test_odd_file_name_skipped_with_a_note(self):
        seen = self.p("ran")
        self.hook("bad name", body=f"#!/bin/sh\ntouch {seen}\n")
        self.hook("semi;colon", body=f"#!/bin/sh\ntouch {seen}\n")
        r = self.run_sandbox()
        self.ok(r)
        self.assertEqual(r.stderr.count("skipped: a hook name is"), 2, r.stderr)
        self.assertFalse(os.path.exists(seen))


class TestRoRequests(SandboxCase):
    def test_ro_adds_a_read_only_mount(self):
        d = self.tree("mods/gcc")
        self.hook("a", [f"ro {d}"])
        r = self.run_sandbox()
        args, _ = self.ok(r)
        self.assertIn(f"{d}:{d}:ro", args)
        self.assertIn(f"sandbox/run: hook a: ro {d}", r.stderr)

    def test_ro_is_resolved_and_not_added_twice(self):
        d = self.tree("mods/gcc")
        link = self.p("link")
        os.symlink(d, link)
        self.hook("a", [f"ro {d}", f"ro {link}"])
        self.hook("b", [f"ro {d}"])
        args, _ = self.ok(self.run_sandbox(env={"GENAI_SANDBOX_RO": d}))
        self.assertEqual(args.count(f"{d}:{d}:ro"), 1)

    def test_ro_under_an_existing_mount_is_not_repeated(self):
        self.hook("a", ["ro /usr/bin"])
        r = self.run_sandbox()
        args, _ = self.ok(r)
        self.assertNotIn("/usr/bin:/usr/bin:ro", args)
        self.assertIn("already mounted", r.stderr)

    def test_unsafe_ro_is_skipped_with_a_note(self):
        d = self.tree("mods/gcc")
        self.hook("a", ["ro " + self.home, "ro " + os.path.dirname(self.home), "ro /", "ro /usr",
                        "ro /opt", "ro /opt/genai", "ro /tmp", "ro /etc", "ro relative/dir",
                        "ro " + self.p("missing"), "ro " + d])
        r = self.run_sandbox()
        args, _ = self.ok(r)
        self.assertEqual([a for a in args if a.endswith(":ro") and a.startswith(self.tmp)], [f"{d}:{d}:ro"])
        for bad in (self.home, "/", "/usr", "/opt", "/opt/genai", "/tmp", "/etc", "relative/dir",
                    self.p("missing")):
            self.assertIn(f"skipped ro {bad} (", r.stderr)
        self.assertNotIn(f"{self.home}:{self.home}", " ".join(args))
        self.assertNotIn("-v /:/:ro", " ".join(args))
        self.assertIn("would cover the sandbox's own /opt/genai", r.stderr)

    def test_genai_sandbox_ro_still_refuses(self):
        r = self.run_sandbox(env={"GENAI_SANDBOX_RO": "/"})
        self.refused(r, "GENAI_SANDBOX_RO: '/' is or contains your home directory")
        r = self.run_sandbox(env={"GENAI_SANDBOX_RO": "/opt"})
        self.refused(r, "would cover the sandbox's own /opt/genai; name a deeper directory")
        r = self.run_sandbox(env={"GENAI_SANDBOX_RO": self.p("missing")})
        self.refused(r, "is not an absolute path to a directory")

    def test_genai_sandbox_ro_adds_a_mount(self):
        d = self.tree("mods/gcc")
        args, _ = self.ok(self.run_sandbox(env={"GENAI_SANDBOX_RO": d}))
        self.assertIn(f"{d}:{d}:ro", args)


class TestPathRequests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.d = self.tree("mods/gcc/bin", "mods/gcc/lib", "outside/lib")
        os.symlink(self.p("outside", "lib"), self.p("mods", "gcc", "escape"))
        os.symlink(self.p("mods", "gcc", "lib"), self.p("mods", "gcc", "inlink"))
        self.gcc = self.p("mods", "gcc")

    def value(self):
        g = self.gcc
        return ":".join([g + "/bin", "", "relative/dir", ".", self.p("missing"), self.p("outside", "lib"),
                         g + "/escape", g + "/inlink", g + "/lib", g + "/bin", g + "/bin/../lib"])

    def test_filtered_value_reaches_podman_environment_only(self):
        self.hook("a", [f"ro {self.gcc}", "path MYPATH"])
        v = self.value()
        r = self.run_sandbox(env={"MYPATH": v}, log_vars="MYPATH")
        args, env = self.ok(r)
        expected = f"{self.gcc}/bin:{self.gcc}/lib"
        self.assertEqual(env["MYPATH"], expected)
        self.assertIn("-e", args)
        self.assertEqual(args[args.index("MYPATH") - 1], "-e")
        for a in args:
            self.assertNotIn(self.gcc + "/bin", a if a != f"{self.gcc}:{self.gcc}:ro" else "")
            self.assertNotIn("MYPATH=", a)
        self.assertIn("path MYPATH", r.stderr)

    def test_request_order_does_not_matter(self):
        self.hook("a", ["path MYPATH"])
        self.hook("b", [f"ro {self.gcc}"])
        _, env = self.ok(self.run_sandbox(env={"MYPATH": self.value()}, log_vars="MYPATH"))
        self.assertEqual(env["MYPATH"], f"{self.gcc}/bin:{self.gcc}/lib")

    def test_nothing_left_means_not_passed(self):
        self.hook("a", [f"ro {self.gcc}", "path MYPATH"])
        r = self.run_sandbox(env={"MYPATH": f":rel:{self.p('outside', 'lib')}:{self.gcc}/escape"},
                             log_vars="MYPATH")
        args, env = self.ok(r)
        self.assertNotIn("MYPATH", args)
        self.assertIn("skipped path MYPATH", r.stderr)

    def test_unset_variable_passes_nothing(self):
        self.hook("a", ["path MYPATH"])
        r = self.run_sandbox(log_vars="MYPATH")
        args, env = self.ok(r)
        self.assertNotIn("MYPATH", args)
        self.assertEqual(env["MYPATH"], "<unset>")  # unset here too: nothing was invented

    def test_ld_library_path_is_accepted(self):
        self.hook("a", [f"ro {self.gcc}", "path LD_LIBRARY_PATH"])
        args, env = self.ok(self.run_sandbox(env={"LD_LIBRARY_PATH": self.gcc + "/lib:/nonexistent"},
                                             log_vars="LD_LIBRARY_PATH"))
        self.assertEqual(env["LD_LIBRARY_PATH"], self.gcc + "/lib")
        self.assertIn("LD_LIBRARY_PATH", args)

    def test_path_variable_is_filtered_and_podman_still_runs(self):
        self.hook("a", [f"ro {self.gcc}", "path PATH"])
        usr_bin = os.path.realpath("/usr/bin")
        value = f"{self.gcc}/bin:{self.bin}:relative:{usr_bin}"
        r = self.run_sandbox(env={"PATH": value}, log_vars="PATH")
        # the fake podman is found by its absolute path; PATH gets only /usr/bin for its own sh
        args, env = self.ok(r)
        self.assertEqual(env["PATH"], f"{self.gcc}/bin:{usr_bin}")
        self.assertNotIn(self.bin, env["PATH"])

    def test_two_hooks_asking_for_one_variable(self):
        self.hook("a", [f"ro {self.gcc}", "path MYPATH"])
        self.hook("b", ["path MYPATH"])
        args, _ = self.ok(self.run_sandbox(env={"MYPATH": self.gcc + "/bin"}))
        self.assertEqual(args.count("MYPATH"), 1)


class TestRefusedNames(SandboxCase):
    def setUp(self):
        super().setUp()
        self.marker = self.p("pwned")
        self.evil = f"a[$(touch${{IFS}}{self.marker})]"
        self.assertNotIn(" ", self.evil)

    def check(self, line, env=None):
        self.hook("a", [line])
        r = self.run_sandbox(env=dict({"LD_PRELOAD": "/x", "HOME_X": "1"}, **(env or {})))
        self.assertNotEqual(r.returncode, 0, line)
        self.assertEqual(self.podman_runs(), [], line)
        self.assertFalse(os.path.exists(self.marker), line)
        return r

    def test_refused_requests(self):
        for line in ("path LD_PRELOAD", "path LD_AUDIT", "env LD_PRELOAD", "env LD_LIBRARY_PATH",
                     "env PATH", "env HOME", "env PYTHONPATH", "env XDG_CONFIG_HOME", "env GENAI_SANDBOX",
                     "path HOME", "path XDG_CONFIG_HOME", "path GENAI_SANDBOX",
                     "path 1BAD", "env 1BAD", "path A-B", "env A.B", "path A=B", "env a/b",
                     f"path {self.evil}", f"env {self.evil}", "env a[0]", "path a[0]", "env $HOME",
                     "env `id`"):
            with self.subTest(line=line):
                self.check(line)

    def test_names_are_refused_even_when_the_variable_is_unset(self):
        r = self.check("env LD_PRELOAD")
        self.assertIn("LD_PRELOAD", r.stderr)

    def test_refused_request_stops_before_any_mount_or_podman(self):
        d = self.tree("mods/a")
        self.hook("a", [f"ro {d}", "env PATH"])
        self.refused(self.run_sandbox())


class TestEnvRequests(SandboxCase):
    def test_env_passes_the_name_not_the_value(self):
        self.hook("a", ["env EBROOTGCC", "env MKLROOT"])
        r = self.run_sandbox(env={"EBROOTGCC": "/apps/gcc/13", "MKLROOT": "/apps/mkl"},
                             log_vars="EBROOTGCC MKLROOT")
        args, env = self.ok(r)
        for n in ("EBROOTGCC", "MKLROOT"):
            self.assertEqual(args[args.index(n) - 1], "-e")
        self.assertEqual(env["EBROOTGCC"], "/apps/gcc/13")
        self.assertNotIn("/apps/gcc/13", "\n".join(args))
        self.assertNotIn("EBROOTGCC=", "\n".join(args))
        self.assertIn("env EBROOTGCC MKLROOT", r.stderr)

    def test_unset_variable_is_skipped_quietly(self):
        self.hook("a", ["env GENAI_TEST_NEVER_SET"])     # a name no real environment has
        r = self.run_sandbox()
        args, _ = self.ok(r)
        self.assertNotIn("GENAI_TEST_NEVER_SET", args)
        self.assertNotIn("skipped", r.stderr)

    def test_hooks_apply_in_every_mode(self):
        d = self.tree("mods/a")
        self.hook("a", [f"ro {d}", "env FOO_ROOT"])
        ep = self.p("ep.json")
        write(ep, '{"name": "e", "base_url": "https://x.example/v1", "model": "m", "key_env": "K_ENV"}', 0o644)
        for mode, extra, env in (("tests", [], {}), ("selfcheck", [], {}), ("live", [ep], {"K_ENV": "secret"}),
                                 ("genai", [], {"GENAI_SANDBOX_WORK": self.p("work")})):
            with self.subTest(mode=mode):
                if os.path.exists(self.log):
                    os.remove(self.log)
                r = self.run_sandbox(*extra, mode=mode, env=dict(env, FOO_ROOT=d))
                args, _ = self.ok(r)
                self.assertIn(f"{d}:{d}:ro", args)
                self.assertIn("FOO_ROOT", args)
                self.assertNotIn("secret", "\n".join(args))


def make_modules(case):
    """A fake module tree: T/apps/gcc/13/{bin,lib64}, T/apps/openmpi/4/bin, a home bin directory."""
    t = case.p("t")
    for rel in ("apps/gcc/13/bin", "apps/gcc/13/lib64", "apps/gcc/13/lib/pkgconfig",
                "apps/openmpi/4/bin", "apps/openmpi/4/share/man", "apps/tool", "home/mybin", "elsewhere"):
        os.makedirs(os.path.join(t, rel))
    return t


class TestModulesHook(SandboxCase):
    def setUp(self):
        super().setUp()
        self.t = make_modules(self)
        self.home = os.path.join(self.t, "home")
        t = self.t
        self.env = {
            "PATH": ":".join([f"{t}/apps/gcc/13/bin", f"{t}/apps/openmpi/4/bin", "relative/bin", "",
                              f"{t}/home/mybin", f"{t}/apps/gcc/13/bin", "/usr/bin", "/etc"]),
            "LD_LIBRARY_PATH": f"{t}/apps/gcc/13/lib64:{t}/apps/openmpi/4/bin",
            "MANPATH": f"{t}/apps/openmpi/4/share/man:/usr/share/man",
            "PKG_CONFIG_PATH": f"{t}/apps/gcc/13/lib/pkgconfig",
            "EBROOTGCC": f"{t}/apps/gcc/13",
            "OTHER_ROOT": f"{t}/elsewhere",
            "USRVAR": "/usr/share",
            "LISTVAR": f"{t}/apps/gcc/13:{t}/apps/gcc/13/bin",
            "HOME": self.home,
        }

    def modules(self, env=None):
        e = dict(self.env, **(env or {}))
        return subprocess.run([BASH, MODULES, "selfcheck"], env=e, capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL)

    def test_script_is_executable(self):
        self.assertTrue(os.stat(MODULES).st_mode & stat.S_IXUSR)

    def test_requests(self):
        t = self.t
        r = self.modules()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(r.stdout.splitlines(), [
            f"ro {t}/apps/gcc/13",
            f"ro {t}/apps/openmpi/4",
            "path PATH", "path LD_LIBRARY_PATH", "path PKG_CONFIG_PATH", "path MANPATH",
            "env EBROOTGCC",
        ])

    def test_module_roots_cover_their_entries(self):
        t = self.t
        r = self.modules({"GENAI_MODULE_ROOTS": f"{t}/apps:/:{t}/missing:relative"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            f"ro {t}/apps",
            "path PATH", "path LD_LIBRARY_PATH", "path PKG_CONFIG_PATH", "path MANPATH",
            "env EBROOTGCC",
        ])

    def test_root_does_not_hide_entries_outside_it(self):
        t = self.t
        r = self.modules({"GENAI_MODULE_ROOTS": f"{t}/apps/openmpi", "PATH": f"{t}/apps/gcc/13/bin:/usr/bin",
                          "LD_LIBRARY_PATH": "", "MANPATH": "", "PKG_CONFIG_PATH": ""})
        self.assertEqual(r.stdout.splitlines(), [
            f"ro {t}/apps/openmpi", f"ro {t}/apps/gcc/13", "path PATH", "env EBROOTGCC"])

    def test_entry_without_a_known_leaf_is_its_own_prefix(self):
        t = self.t
        r = self.modules({"PATH": f"{t}/apps/tool:/usr/bin", "LD_LIBRARY_PATH": "", "MANPATH": "",
                          "PKG_CONFIG_PATH": ""})
        self.assertEqual(r.stdout.splitlines(), [f"ro {t}/apps/tool", "path PATH"])

    def test_never_asks_for_root_or_a_top_level_directory(self):
        r = self.modules({"PATH": "/tmp:/:/usr/bin", "LD_LIBRARY_PATH": "", "MANPATH": "",
                          "PKG_CONFIG_PATH": "", "EBROOTGCC": "/tmp"})
        self.assertEqual(r.stdout.splitlines(), ["path PATH"])

    def test_nothing_set_prints_nothing(self):
        r = subprocess.run([BASH, MODULES], env={"HOME": self.home},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_refused_names_are_never_printed(self):
        t = self.t
        r = self.modules({"LD_PRELOAD": f"{t}/apps/gcc/13/lib64/x", "PYTHONPATH": f"{t}/apps/gcc/13",
                          "XDG_CONFIG_HOME": f"{t}/apps/gcc/13", "GENAI_SANDBOX": f"{t}/apps/gcc/13",
                          "LD_AUDIT": f"{t}/apps/gcc/13", "bad-name": f"{t}/apps/gcc/13"})
        self.assertEqual(r.stdout.splitlines()[-1], "env EBROOTGCC")
        self.assertNotIn("env LD_", r.stdout)
        self.assertNotIn("PYTHONPATH", r.stdout)
        self.assertNotIn("bad-name", r.stdout)

    def test_end_to_end_through_the_launcher(self):
        t = self.t
        os.symlink(MODULES, os.path.join(self.hooks, "modules.sh"))
        fake_dir = os.path.dirname(self.bin)
        env = dict(self.env)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["MANPATH"] = ""
        r = self.run_sandbox(env=env, log_vars="PATH EBROOTGCC LD_LIBRARY_PATH")
        args, penv = self.ok(r)
        for d in (f"{t}/apps/gcc/13", f"{t}/apps/openmpi/4"):
            self.assertIn(f"{d}:{d}:ro", args)
        self.assertIn(f"-e EBROOTGCC", " ".join(args))
        self.assertEqual(penv["EBROOTGCC"], f"{t}/apps/gcc/13")
        self.assertEqual(penv["LD_LIBRARY_PATH"], f"{t}/apps/gcc/13/lib64:{t}/apps/openmpi/4/bin")
        parts = penv["PATH"].split(":")
        self.assertEqual(parts[:3], [self.bin, f"{t}/apps/gcc/13/bin", f"{t}/apps/openmpi/4/bin"])
        self.assertNotIn("", parts)
        self.assertNotIn("relative/bin", parts)
        self.assertNotIn(f"{t}/home/mybin", parts)
        self.assertIn("sandbox/run: hook modules.sh: ro ", r.stderr)
        self.assertNotIn(f"{t}/apps/gcc/13/bin:", " ".join(args))
        self.assertNotIn(fake_dir + "/zzz", args)


class TestTightening(SandboxCase):
    def test_ro_under_kernel_and_session_trees_is_skipped(self):
        self.hook("a", ["ro /dev/shm", "ro /proc/self", "ro /sys/kernel"])
        r = self.run_sandbox()
        args, _ = self.ok(r)
        for d in ("/dev/shm", "/proc/self", "/sys/kernel"):
            self.assertNotIn(f"{d}:{d}:ro", args)
        self.assertIn("skipped ro /dev/shm (is under /dev", r.stderr)

    def test_genai_sandbox_ro_refuses_them_too(self):
        r = self.run_sandbox(env={"GENAI_SANDBOX_RO": "/dev/shm"})
        self.refused(r, "is under /dev")

    def test_the_launchers_own_variables_are_not_passed(self):
        """`state`, `repo` and `pybin` are variables inside sandbox/run, not part of the environment."""
        self.hook("a", ["env state", "env repo", "path pybin"])
        r = self.run_sandbox(log_vars="state repo pybin")
        args, env = self.ok(r)
        for name in ("state", "repo", "pybin"):
            self.assertNotIn(name, args)
            self.assertEqual(env[name], "<unset>")

    def test_control_characters_in_a_request_are_refused(self):
        self.hook("a", body="#!/bin/sh\nprintf 'env MY\\033[2JVAR\\n'\n")
        r = self.run_sandbox(env={"MYVAR": "x"})
        self.refused(r, "unknown request")
        self.assertNotIn("\x1b", r.stderr)

    def test_entry_that_resolves_to_a_name_with_a_colon_is_dropped(self):
        mods = self.tree("mods/gcc/bin", "mods/a:b")
        os.symlink(self.p("mods", "a:b"), self.p("mods", "gcc", "link"))
        self.hook("a", [f"ro {self.p('mods')}", "path MYPATH"])
        r = self.run_sandbox(env={"MYPATH": f"{self.p('mods', 'gcc', 'link')}:{self.p('mods', 'gcc', 'bin')}"},
                             log_vars="MYPATH")
        _, env = self.ok(r)
        self.assertEqual(env["MYPATH"], self.p("mods", "gcc", "bin"))


if __name__ == "__main__":
    unittest.main()
