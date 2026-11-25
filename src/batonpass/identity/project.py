"""Machine-independent project identity.

The problem this solves, in one line of field data:

    Mac mini   /Users/lcpkof/Documents/xueji   ->  49 sessions
    laptop     /home/luo/Documents/xueji       ->  the same 49 sessions
    nobody      .zcode/workspace/default       ->  58 sessions of nothing

If identity is the path, those three are three different projects forever, and
"where is that spreadsheet I made" stays unanswerable across machines. So a
project is not a path — it is the *logical tail* of the path, with everything
machine-specific and everything disposable thrown away first.

Three tiers, best evidence first. A tier is stored with the key so the reader
can tell a git remote from a guess, and no tier is allowed to silently
impersonate another.
"""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath

TIER_GIT = 1
TIER_PATH = 2
TIER_CANDIDATE = 3

CONFIDENCE = {TIER_GIT: "high", TIER_PATH: "medium", TIER_CANDIDATE: "low"}

UNKNOWN = ""  # no project — sessions still searchable, just not groupable

# Home directories differ between machines, and so do the names inside them.
_HOME_PREFIX = re.compile(r"^(users|home|volumes|root|mnt|c)/", re.I)
_DRIVE = re.compile(r"^[a-z]:/", re.I)

# Directories that are disposable by construction. A task that lived in one of
# these was a run, not a project: `draft-42f3f9e1-...` is the same as `tmp`.
_NOISE_SEG = re.compile(
    r"""^(
        \..*|                      # a tool's own home, never a project
        default|workspace|tmp|temp|output|outputs|out|dist|build|target|
        node_modules|__pycache__|\.git|\.venv|venv|env|
        draft[-\_]?[0-9a-f]{6,}[-\w]*|
        [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|
        .*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|
        [0-9]{8}[-\_]?[0-9]{4,6}|
        [0-9]{4}-[0-9]{2}-[0-9]{2}([tT ][0-9:]+)?[-\w]*|
        session[-\_]?\w*
    )$""",
    re.I | re.X,
)

# Sub-directories that belong to *some* project rather than being one. Without
# this, `xueji` and `xueji/frontend` are two projects and the 8 directories you
# actually work in become 20 clusters.
_GENERIC_TAIL = {
    "src", "lib", "app", "apps", "web", "www", "site", "public", "static",
    "assets", "docs", "doc", "scripts", "script", "test", "tests", "spec",
    "frontend", "backend", "server", "client", "api", "core", "main",
    "packages", "components", "pages", "views", "config", "tools",
}

# `~/Documents/xueji` on one machine is `~/projects/xueji` on the next. What
# they share is not the folder — it is the fact that the folder is *just a
# container*. Canonicalising it to `~` merges the two and keeps the leaf name,
# which is the only part a human ever says out loud.
_CONTAINER = {
    "documents", "document", "desktop", "downloads", "projects", "project",
    "work", "works", "workspace2", "code", "codes", "src", "source", "repos",
    "repo", "git", "github", "gitlab", "gitee", "dev", "develop", "sites",
    "apps", "projects2", "files", "user", "home",
}

_REMOTE_URL = re.compile(r"^\s*url\s*=\s*(.+?)\s*$", re.M)
_SCP_LIKE = re.compile(r"^[\w.-]+@([\w.-]+):(?!//)(.+)$")


def norm_path(path: str) -> str:
    """Forward slashes, no trailing slash, no duplicate separators."""
    p = (path or "").strip().replace("\\", "/")
    p = re.sub(r"/{2,}", "/", p)
    return p.rstrip("/")


def drop_machine_prefix(parts: list) -> list:
    """Remove the part of a path that only makes sense on one machine."""
    if parts and _DRIVE.match(parts[0] + "/"):
        parts = parts[1:]
    if parts and parts[0].lower() in ("users", "home", "volumes", "root", "mnt"):
        parts = parts[1:]
        # the account name itself
        if parts and not _NOISE_SEG.match(parts[0]):
            parts = parts[1:]
    if parts and parts[0].lower() == "volumes":
        parts = parts[1:]
        if parts:
            parts = parts[1:]
    return parts


def logical_parts(cwd: str, home: str = None) -> list:
    p = norm_path(cwd)
    if not p:
        return []
    home = norm_path(home if home is not None else os.path.expanduser("~"))
    if home and p.lower().startswith(home.lower() + "/"):
        p = p[len(home) + 1:]
    parts = [s for s in p.split("/") if s]
    # Only strip a user/home prefix when the path actually started at a root.
    if cwd.replace("\\", "/").startswith("/") or _DRIVE.match(norm_path(cwd)):
        parts = drop_machine_prefix(parts)
    else:
        parts = drop_machine_prefix(parts)
    return parts


def meaningful(parts: list) -> list:
    """Drop noise everywhere, generic tails only at the end."""
    kept = [s for s in parts if not _NOISE_SEG.match(s)]
    while len(kept) > 1 and kept[-1].lower() in _GENERIC_TAIL:
        kept.pop()
    return kept


def key_from_path(cwd: str, home: str = None) -> tuple:
    """Tier 2. Returns (key, tier, confidence) — key '' when there is no project."""
    parts = meaningful(logical_parts(cwd, home))
    if not parts:
        return UNKNOWN, TIER_PATH, CONFIDENCE[TIER_PATH]
    # Last two segments: deep enough to disambiguate `xueji` from `frontend`,
    # shallow enough that a re-cloned copy still matches. A container parent
    # collapses to `~`, so `/Users/a/Documents/xueji`, `/home/b/projects/xueji`
    # and `C:/Users/c/Desktop/xueji` are one project.
    if len(parts) == 1:
        if parts[0].lower() in _CONTAINER:
            return UNKNOWN, TIER_PATH, CONFIDENCE[TIER_PATH]
        tail = parts[0].lower()
    else:
        parent, leaf = parts[-2].lower(), parts[-1].lower()
        tail = ("~/" + leaf) if parent in _CONTAINER else (parent + "/" + leaf)
    if len(tail) < 2:
        return UNKNOWN, TIER_PATH, CONFIDENCE[TIER_PATH]
    return tail, TIER_PATH, CONFIDENCE[TIER_PATH]


def remote_to_key(url: str) -> str:
    """`git@github.com:Lcpkof/xueji.git` -> `github.com/lcpkof/xueji`."""
    u = (url or "").strip()
    if not u:
        return ""
    m = _SCP_LIKE.match(u)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        u = re.sub(r"^[a-z]+://", "", u, flags=re.I)
        u = u.split("?")[0].split("#")[0]
        if "@" in u.split("/")[0]:
            u = u.split("@", 1)[1]
        host, _, path = u.partition("/")
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    host = host.split(":")[0]
    if not host or not path:
        return ""
    return ("%s/%s" % (host, path)).lower()


def read_git_remote(path: str) -> str:
    """Tier 1, only when the directory is on *this* machine.

    A git remote is the one identifier that is genuinely the same string on
    every machine, which is why it outranks the path. We read `.git/config`
    directly instead of shelling out — no subprocess, no network, no surprise.
    """
    base = norm_path(path)
    if not base or not os.path.isdir(base):
        return ""
    for candidate in (os.path.join(base, ".git", "config"),
                      os.path.join(base, "config")):
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read(20000)
        except OSError:
            continue
        urls = _REMOTE_URL.findall(text)
        for u in urls:
            k = remote_to_key(u)
            if k:
                return k
    return ""


def title_candidate(title: str) -> str:
    """Tier 3: a *candidate*, never merged into a real cluster.

    Marked with a leading `?` so `batonpass tasks` can show it as a guess and
    nothing downstream can mistake it for a path-derived key.
    """
    t = (title or "").strip()
    if len(t) < 4:
        return UNKNOWN
    t = re.sub(r"^(this is an automatically generated .*?|帮我|请|麻烦)\s*", "", t, flags=re.I)
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "-", t.lower()).strip("-")
    slug = "-".join([s for s in t.split("-") if s][:6])[:40]
    return "?" + slug if len(slug) >= 3 else UNKNOWN


def derive(cwd: str, title: str = "", home: str = None, guess: bool = False) -> tuple:
    """Best available identity for one session: (key, tier, confidence).

    `guess=False` by default, and that is a deliberate choice with a measured
    reason: 409 sessions here have no working directory at all, and their
    titles are sentences ("这道题我做错了，帮我讲讲"). Turning every sentence into
    a project produced 400 clusters of one — noise pretending to be structure.
    Ungrouped sessions are still fully searchable; they are just not lied about.
    """
    if cwd:
        remote = read_git_remote(cwd)
        rk = remote_to_key(remote) if remote else ""
        if rk:
            return rk, TIER_GIT, CONFIDENCE[TIER_GIT]
        key, tier, conf = key_from_path(cwd, home)
        if key:
            return key, tier, conf
    cand = title_candidate(title) if guess else UNKNOWN
    if cand:
        return cand, TIER_CANDIDATE, CONFIDENCE[TIER_CANDIDATE]
    return UNKNOWN, TIER_CANDIDATE, "none"


def is_candidate(key: str) -> bool:
    return bool(key) and key.startswith("?")


def display(key: str) -> str:
    """Human label for a key — the candidate marker is noise in a table."""
    if not key:
        return "(no project)"
    if key.startswith("?"):
        return key[1:]
    return key[2:] if key.startswith("~/") else key
