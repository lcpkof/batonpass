"""Finding the thing you half-remember.

You will arrive with a vague concept, not a query: "那个岗位表", "the ppt for
the exam thing", "what was I doing last Tuesday". So a search here is four
searches — title, project name, filename, message body — fused by reciprocal
rank, because each one alone fails on a different half of that sentence:

    title     the session was *named* after it
    project   the folder was named after it
    file      the deliverable was named after it
    body      it was only ever mentioned in passing

And the silence contract: when nothing matches, return nothing and say so. An
invented answer to "where is my spreadsheet" costs more than "not found",
because you would go looking for a file that does not exist.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

RRF_K = 60
SNIPPET_CHARS = 220

_REL = re.compile(r"^(\d+)\s*(h|d|w|m|y)$", re.I)
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_window(spec: str):
    """`7d` `12h` `today` `2026-09` `2026-09-20` `2026-09-01..2026-09-20` -> (since, until)."""
    if not spec:
        return None, None
    s = spec.strip().lower()
    now = _now()
    if s in ("today", "今天"):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return _iso(start), None
    if s in ("yesterday", "昨天"):
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return _iso(end - timedelta(days=1)), _iso(end)
    m = _REL.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n),
                 "m": timedelta(days=30 * n), "y": timedelta(days=365 * n)}[unit]
        return _iso(now - delta), None
    if ".." in s:
        a, _, b = s.partition("..")
        return _bound(a, end=False), _bound(b, end=True)
    if _MONTH.match(s):
        y, mo = int(s[:4]), int(s[5:7])
        start = datetime(y, mo, 1, tzinfo=timezone.utc)
        end = datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=timezone.utc)
        return _iso(start), _iso(end)
    if _DAY.match(s):
        return _bound(s, end=False), _bound(s, end=True)
    return None, None


def _bound(text: str, end: bool):
    text = text.strip()
    try:
        if _DAY.match(text):
            d = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return _iso(d + timedelta(days=1) if end else d)
        if _MONTH.match(text):
            y, mo = int(text[:4]), int(text[5:7])
            if end:
                return _iso(datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=timezone.utc))
            return _iso(datetime(y, mo, 1, tzinfo=timezone.utc))
    except ValueError:
        return None
    return None


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _fts_phrase(q: str) -> str:
    """Trigram MATCH wants a plain phrase; the user's punctuation is not syntax."""
    return '"%s"' % q.replace('"', " ")


def _snippet(body: str, q: str) -> str:
    body = " ".join((body or "").split())
    idx = body.lower().find(q.lower())
    if idx < 0:
        return body[:SNIPPET_CHARS] + ("…" if len(body) > SNIPPET_CHARS else "")
    start = max(0, idx - SNIPPET_CHARS // 3)
    end = min(len(body), start + SNIPPET_CHARS)
    return ("…" if start else "") + body[start:end] + ("…" if end < len(body) else "")


def _session_filters(at, project, device):
    where, args = [], []
    since, until = parse_window(at) if at else (None, None)
    if since:
        where.append("COALESCE(updated_at,'') >= ?")
        args.append(since)
    if until:
        where.append("COALESCE(updated_at,'') < ?")
        args.append(until)
    if project:
        where.append("(project_key LIKE ? ESCAPE '\\' OR cwd LIKE ? ESCAPE '\\')")
        args += [_like(project), _like(project)]
    if device:
        where.append("device = ?")
        args.append(device)
    return (" AND " + " AND ".join(where)) if where else "", args


def search(conn, q: str, at: str = None, project: str = None, device: str = None,
           limit: int = 5, explain: bool = False) -> list:
    """Return ranked sessions. Empty list is a valid, honest answer."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    clauses, cargs = _session_filters(at, project, device)
    allowed = {r["sid"] for r in conn.execute(
        "SELECT sid FROM session WHERE 1=1" + clauses, cargs)}
    if not allowed:
        return []

    lists = {}
    # ① the session was named after it
    rows = conn.execute(
        "SELECT sid FROM search_fts WHERE kind='title' AND body LIKE ? ESCAPE '\\' "
        "ORDER BY rank LIMIT 40", (_like(q),)).fetchall()
    lists["title"] = [r["sid"] for r in rows]
    # ② the folder was named after it
    rows = conn.execute(
        "SELECT sid FROM session WHERE project_key LIKE ? ESCAPE '\\' "
        "ORDER BY COALESCE(updated_at,'') DESC LIMIT 40", (_like(q),)).fetchall()
    lists["project"] = [r["sid"] for r in rows]
    # ③ the deliverable was named after it
    rows = conn.execute(
        "SELECT sid FROM session WHERE artifact_refs LIKE ? ESCAPE '\\' "
        "ORDER BY COALESCE(updated_at,'') DESC LIMIT 40", (_like(q),)).fetchall()
    lists["file"] = [r["sid"] for r in rows]
    # ④ it was only ever mentioned in passing
    if len(q) >= 3:
        try:
            rows = conn.execute(
                "SELECT sid FROM search_fts WHERE kind<>'title' AND search_fts MATCH ? "
                "ORDER BY rank LIMIT 60", (_fts_phrase(q),)).fetchall()
        except Exception:
            rows = []
    else:
        rows = conn.execute(
            "SELECT sid FROM search_fts WHERE kind<>'title' AND body LIKE ? ESCAPE '\\' "
            "ORDER BY rank LIMIT 60", (_like(q),)).fetchall()
    lists["body"] = [r["sid"] for r in rows]

    scores, why = {}, {}
    for name, sids in lists.items():
        seen = set()
        for rank, sid in enumerate(sids):
            if sid not in allowed or sid in seen:
                continue
            seen.add(sid)
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (RRF_K + rank + 1)
            why.setdefault(sid, []).append(name)

    if not scores:
        return []

    top = sorted(scores.items(), key=lambda kv: -kv[1])[:max(limit, 1)]
    out = []
    for sid, score in top:
        s = conn.execute(
            """SELECT sid, source, device, title, project_key, cwd, updated_at,
                      msg_count FROM session WHERE sid=?""", (sid,)).fetchone()
        if s is None:
            continue
        body, ts = "", ""
        try:
            r = conn.execute(
                "SELECT body, ts FROM search_fts WHERE sid=? AND kind<>'title' "
                "AND search_fts MATCH ? ORDER BY rank LIMIT 1",
                (sid, _fts_phrase(q))).fetchone()
            if r:
                body, ts = r["body"], r["ts"] or ""
        except Exception:
            pass
        if not body:
            r = conn.execute("SELECT body FROM search_fts WHERE sid=? AND kind='title' LIMIT 1",
                             (sid,)).fetchone()
            body = r["body"] if r else ""
        item = {
            "sid": sid,
            "title": (s["title"] or "").strip(),
            "project": s["project_key"] or "",
            "device": s["device"] or "",
            "source": s["source"] or "",
            "updated_at": s["updated_at"] or "",
            "score": round(score, 5),
            "snippet": _snippet(body, q),
            "snippet_at": ts,
            "messages": s["msg_count"] or 0,
        }
        if explain:
            item["why"] = why.get(sid, [])
        out.append(item)
    return out
