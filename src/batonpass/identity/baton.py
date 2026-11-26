"""Passing the baton between tools, without a coordinator.

Two agents can be in the same directory at the same time — one writing, one
about to. There is no server to ask who is in charge, so the only safe model is
the one version control settled on long ago: nothing is ever rewritten, and
every reader computes the same answer from the same log.

The rules, in order of how much trouble they save:

  * an event is immutable. A note, a claim, a hand-off is appended; the id is a
    hash of its content, so writing it twice is writing it once.
  * claims compete by Lamport number — a claim always outranks everything it
    could have seen. Two tools claiming at the same instant get the same
    number, which is the whole point: no clock agreement required.
  * a tie is broken by (device, event id). Arbitrary, but *identical
    everywhere*, which is the only property that matters. Both machines print
    the same winner without talking to each other.
  * the loser is kept, and reported as "claimed at the same moment". The useful
    fact is that two tools both thought they were in charge. Deleting one of
    them would be tidier and a lie.

`done` and `release` free the baton. They do not hand it to the runner-up: if
you put it down, it is down.
"""

from __future__ import annotations

import hashlib
import json
import os

from ..store.content import sha256_text

# One vocabulary for the whole log, so a note written by an agent and a claim
# written by a human land in the same column and can be read by the same code.
KINDS = ("note", "decision", "blocked", "next", "claim", "done", "release")
_FREEING = ("release", "done")


def tool_name() -> str:
    """Which agent is speaking. Set per client by `batonpass setup`."""
    return (os.environ.get("BATONPASS_TOOL") or "cli").strip() or "cli"


def _device() -> str:
    from ..sync.channel import device_name
    return device_name()


def _now() -> str:
    from .tasks import _now_iso
    return _now_iso()


# ── write ───────────────────────────────────────────────────────

def append(conn, tid: str, kind: str, body: str = "", tool: str = None,
           device: str = None, refs: dict = None) -> dict:
    """Append one event. Returns {'eid', 'lamport', 'duplicate'}.

    Repeating the newest event verbatim is a retry, not a new fact: a client
    that crashed after writing gets to try again without polluting the log.
    """
    kind = (kind or "note").strip()
    if kind not in KINDS:
        raise ValueError("unknown relay kind: %r" % kind)
    tool = tool or tool_name()
    device = device or _device()
    body = (body or "").strip()

    newest = conn.execute(
        "SELECT kind,tool,body FROM task_event WHERE tid=? ORDER BY lamport DESC, eid DESC"
        " LIMIT 1", (tid,)).fetchone()
    if newest and newest["kind"] == kind and newest["tool"] == tool \
            and (newest["body"] or "") == body:
        return {"duplicate": True, "lamport": None, "eid": None}

    lamport = conn.execute(
        "SELECT COALESCE(MAX(lamport),0)+1 FROM task_event WHERE tid=?", (tid,)
    ).fetchone()[0]
    eid = sha256_text("%s|%s|%s|%s|%s|%s" % (tid, kind, lamport, tool, device, body))[:32]
    conn.execute(
        "INSERT OR IGNORE INTO task_event(eid,tid,device,lamport,kind,body,refs_json,created_at,tool)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (eid, tid, device, lamport, kind, body,
         json.dumps(refs or {}, ensure_ascii=False) if refs else None, _now(), tool),
    )
    conn.commit()
    return {"duplicate": False, "lamport": lamport, "eid": eid}


def take(conn, tid: str, tool: str = None, note: str = "", device: str = None) -> dict:
    """Claim the baton, and say in one line what you are about to do."""
    tool = tool or tool_name()
    out = append(conn, tid, "claim", note or ("%s 接手" % tool), tool=tool, device=device)
    out["state"] = state(conn, tid)
    return out


# ── read ────────────────────────────────────────────────────────

def state(conn, tid: str) -> dict:
    """Who holds the baton, and everything that ever happened to it."""
    rows = conn.execute(
        "SELECT eid,tid,device,lamport,kind,body,refs_json,created_at,"
        "       COALESCE(tool,'') tool"
        "  FROM task_event WHERE tid=? ORDER BY lamport, created_at, eid", (tid,)
    ).fetchall()
    events = [dict(r) for r in rows]

    holder = None
    displaced = []
    for e in events:
        if e["kind"] == "claim":
            if holder is not None:
                displaced.append(holder)
            holder = e
        elif e["kind"] in _FREEING:
            if holder is not None and e["lamport"] >= holder["lamport"]:
                holder = None

    # The same-lamport claims: everyone must agree on the winner, so the log's
    # own ordering (lamport, created_at, eid) decides and the rest are listed.
    conflicts = []
    if events:
        claims = [e for e in events if e["kind"] == "claim"]
        if claims:
            top = max(c["lamport"] for c in claims)
            same = [c for c in claims if c["lamport"] == top]
            if len(same) > 1:
                conflicts = same[1:]

    # Only a `next` is a next step, and only one written since the last `done`:
    # the person who wrote it is the person who then finished it, so it is
    # exactly as stale as they left it, and a card still offering it is asking
    # the next reader to redo finished work. A `release` is the opposite — the
    # work stopped unfinished, and that plan is what the next person picks up.
    nxt = ""
    for e in reversed(events):
        if e["kind"] == "done":
            break
        if e["kind"] == "next" and (e["body"] or "").strip():
            nxt = e["body"]
            break

    return {
        "tid": tid,
        "holder": holder,
        "displaced": displaced,
        "conflicts": conflicts,
        "next_step": nxt,
        "events": events,
        "claims": sum(1 for e in events if e["kind"] == "claim"),
        "done": any(e["kind"] == "done" for e in events),
        "last_at": events[-1]["created_at"] if events else "",
    }


def others(conn, project_key: str, me: str, limit: int = 3) -> list:
    """What the other tools last did here — the only thing worth reading first.

    A claim tells you who is in the room. This tells you what they did, which
    is what you actually need before typing. Pulled from the archived sessions,
    so it works even for a tool that has no relay support and never will.
    """
    from .tasks import last_turns, sessions_of

    out = []
    for s in sessions_of(conn, project_key, limit=16):
        if (s["source"] or "") == me:
            continue
        line = ""
        try:
            # Not turns[0]: a turn that only called tools has no prose, and a
            # card that quotes nothing is worse than one that quotes an older
            # turn. Walk back until somebody actually said something.
            turns = last_turns(conn, s["sid"], s["source"], ("assistant",), 40)
            line = next((ln.strip()
                         for t in turns
                         for ln in (t.get("text") or "").splitlines()
                         if ln.strip()), "")
        except Exception:
            line = ""                      # the timeline is incomplete; say less, not wrong
        out.append(dict(tool=s["source"], when=s.get("updated_at"),
                        line=line, sid=s["sid"]))
        if len(out) >= limit:
            break
    return out


def warn(conn, project_key: str, me: str = None) -> str:
    """One line when somebody else is in this directory right now.

    Printed by `resume` and by the MCP `resume` tool, because the failure this
    prevents — two agents doing the same hour of work in the same folder — is
    silent until it is expensive.
    """
    from .tasks import task_id

    if not project_key:
        return ""
    h = state(conn, task_id(project_key))["holder"]
    if not h:
        return ""
    if (h["tool"] or "") == (me or tool_name()):
        return ""
    return ("⚠ %s 正在这个目录里干活（%s，设备 %s）%s — 先 relay 再动手"
            % (h["tool"] or "?", _when(h["created_at"]), h["device"] or "?",
               ("，说：「%s」" % _clip(h["body"], 60)) if h["body"] else ""))


def _when(iso: str) -> str:
    """Local, and relative while that is still the more useful answer."""
    from datetime import datetime, timezone

    if not iso:
        return "?"
    try:
        t = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return iso[:16].replace("T", " ")
    local = t.astimezone()
    delta = (datetime.now(timezone.utc) - t).total_seconds()
    if delta < 0:
        return "刚刚"
    if delta < 120:
        return "刚刚"
    if delta < 5400:
        return "%d 分钟前" % (delta // 60)
    if local.date() == datetime.now().date():
        return "%d 小时前" % (delta // 3600)
    if delta < 7 * 86400:
        return "%d 天前" % (delta // 86400)
    return local.strftime("%m-%d %H:%M")


def render(conn, project_key: str, place: dict = None, tool: str = None) -> str:
    """The line a human reads before deciding to type in this directory."""
    from .tasks import task_id
    from .project import display

    tid = task_id(project_key)
    st = state(conn, tid)
    me = tool or tool_name()
    head = display(project_key)
    if place and place.get("path"):
        head = "%s  %s" % (place.get("label") or head, place["path"])

    lines = ["接力  %s" % head]
    if place and place.get("tools"):
        lines.append("在座  %s%s" % (" · ".join(place["tools"]),
                                    "" if len(place["tools"]) > 1 else "（只有一家，还没有接力过）"))

    h = st["holder"]
    if h is None:
        lines.append("持有  没人 — 谁先动手谁拿" if st["events"] else "持有  没人 — 这条线还没有人写过")
    else:
        mark = "（就是你）" if h["tool"] == me else ""
        lines.append("持有  %s · %s · %s%s" % (h["tool"] or "?", _when(h["created_at"]),
                                              h["device"] or "?", mark))
        if h["body"] and h["body"] != "%s 接手" % h["tool"]:
            lines.append("接手时说  「%s」" % h["body"])
        if st["displaced"]:
            prev = st["displaced"][-1]
            lines.append("上一位  %s · %s" % (prev["tool"] or "?", _when(prev["created_at"])))

    if st["next_step"]:
        lines.append("下一步  %s" % st["next_step"])
    for o in others(conn, project_key, me):
        lines.append("另一侧  %s · %s" % (o["tool"], _when(o["when"])))
        if o["line"]:
            lines.append("        「%s」" % _clip(o["line"], 90))
    if st["done"]:
        lines.append("状态  已经收工（done）")
    for c in st["conflicts"]:
        lines.append("同时  %s 在 %s 也点了接手 — 按设备名定序，%s 在前；两条都留着"
                     % (c["tool"] or "?", _when(c["created_at"]),
                        (h or {}).get("tool") or "?"))

    notes = [e for e in st["events"] if e["kind"] == "note"]
    if notes:
        lines.append("记录  %d 条" % len(notes))
        for e in notes[-3:]:
            lines.append("      %s  %s  「%s」" % (_when(e["created_at"]), e["tool"] or "?",
                                                 _clip(e["body"], 70)))
    if st["events"]:
        lines.append("事件  %d 条（%d 次交接）· batonpass relay --note 续写"
                     % (len(st["events"]), st["claims"]))
    return "\n".join(lines)


def _clip(text: str, n: int = 100) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"
