"""genai-tui's private socket: how the prompt pane, the info panel and tmux menus reach the conversation pane.

The socket lives in a directory only the user can open (genai-tui makes it with mktemp -d, mode 0700, in
$XDG_RUNTIME_DIR or /tmp; never on a shared filesystem). The conversation pane listens. Every other pane
connects and first says which role it is; a role may send only its own kinds of message (the panel can
switch a skill, never answer a question), and a connection from another user is closed at once. Messages
are JSON objects, one per line, and a line that is too long or not JSON ends the connection.

A program running as the same user can still connect, as it could drive tmux itself: that boundary is the
user account, and the sandbox is what keeps proposed commands outside it. Standard library only.
"""

import json
import os
import queue
import re
import socket
import struct
import threading
import time

SOCKET = "sock"
MAX_LINE = 1 << 20          # a pasted message can be long; a longer line ends the connection
MAX_CLIENTS = 16
HELLO_TIMEOUT = 2          # seconds to send a complete handshake
SEND_TIMEOUT = 2            # seconds a send may wait on a pane that has stopped reading
ROLES = {
    "prompt": {"input", "key", "scroll", "quit", "stop"},
    "panel": {"coding", "skill", "skills", "command"},
    "menu": {"share", "menu", "mark"},
    "files": set(),          # the files feature adds what this pane may ask for
}
# A plugin's pane connects as "plugin:NAME". It may run its own plugin's read-only commands, put text in the input box
# and offer text to attach after the user reviews it; the plugins feature checks each request (tui/features/plugins.py).
PLUGIN_ROLE = re.compile(r"plugin:[a-z][a-z0-9-]{0,30}")
PLUGIN_KINDS = {"run", "insert", "attach"}
# What no feature may ever be given: answering a question or approving something is the prompt pane's alone.
NEVER = {"hello", "input", "key", "scroll", "quit", "decision", "answer"}


def allow(roles, role, types):
    """Add kinds of message a role may send, in roles (a copy of ROLES) for one Hub: the requests of the features that
    are on. A kind that is not added is refused like any unknown one. The prompt role can't gain any, and nobody
    gains what answers questions or what the core already gives out."""
    if role not in ROLES or role == "prompt":
        raise ValueError(f"no feature may add requests for the {role!r} role")
    bad = set(types) & (NEVER | set().union(*ROLES.values()))
    if bad:
        raise ValueError(f"{sorted(bad)} can't be added: the core owns them")
    roles[role] |= set(types)


def socket_path(state_dir):
    return os.path.join(state_dir, SOCKET)


def encode(msg):
    return (json.dumps(msg, ensure_ascii=False) + "\n").encode()


def peer_uid(conn):
    """The user id of the process at the other end, where the system reports it (Linux); else None."""
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None
    _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, opt, struct.calcsize("3i")))
    return uid


def messages(conn, first_timeout=None):
    """JSON objects from a connection, until it closes or sends something too long or not a JSON object."""
    buf = b""
    deadline = None if first_timeout is None else time.monotonic() + first_timeout
    while True:
        try:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                conn.settimeout(remaining)
            chunk = conn.recv(65536)
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if len(line) > MAX_LINE:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                return
            if not isinstance(msg, dict):
                return
            if deadline is not None:
                conn.settimeout(None)
                deadline = None
            yield msg
        if len(buf) > MAX_LINE:
            return


def send_timeout(conn):
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", SEND_TIMEOUT, 0))


class Hub:
    """The listening end, in the conversation pane. inbox gets (role, message), and ("hello", role) when a
    pane connects, so the conversation pane can send it the current view at once."""

    def __init__(self, state_dir, extra=None):
        self.roles = {role: set(kinds) for role, kinds in ROLES.items()}     # extra: what the features that are on add
        for role, kinds in (extra or {}).items():
            allow(self.roles, role, kinds)
        self.plugins = False       # whether plugin panes may connect (the plugins feature is on)
        self.inbox = queue.Queue()
        self.clients = []          # (connection, role)
        self.connections = set()   # includes sockets still sending a handshake
        self.closed = False
        self.lock = threading.Lock()
        self.path = socket_path(state_dir)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        os.chmod(self.path, 0o600)
        self.srv.listen(MAX_CLIENTS)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            try:
                uid = peer_uid(conn)
            except OSError:
                conn.close()
                continue
            with self.lock:
                accepted = ((uid is None or uid == os.getuid()) and not self.closed
                            and len(self.connections) < MAX_CLIENTS)
                if accepted:
                    self.connections.add(conn)
            if not accepted:
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            send_timeout(conn)
            msgs = messages(conn, first_timeout=HELLO_TIMEOUT)
            hello = next(msgs, None)
            role = hello.get("role") if hello and hello.get("type") == "hello" else None
            if isinstance(role, str) and PLUGIN_ROLE.fullmatch(role) and self.plugins:
                kinds = PLUGIN_KINDS
            elif isinstance(role, str) and role in self.roles:
                kinds = self.roles[role]
            else:
                return
            with self.lock:
                self.clients.append((conn, role))
            self.inbox.put(("hello", role))
            for msg in msgs:
                kind = msg.get("type")
                if isinstance(kind, str) and kind in kinds:
                    self.inbox.put((role, msg))
        except OSError:
            pass
        finally:
            with self.lock:
                self.clients = [c for c in self.clients if c[0] is not conn]
                self.connections.discard(conn)
            conn.close()

    def send(self, role, msg):
        """To every pane connected in that role."""
        data = encode(msg)
        with self.lock:
            targets = [c for c, r in self.clients if r == role]
        for conn in targets:
            try:
                conn.sendall(data)
            except OSError:
                pass

    def close(self):
        with self.lock:
            self.closed = True
            connections = list(self.connections)
        self.srv.close()
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Client:
    """A pane's connection to the conversation pane. Its updates arrive in inbox; None once it has gone."""

    def __init__(self, state_dir, role, wait=5.0):
        self.inbox = queue.Queue()
        deadline = time.time() + wait
        while True:                    # the panes start together; the conversation pane may not be listening yet
            self.conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                self.conn.connect(socket_path(state_dir))
                break
            except OSError:
                self.conn.close()
                if time.time() > deadline:
                    raise
                time.sleep(0.1)
        send_timeout(self.conn)
        self.conn.sendall(encode({"type": "hello", "role": role}))
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for msg in messages(self.conn):
            self.inbox.put(msg)
        self.inbox.put(None)

    def send(self, msg):
        try:
            self.conn.sendall(encode(msg))
            return True
        except OSError:
            return False

    def close(self):
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()
