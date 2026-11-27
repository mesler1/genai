"""hoplon-tui's remote mode: the parts of it that are Python (the launcher, tui/hoplon-tui, does the ssh steps).

The launcher starts an ssh master connection, a local key proxy (tui/keyproxy.py) and a reverse forward from a
port on the remote host to the proxy, then runs `remote.py write` to record what the conversation pane needs in
STATE/remote.json (mode 0600): how to reach the host, the session token and the config hoplon there is started
with. The API key is in none of it.

The conversation pane (conversation.py) calls bootstrap_command() and runs hoplon over the master connection
without putting hoplon's source in the command line, which is limited to 128 KiB. A short Python program on the
remote host reads the source from stdin first (exactly N bytes), runs it, and leaves the rest of stdin to hoplon:
the config line, then the usual protocol. The remote hoplon is always the same version as the local one, and
nothing is installed or left on that host.

A saved conversation stays on your machine too, in hoplon's sessions directory here, marked with the host and user
it ran as. session_for() reads it into the config line when you resume; hoplon there keeps it only in memory and
sends every save back as a session_save event, which save_session() writes here. A session is only ever sent back
to the host and user it came from: never a local one, nor one from another host.

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

# Read N bytes of source from stdin and run it as __main__ (hoplon finds its own options in sys.argv, and reads the
# rest of stdin as protocol lines: sys.stdin shares its buffer with sys.stdin.buffer, so nothing is swallowed).
BOOTSTRAP = ('import sys;n=int(sys.argv[1]);del sys.argv[1];'
             'exec(compile(sys.stdin.buffer.read(n),"hoplon","exec"),globals())')


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


def bootstrap_command(info, size, hoplon_args):
    """The shell command that runs hoplon on the remote host, as one string for ssh (the remote login shell runs
    it). info is remote.json; size is the length of hoplon's source, which is sent first on stdin."""
    cmd = f"{sudo_prefix(info.get('as'))}{shlex.quote(info.get('python') or 'python3')} -c {shlex.quote(BOOTSTRAP)} " \
          f"{int(size)} --json --config-stdin " + " ".join(shlex.quote(a) for a in hoplon_args)
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
    """Write STATE/remote.json from STATE/export.json (what `hoplon --remote-config` printed)."""
    with open(os.path.join(state, "export.json"), encoding="utf-8") as f:
        export = json.load(f)
    endpoint = dict(export["endpoint"])
    endpoint["base_url"] = f"http://127.0.0.1:{int(port)}{export['base_path']}"
    info = {"dest": dest, "as": as_user, "dir": directory, "port": int(port), "python": python,
            "destination_id": export.get("destination_id"),
            "config": {"type": "config", "endpoint": endpoint, "profile": export["profile"], "token": token,
                       "skills": export.get("skills", []), "command_sets": export.get("command_sets", [])},
            "ssh": ["ssh", "-S", os.path.join(state, "cm"), *ssh_opts, dest]}
    path = os.path.join(state, "remote.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(info, f)
    os.chmod(path, 0o600)


SESSION_KEYS = ("endpoint", "profile", "coding", "cwd", "skills", "created", "updated", "messages", "history")


def where(info):
    """Where a remote conversation runs, as a saved session records it."""
    return {"dest": info.get("dest"), "as": info.get("as") or None, "dir": info.get("dir") or None}


def session_problem(path, dest, as_user):
    """Why the saved session at path can't be resumed on dest (as as_user), or None: also None when there is none yet."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        return f"{path}: can't be read ({e})"
    name = os.path.basename(path)[:-5]
    ran = data.get("remote") if isinstance(data, dict) else None
    if not isinstance(ran, dict):
        return (f"session '{name}' is a conversation from this machine: resuming it on {dest} would send it there. "
                "Pick another name with -S, or leave -S out for a new one")
    if ran.get("dest") != dest or (ran.get("as") or None) != (as_user or None):
        was = f"{ran.get('dest')}" + (f" as {ran.get('as')}" if ran.get("as") else "")
        return (f"session '{name}' ran on {was}: resume it there (hoplon-tui --remote {ran.get('dest')}"
                + (f" --as {ran.get('as')}" if ran.get("as") else "") + f" -S {name}), or pick another name")
    return None


def session_for(path, info, allow_change=False):
    """The config line's session for this conversation, kept at path: ({"name", "data"}, None), data None for a new
    one, or (None, why) when it can't be resumed here."""
    name = os.path.basename(path)[:-5]
    why = session_problem(path, info.get("dest"), info.get("as"))
    if why:
        return None, why
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"name": name, "data": None}, None
    except (OSError, ValueError) as e:
        return None, f"{path}: can't be read ({e})"
    if data.get("messages") and data.get("destination_id") != info.get("destination_id") and not allow_change:
        return None, (f"session '{name}' was kept with another endpoint: its saved context would go to this one. "
                      "To approve that, rerun with --allow-session-endpoint-change")
    return {"name": name, "data": {k: data[k] for k in SESSION_KEYS if k in data}}, None


def save_session(path, info, data):
    """Write what hoplon on the host sent (a session_save event's session) to path, here, privately, marked with where
    it ran and the real endpoint's destination (hoplon there only sees the tunnel). Returns False if it wasn't a
    session."""
    if not isinstance(data, dict):
        return False
    out = {k: data[k] for k in SESSION_KEYS if k in data}
    out.update(id=os.path.basename(path)[:-5], remote=where(info), destination_id=info.get("destination_id"))
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return True


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
