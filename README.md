# AI_Skills

存放面向 Trae IDE（及兼容 Agent 框架）的可复用 AI Skills。每个 Skill 由一个 `SKILL.md` 声明能力与调用规范，附带可直接执行的脚本资源，Agent 可按需自动调用。

## Skill 列表

| Skill | 说明 | 提供的能力 | 入口 |
|-------|------|------------|------|
| [git-toolbox](./git-toolbox/SKILL.md) | 封装 git-toolbox 脚本工具集，提供 Git 仓库查询、差异对比、文件搜索、blame 追溯等能力。当用户需要查看 commit、diff、grep、文件列表或追溯代码修改时调用。 | `git_log`、`git_show`、`git_diff`、`git_ls`、`git_find_files`、`git_grep`、`git_read_files`、`git_blame` | [git-toolbox/SKILL.md](./git-toolbox/SKILL.md) |

## 目录结构

```
AI_Skills/
├── README.md
├── .gitignore
└── git-toolbox/
    ├── SKILL.md            # Skill 声明文件（Agent 读取的入口）
    └── scripts/            # Skill 自带脚本资源
        ├── cli.py          # CLI 入口，直接调用各工具
        ├── toolbox.py      # ToolBox 调度核心
        ├── context.py      # 多 repo 上下文管理
        ├── framework.py    # 工具注册框架
        ├── truncator.py    # 输出截断控制
        ├── utils.py        # 通用工具函数
        ├── git_log.py      # 各工具实现
        ├── git_show.py
        ├── git_diff.py
        ├── git_ls.py
        ├── git_find_files.py
        ├── git_grep.py
        ├── git_read_files.py
        ├── git_blame.py
        ├── read_prompt.py
        └── tests/          # 单元测试
            ├── __init__.py
            └── test_toolbox.py
```

## 使用方式

### 1. 在 Trae IDE 中启用

将目标 Skill 目录复制（或软链）到项目的 `.trae/skills/` 下：

```
<your-project>/.trae/skills/<skill-name>/SKILL.md
```

例如启用 git-toolbox：

```powershell
Copy-Item -Recurse "d:\AI\AI_Skills\git-toolbox" "<your-project>\.trae\skills\git-toolbox"
```

Agent 在对话中会根据 `SKILL.md` 的 `description` 自动判断是否调用。

### 2. git-toolbox 调用示例

该 Skill 通过自带 Python 脚本直接调用（无需 MCP 服务器），参数以 JSON 经 stdin 传入：

```powershell
# 查看最新提交
'{"range":"HEAD","limit":3}' | python "<skill-dir>/scripts/cli.py" git_log --repo "D:\your\repo"

# 搜索代码
'{"revision":"HEAD","pattern":"start_kernel","path":"init/main.c","is_literal":true}' | python "<skill-dir>/scripts/cli.py" git_grep --repo "D:\your\repo"

# 对比两个版本
'{"base_revision":"v1.0","target_revision":"v1.1","paths":["Makefile"]}' | python "<skill-dir>/scripts/cli.py" git_diff --repo "D:\your\repo"
```

输出为结构化 JSON（含 `content` / `metadata` / `error` 等字段）。

> 环境要求：本机 `python` 命令可用（已在 Python 3.14 验证）。

## 约定

- 每个 Skill 独立成目录，`SKILL.md` 为唯一声明入口。
- Skill 仅提供只读/工具能力，避免破坏性副作用。
- 脚本资源统一放在 Skill 目录下的 `scripts/` 子目录。
