# -*- coding: utf-8 -*-
"""A transcript is where people paste their keys.

Tier 1 is the tier that travels to every machine — USB stick, shared NAS, iCloud
— so it is the tier that must not carry the key you pasted last Tuesday. The
library keeps the raw text; the channel gets `<redacted>`.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from batonpass.store import redact
from batonpass.store.content import Codec, get_content, put_content
from batonpass.store.db import connect
from batonpass.store.write import (
    message_key, now_iso, put_message, session_key, upsert_session,
)
from batonpass.sync import init_channel, push

KEY = "sk-wROFcE0dUwFLKpQ2m3nBt7vXyZa5HjR1sL9gT4cV8bN2kM"      # shaped, not live
TOKEN = "pypi-AgEIcHlwaS5vcmcCJDU0YjM2ZTk4LTAwM2QtNGY1Ni1hYmNkZWYwMTIzNDU2"


class ScrubTest(unittest.TestCase):
    def test_an_openai_shaped_key_is_replaced(self):
        out, n = redact.scrub("key: " + KEY)
        self.assertEqual(1, n)
        self.assertNotIn(KEY, out)
        self.assertIn("sk-<redacted>", out)

    def test_a_pypi_token_is_replaced(self):
        out, n = redact.scrub("UV_PUBLISH_TOKEN=" + TOKEN)
        self.assertEqual(1, n)
        self.assertNotIn(TOKEN, out)

    def test_a_bearer_token_is_replaced(self):
        out, n = redact.scrub('AUTH = "Bearer mF9q2Lx7Tz0pQr5Vw8Yb1Nc4Kd6Hg3Js"')
        self.assertEqual(1, n)
        self.assertNotIn("mF9q2Lx7Tz0pQr5Vw8Yb1Nc4Kd6Hg3Js", out)
        self.assertIn("Bearer <redacted>", out)

    def test_a_github_token_is_replaced(self):
        out, n = redact.scrub("export GH=ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        self.assertEqual(1, n)
        self.assertIn("ghp_<redacted>", out)

    def test_a_private_key_block_is_replaced(self):
        out, n = redact.scrub("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n"
                              "-----END RSA PRIVATE KEY-----")
        self.assertEqual(1, n)
        self.assertNotIn("MIIEow", out)

    def test_the_prefix_survives_so_you_know_what_to_rotate(self):
        out, _ = redact.scrub(KEY)
        self.assertTrue(out.startswith("sk-"), out)

    def test_ordinary_words_are_left_alone(self):
        for text in ("sk-learn is a library，不是密钥",
                     "sk-cleanup-audit-2026-10-01 的清单",
                     "岗位表.xlsx 已生成",
                     "curl -H 'Authorization: Bearer short'",
                     ""):
            out, n = redact.scrub(text)
            self.assertEqual(0, n, text)
            self.assertEqual(text, out)

    def test_none_is_safe(self):
        self.assertEqual(("", 0), redact.scrub(""))


class TheChannelDoesNotCarryTheKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-redact-")
        self.channel = os.path.join(self.tmp, "channel")
        self.conn = connect(os.path.join(self.tmp, "lib.db"))
        codec = Codec(None)
        sid = session_key("alma", "thread-key", "alpha")
        upsert_session(self.conn, {
            "sid": sid, "source": "alma", "source_sid": "thread-key",
            "device": "alpha", "project_key": "keys", "project_key_tier": 1,
            "cwd": "/work", "title": "为什么 401", "created_at": now_iso(),
            "updated_at": now_iso(), "msg_count": 2, "first_msg_at": now_iso(),
            "last_msg_at": now_iso(), "adapter_ver": "0.5", "content_hash": "x",
        })
        for seq, role, text in (
                (0, "user", "这是我的 key：" + KEY + " 为什么一直 401？"),
                (1, "assistant", "这个 key 属于另一个区域，换 endpoint。")):
            sha, _ = put_content(self.conn, text, codec)
            put_message(self.conn, message_key(sid, seq), sid, seq, role, sha)
        self.conn.commit()
        self.sid = sid
        init_channel(self.channel, device="alpha")

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _t1_lines(self):
        path = os.path.join(self.channel, "stream", "alpha", "t1.jsonl")
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_summary_carries_the_shape_of_the_task_not_the_key(self):
        st = push(self.channel, tier=1, device="alpha", conn=self.conn)
        self.assertGreaterEqual(st["redacted"], 1)
        blob = "\n".join(json.dumps(line, ensure_ascii=False)
                         for line in self._t1_lines())
        self.assertNotIn(KEY, blob)
        self.assertIn("<redacted>", blob)
        self.assertIn("为什么一直 401", blob)

    def test_the_library_still_has_the_real_text(self):
        """Redact on the way out, never on the way in: export must be truthful."""
        push(self.channel, tier=1, device="alpha", conn=self.conn)
        row = self.conn.execute(
            "SELECT text_sha FROM message WHERE sid=? AND seq=0", (self.sid,)).fetchone()
        self.assertIn(KEY, get_content(self.conn, row[0], Codec(None)))


if __name__ == "__main__":
    unittest.main()
