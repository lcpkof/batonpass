"""Getting things back out.

The point of the raw layer is that it can be replayed. The point of `export` is
that you can prove it in one command: dump a session as plain text and diff it
against the source database with your own tools.
"""

from __future__ import annotations

import json

from ..store.content import Codec, get_content, get_dict
from ..store.db import connect


def export_session(sid, db=None, as_json=True):
    conn = connect(db)
    row = conn.execute("SELECT * FROM session WHERE sid LIKE ?||'%'", (sid,)).fetchone()
    if row is None:
        raise SystemExit("no session matching %r" % sid)
    codec = Codec(get_dict(conn, row["source"]))
    msgs = []
    for m in conn.execute("SELECT * FROM message WHERE sid=? ORDER BY seq", (row["sid"],)):
        parts = []
        for p in conn.execute("SELECT * FROM part WHERE mid=? ORDER BY seq", (m["mid"],)):
            parts.append({
                "kind": p["kind"], "name": p["name"],
                "args": get_content(conn, p["args_sha"], codec) or None,
                "result": get_content(conn, p["result_sha"], codec) or None,
                "is_error": bool(p["is_error"]), "ts": p["ts"],
            })
        msgs.append({"seq": m["seq"], "role": m["role"], "ts": m["ts"],
                     "text": get_content(conn, m["text_sha"], codec), "parts": parts})
    doc = {"sid": row["sid"], "source": row["source"], "source_sid": row["source_sid"],
           "device": row["device"], "title": row["title"], "cwd": row["cwd"],
           "created_at": row["created_at"], "updated_at": row["updated_at"],
           "messages": msgs}
    if as_json:
        return json.dumps(doc, ensure_ascii=False, indent=2, default=str)
    lines = ["# %s" % (row["title"] or row["source_sid"])]
    for m in msgs:
        lines.append("\n## %s  %s" % (m["role"], m["ts"] or ""))
        if m["text"]:
            lines.append(m["text"])
        for p in m["parts"]:
            lines.append("  [%s %s] %s" % (p["kind"], p["name"] or "",
                                           (p["result"] or p["args"] or "").strip()[:400]))
    return "\n".join(lines)


def stats(db=None):
    conn = connect(db)
    q = lambda sql: conn.execute(sql).fetchone()[0]
    plain = q("SELECT COALESCE(sum(byte_len),0) FROM content")
    stored = q("SELECT COALESCE(sum(length(body)),0) FROM content")
    raw = q("SELECT COALESCE(sum(length(payload)),0) FROM raw_record")
    return {
        "sessions": q("SELECT count(*) FROM session"),
        "messages": q("SELECT count(*) FROM message"),
        "parts": q("SELECT count(*) FROM part"),
        "raw_sources": q("SELECT count(*) FROM raw_source"),
        "raw_records": q("SELECT count(*) FROM raw_record"),
        "content_unique": q("SELECT count(*) FROM content"),
        "plain_bytes": plain,
        "stored_bytes": stored,
        "raw_bytes": raw,
        "ratio": round(plain / stored, 2) if stored else 0,
        "db_bytes": q("SELECT page_count*page_size FROM pragma_page_count(), pragma_page_size()"),
        "by_source": [dict(r) for r in conn.execute(
            "SELECT source, count(*) n, sum(msg_count) msgs FROM session GROUP BY source ORDER BY n DESC")],
    }
