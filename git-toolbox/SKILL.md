---
name: "git-toolbox"
description: "封装 git-toolbox 脚本工具集，提供 Git 仓库查询、差异对比、文件搜索、blame 追溯等能力。当用户需要查看 commit、diff、grep、文件列表或追溯代码修改时调用。"
---

# Git Toolbox Skill

本 Skill 通过**直接调用自带 Python 脚本**（`scripts/cli.py`）对任意 Git 仓库进行只读查询。不再依赖 MCP 服务器，避免 `undefined` 等调用失败问题。

## 调用原则

1. **统一入口**：所有工具均通过 `scripts/cli.py` 调用，使用 `RunCommand` 执行。
2. **CLI 路径**：使用 skill 目录下的绝对路径，无需关心 cwd：
   ```
   python "<SKILL_DIR>\scripts\cli.py" <tool_name> --repo "<绝对路径>" [--args "<JSON>"]
   ```
   其中 `<SKILL_DIR>` = `d:\AI\Trae\code_review\.trae\skills\git-toolbox`
3. **参数传递（推荐 stdin）**：工具参数以 JSON 通过 **stdin 管道**传入，JSON 用**单引号**包裹，**内部双引号无需转义**，可完全规避 PowerShell 命令行解析坑：
   ```powershell
   '{"range":"HEAD","limit":3}' | python "<SKILL_DIR>\scripts\cli.py" git_log --repo "<绝对路径>"
   ```
4. **参数传递（备选 --args）**：也可用 `--args '<JSON>'`，但 JSON 内部双引号需转义为 `\"`；当 JSON 含空格时 PowerShell 5.1 可能误拆参数，**含空格的 pattern/value 请改用 stdin**。
5. **仓库路径参数**：`--repo` 必须使用绝对路径。
6. **revision 参数**：可使用 `HEAD`、分支名、tag 名、完整或短 commit SHA。
7. **输出格式**：CLI 输出单个 JSON 对象到 stdout（含 `content` / `metadata` / `error` 等字段）。退出码 0 表示 CLI 正常执行（含工具内部 error）；1/2 表示 CLI 参数/运行错误。
8. **批量优先**：对多个独立查询，尽量并行发起多个 `RunCommand`。

---

## 工具清单与调用规范

### 1. git_log — 查看提交日志

**场景**：查看 commit 历史、最新提交、某段时间的变更记录。

```powershell
'{"range":"HEAD","limit":10}' | python "<SKILL_DIR>\scripts\cli.py" git_log --repo "<绝对路径>"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| range | string（必填） | `HEAD` 或 `<sha1>..<sha2>` 或 `<tag>` |
| limit | int（可选） | 默认 10，最大 100 |

---

### 2. git_show — 查看对象详情

**场景**：查看某个 commit 的完整信息（含 diff）、查看指定版本下的某个文件内容。

```powershell
'{"object":"HEAD:README.md","start_line":1,"end_line":100}' | python "<SKILL_DIR>\scripts\cli.py" git_show --repo "<绝对路径>"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| object | string（必填） | `HEAD` / `<sha>` / `HEAD:<相对路径>`（blob 用 `REV:path` 语法） |
| suppress_diff | bool（可选） | true 则抑制 commit 的 diff 输出 |
| start_line | int（可选） | blob 专用，1-based |
| end_line | int（可选） | blob 专用，1-based |
| paths | string[]（可选） | commit 时路径过滤 |

---

### 3. git_diff — 对比两个版本差异

**场景**：用户问"某两个版本改了什么"、"某文件在两个提交间的区别"。

```powershell
'{"base_revision":"<基准>","target_revision":"<目标>","paths":["fs/cifs/connect.c"]}' | python "<SKILL_DIR>\scripts\cli.py" git_diff --repo "<绝对路径>"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| base_revision | string（必填） | 基准版本 sha/tag/branch |
| target_revision | string（必填） | 目标版本 sha/tag/branch |
| paths | string[]（可选） | 限制文件/目录 |

---

### 4. git_ls — 列出指定版本下的目录/文件

**场景**：查看某个版本下某个目录有哪些文件。

```powershell
'{"revision":"HEAD","path":"fs/cifs"}' | python "<SKILL_DIR>\scripts\cli.py" git_ls --repo "<绝对路径>"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| revision | string（必填） | `HEAD` 或 `<sha>` |
| path | string（必填） | `.` 或 `fs/cifs` 等相对路径 |

---

### 5. git_find_files — 按 glob 模式查找文件

**场景**：查找所有 `*.c` 文件、找某个目录下的 `Kconfig`、找 `Makefile*`。

```powershell
'{"revision":"HEAD","pattern":"*.c","path":"fs"}' | python "<SKILL_DIR>\scripts\cli.py" git_find_files --repo "<绝对路径>"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| revision | string（必填） | `HEAD` 或 `<sha>` |
| pattern | string（必填） | glob 模式，如 `*.c` / `**/Kconfig*` |
| path | string（可选） | 限制子目录 |

---

### 6. git_grep — 在指定版本的文件中搜索内容

**场景**：用户问"某函数在哪个文件定义"、"某字符串出现在哪些代码里"。

```powershell
'{"revision":"HEAD","pattern":"cifs_open","path":"fs","context_lines":3,"is_literal":false}' | python "<SKILL_DIR>\scripts\cli.py" git_grep --repo "<绝对路径>"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| revision | string（必填） | `HEAD` 或 `<sha>` |
| pattern | string（必填） | 正则或固定字符串 |
| path | string（可选） | 限制子目录，可含空格分隔多个 pathspec |
| context_lines | int（可选） | 上下文行数，默认 0 |
| count_only | bool（可选） | true 时只返回文件与命中次数 |
| is_literal | bool（可选） | true 时 pattern 当作固定字符串（非正则） |

---

### 7. git_read_files — 批量读取指定版本下的文件内容

**场景**：一次读取多个文件的内容，或读取文件的指定行范围。**最大 10 个文件/请求**。

```powershell
'{"revision":"HEAD","files":[{"path":"init/main.c","start_line":1,"end_line":100},{"path":"kernel/sched/core.c"}]}' | python "<SKILL_DIR>\scripts\cli.py" git_read_files --repo "<绝对路径>"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| revision | string（必填） | `HEAD` 或 `<sha>` |
| files | array（必填） | 每项 `{path, start_line?, end_line?}`，不写行号读全文件 |

---

### 8. git_blame — 追溯每行代码的最后修改者

**场景**：用户问"这段代码是谁写的"、"某行最后被哪个 commit 修改"。

```powershell
'{"revision":"HEAD","path":"fs/cifs/connect.c","start_line":100,"end_line":200}' | python "<SKILL_DIR>\scripts\cli.py" git_blame --repo "<绝对路径>"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| revision | string（必填） | `HEAD` 或 `<sha>` |
| path | string（必填） | 文件相对路径 |
| start_line | int（可选） | 1-based |
| end_line | int（可选） | 1-based |

---

## 标准工作流（SOP）

### 场景 A：用户问"最新 commit 是什么"
1. `git_log` → `range=HEAD, limit=1`
2. 从返回 JSON 的 `content` 解析 commit hash、作者、时间、message

### 场景 B：用户问"某两个 tag 之间改了什么文件"
1. `git_diff` → `base_revision` + `target_revision`
2. 若 diff 过大，先用 `paths` 限制子目录，或先 `git_log` 列出中间 commit

### 场景 C：用户问"某函数 XXX 定义在哪"
1. `git_grep` → `pattern="^[A-Za-z_][A-Za-z0-9_\s\*]*XXX\s*\(", is_literal=false`
2. 若命中多，再用 `path` 限定目录

### 场景 D：用户问"读取 HEAD 下某文件的第 50-100 行"
1. 优先 `git_read_files`（支持批量），或 `git_show`（单文件 + `start_line/end_line`）

---

## 错误处理

| 现象 | 处理方式 |
|------|----------|
| 返回 JSON 含 `"error"` 字段 | 检查参数名/值是否正确；路径是否区分大小写；是否 `path` 限制过严 |
| `metadata.total_items=0` / 空结果 | 检查 pattern/path 是否正确；确认子目录在该 revision 下存在 |
| 返回 `truncated=true` | 缩小 `limit`、缩小 `paths`、增加 `path` 过滤、使用行范围 |
| CLI 退出码 1（stderr 输出 JSON error） | 多为 `--args` JSON 不合法或 `--repo` 路径不存在，按提示修正 |

---

## 注意事项

- **只读原则**：本 Skill 仅提供只读查询，不执行任何 `git commit / push / checkout / reset` 等修改操作。
- **路径规范**：相对路径均相对于仓库根目录，不要以 `/` 开头。
- **大仓库（如 Linux Kernel）**：默认加 `limit` 或 `path` 限制，避免一次返回过多数据导致响应慢或截断。
- **Python 环境**：需本机 `python` 命令可用（已在 Python 3.14 验证）。
