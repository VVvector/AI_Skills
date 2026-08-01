import subprocess
import logging
from typing import Any, Dict, Optional

from .framework import LlmTool
from .truncator import Truncator

logger = logging.getLogger(__name__)


class GitBlameTool(LlmTool):
    def name(self) -> str:
        return "git_blame"

    def description(self) -> str:
        return "Show what revision and author last modified each line of a file."

    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "revision": {
                    "type": "string",
                    "description": "The Git commit SHA or reference to blame from.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative path to the file.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-based start line (optional).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "1-based end line (optional).",
                },
            },
            "required": ["revision", "path"],
        }

    def normalize_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(args)
        if "start_line" not in normalized:
            normalized["start_line"] = None
        if "end_line" not in normalized:
            normalized["end_line"] = None
        return normalized

    def call(self, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        revision_raw = args.get("revision")
        if not revision_raw:
            return {"error": "Missing revision"}

        revision_virt = context.virtualize_ref(revision_raw)
        path_str = args.get("path")
        if not path_str:
            return {"error": "Missing path"}

        start_line = args.get("start_line")
        end_line = args.get("end_line")

        cmd = ["git", "-C", str(context.worktree_path), "blame"]

        if start_line is not None and end_line is not None:
            cmd.append(f"-L{start_line},{end_line}")

        cmd.extend([revision_virt, "--", path_str])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            logger.error("git blame failed: %s", e)
            return {"error": f"git blame failed: {e}"}

        if result.returncode != 0:
            return {"error": f"git blame failed: {result.stderr.strip()}"}

        content = result.stdout
        total_blame_lines = len(content.split("\n"))

        trunc_result = Truncator.truncate_sequential(content, 10000)
        truncated_content = trunc_result["content"]
        lines_kept = trunc_result["lines_kept"]
        is_truncated = trunc_result["truncated"]

        start = start_line if start_line is not None else 1
        if is_truncated and lines_kept > 0:
            end_idx = start + lines_kept - 1
        else:
            end_idx = start + total_blame_lines - 1

        returned_items = lines_kept if (is_truncated and lines_kept > 0) else total_blame_lines

        next_page_hint = None
        if is_truncated:
            next_page_hint = (
                f"Only the first {returned_items} lines of blame are shown. "
                f"To view the remaining blame lines, use start_line={start + returned_items}."
            )

        return {
            "content": truncated_content,
            "truncated": is_truncated,
            "metadata": {
                "total_items": total_blame_lines,
                "returned_items": returned_items,
                "start_index": start,
                "end_index": end_idx,
            },
            "next_page_hint": next_page_hint,
        }