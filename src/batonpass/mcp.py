"""A small MCP server: five tools, and no more.

An agent does not need a hundred endpoints. It needs to *look something up*
(`memory_search`), to *write down what it just learned* (`memory_add`), to *be
handed the baton* (`resume`), to *see where the work lives* (`places`), and to
*say it is taking it this time* (`relay`). Everything else is a CLI subcommand,
which is the right shape for a human and the wrong shape for a model.

The transport is the standard one: newline-delimited JSON-RPC 2.0 on stdin and
stdout. No daemon, no port, no background process — the client starts this, and
when it exits there is nothing left running. Paths and ports are exactly the
things that break on a machine you just sat down at.

`memory_add` and `relay` are the only tools that write, and both append an
event. Neither rewrites history, and neither touches anyone else's data.
"""

from __future__ import annotations

import json
import sqlite3
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
    {
        "name": "places",
        "description": (
            "Every directory the person works in, with all the tools that ever "
            "touched it (alma, dsh, zcode …), merged by path. Use it when you need "
            "to know where the work actually lives, or which projects are shared "
            "between tools — a project with two tools in it is one you can hand over."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "shared": {"type": "boolean", "default": False,
                           "description": "only places two or more tools have worked in"},
                "all": {"type": "boolean", "default": False,
                        "description": "include scratch directories with no project name"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "relay",
        "description": (
            "Who has the baton in this project right now, and what the other tools "
            "last did there. Call it bare to read; pass take/note/next/done to write. "
            "`take` claims the project before you start so a second agent in the same "
            "folder does not duplicate the work. Claims are append-only events, so "
            "concurrent claims are kept and resolved deterministically, never lost."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string",
                            "description": "project path or name; defaults to the cwd"},
                "take": {"type": "boolean",
                         "description": "claim the baton before working"},
                "note": {"type": "string",
                         "description": "one line about what you did or decided"},
                "next": {"type": "string", "description": "the next concrete step"},
                "done": {"type": "boolean", "description": "finish and free the baton"},
                "release": {"type": "boolean",
                            "description": "put it down unfinished, handing it back"},
                "tool": {"type": "string",
                         "description": "your name here (defaults to BATONPASS_TOOL)"},
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
    if hits:
        return hits[0]["project_key"]
    # Not in the library yet, but some tool calls that directory a workspace:
    # the registry still knows the name, so an agent can work there first and
    # sort out the archive later.
    from . import places
    places.refresh(conn)
    ph = places.find(conn, hint)
    return ph[0]["project_key"] if ph else ""


def tool_search(conn, args) -> str:
    from .recall import ensure_index, search

    q = _need(args, "query")
    ensure_index(conn)          # a search must never answer "nothing" out of neglect
    hits = search(conn, q, at=args.get("at"), project=args.get("project"),
                  limit=int(args.get("limit") or 5))
    if not hits:
        # The silence contract: nothing found is said out loud, never papered over.
        msg = ("not found — no session on this machine matches %r. Do not invent one; "
               "say you could not find it." % q)
        from .recall import undecodable_hint

        hint = undecodable_hint(conn)
        return msg + ("\n" + hint if hint else "")
    words = sorted({h["matched"] for h in hits if h.get("matched")})
    head = "%d session(s) match %r" % (len(hits), q)
    if words:
        # the phrase itself hit nothing; say which words did, so nobody thinks
        # the whole sentence was found
        head += " (no exact phrase — matched %s)" % " / ".join(words)
    out = [head]
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


def _ensure_task(conn, key: str) -> str:
    from .identity import project as pj
    from .identity import tasks as tk

    tid = tk.task_id(key)
    conn.execute(
        "INSERT OR IGNORE INTO task(tid,project_key,title,created_at) VALUES(?,?,?,?)",
        (tid, key, pj.display(key), tk._now_iso()),
    )
    conn.commit()
    return tid


def tool_add(conn, args) -> str:
    from .identity import baton

    text = _need(args, "text")
    kind = (args.get("kind") or "note").strip() or "note"
    key = _project_key(conn, args.get("project"))
    if not key:
        return ("cannot record that: no project matches %r. Pass the project directory "
                "or name, or run from it." % (args.get("project") or "the cwd"))
    tid = _ensure_task(conn, key)
    out = baton.append(conn, tid, kind, text)
    if out.get("duplicate"):
        return "already recorded for %s — nothing added" % key
    return "recorded %s for %s (event %s)" % (kind, key, (out["eid"] or "")[:8])


def tool_places(conn, args) -> str:
    """Every directory any tool calls a workspace, merged by the path."""
    from . import places

    places.refresh(conn)
    rows = places.merged(conn)
    if args.get("shared"):
        rows = [r for r in rows if len(r["tools"]) > 1]
    if not args.get("all"):
        rows = [r for r in rows if r["project_key"]]
    rows = rows[:int(args.get("limit") or 20)]
    if not rows:
        return "no places registered yet — run `batonpass archive` once."
    out = ["%s · %s · %d 会话 · %s · %s" % (
        r["label"], "+".join(r["tools"]), r["sessions"], (r["last_at"] or "")[:16],
        r["path"]) for r in rows]
    return "\n".join(out)


def tool_relay(conn, args) -> str:
    """Read the baton card, or write to it — one tool, because an agent that can
    read the room should be able to claim it in the same breath."""
    from . import places
    from .identity import baton

    key = _project_key(conn, args.get("project"))
    if not key:
        return ("no place matches %r — pass a directory or run from it"
                % (args.get("project") or "the cwd"))
    tid = _ensure_task(conn, key)
    tool = (args.get("tool") or baton.tool_name()).strip()

    wrote = []
    note = (args.get("note") or "").strip()
    if args.get("take"):
        r = baton.take(conn, tid, tool=tool, note=note)
        wrote.append("claimed" + (" (already)" if r.get("duplicate") else ""))
        note = ""
    for field, kind in (("note", "note"), ("next", "next")):
        text = note if field == "note" else (args.get(field) or "").strip()
        if text:
            r = baton.append(conn, tid, kind, text, tool=tool)
            wrote.append(kind + (" (already)" if r.get("duplicate") else ""))
    for field, kind in (("done", "done"), ("release", "release")):
        if args.get(field):
            baton.append(conn, tid, kind, "", tool=tool)
            wrote.append(kind)

    places.refresh(conn)
    place = None
    for r in places.merged(conn):
        if r["project_key"] == key:
            place = r
            break
    body = baton.render(conn, key, place, tool=tool)
    if wrote:
        return "wrote: %s\n\n%s" % (" · ".join(wrote), body)
    return body


def tool_resume(conn, args) -> str:
    from .identity import baton
    from .identity import tasks as tk

    key = _project_key(conn, args.get("project"))
    if not key:
        return ("nothing to hand over: no project matches %r. Try memory_search first."
                % (args.get("project") or "the cwd"))
    body = tk.card(conn, key, int(args.get("sessions") or 4))
    held = baton.warn(conn, key)
    return body + ("\n\n" + held if held else "")


_HANDLERS = {"memory_search": tool_search, "memory_add": tool_add, "resume": tool_resume,
             "places": tool_places, "relay": tool_relay}


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
        finally:
            # A Python sqlite3 SELECT leaves a read transaction open, and this
            # process lives for the whole app session. One open reader is enough
            # to block every WAL checkpoint: the -wal file grew to 600 MB and
            # `archive` started answering "database is locked". Close the snapshot
            # after each request so writes elsewhere stay possible.
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
        if reply is None:
            continue
        out.write(json.dumps(reply, ensure_ascii=False) + "\n")
        out.flush()
    if conn is not None:
        conn.close()
    return 0