import subprocess
import logging
from typing import Any, Dict, List, Optional

from .framework import LlmTool

logger = logging.getLogger(__name__)


class GitLsTool(LlmTool):
    def name(self) -> str:
        return "git_ls"

    def description(self) -> str:
        return "List files in a directory at a specific Git revision."

    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "revision": {
                    "type": "string",
                    "description": "The Git commit SHA or reference to list from.",
                },
                "path": {
                    "type": "string",
                    "description": "Relative path to the directory (e.g., '.' or 'src/').",
                },
            },
            "required": ["revision", "path"],
        }

    def call(self, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        revision_raw = args.get("revision")
        if not revision_raw:
            return {"error": "Missing revision"}

        revision_virt = context.virtualize_ref(revision_raw)
        path_str = args.get("path")
        if not path_str:
            return {"error": "Missing path"}

        if revision_virt.startswith("-") or path_str.startswith("-"):
            return {"error": "Invalid revision or path name"}

        if not path_str or path_str == ".":
            tree_spec = revision_virt
        else:
            tree_spec = f"{revision_virt}:{path_str}"

        cmd = [
            "git", "-C", str(context.worktree_path),
            "ls-tree", tree_spec,
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
            logger.error("git ls-tree failed: %s", e)
            return {"error": f"git ls-tree failed: {e}"}

        if result.returncode != 0:
            return {"error": f"git ls-tree failed for {tree_spec}: {result.stderr.strip()}"}

        content = result.stdout
        entries: List[Dict[str, str]] = []
        for line in content.split("\n"):
            if "\t" not in line:
                continue
            parts = line.split("\t", 1)
            if len(parts) < 2:
                continue
            filename = parts[1]
            metadata = parts[0].split()
            if len(metadata) >= 2:
                ty = "dir" if metadata[1] == "tree" else "file"
                entries.append({"name": filename, "type": ty})

        total_entries = len(entries)
        truncated = total_entries > 1000
        if truncated:
            entries = entries[:1000]

        next_page_hint = None
        if truncated:
            next_page_hint = (
                "Directory listing truncated to 1000 entries. "
                "Please call git_ls with a specific subdirectory path (e.g., 'src/worker/') to see more files."
            )

        return {
            "entries": entries,
            "truncated": truncated,
            "total_entries": total_entries,
            "next_page_hint": next_page_hint,
        }