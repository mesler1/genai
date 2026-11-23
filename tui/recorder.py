"""genai-tui's record of the shell pane: each command, its exit code and exactly what it printed.

tmux copies everything the shell pane prints into a FIFO in genai-tui's private directory (pipe-pane), and
this reads it in the conversation pane, in memory only; nothing is written to disk. The shell's prompt hook
(tui/shellrc) prints invisible markers that tmux shows nothing for: one when a command starts (PS0) and one
when the next prompt comes (with the exit code and the command line). What lies between the two is that
command's output, however the pane is resized, scrolled or cleared afterwards.

Every marker carries a random word the shell picks at startup and keeps in an unexported variable, which
the programs it runs can't read. The first marker announces it, before anything you run has printed, and
markers without it are taken as ordinary output. So a command can't fake the end of another one, or add
a command to the list, by printing markers of its own.

Full-screen programs (vim, top, less) draw on tmux's alternate screen; what they draw there isn't output and
is left out.

genai-ssh (a function in tui/shellrc) follows commands on another host the same way, with a key of their own:

1. The local shell announces the session with a marker carrying its own word, so no program can: X, with a
   random id and the destination you typed. The recorder makes a fresh key for it, here, in memory.
2. ssh starts bash on the remote host with our rc. Before anything else runs there, the rc turns the terminal's
   echo off and asks for the key: R, with the id, and nothing secret. The id came over the command line, which
   other users of that host can see, so it only names the session: an R is accepted once, for the session
   announced, within REMOTE_WAIT seconds.
3. genai-tui types the key into the pane, through ssh's encrypted connection. Echo is off, so the key isn't
   shown or recorded, and it is never on a command line, in the environment or in a file. The remote bash
   keeps it in an unexported variable, as the local one keeps its word.
4. The remote markers carry that key, so a program run there can't fake them, and they bring the remote's
   host name and directory. The key is forgotten when the local shell's next prompt says ssh has ended.

Standard library only.
"""

import base64
import os
import re
import secrets
import threading
import time

ESC_MARK = b"\x1b]6973;"
MAX_KEEP = 200               # commands remembered
HEAD = 256 * 1024            # bytes kept from the start of one command's output
TAIL = 768 * 1024            # and from its end; the middle of a huge output is dropped
ALT_ON = re.compile(rb"\x1b\[\?(?:1049|1047|47)h")
ALT_OFF = re.compile(rb"\x1b\[\?(?:1049|1047|47)l")
REMOTE_WAIT = 20             # seconds from genai-ssh's announcement to the remote shell asking for its key


class Command:
    def __init__(self, n, started):
        self.n, self.started = n, started
        self.ended = None
        self.cmd, self.in_history, self.rc = "", True, None
        self.head, self.tail = bytearray(), bytearray()
        self.size = 0                # bytes printed, including any that were dropped
        self.screen = False          # it used the alternate screen (a full-screen program)
        self.host = None             # the host a genai-ssh session ran it on; None for this one
        self._text = None

    def add(self, data):
        self.size += len(data)
        if len(self.head) < HEAD:
            take = HEAD - len(self.head)
            self.head += data[:take]
            data = data[take:]
        if data:
            self.tail += data
            del self.tail[:-TAIL]

    def text(self):
        """The output as lines of plain text, as it looked when the command ended."""
        if self._text is None or self.ended is None:
            dropped = self.size - len(self.head) - len(self.tail)
            out = render(bytes(self.head))
            if dropped > 0:
                out += f"\n[… {dropped} bytes of output not kept …]\n"
            if self.tail:
                out += render(bytes(self.tail))
            out = out.strip("\n")
            if self.ended is None:
                return out
            self._text = out
        return self._text

    def lines(self):
        t = self.text()
        return t.count("\n") + 1 if t else 0


CSI = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])")
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
OTHER_ESC = re.compile(r"\x1b[P^_X][^\x1b]*\x1b\\|\x1b[()][0-9A-Za-z]|\x1b[ -/]*[0-~]")


def render(data):
    """Terminal output as the plain text it shows: escape sequences removed, a carriage return (a progress
    bar redrawing) overwrites the line, backspace moves back, and an erase-line erases. Alternate-screen
    drawing has already been removed."""
    s = data.decode("utf-8", "replace")
    s = OSC.sub("", s)
    lines, line, col = [], [], 0
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\x1b":
            m = CSI.match(s, i)
            if m:
                if m.group(3) == "K" and m.group(1) in ("", "0"):
                    del line[col:]
                elif m.group(3) == "K" and m.group(1) == "2":
                    line, col = [], 0
                i = m.end()
                continue
            m = OTHER_ESC.match(s, i)
            i = m.end() if m else i + 1
            continue
        if ch == "\n":
            lines.append("".join(line).rstrip())
            line, col = [], 0
        elif ch == "\r":
            col = 0
        elif ch == "\b":
            col = max(col - 1, 0)
        elif ch == "\t" or ch >= " " and ch != "\x7f":
            if col < len(line):
                line[col] = ch
            else:
                line.append(ch)
            col += 1
        i += 1
    if line:
        lines.append("".join(line).rstrip())
    return "\n".join(lines) + ("\n" if s.endswith("\n") else "")


class Remote:
    """A genai-ssh session, from its announcement until the local prompt comes back."""

    def __init__(self, sid, dest):
        self.id, self.dest = sid, dest
        self.until = time.time() + REMOTE_WAIT
        self.key = None              # made when the remote shell asks for it; its markers carry it
        self.host, self.cwd = None, ""   # as the remote shell reports them at each prompt
        self.prompt = False
        self.current, self.alt = None, False

    def name(self):
        return self.host or self.dest


class Recorder:
    """Reads the shell pane's output from a FIFO in a thread. commands is newest last; done counts the
    commands that have finished, ever."""

    def __init__(self, fifo=None):
        self.commands = []
        self.current = None
        self.done = 0
        self.serial = 0
        self.nonce = None
        self.alt = False
        self.ps1_lines = 1
        self.primary_prompt = False
        self.remote = None           # the genai-ssh session: id, key, destination, and once it is up host and cwd
        self.handshake = None        # a key to type now, for the remote shell asking for it
        self.buf = b""
        self.lock = threading.Lock()
        if fifo:
            threading.Thread(target=self._read, args=(fifo,), daemon=True).start()

    def _read(self, fifo):
        try:
            with open(fifo, "rb", buffering=0) as f:    # waits for tmux to open the other end
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        return
                    self.feed(chunk)
        except OSError:
            return

    def feed(self, data):
        with self.lock:
            self.buf += data
            while True:
                i = self.buf.find(ESC_MARK)
                if i < 0:
                    keep = 0                            # a marker may be cut off at the end of this chunk
                    for k in range(1, len(ESC_MARK)):
                        if self.buf.endswith(ESC_MARK[:k]):
                            keep = k
                    self._output(self.buf[:len(self.buf) - keep])
                    self.buf = self.buf[len(self.buf) - keep:]
                    return
                end = self.buf.find(b"\x07", i)
                if end < 0:
                    if len(self.buf) - i > 64 * 1024:  # not a marker of ours, just output
                        self._output(self.buf[:i + 1])
                        self.buf = self.buf[i + 1:]
                        continue
                    self._output(self.buf[:i])
                    self.buf = self.buf[i:]
                    return
                self._output(self.buf[:i])
                body = self.buf[i + len(ESC_MARK):end]
                self.buf = self.buf[end + 1:]
                self._marker(body)                      # one without the word is ignored, and shows nothing

    def _output(self, data):
        r = self.remote
        if r and r.current is not None and data:
            self._into(r, data)
        if not data or self.current is None:
            return
        self._into(self, data)

    @staticmethod
    def _into(holder, data):
        """Add output to the command running in holder (the recorder, or a Remote), leaving out what a full-screen
        program draws."""
        while data:
            if holder.alt:
                m = ALT_OFF.search(data)
                if not m:
                    return
                holder.alt, data = False, data[m.end():]
            else:
                m = ALT_ON.search(data)
                if not m:
                    holder.current.add(data)
                    return
                holder.current.add(data[:m.start()])
                holder.current.screen, holder.alt = True, True
                data = data[m.end():]

    def _marker(self, body):
        parts = body.decode("ascii", "replace").split(";")
        if len(parts) < 2:
            return False
        nonce, kind, args = parts[0], parts[1], parts[2:]
        r = self.remote
        if nonce == "ssh" and kind == "R":
            return self._asked(args)
        if r and r.key and nonce == r.key:
            return self._remote_marker(r, kind, args)
        if self.nonce is None:
            if kind == "H" and re.fullmatch(r"[0-9a-f]{16,64}", nonce):
                self.nonce = nonce
                return True
            return False
        if nonce != self.nonce:
            return False
        if kind == "X":
            return self._announced(args)
        if kind == "P":
            self.primary_prompt = False
        elif kind == "S":
            self.primary_prompt = False
            self.serial += 1
            self.current, self.alt = Command(self.serial, time.time()), False
        elif kind == "D":
            self.primary_prompt = True
            self.remote = self.handshake = None             # the local prompt is back: any ssh session has ended
            c, self.current, self.alt = self.current, None, False
            if len(args) >= 4 and args[3].isdigit():
                self.ps1_lines = max(int(args[3]), 1)
            if c is None:                               # Enter on an empty line, or Ctrl-C at the prompt
                return True
            c.ended = time.time()
            c.rc = int(args[0]) if args and args[0].isdigit() else None
            c.in_history = len(args) >= 2 and args[1] == "1"
            if c.in_history and len(args) >= 3:
                try:
                    c.cmd = base64.b64decode(args[2]).decode("utf-8", "replace").strip()
                except ValueError:
                    c.cmd = ""
            self.commands.append(c)
            del self.commands[:-MAX_KEEP]
            self.done += 1
        return True

    def _announced(self, args):
        """X from the local shell (its word proves it): genai-ssh is about to start a session."""
        if len(args) < 2 or not re.fullmatch(r"[0-9a-f]{32}", args[0]):
            return False
        try:
            dest = printable(base64.b64decode(args[1]).decode("utf-8", "replace"))
        except ValueError:
            return False
        self.remote = Remote(args[0], dest[:200])
        return True

    def _asked(self, args):
        """R: a remote shell asks for its key. Only the session just announced, only once, only in time."""
        r = self.remote
        if not r or r.key or not args or args[0] != r.id or time.time() > r.until:
            return False
        r.key = secrets.token_hex(16)
        self.handshake = r.key
        return True

    def _remote_marker(self, r, kind, args):
        if kind == "P":
            r.prompt = False
        elif kind == "S":
            r.prompt = False
            self.serial += 1
            r.current, r.alt = Command(self.serial, time.time()), False
            r.current.host = r.name()
        elif kind == "D":
            r.prompt = True
            if len(args) >= 6:
                for field, value in (("cwd", args[4]), ("host", args[5])):
                    try:                                # from the other host: shown, so only printable text
                        setattr(r, field, printable(base64.b64decode(value).decode("utf-8", "replace"))[:300]
                                or getattr(r, field))
                    except ValueError:
                        pass
            c, r.current, r.alt = r.current, None, False
            if c is None:
                return True
            c.ended = time.time()
            c.rc = int(args[0]) if args and args[0].isdigit() else None
            c.in_history = len(args) >= 2 and args[1] == "1"
            if c.in_history and len(args) >= 3:
                try:
                    c.cmd = base64.b64decode(args[2]).decode("utf-8", "replace").strip()
                except ValueError:
                    c.cmd = ""
            self.commands.append(c)
            del self.commands[:-MAX_KEEP]
            self.done += 1
        return True

    def take_handshake(self):
        """The key to type into the shell pane now, once, or None."""
        with self.lock:
            key, self.handshake = self.handshake, None
            return key

    def remote_prompt(self):
        """(host, directory) while a genai-ssh session's shell is at its prompt; None otherwise."""
        with self.lock:
            r = self.remote
            return (r.name(), r.cwd) if r and r.key and r.prompt else None

    def remote_host(self):
        """The host of a genai-ssh session that is up, or None."""
        with self.lock:
            r = self.remote
            return r.name() if r and r.key else None

    def finished(self):
        with self.lock:
            return list(self.commands)

    def get(self, n):
        with self.lock:
            return next((c for c in self.commands if c.n == n), None)

    def last(self):
        with self.lock:
            return self.commands[-1] if self.commands else None

    def at_primary_prompt(self):
        with self.lock:
            return self.primary_prompt

    def running(self):
        with self.lock:
            return (self.remote and self.remote.current) or self.current


def printable(text):
    return "".join(ch for ch in text if ch.isprintable()).strip()


def fifo_path(state_dir):
    return os.path.join(state_dir, "shell.out")


def label(c):
    """How a command shows in lists: its command line, or why there is none, and the host if it ran on another."""
    text = c.cmd if c.in_history else "(a command kept out of your history)"
    return f"{c.host}: {text}" if c.host else text


def trim(text, head=60, tail=240):
    """Long output for the model: its start and its end, with how much was left out between them."""
    lines = text.split("\n")
    if len(lines) <= head + tail + 20:
        return text
    return "\n".join(lines[:head] + [f"[… {len(lines) - head - tail} lines left out here …]"] + lines[-tail:])


def ago(t, now=None):
    s = max(int((now or time.time()) - t), 0)
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h"
