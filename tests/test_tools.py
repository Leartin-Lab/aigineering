"""Tests for tool registry."""

import pytest

from aigineering.core.tools import ToolRegistry
from aigineering.protocol.types import ToolSpec


def test_tool_registry_exposes_specs_not_handlers():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="lookup",
            description="Look up a value.",
            input_schema={"type": "object"},
        ),
        lambda args: f"value:{args['key']}",
    )

    specs = registry.list_specs()

    assert specs == [
        ToolSpec(
            name="lookup",
            description="Look up a value.",
            input_schema={"type": "object"},
        )
    ]
    assert not hasattr(specs[0], "handler")


def test_tool_registry_runs_private_handler():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    assert registry.run("lookup", {"key": "x"}) == "value:x"


def test_tool_registry_filters_scope():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="allowed"), lambda args: "ok")
    registry.register(ToolSpec(name="hidden"), lambda args: "no")

    assert registry.list_specs(scope=["allowed"]) == [ToolSpec(name="allowed")]


def test_tool_registry_rejects_unknown_tool():
    registry = ToolRegistry()

    with pytest.raises(KeyError, match="unknown tool"):
        registry.run("missing", {})
