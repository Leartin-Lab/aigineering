"""Tool registry with serializable specs and private handlers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aigineering.core.tool_schema import (
    ToolOutputLimitError,
    ToolSchemaValidationError,
    validate_schema_document,
    validate_json_text,
    validate_value,
)
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
        validate_schema_document(spec.input_schema, path="$.input_schema")
        validate_schema_document(spec.output_schema, path="$.output_schema")
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
        spec = self._specs.get(name)
        handler = self._handlers.get(name)
        if handler is None:
            raise KeyError(f"unknown tool '{name}'")
        validate_value(args, spec.input_schema, path="$.input")
        result = handler(args)
        if not isinstance(result, str):
            raise TypeError("tool result must be a string")
        result_bytes = len(result.encode("utf-8"))
        if result_bytes > spec.max_output_bytes:
            raise ToolOutputLimitError(result_bytes, spec.max_output_bytes)
        if spec.output_schema:
            try:
                validate_json_text(result, spec.output_schema, path="$.output")
            except ToolSchemaValidationError as error:
                error.result_bytes = result_bytes
                raise
        return result
