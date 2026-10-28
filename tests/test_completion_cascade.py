"""Transactional regressions for completion through task descendants."""

from __future__ import annotations

import pytest

from aigineering.core.acceptance import acceptance_policy_id
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.commitment import CandidateCommitRejected, CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_attestation_effect,
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset


def _world(tmp_path):
    store = SQLiteStore(tmp_path / "completion-cascade.sqlite")
    signer = Ed25519Signer()
    operator = ActorKey(
        "human:operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        (
            "asset.attest",
            "asset.publish",
            "contract.publish",
            "policy.review",
        ),
    )
    worker = ActorKey(
        "worker:producer",
        "producer-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish", "worker.submit"),
    )
    verifier = ActorKey(
        "worker:verifier",
        "verifier-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish", "worker.submit", "policy.review"),
    )
    genesis = create_genesis_manifest(
        "completion-cascade-test", (operator, worker, verifier), "policy:test"
    )
    initialize_genesis(store, genesis)
    policy = {
        "mode": "independent",
        "policy_version": "cascade-v1",
        "required_attestations": 1,
        "verifier_capabilities": ["policy.review"],
    }
    root = build_control_plane_contract(
        name="root deliverable",
        description="Produce one independently reviewed result.",
        outputs=("final",),
        budget=10,
        acceptance_policy=policy,
    )
    result = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        contract_declaration_effect(root),
        idempotency_key="declare-root",
    )
    assert result.accepted, result
    store.register_worker(
        WorkerRegistration(
            worker.actor_id, actor_id=worker.actor_id, key_id=worker.key_id
        )
    )
    store.register_worker(
        WorkerRegistration(
            verifier.actor_id, actor_id=verifier.actor_id, key_id=verifier.key_id
        )
    )
    return store, signer, genesis, operator, worker, verifier, root, policy


def _declare_child(
    store,
    signer,
    genesis,
    operator,
    *,
    parent,
    name,
    output,
    budget=3,
    inputs=(),
    worker_capabilities=(),
):
    contract = contract_from_fields(
        name=name,
        description=f"Work for {name}.",
        parent_id=parent.id,
        inputs=inputs,
        outputs=(output,),
        activation="",
        budget=budget,
        tool_scope=(),
        labels=(),
        worker_capabilities=worker_capabilities,
        origin="human",
    )
    result = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        contract_declaration_effect(contract),
        idempotency_key=f"declare:{name}",
    )
    assert result.accepted, result
    return contract


def _submit_output(store, signer, genesis, worker, contract, output):
    claim = store.claim_contract(
        contract.id, worker.actor_id, package_id=f"pkg:{output}"
    )
    assert claim is not None
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=worker.actor_id,
        key_id=worker.key_id,
        effects=(
            asset_proposal_effect(
                Asset(id=f"asset:{output}", name=output, content=f"value for {output}")
            ),
        ),
        signer=signer,
        idempotency_key=f"submit:{contract.id}:{output}",
        claim_binding=CandidateClaimBinding(
            contract_id=contract.id,
            claim_id=claim["claim_id"],
            claim_epoch=claim["epoch"],
            package_id=claim["package_id"],
        ),
    )
    decision = CandidateCommitter(store, store).commit(candidate, genesis)
    assert decision.accepted, decision
    return claim, candidate


def _qualify_root(store, signer, genesis, operator, root, policy):
    asset = store.get_assets_by_name("final")[0]
    decision = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_attestation_effect(
            root.id,
            "final",
            asset.id,
            policy_id=acceptance_policy_id(policy),
            policy_version="cascade-v1",
            verdict="accepted",
        ),
        idempotency_key="accept-root",
    )
    assert decision.accepted, decision
    return decision


def test_root_acceptance_cancels_claimed_grandchild_and_fences_late_submit(tmp_path):
    (
        store,
        signer,
        genesis,
        operator,
        worker,
        _verifier,
        root,
        policy,
    ) = _world(tmp_path)
    child = _declare_child(
        store,
        signer,
        genesis,
        operator,
        parent=root,
        name="unfinished child",
        output="child_output",
        budget=5,
    )
    grandchild = _declare_child(
        store,
        signer,
        genesis,
        operator,
        parent=child,
        name="unfinished grandchild",
        output="grandchild_output",
        budget=2,
    )
    grandchild_claim = store.claim_contract(
        grandchild.id, worker.actor_id, package_id="pkg:grandchild"
    )
    assert grandchild_claim is not None
    late_submit = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=worker.actor_id,
        key_id=worker.key_id,
        effects=(
            asset_proposal_effect(
                Asset(
                    id="asset:late-grandchild",
                    name="grandchild_output",
                    content="late result",
                )
            ),
        ),
        signer=signer,
        idempotency_key="late-grandchild-submit",
        claim_binding=CandidateClaimBinding(
            contract_id=grandchild.id,
            claim_id=grandchild_claim["claim_id"],
            claim_epoch=grandchild_claim["epoch"],
            package_id=grandchild_claim["package_id"],
        ),
    )
    _submit_output(store, signer, genesis, _verifier, root, "final")

    _qualify_root(store, signer, genesis, operator, root, policy)

    terminals = {
        record.payload["contract_id"]: record.payload["terminal"]
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    }
    assert terminals[root.id] == "complete"
    assert terminals[child.id] == "cancelled"
    assert terminals[grandchild.id] == "cancelled"
    assert store.get_claim(grandchild.id)["status"] == "released"
    with pytest.raises(CandidateCommitRejected):
        CandidateCommitter(store, store).commit(late_submit, genesis)
    assert store.get_assets_by_name("grandchild_output") == []
    store.close()


def test_completed_intermediate_keeps_verifier_until_root_acceptance(tmp_path):
    (
        store,
        signer,
        genesis,
        operator,
        worker,
        verifier,
        root,
        policy,
    ) = _world(tmp_path)
    child = _declare_child(
        store,
        signer,
        genesis,
        operator,
        parent=root,
        name="completed intermediate",
        output="child_output",
        budget=5,
    )
    review = _declare_child(
        store,
        signer,
        genesis,
        operator,
        parent=child,
        name="root verifier",
        output="review_receipt",
        budget=2,
        inputs=("final",),
        worker_capabilities=("policy.review",),
    )
    _submit_output(store, signer, genesis, worker, child, "child_output")
    child_terminal_before = [
        record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == child.id
    ]
    assert len(child_terminal_before) == 1
    assert child_terminal_before[0].payload["terminal"] == "complete"
    assert store.get_claim(review.id) is None
    review_claim = store.claim_contract(
        review.id, verifier.actor_id, package_id="pkg:review-before-root-acceptance"
    )
    assert review_claim is not None
    assert not any(
        record.payload.get("contract_id") == review.id
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    )

    _submit_output(store, signer, genesis, worker, root, "final")
    _qualify_root(store, signer, genesis, operator, root, policy)

    child_terminals = [
        record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == child.id
    ]
    review_terminals = [
        record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == review.id
    ]
    assert [record.id for record in child_terminals] == [child_terminal_before[0].id]
    assert len(review_terminals) == 1
    assert review_terminals[0].payload["terminal"] == "cancelled"
    assert store.get_claim(review.id)["status"] == "released"
    store.close()


def test_terminal_intermediate_does_not_shield_live_grandchild(tmp_path):
    (
        store,
        signer,
        genesis,
        operator,
        worker,
        _verifier,
        root,
        policy,
    ) = _world(tmp_path)
    child = _declare_child(
        store,
        signer,
        genesis,
        operator,
        parent=root,
        name="historically terminal intermediate",
        output="child_output",
        budget=5,
    )
    grandchild = _declare_child(
        store,
        signer,
        genesis,
        operator,
        parent=child,
        name="live descendant",
        output="grandchild_output",
        budget=2,
    )
    sibling = _declare_child(
        store,
        signer,
        genesis,
        operator,
        parent=root,
        name="already terminal sibling",
        output="sibling_output",
        budget=1,
    )
    _submit_output(store, signer, genesis, _verifier, root, "final")
    prior_terminal = create_runtime_record(
        "lifecycle.terminal",
        {"contract_id": child.id, "terminal": "complete"},
    )
    store.append_runtime_record(prior_terminal)
    prior_sibling_terminal = create_runtime_record(
        "lifecycle.terminal",
        {"contract_id": sibling.id, "terminal": "failed"},
    )
    store.append_runtime_record(prior_sibling_terminal)
    claim = store.claim_contract(
        grandchild.id, worker.actor_id, package_id="pkg:live-descendant"
    )
    assert claim is not None

    _qualify_root(store, signer, genesis, operator, root, policy)

    child_terminals = [
        record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == child.id
    ]
    descendant_terminals = [
        record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == grandchild.id
    ]
    sibling_terminals = [
        record
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == sibling.id
    ]
    assert [record.id for record in child_terminals] == [prior_terminal.id]
    assert [record.id for record in sibling_terminals] == [prior_sibling_terminal.id]
    assert sibling_terminals[0].payload["terminal"] == "failed"
    assert len(descendant_terminals) == 1
    assert descendant_terminals[0].payload["terminal"] == "cancelled"
    assert store.get_claim(grandchild.id)["status"] == "released"
    store.close()
