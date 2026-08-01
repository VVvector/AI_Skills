import subprocess
import logging
from typing import Any, Dict, List, Optional

from .framework import LlmTool
from .truncator import Truncator

logger = logging.getLogger(__name__)


class GitLogTool(LlmTool):
    def name(self) -> str:
        return "git_log"

    def description(self) -> str:
        return "Show commit logs in a specific range or revision history."

    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "range": {
                    "type": "string",
                    "description": "The commit range or reference to view logs for (e.g., 'baseline..HEAD' or 'HEAD').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Limit the number of commits returned (defaults to 10, max 100).",
                },
            },
            "required": ["range"],
        }

    def normalize_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(args)
        if "limit" not in normalized:
            normalized["limit"] = 10
        return normalized

    def call(self, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        range_raw = args.get("range")
        if not range_raw:
            return {"error": "Missing range"}

        range_virt = context.virtualize_ref(range_raw)
        limit = min(args.get("limit", 10), 100)

        if range_virt.startswith("-"):
            return {"error": "Invalid range"}

        cmd = [
            "git", "-C", str(context.worktree_path),
            "log", "-n", str(limit), range_virt,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            logger.error("git log failed: %s", e)
            return {"error": f"git log failed: {e}"}

        if result.returncode != 0:
            return {"error": f"git log failed: {result.stderr.strip()}"}

        raw_stdout = result.stdout
        total_log_lines = len(raw_stdout.split("\n"))

        trunc_result = Truncator.truncate_sequential(raw_stdout, 10000)
        truncated_log = trunc_result["content"]
        lines_kept = trunc_result["lines_kept"]
        is_truncated = trunc_result["truncated"]

        returned_items = lines_kept if (is_truncated and lines_kept > 0) else total_log_lines

        next_page_hint = None
        if is_truncated:
            next_page_hint = (
                "The log output was truncated. Use a smaller commit range or set a lower 'limit' parameter."
            )

        return {
            "content": truncated_log,
            "truncated": is_truncated,
            "metadata": {
                "total_items": total_log_lines,
                "returned_items": returned_items,
            },
            "next_page_hint": next_page_hint,
            "output": truncated_log,
        }