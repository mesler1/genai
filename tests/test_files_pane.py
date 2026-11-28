"""Tests for the TUI's files pane (tui/files.py), without curses. Run: python3 -m unittest tests.test_files_pane"""

import curses
import importlib.machinery
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tui"))


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


F = load("files_pane", os.path.join(REPO, "tui", "files.py"))
DOWN, UP, RIGHT, LEFT = curses.KEY_DOWN, curses.KEY_UP, curses.KEY_RIGHT, curses.KEY_LEFT


def ent(name, kind="file", **kw):
    return {"name": name, "kind": kind, "size": kw.pop("size", 10), "hidden": kw.pop("hidden", False),
            **({"secret": False, "asks": False} if kind == "file" else {}), **kw}


def listing(path, entries, truncated=False):
    return {"type": "listing", "path": path, "entries": entries, "truncated": truncated}


def text(row):
    return "".join(t for t, _ in row[0])


def texts(tree, w=40):
    return [text(r) for r in tree.rows(w)]


def loaded(entries=None, truncated=False):
    t = F.Tree()
    t.receive(listing(".", entries or [], truncated))
    return t


class TreeTests(unittest.TestCase):
    def test_start_requests_root(self):
        self.assertEqual(F.Tree().start(), [{"type": "list", "path": "."}])
        self.assertEqual(texts(F.Tree()), ["  …"])

    def test_order_and_marks(self):
        t = loaded([ent("zeta.txt"), ent("Beta", "dir", size=None), ent("alpha.txt"), ent("apple", "dir", size=None),
                    ent("key.pem", secret=True), ent("cfg", asks=True), ent("lnk", "symlink", size=None),
                    ent("dev", "other", size=None)])
        rows = t.rows(40)
        self.assertEqual([r[1]["name"] for r in rows],
                         ["apple", "Beta", "alpha.txt", "cfg", "dev", "key.pem", "lnk", "zeta.txt"])
        by = {r[1]["name"]: r for r in rows}
        self.assertEqual(text(by["apple"]), "▸ apple/")
        self.assertEqual(by["key.pem"][0][1], ("⚠ ", F.R))
        self.assertEqual(by["cfg"][0][1], ("⚿ ", F.W))
        self.assertTrue(text(by["lnk"]).startswith("↪ lnk (not followed)"))
        self.assertEqual(by["lnk"][0][2][1], F.Q)
        self.assertEqual(by["dev"][0][2][1], F.Q)
        self.assertEqual(by["alpha.txt"][0][1], ("  ", ""))

    def test_secret_wins_over_asks(self):
        t = loaded([ent("x", secret=True, asks=True)])
        self.assertEqual(t.rows(40)[0][0][1][0], "⚠ ")

    def test_sizes(self):
        self.assertEqual([F.human(n) for n in (0, 512, 1024, 4096, 1258291, 12 * 1024, 5 << 30)],
                         ["0", "512", "1.0K", "4.0K", "1.2M", "12K", "5.0G"])
        t = loaded([ent("a", size=4096), ent("d", "dir", size=None)])
        rows = t.rows(40)
        self.assertTrue(text(rows[1]).endswith("4.0K") and len(text(rows[1])) == 40)
        self.assertEqual(text(rows[0]), "▸ d/")
        self.assertEqual(text(t.rows(20)[1]), "  a")  # narrow pane: no sizes

    def test_hidden_toggle(self):
        t = loaded([ent(".git", "dir", size=None), ent("a"), ent("b", hidden=True)])
        self.assertEqual(len(t.rows(40)), 3)
        self.assertEqual(t.rows(40)[0][0][2][1], F.Q)
        self.assertEqual(t.key("."), [])
        self.assertEqual([r[1]["name"] for r in t.rows(40)], ["a"])
        t.key(".")
        self.assertEqual(len(t.rows(40)), 3)

    def test_truncated_row(self):
        t = loaded([ent("a")], truncated=True)
        rows = t.rows(40)
        self.assertEqual(text(rows[-1]), "  … more not listed")
        self.assertIsNone(rows[-1][1])

    def test_expand_loads_child(self):
        t = loaded([ent("d", "dir", size=None)])
        self.assertEqual(t.key(RIGHT), [{"type": "list", "path": "d"}])
        self.assertEqual(texts(t), ["▾ d/", "    …"])
        self.assertEqual(t.key(RIGHT), [])  # already open and asked
        t.receive(listing("d", [ent("sub", "dir", size=None), ent("f")]))
        self.assertEqual([x.rstrip(" 0123456789") for x in texts(t)[:3]], ["▾ d/", "  ▸ sub/", "    f"])
        t.key(DOWN)
        t.key(RIGHT)
        self.assertEqual(t.key("\n"), [])  # Enter on an open dir closes it
        self.assertEqual(t.expanded, {"d"})
        t.key(UP)
        self.assertEqual(t.key("\n"), [])
        self.assertEqual(texts(t), ["▸ d/"])
        self.assertEqual(t.key("\n"), [])  # listing kept: no new request
        self.assertEqual(len(t.rows(40)), 3)

    def test_nested_paths_and_parent_move(self):
        t = loaded([ent("d", "dir", size=None)])
        t.key(RIGHT)
        t.receive(listing("d", [ent("e", "dir", size=None)]))
        t.key(DOWN)
        self.assertEqual(t.key("l"), [{"type": "list", "path": "d/e"}])
        t.receive(listing("d/e", [ent("f")]))
        t.key(DOWN)
        self.assertEqual(t.sel, "d/e/f")
        t.key(LEFT)
        self.assertEqual(t.sel, "d/e")
        t.key("h")  # collapses
        self.assertNotIn("d/e", t.expanded)
        t.key("h")  # to the parent
        self.assertEqual(t.sel, "d")

    def test_error_row(self):
        t = loaded([ent("d", "dir", size=None)])
        t.key(RIGHT)
        t.receive({"type": "listing", "path": "d", "error": "denied\x1b[2J"})
        rows = t.rows(40)
        self.assertIn("denied\\x1b[2J", text(rows[1]))
        self.assertEqual(rows[1][0][1][1], F.R)
        self.assertIsNone(rows[1][1])
        t.key("\n")  # close
        self.assertEqual(t.key("\n"), [{"type": "list", "path": "d"}])  # reopen retries
        self.assertEqual(texts(t)[1], "    …")

    def test_keys_send_messages(self):
        t = loaded([ent("d", "dir", size=None), ent("f"), ent("l", "symlink", size=None)])
        self.assertEqual(t.key("i"), [{"type": "insert", "path": "d"}])
        t.key("j")
        self.assertEqual(t.key("\n"), [{"type": "view", "path": "f"}])
        self.assertEqual(t.key("a"), [{"type": "attach", "path": "f"}])
        self.assertEqual(t.key("i"), [{"type": "insert", "path": "f"}])
        t.key("j")
        self.assertEqual(t.key("\n"), [])
        self.assertIn("not followed", t.note)
        self.assertEqual(t.key("a"), [])
        self.assertIn("symlinks", t.note)
        t.key("k")
        t.key("k")
        self.assertEqual(t.key("a"), [])  # a directory can't be attached
        self.assertTrue(t.note)

    def test_s_asks_to_mark_only_a_file_marked_secret(self):
        t = loaded([ent("d", "dir", size=None), ent("plain"), ent("warned", secret=True)])
        self.assertEqual(t.key("s"), [])                      # a directory
        self.assertIn("⚠", t.note)
        t.key("j")
        self.assertEqual(t.key("s"), [])                      # a file hoplon doesn't take for a secret
        t.key("j")
        self.assertEqual(t.key("s"), [{"type": "not_secret", "path": "warned"}])
        self.assertEqual(t.note, "")

    def test_movement(self):
        t = loaded([ent(f"f{i:02}") for i in range(30)])
        t.page = 10
        t.key(DOWN)
        self.assertEqual(t.sel, "f01")
        t.key(curses.KEY_NPAGE)
        self.assertEqual(t.sel, "f11")
        t.key(curses.KEY_END)
        self.assertEqual(t.sel, "f29")
        t.key(DOWN)
        self.assertEqual(t.sel, "f29")
        t.key(curses.KEY_PPAGE)
        self.assertEqual(t.sel, "f19")
        t.key(curses.KEY_HOME)
        self.assertEqual(t.sel, "f00")

    def test_refresh_requests_loaded_dirs(self):
        t = loaded([ent("d", "dir", size=None), ent("e", "dir", size=None)])
        t.key(RIGHT)
        t.receive(listing("d", [ent("x")]))
        t.key(DOWN)
        t.key(RIGHT)  # e is still loading
        self.assertEqual(t.key("r"), [{"type": "list", "path": "."}, {"type": "list", "path": "d"}])

    def test_click(self):
        t = loaded([ent("d", "dir", size=None), ent("f")])
        self.assertEqual(t.click(1), [])
        self.assertEqual(t.sel, "f")
        self.assertEqual(t.click(1), [{"type": "view", "path": "f"}])
        self.assertEqual(t.click(0), [])
        self.assertEqual(t.click(0), [{"type": "list", "path": "d"}])
        self.assertEqual(t.click(1), [])  # loading row
        self.assertEqual(t.click(99), [])

    def test_untrusted_input_ignored(self):
        t = loaded([ent("ok"), "str", None, {"name": 5}, {"name": ""}, {"name": ".."}, {"name": "a/b"},
                    {"name": "odd", "kind": "weird", "size": True, "secret": "yes", "asks": 1},
                    {"name": "ok"}])
        self.assertEqual([r[1]["name"] for r in t.rows(40)], ["odd", "ok"])
        self.assertEqual(t.entries()[0]["e"]["kind"], "other")
        for bad in ("x", 3, {}, {"type": "listing"}, {"type": "listing", "path": "../x", "entries": []},
                    {"type": "listing", "path": "/etc", "entries": []}, {"type": "listing", "path": ".", "entries": 3},
                    {"type": "listing", "path": "never/asked", "entries": [ent("z")]}, {"type": "mystery"}):
            t.receive(bad)
        self.assertEqual(len(t.rows(40)), 2)

    def test_entry_cap(self):
        t = loaded([ent(f"f{i}") for i in range(F.MAX_ENTRIES + 5)])
        self.assertEqual(len(t.rows(40)), F.MAX_ENTRIES + 1)
        self.assertEqual(text(t.rows(40)[-1]), "  … more not listed")

    def test_control_characters_escaped(self):
        t = loaded([ent("a\x1b[31mb\x07‮c"), ent("d\nx", "dir", size=None)])
        for r in t.rows(60):
            s = text(r)
            self.assertTrue(all(ord(c) >= 32 and ord(c) != 127 and c != "‮" for c in s), repr(s))
        self.assertIn("\\x1b", text(t.rows(60)[1]))
        self.assertIn("d\\nx/", text(t.rows(60)[0]))
        # the path sent back is still the real name
        t.key("j")
        self.assertEqual(t.key("i"), [{"type": "insert", "path": "a\x1b[31mb\x07‮c"}])

    def test_unavailable_and_ended(self):
        t = F.Tree()
        t.receive({"type": "unavailable", "reason": "files are off"})
        self.assertEqual(t.unavailable, "files are off")
        t.receive({"type": "unavailable", "reason": 5})
        self.assertIsInstance(t.unavailable, str)
        self.assertFalse(t.ended)
        t.receive(None)
        self.assertTrue(t.ended)
        self.assertEqual(t.key("j"), [])


if __name__ == "__main__":
    unittest.main()


class TestRefresh(unittest.TestCase):
    def test_a_quick_second_refresh_is_not_lost(self):
        t = F.Tree()
        t.receive({"type": "listing", "path": ".", "entries": [{"name": "old", "kind": "file", "size": 1}]})
        self.assertEqual([m["path"] for m in t.key("r") + t.key("r")], [".", "."])
        t.receive({"type": "listing", "path": ".", "entries": [{"name": "old", "kind": "file", "size": 1}]})
        t.receive({"type": "listing", "path": ".", "entries": [{"name": "new", "kind": "file", "size": 1}]})
        self.assertEqual([e["name"] for e in t.listings["."]["entries"]], ["new"])
        t.receive({"type": "listing", "path": ".", "entries": []})          # nothing more was asked for
        self.assertEqual([e["name"] for e in t.listings["."]["entries"]], ["new"])
