#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batonpass discover — v0.1

Read-only inventory of the AI history already sitting on this machine.

It answers three questions and nothing else:
  1. What is here?
  2. How much of it can I actually read?
  3. What am I silently failing to read, and why?

Guarantees
  * never writes to any source (sqlite opened with mode=ro)
  * never opens a socket (no urllib.request, no http, no socket in this file)
  * the only file it creates is the report you ask for

Usage
  python3 discover.py                        # report -> ./batonpass-report.html
  python3 discover.py --report ~/r.html      # choose output
  python3 discover.py --json out.json        # machine readable too
  python3 discover.py --deep                 # also measure payload bytes (slower)
  python3 discover.py --explain              # print detection reasoning
  python3 discover.py --only zcode,billion   # limit sources

Python 3.9+. No third-party packages.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path

try:
    from .. import __version__ as VERSION
except Exception:  # run as a loose script
    VERSION = "0.2.0"

# --------------------------------------------------------------------------
# Known shapes.
#
# Detection is by *shape*, not by path: we look at which tables exist and
# which columns they have. Paths change between versions, shapes are stable.
#
#   required  : tables that must all be present
#   forbidden : tables that must all be absent (used to tell siblings apart)
#   sessions  : (table, timestamp column) — one row per conversation
#   messages  : (table, timestamp column) — one row per message
#   parts     : (table, timestamp column) — tool calls / reasoning / images
# --------------------------------------------------------------------------

KNOWN = [
    {
        "adapter": "zcode",
        "role": "transcripts",
        "label": "ZCode",
        "required": {"session", "message", "part"},
        "forbidden": {"project", "session_share"},
        "sessions": ("session", "time_created"),
        "messages": ("message", "time_created"),
        "parts": ("part", "time_created"),
        "title_col": ("session", "title"),
        "path_col": ("session", "directory"),
        "counters": ["session", "message", "part", "todo", "session_entry",
                     "tool_usage", "turn_usage", "model_usage", "session_task_link"],
        "notes": "sessions may fork (parent_id); payload sits in .data JSON columns",
    },
    {
        "adapter": "zcode-tasks",
        "role": "index",
        "label": "ZCode task index",
        "required": {"tasks", "projects", "hosts"},
        "forbidden": {"session", "message"},
        "sessions": None,
        "messages": None,
        "parts": None,
        "title_col": ("tasks", "title"),
        "path_col": ("tasks", "workspace_path"),
        "counters": ["tasks", "projects", "hosts", "automations",
                     "automation_runs", "off_peak_tasks", "session_creation_commands"],
        "notes": "the map, not the transcript: task → workspace → session id",
    },
    {
        "adapter": "zhanlu",
        "role": "transcripts",
        "label": "Zhanlu",
        "required": {"project", "session", "message", "part"},
        "forbidden": {"session_entry"},
        "sessions": ("session", "time_created"),
        "messages": ("message", "time_created"),
        "parts": ("part", "time_created"),
        "title_col": ("session", "title"),
        "path_col": ("session", "directory"),
        "counters": ["project", "session", "message", "part", "todo"],
        "notes": "same shape as ZCode minus the extra tables; usually empty",
    },
    {
        "adapter": "billion",
        "role": "transcripts",
        "label": "billion-context",
        "required": {"sessions", "messages", "content"},
        "forbidden": set(),
        "sessions": ("sessions", "created_at"),
        "messages": ("messages", "ts"),
        "parts": None,
        "title_col": ("sessions", "title"),
        "path_col": None,
        "counters": ["sessions", "messages", "content", "blocks", "refs",
                     "message_media", "conversations"],
        "notes": "content is content-addressed; duplicates stored once",
    },
    {
        "adapter": "dsh-chat",
        "role": "transcripts",
        "label": "dsh chat index",
        "required": {"messages", "lines"},
        "forbidden": {"calls"},
        "sessions": None,
        "messages": ("messages", "time"),
        "parts": None,
        "title_col": None,
        "path_col": None,
        "counters": ["messages", "lines", "marks"],
        "notes": "one row per message; session id is a plain string, no session table",
    },
    {
        "adapter": "dsh-usage",
        "role": "usage",
        "label": "dsh usage ledger",
        "required": {"calls", "marks"},
        "forbidden": {"lines"},
        "sessions": None,
        "messages": None,
        "parts": None,
        "title_col": None,
        "path_col": None,
        "counters": ["calls", "marks", "owners", "call_slots"],
        "notes": "token accounting only — no transcripts here",
    },
    {
        "adapter": "dsh-state",
        "role": "state",
        "label": "dsh orchestrator state",
        "required": {"kv", "trigger", "outbox"},
        "forbidden": set(),
        "sessions": None,
        "messages": None,
        "parts": None,
        "title_col": None,
        "path_col": None,
        "counters": ["kv", "trigger", "thread", "decision", "outbox", "ticket"],
        "notes": "control state, not conversation content",
    },
    {
        "adapter": "alma",
        "role": "transcripts",
        "label": "Alma",
        "required": {"chat_threads", "chat_messages"},
        "forbidden": set(),
        "sessions": ("chat_threads", "created_at"),
        "messages": ("chat_messages", "timestamp"),
        "parts": None,
        "title_col": ("chat_threads", "title"),
        "path_col": None,
        "counters": ["chat_threads", "chat_messages", "agent_op_traces",
                     "agent_op_trace_steps", "activity_sessions", "activity_events",
                     "activity_ocr_frames", "memories", "crystals", "thread_briefs"],
        "notes": "message bodies live in chat_messages.message (JSON)",
    },
    {
        "adapter": "pinky",
        "role": "transcripts",
        "label": "Pinky",
        "required": {"tasks", "steps", "devices"},
        "forbidden": set(),
        "sessions": ("tasks", "started_at"),
        "messages": ("steps", "started_at"),
        "parts": None,
        "title_col": ("tasks", "title"),
        "path_col": None,
        "counters": ["tasks", "steps", "sessions", "approvals", "media", "recordings"],
        "notes": "agent run traces; steps carry tool args + screen text",
    },
]

# Where to look. Globs allowed. A hit may be a sqlite file or a directory.
CANDIDATES = [
    "~/.zcode/**/*.sqlite",
    "~/.zcode/**/*.db",
    "~/.local/share/billion-context/**/index.db",
    "~/.dsh/**/*.sqlite",
    "~/.dsh/**/*.db",
    "~/Library/Application Support/alma/*.db",
    "~/Library/Application Support/Pinky/*.db",
    "~/.local/share/zhanlu/*.db",
    "~/.claude/**/*.db",
    "~/.codex/**/*.db",
    "~/.cursor/**/*.db",
    "~/.config/**/chat*.db",
]

# Directory-shaped sources: not a database, a folder of transcript files.
# (pattern, adapter, file glob, label) — the adapter reads it, the scanner only
# says where it is and how much sits there.
DIR_CANDIDATES = [
    ("~/.dsh*/sessions", "dsh-sessions", "session.v*.jsonl.zstd",
     "dsh session files (zstd JSONL, one per session)"),
]

# Directories that look like AI tooling but may hold history we do not parse yet.
PROBE_ROOTS = [
    "~/.claude", "~/.codex", "~/.cursor", "~/.gemini", "~/.continue",
    "~/.aider", "~/.zcode", "~/.dsh", "~/.local/share", "~/.config",
    "~/Library/Application Support",
]

SKIP_DIR_NAMES = {"node_modules", ".git", "__pycache__", "Cache", "Caches",
                  "GPUCache", "Code Cache", "logs", "tmp", "temp"}

# Files that are definitely not transcripts. Skipped by reason, and the reasons
# are counted in the report — a skip you can see is not a silent skip.
NOISE_FRAGMENTS = {
    "browser-cache": ("/code cache/", "/gpucache/", "/dawn", "/shadercache/",
                      "/grshadercache/", "/service worker/", "/local storage/",
                      "/session storage/", "/cache/"),
    "browser-profile": ("chromium", "bsk-chrome", "chrome-publish", "baidu-profile",
                        "google/chrome", "/firefox/", "/safari/"),
    "browser-internal": ("first_party_sets", "heavy_ad_intervention",
                         "criticalactions", "top_sites", "web data",
                         "network persistent state", "/cookies", "/history",
                         "/favorites", "/login data"),
    "backup-copy": (".bak-", ".backup", "/backup/", "-backup-"),
    "nested-artifact": ("/workspace/draft-", "/node_modules/", "/site-packages/"),
}

TEXTISH = {".jsonl", ".json", ".md", ".txt", ".log", ".ndjson"}

# Magic bytes of the compression formats these tools actually use. A blob is
# only called "compressed" when its first bytes say so — never because a column
# is named `algo`.
CODEC_MAGIC = [
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x04\x22\x4d\x18", "lz4"),
    (b"\x00\x00\x00\x0dJXL", "jxl"),
]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def human(n) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def norm_ts(v):
    """Turn whatever a database calls a timestamp into an aware datetime, or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e17:            # nanoseconds
            x /= 1e9
        elif x > 1e14:          # microseconds
            x /= 1e6
        elif x > 1e11:          # milliseconds
            x /= 1e3
        if x < 1e8 or x > 4e9:  # implausible
            return None
        try:
            return dt.datetime.fromtimestamp(x, dt.timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return norm_ts(int(s))
    s2 = s.replace("Z", "+00:00").replace(" ", "T", 1)
    for cand in (s2, s[:19]):
        try:
            d = dt.datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc).astimezone()
        except ValueError:
            continue
    return None


def day(v) -> str:
    if not v:
        return "—"
    return v.strftime("%Y-%m-%d")


def dir_size(path: Path, budget=40000):
    """Rough size + file count, bounded so a huge directory cannot hang the scan."""
    total, files, seen = 0, 0, 0
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for name in names:
            seen += 1
            if seen > budget:
                return total, files, True
            try:
                total += (Path(root) / name).stat().st_size
                files += 1
            except OSError:
                pass
    return total, files, False


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------

def open_ro(path: Path):
    """Read-only sqlite connection.

    Returns (conn, mode). mode is "ro" normally; "immutable" when the database is
    in WAL mode and the sidecar files are not readable for us — `immutable=1`
    tells sqlite to touch nothing at all, at the cost of possibly seeing a
    snapshot that is mid-write. Both are strictly read-only; neither can modify
    the source.
    """
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2.0)
        conn.text_factory = lambda b: b.decode("utf-8", "replace")
        conn.execute("select 1").fetchone()
        return conn, "ro"
    except sqlite3.OperationalError:
        if conn is not None:
            conn.close()
        conn = sqlite3.connect(path.as_uri() + "?immutable=1", uri=True, timeout=2.0)
        conn.text_factory = lambda b: b.decode("utf-8", "replace")
        conn.execute("select 1").fetchone()
        return conn, "immutable"


def table_names(conn) -> set:
    rows = conn.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def match_adapter(tables: set, columns: dict):
    """Exact fingerprint first, then a looser column-set match, then give up."""
    for spec in KNOWN:
        if spec["required"] <= tables and not (spec["forbidden"] & tables):
            return spec, "high", "fingerprint"
    # loose: how many required tables are present at all?
    best, best_score = None, 0.0
    for spec in KNOWN:
        overlap = len(spec["required"] & tables) / max(1, len(spec["required"]))
        if overlap > best_score:
            best, best_score = spec, overlap
    if best and best_score >= 0.6:
        return best, "medium", "table-overlap"
    # content heuristic: does any table smell like a conversation?
    for t, cols in columns.items():
        low = {c.lower() for c in cols}
        if "role" in low and ({"content", "text", "message", "data"} & low):
            return None, "low", "content-heuristic"
    return None, "none", "unknown"


def count_rows(conn, table) -> int | None:
    try:
        return int(conn.execute(f'select count(*) from "{table}"').fetchone()[0])
    except sqlite3.Error:
        return None


def ts_range(conn, table, col):
    try:
        lo, hi = conn.execute(f'select min("{col}"), max("{col}") from "{table}"').fetchone()
    except sqlite3.Error:
        return None, None
    return norm_ts(lo), norm_ts(hi)


def payload_bytes(conn, table, cols) -> int:
    """Sum of blob/text lengths for the payload-ish columns. Slow: --deep only."""
    total = 0
    existing = {c.lower() for c in cols}
    for col in ("data", "bytes", "message", "text", "content", "body", "payload"):
        if col in existing:
            try:
                total += int(conn.execute(
                    f'select coalesce(sum(length("{col}")),0) from "{table}"'
                ).fetchone()[0] or 0)
            except sqlite3.Error:
                pass
    return total


def probe_sqlite(path: Path, deep: bool, explain: bool) -> dict:
    res = {
        "kind": "sqlite",
        "path": str(path),
        "size": path.stat().st_size,
        "adapter": None,
        "role": None,
        "label": None,
        "confidence": "none",
        "detected_by": None,
        "tables": 0,
        "counters": {},
        "sessions": None,
        "messages": None,
        "parts": None,
        "first": None,
        "last": None,
        "payload_bytes": None,
        "notes": "",
        "degradations": [],
    }
    try:
        conn, ro_mode = open_ro(path)
        if ro_mode == "immutable":
            res["degradations"].append(
                ("readonly-immutable",
                 "no lock possible (WAL sidecars unreadable); opened immutable so the "
                 "source stays untouched — a snapshot taken mid-write may be torn"))
    except sqlite3.DatabaseError as e:
        msg = str(e).lower()
        if "locked" in msg or "busy" in msg:
            res["degradations"].append(("locked", f"database is in use: {e}"))
        elif "not a database" in msg or "file is encrypted" in msg:
            res["degradations"].append(("not-a-database", "file is not a readable SQLite database"))
        else:
            res["degradations"].append(("open-failed", str(e)[:200]))
        return res
    except OSError as e:
        res["degradations"].append(("permission-denied", str(e)[:200]))
        return res

    try:
        tables = table_names(conn)
        res["tables"] = len(tables)
        columns = {}
        for t in sorted(tables):
            try:
                columns[t] = [r[1] for r in conn.execute(f'pragma table_info("{t}")')]
            except sqlite3.Error:
                columns[t] = []

        spec, confidence, how = match_adapter(tables, columns)
        if explain:
            print(f"    tables({len(tables)}): {', '.join(sorted(tables))[:400]}")
            print(f"    -> {spec['adapter'] if spec else '(none)'} "
                  f"confidence={confidence} by={how}")
        res["confidence"] = confidence
        res["detected_by"] = how

        if spec is None:
            if confidence == "low":
                res["degradations"].append(
                    ("schema-unknown",
                     "conversation-shaped tables found but no adapter matches yet"))
            return res

        res["adapter"] = spec["adapter"]
        res["role"] = spec.get("role", "transcripts")
        res["label"] = spec["label"]
        res["notes"] = spec.get("notes", "")

        for t in spec.get("counters", []):
            if t in tables:
                n = count_rows(conn, t)
                if n is not None:
                    res["counters"][t] = n

        for slot in ("sessions", "messages", "parts"):
            spec_slot = spec.get(slot)
            if not spec_slot:
                continue
            t, col = spec_slot
            if t not in tables:
                continue
            res[slot] = count_rows(conn, t)
            if col in columns.get(t, []):
                lo, hi = ts_range(conn, t, col)
                if lo and (res["first"] is None or lo < res["first"]):
                    res["first"] = lo
                if hi and (res["last"] is None or hi > res["last"]):
                    res["last"] = hi
            else:
                res["degradations"].append(
                    ("no-timestamp", f'{t} has no "{col}" column — date range unknown'))

        if deep:
            buf = 0
            for t in spec.get("counters", []):
                if t in tables:
                    buf += payload_bytes(conn, t, columns.get(t, []))
            res["payload_bytes"] = buf or None

        # source-specific facts worth reporting
        if spec["adapter"] == "zcode" and "session" in tables:
            if "parent_id" in columns.get("session", []):
                try:
                    forks = conn.execute(
                        "select count(*) from session where parent_id is not null "
                        "and parent_id <> ''").fetchone()[0]
                    if forks:
                        res["counters"]["forked_sessions"] = int(forks)
                except sqlite3.Error:
                    pass
        if spec["adapter"] == "billion" and "content" in tables:
            # Do not trust the column called "algo": in this schema it names the
            # hash, not the codec. Look at the bytes themselves.
            codec = None
            try:
                sample = conn.execute(
                    "select substr(bytes,1,4) from content "
                    "where bytes is not null limit 5").fetchall()
                for (head,) in sample:
                    if isinstance(head, str):
                        head = head.encode("utf-8", "replace")
                    for magic, name in CODEC_MAGIC:
                        if head and head.startswith(magic):
                            codec = name
                            break
                    if codec:
                        break
                if not codec and sample:
                    res["notes"] += " · content decodes as plain text, no decoder needed"
            except sqlite3.Error:
                pass
            if codec:
                res["degradations"].append(
                    ("compressed-payload", f"content blobs are {codec} frames — needs a decoder"))
        if spec["adapter"] == "billion" and "message_media" in tables:
            n = res["counters"].get("message_media")
            if n:
                res["degradations"].append(
                    ("media-by-reference", f"{n} media rows reference files, not copied"))

        if res["sessions"] == 0 and res["messages"] in (0, None) and not res["counters"].get("messages"):
            res["degradations"].append(("empty-source", "recognised, but contains no rows yet"))

        if (res["messages"] or 0) > 0 and res["first"] is None:
            res["degradations"].append(("date-range-unknown", "no usable timestamps"))

    except sqlite3.Error as e:
        res["degradations"].append(("query-failed", str(e)[:200]))
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return res


def probe_dir(path: Path, deep: bool) -> dict:
    """A directory of loose files (jsonl / md / json) — the fallback shape."""
    total, files, truncated = dir_size(path)
    exts, samples = {}, []
    for root, dirs, names in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
        for name in names:
            ext = Path(name).suffix.lower()
            exts[ext] = exts.get(ext, 0) + 1
            if ext in TEXTISH and len(samples) < 3:
                samples.append(str(Path(root) / name))
    res = {
        "kind": "dir",
        "path": str(path),
        "size": total,
        "adapter": None,
        "label": path.name,
        "confidence": "low",
        "detected_by": "path",
        "tables": 0,
        "counters": {"files": files},
        "sessions": None,
        "messages": None,
        "parts": None,
        "first": None,
        "last": None,
        "payload_bytes": None,
        "notes": ", ".join(f"{k or '(none)'}={v}" for k, v in
                           sorted(exts.items(), key=lambda x: -x[1])[:5]),
        "degradations": [],
    }
    if truncated:
        res["degradations"].append(
            ("scan-truncated", "directory too large; counts are a lower bound"))
    if not (TEXTISH & set(exts)):
        res["degradations"].append(
            ("no-transcripts", "no .jsonl/.json/.md files — probably caches or binaries"))
    return res


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def skip_reason(path: Path):
    """Why this file cannot possibly hold transcripts. None = worth probing."""
    p = str(path).lower()
    for reason, frags in NOISE_FRAGMENTS.items():
        if any(f in p for f in frags):
            return reason
    return None


def resolve(patterns):
    import glob as _glob
    seen, out = set(), []
    for pat in patterns:
        for hit in _glob.glob(os.path.expanduser(pat), recursive=True):
            rp = Path(hit)
            if rp.is_file() and rp.suffix.lower() not in (".db", ".sqlite", ".sqlite3", ""):
                continue
            if rp.name.endswith(("-wal", "-shm", ".tmp")):
                continue
            key = str(rp)
            if key in seen:
                continue
            seen.add(key)
            out.append(rp)
    return out


def looks_like_ai_dir(name: str) -> bool:
    low = name.lower()
    keys = ("claude", "codex", "cursor", "gemini", "zcode", "dsh", "aider",
            "continue", "copilot", "cline", "goose", "windsurf", "trae",
            "opencode", "crush", "zed", "chat", "agent", "llm", "ai-", "-ai")
    return any(k in low for k in keys)


# A directory that looks like AI tooling but is structurally not history: a
# bundled runtime, an Electron profile's caches, a system dir. These sat in the
# "unclaimed" list forever — which reads like a promise to collect them, and is
# really just noise. Name the reason and close the ticket.
_NOT_HISTORY = (
    ("dsh-runtime", "a bundled Node runtime, not transcripts"),
    ("Application Support/ZCode", "an Electron profile — caches and IndexedDB, "
                                  "the chat db lives in ~/.zcode"),
    ("ContextStoreAgent", "an Apple system directory"),
    ("/dsh-usage", "a token-usage ledger, not a transcript"),
    ("/dsh-session-archive", "an empty archive — only a state marker inside"),
    (".dsh-upgrade-backup", "a copy of another home taken before an upgrade"),
    ("/.workbuddy/traces", "OpenTelemetry spans with truncated payloads — the "
                           "conversation itself never lands on disk"),
)
_TRANSCRIPT_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".jsonl", ".json")


def _transcript_files(root, depth: int = 3) -> int:
    n, base = 0, str(root).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base >= depth:
            dirnames[:] = []
        n += sum(1 for f in filenames if f.endswith(_TRANSCRIPT_SUFFIXES))
    return n


def not_history_reason(path) -> str:
    """Why this directory cannot hold what we archive — or "" if we don't know."""
    text = str(path)
    for frag, reason in _NOT_HISTORY:
        if frag in text:
            return reason
    try:
        if _transcript_files(path) == 0:
            return "no file that could hold a transcript inside"
    except OSError:
        return ""
    return ""


def probe_dir_sources(keep=None, explain: bool = False):
    """Find directory-shaped transcript stores and describe them honestly.

    Message counts stay None on purpose: counting them means decompressing every
    file, which is a job for `archive`, not for a read-only scan.
    """
    import glob as _glob
    out = []
    for pattern, adapter, glob, label in DIR_CANDIDATES:
        for hit in sorted(_glob.glob(os.path.expanduser(pattern))):
            base = Path(hit)
            if not base.is_dir():
                continue
            named = not_history_reason(base) if (
                "backup" in base.name or ".bak-" in str(base)) else ""
            if named:
                if explain:
                    print(f"    not history: {base}  ({named})")
                continue
            try:
                files = sorted(base.glob("*/*/%s" % glob))
            except OSError:
                continue
            if not files:
                continue
            size = 0
            for f in files:
                try:
                    size += f.stat().st_size
                except OSError:
                    pass
            stamps = []
            for f in files[:2000]:
                try:
                    stamps.append(f.stat().st_mtime)
                except OSError:
                    pass
            if keep is not None and adapter not in keep:
                continue
            out.append({
                "kind": "dir", "path": str(base), "size": size, "adapter": adapter,
                "label": label, "confidence": "high", "detected_by": [label],
                "tables": 0, "counters": {}, "sessions": len(files), "messages": None,
                "parts": None,
                "first": human_ts(min(stamps)) if stamps else None,
                "last": human_ts(max(stamps)) if stamps else None,
                "payload_bytes": None, "notes": "counted, not decompressed",
                "degradations": [], "role": "transcripts",
            })
            if explain:
                print(f"  probe {base}  ({adapter}, {len(files)} files, {human(size)})")
    return out


def human_ts(v):
    """A datetime, not a string: the report formats these itself."""
    try:
        return dt.datetime.fromtimestamp(float(v)).astimezone()
    except Exception:
        return None


def probe_roots(claimed: set, explain: bool):
    """Directories that look like AI tooling but hold nothing we parse.

    Returns (unclaimed, ignored): things we cannot explain, and things we can.
    """
    found, ignored = [], []
    for pattern in PROBE_ROOTS:
        base = Path(os.path.expanduser(pattern))
        if not base.exists():
            continue
        try:
            children = [c for c in base.iterdir() if c.is_dir()]
        except (PermissionError, OSError):
            continue
        for child in children:
            if child.name in SKIP_DIR_NAMES:
                continue
            key = str(child.resolve())
            if key in claimed or not looks_like_ai_dir(child.name):
                continue
            size, files, trunc = dir_size(child)
            if files == 0:
                continue
            reason = not_history_reason(child)
            if reason:
                ignored.append({"path": str(child), "size": size, "files": files,
                                "reason": reason})
                if explain:
                    print(f"    not history: {child}  ({reason})")
                continue
            found.append({"path": str(child), "size": size, "files": files,
                          "truncated": trunc})
            if explain:
                print(f"    unclaimed? {child}  files={files} size={human(size)}")
    found.sort(key=lambda x: -x["size"])
    ignored.sort(key=lambda x: -x["size"])
    return found, ignored


def run(args):
    sources, claimed = [], set()
    skipped = {}
    targets = resolve(CANDIDATES)
    keep = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None

    for path in targets:
        try:
            if path.is_dir():
                continue
            reason = skip_reason(path)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                if args.explain:
                    print(f"  skip  {reason:16} {path}")
                continue
            if path.stat().st_size < 4096:
                skipped["too-small"] = skipped.get("too-small", 0) + 1
                continue
            if args.explain:
                print(f"  probe {path}")
            res = probe_sqlite(path, args.deep, args.explain)
            claimed.add(str(path.parent.resolve()))
            claimed.add(str(path.resolve()))
            if keep is not None and res["adapter"] not in keep:
                continue
            sources.append(res)
        except (OSError, PermissionError) as e:
            sources.append({"kind": "sqlite", "path": str(path), "size": 0,
                            "adapter": None, "label": None, "confidence": "none",
                            "detected_by": None, "tables": 0, "counters": {},
                            "sessions": None, "messages": None, "parts": None,
                            "first": None, "last": None, "payload_bytes": None,
                            "notes": "", "degradations": [("permission-denied", str(e)[:200])]})

    for src in probe_dir_sources(keep=keep, explain=args.explain):
        claimed.add(str(Path(src["path"]).resolve()))
        sources.append(src)

    if keep:
        unclaimed, ignored = [], []
    else:
        unclaimed, ignored = probe_roots(claimed, args.explain)

    # identical copies (a .bak sibling, a cloned profile) must not be counted twice
    seen_sig, deduped = set(), []
    for s in sources:
        sig = (s["adapter"], s["size"], s["sessions"], s["messages"])
        if s["adapter"] and sig in seen_sig:
            skipped["duplicate-copy"] = skipped.get("duplicate-copy", 0) + 1
            continue
        seen_sig.add(sig)
        deduped.append(s)
    sources = deduped

    role_rank = {"transcripts": 0, "index": 1, "usage": 2, "state": 3}
    sources.sort(key=lambda s: (s["adapter"] is None,
                                role_rank.get(s.get("role"), 9),
                                -(s["messages"] or 0), -(s["size"] or 0)))

    tot_msgs = sum(s["messages"] or 0 for s in sources)
    tot_sess = sum(s["sessions"] or 0 for s in sources)
    tot_bytes = sum(s["size"] or 0 for s in sources)
    tot_payload = sum(s["payload_bytes"] or 0 for s in sources)
    recognised = [s for s in sources if s["adapter"]]
    readable = [s for s in recognised if (s["messages"] or 0) > 0]
    deg_count = {}
    for s in sources:
        for code, _ in s["degradations"]:
            deg_count[code] = deg_count.get(code, 0) + 1

    summary = {
        "batonpass": VERSION,
        "generated_at": now_iso(),
        "host": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "?"),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "sources": sources,
        "unclaimed": unclaimed,
        "ignored": ignored,
        "skipped": skipped,
        "totals": {
            "sources": len(sources),
            "recognised": len(recognised),
            "with_messages": len(readable),
            "sessions": tot_sess,
            "messages": tot_msgs,
            "bytes_on_disk": tot_bytes,
            "payload_bytes": tot_payload or None,
            "degradation_kinds": len(deg_count),
            "skipped_files": sum(skipped.values()),
            "unclaimed_dirs": len(unclaimed),
            "ignored_dirs": len(ignored),
        },
    }
    return summary


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

CSS = """
:root{--bg:#0f1115;--fg:#e6e8ee;--mut:#8b93a7;--card:#161923;--line:#252a37;
--ok:#3ecf8e;--warn:#e2b93b;--bad:#e2605c;--acc:#6aa6ff}
@media(prefers-color-scheme:light){:root{--bg:#fbfbfd;--fg:#15171c;--mut:#6b7280;
--card:#fff;--line:#e5e7eb;--ok:#12885b;--warn:#a1740a;--bad:#b03a36;--acc:#2557c7}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
"Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:17px;margin:38px 0 12px;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px}
.hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:12px;margin:26px 0 8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.kpi .n{font-size:24px;font-weight:600;letter-spacing:-.02em}
.kpi .l{color:var(--mut);font-size:12px;margin-top:2px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin:10px 0}
.card h3{margin:0 0 2px;font-size:15px}
.path{color:var(--mut);font-size:12px;word-break:break-all;font-family:ui-monospace,
SFMono-Regular,Menlo,monospace}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));
gap:10px;margin:12px 0}
.grid div{font-size:12px;color:var(--mut)}
.grid b{display:block;font-size:15px;color:var(--fg);font-weight:600}
.pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:99px;
border:1px solid var(--line);color:var(--mut);margin-left:6px}
.pill.high{color:var(--ok);border-color:var(--ok)}
.pill.medium{color:var(--warn);border-color:var(--warn)}
.pill.low,.pill.none{color:var(--bad);border-color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);
vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:12px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.deg{color:var(--bad)}
.note{color:var(--mut);font-size:12px;margin-top:8px}
.foot{margin-top:48px;color:var(--mut);font-size:12px;border-top:1px solid var(--line);
padding-top:16px}
.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:10px}
.bar i{display:block;height:100%;background:var(--ok)}
"""


def esc(v) -> str:
    import html as _h
    return _h.escape(str(v if v is not None else "—"))


def render(summary) -> str:
    t = summary["totals"]
    rows = []

    rows.append(f"""<div class="wrap">
<h1>batonpass <span class="sub">discover v{esc(summary['batonpass'])}</span></h1>
<div class="sub">{esc(summary['host'])} · {esc(summary['platform'])} · python {esc(summary['python'])}
 · generated {esc(summary['generated_at'])}</div>

<div class="hero">
  <div class="kpi"><div class="n">{t['messages']:,}</div><div class="l">messages found</div></div>
  <div class="kpi"><div class="n">{t['sessions']:,}</div><div class="l">conversations</div></div>
  <div class="kpi"><div class="n">{t['recognised']}</div><div class="l">sources recognised</div></div>
  <div class="kpi"><div class="n">{human(t['bytes_on_disk'])}</div><div class="l">readable on disk</div></div>
  <div class="kpi"><div class="n">{t['unclaimed_dirs']}</div><div class="l">dirs in the queue</div></div>
  <div class="kpi"><div class="n">{t.get('ignored_dirs', 0)}</div><div class="l">left alone on purpose</div></div>
</div>""")

    recognised = [s for s in summary["sources"] if s["adapter"]]
    unknown = [s for s in summary["sources"]
               if not s["adapter"] and (s["size"] or 0) > 262144]

    rows.append("<h2>What can be archived today</h2>")
    for s in recognised:
        conf = s["confidence"]
        bits = []
        if s["sessions"] is not None:
            bits.append(f"<div><b>{s['sessions']:,}</b>conversations</div>")
        if s["messages"] is not None:
            bits.append(f"<div><b>{s['messages']:,}</b>messages</div>")
        if s["parts"] is not None:
            bits.append(f"<div><b>{s['parts']:,}</b>parts / tool calls</div>")
        extra = [(k, v) for k, v in s["counters"].items()
                 if k not in ("session", "sessions", "message", "messages", "part",
                              "chat_threads", "chat_messages", "tasks", "steps")]
        for k, v in extra[:4]:
            bits.append(f"<div><b>{v:,}</b>{esc(k)}</div>")
        bits.append(f"<div><b>{human(s['size'])}</b>file size</div>")
        if s["payload_bytes"]:
            bits.append(f"<div><b>{human(s['payload_bytes'])}</b>payload bytes</div>")
        span = "—"
        if s["first"] or s["last"]:
            span = f"{day(s['first'])} → {day(s['last'])}"
        bits.append(f"<div><b>{esc(span)}</b>date range</div>")

        degs = "".join(
            f'<tr><td class="deg">{esc(c)}</td><td>{esc(d)}</td></tr>'
            for c, d in s["degradations"])
        deg_html = (f'<table><tr><th>degradation</th><th>why</th></tr>{degs}</table>'
                    if degs else '<div class="note">no degradation</div>')

        rows.append(f"""<div class="card">
  <h3>{esc(s['label'] or 'unrecognised')}
    <span class="pill {esc(conf)}">{esc(conf)}</span>
    <span class="pill">{esc(s.get('role') or s['detected_by'])}</span></h3>
  <div class="path">{esc(s['path'])}</div>
  <div class="grid">{''.join(bits)}</div>
  {deg_html}
  {f'<div class="note">{esc(s["notes"])}</div>' if s["notes"] else ''}
</div>""")

    if t["messages"]:
        covered = sum(s["messages"] or 0 for s in recognised
                      if s["confidence"] in ("high", "medium"))
        pct = min(100, round(100 * covered / max(1, t["messages"])))
        verified = "100%" if pct == 100 else f"{pct}%"
        rows.append(f"""<div class="card">
<div><b>{verified}</b> of the messages on this machine sit in sources with a known
shape — those are archivable today, at the confidence shown above.</div>
<div class="bar"><i style="width:{pct}%"></i></div></div>""")

    if unknown:
        ur = "".join(
            f"<tr><td><code>{esc(u['path'])}</code></td><td>{human(u['size'])}</td>"
            f"<td>{u['tables']}</td><td>{u['confidence']}</td></tr>"
            for u in unknown[:40])
        rows.append(f"""<h2>Databases with no adapter yet ({len(unknown)})</h2>
<div class="card">
<table><tr><th>file</th><th>size</th><th>tables</th><th>confidence</th></tr>{ur}</table>
<div class="note">Most of these are browser caches and app settings. The ones worth a
second look are the ones with 10+ tables and no obvious browser in the path.
Open an issue with the path and table names — never with conversation content.</div>
</div>""")

    if summary["unclaimed"]:
        ur = "".join(
            f"<tr><td><code>{esc(u['path'])}</code></td><td>{u['files']:,}"
            f"{'+' if u['truncated'] else ''}</td><td>{human(u['size'])}</td></tr>"
            for u in summary["unclaimed"][:40])
        rows.append(f"""<h2>Looks like AI tooling, but no readable history yet</h2>
<div class="card">
<table><tr><th>directory</th><th>files</th><th>size</th></tr>{ur}</table>
<div class="note">These are not bugs. They are the queue. If one of them is yours,
open an issue with the directory name — do not paste conversation content.</div>
</div>""")

    if summary.get("ignored"):
        ur = "".join(
            f"<tr><td><code>{esc(u['path'])}</code></td><td>{human(u['size'])}</td>"
            f"<td>{esc(u['reason'])}</td></tr>" for u in summary["ignored"][:20])
        rows.append(f"""<h2>Looked at, and deliberately not touched</h2>
<div class="card">
<table><tr><th>directory</th><th>size</th><th>why</th></tr>{ur}</table>
<div class="note">A runtime, a browser profile, or a system folder holds no
transcript. Listing them as "unclaimed" would be a promise to collect something
that does not exist, so they are named once and closed.</div>
</div>""")

    if summary["totals"]["degradation_kinds"]:
        rows.append(f"""<h2>What this report does not see</h2>
<div class="card">
<p style="margin:0 0 8px">Every degradation above is deliberate: rather than writing
a placeholder that looks like data, batonpass names the gap so you can judge it.</p>
<ul style="margin:0;padding-left:20px;color:var(--mut);font-size:13px">
<li><b>locked</b> — the tool was mid-write; close it and re-run.</li>
<li><b>compressed-payload / reasoning-encrypted</b> — the tool stored it in a form
that needs a decoder we have not written.</li>
<li><b>media-by-reference</b> — files are pointed at, not copied. By design.</li>
<li><b>schema-unknown</b> — conversation-shaped tables with no adapter. The queue.</li>
</ul></div>""")

    skipped = summary.get("skipped") or {}
    if skipped:
        sk = ", ".join(f"{esc(k)} × {v}" for k, v in sorted(skipped.items(), key=lambda x: -x[1]))
        rows.append(f"""<div class="card"><div class="note">
<b>{sum(skipped.values())} files were skipped on purpose:</b> {sk}.<br>
Browser caches, backup copies and nested build artifacts cannot hold transcripts,
so probing them would only add noise — but the count stays here so you can see
that the skip happened.</div></div>""")

    rows.append(f"""<div class="foot">
<p><b>This report was produced read-only.</b> Every database was opened with
<code>mode=ro</code>; nothing was written outside this file; no network call was made;
no telemetry exists.</p>
<p>Re-run any time: <code>python3 discover.py</code></p>
</div></div>""")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>batonpass discover — {esc(summary['host'])}</title>
<style>{CSS}</style></head><body>{''.join(rows)}</body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="batonpass",
        description="Read-only inventory of local AI history (batonpass v%s)" % VERSION,
        epilog="archive / find / resume are designed but not shipped yet — "
               "see docs/design-v1.md")
    ap.add_argument("command", nargs="?", default="discover", choices=["discover"],
                    help="what to do (default: discover)")
    ap.add_argument("--report", default="batonpass-report.html", help="HTML output path")
    ap.add_argument("--json", dest="json_path", help="also write machine-readable JSON here")
    ap.add_argument("--deep", action="store_true",
                    help="measure payload bytes (slower, reads full tables)")
    ap.add_argument("--only", help="comma separated adapters, e.g. zcode,billion")
    ap.add_argument("--explain", action="store_true", help="print detection reasoning")
    args = ap.parse_args(argv)

    # `--json -` means "the JSON is the answer": stdout stays machine-readable
    # and nothing is written to disk, so it can be piped.
    to_stdout = args.json_path == "-"
    say = sys.stderr if to_stdout else sys.stdout

    print(f"batonpass discover v{VERSION} — read-only scan", file=say)
    summary = run(args)

    out = None
    if not to_stdout:
        out = Path(os.path.expanduser(args.report))
        out.write_text(render(summary), encoding="utf-8")
    if args.json_path and not to_stdout:
        Path(os.path.expanduser(args.json_path)).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
    if to_stdout:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    t = summary["totals"]
    print(f"\n  {t['messages']:,} messages · {t['sessions']:,} conversations · "
          f"{t['recognised']} sources recognised · {human(t['bytes_on_disk'])}",
          file=say)
    print(f"  {t['skipped_files']} files skipped (browser caches, backups) · "
          f"{t['unclaimed_dirs']} dirs in the queue · "
          f"{t.get('ignored_dirs', 0)} left alone on purpose", file=say)
    if out is not None:
        print(f"  report → {out}", file=say)
    if args.json_path and not to_stdout:
        print(f"  json   → {args.json_path}", file=say)
    return 0


if __name__ == "__main__":
    sys.exit(main())
