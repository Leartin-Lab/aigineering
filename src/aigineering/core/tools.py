"""Tool registry with serializable specs and private handlers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aigineering.protocol.types import ToolSpec

ToolHandler = Callable[[Mapping[str, Any]], str]


class ToolRegistry:
    """Registry that exposes tool specs while keeping handlers private."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if not spec.name:
            raise ValueError("tool name must be non-empty")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def get_spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def list_specs(self, scope: list[str] | None = None) -> list[ToolSpec]:
        if scope is None:
            return list(self._specs.values())
        allowed = set(scope)
        return [spec for name, spec in self._specs.items() if name in allowed]

    def run(self, name: str, args: Mapping[str, Any]) -> str:
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"unknown tool '{name}'")
        return handler(args)
