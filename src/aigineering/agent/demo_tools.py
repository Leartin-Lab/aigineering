"""Demo tools — memory-backed built-in tools for tests and examples (v0.5.0-alpha.3).

These tools exercise the ToolExecutor/MCPExecutor pipeline without real
filesystem access.  Not for production use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aigineering.core.tools import ToolRegistry
from aigineering.protocol.types import ToolSpec

# Memory-backed filesystem for demo tools (NOT production).
_demo_store: dict[str, str] = {}


def register_demo_tools(registry: ToolRegistry) -> None:
    """Register minimal memory-backed demo tools on *registry*.

    Registers:

    * ``file_read`` — returns content stored at ``path``, or ``""`` if absent.
    * ``file_write`` — stores ``content`` at ``path``, returns ``"ok"``.
    * ``search`` — returns ``"Found results for: <q>"``.
    """

    def _read(args: Mapping[str, Any]) -> str:
        return _demo_store.get(args["path"], "")

    def _write(args: Mapping[str, Any]) -> str:
        _demo_store[args["path"]] = args["content"]
        return "ok"

    def _search(args: Mapping[str, Any]) -> str:
        return f"Found results for: {args.get('q', '')}"

    registry.register(
        ToolSpec(
            name="file_read",
            description="Read content from a memory-backed file.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        _read,
    )

    registry.register(
        ToolSpec(
            name="file_write",
            description="Write content to a memory-backed file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        _write,
    )

    registry.register(
        ToolSpec(
            name="search",
            description="Search for results.",
            input_schema={
                "type": "object",
                "properties": {"q": {"type": "string"}},
            },
        ),
        _search,
    )


def reset_demo_store() -> None:
    """Clear the memory-backed demo filesystem (for test isolation)."""
    _demo_store.clear()
