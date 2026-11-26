"""Handing work between tools, without anyone in the middle.

The properties worth protecting here are not cosmetic — each one is a way two
agents in the same folder can quietly duplicate or destroy each other's work:

* a claim is a question asked of the log, so every reader gets the same answer;
* two simultaneous claims are both kept, and resolved the same way on every
  machine regardless of the order the events arrive in;
* writing the same thing twice is writing it once;
* putting the baton down does not hand it to whoever is standing closest;
* two tools naming the same directory are one place, not two.

And one upgrade rule, because it is the only part of the schema that is not
free: an old library gains the new column instead of failing to open.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

from batonpass import places
from batonpass.identity import baton
from batonpass.identity import project as pj
from batonpass.identity.tasks import task_id
from batonpass.store.content import sha256_text
from batonpass.store.db import connect

importlib.import_module("batonpass.sync.push")
importlib.import_module("batonpass.sync.pull")
push_mod = sys.modules["batonpass.sync.push"]
pull_mod = sys.modules["batonpass.sync.pull"]


class Relay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self._old = {k: os.environ.get(k) for k in ("HOME", "BATONPASS_HOME", "BATONPASS_TOOL")}
        os.environ["HOME"] = self.home
        os.environ["BATONPASS_HOME"] = os.path.join(self.home, ".batonpass")
        os.environ.pop("BATONPASS_TOOL", None)
        self.conn = connect(None)
        self.key = "demo/app"
        self.tid = task_id(self.key)
        self.conn.execute(
            "INSERT OR IGNORE INTO task(tid,project_key,title,created_at) VALUES(?,?,?,?)",
            (self.tid, self.key, "demo", "2026-01-01T00:00:00Z"))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    # ── claims ──────────────────────────────────────────────────
    def test_later_claim_wins_and_the_earlier_one_is_remembered(self):
        baton.take(self.conn, self.tid, tool="alma", note="先看一眼")
        baton.take(self.conn, self.tid, tool="dsh", note="我来写 UI")
        st = baton.state(self.conn, self.tid)

        self.assertEqual(st["holder"]["tool"], "dsh")
        self.assertEqual(st["holder"]["body"], "我来写 UI")
        self.assertEqual([d["tool"] for d in st["displaced"]], ["alma"])

    def test_simultaneous_claims_are_both_kept_and_resolved_identically(self):
        """Same lamport, two devices, inserted in opposite orders."""
        def raw(tid, device, tool):
            eid = sha256_text("%s|claim|1|%s|%s" % (tid, device, tool))[:32]
            self.conn.execute(
                "INSERT OR IGNORE INTO task_event(eid,tid,device,lamport,kind,body,"
                "created_at,tool) VALUES(?,?,?,?,?,?,?,?)",
                (eid, tid, device, 1, "claim", "抢", "2026-01-01T00:00:00Z", tool))
            self.conn.commit()

        first, second = task_id("demo/one"), task_id("demo/two")
        for tid in (first, second):
            self.conn.execute(
                "INSERT OR IGNORE INTO task(tid,project_key,title,created_at) VALUES(?,?,?,?)",
                (tid, "demo/x", "x", "2026-01-01T00:00:00Z"))
        raw(first, "aaa", "alma")
        raw(first, "zzz", "dsh")
        raw(second, "zzz", "dsh")          # same two claims, opposite order
        raw(second, "aaa", "alma")

        a, b = baton.state(self.conn, first), baton.state(self.conn, second)
        self.assertEqual(a["holder"]["tool"], b["holder"]["tool"],
                         "two machines must agree without talking")
        self.assertEqual([c["tool"] for c in a["conflicts"]], ["alma"])
        self.assertEqual(len(a["events"]), 2, "the loser must not be deleted")

    def test_release_does_not_hand_over_to_the_runner_up(self):
        baton.take(self.conn, self.tid, tool="alma")
        baton.take(self.conn, self.tid, tool="dsh")
        baton.append(self.conn, self.tid, "release", "", tool="dsh")
        self.assertIsNone(baton.state(self.conn, self.tid)["holder"])

    def test_done_frees_the_baton(self):
        baton.take(self.conn, self.tid, tool="alma")
        baton.append(self.conn, self.tid, "done", "", tool="alma")
        st = baton.state(self.conn, self.tid)
        self.assertIsNone(st["holder"])
        self.assertTrue(st["done"])

    # ── the log itself ──────────────────────────────────────────
    def test_writing_the_same_note_twice_is_writing_it_once(self):
        note = "同一个决定，重试一次不应该变成两条"
        one = baton.append(self.conn, self.tid, "note", note, tool="alma")
        two = baton.append(self.conn, self.tid, "note", note, tool="alma")
        self.assertFalse(one["duplicate"])
        self.assertTrue(two["duplicate"])
        self.assertEqual(len(baton.state(self.conn, self.tid)["events"]), 1)

    def test_next_step_is_the_latest_one_written(self):
        baton.append(self.conn, self.tid, "next", "先写测试", tool="alma")
        baton.append(self.conn, self.tid, "next", "再接 MCP", tool="dsh")
        self.assertEqual(baton.state(self.conn, self.tid)["next_step"], "再接 MCP")

    def test_a_finished_plan_is_not_still_the_plan(self):
        baton.append(self.conn, self.tid, "next", "把 MCP 两个新工具接上", tool="alma")
        baton.append(self.conn, self.tid, "done", "", tool="alma")
        self.assertEqual(baton.state(self.conn, self.tid)["next_step"], "",
                         "the person who wrote the next step is the one who "
                         "finished it; offering it again asks for it twice")
        # Putting it down unfinished is the other case: the plan is the handover.
        baton.append(self.conn, self.tid, "release", "", tool="alma")
        st = baton.state(self.conn, self.tid)
        self.assertEqual(st["next_step"], "",
                         "a release does not resurrect a plan that a done closed")
        baton.append(self.conn, self.tid, "next", "从第二台机器 pull 一次", tool="dsh")
        baton.append(self.conn, self.tid, "release", "", tool="dsh")
        self.assertEqual(baton.state(self.conn, self.tid)["next_step"],
                         "从第二台机器 pull 一次")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            baton.append(self.conn, self.tid, "telepathy", "?", tool="alma")

    # ── places ──────────────────────────────────────────────────
    def test_two_tools_naming_one_directory_are_one_place(self):
        work = os.path.join(self.home, "Documents", "Demo", "app")
        os.makedirs(work)
        os.makedirs(os.path.join(self.home, ".dsh", "storages"))
        with open(os.path.join(self.home, ".dsh", "storages", "workspace.json"), "w") as fh:
            json.dump({"tables": {"workspaces": {"w1": {"title": "demo app",
                                                        "path": work,
                                                        "sessionIds": ["a", "b"]}}}}, fh)
        # Alma keeps its workspaces in a live sqlite file; fake just that table.
        adir = os.path.join(self.home, "Library", "Application Support", "alma")
        os.makedirs(adir)
        import sqlite3 as s3
        c = s3.connect(os.path.join(adir, "chat_threads.db"))
        c.execute("CREATE TABLE workspaces (id TEXT, path TEXT, name TEXT)")
        c.execute("INSERT INTO workspaces VALUES('w9',?,'一种写法')", (work,))
        c.commit()
        c.close()

        places.refresh(self.conn, self.home)
        rows = [r for r in places.merged(self.conn, self.home) if r["path"] == work]
        self.assertEqual(len(rows), 1, "one directory is one place, whatever names it")
        self.assertEqual(sorted(rows[0]["tools"]), ["alma", "dsh"])

    def test_a_name_is_a_name_and_a_sentence_is_not(self):
        # Alma titles a workspace after the first thing typed in it, and that
        # title is what you'd see in `places` unless the folder wins.
        self.assertEqual(
            places._name("/w/winter-handdrawn-picture-books",
                         "参考上面的图，帮我做 几张 手抄绘本，关于冬天的"),
            "winter-handdrawn-picture-books")
        self.assertEqual(
            places._name("/w/operate-alma-on-mobile", "怎样使用手机 操作 这个 alma"),
            "operate-alma-on-mobile")
        # ...unless the folder names nothing at all.
        self.assertEqual(places._name("/home/me/.dsh/bot/workspace", "DS Bot"), "DS Bot")
        self.assertEqual(places._name("/w/scratch", ""), "scratch")
        self.assertEqual(places._name("/w/ad-traffic-intel-system", "ad-traffic-intel-system"),
                         "ad-traffic-intel-system")

    def test_a_path_is_written_one_way(self):
        self.assertEqual(places.norm_path("/a/b/"), "/a/b")
        self.assertEqual(places.norm_path("/a/./b//"), "/a/b")
        self.assertEqual(places.norm_path("/"), "/")
        self.assertEqual(places.norm_path("~/x"), os.path.join(self.home, "x"))
        self.assertEqual(places.norm_path("relative/dir"), "",
                         "a relative path is not a place: it means a different "
                         "directory from every different cwd")
        self.assertEqual(places.norm_path(None), "")

    def test_a_broken_tool_db_costs_only_its_own_rows(self):
        work = os.path.join(self.home, "Documents", "Demo", "app")
        os.makedirs(work)
        storages = os.path.join(self.home, ".dsh", "storages")
        os.makedirs(storages)
        with open(os.path.join(storages, "workspace.json"), "w") as fh:
            json.dump({"tables": {"workspaces": {"w1": {"title": "demo app",
                                                        "path": work}}}}, fh)
        adir = os.path.join(self.home, "Library", "Application Support", "alma")
        os.makedirs(adir)
        with open(os.path.join(adir, "chat_threads.db"), "w") as fh:
            fh.write("this is not a database")   # mid-restore, or a dead file
        places.refresh(self.conn, self.home)
        rows = [r for r in places.merged(self.conn, self.home) if r["path"] == work]
        self.assertEqual([r["tools"] for r in rows], [["dsh"]],
                         "one unreadable source must not cost the other sources")

    def test_a_workspace_whose_folder_is_gone_is_not_a_place(self):
        ghosts = os.path.join(self.home, ".dsh", "storages")
        os.makedirs(ghosts)
        with open(os.path.join(ghosts, "workspace.json"), "w") as fh:
            json.dump({"tables": {"workspaces": {"w1": {"title": "gone",
                                                        "path": os.path.join(self.home, "nope")}}}}, fh)
        places.refresh(self.conn, self.home)
        self.assertEqual([r for r in places.merged(self.conn, self.home)
                          if r["path"].endswith("/nope")], [])

    def test_half_written_source_json_does_not_break_the_listing(self):
        ghosts = os.path.join(self.home, ".dsh", "storages")
        os.makedirs(ghosts)
        with open(os.path.join(ghosts, "workspace.json"), "w") as fh:
            fh.write('{"tables": {"workspaces": {"w1": {"path"')   # mid-write
        places.refresh(self.conn, self.home)                        # must not raise
        self.assertIsInstance(places.merged(self.conn, self.home), list)

    def test_a_session_lands_where_its_tool_says_it_belongs(self):
        # dsh's session table has no cwd column at all; the workspace file is the
        # only place it says which conversations belong to which directory. Two
        # spellings of the same id (`session-<uuid>` and `<uuid>`) must both land.
        from batonpass.store.write import now_iso, session_key, upsert_session

        work = os.path.join(self.home, "luocp")
        os.makedirs(work)
        storages = os.path.join(self.home, ".dsh", "storages")
        os.makedirs(storages)
        with open(os.path.join(storages, "workspace.json"), "w") as fh:
            json.dump({"tables": {"workspaces": {"w1": {
                "title": "luocp", "path": work,
                "sessionIds": ["session-aaaa", "bbbb", "session-gone"]}}}}, fh)

        for source_sid, cwd in (("session-aaaa", None), ("bbbb", None),
                                ("cccc", "/elsewhere/already/known")):
            upsert_session(self.conn, {
                "sid": session_key("billion", source_sid, "dev"), "source": "billion",
                "source_sid": source_sid, "device": "dev", "project_key": "dsh",
                "project_key_tier": 3, "cwd": cwd, "title": source_sid,
                "created_at": now_iso(), "updated_at": now_iso(), "msg_count": 1,
                "first_msg_at": now_iso(), "last_msg_at": now_iso(),
                "adapter_ver": "0.7.0", "content_hash": "c" + source_sid,
            })
        self.conn.commit()

        self.assertEqual(places.attach(self.conn, self.home), 2)
        rows = {r["source_sid"]: (r["cwd"], r["project_key"]) for r in self.conn.execute(
            "SELECT source_sid, cwd, project_key FROM session")}
        self.assertEqual(rows["session-aaaa"][0], work)
        self.assertEqual(rows["bbbb"][0], work)
        self.assertEqual(rows["session-aaaa"][1],
                         pj.derive(work, "", self.home)[0],
                         "the session lands under the same project key the "
                         "folder itself derives, not under dsh's own namespace")
        self.assertEqual(rows["cccc"][0], "/elsewhere/already/known",
                         "a cwd a source stated itself is never replaced by a mapping")

    # ── upgrade ─────────────────────────────────────────────────
    def test_an_old_library_gains_the_tool_column(self):
        old = os.path.join(self.home, "old", "batonpass.db")
        os.makedirs(os.path.dirname(old))
        raw = sqlite3.connect(old)
        raw.execute("CREATE TABLE task_event (eid TEXT PRIMARY KEY, tid TEXT NOT NULL,"
                    " device TEXT NOT NULL, lamport INTEGER NOT NULL, kind TEXT NOT NULL,"
                    " body TEXT, refs_json TEXT, created_at TEXT NOT NULL)")
        raw.execute("INSERT INTO task_event VALUES('e1','t1','dev',1,'note','旧记录',NULL,"
                    "'2025-01-01T00:00:00Z')")
        raw.commit()
        raw.close()

        conn = connect(old)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(task_event)")}
            self.assertIn("tool", cols)
            self.assertEqual(conn.execute("SELECT body FROM task_event").fetchone()[0], "旧记录")
        finally:
            conn.close()


class TwoMachines(unittest.TestCase):
    """Two libraries, one channel, no shared database.

    This is the promise the whole design rests on: a claim made here reaches
    the other machine through a folder, and both machines then compute the same
    answer without either of them being in charge.
    """

    KEY = "demo/two-machines"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.channel = os.path.join(self.root, "channel")
        self._old = {k: os.environ.get(k)
                     for k in ("BATONPASS_HOME", "BATONPASS_DEVICE")}
        self.tid = task_id(self.KEY)
        self.conns = {}

    def tearDown(self):
        for c in self.conns.values():
            c.close()
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def machine(self, name):
        os.environ["BATONPASS_HOME"] = os.path.join(self.root, name)
        os.environ["BATONPASS_DEVICE"] = name
        conn = connect(None)
        conn.execute("INSERT OR IGNORE INTO task(tid,project_key,title,created_at)"
                     " VALUES(?,?,?,?)", (self.tid, self.KEY, "demo", "2026-01-01T00:00:00Z"))
        conn.commit()
        self.conns[name] = conn
        return conn

    def test_a_claim_crosses_the_channel_and_both_sides_agree(self):
        a = self.machine("mac-mini")
        baton.take(a, self.tid, tool="alma", note="A 侧先接")
        st = push_mod.push(self.channel, tier=0, conn=a)
        self.assertEqual(st["events"], 1)

        b = self.machine("laptop")
        got = pull_mod.pull(self.channel, tier=0, conn=b)
        self.assertEqual(got.get("events_new"), 1)
        self.assertEqual(baton.state(b, self.tid)["holder"]["body"], "A 侧先接")

        baton.take(b, self.tid, tool="dsh", note="B 侧也接了")
        push_mod.push(self.channel, tier=0, conn=b)
        got = pull_mod.pull(self.channel, tier=0, conn=a)
        self.assertEqual(got.get("events_new"), 1)

        sa, sb = baton.state(a, self.tid), baton.state(b, self.tid)
        self.assertEqual(sa["holder"]["eid"], sb["holder"]["eid"],
                         "neither machine is in charge, so both must conclude the same")
        self.assertEqual(sa["holder"]["tool"], "dsh")
        self.assertEqual([d["tool"] for d in sa["displaced"]], ["alma"])
        self.assertIn("上一位", baton.render(a, self.KEY, None, tool="alma"))

    def test_pushing_twice_sends_nothing_new(self):
        a = self.machine("mac-mini")
        baton.take(a, self.tid, tool="alma")
        self.assertEqual(push_mod.push(self.channel, tier=0, conn=a)["events"], 1)
        self.assertEqual(push_mod.push(self.channel, tier=0, conn=a)["events"], 0)

    def test_a_pulled_event_is_not_echoed_back(self):
        a = self.machine("mac-mini")
        baton.take(a, self.tid, tool="alma")
        push_mod.push(self.channel, tier=0, conn=a)
        b = self.machine("laptop")
        pull_mod.pull(self.channel, tier=0, conn=b)
        self.assertEqual(push_mod.push(self.channel, tier=0, conn=b)["events"], 0,
                         "the other side already has it")


if __name__ == "__main__":
    unittest.main()
