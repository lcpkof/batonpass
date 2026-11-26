"""Identity and recall, on libraries small enough to reason about.

The properties worth protecting, in the order they broke while building this:

* the same folder on two machines is one project — not two;
* a tool's own scratch directory is *not* a project;
* two projects that happen to share a leaf name are not merged by accident;
* a filename glued to Chinese prose is recovered, and a filename that is really
  a sentence is not invented;
* `find` says "nothing" instead of guessing.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from batonpass.identity import artifacts as af
from batonpass.identity import build, card, projects
from batonpass.identity.project import UNKNOWN, derive, display
from batonpass.recall import build_index, search
from batonpass.recall.query import parse_window
from batonpass.store.content import Codec, put_content
from batonpass.store.db import connect
from batonpass.store.write import (
    message_key, now_iso, part_key, put_message, put_part, session_key, upsert_session,
)


def make_session(conn, codec, sid_tail, cwd, title, turns, device="alpha",
                 when=None):
    """One session with a user turn per string in `turns`."""
    when = when or now_iso()
    sid = session_key("alma", sid_tail, device)
    upsert_session(conn, {
        "sid": sid, "source": "alma", "source_sid": sid_tail, "device": device,
        "cwd": cwd, "title": title, "created_at": when, "updated_at": when,
        "msg_count": len(turns), "first_msg_at": when, "last_msg_at": when,
        "adapter_ver": "0.3.0", "content_hash": "h-" + sid_tail,
    })
    for i, text in enumerate(turns):
        mid = message_key(sid, i)
        sha, _ = put_content(conn, text, codec)
        put_message(conn, mid, sid, i, "user" if i % 2 == 0 else "assistant", sha, ts=when)
        psha, _ = put_content(conn, text, codec)
        put_part(conn, part_key(mid, 0), mid, 0, "text", ts=when)
    return sid


class PathIdentityTest(unittest.TestCase):
    def test_same_folder_on_three_machines_is_one_project(self):
        keys = {
            derive("/Users/lcpkof/Documents/xueji")[0],
            derive("/home/luo/projects/xueji")[0],
            derive("C:/Users/c/Desktop/xueji")[0],
        }
        self.assertEqual(keys, {"~/xueji"})
        self.assertEqual(display("~/xueji"), "xueji")

    def test_subdirectory_belongs_to_the_parent(self):
        self.assertEqual(derive("/Users/a/Documents/xueji/frontend")[0], "~/xueji")

    def test_tool_scratch_is_not_a_project(self):
        self.assertEqual(derive("/Users/a/.zcode/workspace/default")[0], UNKNOWN)
        self.assertEqual(derive("/Users/a/Documents")[0], UNKNOWN)
        self.assertEqual(derive("")[0], UNKNOWN)

    def test_shared_leaf_names_are_not_merged(self):
        a = derive("/Users/a/Documents/gp/stock_picker")[0]
        b = derive("/Users/a/work/gp/stock_picker")[0]
        self.assertEqual(a, b)          # same leaf, both parents container-free
        self.assertNotEqual(derive("/Users/a/Documents/gp")[0], a)

    def test_git_remote_outranks_path(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, ".git"))
            with open(os.path.join(tmp, ".git", "config"), "w") as fh:
                fh.write('[remote "origin"]\n\turl = git@github.com:Lcpkof/xueji.git\n')
            key, tier, conf = derive(tmp)
            self.assertEqual(key, "github.com/lcpkof/xueji")
            self.assertEqual((tier, conf), (1, "high"))
        finally:
            shutil.rmtree(tmp)


class ArtifactTest(unittest.TestCase):
    def test_prose_glued_to_a_filename(self):
        self.assertEqual(af.names_in_text("看能否上传 cover.png"), ["cover.png"])
        self.assertEqual(af.names_in_text("603446 于 trades.csv"), ["trades.csv"])
        self.assertIn("岗位表 2026.xlsx", af.names_in_text("把 岗位表 2026.xlsx 发我"))

    def test_sentences_are_not_filenames(self):
        self.assertEqual(af.names_in_text("or fix an existing .xlsx"), [])
        self.assertEqual(af.names_in_text("streamlit.components.v1.html"), [])

    def test_two_filenames_joined_by_a_space_are_not_one(self):
        # `state.json trades.csv` is a pair, and the merged string is neither.
        self.assertEqual(af.names_in_text("禁止修改 state.json trades.csv"), [])

    def test_a_relational_word_splits_a_pair(self):
        self.assertEqual(af.names_in_text("加 card-1 至 card-5.png"), ["card-5.png"])
        self.assertEqual(
            af.names_in_text("更新 岗位表 2026.xlsx 和 汇总表 2026.xlsx"),
            ["岗位表 2026.xlsx", "汇总表 2026.xlsx"])

    def test_repo_furniture_is_ignored(self):
        self.assertEqual(af.names_in_text("见 README.md 与 pyproject.toml"), [])
        self.assertEqual(af.names_in_text("打开 CLAUDE.md"), [])

    def test_markdown_link_and_path_yield_the_basename(self):
        self.assertEqual(af.names_in_text("全文在 [2026-11-18.md](/Users/a/n/2026-11-18.md)"),
                         ["2026-11-18.md"])

    def test_mention_order_is_kept_and_last_mention_wins(self):
        old = json.dumps([{"name": "岗位表-旧.xlsx", "ts": "2026-11-01T00:00:00Z",
                           "via": "text"}])
        new = [{"name": "岗位表.xlsx", "ts": "2026-11-04T00:00:00Z", "via": "tool"}]
        merged = json.loads(af.merge_refs(old, new))
        self.assertEqual(merged[0]["name"], "岗位表.xlsx")

    def test_a_spreadsheet_outranks_a_markdown_note(self):
        refs = [{"name": "notes.md", "ts": "2026-11-04T00:00:00Z", "via": "text"},
                {"name": "岗位表.xlsx", "ts": "2026-11-01T00:00:00Z", "via": "text"}]
        names = [r["name"] for r in sorted(
            refs, key=lambda d: (af.rank(d["name"]), d["ts"]), reverse=True)]
        self.assertEqual(names[0], "岗位表.xlsx")


class WindowTest(unittest.TestCase):
    def test_windows(self):
        since, until = parse_window("7d")
        self.assertTrue(since.endswith("Z") and until is None)
        since, until = parse_window("2026-09")
        self.assertEqual((since, until), ("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z"))
        since, until = parse_window("2026-09-01..2026-09-20")
        self.assertEqual(since, "2026-09-01T00:00:00Z")
        self.assertEqual(until, "2026-09-21T00:00:00Z")
        self.assertEqual(parse_window("garbage"), (None, None))


class CliErrorTest(unittest.TestCase):
    def test_an_unreadable_library_is_one_line_and_a_bad_status(self):
        import contextlib
        import io
        from batonpass.cli import main
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["stats", "--db", "/nonexistent/batonpass/x.db"])
        self.assertEqual(rc, 2)
        self.assertTrue(err.getvalue().startswith("batonpass: "), err.getvalue())
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "library.db")
        self.conn = connect(self.db)
        self.codec = Codec(None)
        self.sid_a = make_session(
            self.conn, self.codec, "xueji-1", "/Users/a/Documents/xueji", "资料箱优化",
            ["帮我看看资料箱", "好的，三个问题：拼写、索引、权限",
             "把 岗位表 2026.xlsx 保存到 documents"])
        self.sid_b = make_session(
            self.conn, self.codec, "gp-1", "/Users/a/Documents/GP", "妖股周复盘",
            ["这周复盘一下", "复盘已写进 2026-11-18.md"])

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp)

    def test_a_pulled_project_key_survives_a_local_rebuild(self):
        # The session came from another machine: no local cwd to derive from, but
        # t0 brought the answer. Rebuilding here must not erase it.
        sid = make_session(self.conn, self.codec, "win-1", None, "xueji 上继续",
                           ["接着做资料箱"], device="windows-box")
        self.conn.execute("UPDATE session SET project_key='~/xueji', project_key_tier=2 "
                          "WHERE sid=?", (sid,))
        self.conn.commit()
        st = build(self.conn, extract_artifacts=False)
        key = self.conn.execute(
            "SELECT project_key FROM session WHERE sid=?", (sid,)).fetchone()[0]
        self.assertEqual(key, "~/xueji")
        self.assertEqual(st["kept_remote"], 1)
        self.assertEqual(st["unknown"], 0)

    def test_a_local_guess_is_still_allowed_to_be_wrong(self):
        # Same shape, but the key was ours: an empty derivation means ungrouped,
        # so the old key goes. Silence beats a confident mistake.
        from batonpass.archive.runner import device_name
        sid = make_session(self.conn, self.codec, "local-1", None, "随手一问",
                           ["今天几号"], device=device_name())
        self.conn.execute("UPDATE session SET project_key='~/ghost', project_key_tier=2 "
                          "WHERE sid=?", (sid,))
        self.conn.commit()
        st = build(self.conn, extract_artifacts=False)
        key = self.conn.execute(
            "SELECT project_key FROM session WHERE sid=?", (sid,)).fetchone()[0]
        self.assertIsNone(key)
        self.assertEqual(st["kept_remote"], 0)

    def test_two_folders_two_projects(self):
        build(self.conn)
        keys = {p["project_key"] for p in projects(self.conn)}
        self.assertEqual(keys, {"~/xueji", "~/gp"})

    def test_card_shows_the_deliverable_and_the_last_word(self):
        build(self.conn)
        text = card(self.conn, "~/xueji", 2)
        self.assertIn("岗位表 2026.xlsx", text)
        self.assertIn("资料箱", text)
        self.assertIn("个会话", text)

    def test_the_last_word_is_the_last_one_that_said_anything(self):
        # A turn that only called tools has no prose of its own — dsh stores one
        # row per kind — so the newest assistant row can be blank. A card that
        # prints its timestamp and then nothing reads as truncated; walk back.
        mid = message_key(self.sid_b, 90)
        sha, _ = put_content(self.conn, "", self.codec)
        put_message(self.conn, mid, self.sid_b, 90, "assistant", sha, ts=now_iso())
        self.conn.commit()
        build(self.conn)
        text = card(self.conn, "~/gp", 2)
        self.assertIn("复盘已写进 2026-11-18.md", text)

    def test_subproject_folds_into_parent(self):
        for i in range(6):
            make_session(self.conn, self.codec, "big-%d" % i, "/Users/a/Documents/big",
                         "big %d" % i, ["turn"])
        make_session(self.conn, self.codec, "small-1", "/Users/a/Documents/big/tools",
                     "small", ["turn"])
        build(self.conn)
        keys = {p["project_key"] for p in projects(self.conn)}
        self.assertIn("~/big", keys)
        self.assertNotIn("big/tools", keys)

    def test_find_is_silent_when_it_does_not_know(self):
        empty = connect(os.path.join(self.tmp, "empty.db"))
        try:
            build_index(empty)
            self.assertEqual(search(empty, "岗位表"), [])
            self.assertEqual(search(empty, "什么也没有"), [])
        finally:
            empty.close()

    def test_find_hits_by_body_and_project(self):
        build(self.conn)
        build_index(self.conn, force=True)
        hits = search(self.conn, "资料箱")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["sid"], self.sid_a)
        self.assertTrue(any("body" in (h.get("why") or ["body"]) for h in hits))
        # every hit belongs to the window it was asked about
        self.assertEqual(search(self.conn, "复盘", at="2026-09"), [])
        self.assertNotEqual(search(self.conn, "资料箱", at="7d"), [])


if __name__ == "__main__":
    unittest.main()
