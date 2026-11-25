"""What did this task actually produce?

A transcript is mostly talk. The one thing you want when you come back to a
task a week later is the file it left behind — and that file is almost never
mentioned in the first message. Measured on a real session: 79 filename
candidates, the first six were `SOUL.md / AGENTS.md / bootstrap.md`. Take the
*last* mention, not the first, and filter out the files every project has.

Two rules, and both are needed:

  whitelist  a deliverable has a deliverable extension — .xlsx .docx .pdf .zip
  blacklist  the eighteen files that exist in every repo are never "the output"

Skipping either one produces a card pointing at `README.md`, which is worse
than a card pointing at nothing, because it looks like an answer.
"""

from __future__ import annotations

import json
import re

DELIVERABLE_EXT = {
    ".xlsx", ".xlsm", ".xls", ".csv", ".numbers",
    ".docx", ".doc", ".pages", ".pdf",
    ".pptx", ".ppt", ".key",
    ".zip", ".tar", ".gz",
    ".md", ".html", ".ipynb",
    ".png", ".jpg", ".jpeg", ".svg",
}

# The files that exist in every repository. They are not wrong, they are just
# never the answer to "where is my work".
NOISE_NAMES = {
    "readme.md", "changelog.md", "license", "license.md", "license.txt",
    "skill.md", "agents.md", "soul.md", "memory.md", "user.md", "claude.md",
    "agents", "contributing.md", "code_of_conduct.md", "security.md",
    "requirements.txt", "pyproject.toml", "package.json", "package-lock.json",
    "tsconfig.json", "mcp.json", "settings.json", ".gitignore", "makefile",
    "dockerfile", "docker-compose.yml", "index.html", "index.js", "index.ts",
    "favicon.ico", "robots.txt", "setup.py", "manifest.json",
}

# Tool arguments arrive as JSON containing JSON, so a "path" can be three
# levels down. These are the keys that hold a destination, across the naming
# conventions of the tools actually on this machine.
PATH_KEYS = {
    "path", "file_path", "filepath", "target_file", "filename", "file",
    "output", "output_path", "save_as", "destination", "dest", "to",
}

NAME_RE = re.compile(
    r"""(?<![\w.\-\\])                    # not mid-word, not mid-extension
        (                                     # a name starts and ends on a word
         [\w\u4e00-\u9fff]                # character — "existing .xlsx" is
         (?:[\w\u4e00-\u9fff .\-_]{0,68}  # prose, "报告 2026.xlsx" is not
            [\w\u4e00-\u9fff\-_])?
         \.(?:xlsx|xlsm|xls|csv|numbers|docx|doc|pages|pdf|pptx|ppt|key|
              zip|tar|gz|md|html|ipynb|png|jpe?g|svg))
        (?![\w])""",
    re.I | re.X,
)

# Not all deliverables are equal. A spreadsheet is the thing you were making;
# a markdown note is usually the thing you were reading. Without this, every
# card's top line is `index.md`.
EXT_RANK = {
    ".xlsx": 3, ".xlsm": 3, ".xls": 3, ".numbers": 3, ".docx": 3, ".doc": 3,
    ".pages": 3, ".pdf": 3, ".pptx": 3, ".ppt": 3, ".key": 3, ".zip": 3,
    ".tar": 3, ".gz": 3, ".csv": 2, ".html": 2, ".ipynb": 2, ".png": 2,
    ".jpg": 2, ".jpeg": 2, ".svg": 2, ".md": 1,
}


def rank(name: str) -> int:
    low = (name or "").lower()
    return EXT_RANK.get("." + low.rsplit(".", 1)[-1] if "." in low else "", 0)


# Chinese prose glues itself to the filename: "产出 xueji_岗位表_2026.xlsx".
# The space is the only seam there is, so drop the words that are never part of
# a name and keep the token that is.
STOP_TOKENS = {
    "产出", "见", "和", "与", "的", "在",
    "保存到", "保存", "生成", "导出", "新建", "修改", "打开", "读取", "写入",
    "输出", "附件", "下载", "上传", "已", "完成", "更新", "查看", "here", "file",
    "the", "see", "and", "to", "into", "from", "for", "with", "saved", "saving",
    "做了", "做到了", "存到", "保存为", "写入到", "文件是", "这里是",
    "且", "并且", "还有", "另见", "详见", "参见", "参考", "注意", "继续读",
    "save", "as", "new", "existing", "or", "and", "then", "with", "a", "an",
    "creating", "editing", "restyling", "generating", "writing", "reading",
    "saving", "uploading", "downloading", "fixing", "fix", "update", "updating",
    "opening", "open", "exporting", "convert", "converting", "legacy", "files",
}

# Bumped whenever the extractor learns something. A rescan is cheap (713
# sessions in ~6 s) while a stale ref list is permanent — merging only ever
# adds, so an improved extractor would otherwise never be able to *remove* the
# `README.md` it used to believe in.
ARTIFACT_VER = "5"

MAX_REFS = 24
MAX_DEPTH = 6


def _ok_name(name: str) -> bool:
    base = _strip_stopwords((name or "").strip())
    base = base.strip("._-")
    if not base or "/" in base or "\\" in base:
        return False
    low = base.lower()
    if low in NOISE_NAMES:
        return False
    ext = "." + low.rsplit(".", 1)[-1] if "." in low else ""
    if ext not in DELIVERABLE_EXT:
        return False
    # `streamlit.components.v1.html` is a module path, not a file you can open.
    if base.count(".") > 2:
        return False
    # `state.json trades.csv` is two filenames separated by a space, and the
    # space is allowed *inside* a name (`报告 2026.xlsx`), so the tell is the
    # text that follows a complete extension: a real name never resumes after
    # its own extension.
    if re.search(r"\.[A-Za-z0-9]{1,5}\s+\S", base):
        return False
    # `1.png` / `a.md` are counters and headings, not deliverables.
    if len(base.rsplit(".", 1)[0]) < 2:
        return False
    return True


# Words that join two names. They are separators, not part of a name, and the
# two sides are two deliverables: "岗位表 2026.xlsx 和 汇总表 2026.xlsx" is a pair,
# not a file called "岗位表 2026.xlsx 汇总表 2026.xlsx".
_REL_SPLIT = re.compile(
    r"\s+(?:至|到|和|与|及|或|以及|然后|to|and|or|through|until|then|into)\s+", re.I)

_CJK_VERB = re.compile(
    r"^(看|请|帮|把|给|要|想|能|可|让|叫|试|先|再|并|和|与|或|至|到|于|从|在|用|对|为|是|有|没|未|已|加|改|写|读|删|传|存|导|做|放|发|跑|禁止|不要|请勿|别|只|仅|继续|继续读|注意|且|并且|还有|另见|详见|参见|参考|重读|补|补充)"
)


def _strip_stopwords(name: str) -> str:
    """Cut the Chinese sentence that grew around the filename.

    "看能否上传 cover.png" and "加 card-1 至 card-5.png" are sentences, not files.
    Only the tokens that are provably not part of a name are removed — a real
    Chinese stem like `岗位表 2026.xlsx` keeps `岗位表`.
    """
    if " " not in name:
        return name
    parts = [p for p in name.split(" ") if p]
    kept = []
    for p in parts:
        if p in STOP_TOKENS:
            continue
        if re.search(r"[A-Za-z]", p):
            kept.append(p)
            continue
        if p.isdigit() and "." not in p:
            continue          # a row id, a price, a date fragment
        if _CJK_VERB.match(p):
            continue
        kept.append(p)
    return " ".join(kept) if kept else name


def names_in_text(text: str) -> list:
    """Every filename in a blob of prose, in order, deduplicated.

    `[2026-11-18.md](/Users/a/n/2026-11-18.md)` names the same file twice; the
    label and the target are one mention, not two.
    """
    out, seen = [], set()
    for m in NAME_RE.finditer(text or ""):
        for piece in _REL_SPLIT.split(m.group(1).strip()):
            name = _strip_stopwords(piece.strip())
            if _ok_name(name) and name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _deep_paths(obj, out, depth=0):
    if depth > MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                if str(k).lower() in PATH_KEYS and _ok_name(v.rsplit("/", 1)[-1]):
                    out.append(v.strip())
            else:
                _deep_paths(v, out, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _deep_paths(v, out, depth + 1)
    elif isinstance(obj, str):
        s = obj.strip()
        if s[:1] in "{[":
            try:
                _deep_paths(json.loads(s), out, depth + 1)
            except (ValueError, TypeError):
                pass


def paths_in_args(raw: str) -> list:
    out = []
    _deep_paths(raw if isinstance(raw, str) else str(raw), out)
    return out


def _basename(p: str) -> str:
    p = (p or "").replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[-1] if p else ""


def scan_session(conn, codecs, sid: str, source: str, limit_rows: int = 4000) -> list:
    """Every deliverable this session mentions, with the last time it appeared.

    Reads message prose, text parts and file-tool arguments — not tool results.
    A directory listing mentions hundreds of files that are not the output;
    `write({"path": "..."})` mentions exactly one.
    """
    seen = {}
    order = 0

    rows = conn.execute(
        "SELECT seq, role, text_sha, ts FROM message WHERE sid=? ORDER BY seq LIMIT ?",
        (sid, limit_rows),
    ).fetchall()
    for r in rows:
        order += 1
        text = codecs.get(r["text_sha"], source)
        if text:
            for name in names_in_text(text):
                seen[name] = {"name": name, "ts": r["ts"], "via": "text", "order": order}

    prows = conn.execute(
        """SELECT p.kind, p.name, p.args_sha, p.result_sha, m.ts
             FROM part p JOIN message m ON m.mid=p.mid
            WHERE m.sid=? ORDER BY m.seq, p.seq LIMIT ?""",
        (sid, limit_rows * 4),
    ).fetchall()
    for r in prows:
        order += 1
        ts = r["ts"]
        kind = (r["kind"] or "").lower()
        if kind in ("text", "tool_call", "file"):
            body = codecs.get(r["args_sha"], source) if kind == "tool_call" else codecs.get(r["result_sha"], source)
            if not body:
                body = codecs.get(r["result_sha"], source) or codecs.get(r["args_sha"], source)
            if kind == "tool_call":
                for path in paths_in_args(body):
                    name = _basename(path)
                    if _ok_name(name):
                        seen[name] = {"name": name, "path": path, "ts": ts,
                                      "via": "tool", "order": order}
                continue
            for name in names_in_text(body):
                seen[name] = {"name": name, "ts": ts, "via": "text", "order": order}

    # A path taken from `write({"path": ...})` outranks a filename spotted in
    # prose: one of them the tool actually touched.
    refs = sorted(seen.values(), key=lambda d: (rank(d["name"]), d["via"] == "tool", d["order"]))
    # Newest first, and the cap keeps a card readable: 24 files is already more
    # than anyone reads on a phone.
    refs = refs[-MAX_REFS:]
    refs.reverse()
    for r in refs:
        r.pop("order", None)
    return refs


def merge_refs(existing_json: str, refs: list) -> str:
    """Union by basename, newest wins. Used when a task spans sessions."""
    merged = {}
    if existing_json:
        try:
            for r in json.loads(existing_json):
                if r.get("name"):
                    merged[r["name"]] = r
        except (ValueError, TypeError):
            pass
    for r in refs:
        name = r.get("name")
        if not name:
            continue
        old = merged.get(name)
        if old is None or str(r.get("ts") or "") >= str(old.get("ts") or ""):
            merged[name] = r
    out = sorted(merged.values(),
                 key=lambda d: (rank(d.get("name")), str(d.get("ts") or "")), reverse=True)
    return json.dumps(out[:MAX_REFS], ensure_ascii=False)
