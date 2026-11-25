"""Getting prose out of a stored body.

Some sources store a message as a JSON envelope rather than as text — billion
keeps `{"id":...,"role":"user","contentType":"text","text":"..."}` — and the
envelope sometimes escapes its newlines twice, so a reader sees `\\n` where a
human wrote a line break.

We unwrap at read time rather than rewrite the library, because rewriting would
mean re-archiving every source and every stored hash along with it. The rule is
narrow on purpose: only an object that actually carries a text-ish field is
unwrapped, so a tool result that happens to be JSON is left exactly as it is.
"""

from __future__ import annotations

import json

TEXT_KEYS = ("text", "content", "body", "message", "value", "prompt")
MAX_DEPTH = 3


def unwrap(text, depth: int = 0) -> str:
    if not isinstance(text, str):
        return text or ""
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[" or depth >= MAX_DEPTH:
        return text
    try:
        obj = json.loads(stripped)
    except ValueError:
        return text
    if isinstance(obj, list) and obj and all(isinstance(x, str) for x in obj):
        return "\n".join(obj)
    if not isinstance(obj, dict):
        return text
    for key in TEXT_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return unescape(unwrap(value, depth + 1))
    return text


def unescape(text: str) -> str:
    """Undo a second round of escaping (`\\n` printed literally)."""
    if not text:
        return text
    if "\\n" in text or "\\t" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    return text


def clean(text) -> str:
    # Order matters: unescaping first would rewrite `\n` *inside* the JSON
    # string literal, which leaves invalid JSON and no prose at all.
    return unescape(unwrap(text if isinstance(text, str) else (text or "")))
