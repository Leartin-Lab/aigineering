"""Tests for tool registry."""

import pytest

from aigineering.core.tools import ToolRegistry
from aigineering.core.tool_schema import validate_value
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


def test_tool_spec_freezes_output_schema_and_retains_compatible_defaults():
    spec = ToolSpec(name="lookup")

    assert spec.version == "0.1.0"
    assert spec.output_schema == {}
    assert spec.max_output_bytes == 1_048_576


def test_tool_registry_validates_input_schema_before_handler():
    registry = ToolRegistry()
    called = False

    def handler(_args):
        nonlocal called
        called = True
        return "never"

    registry.register(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
        ),
        handler,
    )

    with pytest.raises(ValueError, match="required property 'key'"):
        registry.run("lookup", {})
    assert called is False


def test_tool_registry_validates_json_output_schema_and_byte_limit():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="lookup",
            version="2",
            output_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            max_output_bytes=64,
        ),
        lambda _args: '{"value":"ok"}',
    )
    assert registry.run("lookup", {}) == '{"value":"ok"}'

    registry.register(
        ToolSpec(
            name="bad-json",
            output_schema={"type": "object"},
        ),
        lambda _args: "not-json",
    )
    with pytest.raises(ValueError, match="valid JSON"):
        registry.run("bad-json", {})

    registry.register(
        ToolSpec(name="large", max_output_bytes=3),
        lambda _args: "四字",
    )
    with pytest.raises(ValueError, match="max_output_bytes"):
        registry.run("large", {})


def test_tool_registry_rejects_unsupported_schema_keyword():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="unsupported JSON schema keyword"):
        registry.register(
            ToolSpec(name="bad", input_schema={"pattern": "x"}), lambda _args: "ok"
        )


def test_tool_registry_uses_json_equality_for_nested_const_and_enum():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="lookup",
            input_schema={
                "type": "object",
                "properties": {
                    "const_value": {
                        "const": {"enabled": True, "values": [1, {"count": 2}]}
                    },
                    "enum_value": {
                        "enum": [{"enabled": True, "values": [1, {"count": 2}]}]
                    },
                },
                "required": ["const_value", "enum_value"],
            },
        ),
        lambda _args: "ok",
    )

    equivalent = {
        "const_value": {"enabled": True, "values": (1.0, {"count": 2.0})},
        "enum_value": {"enabled": True, "values": [1.0, {"count": 2.0}]},
    }
    assert registry.run("lookup", equivalent) == "ok"

    with pytest.raises(ValueError, match="must equal const"):
        registry.run(
            "lookup",
            {
                "const_value": {"enabled": 1, "values": [1, {"count": 2}]},
                "enum_value": equivalent["enum_value"],
            },
        )
    with pytest.raises(ValueError, match="must match enum"):
        registry.run(
            "lookup",
            {
                "const_value": equivalent["const_value"],
                "enum_value": {"enabled": True, "values": [True, {"count": 2}]},
            },
        )


@pytest.mark.parametrize(
    "keyword",
    [
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
    ],
)
def test_tool_registry_rejects_null_schema_keyword_before_installing_handler(keyword):
    registry = ToolRegistry()
    called = False

    def handler(_args):
        nonlocal called
        called = True
        return "unexpected"

    with pytest.raises(ValueError, match="must not be null"):
        registry.register(
            ToolSpec(name="bad", input_schema={keyword: None}),
            handler,
        )

    assert registry.list_specs() == []
    with pytest.raises(KeyError, match="unknown tool"):
        registry.run("bad", {})
    assert called is False


def test_tool_schema_allows_null_const():
    validate_value(None, {"const": None})


def test_invalid_schema_registration_preserves_existing_handler():
    registry = ToolRegistry()
    registry.register(ToolSpec(name="lookup"), lambda _args: "original")
    called = False

    def replacement(_args):
        nonlocal called
        called = True
        return "replacement"

    with pytest.raises(ValueError, match="must not be null"):
        registry.register(
            ToolSpec(name="lookup", input_schema={"minLength": None}),
            replacement,
        )

    assert registry.run("lookup", {}) == "original"
    assert called is False


@pytest.mark.parametrize(
    "schema",
    [
        {"type": ["string", "null"]},
        {"type": "object", "required": [{}]},
        {"type": "string", "minLength": 2, "maxLength": 1},
        {"type": "array", "minItems": 2, "maxItems": 1},
        {"type": "number", "minimum": 2, "maximum": 1},
    ],
)
def test_registry_rejects_ambiguous_or_impossible_schema_documents(schema):
    registry = ToolRegistry()

    with pytest.raises(ValueError):
        registry.register(ToolSpec(name="bad", input_schema=schema), lambda _args: "ok")
