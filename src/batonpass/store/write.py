"""All writes go through here, and all of them are INSERT OR IGNORE.

That single rule is why `archive` can be run a hundred times: the second run
produces zero new rows, and a half-finished run is simply resumed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .content import sha256_bytes, sha256_text


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_raw_source(conn, source_sha, device, adapter, adapter_ver, path, nbytes):
    conn.execute(
        "INSERT OR IGNORE INTO raw_source"
        "(source_sha,device,adapter,adapter_ver,path,bytes,first_seen) VALUES(?,?,?,?,?,?,?)",
        (source_sha, device, adapter, adapter_ver, path, nbytes, now_iso()),
    )


def put_raw_record(conn, source_sha, seq, kind, payload_sha, codec_name="none") -> bool:
    """The raw layer is content-addressed through `content` too.

    Storing the payload inline looks simpler and is much worse: re-archiving a
    source after its app touched the file would duplicate every byte, because
    the source identity includes the file's mtime. With the payload living in
    `content`, a second pass costs a refcount bump.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_record(source_sha,seq,kind,payload,payload_sha,codec) "
        "VALUES(?,?,?,?,?,?)",
        (source_sha, seq, kind, None, payload_sha, codec_name),
    )
    return cur.rowcount > 0


def upsert_session(conn, row: dict) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO session"
        "(sid,source,source_sid,device,project_key,project_key_tier,cwd,title,created_at,updated_at,"
        " msg_count,first_msg_at,last_msg_at,forked_from,artifact_refs,adapter_ver,content_hash)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row.get("sid"), row.get("source"), row.get("source_sid"), row.get("device"),
         row.get("project_key"), row.get("project_key_tier"), row.get("cwd"), row.get("title"),
         row.get("created_at"), row.get("updated_at"), row.get("msg_count", 0),
         row.get("first_msg_at"), row.get("last_msg_at"), row.get("forked_from"),
         row.get("artifact_refs"), row.get("adapter_ver"), row.get("content_hash")),
    )
    if cur.rowcount:
        return True
    _refresh_session(conn, row)
    return False


def _refresh_session(conn, row: dict):
    """A session's identity is fixed; its size is not.

    `INSERT OR IGNORE` alone freezes msg_count/last_msg_at at first sight, so a
    conversation that grew overnight kept reporting its old length and its old
    "last activity" — which is exactly what the handoff card and `tasks` show.
    Nothing else about the row moves: sid, source, device and created_at stay.
    """
    old = conn.execute(
        "SELECT first_msg_at,last_msg_at,msg_count FROM session WHERE sid=?",
        (row.get("sid"),)).fetchone()
    if old is None:
        return
    firsts = [x for x in (old[0], row.get("first_msg_at")) if x]
    lasts = [x for x in (old[1], row.get("last_msg_at")) if x]
    conn.execute(
        "UPDATE session SET title=COALESCE(?,title), updated_at=COALESCE(?,updated_at),"
        " msg_count=MAX(COALESCE(msg_count,0),?), first_msg_at=?, last_msg_at=?,"
        " cwd=COALESCE(?,cwd), project_key=COALESCE(?,project_key),"
        " content_hash=COALESCE(?,content_hash) WHERE sid=?",
        (row.get("title"), row.get("updated_at"), row.get("msg_count") or 0,
         min(firsts) if firsts else None, max(lasts) if lasts else None,
         row.get("cwd"), row.get("project_key"), row.get("content_hash"),
         row.get("sid")),
    )


def put_message(conn, mid, sid, seq, role, text_sha, model=None, ts=None, meta_json=None) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO message(mid,sid,seq,role,text_sha,model,ts,meta_json) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (mid, sid, seq, role, text_sha, model, ts, meta_json),
    )
    return cur.rowcount > 0


def put_part(conn, pid, mid, seq, kind, name=None, args_sha=None, result_sha=None,
             is_error=0, ts=None) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO part(pid,mid,seq,kind,name,args_sha,result_sha,is_error,ts) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (pid, mid, seq, kind, name, args_sha, result_sha, 1 if is_error else 0, ts),
    )
    return cur.rowcount > 0


def session_key(source, source_sid, device) -> str:
    return sha256_text("session|%s|%s|%s" % (source, device, source_sid))[:32]


def message_key(sid, seq) -> str:
    return sha256_text("message|%s|%d" % (sid, seq))[:32]


def part_key(mid, seq) -> str:
    return sha256_text("part|%s|%d" % (mid, seq))[:32]


def session_content_hash(conn, sid) -> str:
    """Hash of the ordered message-text hashes — the cheap change detector."""
    rows = conn.execute("SELECT text_sha FROM message WHERE sid=? ORDER BY seq", (sid,)).fetchall()
    return sha256_text("".join((r[0] or "") for r in rows)) if rows else ""
