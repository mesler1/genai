"""How hoplon is cut into parts (hoplon_core/), held by tests so the cut stays as AUDIT.md describes it:
each part builds only on earlier ones and its header says which; only the handlers touch the system, and every
other place that does is listed here with what it may do and why; the gate touches nothing; each part keeps to
a line budget. Run: python3 -m unittest discover -s tests"""

import ast
import builtins
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_hoplon import G  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(G.__file__)), "hoplon_core")

# The handlers: the parts that carry out the four requests (GATE_REQUESTS). Touching the system is their job.
HANDLERS = {"files", "client", "commands", "sandbox"}

# Everywhere else that touches the system: the function, exactly what it may call, and why. The gate is not here,
# and must never be: it decides, and the handlers perform.
ELSEWHERE = {
    ("base", "ensure_dir"): ({"os.makedirs"}, "hoplon's own directories"),
    ("base", "open_state_file"): ({"os.open"}, "hoplon's own state files, private to the user"),
    ("base", "read_regular"): ({"os.open"}, "reads a config item, refusing anything but a regular file"),
    ("base", "write_private"): ({"os.chmod", "os.replace", "os.unlink"}, "writes hoplon's own files, private"),
    ("config", "Config.ensure"): ({"os.mkdir"}, "makes the config directory"),
    ("config", "Notifier.fire"): ({"subprocess.Popen"}, "the user's own notify hook, a program they blessed"),
    ("config", "Notifier._reap"): ({"os.killpg"}, "ends a notify hook that outlived its time"),
    ("config", "new_endpoint"): ({"os.makedirs", "os.open"}, "writes the endpoint the user is making"),
    ("leftovers", "hoplon_alive"): ({"open", "os.kill"}, "is the hoplon that made it still running (signal 0)"),
    ("leftovers", "leftover_containers"): ({"shutil.which", "subprocess.run"}, "lists hoplon's podman containers"),
    ("leftovers", "leftover_copies"): ({"open"}, "reads a copy's record"),
    ("leftovers", "leftover_fixtures"): ({"open"}, "reads a fixtures record"),
    ("leftovers", "leftovers"): ({"open"}, "reads the list of projects"),
    ("leftovers", "remove_leftover"): ({"os.rmdir", "os.unlink", "shutil.rmtree", "subprocess.run"},
                                       "removes what hoplon left, when the user asks"),
    ("leftovers", "socket_live"): ({"socket.socket"}, "is a hoplon-tui socket still answered"),
    ("turn", "Engine.tools"): ({"socket.gethostname"}, "names this machine to the model"),
    ("turn", "Engine.where"): ({"socket.gethostname"}, "names this machine to the model"),
    ("front", "Repl.banner"): ({"socket.gethostname"}, "names this machine to the user"),
    ("front", "run_json"): ({"socket.gethostname"}, "names this machine in hello"),
    ("front", "Repl.do_sandbox"): ({"socket.getaddrinfo"}, "checks the endpoint's name resolves, to warn"),
    ("front", "Repl.do_sh"): ({"open", "os.chmod", "os.unlink", "shutil.which", "subprocess.call"},
                              "the user's own subshell (:sh): nothing the model wrote runs in it"),
    ("front", "edit_text"): ({"open", "os.unlink", "subprocess.call"}, "the user's editor, on a private temp file"),
    ("front", "page"): ({"os.unlink", "shutil.which", "subprocess.run"}, "the user's pager"),
    ("front", "read_attachment"): ({"open"}, "a file the user attached"),
}

# What touches the system: making processes, sockets and requests, and changing or opening files. Constants and
# exception classes (subprocess.PIPE, TimeoutExpired) do nothing by themselves and are left out.
TOUCHES = re.compile(r"^(subprocess|socket|urllib\.request|http\.client|shutil|ctypes)\.\w+$"
                     r"|^os\.(open|system|exec\w*|spawn\w*|popen|fork\w*|kill|killpg|remove|unlink|rename|replace"
                     r"|rmdir|removedirs|chmod|chown|symlink|link|mkdir|makedirs|truncate|set\w*id|write|mkfifo)$")

# A line budget per part, so the drift the split undid doesn't come back. Raise one on purpose, not by habit.
BUDGET = {"base": 700, "files": 350, "client": 450, "commands": 750, "sandbox": 1000, "config": 1000,
          "leftovers": 500, "gate": 1300, "turn": 1200, "front": 1800}


def read(name):
    with open(os.path.join(ROOT, f"{name}.py"), encoding="utf-8") as f:
        return f.read()


def tree(name):
    return ast.parse(read(name), f"hoplon_core/{name}.py")


def defined(t):
    """The names a part defines at its top level."""
    out = set()
    for n in t.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for tg in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                out |= {x.id for x in ast.walk(tg) if isinstance(x, ast.Name)}
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in n.names}
    return out


def bound(scope):
    """The names a function, class body, lambda or comprehension binds for itself (nested ones' included: over-counting only
    hides a use of an outer name of the same spelling, which the part's own definitions then still catch)."""
    names = set()
    for n in ast.walk(scope):
        if isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not scope:
            names.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in n.names}
    return names


SCOPES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)     # each binds its own loop names


def free(t):
    """(name, at load time) for each name a part reads that none of its own functions bind: those come from the
    shared namespace. At load time means it is read while the part runs, not later when a function is called."""
    out = set()

    def visit(node, local, in_function):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                # Decorators and defaults run when the function is made; the body only when it is called.
                now = getattr(c, "decorator_list", []) + c.args.defaults + [d for d in c.args.kw_defaults if d]
                for n in now:
                    visit(ast.Expression(n), local, in_function)
                inner = local | bound(c)
                for n in (c.body if isinstance(c.body, list) else [c.body]):
                    visit(ast.Module([n], []) if isinstance(n, ast.stmt) else ast.Expression(n), inner, True)
                continue
            if isinstance(c, ast.ClassDef):
                # A class body runs when the class is made, so it is load time if the class is made then.
                for n in c.decorator_list + c.bases + [k.value for k in c.keywords]:
                    visit(ast.Expression(n), local, in_function)
                for n in c.body:
                    visit(ast.Module([n], []), local | bound(c), in_function)
                continue
            if isinstance(c, SCOPES):
                visit(c, local | bound(c), in_function)
                continue
            if isinstance(c, ast.Name) and isinstance(c.ctx, ast.Load) and c.id not in local:
                out.add((c.id, not in_function))
            visit(c, local, in_function)
    visit(t, set(), False)
    return out


def touches(t):
    """{qualified function: {call, ...}} for each place in a part that touches the system."""
    found = {}

    def visit(node, where):
        for c in ast.iter_child_nodes(node):
            w = where + [c.name] if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else where
            name = ast.unparse(c) if isinstance(c, ast.Attribute) else None
            last = name.rsplit(".", 1)[-1] if name else ""
            if name and TOUCHES.match(name) and not (last.isupper() or last.endswith(("Error", "Expired"))):
                found.setdefault(".".join(where) or "<top level>", set()).add(name)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "open":
                found.setdefault(".".join(where) or "<top level>", set()).add("open")
            visit(c, w)
    visit(t, [])
    return found


class TestParts(unittest.TestCase):
    def setUp(self):
        self.trees = {p: tree(p) for p in G.PARTS}

    def test_the_parts_are_the_files_and_hoplon_runs_them_all(self):
        files = sorted(f[:-3] for f in os.listdir(ROOT) if f.endswith(".py"))
        self.assertEqual(sorted(G.PARTS), files)
        for p in G.PARTS:
            self.assertIn(f"hoplon_core/{p}.py".encode(), G.bundle())

    def test_each_name_is_defined_by_one_part(self):
        owner = {}
        for p in G.PARTS:
            for name in defined(self.trees[p]):
                with self.subTest(name=name):
                    self.assertNotIn(name, owner, f"{name} is defined in {owner.get(name)} and {p}")
                    owner[name] = p

    def test_each_part_uses_only_earlier_parts_and_its_header_says_which(self):
        owner = {n: p for p in G.PARTS for n in defined(self.trees[p])}
        builtin = set(dir(builtins))
        launcher = {"__name__", "__file__", "BUNDLE_SHA256", "PARTS", "bundle", "bundle_digest"}
        for i, p in enumerate(G.PARTS):
            with self.subTest(part=p):
                uses = set()
                for name, at_load in free(self.trees[p]):
                    o = owner.get(name)
                    if o is None:
                        self.assertIn(name, builtin | launcher, f"{p} uses {name}, which no part defines")
                        continue
                    if o == p:
                        continue
                    self.assertLess(G.PARTS.index(o), i, f"{p} uses {name} from {o}, a later part")
                    uses.add(o)
                header = re.search(r"^# Uses: (.*)$", read(p), re.M)
                self.assertTrue(header, f"{p} has no '# Uses:' line in its header")
                said = set() if header.group(1) == "nothing: it is the first" else set(header.group(1).split(", "))
                self.assertEqual(said, uses, f"{p}'s header says it uses {sorted(said)}")

    def test_only_base_imports(self):
        # So no part can reach a call below under another name: everything comes from base's imports.
        for p in G.PARTS:
            if p == "base":
                continue
            with self.subTest(part=p):
                imports = [ast.unparse(n) for n in ast.walk(self.trees[p]) if isinstance(n, (ast.Import, ast.ImportFrom))]
                self.assertEqual(imports, ["import readline"] if p == "front" else [])   # the REPL's line editing

    def test_only_the_handlers_touch_the_system_and_every_other_place_is_listed(self):
        seen = set()
        for p in G.PARTS:
            if p in HANDLERS:
                continue
            for where, calls in touches(self.trees[p]).items():
                with self.subTest(part=p, function=where):
                    self.assertIn((p, where), ELSEWHERE, f"{p}.{where} touches the system: {sorted(calls)}")
                    self.assertLessEqual(calls, ELSEWHERE[(p, where)][0])
                    seen.add((p, where))
        self.assertEqual(set(ELSEWHERE) - seen, set(), "listed but no longer touching anything: remove them")

    def test_the_gate_touches_nothing(self):
        self.assertEqual(touches(self.trees["gate"]), {})
        self.assertFalse([k for k in ELSEWHERE if k[0] == "gate"])

    def test_each_request_is_carried_out_by_a_handler(self):
        where = {n: p for p in G.PARTS for n in defined(self.trees[p])}
        for name, (_, handler) in G.GATE_REQUESTS.items():
            with self.subTest(request=name):
                cls = handler.split(".")[0]
                self.assertIn(where[cls], HANDLERS | {"gate"})

    def test_each_part_keeps_to_its_budget(self):
        self.assertEqual(set(BUDGET), set(G.PARTS))
        for p in G.PARTS:
            with self.subTest(part=p):
                self.assertLessEqual(read(p).count("\n"), BUDGET[p])


class TestAudit(unittest.TestCase):
    """Every function and test AUDIT.md names exists where it says, so the map can't drift from the code."""

    def test_every_reference_in_audit_md_exists(self):
        with open(os.path.join(ROOT, "..", "AUDIT.md"), encoding="utf-8") as f:
            text = f.read()
        code = re.findall(r"`(\w+)\.py: (\w+)(?:\.(\w+))?`", text)
        tests = re.findall(r"`(test_\w+)\.(\w+)\.(test_\w+)`", text)
        self.assertGreater(len(code), 20)
        self.assertGreater(len(tests), 20)
        for part, name, meth in code:
            with self.subTest(ref=f"{part}.py: {name}" + (f".{meth}" if meth else "")):
                self.assertIn(part, G.PARTS)
                self.assertTrue(has(tree(part), name, meth or None))
        here = os.path.dirname(os.path.abspath(__file__))
        for module, cls, meth in tests:
            with self.subTest(ref=f"{module}.{cls}.{meth}"):
                with open(os.path.join(here, f"{module}.py"), encoding="utf-8") as f:
                    self.assertTrue(has(ast.parse(f.read()), cls, meth))


def has(t, name, member=None):
    """Whether a module defines name at its top level, and, if member is given, whether that class defines it itself."""
    for n in t.body:
        names = {n.name} if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else defined(ast.Module([n], []))
        if name in names:
            if member is None:
                return True
            return isinstance(n, ast.ClassDef) and member in {m.name for m in n.body if isinstance(
                m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return False


class TestChecks(unittest.TestCase):
    """The checks above catch what they are for."""

    def test_a_later_name_used_at_load_time_or_later_is_found(self):
        t = ast.parse("X = later()\ndef f(a):\n    return a + other\n")
        self.assertEqual(free(t), {("later", True), ("other", False)})

    def test_a_local_name_is_not_a_use(self):
        t = ast.parse("def f(main):\n    x = [y for y in main]\n    return x, (lambda z: z)\n"
                      "class C:\n    @property\n    def a(self):\n        return 1\n    @a.setter\n    def a(self, v):\n        pass\n")
        self.assertEqual(free(t), {("property", True)})     # a class body runs when the class is made

    def test_what_runs_when_a_function_is_made_is_load_time(self):
        t = ast.parse("@deco\ndef f(a=DEFAULT):\n    return BODY\nclass C(Base):\n    X = VALUE\n")
        self.assertEqual(free(t), {("deco", True), ("DEFAULT", True), ("BODY", False), ("Base", True),
                                   ("VALUE", True)})

    def test_a_call_that_touches_the_system_is_found_where_it_is(self):
        t = ast.parse("class A:\n    def go(self):\n        subprocess.run(['x'], stdout=subprocess.PIPE)\n"
                      "def f():\n    with open('p', 'w'):\n        os.unlink('p')\n")
        self.assertEqual(touches(t), {"A.go": {"subprocess.run"}, "f": {"open", "os.unlink"}})
        self.assertEqual(touches(ast.parse("p = subprocess.Popen(['x'])\n")), {"<top level>": {"subprocess.Popen"}})


if __name__ == "__main__":
    unittest.main()
