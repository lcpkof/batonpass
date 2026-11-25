"""Command line.

Two verbs do the work — `discover` looks, `archive` keeps. Everything else is
an inspection tool. Nothing here writes outside ~/.batonpass.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

from . import __version__


def _fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def cmd_discover(args):
    from .discover import main as discover_main
    argv = []
    if args.only:
        argv += ["--only", args.only]
    if args.json:
        argv.append("--json")
    if getattr(args, "report", None):
        argv += ["--report", args.report]
    return discover_main(argv)


def cmd_archive(args):
    from .archive.runner import run_archive
    only = [s.strip() for s in args.only.split(",") if s.strip()] if args.only else None
    out = run_archive(only=only, level=args.level, limit=args.limit,
                      db=args.db, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0
    head = "dry-run" if args.dry_run else "archived"
    print("batonpass %s — device %s\n" % (head, out["device"]))
    for s in out["sources"]:
        flag = "  " if not s["errors"] else "! "
        print("%s%-8s %-52s %5d sess %7d msg %7d part %s" % (
            flag, s["adapter"], _tail(s["path"], 52), s["sessions"], s["messages"],
            s["parts"], _fmt_bytes(s["bytes_in"])))
        if s["declared_drops"]:
            print("           · %d structural parts skipped (step-start/step-finish) — declared"
                  % s["declared_drops"])
        if s["empty_text"]:
            print("           · %d rows carry no prose (structured parts only) — kept, not dropped"
                  % s["empty_text"])
        for n in s["notes"]:
            print("           · %s" % n)
        for e in s["errors"]:
            print("           ERROR %s" % e)
    t = out["totals"]
    print("\n%d sessions, %d messages, %d parts, %d new rows" % (
        t["sessions"], t["messages"], t["parts"], t["new_rows"]))
    return 0 if not any(s["errors"] for s in out["sources"]) else 1


def _tail(s, n):
    s = str(s)
    return s if len(s) <= n else "…" + s[-(n - 1):]


def cmd_export(args):
    from .archive.export import export_session
    doc = export_session(args.sid, db=args.db, as_json=not args.text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print("wrote %s (%s)" % (args.out, _fmt_bytes(len(doc.encode()))))
    else:
        print(doc)
    return 0


def cmd_stats(args):
    from .archive.export import stats
    st = stats(db=args.db)
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    print("sessions    %d" % st["sessions"])
    print("messages    %d" % st["messages"])
    print("parts       %d" % st["parts"])
    if st["raw_records"]:
        print("raw         %d records from %d sources" % (
            st["raw_records"], st["raw_sources"]))
    else:
        # Level 1 keeps the parsed rows and drops the raw blobs; "0 records" reads
        # like a bug when it is the level you chose.
        print("raw         not copied (level 1 keeps parsed rows; "
              "`archive --level 2` also keeps the originals)")
    print("plaintext   %s in %d unique blobs" % (_fmt_bytes(st["plain_bytes"]), st["content_unique"]))
    print("stored      %s  (%.2fx)" % (_fmt_bytes(st["stored_bytes"]), st["ratio"]))
    print("library     %s" % _fmt_bytes(st["db_bytes"]))
    for row in st["by_source"]:
        print("  %-8s %4d sessions %7d msgs" % (row["source"], row["n"], row["msgs"] or 0))
    return 0


def cmd_mcp(args):
    from . import mcp

    # stdout is the protocol: nothing else may be printed to it.
    return mcp.serve(db_path=args.db)


def cmd_doctor(args):
    from . import doctor

    res = doctor.run(db_path=args.db, channel=args.channel, sources=args.sources)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print(doctor.render(res))
    return 1 if any(c["level"] == doctor.FAIL for c in res["checks"]) else 0


def cmd_sync(args):
    from .store.db import connect, db_path
    from .sync import init_channel, pull, push, status
    from .sync.channel import device_name

    action = args.action
    channel = args.channel

    if action == "init":
        info = init_channel(channel, device=args.device or device_name())
        print("channel  %s" % info["path"])
        print("device   %s" % (args.device or device_name()))
        if getattr(args, "encrypt", False):
            from .sync import crypt

            try:
                res = crypt.enable(channel)
            except RuntimeError as e:
                print("\nencryption not enabled: %s" % e)
                return 2
            print("sealed   %s · %s key %s" % (
                "already" if res["already"] else "yes", crypt.ALGO,
                crypt.fingerprint(channel)))
            if res["sealed"]:
                print("         %d blob(s) already in the channel were re-sealed" %
                      res["sealed"])
            print("passphrase is not in the channel: keep BATONPASS_KEY set on "
                  "every machine, or the blobs stay closed")
        print("\nnothing left to do — `batonpass sync push %s` fills it" % channel)
        return 0

    if action == "compact":
        from .sync import channel as ch

        devs = [args.device or device_name()]
        sdir = os.path.join(os.path.expanduser(channel), "stream")
        if args.all and os.path.isdir(sdir):
            devs = sorted(x for x in os.listdir(sdir)
                          if os.path.isdir(os.path.join(sdir, x)))
        tiers = ["t0", "t1", "t2"] if args.tier is None else ["t%d" % args.tier]
        rows = [ch.compact(channel, d, t, force=args.force)
                for d in devs for t in tiers]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            return 0
        for r in rows:
            if not r["compacted"]:
                print("  %-16s %s %8s  unchanged (%s)" % (
                    r["device"], r["tier"], _fmt_bytes(r["before"]), r.get("reason")))
                continue
            print("  %-16s %s %8s → %8s  gen %d  %s lines" % (
                r["device"], r["tier"], _fmt_bytes(r["before"]), _fmt_bytes(r["after"]),
                r["gen"], format(r.get("lines_after", 0), ",")))
        return 0

    if action == "status":
        conn = connect(args.db)
        st = status(channel, conn)
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2, default=str))
            return 0
        print("channel %s" % st["channel"])
        if not st["exists"]:
            print("  (no channel here yet — run `batonpass sync init %s`)" % channel)
            return 1
        # Which channel is this, and can anyone read it? Both answers change what
        # you do next, and neither is visible from the folder listing.
        print("  identity %s" % (st.get("channel_id") or "?")[:12])
        from .sync import crypt

        m = crypt.marker(channel)
        if m.get("enc"):
            try:
                print("  sealed   %s, key %s" % (m["enc"], crypt.fingerprint(channel)))
            except RuntimeError:
                print("  sealed   %s, but this machine has no key — the blobs stay closed"
                      % m["enc"])
        else:
            print("  sealed   no (anyone holding this folder can read it)")
        print("\ndevices")
        for d in st["devices"]:
            print("  %-16s last seen %s  pushes %s" % (
                d.get("device"), d.get("last_seen"), d.get("pushes")))
        print("\nstreams")
        for s in st["streams"]:
            pct = 100.0 if not s["bytes"] else (100.0 * (s["read"] or 0) / s["bytes"])
            print("  %-16s %s %8s  read %5.1f%%  pending %s" % (
                s["device"], s["tier"], _fmt_bytes(s["bytes"]), pct,
                _fmt_bytes(s["pending"])))
        if st["offsets"]:
            print("\nlocal library has read %d stream(s) up to a stored offset"
                  % len(st["offsets"]))
        return 0

    conn = connect(args.db or str(db_path()))
    if action == "push":
        st = push(channel, tier=args.tier, device=args.device, conn=conn,
                  sid_prefix=args.sid)
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2, default=str))
            return 0 if not st["errors"] else 1
        print("pushed to %s as %s (tier %d)" % (st["channel"], st["device"], st["tier"]))
        print("  %d sessions · %d re-sent · %d up to date" % (
            st["sessions"], st["sessions_sent"], st["skipped"]))
        if st["lines"][1] or st["lines"][0]:
            print("  lines: t0 %d, t1 %d, t2 %d" % (
                st["lines"][0], st["lines"][1], st["lines"][2]))
        if st["tier"] >= 2:
            print("  %d messages, %d parts" % (st["messages"], st["parts"]))
        print("  blobs: %d new, %s" % (st["blobs_new"], _fmt_bytes(st["blob_bytes"])))
        shown = st["errors"][:5]          # five of the same sentence is enough
        for e in shown:
            print("  ERROR %s" % e)
        if len(st["errors"]) > len(shown):
            print("  … and %d more, same reason" % (len(st["errors"]) - len(shown)))
        if st["errors"]:
            print("\n  %d blob(s) stayed behind. The channel carries what this machine "
                  "could read,\n  so it is smaller than the library, not wrong — "
                  "`batonpass doctor` names the missing piece." % len(st["errors"]))
        return 0 if not st["errors"] else 1

    st = pull(channel, tier=args.tier, device=args.device, conn=conn, sid_prefix=args.sid)
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2, default=str))
        return 0
    print("pulled from %s (up to tier %d)" % (st["channel"], st["tier"]))
    print("  devices seen: %s" % (", ".join(st["devices"]) or "none"))
    print("  %d sessions seen · %d new · %d summaries" % (
        st["sessions_seen"], st["sessions_new"], st["heads"]))
    if st["tier"] >= 2:
        print("  %d new messages · %d new parts · %d blobs materialised" % (
            st["messages_new"], st["parts_new"], st["blobs_new"]))
    if st["bad_lines"]:
        print("  %d unreadable line(s) skipped" % st["bad_lines"])
    if st["missing"]:
        print("  %d blob(s) referenced but absent — re-run pull with --tier 2"
              % len(st["missing"]))
    return 0



# ── identity & recall ───────────────────────────────────────────

def _when(iso):
    return (iso or "")[:16].replace("T", " ")


def cmd_index(args):
    from .recall import build_index, index_stats
    from .store.db import connect
    conn = connect(args.db)
    st = build_index(conn, force=args.rebuild)
    after = index_stats(conn)
    if args.json:
        print(json.dumps({"built": st, "index": after}, ensure_ascii=False, indent=2))
        return 0
    print("indexed %d messages across %d sessions (%d empty)" % (
        st["docs"], st["sessions"], st["skipped"]))
    print("index %s · %d docs · %d/%d sessions searchable" % (
        _fmt_bytes(after["bytes"]), after["docs"], after["sessions_indexed"],
        after["sessions_total"]))
    return 0


def cmd_find(args):
    from .recall import ensure_index, search
    from .store.db import connect
    conn = connect(args.db)
    ensure_index(conn)
    hits = search(conn, args.query, at=args.at, project=args.project,
                  device=args.device, limit=args.limit, explain=args.explain)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0 if hits else 1
    if not hits:
        # The silence contract: never invent a hit to look helpful.
        print("nothing matched %r%s" % (
            args.query, " in " + args.at if args.at else ""))
        from .recall import undecodable_hint

        hint = undecodable_hint(conn)
        print(hint or "try: a shorter word · batonpass tasks · drop --at")
        return 1
    print("%d hit%s%s" % (len(hits), "" if len(hits) == 1 else "s",
                          "  (window %s)" % args.at if args.at else ""))
    for i, h in enumerate(hits, 1):
        print("\n%d) %s" % (i, h["project"] or "(no project)"))
        print("   %s" % (h["title"] or "(untitled)")[:100])
        print("   %s · %s · %d messages · %s" % (
            _when(h["updated_at"]), h["device"] or "?", h["messages"], h["sid"][:12]))
        if h["snippet"]:
            print("   “%s”" % h["snippet"][:180])
        if args.explain:
            print("   matched on: %s" % ", ".join(h.get("why") or []))
    print("\nbatonpass export <sid> --text   to read one in full")
    return 0


def cmd_tasks(args):
    from .identity import projects
    from .store.db import connect
    conn = connect(args.db)
    rows = projects(conn, min_sessions=args.min, include_candidates=args.all)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no projects yet — run `batonpass archive` first")
        return 1
    print("%-30s %5s %5s  %-17s %s" % ("PROJECT", "SESS", "FILES", "LAST", "MACHINES"))
    for p in rows[:args.limit]:
        machines = p.get("device_list") or ""
        print("%-30s %5d %5d  %-17s %s%s" % (
            p["label"][:30], p["sessions"], p["with_files"], _when(p["last_at"]),
            machines[:30] or "?", "  ⚠ stale" if p["stale"] else ""))
    print("\n%d projects · batonpass card <name> for the full card" % len(rows))
    return 0


def cmd_card(args):
    from .identity import card, projects, resolve
    from .store.db import connect
    conn = connect(args.db)
    hits = resolve(conn, args.hint)
    if not hits:
        print("no project matches %r" % args.hint)
        for p in projects(conn)[:10]:
            print("  candidate: %s (%d sessions)" % (p["label"], p["sessions"]))
        return 1
    if len(hits) > 1 and not args.pick:
        print("%d projects match %r — pick one with --pick:" % (len(hits), args.hint))
        for i, p in enumerate(hits, 1):
            print("  %d) %-28s %3d sessions  %s" % (i, p["label"], p["sessions"],
                                                   _when(p["last_at"])))
        return 2
    target = hits[0] if not args.pick else hits[args.pick - 1]
    print(card(conn, target["project_key"], args.sessions))
    return 0


def cmd_resume(args):
    from .identity import resume_hint, projects
    from .identity.project import derive, display
    from .store.db import connect
    import os
    conn = connect(args.db)
    cwd = args.cwd or os.getcwd()
    if args.hint:
        from .identity import card, resolve
        hits = resolve(conn, args.hint)
        print(card(conn, hits[0]["project_key"], args.sessions) if hits
              else "no project matches %r" % args.hint)
        return 0 if hits else 1
    text = resume_hint(conn, cwd)
    if not text:
        key, _tier, _c = derive(cwd, "")
        print("nothing recorded for %s yet." % (display(key) if key else cwd))
        rows = projects(conn)[:8]
        if rows:
            print("\nmost recent work elsewhere:")
            for p in rows:
                print("  %-28s %3d sessions  %s" % (p["label"], p["sessions"],
                                                    _when(p["last_at"])))
        return 1
    print(text)
    return 0


def cmd_identity(args):
    from .identity import build
    from .store.db import connect
    conn = connect(args.db)
    st = build(conn, extract_artifacts=not args.no_artifacts, limit=args.limit)
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    print("identity rebuilt: %d sessions%s" % (
        st["sessions"], "" if not st["merged_subprojects"]
        else " · %d sub-project(s) folded into their parent" % st["merged_subprojects"]))
    n_projects = conn.execute(
        "SELECT COUNT(DISTINCT project_key) c FROM session "
        "WHERE project_key IS NOT NULL AND project_key<>''").fetchone()["c"]
    print("  %d projects · %d ungrouped sessions · %d forks linked" % (
        n_projects, st["unknown"], st["forks"]))
    print("  artifacts %d in %d sessions" % (st["artifacts"], st["with_artifacts"]))
    tiers = st["tiers"]
    print("  confidence: high %d · medium %d · low %d" % (
        tiers.get(1, 0), tiers.get(2, 0), tiers.get(3, 0)))
    if st.get("kept_remote"):
        # Sessions that came from another machine: our disk cannot out-vote their
        # answer, so their project key stands as it arrived.
        print("  kept %d project key(s) that arrived from another machine" % st["kept_remote"])
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="batonpass",
        description="Never drop the baton. Read-only archive of the AI history on this machine.")
    p.add_argument("--version", action="version", version="batonpass " + __version__)
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("discover", help="inventory local AI history (read-only, writes nothing)")
    d.add_argument("--only", default=None)
    d.add_argument("--report", default=None)
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_discover)

    a = sub.add_parser("archive", help="copy discovered history into the library")
    a.add_argument("--only", default=None, help="comma separated adapters")
    a.add_argument("--db", default=None)
    a.add_argument("--level", type=int, default=1, choices=[1, 2, 3],
                   help="1 text only, 2 +raw, 3 +trained dictionary")
    a.add_argument("--limit", type=int, default=None, help="max sessions per source")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_archive)

    e = sub.add_parser("export", help="print one session as text or json")
    e.add_argument("sid")
    e.add_argument("--db", default=None)
    e.add_argument("--out", default=None)
    e.add_argument("--text", action="store_true")
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("stats", help="what is in the library")
    s.add_argument("--db", default=None)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stats)

    m = sub.add_parser("mcp", help="run the MCP server on stdio (three tools)")
    m.add_argument("--db", default=None)
    m.set_defaults(func=cmd_mcp)

    d = sub.add_parser("doctor", help="check that everything can actually work")
    d.add_argument("--db", default=None)
    d.add_argument("--channel", default=None,
                   help="also check a channel folder")
    d.add_argument("--sources", action="store_true",
                   help="also scan the machine for sources (a few seconds)")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_doctor)

    y = sub.add_parser("sync", help="exchange history with your other machines")
    ysub = y.add_subparsers(dest="action")
    for name, hlp in (("init", "create a channel folder"),
                      ("status", "who is in the channel, what is unread")):
        q = ysub.add_parser(name, help=hlp)
        q.add_argument("channel")
        q.add_argument("--device", default=None)
        q.add_argument("--db", default=None)
        q.add_argument("--json", action="store_true")
        if name == "init":
            q.add_argument("--encrypt", action="store_true",
                           help="seal the payload with a passphrase (BATONPASS_KEY)")
        q.set_defaults(func=cmd_sync)
    for name, hlp in (("push", "write this machine's history into the channel"),
                      ("pull", "read the channel into this machine's library")):
        q = ysub.add_parser(name, help=hlp)
        q.add_argument("channel")
        q.add_argument("--tier", type=int, default=1 if name == "push" else 0,
                       choices=[0, 1, 2],
                       help="0 catalog, 1 summaries, 2 full messages")
        q.add_argument("--sid", default=None, help="only this session prefix")
        q.add_argument("--device", default=None)
        q.add_argument("--db", default=None)
        q.add_argument("--json", action="store_true")
        q.set_defaults(func=cmd_sync)
    q = ysub.add_parser("compact",
                        help="rewrite a stream as the set of facts it holds")
    q.add_argument("channel")
    q.add_argument("--tier", type=int, default=None, choices=[0, 1, 2])
    q.add_argument("--device", default=None)
    q.add_argument("--all", action="store_true",
                   help="every device's file, not just this machine's")
    q.add_argument("--force", action="store_true", help="ignore the size thresholds")
    q.add_argument("--db", default=None)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_sync)
    y.set_defaults(func=None)

    i = sub.add_parser("index", help="build the local search index (never synced)")
    i.add_argument("--db", default=None)
    i.add_argument("--rebuild", action="store_true", help="throw it away and redo")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_index)

    f = sub.add_parser("find", help="find it again from a half-remembered word")
    f.add_argument("query")
    f.add_argument("--at", default=None,
                   help="7d · 12h · today · yesterday · 2026-09 · 2026-09-20 · "
                        "2026-09-01..2026-09-20")
    f.add_argument("--project", default=None)
    f.add_argument("--device", default=None)
    f.add_argument("--limit", type=int, default=5)
    f.add_argument("--explain", action="store_true", help="which signal matched")
    f.add_argument("--db", default=None)
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_find)

    t = sub.add_parser("tasks", help="what is in flight, one line per project")
    t.add_argument("--min", type=int, default=1, help="sessions required to show")
    t.add_argument("--limit", type=int, default=30)
    t.add_argument("--all", action="store_true", help="include low-confidence clusters")
    t.add_argument("--db", default=None)
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_tasks)

    c = sub.add_parser("card", help="the handover card for one project")
    c.add_argument("hint")
    c.add_argument("--sessions", type=int, default=4)
    c.add_argument("--pick", type=int, default=None, help="when several match, choose 1..n")
    c.add_argument("--db", default=None)
    c.set_defaults(func=cmd_card)

    r = sub.add_parser("resume", help="pick up where this directory left off")
    r.add_argument("hint", nargs="?", default=None)
    r.add_argument("--cwd", default=None)
    r.add_argument("--sessions", type=int, default=4)
    r.add_argument("--db", default=None)
    r.set_defaults(func=cmd_resume)

    n = sub.add_parser("identity", help="recompute project identity and artifacts")
    n.add_argument("--db", default=None)
    n.add_argument("--no-artifacts", action="store_true")
    n.add_argument("--limit", type=int, default=None)
    n.add_argument("--json", action="store_true")
    n.set_defaults(func=cmd_identity)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args) or 0
    except BrokenPipeError:
        # `batonpass export … | head` is a normal thing to do.
        try:
            sys.stdout.close()
        finally:
            return 0
    except (RuntimeError, OSError, sqlite3.OperationalError) as e:
        # A missing optional dependency, an unreadable disk and a half-written
        # library are all one-liners to a human, not a stack trace to read.
        print("batonpass: %s" % e, file=sys.stderr)
        return 2
