#!/usr/bin/env python3
"""
ast_context_prefetch.py — 基于 tree-sitter 的动态 context 预取实现。

为 LLM 代码审查构建动态、AST 感知的 context 预取字符串：
  Phase 1: 解析 diff → 用 tree-sitter AST 从修改文件中提取符号
  Phase 2: 一次 git grep 批量搜索 → AST 评分选出最佳跨文件定义
  Render:  修改文件优先、相邻合并、200K 字符预算

用法:
    python prefetch.py <worktree_path> <diff_file> [--md <output.md>]
    python prefetch.py <worktree_path> - [--md <output.md>]   # 从 stdin 读取 diff

依赖:
    pip install 'tree-sitter>=0.23' 'tree-sitter-c>=0.21'
"""

from __future__ import annotations

import re
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import Optional

try:
    from tree_sitter import Language, Parser, Point
    import tree_sitter_c
except ImportError as e:
    raise SystemExit(
        "缺少依赖。请安装:\n"
        "  pip install 'tree-sitter>=0.23' 'tree-sitter-c>=0.21'"
    ) from e


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MAX_PREFETCH_CHARS = 200_000          # 渲染输出字符上限（约 5-8 万 tokens）
SYMBOL_CAP = 50                       # Phase 2 最多查找的符号数
CANDIDATE_CAP = 32                    # 每个符号每类的候选位置上限
OVERSIZE_LINE_THRESHOLD = 200         # 定义超过此行数则截断
OVERSIZE_HALF_WINDOW = 100            # 截断时取修改点 ± 此行数
ADJACENT_RANGE_GAP = 10               # diff 行范围合并间距
RENDER_RANGE_GAP = 3                  # 渲染时相邻范围合并间距
MAX_FILES_PER_SYMBOL = 3              # 跨文件同分时最多展示的文件数（预算控制）

# Phase 1.5: static 函数 caller 预取的预算控制
# 仅对 static 函数生效（caller 必在同文件，数量天然可控）；
# 非 static 函数的 caller 跨文件、可能成百上千，仍交 LLM 按需查。
MAX_CALLERS_PER_STATIC_FN = 8      # 每个 static 函数最多抓的 caller 数
CALLER_OVERSIZE_THRESHOLD = 100    # caller 超过此行数则截断
CALLER_OVERSIZE_HALF_WINDOW = 50   # 截断时取调用点 ± 此行数

# Phase 1 目标 AST 节点类型（顶层定义）
TARGET_KINDS = {
    "function_definition",
    "struct_specifier",
    "enum_specifier",
    "union_specifier",
    "declaration",
    "type_definition",
    "preproc_def",
    "preproc_function_def",
}

# extract_type_names 中向上查找 enclosing definition 的目标类型
SCOPE_KINDS_FOR_TYPE_EXTRACTION = {
    "function_definition",
    "struct_specifier",
    "union_specifier",
    "enum_specifier",
    "type_definition",
}

# 噪声目录前缀（用户态重实现，会遮蔽真实定义）
NOISY_PREFIXES = (
    "/tools/",
    "/samples/",
    "/Documentation/",
    "/scripts/",
    "/LICENSES/",
)

# C 语言常见关键字与类型别名，提取符号时过滤
COMMON_C_WORDS = {
    "int", "char", "void", "long", "short", "unsigned", "signed",
    "struct", "union", "enum", "typedef", "static", "const", "volatile",
    "if", "else", "for", "while", "do", "switch", "case", "default",
    "return", "break", "continue", "goto", "sizeof", "true", "false",
    "NULL", "inline", "extern", "register", "auto", "restrict",
    "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "bool", "size_t", "ssize_t", "pid_t", "uid_t", "gid_t", "off_t",
    "ret", "err", "len", "size", "res", "tmp", "val", "ptr", "idx", "out",
}


# ---------------------------------------------------------------------------
# Parser 单例（避免重复初始化）
# ---------------------------------------------------------------------------

_parser: Optional[Parser] = None


def _get_parser() -> Parser:
    global _parser
    if _parser is None:
        language = Language(tree_sitter_c.language())
        _parser = Parser(language)
    return _parser


def _parse(source: str):
    """解析 C 源码，返回 tree-sitter Tree。"""
    return _get_parser().parse(source.encode("utf-8"))


def _row(point) -> int:
    """从 Point 对象提取行号（兼容多种 tree-sitter Python 版本）。"""
    if hasattr(point, "row"):
        return point.row
    return point[0]


def _text(node) -> str:
    """安全获取节点文本。"""
    try:
        return node.text.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Step 1: parse_diff_ranges — 解析 unified diff → 0-based 行范围
# ---------------------------------------------------------------------------

CHUNK_HEADER_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """解析 unified diff，返回 {文件名: [(start_0, end_0), ...]}。

    行号转为 0-based 以与 tree-sitter 的 Point.row 对齐。
    相邻范围（间距 ≤ ADJACENT_RANGE_GAP）自动合并。
    """
    files: dict[str, list[tuple[int, int]]] = defaultdict(list)
    current_file: Optional[str] = None

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):]
            files.setdefault(current_file, [])
        elif line.startswith("@@") and current_file is not None:
            m = CHUNK_HEADER_RE.match(line)
            if m:
                start = int(m.group(1) or 1)
                count = int(m.group(2) or 1)
                if count > 0:
                    start_0 = start - 1
                    end_0 = start_0 + count - 1
                    files[current_file].append((start_0, end_0))

    # 合并相邻范围
    for fname, ranges in files.items():
        ranges.sort()
        merged: list[tuple[int, int]] = []
        for r in ranges:
            if merged and r[0] <= merged[-1][1] + ADJACENT_RANGE_GAP:
                merged[-1] = (merged[-1][0], max(merged[-1][1], r[1]))
            else:
                merged.append(r)
        files[fname] = merged
    return dict(files)


# ---------------------------------------------------------------------------
# Step 2: Phase 1 — AST 提取辅助函数
# ---------------------------------------------------------------------------

def overlapping_definitions(source: str, start_line: int, end_line: int) -> list[tuple[int, int]]:
    """收集与 [start_line, end_line] 重叠的顶层定义的行范围。

    遍历 root 的直接子节点（自顶向下），而非从 diff 点向上 walk。
    向上 walk 只能找到一个 enclosing block，会遗漏同样重叠的兄弟定义。
    """
    tree = _parse(source)
    root = tree.root_node
    ranges: list[tuple[int, int]] = []

    for child in root.children:
        if _row(child.end_point) < start_line or _row(child.start_point) > end_line:
            continue
        if child.type not in TARGET_KINDS:
            continue
        blk_start = _row(child.start_point)
        blk_end = _row(child.end_point)
        if blk_end - blk_start > OVERSIZE_LINE_THRESHOLD:
            # 超长定义只取修改点附近 ±OVERSIZE_HALF_WINDOW 行
            center = (start_line + end_line) // 2
            ranges.append((
                max(0, center - OVERSIZE_HALF_WINDOW),
                min(center + OVERSIZE_HALF_WINDOW, blk_end),
            ))
        else:
            ranges.append((blk_start, blk_end))
    return ranges


def extract_enclosing_block(
    source: str, start_line: int, end_line: int
) -> Optional[tuple[str, Optional[str]]]:
    """返回第一个重叠定义的 (块文本, 符号名)。"""
    defs = overlapping_definitions(source, start_line, end_line)
    if not defs:
        return None
    blk_start, blk_end = defs[0]
    lines = source.splitlines()
    clamped_end = min(blk_end, len(lines) - 1)
    if clamped_end < blk_start or blk_start >= len(lines):
        return None
    text = "\n".join(lines[blk_start:clamped_end + 1])
    names = extract_defined_names(source, blk_start, clamped_end)
    name = next(iter(names)) if len(names) == 1 else None
    return text, name


def _function_name(node) -> Optional[str]:
    """沿 declarator 链向下找到函数标识符。"""
    cur = node.child_by_field_name("declarator")
    while cur is not None:
        if cur.type == "identifier":
            return cur.text.decode("utf-8", errors="replace")
        if cur.type in ("function_declarator", "pointer_declarator", "parenthesized_declarator"):
            cur = cur.child_by_field_name("declarator")
        else:
            return None
    return None


def extract_defined_names(source: str, start_line: int, end_line: int) -> set[str]:
    """提取与范围重叠的函数/结构体/枚举/联合体定义名。"""
    tree = _parse(source)
    root = tree.root_node
    names: set[str] = set()

    for child in root.children:
        if _row(child.end_point) < start_line or _row(child.start_point) > end_line:
            continue
        name: Optional[str] = None
        if child.type == "function_definition":
            name = _function_name(child)
        elif child.type in ("struct_specifier", "enum_specifier", "union_specifier"):
            n = child.child_by_field_name("name")
            if n is not None:
                name = _text(n)
        if name:
            names.add(name)
    return names


def extract_type_names(source: str, start_line: int, end_line: int) -> set[str]:
    """提取修改行范围（及其周围）引用的 C 类型标识符。

    通过 descendant_for_point_range 定位 diff 范围对应的最小 AST 节点，
    向上 walk 到 enclosing definition，然后递归遍历子树收集 type_identifier。
    """
    tree = _parse(source)
    root = tree.root_node

    try:
        scope = root.descendant_for_point_range(
            Point(start_line, 0), Point(end_line, 0x7FFFFFFF)
        )
    except Exception:
        scope = None
    if scope is None:
        return set()

    # 向上走到 enclosing definition
    hit_root = False
    cur = scope
    while cur is not None and cur.type not in SCOPE_KINDS_FOR_TYPE_EXTRACTION:
        parent = cur.parent
        if parent is None:
            hit_root = True
            break
        cur = parent

    scope_node = cur if cur is not None and not hit_root else root

    types: set[str] = set()

    def walk(n, bounds: Optional[tuple[int, int]]):
        if bounds is not None:
            lo, hi = bounds
            if _row(n.end_point) < lo or _row(n.start_point) > hi:
                return
        if n.type == "type_identifier":
            text = _text(n)
            if len(text) >= 3 and text not in COMMON_C_WORDS:
                types.add(text)
        for child in n.children:
            walk(child, bounds)

    # 当走到 root、处于 struct/union 作用域、或 AST 有错误时，
    # 限制遍历范围只在 diff 行内，避免拉入无关类型
    bounds: Optional[tuple[int, int]] = None
    if (hit_root
        or scope_node.type in ("struct_specifier", "union_specifier")
        or scope_node.has_error):
        bounds = (start_line, end_line)
    walk(scope_node, bounds)
    return types


def extract_called_functions(source: str, diff_ranges: list[tuple[int, int]]) -> set[str]:
    """从修改行内提取函数调用名（仅直接调用，跳过 obj->method）。"""
    tree = _parse(source)
    funcs: set[str] = set()

    def collect(node):
        if node.type == "call_expression":
            row = _row(node.start_point)
            in_diff = any(s <= row <= e for s, e in diff_ranges)
            if in_diff:
                func = node.child_by_field_name("function")
                if func is not None and func.type == "identifier":
                    name = _text(func)
                    if len(name) >= 3 and name not in COMMON_C_WORDS:
                        funcs.add(name)
        for child in node.children:
            collect(child)

    collect(tree.root_node)
    return funcs


# ---------------------------------------------------------------------------
# Step 2.5: Phase 1.5 — static 函数的 caller 预取
# ---------------------------------------------------------------------------
#
# 设计理由（与 Phase 2 git grep 的区别）：
#   - static 函数的 caller 必然在同文件内（C 语言可见性保证），数量可控
#   - 非 static 函数的 caller 跨文件、可能成百上千，仍交 LLM 按需查
#
# 补上两个盲区：
#   1. caller 函数体 — 让 LLM 判断 caller 是否容忍 modified fn 的新行为
#      （如新增的提前 return、新增的锁、改变的返回值契约）
#   2. caller 传给 modified fn 的回调参数实现 — 函数指针调用盲区
#      （extract_called_functions 跳过函数指针调用，只有从 caller 侧
#       看参数才能发现回调函数名）


def extract_modified_static_functions(
    source: str, diff_ranges: list[tuple[int, int]]
) -> list[tuple[str, int, int]]:
    """返回与 diff 重叠的 static 函数定义 (name, start, end) 列表。

    仅返回含 static 存储类说明符的 function_definition。
    非 static 函数的 caller 跨文件、数量不可控，不在此阶段预取。
    """
    tree = _parse(source)
    root = tree.root_node
    results: list[tuple[str, int, int]] = []
    for child in root.children:
        if child.type != "function_definition":
            continue
        blk_start = _row(child.start_point)
        blk_end = _row(child.end_point)
        if not any(blk_start <= e and blk_end >= s for s, e in diff_ranges):
            continue
        if not has_static_storage(child):
            continue
        name = _function_name(child)
        if name:
            results.append((name, blk_start, blk_end))
    return results


def _collect_top_level_function_names(source: str) -> dict[str, tuple[int, int]]:
    """收集文件内所有顶层函数定义的 {name: (start_row, end_row)}。

    用于验证 caller 传给 modified fn 的参数是否是同文件函数名（回调）。
    """
    tree = _parse(source)
    root = tree.root_node
    result: dict[str, tuple[int, int]] = {}
    for child in root.children:
        if child.type != "function_definition":
            continue
        name = _function_name(child)
        if name:
            result[name] = (_row(child.start_point), _row(child.end_point))
    return result


def _extract_callback_args(call_node) -> list[str]:
    """从 call_expression 的参数中提取函数指针参数名。

    仅提取类型为 bare identifier 的参数（函数名直接作为参数传递）。
    跳过 call_expression、string_literal、number 等非标识符参数。
    调用方需在同文件函数表中验证这些标识符是否真是函数名。
    """
    args_node = call_node.child_by_field_name("arguments")
    if args_node is None:
        return []
    callbacks: list[str] = []
    for child in args_node.children:
        if child.type == "identifier":
            name = _text(child)
            if len(name) >= 3 and name not in COMMON_C_WORDS:
                callbacks.append(name)
    return callbacks


def find_callers_in_file(
    source: str, target_fn: str
) -> list[tuple[int, int, int, list[str]]]:
    """在同文件内查找调用 target_fn 的函数（callers）。

    返回 [(caller_start, caller_end, call_site_row, callback_args), ...]
      - call_site_row: 调用 target_fn 的行号，用于过长 caller 截断定位
      - callback_args: caller 传给 target_fn 的函数指针参数名列表

    去重：同一 caller 多次调用 target_fn 只返回一次（取首个调用点）。
    """
    tree = _parse(source)
    root = tree.root_node
    raw: list[tuple[int, int, int, list[str]]] = []

    def visit(node):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if (func is not None
                    and func.type == "identifier"
                    and _text(func) == target_fn):
                # 向上找到包含此调用的最近 function_definition
                caller_node = node
                while (caller_node is not None
                        and caller_node.type != "function_definition"):
                    caller_node = caller_node.parent
                if caller_node is not None:
                    caller_start = _row(caller_node.start_point)
                    caller_end = _row(caller_node.end_point)
                    call_row = _row(node.start_point)
                    callbacks = _extract_callback_args(node)
                    raw.append((caller_start, caller_end, call_row, callbacks))
        for child in node.children:
            visit(child)

    visit(root)

    # 去重：同一 caller 函数只保留首次出现
    seen: set[tuple[int, int]] = set()
    unique: list[tuple[int, int, int, list[str]]] = []
    for cs, ce, cr, cbs in raw:
        key = (cs, ce)
        if key not in seen:
            seen.add(key)
            unique.append((cs, ce, cr, cbs))
    return unique


# ---------------------------------------------------------------------------
# Step 3: 不透明类型过滤
# ---------------------------------------------------------------------------

DECL_RE = re.compile(r"struct\s+(\w+)\s+\*(\w+)")


def find_opaque_types(
    types: set[str],
    file_ranges: dict[str, list[tuple[int, int]]],
    worktree_path: Path,
) -> set[str]:
    """识别仅作为不透明容器使用的类型。

    若某类型在所有修改文件中：
      - 没有变量解引用其成员（var->member），或
      - 所有被解引用的成员名都包含 "priv"
    则视为不透明类型，无需查看其定义。
    """
    if not types:
        return set()

    type_members: dict[str, set[str]] = {t: set() for t in types}

    for fname in file_ranges:
        fpath = worktree_path / fname
        if not fpath.exists():
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        var_to_type: list[tuple[str, str]] = []
        for m in DECL_RE.finditer(content):
            type_name, var_name = m.group(1), m.group(2)
            if type_name in type_members:
                var_to_type.append((var_name, type_name))

        for var, typ in var_to_type:
            pattern = re.compile(rf"{re.escape(var)}\s*->\s*(\w+)")
            for m in pattern.finditer(content):
                type_members[typ].add(m.group(1))

    return {
        t for t, members in type_members.items()
        if not members or all("priv" in m for m in members)
    }


# ---------------------------------------------------------------------------
# Step 4: Phase 2 — git grep + AST 评分
# ---------------------------------------------------------------------------

def is_noisy_tree(path_str: str) -> bool:
    """排除噪声目录（用户态重实现会遮蔽真实定义）。"""
    # Windows 兼容：Path.__str__() 返回反斜杠，统一成正斜杠匹配 NOISY_PREFIXES
    path_str = path_str.replace("\\", "/")
    return any(p in path_str for p in NOISY_PREFIXES)


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def line_matches_symbol(line: str, sym: str) -> bool:
    """词法边界匹配：sym 在 line 中出现且两侧非标识符字符。"""
    start = 0
    while True:
        idx = line.find(sym, start)
        if idx == -1:
            return False
        end = idx + len(sym)
        before_ok = idx == 0 or not _is_ident_char(line[idx - 1])
        after_ok = end >= len(line) or not _is_ident_char(line[end])
        if before_ok and after_ok:
            return True
        start = end


def _typedef_names_match(node, sym: str) -> bool:
    """检查 typedef 节点是否定义了 sym。"""
    for child in node.children:
        if child.type == "type_identifier" and _text(child) == sym:
            return True
    return False


def has_static_storage(node) -> bool:
    """检查节点是否含 static 存储类说明符。"""
    for child in node.children:
        if child.type == "storage_class_specifier" and _text(child) == "static":
            return True
    return False


def score_definition_node(node, sym: str) -> int:
    """对候选定义节点评分。0 表示非真实定义（前向声明、参数名等）。

    评分体系:
      struct/union/enum 定义 + body:  100
      函数定义 + body:                 90
      typedef:                         80
      #define:                        70
      仅前向声明 / 不匹配:              0（过滤）
    """
    kind = node.type

    def names_symbol(field: str) -> bool:
        n = node.child_by_field_name(field)
        return n is not None and _text(n) == sym

    has_body = node.child_by_field_name("body") is not None

    if kind in ("struct_specifier", "union_specifier", "enum_specifier"):
        if not names_symbol("name"):
            return 0
        return 100 if has_body else 0
    if kind == "function_definition":
        declared = _function_name(node)
        if declared != sym:
            return 0
        return 90 if has_body else 0
    if kind in ("preproc_def", "preproc_function_def"):
        return 70 if names_symbol("name") else 0
    if kind == "type_definition":
        return 80 if _typedef_names_match(node, sym) else 0
    return 0


def score_best_in_file_for_sym(
    content: str, sym: str
) -> list[tuple[int, bool, int, int]]:
    """解析文件，返回 sym 的所有最高分定义候选。

    返回 [(score, is_static, start_line, end_line), ...]。
    通常 1 个；#ifdef 多分支场景（如同文件内 CONFIG=y/#else 两版实现）会返回多个，
    由调用方决定是否都展示。

    设计理由：静态分析无法知道宏配置值，无法判断哪个分支是"真实实现"。
    保留所有同分候选让 LLM 自行决策，避免评分系统替 LLM 做错误选择。
    """
    tree = _parse(content)
    best_score = 0
    best_nodes: list = []

    # 深度优先遍历所有节点，收集所有最高分候选
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        score = score_definition_node(node, sym)
        if score > 0:
            if score > best_score:
                best_score = score
                best_nodes = [node]       # 新最高分，重置列表
            elif score == best_score:
                best_nodes.append(node)   # 同分，追加（保留 #ifdef 多分支）
        stack.extend(node.children)

    if not best_nodes or best_score == 0:
        return []

    results = []
    for node in best_nodes:
        is_static = has_static_storage(node)
        start = _row(node.start_point)
        end = _row(node.end_point)
        if end - start > OVERSIZE_LINE_THRESHOLD:
            end = min(start + OVERSIZE_LINE_THRESHOLD, end)
        results.append((best_score, is_static, start, end))
    return results


def _common_prefix_len(a: str, b: str) -> int:
    """两个路径的最长公共前缀段数。"""
    return sum(1 for x, y in zip(a.split("/"), b.split("/")) if x == y)


def proximity_score(def_path: str, is_static: bool, caller_dirs: set[str]) -> int:
    """接近度评分。

    static .c 定义在调用者目录外几乎肯定是错误匹配（同名重实现）。
    """
    # Windows 兼容：rel_path 在 Windows 上为反斜杠，统一成正斜杠
    def_path = def_path.replace("\\", "/")
    def_dir = def_path.rsplit("/", 1)[0] if "/" in def_path else ""

    if is_static and def_path.endswith(".c"):
        if def_dir not in caller_dirs:
            return -200

    if def_dir in caller_dirs:
        return 50

    if def_path.startswith("include/"):
        return 40

    # 回退到与任意调用者目录的最长公共路径前缀
    return max(
        (_common_prefix_len(def_dir, cd) for cd in caller_dirs),
        default=0,
    )


def best_definition_range(
    sym: str,
    hits: list[tuple[Path, int]],
    worktree_path: Path,
    caller_dirs: set[str],
) -> list[tuple[Path, int, int]]:
    """在所有候选文件中选出 sym 的最佳定义范围列表。

    总分 = 定义类型分 + 接近度分。

    返回 [(path, start, end), ...]：
      - 评分不同时：只返回最高分文件内的所有候选（同文件 #ifdef 多分支都保留）
      - 评分相同且为正分时：所有同分文件都返回（跨文件无法区分=都给 LLM）
      - 负分文件（如 static 跨目录 -200）不入选

    设计原则：评分能区分时靠评分（避免爆炸），评分不能区分时都给 LLM（保信息）。
    预算控制：跨文件同分最多展示 MAX_FILES_PER_SYMBOL 个文件。
    """
    seen: set[Path] = set()
    # 收集每个文件的 (total_score, path, [(start, end), ...])
    file_results: list[tuple[int, Path, list[tuple[int, int]]]] = []

    for path, _line in hits:
        if path in seen:
            continue
        seen.add(path)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        results = score_best_in_file_for_sym(content, sym)
        if not results:
            continue
        # 同文件内所有候选同分（由 score_best_in_file_for_sym 保证）
        def_score = results[0][0]
        is_static = results[0][1]
        try:
            rel_path = str(path.relative_to(worktree_path))
        except ValueError:
            rel_path = str(path)
        prox = proximity_score(rel_path, is_static, caller_dirs)
        total = def_score + prox
        # 负分文件不入选（如 static 跨目录 -200，说明几乎肯定是同名重实现）
        if total <= 0:
            continue
        ranges = [(r[2], r[3]) for r in results]
        file_results.append((total, path, ranges))

    if not file_results:
        return []

    # 找最高总分
    max_total = max(fr[0] for fr in file_results)
    # 所有同最高分的文件（最多 MAX_FILES_PER_SYMBOL 个，按路径字典序稳定排序）
    best_files = [fr for fr in file_results if fr[0] == max_total]
    best_files.sort(key=lambda x: str(x[1]))
    best_files = best_files[:MAX_FILES_PER_SYMBOL]

    # 收集所有候选范围（可能跨多个文件，每个文件可能多个候选）
    output: list[tuple[Path, int, int]] = []
    for _score, path, ranges in best_files:
        for (start, end) in ranges:
            output.append((path, start, end))
    return output


# ---------------------------------------------------------------------------
# Step 5: 渲染输出
# ---------------------------------------------------------------------------

def merge_ranges(ranges: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    """合并相邻范围（gap 行以内的视为一段）。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + gap + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _detect_preproc_anno(lines: list[str], start: int) -> Optional[str]:
    """检查 start 行上方最近的 preproc 指令，返回条件标注。

    用于在 header 中标注 #ifdef 多分支候选的条件上下文，让 LLM 知道
    每个候选位于哪个条件分支（如 "#ifdef CONFIG_NET_NS" 或 "#else of CONFIG_NET_NS"）。

    采用字符串匹配（非 AST），因为 preproc 指令在行首，不易误匹配。
    向上最多扫描 50 行（preproc 块通常不会太大）。
    """
    upper = max(0, start - 50)
    for i in range(start, upper - 1, -1):
        if i >= len(lines):
            continue
        line = lines[i].strip()
        if line.startswith("#else"):
            # 在 #else 分支，继续向上找 #if/#ifdef 的条件
            for j in range(i - 1, upper - 1, -1):
                lj = lines[j].strip() if j < len(lines) else ""
                if lj.startswith("#ifdef") or lj.startswith("#ifndef"):
                    parts = lj.split(None, 1)
                    cond = parts[1] if len(parts) > 1 else "?"
                    return f"#else of {parts[0]} {cond}"
                if lj.startswith("#if "):
                    return f"#else of #if {lj[4:].strip()}"
                if lj.startswith("#elif "):
                    return f"#else/#elif chain near {lj}"
            return "#else"
        if line.startswith("#ifdef") or line.startswith("#ifndef"):
            parts = line.split(None, 1)
            cond = parts[1] if len(parts) > 1 else "?"
            return f"{parts[0]} {cond}"
        if line.startswith("#if "):
            return f"#if {line[4:].strip()}"
    return None


def render_range_map(
    range_map: dict[Path, set[tuple[int, int]]],
    worktree_path: Path,
    modified_files: dict[str, list[tuple[int, int]]],
) -> tuple[str, list[tuple[str, int, int, str]], bool]:
    """将收集到的行范围渲染为最终预取 context 字符串。

    修改文件优先渲染（接近预算上限时核心上下文不被截断）。

    返回:
        (output_text, blocks, truncated)
            blocks 元素: (relative_path, start_line_1based, end_line_1based, symbol_or_unnamed)
    """
    output: list[str] = []
    current_chars = 0
    blocks: list[tuple[str, int, int, str]] = []
    truncated = False

    modified_paths = {worktree_path / f for f in modified_files}

    # 修改文件优先，定义-only 文件次之
    ordered_files = sorted(
        range_map.keys(),
        key=lambda p: 0 if p in modified_paths else 1,
    )

    for file_path in ordered_files:
        ranges = range_map.get(file_path)
        if not ranges:
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = content.splitlines()
        try:
            relative = str(file_path.relative_to(worktree_path))
        except ValueError:
            relative = str(file_path)

        merged = merge_ranges(list(ranges), RENDER_RANGE_GAP)

        for start, end in merged:
            clamped_end = min(end, len(lines) - 1)

            names = extract_defined_names(content, start, clamped_end)
            if len(names) == 1:
                name = next(iter(names))
                header = f"--- {relative}:{start + 1} ({name}) ---\n"
            else:
                name = "<unnamed block>"
                header = f"--- {relative}:{start + 1} ---\n"

            # 检测条件编译上下文，在 header 中标注 #ifdef 分支信息
            anno = _detect_preproc_anno(lines, start)
            if anno:
                header = header.replace(" ---\n", f" [{anno}] ---\n", 1)

            if clamped_end >= start and start < len(lines):
                block = "\n".join(lines[start:clamped_end + 1])
            else:
                block = ""

            if current_chars + len(header) + len(block) + 1 > MAX_PREFETCH_CHARS:
                output.append("\n... (Context prefetch limits reached)\n")
                truncated = True
                return "".join(output), blocks, truncated

            output.append(header)
            output.append(block)
            output.append("\n")
            current_chars += len(header) + len(block) + 1
            blocks.append((relative, start + 1, clamped_end + 1, name))

    return "".join(output), blocks, truncated


# ---------------------------------------------------------------------------
# 主入口: prefetch_context
# ---------------------------------------------------------------------------

def prefetch_context(worktree_path: Path, diff: str) -> str:
    """从 diff 构建预取 context 字符串（向后兼容的简写入口）。

    两阶段流水线:
      Phase 1: 本地 AST 分析（修改文件内）
      Phase 2: 全局 git grep + AST 评分（跨文件定位真实定义）

    说明: 本函数仅返回纯文本，保持与原签名兼容；
          如需完整元数据或 Markdown，请直接调用 `prefetch_context_full`。
    """
    plain_text, _meta = prefetch_context_full(worktree_path, diff)
    return plain_text


def prefetch_context_full(
    worktree_path: Path,
    diff: str,
) -> tuple[str, dict]:
    """从 diff 构建预取 context，并返回 (plain_text, meta)。

    meta 字段:
        symbols_looked_up: int     Phase 2 实际 git grep 的符号数
        blocks: list[tuple]        (relative, start1, end1, name)
        truncated: bool            是否因字符预算截断
        output_chars: int          plain_text 实际长度
        modified_files: list[str]  diff 修改的文件名列表
    """
    worktree_path = Path(worktree_path)
    file_ranges = parse_diff_ranges(diff)

    range_map: dict[Path, set[tuple[int, int]]] = defaultdict(set)
    symbols_to_lookup: set[str] = set()
    already_extracted: set[str] = set()
    called_functions: set[str] = set()

    # ---- Phase 1: 本地分析 ----
    for file, ranges in file_ranges.items():
        if not (file.endswith(".c") or file.endswith(".h")):
            continue
        file_path = worktree_path / file
        if not file_path.exists():
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for start, end in ranges:
            for blk_start, blk_end in overlapping_definitions(content, start, end):
                range_map[file_path].add((blk_start, blk_end))
            already_extracted.update(extract_defined_names(content, start, end))
            symbols_to_lookup.update(extract_type_names(content, start, end))
        called_functions.update(extract_called_functions(content, ranges))

        # ---- Phase 1.5: static 函数的 caller 预取 ----
        # 仅对 static 函数生效（caller 必在同文件，数量可控）。
        # 非 static 函数的 caller 跨文件、可能爆炸，仍交 LLM 按需查。
        # 补两个盲区：caller 函数体 + caller 传给 modified fn 的回调实现。
        static_fns = extract_modified_static_functions(content, ranges)
        if static_fns:
            all_fn_names = _collect_top_level_function_names(content)
            for fn_name, _fn_start, _fn_end in static_fns:
                callers = find_callers_in_file(content, fn_name)
                for caller_start, caller_end, call_row, callbacks \
                        in callers[:MAX_CALLERS_PER_STATIC_FN]:
                    # 过长 caller 截断到调用点附近
                    if caller_end - caller_start > CALLER_OVERSIZE_THRESHOLD:
                        caller_start = max(
                            0, call_row - CALLER_OVERSIZE_HALF_WINDOW
                        )
                        caller_end = min(
                            caller_end, call_row + CALLER_OVERSIZE_HALF_WINDOW
                        )
                    range_map[file_path].add((caller_start, caller_end))
                    # 抓回调参数实现（仅同文件函数，补函数指针调用盲区）
                    for cb in callbacks:
                        if cb in all_fn_names:
                            cb_start, cb_end = all_fn_names[cb]
                            if cb_end - cb_start > CALLER_OVERSIZE_THRESHOLD:
                                cb_end = min(
                                    cb_start + CALLER_OVERSIZE_THRESHOLD,
                                    cb_end,
                                )
                            range_map[file_path].add((cb_start, cb_end))

    # 移除已在本地提取的符号
    symbols_to_lookup -= already_extracted

    # 过滤不透明类型
    opaque = find_opaque_types(symbols_to_lookup, file_ranges, worktree_path)
    symbols_to_lookup -= opaque

    # 合并被调用函数（在不透明过滤之后，避免函数名被误判为不透明类型）
    called_functions -= already_extracted
    symbols_to_lookup.update(called_functions)

    # 过滤 _ops vtable（大型操作结构体，对审查无用）
    symbols_to_lookup = {s for s in symbols_to_lookup if not s.endswith("_ops")}

    symbols = list(symbols_to_lookup)[:SYMBOL_CAP]

    # ---- Phase 2: 全局查找 ----
    if symbols:
        # 构造合并 PCRE 正则，一次匹配所有符号
        sym_alt = "|".join(re.escape(s) for s in symbols)
        regex_pattern = (
            r"^((struct|enum|union)\s+({0})\b"
            r"|#define\s+({0})\b"
            r"|([a-zA-Z_][a-zA-Z0-9_ \t*]+\s*)?({0})\s*\()"
        ).format(sym_alt)

        # 调用者所在目录集合（用于接近度评分）
        caller_dirs: set[str] = {
            f.rsplit("/", 1)[0] for f in file_ranges if "/" in f
        }

        try:
            result = subprocess.run(
                ["git", "grep", "-n", "-I", "-P", "-e", regex_pattern, "--", "*.c", "*.h"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                check=False,
            )
            stdout = result.stdout
        except (FileNotFoundError, subprocess.SubprocessError):
            stdout = ""

        # 解析 git grep 输出，分类候选
        candidates: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))

        for line in stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            path_str, line_num_str, line_content = parts
            try:
                line_num = int(line_num_str)
            except ValueError:
                continue
            abs_path = worktree_path / path_str
            if is_noisy_tree(str(abs_path)):
                continue

            is_priority = (
                path_str.startswith("include/")
                or any(path_str.startswith(d + "/") for d in caller_dirs)
            )

            for sym in symbols:
                if line_matches_symbol(line_content, sym):
                    general, priority = candidates[sym]
                    if is_priority:
                        if len(priority) < CANDIDATE_CAP:
                            priority.append((abs_path, line_num))
                    elif len(general) < CANDIDATE_CAP:
                        general.append((abs_path, line_num))

        # 对每个符号选最佳定义
        for sym, (general, priority) in candidates.items():
            hits = priority + general
            for path, start, end in best_definition_range(sym, hits, worktree_path, caller_dirs):
                range_map[path].add((start, end))

    # ---- 渲染输出 ----
    plain_text, blocks, truncated = render_range_map(
        range_map, worktree_path, file_ranges
    )

    meta = {
        "symbols_looked_up": len(symbols),
        "blocks": blocks,
        "truncated": truncated,
        "output_chars": len(plain_text),
        "modified_files": list(file_ranges.keys()),
    }
    return plain_text, meta


# ---------------------------------------------------------------------------
# Markdown 报告渲染
# ---------------------------------------------------------------------------

def render_markdown_report(
    worktree_path: Path,
    diff: str,
    plain_text: str,
    meta: dict,
) -> str:
    """将预取结果渲染为 Markdown 报告字符串（Summary + Context + Index 三段）。

    该字符串可直接写入 .md 文件。
    """
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    symbols_n = meta.get("symbols_looked_up", 0)
    blocks = meta.get("blocks", [])
    truncated = meta.get("truncated", False)
    output_chars = meta.get("output_chars", len(plain_text))
    modified_files = meta.get("modified_files", [])

    lines: list[str] = []
    lines.append("# Prefetched Context")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- Worktree: `{worktree_path}`")
    lines.append(f"- Generated: `{generated_at}`")
    lines.append(f"- Modified files: `{len(modified_files)}`")
    for mf in modified_files:
        lines.append(f"  - `{mf}`")
    lines.append(f"- Symbols looked up: `{symbols_n}`")
    lines.append(f"- Blocks rendered: `{len(blocks)}`")
    lines.append(f"- Output chars: `{output_chars}` / `{MAX_PREFETCH_CHARS}`")
    lines.append(f"- Truncated: `{'yes' if truncated else 'no'}`")
    lines.append("")
    lines.append("## Context (可直接复制粘贴到 `<pre_fetched_context>` 块)")
    lines.append("")
    lines.append("```")
    lines.append(plain_text.rstrip("\n"))
    lines.append("```")
    lines.append("")
    lines.append("## Index（块索引，便于跳转到对应源码）")
    if blocks:
        for i, (relative, start1, end1, name) in enumerate(blocks, 1):
            if end1 == start1:
                loc = f"{relative}:{start1}"
            else:
                loc = f"{relative}:{start1}-{end1}"
            lines.append(f"{i}. `{loc}` — `{name}`")
    else:
        lines.append("_（无可用块）_")
    lines.append("")
    return "\n".join(lines)


def prefetch_context_to_md(worktree_path: Path, diff: str) -> tuple[str, str, dict]:
    """库调用的便捷包装：返回 (plain_text, md_text, meta)。"""
    plain_text, meta = prefetch_context_full(worktree_path, diff)
    md_text = render_markdown_report(worktree_path, diff, plain_text, meta)
    return plain_text, md_text, meta


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> tuple[str, str, Optional[str]]:
    """极简参数解析（不引入 argparse 依赖，保持脚本轻量）。

    返回: (worktree_str, diff_arg, md_path_or_None)
    """
    md_path: Optional[str] = None
    positional: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--md":
            if i + 1 >= len(argv):
                print("错误: --md 需要一个输出文件路径", file=sys.stderr)
                sys.exit(2)
            md_path = argv[i + 1]
            i += 2
            continue
        positional.append(a)
        i += 1
    if len(positional) < 2:
        print(
            "用法: python prefetch.py <worktree_path> <diff_file|-> [--md <output.md>]\n"
            "  diff_file: unified diff 文件路径，或 '-' 从 stdin 读取\n"
            "  --md:      额外将 Markdown 报告写入指定文件",
            file=sys.stderr,
        )
        sys.exit(2)
    return positional[0], positional[1], md_path


def main() -> None:
    worktree_str, diff_arg, md_path = _parse_args(sys.argv[1:])

    worktree = Path(worktree_str).resolve()
    if diff_arg == "-":
        diff = sys.stdin.read()
    else:
        diff = Path(diff_arg).read_text(encoding="utf-8", errors="replace")

    plain_text, meta = prefetch_context_full(worktree, diff)
    sys.stdout.write(plain_text)

    if md_path:
        md_text = render_markdown_report(worktree, diff, plain_text, meta)
        Path(md_path).write_text(md_text, encoding="utf-8")
        # 同时把原始 diff 保存到相邻 .diff 文件，便于回溯/审计/重跑
        diff_path = Path(md_path).with_suffix(".diff")
        diff_path.write_text(diff, encoding="utf-8")
        print(
            f"[prefetch] Markdown report written to: {Path(md_path).resolve()}",
            file=sys.stderr,
        )
        print(
            f"[prefetch] Diff saved to: {diff_path.resolve()}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
