"""Independent exact-rational oracle for synthetic calibration reports."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from fractions import Fraction
import hashlib
import json
import re

CASE_KEYS = {"schema", "case_id", "sensor_id", "samples"}
STANDARD_KEYS = {"schema", "standard_id", "max_abs_error_C"}
SAMPLE_KEYS = {"sample_id", "time_utc", "reference_C", "reading", "unit"}
REPORT_KEYS = {
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
NORMALIZED_SAMPLE_KEYS = {
    "sample_id",
    "time_utc",
    "reference_C",
    "reading_C",
    "bias_C",
    "source_evidence",
}
FIELD_ROLES = ["time_utc", "reference_C", "reading", "unit"]
STANDARD_FIELDS = ["max_abs_error_C"]
DECIMAL_PATTERN = re.compile(r"-?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?\Z")
TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z"
)
_SCALE = 1_000_000


class _DuplicateKeyError(ValueError):
    pass


def _pairs_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _mapping(value, where: str):
    if isinstance(value, str):
        try:
            value = json.loads(
                value,
                object_pairs_hook=_pairs_without_duplicates,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON constant: {value}")
                ),
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{where} is not strict JSON: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    return dict(value)


def _exact_keys(value, keys: set[str], where: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be an object")
    if set(value) != keys:
        raise ValueError(f"{where} has missing or unexpected fields")


def _text(value, where: str, limit=128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{where} must be a nonempty bounded string")
    return value


def _number(value, where: str, *, nonnegative=False) -> Fraction:
    if not isinstance(value, str) or DECIMAL_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{where} must be a bounded decimal string")
    number = Fraction(value)
    if nonnegative and number < 0:
        raise ValueError(f"{where} must be nonnegative")
    return number


def _timestamp(value, where: str) -> datetime:
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{where} must use YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(f"{where} is not a valid UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError(f"{where} is not canonical UTC")
    return parsed


def _validate_inputs(case_value, standard_value):
    case = _mapping(case_value, "case")
    standard = _mapping(standard_value, "standard")
    _exact_keys(case, CASE_KEYS, "case")
    _exact_keys(standard, STANDARD_KEYS, "standard")
    if case["schema"] != "thermal-calibration-case-v1":
        raise ValueError("unsupported case schema")
    if standard["schema"] != "thermal-calibration-standard-v1":
        raise ValueError("unsupported standard schema")
    _text(case["case_id"], "case.case_id", 64)
    _text(case["sensor_id"], "case.sensor_id", 64)
    _text(standard["standard_id"], "standard.standard_id", 64)
    threshold = _number(
        standard["max_abs_error_C"], "standard.max_abs_error_C", nonnegative=True
    )
    samples = case["samples"]
    if not isinstance(samples, list) or not 2 <= len(samples) <= 256:
        raise ValueError("case.samples must contain 2 to 256 rows")
    seen = set()
    validated = []
    previous = None
    for index, sample in enumerate(samples):
        _exact_keys(sample, SAMPLE_KEYS, f"case.samples[{index}]")
        sample_id = _text(sample["sample_id"], f"case.samples[{index}].sample_id", 64)
        if sample_id in seen:
            raise ValueError("sample_id values must be unique")
        seen.add(sample_id)
        timestamp = _timestamp(sample["time_utc"], f"case.samples[{index}].time_utc")
        if previous is not None and timestamp <= previous:
            raise ValueError("sample timestamps must increase strictly")
        previous = timestamp
        reference = _number(sample["reference_C"], "reference_C")
        reading = _number(sample["reading"], "reading")
        unit = sample["unit"]
        if not isinstance(unit, str) or unit not in {"C", "F", "K"}:
            raise ValueError("unit must be C, F, or K")
        validated.append((sample, timestamp, reference, reading))
    return case, standard, threshold, validated


def _canonical_hash(value) -> str:
    serialized = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _to_celsius(reading: Fraction, unit: str) -> Fraction:
    if unit == "C":
        return reading
    if unit == "F":
        return (reading - 32) * Fraction(5, 9)
    return reading - Fraction(27315, 100)


def _round_half_even(value: Fraction) -> str:
    numerator = value.numerator * _SCALE
    denominator = value.denominator
    lower, remainder = divmod(numerator, denominator)
    doubled = remainder * 2
    if doubled > denominator or (doubled == denominator and lower % 2 != 0):
        rounded = lower + 1
    else:
        rounded = lower
    if rounded == 0:
        return "0.000000"
    sign = "-" if rounded < 0 else ""
    whole, fractional = divmod(abs(rounded), _SCALE)
    return f"{sign}{whole}.{fractional:06d}"


def _expected_report(case, standard, threshold, validated):
    case_hash = _canonical_hash(case)
    standard_hash = _canonical_hash(standard)
    exact_rows = []
    for sample, _timestamp_value, reference, reading in validated:
        reading_c = _to_celsius(reading, sample["unit"])
        bias = reading_c - reference
        exact_rows.append((sample, reference, reading_c, bias))
    rows = [
        {
            "sample_id": sample["sample_id"],
            "time_utc": sample["time_utc"],
            "reference_C": _round_half_even(reference),
            "reading_C": _round_half_even(reading_c),
            "bias_C": _round_half_even(bias),
            "source_evidence": {
                "case_sha256": case_hash,
                "sample_id": sample["sample_id"],
                "field_roles": list(FIELD_ROLES),
            },
        }
        for sample, reference, reading_c, bias in exact_rows
    ]
    biases = [entry[3] for entry in exact_rows]
    start = validated[0][1]
    end = validated[-1][1]
    elapsed = (end - start).days * 86400 + (end - start).seconds
    drift = (biases[-1] - biases[0]) * Fraction(3600, elapsed)
    max_error = max(abs(value) for value in biases)
    return {
        "schema": "thermal-calibration-report-v1",
        "case_id": case["case_id"],
        "sensor_id": case["sensor_id"],
        "case_sha256": case_hash,
        "standard_sha256": standard_hash,
        "standard_id": standard["standard_id"],
        "sample_count": len(validated),
        "samples": rows,
        "aggregate_evidence": {
            "case_sha256": case_hash,
            "standard_sha256": standard_hash,
            "sample_ids": [sample[0]["sample_id"] for sample in exact_rows],
            "standard_fields": list(STANDARD_FIELDS),
        },
        "drift_C_per_hour": _round_half_even(drift),
        "max_abs_error_C": _round_half_even(max_error),
        "within_tolerance": max_error <= threshold,
    }


def _strict_equal(left, right) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _strict_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _strict_equal(lvalue, rvalue) for lvalue, rvalue in zip(left, right)
        )
    return left == right


def evaluate_report(case, standard, report) -> dict:
    """Check a report against independently recomputed exact source evidence.

    `case`, `standard`, and `report` may be mappings or JSON object strings.
    Raw strings are parsed with duplicate-key rejection, allowing the external
    evaluator to inspect Asset content without losing duplicate-key evidence.
    """
    parsed_case, parsed_standard, threshold, validated = _validate_inputs(
        case, standard
    )
    case_hash = _canonical_hash(parsed_case)
    standard_hash = _canonical_hash(parsed_standard)
    try:
        parsed_report = _mapping(report, "report")
    except ValueError as exc:
        error = (
            "duplicate_json_key" if "duplicate JSON key" in str(exc) else "report_json"
        )
        return {
            "passed": False,
            "errors": [error],
            "checked_case_sha256": case_hash,
            "checked_standard_sha256": standard_hash,
            "checked_sample_count": len(validated),
        }

    expected = _expected_report(parsed_case, parsed_standard, threshold, validated)
    errors = []
    if set(parsed_report) != REPORT_KEYS:
        errors.append("report_shape")
    if parsed_report.get("samples") != expected["samples"] or not _strict_equal(
        parsed_report.get("samples"), expected["samples"]
    ):
        errors.append("sample_values_or_evidence")
    if not _strict_equal(
        parsed_report.get("aggregate_evidence"), expected["aggregate_evidence"]
    ):
        errors.append("aggregate_evidence")
    for field, code in (
        ("case_sha256", "case_hash"),
        ("standard_sha256", "standard_hash"),
        ("drift_C_per_hour", "drift"),
        ("max_abs_error_C", "maximum_error"),
        ("within_tolerance", "tolerance_decision"),
    ):
        if not _strict_equal(parsed_report.get(field), expected[field]):
            errors.append(code)
    for field in ("schema", "case_id", "sensor_id", "standard_id", "sample_count"):
        if not _strict_equal(parsed_report.get(field), expected[field]):
            errors.append("report_identity")
            break
    return {
        "passed": not errors,
        "errors": errors,
        "checked_case_sha256": case_hash,
        "checked_standard_sha256": standard_hash,
        "checked_sample_count": len(validated),
    }
