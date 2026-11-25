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
                'pip install "batonpass[zstd]")' % n)
    try:
        docs = conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
    except Exception:            # no index tables yet: nothing has been searchable
        docs = 0
    try:
        sess = conn.execute(
            "SELECT COUNT(*) FROM session WHERE msg_count > 0").fetchone()[0]
    except Exception:
        return ""
    if sess and not docs:
        return ("(%d session(s) are on record, but none of their text is in this "
                "library — pull deeper: batonpass sync pull PATH --tier 2)" % sess)
    return ""
