from abc import ABC, abstractmethod
from typing import Any, Dict, List

import json


class LlmTool(ABC):
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def description(self) -> str:
        ...

    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        ...

    def normalize_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return args

    @abstractmethod
    def call(self, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        ...


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, LlmTool] = {}

    def register(self, tool: LlmTool) -> None:
        self._tools[tool.name()] = tool

    def declarations(self) -> List[Dict[str, Any]]:
        result = []
        for tool in self._tools.values():
            result.append({
                "name": tool.name(),
                "description": tool.description(),
                "parameters": tool.parameters(),
            })
        return result

    def normalize_tool_args(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        tool = self._tools.get(name)
        if tool:
            return tool.normalize_args(args)
        return args

    def call(self, name: str, args: Dict[str, Any], context: Any) -> Dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            raise ValueError(f"Tool not found in registry: {name}")
        return tool.call(args, context)