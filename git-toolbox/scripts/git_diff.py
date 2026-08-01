import subprocess
import logging
from typing import Any, Dict, List, Optional

from .framework import LlmTool
from .truncator import Truncator

logger = logging.getLogger(__name__)


class GitDiffTool(LlmTool):
    def name(self) -> str:
        return "git_diff"

    def description(self) -> str:
        return "Show changes between two commits or revisions."

    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "base_revision": {
                    "type": "string",
                    "description": "The baseline commit SHA or revision reference.",
                },
                "target_revision": {
                    "type": "string",
                    "description": "The target commit SHA or revision reference to compare against.",
                },
                "paths": {
                    "type": "array",
                    "description": "Optional relative file or directory paths to filter the diff.",
                    "items": {"type": "string"},
                },
            },
            "required": ["base_revision", "target_revision"],
        }

    def normalize_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(args)
        if "paths" not in normalized:
            normalized["paths"] = None
        return normalized

    def call(self, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        base_raw = args.get("base_revision")
        if not base_raw:
            return {"error": "Missing base_revision"}

        target_raw = args.get("target_revision")
        if not target_raw:
            return {"error": "Missing target_revision"}

        base_virt = context.virtualize_ref(base_raw)
        target_virt = context.virtualize_ref(target_raw)

        if base_virt.startswith("-") or target_virt.startswith("-"):
            return {"error": "Invalid revision names"}

        cmd = [
            "git", "-C", str(context.worktree_path),
            "diff", "--diff-algorithm=histogram",
            base_virt, target_virt,
        ]

        paths = args.get("paths")
        if paths is not None and isinstance(paths, list):
            cmd.append("--")
            for p in paths:
                if p.startswith("-"):
                    return {"error": f"Invalid path parameter: {p}"}
                cmd.append(p)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            logger.error("git diff failed: %s", e)
            return {"error": f"git diff failed: {e}"}

        if result.returncode != 0:
            return {"error": f"git diff failed: {result.stderr.strip()}"}

        content = result.stdout
        total_diff_lines = len(content.split("\n"))

        trunc_result = Truncator.truncate_diff(content, 10000, "Diff")
        truncated_diff = trunc_result["content"]
        is_truncated = trunc_result["truncated"]
        returned_diff_lines = len(truncated_diff.split("\n"))

        next_page_hint = None
        if is_truncated:
            next_page_hint = (
                "This diff is too large and was truncated by dropping the middle. "
                "To see complete changes, filter by specific 'paths' (e.g., folders/files)."
            )

        return {
            "content": truncated_diff,
            "truncated": is_truncated,
            "metadata": {
                "total_items": total_diff_lines,
                "returned_items": returned_diff_lines,
            },
            "next_page_hint": next_page_hint,
        }