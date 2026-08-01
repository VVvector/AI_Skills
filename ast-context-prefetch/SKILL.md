---
name: "ast-context-prefetch"
description: "基于 AST/tree-sitter 的动态 context 预取，用于 LLM 代码审查（diff → AST 符号提取 → git grep → 评分渲染）。在构建 patch 审查的 prefetch_context 或移植预取流水线时调用。"
---

# AST Context 预取

一种可复用的方法论，用于为 LLM 代码审查构建**动态、AST 感知的 context 预取**。适用于任何大型代码库（C/C++/Rust/TS…），在 LLM 开始调用工具**之前**，将"被 diff 触及的完整定义"注入 LLM system prompt。

开箱即用的实现位于本 SKILL.md 同级的 `scripts/prefetch.py`。

## 何时调用

- 用户要求构建 / 移植 / 重构用于 patch 审查的 `prefetch_context`
- 用户希望通过预注入相关代码定义来减少 LLM 工具调用轮次
- 用户需要为代码审查 LLM 设计基于 AST 的 context 窗口
- 用户提到：tree-sitter、AST 符号提取、diff → context、代码审查预取、动态 context 构建

## 核心原则

> **diff 用正则解析，源码用 AST 解析。** 正则仅对 diff 格式可靠；对于源码，始终使用真正的语法解析器（tree-sitter），才能区分*定义*与*前向声明*、*调用*、*参数名*。

## 快速开始

### 安装依赖

```bash
pip install -r .trae/skills/ast-context-prefetch/scripts/requirements.txt
# 即: pip install 'tree-sitter>=0.23' 'tree-sitter-c>=0.21'
```

### CLI 用法

```bash
# 从文件读取 diff
python .trae/skills/ast-context-prefetch/scripts/prefetch.py /path/to/worktree patch.diff

# 从 stdin 读取 diff（适合管道）
git -C /path/to/worktree show HEAD | python .trae/skills/ast-context-prefetch/scripts/prefetch.py /path/to/worktree -

# 输出可直接重定向到文件，再注入 system prompt
python .../prefetch.py /path/to/worktree patch.diff > prefetched_context.txt
```

### 作为库调用

```python
import sys
sys.path.insert(0, ".trae/skills/ast-context-prefetch/scripts")
from prefetch import prefetch_context
from pathlib import Path

diff = open("patch.diff").read()
prefetched = prefetch_context(Path("/path/to/worktree"), diff)

# 注入到 LLM system prompt
system_prompt = f"""
<pre_fetched_context>
以下 context 基于修改行自动预取。
如果不够，你必须使用可用工具探索源码。不要在不查看相关代码的情况下做假设。

{prefetched}
</pre_fetched_context>
"""
```

### 输出格式

```
--- drivers/net/eth.c:105 (my_func) ---
int my_func(struct net_device *dev, int flags) {
    ...
}

--- include/net/my_hdr.h:42 (struct my_struct) ---
struct my_struct {
    ...
};
```

## 两阶段流水线

```
diff 字符串
   │
   ▼
[Phase 1: 本地分析]  — 仅在修改文件内
   ├─ parse_diff_ranges         diff → 0-based 行范围（与 tree-sitter Point.row 对齐）
   ├─ overlapping_definitions   AST → 与 diff 重叠的顶层完整定义
   ├─ extract_defined_names     记录已定义符号（用于去重）
   ├─ extract_type_names        AST → 引用的类型标识符
   └─ extract_called_functions  AST → 修改行内的函数调用名
   │
   ▼  符号集合（去重 + 不透明过滤 + 上限 50）
[Phase 2: 全局查找]  — 跨文件定位真实定义
   ├─ git grep 批量搜索        一个 PCRE 正则匹配所有符号（避免 N 次往返）
   ├─ 过滤噪声目录             tools/ samples/ Documentation/ scripts/ LICENSES/
   └─ tree-sitter 评分         定义类型分 + 接近度分 → 最佳匹配
   │
   ▼
[渲染]  修改文件优先、合并相邻范围、200K 字符预算
   │
   ▼
注入 system prompt 的 <pre_fetched_context> 块
```

## 分步实现指南

所有代码引用均指向 `scripts/prefetch.py` 中的实际实现。

### Step 0 — 选择 grammar

根据目标语言选择对应的 tree-sitter Python grammar 包：

| 语言 | Python 包 | 目标节点类型 |
|------|----------|-------------|
| C    | `tree-sitter-c`    | `function_definition`, `struct_specifier`, `enum_specifier`, `union_specifier`, `declaration`, `type_definition`, `preproc_def`, `preproc_function_def` |
| C++  | `tree-sitter-cpp`  | + `class_specifier`, `namespace_definition` |
| Rust | `tree-sitter-rust` | `function_item`, `struct_item`, `enum_item`, `impl_item`, `trait_item`, `macro_definition` |
| TS/JS | `tree-sitter-typescript` / `tree-sitter-javascript` | `function_declaration`, `class_declaration`, `interface_declaration`, `type_alias_declaration` |

Parser 初始化（`prefetch.py` 中 `_get_parser()`）：

```python
from tree_sitter import Language, Parser
import tree_sitter_c

language = Language(tree_sitter_c.language())
parser = Parser(language)
```

### Step 1 — 解析 diff 为 0-based 行范围

对应 `prefetch.py: parse_diff_ranges()`。

```python
import re

CHUNK_HEADER_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# 0-based 至关重要：tree-sitter 的 Point.row 是 0-based。
# start_0 = start - 1; end_0 = start_0 + count - 1
# 合并间距 10 行以内的范围，避免把一个逻辑编辑拆碎。
```

### Step 2 — Phase 1：对每个修改文件做 AST 提取

对应 `prefetch.py: overlapping_definitions()`。

对每个 `.c`/`.h`（或相关扩展名）修改文件的每个 `(start, end)` 范围：

```python
tree = parser.parse(source.encode("utf-8"))
root = tree.root_node

# 关键：遍历 root.children（自顶向下），而非从 diff 点向上 walk。
# 向上 walk 只能找到一个 enclosing block，会遗漏同样重叠的兄弟定义。
for child in root.children:
    if child.end_point.row < start_line or child.start_point.row > end_line:
        continue
    if child.type not in TARGET_KINDS:
        continue
    blk_start = child.start_point.row
    blk_end = child.end_point.row
    # 超过 200 行的定义截断为修改点附近 ±100 行
    if blk_end - blk_start > 200:
        center = (start_line + end_line) // 2
        ranges.append((max(0, center - 100), min(center + 100, blk_end)))
    else:
        ranges.append((blk_start, blk_end))
```

**符号提取辅助函数**（均基于 AST，对应 `prefetch.py` 中的同名函数）：

| 函数 | 返回内容 | tree-sitter 机制 |
|------|---------|-----------------|
| `extract_defined_names`   | 本地已定义的函数/结构体/枚举/联合体名 | `child_by_field_name("name")` |
| `extract_type_names`      | diff 作用域内引用的类型标识符        | `descendant_for_point_range(Point(...), Point(...))` → 向上走到 enclosing definition → 递归子树找 `type_identifier` |
| `extract_called_functions`| 修改行内的函数调用名                | 递归找 `call_expression`，取 `function` 字段且为 `identifier`（跳过 `obj->method`） |

**AST 作用域不可靠时的降级** — 当 diff 点向上走到 root（文件作用域）、或落在巨大的 `struct`/`union` 体内、或 `node.has_error` 为 true 时，将类型提取限制在 diff 行内，而非整个 enclosing scope。

### Step 3 — 智能过滤

对应 `prefetch.py: find_opaque_types()` + 过滤逻辑。

- **去重**：从待查找集合中移除已在本地定义的符号
- **不透明类型**：仅作为 `struct X *var` 使用且成员从未被解引用（或仅 `priv` 前缀成员）的类型无审查信号 → 丢弃
- **vtable**：丢弃 `_ops` 后缀结构体（如 `net_device_ops`）— 太大且对审查无用
- **上限**：最多 50 个符号进入 Phase 2

### Step 4 — Phase 2：一次 git grep 批量搜索

对应 `prefetch.py: prefetch_context()` 中的 git grep 调用。

构造一个 PCRE 正则，一次匹配全部 50 个符号：

```
^((struct|enum|union)\s+(sym1|sym2|...)\b
 |#define\s+(sym1|sym2|...)\b
 |([a-zA-Z_][\w \t*]+\s+)?(sym1|sym2|...)\s*\()
```

执行：`git grep -n -I -P -e <pattern> -- *.c *.h`

**为何批量**：避免 N 次独立 IPC 往返；一个正则、一个进程、全部命中。

### Step 5 — 过滤与分类候选

对应 `prefetch.py: is_noisy_tree()` + 候选分类。

- 排除噪声目录：`tools/`、`samples/`、`Documentation/`、`scripts/`、`LICENSES/`（内核原语的用户态重实现如玩具版 `spin_lock` 会遮蔽真实定义）
- 分类：
  - **优先**（上限 32）：`include/` 下或与修改文件同目录
  - **普通**（上限 32）：其他

### Step 6 — 评分选最佳定义

对应 `prefetch.py: score_definition_node()` + `proximity_score()` + `best_definition_range()`。

对每个候选文件，用 tree-sitter 重新解析并 DFS 遍历整棵树，对每个节点评分：

**定义类型分**（验证是真实定义，而非前向声明/参数名）：

| 节点类型                                     | 条件                              | 分数  |
|--------------------------------------------|-----------------------------------|-------|
| `struct_specifier`/`union_specifier`/`enum_specifier` | name == sym 且有 body     | 100   |
| `function_definition`                      | name == sym 且有 body             | 90    |
| `type_definition` (typedef)                | typedef 名匹配 sym                | 80    |
| `preproc_def`/`preproc_function_def`       | name == sym                       | 70    |
| 前向声明 / 参数名 / 不匹配                    | —                                 | 0（过滤） |

**接近度分**：

- 与修改文件同目录：+50
- `include/` 下：+40
- `static` 函数跨目录：**-200**（强烈惩罚，避免选到其他 `.c` 文件中同名的 static 重实现）

**总分 = 类型分 + 接近度分**，取所有候选文件中的最大值。

### Step 7 — 预算感知渲染

对应 `prefetch.py: render_range_map()`。

```python
MAX_PREFETCH_CHARS = 200_000  # 约 5-8 万 tokens

# 1. 修改文件优先，定义-only 文件次之
# 2. 合并相邻范围（gap ≤ 3 行）
# 3. header 格式:
#    --- drivers/net/eth.c:105 (my_func) ---
#    <完整函数体>
# 4. 字符计数；超限即停并输出 "... (Context prefetch limits reached)"
```

### Step 8 — 注入 system prompt

将预取输出包裹在明确分隔的块中，并告知 LLM 这是起点而非穷尽：

```
<pre_fetched_context>
以下 context 基于修改行自动预取。
如果不够，你必须使用可用工具探索源码。不要在不查看相关代码的情况下做假设。

{prefetched}
</pre_fetched_context>
```

## 设计决策（及理由）

| 决策 | 理由 |
|------|------|
| 源码用 tree-sitter 而非正则 | 正则无法可靠区分定义与前向声明或参数名 |
| 遍历 root 子节点，而非从 diff 点向上 walk | 捕获所有与 diff 重叠的兄弟定义，而非仅最近的 enclosing block |
| 两次 AST 解析（Phase 1 + Phase 2 评分） | Phase 1 从修改文件低成本提取符号；Phase 2 验证候选是真实定义 |
| 一个 PCRE 批量 git grep | 一个进程处理 50 个符号；避免 N× IPC 延迟 |
| 评分 = 类型 + 接近度，而非首个命中 | 避免用户态重实现和无关 `.c` 中同名 static 函数 |
| 超过 200 行的定义截断为 ±100 | 巨型结构体（如 `netdevice.h`）会撑爆预算 |
| 不透明类型过滤 | `struct foo *bar` 从不解引用，无审查信号 |
| 丢弃 `_ops` 后缀 | vtable 很大且很少是 patch 审查焦点 |
| 200K 字符预算 | 在 LLM context window 与信息密度间取平衡 |
| 修改文件优先渲染 | 核心上下文必须在预算截断前保留 |

## 需避免的反模式

- ❌ 从 diff 点向上 walk 到单个 enclosing block — 会遗漏兄弟定义
- ❌ 用正则从源码提取符号 — 前向声明、参数、调用会有误报
- ❌ N 次独立 `git grep`，每符号一次 — 拖垮延迟
- ❌ 取首个 `git grep` 命中 — 很可能是用户态重实现
- ❌ 无 `static` 跨目录惩罚 — 会选到无关同名 static 函数
- ❌ 注入预取时不告知 LLM 可能不完整 — LLM 会误以为完备
- ❌ 1-based 行号与 tree-sitter 的 0-based `Point.row` 混用 — 隐蔽的 off-by-one

## 移植清单

移植到新语言或新代码库时（修改 `scripts/prefetch.py`）：

- [ ] 替换 `_get_parser()` 中的 grammar 包（如 `import tree_sitter_rust`）
- [ ] 更新 `TARGET_KINDS` 为新 grammar 的节点类型名
- [ ] 更新 `_function_name()` / `_typedef_names_match()` 适配新 grammar 的 declarator 结构
- [ ] 调整 `COMMON_C_WORDS` 为目标语言的关键字与常见类型别名
- [ ] 调整 `is_noisy_tree` 的噪声目录前缀
- [ ] 重新调参 `proximity_score` 的目录权重（如 `src/` vs `include/` vs `lib/`）
- [ ] 重新调参 `MAX_PREFETCH_CHARS` 适配目标 LLM 的 context window
- [ ] 调整 `DECL_RE` 不透明类型正则（如目标语言指针语法不同）

### 移植示例：C → Rust

```python
# 1. 安装: pip install tree-sitter-rust
# 2. 修改 _get_parser():
import tree_sitter_rust
language = Language(tree_sitter_rust.language())

# 3. 修改 TARGET_KINDS:
TARGET_KINDS = {
    "function_item", "struct_item", "enum_item",
    "impl_item", "trait_item", "macro_definition",
    "type_item", "const_item", "static_item",
}

# 4. 修改 COMMON_C_WORDS → COMMON_RUST_WORDS
# 5. 调整 is_noisy_tree 的噪声目录前缀
```

## 参考实现

完整实现位于 `scripts/prefetch.py`。核心函数索引：

| 函数 | 说明 |
|------|------|
| `prefetch_context()`            | 流水线主入口 |
| `parse_diff_ranges()`           | diff → 0-based 行范围 |
| `overlapping_definitions()`     | Phase 1 AST 提取重叠定义 |
| `extract_enclosing_block()`     | 取首个 enclosing block |
| `extract_defined_names()`       | 已定义符号名 |
| `extract_type_names()`          | 引用类型（含 scope 降级） |
| `extract_called_functions()`    | 修改行内的函数调用 |
| `find_opaque_types()`           | 不透明类型过滤 |
| `is_noisy_tree()`               | 噪声目录过滤 |
| `line_matches_symbol()`         | 词法边界匹配 |
| `score_definition_node()`       | 定义类型评分 |
| `score_best_in_file_for_sym()`  | 文件内最佳定义 |
| `proximity_score()`             | 接近度评分 |
| `best_definition_range()`       | Phase 2 跨文件选最佳 |
| `merge_ranges()`                | 范围合并 |
| `render_range_map()`            | 预算感知渲染 |

依赖：`tree-sitter>=0.23`、`tree-sitter-c>=0.21`（Python）。
