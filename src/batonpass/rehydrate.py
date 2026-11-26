"""Getting a body back from the tool that wrote it.

A slim library keeps the pointers and lets the text go. That trade is only
honest if the other half exists: a way to walk back to the tool's own database
and re-read the session. This is it — no cache, no clever diffing, just "open
the source again and replay this one session", which is also the only version
that still works after the tool has moved its file.

It can fail, and it says so: a session the source no longer has (compacted,
rebuilt, deleted) stays a pointer. That is the price of the window, and the
reason the window never covers prose.
"""

from __future__ import annotations

from .archive.adapters import ArchiveResult, _ingest, get
from .store.content import Codec, get_dict
from .store.db import source_ro

PURGED_SQL = ("(args_sha IN (SELECT sha256 FROM content WHERE codec='purged')"
              " OR result_sha IN (SELECT sha256 FROM content WHERE codec='purged'))")
_PART_PURGED = ("(p.args_sha IN (SELECT sha256 FROM content WHERE codec='purged')"
                " OR p.result_sha IN (SELECT sha256 FROM content WHERE codec='purged'))")


def _session(conn, sid_prefix: str):
    rows = conn.execute(
        "SELECT * FROM session WHERE sid LIKE ?||'%' OR source_sid = ? OR source_sid LIKE ?||'%'"
        " ORDER BY updated_at DESC", (sid_prefix, sid_prefix, sid_prefix)).fetchall()
    if not rows:
        raise RuntimeError("没有以 %r 开头的会话" % sid_prefix)
    return rows[0]


def _source_path(conn, session, explicit=None) -> str:
    if explicit:
        return explicit
    row = conn.execute(
        "SELECT path FROM raw_source WHERE adapter=? AND device=? AND path IS NOT NULL"
        " ORDER BY first_seen DESC LIMIT 1",
        (session["source"], session["device"])).fetchone()
    if row and row["path"]:
        return row["path"]
    from .discover import sources as discover_sources
    for f in discover_sources():
        if f.get("adapter") == session["source"] and f.get("path"):
            return f["path"]
    raise RuntimeError("不知道 %s 的库在哪；用 --from <路径> 指给我" % session["source"])


def _purged_refs(conn, sid) -> set:
    rows = conn.execute(
        "SELECT args_sha s FROM part p JOIN message m ON m.mid=p.mid WHERE m.sid=?"
        " UNION SELECT result_sha FROM part p JOIN message m ON m.mid=p.mid WHERE m.sid=?",
        (sid, sid)).fetchall()
    shas = {r[0] for r in rows if r[0]}
    if not shas:
        return set()
    marks = ",".join("?" * len(shas))
    return {r[0] for r in conn.execute(
        "SELECT sha256 FROM content WHERE codec='purged' AND sha256 IN (%s)" % marks,
        list(shas))}


def fetch(conn, sid_prefix: str = None, path: str = None, everything: bool = False,
          progress=None) -> dict:
    """Replay one session (or every slimmed one) from its source database."""
    if everything:
        targets = conn.execute(
            "SELECT DISTINCT m.sid AS sid FROM message m JOIN part p ON p.mid=m.mid"
            " WHERE " + _PART_PURGED).fetchall()
        sessions = [{"sid": t["sid"]} for t in targets]
    else:
        sessions = [_session(conn, sid_prefix)]

    out = {"sessions": 0, "restored_parts": 0, "still_pointer": 0, "missing": [],
           "skipped": 0, "bytes": 0}
    open_sources = {}
    try:
        for s in sessions:
            row = conn.execute("SELECT * FROM session WHERE sid=?", (s["sid"],)).fetchone()
            if row is None:
                continue
            before = _purged_refs(conn, row["sid"])
            if not before:
                out["skipped"] += 1
                continue
            try:
                key = (row["source"], _source_path(conn, row, path))
            except RuntimeError as exc:
                out["missing"].append({"sid": row["sid"], "why": str(exc)})
                continue
            if key not in open_sources:
                src_conn, _mode = source_ro(key[1])
                open_sources[key] = src_conn
            src_conn = open_sources[key]
            sd = _find_session(get(row["source"]), src_conn, row["source_sid"])
            if sd is None:
                out["missing"].append({"sid": row["sid"],
                                       "why": "原库里已经没有这条会话了"})
                continue
            _forget_pointers(conn, row["sid"])
            res = ArchiveResult(row["source"], key[1])
            codec = Codec(get_dict(conn, row["source"]))
            with conn:
                _ingest(conn, res, codec, row["source"], row["device"], src_conn,
                        [sd], get(row["source"]).messages, level=1)
            after = _purged_refs(conn, row["sid"])
            out["sessions"] += 1
            out["restored_parts"] += len(before) - len(after)
            out["still_pointer"] += len(after)
            if progress:
                progress(row["sid"], len(before) - len(after))
    finally:
        for c in open_sources.values():
            try:
                c.close()
            except Exception:
                pass
    return out


def _find_session(adapter, src_conn, source_sid):
    for s in adapter.sessions(src_conn):
        if str(s.get("source_sid")) == str(source_sid):
            return s
    return None


def _forget_pointers(conn, sid) -> None:
    """Drop the rows that stand for text we no longer have, so the replay can
    put the real rows back. Folded rows (n>1) are exactly those."""
    with conn:
        conn.execute(
            "DELETE FROM part WHERE mid IN (SELECT mid FROM message WHERE sid=?)"
            " AND (COALESCE(n,1) > 1 OR " + PURGED_SQL + ")", (sid,))


def report(out: dict) -> str:
    lines = []
    if out["sessions"]:
        lines.append("从原库读回 %d 条会话，%d 个正文块" % (out["sessions"], out["restored_parts"]))
    if out["skipped"]:
        lines.append("%d 条没有可取的正文（本来就是完整的）" % out["skipped"])
    for m in out["missing"]:
        lines.append("取不回 %s：%s" % (m["sid"][:8], m["why"]))
    if out["still_pointer"]:
        lines.append("还有 %d 块留在指针状态" % out["still_pointer"])
    if not lines:
        lines.append("没有需要取回的会话")
    return "\n".join(lines)
