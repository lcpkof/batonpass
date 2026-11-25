---
name: Unrecognized source
about: batonpass doesn't recognise an AI tool whose history you want to keep
title: "[source] <tool name> not recognised"
labels: source-request
---

<!--
PLEASE do not paste your transcripts, your prompts, or your message content.
A schema fingerprint is enough to build an adapter. Nothing below requires real data.
-->

## The tool

- **Name / version:**
- **OS:**
- **Where the history lives** (path or `-- -> Settings -> Data` hint):

## What `batonpass discover --explain` says about it

```
<paste the block for this source>
```

## Schema fingerprint

Run this and paste the output — it lists table and column **names** only, never rows:

```bash
python3 - <<'PY'
import sqlite3, sys, json, pathlib
p = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "<PATH TO .db>")
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
out = {}
for (t,) in c.execute("select name from sqlite_master where type='table' order by 1"):
    cols = [r[1] for r in c.execute(f'pragma table_info("{t}")')]
    n = c.execute(f'select count(*) from "{t}"').fetchone()[0]
    out[t] = {"columns": cols, "rows": n}
print(json.dumps(out, indent=2))
PY
```

## Optional: the `discover` report

Attach the HTML report if you're comfortable — it contains counts and paths, no message
content. Delete it if the paths are sensitive; the fingerprint above is the part that
actually matters.

## What you were trying to do

<!-- e.g. "continue a coding task started on my other machine" -->
