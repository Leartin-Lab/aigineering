"""Pure, strict declarations for reusable method packages.

Method packages are an adapter format.  They describe ordinary task inputs,
outputs, and routing requirements; they do not introduce runtime authority or
new kernel effects.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from aigineering.core.acceptance import validate_output_shape
from aigineering.core.authority import matched_reserved_prefix
from aigineering.core.ids import canonical_json
from aigineering.protocol.immutability import deep_freeze, deep_thaw

SCHEMA = "method-package-v1"
CASES_SCHEMA = "method-cases-v1"
MAX_PACKAGE_BYTES = 256 * 1024
MAX_DEPTH = 32
MAX_CASES = 32
MAX_CONTEXT_ASSETS = 32
MAX_DEPENDENCIES = 32
_SAFE_INTEGER = 2**53 - 1
_SLOT_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.-]{0,63}$")
_ASSET_ID_PREFIXES = (
    "asset:v1:",
    "asset:",
    "assertion:",
    "content:",
    "definition:",
    "cap:",
)
_REQUIREMENT_FIELDS = (
    "tool_scope",
    "worker_capabilities",
    "worker_pools",
    "delegation_capabilities",
    "delegation_pools",
)
_PACKAGE_FIELDS = {
    "schema",
    "name",
    "version",
    "description",
    "instructions",
    "inputs",
    "outputs",
    "requirements",
    "dependencies",
    "context_asset_ids",
    "examples",
}
_CASE_FIELDS = {"schema", "name", "cases"}
_EXAMPLE_FIELDS = {"name", "inputs", "expected"}


@dataclass(frozen=True)
class MethodPackage:
    schema: str
    name: str
    version: str
    description: str
    instructions: str
    inputs: tuple[str, ...]
    outputs: Mapping[str, object]
    requirements: Mapping[str, tuple[str, ...]]
    dependencies: tuple[str, ...]
    context_asset_ids: tuple[str, ...]
    examples: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "outputs", deep_freeze(dict(self.outputs)))
        object.__setattr__(self, "requirements", deep_freeze(dict(self.requirements)))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "context_asset_ids", tuple(self.context_asset_ids))
        object.__setattr__(self, "examples", deep_freeze(tuple(self.examples)))


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key!r}")
        result[key] = value
    return result


def _check_json_values(value: object, *, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ValueError("method package nesting exceeds 32 levels")
    if isinstance(value, float):
        raise ValueError("floating-point numbers are not allowed")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and abs(value) > _SAFE_INTEGER
    ):
        raise ValueError("integer exceeds the safe JSON integer range")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            _check_json_values(item, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_json_values(item, depth=depth + 1)


def _canonical_value(value: object) -> object:
    """Normalize object keys for canonical collision detection."""
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized = key if not isinstance(key, str) else key.encode().decode()
            # NFC normalization is deliberately local to this adapter so the
            # existing language-neutral canonical_json API remains unchanged.
            normalized = unicodedata.normalize("NFC", normalized)
            if normalized in result:
                raise ValueError(
                    f"JSON object keys collide after NFC normalization: {normalized!r}"
                )
            result[normalized] = _canonical_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _canonical_checked(value: object) -> str:
    _check_json_values(value)
    return canonical_json(_canonical_value(value))


def _load_object(text: str) -> dict[str, object]:
    if not isinstance(text, str):
        raise TypeError("method package text must be a string")
    if len(text.encode("utf-8")) > MAX_PACKAGE_BYTES:
        raise ValueError("method package exceeds 256 KiB")
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicates)
    except (json.JSONDecodeError, RecursionError) as exc:
        if isinstance(exc, RecursionError):
            raise ValueError("method package nesting exceeds 32 levels") from exc
        raise ValueError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError("method package must be a JSON object")
    _check_json_values(value)
    _canonical_checked(value)
    return value


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _slot(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SLOT_RE.fullmatch(value):
        raise ValueError(f"{field} must be a normal slot identifier")
    if matched_reserved_prefix(value) is not None:
        raise ValueError(f"{field} uses a protected prefix")
    return value


def _name(value: object, field: str) -> str:
    result = _required_string(value, field)
    if not _NAME_RE.fullmatch(result):
        raise ValueError(f"{field} has an invalid identifier")
    return result


def _unique_strings(
    value: object, field: str, *, limit: int | None = None
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be an array of strings")
    if limit is not None and len(value) > limit:
        raise ValueError(f"{field} has too many entries")
    if any(not item or item != item.strip() for item in value):
        raise ValueError(f"{field} entries must be non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} entries must be unique")
    return tuple(value)


def _asset_ids(value: object, field: str, limit: int) -> tuple[str, ...]:
    result = _unique_strings(value, field, limit=limit)
    for item in result:
        if (
            any(char.isspace() or ord(char) < 32 for char in item)
            or not item.startswith(_ASSET_ID_PREFIXES)
            or not item.split(":", 2)[-1]
        ):
            raise ValueError(f"{field} entries must be exact Asset IDs")
    return result


def _validate_shape(shape: object, output_name: str) -> None:
    """Validate the existing acceptance shape language through its public API."""
    if shape is None:
        return
    if shape == "string":
        probe: object = ""
    elif shape == "nonempty_string":
        probe = "x"
    elif shape == "number":
        probe = 0
    elif shape == "boolean":
        probe = False
    elif isinstance(shape, Mapping):
        probe = {str(key): _shape_probe(value) for key, value in shape.items()}
    elif isinstance(shape, list) and len(shape) == 1:
        probe = [_shape_probe(shape[0])]
    else:
        raise ValueError(f"outputs.{output_name} uses an unsupported output shape")
    try:
        validate_output_shape(
            {"output_shapes": {output_name: shape}}, output_name, json.dumps(probe)
        )
    except ValueError as exc:
        raise ValueError(
            f"outputs.{output_name} has an invalid output shape: {exc}"
        ) from exc


def _shape_probe(shape: object) -> object:
    if shape is None:
        return None
    if shape == "string":
        return ""
    if shape == "nonempty_string":
        return "x"
    if shape == "number":
        return 0
    if shape == "boolean":
        return False
    if isinstance(shape, Mapping):
        return {str(key): _shape_probe(value) for key, value in shape.items()}
    if isinstance(shape, list) and len(shape) == 1:
        return [_shape_probe(shape[0])]
    raise ValueError("unsupported output shape")


def _output_shapes(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not value:
        raise ValueError("outputs must be a non-empty object")
    result: dict[str, object] = {}
    for key, shape in value.items():
        slot = _slot(key, "output name")
        if slot in result:
            raise ValueError("outputs must have unique names")
        _validate_shape(shape, slot)
        result[slot] = deep_freeze(shape)
    return result


def _requirements(value: object) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != set(_REQUIREMENT_FIELDS):
        raise ValueError("requirements must contain exactly the five routing fields")
    return {
        field: _unique_strings(value[field], f"requirements.{field}")
        for field in _REQUIREMENT_FIELDS
    }


def _validate_example(
    value: object, package: MethodPackage | None = None
) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != _EXAMPLE_FIELDS:
        raise ValueError("each example must contain exactly name, inputs, and expected")
    name = _required_string(value["name"], "example.name")
    inputs = value["inputs"]
    expected = value["expected"]
    if not isinstance(inputs, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in inputs.items()
    ):
        raise ValueError("example.inputs must map slots to text")
    if not isinstance(expected, dict):
        raise ValueError("example.expected must be an object")
    if package is not None:
        if set(inputs) != set(package.inputs):
            raise ValueError("example.inputs must exactly match package inputs")
        if set(expected) != set(package.outputs):
            raise ValueError("example.expected must exactly match package outputs")
        for output_name, shape in package.outputs.items():
            if shape is None:
                if not isinstance(expected[output_name], str):
                    raise ValueError(
                        f"example.expected.{output_name} must be text for a null shape"
                    )
                continue
            content = json.dumps(
                expected[output_name], ensure_ascii=False, separators=(",", ":")
            )
            try:
                validate_output_shape(
                    {"output_shapes": {output_name: shape}}, output_name, content
                )
            except ValueError as exc:
                raise ValueError(
                    f"example.expected.{output_name} violates output shape"
                ) from exc
    return deep_freeze(
        {"name": name, "inputs": dict(inputs), "expected": dict(expected)}
    )


def parse_package(text: str) -> MethodPackage:
    raw = _load_object(text)
    if set(raw) - _PACKAGE_FIELDS:
        raise ValueError(
            f"unknown method package fields: {sorted(set(raw) - _PACKAGE_FIELDS)}"
        )
    required = _PACKAGE_FIELDS - {"examples"}
    missing = required - set(raw)
    if missing:
        raise ValueError(f"missing method package fields: {sorted(missing)}")
    schema = raw["schema"]
    if schema != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA!r}")
    name = _name(raw["name"], "name")
    version = _required_string(raw["version"], "version")
    if not _VERSION_RE.fullmatch(version):
        raise ValueError("version has an invalid identifier")
    description = _required_string(raw["description"], "description")
    instructions = _required_string(raw["instructions"], "instructions")
    inputs_raw = raw["inputs"]
    if not isinstance(inputs_raw, list):
        raise ValueError("inputs must be an array")
    if len(inputs_raw) > 32:
        raise ValueError("inputs has too many entries")
    inputs = tuple(_slot(item, "input name") for item in inputs_raw)
    if len(set(inputs)) != len(inputs):
        raise ValueError("inputs must be unique")
    outputs = _output_shapes(raw["outputs"])
    if len(outputs) > 32:
        raise ValueError("outputs has too many entries")
    requirements = _requirements(raw["requirements"])
    dependencies = _asset_ids(raw["dependencies"], "dependencies", MAX_DEPENDENCIES)
    context_asset_ids = _asset_ids(
        raw["context_asset_ids"], "context_asset_ids", MAX_CONTEXT_ASSETS
    )
    package = MethodPackage(
        schema=SCHEMA,
        name=name,
        version=version,
        description=description,
        instructions=instructions,
        inputs=inputs,
        outputs=outputs,
        requirements=requirements,
        dependencies=dependencies,
        context_asset_ids=context_asset_ids,
    )
    examples = raw.get("examples", [])
    if not isinstance(examples, list):
        raise ValueError("examples must be an array")
    if len(examples) > 32:
        raise ValueError("examples has too many entries")
    example_names = [item.get("name") for item in examples if isinstance(item, dict)]
    if len(example_names) != len(set(example_names)):
        raise ValueError("example names must be unique")
    object.__setattr__(
        package,
        "examples",
        deep_freeze(tuple(_validate_example(item, package) for item in examples)),
    )
    return package


def package_to_dict(package: MethodPackage) -> dict[str, object]:
    if not isinstance(package, MethodPackage):
        raise TypeError("package must be a MethodPackage")
    return deep_thaw(
        {
            "schema": package.schema,
            "name": package.name,
            "version": package.version,
            "description": package.description,
            "instructions": package.instructions,
            "inputs": package.inputs,
            "outputs": package.outputs,
            "requirements": package.requirements,
            "dependencies": package.dependencies,
            "context_asset_ids": package.context_asset_ids,
            "examples": package.examples,
        }
    )


def canonical_package(package: MethodPackage) -> str:
    if not isinstance(package, MethodPackage):
        raise TypeError("package must be a MethodPackage")
    # Reparse so direct dataclass construction cannot bypass schema checks.
    validated = parse_package(_canonical_checked(package_to_dict(package)))
    return _canonical_checked(package_to_dict(validated))


def parse_cases(text: str) -> Mapping[str, object]:
    raw = _load_object(text)
    if set(raw) != _CASE_FIELDS:
        unknown = set(raw) - _CASE_FIELDS
        missing = _CASE_FIELDS - set(raw)
        if unknown:
            raise ValueError(f"unknown method cases fields: {sorted(unknown)}")
        raise ValueError(f"missing method cases fields: {sorted(missing)}")
    if raw["schema"] != CASES_SCHEMA:
        raise ValueError(f"schema must be {CASES_SCHEMA!r}")
    name = _required_string(raw["name"], "name")
    cases = raw["cases"]
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise ValueError("cases must be a non-empty array of at most 32 items")
    return MappingProxyType(
        {
            "schema": CASES_SCHEMA,
            "name": name,
            "cases": tuple(_validate_example(item) for item in cases),
        }
    )


def validate_cases(cases: Mapping[str, object], package: MethodPackage) -> None:
    if not isinstance(cases, Mapping) or cases.get("schema") != CASES_SCHEMA:
        raise ValueError("cases must be parsed method-cases-v1 data")
    values = cases.get("cases")
    if not isinstance(values, (list, tuple)) or not values or len(values) > MAX_CASES:
        raise ValueError("cases must be a non-empty array of at most 32 items")
    for item in values:
        _validate_example(deep_thaw(item), package)
