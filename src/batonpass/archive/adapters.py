"""Source adapters.

Every adapter answers the same two questions and nothing else:

    sessions(conn)          -> what conversations exist?
    messages(conn, sid)     -> what was said in this one?

Storage, compression, idempotency and the raw layer all live in `_ingest`, so a
new source is 30 lines rather than 300.

Degradation is explicit, never silent. When a column is missing the adapter
falls back one level and records the fallback in `result.notes`:

  lossless      every field found, structure preserved
  text-fallback text recovered, tool calls flattened into the text
  skip-placeholder  rows exist but carry no recoverable text; counted, not faked
"""

from __future__ import annotations

import json
import os
import sqlite3

from ..store.content import put_content, sha256_bytes, sha256_text, short, sniff_and_decode
from ..store.write import message_key, part_key, put_message, put_part, session_key, upsert_session

ADAPTER_VER = "0.2.0"


# ── small helpers ───────────────────────────────────────────────

def columns(conn, table) -> list:
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def tables(conn) -> set:
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return set()


def pick(cols, *names):
    for n in names:
        if n in cols:
            return n
    return None


def rowdict(row):
    return dict(row) if isinstance(row, sqlite3.Row) else dict(row or {})


def extract(obj, _depth=0):
    """Pull (text, parts) out of whatever shape a message body happens to be."""
    if obj is None or _depth > 6:
        return "", []
    if isinstance(obj, bytes):
        obj, _ = sniff_and_decode(obj)
        try:
            obj = json.loads(obj.decode("utf-8", "replace"))
        except Exception:
            return obj.decode("utf-8", "replace"), []
    if isinstance(obj, str):
        s = obj.strip()
        if s[:1] in "{[":
            try:
                return extract(json.loads(s), _depth + 1)
            except Exception:
                return obj, []
        return obj, []
    if isinstance(obj, list):
        texts, parts = [], []
        for item in obj:
            t, ps = extract(item, _depth + 1)
            if t:
                texts.append(t)
            parts.extend(ps)
        return "\n".join(texts), parts
    if isinstance(obj, dict):
        kind = obj.get("type") or obj.get("kind") or ""
        if kind in ("text", "input_text", "output_text") and isinstance(obj.get("text"), str):
            return obj["text"], []
        if kind in ("reasoning", "thinking"):
            return "", [{"kind": "reasoning", "text": obj.get("text") or obj.get("thinking") or ""}]
        if kind in ("tool", "tool_call", "tool_use", "tool-invocation"):
            name = obj.get("name") or obj.get("toolName") or obj.get("tool")
            args = obj.get("input") or obj.get("args") or obj.get("parameters") or {}
            return "", [{"kind": "tool_call", "name": name,
                         "args": json.dumps(args, ensure_ascii=False, default=str)}]
        if kind in ("tool_result", "tool-result", "tool_output"):
            body = obj.get("output") or obj.get("result") or obj.get("content") or ""
            t, ps = extract(body, _depth + 1) if not isinstance(body, str) else (body, [])
            return "", ps + [{"kind": "tool_result", "text": t, "is_error": bool(obj.get("isError"))}]
        texts, parts = [], []
        for key in ("content", "parts", "text", "message", "body", "output", "result", "reasoning"):
            if key in obj:
                t, ps = extract(obj[key], _depth + 1)
                if t:
                    texts.append(t)
                parts.extend(ps)
        return "\n".join(texts), parts
    return str(obj), []


class ArchiveResult:
    def __init__(self, adapter, path):
        self.adapter = adapter
        self.path = path
        self.source_sha = ""
        self.sessions = 0
        self.messages = 0
        self.parts = 0
        self.raw_records = 0
        self.new_rows = 0
        self.bytes_in = 0
        self.bytes_stored = 0
        self.skipped = 0
        self.bad_rows = 0
        self.declared_drops = 0
        self.empty_text = 0
        self.errors = []
        self.notes = []
        self.readonly_mode = ""

    def note(self, s):
        if s not in self.notes:
            self.notes.append(s)

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items()}


# ── the one ingestion path ──────────────────────────────────────

def _ingest(conn_lib, result, codec, source, device, src_conn, sessions, messages,
            limit=None, level=1, raw_ok=True):
    seen = 0
    for s in sessions:
        if limit is not None and seen >= limit:
            break
        seen += 1
        sid = session_key(source, s["source_sid"], device)
        msgs = list(messages(src_conn, s["source_sid"]))
        s = dict(s)
        s.update(sid=sid, source=source, device=device, adapter_ver=ADAPTER_VER,
                 msg_count=len(msgs))
        if msgs:
            stamps = [m.get("ts") for m in msgs if m.get("ts")]
            if stamps:
                s["first_msg_at"] = min(stamps)
                s["last_msg_at"] = max(stamps)
        if upsert_session(conn_lib, s):
            result.new_rows += 1
        result.sessions += 1
        if raw_ok and level >= 2:
            row = json.dumps({k: v for k, v in s.items() if k != "sid"},
                             ensure_ascii=False, default=str).encode()
            if _raw(conn_lib, result, kind="session", key=s["source_sid"],
                    payload=row, codec=codec):
                result.new_rows += 1

        for i, m in enumerate(msgs):
            seq = m.get("seq")
            try:
                seq = int(seq)
            except (TypeError, ValueError):
                seq = i
            mid = message_key(sid, seq)
            text = m.get("text") or ""
            try:
                tsha = ""
                if text:
                    tsha, was_new = put_content(conn_lib, text, codec, plain=True)
                    if was_new:
                        result.bytes_stored += len(text.encode())
                if put_message(conn_lib, mid, sid, seq, m.get("role") or "unknown",
                               tsha, _as_str(m.get("model")), _as_str(m.get("ts")),
                               json.dumps(m["meta"], ensure_ascii=False, default=str)
                               if m.get("meta") else None):
                    result.new_rows += 1
                result.messages += 1
                result.declared_drops += m.get("dropped") or 0
                if not text.strip():
                    result.empty_text += 1
                    if not m.get("parts"):
                        result.skipped += 1
                for j, p in enumerate(m.get("parts") or []):
                    args, res = p.get("args"), p.get("text")
                    asha = rsha = None
                    if args:
                        asha, _ = put_content(conn_lib, str(args), codec)
                    if res:
                        rsha, _ = put_content(conn_lib, str(res), codec)
                    if put_part(conn_lib, part_key(mid, j), mid, j, p.get("kind") or "text",
                                _as_str(p.get("name")), asha, rsha, p.get("is_error"),
                                _as_str(m.get("ts"))):
                        result.new_rows += 1
                    result.parts += 1
            except sqlite3.Error as exc:
                # One malformed row must never cost you the other 47,987.
                if len(result.errors) < 8:
                    result.errors.append("message seq=%s: %s" % (seq, exc))
                result.bad_rows += 1


def _raw(conn_lib, result, kind, key, payload: bytes, codec=None) -> bool:
    from ..store.write import put_raw_record
    seq = result.raw_records + 1
    result.raw_records += 1
    if codec is not None:
        sha, was_new = put_content(conn_lib, payload, codec)
        if was_new:
            result.bytes_stored += len(payload)
        return put_raw_record(conn_lib, result.source_sha, seq, kind, sha, codec.name)
    return put_raw_record(conn_lib, result.source_sha, seq, kind, sha256_bytes(payload))


# ── zcode ───────────────────────────────────────────────────────

ZCODE_ROLE = {"user": "user", "assistant": "assistant", "system": "system", "tool": "tool"}


class ZCode:
    name = "zcode"

    @staticmethod
    def detect(conn):
        t = tables(conn)
        score, why = 0.0, []
        if {"session", "message"} <= t:
            score += 0.4
        cols = columns(conn, "message")
        if {"session_id", "data", "sequence"} <= set(cols):
            score += 0.45
            why.append("message(session_id,data,sequence)")
        if "session_entry" in t:
            score += 0.1
        return min(score, 0.99), why

    @staticmethod
    def sessions(conn):
        cols = columns(conn, "session")
        out = []
        for r in conn.execute("SELECT * FROM session ORDER BY time_created"):
            d = rowdict(r)
            out.append(dict(
                source_sid=str(d.get("id")),
                title=d.get("title"),
                cwd=d.get("directory") or d.get("path"),
                created_at=_ts(d.get("time_created")),
                updated_at=_ts(d.get("time_updated")),
                project_key=d.get("project_id") or d.get("workspace_id"),
                project_key_tier=2,
                forked_from=d.get("parent_id"),
                artifact_refs=None,
            ))
        return out

    @staticmethod
    def messages(conn, sid):
        """zcode keeps the words in `part`, not in `message.data`.

        message.data carries only routing metadata (role, model, semantics,
        tool list); every character the user or model actually wrote lives in a
        part row. Reading `message.data` alone yields 48k empty messages, which
        is exactly the kind of silent loss this project exists to avoid.
        """
        by_msg, dropped = {}, {}
        for p in conn.execute("SELECT * FROM part WHERE session_id=?", (sid,)):
            d = rowdict(p)
            try:
                obj = json.loads(d["data"])
            except Exception:
                continue
            mid_key = d["message_id"]
            part = _zcode_part(obj)
            if part is None:
                dropped[mid_key] = dropped.get(mid_key, 0) + 1
                continue
            by_msg.setdefault(mid_key, []).append((int(d["sequence"] or 0), part))

        sql = ("SELECT * FROM message WHERE session_id=? "
               "ORDER BY CAST(sequence AS INTEGER)")
        for r in conn.execute(sql, (sid,)):
            d = rowdict(r)
            try:
                obj = json.loads(d["data"])
            except Exception:
                obj = {}
            sem = (obj.get("semantics") or {})
            kind = sem.get("kind") or ""
            role = {"user_prompt": "user",
                    "assistant_response": "assistant",
                    "system_reminder": "system",
                    "todo_reminder": "system",
                    "background_notification": "system",
                    "timeline_event": "system",
                    "compact_summary": "system"}.get(kind)
            if role is None:
                role = ZCODE_ROLE.get(str(obj.get("role", "")).lower()) or "unknown"

            parts = []
            for _seq, part in sorted(by_msg.get(d["id"], []), key=lambda x: x[0]):
                if part:
                    parts.append(part)
            body = "\n".join(p["text"] for p in parts
                             if p.get("kind") == "text" and p.get("text"))
            if not body:
                summary = obj.get("summary") or {}
                body = summary.get("body") or ""
            yield dict(
                seq=d.get("sequence") or 0, role=role, text=body,
                parts=parts, dropped=dropped.get(d["id"], 0),
                ts=_ts(d.get("time_created")),
                model=_as_str((obj.get("modelSelection") or {}).get("model")
                              if isinstance(obj.get("modelSelection"), dict)
                              else obj.get("model")),
                meta={"id": d["id"], "semantics": kind,
                      "hidden": sem.get("transcriptVisibility") == "hidden"},)


def _tool_of(body):
    """Pull (name, args) out of a tool-call payload, keeping the raw text if
    the shape is unfamiliar — an unparsed call is still worth keeping."""
    try:
        obj = json.loads(body)
    except Exception:
        return None, body
    if isinstance(obj, dict):
        name = obj.get("name") or obj.get("tool") or obj.get("toolName")
        args = obj.get("args") or obj.get("input") or obj.get("arguments") or obj
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False, default=str)
        return name, args
    return None, body


def _as_str(v):
    """Sources store model/title as nested JSON often enough to matter."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    try:
        return json.dumps(v, ensure_ascii=False, default=str)[:200]
    except Exception:
        return str(v)[:200]


SKIP_PART_TYPES = {"step-start", "step-finish", "snapshot", "patch"}


def _zcode_part(obj):
    """Normalise one zcode part into the canonical part shape, or None."""
    if not isinstance(obj, dict):
        return None
    t = obj.get("type") or ""
    if t in SKIP_PART_TYPES:
        return None
    if t == "text":
        return {"kind": "text", "text": obj.get("text") or ""}
    if t == "reasoning":
        return {"kind": "reasoning", "text": obj.get("text") or ""}
    if t == "tool":
        name = obj.get("toolName") or obj.get("name") or obj.get("tool")
        args = obj.get("args") or obj.get("input") or obj.get("parameters")
        state = obj.get("state") or {}
        if isinstance(state, dict) and state.get("input") and not args:
            args = state["input"]
        result = obj.get("result") or obj.get("output")
        if result is None and isinstance(state, dict):
            result = state.get("output") or state.get("error")
        if args is not None and not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False, default=str)
        if result is not None and not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        is_error = bool(obj.get("isError")) or str(
            (state.get("status") if isinstance(state, dict) else "") or "").lower() == "error"
        return {"kind": "tool_call" if result is None else "tool_result",
                "name": name, "args": args, "text": result, "is_error": is_error}
    if t in ("file", "timeline", "compaction"):
        label = obj.get("path") or obj.get("filename") or obj.get("title") or t
        return {"kind": t, "name": label,
                "text": obj.get("text") or obj.get("content") or None}
    return {"kind": t or "unknown",
            "text": obj.get("text") or json.dumps(obj, ensure_ascii=False)[:2000]}


def _role(blob):
    try:
        obj = blob if isinstance(blob, dict) else json.loads(
            blob.decode() if isinstance(blob, bytes) else blob)
        return ZCODE_ROLE.get(str(obj.get("role", "")).lower(), obj.get("role") or "unknown")
    except Exception:
        return "unknown"


def _ts(v):
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        secs = v / 1000.0 if v > 1e11 else v
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(secs, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return str(v)
    return str(v)


# ── billion ─────────────────────────────────────────────────────

class Billion:
    name = "billion"

    @staticmethod
    def detect(conn):
        t, score, why = tables(conn), 0.0, []
        if {"sessions", "messages", "content"} <= t:
            score += 0.5
            why.append("sessions+messages+content")
        if "content_hash" in columns(conn, "messages"):
            score += 0.45
            why.append("messages.content_hash")
        return min(score, 0.99), why

    @staticmethod
    def sessions(conn):
        for r in conn.execute("SELECT * FROM sessions ORDER BY created_at"):
            d = rowdict(r)
            yield dict(
                source_sid=str(d.get("session_id")),
                title=d.get("title") or d.get("label") or d.get("host_title"),
                cwd=None,
                created_at=_ts(d.get("created_at")),
                updated_at=_ts(d.get("updated_at")),
                project_key=d.get("namespace") or d.get("origin"),
                project_key_tier=3,
                forked_from=d.get("parent_session_id"),
                artifact_refs=None,
            )

    @staticmethod
    def messages(conn, sid):
        """billion splits a turn into one row per *kind*: text, reasoning,
        tool-call, tool-result. Only `text` rows are prose; routing the rest
        through a text extractor silently throws away every tool argument,
        which is most of what makes a transcript replayable.
        """
        sql = ("SELECT m.seq,m.role,m.kind,m.ts,m.content_hash,m.identity,"
               "       c.bytes,c.byte_len FROM messages m "
               "JOIN content c ON c.hash=m.content_hash "
               "WHERE m.session_id=? ORDER BY m.seq")
        for r in conn.execute(sql, (sid,)):
            d = rowdict(r)
            kind = (d.get("kind") or "text").lower()
            body = ""
            if d.get("bytes"):
                decoded, _how = sniff_and_decode(d["bytes"])
                body = decoded.decode("utf-8", "replace") if isinstance(decoded, bytes) else str(decoded)

            text, parts = body, []
            if kind == "reasoning":
                text, parts = "", [{"kind": "reasoning", "text": body}]
            elif kind in ("tool-call", "tool_call"):
                name, args = _tool_of(body)
                text, parts = "", [{"kind": "tool_call", "name": name, "args": args}]
            elif kind in ("tool-result", "tool_result"):
                text, parts = "", [{"kind": "tool_result", "text": body,
                                    "is_error": body.lstrip()[:1] == "{" and
                                    '"error"' in body[:400]}]
            yield dict(seq=d.get("seq") or 0, role=d.get("role") or "unknown",
                       text=text, parts=parts, ts=_ts(d.get("ts")),
                       model=None,
                       meta={"kind": d.get("kind"), "identity": d.get("identity")})


# ── dsh ─────────────────────────────────────────────────────────

class Dsh:
    name = "dsh-chat"

    @staticmethod
    def detect(conn):
        cols = columns(conn, "messages")
        if {"text", "who", "session"} <= set(cols):
            return 0.95, ["messages(text,who,session)"]
        return 0.0, []

    @staticmethod
    def sessions(conn):
        t = tables(conn)
        if "sessions" in t:
            cols = columns(conn, "sessions")
            idc = pick(cols, "id", "session", "session_id")
            if idc:
                for r in conn.execute("SELECT * FROM sessions"):
                    d = rowdict(r)
                    yield dict(source_sid=str(d.get(idc)),
                               title=d.get(pick(cols, "title", "name") or "title"),
                               cwd=None, created_at=_ts(d.get(pick(cols, "started_at", "created_at", "time"))),
                               updated_at=None, project_key=None, project_key_tier=3,
                               forked_from=None, artifact_refs=None)
                return
        for r in conn.execute("SELECT session, min(time), max(time), count(*) FROM messages "
                              "GROUP BY session ORDER BY min(time)"):
            yield dict(source_sid=str(r[0]), title=None, cwd=None,
                       created_at=_ts(r[1]), updated_at=_ts(r[2]),
                       project_key=None, project_key_tier=3,
                       forked_from=None, artifact_refs=None)

    @staticmethod
    def messages(conn, sid):
        for r in conn.execute("SELECT * FROM messages WHERE session=? ORDER BY seq", (sid,)):
            d = rowdict(r)
            yield dict(seq=d.get("seq") or d.get("id") or 0,
                       role=_role_from_who(d.get("who")), text=d.get("text") or "",
                       parts=[], ts=_ts(d.get("time")), model=None,
                       meta={"who": d.get("who")})


def _role_from_who(who):
    w = str(who or "").lower()
    if w in ("user", "human"): return "user"
    if w in ("assistant", "bot", "ai", "agent"): return "assistant"
    if w in ("tool", "system"): return w
    return "unknown"


# ── alma ────────────────────────────────────────────────────────

class Alma:
    name = "alma"

    @staticmethod
    def detect(conn):
        t = tables(conn)
        if "chat_threads" in t and "chat_messages" in t:
            return 0.97, ["chat_threads+chat_messages"]
        return 0.0, []

    @staticmethod
    def sessions(conn):
        # A thread only carries a `workspace_id`; the directory lives one table
        # away. Resolving it here is what lets an Alma session line up with the
        # same project seen from dsh or zcode — otherwise every Alma project is
        # a random id and nothing can ever be handed across.
        from ..identity import project as pj

        paths = {}
        if "workspaces" in tables(conn):
            try:
                for w in conn.execute("SELECT id, path, name FROM workspaces"):
                    if w["path"]:
                        paths[str(w["id"])] = w["path"]
            except sqlite3.OperationalError:
                paths = {}                 # an older Alma without that column

        for r in conn.execute("SELECT * FROM chat_threads ORDER BY created_at"):
            d = rowdict(r)
            wid = d.get("workspace_id")
            cwd = paths.get(str(wid)) if wid else None
            if cwd:
                key, tier, _ = pj.derive(cwd, "", None)
            else:
                key, tier = (str(wid) if wid else None), 2
            yield dict(source_sid=str(d.get("id")), title=d.get("title"), cwd=cwd,
                       created_at=_ts(d.get("created_at")), updated_at=_ts(d.get("updated_at")),
                       project_key=key, project_key_tier=tier,
                       forked_from=d.get("parent_thread_id"), artifact_refs=None)

    @staticmethod
    def messages(conn, sid):
        for r in conn.execute("SELECT * FROM chat_messages WHERE thread_id=? "
                              "ORDER BY created_at, id", (sid,)):
            d = rowdict(r)
            text, parts = extract(d.get("message"))
            yield dict(seq=d.get("id") or 0, role=_role(d.get("message")), text=text,
                       parts=parts, ts=_ts(d.get("created_at") or d.get("timestamp")),
                       model=None, meta={"depth": d.get("depth")})


# ── pinky ───────────────────────────────────────────────────────

class Pinky:
    name = "pinky"

    @staticmethod
    def detect(conn):
        t = tables(conn)
        if {"tasks", "steps"} <= t:
            return 0.9, ["tasks+steps"]
        return 0.0, []

    @staticmethod
    def sessions(conn):
        cols = columns(conn, "sessions")
        title = pick(cols, "agent", "id")
        for r in conn.execute("SELECT * FROM sessions ORDER BY started_at"):
            d = rowdict(r)
            yield dict(source_sid=str(d.get("id")), title=d.get(title) or "pinky session",
                       cwd=None, created_at=_ts(d.get("started_at")),
                       updated_at=_ts(d.get("ended_at")),
                       project_key=d.get("agent"), project_key_tier=3,
                       forked_from=None, artifact_refs=None)

    @staticmethod
    def messages(conn, sid):
        for r in conn.execute("SELECT * FROM tasks WHERE session_id=? ORDER BY started_at", (sid,)):
            d = rowdict(r)
            text = "\n".join(x for x in (d.get("title"), d.get("summary"), d.get("outcome")) if x)
            parts = []
            for s in conn.execute("SELECT * FROM steps WHERE task_id=? ORDER BY idx", (d.get("id"),)):
                sd = rowdict(s)
                parts.append({"kind": "step", "name": sd.get("tool"),
                              "args": sd.get("args"),
                              "text": sd.get("intent") or sd.get("text"),
                              "is_error": (sd.get("status") or "") not in ("ok", "done", "success", "")})
            yield dict(seq=d.get("id") or 0, role="assistant", text=text, parts=parts,
                       ts=_ts(d.get("started_at")), model=d.get("agent"),
                       meta={"state": d.get("state"), "device": d.get("device_name")})


ADAPTERS = {a.name: a for a in (ZCode, Billion, Dsh, Alma, Pinky)}
# the scanner's names are the ones that matter; these are the obvious aliases
ADAPTERS["dsh"] = Dsh


def get(name):
    if name not in ADAPTERS:
        raise KeyError("unknown adapter %r; have %s" % (name, ", ".join(sorted(ADAPTERS))))
    return ADAPTERS[name]
