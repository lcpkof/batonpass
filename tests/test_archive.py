"""The three promises `archive` makes, as tests rather than adjectives.

1. it does not touch the source,
2. running it twice does not double anything,
3. what comes back out is what went in.

Builds a tiny source database with the same shape as a real one, so the test
exercise the actual adapter code rather than a mock of it.
"""

import json
import os
import sqlite3
import tempfile
import unittest

from batonpass.archive.export import export_session
from batonpass.archive.runner import archive_one
from batonpass.store.db import connect

CHAT = [
    ("t1", "岗位表修复", [
        ("m1", "user", "帮我把岗位表那个 join 修一下"),
        ("m2", "assistant", "修好了，是 department_id 对错了表"),
        ("m3", "user", "顺手把测试补上"),
    ]),
]


def make_source(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE chat_threads (
            id TEXT PRIMARY KEY, title TEXT, model TEXT, created_at TEXT,
            updated_at TEXT, workspace_id TEXT, parent_thread_id TEXT);
        CREATE TABLE chat_messages (
            id TEXT PRIMARY KEY, thread_id TEXT, message TEXT, created_at TEXT,
            depth INTEGER, metadata TEXT);
    """)
    for tid, title, msgs in CHAT:
        db.execute("INSERT INTO chat_threads VALUES(?,?,?,?,?,?,?)",
                   (tid, title, "test", "2026-11-25T02:00:00Z",
                    "2026-11-25T02:10:00Z", "ws1", None))
        for i, (mid, role, text) in enumerate(msgs):
            blob = {"role": role, "content": [{"type": "text", "text": text}]}
            db.execute("INSERT INTO chat_messages VALUES(?,?,?,?,?,?)",
                       (mid, tid, json.dumps(blob, ensure_ascii=False),
                        "2026-11-25T02:0%d:00Z" % i, 0, None))
    db.commit()
    db.close()


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-test-")
        self.src = os.path.join(self.tmp, "source.db")
        make_source(self.src)
        self.lib = connect(os.path.join(self.tmp, "library.db"))

    def test_source_is_never_written(self):
        before = (os.path.getsize(self.src), os.stat(self.src).st_mtime_ns)
        self.assertEqual(before, before)  # baseline
        archive_one(self.lib, "alma", self.src, "testdev", level=2)
        after = (os.path.getsize(self.src), os.stat(self.src).st_mtime_ns)
        self.assertEqual(before, after)

    def test_second_run_adds_nothing(self):
        first = archive_one(self.lib, "alma", self.src, "testdev", level=2)
        self.assertGreater(first.new_rows, 0)
        second = archive_one(self.lib, "alma", self.src, "testdev", level=2)
        self.assertEqual(second.new_rows, 0)
        self.assertEqual(second.errors, [])

    def test_every_message_arrives(self):
        expected = sum(len(m) for _, _, m in CHAT)
        res = archive_one(self.lib, "alma", self.src, "testdev", level=2)
        got = self.lib.execute("SELECT count(*) FROM message").fetchone()[0]
        self.assertEqual(got, expected)
        self.assertEqual(res.messages, expected)

    def test_text_survives_the_round_trip(self):
        archive_one(self.lib, "alma", self.src, "testdev", level=2)
        sid = self.lib.execute("SELECT sid FROM session LIMIT 1").fetchone()[0]
        doc = export_session(sid, db=os.path.join(self.tmp, "library.db"))
        if isinstance(doc, str):
            doc = json.loads(doc)
        body = json.dumps(doc, ensure_ascii=False)
        for _, _, msgs in CHAT:
            for _, _, text in msgs:
                self.assertIn(text, body)


if __name__ == "__main__":
    unittest.main()
