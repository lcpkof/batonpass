"""Schema, version 1.

raw_*   append-only compressed source payloads — the parser's undo button.
content content-addressed plaintext, refcounted.
session/message/part   the canonical model, rebuildable from raw at any time.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

DDL = [
    """
    CREATE TABLE IF NOT EXISTS raw_source (
      source_sha  TEXT PRIMARY KEY,
      device      TEXT NOT NULL,
      adapter     TEXT NOT NULL,
      adapter_ver TEXT NOT NULL,
      path        TEXT,
      bytes       INTEGER,
      first_seen  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_record (
      id          INTEGER PRIMARY KEY,
      source_sha  TEXT NOT NULL REFERENCES raw_source(source_sha),
      seq         INTEGER NOT NULL,
      kind        TEXT NOT NULL,
      payload     BLOB,
      payload_sha TEXT NOT NULL,
      codec       TEXT NOT NULL DEFAULT 'none',
      UNIQUE(source_sha, seq, kind)
    )
    """,
    "CREATE INDEX IF NOT EXISTS raw_record_sha ON raw_record(payload_sha)",
    """
    CREATE TABLE IF NOT EXISTS content (
      sha256   TEXT PRIMARY KEY,
      byte_len INTEGER NOT NULL,
      refcount INTEGER NOT NULL DEFAULT 1,
      codec    TEXT NOT NULL DEFAULT 'none',
      body     BLOB NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session (
      sid              TEXT PRIMARY KEY,
      source           TEXT NOT NULL,
      source_sid       TEXT NOT NULL,
      device           TEXT NOT NULL,
      project_key      TEXT,
      project_key_tier INTEGER,
      cwd              TEXT,
      title            TEXT,
      created_at       TEXT,
      updated_at       TEXT,
      msg_count        INTEGER DEFAULT 0,
      first_msg_at     TEXT,
      last_msg_at      TEXT,
      forked_from      TEXT,
      artifact_refs    TEXT,
      adapter_ver      TEXT,
      content_hash     TEXT,
      UNIQUE(source, source_sid, device)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS message (
      mid       TEXT PRIMARY KEY,
      sid       TEXT NOT NULL REFERENCES session(sid),
      seq       INTEGER NOT NULL,
      role      TEXT NOT NULL,
      text_sha  TEXT,
      model     TEXT,
      ts        TEXT,
      meta_json TEXT,
      UNIQUE(sid, seq)
    )
    """,
    # no separate indexes on (sid,seq) / (mid,seq): the UNIQUE constraints
    # already create exactly those b-trees. Measured on a 745k-row library the
    # redundant copies cost 35 MB and speed nothing up.
    """
    CREATE TABLE IF NOT EXISTS part (
      pid        TEXT PRIMARY KEY,
      mid        TEXT NOT NULL REFERENCES message(mid),
      seq        INTEGER NOT NULL,
      kind       TEXT NOT NULL,
      name       TEXT,
      args_sha   TEXT,
      result_sha TEXT,
      is_error   INTEGER DEFAULT 0,
      ts         TEXT,
      UNIQUE(mid, seq)
    )
    """,

    """
    CREATE TABLE IF NOT EXISTS task (
      tid         TEXT PRIMARY KEY,
      project_key TEXT NOT NULL,
      title       TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      UNIQUE(project_key, title)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_event (
      eid        TEXT PRIMARY KEY,
      tid        TEXT NOT NULL REFERENCES task(tid),
      device     TEXT NOT NULL,
      lamport    INTEGER NOT NULL,
      kind       TEXT NOT NULL,
      body       TEXT,
      refs_json  TEXT,
      created_at TEXT NOT NULL
    )
    """,
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
]
