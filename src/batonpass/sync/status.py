"""What is in the channel, and how much of it this machine has read.

Deliberately answers the human question first — "who else is in here, and when
did they last show up" — before any row counts.
"""

from __future__ import annotations

import json
import os

from . import channel as ch
from . import wire


def _offsets(conn, ckey):
    out = {}
    for name, value in conn.execute(
        "SELECT key,value FROM sync_state WHERE channel=? AND key LIKE 'pull:%'", (ckey,)
    ):
        out[name] = int(value or 0)
    return out


def status(channel: str, conn=None) -> dict:
    channel = os.path.abspath(os.path.expanduser(channel))
    devices = ch.devices(channel)
    heads = ch.read_heads(channel)
    ckey = ch.identity(channel) or channel
    offsets = _offsets(conn, ckey) if conn is not None else {}
    streams = []
    sdir = os.path.join(channel, "stream")
    if os.path.isdir(sdir):
        for dev in sorted(os.listdir(sdir)):
            d = os.path.join(sdir, dev)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(d, name)
                tier = name[:-6]
                gen = int(ch.read_head(channel, dev, tier).get("gen") or 0)
                # A compacted stream is a new generation: what we read before
                # the rewrite says nothing about what is in the file now.
                key = "pull:%s:%s:g%d" % (dev, tier, gen)
                size = os.path.getsize(path)
                done = offsets.get(key)
                streams.append({
                    "device": dev, "tier": tier, "bytes": size, "gen": gen,
                    "read": done, "pending": (size - done) if done is not None else size,
                })
    return {"channel": channel, "channel_id": ckey if ch.is_channel(channel) else None,
            "exists": ch.is_channel(channel),
            "devices": devices, "heads": heads, "streams": streams,
            "offsets": offsets}
