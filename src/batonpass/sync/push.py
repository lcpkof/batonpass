"""Write this machine's view of the world into a channel.

Tiers, because 2.2 GB of chat history is not what you need in order to
remember that you were in the middle of something:

  t0  catalog      ~140 KB   one line per session: where, when, how big
  t1  summary      ~2 MB     the first ask and the last few rounds, as prose
  t2  messages     ~27 MB    every message and tool call, bodies as blobs

t0+t1 is what makes a brand-new machine useful in a second or two. t2 is only
pulled when you actually continue the work.

A session is re-sent when its message count changed, so a session that grew
after the last push is not silently frozen at its old size. A session that
raised is *not* marked as sent, so the next push retries it.
"""

from __future__ import annotations

import json
import os
import sqlite3

from .. import __version__
from ..store import redact
from ..store.content import Codec, get_content, get_dict, is_purged
from ..store.text import clean
from ..store.write import message_key, part_key
from . import channel as ch
from . import wire

SUMMARY_VER = 4          # 4 redacts credentials out of the prose
MAX_T1_REFS = 12          # 24 is the cap the identity layer keeps; a card shows five
HEAD_PER_ROLE = 3
HEAD_CHARS = 600
ONE_LINE_CHARS = 160


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


def _counts(conn, sid) -> tuple:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(seq),0) FROM message WHERE sid=?", (sid,)
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


class _Codecs:
    """One compressor per source, because each source has its own dictionary."""

    def __init__(self, conn):
        self.conn = conn
        self._cache = {}

    def get(self, source):
        source = source or ""
        if source not in self._cache:
            self._cache[source] = Codec(get_dict(self.conn, source))
        return self._cache[source]

    def text(self, source, sha):
        return get_content(self.conn, sha, self.get(source))


def _t0(row) -> dict:
    return {
        "v": wire.WIRE_VERSION, "t": 0,
        "sid": row["sid"], "src": row["source"], "csid": row["source_sid"],
        "dev": row["device"], "pk": row["project_key"], "pkt": row["project_key_tier"],
        "cwd": row["cwd"], "title": row["title"],
        "c0": row["created_at"], "u0": row["updated_at"],
        "n": row["msg_count"], "f0": row["first_msg_at"], "l0": row["last_msg_at"],
        "parent": row["forked_from"], "av": row["adapter_ver"],
        "ch": row["content_hash"],
    }


def _t1_refs(row) -> list:
    """The files a session produced, small enough to ride along with its summary.

    A handover card without its files is a card about a task rather than the task:
    you arrive on the new machine, the card says what you were doing, and then it
    cannot tell you which document you were writing. The refs live in the session
    row, so they cost nothing extra to send — just bytes on the wire.
    """
    raw = row["artifact_refs"]
    if not raw:
        return []
    try:
        refs = json.loads(raw)
    except (ValueError, TypeError):
        return []
    keep = ("name", "ts", "via")     # rank() only needs the name; `via` says how we know
    return [{k: r[k] for k in keep if k in r}
            for r in refs[:MAX_T1_REFS] if isinstance(r, dict) and r.get("name")]


def _head_prose(conn, codecs, sid, source, stat=None) -> tuple:
    """First ask + last few rounds. Cheap, local, no model involved.

    This is what turns "I think I was doing something like X" into "it was on
    the mini, in this session, and the last thing you asked was Y".

    Credentials are replaced here, before the truncation, because a key cut in
    half at the 600-char boundary would otherwise match nothing and ship anyway.
    """
    rows = conn.execute(
        "SELECT seq, role, text_sha FROM message WHERE sid=? ORDER BY seq", (sid,)
    ).fetchall()
    if not rows:
        return "", []
    first_user = next((r for r in rows if (r[1] or "") == "user"), None)
    pick = [first_user] if first_user else []
    for role in ("user", "assistant"):
        for r in [x for x in rows if (x[1] or "") == role][-HEAD_PER_ROLE:]:
            if r not in pick:
                pick.append(r)
    out = []
    for r in pick:
        shas = [r[2]] if r[2] else []
        for a, b in conn.execute(
            "SELECT args_sha, result_sha FROM part WHERE mid=? ORDER BY seq",
            (message_key(sid, r[0]),),
        ):
            shas.extend([a, b])
        text = "\n".join(t for t in (clean(codecs.text(source, s)) for s in shas if s) if t)
        text, n = redact.scrub(text)
        if stat is not None:
            stat["redacted"] = stat.get("redacted", 0) + n
        if text.strip():
            out.append([r[0], r[1] or "", text[:HEAD_CHARS]])
    one_line = out[0][2].splitlines()[0][:ONE_LINE_CHARS] if out else ""
    return one_line, out





def push(channel: str, tier: int = 1, device: str = None, conn=None,
         sid_prefix: str = None) -> dict:
    channel = os.path.abspath(os.path.expanduser(channel))
    device = device or ch.device_name()
    ch.init_channel(channel, device)
    ckey = ch.identity(channel) or channel     # watermarks follow the channel, not its path
    codecs = _Codecs(conn)
    stat = {"channel": channel, "channel_id": ckey, "device": device, "tier": tier,
            "sessions": 0, "sessions_sent": 0, "lines": {0: 0, 1: 0, 2: 0},
            "events": 0,
            "blobs_new": 0, "blob_bytes": 0, "messages": 0, "parts": 0,
            "redacted": 0,
            "skipped": 0, "errors": []}

    q, args = "SELECT rowid,* FROM session", ()
    if sid_prefix:
        q += " WHERE sid LIKE ?"
        args = (sid_prefix + "%",)
    rows = conn.execute(q + " ORDER BY rowid", args).fetchall()
    stat["sessions"] = len(rows)

    t0_buf, t1_buf, t2_buf = [], [], []
    sent = {}
    for row in rows:
        sid, source = row["sid"], row["source"]
        n_msg, max_seq = _counts(conn, sid)
        sig = "%d:%d:%d" % (n_msg, max_seq, SUMMARY_VER)
        last_tier = int(_sync_get(conn, ckey, "sent_tier:" + sid, 0) or 0)
        # Metadata goes stale when the session grows or the summariser improves;
        # bodies are missing until they have been sent at the tier you are asking
        # for now. Both questions are *per tier*: `push --tier 0` followed by
        # `push --tier 1` must still send the summaries, because a t0 line is a
        # catalog entry and a t1 line is the prose a new machine actually reads.
        meta_tier = _sync_get(conn, ckey, "sent_meta:" + sid)
        meta_tier = -1 if meta_tier is None else int(meta_tier)
        # `stale` means the catalog entry or the prose itself has to be rebuilt
        # (the session grew, or the summariser changed — a redaction rule is a
        # summariser change). `behind` means this tier was never sent. A stale
        # session re-sends its prose even if a higher tier was sent before;
        # that is the only way a fixed summariser reaches an old channel.
        stale = _sync_get(conn, ckey, "sent:" + sid) != sig
        need_meta = stale or meta_tier < tier
        need_bodies = tier >= 2 and last_tier < tier
        if not need_meta and not need_bodies:
            stat["skipped"] += 1
            continue
        try:
            stat["sessions_sent"] += 1
            if need_meta and (stale or meta_tier < 0):
                t0_buf.append(_t0(row))

            if need_meta and tier >= 1 and (stale or meta_tier < 1):
                # The summary carries its text inline. Sending it as blobs too
                # would mean thousands of 700-byte files, and a filesystem
                # charges a 4 KB block for each of them.
                one_line, head = _head_prose(conn, codecs, sid, source, stat)
                line = {"v": wire.WIRE_VERSION, "t": 1, "sid": sid, "one": one_line,
                        "head": head, "n": n_msg}
                refs = _t1_refs(row)
                for ref in refs:                     # a filename can be a secret too
                    ref["name"], n = redact.scrub(ref["name"])
                    stat["redacted"] += n
                if refs:
                    line["refs"] = refs
                t1_buf.append(line)

            if need_bodies:
                stat["messages"] += _push_messages(
                    conn, ckey, channel, codecs, sid, source, t2_buf, stat)
            sent[sid] = [sig, tier, max_seq,
                         tier if need_meta else meta_tier]
        except Exception as exc:            # one bad session must not sink the push
            stat["errors"].append("%s: %s" % (sid, exc))

    # The relay log rides along with the catalog, because it is the same kind
    # of thing: immutable, content-hashed, safe to replay. Without it a claim
    # made on this machine would never reach the other one, and two agents on
    # two machines *would* both start working.
    ev_buf, ev_top = _push_events(conn, ckey)
    if ev_buf:
        t0_buf.extend(ev_buf)
        stat["events"] = len(ev_buf)

    for t, buf in ((0, t0_buf), (1, t1_buf), (2, t2_buf)):
        if not buf:
            continue
        name = "t%d" % t
        wire.append_lines(channel, device, name, buf)
        stat["lines"][t] = len(buf)
        ch.write_head(channel, device, name, {"lines": len(buf)})
        comp = ch.compact(channel, device, name)
        if comp.get("compacted"):
            stat.setdefault("compacted", {})[name] = comp

    if ev_buf:
        _sync_put(conn, ckey, "sent_ev:" + device, ev_top)

    # Mark what we sent only after the bytes are on disk, and never a session
    # that raised — otherwise it would never be retried.
    for sid, (sig, sent_tier, max_seq, meta) in sent.items():
        if _sync_get(conn, ckey, "sent:" + sid) != sig:
            _sync_put(conn, ckey, "sent:" + sid, sig)
        prev_meta = _sync_get(conn, ckey, "sent_meta:" + sid)
        prev_meta = -1 if prev_meta is None else int(prev_meta)
        if meta > prev_meta:
            _sync_put(conn, ckey, "sent_meta:" + sid, meta)
        if sent_tier > int(_sync_get(conn, ckey, "sent_tier:" + sid, 0) or 0):
            _sync_put(conn, ckey, "sent_tier:" + sid, sent_tier)
        if sent_tier >= 2:
            _sync_put(conn, ckey, "sent2:" + sid, max_seq)
    conn.commit()

    ch.heartbeat(channel, device, {"version": __version__,
                                   "counts": {"sessions": stat["sessions"]}})
    return stat


def _push_messages(conn, ckey, channel, codecs, sid, source, buf, stat) -> int:
    """Append messages and parts after the last seq this channel already has.

    Per-session seq watermark, so a session that gained three messages costs
    three messages, not the whole conversation again.
    """
    # -1, not 0: seq 0 is the first message of every session, and a zero
    # watermark would silently drop it from every push forever.
    seen = _sync_get(conn, ckey, "sent2:" + sid)
    already = -1 if seen is None else int(seen)
    n = 0
    rows = conn.execute(
        "SELECT seq,role,text_sha,model,ts,meta_json FROM message"
        " WHERE sid=? AND seq>? ORDER BY seq", (sid, already)
    ).fetchall()
    for m in rows:
        buf.append({
            "v": wire.WIRE_VERSION, "t": 2, "k": "m", "sid": sid, "seq": m[0],
            "r": m[1], "th": m[2], "mo": m[3], "ts": m[4], "mj": m[5],
        })
        n += 1
        _ship_blob(channel, codecs, source, m[2], stat)
        mid = message_key(sid, m[0])
        for p in conn.execute(
            "SELECT seq,kind,name,args_sha,result_sha,is_error,ts,COALESCE(n,1) AS n"
            " FROM part WHERE mid=? ORDER BY seq", (mid,)
        ):
            if p[7] > 1:
                # A folded row stands for calls whose text this machine chose not
                # to keep. Shipping the pointer would ship an empty call.
                stat["folded_skipped"] = stat.get("folded_skipped", 0) + 1
                continue
            buf.append({
                "v": wire.WIRE_VERSION, "t": 2, "k": "p", "mid": mid, "sid": sid,
                "seq": p[0], "pk": p[1], "nm": p[2], "ah": p[3], "rh": p[4],
                "is_err": p[5], "ts": p[6],
            })
            stat["parts"] += 1
            _ship_blob(channel, codecs, source, p[3], stat)
            _ship_blob(channel, codecs, source, p[4], stat)
    return n


def _ship_blob(channel, codecs, source, sha, stat):
    if not sha or wire.has_blob(channel, sha):
        return
    if is_purged(codecs.conn, sha):
        # Slimmed away on purpose: the peer gets the pointer and no body, which
        # is the same deal this machine has. Counted, never silent.
        stat["archived"] = stat.get("archived", 0) + 1
        return
    try:
        text = codecs.text(source, sha)
    except Exception:
        return
    if not text:
        return
    _, wrote, is_new = wire.put_blob(channel, text)
    stat["blob_bytes"] += wrote
    stat["blobs_new"] += 1 if is_new else 0


def _push_events(conn, ckey) -> tuple:
    """This machine's own relay events, as stream lines.

    Only events this machine wrote are sent: one that arrived by pull is
    already on the other side, and echoing it would grow every stream by the
    size of everyone else's history. The author is `ch.device_name()` — the
    same name `relay` stamps on a claim — not whatever `--device` this push
    happens to be labelled with.
    """
    device = ch.device_name()
    try:
        since = int(_sync_get(conn, ckey, "sent_ev:" + device, 0) or 0)
    except (TypeError, ValueError):
        since = 0
    try:
        rows = conn.execute(
            "SELECT e.eid, e.tid, e.lamport, e.kind, e.body, e.refs_json, e.created_at,"
            "       COALESCE(e.tool,'') tool, t.project_key, t.title"
            "  FROM task_event e LEFT JOIN task t ON t.tid = e.tid"
            " WHERE e.device=? AND e.lamport>? ORDER BY e.lamport, e.eid",
            (device, since)).fetchall()
    except sqlite3.OperationalError:            # a library from before the log
        return [], since
    out = []
    for r in rows:
        line = {"v": wire.WIRE_VERSION, "t": 3, "eid": r["eid"], "tid": r["tid"],
                "dev": device, "lam": r["lamport"], "kind": r["kind"],
                "ts": r["created_at"], "tool": r["tool"],
                "pk": r["project_key"] or "", "title": r["title"] or ""}
        if (r["body"] or "").strip():
            line["b"] = r["body"]
        if r["refs_json"]:
            line["refs"] = json.loads(r["refs_json"])
        out.append(line)
    top = max([r["lamport"] for r in rows] + [since])
    return out, top
