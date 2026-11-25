"""The search index, built locally and never synced.

One design decision worth stating up front: this index is *derived*, so it is
the only thing in the library that is allowed to be thrown away. It travels to
no other machine, it is not part of the channel, and a peer rebuilds its own
from the same immutable messages. That is why it can be aggressive — trigram
tokens over every word of every message — without bloating what you sync.

Incremental by construction: messages only ever get appended, so the watermark
is the last `seq` indexed per session. Re-running `index` costs nothing.
"""

from __future__ import annotations

import json

from ..store.codecs import Codecs
from ..store.text import clean

FTS_VER = "1"
MAX_DOC_CHARS = 8000      # one pasted stack trace should not eat the disk


def _fts_ddl():
    return [
        """CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
               body, sid UNINDEXED, ref UNINDEXED, kind UNINDEXED, ts UNINDEXED,
               tokenize='trigram')""",
        """CREATE TABLE IF NOT EXISTS search_state(
               sid TEXT PRIMARY KEY, last_seq INTEGER, ver TEXT)""",
    ]


def ensure(conn) -> None:
    for stmt in _fts_ddl():
        try:
            conn.execute(stmt)
        except Exception as exc:  # pragma: no cover - old sqlite
            raise RuntimeError(
                "this sqlite has no FTS5 trigram tokenizer (%s); batonpass find needs "
                "sqlite >= 3.34" % exc)
    conn.commit()


def _clip(text: str) -> str:
    text = clean(text or "")
    if len(text) <= MAX_DOC_CHARS:
        return text
    head = MAX_DOC_CHARS * 2 // 3
    return text[:head] + "\n…\n" + text[-MAX_DOC_CHARS // 3:]


def build(conn, force: bool = False, limit: int = None, progress=None) -> dict:
    ensure(conn)
    codecs = Codecs(conn)
    if force:
        conn.execute("DELETE FROM search_fts")
        conn.execute("DELETE FROM search_state")
        conn.commit()

    stats = {"docs": 0, "sessions": 0, "skipped": 0}
    sessions = conn.execute(
        "SELECT sid, source, title, project_key FROM session").fetchall()
    for s in sessions:
        sid, source = s["sid"], s["source"]
        st = conn.execute("SELECT last_seq, ver FROM search_state WHERE sid=?", (sid,)).fetchone()
        last = -1
        if st is not None and st["ver"] == FTS_VER and not force:
            last = st["last_seq"] if st["last_seq"] is not None else -1

        # The title is one doc, re-written only when it changes: it is the most
        # common way a person names what they are looking for.
        conn.execute("DELETE FROM search_fts WHERE sid=? AND kind='title'", (sid,))
        title = (s["title"] or "").strip()
        label = clean(title) + " " + (s["project_key"] or "")
        if title:
            conn.execute(
                "INSERT INTO search_fts(body,sid,ref,kind,ts) VALUES(?,?,?,?,?)",
                (_clip(label), sid, sid, "title", ""))

        rows = conn.execute(
            """SELECT mid, seq, role, text_sha, ts FROM message
                WHERE sid=? AND seq>? AND role IN ('user','assistant','unknown')
                ORDER BY seq LIMIT ?""",
            (sid, last, limit or -1),
        ).fetchall()
        for r in rows:
            body = _clip(codecs.get(r["text_sha"], source))
            if body.strip():
                conn.execute(
                    "INSERT INTO search_fts(body,sid,ref,kind,ts) VALUES(?,?,?,?,?)",
                    (body, sid, r["mid"], r["role"], r["ts"] or ""))
                stats["docs"] += 1
            else:
                stats["skipped"] += 1
        conn.execute(
            "INSERT INTO search_state(sid,last_seq,ver) VALUES(?,?,?) "
            "ON CONFLICT(sid) DO UPDATE SET last_seq=excluded.last_seq, ver=excluded.ver",
            (sid, rows[-1]["seq"] if rows else last, FTS_VER))
        stats["sessions"] += 1
        if progress and stats["sessions"] % 100 == 0:
            progress(stats)
        conn.commit()
    return stats


def stale(conn) -> dict:
    """Sessions the index does not describe: never indexed, or behind."""
    missing = conn.execute(
        "SELECT COUNT(*) c FROM session s WHERE s.msg_count > 0 AND NOT EXISTS"
        " (SELECT 1 FROM search_state st WHERE st.sid=s.sid)").fetchone()["c"]
    behind = conn.execute(
        "SELECT COUNT(*) c FROM search_state st WHERE st.ver<>? OR st.last_seq <"
        " (SELECT COALESCE(MAX(seq),-1) FROM message m WHERE m.sid=st.sid)",
        (FTS_VER,)).fetchone()["c"]
    return {"missing": missing, "behind": behind}


def ensure_fresh(conn, progress=None) -> dict:
    """Tables, plus whatever has arrived since the last build.

    `find` on a library nobody had indexed used to answer "nothing matched" —
    the worst possible failure, because it is indistinguishable from the truth.
    """
    ensure(conn)
    counts = stale(conn)
    if counts["missing"] or counts["behind"]:
        return build(conn, progress=progress)
    return {"docs": 0, "sessions": 0, "skipped": 0, "up_to_date": True}


def size_bytes(conn) -> int:
    try:
        row = conn.execute(
            "SELECT SUM(pgsize) p FROM dbstat WHERE name LIKE 'search_fts%'").fetchone()
        return int(row["p"] or 0)
    except Exception:
        return 0


def stats(conn) -> dict:
    ensure(conn)
    n = conn.execute("SELECT count(*) c FROM search_fts").fetchone()["c"]
    indexed = conn.execute("SELECT count(*) c FROM search_state").fetchone()["c"]
    total = conn.execute("SELECT count(*) c FROM session").fetchone()["c"]
    return {"docs": n, "sessions_indexed": indexed, "sessions_total": total,
            "bytes": size_bytes(conn), "ver": FTS_VER}
