"""Search: the index is local and disposable, the queries are not."""

from .index import build as build_index
from .index import ensure as ensure_index
from .index import stats as index_stats
from .query import parse_window, search

__all__ = ["build_index", "ensure_index", "index_stats", "parse_window", "search"]
