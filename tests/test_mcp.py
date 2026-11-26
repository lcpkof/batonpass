"""The MCP surface: three tools, and the promises they make.

The failures worth a test here are not crashes — they are the quiet ones: a
search that invents a session, a note that is written but never shown again, a
protocol that writes a stray line to stdout and confuses the client forever.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest

from batonpass import mcp
from batonpass.identity import tasks as tk
from batonpass.store.content import Codec, put_content
from batonpass.store.db import connect
from batonpass.store.write import message_key, now_iso, put_message, session_key, upsert_session

TEXT = "先看看资料箱模块，然后修一下导入的字段。"


def make_library(path, device="alpha", project_key="~/xueji"):
    conn = connect(path)
    codec = Codec(None)
    sid = session_key("alma", "t1", device)
    upsert_session(conn, {
        "sid": sid, "source": "alma", "source_sid": "t1", "device": device,
        "project_key": project_key, "project_key_tier": 2, "cwd": "/work/xueji",
        "title": "资料箱修复", "created_at": now_iso(), "updated_at": now_iso(),
        "msg_count": 1, "first_msg_at": now_iso(), "last_msg_at": now_iso(),
        "adapter_ver": "0.4.0", "content_hash": "x",
    })
    sha, _ = put_content(conn, TEXT, codec)
    put_message(conn, message_key(sid, 0), sid, 0, "user", sha)
    conn.commit()
    return conn, sid


def session(inp: str, db) -> list:
    out = io.StringIO()
    mcp.serve(db_path=db, instream=io.StringIO(inp), outstream=out)
    return [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]


def call(i, name, args, db):
    req = json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                      "params": {"name": name, "arguments": args}})
    return session(req + "\n", db)[0]


class McpSurface(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-mcp-")
        self.db = os.path.join(self.tmp, "lib.db")
        self.conn, self.sid = make_library(self.db)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_handshake_and_tool_list(self):
        reqs = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]) + "\n"
        got = session(reqs, self.db)
        self.assertEqual(len(got), 2)                 # the notification gets no reply
        self.assertEqual(got[0]["result"]["protocolVersion"], mcp.PROTOCOL)
        names = [t["name"] for t in got[1]["result"]["tools"]]
        self.assertEqual(names, ["memory_search", "memory_add", "resume",
                                 "places", "relay"])

    def test_search_finds_what_is_there_and_says_so(self):
        r = call(1, "memory_search", {"query": "资料箱"}, self.db)
        text = r["result"]["content"][0]["text"]
        self.assertIn("资料箱修复", text)
        self.assertIn(self.sid[:8], text)

    def test_search_never_invents_a_session(self):
        r = call(2, "memory_search", {"query": "妖族"}, self.db)
        text = r["result"]["content"][0]["text"]
        self.assertIn("not found", text)
        self.assertIn("Do not invent", text)

    def test_a_note_is_recorded_and_then_visible_on_the_card(self):
        r = call(3, "memory_add", {"text": "导入要用新字段", "project": "~/xueji",
                                   "kind": "decision"}, self.db)
        self.assertIn("recorded", r["result"]["content"][0]["text"])
        card = tk.card(self.conn, "~/xueji")
        self.assertIn("决定：导入要用新字段", card)

    def test_a_note_without_a_project_says_what_is_missing(self):
        r = call(4, "memory_add", {"text": "随手一句", "project": "no-such-project-anywhere"},
                 self.db)
        text = r["result"]["content"][0]["text"]
        self.assertIn("cannot record", text)

    def test_resume_hands_over_the_card(self):
        r = call(5, "resume", {"project": "~", "sessions": 1}, self.db)
        self.assertIn("content", r["result"])
        card = call(6, "resume", {"project": "~/xueji", "sessions": 1}, self.db)
        self.assertIn("任务", card["result"]["content"][0]["text"])

    def test_unknown_tool_and_unknown_method_are_protocol_errors(self):
        bad_tool = call(7, "nope", {}, self.db)
        self.assertEqual(bad_tool["error"]["code"], -32602)
        bad_method = session(json.dumps({"jsonrpc": "2.0", "id": 8,
                                         "method": "resources/list"}) + "\n", self.db)[0]
        self.assertEqual(bad_method["error"]["code"], -32601)

    def test_a_missing_argument_is_a_clear_error_not_a_traceback(self):
        r = call(9, "memory_search", {}, self.db)
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("query", r["error"]["message"])

    def test_stdout_carries_protocol_and_nothing_else(self):
        out = io.StringIO()
        mcp.serve(db_path=self.db, instream=io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"), outstream=out)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["result"], {})