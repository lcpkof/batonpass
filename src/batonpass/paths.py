"""Where things live. One directory is ours; nothing else is written."""

from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "BATONPASS_HOME"
DB_NAME = "library.db"
DEFAULT_HOME = "~/.batonpass"


def home() -> Path:
    return Path(os.path.expanduser(os.environ.get(ENV_HOME) or DEFAULT_HOME))


def ensure_home() -> Path:
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    return h


def db_path(explicit=None) -> Path:
    if explicit:
        return Path(os.path.expanduser(str(explicit)))
    return ensure_home() / DB_NAME
