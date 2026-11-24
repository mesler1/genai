"""Tests for the Slurm console (plugins/slurm/slurm.py): parsing what the plugin's commands print, what is drawn, and
what the pane asks the conversation pane for. The commands' output here is what sinfo and squeue print with the
formats in plugins/slurm/plugin.json."""

import curses
import json
import os
import queue
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "tui"))
sys.path.insert(0, os.path.join(REPO, "plugins", "slurm"))
import slurm  # noqa: E402

NODES = """c001|batch*|idle|0/32/0/32|192000|none
c002|batch*|drained|0/0/32/32|192000|bad DIMM
c002|debug|drained|0/0/32/32|192000|bad DIMM
c003|batch*|down*|0/0/32/32|192000|Not responding
c004|batch*|mixed|16/16/0/32|192000|none
"""
PARTS = "batch*|up|2-00:00:00|4|1/1/2/4|16/48/64/128\ndebug|down|1:00:00|1|0/0/1/1|0/0/32/32\n"
QUEUE = """101|batch|train|alice|RUNNING|1:00:00|2|c004
102|batch|eval|alice|PENDING|0:00|1|(Resources)
103|batch|other|bob|RUNNING|5:00|1|c004
"""


class Link:
    def __init__(self):
        self.sent, self.inbox = [], queue.Queue()

    def send(self, msg):
        self.sent.append(msg)
        return True


def shown(lines):
    return "\n".join("".join(t for t, _ in ln) for ln in lines)


class TestParsing(unittest.TestCase):
    def test_nodes_once_each_bad_first(self):
        nodes = slurm.parse_nodes(NODES)
        self.assertEqual([n["name"] for n in nodes], ["c003", "c002", "c001", "c004"])
        c002 = next(n for n in nodes if n["name"] == "c002")
        self.assertEqual((c002["partitions"], c002["reason"]), (["batch*", "debug"], "bad DIMM"))
        self.assertEqual(next(n for n in nodes if n["name"] == "c001")["reason"], "")

    def test_queue_counts_and_my_jobs(self):
        counts, mine = slurm.parse_queue(QUEUE, "alice")
        self.assertEqual(counts, {"RUNNING": 2, "PENDING": 1})
        self.assertEqual([j["id"] for j in mine], ["101", "102"])

    def test_malformed_lines_are_skipped(self):
        self.assertEqual(slurm.parse_nodes("garbage\n\nx|y\n"), [])
        self.assertEqual(slurm.parse_partitions(PARTS)[1]["avail"], "down")


class TestConsole(unittest.TestCase):
    def setUp(self):
        self.link = Link()
        self.c = slurm.Console(self.link, "alice")

    def answer(self, purpose, output, **extra):
        rid = next(r for r, p in self.c.waiting.items() if p == purpose)
        self.c.handle(dict({"type": "ran", "id": rid, "exit": 0, "output": output}, **extra))

    def loaded(self):
        self.c.refresh()
        self.assertEqual([m["command"] for m in self.link.sent], ["nodes", "partitions", "queue"])
        self.assertTrue(all(m["type"] == "run" and m["params"] == {} for m in self.link.sent))
        self.answer("nodes", NODES)
        self.answer("partitions", PARTS)
        self.answer("queue", QUEUE)

    def test_overview(self):
        self.loaded()
        lines, acts = slurm.view(self.c.st, 80, None, "")
        text = shown(lines)
        self.assertIn("NODES (4)", text)
        self.assertIn("down* 1 · drained 1 · idle 1 · mixed 1", text)
        self.assertIn("bad DIMM", text)
        self.assertNotIn("c001", text)                       # healthy nodes are only counted, until a filter
        self.assertIn("running 2", text)
        self.assertIn("MY JOBS (alice)", text)
        self.assertNotIn("bob", text)
        roles = {t.strip(): r for ln in lines for t, r in ln}
        self.assertEqual(roles["drained"], slurm.R)
        self.assertEqual([a for a in acts if a], [("node", "c003"), ("node", "c002"), ("job", "101"), ("job", "102")])
        lines, _ = slurm.view(self.c.st, 80, None, "c001")
        self.assertIn("c001", shown(lines))

    def test_details_insert_and_attach(self):
        self.loaded()
        self.link.sent.clear()
        self.c.key(curses.KEY_DOWN, 80)
        self.c.key("\n", 80)
        self.assertEqual(self.link.sent[-1]["command"], "node")
        self.assertEqual(self.link.sent[-1]["params"], {"name": "c003"})
        self.answer(("node", "c003"), "NodeName=c003 State=DOWN\n")
        self.assertIn("NodeName=c003", shown(slurm.view(self.c.st, 80, None, "")[0]))
        self.c.key("a", 80)
        self.assertEqual(self.link.sent[-1], {"type": "attach", "label": "Slurm node c003",
                                              "text": "NodeName=c003 State=DOWN\n"})
        self.c.key("\x1b", 80)
        self.c.key("i", 80)
        self.assertEqual(self.link.sent[-1], {"type": "insert", "text": "c003"})
        for _ in range(5):
            self.c.key(curses.KEY_DOWN, 80)
        self.c.key("\n", 80)
        self.assertEqual(self.link.sent[-1]["params"], {"id": "102"})

    def test_errors_and_refusals_are_shown(self):
        self.c.refresh()
        self.answer("nodes", "", exit=None, error="the program changed since genai started; restart genai")
        self.assertIn("nodes: the program changed", shown(slurm.view(self.c.st, 120, None, "")[0]))
        self.c.handle({"type": "refused", "why": "this plugin is not on"})
        self.assertEqual(self.c.msg, "this plugin is not on")
        self.c.handle({"type": "ran", "id": "unknown", "output": "x"})       # not ours: ignored


class TestManifest(unittest.TestCase):
    def test_the_pane_is_in_the_plugin(self):
        with open(os.path.join(REPO, "plugins", "slurm", "plugin.json")) as f:
            data = json.load(f)
        self.assertEqual(data["pane"], "slurm.py")
        for name in ("nodes", "partitions", "queue", "node", "job"):
            self.assertTrue(data["commands"][name]["read_only"], name)


if __name__ == "__main__":
    unittest.main()
