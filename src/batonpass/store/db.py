"""Connections.

Two kinds, with a hard wall between them:
  connect()     our library, read-write
  source_ro()   somebody else's database, read-only, no escape hatch
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from ..paths import db_path
from .schema import DDL, SCHEMA_VERSION


def connect(path=None) -> sqlite3.Connection:
    p = db_path(path)
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    for stmt in DDL:
        conn.execute(stmt)
    _migrate(conn)
    stored = get_meta(conn, "schema_version")
    if stored is None:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    elif stored.isdigit() and int(stored) < SCHEMA_VERSION:
        # Every upgrade so far has been additive, and the DDL above has just
        # run with IF NOT EXISTS — so the library is already at this version.
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()
    return conn


def _migrate(conn) -> None:
    """Columns added after a table first shipped.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a new
    column is the one upgrade that is not free. Each entry is a check plus an
    ALTER, idempotent, and the list doubles as the honest history of the
    schema. Nothing is dropped and nothing is rewritten.
    """
    for table, column, decl in (
        ("task_event", "tool", "TEXT"),
        ("part", "n", "INTEGER NOT NULL DEFAULT 1"),
    ):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if cols and column not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))


def source_ro(path):
    """Open a source strictly read-only. Returns (conn, mode)."""
    for uri, mode in (("file:%s?mode=ro" % path, "readonly"),
                      ("file:%s?mode=ro&immutable=1" % path, "readonly-immutable")):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn, mode
        except sqlite3.OperationalError:
            continue
    raise sqlite3.OperationalError("cannot open %s read-only" % path)


def set_meta(conn, key, value) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_meta(conn, key, default=None):
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row[0] if row else default


def file_signature(path) -> dict:
    """Size+mtime of the database and its sidecars — the read-only receipt.

    Kept as separate keys on purpose. The main file must not move while we read
    it; the -wal file legitimately does when the *owning application* is running
    (Alma writes its chat log while the user is chatting). Treating those two as
    one fingerprint produces a false "source changed" alarm on every live source.
    """
    out = {}
    for suffix, key in (("", "main"), ("-wal", "wal"), ("-shm", "shm")):
        try:
            st = Path(str(path) + suffix).stat()
            out[key] = "%d:%d" % (st.st_size, st.st_mtime_ns)
        except OSError:
            out[key] = None
    return out


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


class Timer:
    def __init__(self):
        self.t0 = time.time()

    @property
    def done(self) -> float:
        return time.time() - self.t0
