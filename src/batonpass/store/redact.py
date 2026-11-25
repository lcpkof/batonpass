# -*- coding: utf-8 -*-
"""Take the credentials out of anything that leaves this machine.

A chat transcript is the worst place to keep a secret, because people paste
them: "here is the key, why does it 401". Tier 1 is the tier that *always*
travels — to a USB stick, to a shared NAS, into iCloud — so it is the tier that
must not carry the key you pasted last Tuesday.

The library keeps the raw text: it is your copy, and `export` should hand back
what was really said. The channel is the shared copy, and there the secret is
replaced with `<redacted>`.

Deliberately narrow. These patterns match things that are *shaped* like
credentials rather than every long word: a false positive costs you one word
in a summary, a false negative costs you a key.
"""

from __future__ import annotations

import re

# (name, pattern) — the secret is always the LAST group, and anything before it
# is a prefix worth keeping: `sk-<redacted>` still tells you what kind of key
# it was, which is what you need in order to go and rotate it.
_PATTERNS = (
    ("sk", re.compile(r"\b(sk-(?:proj-|ant-|or-|live-|test-)?)([A-Za-z0-9]{16,})")),
    ("pypi", re.compile(r"\b(pypi-)([A-Za-z0-9_\-]{16,})")),
    ("github", re.compile(r"\b(gh[pousr]_|github_pat_)([A-Za-z0-9_]{16,})")),
    ("aws", re.compile(r"\b(AKIA|ASIA)([0-9A-Z]{12,})")),
    ("google", re.compile(r"\b(AIza)([0-9A-Za-z_\-]{30,})")),
    ("slack", re.compile(r"\b(xox[abprs]-)([A-Za-z0-9\-]{10,})")),
    ("bearer", re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{16,})")),
    # api_key=…, "token": "…", password: … — the name says what it is
    ("key", re.compile(
        r"(?i)\b([A-Za-z0-9_\-]*(?:api[_\-]?key|token|secret|password|passwd)"
        r"[A-Za-z0-9_\-]*\s*[:=]\s*[\"']?)"
        r"([A-Za-z0-9._\-+/=]{16,})")),
)

_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.S,
)

MARK = "<redacted>"


def scrub(text: str) -> tuple:
    """Return (clean_text, how_many_replaced).

    Never raises and never returns None: callers stream this straight into a
    channel, and a summary that fails to redact must not take the push down.
    """
    if not text:
        return text, 0
    if not isinstance(text, str):
        text = str(text)
    hits = 0

    def _pem(_m):
        nonlocal hits
        hits += 1
        return "-----BEGIN PRIVATE KEY-----%s-----END PRIVATE KEY-----" % MARK

    try:
        text = _PEM.sub(_pem, text)
        for _name, rx in _PATTERNS:
            def _sub(m, rx=rx):
                nonlocal hits
                hits += 1
                return m.group(1) + MARK
            text = rx.sub(_sub, text)
    except Exception:                    # a redactor that throws is a leak
        return text, hits
    return text, hits


def looks_like_a_secret(text: str) -> bool:
    """For tests and for `doctor`: is there anything in here we would scrub?"""
    return scrub(text)[1] > 0
