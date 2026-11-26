"""A session grows; the row describing it does not get to stay small.

The first archive of a conversation is easy. The hard case is the second one, an
hour or a day later, when the same session id comes back longer. Everything that
reads a session row — the handoff card, `tasks`, the summary tier — trusts
msg_count and last_msg_at to answer "how much is there" and "when did it happen",
so those two have to follow the source while the identity of the row stays put.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from batonpass.store.db import connect
from batonpass.store.write import session_key, upsert_session

SID = session_key("alma", "t1", "alpha")


def row(**over):
    base = {
        "sid": SID, "source": "alma", "source_sid": "t1", "device": "alpha",
        "project_key": "~/xueji", "project_key_tier": 2, "cwd": "/work/xueji",
        "title": "first title", "created_at": "2026-11-25T01:00:00Z",
        "updated_at": "2026-11-25T01:30:00Z",
        "msg_count": 108, "first_msg_at": "2026-11-25T01:00:00Z",
        "last_msg_at": "2026-11-25T01:30:00Z",
        "adapter_ver": "0.6.2", "content_hash": "h1",
    }
    base.update(over)
    return base


class GrowingSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bp-store-")
        self.conn = connect(os.path.join(self.tmp, "library.db"))

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self):
        return self.conn.execute(
            "SELECT msg_count,last_msg_at,first_msg_at,title,created_at "
            "FROM session WHERE sid=?", (SID,)).fetchone()

    def test_a_longer_second_pass_updates_the_row(self):
        self.assertTrue(upsert_session(self.conn, row()))
        self.assertFalse(upsert_session(self.conn, row(
            msg_count=144, last_msg_at="2026-11-26T03:18:17Z",
            updated_at="2026-11-26T03:18:17Z", content_hash="h2")))
        got = self.get()
        self.assertEqual(got[0], 144)
        self.assertEqual(got[1], "2026-11-26T03:18:17Z")
        self.assertEqual(got[2], "2026-11-25T01:00:00Z")   # start never moves
        self.assertEqual(got[4], "2026-11-25T01:00:00Z")   # nor does identity
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)

    def test_a_stale_second_pass_does_not_shrink_it(self):
        """The other machine's copy can be behind; behind is not smaller."""
        upsert_session(self.conn, row(msg_count=144, last_msg_at="2026-11-26T03:18:17Z"))
        upsert_session(self.conn, row(msg_count=108, last_msg_at="2026-11-25T01:30:00Z"))
        got = self.get()
        self.assertEqual(got[0], 144)
        self.assertEqual(got[1], "2026-11-26T03:18:17Z")

    def test_title_and_cwd_follow_the_source(self):
        upsert_session(self.conn, row())
        upsert_session(self.conn, row(title="renamed", cwd="/work/elsewhere"))
        self.assertEqual(self.get()[3], "renamed")
        self.assertEqual(
            self.conn.execute("SELECT cwd FROM session WHERE sid=?", (SID,)).fetchone()[0],
            "/work/elsewhere")


if __name__ == "__main__":
    unittest.main()
