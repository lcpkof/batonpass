"""Content-addressed compressed text.

Three decisions worth writing down:

1. The key is sha256 of the plaintext. The codec lives in its own column, so
   swapping zstd for lzma (or for 3.14's stdlib zstd) never invalidates a
   library that already exists.
2. Identical text is stored once. The sources are full of duplicates.
3. Foreign blobs may be several concatenated zstd frames. Decoding only the
   first silently loses the tail, so decode_all() walks the unused_data.
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import sqlite3

try:
    import zstandard as _zstd
    HAVE_ZSTD = True
except Exception:  # pragma: no cover
    _zstd = None
    HAVE_ZSTD = False

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
DICT_SIZE = 112_640
LEVEL = 19


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def short(*parts) -> str:
    return sha256_text("|".join(str(p) for p in parts))[:32]


# ── dictionary ──────────────────────────────────────────────────

def train_dict(samples, size=DICT_SIZE):
    if not HAVE_ZSTD:
        return None
    uniq, seen = [], set()
    for s in samples:
        if not s or len(s) < 8:
            continue
        h = sha256_bytes(s)
        if h in seen:
            continue
        seen.add(h)
        uniq.append(bytes(s))
        if len(uniq) >= 3000:
            break
    if len(uniq) < 8:
        return None
    try:
        return _zstd.train_dictionary(size, uniq).as_bytes()
    except Exception:
        return None


class Codec:
    def __init__(self, dict_bytes=None):
        self.dict_bytes = dict_bytes
        self._c = self._d = None
        if HAVE_ZSTD:
            try:
                dd = _zstd.ZstdCompressionDict(dict_bytes) if dict_bytes else None
                kw = {"level": LEVEL}
                if dd:
                    kw["dict_data"] = dd
                self._c = _zstd.ZstdCompressor(**kw)
                self._d = _zstd.ZstdDecompressor(dict_data=dd) if dd else _zstd.ZstdDecompressor()
            except Exception:
                self._c = self._d = None
        if self._c is not None:
            self.name = "zstd19+dict" if dict_bytes else "zstd19"
        else:
            self.name = "lzma6"

    def compress(self, data: bytes):
        if self._c is not None:
            try:
                return self._c.compress(data), self.name
            except Exception:
                pass
        return lzma.compress(data, preset=6), "lzma6"

    def decompress(self, blob: bytes, codec: str) -> bytes:
        if not blob:
            return b""
        if codec.startswith("zstd"):
            if not HAVE_ZSTD:
                raise RuntimeError("library needs zstandard installed to read: pip install zstandard")
            return decode_all(blob, self._d)
        if codec.startswith("lzma"):
            return lzma.decompress(blob)
        if codec == "none":
            return blob
        raise ValueError("unknown codec %r" % codec)


def decode_all(blob: bytes, dctx=None) -> bytes:
    """Decompress one or more concatenated zstd frames."""
    dctx = dctx or _zstd.ZstdDecompressor()
    out, data = bytearray(), blob
    while data:
        obj = dctx.decompressobj()
        out += obj.decompress(data)
        rest = obj.unused_data
        if not rest or rest == data:
            break
        data = rest
    return bytes(out)


def sniff_and_decode(blob: bytes) -> tuple[bytes, str]:
    """Decode a foreign blob by magic bytes — never trust a column called `algo`."""
    if blob[:4] == ZSTD_MAGIC:
        if not HAVE_ZSTD:
            return blob, "zstd-unavailable"
        try:
            return decode_all(blob), "zstd"
        except Exception:
            return blob, "zstd-failed"
    if blob[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(blob), "gzip"
        except Exception:
            return blob, "gzip-failed"
    if blob[:3] == b"BZh":
        try:
            return bz2.decompress(blob), "bzip2"
        except Exception:
            return blob, "bzip2-failed"
    return blob, "plain"


# ── storage ─────────────────────────────────────────────────────

def put_content(conn: sqlite3.Connection, text, codec: Codec) -> tuple[str, bool]:
    """Store text once, bumping refcount on repeats. Returns (sha, was_new)."""
    if text is None:
        return "", False
    data = text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text)
    sha = sha256_bytes(data)
    row = conn.execute("SELECT 1 FROM content WHERE sha256=?", (sha,)).fetchone()
    if row:
        conn.execute("UPDATE content SET refcount=refcount+1 WHERE sha256=?", (sha,))
        return sha, False
    body, cname = codec.compress(data)
    conn.execute("INSERT INTO content(sha256,byte_len,refcount,codec,body) VALUES(?,?,?,?,?)",
                 (sha, len(data), 1, cname, body))
    return sha, True


def get_content(conn: sqlite3.Connection, sha: str, codec: Codec) -> str:
    if not sha:
        return ""
    row = conn.execute("SELECT body,codec FROM content WHERE sha256=?", (sha,)).fetchone()
    if row is None:
        return ""
    return codec.decompress(row["body"], row["codec"]).decode("utf-8", "replace")


def get_dict(conn, adapter: str):
    from .db import get_meta
    hexed = get_meta(conn, "dict:" + adapter)
    if not hexed:
        return None
    try:
        return bytes.fromhex(hexed)
    except ValueError:
        return None


def save_dict(conn, adapter: str, data: bytes) -> None:
    from .db import set_meta
    set_meta(conn, "dict:" + adapter, data.hex())
