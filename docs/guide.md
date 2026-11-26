# batonpass · 用户接入指南 + 成本

> 这份就是以后 README 的正文骨架。全文按"你只想试一下"→"我要长期用"→"我要多台机器"排序。
> 每一档都写清：要多久、要多少磁盘、会不会动我的文件。

---

## 三档用法，自己挑

| 档 | 你得到什么 | 时间 | 风险 | 前提 |
|---|---|---|---|---|
| **A. 看一眼** | 一张 HTML 报告：你所有 AI 工具里到底存了多少东西、哪些能读、哪些读不到 | **30 秒** | 零（只读，不联网） | Python 3.11+ |
| **B. 单机归档** | 所有历史进一个本地库，能全文搜、能导出、能备份 | **5 分钟**（含首次归档） | 低（只新增 `~/.batonpass/`） | A 的基础上 |
| **C. 多机同步** | 换台机器 2 秒看到"有这件事、在哪台机器"，要动手再拉细节 | A+B + **2 分钟** | 低（通道是你自己的目录） | 一个能共享的目录 |

**建议所有人从 A 开始。** A 不动任何东西、不装任何依赖、不建任何后台进程。看完报告你才知道自己有多少东西值得归档。

---

## A 档：看一眼（30 秒，零依赖）

```bash
# 方式一：如果装了 uv（推荐，不污染系统 Python）
uvx --from batonpass-cli batonpass discover --report ~/Desktop/batonpass-report.html

# 方式二：直接跑单文件，连装都不用
curl -sL https://raw.githubusercontent.com/<你>/batonpass/main/discover.py -o /tmp/discover.py
python3 /tmp/discover.py --report ~/Desktop/batonpass-report.html
```

打开报告，你会看到：

- **每个源一张卡**：路径、大小、会话数、消息数、最早/最晚一条的时间、识别置信度（`high/medium/low`）
- **总账**：一共多少条消息、多少原始字节、能归档多少
- **降级台账**：哪几类内容拿不到（`reasoning-encrypted` / `attachment-skipped` / `locked`…）各多少条
- **未识别列表**：看着像 AI 数据但我不认识的目录，附前 200 字节摘要——方便提 issue

**成本**：0 秒配置、0 依赖、0 写入（除那份 HTML）、0 网络。CPU 一次性 5–30 秒（取决于库大小）。

**它不会做的事**：不改任何文件、不建库、不装后台任务、不上传任何东西。报告是**单文件 HTML**，没有 CDN、没有外链、断网也能开。

---

## B 档：单机归档（5 分钟）

```bash
# 1. 先看有什么（只读，不写任何文件）
batonpass discover

# 2. 归档（首次全量，之后重跑只补新增）
batonpass archive

# 3. 建检索索引（本地派生物，可随时重建）
batonpass index

# 4. 查
batonpass find "岗位表"
batonpass find "反爬" --at 7d --explain
batonpass tasks                    # 每个项目一行，按最近活跃排序
batonpass card xueji               # 一个项目的交接卡
batonpass resume                   # 当前目录上次干到哪了
```

> 出问题先跑 `batonpass doctor`：它把"为什么不好使"翻译成一行能动手的话（Python 版本、sqlite 有没有 trigram、库是哪个旧版本写的、通道在哪台机器落后了），只读，不改任何东西。`init` / `plan` 仍是 v1.0 的动词。

### 首次归档要多久

以你这台 Mac mini 的真实体量（`zcode` 846 MB / `billion` 1.4 GB / `dsh` 1.4 GB 目录 / alma / pinky / zhanlu，合计约 3.8 GB 相关数据）实测推算：

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 扫源 + 识别 | 3–10 s | 只读 schema 和前 64 KB |
| 全量读 + 归档 | **5–12 分钟** | 主要是 zstd-19 压缩，可 `--level 3` 降到 1–2 分钟（体积大 30%） |
| 建索引 | 10–40 s | FTS5 trigram |
| **合计** | **约 6–13 分钟（一次性）** | |

之后的增量：`< 2 秒` / 次。

### 磁盘成本（实测，不是估的）

`batonpass size` 会把库拆成四笔账。这是**没瘦身之前**的样子：

```
库 716 MB · 725 个会话 · 268308 条消息 · 492901 次工具调用

构成                     占用      占比
正文                  39 MB      5%    说的话，换台机器也换不回来
工具输入输出            206 MB     29%    可复现，通常是最大的一笔
搜索索引               166 MB     23%    本机派生，可随时重建，不同步
目录与索引             219 MB     31%    会话/消息/调用的指针
```

看这四个数就明白一件事：**你以为在存「对话」，其实一大半不是对话。**

- 725 个会话说过的话加起来只有 **39 MB**（压缩后）。这是唯一复制不回来的部分。
- **206 MB 是工具调用**：某次 `run_code` 的 stdout、一次 grep 的几百行、一次 read_file 的整个文件。它们可复现，也不该跟着你跨机器跑。
- **索引 166 MB，比它索引的正文大四倍**。中文 trigram 就是这样——每个字一个 token。它是派生物，删了重建，永远不同步。
- 目录 219 MB，其中 88 MB 是「每次工具调用一行」的指针（49 万次调用）。

源库现在 2.2 GB（billion 1.4 G + zcode 814 M + alma 36 M …），库 716 MB，已经压到约 1/3。

### 现在：716 MB → 365 MB，一条命令

```
batonpass slim --days 7 --vacuum        # 18 秒
716.6 MB  →  365.3 MB     （省 351 MB，49%）

构成                     瘦身前      瘦身后
正文                     39 MB       45 MB    （改存明文，见下）
工具输入输出               206 MB      101 MB   （7 天窗口外只留指针）
搜索索引                 166 MB      360 KB   （只索引标题）
目录与索引               219 MB      137 MB   （折叠 + 去 identity + VACUUM）
```

四笔账各自怎么省的：

| 动作 | 效果 | 代价 |
|---|---|---|
| 窗口外的工具输入输出 / 推理过程移出正文（`--days N`） | 156,669 块 · 224 MB | 只留指针（sha + 会话 + 序号）；`batonpass fetch <会话>` 回原库读回来 |
| 同一段重复调用折成一行，次数记在 `part.n` | 461,720 行 → 238,878 行 | 时间轴上「第 37 次」并成一条，花括号里的 37 还在 |
| 删掉从别人库里抄来的 `identity` 哈希 | 219,002 行 · 21 MB | 那个 sha 是另一张表的主键，本地用不上 |
| 索引只留标题 | 166 MB → 360 KB | 正文改在存储处搜（见下） |
| 正文改存明文 | **多占 23 MB** | 换来全文搜索 0.2 秒，而不是压着存解码 1.7 秒 |

窗口是价目表，先问价再花：

```
--days 1   221.8 MB     --days 7   131.5 MB     --days 30   33.4 MB
--days 3   169.6 MB     --days 14   57.0 MB     --days 90    7.8 MB
```

两个测出来的结论，都推翻了原来的猜想：

- **截断文档治不了中文 trigram 索引。** 800 字截断之后，14 MB 正文仍然要 213 MB 索引——中文每个 trigram 几乎都只属于一篇文档，索引是正文的 15 倍。截断只把 213 MB 变成 213 MB。
- **贵的是解码，不是扫描。** 59,720 条消息全量解码再扫 = 1.7 秒；同样一遍扫描明文 = 0.2 秒。所以正文存明文（多 23 MB，但用的时候便宜），索引只留「会话叫什么」这一张必须快的表。

`batonpass fetch <会话>` 实测：8,401 个正文块，4 秒，逐字节一致。**正文一个字都没删过**——`message.text_sha` 指向的内容永远不是候选。

### 关于「同一台机器就别复制了，直接读原目录」

可行，但得先说清它买什么、卖什么：

- **买**：本机省掉 716 MB，永远是「零拷贝、零延迟」。
- **卖**：**源库不是你的**。zcode 会 compact、billion 的 `index.db` 会重建、卸载一个工具时它的目录会被顺手删掉。batonpass 复制的那一份，是唯一挡在「工具自己清了历史」前面的东西。做 `places` 时验证过：dsh 的 workspace 文件里记着 93 条属于 `~/luocp` 的会话，其中 38 条在它的会话库里**已经不存在了**——当时要是没复制，它们现在就没了。

所以合理的形态不是「不复制」，是**分层**——四层现在都有对应的命令：

1. **正文永远全留**（45 MB）。最便宜、最不可替代。`slim` 不碰它。
2. **工具输入输出只留热窗口**（默认 30 天，本机用 7 天）。更早的只留指针（sha + 会话 + 序号），`batonpass fetch` 回源库读；源库不在了，前端和 `export` 都会写「这段已不可取」，而不是当成空白。
3. **索引是本机的，随时可删**。`index --force` 现在是真的重建（先 DROP 再建，否则 FTS5 的影子表会一直占着那 166 MB）。
4. **跨机器默认只传目录 + 摘要**，正文按需拉一个会话。被移出的正文推过去也不带字节，只带指针——对端和本机是同一笔交易。

### 其他成本

| 项 | 值 |
|---|---|
| CPU | 首次归档占满 1 核约 10 分钟；之后每次 <2 s |
| 内存 | 峰值 <200 MB（流式处理，不整体加载） |
| 网络 | **0**（B 档完全不联网） |
| 权限 | 只要读你的 AI 工具数据目录；不申请任何系统权限、不要管理员 |
| 后台进程 | 可选。不装也能用，`batonpass archive` 手动跑 |

---

## C 档：多机同步（再加 2 分钟）

### 选一个"通道"

通道就是一个**你两台机器都能读写的目录**。随便挑一个：

| 通道 | 怎么建 | 耗时 | 适合 |
|---|---|---|---|
| **网盘目录**（最省事） | `batonpass sync init ~/Nutstore/batonpass` | 10 s | 有坚果云/OneDrive/iCloud |
| **git 私有仓库** | `git init ~/baton-data` 后 `batonpass sync init ~/baton-data`，再 `git push` | 30 s | 想要版本历史 |
| **U 盘** | `batonpass sync init /Volumes/USB/batonpass` | 5 s | 物理隔离、极度敏感 |
| **tailnet 路径** | `batonpass sync init /Volumes/peer/batonpass` | 30 s | 已有 Tailscale |

通道里没有任何服务要跑，也没有进程要活着——就是一个文件夹。

### 第一台机器

```bash
batonpass archive                                   # 先有库
batonpass sync init ~/Nutstore/batonpass            # 建通道（幂等，重复跑没事）
batonpass sync push ~/Nutstore/batonpass --tier 1   # 推目录 + 摘要，约 2.8 MB
```

### 第二台机器

```bash
batonpass sync pull ~/Nutstore/batonpass --tier 0   # 目录，0.04 s，713 个会话
batonpass sync pull ~/Nutstore/batonpass --tier 1   # 摘要，0.05 s
```

到这一步，第二台机器上**一条消息都还没有**，但你已经能回答"那件事是在哪台机器、哪个会话里做的、第一句问的是什么"。

真要接着干了，再深拉那一个任务：

```bash
batonpass sync pull ~/Nutstore/batonpass --tier 2 --sid 5d49fc52
batonpass export 5d49fc52 --text                    # 完整对话，逐字节一致
```

实测：64 条消息 + 256 个工具调用 + 317 个 blob，0.14 秒；64/64 消息哈希完全一致，正文 0 处差异。

### 同步的节奏

| 触发 | 命令 | 成本 |
|---|---|---|
| 每天一次 / 收工前 | `batonpass sync push <通道> --tier 1` | 2 MB 级，2 秒 |
| 换机器前 | `batonpass sync push <通道> --tier 1` | 同上 |
| 到新机器 | `batonpass sync pull <通道> --tier 1` | 0.1 秒 |
| 要接手某个任务 | `batonpass sync pull <通道> --tier 2 --sid <前缀>` | 每任务百 KB 级 |

**成本**：通道 2.8 MB（713 个会话的目录 + 摘要）；本地库不变；没有常驻进程。
已推过的会话不会重推，除非它长了新消息、摘要算法升级、或者你提高了 tier。

反复重推会让流文件变长（追加模型的代价）。这件事自己会收：`sync push` 在文件明显超过
"活着的大小"时把它重写成一条事实一行；想手动收就说 `batonpass sync compact <通道>`。
重写会把 `heads` 里的 generation +1，读的一方看到新 generation 就从 0 重读一遍——
安全，因为导入全是幂等的 `INSERT OR IGNORE`。实测：同一批 713 个会话推三次 1005 KB / 2139 行，
compaction 后回到 335 KB / 713 行，pull 过来的库一字不差。

### 通道放在网盘里，就顺手加密

```bash
export BATONPASS_KEY='你每台机器都会输入的这句'
batonpass sync init ~/Nutstore/batonpass --encrypt
```

- 落盘的每个 payload 都先 AES-256-GCM 封好；通道里已有的 blob 会就地重封。
- 密钥（scrypt 派生）**不进通道**：从 `BATONPASS_KEY` 或 `~/.batonpass/key` 第一行取。
- 地址仍然是明文 sha256 → 同一条消息重传还是落到同一个路径，幂等没被破坏。
- 口令不对时说一句人话（"这个通道是加密的，密钥对不上"），不会假装文件损坏。
- **没封的只有目录行**（dedupe key 在里面）：网盘方能看到"你有 713 个会话、标题是这些、日期是这些"，
  看不到一句正文。这个泄漏是真的，所以写在明面上。

### 让 AI 工具直接查（可选）

```bash
batonpass mcp          # 在 stdio 上说 MCP，客户端自己起，退出后就没了
```

五个工具：`memory_search`（用模糊概念找）、`memory_add`（把刚学到的记下来，会出现在交接卡的"备注"里）、
`resume`（把接力棒交给你）、`places`（这个人在哪些目录干过活，哪个工具干过）、
`relay`（谁正拿着这条线；`take`/`note`/`next`/`done` 顺手就能写）。配到 Claude Code / Cursor 的 MCP 配置里：

```json
{"mcpServers": {"batonpass": {"command": "uvx", "args": ["batonpass", "mcp"]}}}
```

找不到东西时它不会编：返回的是"not found — 别自己造一个"。

### 两台机器同时干同一件事会怎样

不会打架，也不会悄悄丢东西：

- 记录只追加 → 合并不冲突
- 同一个任务在两台机器继续聊 → 生成**两个看得见的副本**，标 `continued-on-another-device`，你能看见分叉
- 你手改的 `.md` → 各写各的，工具**不合并**，给你看"两份，你选"
- 任务状态 → 按时间+设备取最新一条，标出"这是 Mac mini 10-28 更新的"

**看得见的分叉能处理，静默的丢数据不能。** 这是刻意的取舍。

### 同一台机器上，两个工具干同一件事

Alma、dsh、zcode 各有各的"项目"概念，谁也不认谁的。能对上的只有一个东西：**绝对路径**。

```bash
batonpass places                 # 一个目录一行，标明哪些工具在里面干过
batonpass places --shared        # 只看两家以上工具都碰过的
batonpass relay                  # 这个目录谁拿着、另一侧上次做到哪
batonpass relay --take --note "先把 dsh 的目录并进来"
batonpass relay --next "把 MCP 两个新工具接上"
batonpass relay --done           # 收工，把棒放下
```

还有一件事得单独说：**dsh 的会话表里根本没有 cwd 这一列**。它只在 `~/.dsh*/storages/workspace.json` 里说「这几条会话属于这个目录」。所以归档的时候会顺手按这份声明把会话放回各自的工作区——只补没有目录的，源自己写过的 cwd 绝不动；同一条会话在两边有两种写法（`session-<uuid>` 和 `<uuid>`）也认成同一条。少了这一步，`resume --cwd ~/luocp` 会对一个装着 55 段对话的文件夹说「没有记录」。

棒不存任何地方，是从事件日志里算出来的：事件只追加、带 Lamport 号，号大的持有；
同号的两次接手**两条都留着**，按 `(设备, 事件 id)` 定胜负——随便定的规则，但每台机器算出来
一样，所以两个从不通信的 agent 也不会各干一份。输的那条不删，卡片上写"同一时刻也有人接了"。

接力记录会跟着 `batonpass sync push` 走（t0 流），所以另一台机器拉一次就知道棒在谁手上——
两台机器不需要互相通信，也不需要谁当裁判。

`--done` 会把之前写的「下一步」一起作废（写它的人就是做完它的人），所以卡片不会拿一件已经做完的
事当计划递给下一个人；`--release` 相反，那是「没干完就放下了」，那条下一步留给接的人。

`batonpass resume` 会多一行，回答卡片答不了的那个问题——**这个目录现在有没有人正在里面干活**。

---

## 常见坑（真实会遇到）

| 现象 | 原因 | 解法 |
|---|---|---|
| `batonpass resume` 返回空 | 沉默契约：没有高质量命中就不编 | 换更具体的关键词，或 `--at 30d` 放宽时间窗 |
| 明明有新对话，同步后却没变化 | WAL 模式主库 `mtime/size` 没变 | 已内置 WAL 边车签名；旧版本请升级 |
| 某个日志文件解析出一半就断了 | 多帧 zstd，标准库只解第一帧 | 已内置循环解帧 + 长度校验；报 `truncated` 就是真的缺 |
| `locked` 降级 | 工具正在写库 | 关掉那个 AI 工具再跑，或等下一轮 |
| 中文搜不到 | FTS5 用了 unicode61 分词 | 已默认 trigram；`batonpass doctor` 会告警 |
| 换了工具版本后认不出来 | 表结构变了 | `batonpass discover --explain` 看是哪一档失败；提 issue 附 schema |
| 报告里"未识别"一堆 | 你的工具还不支持 | 提 issue，附路径 + 前 200 字节（**别贴对话内容**） |
| 归档库比预期大 | 媒体被复制了 | 默认 `--media=ref`；检查是不是加过 `--media=copy` |
| 磁盘快满了 | 首次归档临时占空间 | `batonpass archive --level 3` 降压缩等级，或先 `batonpass archive --only zcode` |
| 库太大了 | 工具输入输出占了一大半，都可复现 | `batonpass slim --dry-run` 先问价，再 `batonpass slim --days 7 --vacuum` |

---

## 卸载与回滚

```bash
batonpass uninstall          # 删定时器、删 MCP 注册
rm -rf ~/.batonpass          # 删库（源文件从头到尾没被动过）
```

**你的源数据一个字节都没被改过**，所以卸载就是删掉自己建的那一个目录。这是"只读"设计的额外好处。

---

## 一句话总结成本

**A 档：30 秒，零风险，先看报告。**
**B 档：5 分钟、约 400 MB 磁盘、一次性 10 分钟 CPU，换一个能全文搜的永久档案。**
**C 档：再加 2 分钟和一个共享目录，换"换机器不用重新解释"这件事。**
**同一目录两个工具：零配置，`batonpass relay` 就够——换工具不用重新解释。**
