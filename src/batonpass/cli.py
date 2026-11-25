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
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args) or 0
