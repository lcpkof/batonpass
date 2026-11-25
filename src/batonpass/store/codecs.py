"""One codec per source, resolved lazily.

Every source gets its own compression dictionary because the training data is
its own history — a zcode dictionary does nothing for billion-context rows and
vice versa. Readers therefore cannot pick a codec out of the air; they have to
ask which source a row came from first. This object is that "ask once, cache
forever" cache, so callers stop rediscovering it.
"""

from __future__ import annotations

from .content import Codec, get_content, get_dict


class Codecs:
    def __init__(self, conn):
        self.conn = conn
        self._cache = {}

    def for_source(self, source: str) -> Codec:
        key = source or ""
        codec = self._cache.get(key)
        if codec is None:
            codec = Codec(get_dict(self.conn, key))
            self._cache[key] = codec
        return codec

    def get(self, sha: str, source: str) -> str:
        if not sha:
            return ""
        return get_content(self.conn, sha, self.for_source(source))
