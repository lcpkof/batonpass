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

from ..store.codecs import Codecs

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


def _body_hits(conn, q: str, allowed: set, limit: int = 60) -> tuple[list, dict]:
    """Sessions that mention `q` in prose, plus the first snippet seen.

    Prose is stored as plain TEXT, so this is one SQLite LIKE — the scan happens
    in C. Libraries that still hold compressed prose (pulled from a peer, or
    never slimmed) are covered by a decode-and-scan fallback that only runs when
    such rows exist at all.
    """
    like = _like(q)
    rows = conn.execute(
        """SELECT m.sid sid, COUNT(*) n, MAX(m.ts) ts
             FROM message m JOIN content c ON c.sha256=m.text_sha
            WHERE c.codec='none' AND c.body LIKE ? ESCAPE '\\'
            GROUP BY m.sid ORDER BY n DESC, ts DESC LIMIT ?""", (like, limit)).fetchall()
    sids = [r["sid"] for r in rows if r["sid"] in allowed]
    snips = {}

    packed = conn.execute(
        """SELECT 1 FROM message m JOIN content c ON c.sha256=m.text_sha
            WHERE c.codec NOT IN ('none','purged') LIMIT 1""").fetchone()
    if packed:
        codecs = Codecs(conn)
        sources = {r["sid"]: r["source"] for r in conn.execute("SELECT sid, source FROM session")}
        counts = {}
        for r in conn.execute(
                """SELECT sid, ts, text_sha FROM message
                    WHERE text_sha IS NOT NULL AND text_sha<>''
                    ORDER BY sid, seq"""):
            if r["sid"] not in allowed:
                continue
            text = codecs.get(r["text_sha"], sources.get(r["sid"]))
            idx = text.lower().find(q.lower())
            if idx < 0:
                continue
            counts[r["sid"]] = counts.get(r["sid"], 0) + text.lower().count(q.lower())
            if r["sid"] not in snips:
                snips[r["sid"]] = (r["ts"] or "", _snippet(text, q))
        for sid, n in counts.items():
            if sid not in sids:
                sids.append(sid)

    for sid in sids:
        if sid in snips:
            continue
        r = conn.execute(
            """SELECT c.body body, m.ts ts FROM message m JOIN content c ON c.sha256=m.text_sha
                WHERE m.sid=? AND c.codec='none' AND c.body LIKE ? ESCAPE '\\'
                ORDER BY m.seq LIMIT 1""", (sid, like)).fetchone()
        if r:
            snips[sid] = (r["ts"] or "", _snippet(r["body"] if isinstance(r["body"], str)
                                                 else r["body"].decode("utf-8", "replace"), q))
    return sids[:limit], snips


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


_SPLIT = re.compile(r"[\s,，、;；:：/|()（）\[\]【】\"“”'<>《》!！?？.。]+")


def terms(q: str, limit: int = 6) -> list:
    """A sentence is not a key. "岗位表 join 修复" is three keys."""
    out, seen = [], set()
    for t in _SPLIT.split(q or ""):
        t = t.strip()
        if len(t) < 2 or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
    out.sort(key=len, reverse=True)
    return out[:limit]


def search(conn, q: str, at: str = None, project: str = None, device: str = None,
           limit: int = 5, explain: bool = False) -> list:
    """Return ranked sessions. Empty list is a valid, honest answer."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    hits = _search_once(conn, q, at, project, device, limit, explain)
    if hits:
        return hits
    words = terms(q)
    if len(words) < 2:
        return []
    merged = {}
    for tried, term in enumerate(words, 1):
        for it in _search_once(conn, term, at, project, device, limit, explain):
            prev = merged.get(it["sid"])
            if prev is None or it["score"] > prev["score"]:
                it = dict(it, matched=term)
                merged[it["sid"]] = it
        if tried >= 2 and len(merged) >= max(limit, 1):
            break          # two words already answered; stop scanning
    return sorted(merged.values(), key=lambda x: -x["score"])[:max(limit, 1)]


def _search_once(conn, q, at, project, device, limit, explain) -> list:
    from .index import ensure
    ensure(conn)          # the title list is the one part that needs the table
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
    body_sids, body_snips = _body_hits(conn, q, allowed)
    lists["body"] = body_sids

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
        ts, snippet = body_snips.get(sid, ("", ""))
        if not snippet:
            r = conn.execute("SELECT body FROM search_fts WHERE sid=? AND kind='title' LIMIT 1",
                             (sid,)).fetchone()
            snippet = _snippet(r["body"] if r else "", q)
        item = {
            "sid": sid,
            "title": (s["title"] or "").strip(),
            "project": s["project_key"] or "",
            "device": s["device"] or "",
            "source": s["source"] or "",
            "updated_at": s["updated_at"] or "",
            "score": round(score, 5),
            "snippet": snippet,
            "snippet_at": ts,
            "messages": s["msg_count"] or 0,
        }
        if explain:
            item["why"] = why.get(sid, [])
        out.append(item)
    return out
