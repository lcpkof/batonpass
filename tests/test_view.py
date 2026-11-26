"""The human window, and the bill behind it.

Two surfaces are tested here, and the thing they have in common is that both
answer a question the agent-facing commands never had to: *what is actually in
here, and what does it cost?* A viewer that silently drops a tool's history, or
a size report that files tool output under prose, is worse than no answer.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

from batonpass import serve, size
from batonpass.recall import build_index
from batonpass.store.content import Codec, put_content
from batonpass.store.db import connect
from batonpass.store.write import (message_key, now_iso, part_key, put_message,
                                   put_part, session_key, upsert_session)

SAID = "把导入的字段改成新的客户编号。"
# 真工具输出是压缩不动的：一串重复字符压成 18 字节，会把"工具输出很小"
# 这个假象写进测试里。用随机串才量得出来。
TOOL_OUT = None


def add_session(conn, source, source_sid, device, said, path="/work/xueji",
                calls=0):
    sid = session_key(source, source_sid, device)
    upsert_session(conn, {
        "sid": sid, "source": source, "source_sid": source_sid, "device": device,
        "project_key": "~/xueji", "project_key_tier": 2, "cwd": path,
        "title": "资料箱修复", "created_at": now_iso(), "updated_at": now_iso(),
        "msg_count": 1, "first_msg_at": now_iso(), "last_msg_at": now_iso(),
        "adapter_ver": "0.8.0", "content_hash": "x",
    })
    codec = Codec(None)
    sha, _ = put_content(conn, said, codec)
    mid = message_key(sid, 0)
    put_message(conn, mid, sid, 0, "assistant", sha, ts=now_iso())
    for i in range(calls):
        out_sha, _ = put_content(conn, os.urandom(2000).hex(), codec)
        put_part(conn, part_key(mid, i), mid, i, "tool-call", name="run_code",
                 result_sha=out_sha)
    conn.commit()
    return sid


class View(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "library.db")
        self.conn = connect(self.db)
        self.a = add_session(self.conn, "billion", "s1", "alpha", SAID, calls=3)
        self.b = add_session(self.conn, "zcode", "s2", "beta", "复盘写进 2026-11-26.md",
                             path="/work/winter")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_tool_on_every_machine_is_one_list(self):
        s = serve.summary(self.conn)
        self.assertEqual(s["sessions"], 2)
        self.assertEqual({t["tool"] for t in s["tools"]}, {"billion", "zcode"})
        self.assertEqual({d["device"] for d in s["devices"]}, {"alpha", "beta"})

        rows = serve.sessions(self.conn)["sessions"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["messages"] for r in rows), "消息数不能是 0")
        only = serve.sessions(self.conn, tool="zcode")["sessions"]
        self.assertEqual([r["sid"] for r in only], [self.b],
                         "按工具过滤只能剩下那一个")

    def test_a_session_shows_its_words_and_counts_its_calls(self):
        t = serve.transcript(self.conn, self.a)
        self.assertEqual(t["path"], "/work/xueji")
        self.assertEqual(t["total"], 1)
        self.assertIn("客户编号", t["messages"][0]["text"])
        chips = t["messages"][0]["chips"]
        self.assertEqual(len(chips), 1, "三次 run_code 应该是一个标签，不是三行")
        self.assertEqual(chips[0]["n"], 3)

    def test_search_names_the_tool_it_came_from(self):
        build_index(self.conn, force=True)
        hits = serve.search(self.conn, "客户编号")["hits"]
        self.assertTrue(hits)
        self.assertEqual(hits[0]["sid"], self.a)
        self.assertEqual(hits[0]["tool"], "billion")

    def test_the_page_answers_over_http(self):
        serve.Handler.db = self.db
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            base = "http://127.0.0.1:%d" % httpd.server_address[1]
            with urlopen(base + "/") as r:
                self.assertIn("batonpass", r.read().decode())
            with urlopen(base + "/api/summary") as r:
                self.assertEqual(json.loads(r.read())["sessions"], 2)
            with urlopen(base + "/api/session?sid=" + self.a) as r:
                self.assertEqual(json.loads(r.read())["total"], 1)
            try:
                urlopen(base + "/api/nope")
                self.fail("不存在的路径应该 404")
            except HTTPError as e:
                self.assertEqual(e.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()
            serve.Handler.db = None

    def test_size_keeps_prose_and_tool_output_apart(self):
        b = size.breakdown(self.conn)
        self.assertGreater(b["prose"]["stored"], 0)
        self.assertGreater(b["tool_io"]["stored"], b["prose"]["stored"],
                           "4000 字节 × 3 次调用是正文的几十倍，不能算进正文里")
        per = {t["tool"]: t for t in b["per_tool"]}
        self.assertEqual(per["billion"]["tool_io"], b["tool_io"]["stored"])
        self.assertEqual(per["zcode"]["tool_io"], 0)


if __name__ == "__main__":
    unittest.main()
