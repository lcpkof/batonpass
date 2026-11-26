"""One command that does the whole first run.

`batonpass setup` is the only command a new user has to know:

    scan what is here  →  remember it  →  open the door for agents

Everything it does is also available as its own verb. This just runs the verbs
in the right order, says what happened in plain lines, and stops asking.

Nothing here writes into any AI tool's data directory. The only files touched
outside ~/.batonpass are the *agent configs* you say yes to, and every one of
those gets a `.bak-batonpass` copy first.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time

from . import __version__

# name, proof the client is installed, config file (relative to that proof)
CLIENTS = (
    ("Alma", "~/.config/alma", "mcp.json"),
    ("Claude Code", "~/.claude.json", None),
    ("Claude Desktop", "~/Library/Application Support/Claude", "claude_desktop_config.json"),
    ("Cursor", "~/.cursor", "mcp.json"),
)

SERVER_NAME = "batonpass"


# ---------------------------------------------------------------- agents

def mcp_server_entry():
    """The command that starts the MCP server on this machine."""
    exe = shutil.which("batonpass")
    if exe:
        return {"command": exe, "args": ["mcp"]}
    return {"command": sys.executable, "args": ["-m", "batonpass", "mcp"]}


def _expand(p):
    return os.path.abspath(os.path.expanduser(p))


def find_clients():
    """[(name, config_path, already_registered)] for clients that are here."""
    out = []
    for name, proof, cfg in CLIENTS:
        proof = _expand(proof)
        if not os.path.exists(proof):
            continue
        path = proof if cfg is None else os.path.join(proof, cfg)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            have = SERVER_NAME in (data.get("mcpServers") or {})
        except (OSError, ValueError):
            have = False
        out.append((name, path, have))
    return out


def register(path, entry=None):
    """Add the MCP server to one client config. Returns a sentence."""
    entry = entry or mcp_server_entry()
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except ValueError as e:
            return "skipped, %s is not valid JSON (%s)" % (os.path.basename(path), e)
        if not isinstance(data, dict):
            return "skipped, %s is not an object" % os.path.basename(path)
        shutil.copy2(path, path + ".bak-batonpass")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if servers.get(SERVER_NAME) == entry:
        return "already connected"
    servers[SERVER_NAME] = entry
    data["mcpServers"] = servers
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp-batonpass"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return "connected"


# ---------------------------------------------------------------- steps

def _say(msg="", stream=None):
    print(msg, file=stream or sys.stdout)


def _sources_line(cap=3):
    from .discover import summary

    s = summary()
    t = s["totals"]
    named = [x for x in s.get("sources", []) if x.get("sessions")]
    named.sort(key=lambda x: -x["sessions"])
    head = ", ".join("%s %d" % (x["adapter"], x["sessions"]) for x in named[:cap])
    if len(named) > cap:
        head += ", +%d more" % (len(named) - cap)
    return t, head


def run(db=None, channel=None, join=None, archive=True, index=True,
        agents=True, yes=False, ask=None) -> int:
    ask = ask or _ask
    t0 = time.time()
    _say("batonpass setup %s\n" % __version__)

    if join:
        rc = _join(join, db=db)
        if rc:
            return rc
        archive = False                       # pulled history is already here

    # 1. what is on this machine
    try:
        t, head = _sources_line()
    except Exception as e:
        _say("scan        failed: %s" % e)
        t, head = {"recognised": 0, "sessions": 0, "messages": 0}, ""
    if t["recognised"]:
        _say("found       %d source(s), %s conversations, %s messages"
             % (t["recognised"], format(t["sessions"], ","), format(t["messages"], ",")))
        _say("            %s" % head)
    else:
        _say("found       no AI history this build understands")
        _say("            nothing to copy yet — see the list at")
        _say("            github.com/lcpkof/batonpass#what-it-reads")

    # 2. remember it
    if archive and t["recognised"]:
        from .archive.runner import run_archive

        _say("archiving   …")
        try:
            out = run_archive(level=1, db=db)
        except (RuntimeError, OSError) as e:
            _say("archiving   failed: %s" % e)
            return 2
        tot = out["totals"]
        _say("archived    %s sessions, %s messages, %s new rows"
             % (format(tot["sessions"], ","), format(tot["messages"], ","),
                format(tot["new_rows"], ",")))
        for s in out["sources"]:
            for e in s["errors"]:
                _say("            ! %s: %s" % (s["adapter"], e))

    # 3. make it searchable
    if index:
        from .recall import build_index, index_stats
        from .store.db import connect

        try:
            conn = connect(db)
        except (OSError, RuntimeError) as e:
            _say("index       skipped: %s" % e)
        else:
            _say("indexing    …")
            build_index(conn)
            st = index_stats(conn)
            _say("indexed     %s documents, %d/%d sessions searchable"
                 % (format(st["docs"], ","), st["sessions_indexed"], st["sessions_total"]))
            conn.close()

    # 4. open the door for agents
    if agents:
        _agents(yes=yes, ask=ask)

    # 5. where you can go from here
    _say("")
    _say("ready in %ds. In any agent that speaks MCP, just ask it:" % int(time.time() - t0))
    _say('            "search my history for …"')
    _say("or here:")
    _say("            batonpass find \"half a sentence you remember\"")
    _say("            batonpass tasks            # what is in flight, per project")
    _say("            batonpass resume --cwd .   # where this directory left off")
    if not join and channel:
        _say("")
        _say("other machines:  batonpass setup --join %s" % channel)
    return 0


def _agents(yes, ask):
    found = find_clients()
    if not found:
        _say("agents      no MCP client found on this machine")
        return
    todo = [(n, p) for n, p, have in found if not have]
    if not todo:
        _say("agents      %s — all connected" % ", ".join(n for n, p, h in found))
        return
    entry = mcp_server_entry()
    names = ", ".join(n for n, p in todo)
    if not yes:
        if not ask("agents      connect %s? [Y/n] " % names):
            _say("agents      left alone — `batonpass setup` again when you want it")
            return
    for name, path in todo:
        _say("agents      %-14s %s" % (name, register(path, entry)))


def _join(channel, db=None):
    """A second machine: read the channel, then be usable."""
    from .store.db import connect
    from .sync import channel as ch
    from .sync import pull

    channel = _expand(channel)
    _say("channel     %s" % channel)
    if not ch.is_channel(channel):
        _say("            not a batonpass channel — check the path")
        return 2
    conn = connect(db)
    try:
        out = pull(channel, tier=1, conn=conn)
    except (SystemExit, RuntimeError) as e:
        _say("            %s" % e)
        return 2
    finally:
        conn.close()
    _say("pulled      %s sessions seen, %s new, %s summaries"
         % (format(out["sessions_seen"], ","), format(out["sessions_new"], ","),
            format(out["heads"], ",")))
    if not out["sessions_seen"]:
        _say("            the channel is empty — push from the first machine first")
    return 0


def _ask(question):
    """Yes by default when a human is there, no when a script is driving."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(question).strip().lower() not in ("n", "no")
    except EOFError:
        return False
