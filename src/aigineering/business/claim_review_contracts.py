"""Pure input and receipt contracts for the bounded claim-review example."""

from aigineering.business.claim_check_worker import _available, _candidate, _read_json
from aigineering.business.claim_checks import (
    check_evidence_bindings,
    check_original_text,
)
from aigineering.core.provenance import verify_asset_seal
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.identity import canonical_json

BASE_INPUTS = ("research_result", "draft_report", "review_policy")


def failure_completion(worker_id, contract):
    payload = method_payload(contract)
    if contract.origin == "system" and payload.get("method") == "fail":
        if len(contract.outputs) != 1:
            raise ValueError("invalid failure-report contract")
        import json

        return _candidate(
            worker_id,
            "exec",
            {"outputs": {contract.outputs[0]: json.dumps(payload, sort_keys=True)}},
        )
    return None


def read_inputs(contract, assets, names):
    selected = {}
    for name in names:
        matches = [a for a in assets if a.name == name]
        if name not in contract.inputs or len(matches) != 1:
            raise ValueError("missing or ambiguous review input: " + name)
        asset = matches[0]
        if not _available(asset) or not verify_asset_seal(asset):
            raise ValueError("unavailable review input: " + name)
        selected[name] = asset
    for name in BASE_INPUTS:
        if name not in selected or selected[name].id not in contract.context_asset_ids:
            raise ValueError("review base input is not frozen: " + name)
    policy = _read_json(selected["review_policy"].content)
    if not isinstance(policy, dict) or set(policy) != {
        "schema",
        "max_revisions",
        "actors",
    }:
        raise ValueError("invalid review policy")
    if (
        policy["schema"] != "claim-review-policy-v1"
        or type(policy["max_revisions"]) is not int
        or policy["max_revisions"] != 1
    ):
        raise ValueError("unsupported revision bound")
    actors = policy["actors"]
    if not isinstance(actors, dict) or set(actors) != {
        "assessor",
        "reviser",
        "gate",
        "semantic",
        "acceptor",
    }:
        raise ValueError("invalid review actors")
    if (
        any(not isinstance(actor, str) or not actor for actor in actors.values())
        or len(set(actors.values())) != 5
    ):
        raise ValueError("review actors must be distinct")
    return selected, policy


def require_actor(asset, actor):
    # Authenticity comes from the committed Candidate path. This seal only checks
    # consistency of the disclosed view; it is not a public-key signature.
    if (
        not _available(asset)
        or not verify_asset_seal(asset)
        or asset.signed_by != actor
    ):
        raise ValueError("unexpected review artifact producer: " + asset.name)


def expected_draft_checks(inputs):
    source = inputs["research_result"]
    draft = inputs["draft_report"]
    source_data, draft_data = _read_json(source.content), _read_json(draft.content)
    checks = [
        check_original_text(source_data, draft_data),
        check_evidence_bindings(source_data, draft_data),
    ]
    return {
        "schema": "claim-draft-checks-v1",
        "source_asset_id": source.id,
        "draft_asset_id": draft.id,
        "policy_asset_id": inputs["review_policy"].id,
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
        "max_revisions": 1,
    }


def validate_draft_checks(inputs, policy):
    require_actor(inputs["draft_checks"], policy["actors"]["assessor"])
    actual = _read_json(inputs["draft_checks"].content)
    if canonical_json(actual) != canonical_json(expected_draft_checks(inputs)):
        raise ValueError("draft check receipt differs from exact inputs")


def expected_gate_receipt(inputs):
    return {
        "schema": "claim-review-gate-v1",
        "source_asset_id": inputs["research_result"].id,
        "draft_asset_id": inputs["draft_report"].id,
        "report_asset_id": inputs["review_report"].id,
        "policy_asset_id": inputs["review_policy"].id,
        "draft_checks_asset_id": inputs["draft_checks"].id,
        "revision_depth": 1,
        "structural_checks_passed": True,
        "semantic_checked": False,
    }


def validate_final_report(inputs, policy):
    validate_draft_checks(inputs, policy)
    require_actor(inputs["review_report"], policy["actors"]["reviser"])
    source = _read_json(inputs["research_result"].content)
    report = _read_json(inputs["review_report"].content)
    findings = [
        check_original_text(source, report),
        check_evidence_bindings(source, report),
    ]
    if any(not item["passed"] for item in findings):
        raise ValueError(
            "final structural checks failed: "
            + "\n".join(item["findings"] for item in findings if not item["passed"])
        )


def validate_gate(inputs, policy):
    validate_final_report(inputs, policy)
    require_actor(inputs["structural_receipt"], policy["actors"]["gate"])
    receipt = _read_json(inputs["structural_receipt"].content)
    # Exact JSON types matter: True must not masquerade as revision_depth=1.
    if (
        canonical_json(receipt) != canonical_json(expected_gate_receipt(inputs))
        or type(receipt.get("revision_depth")) is not int
        or receipt.get("structural_checks_passed") is not True
        or receipt.get("semantic_checked") is not False
    ):
        raise ValueError("gate receipt differs from exact inputs or revision bound")
