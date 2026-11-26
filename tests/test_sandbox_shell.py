"""Tests for "sandbox/run shell": the podman command line it builds (podman is a fake that logs it) and
the rc file's prompt and banner. Run: python3 -m unittest discover -s tests"""

import os
import pty
import subprocess
import unittest

from test_sandbox_hooks import BASH, ROOT, RUN, SandboxCase

RC = os.path.join(ROOT, "sandbox", "shellrc")


class ShellCase(SandboxCase):
    def run_shell(self, *args, env=None, tty=True):
        """Run sandbox/run shell with a pty for stdin and stdout (or /dev/null and pipes when tty is false)."""
        e = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], HOME=self.home,
                 HOPLON_SANDBOX_STATE=self.state, HOPLON_SANDBOX_WORK=self.p("work"), FAKE_LOG=self.log,
                 FAKE_LOG_VARS="")
        for k in ("HOPLON_SANDBOX_HOOKS", "HOPLON_SANDBOX_RO", "HOPLON_KEY_ENV"):
            e.pop(k, None)
        e.update(env or {})
        cmd = ["bash", RUN, "shell", *args]
        if not tty:
            return subprocess.run(cmd, env=e, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        master, slave = pty.openpty()
        try:
            return subprocess.run(cmd, env=e, stdin=slave, stdout=slave, stderr=subprocess.PIPE, text=True,
                                  timeout=60)
        finally:
            os.close(master)
            os.close(slave)


class TestShellMode(ShellCase):
    def test_default_has_no_network_and_a_terminal(self):
        r = self.run_shell(env={"K_ENV": "secret", "HOPLON_KEY_ENV": "K_ENV"})
        args, _ = self.ok(r)
        self.assertIn("--network=none", args)
        self.assertIn("-it", args)
        self.assertIn("network off", r.stderr)
        self.assertIn(f"HOPLON_SANDBOX_WORK_HOST={self.p('work')}", args)
        self.assertIn("HOPLON_SANDBOX_NET=off", args)
        self.assertIn("HOPLON_SANDBOX=podman", args)
        self.assertIn(f"{self.p('work')}:/work", args)
        self.assertIn("--read-only", args)           # the same hardened flags as every mode
        self.assertIn("--cap-drop=ALL", args)
        # the key variable is not passed, by name or value
        self.assertNotIn("K_ENV", args)
        self.assertNotIn("secret", "\n".join(args))

    def test_net_gives_the_network(self):
        r = self.run_shell("--net")
        args, _ = self.ok(r)
        self.assertFalse([a for a in args if a.startswith("--network")])
        self.assertIn("HOPLON_SANDBOX_NET=on", args)
        self.assertIn("network on", r.stderr)

    def test_bash_runs_with_the_rc_file(self):
        args, _ = self.ok(self.run_shell())
        i = args.index("--noprofile")
        self.assertTrue(args[i - 1].endswith("/bash"))
        self.assertEqual(args[i:], ["--noprofile", "--rcfile", "/opt/hoplon/sandbox/shellrc", "-i"])
        self.assertIn(f"{ROOT}:/opt/hoplon:ro", args)
        self.assertTrue(os.path.isfile(RC))

    def test_hooks_run_with_mode_shell(self):
        self.hook("a", body='#!/bin/sh\necho "mode=$1" >&2\necho "env FOO_VAR"\n')
        r = self.run_shell(env={"FOO_VAR": "x"})
        args, _ = self.ok(r)
        self.assertIn("FOO_VAR", args)
        self.assertIn("hook a: env FOO_VAR", r.stderr)
        self.assertIn("mode=shell", r.stderr)

    def test_failing_hook_stops_the_shell(self):
        self.hook("a", body="#!/bin/sh\nexit 3\n")
        self.refused(self.run_shell(), "hook a failed")

    def test_refused_without_a_terminal(self):
        self.refused(self.run_shell(tty=False), "shell needs a terminal")

    def test_refused_without_a_terminal_before_hooks_run(self):
        marker = self.p("hook-ran")
        self.hook("a", body=f"#!/bin/sh\ntouch {marker}\n")
        self.run_shell(tty=False)
        self.assertFalse(os.path.exists(marker))

    def test_unknown_argument_prints_usage(self):
        self.refused(self.run_shell("--bogus"), "usage: sandbox/run shell [--net]")

    def test_usage_text_lists_shell(self):
        r = self.run_sandbox(mode="nonsense")
        self.assertEqual(r.returncode, 2)
        self.assertIn("sandbox/run shell [--net]", r.stderr)
        self.assertEqual(self.podman_runs(), [])

    def test_hoplon_mode_passes_the_host_work_path(self):
        r = self.run_sandbox(mode="hoplon", env={"HOPLON_SANDBOX_WORK": self.p("work")})
        args, _ = self.ok(r)
        self.assertIn(f"HOPLON_SANDBOX_WORK_HOST={self.p('work')}", args)


class TestShellRc(unittest.TestCase):
    """The rc file, sourced by a plain bash outside any container."""

    def source(self, env, tail=""):
        e = {"PATH": os.environ["PATH"], **env}
        return subprocess.run([BASH, "-c", f". {RC}; {tail}"], env=e, capture_output=True, text=True)

    def test_prompt_history_and_banner(self):
        r = self.source({"HOPLON_SANDBOX_WORK_HOST": "/host/w"}, 'printf "%s\\n%s\\n" "$PS1" "$HISTFILE"')
        self.assertIn("[sandbox:podman /work]", r.stdout)
        self.assertIn("\\[\\e[7m\\]", r.stdout)            # escapes sit inside \[ \]
        self.assertIn("/tmp/.bash_history", r.stdout)
        self.assertIn("You are in the podman sandbox: /work is /host/w, writable", r.stdout)
        self.assertIn("network: off (sandbox/run shell --net to allow)", r.stdout)

    def test_banner_says_network_on(self):
        self.assertIn("network: on (--net)", self.source({"HOPLON_SANDBOX_NET": "on"}).stdout)


if __name__ == "__main__":
    unittest.main()
