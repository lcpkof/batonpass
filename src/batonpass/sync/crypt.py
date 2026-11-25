"""A channel is a folder on someone else's disk.

The whole point of the channel design is that you can put it on a cloud drive, a
shared volume or a USB stick. That is also the whole reason it needs this file:
whatever that drive's operator can read, they can read. So the payload is sealed
before it is written, and the channel marker says so.

What is sealed: every blob — message bodies, tool calls, the bulk of the bytes —
and the body of every stream line. A line keeps only its identity in the clear
(`v`, tier, session id, message/part position), because compaction has to tell
two facts apart without a key; everything a line *says* travels in `d`. So an
operator of the drive learns that some machine had 713 sessions of these sizes
at these times, and not one word of them.

What is not sealed: the file names of blobs, which are the plaintext sha256 —
someone holding a known text can confirm it is in the channel. That is a
content-confirmation leak, not a content leak, and it is what buys idempotency.

Addressing stays on the plaintext sha256, so idempotency survives. The trade is
that someone holding a known plaintext can confirm it is in the channel — a
content-confirmation leak, not a content leak.

The key never goes into the channel. It comes from `BATONPASS_KEY`, or from
`~/.batonpass/key` (first line, mode 600). The salt is public and per-channel.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

MAGIC = b"BPC1"
ALGO = "aes-256-gcm"
SALT_BYTES = 16
_SCRYPT = dict(n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
_cache = {}


def available() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _marker_path(channel: str) -> str:
    from .channel import MARKER
    return os.path.join(channel, MARKER)


def marker(channel: str) -> dict:
    try:
        with open(_marker_path(channel), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def enabled(channel: str) -> bool:
    return marker(channel).get("enc") == ALGO


def new_salt() -> bytes:
    return os.urandom(SALT_BYTES)


def _passphrase(required: bool) -> str:
    text = (os.environ.get("BATONPASS_KEY") or "").strip()
    if not text:
        path = os.path.expanduser("~/.batonpass/key")
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.readline().strip()
        except OSError:
            text = ""
    if not text and required:
        raise RuntimeError(
            "this channel is encrypted; set BATONPASS_KEY (or put the passphrase "
            "on the first line of ~/.batonpass/key)")
    return text


def derive(passphrase: str, salt: bytes) -> bytes:
    """scrypt, because a passphrase that travels through a cloud drive is a
    passphrase that gets copied to a worse disk than the one it was typed on.

    Via `cryptography`'s own binding rather than `hashlib.scrypt`: Apple's
    system Python is built without it, and a key-derivation function that works
    on one machine and not the next is exactly the class of bug this project
    exists to avoid.
    """
    try:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:                       # pragma: no cover - crypto is required
        return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt,
                              n=_SCRYPT["n"], r=_SCRYPT["r"], p=_SCRYPT["p"],
                              dklen=_SCRYPT["dklen"])
    return Scrypt(salt=salt, length=_SCRYPT["dklen"], n=_SCRYPT["n"],
                  r=_SCRYPT["r"], p=_SCRYPT["p"]).derive(passphrase.encode("utf-8"))


def key_for(channel: str):
    """The channel's key, or None when the channel is not encrypted."""
    info = marker(channel)
    if info.get("enc") != ALGO:
        return None
    salt_hex = info.get("salt") or ""
    if not salt_hex:
        raise RuntimeError("channel says it is encrypted but carries no salt; "
                           "re-run `sync init` with the same passphrase")
    passphrase = _passphrase(required=True)
    tag = (salt_hex, passphrase)
    if tag not in _cache:
        _cache[tag] = derive(passphrase, bytes.fromhex(salt_hex))
    return _cache[tag]


def seal(channel: str, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = key_for(channel)
    if key is None:
        return data
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def open_(channel: str, blob: bytes):
    """Plaintext of a blob, or None if it cannot be opened with this key."""
    if not blob.startswith(MAGIC):
        return blob                      # written before the channel was sealed
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        key = key_for(channel)
    except RuntimeError:
        return None
    nonce, body = blob[len(MAGIC):len(MAGIC) + 12], blob[len(MAGIC) + 12:]
    try:
        return AESGCM(key).decrypt(nonce, body, MAGIC)
    except Exception:
        return None


def sealed_with(channel: str, blob: bytes) -> bool:
    return blob.startswith(MAGIC)


# ── turning it on ─────────────────────────────────────────────────

def enable(channel: str) -> dict:
    """Mark a channel encrypted and seal whatever blobs are already in it."""
    if not available():
        raise RuntimeError('encryption needs one optional dependency: '
                           'pip install "batonpass-cli[crypto]"')
    _passphrase(required=True)
    info = marker(channel)
    if info.get("enc") == ALGO:
        return {"already": True, "sealed": 0, "skipped": 0}
    if not info.get("salt"):
        info["salt"] = new_salt().hex()
    info["enc"] = ALGO
    info["enc_at"] = _now()
    tmp = _marker_path(channel) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, _marker_path(channel))       # key must exist before sealing
    return {"already": False, "sealed": _seal_existing(channel), "skipped": 0}


def _seal_existing(channel: str) -> int:
    root = os.path.join(channel, "blobs")
    if not os.path.isdir(root):
        return 0
    n = 0
    for sub in os.listdir(root):
        ddir = os.path.join(root, sub)
        if not os.path.isdir(ddir):
            continue
        for name in os.listdir(ddir):
            path = os.path.join(ddir, name)
            if name.endswith(".tmp") or not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                raw = fh.read()
            if sealed_with(channel, raw):
                continue
            sealed = seal(channel, raw)
            tmp = path + ".tmp%d" % os.getpid()
            with open(tmp, "wb") as fh:
                fh.write(sealed)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            n += 1
    return n


def fingerprint(channel: str) -> str:
    """Eight hex chars of the key — enough to tell two passphrases apart, not
    enough to be worth attacking."""
    key = key_for(channel)
    if key is None:
        return ""
    return hmac.new(b"batonpass-fingerprint", key, hashlib.sha256).hexdigest()[:8]


def _now() -> str:
    from .channel import now_iso
    return now_iso()


def b64(b: bytes) -> str:          # pragma: no cover - debugging aid
    return base64.b64encode(b).decode("ascii")