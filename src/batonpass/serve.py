"""A read-only window into the library, for humans.

Every other surface batonpass has is built for an agent: `find` returns hits,
`resume` returns a card, MCP returns JSON. None of them let a person *see* the
archive — which tool said what, on which machine, in which folder. This is that
surface: one page, every session in the library, whatever tool wrote it and
whatever machine it came from.

It only ever reads. The connection is opened per request and nothing is written
back, so pointing it at a library that a sync is writing to is safe.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store.codecs import Codecs
from .store.content import is_purged
from .store.db import connect

DEFAULT_PORT = 7629
MAX_MESSAGES = 4000          # a session longer than this renders in slices
PAGE = 60


def _q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def summary(conn) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]
    tools = [
        {"tool": r["source"], "sessions": r["n"], "messages": r["m"] or 0}
        for r in _q(conn, """
            SELECT source, COUNT(*) n, SUM(msg_count) m FROM session
             GROUP BY source ORDER BY n DESC""")
    ]
    devices = [
        {"device": r["device"], "sessions": r["n"], "last": r["last"]}
        for r in _q(conn, """
            SELECT device, COUNT(*) n, MAX(COALESCE(last_msg_at, updated_at)) last
              FROM session GROUP BY device ORDER BY n DESC""")
    ]
    return {
        "sessions": one("SELECT COUNT(*) FROM session"),
        "messages": one("SELECT COUNT(*) FROM message"),
        "parts": one("SELECT COUNT(*) FROM part"),
        "tools": tools,
        "devices": devices,
        "db_bytes": os.path.getsize(conn.execute("PRAGMA database_list").fetchone()[2])
        if conn.execute("PRAGMA database_list").fetchone()[2] else 0,
    }


def sessions(conn, tool="", device="", q="", offset=0, limit=PAGE) -> dict:
    where, args = [], []
    if tool:
        where.append("s.source=?")
        args.append(tool)
    if device:
        where.append("s.device=?")
        args.append(device)
    if q:
        where.append("(s.title LIKE ? OR s.cwd LIKE ? OR s.project_key LIKE ?)")
        args += ["%%%s%%" % q] * 3
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    rows = _q(conn, """
        SELECT s.sid, s.source, s.device, s.title, s.cwd, s.project_key,
               s.msg_count, s.created_at, s.updated_at,
               COALESCE(s.last_msg_at, s.updated_at) AS last
          FROM session s %s
         ORDER BY last DESC
         LIMIT ? OFFSET ?""" % sql_where, args + [limit, offset])
    total = conn.execute("SELECT COUNT(*) FROM session s %s" % sql_where, args).fetchone()[0]
    out = []
    for r in rows:
        out.append({
            "sid": r["sid"], "tool": r["source"], "device": r["device"],
            "title": r["title"] or r["project_key"] or r["sid"],
            "path": r["cwd"] or "", "project": r["project_key"] or "",
            "messages": r["msg_count"] or 0, "last": r["last"] or "",
        })
    return {"total": total, "offset": offset, "sessions": out}


def transcript(conn, sid, offset=0, limit=MAX_MESSAGES) -> dict:
    row = conn.execute("SELECT * FROM session WHERE sid=?", (sid,)).fetchone()
    if row is None:
        return {"error": "no such session"}
    codecs = Codecs(conn)
    source = row["source"]
    msgs = _q(conn, """SELECT mid, seq, role, text_sha, ts FROM message
                        WHERE sid=? ORDER BY seq LIMIT ? OFFSET ?""",
              (sid, limit, offset))
    # One row per (message, kind, name) beats one row per tool call: a session
    # with 867 `run_code` calls is one chip, not 867 lines of noise.
    # `n` is folded: one row can stand for 37 calls, and the count is the honest
    # one. `arch` says how many of those calls have no body left here — the page
    # can then say so instead of drawing an empty result and looking broken.
    chips = {}
    purged = "SELECT sha256 FROM content WHERE codec='purged'"
    for p in _q(conn, """SELECT p.mid, p.kind, p.name, SUM(COALESCE(p.n,1)) n,
                                SUM(CASE WHEN p.args_sha IN (%s) OR p.result_sha IN (%s)
                                         THEN 1 ELSE 0 END) arch
                           FROM part p JOIN message m ON m.mid=p.mid
                          WHERE m.sid=? GROUP BY p.mid, p.kind, p.name"""
                  % (purged, purged), (sid,)):
        chips.setdefault(p["mid"], []).append(
            {"kind": p["kind"], "name": p["name"] or "", "n": p["n"], "arch": p["arch"]})
    out = []
    for m in msgs:
        text = codecs.get(m["text_sha"], source)
        out.append({"mid": m["mid"], "seq": m["seq"], "role": m["role"],
                    "ts": m["ts"] or "", "text": text or "",
                    "chips": chips.get(m["mid"], [])})
    total = conn.execute("SELECT COUNT(*) FROM message WHERE sid=?", (sid,)).fetchone()[0]
    return {"sid": sid, "tool": source, "device": row["device"],
            "title": row["title"] or "", "path": row["cwd"] or "",
            "project": row["project_key"] or "",
            "created": row["created_at"] or "", "updated": row["updated_at"] or "",
            "total": total, "offset": offset, "messages": out}


def search(conn, q, limit=40) -> dict:
    """Same ranking as `batonpass find`, so the window and the terminal agree.

    The body half of it is a LIKE over plain-text prose now, not a trigram index
    — see recall/index.py for why the index was the wrong place to keep it.
    """
    if not q.strip():
        return {"q": q, "hits": []}
    from .recall.query import search as recall_search
    hits = []
    for h in recall_search(conn, q, limit=limit):
        hits.append({"sid": h["sid"], "ref": h["sid"], "kind": "", "ts": h["snippet_at"],
                     "snip": h["snippet"], "title": h["title"], "tool": h["source"],
                     "device": h["device"], "path": h["project"]})
    return {"q": q, "hits": hits}


def session_parts(conn, mid) -> dict:
    row = conn.execute("""SELECT m.sid, m.role, s.source FROM message m
                            JOIN session s ON s.sid=m.sid WHERE m.mid=?""", (mid,)).fetchone()
    if row is None:
        return {"error": "no such message"}
    codecs = Codecs(conn)
    out = []
    for p in _q(conn, "SELECT * FROM part WHERE mid=? ORDER BY seq", (mid,)):
        out.append({"kind": p["kind"], "name": p["name"] or "",
                    "n": int(p["n"] or 1),
                    "is_error": bool(p["is_error"]),
                    "archived": not (p["args_sha"] or p["result_sha"])
                                or is_purged(conn, p["args_sha"])
                                or is_purged(conn, p["result_sha"]),
                    "args": codecs.get(p["args_sha"], row["source"])[:4000],
                    "result": codecs.get(p["result_sha"], row["source"])[:4000]})
    return {"mid": mid, "parts": out}


class Handler(BaseHTTPRequestHandler):
    db = None
    quiet = False

    def log_message(self, fmt, *a):  # the terminal is not a log file
        if not self.quiet:
            pass

    def _send(self, body: bytes, ctype="application/json; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, ensure_ascii=False, default=str).encode(), code=code)

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            return self._send(ASSET.encode(), "text/html; charset=utf-8")
        conn = connect(self.db)
        try:
            if u.path == "/api/summary":
                return self._json(summary(conn))
            if u.path == "/api/sessions":
                return self._json(sessions(
                    conn, qs.get("tool", ""), qs.get("device", ""), qs.get("q", ""),
                    int(qs.get("offset", 0)), min(int(qs.get("limit", PAGE)), 300)))
            if u.path == "/api/session":
                return self._json(transcript(conn, qs.get("sid", ""),
                                             int(qs.get("offset", 0))))
            if u.path == "/api/search":
                return self._json(search(conn, qs.get("q", "")))
            if u.path == "/api/parts":
                return self._json(session_parts(conn, qs.get("mid", "")))
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            return self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
        finally:
            conn.close()


def serve(db=None, port=DEFAULT_PORT, open_browser=True, quiet=False):
    Handler.db = db
    Handler.quiet = quiet
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("batonpass serve  %s  (ctrl-c to stop)" % url, flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("")
    finally:
        httpd.server_close()
    return 0


ASSET = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>batonpass — 所有工具、所有机器</title>
<style>
:root{--bg:#fbfaf8;--fg:#1d1c1a;--mut:#6f6a63;--line:#e6e2db;--card:#fff;
      --acc:#c2410c;--chip:#f2efe9;--user:#1f6feb;--asst:#0f766e}
@media (prefers-color-scheme:dark){:root{--bg:#161514;--fg:#ece8e1;--mut:#9a938a;
      --line:#2c2a27;--card:#1d1c1a;--acc:#f97316;--chip:#262420;--user:#60a5fa;--asst:#2dd4bf}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.6 -apple-system,BlinkMacSystemFont,"SF Pro SC","PingFang SC",sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);
       padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;margin:0;font-weight:650;letter-spacing:.2px}
h1 span{color:var(--mut);font-weight:400;margin-left:8px;font-size:12px}
input[type=search]{flex:1;min-width:220px;background:var(--card);border:1px solid var(--line);
       border-radius:8px;padding:7px 11px;color:var(--fg);font-size:13px;outline:none}
input[type=search]:focus{border-color:var(--acc)}
main{display:grid;grid-template-columns:340px 1fr;height:calc(100vh - 53px)}
@media (max-width:820px){main{grid-template-columns:1fr;height:auto}}
#list{border-right:1px solid var(--line);overflow:auto}
#detail{overflow:auto;padding:18px 22px}
.tools{display:flex;gap:6px;flex-wrap:wrap;padding:9px 12px;border-bottom:1px solid var(--line)}
.chip{background:var(--chip);border:1px solid transparent;border-radius:999px;padding:3px 10px;
      font-size:12px;color:var(--mut);cursor:pointer;white-space:nowrap}
.chip.on{border-color:var(--acc);color:var(--acc)}
.tc.arch{color:var(--muted);border:1px dashed var(--line)}
.row{padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:var(--card)}
.row.on{background:var(--card);box-shadow:inset 2px 0 0 var(--acc)}
.row .t{font-weight:560;font-size:13px;display:flex;gap:6px;align-items:baseline}
.badge{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--acc);
       border:1px solid var(--line);border-radius:4px;padding:0 4px;flex:none}
.row .p{color:var(--mut);font-size:11.5px;white-space:nowrap;overflow:hidden;
        text-overflow:ellipsis;direction:rtl;text-align:left}
.row .m{color:var(--mut);font-size:11px;margin-top:2px}
.msg{margin:0 0 16px}
.msg .h{font-size:11px;color:var(--mut);margin-bottom:3px;display:flex;gap:8px}
.role{font-weight:600;letter-spacing:.3px}
.role.user{color:var(--user)} .role.assistant{color:var(--asst)}
.body{white-space:pre-wrap;word-break:break-word}
.chips{display:flex;gap:5px;flex-wrap:wrap;margin-top:5px}
.tc{font-size:11px;background:var(--chip);border:none;border-radius:5px;padding:2px 7px;
    color:var(--mut);cursor:pointer;font-family:ui-monospace,Menlo,monospace}
.tc:hover{color:var(--fg)}
#empty{color:var(--mut);padding:40px 20px;text-align:center}
.hdr{border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:16px}
.hdr h2{font-size:16px;margin:0 0 3px}
.hdr .meta{color:var(--mut);font-size:11.5px}
.hit{padding:9px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.hit:hover{background:var(--card)}
.hit b{color:var(--acc);font-weight:600}
pre{margin:6px 0;padding:10px;background:var(--chip);border-radius:8px;overflow:auto;
    font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;max-height:340px}
</style></head><body>
<header>
  <h1>batonpass<span id="sub">…</span></h1>
  <input id="q" type="search" placeholder="搜一句话 / 一个词（也搜得到工具输出里的字样）">
</header>
<main>
  <div id="list">
    <div class="tools" id="tools"></div>
    <div id="rows"></div>
  </div>
  <div id="detail"><div id="empty">左边点一个会话<br><span style="font-size:12px">或者在上面搜</span></div></div>
</main>
<script>
const $ = s => document.querySelector(s);
let state = {tool:"", q:"", offset:0, total:0, cur:null};

const fmt = t => (t||"").replace("T"," ").replace("Z","").slice(0,16);
const esc = s => (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

async function api(p){ const r = await fetch(p); return r.json(); }

async function boot(){
  const s = await api("/api/summary");
  $("#sub").textContent = `${s.sessions} 个会话 · ${s.messages} 条消息 · ` +
    s.devices.map(d=>d.device).join(" + ") + ` · ${(s.db_bytes/1048576).toFixed(0)} MB`;
  $("#tools").innerHTML = `<span class="chip on" data-t="">全部 ${s.sessions}</span>` +
    s.tools.map(t=>`<span class="chip" data-t="${t.tool}">${t.tool} ${t.sessions}</span>`).join("");
  $("#tools").onclick = e => {
    const c = e.target.closest(".chip"); if(!c) return;
    [...$("#tools").children].forEach(x=>x.classList.toggle("on", x===c));
    state.tool = c.dataset.t; state.q = ""; $("#q").value=""; load();
  };
  load();
}

async function load(){
  const p = new URLSearchParams({tool:state.tool, q:state.q, offset:state.offset});
  const d = await api("/api/sessions?"+p);
  state.total = d.total;
  $("#rows").innerHTML = d.sessions.map(s=>`
    <div class="row" data-sid="${esc(s.sid)}">
      <div class="t"><span class="badge">${esc(s.tool)}</span><span>${esc(s.title)}</span></div>
      <div class="p">${esc(s.path)||"—"}</div>
      <div class="m">${s.messages} 条 · ${fmt(s.last)}${s.device?" · "+esc(s.device):""}</div>
    </div>`).join("") || `<div id="empty">没有匹配的会话</div>`;
  $("#rows").onclick = e => {
    const r = e.target.closest(".row"); if(!r) return;
    [...$("#rows").children].forEach(x=>x.classList.toggle("on", x===r));
    open(r.dataset.sid, 0);
  };
  if (state.offset + d.sessions.length < d.total){
    const more = document.createElement("div");
    more.className="row"; more.style.textAlign="center"; more.textContent="加载更多…";
    more.onclick = () => { state.offset += 60; load(); };
    $("#rows").appendChild(more);
  }
}

async function open(sid, offset){
  state.cur = sid;
  const s = await api(`/api/session?sid=${encodeURIComponent(sid)}&offset=${offset||0}`);
  if (s.error){ $("#detail").innerHTML = `<div id="empty">${esc(s.error)}</div>`; return; }
  const msgs = s.messages.map(m=>`
    <div class="msg">
      <div class="h"><span class="role ${m.role}">${m.role}</span><span>${fmt(m.ts)}</span></div>
      ${m.text?`<div class="body">${esc(m.text)}</div>`:""}
      ${m.chips.length?`<div class="chips">${m.chips.map(c=>
        `<button class="tc${c.arch?" arch":""}" data-mid="${esc(m.mid||"")}"${c.arch?' title="正文已归档，batonpass fetch 可取回"':""}>${esc(c.name||c.kind)} ×${c.n}${c.arch?" ↺":""}</button>`).join("")}</div>`:""}
    </div>`).join("");
  $("#detail").innerHTML = `
    <div class="hdr"><h2>${esc(s.title||s.sid)}</h2>
    <div class="meta">${esc(s.tool)} · ${esc(s.device)} · ${esc(s.path)||"无目录"} ·
      ${s.total} 条消息 · ${fmt(s.created)} → ${fmt(s.updated)}</div></div>
    ${msgs || '<div id="empty">这个会话没有可读正文</div>'}`;
  document.querySelectorAll(".tc").forEach(b=>b.onclick = async () => {
    const d = await api("/api/parts?mid="+encodeURIComponent(b.dataset.mid));
    b.insertAdjacentHTML("afterend", `<div style="flex-basis:100%">` +
      (d.parts||[]).map(p=>`<pre>${esc(p.name)} ${p.is_error?"(失败)":""}\n` +
      `${esc(p.result||p.args||"")}</pre>`).join("") + `</div>`);
    b.disabled = true;
  });
  $("#detail").scrollTop = 0;
}

let timer;
$("#q").addEventListener("input", e => {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const q = e.target.value.trim();
    if (q.length < 1){ state.q=""; state.offset=0; load(); return; }
    const d = await api("/api/search?q="+encodeURIComponent(q));
    $("#rows").innerHTML = d.hits.map(h=>`
      <div class="hit" data-sid="${esc(h.sid)}">
        <div class="t"><span class="badge">${esc(h.tool||"")}</span>
          <span>${esc(h.title||h.sid)}</span></div>
        <div style="font-size:12px;color:var(--mut);margin-top:3px">${h.snip||""}</div>
      </div>`).join("") || `<div id="empty">没找到“${esc(q)}”</div>`;
    $("#rows").onclick = ev => {
      const r = ev.target.closest(".hit"); if(!r) return;
      open(r.dataset.sid, 0);
    };
  }, 220);
});
boot();
</script></body></html>
"""
