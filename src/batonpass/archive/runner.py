"""Running an archive pass.

One source at a time, in its own transaction. A source that blows up is rolled
back and recorded; the rest of the run continues. Partial success is normal and
must never look like total failure.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone

from ..store.content import Codec, get_dict, save_dict, sha256_bytes, train_dict
from ..store.db import connect, file_signature, source_ro
from ..store.write import upsert_raw_source
from .adapters import ADAPTER_VER, ArchiveResult, get
from .adapters import _ingest


def device_name() -> str:
    return os.environ.get("BATONPASS_DEVICE") or socket.gethostname().split(".")[0]


def archive_one(conn_lib, adapter_name, path, device, level=1, dry_run=False, limit=None):
    adapter = get(adapter_name)
    result = ArchiveResult(adapter_name, os.path.abspath(path))
    before = file_signature(path)
    result.bytes_in = os.path.getsize(path)
    result.source_sha = sha256_bytes(("%s|%s|%s" % (adapter_name, result.path,
                                                   before["main"])).encode())

    if dry_run:
        conn_src, mode = source_ro(path)
        result.readonly_mode = mode
        try:
            ses = list(adapter.sessions(conn_src))
            result.sessions = len(ses)
            result.messages = sum(1 for s in ses for _ in adapter.messages(conn_src, s["source_sid"]))
        finally:
            conn_src.close()
        result.note("dry-run: nothing written")
        return result

    codec = Codec(get_dict(conn_lib, adapter_name))
    conn_src, mode = source_ro(path)
    result.readonly_mode = mode
    try:
        upsert_raw_source(conn_lib, result.source_sha, device, adapter_name, ADAPTER_VER,
                          result.path, result.bytes_in)
        if level >= 3 and not get_dict(conn_lib, adapter_name):
            d = _train_from_source(adapter, conn_src)
            if d:
                save_dict(conn_lib, adapter_name, d)
                codec = Codec(d)
                result.note("trained %d-byte dictionary" % len(d))
        with conn_lib:
            _ingest(conn_lib, result, codec, adapter_name, device, conn_src,
                    adapter.sessions(conn_src), adapter.messages,
                    limit=limit, level=level)
    except Exception as exc:
        conn_lib.rollback()
        result.errors.append("%s: %s" % (type(exc).__name__, exc))
    finally:
        conn_src.close()

    after = file_signature(path)
    grew = _size(before["main"]) != _size(after["main"])
    touched = before["main"] != after["main"] or before["wal"] != after["wal"]
    if grew:
        result.errors.append("SOURCE SIZE CHANGED DURING READ (%s -> %s bytes); "
                             "re-run to pick up the new rows"
                             % (_size(before["main"]), _size(after["main"])))
    elif touched:
        result.note("its app wrote to the file while we read; row counts still reconcile")
    else:
        result.note("source untouched (%s)" % mode)
    return result


def _size(sig):
    return int(str(sig).split(":")[0]) if sig else -1


def _train_from_source(adapter, conn_src):
    samples, n = [], 0
    for s in adapter.sessions(conn_src):
        for m in adapter.messages(conn_src, s["source_sid"]):
            for chunk in (m.get("text"),):
                if chunk and len(chunk) > 32:
                    samples.append(chunk.encode()[:4096])
            for p in m.get("parts") or []:
                for chunk in (p.get("args"), p.get("text")):
                    if chunk and len(chunk) > 32:
                        samples.append(str(chunk).encode()[:4096])
            n += 1
            if n > 4000:
                return train_dict(samples)
    return train_dict(samples)


def run_archive(only=None, level=1, limit=None, db=None, dry_run=False,
               with_identity=True):
    conn = connect(db)
    device = device_name()
    from ..discover import sources as discover_sources
    from .adapters import ADAPTERS as KNOWN
    found = discover_sources()
    wanted = [f for f in found if f.get("adapter") in KNOWN]
    unsupported = sorted({f.get("adapter") for f in found
                          if f.get("adapter") and f.get("adapter") not in KNOWN})
    if only:
        names = set(only)
        wanted = [f for f in wanted if f.get("adapter") in names]
    out = {"device": device, "when": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "sources": [], "totals": {}, "unsupported": unsupported}
    for f in wanted:
        r = archive_one(conn, f["adapter"], f["path"], device, level=level,
                        dry_run=dry_run, limit=limit)
        out["sources"].append(r.as_dict())
    for key in ("sessions", "messages", "parts", "raw_records", "new_rows", "skipped"):
        out["totals"][key] = sum(s[key] for s in out["sources"])
    # Identity is derived, so it runs after the append, not inside it: a source
    # that failed to archive must not leave a half-labelled project behind.
    if with_identity and not dry_run:
        from ..identity import build
        out["identity"] = build(conn)
    return out
