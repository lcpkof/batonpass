# batonpass

> **Never drop the baton.**
> Switch machines mid-task — your work picks up exactly where you left it.

You are 3 hours into a task on the Mac mini. You open the laptop. Nothing is there.

Not the transcript, not the tool output that told you which endpoint 404'd, not the reason
you rejected the third approach. You remember the *concept* — "the岗位表 thing, I fixed the
join" — and you spend 20 minutes re-deriving what you already knew.

`batonpass` is the thing that hands the baton over: **from one machine to the next, from
last-week-you to today-you**, without the work touching the ground.

```
batonpass discover              # what AI history is on this machine?  (read-only, 2 s)
batonpass archive               # pull it into a portable library  (~1 min per GB)
batonpass export <session> --text   # read one conversation back out
batonpass find "岗位表" --at 7d      # search it, with time windows   (next)
batonpass resume                # handover packet for a fresh machine (next)
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

**Read-only in · you own the copy out.**

---

## Start here: `batonpass discover`

Before you trust a tool with your history, look at what's actually there.

```bash
uvx batonpass discover --report ~/Desktop/batonpass-report.html
```

One file, standard library only, ~2 seconds, no network, no writes. It produces a
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

## Status

| verb | state |
|---|---|
| `discover` | **shipped** — read-only inventory, ~2 s, one HTML page |
| `archive` | **shipped** — five adapters, replay-safe (re-run adds 0 rows) |
| `find` · `resume` · `sync` | designed in [`docs/design-v1.md`](docs/design-v1.md), landing next |

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
uvx batonpass discover        # no install, runs from a temp env
pipx run batonpass discover
pip install batonpass         # or install it
```

Python 3.9+. Zero dependencies.

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
