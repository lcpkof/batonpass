"""A small MCP server: three tools, because recall needs three verbs.

An agent does not need a hundred endpoints. It needs to *look something up*
(`memory_search`), to *write down what it just learned* (`memory_add`), and to
*be handed the baton* (`resume`). Everything else is a CLI subcommand, which is
the right shape for a human and the wrong shape for a model.

The transport is the standard one: newline-delimited JSON-RPC 2.0 on stdin and
stdout. No daemon, no port, no background process — the client starts this, and
when it exits there is nothing left running. Paths and ports are exactly the
things that break on a machine you just sat down at.

`memory_add` is the only tool that writes, and it appends an event. It never
rewrites history and never touches anyone else's data.
"""

from __future__ import annotations

import json
import sys

from . import __version__

PROTOCOL = "2024-11-05"
SERVER_NAME = "batonpass"

TOOLS = [
    {
        "name": "memory_search",
        "description": (
            "Search everything this person has ever done with an AI tool — on every "
            "machine they own — by a vague phrase rather than an exact query. Use it "
            "when you need to know whether something was already done, where a file "
            "came from, or what the last state of a task was. Returns sessions, not "
            "documents. An empty result is a real answer: report it as not found "
            "instead of guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "what you half-remember, e.g. 岗位表 or 反爬"},
                "at": {"type": "string",
                       "description": "time window: 7d, 12h, today, 2026-09, "
                                      "2026-09-01..2026-09-20"},
                "project": {"type": "string",
                            "description": "restrict to one project (path, or part of its name)"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_add",
        "description": (
            "Record a fact about the work in progress so the next machine inherits it: "
            "a decision, a dead end, a path that matters. Appends an event to the "
            "project's task; it never edits history. Use it when you learn something "
            "the person would hate to repeat."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "one sentence, in their language"},
                "project": {"type": "string",
                            "description": "project path or name; defaults to the cwd"},
                "kind": {"type": "string", "default": "note",
                         "enum": ["note", "decision", "blocked", "done"]},
            },
            "required": ["text"],
        },
    },
    {
        "name": "resume",
        "description": (
            "Hand over the baton for a project: the card with what it is, what was "
            "produced, what was done, what is stuck and what is next, plus any fork "
            "(the same task touched on two machines). Call this at the start of a "
            "session when the person says 'where was I' or names a project."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "project path or name; defaults to the cwd"},
                "sessions": {"type": "integer", "default": 4,
                             "description": "how many recent sessions to roll up"},
            },
        },
    },
]


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _need(args, key):
    v = args.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError("missing required argument: %s" % key)
    return v


def _project_key(conn, hint) -> str:
    """Turn a path, a name fragment, or nothing at all into a project key."""
    from .identity import project as pj
    from .identity import tasks as tk

    hint = (hint or "").strip()
    if not hint:
        import os
        hint = os.getcwd()
    if "/" in hint or hint.startswith("~"):
        key, _tier, _ = pj.derive(hint, "", None)
        if key:
            return key
    hits = tk.resolve(conn, hint)
    return hits[0]["project_key"] if hits else ""


def tool_search(conn, args) -> str:
    from .recall import ensure_index, search

    q = _need(args, "query")
    ensure_index(conn)          # a search must never answer "nothing" out of neglect
    hits = search(conn, q, at=args.get("at"), project=args.get("project"),
                  limit=int(args.get("limit") or 5))
    if not hits:
        # The silence contract: nothing found is said out loud, never papered over.
        return ("not found — no session on this machine matches %r. Do not invent one; "
                "say you could not find it." % q)
    out = ["%d session(s) match %r" % (len(hits), q)]
    for h in hits:
        out.append("")
        out.append("%s  %s  %s  %s" % (
            (h.get("sid") or "")[:8], (h.get("updated_at") or "")[:16],
            h.get("device") or "?", h.get("title") or ""))
        if h.get("project"):
            out.append("  project  %s" % h["project"])
        if h.get("snippet"):
            out.append("  …%s" % h["snippet"])
        if h.get("why"):
            out.append("  matched  %s" % ", ".join(h["why"]))
    return "\n".join(out)


def tool_add(conn, args) -> str:
    from .identity import artifacts as af  # noqa: F401  (keeps ver constant importable)
    from .identity import tasks as tk
    from .sync.channel import device_name
    from .store.db import set_meta

    text = _need(args, "text")
    kind = (args.get("kind") or "note").strip() or "note"
    key = _project_key(conn, args.get("project"))
    if not key:
        return ("cannot record that: no project matches %r. Pass the project directory "
                "or name, or run from it." % (args.get("project") or "the cwd"))
    tid = tk.task_id(key)
    conn.execute(
        "INSERT OR IGNORE INTO task(tid,project_key,title,created_at) VALUES(?,?,?,?)",
        (tid, key, key, tk._now_iso()),
    )
    lamport = conn.execute(
        "SELECT COALESCE(MAX(lamport),0)+1 FROM task_event WHERE tid=?", (tid,)
    ).fetchone()[0]
    import hashlib
    eid = hashlib.sha256(("%s|%s|%s" % (tid, lamport, text)).encode("utf-8")).hexdigest()[:32]
    conn.execute(
        "INSERT OR IGNORE INTO task_event(eid,tid,device,lamport,kind,body,refs_json,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (eid, tid, device_name(), lamport, kind, text, None, tk._now_iso()),
    )
    conn.commit()
    return "recorded %s for %s (event %s)" % (kind, key, eid[:8])


def tool_resume(conn, args) -> str:
    from .identity import tasks as tk

    key = _project_key(conn, args.get("project"))
    if not key:
        return ("nothing to hand over: no project matches %r. Try memory_search first."
                % (args.get("project") or "the cwd"))
    return tk.card(conn, key, int(args.get("sessions") or 4))


_HANDLERS = {"memory_search": tool_search, "memory_add": tool_add, "resume": tool_resume}


def handle(conn, method: str, params: dict):
    """One JSON-RPC call in, one result out. `None` means 'no response'."""
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = (params or {}).get("name")
        args = (params or {}).get("arguments") or {}
        fn = _HANDLERS.get(name)
        if fn is None:
            raise LookupError("unknown tool: %s" % name)
        try:
            return _text(fn(conn, args))
        except ValueError as e:               # bad arguments are the caller's fault
            raise
        except Exception as e:                 # a broken library must not kill the session
            return {"content": [{"type": "text", "text": "error: %s" % e}],
                    "isError": True}
    raise NotImplementedError("unknown method: %s" % method)


def serve(db_path=None, instream=None, outstream=None) -> int:
    """Read requests until stdin closes. Nothing is left running afterwards."""
    from .store.db import connect
    from .paths import db_path as default_db, ensure_home

    inp = instream or sys.stdin
    out = outstream or sys.stdout
    path = str(db_path or default_db())
    conn = None
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict) or "method" not in msg:
            continue                          # a response, not a request: ignore
        rid = msg.get("id")
        try:
            if conn is None:
                import os
                if not os.path.exists(path):
                    ensure_home()             # a first run creates an empty library
                conn = connect(path)
            result = handle(conn, msg["method"], msg.get("params") or {})
            reply = None if msg["method"].startswith("notifications/") else \
                {"jsonrpc": "2.0", "id": rid, "result": result}
        except NotImplementedError as e:
            reply = {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32601, "message": str(e)}}
        except LookupError as e:
            reply = {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32602, "message": str(e)}}
        except ValueError as e:
            reply = {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32602, "message": str(e)}}
        except Exception as e:
            reply = {"jsonrpc": "2.0", "id": rid,
                     "error": {"code": -32603, "message": "%s: %s" % (type(e).__name__, e)}}
        if reply is None:
            continue
        out.write(json.dumps(reply, ensure_ascii=False) + "\n")
        out.flush()
    if conn is not None:
        conn.close()
    return 0