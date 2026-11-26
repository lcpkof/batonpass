"""One project, every tool, one handle.

Each agent keeps its own idea of "a workspace" and none of them can read the
others': Alma has a `workspaces` table, dsh a `storages/workspace.json` per
instance, zcode nothing at all — it just stamps a cwd onto every session. Three
incompatible models, one thing in common: an absolute directory. So the
registry below is a join on that path and nothing else.

Two rules keep it honest. A row exists only because some tool named that
directory — no guessed projects, no synthesised paths. And every source is
opened read-only: a tool that is missing, locked, or mid-write costs its own
rows and nothing else, because a listing that fails entirely is worse than a
listing that is short one tool.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .identity import project as pj
from .store.db import source_ro


def norm_path(path) -> str:
    """The one canonical spelling of a directory: absolute, no trailing slash."""
    p = os.path.expanduser(str(path or "").strip())
    if not p or not p.startswith("/"):
        return ""
    p = os.path.normpath(p)
    return p if p == "/" else p.rstrip("/")


# ── where each tool keeps its workspaces ────────────────────────

def _alma(home: str) -> list:
    """Alma: a sqlite table, live. Read-only, and a locked db is not fatal."""
    db = Path(home, "Library/Application Support/alma/chat_threads.db")
    if not db.exists():
        return []
    conn = None
    try:
        conn, _mode = source_ro(str(db))
        rows = conn.execute("SELECT id, path, name FROM workspaces").fetchall()
        return [dict(tool="alma", ref=str(r["id"]), path=r["path"], label=r["name"],
                     sessions=0, last_at=None) for r in rows]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def _dsh(home: str) -> list:
    """dsh: one JSON file per instance (~/.dsh, ~/.dsh-alice, ~/.dsh-yannick)."""
    out = []
    for f in sorted(Path(home).glob(".dsh*/storages/workspace.json")):
        instance = f.parent.parent.name.lstrip(".")
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue                       # a half-written file is not an error
        tables = (data.get("tables") or {}).get("workspaces") or {}
        for wid, w in tables.items():
            if not isinstance(w, dict):
                continue
            out.append(dict(tool="dsh", ref="%s/%s" % (instance, wid),
                            path=w.get("path"), label=w.get("title"),
                            sessions=len(w.get("sessionIds") or []),
                            last_at=w.get("updatedAt") or w.get("updated_at")))
    return out


def _sessions(conn) -> list:
    """Anything the library saw a real cwd for — the tool already told us."""
    rows = conn.execute(
        "SELECT source, cwd, COUNT(*) n, MAX(COALESCE(last_msg_at, updated_at, '')) last"
        "  FROM session WHERE cwd IS NOT NULL AND cwd <> '' GROUP BY source, cwd"
    ).fetchall()
    return [dict(tool=r["source"], ref="", path=r["cwd"], label=None,
                 sessions=r["n"], last_at=r["last"]) for r in rows]


def collect(conn, home: str = None) -> list:
    """Every (tool, path) claim this machine can evidence."""
    home = home or os.path.expanduser("~")
    rows = []
    for r in _sessions(conn) + _alma(home) + _dsh(home):
        path = norm_path(r.get("path"))
        if not path or not os.path.isdir(path):
            continue                       # a workspace whose folder is gone is not a place
        r["path"] = path
        rows.append(r)
    return rows


def refresh(conn, home: str = None) -> int:
    """Repopulate the registry. Idempotent; safe to run on every listing."""
    from .identity.tasks import _now_iso

    now = _now_iso()
    seen = set()
    for r in collect(conn, home):
        key = (r["path"], r["tool"], r.get("ref") or "")
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            "INSERT INTO place(path,tool,ref,label,sessions,last_at,seen_at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(path,tool,ref) DO UPDATE SET"
            "   label=COALESCE(excluded.label,place.label),"
            "   sessions=MAX(COALESCE(place.sessions,0),COALESCE(excluded.sessions,0)),"
            "   last_at=CASE WHEN COALESCE(excluded.last_at,'') > COALESCE(place.last_at,'')"
            "                THEN excluded.last_at ELSE place.last_at END,"
            "   seen_at=excluded.seen_at",
            (r["path"], r["tool"], r.get("ref") or "", r.get("label"),
             int(r.get("sessions") or 0), r.get("last_at"), now),
        )
    conn.commit()
    return len(seen)


# ── read ────────────────────────────────────────────────────────

_GENERIC = {"workspace", "default", "main", "root", "home", "scratch", "tmp",
            "project", "repo", "app", "code", "untitled"}


def _name(path: str, label: str, key: str = "") -> str:
    """What to call this directory in a listing.

    Two sources, and neither is always right. A tool's label is a human name —
    except when it isn't: Alma titles a workspace after the first thing you
    typed in it, so one of them is called 「参考上面的图，帮我做 几张 手抄绘本」.
    A folder name is stable and short — except when it's `workspace`, which
    names nothing at all. So: take the label when it reads like a name, and the
    folder when the label reads like a sentence or the folder is the only thing
    that says anything.
    """
    base = os.path.basename(path.rstrip("/")) or pj.display(key) or path
    label = (label or "").strip()
    if not label:
        return base
    if base.lower() in _GENERIC:
        return label
    if len(label) > 32 or " " in label:
        return base
    return label


def merged(conn, home: str = None) -> list:
    """One row per directory, with every tool that named it.

    `tools` is the whole point: a place with two tools in it is a place you can
    hand work between, and a place with one is just somebody's folder.
    """
    home = home or os.path.expanduser("~")
    by_path = {}
    for r in conn.execute("SELECT path,tool,ref,label,sessions,last_at FROM place"):
        row = by_path.setdefault(r["path"], {
            "path": r["path"], "tools": [], "labels": [], "sessions": 0,
            "last_at": "", "refs": {},
        })
        if r["tool"] not in row["tools"]:
            row["tools"].append(r["tool"])
        if r["label"] and r["label"] not in row["labels"]:
            row["labels"].append(r["label"])
        row["sessions"] += int(r["sessions"] or 0)
        row["last_at"] = max(row["last_at"], r["last_at"] or "")
        row["refs"].setdefault(r["tool"], []).append(r["ref"])

    # A directory's session count from the registry can double-count (two
    # tools, two rows). The library's own sessions are the ground truth.
    counts = {r["cwd"]: r["n"] for r in conn.execute(
        "SELECT cwd, COUNT(*) n FROM session WHERE cwd IS NOT NULL AND cwd<>''"
        " GROUP BY cwd")}

    out = []
    for path, row in by_path.items():
        key, tier, _ = pj.derive(path, "", home)
        row["project_key"] = key
        row["tier"] = tier
        row["sessions"] = counts.get(path, row["sessions"] if len(row["tools"]) == 1 else 0)
        row["label"] = _name(path, row["labels"][0] if row["labels"] else "", key)
        row["exists"] = os.path.isdir(path)
        out.append(row)
    out.sort(key=lambda r: (r["last_at"] or "", len(r["tools"])), reverse=True)
    return out


def find(conn, hint: str, home: str = None) -> list:
    """Resolve a path, a tail of one, or a name fragment to places."""
    hint = (hint or "").strip()
    rows = merged(conn, home)
    if not hint:
        return []
    as_path = norm_path(hint)
    if as_path:
        exact = [r for r in rows if r["path"] == as_path]
        if exact:
            return exact
    low = hint.lower()
    hits = [r for r in rows
            if low in r["path"].lower() or low in (r["label"] or "").lower()]
    return sorted(hits, key=lambda r: (len(r["tools"]), r["last_at"] or ""), reverse=True)
