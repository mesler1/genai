"""Model aliases: an endpoint names its models (low, medium, high, or your own), and every place a model is named
takes an alias: the endpoint's own model, a profile's model, a work session's planner and :model."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakeapi import FakeAPI, reply  # noqa: E402
from test_hoplon import G, Proc, Tmp, bless_path, make_home, write_json  # noqa: E402

ALIASES = {"low": "flash-model", "medium": "fake-model", "high": "accounts/acme/models/pro-model-2026-instruct-long"}


def endpoint(**extra):
    return G.Endpoint({"name": "e", "base_url": "https://llm.example/v1", "model": "fake-model", **extra}, "e.json")


class TestEndpointAliases(unittest.TestCase):
    def test_aliases_are_checked(self):
        for bad in ([], {"9x": "m"}, {"has space": "m"}, {"ok": "bad model"}, {"ok": 5}, {"x" * 33: "m"},
                    {"auto": "m"}):
            with self.subTest(bad=bad):
                with self.assertRaises(G.HoplonError):
                    endpoint(aliases=bad)

    def test_the_endpoints_model_may_be_an_alias(self):
        ep = endpoint(model="high", aliases=ALIASES)
        self.assertEqual(ep.model, ALIASES["high"])
        self.assertEqual(ep.info()["alias"], "high")

    def test_both_names_are_shown_and_long_ones_shortened(self):
        ep = endpoint(aliases=ALIASES)
        self.assertEqual(ep.shown_model(), "medium (fake-model)")
        self.assertEqual(ep.shown_model(ALIASES["high"]), "high (pro-model-2026-instruct-long)")
        self.assertEqual(ep.shown_model("other-model"), "other-model")
        self.assertEqual(G.short_model("a/" + "x" * 40), "x" * 31 + "…")
        text, _ = ep.summary()
        self.assertIn("  model:    medium (fake-model)", text)
        self.assertIn("  alias:    high sends to accounts/acme/models/pro-model-2026-instruct-long", text)

    def test_the_blessing_covers_the_aliases(self):
        self.assertIn("aliases", G.ENDPOINT_KEYS)        # in the endpoint file, so its hash changes with them

    def test_a_profile_may_name_a_model(self):
        self.assertEqual(G.Profile({"name": "p", "model": "high"}, "p.json").model, "high")
        with self.assertRaises(G.HoplonError):
            G.Profile({"name": "p", "model": "has space"}, "p.json")


class TestAliasesInUse(Tmp):
    MODELS = [{"id": "fake-model"}, {"id": "flash-model", "max_model_len": 8000}]

    def home(self, api, profile=None):
        home, path = make_home(self.tmp, api.url, aliases=ALIASES)
        bless_path(home, path)
        if profile:
            bless_path(home, write_json(os.path.join(home, "profiles", "p.json"), {"name": "p", **profile}))
        return home

    def audit(self, home):
        return [json.loads(ln) for ln in open(os.path.join(home, "audit.log"))]

    def test_model_takes_an_alias_and_the_audit_log_records_the_real_name(self):
        with FakeAPI([reply("one"), reply("two")], models=self.MODELS) as api:
            home = self.home(api)
            g = Proc(home, self.tmp, ["-e", "test"])
            self.assertEqual(g.until("hello")["endpoint"]["alias"], "medium")
            g.send({"type": "models"})
            self.assertEqual(g.until("models")["aliases"], ALIASES)
            g.send({"type": "user", "text": "hi"})
            g.until("done")
            g.send({"type": "model", "model": "high"})       # an alias needn't be listed: it was blessed
            ev = g.until("model")
            self.assertEqual((ev["endpoint"]["model"], ev["endpoint"]["alias"]), (ALIASES["high"], "high"))
            g.send({"type": "user", "text": "again"})
            g.until("done")
            g.send({"type": "model", "model": "huge"})
            err = g.until("error")
            g.close()
        self.assertEqual(err["code"], "unknown_model")
        self.assertIn('add "aliases": {"huge": "MODEL"}', err["message"])
        self.assertIn("it has: high, low, medium", err["message"])
        self.assertEqual([r["body"]["model"] for r in api.requests if r.get("body")], ["fake-model", ALIASES["high"]])
        self.assertIn(("model", ALIASES["high"], "high"),
                      [(a["event"], a.get("model"), a.get("alias")) for a in self.audit(home)])

    def test_a_profiles_model_is_used_and_one_the_endpoint_lacks_refuses_the_session(self):
        with FakeAPI([reply("hi")], models=self.MODELS) as api:
            home = self.home(api, {"endpoint": "test", "model": "low"})
            g = Proc(home, self.tmp, ["-p", "p"])
            self.assertEqual(g.until("hello")["endpoint"]["model"], "flash-model")
            g.send({"type": "user", "text": "hi"})
            g.until("done")
            g.close()
        self.assertEqual(api.requests[-1]["body"]["model"], "flash-model")
        with FakeAPI([], models=self.MODELS) as api:
            home = os.path.join(self.tmp, "h2")
            os.makedirs(home)
            h, path = make_home(home, api.url)          # this endpoint has no aliases: no fallback to its model
            bless_path(h, path)
            bless_path(h, write_json(os.path.join(h, "profiles", "p.json"), {"name": "p", "model": "high"}))
            g = Proc(h, self.tmp, ["-p", "p", "-e", "test"])
            err = g.until("error", "hello")
            g.close()
        self.assertEqual((err["type"], err.get("code")), ("error", "unknown_model"))
        self.assertIn('add "aliases": {"high": "MODEL"}', err["message"])


if __name__ == "__main__":
    unittest.main()
