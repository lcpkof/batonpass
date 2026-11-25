"""A sealed channel must still behave exactly like an unsealed one.

The failure that matters is not "the attacker reads it" — it's "the honest
machine can no longer open its own archive", or worse, "a wrong key looks like a
corrupt file". Both are tested here by name.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from batonpass.sync import channel as ch
from batonpass.sync import crypt, wire

crypto_ok = crypt.available()


def slurp(path, binary=True):
    if binary:
        with open(path, "rb") as fh:
            return fh.read()
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def sealed_channel(path, passphrase="correct horse battery"):
    ch.init_channel(path, device="alpha")
    os.environ["BATONPASS_KEY"] = passphrase
    crypt.enable(path)
    return path


@unittest.skipUnless(crypto_ok, 'needs pip install "batonpass-cli[crypto]"')
class SealedChannel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-crypt-")
        self.dir = os.path.join(self.tmp, "ch")
        self._env = os.environ.get("BATONPASS_KEY")
        self._home = os.environ.get("HOME")

    def tearDown(self):
        for name, value in (("BATONPASS_KEY", self._env), ("HOME", self._home)):
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_payload_round_trips_and_the_file_on_disk_does_not_contain_it(self):
        sealed_channel(self.dir)
        text = "岗位表 · the column names are 岗位,城市,薪资".encode("utf-8")
        sha, size, new = wire.put_blob(self.dir, text)
        self.assertTrue(new)
        self.assertGreater(size, 0)
        self.assertEqual(wire.get_blob(self.dir, sha), text)

        raw = slurp(wire.blob_path(self.dir, sha))
        self.assertTrue(raw.startswith(crypt.MAGIC))
        self.assertNotIn("岗位表".encode("utf-8"), raw)

    def test_the_same_plaintext_lands_on_the_same_path_twice(self):
        sealed_channel(self.dir)
        first = wire.put_blob(self.dir, b"one conversation")
        second = wire.put_blob(self.dir, b"one conversation")
        self.assertEqual(first[0], second[0])
        self.assertFalse(second[2])          # already there: idempotency survives

    def test_a_wrong_passphrase_says_so_instead_of_looking_corrupt(self):
        sealed_channel(self.dir)
        sha = wire.put_blob(self.dir, b"a message")[0]
        os.environ["BATONPASS_KEY"] = "the wrong one"
        with self.assertRaises(RuntimeError) as ctx:
            wire.get_blob(self.dir, sha)
        self.assertIn("encrypted", str(ctx.exception))

    def test_no_passphrase_anywhere_is_one_actionable_sentence(self):
        sealed_channel(self.dir)
        sha = wire.put_blob(self.dir, b"a message")[0]
        os.environ.pop("BATONPASS_KEY", None)
        os.environ["HOME"] = self.tmp          # no ~/.batonpass/key to fall back on
        with self.assertRaises(RuntimeError) as ctx:
            wire.get_blob(self.dir, sha)
        self.assertIn("BATONPASS_KEY", str(ctx.exception))

    def test_enabling_on_a_channel_that_already_has_blobs_seals_them(self):
        ch.init_channel(self.dir, device="alpha")
        sha, _, _ = wire.put_blob(self.dir, b"written before the channel was sealed")
        self.assertFalse(slurp(wire.blob_path(self.dir, sha))[:4] == crypt.MAGIC)

        os.environ["BATONPASS_KEY"] = "correct horse battery"
        res = crypt.enable(self.dir)
        self.assertEqual(res["sealed"], 1)
        self.assertTrue(slurp(wire.blob_path(self.dir, sha))[:4] == crypt.MAGIC)
        self.assertEqual(wire.get_blob(self.dir, sha), b"written before the channel was sealed")

    def test_the_key_is_not_stored_in_the_channel(self):
        sealed_channel(self.dir)
        text = "".join(slurp(os.path.join(self.dir, f), binary=False)
                       for f in os.listdir(self.dir) if f.endswith(".json"))
        self.assertNotIn("correct horse battery", text)


class PlainChannelStillPlain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="batonpass-plain-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_encryption_means_zlib_and_two_tools(self):
        ch.init_channel(self.tmp, device="alpha")
        sha, _, _ = wire.put_blob(self.tmp, b"hello")
        raw = slurp(wire.blob_path(self.tmp, sha))
        self.assertFalse(raw.startswith(crypt.MAGIC))
        import zlib
        self.assertEqual(zlib.decompress(raw), b"hello")
        self.assertFalse(crypt.enabled(self.tmp))
        self.assertIsNone(crypt.key_for(self.tmp))