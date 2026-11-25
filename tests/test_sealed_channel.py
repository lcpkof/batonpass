"""A channel on somebody else's disk must not be a transcript.

The blobs were sealed first, and that felt like enough until the numbers were
real: 716 summarised conversations is 2.7 MB of prose, and the tier that has to
travel — through iCloud, through a NAS, through a stick you might lose — is
exactly that one. So the lines are sealed too, and what stays readable is only
enough to tell two facts apart.

Three things are being protected here:

* the prose does not appear in the file, in any encoding;
* the key holder gets the same result as before, byte for byte;
* a wrong key says so, instead of looking like an empty channel.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr

from batonpass.store.db import connect
from batonpass.sync import init_channel, pull, push
from batonpass.sync import channel as ch
from batonpass.sync import crypt

from test_sync import make_library

KEY = "correct-horse-battery-staple-2026"


def crypto_available():
    try:
        return crypt.available()
    except Exception:
        return False


@unittest.skipUnless(crypto_available(), 'needs the crypto extra')
class SealedChannelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-sealed-")
        self.channel = os.path.join(self.tmp, "channel")
        self.a, self.sid = make_library(os.path.join(self.tmp, "a.db"))
        init_channel(self.channel, device="alpha")
        self._prev = os.environ.get("BATONPASS_KEY")
        os.environ["BATONPASS_KEY"] = KEY
        crypt.enable(self.channel)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("BATONPASS_KEY", None)
        else:
            os.environ["BATONPASS_KEY"] = self._prev
        try:
            self.a.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stream(self, tier):
        path = os.path.join(self.channel, "stream", "alpha", "%s.jsonl" % tier)
        with open(path, "rb") as fh:
            return fh.read()

    def test_the_prose_is_not_in_the_file(self):
        push(self.channel, tier=1, device="alpha", conn=self.a)
        raw = self._stream("t1")
        self.assertNotIn("接力棒".encode("utf-8"), raw)      # the title
        self.assertNotIn("记忆中枢".encode("utf-8"), raw)     # the ask
        self.assertNotIn(b"archive", raw)                    # a body word
        line = json.loads(raw.decode("utf-8").splitlines()[0])
        self.assertEqual(line.get("se"), "bpc1")
        self.assertTrue(line.get("d"))                        # ciphertext
        self.assertEqual(line.get("t"), 1)
        self.assertTrue(line.get("sid"))                      # identity stays public

    def test_the_key_holder_reads_it_as_before(self):
        push(self.channel, tier=1, device="alpha", conn=self.a)
        b_path = os.path.join(self.tmp, "b.db")
        b = connect(b_path)
        try:
            stat = pull(self.channel, tier=1, device="beta", conn=b)
            self.assertEqual(stat["sessions_new"], 1)
            self.assertEqual(stat["sessions_new"], 1)
            row = b.execute("SELECT s.title, h.one_line, h.head_json FROM session s"
                            " JOIN session_head h USING (sid)").fetchone()
            self.assertEqual(row[0], "接力棒")
            self.assertIn("记忆中枢", row[1])
            self.assertIn("好，先写 archive", row[2])
        finally:
            b.close()

    def test_tier_two_bodies_survive_the_round_trip(self):
        push(self.channel, tier=2, device="alpha", conn=self.a)
        b = connect(os.path.join(self.tmp, "c.db"))
        try:
            pull(self.channel, tier=2, device="beta", conn=b)
            self.assertEqual(b.execute("SELECT COUNT(*) FROM message").fetchone()[0], 3)
            sha = b.execute("SELECT text_sha FROM message ORDER BY seq").fetchone()[0]
            from batonpass.store.content import Codec, get_content
            self.assertIn("记忆中枢", get_content(b, sha, Codec(None)))
        finally:
            b.close()

    def test_a_wrong_key_says_so(self):
        push(self.channel, tier=1, device="alpha", conn=self.a)
        os.environ["BATONPASS_KEY"] = "not-the-key"
        b = connect(os.path.join(self.tmp, "d.db"))
        try:
            with self.assertRaises(RuntimeError) as ctx:
                pull(self.channel, tier=1, device="beta", conn=b)
            self.assertIn("key does not match", str(ctx.exception))
        finally:
            b.close()

    def test_only_the_identity_of_a_fact_stays_public(self):
        push(self.channel, tier=1, device="alpha", conn=self.a)
        line = json.loads(self._stream("t1").decode("utf-8").splitlines()[0])
        leaked = set(line) - {"v", "t", "sid", "se", "d"}
        self.assertEqual(leaked, set(), "unexpected plaintext field on a sealed line")


class UnsealedChannelTest(unittest.TestCase):
    """The default has to stay readable: sealing is a choice, not a side effect."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-plain-")
        self.channel = os.path.join(self.tmp, "channel")
        self.a, self.sid = make_library(os.path.join(self.tmp, "a.db"))
        init_channel(self.channel, device="alpha")

    def tearDown(self):
        try:
            self.a.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_plain_channel_is_still_plain_json(self):
        push(self.channel, tier=1, device="alpha", conn=self.a)
        with open(os.path.join(self.channel, "stream", "alpha", "t1.jsonl"),
                  encoding="utf-8") as fh:
            line = json.loads(fh.readline())
        self.assertNotIn("se", line)
        self.assertIn("head", line)


if __name__ == "__main__":          # pragma: no cover
    unittest.main()
