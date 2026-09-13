import json
from dataclasses import replace

from aigineering.business.claim_review_workers import ReviewStageWorker
from aigineering.business.claim_review_contracts import (
    expected_draft_checks,
    expected_gate_receipt,
)
from aigineering.core.provenance import sign_asset
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.protocol.types import Candidate, Contract

ACTORS = {
    "assessor": "actor:assessor",
    "reviser": "actor:reviser",
    "gate": "actor:gate",
    "semantic": "actor:semantic",
    "acceptor": "actor:acceptor",
}


def _asset(name, value, signer):
    return sign_asset(
        build_control_plane_asset(name=name, content=json.dumps(value)), signer
    )


def _bundle(*, max_revisions=1):
    source = _asset(
        "research_result",
        {
            "claims": [{"id": "c1", "text": "Observed."}],
            "source_excerpts": [{"id": "e1", "text": "Observed."}],
        },
        "actor:source",
    )
    draft = _asset(
        "draft_report",
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "original_text": "Observed.",
                    "evidence_excerpt_ids": ["e1"],
                    "evidence_bindings": [{"excerpt_id": "e1", "quote": "Observed."}],
                }
            ]
        },
        "actor:draft",
    )
    policy = _asset(
        "review_policy",
        {
            "schema": "claim-review-policy-v1",
            "max_revisions": max_revisions,
            "actors": ACTORS,
        },
        "actor:policy",
    )
    checks_data = expected_draft_checks(
        {"research_result": source, "draft_report": draft, "review_policy": policy}
    )
    checks = _asset("draft_checks", checks_data, ACTORS["assessor"])
    report = _asset(
        "review_report",
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "original_text": "Observed.",
                    "evidence_excerpt_ids": ["e1"],
                    "evidence_bindings": [{"excerpt_id": "e1", "quote": "Observed."}],
                }
            ]
        },
        ACTORS["reviser"],
    )
    gate = _asset(
        "structural_receipt",
        expected_gate_receipt(
            {
                "research_result": source,
                "draft_report": draft,
                "review_policy": policy,
                "draft_checks": checks,
                "review_report": report,
            }
        ),
        ACTORS["gate"],
    )
    semantic = _asset(
        "semantic_review",
        {
            "schema": "claim-semantic-review-v1",
            "source_asset_id": source.id,
            "report_asset_id": report.id,
            "policy_asset_id": policy.id,
            "gate_receipt_asset_id": gate.id,
            "verdict": "accepted",
            "reason": "checked",
        },
        ACTORS["semantic"],
    )
    assets = [source, draft, policy, checks, report, gate, semantic]
    return Contract(
        id="task:accept",
        parent_id="task:root",
        inputs=tuple(a.name for a in assets),
        outputs=("acceptance_receipt",),
        context_asset_ids=(source.id, draft.id, policy.id),
    ), assets


class _Delegate:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def invoke(self, contract, disclosed_assets):
        self.calls += 1
        return Candidate(
            worker_id="model",
            raw_output=self.raw,
            metadata={"total_tokens": 7},
        )


def _stage(role, contract, assets):
    output = {
        "assessor": "draft_checks",
        "reviser": "review_report",
        "gate": "structural_receipt",
        "semantic": "semantic_review",
    }[role]
    inputs = {
        "assessor": ("research_result", "draft_report", "review_policy"),
        "reviser": ("research_result", "draft_report", "review_policy", "draft_checks"),
        "gate": (
            "research_result",
            "draft_report",
            "review_policy",
            "draft_checks",
            "review_report",
        ),
        "semantic": (
            "research_result",
            "draft_report",
            "review_policy",
            "draft_checks",
            "review_report",
            "structural_receipt",
        ),
    }[role]
    return replace(contract, inputs=inputs, outputs=(output,))


def test_assessor_detects_replaced_original_text():
    contract, assets = _bundle()
    draft = next(a for a in assets if a.name == "draft_report")
    changed = sign_asset(
        replace(
            draft,
            content=json.dumps(
                {"claims": [{"claim_id": "c1", "original_text": "changed"}]}
            ),
        ),
        ACTORS["reviser"],
    )
    assets[assets.index(draft)] = changed
    candidate = ReviewStageWorker(ACTORS["assessor"], "assessor").invoke(
        _stage("assessor", contract, assets), assets
    )
    output = json.loads(
        json.loads(candidate.raw_output.removeprefix("/exec "))["outputs"][
            "draft_checks"
        ]
    )
    assert output["passed"] is False


def test_reviser_delegates_once_and_preserves_metadata():
    contract, assets = _bundle()
    delegate = _Delegate('/exec {"outputs":{"review_report":"{}"}}')
    candidate = ReviewStageWorker(ACTORS["reviser"], "reviser", delegate).invoke(
        _stage("reviser", contract, assets), assets
    )
    assert delegate.calls == 1
    assert candidate.metadata["total_tokens"] == 7


def test_reviser_rejects_plan_and_revision_inputs_without_delegate_call():
    contract, assets = _bundle()
    delegate = _Delegate("/plan {}")
    worker = ReviewStageWorker(ACTORS["reviser"], "reviser", delegate)
    candidate = worker.invoke(_stage("reviser", contract, assets), assets)
    assert delegate.calls == 1
    assert candidate.raw_output.startswith("/fail ")
    assert "rejected_model_action" in str(candidate.metadata)

    delegate = _Delegate('/exec {"outputs":{"review_report":"{}"}}')
    bad_contract = replace(
        _stage("reviser", contract, assets),
        inputs=_stage("reviser", contract, assets).inputs + ("review_report",),
    )
    candidate = ReviewStageWorker(ACTORS["reviser"], "reviser", delegate).invoke(
        bad_contract, assets
    )
    assert delegate.calls == 0
    assert candidate.raw_output.startswith("/fail ")


def test_semantic_missing_or_wrong_gate_fails_before_delegate():
    contract, assets = _bundle()
    delegate = _Delegate('/exec {"outputs":{"semantic_review":"{}"}}')
    worker = ReviewStageWorker(ACTORS["semantic"], "semantic", delegate)
    gate = next(a for a in assets if a.name == "structural_receipt")
    variants = [
        assets[: assets.index(gate)] + assets[assets.index(gate) + 1 :],
        list(assets),
    ]
    variants[1][variants[1].index(gate)] = _asset(
        "structural_receipt", {"bad": True}, ACTORS["gate"]
    )
    for variant in variants:
        candidate = worker.invoke(_stage("semantic", contract, variant), variant)
        assert candidate.raw_output.startswith("/fail ")
    assert delegate.calls == 0


def test_revision_bound_and_system_failure_are_fail_closed():
    contract, assets = _bundle(max_revisions=2)
    delegate = _Delegate('/exec {"outputs":{"review_report":"{}"}}')
    candidate = ReviewStageWorker(ACTORS["reviser"], "reviser", delegate).invoke(
        _stage("reviser", contract, assets), assets
    )
    assert delegate.calls == 0 and candidate.raw_output.startswith("/fail ")

    system = Contract(
        id="task:system-fail",
        origin="system",
        description=json.dumps({"method": "fail", "payload": {"reason": "stop"}}),
        outputs=("failure_report",),
    )
    delegate = _Delegate("must-not-run")
    candidate = ReviewStageWorker(ACTORS["semantic"], "semantic", delegate).invoke(
        system, []
    )
    assert delegate.calls == 0 and candidate.raw_output.startswith("/exec ")
