# ast-context-prefetch Skill — 输入输出规格

## 一、核心函数 `prefetch_context(worktree_path, diff)`

```
输入                                      输出
┌─────────────────────────┐              ┌──────────────────────────────────┐
│ worktree_path: Path     │              │ str                              │
│   代码仓库的工作目录     │   ──────►   │   预取的 context 字符串           │
│                         │              │   （≤200K 字符）                  │
│ diff: str               │              │                                  │
│   unified diff 文本     │              │ 格式:                            │
│   (git show / git diff) │              │   --- path:line (name) ---       │
└─────────────────────────┘              │   <代码块>                       │
                                         │   ...                            │
                                         └──────────────────────────────────┘
```

## 二、输入详解

| 参数 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `worktree_path` | `Path` | 审查目标的工作目录 | 必须是 git 仓库（Phase 2 需要 `git grep`）；含 `.c`/`.h` 源文件 |
| `diff` | `str` | `git show <commit>` 或 `git diff` 的输出 | unified diff 格式；脚本从中解析 `+++ b/file` 和 `@@ -x,y +a,b @@` |

### CLI 入口

```bash
python prefetch.py <worktree_path> <diff_file|-> [--md <output.md>]
# diff_file: 文件路径
# -        : 从 stdin 读取
# --md     : 可选，额外将 Markdown 报告写入指定文件（不影响 stdout）
```

## 三、输出详解

输出分为两种形式：**纯文本 stdout**（默认）和 **Markdown 落盘**（`--md` 启用）。

### 3.1 纯文本 stdout

输出是一个**纯文本字符串**，由若干代码块组成，每块带 header：

```
--- drivers/net/eth.c:105 (my_func) ---          ← 修改文件内的定义（Phase 1）
int my_func(struct net_device *dev, int flags) {
    ...
}

--- include/net/my_struct.h:42 (struct my_struct) ---  ← 跨文件查到的定义（Phase 2）
struct my_struct {
    ...
};

... (Context prefetch limits reached)             ← 超 200K 字符时截断标记
```

**Header 格式**：`--- <相对路径>:<起始行号(1-based)> (<符号名>) ---`

| 输出特征 | 说明 |
|---------|------|
| 排序 | 修改文件优先，定义-only 文件次之 |
| 合并 | 相邻范围 gap ≤ 3 行自动合并 |
| 截断 | 超 `MAX_PREFETCH_CHARS`(200K) 字符即停 |
| 符号名 | 仅当该块恰好含 1 个定义时显示 |

### 3.2 Markdown 落盘（--md <path>）

当提供 `--md` 参数时，除 stdout 外额外写入一份 Markdown 文件，并自动把原始 diff 保存到相邻 `.diff` 文件。Markdown 文件结构固定三段，便于程序化消费和人类 review；diff 文件便于审计、重跑、人工对比。

**文件命名规则**：

| `--md` 参数 | 生成的 MD 文件 | 自动生成的 diff 文件 |
|---|---|---|
| `prefetched.md` | `prefetched.md` | `prefetched.diff` |
| `prefetched_context.md` | `prefetched_context.md` | `prefetched_context.diff` |
| `out/prefetched.txt` | `out/prefetched.txt` | `out/prefetched.diff` |

> diff 文件路径 = `Path(md_path).with_suffix(".diff")`。

```markdown
# Prefetched Context

## Summary
- Worktree: `/path/to/worktree`
- Generated: `YYYY-MM-DD HH:MM:SS`
- Modified files: `N`
  - `file1.c`
  - `include/file2.h`
- Symbols looked up: `M`
- Blocks rendered: `K`
- Output chars: `X` / `MAX_PREFETCH_CHARS`
- Truncated: `yes | no`

## Context (可直接复制粘贴到 `<pre_fetched_context>` 块)

```
--- path:line (name) ---
<code block>
...
```

## Index（块索引，便于跳转到对应源码）
1. `path:start-end` — `(symbol_name or <unnamed block>)`
2. ...
```

**渲染函数**：`render_markdown_report(worktree_path, diff, plain_text, meta)`

**库调用包装**：`prefetch_context_to_md(worktree_path, diff)` → `(plain_text, md_text, meta)`

**meta 字段说明**（同时作为库调用返回值的一部分）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `symbols_looked_up` | int | Phase 2 实际经 git grep 查找的符号数 |
| `blocks` | list[tuple] | `(relative, start1, end1, name)` 列表 |
| `truncated` | bool | 是否因字符预算而截断 |
| `output_chars` | int | plain_text 的实际字符数 |
| `modified_files` | list[str] | diff 命中的文件列表 |

## 四、输出的消费方式

输出字符串注入 LLM system prompt 的 `<pre_fetched_context>` 块：

```
<pre_fetched_context>
以下 context 基于修改行自动预取。
如果不够，你必须使用可用工具探索源码。不要在不查看相关代码的情况下做假设。

{prefetch_context 的输出}
</pre_fetched_context>
```

注入逻辑参见 `scripts/prefetch.py` 的 `prefetch_context()` 返回值用法（SKILL.md "作为库调用" 小节）。

## 五、内部数据流（输入→输出经过哪些变换）

```
diff (str)
  │
  ├─ parse_diff_ranges ──► {文件名: [(start_0, end_0), ...]}
  │
  ├─ Phase 1: 对每个修改文件
  │   ├─ overlapping_definitions  ──► [(blk_start, blk_end), ...]  ──► range_map
  │   ├─ extract_defined_names    ──► {已定义符号}  ──► 去重
  │   ├─ extract_type_names       ──► {类型名}     ──┐
  │   └─ extract_called_functions ──► {函数名}     ──┤
  │                                                  ▼
  │   过滤: 去重 + 不透明类型 + _ops + 最多50个 → symbols (set)
  │
  ├─ Phase 2: git grep 批量搜索 symbols
  │   ├─ 候选分类 (priority/general, 各≤32)
  │   ├─ score_best_in_file_for_sym → (定义分, is_static, start, end)
  │   ├─ proximity_score            → 接近度分
  │   └─ best_definition_range      ──► (path, start, end)  ──► range_map
  │
  └─ render_range_map(range_map) ──► (plain_text, blocks, truncated)
                                                      │
                                                      ├─► stdout（纯文本）
                                                      └─► render_markdown_report() ──► .md 文件（--md 参数）
```

## 六、一句话总结

**输入**：git worktree 路径 + unified diff 文本
**输出**：一段 ≤200K 字符的代码 context 字符串（含被 diff 触及的完整函数/结构体/类型定义），直接注入 LLM system prompt；可选同时落盘一份结构化 Markdown 报告（含 Summary、Context、Index 三段）。
