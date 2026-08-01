import subprocess
import logging
from typing import Any, Dict, List, Optional

from .framework import LlmTool
from .truncator import Truncator

logger = logging.getLogger(__name__)


class GitReadFilesTool(LlmTool):
    def name(self) -> str:
        return "git_read_files"

    def description(self) -> str:
        return "Read files at a Git revision."

    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "revision": {
                    "type": "string",
                    "description": "Git SHA or reference to read from.",
                },
                "files": {
                    "type": "array",
                    "description": "List of files to read (max 10).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path."},
                            "start_line": {"type": "integer", "description": "Focus start line (optional)."},
                            "end_line": {"type": "integer", "description": "Focus end line (optional)."},
                        },
                        "required": ["path"],
                    },
                },
            },
            "required": ["revision", "files"],
        }

    def normalize_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(args)
        if "mode" not in normalized:
            normalized["mode"] = "raw"
        files = normalized.get("files", [])
        if isinstance(files, list):
            for file_args in files:
                if isinstance(file_args, dict):
                    if "start_line" not in file_args:
                        file_args["start_line"] = None
                    if "end_line" not in file_args:
                        file_args["end_line"] = None
        return normalized

    def call(self, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        revision = args.get("revision")
        if not revision:
            return {"error": "Missing revision"}

        files = args.get("files")
        if not files or not isinstance(files, list):
            return {"error": "Missing files"}

        if len(files) > 10:
            return {"error": "Too many files requested. Maximum limit is 10 files per request."}

        results = []
        for file_args in files:
            if not isinstance(file_args, dict):
                results.append({"error": "Invalid file entry"})
                continue

            path_str = file_args.get("path", "")
            if not path_str:
                results.append({"error": "Missing path", "path": ""})
                continue

            start_line = file_args.get("start_line")
            end_line = file_args.get("end_line")

            try:
                val = self._read_single_file(context, revision, path_str, start_line, end_line)
                if isinstance(val, dict):
                    val["path"] = path_str
                results.append(val)
            except Exception as e:
                logger.error("Failed to read file %s: %s", path_str, e)
                results.append({"path": path_str, "error": str(e)})

        return {"results": results}

    def _read_single_file(
        self,
        context: Any,
        revision: str,
        path_str: str,
        start_line: Optional[int],
        end_line: Optional[int],
    ) -> Dict[str, Any]:
        revision_virt = context.virtualize_ref(revision)

        if path_str.startswith("-"):
            return {"error": f"Invalid path name: {path_str}"}

        if start_line is not None and end_line is not None and start_line > end_line:
            return {"error": f"Invalid range: start_line ({start_line}) > end_line ({end_line})"}

        cmd = [
            "git", "-C", str(context.worktree_path),
            "show", f"{revision_virt}:{path_str}",
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
            return {"error": f"git show failed: {e}"}

        if result.returncode != 0:
            return {
                "error": f"git show failed to read file {path_str} at {revision}: {result.stderr.strip()}"
            }

        content = result.stdout
        lines = content.split("\n")
        total_lines = len(lines)

        if start_line is not None:
            start_line = max(1, min(start_line, total_lines))
        if end_line is not None:
            end_line = max(1, min(end_line, total_lines))

        if start_line is not None and end_line is not None:
            start = max(start_line, 1) - 1
            end = min(end_line, total_lines)
        elif start_line is not None:
            start = max(start_line, 1) - 1
            end = total_lines
        elif end_line is not None:
            start = 0
            end = min(end_line, total_lines)
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
            }

        slice_lines = lines[start:end]
        result_str = "\n".join(slice_lines)

        trunc_result = Truncator.truncate_sequential(result_str, 20000)
        truncated = trunc_result["content"]
        lines_kept = trunc_result["lines_kept"]
        is_truncated_content = trunc_result["truncated"]

        start_idx = start + 1

        if is_truncated_content and lines_kept > 0:
            end_idx = start + lines_kept
        else:
            end_idx = end

        returned_items = lines_kept if (is_truncated_content and lines_kept > 0) else len(slice_lines)

        next_page_hint = None
        if is_truncated_content:
            next_page_hint = (
                f"Only lines {start_idx}-{end_idx} of {total_lines} are shown due to token limits. "
                f"To read the remaining lines, call git_read_files with start_line={end_idx + 1}."
            )

        return {
            "content": truncated,
            "truncated": is_truncated_content,
            "metadata": {
                "total_items": total_lines,
                "returned_items": returned_items,
                "start_index": start_idx,
                "end_index": end_idx,
            },
            "next_page_hint": next_page_hint,
            "lines_read": returned_items,
            "total_lines": total_lines,
            "start_line": start_idx,
            "end_line": end_idx,
        }