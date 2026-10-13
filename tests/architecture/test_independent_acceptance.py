"""Independent acceptance must bind a different verifier to one exact Asset."""

from __future__ import annotations

import ast
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from aigineering.core.commitment import CandidateCommitter
from aigineering.core.acceptance import (
    OutputQualificationConflict,
    validate_output_qualification_commit,
    validate_output_shape,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.ids import acceptance_policy_id, hash_contract_v3
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.agent.worker import WorkerHost
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
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
    worker_claim_effect,
    worker_registration_effect,
)
from aigineering.protocol.types import Asset, Candidate, Contract
from aigineering.protocol.runtime_record import create_runtime_record


@pytest.fixture
def acceptance_domain(tmp_path):
    db_path = str(tmp_path / "acceptance.db")
    store = SQLiteStore(db_path)
    owner = Ed25519Signer()
    producer = Ed25519Signer()
    verifier = Ed25519Signer()
    weak_verifier = Ed25519Signer()
    verifier_two = Ed25519Signer()
    genesis = create_genesis_manifest(
        "independent-sqlite",
        (
            ActorKey(
                "human:owner",
                "owner-key",
                owner.kind,
                owner.signer_id,
                ("asset.publish", "contract.publish", "worker.register"),
            ),
            ActorKey(
                "worker:producer",
                "producer-key",
                producer.kind,
                producer.signer_id,
                ("worker.submit", "worker.claim", "asset.attest", "verify.compliance"),
            ),
            ActorKey(
                "worker:verifier",
                "verifier-key",
                verifier.kind,
                verifier.signer_id,
                ("asset.attest", "verify.compliance"),
            ),
            ActorKey(
                "worker:weak-verifier",
                "weak-key",
                weak_verifier.kind,
                weak_verifier.signer_id,
                ("asset.attest",),
            ),
            ActorKey(
                "worker:verifier-two",
                "verifier-two-key",
                verifier_two.kind,
                verifier_two.signer_id,
                ("asset.attest", "verify.compliance"),
            ),
        ),
        "policy:independent-acceptance",
    )
    initialize_genesis(store, genesis)
    committer = CandidateCommitter(store, store)
    values = (
        store,
        genesis,
        committer,
        owner,
        producer,
        verifier,
        weak_verifier,
        verifier_two,
        db_path,
    )
    try:
        yield values
    finally:
        store.close()


def _candidate(genesis, actor_id, key_id, signer, effect, nonce):
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor_id,
        key_id=key_id,
        effects=(effect,),
        signer=signer,
        idempotency_key=nonce,
    )


def _publish_contract_and_output(
    domain,
    *,
    produce_output: bool = True,
    output_content: str = "candidate report",
    output_shapes=None,
    expected_output_status: str = "accepted",
):
    store, genesis, committer, owner, producer, _, _, _, _ = domain
    rubric = Asset(id="rubric:compliance", name="review_rubric", content="be exact")
    evidence = Asset(
        id="evidence:source", name="review_evidence", content="source facts"
    )
    for item in (rubric, evidence):
        proposed = _candidate(
            genesis,
            "human:owner",
            "owner-key",
            owner,
            asset_proposal_effect(item),
            f"context:{item.id}",
        )
        assert committer.commit(proposed, genesis).accepted
    rubric_id = store.get_assets_by_name(rubric.name)[0].id
    evidence_id = store.get_assets_by_name(evidence.name)[0].id
    fields = {
        "name": "independently_reviewed_report",
        "description": "Publish a report that another actor must verify.",
        "inputs": (),
        "outputs": ("report",),
        "activation": "",
        "budget": 3,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
        "acceptance_policy": {
            "mode": "independent",
            "policy_version": "review-v1",
            "rubric_asset_ids": [rubric_id],
            "evidence_asset_ids": [evidence_id],
            "required_attestations": 1,
            "verifier_capabilities": ["verify.compliance"],
            **({"output_shapes": output_shapes} if output_shapes is not None else {}),
        },
    }
    contract = Contract(id=hash_contract_v3(**fields), **fields)
    declared = _candidate(
        genesis,
        "human:owner",
        "owner-key",
        owner,
        contract_declaration_effect(contract),
        "declare",
    )
    assert committer.commit(declared, genesis).accepted
    registration = WorkerRegistration(
        "worker:producer",
        actor_id="worker:producer",
        key_id="producer-key",
    )
    registered = _candidate(
        genesis,
        "human:owner",
        "owner-key",
        owner,
        worker_registration_effect(registration),
        "register-producer",
    )
    assert committer.commit(registered, genesis).accepted
    if not produce_output:
        return contract, None
    claimed = claim_next_package(store, worker_id="worker:producer")
    assert claimed is not None

    class _Producer:
        worker_id = "worker:producer"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            return Candidate(
                worker_id=self.worker_id,
                raw_output="/exec "
                + json.dumps({"outputs": {"report": output_content}}),
            )

    host = WorkerHost(
        _Producer(),
        genesis,
        next(key for key in genesis.root_keys if key.actor_id == "worker:producer"),
        producer,
    )
    result = execute_claimed_package(claimed, host, store)
    assert result["status"] == expected_output_status
    if expected_output_status != "accepted":
        return contract, None
    asset = store.get_assets_by_name("report")[0]
    assert asset.created_by == contract.id
    assert not all_outputs_satisfied(contract, store)
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    return contract, asset


def _attestation_effect(contract, asset, *, verdict="accepted"):
    policy = contract.acceptance_policy
    assert policy is not None
    return asset_attestation_effect(
        contract.id,
        "report",
        asset.id,
        policy_id=acceptance_policy_id(policy),
        policy_version=str(policy["policy_version"]),
        verdict=verdict,
        rubric_asset_ids=tuple(policy.get("rubric_asset_ids", ())),
        evidence_asset_ids=tuple(policy.get("evidence_asset_ids", ())),
    )


def test_declared_output_number_shapes_reject_non_finite_values_recursively():
    shape = {"samples": [{"value": "number"}]}
    for content in (
        '{"samples":[{"value":NaN}]}',
        '{"samples":[{"value":Infinity}]}',
        '{"samples":[{"value":-Infinity}]}',
        '{"samples":[{"value":1e9999}]}',
    ):
        with pytest.raises(ValueError):
            validate_output_shape(
                {"output_shapes": {"report": shape}}, "report", content
            )

    validate_output_shape(
        {"output_shapes": {"report": shape}},
        "report",
        '{"samples":[{"value":12.5},{"value":0},{"value":-123456789012345678901234567890}]}',
    )
    for content in (
        '{"samples":[{"value":true}]}',
        '{"samples":[{"value":"Infinity"}]}',
    ):
        with pytest.raises(ValueError):
            validate_output_shape(
                {"output_shapes": {"report": shape}}, "report", content
            )
    validate_output_shape(
        {"output_shapes": {"report": {"samples": [{"value": "string"}]}}},
        "report",
        '{"samples":[{"value":"Infinity"}]}',
    )


def test_unshaped_output_content_is_not_parsed_as_json():
    validate_output_shape(
        {"output_shapes": {"another_output": "number"}},
        "report",
        "ordinary text that is not JSON: NaN Infinity 1e9999",
    )


def test_output_shape_validator_does_not_import_tool_schema():
    source = Path(__file__).resolve().parents[2] / "src/aigineering/core/acceptance.py"
    tree = ast.parse(source.read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module == "aigineering.core.tool_schema"
        or isinstance(node, ast.Import)
        and any(alias.name == "aigineering.core.tool_schema" for alias in node.names)
        for node in ast.walk(tree)
    )


def _signed_claim_output_candidate(domain, *, output_shapes, output_content, key):
    store, genesis, committer, _, producer, _, _, _, _ = domain
    contract, _ = _publish_contract_and_output(
        domain, produce_output=False, output_shapes=output_shapes
    )
    claim_request = _candidate(
        genesis,
        "worker:producer",
        "producer-key",
        producer,
        worker_claim_effect("worker:producer", contract_id=contract.id),
        f"signed-output-claim:{key}",
    )
    claimed = claim_next_package(
        store,
        worker_id="worker:producer",
        contract_id=contract.id,
        claim_candidate=claim_request,
    )
    assert claimed is not None, "signed worker.claim did not produce a claim"
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id="worker:producer",
        key_id="producer-key",
        effects=(
            asset_proposal_effect(
                Asset(id="untrusted-output", name="report", content=output_content)
            ),
        ),
        signer=producer,
        idempotency_key=f"signed-output:{key}",
        claim_binding=CandidateClaimBinding(
            contract.id,
            claimed.package.claim_id,
            claimed.package.claim_epoch,
            claimed.package.package_id,
        ),
    )
    return contract, candidate, committer


def test_signed_claim_submission_rejects_non_finite_output_without_business_facts(
    acceptance_domain,
):
    store, genesis, committer, _, _, _, _, _, _ = acceptance_domain
    contract, candidate, committer = _signed_claim_output_candidate(
        acceptance_domain,
        output_shapes={"report": {"score": "number"}},
        output_content='{"score":1e9999}',
        key="non-finite",
    )
    decision = committer.commit(candidate, genesis)
    assert not decision.accepted
    assert any(
        record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
        for _, record in store.scan_runtime_records()
    )
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == candidate.id
        and "finite number" in str(record.payload)
        for _, record in store.scan_runtime_records()
    )
    assert not any(
        record.record_type == "asset.committed"
        and record.payload.get("contract_id") == contract.id
        and record.payload.get("asset", {}).get("name") == "report"
        for _, record in store.scan_runtime_records()
    )
    assert not store.scan_runtime_records(record_type="output.qualified")


def test_attestation_rejects_preexisting_non_finite_claim_output_without_rewriting_history(
    acceptance_domain, monkeypatch
):
    import aigineering.core.effect_projection as effect_projection

    store, genesis, committer, _, _, verifier, _, _, db_path = acceptance_domain
    original_guard = effect_projection.validate_output_shape
    monkeypatch.setattr(effect_projection, "validate_output_shape", lambda *_: None)
    contract, candidate, committer = _signed_claim_output_candidate(
        acceptance_domain,
        output_shapes={"report": {"score": "number"}},
        output_content='{"score":1e9999}',
        key="pre-fix-history",
    )
    prepared = committer.commit(candidate, genesis)
    monkeypatch.setattr(effect_projection, "validate_output_shape", original_guard)
    assert prepared.accepted, prepared
    asset = store.get_assets_by_name("report")[0]
    from aigineering.core.projection_context import load_effect_projection_context
    from aigineering.core.acceptance import validate_claim_bound_output

    context = load_effect_projection_context(store)
    validate_claim_bound_output(context, asset, contract)
    committed_before = tuple(
        record
        for _, record in store.scan_runtime_records(record_type="asset.committed")
        if record.payload.get("asset", {}).get("id") == asset.id
    )
    assert len(committed_before) == 1
    claim_provenance_before = tuple(
        (record.record_type, record.id)
        for _, record in store.scan_runtime_records()
        if record.record_type in {"claim.granted", "claim.submitted"}
        and record.payload.get("contract_id") == contract.id
    )
    attestation = _candidate(
        genesis,
        "worker:verifier",
        "verifier-key",
        verifier,
        _attestation_effect(contract, asset),
        "reject-preexisting-nonfinite-output",
    )
    rejected = committer.commit(attestation, genesis)
    assert not rejected.accepted
    attestation_rejections = tuple(
        record
        for _, record in store.scan_runtime_records(record_type="candidate.rejected")
        if record.payload.get("candidate_id") == attestation.id
    )
    assert len(attestation_rejections) == 1
    rejection_reason = str(attestation_rejections[0].payload.get("reason", ""))
    assert "output shape mismatch" in rejection_reason
    assert "finite number" in rejection_reason
    assert not store.scan_runtime_records(record_type="asset.attested")
    assert not store.scan_runtime_records(record_type="output.qualified")
    committed_after = tuple(
        record
        for _, record in store.scan_runtime_records(record_type="asset.committed")
        if record.payload.get("asset", {}).get("id") == asset.id
    )
    assert committed_after == committed_before
    claim_provenance_after = tuple(
        (record.record_type, record.id)
        for _, record in store.scan_runtime_records()
        if record.record_type in {"claim.granted", "claim.submitted"}
        and record.payload.get("contract_id") == contract.id
    )
    assert claim_provenance_after == claim_provenance_before
    assert store.get_assets_by_name("report")[0] == asset
    post_rejection_digest = store.runtime_materialization_digest()
    assert store.rebuild_runtime_materializations(strict=True) == post_rejection_digest
    reopened = SQLiteStore(db_path)
    try:
        assert reopened.runtime_materialization_digest() == post_rejection_digest
        assert (
            reopened.rebuild_runtime_materializations(strict=True)
            == post_rejection_digest
        )
        assert reopened.get_assets_by_name("report")[0] == asset
    finally:
        reopened.close()


def test_finite_claim_bound_output_qualifies_replays_and_rebuilds(acceptance_domain):
    store, genesis, committer, _, _, verifier, _, _, db_path = acceptance_domain
    contract, candidate, committer = _signed_claim_output_candidate(
        acceptance_domain,
        output_shapes={"report": {"score": "number"}},
        output_content='{"score":12.5}',
        key="finite-positive",
    )
    produced = committer.commit(candidate, genesis)
    assert produced.accepted, produced
    asset = store.get_assets_by_name("report")[0]
    assert asset.content == '{"score":12.5}'
    replayed_output = committer.commit(candidate, genesis)
    assert replayed_output.accepted
    assert len(store.get_assets_by_name("report")) == 1

    attestation = _candidate(
        genesis,
        "worker:verifier",
        "verifier-key",
        verifier,
        _attestation_effect(contract, asset),
        "attest-finite-output",
    )
    qualified = committer.commit(attestation, genesis)
    assert qualified.accepted, qualified
    assert len(store.scan_runtime_records(record_type="output.qualified")) == 1
    digest = store.runtime_materialization_digest()
    reopened = SQLiteStore(db_path)
    try:
        assert reopened.runtime_materialization_digest() == digest
        assert reopened.rebuild_runtime_materializations(strict=True) == digest
        assert len(reopened.scan_runtime_records(record_type="output.qualified")) == 1
        assert reopened.get_assets_by_name("report")[0] == asset
    finally:
        reopened.close()


def test_producer_cannot_attest_its_own_output(acceptance_domain):
    store, genesis, committer, _, producer, _, _, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    self_attestation = _candidate(
        genesis,
        "worker:producer",
        "producer-key",
        producer,
        _attestation_effect(contract, asset),
        "self-attest",
    )
    decision = committer.commit(self_attestation, genesis)
    assert not decision.accepted
    assert "cannot attest its own output" in str(decision.runtime_records[1].payload)
    assert not all_outputs_satisfied(contract, store)


def test_verifier_capability_and_exact_asset_qualification(acceptance_domain):
    store, genesis, committer, _, _, verifier, weak_verifier, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    weak = _candidate(
        genesis,
        "worker:weak-verifier",
        "weak-key",
        weak_verifier,
        _attestation_effect(contract, asset),
        "weak-attest",
    )
    assert not committer.commit(weak, genesis).accepted
    exact = _candidate(
        genesis,
        "worker:verifier",
        "verifier-key",
        verifier,
        _attestation_effect(contract, asset),
        "verified-attest",
    )
    decision = committer.commit(exact, genesis)
    assert decision.accepted
    assert any(r.record_type == "asset.attested" for r in decision.runtime_records)
    qualification = next(
        r for r in decision.runtime_records if r.record_type == "output.qualified"
    )
    assert qualification.payload["asset_id"] == asset.id
    assert all_outputs_satisfied(contract, store)
    terminals = store.scan_runtime_records(record_type="lifecycle.terminal")
    assert len(terminals) == 1
    assert terminals[0][1].payload == {
        "contract_id": contract.id,
        "terminal": "complete",
    }


def test_claim_bound_output_rejects_mismatched_deterministic_output_shape(
    acceptance_domain,
):
    store, _, _, _, _, _, _, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(
        acceptance_domain,
        output_content=(
            '{"answer":"grounded","citations":["W1"],"limitations":"none"}'
        ),
        output_shapes={
            "report": {
                "answer": "nonempty_string",
                "citations": ["nonempty_string"],
                "limitations": ["nonempty_string"],
            }
        },
        expected_output_status="rejected",
    )

    assert asset is None
    rejection = store.scan_runtime_records(record_type="candidate.rejected")[-1][1]
    assert "output shape mismatch" in str(rejection.payload)


def test_signed_attestation_request_derives_policy_from_exact_contract_identity(
    acceptance_domain,
):
    _, genesis, committer, _, _, verifier, _, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    effect = asset_attestation_effect(
        contract.id,
        "report",
        asset.id,
        policy_id="",
        policy_version="",
        rubric_asset_ids=tuple(contract.acceptance_policy["rubric_asset_ids"]),
        evidence_asset_ids=tuple(contract.acceptance_policy["evidence_asset_ids"]),
    )

    decision = committer.commit(
        _candidate(
            genesis,
            "worker:verifier",
            "verifier-key",
            verifier,
            effect,
            "derived-policy-binding",
        ),
        genesis,
    )

    assert decision.accepted
    attested = next(
        record
        for record in decision.runtime_records
        if record.record_type == "asset.attested"
    )
    assert attested.payload["policy_id"] == acceptance_policy_id(
        contract.acceptance_policy
    )


def test_parent_acceptance_can_bind_exact_descendant_output(acceptance_domain):
    store, genesis, committer, owner, producer, verifier, _, _, _ = acceptance_domain
    contract, _ = _publish_contract_and_output(acceptance_domain, produce_output=False)
    fields = {
        "name": "report.continue",
        "description": "Publish the report after delegated evidence retrieval.",
        "inputs": (),
        "outputs": ("report",),
        "activation": "",
        "budget": 1,
        "tool_scope": (),
        "labels": (),
        "origin": "continuation",
        "parent_id": contract.id,
    }
    child = Contract(id=hash_contract_v3(**fields), **fields)
    assert committer.commit(
        _candidate(
            genesis,
            "human:owner",
            "owner-key",
            owner,
            contract_declaration_effect(child),
            "declare-descendant",
        ),
        genesis,
    ).accepted
    claimed = claim_next_package(
        store, worker_id="worker:producer", contract_id=child.id
    )
    assert claimed is not None

    class _Producer:
        worker_id = "worker:producer"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            return Candidate(
                worker_id=self.worker_id,
                raw_output='/exec {"outputs":{"report":"descendant report"}}',
            )

    host = WorkerHost(
        _Producer(),
        genesis,
        next(key for key in genesis.root_keys if key.actor_id == "worker:producer"),
        producer,
    )
    assert execute_claimed_package(claimed, host, store)["status"] == "accepted"
    descendant_asset = next(
        asset
        for asset in store.get_assets_by_name("report")
        if asset.created_by == child.id
    )
    decision = committer.commit(
        _candidate(
            genesis,
            "worker:verifier",
            "verifier-key",
            verifier,
            _attestation_effect(contract, descendant_asset),
            "attest-descendant",
        ),
        genesis,
    )
    assert decision.accepted
    assert all_outputs_satisfied(contract, store)


def test_parent_acceptance_rejects_unrelated_same_name_asset(acceptance_domain):
    store, genesis, committer, owner, _, verifier, _, _, _ = acceptance_domain
    contract, _ = _publish_contract_and_output(acceptance_domain)
    unrelated = Asset(id="unrelated", name="report", content="unrelated")
    published = committer.commit(
        _candidate(
            genesis,
            "human:owner",
            "owner-key",
            owner,
            asset_proposal_effect(unrelated),
            "publish-unrelated",
        ),
        genesis,
    )
    assert published.accepted
    asset = next(
        item
        for item in store.get_assets_by_name("report")
        if item.created_by == "human:owner"
    )
    decision = committer.commit(
        _candidate(
            genesis,
            "worker:verifier",
            "verifier-key",
            verifier,
            _attestation_effect(contract, asset),
            "attest-unrelated",
        ),
        genesis,
    )
    assert not decision.accepted
    assert "or its descendants" in str(decision.runtime_records[1].payload)


def test_attestation_must_bind_exact_policy_rubric_and_evidence(acceptance_domain):
    _, genesis, committer, _, _, verifier, _, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    policy = contract.acceptance_policy
    assert policy is not None
    cases = (
        asset_attestation_effect(
            contract.id,
            "report",
            asset.id,
            policy_id="acceptance:v1:wrong",
            policy_version=str(policy["policy_version"]),
            rubric_asset_ids=tuple(policy["rubric_asset_ids"]),
            evidence_asset_ids=tuple(policy["evidence_asset_ids"]),
        ),
        asset_attestation_effect(
            contract.id,
            "report",
            asset.id,
            policy_id=acceptance_policy_id(policy),
            policy_version=str(policy["policy_version"]),
            rubric_asset_ids=(),
            evidence_asset_ids=tuple(policy["evidence_asset_ids"]),
        ),
        asset_attestation_effect(
            contract.id,
            "report",
            asset.id,
            policy_id=acceptance_policy_id(policy),
            policy_version=str(policy["policy_version"]),
            rubric_asset_ids=tuple(policy["rubric_asset_ids"]),
            evidence_asset_ids=(),
        ),
    )
    for index, effect in enumerate(cases):
        decision = committer.commit(
            _candidate(
                genesis,
                "worker:verifier",
                "verifier-key",
                verifier,
                effect,
                f"bad-policy-binding:{index}",
            ),
            genesis,
        )
        assert not decision.accepted


def test_qualification_slot_replay_allows_same_asset_and_rejects_replacement() -> None:
    first = create_runtime_record(
        "output.qualified",
        {"contract_id": "task:one", "output_name": "report", "asset_id": "a1"},
    )
    replay = create_runtime_record(
        "output.qualified",
        {
            "contract_id": "task:one",
            "output_name": "report",
            "asset_id": "a1",
            "verifier_actor_ids": ["worker:two"],
        },
    )
    replacement = create_runtime_record(
        "output.qualified",
        {"contract_id": "task:one", "output_name": "report", "asset_id": "a2"},
    )

    validate_output_qualification_commit((first,), (replay,))
    with pytest.raises(OutputQualificationConflict, match="different immutable Asset"):
        validate_output_qualification_commit((first,), (replacement,))


def test_qualification_race_becomes_durable_candidate_rejection(
    acceptance_domain, monkeypatch
) -> None:
    store, genesis, committer, _, _, verifier, _, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    candidate = _candidate(
        genesis,
        "worker:verifier",
        "verifier-key",
        verifier,
        _attestation_effect(contract, asset),
        "qualification-race",
    )
    original = validate_output_qualification_commit

    def race(existing_records, pending_records):
        if any(record.record_type == "output.qualified" for record in pending_records):
            raise OutputQualificationConflict("simulated competing output selection")
        return original(existing_records, pending_records)

    monkeypatch.setattr(
        "aigineering.core.acceptance.validate_output_qualification_commit", race
    )

    decision = committer.commit(candidate, genesis)

    assert not decision.accepted
    assert any(
        record.record_type == "candidate.rejected"
        for record in decision.runtime_records
    )
    assert store.scan_runtime_records(record_type="output.qualified") == []
    assert store.scan_runtime_records(record_type="lifecycle.terminal") == []


def test_rejected_attestation_is_evidence_not_qualification(acceptance_domain):
    store, genesis, committer, _, _, verifier, _, _, _ = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    rejected = _candidate(
        genesis,
        "worker:verifier",
        "verifier-key",
        verifier,
        _attestation_effect(contract, asset, verdict="rejected"),
        "reject-attest",
    )
    decision = committer.commit(rejected, genesis)
    assert decision.accepted
    assert any(r.record_type == "asset.attested" for r in decision.runtime_records)
    assert not any(
        r.record_type == "output.qualified" for r in decision.runtime_records
    )
    assert not all_outputs_satisfied(contract, store)


def test_concurrent_independent_attestations_converge_without_terminal_conflict(
    acceptance_domain,
):
    (
        store,
        genesis,
        _,
        _,
        _,
        verifier,
        _,
        verifier_two,
        db_path,
    ) = acceptance_domain
    contract, asset = _publish_contract_and_output(acceptance_domain)
    candidates = (
        _candidate(
            genesis,
            "worker:verifier",
            "verifier-key",
            verifier,
            _attestation_effect(contract, asset),
            "concurrent-one",
        ),
        _candidate(
            genesis,
            "worker:verifier-two",
            "verifier-two-key",
            verifier_two,
            _attestation_effect(contract, asset),
            "concurrent-two",
        ),
    )
    stores = (SQLiteStore(db_path), SQLiteStore(db_path))
    barrier = Barrier(2)
    for current in stores:
        original = current.commit_ingress_batch

        def synchronized_commit(*args, _original=original, **kwargs):
            barrier.wait(timeout=5)
            return _original(*args, **kwargs)

        current.commit_ingress_batch = synchronized_commit
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = tuple(
                pool.map(
                    lambda pair: CandidateCommitter(pair[0], pair[0]).commit(
                        pair[1], genesis
                    ),
                    zip(stores, candidates, strict=True),
                )
            )
        assert all(decision.accepted for decision in decisions)
        assert len(store.scan_runtime_records(record_type="output.qualified")) == 2
        assert len(store.scan_runtime_records(record_type="lifecycle.terminal")) == 1
        assert all_outputs_satisfied(contract, store)
    finally:
        for current in stores:
            current.close()
