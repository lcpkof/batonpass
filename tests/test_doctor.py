"""`doctor` has one job: turn a broken install into a sentence you can act on.

So the tests are about sentences, not exit codes. A check that says "FAIL" and
nothing else is as useless as the traceback it replaced.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from batonpass import doctor
from batonpass.store.content import Codec, put_content
from batonpass.store.db import connect
from batonpass.store.write import message_key, now_iso, put_message, session_key, upsert_session


def tiny_library(path):
    conn = connect(path)
    codec = Codec(None)
    sid = session_key("alma", "t1", "alpha")
    upsert_session(conn, {
        "sid": sid, "source": "alma", "source_sid": "t1", "device": "alpha",
        "project_key": "~/xueji", "project_key_tier": 2, "cwd": "/work/xueji",
        "title": "x", "created_at": now_iso(), "updated_at": now_iso(),
        "msg_count": 1, "first_msg_at": now_iso(), "last_msg_at": now_iso(),
        "adapter_ver": "0.4.0", "content_hash": "x",
    })
    sha, _ = put_content(conn, "看看资料箱", codec)
    put_message(conn, message_key(sid, 0), sid, 0, "user", sha)
    conn.commit()
    return conn


def level_of(result, name):
    return [c["level"] for c in result["checks"] if c["name"] == name]


class DoctorSpeaksHuman(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-doctor-")
        self.db = os.path.join(self.tmp, "lib.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_healthy_library_is_all_green_and_changes_nothing(self):
        conn = tiny_library(self.db)
        conn.close()
        before = os.path.getsize(self.db)
        res = doctor.run(self.db)
        self.assertEqual(level_of(res, "library"), ["OK"])
        self.assertNotIn("FAIL", [c["level"] for c in res["checks"]])
        self.assertIn("Nothing was changed", doctor.render(res))
        self.assertEqual(os.path.getsize(self.db), before)

    def test_no_library_is_advice_not_a_failure(self):
        res = doctor.run(os.path.join(self.tmp, "absent.db"))
        self.assertEqual(level_of(res, "library"), [doctor.INFO])
        fix = [c["fix"] for c in res["checks"] if c["name"] == "library"][0]
        self.assertIn("archive", fix)

    def test_a_file_that_is_not_a_library_fails_with_a_next_step(self):
        with open(self.db, "wb") as fh:
            fh.write(b"this is not sqlite" * 100)
        res = doctor.run(self.db)
        self.assertEqual(level_of(res, "library"), [doctor.FAIL])
        fix = [c["fix"] for c in res["checks"] if c["name"] == "library"][0]
        self.assertTrue(fix)

    def test_a_channel_that_is_not_there_says_how_to_make_one(self):
        res = doctor.run(None, channel=os.path.join(self.tmp, "nope"))
        chk = [c for c in res["checks"] if c["name"] == "channel"][0]
        self.assertEqual(chk["level"], doctor.FAIL)
        self.assertIn("sync init", chk["fix"])

    def test_python_and_sqlite_are_reported_with_versions(self):
        res = doctor.run(self.db)
        for name in ("python", "sqlite"):
            self.assertEqual(level_of(res, name), ["OK"])
        detail = [c["detail"] for c in res["checks"] if c["name"] == "sqlite"][0]
        self.assertIn("trigram", detail)

    def test_a_stale_index_is_named_rather_than_hidden(self):
        conn = tiny_library(self.db)
        conn.close()
        res = doctor.run(self.db)
        # never indexed: doctor has to say so, not pretend the library is complete
        self.assertIn(doctor.INFO, level_of(res, "index"))