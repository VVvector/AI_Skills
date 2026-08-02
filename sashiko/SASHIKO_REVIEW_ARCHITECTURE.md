# Sashiko 多阶段 LLM Review 架构详解

> 基于 sashiko-main 代码的深度分析文档
> 
> 生成时间: 2026-07-29

---

## 目录

- [1. 总体架构](#1-总体架构)
- [2. Stage 执行链路](#2-stage-执行链路)
- [3. Prompt 构建详解](#3-prompt-构建详解)
- [4. Context 构建详解](#4-context-构建详解)
- [5. Stage 验证逻辑](#5-stage-验证逻辑)
- [6. Phase 0 — 子系统预选](#6-phase-0--子系统预选)
- [7. Planning Phase](#7-planning-phase)
- [8. Prefetch Context 预取策略](#8-prefetch-context-预取策略)
- [9. 动态代码提取机制](#9-动态代码提取机制)
- [10. 工具使用前置条件与工作原理](#10-工具使用前置条件与工作原理)
- [11. 关键文件索引](#11-关键文件索引)

---

## 1. 总体架构

### 1.1 架构图

```
reviewer.rs::review_patchset_task()
  │
  ├─ run_review_tool()                    ← 子进程模式（fork review binary）
  │     └─ bin/review.rs::main()
  │           └─ local_review.rs::run_worker_from_stdin()
  │                 └─ run_worker_in_worktree()
  │                       └─ review_single_patch()
  │                             └─ Worker::run()     ← 核心入口
  │                                   ├─ Phase 0: 预选子系统指南
  │                                   ├─ 构建 shared_context（静态+动态）
  │                                   ├─ Planning Phase: 选择执行哪些 stage
  │                                   ├─ Stage 1-7 并行执行（try_join_all）
  │                                   │     └─ execute_stage()
  │                                   │           └─ ReviewStageSession + SessionRunner
  │                                   │                 └─ AiRequest { system, messages, tools }
  │                                   │                       └─ provider.generate_content()
  │                                   └─ Stage 8-11 顺序执行
  │                                         去重 → 冲突解决 → 验证 → LKML 报告
```

### 1.2 核心设计理念

| 设计点 | 说明 |
|--------|------|
| **多阶段并行审查** | Stage 1-7 并行，每个阶段独立审查视角 |
| **动态 Prompt 加载** | LLM 自动选择相关子系统指南，避免全量加载爆 token |
| **工具赋能** | 9 个 git 相关工具供 LLM 动态查询代码上下文 |
| **格式强制约束** | 严格 JSON Schema 验证 + 重试机制 |
| **预取优化** | Tree-sitter 预加载函数体，减少工具调用 |
| **双版本 Context** | Stage 3-6 用精简版（不含 commit log），节省 token |

### 1.3 Stage 职责概览

| Stage | 名称 | 职责 | 必选 |
|-------|------|------|------|
| 1 | Analyze commit main goal | 分析提交的高层意图、架构缺陷、UAPI 破坏 | ✅ |
| 2 | High-level implementation verification | 验证代码是否实现了 commit message 的声称 | ✅ |
| 3 | Execution flow verification | 静态分析控制流、逻辑错误、边界条件 | ✅ |
| 4 | Resource management | 内存泄漏、UAF、引用计数、生命周期 | ❌ |
| 5 | Locking and synchronization | 死锁、竞态、RCU、锁顺序 | ❌ |
| 6 | Security audit | 缓冲区溢出、TOCTOU、权限提升、信息泄漏 | ❌ |
| 7 | Hardware engineer's review | 寄存器访问、IRQ、DMA、内存屏障 | ❌ |
| 8 | Deduplication | 合并去重所有 concerns | ✅ |
| 9 | Conflict resolution | 解决 concern 与 dismissed_concern 的冲突 | ✅ |
| 10 | Verification & severity | 验证 + 严重性评估 + 误报过滤 | ✅ |
| 11 | LKML report | 生成 LKML 格式报告 | ✅ |

---

## 2. Stage 执行链路

### 2.1 并行调度

```rust
// prompts.rs 中 Worker::run() 的 Stage 并行调度
let mut stage_futures = Vec::new();
for stage_num in 1..=7 {
    let stage = create_stage(stage_num);
    let system_prompt = if use_log {
        shared_context.clone()           // Stage 1,2,7: 含 commit log
    } else {
        shared_context_no_log.clone()    // Stage 3,4,5,6: 精简版
    };
    stage_futures.push(self.execute_stage(stage, system_prompt, ...));
}

// 并行执行所有 Stage
let stage_results = futures::future::try_join_all(stage_futures).await?;
```

### 2.2 单个 Stage 执行流程

```
execute_stage(stage, system_prompt, user_prompt)
    │
    ├─ 1. get_stage_prompt(stage_num)        ← 获取 Stage 专属指令
    │
    ├─ 2. 构造 format_guidance              ← JSON 输出格式约束
    │
    ├─ 3. 组合 user_prompt = stage_prompt + format_guidance
    │
    ├─ 4. 创建 ReviewStageSession
    │     ├─ system_prompt: 静态+动态上下文
    │     ├─ user_prompt: Stage 指令 + 格式约束
    │     ├─ tools: 9 个 git 工具
    │     └─ temperature: 配置值
    │
    └─ 5. SessionRunner::run(session)        ← 对话循环引擎
          ├─ 构造 AiRequest
          ├─ 调用 LLM
          ├─ 处理工具调用（如有）
          ├─ 验证最终输出
          └─ 失败则重试（最多 3 次）
```

### 2.3 SessionRunner 对话循环

```
loop {
    request = AiRequest {
        system: session.system_prompt(),     ← 完整上下文
        messages: history,                   ← 对话历史
        tools: session.tools(),              ← 工具声明
        temperature: session.temperature(),
    }
    
    resp = provider.generate_content(request)
    
    if resp.tool_calls:
        results = session.call_tools(tool_calls)
        history += tool_results
        continue                            ← 继续循环
    
    match session.validate(&resp) {
        Ok(output) → return Ok(SessionResult { output, history, usage })
        Err(FormatViolation(msg)) → 追加反馈消息，重试
        Err(Fatal(err)) → 报错退出
    }
}
```

### 2.4 工具集

| 工具 | 功能 |
|------|------|
| `git_read_files` | 读取指定文件/行范围 |
| `git_blame` | 查看代码作者归属 |
| `git_diff` | 获取 diff 内容 |
| `git_show` | 查看 commit 内容 |
| `git_log` | 查看 git 历史 |
| `git_ls` | 列出目录结构 |
| `git_grep` | 搜索代码模式 |
| `git_find_files` | 按模式查找文件 |
| `read_prompt` | 读取 review-prompt 知识库 |

---

## 3. Prompt 构建详解

### 3.1 两层 Prompt 架构

每个 Stage 的 Prompt 由两部分组成：

```
AiRequest = {
    system: system_prompt,     // ← 静态知识库 + 动态上下文
    user:   user_prompt,       // ← Stage 指令 + 格式约束
    tools:  [工具声明],
}
```

### 3.2 system_prompt（静态+动态上下文）

```
system_prompt = static_context + dynamic_context
```

**static_context 组成:**
```
"当前日期是 2026年7月29日"
"你是 Linux 内核专家维护者..."
"批量并行调用工具..."
<global_review_guidelines>
  networking-core.md 全文    ← Phase 0 选中的子系统指南
  locking.md 全文             ← Phase 0 选中的子系统指南
  rcu.md 全文                 ← Phase 0 选中的子系统指南
  patterns/*.md               ← Phase 0 选中的模式文件
</global_review_guidelines>
```

**dynamic_context 组成:**
```
=== Git Metadata ===
Target Commit SHA: abc1234
Baseline SHA: def5678

Target Commit:
<git show 完整输出，含 commit message + diff>

<pre_fetched_context>
自动预取的函数体代码（Tree-sitter 分析）
</pre_fetched_context>
```

### 3.3 user_prompt（Stage 指令）

每个 Stage 有独立的审查视角指令：

**Stage 5 — Locking and synchronization 示例:**
```
# Stage 5. Locking and synchronization

你是世界级并发和锁专家，审查 Linux 内核补丁。
必须检查以下类别：
1. 原子上下文中睡眠（spinlock 中调用 mutex_lock 等）
2. 锁顺序与死锁（AB-BA 死锁）
3. 竞态条件与无锁访问
4. UAF / 释放内存上的锁操作
5. RCU 规则
6. 未保护的状态修改
...
```

**format_guidance（格式约束）:**
```
输出必须是 JSON 对象：
{
  "concerns": [
    {
      "type": "locking",
      "description": "...",
      "reasoning": "...",
      "preexisting": false,
      "locations": [{
        "file": "drivers/net/eth.c",
        "function_or_symbol": "eth_start_xmit",
        "line": 100,
        "code_snippet": "...",
        "why_this_location_matters": "..."
      }]
    }
  ],
  "dismissed_concerns": [...]
}
```

### 3.4 双版本 Context 优化

```rust
// Stage 1, 2, 7: 使用完整 context（含 commit message）
let system_prompt = shared_context;

// Stage 3, 4, 5, 6: 使用精简版（不含 commit message）
let system_prompt = shared_context_no_log;
```

**原因**: Stage 3-6 专注于代码逻辑审查，不需要 commit message。精简版可节省约 10-20% 的 token。

**实现机制**:
```rust
// stage.rs 中宏定义
fn use_log_in_context(&self) -> bool {
    !((3..=6).contains(&$num))
}
```

---

## 4. Context 构建详解

### 4.1 构建流程

```
Phase 0: 子系统预选
    │
    ▼
PromptRegistry::build_context(selected_prompts)
    │
    ├─ 注入系统身份 + 日期事实 + 工具指导
    ├─ 加载选中的子系统指南（subsystem/*.md）
    └─ 加载选中的模式文件（patterns/*.md）
    │
    ▼
构建 dynamic_context
    │
    ├─ Git 元数据（SHA, baseline）
    ├─ Target Commit Diff（git show 输出）
    └─ Prefetch Context（Tree-sitter 预取代码）
    │
    ▼
组合最终 Context
    ├─ shared_context = static + dynamic
    └─ shared_context_no_log = static + dynamic_no_log
```

### 4.2 静态知识库构建

```rust
// prompts.rs 中 build_context() 方法
pub async fn build_context(
    &self,
    selected_prompts: Option<&[String]>,  // Phase 0 输出
) -> Result<(String, String)> {
    
    // 1. 注入系统身份
    content.push_str("你是 Linux 内核专家维护者...");
    
    // 2. 注入日期事实
    content.push_str(&format!("当前日期是 {}...", current_date));
    
    // 3. 注入工具指导
    content.push_str("批量并行调用工具...");
    
    // 4. 加载选中的子系统指南
    self.append_directory(&mut content, &subsystem_dir, |name| {
        if let Some(selected) = selected_prompts {
            selected.iter().any(|s| name == s)  // ← 只加载 Phase 0 选中的
        } else {
            true
        }
    });
    
    // 5. 加载选中的模式文件
    self.append_directory(&mut content, &patterns_dir, |name| { ... });
}
```

### 4.3 动态 Context 构建

```rust
// prompts.rs 中 Worker::run() 方法

// Git 元数据
let mut git_metadata = String::new();
git_metadata.push_str("Target Commit SHA: abc1234\n");
git_metadata.push_str("Baseline SHA: def5678\n");

// 完整 diff（含 commit message）
let mut dynamic_context = git_metadata.clone();
dynamic_context.push_str("\n\nTarget Commit:\n");
dynamic_context.push_str(&target_commit_diff);  // git show 输出

// 精简 diff（不含 commit message）
let mut dynamic_context_no_log = git_metadata.clone();
dynamic_context_no_log.push_str("\n\nTarget Commit Diff:\n");
dynamic_context_no_log.push_str(&target_commit_diff_only);  // 纯 diff

// 预取上下文
let prefetched = prefetch_context(worktree_path, &target_commit_diff).await;
dynamic_context.push_str("<pre_fetched_context>\n");
dynamic_context.push_str(&prefetched);
dynamic_context.push_str("</pre_fetched_context>\n");
```

### 4.4 最终 Context 组合

```rust
// prompts.rs#L685-L698
let shared_context = format!("{}{}", static_context, dynamic_context);
let shared_context_no_log = format!("{}{}", static_context, dynamic_context_no_log);
```

两个版本都包含：
- 身份 + 工具指导
- 选中的子系统指南 + 模式文件
- Git 元数据 + 预取代码

区别仅在于：是否包含 commit message。

---

## 5. Stage 验证逻辑

### 5.1 分层验证架构

```
Layer 1: JSON 解析 (parse_json_response)
    ↓
Layer 2: 数组结构校验 (required_stage_arrays)
    ↓
Layer 3: Stage 特定字段验证 (各 Stage 的 validate 方法)
    ↓
Layer 4: 验证反馈格式化 (format_validation_feedback)
```

### 5.2 ReviewStage Trait

```rust
// stage.rs
pub trait ReviewStage: Send + Sync {
    fn number(&self) -> u8;
    fn name(&self) -> &'static str;
    fn use_log_in_context(&self) -> bool { true }
    fn validate(&mut self, response: &AiResponse) -> Result<Value, ValidationError>;
    fn format_validation_feedback(&self, violation: &str) -> String { ... }
    fn handle_recitation_error(&mut self) -> Option<ErrorAction> { None }
}
```

### 5.3 JSON 解析容错

```rust
// stage.rs parse_json_response()
fn parse_json_response(response: &AiResponse) -> Result<Value, ValidationError> {
    let raw_text = response.content.as_deref().unwrap_or("");
    let cleaned = clean_json_string(raw_text);           // 移除转义字符
    let parsed = serde_json::from_str(&cleaned)         // 标准 JSON 解析
        .unwrap_or_else(|_| {
            let cands = find_json_candidates(raw_text);  // 容错：暴力提取 {...}
            cands.into_iter().last().unwrap_or(json!({}))
        });
    Ok(parsed)
}
```

三层容错：
1. `clean_json_string()` — 移除 JSON 字符串外的转义
2. `serde_json::from_str()` — 标准解析
3. `find_json_candidates()` — 通过括号匹配暴力提取 JSON 对象

### 5.4 数组结构校验

```rust
// stage.rs required_stage_arrays()
fn required_stage_arrays(value: &Value) -> Result<(&[Value], &[Value]), String> {
    let concerns = value.get("concerns")
        .and_then(Value::as_array)
        .ok_or_else(|| "JSON output is missing 'concerns' array".to_string())?;
    let dismissed_concerns = value.get("dismissed_concerns")
        .and_then(Value::as_array)
        .ok_or_else(|| "JSON output is missing 'dismissed_concerns' array".to_string())?;
    Ok((concerns.as_slice(), dismissed_concerns.as_slice()))
}
```

### 5.5 各 Stage 验证规则

| Stage | 验证规则 |
|-------|----------|
| 1-7 | 必须包含 `concerns` 和 `dismissed_concerns` 两个数组 |
| 8 | 同上 + 额外验证数组类型正确 |
| 9 | 只要求 `concerns` 数组 |
| 10 | 要求 `findings` 数组 |
| 11 | LKML 文本格式：必须含 commit 头、> 引用、评论 |

**Stage 11 特殊验证**：
```rust
fn validate_inline_format(content: &str) -> Result<(), String> {
    // 不能有 Markdown 代码块
    if content.lines().any(|l| l.trim_start().starts_with("```")) { ... }
    // 必须包含 '>' 引用块
    if !content.lines().any(|l| l.trim_start().starts_with(">")) { ... }
    // 必须以 'commit <hash>' 开头
    if !has_commit_header { ... }
    // 必须包含 'Author:' 行
    if !has_author_header { ... }
    // 必须有非头部字段的评论
    if !has_comments { ... }
}
```

### 5.6 验证重试机制

```rust
// session.rs SessionRunner::run()

match session.validate(&resp) {
    Ok(output) => return Ok(SessionResult { output, history, usage }),
    
    Err(ValidationError::FormatViolation(violation)) => {
        validation_attempts += 1;
        if validation_attempts >= max_validation_attempts {
            bail!("验证失败，已达最大重试次数")
        }
        // 生成反馈消息，追加到对话历史
        let feedback = session.format_validation_feedback(&violation);
        history.push(AiMessage { role: AiRole::User, content: Some(feedback) });
        // 继续循环，让 LLM 重试
    }
    
    Err(ValidationError::Fatal(err)) => bail!("致命错误: {}", err),
}
```

**错误类型:**
```rust
pub enum ValidationError {
    FormatViolation(String),  // 可重试的格式错误
    Fatal(String),            // 不可恢复的致命错误
}

pub enum ErrorAction {
    RetryWithFeedback(String),  // 追加反馈后重试
    Fail,                       // 立即终止
}
```

### 5.7 Stage 11 背诵错误处理

```rust
// stage.rs Stage11::handle_recitation_error()
fn handle_recitation_error(&mut self) -> Option<ErrorAction> {
    if !self.free_form_mode {
        self.free_form_mode = true;
        let fallback = "CRITICAL: 由于 RECITATION 策略违规...请改用自由文本模式";
        Some(ErrorAction::RetryWithFeedback(fallback.to_string()))
    } else {
        None  // 已降级为自由文本模式，无法进一步处理
    }
}
```

---

## 6. Phase 0 — 子系统预选

### 6.1 目标

Linux 内核有 **60+ 子系统指南**，每个指南包含 API 契约、不变量和常见 bug 模式。全量加载会爆 token，因此需要 LLM 预选。

### 6.2 执行流程

**Step 1: 读取子系统索引**
```rust
let subsystem_md_path = self.prompts.base_dir.join("subsystem/subsystem.md");
let subsystem_md = tokio::fs::read_to_string(&subsystem_md_path).await;
```

索引文件是一个大表格：
| Subsystem | Triggers | File |
|-----------|----------|------|
| Networking Core | `net/`, `skb_`, `sockets` | networking-core.md |
| Locking | `spin_lock*`, `mutex_*` | locking.md |
| RCU | `rcu*`, `call_rcu` | rcu.md |
| ... (60+ 行) | | |

**Step 2: 构造 Phase 0 Prompt**
```rust
let phase0_system = "你是 AI 助手，准备 Linux 内核补丁审查。
从索引中选择所有可能相关的子系统指南。
关键偏置规则：宁可多选，只有 100% 不相关才排除。
必须只返回 JSON 对象: {\"selected_prompts\": [\"networking.md\"]}";

let phase0_prompt = format!(
    "<subsystem_guide_index>\n{}\n</subsystem_guide_index>\n\n<patch>\n{}\n</patch>",
    subsystem_md,        // 整个索引表格
    target_commit_diff   // 当前 patch 的 git show 输出
);
```

**Step 3: 调用 LLM**
```rust
let req = AiRequest {
    system: Some(phase0_system),
    messages: vec![AiMessage { role: User, content: Some(phase0_prompt) }],
    temperature: Some(0.0),  // 确定性输出
    response_format: Some(Json { schema: Some(json_schema) }),  // 强制 JSON
};
```

**Step 4: 过滤 Stage 独占指南**
```rust
const STAGE_EXCLUSIVE_GUIDES: &[&str] = &["locking.md"];

// locking.md 只在 Stage 5 深度审查时加载
.filter(|name| !STAGE_EXCLUSIVE_GUIDES.contains(&name.as_str()))
```

### 6.3 输入输出示例

```
输入:
  system: "从索引中选相关指南，宁多勿少"
  user: <subsystem_guide_index> 60+ 指南表格 </subsystem_guide_index>
        <patch> git show 输出 </patch>

输出 (LLM 返回):
  {"selected_prompts": ["networking-core.md", "locking.md", "rcu.md"]}

最终结果:
  ["networking-core.md", "rcu.md"]  // locking.md 被过滤
```

---

## 7. Planning Phase

### 7.1 目标

决定 Stage 1-7 中哪些需要执行。Stage 1-3 必选，Stage 4-7 可选。

### 7.2 执行流程

**Step 1: 检查是否已手动指定**
```rust
if self.stages.is_none() {
    // 没有手动指定，需要 LLM 规划
}
```

**Step 2: 构造 Planning Prompt**
```rust
let planning_prompt = "分析 patch，决定哪些审查阶段需要执行：
- Stage 4: Resource management
- Stage 5: Locking and synchronization
- Stage 6: Security audit
- Stage 7: Hardware engineer's review

关键：宁可多跑，小 typo fix 可跳过。
Stages 1, 2, 3 总是执行，无需包含。
必须只返回 JSON: {\"relevant_stages\": [4, 5, 6, 7]}";
```

**Step 3: 使用完整 Context 调用 LLM**
```rust
let req = AiRequest {
    system: None,  // 一次性决策，不需要 system prompt
    messages: vec![AiMessage {
        role: User,
        content: Some(format!("{}\n\n{}", shared_context, planning_prompt)),
        //         ← 完整 context
        //                    ← 规划指令
    }],
    temperature: Some(0.0),
    response_format: Some(Json { schema: Some(json_schema) }),
};
```

**Step 4: 解析结果，决定 Stage 列表**
```rust
let mut stages = vec![1, 2, 3];  // 前三个必选
for v in arr {
    if let Some(n) = v.as_u64() && (4..=7).contains(&n) {
        stages.push(n as u8);  // LLM 选中的可选 stage
    }
}
// 例: LLM 返回 [4, 5, 6] → stages = [1, 2, 3, 4, 5, 6]
```

---

## 8. Prefetch Context 预取策略

### 8.1 核心常量

```rust
const MAX_PREFETCH_CHARS: usize = 200000;  // 预取总量上限：20万字符
```

### 8.2 两阶段预取流程

```
Phase 1: 本地分析 → Phase 2: 全局查找 → 渲染输出
```

### 8.3 Phase 1：本地分析

**Step 1: 解析 diff 获取修改行范围**
```rust
// 输入: unified diff
// 输出: HashMap<"file.c", [(start_line, end_line), ...]>
// 例: {"drivers/net/eth.c": [(100, 120), (250, 255)]}

// 自动合并相邻范围（间距 ≤ 10 行）
if r.0 <= last.1 + 10 {
    last.1 = std::cmp::max(last.1, r.1);
}
```

**Step 2: Tree-sitter 分析每个修改范围**
```rust
for &(start, end) in ranges {
    // 提取完整定义（函数、结构体、枚举、宏等）
    overlapping_definitions(&content, start, end)
    
    // 提取符号名
    extract_defined_names(&content, start, end)
    
    // 提取类型名
    extract_type_names(&content, start, end)
}
// 提取调用的函数
called_functions.extend(extract_called_functions(&content, ranges));
```

**Step 3: 智能过滤**

| 规则 | 说明 | 原因 |
|------|------|------|
| 已提取符号 | 定义在本地文件中已获取 | 避免重复 |
| 不透明类型 | 只有指针声明，不解引用成员 | 无需看定义 |
| `_ops` 后缀 | 操作结构体（vtable） | 太大且对审查无用 |
| 数量上限 | 最多 50 个符号 | 控制 git grep 范围 |

**Step 4: 超长定义截断**
```rust
if line_count > 200 {
    // 超过 200 行，只取修改点附近 ±100 行
    let center = (start_line + end_line) / 2;
    ranges.push((center.saturating_sub(100), min(center + 100, blk_end)));
}
```

### 8.4 Phase 2：全局查找

**Step 1: git grep 批量搜索**
```rust
// 用 | 连接最多 50 个符号，一次 git grep
let regex_pattern = format!(
    "^((struct|enum|union)\\s+({0})\\b|#define\\s+({0})\\b|...)",
    symbols.join("|")
);
// git grep -n -I -P -e <pattern> -- *.c *.h
```

**Step 2: 过滤噪声目录**
```rust
// 排除 tools/, samples/, Documentation/, scripts/, LICENSES/
// 这些目录包含用户态重实现（如玩具版 spin_lock）
```

**Step 3: 分类候选位置**
```rust
let is_priority = rel.starts_with("include/")   // include/ 目录
    || caller_dirs.iter()                       // 或与修改文件同目录
        .any(|d| rel.starts_with(d));

// 每个符号最多 32 个优先候选 + 32 个普通候选
```

**Step 4: 综合评分选最佳定义**

```
定义类型分:
  struct/union/enum 定义 + body: 100 分
  函数定义 + body:                90 分
  typedef:                        80 分
  #define:                        70 分
  仅前向声明:                      0 分（过滤）

接近度分:
  同目录:          +50 分
  include/ 下:     +40 分
  static 跨目录:   -200 分（强烈惩罚）
```

### 8.5 渲染输出

```rust
// 1. 按文件排序（修改文件优先）
// 2. 合并相邻范围（gap ≤ 3 行）
// 3. 格式: --- path:line (name) ---
//         代码块内容
// 4. 字符计数，到达 200K 停止

if current_chars + header.len() + block.len() + 1 > MAX_PREFETCH_CHARS {
    output.push_str("\n... (Context prefetch limits reached)\n");
    return Ok(output);
}
```

### 8.6 预取策略总结

| 决策 | 说明 |
|------|------|
| **200K 字符上限** | 约 5-8 万 tokens，控制 context 大小 |
| **Tree-sitter 而非正则** | 精确识别 C 语法结构 |
| **超长定义截断** | 超大结构体只取局部 |
| **不透明类型过滤** | `struct foo *bar` 且 `bar->x` 全是 priv_ 开头则跳过 |
| **git grep 批量搜索** | 一次搜索所有符号，避免 N 次独立调用 |
| **综合评分选最佳** | 避免选到用户态重实现 |
| **修改文件优先** | 确保核心上下文不被截断 |

---

## 9. 动态代码提取机制

### 9.1 双模式上下文获取

Stage 1-7 审查过程中，代码上下文采用 **"预置 + 动态"** 双模式获取：

```
┌────────────────────────────────────────────────────────────┐
│                    Context 获取模式                         │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  【模式 1: 预置 Context】（被动）                            │
│  ───────────────────────────────────────────               │
│  - Prefetch 预加载的函数体（Tree-sitter 分析）              │
│  - Git diff 完整输出                                        │
│  - 子系统指南（locking.md, rcu.md 等）                     │
│  - Git 元数据（SHA, baseline）                              │
│                                                            │
│  【模式 2: 动态提取】（主动）                               │
│  ───────────────────────────────────────────               │
│  - LLM 发现需要更多上下文时                                 │
│  - 通过调用工具主动获取                                     │
│  - 工具执行结果追加到对话历史                               │
│  - LLM 基于结果继续审查或再次调用工具                       │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### 9.2 工具调用流程

#### 流程概览

```
LLM 返回 → 包含 tool_calls → SessionRunner 处理 → 执行工具 → 结果回传 LLM
    ↑                                                              │
    └────────────── 继续审查循环 ←──────────────────────────────────┘
```

#### Step 1: 工具传递给 Session

在 [execute_stage()](file:///d:/AI/sashiko/sashiko-main/src/worker/prompts.rs#L1584-L1592) 中，所有 9 个工具被注入到 Session：

```rust
let mut session = ReviewStageSession::new(
    stage,
    system_prompt,
    user_prompt,
    clean_user_prompt,
    self.tools.clone(),  // ← 9 个工具全部注入
    self.temperature,
    self.context_tag.as_deref(),
);
```

#### Step 2: 工具声明暴露给 LLM

[ReviewStageSession::tools()](file:///d:/AI/sashiko/sashiko-main/src/worker/prompts.rs#L1784-L1786) 返回所有工具的 JSON Schema 声明：

```rust
impl LlmSession for ReviewStageSession {
    fn tools(&self) -> Option<Vec<AiTool>> {
        // 返回所有工具的 JSON Schema 声明
        Some(self.tools.get_declarations_generic())
    }
}
```

#### Step 3: SessionRunner 处理工具调用

[SessionRunner::run()](file:///d:/AI/sashiko/sashiko-main/src/ai/session.rs#L314-L330) 主循环中处理工具调用：

```rust
// 处理 LLM 返回的工具调用请求
if let Some(tool_calls) = &resp.tool_calls {
    // 执行所有工具调用（支持批量并行）
    let results = session.call_tools(tool_calls.clone()).await?;
    
    // 将工具执行结果追加到对话历史
    for (call_id, result) in results {
        let tool_msg = AiMessage {
            role: AiRole::Tool,
            content: Some(result.to_string()),  // ← 工具返回的代码内容
            tool_call_id: Some(call_id),
        };
        history.push(tool_msg);  // ← 追加到对话历史
    }
    continue;  // ← 继续循环，让 LLM 基于新结果继续审查
}
```

#### Step 4: 工具实际执行与缓存

[ToolBox::call()](file:///d:/AI/sashiko/sashiko-main/src/toolbox/mod.rs#L145-L179) 实现了参数规范化 + 结果缓存：

```rust
pub async fn call(&self, name: &str, args: Value) -> Result<Value> {
    // 规范化参数（填充默认值，增加缓存命中率）
    let normalized_args = self.registry.normalize_tool_args(&name_normalized, &args);
    
    // 检查缓存，避免重复调用
    let key = format!("{}:{}", name_normalized, serde_json::to_string(&normalized_args)?);
    {
        let cache = self.cache.read().unwrap();
        if let Some(val) = cache.get(&key) {
            return Ok(val.clone());  // ← 缓存命中，直接返回
        }
    }
    
    // 执行工具（如 git grep, git show 等）
    let res = self.registry.call(&name_normalized, args, &self.context).await?;
    
    // 缓存结果
    {
        let mut cache = self.cache.write().unwrap();
        cache.insert(key, res.clone());
    }
    
    Ok(res)
}
```

### 9.3 常用动态提取场景

#### 场景 1: 读取文件内容

**触发时机**: Stage 4（资源管理）发现某函数分配了内存，需要查看错误路径是否释放。

**LLM 调用**:
```json
{
  "tool": "git_read_files",
  "arguments": {
    "revision": "abc1234",
    "files": [
      {"path": "drivers/net/eth.c", "start_line": 200, "end_line": 280}
    ]
  }
}
```

**工具实现** — [git_read_files.rs#L80-L103](file:///d:/AI/sashiko/sashiko-main/src/toolbox/git_read_files.rs#L80-L103)：
```rust
async fn call(&self, args: Value, context: &SashikoToolContext) -> Result<Value> {
    let revision = args["revision"].as_str()?;
    let files = args["files"].as_array()?;
    
    for file_args in files {
        let path = file_args["path"].as_str()?;
        let start_line = file_args["start_line"].as_u64();
        let end_line = file_args["end_line"].as_u64();
        
        // 执行 git show 获取文件内容
        let output = Command::new("git")
            .arg("show")
            .arg(format!("{}:{}", revision, path))
            .output().await?;
        
        // 如果指定了行范围，截取对应部分
        // 返回代码内容给 LLM
    }
}
```

#### 场景 2: 搜索代码模式

**触发时机**: Stage 5（锁同步）怀疑某处可能有 AB-BA 死锁，需要搜索全局锁使用情况。

**LLM 调用**:
```json
{
  "tool": "git_grep",
  "arguments": {
    "revision": "abc1234",
    "pattern": "mutex_lock\\(&dev->mutex\\)",
    "path": "drivers/net/",
    "context_lines": 3
  }
}
```

**工具实现** — [git_grep.rs#L27-L49](file:///d:/AI/sashiko/sashiko-main/src/toolbox/git_grep.rs#L27-L49)：
```rust
async fn call(&self, args: Value, context: &SashikoToolContext) -> Result<Value> {
    // 执行 git grep
    let output = Command::new("git")
        .arg("grep")
        .arg("-n")           // 显示行号
        .arg("-C")           // 显示上下文
        .arg(context_lines.to_string())
        .arg("-e")
        .arg(pattern)
        .arg("--")
        .arg(path)
        .output().await?;
    
    // 返回匹配行及上下文
}
```

#### 场景 3: 查看 commit 历史

**触发时机**: Stage 2（实现验证）想确认某个函数的修改历史。

**LLM 调用**:
```json
{
  "tool": "git_show",
  "arguments": {
    "revision": "abc1234",
    "file": "drivers/net/eth.c"
  }
}
```

### 9.4 动态提取完整示例

以 Stage 5（Locking）发现 AB-BA 死锁为例，展示完整的工具调用链：

```
┌─────────────────────────────────────────────────────────────┐
│  Stage 5 (Locking) 动态提取示例                               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Turn 1: LLM 分析预置 Context                                │
│  ─────────────────────────────────────────                  │
│  发现 diff 中有 mutex_lock(&dev->mutex)                     │
│  但不确定:                                                   │
│  1. dev->mutex 还在哪里使用？                                │
│  2. 是否存在 AB-BA 死锁风险？                                │
│  3. 其他获取 dev->mutex 的函数持有什么锁？                   │
│                                                             │
│  → 决定调用 git_grep 搜索 mutex_lock(&dev->mutex)           │
│                                                             │
│  Turn 2: 工具执行结果返回                                    │
│  ─────────────────────────────────────────                  │
│  git_grep 结果:                                             │
│  drivers/net/eth.c:150: mutex_lock(&dev->mutex);           │
│  drivers/net/eth.c:250: mutex_lock(&dev->mutex);           │
│  drivers/net/eth.c:380: mutex_lock(&dev->mutex);           │
│                                                             │
│  → LLM 决定调用 git_read_files 读取 150 和 380 行的上下文   │
│                                                             │
│  Turn 3: 读取代码上下文                                     │
│  ─────────────────────────────────────────                  │
│  git_read_files 结果:                                       │
│  --- drivers/net/eth.c:140-160 ---                         │
│  void eth_open(struct net_device *dev) {                    │
│      mutex_lock(&dev->mutex);   // ← 150 行                │
│      spin_lock(&dev->lock);     // ← 持有 spinlock         │
│      ...                                                    │
│  }                                                          │
│                                                             │
│  --- drivers/net/eth.c:370-395 ---                         │
│  void eth_close(struct net_device *dev) {                   │
│      spin_lock(&dev->lock);     // ← 先获取 spinlock       │
│      mutex_lock(&dev->mutex);   // ← 380 行                │
│      ...                                                    │
│  }                                                          │
│                                                             │
│  → LLM 发现:                                                │
│  eth_open:  mutex → spin   (锁序: mutex → spin)             │
│  eth_close: spin → mutex   (锁序: spin → mutex)             │
│  ⚠️ AB-BA 死锁风险！                                        │
│                                                             │
│  → 生成 concern 并输出最终 JSON                              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 9.5 动态提取的优势

| 优势 | 说明 |
|------|------|
| **按需获取** | LLM 只在需要时才调用工具，避免浪费 token |
| **精准定位** | 可以指定具体文件、行范围、搜索模式 |
| **批量并行** | LLM 可以一次发起多个工具调用，减少回合数 |
| **结果缓存** | ToolBox 内部缓存相同参数的调用结果，避免重复执行 |
| **深度探索** | 可以链式调用：grep → read_files → grep 查看调用方 |

### 9.6 动态提取 vs 预置 Context

| 对比维度 | 预置 Context | 动态提取 |
|----------|--------------|----------|
| **触发时机** | Stage 执行前自动构建 | LLM 审查过程中按需调用 |
| **内容来源** | Tree-sitter 预取 + Git diff | 实时 git 命令执行 |
| **覆盖范围** | 修改文件 + 引用符号定义 | 任意文件和符号 |
| **Token 成本** | 已包含在 system prompt 中 | 额外 token（但缓存可复用） |
| **适用场景** | 基础审查、代码逻辑验证 | 深度探索、全局分析、历史追溯 |

---

## 10. 工具使用前置条件与工作原理

### 10.1 核心前提：Git 仓库已存在

**是的，必须先有一个 Git 仓库（如 Linux kernel）本地 clone 完成。**

Sashiko 的所有工具都直接执行 **原生 git 命令**（`git show`, `git grep`, `git blame` 等），它们不是从磁盘直接读取文件，而是通过 git 命令查询指定 commit 的内容。

```
┌────────────────────────────────────────────────────────────┐
│                    工具使用前置条件                          │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  【必要条件】                                               │
│  ───────────────────────────────────────────               │
│  ✅ 已 clone 目标仓库（如 Linux kernel）                     │
│  ✅ git binary 在系统 PATH 中可用                           │
│  ✅ 要审查的 commit SHA 存在于本地仓库中                     │
│     （如不存在，FetchAgent 会自动 fetch）                   │
│                                                            │
│  【工作目录】                                               │
│  ───────────────────────────────────────────               │
│  ✅ 工具在 GitWorktree 中执行                               │
│  ✅ worktree 是仓库某个 commit 的临时检出副本                │
│  ✅ 支持任意 commit 的代码查询（不限于 HEAD）               │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### 10.2 Git 仓库准备流程

#### Step 1: FetchAgent 自动拉取缺失的 commit

[FetchAgent](file:///d:/AI/sashiko/sashiko-main/src/fetcher.rs#L35-L88) 会自动确保需要审查的 commit 存在于本地仓库：

```rust
// fetcher.rs FetchAgent::run()
async fn run(mut self) {
    // 接收审查请求（含 commit_hash 和 repo_url）
    // 检查 commit 是否在本地存在
    // 如果不存在:
    //   1. 尝试 fetch 特定 commit（optimistic fetch）
    //   2. 失败则 fetch 全部 heads（fallback）
}
```

**检查逻辑** — [fetcher.rs#L121-L156](file:///d:/AI/sashiko/sashiko-main/src/fetcher.rs#L121-L156)：
```rust
for commit in &commits_to_check {
    if !self.is_present(commit).await {
        missing_commits.push(commit.clone());
    }
}

if !missing_commits.is_empty() {
    // 远程仓库: git fetch origin <commit_sha>
    // 本地仓库但 commit 缺失: 报错
    if let Some(url) = url_opt {
        self.fetch_commits(&remote_name, &missing_commits).await;
    }
}
```

#### Step 2: 创建 GitWorktree 临时工作目录

[GitWorktree::new()](file:///d:/AI/sashiko/sashiko-main/src/git_ops.rs#L61-L122) 为审查创建一个临时的 worktree：

```rust
// git_ops.rs GitWorktree::new()
pub async fn new(repo_path: &Path, commit_hash: &str, parent_dir: Option<&Path>) -> Result<Self> {
    // 1. 创建临时目录 (tempfile::Builder)
    let temp_dir = tempfile::Builder::new()
        .prefix("sashiko-worktree-")
        .tempdir_in(parent_dir)?;
    
    // 2. 使用 git worktree add 创建检出
    //    注意: --detach 模式，不创建分支
    let output = Command::new("git")
        .current_dir(repo_path)
        .args(["worktree", "add", "--detach", "--no-checkout", &path, commit_hash])
        .output().await?;
    
    // 3. 检出文件到 worktree
    let output = Command::new("git")
        .current_dir(&path)
        .args(["reset", "--hard", commit_hash])
        .output().await?;
    
    // 4. 返回 GitWorktree（包含 temp_dir，drop 时自动清理）
    Ok(Self { dir: Some(temp_dir), path, repo_path, is_managed: true })
}
```

**为什么用 worktree？**
- 不污染主仓库的工作目录
- 审查完成后自动清理（TempDir drop 时删除）
- 支持并行审查多个 commit

#### Step 3: 配置 SashikoToolContext

[ToolBox::new()](file:///d:/AI/sashiko/sashiko-main/src/toolbox/mod.rs#L73-L99) 为工具配置执行上下文：

```rust
// mod.rs ToolBox::new()
pub fn new(worktree_path: PathBuf, prompts_path: Option<PathBuf>) -> Self {
    let context = SashikoToolContext {
        worktree_path,              // ← worktree 路径，工具在此执行 git 命令
        prompts_path,              // ← review-prompt 知识库路径
        active_patch_files: RwLock::new(Vec::new()),  // ← 当前审查的文件列表
        virtual_head: RwLock::new(None),              // ← 虚拟 HEAD
        cache: Arc::new(RwLock::new(HashMap::new())), // ← 结果缓存
    };
    
    // 注册所有工具
    let mut registry = ToolRegistry::new();
    registry.register(git_read_files::GitReadFilesTool);
    registry.register(git_blame::GitBlameTool);
    registry.register(git_diff::GitDiffTool);
    registry.register(git_show::GitShowTool);
    registry.register(git_log::GitLogTool);
    registry.register(git_ls::GitLsTool);
    registry.register(git_grep::GitGrepTool);
    registry.register(git_find_files::GitFindFilesTool);
    if context.prompts_path.is_some() {
        registry.register(read_prompt::ReadPromptTool);
    }
}
```

### 10.3 工具执行原理

#### 核心机制：原生 git 命令

每个工具都是通过执行 **原生 git CLI 命令** 来获取代码信息，而不是直接读取文件：

```rust
// git_grep.rs GitGrepTool::call()
async fn call(&self, args: Value, context: &SashikoToolContext) -> Result<Value> {
    // 在 worktree 目录执行 git grep
    let mut cmd = Command::new("git");
    cmd.current_dir(&context.worktree_path)  // ← 关键: 在 worktree 中执行
        .arg("grep")
        .arg("-n")          // 显示行号
        .arg("-I")          // 忽略二进制文件
        .arg(format!("-C{}", context_lines))  // 上下文行数
        .arg(pattern)
        .arg(revision);     // ← 支持任意 commit SHA
    
    // 执行命令并返回结果
    let output = cmd.output().await?;
    // 解析输出...
}
```

#### 虚拟 HEAD 机制

[virtualize_ref()](file:///d:/AI/sashiko/sashiko-main/src/toolbox/mod.rs#L48-L57) 将 LLM 传入的 `HEAD` 引用替换为当前审查的 commit SHA：

```rust
// mod.rs SashikoToolContext::virtualize_ref()
pub fn virtualize_ref(&self, r: &str) -> String {
    let vhead = self.virtual_head.read().unwrap();
    if let Some(ref sha) = *vhead {
        // HEAD → 实际 commit SHA
        re.replace_all(r, format!("${{1}}{}${{2}}", sha)).into_owned()
    } else {
        r.to_string()
    }
}
```

这意味着：LLM 可以传入 `HEAD` 而不需要知道具体的 commit SHA，系统会自动转换。

#### 工具调用完整示例

以 `git_read_files` 为例，展示完整的调用链：

```
┌─────────────────────────────────────────────────────────────┐
│  git_read_files 工具调用完整流程                              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. LLM 发起调用                                            │
│  ─────────────────────────────────────────                  │
│  {                                                          │
│    "tool": "git_read_files",                                │
│    "arguments": {                                           │
│      "revision": "HEAD",                                    │
│      "files": [{"path": "drivers/net/eth.c",                 │
│                 "start_line": 100,                          │
│                 "end_line": 150}]                           │
│    }                                                        │
│  }                                                          │
│                                                             │
│  2. SessionRunner 处理                                       │
│  ─────────────────────────────────────────                  │
│  - 解析 tool_calls                                          │
│  - 调用 session.call_tools()                               │
│  - 内部调用 toolbox.call()                                  │
│                                                             │
│  3. ToolBox 执行                                             │
│  ─────────────────────────────────────────                  │
│  - virtualize_ref("HEAD") → "abc1234"  (替换为实际 SHA)     │
│  - normalize_args() → 补全默认参数                           │
│  - 检查缓存 → 未命中                                         │
│  - 执行 git 命令                                            │
│                                                             │
│  4. 实际 git 命令                                            │
│  ─────────────────────────────────────────                  │
│  cd /tmp/sashiko-worktree-xxx                               │
│  git show abc1234:drivers/net/eth.c                         │
│  ← 获取整个文件内容                                         │
│  ← 截取第 100-150 行                                        │
│                                                             │
│  5. 结果返回                                                 │
│  ─────────────────────────────────────────                  │
│  {                                                          │
│    "file": "drivers/net/eth.c",                             │
│    "lines": 100-150,                                        │
│    "content": "void eth_open(struct net_device *dev) {...}" │
│  }                                                          │
│                                                             │
│  6. 结果追加到对话历史                                       │
│  ─────────────────────────────────────────                  │
│  SessionRunner 将结果作为 AiRole::Tool 消息追加             │
│  LLM 基于新上下文继续审查                                   │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 10.4 工具 vs 传统文件读取的区别

| 对比维度 | 工具方式（git 命令） | 传统文件读取 |
|----------|----------------------|-------------|
| **执行环境** | worktree 目录 | 任意目录 |
| **数据来源** | `git show <sha>:<path>` | `fs::read_to_string()` |
| **支持的版本** | 任意 commit SHA | 仅当前版本 |
| **并行安全** | 天然支持（每个 worktree 独立） | 需加锁 |
| **缓存效率** | 基于 SHA 缓存，跨会话复用 | 基于路径，版本敏感 |
| **额外信息** | 可获取 blame、diff 等 git 元数据 | 仅限文件内容 |

### 10.5 典型使用场景

| 场景 | 使用的工具 | 前置条件 |
|------|-----------|----------|
| 读取当前修改的文件 | `git_read_files` | worktree 中存在该文件 |
| 搜索特定代码模式 | `git_grep` | git 仓库已 clone |
| 查看代码修改历史 | `git_show` / `git_log` | commit 存在于本地 |
| 查看代码 blame | `git_blame` | 文件存在于 commit 中 |
| 列出目录结构 | `git_ls` | 路径有效 |
| 查找文件名 | `git_find_files` | git 仓库已 clone |

### 10.6 初始化配置示例

```bash
# 1. Clone Linux kernel 仓库
git clone https://github.com/torvalds/linux.git /path/to/linux

# 2. 启动 Sashiko（配置文件中指定 repo 路径）
# config.yaml
review:
  repo_path: /path/to/linux
  worktree_dir: /tmp/sashiko-worktrees

# 3. Sashiko 自动:
#    - Fetch 缺失的 commit（如有需要）
#    - 创建 worktree
#    - 注入工具
#    - 开始审查
```

---

## 11. 关键文件索引

| 文件 | 职责 | 关键函数/结构 |
|------|------|--------------|
| `src/reviewer.rs` | 审查入口 | `review_patchset_task()` |
| `src/worker/prompts.rs` | Worker 核心 | `Worker::run()`, `PromptRegistry::build_context()`, `execute_stage()` |
| `src/worker/stage.rs` | Stage 定义 | `ReviewStage` trait, `Stage1-11`, `validate_stages_1_to_7()` |
| `src/ai/session.rs` | 会话引擎 | `SessionRunner`, `LlmSession` trait, `ValidationError` |
| `src/ai/mod.rs` | AI 接口 | `AiProvider`, `AiRequest`, `AiResponse` |
| `src/toolbox/mod.rs` | 工具集 | `ToolBox`, `SashikoToolContext` |
| `src/toolbox/framework.rs` | 工具框架 | `LlmTool<C>` trait, `ToolRegistry<C>` |
| `src/worker/prefetch.rs` | 预取逻辑 | `prefetch_context()`, `overlapping_definitions()` |
| `third_party/prompts/kernel/subsystem/` | 子系统指南 | 60+ `.md` 文件 |
| `review-prompts-main/kernel/subsystem/subsystem.md` | 子系统索引 | 触发条件与文件映射表 |

---

## 附录：完整执行流程

```
┌───────────────────────────────────────────────────────────────┐
│                    Phase 0: 子系统预选                          │
│                                                               │
│  输入:                                                        │
│  system: "从索引中选相关指南，宁多勿少"                           │
│  user: <subsystem_guide_index> 60+ 指南表格 </index>            │
│        <patch> git show 输出 </patch>                          │
│                                                               │
│  LLM 调用: temperature=0, JSON Schema 强制                      │
│                                                               │
│  输出: ["networking-core.md", "rcu.md"]                       │
└───────────────────────────┬───────────────────────────────────┘
                            │
                            ▼
┌───────────────────────────────────────────────────────────────┐
│                    Context 构建                                 │
│                                                               │
│  static_context:                                              │
│  ┌─────────────────────────────────────────────────────┐     │
│  │ "当前日期是..."                                       │     │
│  │ "你是 Linux 内核专家维护者"                           │     │
│  │ <global_review_guidelines>                            │     │
│  │   networking-core.md 全文                             │     │
│  │   rcu.md 全文                                         │     │
│  │ </global_review_guidelines>                           │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                               │
│  dynamic_context:                                            │
│  ┌─────────────────────────────────────────────────────┐     │
│  │ Git 元数据 (SHA, baseline)                            │     │
│  │ Target Commit (git show 完整输出)                     │     │
│  │ <pre_fetched_context> 预取的函数体 </pre_fetched_...>  │     │
│  └─────────────────────────────────────────────────────┘     │
│                                                               │
│  shared_context = static + dynamic  (含 commit log)           │
│  shared_context_no_log = static + dynamic_no_log  (不含)      │
└───────────────────────────┬───────────────────────────────────┘
                            │
                            ▼
┌───────────────────────────────────────────────────────────────┐
│                    Planning Phase                              │
│                                                               │
│  输入:                                                        │
│  user: shared_context + "决定 Stage 4-7 哪些需要执行"            │
│                                                               │
│  LLM 调用: temperature=0, JSON Schema 强制                      │
│                                                               │
│  输出: {"relevant_stages": [4, 5, 6, 7]}                       │
│                                                               │
│  最终计划: [1, 2, 3, 4, 5, 6, 7]                               │
└───────────────────────────┬───────────────────────────────────┘
                            │
                            ▼
┌───────────────────────────────────────────────────────────────┐
│                    Stage 1-7 并行执行                           │
│                                                               │
│  每个 Stage:                                                  │
│  system: shared_context (或 shared_context_no_log)            │
│  user:   stage_instruction + format_guidance                  │
│  tools:  [git_read_files, git_grep, git_show, ...]            │
│                                                               │
│  SessionRunner 对话循环:                                      │
│    LLM → tool_call → 执行工具 → 结果回传 LLM                  │
│    LLM → 最终 JSON → validate → 成功/重试                     │
│                                                               │
│  输出: concerns[] + dismissed_concerns[]                      │
└───────────────────────────┬───────────────────────────────────┘
                            │
                            ▼
┌───────────────────────────────────────────────────────────────┐
│                    Stage 8-11 顺序执行                          │
│                                                               │
│  Stage 8: 去重合并所有 concerns                                │
│  Stage 9: 冲突解决（concern vs dismissed）                     │
│  Stage 10: 验证 + 严重性评估 + 误报过滤                         │
│  Stage 11: LKML 格式报告生成                                   │
│                                                               │
│  输出: { findings: [...], review_inline: "..." }               │
└───────────────────────────────────────────────────────────────┘
```