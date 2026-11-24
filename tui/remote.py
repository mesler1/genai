"""genai-tui's remote mode: the parts of it that are Python (the launcher, tui/genai-tui, does the ssh steps).

The launcher starts an ssh master connection, a local key proxy (tui/keyproxy.py) and a reverse forward from a
port on the remote host to the proxy, then runs `remote.py write` to record what the conversation pane needs in
STATE/remote.json (mode 0600): how to reach the host, the session token and the config genai there is started
with. The API key is in none of it.

The conversation pane (conversation.py) calls bootstrap_command() and runs genai over the master connection
without putting genai's source in the command line, which is limited to 128 KiB. A short Python program on the
remote host reads the source from stdin first (exactly N bytes), runs it, and leaves the rest of stdin to genai:
the config line, then the usual protocol. The remote genai is always the same version as the local one, and
nothing is installed or left on that host.

    remote.py write STATE DEST AS DIR PORT PYTHON -- SSH-OPTION...    token on stdin; STATE/export.json read

Standard library only.
"""

import json
import os
import re
import shlex
import sys

USER_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.@-]*")
PYTHON_RE = re.compile(r"python3(?:\.[0-9]+)?")

# Read N bytes of source from stdin and run it as __main__ (genai finds its own options in sys.argv, and reads the
# rest of stdin as protocol lines: sys.stdin shares its buffer with sys.stdin.buffer, so nothing is swallowed).
BOOTSTRAP = ('import sys;n=int(sys.argv[1]);del sys.argv[1];'
             'exec(compile(sys.stdin.buffer.read(n),"genai","exec"),globals())')


def check_destination(dest, as_user, directory):
    """The reason these values can't be used, or None. They end up in a shell command on the remote host."""
    if not dest or dest.startswith("-") or re.search(r"[\s\x00-\x1f\x7f]", dest):
        return "--remote needs a host, such as host or user@host (no spaces, not starting with '-')"
    if as_user is not None and not USER_RE.fullmatch(as_user):
        return f"--as {as_user!r} is not a user name"
    if directory is not None and (not directory or re.search(r"[\x00-\x1f\x7f]", directory)):
        return "--dir needs a path without control characters"
    return None


def sudo_prefix(as_user):
    return f"sudo -n -H -u {shlex.quote(as_user)} -- " if as_user else ""


def bootstrap_command(info, size, genai_args):
    """The shell command that runs genai on the remote host, as one string for ssh (the remote login shell runs
    it). info is remote.json; size is the length of genai's source, which is sent first on stdin."""
    cmd = f"{sudo_prefix(info.get('as'))}{shlex.quote(info.get('python') or 'python3')} -c {shlex.quote(BOOTSTRAP)} " \
          f"{int(size)} --json --config-stdin " + " ".join(shlex.quote(a) for a in genai_args)
    if info.get("dir"):
        cmd = f"cd {shlex.quote(info['dir'])} && {cmd}"
    return cmd.rstrip()


def load(path):
    with open(path, encoding="utf-8") as f:
        info = json.load(f)
    if not isinstance(info.get("ssh"), list) or not isinstance(info.get("config"), dict):
        raise ValueError(f"{path}: not a remote.json")
    return info


def write(state, dest, as_user, directory, port, python, token, ssh_opts):
    """Write STATE/remote.json from STATE/export.json (what `genai --remote-config` printed)."""
    with open(os.path.join(state, "export.json"), encoding="utf-8") as f:
        export = json.load(f)
    endpoint = dict(export["endpoint"])
    endpoint["base_url"] = f"http://127.0.0.1:{int(port)}{export['base_path']}"
    info = {"dest": dest, "as": as_user, "dir": directory, "port": int(port), "python": python,
            "config": {"type": "config", "endpoint": endpoint, "profile": export["profile"], "token": token,
                       "skills": export.get("skills", []), "plugins": export.get("plugins", [])},
            "ssh": ["ssh", "-S", os.path.join(state, "cm"), *ssh_opts, dest]}
    path = os.path.join(state, "remote.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(info, f)
    os.chmod(path, 0o600)


def main(argv):
    if len(argv) < 8 or argv[0] != "write" or argv[7] != "--":
        print("usage: remote.py write STATE DEST AS DIR PORT PYTHON -- SSH-OPTION...  (token on stdin)",
              file=sys.stderr)
        return 2
    state, dest, as_user, directory, port, python = argv[1:7]
    token = sys.stdin.readline().strip()
    if not re.fullmatch(r"[0-9a-f]{32,128}", token) or not port.isdigit() or not PYTHON_RE.fullmatch(python):
        print("remote.py: bad token, port or python name", file=sys.stderr)
        return 2
    write(state, dest, as_user or None, directory or None, port, python, token, argv[8:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
