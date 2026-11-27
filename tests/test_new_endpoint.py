"""hoplon --new-endpoint: it asks, checks, writes a private endpoint file, and never blesses it."""

import json
import os
import stat
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fakeapi import FakeAPI  # noqa: E402
from test_hoplon import G, Tmp  # noqa: E402


class TestNewEndpoint(Tmp):
    def setUp(self):
        super().setUp()
        self.home = self.path("home", "hoplon")
        self.cfg = G.Config(self.home)
        self.cfg.ensure()

    def run_with(self, answers, client=None):
        answers, out = list(answers), []

        def ask(q):
            out.append(q)
            if not answers:
                raise EOFError
            return answers.pop(0)
        path = G.new_endpoint(self.cfg, ask, out.append, client)
        return path, "\n".join(map(str, out))

    def test_it_lists_the_models_offers_aliases_and_writes_a_private_file(self):
        with FakeAPI([], models=[{"id": "small", "max_model_len": 16000}, {"id": "big", "max_model_len": 128000}]) as api:
            with mock.patch.dict(os.environ, {"TEST_KEY_VAR": "sk-not-real"}):
                path, out = self.run_with(["work", api.url, "TEST_KEY_VAR", "y", "2", "", "y", "1", "", "2"])
        self.assertEqual(path, os.path.join(self.home, "endpoints", "work.json"))
        data = json.load(open(path))
        self.assertEqual(data, {"name": "work", "base_url": api.url, "key_env": "TEST_KEY_VAR", "model": "big",
                                "context_limit": 128000, "aliases": {"low": "small", "high": "big"}})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertIn(" 2. big  (128000 tokens)", out)
        self.assertIn("That sends a request with your key ($TEST_KEY_VAR) there now, before you bless", out)
        self.assertIn(f"hoplon --bless {path}", out)
        self.assertNotIn("sk-not-real", out + open(path).read())
        self.assertNotIn(os.path.abspath(path), self.cfg.blessed())           # never blessed for you
        G.Endpoint.load(path)

    def test_answers_are_checked_and_asked_again(self):
        def client(ep):
            raise G.HoplonError("cannot reach it")
        path, out = self.run_with(["bad name", "ok", "ftp://x", "https://llm.example/v1", "sk-abc-123-not-a-name",
                                   "", "y", "has space", "my-model", "12", "64000", ""], client)
        self.assertIn("letters, digits", out)
        self.assertIn("an http(s) address", out)
        self.assertIn("never the key itself", out)
        self.assertNotIn("sk-abc-123-not-a-name", out.split("never the key itself", 1)[1])
        self.assertIn("could not list the server's models: cannot reach it", out)
        self.assertIn("a model name", out)
        self.assertIn("a whole number of tokens", out)
        self.assertEqual(json.load(open(path)), {"name": "ok", "base_url": "https://llm.example/v1",
                                                 "model": "my-model", "context_limit": 64000})

    def test_an_existing_name_is_refused_and_plain_http_asks(self):
        os.makedirs(os.path.join(self.home, "endpoints"), exist_ok=True)
        open(os.path.join(self.home, "endpoints", "taken.json"), "w").close()
        with self.assertRaisesRegex(G.HoplonError, "nothing was written"):
            self.run_with(["taken", "new", "http://llm.example/v1", "n"], lambda ep: None)
        self.assertFalse(os.path.exists(os.path.join(self.home, "endpoints", "new.json")))

    def test_stopping_writes_nothing(self):
        with self.assertRaisesRegex(G.HoplonError, "nothing was written"):
            self.run_with(["half"])
        with self.assertRaisesRegex(G.HoplonError, "nothing was written"):      # Ctrl-D at the last question
            self.run_with(["late", "https://llm.example/v1", "", "n", "m", ""])
        self.assertEqual(os.listdir(os.path.join(self.home, "endpoints")), [])

    def test_nothing_is_sent_unless_you_say_so(self):
        def client(ep):
            raise AssertionError("asked the server")
        with mock.patch.dict(os.environ, {"TEST_KEY_VAR": "sk-not-real"}):
            path, out = self.run_with(["quiet", "http://[::1", "https://llm.example/v1", "TEST_KEY_VAR", "n",
                                       "123", "", "n"], client)
        self.assertIn("an http(s) address", out)         # a malformed address is asked again, not a crash
        self.assertEqual(json.load(open(path))["model"], "123")     # with no list, a number is a name


if __name__ == "__main__":
    unittest.main()
