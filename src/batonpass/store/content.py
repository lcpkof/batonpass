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

PURGED = "purged"
PLAIN = "none"   # the codec name a body already carries when we store it as-is


def _plain_ok(text) -> bool:
    return isinstance(text, str)


def put_content(conn: sqlite3.Connection, text, codec: Codec,
                plain: bool = False) -> tuple[str, bool]:
    """Store text once, bumping refcount on repeats. Returns (sha, was_new).

    `plain=True` skips compression and keeps the body as TEXT. Prose is stored
    that way on purpose: it is the one thing a search has to read from end to
    end, and decoding seventy thousand small frames costs more than the 20 MB
    the compression saves (measured: 1.7 s per query against 0.1 s). Tool I/O
    is the opposite — huge, rarely read back — so it stays compressed.

    A row whose body was purged keeps its sha and its byte_len — the pointer is
    still valid, the text is simply not here. Writing the same text again is
    therefore not a no-op: it revives the body, which is why re-archiving a
    source quietly undoes a slim run without any special "restore" path.
    """
    if text is None:
        return "", False
    data = text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text)
    sha = sha256_bytes(data)
    keep = data.decode("utf-8", "replace") if (plain and _plain_ok(text)) else None
    row = conn.execute("SELECT codec FROM content WHERE sha256=?", (sha,)).fetchone()
    if row:
        if row["codec"] != PURGED:
            conn.execute("UPDATE content SET refcount=refcount+1 WHERE sha256=?", (sha,))
            return sha, False
        body, cname = (keep, PLAIN) if keep is not None else codec.compress(data)
        conn.execute("UPDATE content SET body=?,codec=?,refcount=refcount+1 WHERE sha256=?",
                     (body, cname, sha))
        return sha, False
    body, cname = (keep, PLAIN) if keep is not None else codec.compress(data)
    conn.execute("INSERT INTO content(sha256,byte_len,refcount,codec,body) VALUES(?,?,?,?,?)",
                 (sha, len(data), 1, cname, body))
    return sha, True


def get_content(conn: sqlite3.Connection, sha: str, codec: Codec) -> str:
    if not sha:
        return ""
    row = conn.execute("SELECT body,codec FROM content WHERE sha256=?", (sha,)).fetchone()
    if row is None or row["codec"] == PURGED:
        return ""
    if row["codec"] == PLAIN:
        # Stored as TEXT so SQLite itself can substring-match it; a pulled or
        # older row may still be bytes under the same codec name.
        body = row["body"]
        return body if isinstance(body, str) else body.decode("utf-8", "replace")
    return codec.decompress(row["body"], row["codec"]).decode("utf-8", "replace")


def is_purged(conn: sqlite3.Connection, sha: str) -> bool:
    """True when the pointer is real but the text was moved out on purpose."""
    if not sha:
        return False
    row = conn.execute("SELECT codec FROM content WHERE sha256=?", (sha,)).fetchone()
    return bool(row) and row["codec"] == PURGED


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


def plain_prose(conn, codecs, dry_run: bool = False) -> dict:
    """Rewrite compressed prose as TEXT, in place, keeping every pointer.

    Search reads prose from end to end on every query. While it was zstd
    frames, that meant seventy thousand small decodes per search (1.7 s here);
    as TEXT it is one SQLite LIKE (0.1 s) for about 20 MB more on disk. The
    sha, the byte_len and the refcount do not move, so nothing else notices.
    """
    rows = conn.execute(
        """SELECT c.sha256 sha, c.body body, c.codec codec, c.byte_len byte_len,
                  MIN(s.source) src
             FROM content c JOIN message m ON m.text_sha=c.sha256
             JOIN session s ON s.sid=m.sid
            WHERE c.codec NOT IN (?, ?) AND c.byte_len > 0
            GROUP BY c.sha256""", (PURGED, PLAIN)).fetchall()
    out = {"rows": 0, "skipped": 0, "bytes": 0}
    for r in rows:
        try:
            text = codecs.for_source(r["src"]).decompress(r["body"], r["codec"])
        except Exception:
            out["skipped"] += 1          # unreadable blob: leave it exactly as it is
            continue
        out["rows"] += 1
        out["bytes"] += max(len(text) - len(r["body"]), 0)
        if not dry_run:
            conn.execute("UPDATE content SET body=?, codec=? WHERE sha256=?",
                         (text.decode("utf-8", "replace"), PLAIN, r["sha"]))
    if not dry_run:
        conn.commit()
    return out
