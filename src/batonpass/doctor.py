"""`doctor` — the answer to "why isn't it working", before you have to ask.

Every check here earned its place by being something that actually went wrong
once: a missing compressor, an archive written by an older extractor, an index
built before the messages grew, a channel whose folder moved, a disk that could
not take another gigabyte. Each one prints one line it can act on, and never a
stack trace.

Nothing here writes to a library, a channel, or a source. Doctor is read-only,
like everything else that is allowed to touch other people's data.
"""

from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import sys

from . import __version__

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "--"
_LABEL = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL", INFO: "  · "}


def _check(level, name, detail, fix=None) -> dict:
    return {"level": level, "name": name, "detail": detail, "fix": fix}


def _human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def _dir_bytes(path, limit=200000) -> float:
    total, seen = 0, 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            seen += 1
            if seen > limit:
                return total
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ── checks ─────────────────────────────────────────────────────


def check_python():
    v = "%d.%d.%d" % sys.version_info[:3]
    if sys.version_info < (3, 9):
        return _check(FAIL, "python", "%s — batonpass needs 3.9 or newer" % v,
                      "install python 3.9+ and run it with that interpreter")
    return _check(OK, "python", v)


def check_sqlite():
    v = sqlite3.sqlite_version
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    except sqlite3.Error as e:
        return _check(FAIL, "sqlite", "%s without fts5 (%s)" % (v, e),
                      "use a python built with sqlite 3.34+ (python.org or Homebrew)")
    try:
        c.execute("CREATE VIRTUAL TABLE g USING fts5(body, tokenize='trigram')")
        trigram = True
    except sqlite3.Error:
        trigram = False
    c.close()
    if not trigram:
        # Chinese has no spaces; trigram is how a search for 「岗位表」 ever matches.
        return _check(WARN, "sqlite", "%s with fts5 but no trigram tokenizer" % v,
                      "search still works for latin words; use sqlite 3.34+ for CJK")
    return _check(OK, "sqlite", "%s, fts5 + trigram" % v)


def check_zstd():
    try:
        import zstandard  # noqa: F401
    except Exception:
        return _check(WARN, "zstd", "not installed — archives stay readable, "
                                    "2.50x becomes 2.04x",
                      'pip install "batonpass-cli[zstd]"')
    return _check(OK, "zstd", getattr(__import__("zstandard"), "__version__", "installed"))


def check_disk(path):
    try:
        os.makedirs(path, exist_ok=True)
        free = shutil.disk_usage(path).free
    except OSError as e:
        return _check(FAIL, "disk", "%s is not writable (%s)" % (path, e),
                      "point BATONPASS_HOME at a writable directory")
    if free < 2 * 1024 ** 3:
        return _check(WARN, "disk", "%s free at %s — a big archive may not fit"
                      % (_human(free), path), "free space, or archive elsewhere with --db")
    return _check(OK, "disk", "%s free at %s" % (_human(free), path))


def check_library(db_path):
    from .store.db import get_meta
    from .store.schema import SCHEMA_VERSION

    out = []
    if not os.path.exists(db_path):
        return [_check(INFO, "library", "no library yet at %s" % db_path,
                       "batonpass archive   # or point --db somewhere else")]
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT COUNT(*) FROM session").fetchone()
    except sqlite3.Error as e:
        return [_check(FAIL, "library", "%s: %s" % (db_path, e),
                       "if it was written by an older batonpass, run "
                       "`batonpass archive` again into a new --db")]

    sessions = conn.execute("SELECT COUNT(*) FROM session").fetchone()[0]
    messages = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    size = os.path.getsize(db_path)
    ver = get_meta(conn, "schema_version")
    if str(ver or "") != str(SCHEMA_VERSION):
        out.append(_check(WARN, "schema", "library is v%s, this build is v%s"
                          % (ver, SCHEMA_VERSION), "re-run `batonpass archive`"))
    out.append(_check(OK, "library", "%s, %s sessions, %s messages"
                      % (_human(size), format(sessions, ","), format(messages, ","))))
    integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok":
        out.append(_check(FAIL, "integrity", integrity,
                          "copy the file aside and re-run `batonpass archive`"))

    # identity: stale extractors are silent otherwise
    from .identity import artifacts as af
    have = get_meta(conn, "artifact_ver")
    if sessions and str(have or "") and str(have) != af.ARTIFACT_VER:
        out.append(_check(WARN, "identity", "built by artifact extractor v%s, current is v%s"
                          % (have, af.ARTIFACT_VER), "batonpass identity"))
    elif sessions and not have:
        out.append(_check(WARN, "identity", "never built for this library",
                          "batonpass identity"))
    else:
        ok = conn.execute(
            "SELECT COUNT(*) FROM session WHERE project_key IS NOT NULL AND project_key<>''"
        ).fetchone()[0]
        ungrouped = sessions - ok
        out.append(_check(OK, "identity", "%d named project key(s), %d honestly ungrouped"
                          % (len(set(r[0] for r in conn.execute(
                              "SELECT project_key FROM session WHERE project_key IS NOT NULL"
                              " AND project_key<>''"))), ungrouped)))

    # index: does it still describe the messages that are there?
    try:
        docs = conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
        from .recall.index import FTS_VER

        vers = sorted(r[0] or "" for r in conn.execute(
            "SELECT DISTINCT ver FROM search_state"))
        stale = conn.execute(
            "SELECT COUNT(*) FROM session s WHERE s.msg_count > 0 AND NOT EXISTS"
            " (SELECT 1 FROM search_state d WHERE d.sid=s.sid)"
        ).fetchone()[0]
        behind = conn.execute(
            "SELECT COUNT(*) FROM search_state st WHERE st.last_seq <"
            " (SELECT COALESCE(MAX(seq),-1) FROM message m WHERE m.sid=st.sid)"
        ).fetchone()[0]
        if FTS_VER not in vers:
            out.append(_check(INFO, "index", "not built with this version",
                              "batonpass index"))
        elif stale or behind:
            # Not a warning: every search catches the index up before it runs.
            out.append(_check(INFO, "index", "%d session(s) not indexed yet, %d behind"
                              % (stale, behind),
                              "batonpass index   # or just run a find — it catches up"))
        else:
            out.append(_check(OK, "index", "%s documents" % format(docs, ",")))
    except sqlite3.Error:
        out.append(_check(INFO, "index", "not built yet — find has nothing to read",
                          "batonpass index"))
    conn.close()

    return out


def check_channel(channel):
    from .sync import channel as ch
    from .sync import wire

    channel = os.path.abspath(os.path.expanduser(channel))
    if not ch.is_channel(channel):
        return [_check(FAIL, "channel", "no %s at %s" % (ch.MARKER, channel),
                       "batonpass sync init %s" % channel)]
    out = []
    marker = {}
    try:
        import json
        with open(os.path.join(channel, ch.MARKER), encoding="utf-8") as fh:
            marker = json.load(fh)
    except (OSError, ValueError):
        pass
    if int(marker.get("schema") or 0) > ch.SCHEMA:
        out.append(_check(FAIL, "channel", "schema %s is newer than this build (%s)"
                          % (marker.get("schema"), ch.SCHEMA),
                          "upgrade batonpass on this machine"))
    devs = ch.devices(channel)
    out.append(_check(OK, "channel", "id %s, %d device(s), %s"
                      % ((marker.get("id") or "")[:12], len(devs),
                         _human(_dir_bytes(os.path.join(channel, "blobs"))))))
    for d in devs:
        when = d.get("last_seen") or ""
        ver = d.get("batonpass") or "?"
        if ver and ver != __version__:
            out.append(_check(INFO, "channel", "%s last seen %s, on batonpass %s"
                              % (d.get("device"), when, ver), None))
    if marker.get("enc"):
        from .sync import crypt

        try:
            out.append(_check(OK, "encryption", "%s, key %s"
                              % (marker["enc"], crypt.fingerprint(channel))))
        except RuntimeError:
            out.append(_check(WARN, "encryption",
                              "channel is sealed, but this machine has no key",
                              "export BATONPASS_KEY=…  (or ~/.batonpass/key)"))
        except Exception as exc:                  # cryptography missing
            out.append(_check(WARN, "encryption", "channel is sealed; %s" % exc,
                              'pip install "batonpass-cli[crypto]"'))
    sizes = sum(_streams(channel))
    out.append(_check(INFO, "channel", "%s of streams — `sync status` says how much is new"
                      % _human(sizes)))
    return out


def _streams(channel):
    """Total bytes of every stream file, per device directory."""
    sdir = os.path.join(channel, "stream")
    if not os.path.isdir(sdir):
        return
    for dev in sorted(os.listdir(sdir)):
        ddir = os.path.join(sdir, dev)
        if not os.path.isdir(ddir):
            continue
        total = 0
        for name in os.listdir(ddir):
            if name.endswith(".jsonl"):
                try:
                    total += os.path.getsize(os.path.join(ddir, name))
                except OSError:
                    pass
        yield total


def check_sources():
    from .discover import summary

    try:
        s = summary()
    except Exception as e:                    # a scan must never be what breaks doctor
        return [_check(WARN, "sources", "scan failed: %s" % e, "batonpass discover --explain")]
    t = s["totals"]
    out = [_check(OK if t["recognised"] else INFO, "sources",
                  "%d recognised, %s conversations, %s messages, %s on disk"
                  % (t["recognised"], format(t["sessions"], ","),
                     format(t["messages"], ","), _human(t["bytes_on_disk"])))]
    if s.get("unclaimed"):
        names = ", ".join(u["path"].split("/")[-1] if isinstance(u, dict) else str(u)
                          for u in s["unclaimed"][:5])
        out.append(_check(INFO, "sources", "%d dir(s) in the queue: %s"
                          % (len(s["unclaimed"]), names),
                          "batonpass discover --explain   # or open an issue with the name"))
    return out


def run(db_path=None, channel=None, sources=False) -> dict:
    from .paths import db_path as default_db

    db = str(db_path or default_db())
    checks = [check_python(), check_sqlite(), check_zstd(),
              check_disk(os.path.dirname(os.path.abspath(db)) or ".")]
    checks.extend(check_library(db))
    if channel:
        checks.extend(check_channel(channel))
    else:
        checks.append(_check(INFO, "channel", "not asked about (--channel PATH)",
                             "batonpass sync status PATH"))
    if sources:
        checks.extend(check_sources())
    return {"version": __version__, "platform": "%s %s" % (platform.system(),
                                                           platform.release()),
            "db": db, "channel": channel, "checks": checks}


def render(result) -> str:
    lines = ["batonpass doctor %s · %s · %s"
             % (result["version"], result["platform"], result["db"]), ""]
    for c in result["checks"]:
        lines.append("%s  %-12s %s" % (_LABEL.get(c["level"], c["level"]),
                                       c["name"], c["detail"]))
        if c.get("fix"):
            lines.append("            → %s" % c["fix"])
    bad = [c for c in result["checks"] if c["level"] == FAIL]
    lines.append("")
    if bad:
        lines.append("%d thing(s) need a hand. Nothing was changed." % len(bad))
    else:
        lines.append("Nothing broken. Nothing was changed.")
    return "\n".join(lines)