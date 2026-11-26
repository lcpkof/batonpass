"""A sentence is not a key, and a long-lived reader is not free.

Two quiet failures from real use:

  ① "岗位表 join 修复" — how a person actually types it — matched nothing, while
     every single word in it matches a session. The silence contract still holds
     (no invented hits), but the answer was silence for a question that has an
     answer. Now the phrase is tried first, then its words, and the reply says
     which word did the work so nobody thinks the sentence was found verbatim.

  ② The MCP server lives for the whole app session. A Python sqlite3 SELECT
     leaves a read transaction open, one open reader is enough to block every
     WAL checkpoint, and the -wal file grew to 600 MB until `archive` started
     answering "database is locked". So: no snapshot survives a request.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest

from batonpass import mcp
from batonpass.recall import index as recall_index
from batonpass.recall.query import search, terms
from batonpass.store.content import Codec, put_content
from batonpass.store.db import connect
from batonpass.store.write import (message_key, now_iso, put_message,
                                   session_key, upsert_session)


def make_library(path, text, title="资料箱修复", project_key="~/xueji"):
    conn = connect(path)
    codec = Codec(None)
    sid = session_key("alma", "t1", "alpha")
    upsert_session(conn, {
        "sid": sid, "source": "alma", "source_sid": "t1", "device": "alpha",
        "project_key": project_key, "project_key_tier": 2, "cwd": "/work/xueji",
        "title": title, "created_at": now_iso(), "updated_at": now_iso(),
        "msg_count": 1, "first_msg_at": now_iso(), "last_msg_at": now_iso(),
        "adapter_ver": "0.4.0", "content_hash": "x",
    })
    sha, _ = put_content(conn, text, codec)
    put_message(conn, message_key(sid, 0), sid, 0, "user", sha)
    conn.commit()
    recall_index.build(conn, force=True)
    return conn, sid


class TermsTest(unittest.TestCase):
    def test_a_sentence_becomes_its_words(self):
        self.assertEqual(set(terms("岗位表 join 修复")), {"岗位表", "join", "修复"})

    def test_longest_word_first_because_it_is_the_most_specific(self):
        self.assertEqual(terms("ppt 去年考试 的那个")[0], "去年考试")

    def test_punctuation_is_not_a_word(self):
        self.assertEqual(terms("、，；：（）"), [])

    def test_one_characters_are_not_words(self):
        self.assertEqual(terms("a b 一"), [])


class PhraseFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "library.db")
        self.conn, self.sid = make_library(
            self.db, "先看看资料箱模块，然后修一下导入的字段，最后跑一遍测试。")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_whole_phrase_still_answers_when_it_is_really_there(self):
        hits = search(self.conn, "资料箱模块")
        self.assertTrue(hits)
        self.assertFalse([h for h in hits if h.get("matched")])

    def test_a_phrase_that_misses_falls_back_to_its_words(self):
        hits = search(self.conn, "资料箱 导出 HTML")
        self.assertTrue(hits, "两个词都在里面，不该是沉默")
        self.assertEqual(hits[0]["sid"], self.sid)
        self.assertTrue(hits[0]["matched"])

    def test_the_fallback_admits_it_did_not_find_the_phrase(self):
        out = io.StringIO()
        mcp.serve(db_path=self.db, instream=io.StringIO("\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05"}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "memory_search",
                                   "arguments": {"query": "资料箱 导出 HTML"}}}),
        ]) + "\n"), outstream=out)
        text = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()][-1]
        body = text["result"]["content"][0]["text"]
        self.assertIn("no exact phrase", body)
        self.assertIn("matched", body)

    def test_nothing_anywhere_is_still_silence(self):
        self.assertEqual(search(self.conn, "量子 纠缠 实验"), [])


class NoSnapshotSurvivesARequest(unittest.TestCase):
    """The reader that blocked every checkpoint, kept out by a test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "library.db")
        conn, _sid = make_library(self.db, "先看看资料箱模块。")
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_second_request_does_not_inherit_a_read_transaction(self):
        states = []
        real_handle = mcp.handle

        def spy(conn, method, params):
            states.append(conn.in_transaction)
            return real_handle(conn, method, params)

        mcp.handle = spy
        try:
            mcp.serve(db_path=self.db, instream=io.StringIO("\n".join([
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2024-11-05"}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "memory_search",
                                       "arguments": {"query": "资料箱"}}}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "memory_search",
                                       "arguments": {"query": "模块"}}}),
            ]) + "\n"), outstream=io.StringIO())
        finally:
            mcp.handle = real_handle
        self.assertEqual(states, [False, False, False],
                         "一个常驻的读快照就够把 WAL checkpoint 堵死")


if __name__ == "__main__":
    unittest.main()
