import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .framework import LlmTool
from .utils import validate_path

logger = logging.getLogger(__name__)


class ReadPromptTool(LlmTool):
    def name(self) -> str:
        return "read_prompt"

    def description(self) -> str:
        return "Read a specific prompt file from the prompt registry (e.g., 'mm.md', 'locking.md')."

    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the prompt file (e.g., 'patterns/BPF-001.md').",
                },
            },
            "required": ["name"],
        }

    def call(self, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        prompts_path = context.prompts_path
        if prompts_path is None:
            return {"error": "read_prompt tool is not available"}

        name = args.get("name")
        if not name:
            return {"error": "Missing prompt name"}

        try:
            path = validate_path(name, prompts_path)
        except ValueError as e:
            return {"error": str(e)}

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Failed to read prompt file: %s", e)
            return {"error": f"Failed to read prompt file: {e}"}

        return {"content": content}