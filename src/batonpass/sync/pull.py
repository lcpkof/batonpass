"""Read other machines' streams into this library.

Everything here is a tail read of an append-only file, resumable by byte
offset. Re-reading is always safe: every insert is INSERT OR IGNORE and every
blob is addressed by its plaintext hash, so the same line imported twice
leaves exactly the rows the first import left.
"""

from __future__ import annotations

import os

from ..store.content import Codec, get_dict, put_content
from ..store.write import message_key, part_key, put_message, put_part, upsert_session
from . import channel as ch
from . import wire


def _sync_get(conn, channel, key, default=None):
    row = conn.execute(
        "SELECT value FROM sync_state WHERE channel=? AND key=?", (channel, key)
    ).fetchone()
    return row[0] if row else default


def _sync_put(conn, channel, key, value):
    conn.execute(
        "INSERT INTO sync_state(channel,key,value) VALUES(?,?,?)"
        " ON CONFLICT(channel,key) DO UPDATE SET value=excluded.value",
        (channel, key, str(value)),
    )


def _session_row(o) -> dict:
    return {
        "sid": o.get("sid"), "source": o.get("src"), "source_sid": o.get("csid"),
        "device": o.get("dev"), "project_key": o.get("pk"),
        "project_key_tier": o.get("pkt"), "cwd": o.get("cwd"),
        "title": o.get("title"), "created_at": o.get("c0"),
        "updated_at": o.get("u0"), "msg_count": o.get("n") or 0,
        "first_msg_at": o.get("f0"), "last_msg_at": o.get("l0"),
        "forked_from": o.get("parent"), "adapter_ver": o.get("av"),
        "content_hash": o.get("ch"),
    }


def _put_head(conn, o):
    conn.execute(
        "INSERT INTO session_head(sid,device,one_line,head_json,msg_total,updated_at)"
        " VALUES(?,?,?,?,?,datetime('now'))"
        " ON CONFLICT(sid) DO UPDATE SET one_line=excluded.one_line,"
        " head_json=excluded.head_json, msg_total=excluded.msg_total,"
        " updated_at=excluded.updated_at",
        (o.get("sid"), o.get("dev"), o.get("one"), wire.pack({"head": o.get("head")}),
         int(o.get("n") or 0)),
    )


def _import_blob(conn, channel, sha, codecs, source, stat):
    """Turn a channel blob into a local content row, keeping the same sha."""
    if not sha:
        return True
    if conn.execute("SELECT 1 FROM content WHERE sha256=?", (sha,)).fetchone():
        return True
    data = wire.get_blob(channel, sha)
    if data is None:
        stat["missing"].append(sha)
        return False
    got, _ = put_content(conn, data, codecs.get(source))
    if got != sha:
        stat["missing"].append("%s!=%s" % (sha[:8], got[:8]))
        return False
    stat["blobs_new"] += 1
    return True


def _device_dirs(channel):
    out = []
    sdir = os.path.join(channel, "stream")
    if not os.path.isdir(sdir):
        return out
    for name in sorted(os.listdir(sdir)):
        if os.path.isdir(os.path.join(sdir, name)):
            out.append(name)
    return out


def pull(channel: str, tier: int = 0, device: str = None, conn=None,
         sid_prefix: str = None) -> dict:
    channel = os.path.abspath(os.path.expanduser(channel))
    me = device or ch.device_name()
    if not ch.is_channel(channel):
        raise SystemExit("no batonpass channel at %s" % channel)
    # Pull never writes into the channel: reading other machines' history
    # should not be able to change it.
    ckey = ch.identity(channel) or channel
    codecs = _Codecs(conn)
    stat = {"channel": channel, "channel_id": ckey, "device": me, "tier": tier, "devices": [],
            "sessions_new": 0, "sessions_seen": 0, "heads": 0, "messages_new": 0,
            "parts_new": 0, "blobs_new": 0, "bad_lines": 0, "missing": [],
            "sources": {}}

    # A filtered pull is a one-off deep read of a specific task: don't advance
    # any offset, so unfiltered rows are never skipped by accident.
    advance = not sid_prefix

    for dev in _device_dirs(channel):
        stat["devices"].append(dev)
        for t in range(0, tier + 1):
            tier_name = "t%d" % t
            # The offset belongs to a *generation* of the file, not to the file.
            # A compacted stream (gen+1) therefore looks brand new and is read
            # from zero — which is safe, because every import is idempotent.
            gen = int(ch.read_head(channel, dev, tier_name).get("gen") or 0)
            key = "pull:%s:%s:g%d" % (dev, tier_name, gen)
            offset = 0 if not advance else int(_sync_get(conn, ckey, key, 0) or 0)
            rows, new_offset, bad = wire.read_since(channel, dev, tier_name, offset)
            if advance and int(ch.read_head(channel, dev, tier_name).get("gen") or 0) != gen:
                gen = int(ch.read_head(channel, dev, tier_name).get("gen") or 0)
                key = "pull:%s:%s:g%d" % (dev, tier_name, gen)
                rows, new_offset, bad = wire.read_since(channel, dev, tier_name, 0)
                stat["gen_changed"] = stat.get("gen_changed", 0) + 1
            stat["bad_lines"] += bad
            for o in rows:
                sid = o.get("sid")
                if sid_prefix and not (sid or "").startswith(sid_prefix):
                    continue
                if o.get("t") == 0:
                    stat["sessions_seen"] += 1
                    if upsert_session(conn, _session_row(o)):
                        stat["sessions_new"] += 1
                elif o.get("t") == 1:
                    _put_head(conn, o)
                    stat["heads"] += 1
                elif o.get("t") == 2:
                    _apply_t2(conn, channel, codecs, o, stat)
            if advance and new_offset != offset:
                _sync_put(conn, ckey, key, new_offset)
    conn.commit()
    return stat


class _Codecs:
    def __init__(self, conn):
        self.conn, self._cache = conn, {}

    def get(self, source):
        source = source or ""
        if source not in self._cache:
            self._cache[source] = Codec(get_dict(self.conn, source))
        return self._cache[source]


def _apply_t2(conn, channel, codecs, o, stat):
    source = _source_of(conn, o.get("sid"))
    if o.get("k") == "m":
        sid, seq = o.get("sid"), int(o.get("seq") or 0)
        _import_blob(conn, channel, o.get("th"), codecs, source, stat)
        if put_message(conn, message_key(sid, seq), sid, seq, o.get("r"),
                       o.get("th"), o.get("mo"), o.get("ts"), o.get("mj")):
            stat["messages_new"] += 1
        stat["sources"][source] = stat["sources"].get(source, 0) + 1
    elif o.get("k") == "p":
        mid, seq = o.get("mid"), int(o.get("seq") or 0)
        _import_blob(conn, channel, o.get("ah"), codecs, source, stat)
        _import_blob(conn, channel, o.get("rh"), codecs, source, stat)
        if put_part(conn, part_key(mid, seq), mid, seq, o.get("pk"), o.get("nm"),
                    o.get("ah"), o.get("rh"), o.get("e"), o.get("ts")):
            stat["parts_new"] += 1


def _source_of(conn, sid) -> str:
    row = conn.execute("SELECT source FROM session WHERE sid=?", (sid,)).fetchone()
    return row[0] if row else ""
