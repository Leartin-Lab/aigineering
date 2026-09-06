"""Small deterministic JSON-schema subset used by registered tools."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any

DEFAULT_MAX_OUTPUT_BYTES = 1_048_576

_SUPPORTED_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
    }
)
_SUPPORTED_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)


class ToolSchemaValidationError(ValueError):
    """A deterministic tool input or output failed schema validation."""

    retryable = False


class ToolOutputLimitError(ValueError):
    """A tool returned more UTF-8 bytes than its contract permits."""

    retryable = False

    def __init__(self, actual: int, limit: int) -> None:
        self.actual = actual
        self.limit = limit
        self.result_bytes = actual
        super().__init__(f"tool output exceeds max_output_bytes ({actual} > {limit})")


def validate_schema_document(schema: Mapping[str, Any], *, path: str = "$") -> None:
    """Validate the supported, deterministic schema vocabulary itself."""

    if not isinstance(schema, Mapping):
        raise ValueError(f"{path} must be a JSON-schema object")
    for key in schema:
        if key not in _SUPPORTED_KEYWORDS:
            raise ValueError(f"unsupported JSON schema keyword '{key}' at {path}")
        if key != "const" and schema[key] is None:
            raise ValueError(f"{path}.{key} must not be null")

    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str) or schema_type not in _SUPPORTED_TYPES
    ):
        raise ValueError(f"unsupported JSON schema type '{schema_type}' at {path}")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            raise ValueError(f"{path}.properties must be an object")
        for name, child in properties.items():
            if not isinstance(name, str) or not isinstance(child, Mapping):
                raise ValueError(f"{path}.properties must map names to schema objects")
            validate_schema_document(child, path=f"{path}.properties.{name}")

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise ValueError(f"{path}.required must be an array")
        if any(not isinstance(name, str) for name in required) or len(
            set(required)
        ) != len(required):
            raise ValueError(f"{path}.required must contain unique string names")
        if properties is not None:
            unknown = [name for name in required if name not in properties]
            if unknown:
                raise ValueError(
                    f"{path}.required names undeclared property '{unknown[0]}'"
                )

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        raise ValueError(f"{path}.additionalProperties must be a boolean")

    items = schema.get("items")
    if items is not None:
        if not isinstance(items, Mapping):
            raise ValueError(f"{path}.items must be a schema object")
        validate_schema_document(items, path=f"{path}.items")

    enum = schema.get("enum")
    if enum is not None and (
        not isinstance(enum, Sequence) or isinstance(enum, (str, bytes)) or not enum
    ):
        raise ValueError(f"{path}.enum must be a non-empty array")

    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        value = schema.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError(f"{path}.{key} must be a non-negative integer")
    for key in ("minimum", "maximum"):
        value = schema.get(key)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(value)
        ):
            raise ValueError(f"{path}.{key} must be a finite number")
    if schema.get("minLength", 0) > schema.get("maxLength", math.inf):
        raise ValueError(f"{path}.minLength must not exceed maxLength")
    if schema.get("minItems", 0) > schema.get("maxItems", math.inf):
        raise ValueError(f"{path}.minItems must not exceed maxItems")
    if schema.get("minimum", -math.inf) > schema.get("maximum", math.inf):
        raise ValueError(f"{path}.minimum must not exceed maximum")


def _type_matches(value: Any, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, Mapping)
    if schema_type == "array":
        return isinstance(value, (list, tuple))
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "number":
        return (
            isinstance(value, Real)
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return value is None


def _json_structural_equal(left: Any, right: Any) -> bool:
    """Compare JSON-like values without conflating booleans and numbers."""

    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, Real) and isinstance(right, Real):
        return left == right
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if len(left) != len(right) or left.keys() != right.keys():
            return False
        return all(_json_structural_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
            return False
        return len(left) == len(right) and all(
            _json_structural_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> None:
    schema_type = schema.get("type")
    if schema_type is not None and not _type_matches(value, schema_type):
        raise ToolSchemaValidationError(f"{path} must be of type {schema_type}")
    if "const" in schema and not _json_structural_equal(value, schema["const"]):
        raise ToolSchemaValidationError(f"{path} must equal const")
    if "enum" in schema and not any(
        _json_structural_equal(value, candidate) for candidate in schema["enum"]
    ):
        raise ToolSchemaValidationError(f"{path} must match enum")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ToolSchemaValidationError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ToolSchemaValidationError(f"{path} exceeds maxLength")
    if isinstance(value, (list, tuple)):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ToolSchemaValidationError(f"{path} has fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ToolSchemaValidationError(f"{path} exceeds maxItems")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _validate_value(item, item_schema, f"{path}[{index}]")
    if isinstance(value, Real) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolSchemaValidationError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolSchemaValidationError(f"{path} exceeds maximum")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                raise ToolSchemaValidationError(
                    f"{path} missing required property '{name}'"
                )
        for name, child in properties.items():
            if name in value:
                _validate_value(value[name], child, f"{path}.{name}")
        if schema.get("additionalProperties") is False:
            extra = next((name for name in value if name not in properties), None)
            if extra is not None:
                raise ToolSchemaValidationError(
                    f"{path} has unexpected property '{extra}'"
                )


def validate_value(value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
    validate_schema_document(schema, path=path)
    _validate_value(value, schema, path)


def _reject_non_finite_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value}")


def validate_json_text(
    result: str, schema: Mapping[str, Any], *, path: str = "$"
) -> None:
    try:
        value = json.loads(result, parse_constant=_reject_non_finite_constant)
    except (TypeError, ValueError) as error:
        raise ToolSchemaValidationError(
            f"{path} must be valid JSON: {error}"
        ) from error
    validate_value(value, schema, path=path)
