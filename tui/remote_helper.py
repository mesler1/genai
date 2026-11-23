"""Read-only file helper that genai-tui runs on a remote host.

genai-tui lets the user browse files on a host reached with genai-ssh, and lets the model read remote files after
the user approves each read. Nothing may be installed remotely, so this one file is sent as a base64 argument over
the user's existing ssh connection and run by the host's own python3:

    ssh ... HOST python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv[1]))' <base64 of this file>

It then speaks JSON Lines on stdin/stdout until EOF.  Requests: {"id": <int>, "op": "hello"|"list"|"read"|"tail",
...}.  Every response echoes "id" and is either the result or {"id", "error": "<plain message>"}.

  hello                      -> helper, python, host, user, home, cwd
  list  path                 -> path, entries[name, kind, size, mode, hidden, readable, target?], truncated
  read  path [cap]           -> path, size, text
  tail  path [lines]         -> path, size, text, lines, partial

Safety rules (this runs with the user's own privileges on a machine they may not own):
  * Strictly read-only: it never writes, creates, deletes, changes modes or runs anything.  No subprocess, no
    shell, no network.
  * Symlinks are never followed, at any point in a path.  Paths are walked one component at a time with
    O_NOFOLLOW from "/" (absolute) or from the home directory (relative, "~" and "~/x").  ".." is refused.
    Symlinks are listed (kind "symlink" plus a "target" string) but never opened.
  * Caps: 2000 entries per list; read at most `cap` bytes (default 262144, max 1048576) and refuse bigger files;
    tail returns at most `lines` lines (default 200, max 5000) and 262144 bytes; 65536 bytes per request line
    (longer ends the helper with exit 2); 10000 requests per run.
  * Files whose first 8192 bytes hold a NUL byte are refused as binary.  Text is decoded as UTF-8 with
    errors="replace".
  * Output is ASCII-only JSON, one object per line, and nothing else is printed on stdout.

The receiver must still treat everything sent here as untrusted: file names, file contents and error text come from
the remote host and may be hostile.  Python 3.6 compatible; standard library only.
"""
import errno
import json
import os
import pwd
import socket
import stat
import sys

MAX_ENTRIES = 2000
DEFAULT_CAP, MAX_CAP = 262144, 1048576
DEFAULT_LINES, MAX_LINES = 200, 5000
TAIL_BYTES = 262144
MAX_REQUEST = 65536
MAX_REQUESTS = 10000
BINARY_PROBE = 8192
TARGET_CAP = 300


class Refused(Exception):
    """A request we decline; the message goes back to the client as is."""


def home_dir():
    home = os.environ.get("HOME")
    if not home or not os.path.isabs(home):
        home = pwd.getpwuid(os.geteuid()).pw_dir
    return home


def split_path(path):
    """Return (start directory, components, normalized absolute path)."""
    if "\0" in path:
        raise Refused("bad path")
    relative = not path.startswith("/")
    if path == "~" or path.startswith("~/"):
        path = path[1:]
    parts = []
    for part in path.split("/"):
        if part == "..":
            raise Refused("'..' is not allowed")
        if part not in ("", "."):
            parts.append(part)
    # The home directory itself is trusted (it may legitimately be a symlink); everything below it is not.
    start = os.path.realpath(home_dir()) if relative else "/"
    return start, parts, start.rstrip("/") + "/" + "/".join(parts) if parts else start


def explain(exc):
    """Plain message for an OSError."""
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "permission denied"
    if exc.errno == errno.ENOENT:
        return "no such file or directory"
    if exc.errno == errno.ELOOP:
        return "symlinks are not followed"
    if exc.errno == errno.ENOTDIR:
        return "not a directory"
    return exc.strerror or "i/o error"


def open_at(fd, name, flags):
    """os.open below fd without following a symlink; says so plainly if `name` is one."""
    try:
        return os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            try:
                is_link = stat.S_ISLNK(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
            except OSError:
                is_link = False
            if is_link:
                raise Refused("symlinks are not followed")
        raise


def walk(start, parts):
    """Open each component as a directory, never following symlinks; returns the last fd (caller closes)."""
    fd = os.open(start, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in parts:
            nfd = open_at(fd, name, os.O_RDONLY | os.O_DIRECTORY)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_file(path):
    """Return (fd, normalized path, size) for a regular text file; every component is checked for symlinks."""
    start, parts, norm = split_path(path)
    if not parts:
        raise Refused("not a regular file")
    dfd = walk(start, parts[:-1])
    try:
        fd = open_at(dfd, parts[-1], os.O_RDONLY | os.O_NONBLOCK)
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused("not a regular file")
        if b"\0" in os.pread(fd, BINARY_PROBE, 0):
            raise Refused("binary file")
    except BaseException:
        os.close(fd)
        raise
    return fd, norm, st.st_size


def read_range(fd, offset, count):
    chunks = []
    while count > 0:
        data = os.pread(fd, min(count, 65536), offset)
        if not data:
            break
        chunks.append(data)
        offset += len(data)
        count -= len(data)
    return b"".join(chunks)


def text_of(data):
    return data.decode("utf-8", errors="replace")


def op_hello(req):
    return {"helper": 1, "python": "%d.%d.%d" % sys.version_info[:3], "host": socket.gethostname(),
            "user": pwd.getpwuid(os.geteuid()).pw_name, "home": home_dir(), "cwd": os.getcwd()}


def entry_info(dfd, name):
    entry = {"name": name, "kind": "other", "size": None, "mode": "?????????", "hidden": name.startswith("."),
             "readable": False}
    try:
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except OSError:
        return entry
    entry["mode"] = stat.filemode(st.st_mode)[1:]
    if stat.S_ISREG(st.st_mode):
        entry["kind"], entry["size"] = "file", st.st_size
    elif stat.S_ISDIR(st.st_mode):
        entry["kind"] = "dir"
    elif stat.S_ISLNK(st.st_mode):
        entry["kind"] = "symlink"
        try:
            entry["target"] = os.readlink(name, dir_fd=dfd)[:TARGET_CAP]
        except OSError:
            entry["target"] = ""
    if entry["kind"] in ("file", "dir"):
        try:
            entry["readable"] = os.access(name, os.R_OK, dir_fd=dfd, effective_ids=True, follow_symlinks=False)
        except (OSError, NotImplementedError):
            entry["readable"] = False
    return entry


def path_arg(req, default=None):
    path = req.get("path", default)
    if not isinstance(path, str):
        raise Refused("bad request")
    return path


def int_arg(req, key, default, maximum):
    value = req.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise Refused("bad request")
    return min(value, maximum)


def op_list(req):
    start, parts, norm = split_path(path_arg(req, "~"))
    fd = walk(start, parts)
    try:
        names = sorted(os.listdir(fd))
        entries = [entry_info(fd, name) for name in names[:MAX_ENTRIES]]
    finally:
        os.close(fd)
    return {"path": norm, "entries": entries, "truncated": len(names) > MAX_ENTRIES}


def op_read(req):
    path = path_arg(req)
    cap = int_arg(req, "cap", DEFAULT_CAP, MAX_CAP)
    fd, norm, size = open_file(path)
    try:
        if size > cap:
            raise Refused("file is %d bytes; the limit is %d" % (size, cap))
        data = read_range(fd, 0, cap)
    finally:
        os.close(fd)
    return {"path": norm, "size": size, "text": text_of(data)}


def op_tail(req):
    path = path_arg(req)
    want = int_arg(req, "lines", DEFAULT_LINES, MAX_LINES)
    fd, norm, size = open_file(path)
    try:
        start = max(0, size - TAIL_BYTES)
        data = read_range(fd, start, size - start)
    finally:
        os.close(fd)
    lines = data.split(b"\n")
    partial = start > 0
    if partial and len(lines) > 1:
        lines = lines[1:]  # the first piece began mid-line
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) > want:
        lines = lines[-want:]
        partial = True
    return {"path": norm, "size": size, "text": text_of(b"\n".join(lines)), "lines": len(lines),
            "partial": partial}


OPS = {"hello": op_hello, "list": op_list, "read": op_read, "tail": op_tail}


def handle(line):
    try:
        req = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return {"id": None, "error": "bad request"}
    if not isinstance(req, dict):
        return {"id": None, "error": "bad request"}
    rid = req.get("id")
    if not isinstance(rid, int) or isinstance(rid, bool):
        return {"id": None, "error": "id must be an integer"}
    op = req.get("op")
    func = OPS.get(op) if isinstance(op, str) else None
    if func is None:
        return {"id": rid, "error": "unknown op"}
    try:
        result = func(req)
    except Refused as exc:
        return {"id": rid, "error": str(exc)}
    except OSError as exc:
        return {"id": rid, "error": explain(exc)}
    except Exception:
        return {"id": rid, "error": "internal error"}
    result["id"] = rid
    return result


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main():
    stdin = sys.stdin.buffer
    for _ in range(MAX_REQUESTS):
        line = stdin.readline(MAX_REQUEST + 2)
        if not line:
            return 0
        body = line.rstrip(b"\r\n")
        if len(body) > MAX_REQUEST or (len(line) > MAX_REQUEST and not line.endswith(b"\n")):
            sys.stderr.write("request line too long\n")
            return 2
        if body.strip():
            send(handle(body))
    send({"id": None, "error": "too many requests"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
