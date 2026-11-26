# batonpass

[![PyPI](https://img.shields.io/pypi/v/batonpass-cli)](https://pypi.org/project/batonpass-cli/)
[![Python](https://img.shields.io/pypi/pyversions/batonpass-cli)](https://pypi.org/project/batonpass-cli/)
[![License](https://img.shields.io/pypi/l/batonpass-cli)](LICENSE)
[![CI](https://github.com/lcpkof/batonpass/actions/workflows/ci.yml/badge.svg)](https://github.com/lcpkof/batonpass/actions/workflows/ci.yml)

> **Never drop the baton.**
> Switch machines mid-task — your work picks up exactly where you left it.

You are 3 hours into a task on the Mac mini. You open the laptop. Nothing is there.

Not the transcript, not the tool output that told you which endpoint 404'd, not the reason
you rejected the third approach. You remember the *concept* — "the岗位表 thing, I fixed the
join" — and you spend 20 minutes re-deriving what you already knew.

`batonpass` is the thing that hands the baton over: **from one machine to the next, from
last-week-you to today-you**, without the work touching the ground.

```
batonpass setup                        # scan · remember · hand your agents the key  (one command)
batonpass discover                     # what AI history is on this machine?  (read-only, 2 s)
batonpass archive                      # pull it into a portable library  (~1 min per GB)
batonpass export <session> --text      # read one conversation back out
batonpass sync push ~/Dropbox/baton    # one file per machine, summaries only (~2 MB)
batonpass sync pull ~/Dropbox/baton    # on the other machine: 713 sessions, zero messages
batonpass index                        # local search index, ~17 s per 700 sessions
batonpass find "岗位表" --at 7d           # search it, with time windows
batonpass tasks                        # every project, one line each
batonpass resume                       # handover card for the directory you are in
batonpass places                       # every folder, and which tools worked in it
batonpass relay                        # who has the baton here, and what they did last
batonpass serve                        # every tool's history in one browser window
batonpass size                         # what the library weighs, line by line
batonpass slim --dry-run               # what a slim run would give back (changes nothing)
batonpass slim --days 7 --vacuum       # give the disk back, keep the pointer
batonpass fetch <session>              # need the detail again? re-read it from the tool
```

---

## One command: `batonpass setup`

```bash
uvx --from batonpass-cli batonpass setup
```

Four lines come out, and then your agents can already answer for you:

```
found       5 source(s), 726 conversations, 268,272 messages
archived    724 sessions, 267,973 messages, 7,596 new rows
indexed     60,190 documents, 724/724 sessions searchable
agents      Alma connected, Claude Code connected
```

What it does, in order — and every step is also its own verb if you want to run it alone:

1. **scan** — `discover`, read-only, tells you what is actually on the disk
2. **remember** — `archive`, into `~/.batonpass/library.db`, nothing touched in place
3. **index** — `find` and the MCP tools need this to be fast
4. **connect** — adds an `mcpServers.batonpass` entry to the agent configs it finds
   (Alma, Claude Code, Claude Desktop, Cursor), each one copied to `.bak-batonpass` first.
   **It asks first.** No terminal to ask, no edit — `--yes` if you mean it.

Then, in any agent that speaks MCP:

> *"search my history for the thing I fixed last Tuesday"*

The third and fourth machine are the same sentence:

```bash
batonpass setup --join ~/Dropbox/baton    # pull summaries, index them, connect the agents
```

---

## The rule that makes this safe to point at your machine

**batonpass never writes to your AI tools' data. Ever.**

Your Claude / Codex / Cursor / zcode / dsh / billion databases are opened `mode=ro`.
No schema migrations, no `ALTER TABLE`, no "helpful" cleanup. If a tool ships a new
schema tomorrow, batonpass reads what it can, names what it couldn't, and never touches
the file to find out.

This is not a preference, it's the first law, and `discover` is built to prove it: it
refuses to import a networking module, and it records any source it had to open
immutably instead of silently proceeding.

A database is not the only place a transcript lives. dsh keeps its real conversations as
append-only **zstd JSONL files** — one file per session, one frame per flush — so those
are read as files: streamed frame by frame, nothing written back, no unpacked copy left
on disk. 956 such sessions (501 MB of `.zstd`) go in as 148k messages, and `fetch`
re-reads the original file when a purged body comes back.

**Read-only in · you own the copy out.**

---

## Start here: `batonpass discover`

Before you trust a tool with your history, look at what's actually there.

```bash
uvx --from batonpass-cli batonpass discover --report ~/Desktop/batonpass-report.html
```

One file, standard library only, a few seconds, no network, no writes. It produces a
single self-contained HTML page telling you:

- **what it found** — 261,354 messages across 713 conversations in 9 sources, 2.2 GB on my machine
- **how much it can actually read** — every source gets a coverage verdict, not a vibe
- **what it is silently failing to read, and why** — a skip ledger: `browser-cache: 14`,
  `backup-copy: 4`, `nested-artifact: 1`. A skip you can see is not a silent skip.
- **what it refused to guess** — databases it recognised *without* a known adapter, with
  the reason and a repro command, so you can open an issue instead of debugging alone

A real run on this machine:

```
261,354 messages · 713 conversations · 9 sources recognised · 2.2 GB
26 files skipped (browser caches, backups)   ·   7 dirs still in the queue
billion   212,433 msg   385 sess   1,489.9 MB   degraded: media-by-reference
zcode      48,078 msg   304 sess     847.8 MB   ok
alma           75 msg     2 sess      11.4 MB   ok
zcode-tasks   203 tasks  10 projects   5 hosts  ok   (role: index)
```

Note the third line: 1.4 GB of that source is media stored by reference — the text is
fine, the images aren't in the DB. `batonpass` says so up front instead of handing you a
176 MB archive that turns out to be empty in the middle.

---

## Then: three tiers, pick your cost

| Tier | What you get | Time | Cost |
|---|---|---|---|
| **A · Look** | The report above. No library, no config, no writes. | 30 s | 0 |
| **B · Archive one machine** | Full local library, `find` / `resume` over it. | ~5 min setup, 6–13 min first archive | ~250–450 MB from 3.8 GB |
| **C · Sync machines** | Every machine's history in one place, details on demand. | +2 min | 2 MB of catalog replicated first |

Tier C is the interesting one, and it's built on a deliberate split:

**2 MB of catalog first, the details only when you ask.**

A new machine doesn't download your history. It downloads an index — who, when, which
task, which file — in about two seconds, and then pulls transcripts *for the task you're
actually continuing*. Measured on real data: `message.data` gzips 5.43 MB → 0.80 MB and
zstd-19s to **0.28 MB (19.7×)**. You don't need the other 250 MB to keep working.

The summary tier is also the tier that goes everywhere — a USB stick, a shared NAS, your
iCloud folder — so it's the tier that gets scrubbed: keys, tokens and PEM blocks pasted
into a chat are replaced with `sk-<redacted>` on the way out. Your own library still has
the raw text, because `export` should tell you what was actually said. (On our first real
channel this caught 32 credentials, including one live key from a chat six months old.)

## Why a baton, and not a sync

When you're mid-task on two machines, the second machine doesn't need a *merge*, it needs
to know **what you already decided**. So the store is append-only and the conflict rule is
one line: **fork, never merge**. Two machines can't corrupt each other because neither one
writes the other's file. Transcripts are immutable facts; the only mutable thing is
`task.status`, which is itself a stream of appended events.

The result: you can work on the Mac mini and the laptop **at the same time**, and
`batonpass resume` will show you both branches and tell you they diverged — instead of
picking one silently and losing the other.

---

## The same folder, seen from every tool

Every tool has its own idea of a project, and none of those ideas travel. Alma keeps
workspaces in a sqlite table, dsh in one `workspace.json` per instance, zcode only in
whatever directory you happened to launch it from. You cannot join them on a "project
list" — there isn't one to join.

So the join is the one thing all of them agree on: **the absolute path**.

```
PROJECT                    TOOLS   SESS  LAST           PATH
ai-agent-memory-continuity alma       1  刚刚           ~/Documents/Alma/2026-11-25/ai-agent-memory-continuity
luocp                      dsh       93  11-25 19:30    ~/luocp
gp                         zcode     48  11-18 12:11    ~/Documents/GP
```

A directory is in that list because some tool named it — never because we guessed it, and
never because we synthesised a path from a project id. A tool with no opinion about
directories simply has no rows. Scratch folders that no tool can put a name to stay out
of the way unless you ask for them (`--all`).

Once two tools can see one folder, one of them can hand it over:

```
batonpass relay                                            # who has it, and what the other side did
batonpass relay --take --note "先把 dsh 的目录并进来"
batonpass relay --next "把 MCP 两个新工具接上"
batonpass relay --done
```

The baton is not stored anywhere. It is computed from an append-only log:

- every event is content-hashed, so writing the same note twice writes it once;
- a claim carries a Lamport number; the highest number holds the baton;
- two claims at the same number are **both kept** and resolved by `(device, event id)` —
  arbitrary, but *identical on every machine*, so two agents that never talk still agree;
- the log rides the sync channel alongside the catalog (an event is immutable and
  content-hashed, which is the one thing a shared folder carries without a merge rule),
  so "every machine" includes the one that was asleep;
- the loser is not deleted. The card says "claimed at the same moment", because that is
  what happened;
- `done` / `release` free the baton and do **not** promote the runner-up — nobody inherits
  an obligation they never accepted;
- a `done` retires the plan written before it (whoever wrote it is the one who finished
  it), while a `release` leaves that plan standing for whoever picks the work up.

And `resume` answers the one question a card cannot: is the folder occupied *right now*.

```
⚠ dsh 正在这个目录里干活（4 分钟前，设备 c8806d63a6c9），说：「我来写 UI」 — 先 relay 再动手
```

That line exists because the expensive failure is not forgetting what you did. It is two
agents doing the same hour of work in the same directory, and finding out afterwards.

---

## Status

| verb | state |
|---|---|
| `discover` | **shipped** — read-only inventory, ~2 s, one HTML page |
| `archive` | **shipped** — five adapters, replay-safe (re-run adds 0 rows) |
| `sync` | **shipped** — append-only channel, tiered push/pull, replay-safe both ways |
| `identity` | **shipped** — machine-independent project key, artifact refs, handover cards |
| `index` · `find` | **shipped** — FTS5 trigram over everything you typed, with time windows |
| `tasks` · `card` · `resume` | **shipped** — the handover packet, from a project name or a directory |
| `sync compact` | **shipped** — a re-pushed stream collapses back to one line per fact |
| `places` | **shipped** — one project = one path; every tool's workspaces merged by folder |
| `relay` | **shipped** — append-only claims + Lamport order; the baton is computed, never stored |
| `doctor` | **shipped** — every "why doesn't it work" turned into one line you can act on |
| `mcp` | **shipped** — `memory_search` · `memory_add` · `resume` · `places` · `relay` over stdio |
| `serve` | **shipped** — every tool, every machine, one read-only page in the browser |
| `size` | **shipped** — the library split into prose · tool I/O · index · catalog, so the trade is visible |
| `slim` | **shipped** — drop reproducible bodies outside a window, fold runs of identical calls, keep every pointer |
| `fetch` | **shipped** — the other half of `slim`: re-read a session from the tool's own database |

`archive`, measured end-to-end on this machine — all five sources, one pass:

```
712 sessions · 261,310 messages · 484,573 parts · 52 s
2.2 GB of source databases   →   466 MB portable library
448 MB of plaintext          →   179.5 MB stored   (2.50×, trained dict + zstd-19)
```

The number that matters more than the ratio: **running it again adds zero rows.**
Counts reconcile against the sources exactly — 304/304 sessions, 48,168/48,168
messages — and parts match once you account for the 72,184 `step-start` /
`step-finish` rows it skips *by name*. A message with no prose (a tool call, a
reasoning block) becomes a structured part instead of being thrown away. Nothing is
dropped silently; anything unaudited is reported as a number you can argue with.

`identity` + `find`, measured on that same 713-session library:

```
713 sessions                →  12 real projects · 558 honestly ungrouped
                                57 sessions folded into `xueji`, 50 into `gp`
6,479 deliverables          →  found in 575 sessions (xlsx, csv, pdf, png, md…)
58,508 messages indexed     →  16.6 s one-off · 164 MB · 59212 docs
batonpass find 岗位表        →  one hit, with the sentence it appeared in
batonpass find 妖族          →  nothing   (it never appears — 妖股 does)
```

And the round trip, on a machine that has never seen the data — it pulls 356 KB
of catalog (t0) and can already say what is in flight:

```
pull t0                     →  713 sessions, 713 new, 0.04 s
batonpass tasks             →  12 projects, sizes, devices, stale marks
batonpass card xueji        →  the same card, forks and all
```

The ungrouped 558 are the point. Every one of them had a session title that looked
like a project name ("这道题我做错了，帮我讲讲"), and promoting those produced 400
clusters of one — structure that was pure invention. They stay searchable; they are just
not lied about.

## The disk, and what a pointer is worth

A library of 725 sessions is 716 MB, and only 12% of it is anything you would miss.
`size` says so; `slim` does something about it:

```
716.6 MB  →  365.3 MB     one run, 18 s (--days 7 --vacuum)
  156,669 bodies emptied   224 MB   tool output and reasoning older than the window
  461,720 rows → 238,878    the same call repeated 37 times is now one row that says 37
  219,002 identity hashes   21 MB   a hash of a row in somebody else's database
  166 MB of index  →  360 KB  titles only; bodies are searched where they are stored
  prose +23 MB              stored as plain TEXT, one LIKE instead of 70,000 decodes
```

Nothing is deleted. A body that leaves is `content.codec='purged'`: the row, the sha and
the byte length stay, so every pointer still resolves and `find` still says a message
exists — it just cannot quote it. `batonpass fetch <session>` re-reads that session from
the tool's own database and puts it back (8,401 bodies, 4 s, verified byte-identical).
Prose is never a candidate: `message.text_sha` is the irreplaceable tier.

Two findings worth keeping, because both were guesses that tested false:

* **Capping body documents does not fix a trigram index.** 60,284 docs at 800 chars still
  came to 213 MB of `search_fts_data` for 14 MB of text — Chinese prose makes nearly every
  trigram unique to its document, so the index costs ~15× the text. Truncation moved it
  from 213 MB to 213 MB.
* **Decoding is the expensive part, not scanning.** A full decode-and-scan of 59,720
  messages costs 1.7 s; the same scan over plain TEXT costs 0.2 s. So prose is stored
  uncompressed (cheaper at the point of use, +23 MB on disk) and the index keeps the one
  list that must be fast: what sessions are called.

The window is a price list, not a policy — `slim --dry-run` prices it before you spend:

```
--days 1   221.8 MB     --days 7   131.5 MB     --days 30   33.4 MB
--days 3   169.6 MB     --days 14   57.0 MB     --days 90    7.8 MB
```

Across machines the same rule holds. A peer is sent folded rows as one row (`n`) and
purged bodies as a pointer with no body, counted as `archived` — it does not get a copy of
bytes it could re-read from the tool itself. That is the answer to "don't copy the file,
read the directory": for a place one machine can read, `raw_source.path` *is* the copy,
and `fetch` is how you go back to it.

`sync`, measured the same way — a real 713-session library, a channel folder, and a
second library that has never seen any of those apps:

```
library 528 MB              →   channel 2.8 MB      push 713 sessions in 2.0 s
                                   t0 catalog   356 KB
                                   t1 summaries 2.70 MB  (incl. the files each session produced)
fresh machine reads t0        0.04 s   → 713 sessions, 0 messages
              then t1         0.05 s   → every session has a first ask, its last rounds,
                                          and the files it produced
deep-pull one task            0.14 s   → 64 messages, 256 parts, 317 blobs
verify: 64/64 message hashes identical, 0 bodies differ
```

A channel is just a folder — a shared volume, a synced directory, a git remote, a USB
stick. Each machine appends only to *its own* file, so there is no lock, no leader and
no merge step, and pulling never writes into the channel. Everything is addressed by the
sha256 of its plaintext, which is why the same line arriving twice is indistinguishable
from it arriving once. The one cost of append-only: a re-pushed summary leaves the old
line behind, and a reader takes the last one. So a stream gets rewritten once it has
grown past a floor — `sync compact` (and every push, when it is worth it):

```
3 pushes of the same 713 sessions  →  1005 KB · 2139 lines
sync compact                       →   335 KB ·  713 lines   (one line per session)
```

Each rewrite bumps a generation counter in `heads/<device>.<tier>.json`. A reader keys
its byte offset on the generation, so a rewritten file simply looks new and is read from
zero — safe precisely because every import is an idempotent `INSERT OR IGNORE`.

### When the channel is a cloud drive

A folder on someone else's disk is a folder on someone else's disk. Say so at `init`:

```bash
export BATONPASS_KEY='the passphrase you will type on every machine'
batonpass sync init ~/Nutstore/batonpass --encrypt
```

Every payload is sealed with AES-256-GCM before it lands on the drive, keyed by scrypt
from that passphrase; blobs already in the channel are re-sealed in place. The key is
never written into the channel — it comes from `BATONPASS_KEY` or `~/.batonpass/key`.
Addressing stays on the plaintext sha256, so a re-sent message still lands on exactly the
same path, and a wrong passphrase produces one sentence (*"this channel is encrypted and
the key does not match"*) instead of a corrupt-archive mystery.

The stream lines are sealed too, not just the blobs. A line keeps only its identity in the
clear — wire version, tier, session id, message position — because compaction has to tell
two facts apart without a key; everything it *says* travels in `d`. On the first real
channel that meant 716 sessions of prose in 4.2 MB with zero readable characters in it.

What is **not** sealed: the file names of blobs, which are the plaintext sha256. Someone
holding a known text can confirm it is in the channel. That is a content-confirmation leak,
not a content leak, and it is what buys idempotency. Stating it is the point.

Sealed lines are wire v2, so upgrade every machine before you seal a channel — a newer
reader refuses an older channel politely; an older reader would misread a sealed one.

If it ever doesn't work, it says why instead of throwing:

```
$ batonpass doctor --channel ~/Dropbox/batonpass
OK    python       3.9.6
OK    sqlite       3.54.0, fts5 + trigram
WARN  zstd         not installed — archives stay readable, 2.50x becomes 2.04x
             → pip install "batonpass-cli[zstd]"
OK    library      680.6 MB, 713 sessions, 263,256 messages
OK    identity     12 named project key(s), 558 honestly ungrouped
OK    channel      id c62264a7f0b4, 1 device(s), 0 B
```

### Letting the AI tools call it

`batonpass mcp` is an MCP server on stdio — no daemon, no port, nothing left running
when the client exits. Three tools, because that is the whole vocabulary an agent needs:
`memory_search` (look something up by a vague phrase), `memory_add` (write down what it
just learned, before it is forgotten), `resume` (hand the baton over).

```json
{"mcpServers": {"batonpass": {"command": "uvx", "args": ["batonpass", "mcp"]}}}
```

The silence contract applies here too: a search that finds nothing returns
*"not found — do not invent one"*, because a model that hallucinates a file path costs
you twenty minutes of looking for a file that never existed.

If you run it and it *doesn't* recognise a source you use, that is the most useful bug
report there is — please use the
[unrecognized source template](.github/ISSUE_TEMPLATE/unrecognized-source.md). It asks
for a schema fingerprint, not your data.

## Non-goals

- **Not a memory layer.** `memory-bridge` and friends remember *conclusions*. This keeps
  the *transcript* — the 40 tool calls that produced the conclusion. Different artifact.
- **Not bidirectional.** There is no write-back into Claude or Cursor. That's the whole
  reason it's safe.
- **Not a cloud service.** The data plane is yours: a folder, a git remote, or a machine
  you own. The control plane is disposable by design.

## Install

```bash
pipx install batonpass-cli             # the command it gives you is `batonpass`
uvx --from batonpass-cli batonpass discover   # or run it without installing
pip install "batonpass-cli[zstd]"      # zstandard buys the 2.5x storage ratio
```

`uvx` needs `--from` because it looks for an executable with the package's name,
and this package's executable is `batonpass`, not `batonpass-cli`.

The package is `batonpass-cli` on PyPI, because the plain name `batonpass` is
already spoken for — an unrelated project called `baton-pass` normalizes to the
same string, and PyPI compares names with the separators removed. The command
it installs is still `batonpass`.

Python 3.9+. Zero required dependencies — `zstandard` is the only extra, and it
buys the 2.5× storage ratio. Without it, libraries written with zstd still
refuse gracefully and tell you the one command to run.

## License

MIT. See [`Acknowledgments`](#acknowledgments).

### Acknowledgments

The design learned from two prior projects — **no source code was copied from either**:

- [`jiabaobei/memory-bridge`](https://github.com/jiabaobei/memory-bridge) — per-device own-file
  heartbeat, write-once wiring, content-freeze, handover cards with staleness windows, and
  the one-line session index that made "which file was it" searchable again.
- [`Nwflower/dsh-chat-import`](https://github.com/Nwflower/dsh-chat-import) — the Interchange
  IR, named degradation policies (`lossless | text-fallback | skip-placeholder`), multi-frame
  zstd decoding, and the `PRAGMA table_info` adaptivity that lets a reader survive schema drift.

Both solve a real slice of this problem. Neither ships a machine-to-machine handoff.
That gap is what batonpass is for.
