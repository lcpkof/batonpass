"""A miss has to say what kind of miss it is.

"Nothing matched" is the one answer a memory tool must never get wrong, because the
person asking has no way to tell it apart from "there is nothing there". These tests
cover the two ways a library can be honest and still unable to answer: the text is
stored under a codec this machine cannot read, or the text never travelled.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from batonpass.recall import build_index, undecodable_hint
from batonpass.store.db import connect
from batonpass.store.write import now_iso, session_key, upsert_session


def one_session(conn, msgs: int):
    sid = session_key("alma", "t1", "alpha")
    upsert_session(conn, {
        "sid": sid, "source": "alma", "source_sid": "t1", "device": "alpha",
        "project_key": "~/xueji", "project_key_tier": 2, "cwd": "/work/xueji",
        "title": "x", "created_at": now_iso(), "updated_at": now_iso(),
        "msg_count": msgs, "first_msg_at": now_iso(), "last_msg_at": now_iso(),
        "adapter_ver": "0.5.0", "content_hash": "x",
    })
    conn.commit()


class HonestMiss(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bp-recall-")
        self.conn = connect(os.path.join(self.tmp, "library.db"))

    def tearDown(self):
        self.conn.close()

    def test_a_library_with_text_says_nothing_extra(self):
        one_session(self.conn, 1)
        build_index(self.conn)
        self.assertEqual(undecodable_hint(self.conn), "")

    def test_sessions_on_record_but_no_text_reads_as_a_pull_that_was_too_shallow(self):
        one_session(self.conn, 42)
        hint = undecodable_hint(self.conn)
        self.assertIn("1 session(s)", hint)
        self.assertIn("--tier 2", hint)


if __name__ == "__main__":
    unittest.main()