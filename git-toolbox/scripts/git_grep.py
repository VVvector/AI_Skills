import subprocess
import logging
from typing import Any, Dict, List, Optional

from .framework import LlmTool
from .truncator import Truncator
from .utils import format_git_grep_output

logger = logging.getLogger(__name__)


class GitGrepTool(LlmTool):
    def name(self) -> str:
        return "git_grep"

    def description(self) -> str:
        return "Search for a pattern in files using git grep at a specific Git revision. Returns matching lines with context."

    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "revision": {
                    "type": "string",
                    "description": "Git commit SHA or reference to search at.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Regex or literal pattern to search.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative paths or pathspecs to restrict search (optional).",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Context lines to show. Default: 0.",
                },
                "count_only": {
                    "type": "boolean",
                    "description": "If true, returns file names and match counts only.",
                },
                "is_literal": {
                    "type": "boolean",
                    "description": "If true, treats pattern as literal fixed string rather than PCRE regex.",
                },
            },
            "required": ["revision", "pattern"],
        }

    def normalize_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(args)
        if "path" not in normalized:
            normalized["path"] = None
        if "context_lines" not in normalized:
            normalized["context_lines"] = 0
        if "count_only" not in normalized:
            normalized["count_only"] = False
        if "is_literal" not in normalized:
            normalized["is_literal"] = False
        return normalized

    def call(self, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        revision_raw = args.get("revision")
        if not revision_raw:
            return {"error": "Missing revision"}

        revision_virt = context.virtualize_ref(revision_raw)
        pattern = args.get("pattern")
        if not pattern:
            return {"error": "Missing pattern"}

        path_str = args.get("path")
        context_lines = args.get("context_lines", 0)
        count_only = args.get("count_only", False)
        is_literal = args.get("is_literal", False)

        if revision_virt.startswith("-") or pattern.startswith("-"):
            return {"error": "Invalid revision or pattern"}

        cmd = ["git", "-C", str(context.worktree_path), "grep"]

        if count_only:
            cmd.append("-c")
        else:
            cmd.extend(["-n", "-I", f"-C{context_lines}"])

        if is_literal:
            cmd.append("-F")
        else:
            cmd.append("-P")

        cmd.extend([pattern, revision_virt])

        if path_str and path_str != "." and path_str != "":
            cmd.append("--")
            for pathspec in path_str.split():
                cmd.append(pathspec)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            logger.error("git grep failed: %s", e)
            return {"error": f"git grep failed: {e}"}

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if not stderr:
                return {
                    "content": "",
                    "truncated": False,
                    "metadata": {"total_items": 0, "returned_items": 0},
                    "matches": [],
                    "message": "No matches found.",
                }
            return {"error": f"git grep failed: {stderr}"}

        content = result.stdout

        if count_only:
            prefix = f"{revision_virt}:"
            lines = []
            for line in content.split("\n"):
                if line.startswith(prefix):
                    lines.append(line[len(prefix):])
                else:
                    lines.append(line)
            formatted = "\n".join(lines)
        else:
            active_files = context.active_patch_files
            formatted = format_git_grep_output(content, revision_virt, active_files)

        total_grep_lines = len(formatted.split("\n"))
        trunc_result = Truncator.truncate_sequential(formatted, 10000)
        truncated_grep = trunc_result["content"]
        lines_kept = trunc_result["lines_kept"]
        is_truncated = trunc_result["truncated"]

        returned_items = lines_kept if (is_truncated and lines_kept > 0) else total_grep_lines

        next_page_hint = None
        if is_truncated:
            next_page_hint = (
                "Grep matches were truncated. Narrow your search using a pathspec or a more specific regex pattern."
            )

        return {
            "content": truncated_grep,
            "truncated": is_truncated,
            "metadata": {
                "total_items": total_grep_lines,
                "returned_items": returned_items,
            },
            "next_page_hint": next_page_hint,
        }