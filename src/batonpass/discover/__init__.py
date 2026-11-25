"""Source discovery — the read-only half of batonpass.

`probe.py` is the scanner: it walks the well-known locations, identifies what it
finds, and explains itself. It opens every database with `mode=ro` and never
imports a network module, which is what lets the README claim "zero writes" and
mean it literally.
"""

from __future__ import annotations

import argparse

from .probe import (  # noqa: F401
    VERSION,
    main,
    probe_roots,
    render,
    resolve,
    run,
)

__all__ = ["VERSION", "main", "probe_roots", "render", "resolve", "run", "sources"]


def sources(deep=False):
    """Discovered, recognised sources as plain dicts — what `archive` consumes."""
    args = argparse.Namespace(only=None, explain=False, deep=deep,
                              report=None, json_path=None)
    summary = run(args)
    return [s for s in summary["sources"] if s.get("adapter")]


def summary(deep=False):
    args = argparse.Namespace(only=None, explain=False, deep=deep,
                              report=None, json_path=None)
    return run(args)
