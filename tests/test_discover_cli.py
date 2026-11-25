#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
`batonpass discover --json` has to be usable from a script.

It was not: the subcommand accepted the flag and the scanner demanded a path,
so `batonpass discover --json` died with "argument --json: expected one
argument" — the flag existed all along and did nothing. These tests pin the
contract down: no argument means stdout, `-` means stdout, a path means the
file, and stdout-only writes nothing at all.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from batonpass import cli

SUMMARY = {
    "batonpass": "test",
    "generated_at": "2026-01-01T00:00:00+00:00",
    "host": "test-host",
    "platform": "test",
    "python": "3.9.0",
    "sources": [],
    "unclaimed": [],
    "ignored": [],
    "skipped": {},
    "totals": {"sources": 0, "recognised": 0, "sessions": 0, "messages": 0,
               "bytes_on_disk": 0, "skipped_files": 0, "unclaimed_dirs": 0,
               "ignored_dirs": 0, "payload_bytes": None, "with_messages": 0,
               "degradation_kinds": 0},
}


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("batonpass.discover.probe.run", return_value=SUMMARY), \
            redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class DiscoverJsonTest(unittest.TestCase):
    def test_bare_json_flag_prints_to_stdout(self):
        rc, out, err = run_cli(["discover", "--json"])
        self.assertEqual(0, rc)
        self.assertEqual(0, json.loads(out)["totals"]["messages"])
        self.assertIn("read-only scan", err)

    def test_dash_is_stdout_too(self):
        rc, out, err = run_cli(["discover", "--json", "-"])
        self.assertEqual(0, rc)
        self.assertIn("totals", json.loads(out))

    def test_stdout_only_writes_nothing(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                run_cli(["discover", "--json", "-"])
                self.assertEqual([], os.listdir(tmp))
            finally:
                os.chdir(cwd)

    def test_a_path_still_writes_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "out.json")
            rc, out, err = run_cli(["discover", "--json", dest,
                                    "--report", os.path.join(tmp, "r.html")])
            self.assertEqual(0, rc)
            with open(dest, encoding="utf-8") as fh:
                self.assertIn("totals", json.load(fh))
            self.assertNotIn("totals", out)   # the summary, not the JSON
            self.assertIn("report →", out)
            self.assertTrue(os.path.exists(os.path.join(tmp, "r.html")))


if __name__ == "__main__":
    unittest.main()
