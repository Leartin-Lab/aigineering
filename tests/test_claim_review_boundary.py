"""SQLite integration coverage for the claim-review acceptance boundary."""

from __future__ import annotations

import json

import pytest

from aigineering.business.claim_review_acceptance import create_acceptance_worker
from aigineering.business.claim_review_contracts import (
    expected_draft_checks,
    expected_gate_receipt,
)
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import hash_contract_current
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Candidate, Contract
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.worker_hosting import authorize_worker_host


ACTORS = {
    "source": "actor:source",
    "reviser": "actor:reviser",
    "policy": "actor:policy",
    "assessor": "actor:assessor",
    "gate": "actor:gate",
    "semantic": "actor:semantic",
    "producer": "actor:reviser",
    "acceptor": "worker:acceptor",
}


class _Producer:
    worker_id = ACTORS["producer"]

    def registration(self):
        from aigineering.core.worker_routing import WorkerRegistration

        return WorkerRegistration(
            self.worker_id,
            capabilities=("claim.review.produce",),
            pools=("verification",),
        )

    def invoke(self, contract, disclosed_assets):
        del contract, disclosed_assets
        return Candidate(
            worker_id=self.worker_id,
            raw_output='/exec {"outputs":{"review_report":"{\\"claims\\":[{\\"claim_id\\":\\"c1\\",\\"original_text\\":\\"Observed.\\",\\"evidence_excerpt_ids\\":[\\"e1\\"],\\"evidence_bindings\\":[{\\"excerpt_id\\":\\"e1\\",\\"quote\\":\\"Observed.\\"}]}]}"}}',
        )


@pytest.fixture
def review_domain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(str(tmp_path / "review.db"))
    owner_signer = Ed25519Signer()
    owner_key = ActorKey(
        "human:owner",
        "owner",
        owner_signer.kind,
        owner_signer.signer_id,
        ("actor.authorize", "asset.publish", "contract.publish", "worker.register"),
    )
    genesis = initialize_genesis(
        store, create_genesis_manifest("claim-review", (owner_key,), "policy:test")
    )
    owner = CandidatePublisher(store, store, genesis, owner_key, owner_signer)
    signers = {}
    publishers = {}
    for name in ("source", "reviser", "policy", "assessor", "gate", "semantic"):
        signer = Ed25519Signer()
        actor = ACTORS[name]
        key = ActorKey(
            actor, f"{name}-key", signer.kind, signer.signer_id, ("asset.publish",)
        )
        assert owner.publish(
            (actor_authorization_effect(key),), idempotency_key=f"authorize:{name}"
        ).accepted
        signers[name] = signer
        publishers[name] = CandidatePublisher(store, store, genesis, key, signer)
    try:
        yield store, genesis, owner, publishers
    finally:
        store.close()


def _publish(publisher, asset, nonce):
    decision = publisher.publish((asset_proposal_effect(asset),), idempotency_key=nonce)
    assert decision.accepted, decision.runtime_records
    return decision.assets[0]


def _bundle(
    domain,
    *,
    broken_gate=False,
    missing_gate=False,
    acceptor_id=None,
    intermediate_obligation=False,
):
    store, genesis, owner, publishers = domain
    source = _publish(
        publishers["source"],
        build_control_plane_asset(
            name="research_result",
            content=json.dumps(
                {
                    "claims": [{"id": "c1", "text": "Observed."}],
                    "source_excerpts": [{"id": "e1", "text": "Observed."}],
                }
            ),
        ),
        "source",
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
    draft = _publish(
        publishers["reviser"],
        build_control_plane_asset(name="draft_report", content=json.dumps(draft_value)),
        "draft",
    )
    policy_value = {
        "schema": "claim-review-policy-v1",
        "max_revisions": 1,
        "actors": {
            k: (acceptor_id if k == "acceptor" and acceptor_id else ACTORS[k])
            for k in ("assessor", "reviser", "gate", "semantic", "acceptor")
        },
    }
    policy = _publish(
        publishers["policy"],
        build_control_plane_asset(
            name="review_policy", content=json.dumps(policy_value)
        ),
        "policy",
    )
    inputs = {"research_result": source, "draft_report": draft, "review_policy": policy}
    checks = _publish(
        publishers["assessor"],
        build_control_plane_asset(
            name="draft_checks", content=json.dumps(expected_draft_checks(inputs))
        ),
        "checks",
    )
    report = None
    gate = None
    if not missing_gate:
        # The real report is produced by the claimed producer below; the receipt is
        # published after that report exists.
        gate = "deferred"
    root_fields = dict(
        name="claim-review-root",
        description="produce review",
        inputs=("research_result", "draft_report", "review_policy"),
        outputs=("review_report",),
        activation="",
        budget=3 if intermediate_obligation else 2,
        tool_scope=(),
        labels=("research_result", "draft_report", "review_policy"),
        context_asset_ids=(source.id, draft.id, policy.id),
        worker_capabilities=("claim.review.produce",),
        worker_pools=("verification",),
        delegation_capabilities=("claim.review.accept",),
        delegation_pools=("verification",),
        origin="human",
        parent_id=None,
        acceptance_policy={
            "mode": "independent",
            "policy_version": "claim-review-closure-v1",
            "rubric_asset_ids": [],
            "evidence_asset_ids": sorted((source.id, draft.id, policy.id)),
            "required_attestations": 1,
            "verifier_capabilities": ["claim.review.accept"],
        },
    )
    root = Contract(id=hash_contract_current(**root_fields), **root_fields)
    root_decision = owner.publish(
        (contract_declaration_effect(root),), idempotency_key="root"
    )
    assert root_decision.accepted, root_decision.runtime_records
    intermediate = None
    if intermediate_obligation:
        child_fields = dict(
            name="claim-review-intermediate",
            description="intermediate compile obligation",
            inputs=root.inputs,
            outputs=root.outputs,
            activation="",
            budget=1,
            tool_scope=(),
            labels=root.labels,
            context_asset_ids=root.context_asset_ids,
            worker_capabilities=root.worker_capabilities,
            worker_pools=root.worker_pools,
            origin="plugin",
            parent_id=root.id,
            acceptance_policy=root.acceptance_policy,
        )
        intermediate = Contract(
            id=hash_contract_current(**child_fields), **child_fields
        )
        assert owner.publish(
            (contract_declaration_effect(intermediate),),
            idempotency_key="intermediate",
        ).accepted
    producer_signer = Ed25519Signer()
    producer_key = ActorKey(
        ACTORS["producer"],
        "producer-key-2",
        producer_signer.kind,
        producer_signer.signer_id,
        ("worker.submit",),
    )
    producer = authorize_worker_host(
        _Producer(), genesis, producer_key, producer_signer, owner
    )
    claimed = claim_next_package(
        store, worker_id=ACTORS["producer"], contract_id=root.id
    )
    assert claimed is not None
    assert execute_claimed_package(claimed, producer, store)["status"] in {
        "accepted",
        "task_delegated",
    }
    report = store.get_assets_by_name("review_report")[0]
    inputs["draft_checks"] = checks
    inputs["review_report"] = report
    gate_value = expected_gate_receipt(inputs)
    if broken_gate:
        gate_value["semantic_checked"] = True
    if not missing_gate:
        gate = _publish(
            publishers["gate"],
            build_control_plane_asset(
                name="structural_receipt", content=json.dumps(gate_value)
            ),
            "gate",
        )
    semantic = (
        _publish(
            publishers["semantic"],
            build_control_plane_asset(
                name="semantic_review",
                content=json.dumps(
                    {
                        "schema": "claim-semantic-review-v1",
                        "source_asset_id": source.id,
                        "report_asset_id": report.id,
                        "policy_asset_id": policy.id,
                        "gate_receipt_asset_id": getattr(gate, "id", "missing"),
                        "verdict": "accepted",
                        "reason": "checked",
                    }
                ),
            ),
            "semantic",
        )
        if not missing_gate
        else None
    )
    acceptance_fields = dict(
        name="claim-review-accept",
        description="accept review\n" + json.dumps({"acceptance_target": root.id}),
        inputs=tuple(
            (
                "research_result",
                "draft_report",
                "review_policy",
                "draft_checks",
                "review_report",
                "structural_receipt",
                "semantic_review",
            )
        ),
        outputs=("acceptance_receipt",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=("research_result", "draft_report", "review_policy"),
        context_asset_ids=(source.id, draft.id, policy.id),
        worker_capabilities=("claim.review.accept",),
        worker_pools=("verification",),
        origin="human",
        parent_id=root.id,
    )
    acceptance = Contract(
        id=hash_contract_current(**acceptance_fields), **acceptance_fields
    )
    assert owner.publish(
        (contract_declaration_effect(acceptance),), idempotency_key="acceptance"
    ).accepted
    return (
        root,
        acceptance,
        intermediate,
        {
            "research_result": source,
            "draft_report": draft,
            "review_policy": policy,
            "draft_checks": checks,
            "review_report": report,
            "structural_receipt": gate,
            "semantic_review": semantic,
        },
    )


def _run_acceptance(
    domain,
    *,
    broken_gate=False,
    missing_gate=False,
    acceptor_id=None,
    authority_caps=("worker.submit", "asset.attest", "claim.review.accept"),
):
    store, genesis, owner, _ = domain
    root, acceptance, intermediate, assets = _bundle(
        domain,
        broken_gate=broken_gate,
        missing_gate=missing_gate,
        acceptor_id=acceptor_id,
    )
    signer = Ed25519Signer()
    worker_id = acceptor_id or ACTORS["acceptor"]
    key = ActorKey(
        worker_id, "acceptor-key", signer.kind, signer.signer_id, authority_caps
    )
    worker = authorize_worker_host(
        create_acceptance_worker(worker_id=worker_id), genesis, key, signer, owner
    )
    claimed = claim_next_package(store, worker_id=worker_id, contract_id=acceptance.id)
    if claimed is None:
        return store, root, {"status": "unclaimable"}
    result = execute_claimed_package(claimed, worker, store)
    return store, root, result


def test_valid_review_acceptance_qualifies_root(review_domain):
    store, root, result = _run_acceptance(review_domain)
    assert result["status"] == "accepted", result
    assert all_outputs_satisfied(root, store)


@pytest.mark.parametrize("case", [(True, False), (False, True)])
def test_missing_or_wrong_gate_cannot_qualify(review_domain, case):
    store, root, result = _run_acceptance(
        review_domain, broken_gate=case[0], missing_gate=case[1]
    )
    assert result["status"] in {"accepted", "task_delegated", "unclaimable", "failed"}
    assert not any(
        r.payload.get("contract_id") == root.id
        for _, r in store.scan_runtime_records(record_type="asset.attested")
    )
    assert not all_outputs_satisfied(root, store)


def test_actor_without_claim_accept_authority_is_persistently_rejected(review_domain):
    store, root, result = _run_acceptance(
        review_domain,
        acceptor_id="worker:weak",
        authority_caps=("worker.submit", "asset.attest"),
    )
    assert result["status"] in {"accepted", "task_delegated", "rejected"}
    assert not any(
        record.payload.get("contract_id") == root.id
        for _, record in store.scan_runtime_records(record_type="asset.attested")
    )
    assert any(
        record.record_type == "candidate.rejected"
        and "claim.review.accept" in str(record.payload.get("reason", ""))
        for _, record in store.scan_runtime_records()
    )


def test_root_completion_cancels_unfinished_intermediate_obligation(review_domain):
    store, genesis, owner, _ = review_domain
    root, acceptance, intermediate, _ = _bundle(
        review_domain, intermediate_obligation=True
    )
    signer = Ed25519Signer()
    key = ActorKey(
        ACTORS["acceptor"],
        "acceptor-intermediate-key",
        signer.kind,
        signer.signer_id,
        ("worker.submit", "asset.attest", "claim.review.accept"),
    )
    worker = authorize_worker_host(
        create_acceptance_worker(worker_id=ACTORS["acceptor"]),
        genesis,
        key,
        signer,
        owner,
    )
    claimed = claim_next_package(
        store, worker_id=ACTORS["acceptor"], contract_id=acceptance.id
    )
    assert claimed is not None
    assert execute_claimed_package(claimed, worker, store)["status"] == "accepted"
    assert all_outputs_satisfied(root, store)
    terminal = [
        record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == intermediate.id
    ]
    assert terminal and terminal[-1].payload["terminal"] == "cancelled"
    assert (
        claim_next_package(
            store, worker_id=ACTORS["producer"], contract_id=intermediate.id
        )
        is None
    )
    digest = store.runtime_materialization_digest()
    assert store.rebuild_runtime_materializations() == digest
