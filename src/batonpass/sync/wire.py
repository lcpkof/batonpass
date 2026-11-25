"""The wire format: append-only JSON lines, blobs addressed by their content.

Two rules make machine-to-machine safe without any merge algorithm:

* a device only ever appends to **its own** file, so there is nothing to lock
  and nothing to reconcile;
* every payload is addressed by the sha256 of its plaintext, so arriving twice
  is indistinguishable from arriving once. Union of two append-only sets is
  set union — that is the whole conflict story.

Blobs travel **zlib**-compressed, not zstd-with-dictionary. The trained
dictionary is per-device and would make one machine's stream unreadable on the
other; zlib is in the standard library on both ends, so a machine that has
installed nothing but batonpass can still read the channel.
"""

from __future__ import annotations

import json
import os
import zlib

from ..store.content import sha256_bytes

WIRE_VERSION = 1
_TIERS = ("t0", "t1", "t2")


def pack(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str) + "\n"


def unpack(line: str):
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# ── blobs ───────────────────────────────────────────────────────

def blob_path(channel: str, sha: str) -> str:
    return os.path.join(channel, "blobs", sha[:2], sha)


def put_blob(channel: str, data: bytes) -> tuple[str, int, bool]:
    """Store a payload once. Returns (sha, bytes_written, was_new)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    sha = sha256_bytes(data)
    path = blob_path(channel, sha)
    if os.path.exists(path):
        return sha, 0, False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob = zlib.compress(data, 9)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)          # atomic: a reader sees all or nothing
    return sha, len(blob), True


def get_blob(channel: str, sha: str):
    path = blob_path(channel, sha)
    if not sha or not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        return zlib.decompress(fh.read())


def has_blob(channel: str, sha: str) -> bool:
    return bool(sha) and os.path.exists(blob_path(channel, sha))


# ── streams ─────────────────────────────────────────────────────

def stream_path(channel: str, device: str, tier: str) -> str:
    return os.path.join(channel, "stream", device, "%s.jsonl" % tier)


def append_lines(channel: str, device: str, tier: str, lines) -> int:
    """Append a batch, fsync once. Returns bytes written."""
    path = stream_path(channel, device, tier)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    block = "".join(lines)
    if not block:
        return 0
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())
    return len(block.encode("utf-8"))


def read_since(channel: str, device: str, tier: str, offset: int = 0):
    """Read new complete lines from `offset`.

    A line is only accepted if it ends with a newline, so a pull that races a
    push never imports half a record — it simply stops at the last newline and
    picks the rest up next time.
    """
    path = stream_path(channel, device, tier)
    if not os.path.exists(path):
        return [], offset, 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        raw = fh.read()
    cut = raw.rfind(b"\n")
    if cut < 0:
        return [], offset, 0
    usable, rest = raw[: cut + 1], len(raw[: cut + 1])
    rows, bad = [], 0
    for line in usable.decode("utf-8", "replace").splitlines():
        obj = unpack(line)
        if obj is None:
            bad += 1
        else:
            rows.append(obj)
    return rows, offset + rest, bad


def tiers():
    return _TIERS
