"""Command line.

Two verbs do the work — `discover` looks, `archive` keeps. Everything else is
an inspection tool. Nothing here writes outside ~/.batonpass.
"""

from __future__ import annotations

import argparse
import json
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
    print("raw         %d records from %d sources" % (st["raw_records"], st["raw_sources"]))
    print("plaintext   %s in %d unique blobs" % (_fmt_bytes(st["plain_bytes"]), st["content_unique"]))
    print("stored      %s  (%.2fx)" % (_fmt_bytes(st["stored_bytes"]), st["ratio"]))
    print("library     %s" % _fmt_bytes(st["db_bytes"]))
    for row in st["by_source"]:
        print("  %-8s %4d sessions %7d msgs" % (row["source"], row["n"], row["msgs"] or 0))
    return 0


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
        print("\nnothing left to do — `batonpass sync push %s` fills it" % channel)
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
        for e in st["errors"]:
            print("  ERROR %s" % e)
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

    y = sub.add_parser("sync", help="exchange history with your other machines")
    ysub = y.add_subparsers(dest="action")
    for name, hlp in (("init", "create a channel folder"),
                      ("status", "who is in the channel, what is unread")):
        q = ysub.add_parser(name, help=hlp)
        q.add_argument("channel")
        q.add_argument("--device", default=None)
        q.add_argument("--db", default=None)
        q.add_argument("--json", action="store_true")
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
    y.set_defaults(func=None)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args) or 0
