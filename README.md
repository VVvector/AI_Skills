# AI_Skills

存放面向 Trae IDE（及兼容 Agent 框架）的可复用 AI Skills。每个 Skill 由一个 `SKILL.md` 声明能力与调用规范，附带可直接执行的脚本资源，Agent 可按需自动调用。

## Skill 列表

| Skill | 简介 |
|-------|------|
| [git-toolbox](./git-toolbox/SKILL.md) | 封装 git-toolbox 脚本工具集，提供 Git 仓库查询、差异对比、文件搜索、blame 追溯等能力。当用户需要查看 commit、diff、grep、文件列表或追溯代码修改时调用。 |
| [ast-context-prefetch](./ast-context-prefetch/SKILL.md) | 基于 AST/tree-sitter 的动态 context 预取，用于 LLM 代码审查（diff → AST 符号提取 → git grep → 评分渲染）。在构建 patch 审查的 prefetch_context 或移植 sashiko 预取流水线时调用。 |

## 使用方式

### 在 Trae IDE 中启用

将目标 Skill 目录复制（或软链）到项目的 `.trae/skills/` 下：

```
<your-project>/.trae/skills/<skill-name>/SKILL.md
```

例如启用 git-toolbox：

```powershell
Copy-Item -Recurse "d:\AI\AI_Skills\git-toolbox" "<your-project>\.trae\skills\git-toolbox"
```

Agent 在对话中会根据 `SKILL.md` 的 `description` 自动判断是否调用。

## 约定

- 每个 Skill 独立成目录，`SKILL.md` 为唯一声明入口。
- Skill 仅提供只读/工具能力，避免破坏性副作用。
- 脚本资源统一放在 Skill 目录下的 `scripts/` 子目录。
