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
import socket
import struct
import threading
import time

SOCKET = "sock"
MAX_LINE = 1 << 20          # a pasted message can be long; a longer line ends the connection
MAX_CLIENTS = 16
SEND_TIMEOUT = 2            # seconds a send may wait on a pane that has stopped reading
ROLES = {
    "prompt": {"input", "key", "scroll", "quit"},
    "panel": {"coding", "skill", "skills"},
    "menu": {"share", "menu"},
}


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


def messages(conn):
    """JSON objects from a connection, until it closes or sends something too long or not a JSON object."""
    buf = b""
    while True:
        try:
            chunk = conn.recv(65536)
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                return
            if not isinstance(msg, dict):
                return
            yield msg
        if len(buf) > MAX_LINE:
            return


def send_timeout(conn):
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", SEND_TIMEOUT, 0))


class Hub:
    """The listening end, in the conversation pane. inbox gets (role, message), and ("hello", role) when a
    pane connects, so the conversation pane can send it the current view at once."""

    def __init__(self, state_dir):
        self.inbox = queue.Queue()
        self.clients = []          # (connection, role)
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
            uid = peer_uid(conn)
            with self.lock:
                full = len(self.clients) >= MAX_CLIENTS
            if (uid is not None and uid != os.getuid()) or full:
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        send_timeout(conn)
        msgs = messages(conn)
        try:
            hello = next(msgs, None)
            if not hello or hello.get("type") != "hello" or hello.get("role") not in ROLES:
                return
            role = hello["role"]
            with self.lock:
                self.clients.append((conn, role))
            self.inbox.put(("hello", role))
            for msg in msgs:
                if msg.get("type") in ROLES[role]:
                    self.inbox.put((role, msg))
        finally:
            with self.lock:
                self.clients = [c for c in self.clients if c[0] is not conn]
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
        self.srv.close()
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
