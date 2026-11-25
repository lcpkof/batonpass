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

import base64
import json
import os
import zlib

from . import crypt

from ..store.content import sha256_bytes

WIRE_VERSION = 2          # 2 seals the line body when the channel is encrypted
_TIERS = ("t0", "t1", "t2")

# The only parts of a line that stay readable on a sealed channel: enough to
# tell two facts apart (so compaction and dedupe still work), and nothing that
# says what was said.
_PUBLIC_KEYS = ("v", "t", "sid", "k", "mid", "seq")
SEALED = "bpc1"


def pack(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str) + "\n"


def seal_line(channel: str, obj: dict) -> dict:
    """Seal what a line *says*, keep what a line *is*.

    Without this, `stream/<device>/t1.jsonl` on a cloud drive is a readable
    index of every conversation you ever had — 600 characters of prose each.
    Sealing the whole line would break compaction, which needs a stable key per
    fact; so the identity stays public and the payload goes in `d`.
    """
    if not crypt.enabled(channel):
        return obj
    body = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    env = {k: obj[k] for k in _PUBLIC_KEYS if k in obj}
    env["se"] = SEALED
    env["d"] = base64.b64encode(crypt.seal(channel, body)).decode("ascii")
    return env


def open_line(channel: str, obj: dict, where: str = "") -> dict:
    """Inner line, decrypting if it was sealed. Raises if the key is wrong."""
    if obj.get("se") != SEALED:
        return obj
    try:
        raw = crypt.open_(channel, base64.b64decode(obj.get("d") or ""))
    except Exception:
        raw = None
    if raw is None:
        raise RuntimeError(
            "cannot read %s: this channel is encrypted and the key does not match "
            "(set BATONPASS_KEY, or put the passphrase on the first line of "
            "~/.batonpass/key)" % (where or "a stream line"))
    try:
        inner = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}
    return inner if isinstance(inner, dict) else {}


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
    """Store a payload once. Returns (sha, bytes_written, was_new).

    The path is the sha256 of the *plaintext* — that is what makes a re-send
    indistinguishable from a first send. The bytes on disk are zlib, then sealed
    if the channel is encrypted, so the same plaintext always lands on the same
    path whether or not the channel is sealed.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    sha = sha256_bytes(data)
    path = blob_path(channel, sha)
    if os.path.exists(path):
        return sha, 0, False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob = zlib.compress(data, 9)
    if crypt.enabled(channel):
        blob = crypt.seal(channel, blob)
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
        raw = fh.read()
    if crypt.sealed_with(channel, raw):
        # A wrong or missing key must not look like a corrupt archive.
        raw = crypt.open_(channel, raw)
        if raw is None:
            raise RuntimeError(
                "cannot open %s: this channel is encrypted and the key does not "
                "match (check BATONPASS_KEY, or ~/.batonpass/key)" % sha[:12])
    return zlib.decompress(raw)


def has_blob(channel: str, sha: str) -> bool:
    return bool(sha) and os.path.exists(blob_path(channel, sha))


# ── streams ─────────────────────────────────────────────────────

def stream_path(channel: str, device: str, tier: str) -> str:
    return os.path.join(channel, "stream", device, "%s.jsonl" % tier)


def append_lines(channel: str, device: str, tier: str, lines) -> int:
    """Append a batch, fsync once. Returns bytes written.

    Accepts packed strings or raw dicts; a dict is the honest form because a
    sealed channel cannot be built by string concatenation.
    """
    path = stream_path(channel, device, tier)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sealed = crypt.enabled(channel)
    out = []
    for ln in lines:
        if isinstance(ln, dict):
            out.append(pack(seal_line(channel, ln)))
        elif sealed:
            obj = unpack(ln)
            out.append(pack(seal_line(channel, obj)) if isinstance(obj, dict) else ln)
        else:
            out.append(ln)
    block = "".join(out)
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
            continue
        if obj.get("v", 1) > WIRE_VERSION:
            raise RuntimeError(
                "this channel was written by a newer batonpass (wire v%s > v%s); "
                "upgrade this machine before pulling" % (obj.get("v"), WIRE_VERSION))
        rows.append(open_line(channel, obj, stream_path(channel, device, tier)))
    return rows, offset + rest, bad


def stream_bytes(channel: str, device: str, tier: str) -> int:
    path = stream_path(channel, device, tier)
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def dedupe_key(tier: str, obj: dict):
    """What makes two stream lines the same fact, newest wins.

    t0/t1 carry one line per session, so the session id is the key. t2 carries
    per-message and per-part lines, so the key is the position they occupy —
    re-sending a message must not look like a second message.
    """
    if tier in ("t0", "t1"):
        return ("s", obj.get("sid"))
    if obj.get("k") == "p":
        return ("p", obj.get("mid"), obj.get("seq"))
    return ("m", obj.get("sid"), obj.get("seq"))


def compact_stream(channel: str, device: str, tier: str) -> dict:
    """Rewrite one device's own stream as the union of what it says.

    Append-only means re-sent facts pile up: 713 sessions pushed twenty times
    is twenty copies of the same catalog, and every reader scans all of it.
    A stream is a *set* of facts, so the compact form is one line per key —
    last one wins, because the last one is the newest thing we know. Readers
    key their byte offset on the generation number, so they simply start over.
    """
    path = stream_path(channel, device, tier)
    if not os.path.exists(path):
        return {"tier": tier, "before": 0, "after": 0, "lines_before": 0, "lines_after": 0}
    with open(path, "rb") as fh:
        raw = fh.read()
    before = len(raw)
    kept, bad = {}, 0
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        obj = unpack(line)
        if obj is None:
            bad += 1
            continue
        kept[dedupe_key(tier, obj)] = obj        # dict keeps first position,
        #                                          assignment keeps newest value
    block = "".join(pack(o) for o in kept.values())
    tmp = path + ".compact%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)                        # a reader sees old or new, never half
    return {"tier": tier, "before": before, "after": len(block.encode("utf-8")),
            "lines_before": len(kept) + bad, "lines_after": len(kept), "bad": bad}


def tiers():
    return _TIERS
