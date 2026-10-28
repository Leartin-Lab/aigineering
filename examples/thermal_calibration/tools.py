"""Pure, bounded tools for the synthetic temperature calibration fixture."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from collections.abc import Mapping

from aigineering.core.tools import ToolRegistry
from aigineering.protocol.types import ToolSpec

CASE_KEYS = frozenset({"schema", "case_id", "sensor_id", "samples"})
STANDARD_KEYS = frozenset({"schema", "standard_id", "max_abs_error_C"})
SAMPLE_KEYS = frozenset({"sample_id", "time_utc", "reference_C", "reading", "unit"})
NORMALIZED_KEYS = frozenset(
    {
        "schema",
        "case",
        "standard",
        "case_sha256",
        "standard_sha256",
        "samples",
    }
)
NORMALIZED_SAMPLE_KEYS = frozenset(
    {"sample_id", "time_utc", "reference_C", "reading_C", "bias_C", "source_evidence"}
)
REPORT_KEYS = frozenset(
    {
        "schema",
        "case_id",
        "sensor_id",
        "case_sha256",
        "standard_sha256",
        "standard_id",
        "sample_count",
        "samples",
        "aggregate_evidence",
        "drift_C_per_hour",
        "max_abs_error_C",
        "within_tolerance",
    }
)
FIELD_ROLES = ["time_utc", "reference_C", "reading", "unit"]
STANDARD_FIELDS = ["max_abs_error_C"]
DECIMAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")
TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
MICRO_SCALE = 1_000_000
CELSIUS_DENOMINATOR = 9 * MICRO_SCALE


def _plain_json(item):
    if isinstance(item, Mapping):
        return {key: _plain_json(member) for key, member in item.items()}
    if isinstance(item, (list, tuple)):
        return [_plain_json(member) for member in item]
    return item


def _encode(value) -> str:
    return json.dumps(
        _plain_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value) -> str:
    return hashlib.sha256(_encode(value).encode("utf-8")).hexdigest()


def _exact_keys(value, keys: frozenset[str], where: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise ValueError(f"{where} keys mismatch (missing={missing}, extra={extra})")


def _text(value, where: str, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{where} must be a nonempty string up to {max_length} chars")
    return value


def _micro_units(value, where: str, *, nonnegative: bool = False) -> int:
    if not isinstance(value, str) or DECIMAL_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{where} must be a bounded decimal string")
    negative = value.startswith("-")
    unsigned = value[1:] if negative else value
    whole, _, fraction = unsigned.partition(".")
    result = int(whole) * MICRO_SCALE + int(fraction.ljust(6, "0") or "0")
    if negative:
        result = -result
    if nonnegative and result < 0:
        raise ValueError(f"{where} must be finite and nonnegative")
    return result


def _timestamp(value, where: str) -> datetime:
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{where} must use YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"{where} is not a valid UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError(f"{where} is not a canonical UTC timestamp")
    return parsed


def _validate_case(case) -> list[tuple[dict, datetime, int, int]]:
    _exact_keys(case, CASE_KEYS, "case")
    if case["schema"] != "thermal-calibration-case-v1":
        raise ValueError("case schema is unsupported")
    _text(case["case_id"], "case.case_id", max_length=64)
    _text(case["sensor_id"], "case.sensor_id", max_length=64)
    samples = case["samples"]
    if not isinstance(samples, (list, tuple)) or not 2 <= len(samples) <= 256:
        raise ValueError("case.samples must contain 2 to 256 rows")
    seen_ids: set[str] = set()
    validated = []
    previous_time = None
    for index, sample in enumerate(samples):
        where = f"case.samples[{index}]"
        _exact_keys(sample, SAMPLE_KEYS, where)
        sample_id = _text(sample["sample_id"], f"{where}.sample_id", max_length=64)
        if sample_id in seen_ids:
            raise ValueError("sample_id values must be unique")
        seen_ids.add(sample_id)
        timestamp = _timestamp(sample["time_utc"], f"{where}.time_utc")
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError("sample timestamps must increase strictly in input order")
        previous_time = timestamp
        reference = _micro_units(sample["reference_C"], f"{where}.reference_C")
        reading = _micro_units(sample["reading"], f"{where}.reading")
        if not isinstance(sample["unit"], str) or sample["unit"] not in {
            "C",
            "F",
            "K",
        }:
            raise ValueError(f"{where}.unit must be C, F, or K")
        validated.append((sample, timestamp, reference, reading))
    return validated


def _validate_standard(standard) -> int:
    _exact_keys(standard, STANDARD_KEYS, "standard")
    if standard["schema"] != "thermal-calibration-standard-v1":
        raise ValueError("standard schema is unsupported")
    _text(standard["standard_id"], "standard.standard_id", max_length=64)
    return _micro_units(
        standard["max_abs_error_C"], "standard.max_abs_error_C", nonnegative=True
    )


def _to_scaled_celsius(reading_micro_units: int, unit: str) -> int:
    """Return Celsius in exact units of 1/(9 * 10**6) degree."""
    if unit == "C":
        return reading_micro_units * 9
    if unit == "F":
        return (reading_micro_units - 32 * MICRO_SCALE) * 5
    return (reading_micro_units - 273_150_000) * 9


def _display_ratio(numerator: int, denominator: int) -> str:
    """Render an exact rational to six places using ROUND_HALF_EVEN."""
    if denominator <= 0:
        raise ValueError("display denominator must be positive")
    scaled, remainder = divmod(abs(numerator) * MICRO_SCALE, denominator)
    doubled_remainder = remainder * 2
    if doubled_remainder > denominator or (
        doubled_remainder == denominator and scaled % 2 == 1
    ):
        scaled += 1
    sign = "-" if numerator < 0 and scaled else ""
    whole, fractional = divmod(scaled, MICRO_SCALE)
    return f"{sign}{whole}.{fractional:06d}"


def _normalized(case, standard) -> dict:
    validated = _validate_case(case)
    _validate_standard(standard)
    case_hash = _digest(case)
    normalized_samples = []
    for sample, _timestamp_value, reference, reading in validated:
        reading_c = _to_scaled_celsius(reading, sample["unit"])
        bias = reading_c - reference * 9
        normalized_samples.append(
            {
                "sample_id": sample["sample_id"],
                "time_utc": sample["time_utc"],
                "reference_C": _display_ratio(reference, MICRO_SCALE),
                "reading_C": _display_ratio(reading_c, CELSIUS_DENOMINATOR),
                "bias_C": _display_ratio(bias, CELSIUS_DENOMINATOR),
                "source_evidence": {
                    "case_sha256": case_hash,
                    "sample_id": sample["sample_id"],
                    "field_roles": list(FIELD_ROLES),
                },
            }
        )
    return {
        "schema": "thermal-calibration-normalized-v1",
        "case": case,
        "standard": standard,
        "case_sha256": case_hash,
        "standard_sha256": _digest(standard),
        "samples": normalized_samples,
    }


def _normalize_handler(args) -> str:
    return _encode(_normalized(args["case"], args["standard"]))


def _analyze_handler(args) -> str:
    normalized = _plain_json(args["normalized"])
    _exact_keys(normalized, NORMALIZED_KEYS, "normalized")
    if normalized["schema"] != "thermal-calibration-normalized-v1":
        raise ValueError("normalized schema is unsupported")
    expected_normalized = _normalized(normalized["case"], normalized["standard"])
    if normalized != expected_normalized:
        raise ValueError("normalized payload does not match the complete source case")

    case = normalized["case"]
    standard = normalized["standard"]
    validated = _validate_case(case)
    exact_biases = [
        _to_scaled_celsius(reading, sample["unit"]) - reference * 9
        for sample, _timestamp_value, reference, reading in validated
    ]
    max_abs_error = max(abs(bias) for bias in exact_biases)
    elapsed_delta = validated[-1][1] - validated[0][1]
    elapsed = elapsed_delta.days * 86400 + elapsed_delta.seconds
    drift_numerator = (exact_biases[-1] - exact_biases[0]) * 3600
    drift_denominator = CELSIUS_DENOMINATOR * elapsed
    report = {
        "schema": "thermal-calibration-report-v1",
        "case_id": case["case_id"],
        "sensor_id": case["sensor_id"],
        "case_sha256": normalized["case_sha256"],
        "standard_sha256": normalized["standard_sha256"],
        "standard_id": standard["standard_id"],
        "sample_count": len(validated),
        "samples": normalized["samples"],
        "aggregate_evidence": {
            "case_sha256": normalized["case_sha256"],
            "standard_sha256": normalized["standard_sha256"],
            "sample_ids": [sample[0]["sample_id"] for sample in validated],
            "standard_fields": list(STANDARD_FIELDS),
        },
        "drift_C_per_hour": _display_ratio(drift_numerator, drift_denominator),
        "max_abs_error_C": _display_ratio(max_abs_error, CELSIUS_DENOMINATOR),
        "within_tolerance": max_abs_error <= _validate_standard(standard) * 9,
    }
    return _encode(report)


_STRING = {"type": "string", "minLength": 1, "maxLength": 128}
_DECIMAL_STRING = {"type": "string", "minLength": 1, "maxLength": 20}
_DISPLAY_DECIMAL_STRING = {"type": "string", "minLength": 1, "maxLength": 32}
_SOURCE_EVIDENCE_SCHEMA = {
    "type": "object",
    "required": ["case_sha256", "sample_id", "field_roles"],
    "additionalProperties": False,
    "properties": {
        "case_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "sample_id": _STRING,
        "field_roles": {
            "type": "array",
            "minItems": 4,
            "maxItems": 4,
            "items": _STRING,
        },
    },
}
_NORMALIZED_SAMPLE_SCHEMA = {
    "type": "object",
    "required": [
        "sample_id",
        "time_utc",
        "reference_C",
        "reading_C",
        "bias_C",
        "source_evidence",
    ],
    "additionalProperties": False,
    "properties": {
        "sample_id": _STRING,
        "time_utc": {"type": "string", "minLength": 20, "maxLength": 20},
        "reference_C": _DISPLAY_DECIMAL_STRING,
        "reading_C": _DISPLAY_DECIMAL_STRING,
        "bias_C": _DISPLAY_DECIMAL_STRING,
        "source_evidence": _SOURCE_EVIDENCE_SCHEMA,
    },
}
_CASE_SCHEMA = {
    "type": "object",
    "required": ["schema", "case_id", "sensor_id", "samples"],
    "additionalProperties": False,
    "properties": {
        "schema": {"type": "string", "const": "thermal-calibration-case-v1"},
        "case_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "sensor_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "samples": {
            "type": "array",
            "minItems": 2,
            "maxItems": 256,
            "items": {
                "type": "object",
                "required": [
                    "sample_id",
                    "time_utc",
                    "reference_C",
                    "reading",
                    "unit",
                ],
                "additionalProperties": False,
                "properties": {
                    "sample_id": {"type": "string", "minLength": 1, "maxLength": 64},
                    "time_utc": {"type": "string", "minLength": 20, "maxLength": 20},
                    "reference_C": _DECIMAL_STRING,
                    "reading": _DECIMAL_STRING,
                    "unit": {"type": "string", "enum": ["C", "F", "K"]},
                },
            },
        },
    },
}
_STANDARD_SCHEMA = {
    "type": "object",
    "required": ["schema", "standard_id", "max_abs_error_C"],
    "additionalProperties": False,
    "properties": {
        "schema": {
            "type": "string",
            "const": "thermal-calibration-standard-v1",
        },
        "standard_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "max_abs_error_C": _DECIMAL_STRING,
    },
}
_NORMALIZED_SCHEMA = {
    "type": "object",
    "required": [
        "schema",
        "case",
        "standard",
        "case_sha256",
        "standard_sha256",
        "samples",
    ],
    "additionalProperties": False,
    "properties": {
        "schema": {
            "type": "string",
            "const": "thermal-calibration-normalized-v1",
        },
        "case": _CASE_SCHEMA,
        "standard": _STANDARD_SCHEMA,
        "case_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "standard_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "samples": {
            "type": "array",
            "minItems": 2,
            "maxItems": 256,
            "items": _NORMALIZED_SAMPLE_SCHEMA,
        },
    },
}
_AGGREGATE_EVIDENCE_SCHEMA = {
    "type": "object",
    "required": ["case_sha256", "standard_sha256", "sample_ids", "standard_fields"],
    "additionalProperties": False,
    "properties": {
        "case_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "standard_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
        },
        "sample_ids": {
            "type": "array",
            "minItems": 2,
            "maxItems": 256,
            "items": _STRING,
        },
        "standard_fields": {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": _STRING,
        },
    },
}
_REPORT_SCHEMA = {
    "type": "object",
    "required": [
        "schema",
        "case_id",
        "sensor_id",
        "case_sha256",
        "standard_sha256",
        "standard_id",
        "sample_count",
        "samples",
        "aggregate_evidence",
        "drift_C_per_hour",
        "max_abs_error_C",
        "within_tolerance",
    ],
    "additionalProperties": False,
    "properties": {
        "schema": {"type": "string", "const": "thermal-calibration-report-v1"},
        "case_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "sensor_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "case_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "standard_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "standard_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "sample_count": {"type": "integer", "minimum": 2, "maximum": 256},
        "samples": {
            "type": "array",
            "minItems": 2,
            "maxItems": 256,
            "items": _NORMALIZED_SAMPLE_SCHEMA,
        },
        "aggregate_evidence": _AGGREGATE_EVIDENCE_SCHEMA,
        "drift_C_per_hour": _DISPLAY_DECIMAL_STRING,
        "max_abs_error_C": _DISPLAY_DECIMAL_STRING,
        "within_tolerance": {"type": "boolean"},
    },
}


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="normalize_calibration",
            description="Validate and normalize every sample in a bounded synthetic temperature calibration case.",
            version="1.0.0",
            input_schema={
                "type": "object",
                "required": ["case", "standard"],
                "additionalProperties": False,
                "properties": {"case": _CASE_SCHEMA, "standard": _STANDARD_SCHEMA},
            },
            output_schema=_NORMALIZED_SCHEMA,
            max_output_bytes=1_048_576,
        ),
        _normalize_handler,
    )
    registry.register(
        ToolSpec(
            name="analyze_calibration",
            description="Revalidate a complete normalized source and compute deterministic bias, drift, maximum error, and tolerance status.",
            version="1.0.0",
            input_schema={
                "type": "object",
                "required": ["normalized"],
                "additionalProperties": False,
                "properties": {"normalized": _NORMALIZED_SCHEMA},
            },
            output_schema=_REPORT_SCHEMA,
            max_output_bytes=1_048_576,
        ),
        _analyze_handler,
    )
    return registry
