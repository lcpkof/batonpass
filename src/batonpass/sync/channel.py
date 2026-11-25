"""A channel is just a folder. Three transports fall out of that:

* a directory (a shared mount, an SSHFS, a USB stick),
* a git remote (push/pull the folder),
* a relay mailbox (the folder lives on the relay).

There is no server in the loop and no process to keep alive. If the folder is
reachable right now, sync works; if it isn't, everything already pulled stays
queryable, because the library is local.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone

from . import wire

MARKER = "batonpass-channel.json"
SCHEMA = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def device_name() -> str:
    return os.environ.get("BATONPASS_DEVICE") or socket.gethostname().split(".")[0]


def _atomic_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def init_channel(path: str, device: str = None) -> dict:
    path = os.path.abspath(os.path.expanduser(path))
    for sub in ("devices", "stream", "heads", "blobs"):
        os.makedirs(os.path.join(path, sub), exist_ok=True)
    marker = os.path.join(path, MARKER)
    info = {"schema": SCHEMA, "created_at": now_iso(), "kind": "batonpass-channel"}
    if not os.path.exists(marker):
        info["id"] = uuid.uuid4().hex
        _atomic_json(marker, info)
    else:
        with open(marker, encoding="utf-8") as fh:
            try:
                info = json.load(fh)
            except ValueError:
                info = {}
        if not info.get("id"):                 # a channel from before ids
            info["id"] = uuid.uuid4().hex
            info.setdefault("schema", SCHEMA)
            info.setdefault("created_at", now_iso())
            info["kind"] = info.get("kind") or "batonpass-channel"
            _atomic_json(marker, info)
    if device:
        heartbeat(path, device)
    return {"path": path, "id": info["id"], "schema": info.get("schema", SCHEMA),
            "created_at": info.get("created_at")}


def identity(path: str) -> str:
    """The channel's own id, not its path.

    Push watermarks are keyed by this, so a deleted or re-created channel
    folder is recognised as a *different* channel and everything is pushed
    again. Keying by path would have a library insist it had already sent
    713 sessions into an empty directory.
    """
    marker = os.path.join(os.path.abspath(os.path.expanduser(path)), MARKER)
    try:
        with open(marker, encoding="utf-8") as fh:
            return json.load(fh).get("id") or ""
    except (OSError, ValueError):
        return ""


def is_channel(path: str) -> bool:
    return os.path.exists(os.path.join(os.path.expanduser(path), MARKER))


def heartbeat(channel: str, device: str, extra: dict = None) -> dict:
    """Per-device own file, rewritten in place — never a shared index."""
    path = os.path.join(channel, "devices", "%s.json" % device)
    prev = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except ValueError:
            prev = {}
    info = {
        "device": device,
        "hostname": socket.gethostname(),
        "first_seen": prev.get("first_seen") or now_iso(),
        "last_seen": now_iso(),
        "pushes": int(prev.get("pushes") or 0) + 1,
        "batonpass": (extra or {}).get("version") or prev.get("batonpass"),
        "counts": (extra or {}).get("counts") or prev.get("counts"),
    }
    _atomic_json(path, info)
    return info


def devices(channel: str) -> list:
    out = []
    ddir = os.path.join(channel, "devices")
    if not os.path.isdir(ddir):
        return out
    for name in sorted(os.listdir(ddir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(ddir, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (ValueError, OSError):
            continue
    return out


def write_head(channel: str, device: str, tier: str, stats: dict) -> dict:
    """Each tier's own head file: how far this device has written, and its hash."""
    path = os.path.join(channel, "heads", "%s.%s.json" % (device, tier))
    prev = read_head(channel, device, tier)
    head = {
        "device": device,
        "tier": tier,
        "gen": int(prev.get("gen") or 0),
        "lines": int(prev.get("lines") or 0) + int(stats.get("lines") or 0),
        "bytes": wire.stream_bytes(channel, device, tier),
        "live_bytes": int(prev.get("live_bytes") or 0),
        "updated": now_iso(),
    }
    _atomic_json(path, head)
    return head


def read_head(channel: str, device: str, tier: str) -> dict:
    path = os.path.join(channel, "heads", "%s.%s.json" % (device, tier))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# A stream is allowed to grow before it gets rewritten. Rewriting on every push
# would mean fsyncing the whole file every time; the point is to stop unbounded
# growth, not to keep the file minimal.
COMPACT_MIN = {"t0": 512 * 1024, "t1": 4 * 1024 * 1024, "t2": 32 * 1024 * 1024}


def compact(channel: str, device: str, tier: str, force: bool = False) -> dict:
    """Rewrite one device's stream as the set of facts it describes.

    The generation goes up, so every reader's offset — which is keyed on the
    generation — stops applying and the file is re-read from zero. Re-reading
    is safe by construction: imports are idempotent.
    """
    head = read_head(channel, device, tier)
    size = wire.stream_bytes(channel, device, tier)
    out = {"device": device, "tier": tier, "before": size, "after": size,
           "gen": int(head.get("gen") or 0), "compacted": False}
    if not os.path.exists(wire.stream_path(channel, device, tier)):
        out["reason"] = "empty"            # never a head file for a file that isn't there
        return out
    if not force:
        if size <= COMPACT_MIN.get(tier, 4 * 1024 * 1024):
            out["reason"] = "small"
            return out
        live = int(head.get("live_bytes") or 0)
        if live and size < 2 * live:
            out["reason"] = "not worth it yet"
            return out
    res = wire.compact_stream(channel, device, tier)
    head = {
        "device": device, "tier": tier,
        "gen": int(head.get("gen") or 0) + 1,
        "lines": res["lines_after"],
        "bytes": res["after"],
        "live_bytes": res["after"],
        "compacted_at": now_iso(),
        "updated": now_iso(),
    }
    _atomic_json(os.path.join(channel, "heads", "%s.%s.json" % (device, tier)), head)
    out.update({"after": res["after"], "gen": head["gen"], "compacted": True,
                "lines_before": res["lines_before"], "lines_after": res["lines_after"]})
    return out


def read_heads(channel: str) -> list:
    out = []
    hdir = os.path.join(channel, "heads")
    if not os.path.isdir(hdir):
        return out
    for name in sorted(os.listdir(hdir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(hdir, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
        except (ValueError, OSError):
            continue
    return out
