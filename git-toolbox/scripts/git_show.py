import subprocess
import logging
from typing import Any, Dict, List, Optional

from .framework import LlmTool
from .truncator import Truncator

logger = logging.getLogger(__name__)


class GitShowTool(LlmTool):
    def name(self) -> str:
        return "git_show"

    def description(self) -> str:
        return "Show commits, trees, tags or blobs."

    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "object": {
                    "type": "string",
                    "description": "Git object specifier (e.g. 'HEAD:README.md' or 'HEAD').",
                },
                "suppress_diff": {
                    "type": "boolean",
                    "description": "If true, suppresses commit diff output.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Focus start line (blobs only, optional).",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Focus end line (blobs only, optional).",
                },
                "paths": {
                    "type": "array",
                    "description": "Path filters (commits only, optional).",
                    "items": {"type": "string"},
                },
            },
            "required": ["object"],
        }

    def normalize_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(args)
        if "suppress_diff" not in normalized:
            normalized["suppress_diff"] = False
        if "start_line" not in normalized:
            normalized["start_line"] = None
        if "end_line" not in normalized:
            normalized["end_line"] = None
        if "paths" not in normalized:
            normalized["paths"] = None
        if "mode" not in normalized:
            normalized["mode"] = "raw"
        return normalized

    def call(self, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        object_raw = args.get("object")
        if not object_raw:
            return {"error": "Missing object"}

        object_virt = context.virtualize_ref(object_raw)
        suppress_diff = args.get("suppress_diff", False)
        start_line = args.get("start_line")
        end_line = args.get("end_line")

        if object_virt.startswith("-"):
            return {"error": f"Invalid object name: {object_virt}"}

        paths_val = args.get("paths")
        raw_key = f"git_show_raw:{object_virt}:{str(suppress_diff).lower()}:{paths_val}"

        cached_raw = context.get_cache(raw_key)
        if cached_raw is not None:
            content = cached_raw
        else:
            cmd = ["git", "-C", str(context.worktree_path), "show"]

            if suppress_diff:
                cmd.append("--no-patch")

            cmd.append(object_virt)

            if paths_val is not None and isinstance(paths_val, list):
                cmd.append("--")
                for p in paths_val:
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
                logger.error("git show failed: %s", e)
                return {"error": f"git show failed: {e}"}

            if result.returncode != 0:
                return {"error": f"git show failed: {result.stderr.strip()}"}

            content = result.stdout
            context.set_cache(raw_key, content)

        is_file = ":" in object_virt and not object_virt.startswith(":")

        if start_line is not None or end_line is not None:
            lines = content.split("\n")
            total_lines = len(lines)

            if start_line is not None and end_line is None:
                resolved_end_line = start_line + 100
            else:
                resolved_end_line = end_line

            if start_line is not None and resolved_end_line is not None:
                start = max(start_line, 1) - 1
                end = min(resolved_end_line, total_lines)
            elif start_line is not None:
                start = max(start_line, 1) - 1
                end = total_lines
            elif resolved_end_line is not None:
                start = 0
                end = min(resolved_end_line, total_lines)
            else:
                start = 0
                end = total_lines

            start = min(start, total_lines)
            end = max(start, min(end, total_lines))

            if start >= total_lines:
                return {
                    "content": "",
                    "truncated": False,
                    "metadata": {
                        "total_items": total_lines,
                        "returned_items": 0,
                        "start_index": start + 1,
                        "end_index": end,
                    },
                    "lines_read": 0,
                    "total_lines": total_lines,
                    "start_line": start + 1,
                    "end_line": end,
                }

            slice_lines = lines[start:end]
            result_str = "\n".join(slice_lines)

            if is_file:
                trunc_res = Truncator.truncate_sequential(result_str, 20000)
                truncated = trunc_res["content"]
                lines_kept = trunc_res["lines_kept"]
                is_truncated_content = trunc_res["truncated"]
            else:
                trunc_res = Truncator.truncate_diff(result_str, 10000, "Commit")
                truncated = trunc_res["content"]
                lines_kept = 0
                is_truncated_content = trunc_res["truncated"]

            if is_truncated_content and lines_kept > 0:
                end_idx = start + lines_kept
            else:
                end_idx = end

            returned_items = lines_kept if (is_truncated_content and lines_kept > 0) else len(slice_lines)

            next_page_hint = None
            if is_truncated_content:
                next_page_hint = (
                    f"Only lines {start + 1}-{end_idx} of {total_lines} are shown. "
                    f"To read more, call git_show with start_line={end_idx + 1}."
                )

            return {
                "content": truncated,
                "truncated": is_truncated_content,
                "metadata": {
                    "total_items": total_lines,
                    "returned_items": returned_items,
                    "start_index": start + 1,
                    "end_index": end_idx,
                },
                "next_page_hint": next_page_hint,
                "total_lines": total_lines,
                "start_line": start + 1,
                "end_line": end,
            }

        total_lines = len(content.split("\n"))

        if is_file:
            trunc_res = Truncator.truncate_sequential(content, 20000)
            truncated = trunc_res["content"]
            is_truncated = trunc_res["truncated"]
        else:
            trunc_res = Truncator.truncate_diff(content, 10000, "Commit")
            truncated = trunc_res["content"]
            is_truncated = trunc_res["truncated"]

        returned_lines = len(truncated.split("\n"))

        next_page_hint = None
        if is_truncated:
            next_page_hint = (
                "This content was truncated due to token budget. "
                "Specify a start_line range to fetch the next slice."
            )

        return {
            "content": truncated,
            "truncated": is_truncated,
            "metadata": {
                "total_items": total_lines,
                "returned_items": returned_lines,
            },
            "next_page_hint": next_page_hint,
        }