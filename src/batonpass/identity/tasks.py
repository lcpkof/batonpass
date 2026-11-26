"""Grouping sessions into tasks, and rendering a card you can act on.

Everything on the card is *derived*, never written by a model: the tool calls
that happened, the files that were produced, the last thing that was said, the
session id to open. A card that guesses reads better and is useless — so where
there is no evidence, the line is omitted instead of filled in.

Two rules borrowed from the field and kept:

  * the newest session of a project wins the title (an old session keeps the
    old name forever otherwise)
  * a task untouched for a week is marked stale — a stale workbench is more
    dangerous than no workbench, because it is confidently wrong
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from ..store.content import sha256_text
from . import artifacts as af
from . import project as pj

STALE_DAYS = 7

# Tool names that mean "this task moved forward", grouped so the card says
# "写了 31 个文件" instead of listing 2,000 calls.
TOOL_GROUPS = (
    ("写", {"write", "edit", "str_replace_editor", "create_file", "apply_patch",
            "multi_edit", "notebookedit"}),
    ("读", {"read", "read_file", "view", "read_image", "readimage"}),
    ("跑", {"bash", "run_code", "exec", "shell", "js", "python", "terminal"}),
    ("查", {"websearch", "webfetch", "grep", "glob", "search", "list_dir"}),
    ("查", {"job_output", "todo_write", "todowrite"}),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def task_id(project_key: str) -> str:
    return sha256_text("task|%s" % project_key)[:32]


# ── build ───────────────────────────────────────────────────────

def build(conn, extract_artifacts: bool = True, limit: int = None, home: str = None) -> dict:
    """Recompute identity for every session. Idempotent: run it as often as you like."""
    from ..store.codecs import Codecs

    codecs = Codecs(conn)
    if extract_artifacts:
        from ..store.db import get_meta, set_meta
        if get_meta(conn, "artifact_ver") != af.ARTIFACT_VER:
            conn.execute("UPDATE session SET artifact_refs=NULL")
            set_meta(conn, "artifact_ver", af.ARTIFACT_VER)

    # Read rows *after* the reset: a row cached before it still carries the old
    # refs, and merge_refs would faithfully resurrect every one of them.
    rows = conn.execute(
        "SELECT sid, source, source_sid, device, cwd, title, updated_at, "
        "project_key, artifact_refs FROM session ORDER BY COALESCE(updated_at,'') DESC"
        + (" LIMIT %d" % int(limit) if limit else "")
    ).fetchall()

    stats = {"sessions": 0, "keys_changed": 0, "artifacts": 0, "with_artifacts": 0,
             "tiers": {}, "unknown": 0, "forks": 0, "merged_subprojects": 0,
             "kept_remote": 0}

    # Our paths say nothing about another machine's disk. A session that arrived
    # through a channel already carries the answer the machine that owned it gave
    # (t0 `pk`), so an empty local derivation must not erase it — otherwise
    # "switch machines" silently loses exactly the grouping it was for.
    from ..archive.runner import device_name
    try:
        local_device = device_name()
    except Exception:
        local_device = ""

    derived = []
    for r in rows:
        key, tier, _conf = pj.derive(r["cwd"], r["title"], home)
        derived.append((r, key, tier))

    # A sub-directory of a project is not a second project. `documents/gp` had
    # 48 sessions and `gp/stock_picker` had 2 — same work, two clusters, and the
    # small one is unreachable in a list sorted by size.
    remap = _subproject_remap([k for _r, k, _t in derived])
    stats["merged_subprojects"] = len(remap)

    for r, key, tier in derived:
        stats["sessions"] += 1
        key = remap.get(key, key)
        stats["tiers"][tier] = stats["tiers"].get(tier, 0) + 1
        if not key and (r["project_key"] or "") and (r["device"] or "") != local_device:
            stats["kept_remote"] += 1
            key = r["project_key"]
        if not key:
            stats["unknown"] += 1
        if key != (r["project_key"] or ""):
            stats["keys_changed"] += 1
            conn.execute("UPDATE session SET project_key=?, project_key_tier=? WHERE sid=?",
                         (key or None, tier if key else None, r["sid"]))
        if not extract_artifacts:
            continue
        refs = af.scan_session(conn, codecs, r["sid"], r["source"])
        if not refs:
            continue
        stats["with_artifacts"] += 1
        stats["artifacts"] += len(refs)
        merged = af.merge_refs(r["artifact_refs"], refs)
        if merged != (r["artifact_refs"] or ""):
            conn.execute("UPDATE session SET artifact_refs=? WHERE sid=?", (merged, r["sid"]))

    stats["forks"] = _link_forks(conn)
    _sync_tasks(conn)
    conn.commit()
    return stats


def _subproject_remap(keys: list) -> dict:
    """Fold a small `parent/child` cluster back into `*/parent`.

    Deliberately conservative: the parent must already be a real cluster (>=5
    sessions) and the child must be a rounding error next to it (<= max(2, 20%)),
    otherwise a genuine second project sharing a folder name gets eaten.
    """
    counts = {}
    for k in keys:
        if k:
            counts[k] = counts.get(k, 0) + 1
    parents = {}
    for k, n in counts.items():
        tail = k.rsplit("/", 1)[-1]
        if n >= 5:
            parents.setdefault(tail, (k, n))
    remap = {}
    for k, n in counts.items():
        if "/" not in k:
            continue
        head = k.split("/", 1)[0]
        parent = parents.get(head)
        if not parent or parent[0] == k:
            continue
        if n <= max(2, int(parent[1] * 0.2)):
            remap[k] = parent[0]
    return remap


def _link_forks(conn) -> int:
    """Same source session on two machines = one was continued elsewhere.

    We never merge them: the second copy keeps its own rows and points back at
    the first. A visible fork can be dealt with; a silently merged one cannot.
    """
    rows = conn.execute(
        """SELECT source, source_sid, sid, device, updated_at FROM session
            WHERE source_sid IN (
                SELECT source_sid FROM session GROUP BY source, source_sid
                HAVING COUNT(*) > 1)
            ORDER BY source, source_sid, COALESCE(updated_at,'') """
    ).fetchall()
    n, seen = 0, {}
    for r in rows:
        k = (r["source"], r["source_sid"])
        first = seen.get(k)
        if first is None:
            seen[k] = r["sid"]
            continue
        if r["sid"] != first:
            cur = conn.execute("SELECT forked_from FROM session WHERE sid=?", (r["sid"],)).fetchone()
            if cur is not None and not cur["forked_from"]:
                conn.execute("UPDATE session SET forked_from=? WHERE sid=?", (first, r["sid"]))
                n += 1
    return n


def _sync_tasks(conn) -> None:
    """One task row per project that has sessions. Title follows the newest session."""
    projects = conn.execute(
        """SELECT project_key, COUNT(*) n FROM session
            WHERE project_key IS NOT NULL AND project_key <> ''
            GROUP BY project_key"""
    ).fetchall()
    for p in projects:
        key = p["project_key"]
        tid = task_id(key)
        newest = conn.execute(
            """SELECT title, updated_at FROM session WHERE project_key=?
                ORDER BY COALESCE(updated_at,'') DESC LIMIT 1""", (key,)
        ).fetchone()
        title = pj.display(key)
        conn.execute(
            "INSERT OR IGNORE INTO task(tid,project_key,title,created_at) VALUES(?,?,?,?)",
            (tid, key, title, _now_iso()),
        )
        if newest is not None:
            conn.execute("UPDATE task SET title=? WHERE tid=?", (title, tid))


# ── read ────────────────────────────────────────────────────────

def projects(conn, min_sessions: int = 1, include_candidates: bool = False) -> list:
    sql = """SELECT project_key,
                    COUNT(*) n,
                    MAX(COALESCE(updated_at,'')) last_at,
                    MIN(COALESCE(created_at, updated_at, '')) first_at,
                    COUNT(DISTINCT device) devices,
                    GROUP_CONCAT(DISTINCT device) device_list,
                    SUM(CASE WHEN artifact_refs IS NOT NULL AND artifact_refs<>'' THEN 1 ELSE 0 END) with_files
               FROM session
              WHERE project_key IS NOT NULL AND project_key <> ''
              GROUP BY project_key HAVING n >= ?
              ORDER BY last_at DESC"""
    out = []
    for r in conn.execute(sql, (min_sessions,)):
        key = r["project_key"]
        if pj.is_candidate(key) and not include_candidates:
            continue
        out.append({
            "project_key": key,
            "label": pj.display(key),
            "sessions": r["n"],
            "last_at": r["last_at"],
            "first_at": r["first_at"],
            "devices": r["devices"],
            "device_list": (r["device_list"] or ""),
            "with_files": r["with_files"],
            "candidate": pj.is_candidate(key),
            "stale": bool(r["last_at"] and r["last_at"] < _ago_iso(STALE_DAYS)),
        })
    return out


def resolve(conn, hint: str) -> list:
    """Find the project a vague hint points at. Path, key fragment — best first."""
    hint = (hint or "").strip()
    if not hint:
        return []
    if "/" in hint or hint.startswith("~"):
        key, tier, _ = pj.derive(hint, "", None)
        if key:
            return [p for p in projects(conn, include_candidates=True) if p["project_key"] == key]
    low = hint.lower()
    hits = [p for p in projects(conn, include_candidates=True)
            if low in p["project_key"].lower() or low in p["label"].lower()]
    return sorted(hits, key=lambda p: (-(p["sessions"] or 0), p["last_at"] or ""))


def sessions_of(conn, project_key: str, limit: int = 8) -> list:
    rows = conn.execute(
        """SELECT s.sid, s.source, s.device, s.title, s.cwd, s.updated_at,
                  s.msg_count, s.artifact_refs, s.forked_from,
                  (SELECT COUNT(*) FROM message m WHERE m.sid=s.sid) AS n_msg
             FROM session s WHERE s.project_key=?
             ORDER BY COALESCE(s.updated_at,'') DESC LIMIT ?""",
        (project_key, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def tool_histogram(conn, sid: str, top: int = 5) -> list:
    rows = conn.execute(
        """SELECT p.name, SUM(COALESCE(p.n,1)) n FROM part p JOIN message m ON m.mid=p.mid
            WHERE m.sid=? AND p.kind='tool_call' AND p.name IS NOT NULL
            GROUP BY LOWER(p.name) ORDER BY n DESC LIMIT ?""",
        (sid, top),
    ).fetchall()
    return [{"name": r["name"], "count": r["n"]} for r in rows]


def _unwrap(text: str) -> str:
    from ..store.text import clean
    return clean(text or "")


def last_turns(conn, sid: str, source: str, roles=("assistant",), limit: int = 1) -> list:
    from ..store.codecs import Codecs
    codecs = Codecs(conn)
    q = ("SELECT role, text_sha, ts FROM message WHERE sid=? AND role IN (%s) "
         "ORDER BY seq DESC LIMIT ?" % ",".join("?" * len(roles)))
    rows = conn.execute(q, (sid,) + tuple(roles) + (limit,)).fetchall()
    out = []
    for r in rows:
        out.append({"role": r["role"], "ts": r["ts"],
                    "text": _unwrap(codecs.get(r["text_sha"], source))})
    return out


def first_ask(conn, sid: str, source: str) -> str:
    from ..store.codecs import Codecs
    codecs = Codecs(conn)
    r = conn.execute(
        "SELECT text_sha FROM message WHERE sid=? AND role='user' ORDER BY seq LIMIT 1",
        (sid,)).fetchone()
    return _unwrap(codecs.get(r["text_sha"], source)) if r else ""


def artifact_list(row) -> list:
    try:
        return json.loads(row or "[]") or []
    except (ValueError, TypeError):
        return []


# ── render ──────────────────────────────────────────────────────

def _when(iso: str) -> str:
    return (iso or "")[:16].replace("T", " ") or "—"


def _clip(text: str, n: int = 150) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def card(conn, project_key: str, sessions: int = 4) -> str:
    """The handover card. Everything printed here is evidence, not inference."""
    rows = sessions_of(conn, project_key, sessions)
    if not rows:
        return "no sessions for %s" % pj.display(project_key)
    total = conn.execute("SELECT COUNT(*) c FROM session WHERE project_key=?",
                         (project_key,)).fetchone()["c"]
    newest = rows[0]
    lines = []
    lines.append("任务  %s" % pj.display(project_key))
    devices = sorted({(r["device"] or "?") for r in rows})
    age = newest.get("updated_at") or ""
    stale = bool(age and age < _ago_iso(STALE_DAYS))
    lines.append("状态  %s%s" % (
        "%d 个会话 · 最近 %s 由 %s 更新" % (total, _when(age), newest.get("device") or "?"),
        "  ⚠ 已 %s 天没动" % STALE_DAYS if stale else "",
    ))
    if len(devices) > 1:
        lines.append("设备  %s" % " · ".join(devices))

    # Roll up across the project's recent sessions: the newest session is often
    # a question, and the file you want was made three sessions ago.
    bag = {}
    for row in rows:
        for ref in artifact_list(row.get("artifact_refs")):
            if ref.get("name") and ref["name"] not in bag:
                bag[ref["name"]] = ref
    refs = sorted(bag.values(),
                  key=lambda d: (af.rank(d.get("name")), str(d.get("ts") or "")), reverse=True)
    if refs:
        first = refs[0]
        lines.append("产出  %s%s" % (first.get("name"),
                                     "    ← %s" % _when(first.get("ts")) if first.get("ts") else ""))
        for r in refs[1:4]:
            lines.append("      %s" % r.get("name"))
        if len(refs) > 4:
            lines.append("      …另外 %d 个" % (len(refs) - 4))

    hist = tool_histogram(conn, newest["sid"], 4)
    if hist:
        lines.append("做过  %s" % " · ".join("%s×%d" % (h["name"], h["count"]) for h in hist))

    # What someone wrote down on purpose. This is the only part of a card that
    # is not inferred from a transcript, so it goes near the top.
    try:
        tid = task_id(project_key)
        events = conn.execute(
            "SELECT kind, body FROM task_event WHERE tid=? AND COALESCE(body,'')<>''"
            " ORDER BY lamport DESC LIMIT 2", (tid,)
        ).fetchall()
    except Exception:                      # a library from before task_event
        events = []
    for e in reversed(events):
        label = {"blocked": "卡住", "decision": "决定", "done": "完成"}.get(e["kind"], "笔记")
        lines.append("备注  %s：%s" % (label, _clip(e["body"], 80)))

    ask = first_ask(conn, newest["sid"], newest["source"])
    if ask:
        lines.append("原话  「%s」" % _clip(ask, 60))

    # The newest assistant turn is often a tool call with no prose of its own —
    # dsh splits one turn into a row per kind — so walk back to the last one that
    # actually said something instead of printing a timestamp and a blank.
    tail = [t for t in last_turns(conn, newest["sid"], newest["source"],
                                  ("assistant",), 40)
            if (t.get("text") or "").strip()]
    if tail:
        # One line, not the whole answer: a reply that starts with a table turns
        # into `| | 今日必装 | …` once the whitespace is collapsed.
        said = next((ln.strip() for ln in (tail[0]["text"] or "").splitlines()
                     if ln.strip()), "")
        lines.append("最后  助手 %s: %s" % (_when(tail[0]["ts"]),
                                           _clip(said, 90)))

    for r in rows[1:sessions]:
        fork = "  (fork of %s)" % (r["forked_from"] or "")[:8] if r.get("forked_from") else ""
        lines.append("其他  %s  %s  %s 条%s" % (
            (r["sid"] or "")[:8], _when(r.get("updated_at")), r.get("n_msg") or 0, fork))
    return "\n".join(lines)


def card_for_session(conn, sid: str) -> str:
    row = conn.execute(
        "SELECT sid, source, device, title, cwd, project_key, updated_at, msg_count FROM session WHERE sid=?",
        (sid,)).fetchone()
    if row is None:
        return "no such session: %s" % sid
    if row["project_key"]:
        return card(conn, row["project_key"])
    return "\n".join([
        "任务  （无项目·单次会话）",
        "会话  %s  %s  %s" % (sid[:8], row["source"], _when(row["updated_at"])),
        "原话  「%s」" % _clip(first_ask(conn, sid, row["source"]), 80),
    ])


def resume_hint(conn, cwd: str, home: str = None) -> str:
    """Proactive recall: the machine you just sat down at knows what you were doing."""
    key, tier, _ = pj.derive(cwd, "", home or _home())
    if not key:
        return ""
    rows = sessions_of(conn, key, 1)
    if not rows:
        return ""
    return card(conn, key, 3)


def _home() -> str:
    import os
    return os.path.expanduser("~")
