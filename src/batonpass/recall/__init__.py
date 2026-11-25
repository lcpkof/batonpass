"""Search: the index is local and disposable, the queries are not."""

from .index import build as build_index
from .index import ensure as ensure_tables
from .index import ensure_fresh as ensure_index
from .index import stats as index_stats
from .query import parse_window, search

__all__ = ["build_index", "ensure_index", "ensure_tables", "undecodable_hint", "index_stats", "parse_window", "search"]


def undecodable_hint(conn) -> str:
    """Why a miss might not be a miss.

    Two ways a library can be honest and still useless: its texts are stored under a
    codec this machine cannot decode, or the text never travelled at all (a tier-1
    pull carries summaries, not transcripts). In both cases "nothing matched" is
    literally true and completely misleading, so the sentence says which one it is.

    The test for the second case is *message rows*, not index documents: a summary
    library is searchable — it has one document per session — and that is exactly
    how a miss there stays silent when it should not.
    """
    from ..store.content import HAVE_ZSTD

    n = 0
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM content WHERE codec LIKE 'zstd%'").fetchone()[0]
    except Exception:
        n = 0
    if n and not HAVE_ZSTD:
        return ('(%d stored texts cannot be read without zstandard — '
                'pip install "batonpass-cli[zstd]")' % n)
    try:
        sess = conn.execute(
            "SELECT COUNT(*) FROM session WHERE msg_count > 0").fetchone()[0]
        bodies = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    except Exception:            # a library too young for these tables
        return ""
    if sess and not bodies:
        return ("(%d session(s) are on record here, but not one message — this "
                "library holds summaries only. Pull deeper: "
                "batonpass sync pull PATH --tier 2)" % sess)
    return ""
