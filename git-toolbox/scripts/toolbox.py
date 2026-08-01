import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .context import GitToolContext
from .framework import ToolRegistry
from .git_blame import GitBlameTool
from .git_diff import GitDiffTool
from .git_find_files import GitFindFilesTool
from .git_grep import GitGrepTool
from .git_log import GitLogTool
from .git_ls import GitLsTool
from .git_read_files import GitReadFilesTool
from .git_show import GitShowTool
from .read_prompt import ReadPromptTool

logger = logging.getLogger(__name__)


class ToolBox:
    """Git 工具集，支持按 repo_path 隔离的多 repo 上下文。

    - worktree_path 为可选：传入则作为默认 repo（向后兼容旧 API）。
    - call() 接收 repo_path 参数：传入则使用/创建对应 repo 的 context；
      不传则 fallback 到默认 context（若未配置默认 repo 则报错）。
    - virtual_head / active_patch_files / cache 均按 repo_path 隔离。
    """

    def __init__(
        self,
        worktree_path: Optional[str] = None,
        prompts_path: Optional[str] = None,
    ):
        self.prompts_path: Optional[str] = prompts_path
        self.registry = ToolRegistry()

        # repo_path(规范化后的字符串) -> context
        self._contexts: Dict[str, GitToolContext] = {}
        self._contexts_lock = threading.Lock()

        # 默认 repo key（仅当 worktree_path 传入时存在）
        self._default_repo_key: Optional[str] = None

        self.registry.register(GitReadFilesTool())
        self.registry.register(GitBlameTool())
        self.registry.register(GitDiffTool())
        self.registry.register(GitShowTool())
        self.registry.register(GitLogTool())
        self.registry.register(GitLsTool())
        self.registry.register(GitGrepTool())
        self.registry.register(GitFindFilesTool())

        if prompts_path is not None:
            self.registry.register(ReadPromptTool())

        if worktree_path is not None:
            ctx = GitToolContext(worktree_path, prompts_path)
            key = str(Path(worktree_path).resolve())
            self._contexts[key] = ctx
            self._default_repo_key = key

    # ── context 管理 ──────────────────────────────────────────────

    @staticmethod
    def _normalize_repo_path(repo_path: str) -> str:
        return str(Path(repo_path).resolve())

    def _get_context(self, repo_path: Optional[str]) -> GitToolContext:
        """获取或创建对应 repo 的 context。

        - repo_path 为 None：返回默认 context；无默认 repo 时抛 ValueError。
        - repo_path 非空：校验路径存在，按需创建并缓存 context。
        """
        if repo_path is None:
            if self._default_repo_key is None:
                raise ValueError(
                    "repo_path is required: no default repo configured. "
                    "Pass repo_path explicitly, or initialize ToolBox with a worktree_path."
                )
            return self._contexts[self._default_repo_key]

        key = self._normalize_repo_path(repo_path)
        with self._contexts_lock:
            ctx = self._contexts.get(key)
            if ctx is None:
                if not Path(key).exists():
                    raise ValueError(f"repo path does not exist: {key}")
                ctx = GitToolContext(key, self.prompts_path)
                self._contexts[key] = ctx
            return ctx

    # ── 向后兼容的属性访问 ────────────────────────────────────────

    @property
    def cache(self) -> Dict[str, Any]:
        """默认 context 的 cache（向后兼容）。无默认 repo 时返回空 dict。"""
        if self._default_repo_key is None:
            return {}
        return self._contexts[self._default_repo_key].cache

    def set_virtual_head(
        self,
        sha: str,
        repo_path: Optional[str] = None,
    ) -> None:
        self._get_context(repo_path).virtual_head = sha

    def set_active_patch_files(
        self,
        files: List[str],
        repo_path: Optional[str] = None,
    ) -> None:
        self._get_context(repo_path).active_patch_files = files

    def virtualize_ref(
        self,
        r: str,
        repo_path: Optional[str] = None,
    ) -> str:
        return self._get_context(repo_path).virtualize_ref(r)

    def get_worktree_path(self, repo_path: Optional[str] = None):
        return self._get_context(repo_path).worktree_path

    def get_declarations_generic(self) -> List[Dict[str, Any]]:
        return self.registry.declarations()

    # ── 调用入口 ──────────────────────────────────────────────────

    def call(
        self,
        name: str,
        args: Dict[str, Any],
        repo_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """调用一个 tool。

        Args:
            name: tool 名称。
            args: tool 参数（不含 repo_path）。
            repo_path: 可选，目标 git repo 路径。不传则用默认 context。
        """
        try:
            context = self._get_context(repo_path)
        except ValueError as e:
            return {"error": str(e)}

        name_normalized = name.strip().lower()
        should_cache = name_normalized != "todowrite"

        normalized_args = self.registry.normalize_tool_args(name_normalized, args)

        # cache key 显式包含 repo，避免跨 repo 污染
        repo_key = repo_path if repo_path is not None else (self._default_repo_key or "default")

        cache_key = None
        if should_cache:
            virtual_head = context.virtual_head or "none"
            try:
                key = f"{name_normalized}:{repo_key}:{virtual_head}:{json.dumps(normalized_args, sort_keys=True)}"
            except (TypeError, ValueError):
                key = f"{name_normalized}:{repo_key}:{virtual_head}:{normalized_args}"

            cached = context.get_cache(key)
            if cached is not None:
                if isinstance(cached, dict) and "error" in cached:
                    pass
                else:
                    return cached
            cache_key = key

        try:
            res = self.registry.call(name_normalized, args, context)
        except ValueError as e:
            return {"error": str(e)}

        if cache_key is not None:
            context.set_cache(cache_key, res)

        return res
