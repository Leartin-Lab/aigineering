"""Authorized effect extensions cannot manufacture task satisfaction."""

import pytest

from aigineering.core.acceptance import (
    acceptance_policy_id,
    project_asset_attestation_records,
)
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.effect_projectors import EffectProjection
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    CandidateEffect,
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
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset
from aigineering.runtime import claim_next_package


def _domain(tmp_path, *, independent=False, verifier_caps=()):
    store = SQLiteStore(tmp_path / "fence.db")
    owner_signer, worker_signer, verifier_signer = (
        Ed25519Signer(),
        Ed25519Signer(),
        Ed25519Signer(),
    )
    owner = ActorKey(
        "operator",
        "operator-key",
        owner_signer.kind,
        owner_signer.signer_id,
        ("contract.publish", "asset.publish", "worker.register", "probe.complete"),
    )
    worker = ActorKey(
        "worker",
        "worker-key",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.claim", "worker.submit"),
    )
    verifier = ActorKey(
        "verifier",
        "verifier-key",
        verifier_signer.kind,
        verifier_signer.signer_id,
        ("probe.policy", *verifier_caps),
    )
    genesis = create_genesis_manifest(
        "completion-fence", (owner, worker, verifier), "policy:test"
    )
    initialize_genesis(store, genesis)
    policy = (
        {
            "mode": "independent",
            "policy_version": "v1",
            "verifier_capabilities": ["verify.special"],
        }
        if independent
        else {"mode": "mechanical"}
    )
    task = build_control_plane_contract(
        name="obtain-report", outputs=("report",), budget=3, acceptance_policy=policy
    )
    result = publish_effect(
        store,
        store,
        genesis,
        owner,
        owner_signer,
        contract_declaration_effect(task),
        idempotency_key="task",
    )
    assert result.accepted, result
    return (
        store,
        genesis,
        owner,
        owner_signer,
        worker,
        worker_signer,
        verifier,
        verifier_signer,
        task,
    )


def _candidate(genesis, actor, signer, effects, key, *, binding=None):
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=effects,
        signer=signer,
        idempotency_key=key,
        claim_binding=binding,
    )


def _complete_handler(task):
    def project(effect, candidate, receipt_id, context):
        del effect, candidate, receipt_id, context
        return EffectProjection(
            records=(create_terminal_record(task.id, "complete"),),
            relation_target=task.id,
        )

    return EffectHandler(
        "probe.complete",
        project,
        direct_output_fact_types=frozenset({"lifecycle.terminal"}),
    )


@pytest.mark.parametrize("outputs", [("report",), ()])
def test_signed_control_extension_cannot_complete_without_required_asset(
    tmp_path, outputs
):
    store, genesis, owner, signer, *_, task = _domain(tmp_path)
    if not outputs:
        task = build_control_plane_contract(name="no-asset-condition", budget=1)
        result = publish_effect(
            store,
            store,
            genesis,
            owner,
            signer,
            contract_declaration_effect(task),
            idempotency_key="empty-task",
        )
        assert result.accepted
    before = tuple(
        r.id for _, r in store.scan_runtime_records(record_type="allowance.granted")
    )
    proposal = _candidate(
        genesis, owner, signer, (CandidateEffect("probe.complete", {}),), "complete"
    )
    decision = CandidateCommitter(
        store, store, additional_effects={"probe.complete": _complete_handler(task)}
    ).commit(proposal, genesis)
    assert not decision.accepted
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    assert (
        tuple(
            r.id for _, r in store.scan_runtime_records(record_type="allowance.granted")
        )
        == before
    )
    assert not store.scan_runtime_records(record_type="claim.granted")
    assert any(
        "satisfying declared Asset conditions" in str(r.payload.get("reason", ""))
        for r in decision.runtime_records
        if r.record_type == "candidate.rejected"
    )
    store.close()


def test_same_batch_asset_allows_canonical_completion_and_exact_replay(tmp_path):
    store, genesis, owner, signer, *_, task = _domain(tmp_path)
    candidate = _candidate(
        genesis,
        owner,
        signer,
        (
            asset_proposal_effect(
                Asset(id="unused", name="report", content="available")
            ),
        ),
        "asset-and-complete",
    )
    committer = CandidateCommitter(store, store)
    decision = committer.commit(candidate, genesis)
    assert decision.accepted, decision
    assert all_outputs_satisfied(task, store)
    terminals = store.scan_runtime_records(record_type="lifecycle.terminal")
    assert len(terminals) == 1 and terminals[0][1].payload["terminal"] == "complete"
    assert committer.commit(candidate, genesis).accepted
    store.close()
    with SQLiteStore(tmp_path / "fence.db") as reopened:
        digest = reopened.runtime_materialization_digest()
        assert reopened.rebuild_runtime_materializations(strict=True) == digest
        assert len(reopened.scan_runtime_records(record_type="lifecycle.terminal")) == 1


@pytest.mark.parametrize("asset_arrives_after_preview", [False, True])
def test_new_declaration_observes_existing_asset_without_domain_task_scan(
    tmp_path, monkeypatch, asset_arrives_after_preview
):
    store, genesis, owner, signer, *_ = _domain(tmp_path)
    task = build_control_plane_contract(
        name="new-consumer", outputs=("preexisting-result",), budget=1
    )
    with SQLiteStore(tmp_path / "fence.db") as other:

        def publish_asset():
            result = publish_effect(
                other,
                other,
                genesis,
                owner,
                signer,
                asset_proposal_effect(
                    Asset(id="ignored", name="preexisting-result", content="available")
                ),
                idempotency_key="existing-result",
            )
            assert result.accepted, result

        if asset_arrives_after_preview:
            original_commit = store.commit_ingress_batch

            def commit_after_asset(*args, **kwargs):
                publish_asset()
                return original_commit(*args, **kwargs)

            monkeypatch.setattr(store, "commit_ingress_batch", commit_after_asset)
        else:
            publish_asset()

        def forbidden_scan():
            raise AssertionError("declaration ingress must not scan domain tasks")

        monkeypatch.setattr(store, "get_all_contracts", forbidden_scan)
        decision = CandidateCommitter(store, store).commit(
            _candidate(
                genesis, owner, signer, (contract_declaration_effect(task),), "new-task"
            ),
            genesis,
        )
        assert decision.accepted, decision
        terminal = [
            record.payload["terminal"]
            for _, record in store.scan_runtime_records(
                record_type="lifecycle.terminal"
            )
            if record.payload.get("contract_id") == task.id
        ]
        assert terminal == ["complete"]
    store.close()


def _claimed_output(env):
    store, genesis, owner, signer, worker, worker_signer, _, _, task = env
    registered = publish_effect(
        store,
        store,
        genesis,
        owner,
        signer,
        worker_registration_effect(
            WorkerRegistration(
                worker.actor_id, actor_id=worker.actor_id, key_id=worker.key_id
            )
        ),
        idempotency_key="worker",
    )
    assert registered.accepted
    claim = claim_next_package(
        store,
        worker_id=worker.actor_id,
        contract_id=task.id,
        claim_candidate=_candidate(
            genesis,
            worker,
            worker_signer,
            (worker_claim_effect(worker.actor_id, contract_id=task.id),),
            "claim",
        ),
    )
    assert claim is not None
    binding = CandidateClaimBinding(
        task.id,
        claim.package.claim_id,
        claim.package.claim_epoch,
        claim.package.package_id,
    )
    produced = CandidateCommitter(store, store).commit(
        _candidate(
            genesis,
            worker,
            worker_signer,
            (
                asset_proposal_effect(
                    Asset(id="unused", name="report", content="unreviewed")
                ),
            ),
            "produce",
            binding=binding,
        ),
        genesis,
    )
    assert produced.accepted, produced
    return produced.assets[0]


@pytest.mark.parametrize(
    "caps", [(), ("asset.attest",), ("asset.attest", "verify.special")]
)
@pytest.mark.parametrize(
    "kind", ["qualification-only", "canonical", "changed-qualification"]
)
def test_policy_facts_must_pass_the_canonical_current_actor_gate(tmp_path, caps, kind):
    env = _domain(tmp_path, independent=True, verifier_caps=caps)
    store, genesis, _, _, _, _, verifier, signer, task = env
    target = _claimed_output(env)
    assert not all_outputs_satisfied(task, store)
    effect = asset_attestation_effect(
        task.id,
        "report",
        target.id,
        policy_id=acceptance_policy_id(task.acceptance_policy),
        policy_version="v1",
    )

    def project(request, candidate, receipt_id, context):
        del request
        if kind == "qualification-only":
            records = (
                create_runtime_record(
                    "output.qualified",
                    {
                        "contract_id": task.id,
                        "output_name": "report",
                        "asset_id": target.id,
                        "policy_id": acceptance_policy_id(task.acceptance_policy),
                        "verifier_actor_ids": [candidate.actor_id],
                    },
                    causal_parents=(receipt_id,),
                ),
            )
        else:
            records = project_asset_attestation_records(
                effect, candidate, receipt_id, context
            ).records
            if kind == "changed-qualification":
                qualification = records[-1]
                records = (
                    *records[:-1],
                    create_runtime_record(
                        qualification.record_type,
                        {
                            **qualification.payload,
                            "verifier_actor_ids": ["forged-verifier"],
                        },
                        causal_parents=qualification.causal_parents,
                    ),
                )
        return EffectProjection(records=records, relation_target=task.id)

    handler = EffectHandler(
        "probe.policy",
        project,
        direct_output_fact_types=frozenset({"asset.attested", "output.qualified"}),
    )
    candidate = _candidate(
        genesis, verifier, signer, (CandidateEffect("probe.policy", {}),), "policy"
    )
    decision = CandidateCommitter(
        store, store, additional_effects={"probe.policy": handler}
    ).commit(candidate, genesis)
    expected = kind == "canonical" and set(caps) == {"asset.attest", "verify.special"}
    assert decision.accepted is expected, decision
    assert all_outputs_satisfied(task, store) is expected
    assert (
        bool(store.scan_runtime_records(record_type="lifecycle.terminal")) is expected
    )
    assert bool(store.scan_runtime_records(record_type="output.qualified")) is expected
    if not expected:
        assert not store.scan_runtime_records(record_type="asset.attested")
        assert any(
            r.record_type == "candidate.rejected" for r in decision.runtime_records
        )
    store.close()


def test_external_asset_must_meet_the_consumer_shape_before_completion(tmp_path):
    store, genesis, owner, signer, *_, _task = _domain(tmp_path)
    task = build_control_plane_contract(
        name="numeric-result",
        outputs=("numeric",),
        budget=1,
        acceptance_policy={
            "mode": "mechanical",
            "output_shapes": {"numeric": "number"},
        },
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        owner,
        signer,
        contract_declaration_effect(task),
        idempotency_key="numeric-task",
    )
    assert declared.accepted
    wrong = publish_effect(
        store,
        store,
        genesis,
        owner,
        signer,
        asset_proposal_effect(Asset(id="unused", name="numeric", content='"text"')),
        idempotency_key="wrong-shape-asset",
    )
    assert wrong.accepted, wrong
    assert not all_outputs_satisfied(task, store)
    assert not store.scan_runtime_records(record_type="lifecycle.terminal")
    forced = _candidate(
        genesis,
        owner,
        signer,
        (CandidateEffect("probe.complete", {}),),
        "wrong-shape-complete",
    )
    result = CandidateCommitter(
        store, store, additional_effects={"probe.complete": _complete_handler(task)}
    ).commit(forced, genesis)
    assert not result.accepted
    right = publish_effect(
        store,
        store,
        genesis,
        owner,
        signer,
        asset_proposal_effect(Asset(id="unused", name="numeric", content="12")),
        idempotency_key="correct-shape-asset",
    )
    assert right.accepted, right
    assert all_outputs_satisfied(task, store)
    assert len(store.scan_runtime_records(record_type="lifecycle.terminal")) == 1
    store.close()
