"""What the library actually weighs, and which part of it is worth keeping.

The number people quote is "the library is N hundred MB" — useless, because it
says nothing about what would be lost by making it smaller. This splits the file
into the four things it is spend on, so the trade is visible:

  prose     what was said, compressed — the part you cannot rebuild anywhere
  tool      arguments and results of tool calls — reproducible, and usually the
            single biggest line item by a wide margin
  index     the local trigram index — derived, rebuildable, never synced
  catalog   session/message/part rows and their indexes — the pointers

Nothing here deletes anything. It reports.
"""

from __future__ import annotations

MB = 1048576.0


def _one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return (r[0] if r else 0) or 0


def breakdown(conn) -> dict:
    pages = {r[0]: r[1] or 0 for r in conn.execute(
        "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
    total = sum(pages.values())
    index = sum(v for k, v in pages.items() if k.startswith("search_fts")
                or k == "search_state")
    content = pages.get("content", 0) + sum(
        v for k, v in pages.items() if k.startswith("sqlite_autoindex_content"))

    tool_sha = _one(conn, "SELECT COALESCE(SUM(byte_len),0) FROM content "
                          "WHERE sha256 NOT IN (SELECT text_sha FROM message "
                          "WHERE text_sha IS NOT NULL)")

    # Stored (compressed) bytes, not raw: the question is disk, not text.
    prose_stored = _one(conn, "SELECT COALESCE(SUM(LENGTH(c.body)),0) FROM content c "
                              "WHERE c.sha256 IN (SELECT text_sha FROM message "
                              "WHERE text_sha IS NOT NULL)")
    tool_stored = _one(conn, "SELECT COALESCE(SUM(LENGTH(c.body)),0) FROM content c "
                             "WHERE c.sha256 NOT IN (SELECT text_sha FROM message "
                             "WHERE text_sha IS NOT NULL)")

    prose_by = {r["tool"]: r["n"] for r in conn.execute("""
        SELECT s.source tool, COALESCE(SUM(LENGTH(c.body)),0) n
          FROM session s
          JOIN message m ON m.sid=s.sid
          JOIN content c ON c.sha256=m.text_sha
         GROUP BY s.source""")}
    # Tool I/O hangs off `part`, not `message` — attributing it through
    # message.text_sha finds nothing, which is how it used to report 0 KB.
    tool_by = {r["tool"]: r["n"] for r in conn.execute("""
        SELECT s.source tool, COALESCE(SUM(LENGTH(c.body)),0) n
          FROM session s
          JOIN message m ON m.sid=s.sid
          JOIN part p ON p.mid=m.mid
          JOIN content c ON c.sha256 IN (p.args_sha, p.result_sha)
         GROUP BY s.source""")}
    per_tool = [{"tool": t, "sessions": n,
                 "prose": prose_by.get(t, 0), "tool_io": tool_by.get(t, 0)}
                for t, n in ((r["source"], r["n"]) for r in conn.execute(
                    "SELECT source, COUNT(*) n FROM session GROUP BY source"))]
    per_tool.sort(key=lambda t: t["prose"] + t["tool_io"], reverse=True)

    # Bodies that were moved out on purpose. They are not on disk, so they are
    # not part of any row above — but leaving them out of the report makes a
    # slimmed library look like it never had that history.
    archived = _one(conn, "SELECT COALESCE(SUM(byte_len),0) FROM content"
                          " WHERE codec='purged'")
    purged_blobs = _one(conn, "SELECT COUNT(*) FROM content WHERE codec='purged'")

    catalog = max(total - content - index, 0)
    part_pages = sum(v for k, v in pages.items() if k.startswith("part"))
    msg_pages = sum(v for k, v in pages.items() if k.startswith("message"))
    return {
        "catalog_detail": {"part_rows": part_pages, "message_rows": msg_pages},
        "file": total,
        "content_pages": content, "prose": {"stored": prose_stored},
        "tool_io": {"stored": tool_stored, "raw": tool_sha},
        "index": index,
        "catalog": catalog,
        "counts": {
            "sessions": _one(conn, "SELECT COUNT(*) FROM session"),
            "messages": _one(conn, "SELECT COUNT(*) FROM message"),
            "parts": _one(conn, "SELECT COUNT(*) FROM part"),
            "calls": _one(conn, "SELECT COALESCE(SUM(COALESCE(n,1)),0) FROM part"),
            "blobs": _one(conn, "SELECT COUNT(*) FROM content"),
        },
        "archived": {"plain": archived, "blobs": purged_blobs},
        "per_tool": per_tool,
    }


def report(conn) -> str:
    b = breakdown(conn)
    f = lambda n: "%.0f MB" % (n / MB) if n >= MB else "%.0f KB" % (n / 1024.0)
    pct = lambda n: 100.0 * n / b["file"] if b["file"] else 0
    lines = ["库 %s · %d 个会话 · %d 条消息 · %d 次工具调用（占 %d 行）" % (
        f(b["file"]), b["counts"]["sessions"], b["counts"]["messages"],
        b["counts"]["calls"], b["counts"]["parts"]), ""]
    rows = [
        ("正文", b["prose"]["stored"], "说的话，换台机器也换不回来"),
        ("工具输入输出", b["tool_io"]["stored"], "可复现，通常是最大的一笔"),
        ("搜索索引", b["index"], "本机派生，可随时重建，不同步"),
        ("目录与索引", b["catalog"], "会话/消息/调用的指针"),
    ]
    lines.append("%-14s %10s %7s  %s" % ("构成", "占用", "占比", "说明"))
    for name, n, note in rows:
        lines.append("%-14s %10s %6.0f%%  %s" % (name, f(n), pct(n), note))
    lines.append("")
    lines.append("目录里 %s 是工具调用行（每次调用一行），%s 是消息行" % (
        f(b["catalog_detail"]["part_rows"]), f(b["catalog_detail"]["message_rows"])))
    lines.append("口径：正文/工具按压缩后字节算（去重后），索引与目录按 sqlite 页算")
    lines.append("")
    lines.append("%-12s %8s %10s %10s" % ("工具", "会话", "正文", "工具输入输出"))
    for t in b["per_tool"]:
        lines.append("%-12s %8d %10s %10s" % (t["tool"], t["sessions"],
                                             f(t["prose"]), f(t["tool_io"])))
    if b["archived"]["blobs"]:
        lines.append("")
        lines.append("已移出正文 %d 块（%s）：行还在、指针还在，正文回原库读，"
                     % (b["archived"]["blobs"], f(b["archived"]["plain"])))
        lines.append("  `batonpass fetch <会话>` 或 `--all` 取回来")
    lines.append("")
    lines.append("看看还能省多少：`batonpass slim --dry-run`（什么都不动，只报数）")
    lines.append("  正文一个字都不删；动的是工具输入输出 / 推理过程，加 --vacuum 才真变小")
    return "\n".join(lines)
