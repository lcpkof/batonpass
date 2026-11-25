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
uvx batonpass discover --report ~/Desktop/batonpass-report.html

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
# 1. 初始化（建库、注册本机）
batonpass init

# 2. 先看它打算干什么，不实际写入
batonpass plan

# 3. 正式归档（首次全量）
batonpass archive --all

# 4. 查
batonpass find "岗位表"
batonpass find "上周关于反爬的讨论" --at 7d

# 5. 体检
batonpass doctor
```

### 首次归档要多久

以你这台 Mac mini 的真实体量（`zcode` 846 MB / `billion` 1.4 GB / `dsh` 1.4 GB 目录 / alma / pinky / zhanlu，合计约 3.8 GB 相关数据）实测推算：

| 阶段 | 耗时 | 说明 |
|---|---|---|
| 扫源 + 识别 | 3–10 s | 只读 schema 和前 64 KB |
| 全量读 + 归档 | **5–12 分钟** | 主要是 zstd-19 压缩，可 `--level 3` 降到 1–2 分钟（体积大 30%） |
| 建索引 | 10–40 s | FTS5 trigram |
| **合计** | **约 6–13 分钟（一次性）** | |

之后的增量：`< 2 秒` / 次。

### 磁盘成本

| 项 | 大小 |
|---|---|
| 归档库（默认 `--media=ref`，媒体只记引用不复制） | **约 250–450 MB** |
| 归档库（`--media=copy` 全复制媒体） | + 媒体原大小 |
| 索引 | 约 60–90 MB |
| 报告 | 100–500 KB |
| **合计（默认）** | **约 350–550 MB / 3.8 GB 源（≈ 10%）** |

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

### 两台机器同时干同一件事会怎样

不会打架，也不会悄悄丢东西：

- 记录只追加 → 合并不冲突
- 同一个任务在两台机器继续聊 → 生成**两个看得见的副本**，标 `continued-on-another-device`，你能看见分叉
- 你手改的 `.md` → 各写各的，工具**不合并**，给你看"两份，你选"
- 任务状态 → 按时间+设备取最新一条，标出"这是 Mac mini 10-28 更新的"

**看得见的分叉能处理，静默的丢数据不能。** 这是刻意的取舍。

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
