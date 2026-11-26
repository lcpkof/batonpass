"""Slimming: what leaves, what never leaves, and how it comes back.

The thing under test is not "does it delete bytes" — anyone can delete bytes.
It is the promise around it: prose is never a candidate, a pointer that survives
a purge still resolves after a re-archive, and a session that was slimmed can be
re-read from the tool that wrote it.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from batonpass.archive.runner import archive_one
from batonpass.recall.query import search
from batonpass.rehydrate import fetch
from batonpass.slim import apply, plan, savings
from batonpass.store.codecs import Codecs
from batonpass.store.codecs import Codecs as _Codecs
from batonpass.store.content import PLAIN, PURGED, is_purged, plain_prose, put_content
from batonpass.store.db import connect
from batonpass.store.write import (message_key, now_iso, put_message, put_part,
                                   session_key, upsert_session)


def _lib(tmp) -> object:
    return connect(os.path.join(tmp, "library.db"))


def _session(conn, source="billion", source_sid="s1", device="d1", text="说了句话"):
    sid = session_key(source, source_sid, device)
    upsert_session(conn, {
        "sid": sid, "source": source, "source_sid": source_sid, "device": device,
        "project_key": "~/xueji", "project_key_tier": 2, "cwd": "/work/xueji",
        "title": "资料箱", "created_at": now_iso(), "updated_at": now_iso(),
        "msg_count": 1, "first_msg_at": now_iso(), "last_msg_at": now_iso(),
        "adapter_ver": "0.9.0", "content_hash": "x",
    })
    return sid


def _message_with_parts(conn, sid, seq, said="", parts=(), ts="2026-01-01T00:00:00Z"):
    codec = Codecs(conn).for_source("billion")
    tsha = put_content(conn, said, codec)[0] if said else ""
    mid = message_key(sid, seq)
    put_message(conn, mid, sid, seq, "assistant" if not said else "user", tsha,
                None, ts, '{"kind":"text","identity":"%s"}' % ("a" * 64))
    for i, (kind, body) in enumerate(parts):
        sha = put_content(conn, body, codec)[0] if body else None
        put_part(conn, "%s-%d" % (mid[:24], i), mid, i, kind,
                 "run_code" if kind == "tool_call" else None,
                 sha if kind == "tool_call" else None,
                 sha if kind != "tool_call" else None, 0, ts)
    return mid


def _billion_source(path, n_tools=3):
    """A tiny stand-in for billion's own database: sessions/messages/content."""
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE sessions(session_id TEXT, title TEXT, created_at TEXT);
      CREATE TABLE messages(session_id TEXT, seq INT, role TEXT, kind TEXT,
                            ts TEXT, content_hash TEXT, identity TEXT);
      CREATE TABLE content(hash TEXT PRIMARY KEY, bytes BLOB, byte_len INT);
    """)
    conn.execute("INSERT INTO sessions VALUES('s1','资料箱','2026-01-01T00:00:00Z')")
    rows = [(0, "user", "text", "把客户编号改成新的"),
            (1, "assistant", "reasoning", "先看字段映射，再改导入脚本"),
            (2, "assistant", "tool-call", '{"cmd": "pytest -q"}')]
    for i in range(n_tools):
        rows.append((3 + i, "tool", "tool-result", "FAILED test_%d — 18 个用例里 1 个挂了" % i))
    for seq, role, kind, body in rows:
        h = "h%d" % seq
        conn.execute("INSERT INTO content VALUES(?,?,?)", (h, body.encode(), len(body.encode())))
        conn.execute("INSERT INTO messages VALUES('s1',?,?,?,?,?,?)",
                     (seq, role, kind, "2026-01-01T00:00:%02dZ" % seq, h, "id%d" % seq))
    conn.commit()
    conn.close()


class SlimTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── the promise ────────────────────────────────────────────
    def test_prose_is_never_a_candidate(self):
        conn = _lib(self.tmp)
        sid = _session(conn)
        _message_with_parts(conn, sid, 0, said="这句话不能动",
                            parts=[("tool_result", "命令输出 " * 40)])
        p = plan(conn, tool_days=0, reasoning_days=0)
        shas = set(p["purge"]["shas"])
        prose_sha = conn.execute("SELECT text_sha FROM message WHERE sid=?", (sid,)).fetchone()[0]
        self.assertNotIn(prose_sha, shas, "正文被列进了要移出的名单")
        apply(conn, p)
        self.assertEqual(Codecs(conn).get(prose_sha, "billion"), "这句话不能动")
        conn.close()

    def test_a_sha_shared_with_prose_is_left_alone(self):
        conn = _lib(self.tmp)
        sid = _session(conn)
        shared = "同一段文字既当正文又当工具输出"
        _message_with_parts(conn, sid, 0, said=shared, parts=[("tool_result", shared)])
        p = plan(conn, tool_days=0, reasoning_days=0)
        self.assertEqual(p["purge"]["blobs"], 0)
        self.assertEqual(p["purge"]["kept_because_shared_with_prose"], 1)
        conn.close()

    def test_only_old_bodies_leave(self):
        conn = _lib(self.tmp)
        sid = _session(conn)
        _message_with_parts(conn, sid, 0, parts=[("tool_result", "旧输出" * 200)],
                            ts="2020-01-01T00:00:00Z")
        _message_with_parts(conn, sid, 1, parts=[("tool_result", "新输出" * 200)],
                            ts=now_iso())
        p = plan(conn, tool_days=30, reasoning_days=30)
        self.assertEqual(p["purge"]["blobs"], 1, "30 天窗口应该只挑走旧的那块")
        apply(conn, p)
        rows = conn.execute("SELECT codec FROM content WHERE codec=? OR byte_len>300"
                            " ORDER BY byte_len", (PURGED,)).fetchall()
        self.assertIn(PURGED, [r[0] for r in rows])
        fresh = conn.execute("SELECT sha256 FROM content WHERE codec<>? AND byte_len>300",
                             (PURGED,)).fetchone()
        self.assertTrue(Codecs(conn).get(fresh[0], "billion").startswith("新输出"))
        conn.close()

    def test_dry_run_is_a_read(self):
        conn = _lib(self.tmp)
        sid = _session(conn)
        _message_with_parts(conn, sid, 0, parts=[("tool_result", "x" * 500)])
        before = conn.execute("SELECT COUNT(*), SUM(byte_len) FROM content").fetchone()
        p = plan(conn, tool_days=0, reasoning_days=0)
        savings(conn, p)
        after = conn.execute("SELECT COUNT(*), SUM(byte_len) FROM content").fetchone()
        self.assertEqual(tuple(before), tuple(after))
        conn.close()

    # ── folding ────────────────────────────────────────────────
    def test_fold_keeps_the_count_not_the_rows(self):
        conn = _lib(self.tmp)
        sid = _session(conn)
        mid = _message_with_parts(conn, sid, 0,
                                  parts=[("reasoning", "想第 %d 步" % i) for i in range(6)])
        p = plan(conn, tool_days=0, reasoning_days=0)
        apply(conn, p)
        rows = conn.execute("SELECT COUNT(*), SUM(n) FROM part WHERE mid=?", (mid,)).fetchone()
        self.assertEqual(rows[0], 1, "六步同样的推理应该收成一行")
        self.assertEqual(rows[1], 6, "次数不能丢")
        row = conn.execute("SELECT args_sha, result_sha FROM part WHERE mid=?", (mid,)).fetchone()
        self.assertEqual((row[0], row[1]), (None, None))
        conn.close()

    def test_fold_refuses_when_a_body_is_still_here(self):
        conn = _lib(self.tmp)
        sid = _session(conn)
        mid = _message_with_parts(conn, sid, 0, ts=now_iso(),
                                  parts=[("reasoning", "还留着的推理 %d" % i) for i in range(4)])
        p = plan(conn, tool_days=30, reasoning_days=30)   # 全是今天写的
        self.assertEqual(p["fold"]["rows_now"], p["fold"]["rows_after"])
        apply(conn, p)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM part WHERE mid=?", (mid,)).fetchone()[0], 4)
        conn.close()

    # ── the pointer still works ────────────────────────────────
    def test_writing_the_same_text_again_revives_it(self):
        conn = _lib(self.tmp)
        codec = Codecs(conn).for_source("billion")
        sha, _ = put_content(conn, "一条工具输出", codec)
        with conn:
            conn.execute("UPDATE content SET body=x'', codec=? WHERE sha256=?", (PURGED, sha))
        self.assertTrue(is_purged(conn, sha))
        self.assertEqual(Codecs(conn).get(sha, "billion"), "")
        put_content(conn, "一条工具输出", codec)
        self.assertFalse(is_purged(conn, sha))
        self.assertEqual(Codecs(conn).get(sha, "billion"), "一条工具输出")
        conn.close()

    # ── reading it back from the tool's own directory ─────────
    def test_fetch_replays_the_session_from_the_source(self):
        src = os.path.join(self.tmp, "billion.db")
        _billion_source(src, n_tools=3)
        conn = _lib(self.tmp)
        res = archive_one(conn, "billion", src, "d1", level=1)
        self.assertEqual(res.messages, 6, "夹具有 6 条消息")
        sid = conn.execute("SELECT sid FROM session").fetchone()[0]

        p = plan(conn, tool_days=0, reasoning_days=0)
        self.assertGreaterEqual(p["purge"]["blobs"], 4)
        apply(conn, p)
        gone = conn.execute("SELECT COUNT(*) FROM content WHERE codec=?", (PURGED,)).fetchone()[0]
        self.assertGreaterEqual(gone, 4)

        out = fetch(conn, sid[:8], path=src)
        self.assertEqual(out["sessions"], 1)
        self.assertGreaterEqual(out["restored_parts"], 4)
        self.assertEqual(out["still_pointer"], 0)
        texts = [Codecs(conn).get(r[0], "billion") for r in conn.execute(
            "SELECT result_sha FROM part WHERE kind='tool_result' AND result_sha IS NOT NULL")]
        self.assertTrue(any("FAILED test_0" in t for t in texts), "工具输出应该读回来了")
        conn.close()

    def test_fetch_says_so_when_the_source_forgot_it(self):
        src = os.path.join(self.tmp, "billion.db")
        _billion_source(src)
        conn = _lib(self.tmp)
        archive_one(conn, "billion", src, "d1", level=1)
        sid = conn.execute("SELECT sid FROM session").fetchone()[0]
        apply(conn, plan(conn, tool_days=0, reasoning_days=0))

        empty = os.path.join(self.tmp, "empty.db")
        c = sqlite3.connect(empty)
        c.executescript("CREATE TABLE sessions(session_id TEXT, title TEXT, created_at TEXT);"
                        "CREATE TABLE messages(session_id TEXT, seq INT, role TEXT, kind TEXT,"
                        " ts TEXT, content_hash TEXT, identity TEXT);"
                        "CREATE TABLE content(hash TEXT PRIMARY KEY, bytes BLOB, byte_len INT);")
        c.commit()
        c.close()
        out = fetch(conn, sid[:8], path=empty)
        self.assertEqual(out["sessions"], 0)
        self.assertEqual(len(out["missing"]), 1)
        self.assertIn("没有这条会话", out["missing"][0]["why"])
        conn.close()

    # ── the bill ───────────────────────────────────────────────
    def test_savings_counts_compressed_bytes(self):
        conn = _lib(self.tmp)
        sid = _session(conn)
        _message_with_parts(conn, sid, 0, parts=[("tool_result", "重复的输出 " * 2000)])
        p = plan(conn, tool_days=0, reasoning_days=0)
        s = savings(conn, p)
        self.assertGreater(s["net"], 0)
        plain = conn.execute("SELECT SUM(byte_len) FROM content WHERE codec<>?",
                             (PURGED,)).fetchone()[0]
        self.assertLess(s["bodies"], plain, "算的是压缩后的字节，不是原文")
        conn.close()

    def test_meta_identity_dropped_kind_kept(self):
        conn = _lib(self.tmp)
        sid = _session(conn)
        mid = _message_with_parts(conn, sid, 0, said="正文")
        p = plan(conn, tool_days=30, reasoning_days=30)
        self.assertEqual(p["meta"]["rows"], 1)
        apply(conn, p)
        meta = conn.execute("SELECT meta_json FROM message WHERE mid=?", (mid,)).fetchone()[0]
        self.assertNotIn("identity", meta)
        self.assertIn("text", meta)
        conn.close()


class SearchAfterSlimTest(unittest.TestCase):
    """Search is why prose is kept at all, so it is tested against a slimmed library."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _one(self, said):
        conn = _lib(self.tmp)
        sid = _session(conn, text=said)
        _message_with_parts(conn, sid, 0, said=said,
                            parts=[("tool_result", "输出 " * 100)])
        plain_prose(conn, _Codecs(conn))
        return conn, sid

    def test_a_phrase_in_the_middle_of_a_message_is_found(self):
        said = "前面一堆背景说明，然后 客户编号 又要改回去，后面还有别的话。"
        conn, sid = self._one(said)
        hits = search(conn, "客户编号")
        self.assertEqual([h["sid"] for h in hits], [sid])
        self.assertIn("客户编号", hits[0]["snippet"])
        conn.close()

    def test_compressed_prose_is_still_searchable(self):
        """A peer's library, or one nobody slimmed yet: the fallback must hold."""
        said = "从别的机器同步过来的 岗位表 记录"
        conn, sid = self._one(said)
        codec = _Codecs(conn).for_source("billion")
        body, name = codec.compress(said.encode())
        conn.execute("UPDATE content SET body=?, codec=? WHERE sha256=?", (body, name, sid))
        conn.execute("""UPDATE content SET body=?, codec=? WHERE sha256 IN
                        (SELECT text_sha FROM message WHERE sid=?)""", (body, name, sid))
        conn.commit()
        hits = search(conn, "岗位表")
        self.assertEqual([h["sid"] for h in hits], [sid])
        conn.close()

    def test_plain_prose_keeps_sha_and_byte_len(self):
        said = "这句话的 sha 不能变" * 20
        conn = _lib(self.tmp)
        sid = _session(conn, text=said)
        _message_with_parts(conn, sid, 0, said=said)
        before = conn.execute("""SELECT c.sha256, c.byte_len FROM content c
                                   JOIN message m ON m.text_sha=c.sha256""").fetchone()
        out = plain_prose(conn, _Codecs(conn))
        self.assertEqual(out["rows"], 1)
        after = conn.execute("""SELECT c.sha256, c.byte_len, c.codec FROM content c
                                  JOIN message m ON m.text_sha=c.sha256""").fetchone()
        self.assertEqual((before[0], before[1]), (after[0], after[1]))
        self.assertEqual(after[2], PLAIN)
        self.assertEqual(_Codecs(conn).get(after[0], "billion"), said)
        self.assertEqual(plain_prose(conn, _Codecs(conn))["rows"], 0, "再跑一次不该动任何东西")
        conn.close()

    def test_slim_says_what_plain_prose_costs(self):
        conn = _lib(self.tmp)
        sid = _session(conn, text="正文" * 300)
        _message_with_parts(conn, sid, 0, said="正文" * 300)
        p = plan(conn, tool_days=30, reasoning_days=30)
        self.assertEqual(p["plain"]["rows"], 1)
        self.assertGreater(p["plain"]["bytes"], 0)
        s = savings(conn, p)
        self.assertEqual(s["plain"], p["plain"]["bytes"])
        conn.close()


if __name__ == "__main__":
    unittest.main()