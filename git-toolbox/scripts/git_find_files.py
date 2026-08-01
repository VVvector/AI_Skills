import subprocess
import logging
import re
from typing import Any, Dict, List, Optional

from .framework import LlmTool
from .utils import glob_to_regex

logger = logging.getLogger(__name__)


class GitFindFilesTool(LlmTool):
    def name(self) -> str:
        return "git_find_files"

    def description(self) -> str:
        return "Find files matching a glob pattern in a specific Git revision."

    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "revision": {
                    "type": "string",
                    "description": "The Git commit SHA or reference to search in.",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match (e.g., '*.rs' or 'src/**/mod.rs').",
                },
                "path": {
                    "type": "string",
                    "description": "Optional relative path to restrict the search.",
                },
            },
            "required": ["revision", "pattern"],
        }

    def normalize_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(args)
        if "path" not in normalized:
            normalized["path"] = None
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

        if revision_virt.startswith("-"):
            return {"error": "Invalid revision"}

        cmd = [
            "git", "-C", str(context.worktree_path),
            "ls-tree", "-r", "--name-only", revision_virt,
        ]

        if path_str and path_str != "." and path_str != "":
            if path_str.startswith("-"):
                return {"error": "Invalid path parameter"}
            cmd.extend(["--", path_str])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            logger.error("git ls-tree failed: %s", e)
            return {"error": f"git ls-tree failed: {e}"}

        if result.returncode != 0:
            return {"error": f"git ls-tree failed: {result.stderr.strip()}"}

        try:
            regex_str = glob_to_regex(pattern)
            regex = re.compile(regex_str)
        except Exception as e:
            return {"error": f"Invalid glob pattern: {e}"}

        matched_files: List[str] = []
        total_found = 0
        is_truncated = False

        for line in result.stdout.split("\n"):
            if regex.search(line):
                total_found += 1
                if len(matched_files) < 1000:
                    matched_files.append(line)
                else:
                    is_truncated = True
                    break

        truncated_files = "\n".join(matched_files)

        next_page_hint = None
        message = None
        if is_truncated:
            next_page_hint = "More than 1000 files matched. Please use a narrower path or pattern prefix to restrict search."
            message = "Output truncated to 1000 files."

        return {
            "content": truncated_files,
            "truncated": is_truncated,
            "metadata": {
                "total_items": total_found,
                "returned_items": 1000 if is_truncated else total_found,
            },
            "next_page_hint": next_page_hint,
            "files": truncated_files,
            "total_found": total_found,
            "message": message,
        }