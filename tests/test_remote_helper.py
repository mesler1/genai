"""Tests for tui/remote_helper.py, the read-only helper genai-tui runs on remote hosts."""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(ROOT, "tui", "remote_helper.py")


def helper_bytes():
    with open(HELPER, "rb") as f:
        return f.read()


def run_helper(requests, home, raw=False, argv=None, timeout=30):
    """Feed requests (dicts, or raw bytes lines when raw) to the helper; return (responses, returncode, stderr)."""
    if raw:
        data = b"".join(requests)
    else:
        data = b"".join(json.dumps(r).encode() + b"\n" for r in requests)
    env = dict(os.environ, HOME=home)
    proc = subprocess.run(argv or [sys.executable, HELPER], input=data, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, timeout=timeout)
    lines = proc.stdout.split(b"\n")
    assert lines[-1] == b"", "output must end with a newline"
    for line in lines[:-1]:
        line.decode("ascii")  # raises if any non-ASCII byte travels
    return [json.loads(line) for line in lines[:-1]], proc.returncode, proc.stderr


class HelperCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="rh-"))
        self.home = os.path.join(self.tmp, "home")
        os.mkdir(self.home)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def ask(self, *reqs):
        out, rc, err = run_helper([dict(r, id=i) for i, r in enumerate(reqs)], self.home)
        self.assertEqual(rc, 0, err)
        self.assertEqual(err, b"")
        self.assertEqual([o["id"] for o in out], list(range(len(reqs))))
        return out

    def one(self, **req):
        return self.ask(req)[0]

    def write(self, rel, data=b"x", mode=None):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        if mode is not None:
            os.chmod(path, mode)
        return path


class TestHello(HelperCase):
    def test_hello(self):
        r = self.one(op="hello")
        self.assertEqual(r["helper"], 1)
        self.assertEqual(r["home"], self.home)
        self.assertEqual(r["python"].split(".")[0], "3")
        for key in ("host", "user", "cwd"):
            self.assertIsInstance(r[key], str)


class TestList(HelperCase):
    def test_list_kinds_sorted(self):
        self.write("d/b.txt", b"hello", 0o640)
        self.write("d/.hid", b"")
        self.write("d/a.sh", b"1234567", 0o755)
        os.mkdir(os.path.join(self.tmp, "d", "sub"))
        os.symlink("/etc/passwd", os.path.join(self.tmp, "d", "ln"))
        os.mkfifo(os.path.join(self.tmp, "d", "fifo"))
        r = self.one(op="list", path=os.path.join(self.tmp, "d"))
        self.assertEqual(r["path"], os.path.join(self.tmp, "d"))
        self.assertFalse(r["truncated"])
        names = [e["name"] for e in r["entries"]]
        self.assertEqual(names, sorted(names))
        by = {e["name"]: e for e in r["entries"]}
        self.assertEqual(by["b.txt"]["kind"], "file")
        self.assertEqual(by["b.txt"]["size"], 5)
        self.assertEqual(by["b.txt"]["mode"], "rw-r-----")
        self.assertEqual(by["a.sh"]["mode"], "rwxr-xr-x")
        self.assertTrue(by[".hid"]["hidden"])
        self.assertFalse(by["b.txt"]["hidden"])
        self.assertTrue(by["b.txt"]["readable"])
        self.assertEqual(by["sub"]["kind"], "dir")
        self.assertEqual(by["ln"]["kind"], "symlink")
        self.assertEqual(by["ln"]["target"], "/etc/passwd")
        self.assertEqual(by["fifo"]["kind"], "other")
        self.assertIsNone(by["fifo"]["size"])

    def test_truncated(self):
        d = os.path.join(self.tmp, "many")
        os.mkdir(d)
        for i in range(2005):
            open(os.path.join(d, "f%04d" % i), "w").close()
        r = self.one(op="list", path=d)
        self.assertEqual(len(r["entries"]), 2000)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["entries"][-1]["name"], "f1999")

    def test_long_target_capped(self):
        os.symlink("t" * 1000, os.path.join(self.home, "ln"))
        r = self.one(op="list", path="~")
        self.assertEqual(len(r["entries"][0]["target"]), 300)

    def test_relative_and_tilde(self):
        self.write("home/sub/f.txt", b"hi\n")
        for p in ("~", "", ".", "~/", None):
            req = {"op": "list"} if p is None else {"op": "list", "path": p}
            r = self.one(**req)
            self.assertEqual(r["path"], self.home)
            self.assertEqual([e["name"] for e in r["entries"]], ["sub"])
        for p in ("sub", "~/sub", "./sub//", "~/./sub"):
            r = self.one(op="list", path=p)
            self.assertEqual(r["path"], os.path.join(self.home, "sub"))
            self.assertEqual([e["name"] for e in r["entries"]], ["f.txt"])

    def test_errors(self):
        self.write("f.txt")
        self.assertEqual(self.one(op="list", path=self.tmp + "/nope")["error"], "no such file or directory")
        self.assertEqual(self.one(op="list", path=self.tmp + "/f.txt")["error"], "not a directory")
        self.assertEqual(self.one(op="list", path=self.tmp + "/../x")["error"], "'..' is not allowed")
        os.symlink(self.tmp, os.path.join(self.tmp, "ln"))
        self.assertEqual(self.one(op="list", path=self.tmp + "/ln")["error"], "symlinks are not followed")
        self.assertEqual(self.one(op="list", path=5)["error"], "bad request")

    def test_names_are_escaped(self):
        name = "a\x1b[31mé‮\n.txt"
        self.write("home/" + name, b"x")
        r = self.one(op="list", path="~")
        self.assertEqual(r["entries"][0]["name"], name)  # round-trips, yet run_helper proved the wire is ASCII


class TestRead(HelperCase):
    def test_read_text(self):
        self.write("home/a.txt", "café\nline2\n".encode())
        r = self.one(op="read", path="~/a.txt")
        self.assertEqual(r["text"], "café\nline2\n")
        self.assertEqual(r["size"], 12)
        self.assertEqual(r["path"], os.path.join(self.home, "a.txt"))
        self.assertEqual(self.one(op="read", path="a.txt")["text"], r["text"])

    def test_invalid_utf8_replaced(self):
        p = self.write("bad.txt", b"ok \xff\xfe end")
        self.assertEqual(self.one(op="read", path=p)["text"], "ok �� end")

    def test_cap(self):
        p = self.write("big.txt", b"a" * 300000)
        r = self.one(op="read", path=p)
        self.assertEqual(r["error"], "file is 300000 bytes; the limit is 262144")
        self.assertEqual(len(self.one(op="read", path=p, cap=400000)["text"]), 300000)
        self.assertIn("the limit is 1048576", self.one(op="read", path=self.write("huge", b"a" * 1048577),
                                                       cap=10 ** 9)["error"])
        self.assertEqual(self.one(op="read", path=p, cap=0)["error"], "bad request")
        self.assertEqual(self.one(op="read", path=p, cap="9")["error"], "bad request")

    def test_binary(self):
        p = self.write("bin", b"abc\0def")
        self.assertEqual(self.one(op="read", path=p)["error"], "binary file")
        self.assertEqual(self.one(op="tail", path=p)["error"], "binary file")

    def test_directory_and_fifo(self):
        self.assertEqual(self.one(op="read", path=self.tmp)["error"], "not a regular file")
        self.assertEqual(self.one(op="read", path="/")["error"], "not a regular file")
        fifo = os.path.join(self.tmp, "fifo")
        os.mkfifo(fifo)
        self.assertEqual(self.one(op="read", path=fifo)["error"], "not a regular file")

    def test_symlink_final(self):
        p = self.write("real.txt", b"secret")
        os.symlink(p, os.path.join(self.tmp, "ln.txt"))
        self.assertEqual(self.one(op="read", path=self.tmp + "/ln.txt")["error"], "symlinks are not followed")
        self.assertEqual(self.one(op="tail", path=self.tmp + "/ln.txt")["error"], "symlinks are not followed")

    def test_symlink_middle(self):
        self.write("realdir/f.txt", b"secret")
        os.symlink(os.path.join(self.tmp, "realdir"), os.path.join(self.tmp, "lndir"))
        self.assertEqual(self.one(op="read", path=self.tmp + "/lndir/f.txt")["error"],
                         "symlinks are not followed")
        os.symlink(os.path.join(self.tmp, "realdir"), os.path.join(self.home, "hl"))
        self.assertEqual(self.one(op="read", path="~/hl/f.txt")["error"], "symlinks are not followed")

    def test_dotdot(self):
        self.write("f.txt")
        for p in (self.tmp + "/home/../f.txt", "../f.txt", "~/../f.txt", "a/../b"):
            self.assertEqual(self.one(op="read", path=p)["error"], "'..' is not allowed")

    def test_missing(self):
        self.assertEqual(self.one(op="read", path=self.tmp + "/nope")["error"], "no such file or directory")
        self.assertEqual(self.one(op="read", path=self.tmp + "/nope/x")["error"], "no such file or directory")

    @unittest.skipIf(os.geteuid() == 0, "root can read anything")
    def test_permission_denied(self):
        p = self.write("secret.txt", b"x", 0o000)
        self.assertEqual(self.one(op="read", path=p)["error"], "permission denied")
        r = self.one(op="list", path=self.tmp)
        self.assertFalse([e for e in r["entries"] if e["name"] == "secret.txt"][0]["readable"])


class TestTail(HelperCase):
    def test_last_lines(self):
        p = self.write("log", "".join("line %d\n" % i for i in range(1000)).encode())
        r = self.one(op="tail", path=p, lines=3)
        self.assertEqual(r["text"], "line 997\nline 998\nline 999")
        self.assertEqual((r["lines"], r["partial"]), (3, True))
        r = self.one(op="tail", path=p)
        self.assertEqual((r["lines"], r["partial"]), (200, True))

    def test_whole_file(self):
        p = self.write("short", b"a\nb\nc")
        r = self.one(op="tail", path=p, lines=10)
        self.assertEqual((r["text"], r["lines"], r["partial"]), ("a\nb\nc", 3, False))
        self.assertEqual(self.one(op="tail", path=self.write("empty", b""))["lines"], 0)

    def test_big_file_only_the_end(self):
        p = os.path.join(self.tmp, "big.log")
        with open(p, "wb") as f:
            for _ in range(5):
                f.write(b"x" * 1048575 + b"\n")
            f.write(b"the end\n")
        start = time.time()
        r = self.one(op="tail", path=p, lines=5000)
        self.assertLess(time.time() - start, 5)
        self.assertEqual(r["size"], 5 * 1048576 + 8)
        self.assertTrue(r["partial"])
        self.assertLessEqual(len(r["text"]), 262144)
        self.assertEqual((r["text"], r["lines"]), ("the end", 1))

    def test_bad_lines(self):
        p = self.write("f", b"x")
        self.assertEqual(self.one(op="tail", path=p, lines=-1)["error"], "bad request")
        self.assertEqual(self.one(op="tail", path=p, lines=True)["error"], "bad request")


class TestProtocol(HelperCase):
    def raw(self, *lines):
        out, rc, err = run_helper(list(lines), self.home, raw=True)
        return out, rc, err

    def test_bad_input_continues(self):
        out, rc, err = self.raw(b"not json\n", b"[1]\n", b'{"id": 1, "op": "bogus"}\n', b'{"id": "1", "op": "hello"}\n',
                                b'{"id": true, "op": "hello"}\n', b"\xff\xfe\n", b"\n", b'{"id": 2, "op": "hello"}\n',
                                b'{"id": 3, "op": 7}\n', b'{"id": 4}\n')
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], {"id": None, "error": "bad request"})
        self.assertEqual(out[1], {"id": None, "error": "bad request"})
        self.assertEqual(out[2], {"id": 1, "error": "unknown op"})
        self.assertIsNone(out[3]["id"])
        self.assertIn("error", out[3])
        self.assertIsNone(out[4]["id"])
        self.assertEqual(out[5], {"id": None, "error": "bad request"})
        self.assertEqual(out[6]["id"], 2)
        self.assertEqual(out[7], {"id": 3, "error": "unknown op"})
        self.assertEqual(out[8], {"id": 4, "error": "unknown op"})
        self.assertEqual(len(out), 9)

    def test_oversized_line_exits(self):
        out, rc, err = self.raw(b'{"id": 1, "op": "hello"}\n', b'{"id": 2, "op": "' + b"a" * 70000 + b'"}\n',
                                b'{"id": 3, "op": "hello"}\n')
        self.assertEqual(rc, 2)
        self.assertEqual([o["id"] for o in out], [1])

    def test_line_at_limit_ok(self):
        pad = b"a" * (65536 - len(b'{"id": 1, "op": "hello", "x": ""}'))
        out, rc, err = self.raw(b'{"id": 1, "op": "hello", "x": "' + pad + b'"}\n')
        self.assertEqual((rc, out[0]["helper"]), (0, 1))

    def test_request_limit(self):
        out, rc, err = self.raw(*[b'{"id": 1, "op": "hello"}\n'] * 10001)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), 10001)
        self.assertEqual(out[-1]["error"], "too many requests")

    def test_exec_launch(self):
        code = base64.b64encode(helper_bytes()).decode()
        argv = [sys.executable, "-c", "import base64,sys;exec(base64.b64decode(sys.argv[1]))", code]
        out, rc, err = run_helper([{"id": 1, "op": "hello"}], self.home, argv=argv)
        self.assertEqual((rc, err, out[0]["id"], out[0]["helper"]), (0, b"", 1, 1))

    def test_python36_syntax(self):
        code = "import ast,sys\nwith open(sys.argv[1]) as f: ast.parse(f.read(), feature_version=(3, 6))"
        subprocess.check_call([sys.executable, "-W", "error", "-c", code, HELPER])

    def test_no_forbidden_modules(self):
        src = helper_bytes().decode()
        for word in ("subprocess", "os.system", "os.exec", "os.remove", "os.unlink", "os.write(", "socket.socket"):
            self.assertNotIn(word, src.replace('"""', "").split("import errno")[1], word)


def find_container_python():
    """(image, python command) for a local image whose own python3 can run the helper, else None."""
    if not shutil.which("podman"):
        return None
    for image, pys in (("registry.suse.com/bci/bci-base:15.6", ["python3"]),
                       ("docker.io/rockylinux/rockylinux:9", ["python3", "/usr/libexec/platform-python"])):
        if subprocess.call(["podman", "image", "exists", image], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) != 0:
            continue
        for py in pys:
            probe = subprocess.run(["podman", "run", "--rm", image, py, "-c", "print(1)"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if probe.returncode == 0:
                return image, py
    return None


class TestContainer(unittest.TestCase):
    def test_container_python(self):
        found = find_container_python()
        if not found:
            self.skipTest("no local container image with a python3")
        image, py = found
        code = base64.b64encode(helper_bytes()).decode()
        reqs = b"".join(json.dumps(r).encode() + b"\n" for r in (
            {"id": 1, "op": "hello"}, {"id": 2, "op": "list", "path": "/etc"},
            {"id": 3, "op": "read", "path": "/etc/hostname"}))
        proc = subprocess.run(["podman", "run", "--rm", "-i", image, py, "-c",
                               "import base64,sys;exec(base64.b64decode(sys.argv[1]))", code],
                              input=reqs, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(out[0]["helper"], 1)
        print("container python %s %s: %s" % (image, py, out[0]["python"]), file=sys.stderr)
        self.assertTrue(out[1]["entries"])
        self.assertIn("text", out[2])


if __name__ == "__main__":
    unittest.main()
