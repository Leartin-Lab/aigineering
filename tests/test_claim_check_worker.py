"""Fleet-facing tests for the structural claim-check Worker."""

import json

from aigineering.business.claim_check_worker import create_structural_gate_worker
from aigineering.business.claim_checks import check_original_text
from aigineering.protocol.types import Asset, Contract


def _asset(name, content, *, promptable=True, view="original"):
    return Asset(
        id=f"asset:{name}",
        name=name,
        content=json.dumps(content),
        promptable=promptable,
        disclosure_view=view,
    )


def _report(source, *, original=None, evidence=None):
    return {
        "claims": [
            {
                "claim_id": "claim-1",
                "original_text": original or source["claims"][0]["text"],
                "verdict": "supported",
                "evidence_excerpt_ids": evidence or ["excerpt-1"],
                "evidence_bindings": [
                    {
                        "excerpt_id": "excerpt-1",
                        "quote": source["source_excerpts"][0]["text"],
                    }
                ],
                "rationale": "The excerpt supports the claim.",
                "revised_text": source["claims"][0]["text"],
            }
        ],
        "revised_summary": "summary",
        "limitations": ["limitation"],
    }


def _contract(*, inputs=("research_result", "review_report")):
    return Contract(
        id="task:gate",
        name="claim-check",
        inputs=inputs,
        outputs=("receipt",),
    )


def test_structural_gate_returns_receipt_with_exact_asset_bindings():
    source = {
        "claims": [
            {
                "id": "claim-1",
                "text": "Observed association.",
                "source_excerpt_ids": ["excerpt-1"],
            }
        ],
        "source_excerpts": [{"id": "excerpt-1", "text": "Observed association."}],
    }
    report = _report(source)
    candidate = create_structural_gate_worker(worker_id="gate").invoke(
        _contract(),
        [_asset("research_result", source), _asset("review_report", report)],
    )
    payload = json.loads(candidate.raw_output.removeprefix("/exec "))
    receipt = json.loads(payload["outputs"]["receipt"])
    assert receipt == {
        "report_asset_id": "asset:review_report",
        "source_asset_id": "asset:research_result",
        "structural_checks_passed": True,
        "semantic_checked": False,
    }


def test_structural_gate_fails_closed_for_replaced_claim_and_bad_disclosure():
    source = {
        "claims": [
            {
                "id": "claim-1",
                "text": "Observed association.",
                "source_excerpt_ids": ["excerpt-1"],
            }
        ],
        "source_excerpts": [{"id": "excerpt-1", "text": "Observed association."}],
    }
    worker = create_structural_gate_worker(worker_id="gate")
    replaced = worker.invoke(
        _contract(),
        [
            _asset("research_result", source),
            _asset("review_report", _report(source, original="Different claim.")),
        ],
    )
    assert replaced.raw_output.startswith("/fail ")

    withheld = worker.invoke(
        _contract(),
        [
            _asset("research_result", source, promptable=False),
            _asset("review_report", _report(source)),
        ],
    )
    assert withheld.raw_output.startswith("/fail ")


def test_original_text_checker_is_strict_about_claim_identity():
    source = {
        "claims": [
            {
                "id": "claim-1",
                "text": "Observed association.",
                "source_excerpt_ids": [],
            }
        ],
        "source_excerpts": [],
    }
    result = check_original_text(
        source,
        {
            "claims": [{"claim_id": "claim-1", "original_text": "Different claim."}],
        },
    )
    assert result["passed"] is False
