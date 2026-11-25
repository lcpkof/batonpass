#!/usr/bin/env python3
"""Compatibility shim.

Before 0.2 this file *was* the project. It still works exactly like it used to:

    python3 discover.py
    python3 discover.py --json out.json --deep

The real code now lives in src/batonpass/discover/probe.py, so `pip install
batonpass` and `git clone` behave identically.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from batonpass.discover import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
