"""`setup` is the one command a stranger runs.

So the tests are about two promises: it never eats a config it did not write,
and running it twice is the same as running it once.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from batonpass import setup


class FakeHome(unittest.TestCase):
    """A throwaway $HOME, so no real agent config is ever in reach."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self._old_home = os.environ.get("HOME")
        self._old_bp = os.environ.pop("BATONPASS_HOME", None)
        os.environ["HOME"] = self.home
        os.environ["BATONPASS_HOME"] = os.path.join(self.home, ".batonpass")

    def tearDown(self):
        os.environ["HOME"] = self._old_home or self.home
        if self._old_bp is None:
            os.environ.pop("BATONPASS_HOME", None)
        else:
            os.environ["BATONPASS_HOME"] = self._old_bp
        self._tmp.cleanup()

    def path(self, *parts):
        return os.path.join(self.home, *parts)


class RegisterTests(FakeHome):
    def test_creates_a_config_when_there_is_none(self):
        cfg = self.path(".cursor", "mcp.json")
        os.makedirs(os.path.dirname(cfg))
        self.assertEqual(setup.register(cfg), "connected")
        data = json.load(open(cfg, encoding="utf-8"))
        entry = data["mcpServers"]["batonpass"]
        self.assertEqual(entry["args"][-1], "mcp")

    def test_keeps_the_servers_that_were_already_there(self):
        cfg = self.path(".cursor", "mcp.json")
        os.makedirs(os.path.dirname(cfg))
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"other": {"command": "x"}}, "zeta": 1}, fh)
        setup.register(cfg)
        data = json.load(open(cfg, encoding="utf-8"))
        self.assertIn("other", data["mcpServers"])
        self.assertIn("batonpass", data["mcpServers"])
        self.assertEqual(data["zeta"], 1, "unknown keys survive the rewrite")

    def test_the_original_is_copied_aside(self):
        cfg = self.path(".cursor", "mcp.json")
        os.makedirs(os.path.dirname(cfg))
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {}}, fh)
        setup.register(cfg)
        self.assertTrue(os.path.exists(cfg + ".bak-batonpass"))

    def test_twice_is_the_same_as_once(self):
        cfg = self.path(".cursor", "mcp.json")
        os.makedirs(os.path.dirname(cfg))
        setup.register(cfg)
        first = open(cfg, encoding="utf-8").read()
        self.assertEqual(setup.register(cfg), "already connected")
        self.assertEqual(open(cfg, encoding="utf-8").read(), first)

    def test_a_broken_config_is_left_exactly_as_it_was(self):
        cfg = self.path(".cursor", "mcp.json")
        os.makedirs(os.path.dirname(cfg))
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        out = setup.register(cfg)
        self.assertIn("not valid JSON", out)
        self.assertEqual(open(cfg, encoding="utf-8").read(), "{ this is not json")


class FindTests(FakeHome):
    def test_no_client_here_means_no_client(self):
        self.assertEqual(setup.find_clients(), [])

    def test_a_client_that_exists_is_found_and_marked_new(self):
        os.makedirs(self.path(".config", "alma"))
        (name, path, have), = setup.find_clients()
        self.assertEqual(name, "Alma")
        self.assertFalse(have)
        self.assertTrue(path.endswith(os.path.join(".config", "alma", "mcp.json")))

    def test_one_that_already_has_it_says_so(self):
        os.makedirs(self.path(".config", "alma"))
        cfg = self.path(".config", "alma", "mcp.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {"batonpass": {"command": "x", "args": ["mcp"]}}}, fh)
        (_, _, have), = setup.find_clients()
        self.assertTrue(have)


class RunTests(FakeHome):
    def test_a_scripted_run_never_edits_a_config_by_itself(self):
        """No tty, no --yes: report the agents, touch nothing."""
        os.makedirs(self.path(".config", "alma"))
        cfg = self.path(".config", "alma", "mcp.json")
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {}}, fh)
        rc = setup.run(archive=False, index=False, yes=False, ask=lambda q: False)
        self.assertEqual(rc, 0)
        self.assertEqual(json.load(open(cfg, encoding="utf-8")), {"mcpServers": {}})

    def test_yes_connects_them(self):
        os.makedirs(self.path(".config", "alma"))
        with open(self.path(".config", "alma", "mcp.json"), "w", encoding="utf-8") as fh:
            json.dump({"mcpServers": {}}, fh)
        rc = setup.run(archive=False, index=False, yes=True)
        self.assertEqual(rc, 0)
        data = json.load(open(self.path(".config", "alma", "mcp.json"), encoding="utf-8"))
        self.assertIn("batonpass", data["mcpServers"])

    def test_join_refuses_a_folder_that_is_not_a_channel(self):
        self.assertEqual(setup.run(join=self.path("nope"), agents=False,
                                   archive=False, index=False), 2)


if __name__ == "__main__":
    unittest.main()
