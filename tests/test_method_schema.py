import json

import pytest

from aigineering.methods.schema import (
    CASES_SCHEMA,
    SCHEMA,
    MethodPackage,
    canonical_package,
    package_to_dict,
    parse_cases,
    parse_package,
    validate_cases,
)


def _raw(**overrides):
    value = {
        "schema": SCHEMA,
        "name": "literature.review-v1",
        "version": "1.0.0",
        "description": "Review supplied evidence.",
        "instructions": "Read inputs and produce the report.",
        "inputs": ["source_text", "rubric"],
        "outputs": {
            "report": {"summary": "nonempty_string", "score": "number"},
            "trace": None,
        },
        "requirements": {
            "tool_scope": [],
            "worker_capabilities": ["review"],
            "worker_pools": [],
            "delegation_capabilities": [],
            "delegation_pools": [],
        },
        "dependencies": ["asset:v1:method-dependency"],
        "context_asset_ids": ["asset:v1:context"],
        "examples": [
            {
                "name": "basic",
                "inputs": {"source_text": "text", "rubric": "rubric"},
                "expected": {
                    "report": {"summary": "good", "score": 1},
                    "trace": "trace text",
                },
            }
        ],
    }
    value.update(overrides)
    return value


def test_roundtrip_canonical_and_null_shape():
    package = parse_package(json.dumps(_raw()))

    assert isinstance(package, MethodPackage)
    assert package.schema == SCHEMA
    assert package.inputs == ("source_text", "rubric")
    assert package.outputs["trace"] is None
    assert package_to_dict(package) == _raw()
    assert canonical_package(package) == canonical_package(
        parse_package(canonical_package(package))
    )


def test_nested_values_are_frozen():
    package = parse_package(json.dumps(_raw()))

    with pytest.raises(TypeError):
        package.outputs["report"] = "string"  # type: ignore[index]
    with pytest.raises(TypeError):
        package.requirements["tool_scope"] += ("write",)  # type: ignore[index]
    with pytest.raises(TypeError):
        package.examples[0]["inputs"] = {}  # type: ignore[index]


@pytest.mark.parametrize("field", ["path", "permissions", "unknown"])
def test_unknown_and_permission_like_fields_are_rejected(field):
    value = _raw()
    value[field] = "forbidden"
    with pytest.raises(ValueError, match="unknown"):
        parse_package(json.dumps(value))


def test_duplicate_json_keys_are_rejected():
    text = json.dumps(_raw()).replace(
        '"schema": "method-package-v1"',
        '"schema": "method-package-v1", "schema": "method-package-v1"',
        1,
    )
    with pytest.raises(ValueError, match="duplicate"):
        parse_package(text)


def test_nfc_colliding_keys_are_rejected():
    value = _raw(outputs={"e\u0301": "string", "é": "string"})
    text = json.dumps(value)
    with pytest.raises(ValueError, match="NFC"):
        parse_package(text)


def test_invalid_slots_and_protected_outputs_are_rejected():
    value = _raw(outputs={"Bad Slot": "string"})
    with pytest.raises(ValueError, match="slot"):
        parse_package(json.dumps(value))
    value = _raw(outputs={"_tool_obs_result": "string"})
    with pytest.raises(ValueError, match="slot|protected"):
        parse_package(json.dumps(value))


def test_cases_require_exact_slots_and_shapes():
    package = parse_package(json.dumps(_raw()))
    cases = parse_cases(
        json.dumps(
            {
                "schema": CASES_SCHEMA,
                "name": "review-cases",
                "cases": [
                    {
                        "name": "bad",
                        "inputs": {"source_text": "x"},
                        "expected": {
                            "report": {"summary": "ok", "score": 1},
                            "trace": "x",
                        },
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="exactly match"):
        validate_cases(cases, package)

    wrong_shape = parse_cases(
        json.dumps(
            {
                "schema": CASES_SCHEMA,
                "name": "review-cases",
                "cases": [
                    {
                        "name": "bad",
                        "inputs": {"source_text": "x", "rubric": "r"},
                        "expected": {
                            "report": {"summary": "", "score": "high"},
                            "trace": "x",
                        },
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="shape"):
        validate_cases(wrong_shape, package)


def test_cases_are_immutable_and_bounded():
    cases = parse_cases(
        json.dumps(
            {
                "schema": CASES_SCHEMA,
                "name": "small",
                "cases": [{"name": "x", "inputs": {}, "expected": {}}],
            }
        )
    )
    assert isinstance(cases["cases"], tuple)
    with pytest.raises(TypeError):
        cases["cases"] = ()  # type: ignore[index]

    too_many = {
        "schema": CASES_SCHEMA,
        "name": "too-many",
        "cases": [{"name": str(i), "inputs": {}, "expected": {}} for i in range(33)],
    }
    with pytest.raises(ValueError, match="32"):
        parse_cases(json.dumps(too_many))


def test_size_depth_and_number_rules():
    with pytest.raises(ValueError, match="256 KiB"):
        parse_package(json.dumps(_raw(instructions="x" * (256 * 1024))))
    with pytest.raises(ValueError, match="floating"):
        parse_package(json.dumps(_raw(outputs={"result": 1.25})))
    with pytest.raises(ValueError, match="safe JSON"):
        parse_package(json.dumps(_raw(outputs={"result": 2**53})))

    with pytest.raises(ValueError, match="invalid identifier"):
        parse_package(json.dumps(_raw(version="../secret")))

    nested = "string"
    for _ in range(33):
        nested = [nested]
    with pytest.raises(ValueError, match="nesting"):
        parse_package(json.dumps(_raw(outputs={"result": nested})))
