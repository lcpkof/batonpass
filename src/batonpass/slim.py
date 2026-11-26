"""Giving disk back without giving memory back.

Measured on a real library before this module existed: 716 MB, of which

    tool results   120 MB   what a command printed
    reasoning       75 MB   what the model thought on the way
    tool calls      50 MB   what it was asked to run
    prose           54 MB   what a human and an assistant actually said
    index          166 MB   a local trigram copy of the prose

The first three are reproducible: the tool that produced them still has them on
disk, in its own directory. The prose is not reproducible by anything. So slim
draws the line there and nowhere else:

  * a body older than the window leaves the library (`content.codec='purged'`),
    its row and its sha stay, so every pointer in every table remains valid;
  * runs of identical, now-empty tool/reasoning rows collapse into one row that
    carries the count (`part.n`), because 37 reasoning steps are worth the
    number 37 and not 37 rows;
  * `batonpass fetch` walks back to the source database and puts the text back.

Nothing here touches a message. `message.text_sha` is never in the candidate
set, and a sha shared by prose and a tool result is disqualified by that alone.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

PURGEABLE = ("tool_call", "tool_result", "reasoning")
MB = 1048576.0


def _cutoff(days: float, now: datetime = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _candidates(conn) -> list:
    """Every content blob and who points at it, in one pass."""
    sql = """
        SELECT c.sha256              AS sha,
               c.byte_len            AS byte_len,
               c.codec               AS codec,
               COUNT(*)              AS refs,
               MIN(p.ts)             AS first_ts,
               MAX(p.ts)             AS last_ts,
               SUM(CASE WHEN p.kind IN ('tool_call','tool_result') THEN 1 ELSE 0 END) AS tool_refs,
               SUM(CASE WHEN p.kind = 'reasoning' THEN 1 ELSE 0 END)                     AS reason_refs,
               SUM(CASE WHEN p.kind NOT IN ('tool_call','tool_result','reasoning') THEN 1 ELSE 0 END) AS other_refs
          FROM part p
          JOIN content c ON c.sha256 IN (p.args_sha, p.result_sha)
         GROUP BY c.sha256
    """
    return conn.execute(sql).fetchall()


def _prose_shas(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT text_sha FROM message WHERE text_sha IS NOT NULL AND text_sha<>''")}


def plan(conn, tool_days: float = 30, reasoning_days: float = 30,
         fold: bool = True, meta: bool = True, plain: bool = True,
         now: datetime = None) -> dict:
    """What a slim run would do. Reads only — safe to call on a live library."""
    cut_tool = _cutoff(tool_days, now)
    cut_reason = _cutoff(reasoning_days, now)
    prose = _prose_shas(conn)

    purge, kept_shared = [], 0
    for r in _candidates(conn):
        sha = r["sha"]
        if r["codec"] == "purged" or sha in prose:
            if sha in prose and (r["tool_refs"] or r["reason_refs"]):
                kept_shared += 1
            continue
        if r["other_refs"]:
            continue
        keep = False
        if r["tool_refs"] and (r["last_ts"] or "") >= cut_tool:
            keep = True
        if r["reason_refs"] and (r["last_ts"] or "") >= cut_reason:
            keep = True
        if keep:
            continue
        purge.append({"sha": sha, "bytes_in": int(r["byte_len"] or 0),
                      "refs": int(r["refs"]), "last_ts": r["last_ts"]})

    out = {
        "window": {"tool_days": tool_days, "reasoning_days": reasoning_days,
                   "tool_before": cut_tool, "reasoning_before": cut_reason},
        "purge": {
            "blobs": len(purge),
            "plain_bytes": sum(p["bytes_in"] for p in purge),
            "rows": sum(p["refs"] for p in purge),
            "shas": [p["sha"] for p in purge],
            "kept_because_shared_with_prose": kept_shared,
        },
        "fold": {"rows_now": 0, "rows_after": 0, "groups": 0},
        "meta": {"rows": 0, "bytes": 0},
        "plain": {"rows": 0, "bytes": 0},
    }

    done = set(out["purge"]["shas"])
    if fold:
        out["fold"] = _plan_fold(conn, done)
    if meta:
        out["meta"] = _plan_meta(conn)
    if plain:
        from .store.codecs import Codecs
        from .store.content import plain_prose
        out["plain"] = plain_prose(conn, Codecs(conn), dry_run=True)
    return out


def _purged_set(conn, extra=()) -> set:
    """Every sha whose body is already gone. One query, then O(1) per part."""
    return set(extra) | {r[0] for r in conn.execute(
        "SELECT sha256 FROM content WHERE codec='purged'")}


def _runs(conn, gone: set) -> list:
    """Group the purgeable parts of each message into runs worth folding.

    The run key is (kind, name, is_error): two different tools are two facts,
    two calls of the same tool are one fact repeated. A part whose body is
    still here ends a run — folding it would throw the text away.
    """
    rows = conn.execute(
        "SELECT pid, mid, seq, kind, name, is_error, args_sha, result_sha, n"
        "  FROM part WHERE kind IN ('tool_call','tool_result','reasoning')"
        " ORDER BY mid, seq").fetchall()
    runs, cur, key = [], [], None
    for r in rows:
        empty = ((not r["args_sha"] or r["args_sha"] in gone)
                 and (not r["result_sha"] or r["result_sha"] in gone))
        k = (r["mid"], r["kind"], r["name"], r["is_error"])
        if empty and cur and k == key:
            cur.append(r)
        else:
            cur = [r]
            key = k
            runs.append(cur)
    return runs


def _plan_fold(conn, purging: set) -> dict:
    runs = _runs(conn, _purged_set(conn, purging))
    now = sum(len(r) for r in runs)
    return {"rows_now": now,
            "rows_after": len(runs),
            "groups": sum(1 for r in runs if len(r) > 1)}


def _plan_meta(conn) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(LENGTH(meta_json)),0) b FROM message"
        " WHERE meta_json LIKE '%\"identity\"%'").fetchone()
    return {"rows": int(row["c"]), "bytes": int(row["b"])}


def apply(conn, p: dict, vacuum: bool = False, progress=None) -> dict:
    """Do what plan() described. One transaction per step, so a kill leaves
    the library consistent rather than half-purged."""
    stats = {"purged_blobs": 0, "freed_bytes": 0, "folded_rows": 0,
             "meta_rows": 0, "plain_rows": 0, "vacuumed": False}

    # Prose first: it is also what the search has to read, and doing it before
    # the purge keeps the two ledgers (freed here, spent there) honest.
    if p.get("plain", {}).get("rows"):
        from .store.codecs import Codecs
        from .store.content import plain_prose
        stats["plain_rows"] = plain_prose(conn, Codecs(conn))["rows"]

    shas = p["purge"]["shas"]
    for i in range(0, len(shas), 400):
        chunk = shas[i:i + 400]
        marks = ",".join("?" * len(chunk))
        with conn:
            conn.execute(
                "UPDATE content SET body=x'', codec='purged' WHERE sha256 IN (%s)" % marks,
                chunk)
        stats["purged_blobs"] += len(chunk)
        if progress:
            progress("purged %d blobs" % stats["purged_blobs"])
    stats["freed_bytes"] = int(p["purge"]["plain_bytes"])

    if p["fold"]["groups"]:
        stats["folded_rows"] = _apply_fold(conn)

    if p["meta"]["rows"]:
        stats["meta_rows"] = _apply_meta(conn)

    if vacuum:
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        stats["vacuumed"] = True
    return stats


def _apply_fold(conn) -> int:
    removed = 0
    for group in _runs(conn, _purged_set(conn)):
        if len(group) < 2:
            continue
        total = sum(int(r["n"] or 1) for r in group)
        with conn:
            conn.execute("UPDATE part SET n=?, args_sha=NULL, result_sha=NULL WHERE pid=?",
                         (total, group[0]["pid"]))
            conn.executemany("DELETE FROM part WHERE pid=?",
                             [(r["pid"],) for r in group[1:]])
        removed += len(group) - 1
    return removed


def _apply_meta(conn) -> int:
    """Drop the 64-hex `identity` we copied from a source and never read.

    It is a hash of a row in somebody else's database: we already have the row.
    Keeping `kind`/`who` because those decide how a message is drawn.
    """
    n = 0
    rows = conn.execute(
        "SELECT mid, meta_json FROM message WHERE meta_json LIKE '%\"identity\"%'").fetchall()
    for r in rows:
        try:
            d = json.loads(r["meta_json"])
        except (TypeError, ValueError):
            continue
        if "identity" not in d:
            continue
        d.pop("identity", None)
        with conn:
            conn.execute("UPDATE message SET meta_json=? WHERE mid=?",
                         (json.dumps(d, ensure_ascii=False, separators=(",", ":")) if d else None,
                          r["mid"]))
        n += 1
        if n % 5000 == 0:
            conn.commit()
    return n


# ── the ledger ──────────────────────────────────────────────────

def savings(conn, p: dict) -> dict:
    """How much of the file this plan is actually worth, in bytes on disk.

    Compressed body size is what leaves the file, so it is measured, not
    extrapolated from plain bytes.
    """
    shas = p["purge"]["shas"]
    stored = 0
    for i in range(0, len(shas), 400):
        chunk = shas[i:i + 400]
        marks = ",".join("?" * len(chunk))
        row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(body)),0) b FROM content WHERE sha256 IN (%s)" % marks,
            chunk).fetchone()
        stored += int(row["b"] or 0)
    plain = p.get("plain") or {"bytes": 0}
    fold = p["fold"]
    row_bytes = conn.execute(
        "SELECT COALESCE(AVG(LENGTH(pid)+LENGTH(mid)+LENGTH(kind)+LENGTH(COALESCE(name,''))+24),0) b"
        "  FROM part").fetchone()["b"]
    rows = int((fold["rows_now"] - fold["rows_after"]) * (float(row_bytes) + 34))
    gross = stored + rows + p["meta"]["bytes"]
    return {
        "bodies": stored,
        "rows": rows,
        "meta": p["meta"]["bytes"],
        "plain": int(plain["bytes"]),
        "gross": gross,
        "net": gross - int(plain["bytes"]),
    }


def report(conn, p: dict, save: dict, stats: dict = None, dry: bool = True) -> str:
    head = "打算这么做（还没有动）" if dry else "已经这么做了"
    w = p["window"]
    lines = [head + "：",
             "  正文窗口   工具输入输出 / 推理过程，早于 %s 的不再留正文"
             % w["tool_before"][:10]]
    lines.append("  要移出     %s 个正文块，%s（%s 处引用）"
                 % (f"{p['purge']['blobs']:,}", _fmt(save["bodies"]),
                    f"{p['purge']['rows']:,}"))
    if p["purge"]["kept_because_shared_with_prose"]:
        lines.append("  不动       %s 块和正文共用一个 sha —— 删了就等于删正文"
                     % f"{p['purge']['kept_because_shared_with_prose']:,}")
    if p["fold"]["groups"]:
        lines.append("  折叠       %s 行 -> %s 行（%s 组同样的调用并成一行，带次数）"
                     % (f"{p['fold']['rows_now']:,}", f"{p['fold']['rows_after']:,}",
                        f"{p['fold']['groups']:,}"))
    if p["meta"]["rows"]:
        lines.append("  元数据     %s 行里那个从别人库里抄来的 identity 哈希，%s"
                     % (f"{p['meta']['rows']:,}", _fmt(save["meta"])))
    if (p.get("plain") or {}).get("rows"):
        lines.append("  正文改明文 %s 条，多占 %s —— 换来搜全文 0.2 秒，压着存要解码 1.7 秒"
                     % (f"{p['plain']['rows']:,}", _fmt(save["plain"])))
    if save["plain"]:
        sign = "净省" if save["net"] >= 0 else "净多占"
        lines.append("  合计       省 %s，减去上面这笔多占，%s %s；"
                     "外层还有一段碎片，VACUUM 之后才是文件真正变小"
                     % (_fmt(save["gross"]), sign, _fmt(abs(save["net"]))))
    else:
        lines.append("  合计       %s；外层还有一段碎片，VACUUM 之后才是文件真正变小"
                     % _fmt(save["gross"]))
    if save["net"] < 0:
        lines.append("  提醒       这一档窗口里移出的正文还没明文多占的多 ——"
                     "要么把窗口放长（--days），要么加 --keep-packed")
    lines.append("  取回       batonpass fetch <会话> / --all，回原库重读")
    if stats and not dry:
        lines.append("  实际       %s 块移出，%s 行折叠，%s 行元数据，%s"
                     % (f"{stats['purged_blobs']:,}", f"{stats['folded_rows']:,}",
                        f"{stats['meta_rows']:,}",
                        "已 VACUUM" if stats["vacuumed"] else "未 VACUUM"))
    return "\n".join(lines)


def _fmt(n) -> str:
    n = float(n or 0)
    if n < 0:
        return "-" + _fmt(-n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f GB" % n
