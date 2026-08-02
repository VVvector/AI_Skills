# Sashiko 输入层 & 准备层接口设计

> 本文档聚焦两层之间的**接口契约、数据格式、设计原则**，不涉及具体实现细节。
> 两层的**核心契约**是 `PatchInput` 结构——无论来源是什么，最终都要变成 `PatchInput[]` 才能进入准备层。

---

## 一、输入层：5 种 Patch 来源

### 1.1 接口设计总览

| 来源 | 输入格式 | 输出格式 | 是否需要本地仓库 | 是否入库 |
|---|---|---|---|---|
| **commit SHA / range** | `repo_path` + `<sha>` 或 `<sha1>..<sha2>` | `ReviewInput` | ✅ 必须 | ❌ |
| **mbox 邮件** | RFC822 邮件原文（字节流） | `PatchsetMetadata` + `Patch` | ❌ | ✅ |
| **webhook** | HTTP POST body + 签名 | `FetchRequest`（异步） | ❌ | ✅ |
| **fetcher** | `FetchRequest` | `Event::PatchSubmitted` | ✅ kernel 镜像 | ✅ |
| **patchwork** | patchwork check 事件 | check 状态回写 | ⚠️ 主要输出方向 | ✅ |

### 1.2 统一输出契约：`ReviewInput`

无论哪种来源，最终都标准化为：

```rust
struct ReviewInput {
    id: i64,
    subject: String,
    patches: Vec<PatchInput>,
}

struct PatchInput {
    index: i64,                  // patch 在系列中的序号
    diff: String,                // 纯 diff 或 mbox 正文
    subject: Option<String>,
    author: Option<String>,
    date: Option<i64>,
    message_id: Option<String>,
    commit_id: Option<String>,   // 关键：决定准备层走 checkout 还是 git am
}
```

**`commit_id` 是关键分水岭字段：**

| `commit_id` | 来源 | 准备层路径 |
|---|---|---|
| ✅ 有值 | commit SHA/range、fetcher | `git checkout`（快） |
| ❌ 无值 | mbox 邮件、webhook | `git am`（需构造 mbox） |

---

## 二、准备层：5 个阶段

### 2.1 接口设计总览

| 阶段 | 输入 | 输出 | 关键决策点 |
|---|---|---|---|
| **① Baseline 检测** | patch body、subject、MAINTAINERS、remote 配置 | `baseline_sha: String` | 6 级优先级 fallback |
| **② Worktree 准备** | `repo_path`、`baseline_sha`、策略选项 | `GitWorktree { path, repo_path }` | 4 种策略选择 |
| **③ Patch 应用** | `PatchInput`、`GitWorktree` | 3 个 HashMap（shas/shows/messages） | `commit_id` 双路径 |
| **④ patchset JSON 组装** | `PatchInput[]` + 3 个 HashMap | `rich_patches: Vec<JSON>` | 字段映射 |
| **⑤ 并发 review** | `rich_patches` + `baseline_sha` + 配置 | 每个 patch 的 review JSON | `concurrency` 限流 |

### 2.2 阶段 ① Baseline 检测

**输入：** patch body / subject / MAINTAINERS / remote_map / custom_remotes

**输出：** `baseline_sha: String`

**优先级机制**（从高到低）：

| 优先级 | 来源 | 解析方式 |
|---|---|---|
| 1 | patch body 显式声明 | `base commit: <sha>` / `based on <sha>` |
| 2 | subject 版本标签 | `[PATCH v6]` → 推断 -rc 版本 |
| 3 | subsystem heuristic | MAINTAINERS 匹配 → 子系统默认 tree |
| 4 | linux-next remote | 仓库中 `linux-next` remote |
| 5 | mainline remote | 仓库中 `mainline` remote |
| 6 | custom remotes | 用户自定义 remote |

### 2.3 阶段 ② Worktree 准备

**输入：** `repo_path`、`baseline_sha`、策略选项

**输出：** `GitWorktree { path, repo_path }`

**4 种策略对比：**

| 策略 | 触发条件 | 隔离性 | 性能 | 适用场景 |
|---|---|---|---|---|
| `current_tree` | 本地 review 默认 | ❌ 不隔离 | ⚡ 最快 | 本地调试 |
| `reuse_worktree` | 指定复用路径 | ⚠️ 半隔离 | ⚡ 快 | 多次 review 同一 patch |
| `scratch_clone` | 需完全隔离 | ✅ 完全隔离 | 🐢 慢 | 需独立源码树 |
| 新建 worktree（默认） | 服务端 | ✅ 隔离+共享对象库 | 🟡 中 | 服务端 review |

### 2.4 阶段 ③ Patch 应用（双路径）

**输入：** `PatchInput` + `GitWorktree`

**输出：** 3 个 HashMap

| 路径 | 触发条件 | 机制 | 产出 |
|---|---|---|---|
| **A: checkout** | `commit_id = Some(sha)` | `git checkout <sha>` | 直接用已知 SHA |
| **B: git am** | `commit_id = None` | 构造 mbox → `git am` | 应用后取 `HEAD` SHA |

**无论哪条路径，都填充相同的 3 个 HashMap：**

| HashMap | key | value | 获取方式 |
|---|---|---|---|
| `patch_shas` | patch index | commit SHA | checkout 或 `git am` 后取 HEAD |
| `patch_shows` | patch index | `git show --patch` 输出 | 完整 commit（header+message+diff） |
| `patch_messages` | patch index | `git show --no-patch` 输出 | 仅 commit header+message |

### 2.5 阶段 ④ patchset JSON 组装

**输入：** `PatchInput[]` + 3 个 HashMap

**输出：** `rich_patches: Vec<JSON>`

**JSON Schema：**

```json
{
  "patches": [{
    "index": <i64>,
    "subject": "<string>",
    "author": "<string>",
    "date_string": "<RFC2822>",
    "diff": "<纯 diff>",                    // ← PatchInput.diff
    "commit_id": "<sha>",                   // ← patch_shas
    "git_show": "<git show --patch>",       // ← patch_shows
    "commit_message_full": "<git show --no-patch>"  // ← patch_messages
  }]
}
```

**字段消费映射**（Worker 层如何使用）：

| JSON 字段 | Worker 层用途 |
|---|---|
| `diff` | → `target_commit_diff_only`（精简 context，Stage 3-6） |
| `git_show` | → `target_commit_diff`（完整 context，Stage 1/2/7，+ changelog 注入） |
| `commit_id` | → `target_commit_sha`（git metadata、virtual HEAD） |
| `commit_message_full` | → 展示用，不直接进 context |
| `index` / `subject` / `author` / `date_string` | → 元数据展示 |

### 2.6 阶段 ⑤ 并发 review

**输入：** `rich_patches` + `baseline_sha` + `concurrency` 配置

**输出：** 每个 patch 的 review JSON

**机制：** 多 patch 并发调用 `review_single_patch`，受 `concurrency` 限流；每个 patch 创建**独立的 Worker 实例**（不跨 patch 复用）

---

### 2.7 准备层 → Worker 层接口契约

准备层通过**两个渠道**把数据注入 Worker 层：

#### 渠道 1：配置级数据（Worker 构造时注入，跨 patch 不变）

| 数据项 | 来源 | Worker 用途 |
|---|---|---|
| **worktree 路径** | `GitWorktree.path` | 所有 git 工具的执行目录 |
| **virtual_head** | `patch_shas[p.index]` | LLM 用 `HEAD` 时指向当前 patch 的 SHA |
| **active_patch_files** | `git diff-tree` 获取 | 当前 patch 修改的文件列表（优化工具调用） |
| **prompts 路径** | `options.prompts` | 加载 prompt 指南文件 |
| **series_range** | `"{baseline_sha}..{target_sha}"` | 提取 `baseline_sha` + Stage 10 系列验证 |
| **AI 参数** | `ai.max_input_tokens` 等 | `max_interactions` / `temperature` |
| **stages** | `options.stages` | 指定运行哪些 stage |
| **custom_prompt** | `options.custom_prompt` | 自定义 prompt 覆盖 |

#### 渠道 2：patch 级数据（每次 review 调用注入）

**输入格式（patchset JSON）：**

```json
{
  "id": <patchset_id>,
  "subject": "<patchset 主题>",
  "patches": [<完整系列的所有 patch>],
  "patch_index": <当前要 review 的 patch 索引>
}
```

| 字段 | 来源 | Worker 用途 |
|---|---|---|
| `id` | `patchset_id` | 日志标识 |
| `subject` | `subject` | 主题展示 |
| `patches` | `rich_patches`（完整系列） | **系列上下文** + 提取当前 patch 的 `diff`/`git_show`/`commit_id` |
| `patch_index` | `p.index` | **定位当前 review 哪个 patch** |

#### Worker 内部消费逻辑

| 提取项 | 来源 | 用途 |
|---|---|---|
| `baseline_sha` | `series_range` 按 `..` 切分取前半 | git metadata 注入 context |
| `target_commit_sha` | `patches[]` 中按 `patch_index` 匹配取 `commit_id` | git metadata、virtual HEAD |
| `target_commit_diff` | 当前 patch 的 `git_show` + changelog 注入 | 完整 context（Stage 1/2/7） |
| `target_commit_diff_only` | 当前 patch 的 `diff` | 精简 context（Stage 3-6） |

#### 接口契约总结

| 契约 | 类型 | 必填 | 用途 |
|---|---|---|---|
| `patchset JSON` | `JSON` | ✅ | Worker 入口，含系列 + 当前 patch 标识 |
| `series_range` | `String` | ✅ | 提取 baseline_sha + 系列验证 |
| `worktree.path` | `PathBuf` | ✅ | 工具执行目录 |
| `virtual_head` | `String` | ✅ | LLM 的 HEAD 引用解析 |
| `active_patch_files` | `Vec<String>` | ⚠️ 可选 | 工具调用优化 |
| `prompts 路径` | `PathBuf` | ✅ | 加载 prompt 指南 |

#### 设计原则

| 原则 | 体现 | 目的 |
|---|---|---|
| **双渠道分离** | 配置级走构造函数，patch 级走 run 调用 | 跨 patch 不变 vs 每个 patch 不同 |
| **系列上下文共享** | `patches` 传整个系列，`patch_index` 标识当前 | Worker 能看全系列，但聚焦当前 patch |
| **virtual_head 机制** | LLM 用 `HEAD` 时自动指向当前 patch | 避免误读基准 commit |
| **Worker 不跨 patch 复用** | 每个 patch 创建新 Worker | 因为 series_range/virtual_head 是 patch 级的 |

---

## 三、整体数据流

```
[输入层] 5 种异构来源
   │
   ├── commit SHA/range   ─┐
   ├── mbox 邮件           │
   ├── webhook            ├──► 标准化 ──► ReviewInput { patches: PatchInput[] }
   ├── fetcher            │
   └── patchwork (输出方向)┘
                              │
[准备层]                      ↓
                              │
   ┌──────────────────────────┴───────────────────────────┐
   │                                                      │
   │  ① Baseline 检测  ──► baseline_sha                   │
   │  ② Worktree 准备  ──► GitWorktree                    │
   │  ③ Patch 应用     ──► 3 HashMap (shas/shows/messages)│
   │  ④ JSON 组装      ──► rich_patches                   │
   │  ⑤ 并发 review    ──► review_single_patch            │
   │                                                      │
   └──────────────────────────────────────────────────────┘
                              │
                              ↓
              [Worker 层: build_context → stages]
```

---

## 四、设计原则与机制

### 4.1 核心设计原则

| 原则 | 体现 | 目的 |
|---|---|---|
| **统一契约** | 5 种来源都归一化为 `PatchInput[]` | 下游无需关心来源，解耦输入与处理 |
| **字段驱动分支** | `commit_id` 是否有值决定 checkout/git am | 一字段控制双路径，简洁可测 |
| **隔离与共享平衡** | worktree 4 种策略 | 隔离性 vs 性能（共享 object store） |
| **Fallback 容错** | baseline 6 级优先级 | 确保总能找到合适基准 commit |
| **并发限流** | `buffer_unordered(concurrency)` | 多 patch 系列并发，但可控资源 |
| **系列上下文共享** | `rich_patches` 整体 clone | 每个 patch review 能看到全系列 |
| **短路保护** | `--no-ai` / 应用失败 / 空 patches | 跳过 AI review，快速返回 |

### 4.2 关键机制说明

#### 机制 1：`commit_id` 双路径

```
                    ┌─ commit_id = Some(sha) ──► git checkout ──┐
PatchInput ─────────┤                                                     ├─► 相同的 3 HashMap
                    └─ commit_id = None ──────► git am ────────┘
```

**设计意图：**
- 已知 SHA（来自 git 仓库）→ 直接 checkout，最快
- 未知 SHA（来自邮件/webhook）→ 构造 mbox 用 `git am` 应用，兼容
- **殊途同归**：无论哪条路径，产出相同的 3 个 HashMap，下游无感知

#### 机制 2：Baseline 6 级 Fallback

```
显式声明 ──► subject 标签 ──► subsystem heuristic
                                          ↓ (失败)
                              linux-next ──► mainline ──► custom remotes
```

**设计意图：**
- 优先用作者显式声明的 base commit（最准）
- 退而求其次用 subject 版本标签推断
- 再退用子系统默认 tree（如 net→net-next）
- 最后 fallback 到 remote 配置
- **确保总能找到基准**，避免 review 无法进行

#### 机制 3：Worktree 隔离策略

```
current_tree (不隔离)  ──►  reuse_worktree (半隔离)
        │                            │
        ↓ (隔离性递增)                ↓
scratch_clone (完全隔离)  ──►  新建 worktree (隔离+共享对象库)
```

**设计意图：**
- **本地调试**：`current_tree` 最快，直接用当前工作树
- **服务端 review**：新建 worktree 隔离 + 共享 object store（省磁盘）
- **完全隔离**：`scratch_clone` 独立源码树（最安全）
- **复用优化**：`reuse_worktree` 跳过重建，多次 review 同一 patch

#### 机制 4：三个 HashMap 分离

```
patch_shas     ──► index → SHA           (身份标识)
patch_shows    ──► index → 完整 commit   (含日志，供语义审查)
patch_messages ──► index → 仅 commit msg (展示用)
```

**设计意图：**
- **独立获取**：每个字段独立调用 git 命令，失败可单独重试
- **按需消费**：不同 Stage 用不同字段（Stage 3-6 用 diff，Stage 1/2/7 用 git_show）
- **部分缺失兜底**：某字段获取失败不阻塞整个 review

#### 机制 5：并发 + 系列上下文

```
patches[0] ─┐
patches[1] ├─► buffer_unordered(concurrency) ─► review_single_patch
patches[2] ┤        │
...        │        └─ 每个 review 都能看到完整 rich_patches（系列上下文）
patches[N] ─┘
```

**设计意图：**
- **并发提速**：多 patch 系列并发 review
- **限流保护**：`concurrency` 控制最大并发度，避免资源耗尽
- **系列感知**：每个 patch review 能看到整个系列（如 patch 2 可以引用 patch 1 的改动）

---

## 五、接口契约总结表

### 输入层 → 准备层

| 契约 | 类型 | 必填字段 | 用途 |
|---|---|---|---|
| `ReviewInput` | struct | `id`, `subject`, `patches[]` | 输入层→准备层的统一载体 |
| `PatchInput` | struct | `index`, `diff` | 单个 patch 标准化表示 |
| `PatchInput.commit_id` | `Option<String>` | 可选 | **决定准备层路径**（checkout vs git am） |

### 准备层 → Worker 层

| 契约 | 类型 | 必填字段 | 用途 |
|---|---|---|---|
| `rich_patches` | `Vec<JSON>` | `diff`, `commit_id`, `git_show` | patchset JSON，Worker 入口 |
| `baseline_sha` | `String` | 必填 | git metadata、series_range |
| `GitWorktree.path` | `PathBuf` | 必填 | 工具执行目录、AST 解析 |
| `series_range` | `Option<String>` | 可选 | Stage 10 系列验证 |

---

## 六、一句话总结

**输入层用 `PatchInput` 统一 5 种来源，`commit_id` 字段驱动准备层双路径（checkout/git am）；准备层通过 6 级 fallback 检测 baseline、4 种策略隔离 worktree、3 个 HashMap 分离获取元数据，最终组装为 patchset JSON 并发传给 Worker 层**。
