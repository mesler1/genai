"""hoplon-ssh, as the conversation pane sees it: answering the other host's handshake, and saying where the shell is.

The recorder (recorder.py), the shell's rc file and Shell.ready() stay in the core: they decide whether a command can
be typed into the shell at all. Without this feature, hoplon-ssh's key is never typed in, so a hoplon-ssh session isn't
followed, and nothing is sent to that host."""

import subprocess

from . import Feature


class Ssh(Feature):
    name = "ssh"

    def __init__(self):
        self.remote_host = None    # the host of a hoplon-ssh session in the shell pane, while it is up

    def poll(self, app):
        """A hoplon-ssh shell on another host asks for its key: type it in now, once (see recorder.py). Its echo is
        off, so the key isn't shown; it goes through ssh's encrypted connection, and nowhere else."""
        self.remote_host = app.rec.remote_host()
        key = app.rec.take_handshake()
        if key and app.shell:
            try:
                app.shell.tmux("send-keys", "-t", app.shell.pane, "-l", key, check=True)
                app.shell.tmux("send-keys", "-t", app.shell.pane, "Enter", check=True)
            except (OSError, subprocess.SubprocessError) as e:
                app.m.note(f"hoplon-ssh: could not answer the other host ({e}); hoplon-tui won't follow that session",
                           self.core.R)
                return
            app.m.note("hoplon-ssh: following the shell on the other host. Commands sent there run on that host, "
                       "and each still asks first.", self.core.W)

    def snapshot(self, m, view):
        if self.remote_host:
            view["session"]["shell_where"] = self.core.remote_where(self.remote_host)


FEATURE = Ssh
