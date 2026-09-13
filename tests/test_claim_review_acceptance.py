import json
from dataclasses import replace

import pytest

from aigineering.business.claim_review_acceptance import create_acceptance_worker
from aigineering.business.claim_review_contracts import (
    expected_draft_checks,
    expected_gate_receipt,
)
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.provenance import sign_asset
from aigineering.protocol.types import Contract


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


def _bundle(*, semantic_verdict="accepted", same_actor=False, max_revisions=1):
    source = _asset(
        "research_result",
        {
            "claims": [{"id": "c1", "text": "Observed."}],
            "source_excerpts": [{"id": "e1", "text": "Observed."}],
        },
        "actor:source",
    )
    draft_value = {
        "claims": [
            {
                "claim_id": "c1",
                "original_text": "Observed.",
                "evidence_excerpt_ids": ["e1"],
                "evidence_bindings": [{"excerpt_id": "e1", "quote": "Observed."}],
            }
        ]
    }
    draft = _asset("draft_report", draft_value, ACTORS["reviser"])
    policy = _asset(
        "review_policy",
        {
            "schema": "claim-review-policy-v1",
            "max_revisions": max_revisions,
            "actors": (
                {**ACTORS, "semantic": ACTORS["reviser"]} if same_actor else ACTORS
            ),
        },
        "actor:policy",
    )
    inputs = {"research_result": source, "draft_report": draft, "review_policy": policy}
    checks = expected_draft_checks(inputs)
    draft_checks = _asset("draft_checks", checks, ACTORS["assessor"])
    report = _asset("review_report", draft_value, ACTORS["reviser"])
    inputs.update({"draft_checks": draft_checks, "review_report": report})
    gate = _asset("structural_receipt", expected_gate_receipt(inputs), ACTORS["gate"])
    inputs["structural_receipt"] = gate
    semantic = _asset(
        "semantic_review",
        {
            "schema": "claim-semantic-review-v1",
            "source_asset_id": source.id,
            "report_asset_id": report.id,
            "policy_asset_id": policy.id,
            "gate_receipt_asset_id": gate.id,
            "verdict": semantic_verdict,
            "reason": "checked" if semantic_verdict == "accepted" else "rejected",
        },
        ACTORS["semantic"],
    )
    inputs["semantic_review"] = semantic
    contract = Contract(
        id="task:accept",
        parent_id="task:v5:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        description='{"acceptance_target":"task:v5:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
        inputs=tuple(inputs),
        outputs=("acceptance_receipt",),
        context_asset_ids=(source.id, draft.id, policy.id),
    )
    return contract, list(inputs.values())


def test_acceptance_emits_exact_attestation_and_evidence_ids():
    contract, assets = _bundle()
    candidate = create_acceptance_worker(worker_id=ACTORS["acceptor"]).invoke(
        contract, assets
    )
    assert candidate.raw_output.startswith("/attest ")
    payload = json.loads(candidate.raw_output.removeprefix("/attest "))
    assert (
        payload["contract_id"]
        == "task:v5:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert payload["output_name"] == "review_report"
    assert payload["asset_id"] == next(
        a.id for a in assets if a.name == "review_report"
    )
    assert payload["evidence_asset_ids"] == sorted(
        [assets[0].id, assets[1].id, assets[2].id]
    )
    assert payload["policy_version"] == "claim-review-closure-v1"
    assert json.loads(payload["outputs"]["acceptance_receipt"])["revision_depth"] == 1


def test_acceptance_accepts_prose_and_embedded_json_repeating_same_target():
    contract, assets = _bundle()
    target_id = contract.parent_id
    contract = replace(
        contract,
        description=(
            f"Approve the review for {target_id}. "
            + json.dumps({"acceptance_target": target_id})
        ),
    )

    candidate = create_acceptance_worker(worker_id=ACTORS["acceptor"]).invoke(
        contract, assets
    )

    assert candidate.raw_output.startswith("/attest ")


@pytest.mark.parametrize(
    "description",
    [
        ("Approve task:v5:" + "a" * 64 + " and task:v5:" + "b" * 64),
        "Approve this review without naming a canonical target.",
    ],
)
def test_acceptance_rejects_multiple_or_missing_target_ids(description):
    contract, assets = _bundle()
    contract = replace(contract, description=description)

    candidate = create_acceptance_worker(worker_id=ACTORS["acceptor"]).invoke(
        contract, assets
    )

    assert candidate.raw_output.startswith("/fail ")


def test_acceptance_rejects_semantic_asset_with_wrong_actual_signer():
    contract, assets = _bundle()
    semantic = next(asset for asset in assets if asset.name == "semantic_review")
    wrong_signer = sign_asset(semantic, "actor:wrong-semantic")
    assets[assets.index(semantic)] = wrong_signer

    candidate = create_acceptance_worker(worker_id=ACTORS["acceptor"]).invoke(
        contract, assets
    )

    assert candidate.raw_output.startswith("/fail ")


@pytest.mark.parametrize(
    "change", ["missing", "wrong", "semantic", "revision", "actor"]
)
def test_acceptance_fails_closed(change):
    contract, assets = _bundle(
        semantic_verdict="rejected" if change == "semantic" else "accepted",
        same_actor=change == "actor",
        max_revisions=2 if change == "revision" else 1,
    )
    if change == "missing":
        assets = [asset for asset in assets if asset.name != "structural_receipt"]
    elif change == "wrong":
        report = next(asset for asset in assets if asset.name == "review_report")
        assets[assets.index(report)] = _asset(
            "review_report", {"bad": True}, ACTORS["reviser"]
        )
    candidate = create_acceptance_worker(worker_id=ACTORS["acceptor"]).invoke(
        contract, assets
    )
    assert candidate.raw_output.startswith("/fail ")


def test_acceptance_rejects_duplicate_semantic_keys_and_body_signer_spoof():
    contract, assets = _bundle()
    semantic = next(asset for asset in assets if asset.name == "semantic_review")
    spoofed = _asset(
        "semantic_review",
        {
            "schema": "claim-semantic-review-v1",
            "source_asset_id": assets[0].id,
            "report_asset_id": next(a.id for a in assets if a.name == "review_report"),
            "policy_asset_id": assets[2].id,
            "gate_receipt_asset_id": next(
                a.id for a in assets if a.name == "structural_receipt"
            ),
            "verdict": "accepted",
            "reason": "checked",
            "signed_by": ACTORS["acceptor"],
        },
        ACTORS["semantic"],
    )
    assets[assets.index(semantic)] = spoofed
    assert (
        create_acceptance_worker(worker_id=ACTORS["acceptor"])
        .invoke(contract, assets)
        .raw_output.startswith("/fail ")
    )

    duplicate = json.dumps(
        {
            "schema": "claim-semantic-review-v1",
            "source_asset_id": assets[0].id,
            "report_asset_id": next(a.id for a in assets if a.name == "review_report"),
            "policy_asset_id": assets[2].id,
            "gate_receipt_asset_id": next(
                a.id for a in assets if a.name == "structural_receipt"
            ),
            "verdict": "accepted",
            "reason": "checked",
        }
    ).replace('"reason": "checked"', '"reason": "checked", "reason": "forged"')
    from dataclasses import replace

    duplicate_asset = sign_asset(
        replace(
            next(asset for asset in assets if asset.name == "semantic_review"),
            content=duplicate,
        ),
        ACTORS["semantic"],
    )
    assets[assets.index(spoofed)] = duplicate_asset
    assert (
        create_acceptance_worker(worker_id=ACTORS["acceptor"])
        .invoke(contract, assets)
        .raw_output.startswith("/fail ")
    )
