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

from ..store.text import clean

FTS_VER = "3"
# Only titles live in the trigram index now. Message bodies used to be indexed
# here too, at 8000 chars each, and on a 70k-doc library that came to 213 MB of
# index for 14 MB of text — a trigram index over Chinese prose costs ~15 bytes
# per character, because nearly every trigram is unique to its document. Capping
# the document did not help (the mean message is 245 chars). So bodies are
# searched where they are stored, with a LIKE over plain TEXT, and this index
# keeps the one list that has to be fast and small: what sessions are called.
TITLE_MAX = 240


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
    return text if len(text) <= TITLE_MAX else text[:TITLE_MAX] + "…"


def build(conn, force: bool = False, limit: int = None, progress=None) -> dict:
    ensure(conn)
    if force:
        # DROP, not DELETE: FTS5 keeps deleted segments in its shadow b-tree
        # until an optimize, and on a 219 MB index neither DELETE nor VACUUM
        # gave a single page back. Dropping the virtual table takes its shadow
        # tables with it, and then the space is really gone.
        conn.execute("DROP TABLE IF EXISTS search_fts")
        conn.execute("DELETE FROM search_state")
        conn.commit()
        ensure(conn)

    stats = {"docs": 0, "sessions": 0}
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
            stats["docs"] += 1

        # The watermark still advances — `stale()` and `doctor` read it — but
        # there are no body docs to write any more.
        last_seq = conn.execute(
            "SELECT COALESCE(MAX(seq),-1) FROM message WHERE sid=?", (sid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO search_state(sid,last_seq,ver) VALUES(?,?,?) "
            "ON CONFLICT(sid) DO UPDATE SET last_seq=excluded.last_seq, ver=excluded.ver",
            (sid, last_seq, FTS_VER))
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
