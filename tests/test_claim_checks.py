"""Pure structural checks for claim provenance and evidence bindings."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from aigineering.business.claim_checks import (
    check_evidence_bindings,
    check_original_text,
)


SOURCE = json.loads(
    (
        Path(__file__).parents[1]
        / "examples/claim-evidence-review/research-result.json"
    ).read_text()
)


def valid_report() -> dict:
    return {
        "claims": [
            {
                "claim_id": claim["id"],
                "original_text": claim["text"],
                "evidence_excerpt_ids": list(claim["source_excerpt_ids"]),
                "evidence_bindings": [
                    {
                        "excerpt_id": excerpt_id,
                        "quote": next(
                            excerpt["text"][:20]
                            for excerpt in SOURCE["source_excerpts"]
                            if excerpt["id"] == excerpt_id
                        ),
                    }
                    for excerpt_id in claim["source_excerpt_ids"]
                ],
            }
            for claim in SOURCE["claims"]
        ]
    }


def test_original_text_accepts_exact_claims_and_rejects_changed_text():
    report = valid_report()
    assert check_original_text(SOURCE, report) == {
        "check": "original-text-v1",
        "passed": True,
        "findings": "",
        "semantic_checked": False,
    }
    report["claims"][0]["original_text"] += " changed"
    result = check_original_text(SOURCE, report)
    assert not result["passed"]
    assert "original-text-mismatch:claim-1" in result["findings"]


def test_original_text_rejects_missing_duplicate_and_unicode_change():
    report = valid_report()
    report["claims"].pop()
    result = check_original_text(SOURCE, report)
    assert "claim-id-set-mismatch" in result["findings"]

    report = valid_report()
    report["claims"].append(deepcopy(report["claims"][0]))
    result = check_original_text(SOURCE, report)
    assert "duplicate-report-claim-id" in result["findings"]

    report = valid_report()
    report["claims"][0]["original_text"] = (
        "Students who sleep at least seven hours perform better on examinations.\u0301"
    )
    assert not check_original_text(SOURCE, report)["passed"]


def test_evidence_bindings_accept_literal_quotes_and_reject_missing_bindings():
    report = valid_report()
    assert check_evidence_bindings(SOURCE, report)["passed"]
    for claim in report["claims"]:
        claim.pop("evidence_bindings")
    result = check_evidence_bindings(SOURCE, report)
    assert not result["passed"]
    assert "missing-evidence-bindings:claim-1" in result["findings"]


def test_evidence_bindings_reject_unknown_duplicate_and_forged_quotes():
    report = valid_report()
    binding = report["claims"][0]["evidence_bindings"][0]
    binding["quote"] = "not in the source"
    result = check_evidence_bindings(SOURCE, report)
    assert "quote-not-substring:claim-1:excerpt-results" in result["findings"]

    report = valid_report()
    report["claims"][0]["evidence_excerpt_ids"] = ["excerpt-results", "missing"]
    report["claims"][0]["evidence_bindings"].append(
        {"excerpt_id": "missing", "quote": "anything"}
    )
    result = check_evidence_bindings(SOURCE, report)
    assert "unknown-evidence-excerpt-id:claim-1" in result["findings"]

    report = valid_report()
    duplicate = deepcopy(report["claims"][0]["evidence_bindings"][0])
    report["claims"][0]["evidence_bindings"].append(duplicate)
    result = check_evidence_bindings(SOURCE, report)
    assert "duplicate-evidence-binding:claim-1" in result["findings"]


def test_evidence_bindings_allow_multiple_distinct_quotes_for_one_excerpt():
    report = valid_report()
    report["claims"][0]["evidence_bindings"].append(
        {"excerpt_id": "excerpt-results", "quote": "Results:"}
    )
    assert check_evidence_bindings(SOURCE, report)["passed"]


def test_checks_fail_closed_for_malformed_and_oversized_input():
    for source, report in [(None, {}), ({"claims": "bad"}, {}), (SOURCE, None)]:
        assert not check_original_text(source, report)["passed"]
        assert not check_evidence_bindings(source, report)["passed"]

    oversized = {"claims": [], "source_excerpts": [], "padding": "x" * (1 << 20)}
    assert not check_original_text(oversized, {"claims": []})["passed"]
    assert not check_evidence_bindings(oversized, {"claims": []})["passed"]

    assert (
        "empty-source-claims"
        in check_original_text({"claims": []}, {"claims": []})["findings"]
    )
    assert (
        "empty-source-excerpts"
        in check_evidence_bindings(
            {"claims": [{"id": "c", "text": "x"}], "source_excerpts": []},
            {"claims": []},
        )["findings"]
    )

    nested: list = []
    for _ in range(34):
        nested = [nested]
    assert (
        "input-too-deep"
        in check_original_text({"claims": [], "nested": nested}, {"claims": []})[
            "findings"
        ]
    )

    cyclic: list = []
    cyclic.append(cyclic)
    assert (
        "cyclic-input"
        in check_original_text({"claims": [], "nested": cyclic}, {"claims": []})[
            "findings"
        ]
    )


def test_historical_claim_reports_preserve_original_text_only_after_initial():
    root = Path(__file__).parents[1] / "reports/data/063-runtime-native-cases"
    initial = json.loads((root / "claims-initial.json").read_text())
    intermediate = json.loads((root / "claims-intermediate.json").read_text())
    tightened = json.loads((root / "claims-citation-tightened.json").read_text())
    assert not check_original_text(SOURCE, initial["final_output"]["content"])["passed"]
    assert check_original_text(SOURCE, intermediate["final_output"]["content"])[
        "passed"
    ]
    assert check_original_text(SOURCE, tightened["final_output"]["content"])["passed"]


def test_historical_reports_without_bindings_fail_closed():
    root = Path(__file__).parents[1] / "reports/data/063-runtime-native-cases"
    for filename in ("claims-initial.json", "claims-intermediate.json"):
        report = json.loads((root / filename).read_text())["final_output"]["content"]
        result = check_evidence_bindings(SOURCE, report)
        assert not result["passed"]
        assert "missing-evidence-bindings:claim-1" in result["findings"]
