"""hoplon-share: send output from the shell pane to the conversation, to go with your next message.

    make test 2>&1 | hoplon-share --note "the flaky one"
    hoplon-share --note "before the fix" make test

Piped, it passes what it reads through to your screen and keeps a copy. Given a command, it runs it (output and
errors together), so the exit code goes too, and exits with that code. Nothing starts a turn: each share waits,
fenced as data, until you next send a message, so you can collect several and then ask about them all. -r opens it
in your editor first. hoplon masks the secrets it recognises, as for /share. /shared in the conversation lists what
is waiting and drops one. Standard library only."""

import argparse
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ipc  # noqa: E402

KEEP = 4 << 20              # bytes kept from each end of very long output; the conversation trims it further
SEND = 512 * 1024           # characters sent at most (a socket line is capped at 1 MiB)
ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\r(?!\n)")


class Tail:
    """The first and last KEEP bytes of a stream, and whether anything between them was dropped."""

    def __init__(self):
        self.head, self.tail, self.dropped = bytearray(), bytearray(), 0

    def add(self, b):
        room = KEEP - len(self.head)
        self.head += b[:room]
        self.tail += b[room:] if room > 0 else b
        if len(self.tail) > KEEP:
            self.dropped += len(self.tail) - KEEP
            del self.tail[:len(self.tail) - KEEP]

    def text(self):
        mid = f"\n[… {self.dropped} bytes left out here …]\n".encode() if self.dropped else b""
        return ESCAPES.sub("", bytes(self.head + mid + self.tail).decode("utf-8", "replace"))


def copy(src, keep):
    out = sys.stdout.buffer
    while True:
        b = os.read(src, 65536)
        if not b:
            return
        keep.add(b)
        try:
            out.write(b)
            out.flush()
        except (BrokenPipeError, OSError):
            pass


def review(text):
    """The text in your editor, at the terminal (stdin may be the pipe); None if you emptied it."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    fd, path = tempfile.mkstemp(prefix="hoplon-share-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        with open("/dev/tty", "rb") as tin, open("/dev/tty", "wb") as tout:
            subprocess.run(["sh", "-c", f'{editor} "$1"', "editor", path], stdin=tin, stdout=tout, stderr=tout)
        with open(path, encoding="utf-8", errors="replace") as f:
            kept = f.read()
        return kept if kept.strip() else None
    finally:
        os.unlink(path)


def main(argv):
    ap = argparse.ArgumentParser(prog="hoplon-share", description=__doc__.split("\n\n")[0])
    ap.add_argument("--note", default="", help="what this is, for the model (and you, in /shared)")
    ap.add_argument("-r", "--review", action="store_true", help="open it in your editor before it is sent")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="a command to run; without one, it reads its input")
    args = ap.parse_args(argv)
    state = os.environ.get("HOPLON_TUI_STATE")
    if not state or not os.path.exists(ipc.socket_path(state)):
        print("hoplon-share: works only in hoplon-tui's shell pane", file=sys.stderr)
        return 2
    keep, rc = Tail(), None
    if args.command:
        try:
            p = subprocess.Popen(args.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as e:
            print(f"hoplon-share: can't run {args.command[0]}: {e.strerror}", file=sys.stderr)
            return 127
        try:
            copy(p.stdout.fileno(), keep)
        except KeyboardInterrupt:
            p.terminate()
        rc = p.wait()
    elif sys.stdin.isatty():
        ap.error("pipe output into it (cmd 2>&1 | hoplon-share), or give it a command to run")
    else:
        try:
            copy(sys.stdin.fileno(), keep)
        except KeyboardInterrupt:
            pass
    text = keep.text()
    if args.review:
        text = review(text)
        if text is None:
            print("hoplon-share: nothing sent", file=sys.stderr)
            return rc or 0
    if len(text) > SEND:
        text = text[:SEND // 4] + f"\n[… {len(text) - SEND} characters left out here …]\n" + text[-(SEND * 3 // 4):]
    try:
        c = ipc.Client(state, "pipe", wait=2)
    except OSError as e:
        print(f"hoplon-share: the conversation isn't answering ({e.strerror or e}); nothing sent", file=sys.stderr)
        return 2 if rc is None else rc
    ok = c.send({"type": "piped", "note": args.note, "command": " ".join(args.command) if args.command else None,
                 "rc": rc, "text": text, "cwd": os.getcwd()})
    c.close()
    lines = text.count("\n") + (not text.endswith("\n") and bool(text))
    print(f"hoplon-share: {lines} line{'s' if lines != 1 else ''} queued for your next message" if ok else
          "hoplon-share: the conversation went away; nothing sent", file=sys.stderr)
    return rc if rc is not None else (0 if ok else 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
