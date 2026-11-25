"""Two machines, one channel folder, no server.

The four properties this file is here to protect:

* a new machine gets the catalog and the summaries, and nothing else;
* deep-pulling one task brings its messages across byte-identically;
* pushing twice adds nothing, and pulling twice adds nothing;
* two machines that did the same task keep two separate sessions — a fork,
  never a merge.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest

from batonpass.store.content import Codec, get_content, put_content
from batonpass.store.db import connect
from batonpass.store.write import (
    message_key, part_key, put_message, put_part, session_key, upsert_session,
    now_iso,
)
from batonpass.sync import init_channel, pull, push, status

TEXT = "把记忆中枢换成接力棒，先做 archive，再做 sync。"


def make_library(path, device="alpha"):
    conn = connect(path)
    codec = Codec(None)
    sid = session_key("alma", "thread-1", device)
    upsert_session(conn, {
        "sid": sid, "source": "alma", "source_sid": "thread-1", "device": device,
        "project_key": "batonpass", "project_key_tier": 1, "cwd": "/work",
        "title": "接力棒", "created_at": now_iso(), "updated_at": now_iso(),
        "msg_count": 3, "first_msg_at": now_iso(), "last_msg_at": now_iso(),
        "adapter_ver": "0.2.0", "content_hash": "deadbeef",
    })
    for seq, role, text in ((0, "user", TEXT), (1, "assistant", "好，先写 archive。"),
                            (2, "user", "那就开始吧")):
        sha, _ = put_content(conn, text, codec)
        mid = message_key(sid, seq)
        put_message(conn, mid, sid, seq, role, sha)
        if seq == 1:
            a, _ = put_content(conn, "WebSearch", codec)
            r, _ = put_content(conn, "6 results", codec)
            put_part(conn, part_key(mid, 0), mid, 0, "tool_call", "WebSearch", a, r)
    conn.commit()
    return conn, sid


class SyncBetweenTwoMachines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-sync-")
        self.channel = os.path.join(self.tmp, "channel")
        self.a, self.sid = make_library(os.path.join(self.tmp, "a.db"))
        self.b_path = os.path.join(self.tmp, "b.db")
        self.b = connect(self.b_path)
        init_channel(self.channel, device="alpha")

    def tearDown(self):
        for c in (self.a, self.b):
            try:
                c.close()
            except Exception:
                pass

    def test_catalog_arrives_without_a_single_message(self):
        push(self.channel, tier=1, device="alpha", conn=self.a)
        st = pull(self.channel, tier=1, device="beta", conn=self.b)
        self.assertEqual(st["sessions_new"], 1)
        self.assertEqual(self.b.execute("SELECT COUNT(*) FROM message").fetchone()[0], 0)
        head = self.b.execute("SELECT one_line, head_json FROM session_head").fetchone()
        self.assertIn("记忆中枢", head["one_line"])
        self.assertIn("那就开始吧", head["head_json"])

    def test_deep_pull_of_one_task_is_byte_identical(self):
        push(self.channel, tier=2, device="alpha", conn=self.a)
        pull(self.channel, tier=0, device="beta", conn=self.b)
        st = pull(self.channel, tier=2, device="beta", conn=self.b, sid_prefix=self.sid[:8])
        self.assertEqual(st["messages_new"], 3)
        self.assertEqual(st["parts_new"], 1)
        row = self.b.execute("SELECT text_sha FROM message WHERE sid=? AND seq=0",
                             (self.sid,)).fetchone()
        got = get_content(self.b, row[0], Codec(None))
        self.assertEqual(got, TEXT)
        self.assertEqual(self.b.execute("SELECT COUNT(*) FROM part").fetchone()[0], 1)

    def test_pushing_and_pulling_twice_adds_nothing(self):
        push(self.channel, tier=1, device="alpha", conn=self.a)
        again = push(self.channel, tier=1, device="alpha", conn=self.a)
        self.assertEqual(again["sessions_sent"], 0)
        self.assertEqual(again["skipped"], 1)
        pull(self.channel, tier=1, device="beta", conn=self.b)
        second = pull(self.channel, tier=1, device="beta", conn=self.b)
        self.assertEqual(second["sessions_seen"], 0)
        self.assertEqual(self.b.execute("SELECT COUNT(*) FROM session").fetchone()[0], 1)

    def test_same_task_on_two_machines_forks_instead_of_merging(self):
        push(self.channel, tier=1, device="alpha", conn=self.a)
        c = connect(os.path.join(self.tmp, "c.db"))
        make_library(os.path.join(self.tmp, "c.db"), device="gamma")
        # the other machine archived the same source id under its own device
        push(self.channel, tier=1, device="gamma", conn=c)
        pull(self.channel, tier=0, device="beta", conn=self.b)
        self.assertEqual(self.b.execute("SELECT COUNT(*) FROM session").fetchone()[0], 2)

    def test_a_recreated_channel_gets_refilled(self):
        """Found in the field: a deleted channel must not look already-sent."""
        push(self.channel, tier=1, device="alpha", conn=self.a)
        shutil.rmtree(self.channel)
        st = push(self.channel, tier=1, device="alpha", conn=self.a)
        self.assertEqual(st["sessions_sent"], 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.channel, "stream", "alpha", "t0.jsonl")))

    def test_status_reports_unread_bytes(self):
        push(self.channel, tier=1, device="alpha", conn=self.a)
        st = status(self.channel, self.b)
        self.assertEqual([d["device"] for d in st["devices"]], ["alpha"])
        pending = {s["tier"]: s["pending"] for s in st["streams"]}
        self.assertGreater(pending["t1"], 0)
        pull(self.channel, tier=1, device="beta", conn=self.b)
        after = {s["tier"]: s["pending"] for s in status(self.channel, self.b)["streams"]}
        self.assertEqual(after["t1"], 0)


if __name__ == "__main__":
    unittest.main()
