"""dsh keeps its real transcripts as multi-frame zstd JSONL, not in sqlite.

The file on disk is append-only: every flush is another zstd frame. Reading only
the first frame loses the tail of the conversation, and reading the whole file
into memory loses the machine on a 100 MB session. So: a fixture with three
frames, plus a truncated last frame — the case a live session actually leaves.
"""

import json
import os
import shutil
import tempfile
import unittest

import zstandard

from batonpass.archive.adapters import DshSessions, get
from batonpass.archive.runner import archive_one
from batonpass.store.db import connect

EVENTS = [
    [{"type": "session", "version": 4, "id": "session-abc",
      "createdAt": 1794546007609, "cwd": "/Users/x/luocp"}],
    [
        {"type": "user/message", "seq": 1, "time": 1794546008000,
         "data": {"content": [{"type": "text", "text": "帮我把岗位表那个 join 修一下"}],
                  "source": {"kind": "user"}, "role": "user"}},
        {"type": "user/message", "seq": 2, "time": 1794546008100,
         "data": {"content": [{"type": "text", "text": "<system-reminder>skills</system-reminder>"}],
                  "source": {"kind": "skill-catalog"}, "role": "user"}},
        {"type": "assistant/message", "seq": 3, "time": 1794546009000,
         "data": {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [
             {"type": "reasoning", "text": "先看表结构"},
             {"type": "text", "text": "我看一下表结构"}]}}},
    ],
    [
        {"type": "tool/call", "seq": 4, "time": 1794546009100,
         "data": {"turn": 1, "step": 1, "callId": "c1", "name": "bash",
                  "arguments": "{\"command\":\"sqlite3 jw.db .schema\"}"}},
        {"type": "tool/result", "seq": 5, "time": 1794546009500,
         "data": {"turn": 1, "step": 1,
                  "message": {"role": "tool", "toolCallId": "c1",
                              "content": [{"type": "text", "text": "CREATE TABLE jw(id, department_id)"}]}}},
        {"type": "assistant/message", "seq": 6, "time": 1794546010000,
         "data": {"turn": 1, "step": 2, "message": {"role": "assistant",
                  "content": [{"type": "text", "text": "是 department_id 对错了表"}]}}},
    ],
]


def write_session(root, slug="--Users-x-luocp--", uuid="abc", frames=3, tail=False):
    d = os.path.join(root, slug, "session-%s" % uuid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "session.v4.jsonl.zstd")
    buf = b""
    for i in range(min(frames, len(EVENTS))):
        payload = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in EVENTS[i])
        buf += zstandard.ZstdCompressor().compress(payload.encode())
    if tail:  # a half-written frame, exactly what a live session leaves behind
        buf += zstandard.ZstdCompressor().compress(b'{"type":"assistant/mess')
        buf = buf[:-8]
    with open(path, "wb") as fh:
        fh.write(buf)
    return path


class DshSessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-dsh-")
        self.home = os.path.join(self.tmp, ".dsh")
        self.root = os.path.join(self.home, "sessions")
        write_session(self.root)
        self.lib = connect(os.path.join(self.tmp, "library.db"))

    def tearDown(self):
        self.lib.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_multi_frame_file_is_read_across_frames(self):
        msgs = list(DshSessions.messages(self.root, "--Users-x-luocp--/session-abc"))
        texts = " ".join(m["text"] for m in msgs)
        # the last message lives in the third frame; reading frame one only would lose it
        self.assertIn("department_id 对错了表", texts)

    def test_truncated_tail_does_not_lose_the_head(self):
        write_session(self.root, uuid="torn", frames=3, tail=True)
        msgs = list(DshSessions.messages(self.root, "--Users-x-luocp--/session-torn"))
        self.assertIn("帮我把岗位表那个 join 修一下", " ".join(m["text"] for m in msgs))

    def test_tool_call_and_result_land_on_one_message(self):
        msgs = list(DshSessions.messages(self.root, "--Users-x-luocp--/session-abc"))
        parts = [p for m in msgs for p in (m.get("parts") or [])]
        kinds = [p["kind"] for p in parts]
        self.assertIn("tool_call", kinds)
        self.assertIn("tool_result", kinds)
        call = next(p for p in parts if p["kind"] == "tool_call")
        self.assertEqual(call["name"], "bash")
        res = next(p for p in parts if p["kind"] == "tool_result")
        self.assertEqual(res["name"], "bash")  # joined by callId, not guessed
        self.assertIn("department_id", res["text"])

    def test_injected_context_is_not_filed_as_something_you_said(self):
        msgs = list(DshSessions.messages(self.root, "--Users-x-luocp--/session-abc"))
        skill = [m for m in msgs if "system-reminder" in m["text"]]
        self.assertEqual(len(skill), 1)
        self.assertEqual(skill[0]["role"], "system")
        self.assertEqual([m["role"] for m in msgs if "join 修一下" in m["text"]], ["user"])

    def test_archive_a_directory_source(self):
        res = archive_one(self.lib, "dsh-sessions", self.root, "testdev", level=1)
        self.assertEqual(res.errors, [])
        self.assertEqual(res.sessions, 1)
        self.assertGreaterEqual(res.messages, 4)
        row = self.lib.execute("SELECT title, cwd FROM session").fetchone()
        self.assertEqual(row[1], "/Users/x/luocp")

    def test_second_archive_adds_nothing(self):
        archive_one(self.lib, "dsh-sessions", self.root, "testdev", level=1)
        again = archive_one(self.lib, "dsh-sessions", self.root, "testdev", level=1)
        self.assertEqual(again.new_rows, 0)

    def test_the_adapter_is_registered(self):
        self.assertIs(get("dsh-sessions"), DshSessions)


if __name__ == "__main__":
    unittest.main()
