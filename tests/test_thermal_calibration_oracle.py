"""Deterministic source tools and independent Fraction oracle fixtures."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from examples.thermal_calibration.oracle import evaluate_report
from examples.thermal_calibration.tools import build_registry

ROOT = Path(__file__).resolve().parents[1] / "examples" / "thermal_calibration"


def _canonical_hash(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _case(split, entry):
    folder = ROOT / "fixtures" / split
    return _json(folder / entry["case_file"]), _json(folder / entry["standard_file"])


def _run_tools(case, standard, registry=None):
    registry = registry or build_registry()
    normalized = json.loads(
        registry.run("normalize_calibration", {"case": case, "standard": standard})
    )
    report_text = registry.run("analyze_calibration", {"normalized": normalized})
    return normalized, json.loads(report_text), report_text


def _two_point_case(case_id, final_reading):
    return {
        "schema": "thermal-calibration-case-v1",
        "case_id": case_id,
        "sensor_id": "sensor-half-even",
        "samples": [
            {
                "sample_id": "tie-start",
                "time_utc": "2026-06-01T00:00:00Z",
                "reference_C": "0.000000",
                "reading": "0.000000",
                "unit": "C",
            },
            {
                "sample_id": "tie-end",
                "time_utc": "2026-06-01T02:00:00Z",
                "reference_C": "0.000000",
                "reading": final_reading,
                "unit": "C",
            },
        ],
    }


def _standard(threshold="2.000001"):
    return {
        "schema": "thermal-calibration-standard-v1",
        "standard_id": "synthetic-temperature-calibration-half-even",
        "max_abs_error_C": threshold,
    }


def test_train_and_eval_manifests_pin_cases_and_tools_match_fraction_oracle():
    registry = build_registry()
    seen = set()
    for split in ("train", "eval"):
        manifest = _json(ROOT / "fixtures" / split / "manifest.json")
        assert manifest == {
            "schema": "thermal-calibration-fixture-manifest-v1",
            "partition": split,
            "cases": manifest["cases"],
        }
        for entry in manifest["cases"]:
            case, standard = _case(split, entry)
            assert case["case_id"] == entry["case_id"]
            assert entry["case_id"] not in seen
            seen.add(entry["case_id"])
            assert _canonical_hash(case) == entry["case_sha256"]
            assert _canonical_hash(standard) == entry["standard_sha256"]
            assert standard["standard_id"] == entry["standard_id"]
            _normalized, report, report_text = _run_tools(case, standard, registry)
            result = evaluate_report(case, standard, report_text)
            assert result["passed"] is True, (entry["case_id"], result)
            assert result["errors"] == []
            assert result["checked_sample_count"] == len(case["samples"])
            assert report["case_sha256"] == entry["case_sha256"]
            assert report["standard_sha256"] == entry["standard_sha256"]
    assert len(seen) == 5


def test_hand_calculated_mixed_unit_case_and_exact_threshold_equality():
    case, standard = _case(
        "train",
        next(
            entry
            for entry in _json(ROOT / "fixtures" / "train" / "manifest.json")["cases"]
            if entry["case_id"] == "mix-three-point"
        ),
    )
    normalized, report, report_text = _run_tools(case, standard)
    assert [row["reading_C"] for row in normalized["samples"]] == [
        "20.000000",
        "20.000000",
        "19.800000",
    ]
    assert [row["bias_C"] for row in report["samples"]] == [
        "0.000000",
        "0.000000",
        "-0.200000",
    ]
    assert report["drift_C_per_hour"] == "-0.240000"
    assert report["max_abs_error_C"] == "0.200000"
    assert report["within_tolerance"] is True
    assert evaluate_report(case, standard, report_text)["passed"] is True


@pytest.mark.parametrize(
    ("final_reading", "expected_drift"),
    [("2.000001", "1.000000"), ("-2.000001", "-1.000000")],
)
def test_fraction_oracle_and_decimal_tool_round_positive_and_negative_ties_half_even(
    final_reading, expected_drift
):
    case = _two_point_case(f"tie-{expected_drift}", final_reading)
    standard = _standard()
    _normalized, report, report_text = _run_tools(case, standard)
    assert report["drift_C_per_hour"] == expected_drift
    assert report["max_abs_error_C"] == "2.000001"
    assert report["within_tolerance"] is True
    assert evaluate_report(case, standard, report_text)["passed"] is True


@pytest.mark.parametrize(
    ("start_reading", "end_reading", "expected_drift"),
    [
        ("32.000019", "32.000022", "0.000002"),
        ("32.000022", "32.000019", "-0.000002"),
    ],
)
def test_fahrenheit_drift_exact_half_even_tie_matches_fraction_oracle(
    start_reading, end_reading, expected_drift
):
    case = {
        "schema": "thermal-calibration-case-v1",
        "case_id": f"fahrenheit-tie-{expected_drift}",
        "sensor_id": "sensor-fahrenheit-tie",
        "samples": [
            {
                "sample_id": "tie-start",
                "time_utc": "2026-06-01T00:00:00Z",
                "reference_C": "0.000000",
                "reading": start_reading,
                "unit": "F",
            },
            {
                "sample_id": "tie-end",
                "time_utc": "2026-06-01T01:06:40Z",
                "reference_C": "0.000000",
                "reading": end_reading,
                "unit": "F",
            },
        ],
    }
    standard = _standard("1.000000")
    _normalized, report, report_text = _run_tools(case, standard)
    assert report["drift_C_per_hour"] == expected_drift
    assert evaluate_report(case, standard, report_text)["passed"] is True


def test_negative_zero_is_canonicalized_without_changing_source_hash():
    entry = next(
        item
        for item in _json(ROOT / "fixtures" / "train" / "manifest.json")["cases"]
        if item["case_id"] == "negative-zero"
    )
    case, standard = _case("train", entry)
    normalized, report, _text = _run_tools(case, standard)
    assert normalized["case_sha256"] == entry["case_sha256"]
    assert normalized["samples"][0]["reading_C"] == "0.000000"
    assert normalized["samples"][0]["bias_C"] == "0.000000"
    assert report["samples"][0]["source_evidence"]["sample_id"] == "z-000"


def test_raw_error_above_threshold_can_share_display_value_but_fails():
    entry = next(
        item
        for item in _json(ROOT / "fixtures" / "eval" / "manifest.json")["cases"]
        if item["case_id"] == "near-threshold-fahrenheit"
    )
    case, standard = _case("eval", entry)
    _normalized, report, report_text = _run_tools(case, standard)
    assert report["max_abs_error_C"] == standard["max_abs_error_C"] == "0.200001"
    assert report["within_tolerance"] is False
    assert evaluate_report(case, standard, report_text)["passed"] is True


def test_correct_report_for_out_of_tolerance_sensor_is_a_passed_evaluation():
    case = _two_point_case("outside-standard", "1.000000")
    standard = _standard("0.000000")
    _normalized, report, report_text = _run_tools(case, standard)
    assert report["within_tolerance"] is False
    assert evaluate_report(case, standard, report_text) == {
        "passed": True,
        "errors": [],
        "checked_case_sha256": _canonical_hash(case),
        "checked_standard_sha256": _canonical_hash(standard),
        "checked_sample_count": 2,
    }


def test_registry_output_schema_accepts_reachable_decimal_extremes():
    case = {
        "schema": "thermal-calibration-case-v1",
        "case_id": "decimal-extremes",
        "sensor_id": "sensor-extremes",
        "samples": [
            {
                "sample_id": "positive-bias",
                "time_utc": "2026-01-01T00:00:00Z",
                "reference_C": "-999999999999.999999",
                "reading": "999999999999.999999",
                "unit": "C",
            },
            {
                "sample_id": "negative-kelvin-bias",
                "time_utc": "2026-01-01T00:00:01Z",
                "reference_C": "999999999999.999999",
                "reading": "-999999999999.999999",
                "unit": "K",
            },
        ],
    }
    standard = _standard("0.000000")
    normalized, report, report_text = _run_tools(case, standard)
    assert normalized["samples"][0]["bias_C"] == "1999999999999.999998"
    assert normalized["samples"][1]["reading_C"] == "-1000000000273.149999"
    assert report["max_abs_error_C"] == "2000000000273.149998"
    assert len(report["drift_C_per_hour"]) <= 32
    assert evaluate_report(case, standard, report_text)["passed"] is True


@pytest.mark.parametrize(
    ("case_change", "standard_change"),
    [
        (lambda case: case["samples"][0].update(unit="Rankine"), None),
        (lambda case: case["samples"][1].update(time_utc="2026-01-01T00:00:00Z"), None),
        (lambda case: case["samples"].reverse(), None),
        (
            lambda case: case["samples"][1].update(
                sample_id=case["samples"][0]["sample_id"]
            ),
            None,
        ),
        (lambda case: case["samples"][0].update(reading="1e2"), None),
        (lambda case: case["samples"][0].update(reading=20), None),
        (lambda case: case["samples"][0].update(reading=True), None),
        (lambda case: case.update(unexpected=True), None),
        (lambda case: case.update(schema="thermal-calibration-case-v2"), None),
        (None, lambda standard: standard.update(max_abs_error_C="-0.1")),
        (None, lambda standard: standard.update(unexpected=True)),
    ],
)
def test_invalid_case_and_standard_shapes_are_rejected(case_change, standard_change):
    case = _two_point_case("invalid-case", "1.000000")
    standard = _standard("2.000000")
    if case_change is not None:
        case_change(case)
    if standard_change is not None:
        standard_change(standard)
    with pytest.raises(ValueError):
        build_registry().run(
            "normalize_calibration", {"case": case, "standard": standard}
        )


@pytest.mark.parametrize("bad_samples", [None, ["not-an-object", {}]])
def test_oracle_bad_source_shapes_raise_value_error_not_type_error(bad_samples):
    case = _two_point_case("oracle-invalid-shape", "1.000000")
    case["samples"] = bad_samples
    with pytest.raises(ValueError):
        evaluate_report(case, _standard(), {})


@pytest.mark.parametrize("bad_unit", [[], {}, None, True, 7])
def test_oracle_unhashable_or_wrong_unit_types_raise_value_error(bad_unit):
    case = _two_point_case("oracle-invalid-unit", "1.000000")
    case["samples"][0]["unit"] = bad_unit
    with pytest.raises(ValueError):
        evaluate_report(case, _standard(), {})


@pytest.mark.parametrize("bad_unit", [[], {}, None, True, 7])
def test_tool_rejects_wrong_unit_types_without_leaking_type_error(bad_unit):
    case = _two_point_case("tool-invalid-unit", "1.000000")
    case["samples"][0]["unit"] = bad_unit
    with pytest.raises(ValueError):
        build_registry().run(
            "normalize_calibration", {"case": case, "standard": _standard()}
        )


def test_analysis_rejects_changed_missing_or_extra_normalized_evidence():
    case = _two_point_case("normalized-integrity", "1.000000")
    standard = _standard("2.000000")
    normalized, _report, _text = _run_tools(case, standard)
    mutations = []
    changed_value = deepcopy(normalized)
    changed_value["samples"][0]["reading_C"] = "99.000000"
    mutations.append(changed_value)
    missing_row = deepcopy(normalized)
    missing_row["samples"].pop()
    mutations.append(missing_row)
    extra_field = deepcopy(normalized)
    extra_field["surprise"] = "not part of the source"
    mutations.append(extra_field)
    changed_embedded_source = deepcopy(normalized)
    changed_embedded_source["case"]["samples"][0]["reading"] = "1.000001"
    mutations.append(changed_embedded_source)
    for mutation in mutations:
        with pytest.raises(ValueError, match="normalized|unexpected|missing"):
            build_registry().run("analyze_calibration", {"normalized": mutation})


@pytest.mark.parametrize(
    ("change", "error_code"),
    [
        (lambda report: report.update(unexpected="field"), "report_shape"),
        (lambda report: report["samples"].pop(), "sample_values_or_evidence"),
        (
            lambda report: report["samples"][0]["source_evidence"].update(
                sample_id="other"
            ),
            "sample_values_or_evidence",
        ),
        (
            lambda report: report["aggregate_evidence"].update(sample_ids=[]),
            "aggregate_evidence",
        ),
        (lambda report: report.update(case_sha256="0" * 64), "case_hash"),
        (lambda report: report.update(standard_sha256="0" * 64), "standard_hash"),
        (lambda report: report.update(drift_C_per_hour="0.000000"), "drift"),
        (lambda report: report.update(max_abs_error_C="0.000000"), "maximum_error"),
        (lambda report: report.update(within_tolerance=1), "tolerance_decision"),
    ],
)
def test_oracle_rejects_report_shape_evidence_hash_and_value_tampering(
    change, error_code
):
    case = _two_point_case("report-tampering", "1.000000")
    standard = _standard("2.000000")
    _normalized, report, _text = _run_tools(case, standard)
    changed = deepcopy(report)
    change(changed)
    result = evaluate_report(case, standard, changed)
    assert result["passed"] is False
    assert error_code in result["errors"]


def test_oracle_rejects_duplicate_json_keys_in_raw_report_and_inputs():
    case = _two_point_case("duplicate-json", "1.000000")
    standard = _standard("2.000000")
    duplicate_report = '{"schema":"thermal-calibration-report-v1","schema":"other"}'
    result = evaluate_report(case, standard, duplicate_report)
    assert result["passed"] is False
    assert result["errors"] == ["duplicate_json_key"]
    duplicate_case = '{"schema":"a","schema":"b"}'
    with pytest.raises(ValueError, match="duplicate JSON key"):
        evaluate_report(duplicate_case, standard, {})


def test_tool_output_limits_cover_maximum_case_size():
    samples = []
    start = datetime(2026, 1, 1)
    for index in range(256):
        timestamp = start + timedelta(seconds=index)
        samples.append(
            {
                "sample_id": f"sample-{index:03d}-" + "x" * 52,
                "time_utc": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "reference_C": "20.000000",
                "reading": "68.000000",
                "unit": "F",
            }
        )
    case = {
        "schema": "thermal-calibration-case-v1",
        "case_id": "maximum-size-case",
        "sensor_id": "sensor-" + "s" * 57,
        "samples": samples,
    }
    standard = _standard("0.000000")
    registry = build_registry()
    normalized_text = registry.run(
        "normalize_calibration", {"case": case, "standard": standard}
    )
    assert (
        len(normalized_text.encode("utf-8"))
        <= registry.get_spec("normalize_calibration").max_output_bytes
    )
    normalized = json.loads(normalized_text)
    report_text = registry.run("analyze_calibration", {"normalized": normalized})
    assert (
        len(report_text.encode("utf-8"))
        <= registry.get_spec("analyze_calibration").max_output_bytes
    )
    assert evaluate_report(case, standard, report_text)["passed"] is True


def test_tools_accept_deeply_immutable_json_container_shapes():
    from aigineering.protocol.immutability import deep_freeze

    case = _two_point_case("frozen-input", "1.000000")
    standard = _standard("2.000000")
    frozen_args = deep_freeze({"case": case, "standard": standard})
    registry = build_registry()
    normalized_text = registry.run("normalize_calibration", frozen_args)
    normalized = deep_freeze(json.loads(normalized_text))
    report_text = registry.run("analyze_calibration", {"normalized": normalized})
    assert evaluate_report(case, standard, report_text)["passed"] is True
