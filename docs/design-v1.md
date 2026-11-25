# batonpass · 技术方案 v1

> **只借设计，不借代码。** 借鉴别人的**架构判断**（他们踩过的坑），实现全部自己写。
> 代码库不引入第三方源码，`Acknowledgments` 里点名致谢。
>
> 写于 2026-11-25 · 对应需求：任意机器 / 任意 AI 工具 / 任意任务，换台机器能无缝接上，所有历史变成随处可查的记忆。

---

## 0. 一句话

> **Never drop the baton.**
> 换机器、换工具、换场景，活儿接着干，一点不丢。

**别人做的是记忆助手，我们做的是档案馆 + 交接棒。**
他们存"结论"，我们存"原话 + 结论"，而且换机器时只先搬目录，要哪段再拉哪段。

交付物是一个命令行工具 `batonpass` + 一个 MCP server，跑在你自己的机器上，数据不出门。

**为什么叫 batonpass**：接力赛里唯一不能发生的事就是掉棒。这个项目要解决的正是"交接的那一秒"——把任务从一台机器递到另一台机器、从上周的你递到今天的你，中间不能有信息落地。

---

## 1. 名字与命名空间（已查重，2026-11-25，三处全空）

| 位置 | 名字 | 状态 |
|---|---|---|
| GitHub repo | `batonpass` | ✅ 空 |
| PyPI 包名 | `batonpass` | ✅ 空 |
| npm 包名 | `batonpass` | ✅ 空 |
| 命令行 | `batonpass` | 自定 |
| Python import | `batonpass` | 本地包，不冲突 |
| 中文名 | 接力棒 | — |

三处都空，包名和命令名一致，`pip install batonpass` 装到的就是它，不会有歧义。即便如此，**发布前第一件事仍然是把三个名字都占下来**——`membridge` 就是名字没占，用户 `pip install membridge` 装到了别人的项目上。

对标单词查过被占的：`baton` / `weft` / `cairn` / `portage` / `carryover` / `handover` / `switchboard`，全部在 PyPI 或 npm 上有主，所以走复合词。

域名/社媒不用管，这个项目靠 GitHub 传播。

---

## 2. 三条铁律（先写死，后面所有设计都从这来）

**铁律一：源永远是别人的，我们只读。**
不写回 zcode / dsh / Cursor / Claude Code 的任何数据文件。导入错了顶多是一份副本，写回错了动的是用户的原始记录。
→ 由此推导：**本项目没有任何双向同步功能**，永远不会加。

**铁律二：只追加，不改写。**
所有记录一次写入永不修改，需要"改"就再追加一条事件，当前值靠推导。
→ 由此推导：两台机器同时干活也不会打架，因为根本没有"同一行被两边改"这回事。

**铁律三：换机器不该需要重新理解。**
搬过去的必须是"能自己解释自己"的东西：任务名、产出文件名、日期、原话。不是一堆 uuid 和路径。
→ 由此推导：所有同步数据都带自描述字段 + 摘要层独立可读。

---

## 3. 全景

```
      ┌──────────────────── 你的机器 A ────────────────────┐
      │                                                    │
  zcode.db ─┐                                              │
 billion.db ─┤                                              │
   dsh.db ───┼→ ①discover ─→ ②archive ─→ ③identity ─┐       │
  alma.db ───┤   (探测+适配)   (内容寻址+压缩)  (任务身份) │       │
  pinky.db ──┤        │                                  │       │
 .md/.jsonl ┘        └──→ 降级报告（哪段丢了、为什么）    │       │
                                                          ▼       │
                                                    ④sync ───────┼──→ 通道（git / 网盘 / U 盘 / tailnet）
                                                          ▲       │      ↑ 单向：只写自己的 outbox/<设备>/
                                                          │       │      ↓ 只读别人的 outbox
                                                     ⑤recall ←─────┘
                                                   (FTS5 + 可选向量 + RRF)
                                                          │
                                              ┌───────────┴───────────┐
                                              ▼                       ▼
                                          CLI 查询              MCP server
                                        `batonpass find`     (Claude/Codex/Alma)
```

五个模块，一个命令一个动作：`discover` / `archive` / `sync` / `recall` / `handoff`。

---

## 4. 五个模块

### 4.1 discover —— 认形状，不认路径

不靠"我知道 zcode 在 `~/.zcode/cli/db/db.sqlite`"，而靠**看文件长什么样**。因为你换了工具版本、换了系统、换了安装位置，路径就变了。

**三级探测**（顺序固定，前一档成功就不走下一档）：

| 级别 | 手段 | 成本 | 结果 |
|---|---|---|---|
| L0 | 已知指纹表：约 15 条 `(表名集合, 关键列名)` 的哈希 | 读 1 页 schema | 命中即确定适配器，置信度 `high` |
| L1 | 列集合 Jaccard 相似度 > 0.75，任一已知适配器 | 读 1 页 schema | `medium`，走对应适配器但标记"未经确认" |
| L2 | 内容启发：JSON 里有 `messages[]` / `turns[]` / `chat_messages` / `role+content` 模式 | 读前 64 KB | `low`，先原文入库（`parser=none`），等适配器更新后重放 |
| L3 | 什么都不是 | — | **不静默跳过**。写进报告的 `unrecognized[]`，附路径、大小、前 200 字节摘要 |

每一档都记 `detectedBy`（`explicit` / `marker` / `path-order` / `fingerprint`），报告里要能看出"我是怎么认出来的"。

**失败要大声。** 每个候选源都要留下失败原因（`no table / schema mismatch / permission denied / locked / truncated frame`），不能只报第一个错误就退出。用户看不到失败，就会以为数据进来了。

**降级必须有名有姓。** 不是"部分内容丢失"，而是明确的事件：

```
tool-result-missing        工具结果只存了引用，原文没落盘
tool-result-text-fallback  只有文本摘要，结构丢了
reasoning-encrypted        推理过程是加密的，拿不到
cwd-missing                找不到工作目录，任务归并只能靠标题
branch-collapsed           fork 分支被压平成一条线
attachment-skipped         附件是 base64/外链，未复制（默认按引用）
compaction-shadowed        这段被压缩覆盖了，后面有更新版本
locked                     库被占用，本轮跳过（下次重试）
```

每条降级都有 `count`，报告第一屏就显示"这次有 3 类降级共 1,204 条"。**默认策略 `text-fallback`，`--strict` 时改为 `skip-placeholder`**（宁可缺一条，也不写占位符污染搜索结果）。

**适配器是数据，不是代码。** `adapters/*.yaml` 描述表名、列映射、时间字段、消息行/角色判断规则、产物文件识别；配套一个 `fixtures/<源>.db` 小样本。加新工具 = 加一个 yaml + 一个 fixture，不需要改核心。社区能贡献。

### 4.2 archive —— 存全量，传分级

**内容寻址**：每条文本 `sha256` 做 key，重复内容只存一份。你的库里已经有天然重复（billion 的 `refcount` 平均 1.219、最高 277，约 18% 是重复的），这条路能白捡 18%。

**压缩**（实测于你的真实数据，不是估算）：

| 数据 | 原始 | gzip-6 | zstd-19 | 倍数 |
|---|---|---|---|---|
| `message.data` | 5.43 MB | 0.80 MB | **0.28 MB** | 19.7× |
| `part.data` | 20.48 MB | 7.30 MB | **4.97 MB** | 3.1×（含 base64） |

再叠一层**训练字典**（112,640 字节，从你自己的样本训练）：单条记录再省 **34%**。因为你的消息都是同一种 JSON 壳子，字典学得快。

剥壳：把 JSON 外皮去掉只留 `text/content/thinking/result/output`，白省 7%。

**媒体默认按引用，不复制。** 图片/附件只记 `路径 + sha256 + 大小`，要搬的时候 `--media=copy`。这一条决定了归档库能小一个数量级。

**原始层永远保留。** 每条源数据先原文入库（`raw_record.payload`），规范层是从原始层解析出来的。解析器升级后 `batonpass replay` 重跑规范层，**不用重新读源文件**。这是唯一能在一年后修 bug 的设计。

### 4.3 identity —— 机器无关的任务身份

问题现场：同一件事，Mac mini 上是 `proj_c-users-lcpkof-documents-xueji`，笔记本上是 `proj_home-lcpkof-documents-xueji`。同一个项目，两个 ID，永远认不出来。

三级归一化 `project_key(path, git_remote, title)`：

| 级别 | 规则 | 例子 | 置信度 |
|---|---|---|---|
| 1 | git remote URL 归一（去协议、去 `.git`、小写） | `github.com/lcpkof/xueji` | `high` |
| 2 | 目录逻辑名：去 home 前缀、去盘符、去 UUID/时间戳/隐藏目录，**容器目录归一成 `~`**，取末两段 | `~/xueji` | `medium` |
| 3 | 标题 + 向量聚类（拿不到路径时的兜底） | `xueji` 聚类 | `low` |

UUID / 时间戳命名的目录**不算项目**（`workspace/draft-42f3f9e1…` 这种是临时运行目录）。以 `.` 开头的目录一律不算项目——那是工具自己的家（`~/.zcode/workspace/default` 在这台机器上有 58 个会话，它是运行目录，不是项目）。

容器归一化是"同一条铁律"的第二种形态：`/Users/a/Documents/xueji`、`/home/b/projects/xueji`、`C:/Users/c/Desktop/xueji` 三者都是 `~/xueji`。`documents` `projects` `code` `repos` `desktop` 这类目录名只说明它被放在哪，不说明它是什么；把它们换成同一个 `~`，换机器就自动对上了，叶子名（人嘴里说的那个词）一个字不动。

**外来身份不被本地推翻**：一条会话从通道进来时，已经带着拥有它的那台机器算出的 `project_key`（t0 的 `pk`）。本地重建时如果从 cwd 推不出东西（对端的磁盘路径我们根本没有），这个外来答案**保留**，不会被空值覆盖——否则"换机器继续"恰好会丢掉它最该保住的那层分组。反过来，键是我们自己算的、现在推不出来了，就置空（未分组），因为那是我们的错，不该留着一个错的。`identity` 会把保留数量单独报出来（`kept N project key(s) that arrived from another machine`）。

**标题兜底默认关闭**（`guess=False`）。实测证据：这台机器 409 个会话没有工作目录，标题是句子——"这道题我做错了，帮我讲讲"。全部提升成项目，得到 400 个各含 1 个会话的簇，是发明的结构不是发现的结构。这些会话仍然可检索，只是不假装有项目。

四级召回 + RRF 融合：**① 路径精确 ② project_key ③ 标题字面 ④ 向量语义**。你在另一台机器上说"那个岗位表"，走的是 ③+④。

**Task Card**：跨机器的一个任务，渲染成一张卡：

```
任务  xueji 岗位表
状态  进行中（10-28 由 Mac mini 更新）
产出  xueji_岗位表_2026.xlsx  ← 最后一次出现在 10-28 22:14
做过  爬了 3 个招聘站 / 合并去重 / 导出 xlsx
卡住  第 4 站有反爬，未解
下一步 补上第 4 站，再跑一次合并
原话  11-04 你说"先把能拿的拿到" → 会话 zcode:c4a1…
```

**产物文件名从"最后一次出现"取，不是第一次。** 实测：11-04 那个会话里有 79 个文件名候选，取前 6 个只拿到 `SOUL.md / AGENTS.md / bootstrap.md`，真正的岗位表被挤没了。再配一个噪声黑名单（`readme.md / changelog.md / skill.md / agents.md / license.md / requirements.txt / pyproject.toml / soul.md / memory.md / claude.md / mcp.json …`），只认有交付价值的扩展名（`.xlsx .xls .docx .doc .pdf .csv .pptx .zip .md`）。

文件名按**交付价值**排序，不按出现顺序：表格/文档/PPT/PDF/压缩包 3，csv/html/ipynb/图片 2，`.md` 1。理由是"表格是你当时在做的东西，markdown 笔记通常是你当时在读的东西"——没有这个排序，每张卡的产出第一行都是 `index.md`。工具调用里出现的（`via=tool`）排在散文里提到的前面。

**中文散文粘着文件名**是这个提取器最脏的一类输入，实测真实字符串：`看能否上传 cover.png`、`603446 于 trades.csv`、`做到了 07-01_错题.md`、`加 card-1 至 card-5.png`。修法不是加正则，是加语义：名字必须**以词字符开头和结尾**（`existing .xlsx` 是句子，`岗位表 2026.xlsx` 是文件），再去掉停用词表和中文动词前缀（`看/帮/把/生成/导出/保存到…`），纯数字丢掉，点分超过三段丢掉（`streamlit.components.v1.html` 是模块路径）。实测 713 个会话提出 6,479 个产物引用，覆盖 575 个会话。

**ARTIFACT_VER 是必须的**：提取逻辑只能越改越准，但合并只会做并集——没有版本闸门，一个改进过的提取器永远删不掉它上一版留下的 `README.md`。

### 4.4 sync —— 通道就是一个目录

不搞服务器。**通道 = 一个你能读写的目录**：git 私有仓库、坚果云/OneDrive 同步文件夹、U 盘、tailnet 上的一个共享路径。四种都用同一套协议。

**协议只有五条规则**，全部为了防止"两台机器同时写同一行"：

1. **每台机器只写自己的文件**：`stream/<device>/t0.jsonl`、`t1.jsonl`、`t2.jsonl`，JSON Lines，只追加。永远不碰别人的文件。
   → 零共享可变状态，零冲突。不需要租约，不需要锁，不需要 CRDT。并发的两台机器各自追加各自的行，读的人拿到的是集合并集，而并集没有"丢更新"这种状态。
2. **心跳也是自己的文件**：`devices/<device>.json`（设备名、平台、最后活跃、条数、版本）。`init` 时立刻注册一次。
3. **水位在本地，且分两层**：`sync_state(channel,key,value)`。`pull:<dev>:<tier>` 记读到第几个字节；`sent:<sid>` 记这个会话的元数据签名（`条数:最大seq:摘要版本`）；`sent_tier:<sid>` 记正文发到第几层。三个都必须分开：只记"发过"会让提高 tier 时正文永远发不出去，只记字节数会让删掉的通道永远补不回来。
4. **原子写**：先写 `<file>.tmp` 再 `os.replace`。网盘同步过程中读到半个文件的惨案就不会发生，扫目录时排除 `.tmp`。
5. **通道身份不等于路径**：`batonpass-channel.json` 里有 `id`（uuid）。水位以 `id` 为键。删掉或重建通道目录 = 换了一个通道，会自动全量重推。这条是实测踩出来的：按路径做键时，删掉通道后库会坚持"713 个会话都推过了"，然后推一个空目录。

> 这一条是从别人的翻车现场学的：他们一开始把"我同步到哪了"写进共享网盘，读-改-写，多端并发**必丢更新**。后来挪到本地 `netdisk.json` 才好。我们的水位从第一天就在本地库。

**payload 按明文 sha256 寻址**：`blobs/<sha[:2]>/<sha>`。同一条消息到两次和到一次，状态完全一样——这是幂等的全部机制。`content` 表的主键本来就是 sha256，所以落库时不需要重新计算、不需要比对。

**blob 用 zlib，不用带字典的 zstd**：那份训练字典是**每台机器自己**从本机数据里训出来的，用它压缩的流在对端解不开。zlib 两端都有、都是标准库，代价是压缩率低一点——换来的是"任何一台机器都能读任何一台机器"。

**`channel.json` 自描述**：schema 版本、通道 id、创建时间。首次接入自动采纳；不一致就明确警告，不静默猜。

**T0–T4 分层，中继全存，设备按需拉（v0.3 已实现 T0–T2）：**

| 层 | 内容 | 大小（真实 713 会话库） | 什么时候拉 |
|---|---|---|---|
| T0 | 目录：会话列表 + 设备 + 工作目录 | 356 KB | 永远，每次同步 |
| T1 | 摘要：每会话一行「第一句问的是什么 + 最近几轮 + 产出的文件名」 | 2.70 MB | 永远，每次同步 |
| T2 | 消息正文 + 工具调用（payload 走 blob） | 每任务百 KB 级 | 要接着干这个任务时 |
| T3 | 工具输出全量 / 原始附件 | 尚未拆出，暂在 T2 里 | 顺着消息点进去时 |

新机器到达 = 2.8 MB / 0.1 秒，然后立刻能"我记得有这件事，是在哪台机器、哪个会话"。只拉 t0 的 **356 KB** 实测就够：`tasks` 列出 12 个项目（含会话数、设备、⚠ 过期标记），`card xueji` 打开同一张交接卡，fork 关系也在——因为 t0 每行本来就带着 `pk` 和 `forked_from`。**产出文件名随 T1 走**（每会话最多 12 个，只留 `name/ts/via`）：不然新机器上的卡片能说出"你在做什么"，却说不出"你在改哪个文件"，那等于没说。要动手了再拉 T2。
**别做"全下载"或"啥都没有"两档**——那是别人现在的状态，也是要解决的那点。

> **append-only 的代价，以及它怎么自己收**：重推的摘要会在文件里留下旧行，读的人取最后一行。所以流文件在明显超过"活着的大小"时会被重写成**一条键一行**（后写的赢）：`sync push` 顺手做，`sync compact` 手动做。`heads/<device>.<tier>.json` 带 `gen` 计数，重写后 +1；读的人发现 `gen` 变了就把偏移重置为 0 重读——因为插入幂等，重读永远安全。实测：同一批 713 会话推三次 = 1005 KB / 2139 行 → 335 KB / 713 行，pull 出来的库一字不差。

> **为什么不是每次推完都重写**：重写意味着 fsync 整个文件。目标是不让文件无界增长，不是让它保持最小。三道闸：小于地板（t0 512 KB / t1 4 MB / t2 32 MB）不动；没比"活着的大小"大一倍不动；`--force` 想什么时候收就什么时候收。

### 4.5 recall —— 检索 + 交接

> v0.4/v0.5 已实现：`identity` `index` `find` `tasks` `card` `resume`。下面每条后面的数字都是在本机 713 会话库上跑出来的。

- **混合检索**：FTS5（trigram，中文友好）+ 可选向量，`RRF` 融合，K=60。
  - 已实现四路：标题 / 项目名 / 产物文件名 / 正文，各自 RRF 融合。每一路负责这句话的一半——"那个岗位表"可能只出现在正文里，可能只是文件名，也可能文件夹就叫这个名字。
  - 实测：58,508 条消息建索引 16.6 s，索引 164 MB，71 个会话里没有正文（只有工具调用）被跳过并计数。
  - **索引是本地派生物，不进通道**：对端用同一批不可变消息自己重建，所以它可以随便激进（每个词都切 trigram），不占同步预算。向量是可选增强，没有 embedding provider 也能用（Alma 现在就卡在这，HTTP 400）。
- **时间窗语法**：`at:7d` / `at:2026-09` / `at:2026-09-20` / `at:2026-09-01..2026-09-20`。
- **沉默契约**：没有高质量命中就明确返回空，附"我查了什么"。绝不编。
- **MCP 只暴露三个工具**（v0.5 已实现，`batonpass mcp`，stdio，无守护进程）：`memory_search(query, at, project, limit)` / `memory_add(text, project, kind)` / `resume(project, sessions)`。工具越少，模型越会用。`memory_add` 写的是 `task_event` 追加事件，会出现在交接卡的「备注」行——写了看不见，等于没写。
- **交接卡**：`kind=handover`，五行 `goal / done / failed / next / refs`。和 memory-bridge 的格式**兼容**（能读能写），这样两边的用户数据能互通。
- **过期工作台比没有工作台更危险**：handover 超过 7 天标 `stale`，不自动喂给模型。
- **换机自动回忆**：会话启动时读 `cwd` → `project_key` → 有命中就把任务卡塞进上下文。不用你问。
- **索引自己补**：`find` / `memory_search` 动手前先做一次增量检查（缺会话、落后、版本旧），需要就补。理由很直白：没建索引的库如果回答"没找到"，和真相长得一模一样。
- **体检**：`doctor` 只读，把失败翻译成一行能动手的话（Python / sqlite+trigram / zstd / 磁盘 / 库版本 / 身份新旧 / 索引落后 / 通道 schema 与设备版本）。

---

## 5. 数据模型

SQLite（WAL），单文件，可 VACUUM 后拷走。

```sql
-- ── 原始层：只追加，解析器的后悔药 ──────────────────────────
CREATE TABLE raw_source (
  source_sha    TEXT PRIMARY KEY,   -- sha256(源文件内容指纹)
  device        TEXT NOT NULL,
  adapter       TEXT NOT NULL,
  adapter_ver   TEXT NOT NULL,
  path          TEXT,               -- 采集时的路径，仅供人看
  bytes         INTEGER,
  first_seen    TEXT NOT NULL
);

CREATE TABLE raw_record (
  id            INTEGER PRIMARY KEY,
  source_sha    TEXT NOT NULL REFERENCES raw_source(source_sha),
  seq           INTEGER NOT NULL,   -- 源内序号
  kind          TEXT NOT NULL,      -- session|message|part|file|raw
  payload       BLOB NOT NULL,      -- zstd(-19, dict) 压缩的原始 JSON
  payload_sha   TEXT NOT NULL,      -- 明文 sha256，用于去重
  UNIQUE(source_sha, seq, kind)
);

-- ── 规范层：可以随时从原始层重建 ────────────────────────────
CREATE TABLE content (
  sha256        TEXT PRIMARY KEY,
  byte_len      INTEGER NOT NULL,
  refcount      INTEGER NOT NULL DEFAULT 1,
  body          BLOB NOT NULL       -- zstd(-19, dict)
);

CREATE TABLE session (
  sid           TEXT PRIMARY KEY,   -- uuid，跨设备唯一
  source        TEXT NOT NULL,      -- zcode|billion|dsh|alma|pinky|zhanlu|file
  source_sid    TEXT NOT NULL,      -- 源系统内的 id
  device        TEXT NOT NULL,
  project_key   TEXT,               -- 归一化任务身份
  project_key_tier INTEGER,         -- 1|2|3，置信度
  cwd           TEXT,
  title         TEXT,
  created_at    TEXT,
  updated_at    TEXT,
  msg_count     INTEGER DEFAULT 0,
  first_msg_at  TEXT,
  last_msg_at   TEXT,
  forked_from   TEXT,               -- 源内 fork 的父会话
  artifact_refs TEXT,               -- JSON: 产出的文件（文件名+路径+最后出现时间）
  adapter_ver   TEXT,
  content_hash  TEXT,               -- 整会话指纹，用于增量与幂等
  UNIQUE(source, source_sid, device)
);

CREATE TABLE message (
  mid           TEXT PRIMARY KEY,
  sid           TEXT NOT NULL REFERENCES session(sid),
  seq           INTEGER NOT NULL,
  role          TEXT NOT NULL,      -- user|assistant|system|tool
  text_sha      TEXT,               -- → content.sha256
  model         TEXT,
  ts            TEXT,
  meta_json     TEXT,
  UNIQUE(sid, seq)
);

CREATE TABLE part (
  pid           TEXT PRIMARY KEY,
  mid           TEXT NOT NULL REFERENCES message(mid),
  seq           INTEGER NOT NULL,
  kind          TEXT NOT NULL,      -- text|reasoning|tool_call|tool_result|image
  name          TEXT,               -- 工具名
  args_sha      TEXT,               -- → content
  result_sha    TEXT,               -- → content
  is_error      INTEGER DEFAULT 0,
  ts            TEXT,
  UNIQUE(mid, seq)
);

-- ── 索引 ───────────────────────────────────────────────
CREATE VIRTUAL TABLE session_fts USING fts5(
  title, artifacts, first_user_msg,
  content='', tokenize='trigram'
);
CREATE VIRTUAL TABLE message_fts USING fts5(
  text, content='', tokenize='trigram'
);

-- ── 任务（跨机器第一实体，机器只是属性）─────────────────
CREATE TABLE task (
  tid           TEXT PRIMARY KEY,
  project_key   TEXT NOT NULL,
  title         TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  UNIQUE(project_key, title)
);

CREATE TABLE task_event (            -- 状态是事件，不是字段
  eid           TEXT PRIMARY KEY,
  tid           TEXT NOT NULL REFERENCES task(tid),
  device        TEXT NOT NULL,
  lamport       INTEGER NOT NULL,
  kind          TEXT NOT NULL,      -- start|progress|blocked|done|converge|handover
  body          TEXT,
  refs_json     TEXT,
  created_at    TEXT NOT NULL
);

-- ── 同步账（全部本地，不放通道）────────────────────────
CREATE TABLE device (
  device        TEXT PRIMARY KEY,
  platform      TEXT,
  last_seen     TEXT,
  lamport       INTEGER DEFAULT 0,
  schema_fp     TEXT,               -- 12 位容器指纹
  counts_json   TEXT
);

CREATE TABLE source_cursor (         -- 每源每设备一个游标
  device        TEXT NOT NULL,
  source        TEXT NOT NULL,
  cursor_json   TEXT,               -- {last_ts, last_rowid, ...}
  wal_sig       TEXT,               -- ★ WAL 边车签名，见下
  adapter_ver   TEXT,
  updated_at    TEXT,
  PRIMARY KEY(device, source)
);

CREATE TABLE peer_watermark (        -- 我读到别人哪个 seq 了
  peer_device   TEXT NOT NULL,
  seq           INTEGER NOT NULL,
  PRIMARY KEY(peer_device)
);

CREATE TABLE degradation (           -- 降级台账
  id            INTEGER PRIMARY KEY,
  source        TEXT, session_sid TEXT,
  code          TEXT NOT NULL,      -- tool-result-missing / cwd-missing / ...
  detail        TEXT,
  seen_at       TEXT NOT NULL
);
```

**★ 游标必须带 WAL 边车签名。** WAL 模式下主库的 `mtime`/`size` 可能在你 checkpoint 之前一动不动，只盯 `mtime+size` 的游标会**静默漏掉**新写入。游标要同时记 `wal` 文件的 `size + mtime`，两者任一变化就重扫尾部。这是别人真踩过的坑。

**★ zstd 必须自己解多帧。** 有些工具（dsh 就是）每个事件 flush 一次，一个日志是**几千个 zstd 帧**拼起来的（实测一个 6.5 MB 压缩 / 33 MB 明文 = 1962 帧）。而标准库和很多库**只解第一帧，剩下的静默丢弃，连"被截断"都不报**。所以：用能循环解帧的库，或者自己读帧头循环。解完要校验总长度和预期一致，不一致就报 `truncated`。

---

## 6. 线上格式（信封）

```json
{
  "v": 1,
  "kind": "delta",
  "device": "mac-mini",
  "seq": 1287,
  "lamport": 91234,
  "schema_fp": "a1b2c3d4e5f6",
  "created_at": "2026-11-25T12:40:00+08:00",
  "sections": {
    "sessions":   [ ... ],
    "messages":   [ ... ],
    "parts":      [ ... ],
    "task_events":[ ... ],
    "degradations":[ ... ]
  },
  "blobs": [ {"sha256":"...", "codec":"zstd19+dict", "len":1234} ],
  "edges":    [ ... ],        // 旧字段，继续填，别删
  "edges_v2": [ ... ]         // 新字段，带 kind/evidence
}
```

**兼容规则（从第一天就写进代码，不是以后再说）：**

1. **每个 section 可选**，未知 key 一律忽略，不许崩。
2. **永不修改已发布的字段语义**；要换就加新字段（`edges` → `edges_v2` 的活教材）。
3. **`schema_fp` 是容器指纹**：12 位 SHA-256，来自 `PRAGMA table_info` 的实际列。
   收到包先 `reconcile()`：
   - 我方缺列 **且** 该列有登记的 migration → 自动 `ALTER TABLE`，继续。
   - 我方缺列 **且** 没登记 → **拒收这个包，并打印升级路径**（要升级到哪个版本）。绝不静默写入半条数据。
4. 旧版本遇到不认识的新字段：忽略 + 记一条 `unknown_field` 到日志，继续跑。

> 反面教材：他们的线上格式冻结时没留扩展位，后来想加 `kind`/`evidence` 只能偷偷塞进元组，老版本读新版本的数据会静默丢字段。我们不留这个坑。

---

## 7. 压缩与分层（决策表）

| 场景 | 传什么 | 体积 | 耗时 |
|---|---|---|---|
| 每 15 分钟增量同步 | 新 delta（T0+T1+T2 新增） | KB 级 | <1 s |
| 新机器首次接入 | T0 + T1 | ~2.2 MB | 1–2 s |
| "接着干那个任务" | + 该任务 T2 | 几十 KB–MB | <1 s |
| 点开某条消息看工具输出 | + 该 part T3 | KB | <1 s |
| 要原始附件 | + T4 | 按文件 | 按需 |
| 全量备份/搬库 | 单文件 `.batonpass/db.zst` | ~250–450 MB | 分钟级 |

压缩策略：`zstd -19 + 训练字典（每 6 个月重训）`；媒体按引用；文本剥壳。整体预计归档库是源库的 **8–12%**（你的 3.8 GB 源 → 约 300–450 MB，媒体按引用时约 250 MB）。

---

## 8. 冲突模型

**只三条规矩，没有第四条。**

| 情况 | 处理 |
|---|---|
| 同一会话两台机器都写 | 不可能——源会话本来就在某台机器上 |
| 同一会话在 A 后又长了（换了机器继续聊） | A 的记录不动；B 产生**新副本**，`forked_from` 指向原会话，`reason: continued-on-another-device` |
| 两台风同时改同一个 .md 文件 | 各写各的 `devices/<device>/` 副本，**不合并**；给人看"两份，选一份或自己合" |
| `task` 的状态 | 追加 `task_event`，当前状态 = `max(lamport, device)` 那条，**派生** |
| 两边声明"已解决"但内容不同 | 生成 `converge` 事件，让模型/人判断，不自动覆盖 |

**为什么不需要 CRDT**：聊天记录是只追加的，append-only 的合并就是**集合并集**，天然有交换律。CRDT 只对"多方同时改同一份可变文档"有价值——那正好是我们明令禁止的场景（每个人只写自己的文件）。

**fork 不 merge，这一条要写进 README。** 冲突不会消失，但会变成"看得见的分叉"，而不是"悄悄丢掉一个更新"。看得见的分叉是能处理的，丢数据不能。

---

## 9. 安全与隐私

**默认值（README 第一屏就要印）：**

```
⚠ Read-only by default. No network. No telemetry.
```

- 采集器**只读**打开源库（SQLite `?mode=ro&immutable=0`），源文件的 `mtime` 前后校验，动了就报错退出。
- 默认**不发任何网络请求**。要联网必须 `batonpass sync --enable` 显式打开，并打印目标地址。
- 无遥测、无崩溃上报、无"匿名使用统计"。
- 脱敏门设在**出口**（构造 delta 之前，不是传完之后）：命中 `密码|私钥|身份证|api key|token|secret|BEGIN PRIVATE KEY|sk-` 的记录强制 `local_only`，不出这台机器。被拦的要记 `redacted` 计数，让人知道。

**通道加密（v0.5 已实现）**：`batonpass sync init PATH --encrypt` 之后，每个 blob 落盘前先 `zlib → AES-256-GCM`，通道里已有的 blob 就地重封。密钥由 scrypt（`cryptography` 自带的绑定，不用 `hashlib.scrypt`——Apple 系统 Python 没有它）从口令派生，口令走 `BATONPASS_KEY` 或 `~/.batonpass/key`，**不进通道、不落日志、不打印**。地址仍是明文 sha256，所以幂等不受影响；口令错了给一句话，不伪装成文件损坏。

**明说的那点泄漏**：目录行（`t0/t1` 的 JSON 行）不封——dedupe key 在里面，封了 compaction 就没法比较对象。所以通道的持有者能看到"多少会话、什么标题、什么日期"，看不到正文。想连这点都藏，只能整条通道换镜像（T2 全封 + 目录行只留 sid），代价是不可压缩、不可人工 grep——不做。

**令牌**：不存静态共享 token（现在的记忆中枢就是一个静态 token + CORS `*` + 明文 HTTP 监听 0.0.0.0，这是要还的债）。通道模式不需要任何 token——文件系统权限就是认证。

---

## 10. 实现模式

**语言：Python 3.11+。理由：** MCP 生态、sqlite3 内置、三平台都有、Windows 上 `uv` 一条命令。不为了"单二进制好看"去写 Go/Rust 然后牺牲迭代速度。

**依赖底线：**

| 版本 | 依赖 | 说明 |
|---|---|---|
| v0.1 `discover` | **零依赖，纯 stdlib** | 一条命令就能跑，`python3 discover.py` |
| v0.2 `archive` | `zstandard`（唯一硬依赖） | 3.14+ 可用标准库 `compression.zstd` 时自动切换 |
| v0.5 `recall` | `mcp`（可选，只装 MCP server 时） | CLI 查询不需要 |

**目录结构：**

```
batonpass/
├── README.md
├── LICENSE                     MIT
├── pyproject.toml
├── src/batonpass/
│   ├── __main__.py             argparse 入口，子命令分发
│   ├── cli.py                  discover/init/sync/find/resume/handoff/report/replay/doctor
│   ├── paths.py                home 解析、平台差异、sandbox 守卫
│   ├── discover/
│   │   ├── registry.py         SOURCES 表（唯一真相源）
│   │   ├── probe.py            read_schema() / match_fingerprint() / shape_rank()
│   │   ├── fingerprint.py      15 条已知指纹，可被 sync 共享
│   │   ├── degrade.py          DEGRADATIONS 表 + Counter
│   │   └── adapters/
│   │       ├── base.py         Adapter 协议: probe/iter_sessions/iter_messages
│   │       ├── zcode.py  billion.py  dsh.py  alma.py  pinky.py  zhanlu.py
│   │       ├── files.py        .md/.jsonl 目录
│   │       └── generic.py      未知源 → Interchange 风格通用文档
│   ├── store/
│   │   ├── schema.py           DDL + migrations 注册表
│   │   ├── db.py               连接、WAL、feature detect(FTS5 trigram)
│   │   ├── content.py          sha256 内容寻址 + 字典训练 + zstd 多帧
│   │   └── write.py            幂等落库（content_hash 去重）
│   ├── identity/
│   │   ├── project_key.py      normalize() 三级
│   │   ├── artifacts.py        最后一次出现 + 噪声黑名单
│   │   └── tasks.py            Task Card、状态派生、stale 判定
│   ├── sync/
│   │   ├── channel.py          目录通道、channel.json、原子写、.tmp 排除
│   │   ├── envelope.py         编解码 + schema_fp + reconcile
│   │   ├── push.py  pull.py  heartbeat.py
│   │   └── transport/          git.py  dir.py  (v1.0 再加 s3/relay)
│   ├── recall/
│   │   ├── search.py           FTS5 + RRF + at: 解析 + 沉默契约
│   │   └── hooks.py            cwd → 自动召回
│   ├── mcp/server.py           三个工具
│   └── report/html.py          自包含单文件 HTML（无 CDN）
├── adapters/*.yaml             适配器即数据（社区可贡献）
├── fixtures/                   每源一个小样本库，CI 用
└── tests/
```

**关键签名（先定接口，再填实现）：**

```python
# discover
def probe(path: Path) -> Probe            # → {adapter, confidence, detected_by, failures[]}
def iter_sessions(adapter, conn) -> Iterator[RawSession]
def iter_messages(conn, session) -> Iterator[RawMessage]

# store
def put_content(db, text: str) -> str                      # → sha256
def get_content(db, sha: str) -> str
def train_dictionary(samples: list[bytes], size=112_640) -> bytes
def frame_decode_all(blob: bytes) -> bytes                 # 多帧！不丢尾巴

# identity
def normalize_project(path: str|None, git_remote: str|None, title: str|None) -> ProjectKey
def pick_artifacts(messages: list[Message]) -> list[Artifact]   # 从最后一次出现取

# sync
def write_delta(channel, device, sections, blobs) -> str    # → seq
def read_peer(channel, device, after_seq: int) -> Iterator[Delta]
def reconcile(local_schema, remote_fp, migrations) -> Literal["ok","migrated","refuse"]

# recall
def search(db, q: str, scope: str|None, limit=10) -> list[Hit]   # 空就是空
def resume_context(db, cwd: str, hours=72) -> str | None
```

**自检命令 `batonpass doctor`**：源有没有变、游标有没有卡、通道有没有人在写、库里有没有降级堆积、schema_fp 有没有分歧。**一条命令回答"我现在的状态健康吗"**。

---

## 11. 里程碑与验收

| 版本 | 内容 | 人日 | 验收标准（客观可查） |
|---|---|---|---|
| **v0.1 discover** | 单文件只读探测，出 `report.html` | 2 | 6 个源全扫；每源条数与手工 `count(*)` 差 <1%；除报告外**零写入**；`lsof` 证明零网络 |
| **v0.2 archive** | 归档 + 压缩 + 原始层 + 完整性校验 | 5 | zcode 47,988 条 100% 入库；重跑 0 新增；任意会话可明文还原并逐字比对 |
| **v0.3 sync** | 目录通道 + 信封 + 分层拉取 | 4 | 两台机器 T0+T1 互见 <3 s；拔掉通道本地仍可查；重放 100 次不产生重复行 · **已交付**：`39a3881`，2.8 MB / 0.1 s |
| **v0.4 identity** | 三级 project_key + 产物 + 交接卡 | 6 | 8 个目录簇 >95% 正确、误合并 <2% · **已交付**：12 个项目 / 558 诚实未分组，6,479 个产物引用 |
| **v0.5 recall** | FTS5 trigram + 时间窗 + 沉默契约 | 3 | `find "岗位表" --at 7d` 能命中 · **已交付**：58,508 条消息 16.6 s 建索引 |
| **v1.0** | 加密、doctor、CI、文档、发布 | 4 | 三平台安装通过；pip/npm 可装；有 3 个 fixture 的 CI |

合计 **~24 人日**。但 v0.1（2 天）就是可以发出去的独立产品——这就是反向发布顺序：先发一个"只看不动的体检工具"，再长成档案馆。

---

## 12. 设计借鉴台账

**这一节要原样进 README 的 `Acknowledgments`。** MIT 允许借设计，但致谢是体面，也是事实。

| 借了什么（设计层面） | 出处 | 我们怎么写的（实现层面） |
|---|---|---|
| 25+ 源的导入能力矩阵、命名化降级、三级格式探测、`PRAGMA table_info` 自适应、WAL 边车游标、多帧 zstd、产物取最后一次出现 | `Nwflower/dsh-chat-import` | 不看它的代码，按它公开文档里的 D1–D27 设计决策重写；我们的三级探测用指纹哈希 + Jaccard，它的用路径特征排序 |
| 每设备只写自己的文件 / 心跳自己文件 / 通道自描述 / 原子写 / seq+watermark / 双键兼容 / schema 指纹 reconcile / handoff 五行卡 / 一行会话索引 / 隐私门设在出口 | `jiabaobei/memory-bridge` | 同上，重写。我们的差别：**存原文**（他们不复制正文）、**T0–T4 分层按需拉**（他们只有全量）、**project_key 归一化**（他们靠日期+标题+文件名） |
| append-only + 内容寻址 + 去重 refcount | 自己的 billion-context 数据观察（实测 refcount 1.219） | 原创 |
| zstd-19 + 训练字典的实测数据 | 自己跑出来的（19.7× / +34%） | 原创 |
| 中继可丢弃、分层 T0–T4、按需深拉 | 自己需求推导 | 原创 |

致谢写法：

```markdown
## Acknowledgments

Design ideas in this project were informed by the public documentation of:
- [dsh-chat-import](https://github.com/Nwflower/dsh-chat-import) — capability matrix, named degradations,
  three-level format detection, WAL-sidecar cursors, multi-frame zstd.
- [memory-bridge](https://github.com/jiabaobei/memory-bridge) — per-device file ownership, channel identity,
  atomic writes, handoff cards.

No source code was copied. All implementations are original.
```

**不引用它们的代码**，也就不需要带它们的 LICENSE 文件；但如果你哪天想直接搬一段，必须加文件头 + 第三方 LICENSE，那不是现在的路线。

---

## 13. 明确不做（反范围）

| 不做 | 原因 |
|---|---|
| 双向同步 / 写回源工具 | 会动用户的源转录，别人的翻车现场。永远不做 |
| 云端托管服务 | 数据不出门是卖点，也是护城河 |
| 通用 CRDT 引擎 | 我们的场景不需要，引入就是复杂度税 |
| 语义重写 / 摘要生成 | 我们不"理解"，我们"保存"。生成摘要会让用户分不清原话和二手结论 |
| 自动合并两份冲突文件 | 合并错了比看得见的分叉更糟 |
| 遥测 | 第一屏承诺 No telemetry，就不能有 |
| 自建账号体系 | 通道模式不需要账号 |
