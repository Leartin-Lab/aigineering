"""Batch-local bindings compile once to ordinary exact-ID Contract facts."""

from dataclasses import replace

import pytest
from conftest import candidate_runtime

from aigineering.core import commitment, effect_projectors, projection_context
from aigineering.core.actor_facts import candidate_actor_capabilities
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.declaration_bindings import bind_declaration_assets
from aigineering.core.effect_projection import project_effect_batch
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.signing import Ed25519Signer, create_verifier
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    candidate_received_record,
    create_candidate_proposal,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
    local_asset_reference,
)
from aigineering.protocol.identity import CONTRACT_SELF_REFERENCE, contract_from_fields
from aigineering.protocol.types import Asset


def _case(store, *, input_ref=None, parent_id=None, authority=(), runtime=None):
    runtime = runtime or candidate_runtime(store)
    template = contract_from_fields(
        name="bound-declaration",
        description="Keep candidate-asset:0 as literal prose.",
        parent_id=parent_id,
        inputs=("source",),
        outputs=("answer",),
        activation="source",
        budget=2,
        labels=("guide",),
        origin="human",
        tool_scope=(),
        context_asset_ids=(local_asset_reference(1),),
        minting_authority=authority,
        sensitive_input_policy={
            "input_asset_bindings": {
                "source": [
                    input_ref if input_ref is not None else local_asset_reference(0)
                ],
                "unused": ["candidate-asset:999"],
            }
        },
        acceptance_policy={
            "mode": "independent",
            "policy_version": "bound-v1",
            "evidence_asset_ids": [local_asset_reference(0)],
        },
    )
    effects = (
        asset_proposal_effect(
            Asset(
                id="fixture:source",
                name="source",
                content="e\u0301雪42",
                promptable=False,
                disclosure_view="sealed",
            )
        ),
        asset_proposal_effect(Asset(id="fixture:guide", name="guide", content="guide")),
        contract_declaration_effect(template, bind_local_assets=True),
    )
    candidate = create_candidate_proposal(
        domain_id=runtime.genesis.id,
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        signer=runtime.signer,
        effects=effects,
        idempotency_key="bound-template",
    )
    return runtime, template, candidate


def _reason(decision):
    return next(
        str(r.payload["reason"])
        for r in decision.runtime_records
        if r.record_type == "candidate.rejected"
    )


def test_local_bindings_have_indexed_reference_equivalence_and_maker_free_replay(
    tmp_path, monkeypatch
):
    with SQLiteStore(tmp_path / "bindings.db") as store:
        runtime, template, candidate = _case(store)
        runtime.accept_asset(
            Asset(id="fixture:old", name="source", content="old same-name content")
        )
        request = projection_context.build_contract_declaration_projection_request(
            candidate
        )
        assert request is not None
        narrow = projection_context.load_candidate_projection_context(
            store, candidate, actor_keys=runtime.genesis.root_keys
        )
        full = projection_context.load_effect_projection_context(
            store, actor_keys=runtime.genesis.root_keys
        )
        receipt = candidate_received_record(
            candidate,
            runtime.genesis,
            verifier_factory=create_verifier,
            actor_keys=runtime.genesis.root_keys,
        )
        capabilities = candidate_actor_capabilities(
            candidate, runtime.genesis.root_keys
        )
        selected = project_effect_batch(candidate, receipt.id, capabilities, narrow)
        reference = project_effect_batch(candidate, receipt.id, capabilities, full)

        def semantic_records(batch):
            return [
                (r.id, r.record_type, r.payload, r.causal_parents)
                for r in batch.records
            ]

        assert semantic_records(selected) == semantic_records(reference)
        assert selected.contracts == reference.contracts
        assert selected.assets == reference.assets
        assert request.incoming_contract_ids == (selected.contract.id,)
        assert set(request.asset_ids) == {a.id for a in selected.assets}
        assert template.id != selected.contract.id
        assert not narrow.assets  # no same-name historical Asset is selected

        def forbidden(*args, **kwargs):
            raise AssertionError("fresh read must not load unrelated history")

        original_seal = effect_projectors.sign_asset
        seals = []

        def seal(*args, **kwargs):
            seals.append(args[0].name)
            return original_seal(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(
                projection_context, "load_effect_projection_context", forbidden
            )
            patch.setattr(store, "get_all_assets", forbidden)
            patch.setattr(store, "get_all_contracts", forbidden)
            patch.setattr(effect_projectors, "sign_asset", seal)
            decision = commitment.CandidateCommitter(store, store).commit(
                candidate, runtime.genesis
            )
        assert decision.accepted
        assert seals == ["source", "guide"]  # hint calculation never projects/seals
        actual = decision.contract
        validate_contract_commitment(actual, assets=decision.assets)
        assert actual.description == template.description
        assert actual.sensitive_input_policy["input_asset_bindings"]["unused"] == (
            "candidate-asset:999",
        )
        assert actual.context_asset_ids == (decision.assets[1].id,)
        assert actual.acceptance_policy["evidence_asset_ids"] == (
            decision.assets[0].id,
        )
        assert actual.sensitive_input_policy["input_asset_bindings"]["source"] == (
            decision.assets[0].id,
        )
        assert decision.assets[0].content == "e\u0301雪42"
        assert decision.assets[0].disclosure_view == "sealed"
        assert decision.assets[0].promptable is False
        assert actual.acceptance_policy["mode"] == "independent"
        # An older writer's unchanged declaration projector cannot silently
        # accept the new marker/template, even with the real Assets available.
        with pytest.raises(ValueError, match="missing Asset"):
            effect_projectors.project_contract_declaration(
                candidate.effects[-1],
                candidate,
                receipt.id,
                EffectProjectionContext(assets=decision.assets),
            )
        before = store.get_runtime_revision()
        with monkeypatch.context() as patch:
            patch.setattr(
                projection_context, "load_candidate_projection_context", forbidden
            )
            patch.setattr(commitment, "project_effect_batch", forbidden)
            replay = commitment.CandidateCommitter(store, store).commit(
                candidate, runtime.genesis
            )
        assert (
            replay.accepted
            and replay.contracts == decision.contracts
            and replay.assets == decision.assets
        )
        assert store.get_runtime_revision() == before


@pytest.mark.parametrize(
    "value,reason",
    [
        ("candidate-asset:2", "prior asset.propose"),
        ("candidate-asset:9", "prior asset.propose"),
        ("candidate-asset:-1", "invalid local Asset"),
        ("candidate-asset:01", "invalid local Asset"),
        ("candidate-asset:١", "invalid local Asset"),
        ("candidate-asset:x", "invalid local Asset"),
        ("candidate-asset:1", "does not match its role"),
    ],
)
def test_bad_local_bindings_reach_the_feature_fence_and_leave_no_effects(
    tmp_path, value, reason
):
    with SQLiteStore(tmp_path / "bad.db") as store:
        runtime, _, candidate = _case(store, input_ref=value)
        decision = commitment.CandidateCommitter(store, store).commit(
            candidate, runtime.genesis
        )
        assert not decision.accepted
        assert reason in _reason(decision)
        assert not store.get_all_assets() and not store.get_all_contracts()
        assert not store.scan_runtime_records(record_type="allowance.granted")
        assert not store.scan_runtime_records(record_type="candidate.committed")


def test_marker_requires_true_and_an_active_input_and_does_not_weaken_template_identity(
    tmp_path,
):
    with SQLiteStore(tmp_path / "marker.db") as store:
        runtime, template, candidate = _case(store)
        for marker in (False, 1, "true"):
            effect = CandidateEffect(
                "contract.declare",
                {**candidate.effects[-1].payload, "bind_local_assets": marker},
            )
            with pytest.raises(ValueError, match="must be true"):
                bind_declaration_assets(effect, candidate, 2)
        fields = dict(candidate.effects[-1].payload["contract"])
        fields["id"] = "task:v6:tampered"
        with pytest.raises(ValueError, match="canonical v6 identity"):
            bind_declaration_assets(
                CandidateEffect(
                    "contract.declare", {"contract": fields, "bind_local_assets": True}
                ),
                candidate,
                2,
            )
        plain = replace(
            template,
            sensitive_input_policy={
                "input_asset_bindings": {"source": ["asset:external"]}
            },
        )
        values = dict(candidate.effects[-1].payload["contract"])
        values.pop("id")
        values["sensitive_input_policy"] = plain.sensitive_input_policy
        plain = contract_from_fields(**values)
        with pytest.raises(ValueError, match="active local input binding"):
            bind_declaration_assets(
                contract_declaration_effect(plain, bind_local_assets=True), candidate, 2
            )
        assert (
            bind_declaration_assets(contract_declaration_effect(template), candidate, 2)
            is None
        )


def test_local_binding_does_not_grant_contract_publication_authority(tmp_path):
    with SQLiteStore(tmp_path / "authority.db") as store:
        runtime, _, candidate = _case(store)
        signer = Ed25519Signer()
        key = ActorKey(
            "limited", "key", signer.kind, signer.signer_id, ("asset.publish",)
        )
        runtime.authorize_actor(key)
        limited = create_candidate_proposal(
            domain_id=runtime.genesis.id,
            actor_id=key.actor_id,
            key_id=key.key_id,
            signer=signer,
            effects=candidate.effects,
            idempotency_key="limited-bound",
        )
        decision = commitment.CandidateCommitter(store, store).commit(
            limited, runtime.genesis
        )
        assert not decision.accepted and "contract.publish" in _reason(decision)
        assert not store.get_all_assets() and not store.get_all_contracts()


def test_self_referential_authority_uses_existing_canonical_identity_owner(tmp_path):
    with SQLiteStore(tmp_path / "self.db") as store:
        runtime, _, candidate = _case(
            store, authority=(f"_method_{CONTRACT_SELF_REFERENCE}_",)
        )
        decision = commitment.CandidateCommitter(store, store).commit(
            candidate, runtime.genesis
        )
        assert decision.accepted
        assert decision.contract.minting_authority == (
            f"_method_{decision.contract.id}_",
        )
        validate_contract_commitment(decision.contract, assets=decision.assets)


def test_interruption_rolls_back_all_completion_and_cancellation_consequences(
    tmp_path, monkeypatch
):
    from aigineering.core.control_plane import build_control_plane_contract
    from aigineering.core.runtime_projection import RuntimeProjection

    with SQLiteStore(tmp_path / "consequences.db") as store:
        runtime = candidate_runtime(store)
        consumer = runtime.accept_contract(
            build_control_plane_contract(name="consumer", outputs=("source",), budget=5)
        )
        child = runtime.accept_contract(
            replace(
                build_control_plane_contract(
                    name="pending-child", outputs=("later",), budget=1
                ),
                id="fixture:child",
                parent_id=consumer.id,
            )
        )
        runtime, _, candidate = _case(store, parent_id=consumer.id, runtime=runtime)
        request = projection_context.build_contract_declaration_projection_request(
            candidate
        )
        assert request.allowance_source_contract_ids == (consumer.id,)
        before = store.get_runtime_revision()
        original_insert = store._insert_runtime_record

        def interrupted(record):
            if record.record_type == "candidate.committed":
                raise RuntimeError("injected complete-closure interruption")
            return original_insert(record)

        with monkeypatch.context() as patch:
            patch.setattr(store, "_insert_runtime_record", interrupted)
            with pytest.raises(RuntimeError, match="complete-closure interruption"):
                commitment.CandidateCommitter(store, store).commit(
                    candidate, runtime.genesis
                )
        assert store.get_runtime_revision() == before
        assert not store.get_all_assets()
        assert RuntimeProjection(store, store).contract_view(consumer).terminal is None
        assert RuntimeProjection(store, store).contract_view(child).terminal is None
        decision = commitment.CandidateCommitter(store, store).commit(
            candidate, runtime.genesis
        )
        assert decision.accepted
        assert (
            RuntimeProjection(store, store).contract_view(consumer).terminal
            == "complete"
        )
        assert (
            RuntimeProjection(store, store).contract_view(child).terminal == "cancelled"
        )
        records = [r for _, r in store.scan_runtime_records(after_revision=before)]
        (receipt,) = [r for r in records if r.record_type == "candidate.received"]
        (head,) = [r for r in records if r.record_type == "candidate.committed"]
        assert set(head.payload["committed_record_ids"]) == {
            r.id for r in records if r.id not in (receipt.id, head.id)
        }
        assert decision.contract.parent_id == consumer.id


def test_many_declarations_share_scalar_hint_work_and_keep_one_atomic_publication(
    tmp_path, monkeypatch
):
    import aigineering.core.declaration_bindings as binding_owner
    from aigineering.protocol.wire import contract_to_dict

    with SQLiteStore(tmp_path / "many.db") as store:
        runtime, template, original = _case(store)
        declarations = []
        for index in range(16):
            fields = contract_to_dict(template)
            fields.pop("id")
            fields.update(name=f"consumer-{index}", outputs=[f"answer_{index}"])
            declarations.append(
                contract_declaration_effect(
                    contract_from_fields(**fields), bind_local_assets=True
                )
            )
        candidate = create_candidate_proposal(
            domain_id=runtime.genesis.id,
            actor_id=runtime.actor_key.actor_id,
            key_id=runtime.actor_key.key_id,
            signer=runtime.signer,
            effects=(*original.effects[:2], *declarations),
            idempotency_key="many-bound",
        )
        scalar = binding_owner.proposed_asset_identity
        lookups = []

        def observe(*args, **kwargs):
            lookups.append(args[1]["name"])
            return scalar(*args, **kwargs)

        monkeypatch.setattr(binding_owner, "proposed_asset_identity", observe)
        decision = commitment.CandidateCommitter(store, store).commit(
            candidate, runtime.genesis
        )
        assert decision.accepted
        assert lookups == [
            "source",
            "guide",
        ]  # shared inputs are hashed once, not once per Task
        assert len(decision.contracts) == 16
        source, guide = decision.assets
        for task in decision.contracts:
            assert task.sensitive_input_policy["input_asset_bindings"]["source"] == (
                source.id,
            )
            assert task.context_asset_ids == (guide.id,)
            assert task.acceptance_policy["evidence_asset_ids"] == (source.id,)
        assert len(store.scan_runtime_records(record_type="candidate.committed")) == 1


def test_claim_bound_mixed_action_keeps_existing_rejection_and_indexed_hint(
    tmp_path, monkeypatch
):
    from conftest import hosted_worker
    from aigineering.agent.mock import MockWorker
    from aigineering.core.control_plane import build_control_plane_contract
    from aigineering.protocol.candidate import CandidateClaimBinding
    from aigineering.runtime import claim_next_package

    with SQLiteStore(tmp_path / "mixed.db") as store:
        runtime, _, original = _case(store)
        parent = runtime.accept_contract(
            build_control_plane_contract(
                name="claimed", outputs=("source", "guide"), budget=5
            )
        )
        host = hosted_worker(
            store,
            MockWorker(worker_id="worker:local-reference"),
            genesis=runtime.genesis,
            authority_key=runtime.actor_key,
            authority_signer=runtime.signer,
        )
        claimed = claim_next_package(
            store,
            worker_id=host.worker_id,
            contract_id=parent.id,
            claim_candidate=host.claim_candidate(
                request_id="mixed-claim", contract_id=parent.id
            ),
        )
        assert claimed is not None
        binding = CandidateClaimBinding(
            parent.id,
            claimed.package.claim_id,
            claimed.package.claim_epoch,
            claimed.package.package_id,
        )
        candidate = create_candidate_proposal(
            domain_id=runtime.genesis.id,
            actor_id=host.actor_key.actor_id,
            key_id=host.actor_key.key_id,
            signer=host.signer,
            effects=original.effects,
            idempotency_key="mixed-action",
            claim_binding=binding,
        )
        request = projection_context.build_contract_declaration_projection_request(
            candidate
        )
        assert request is not None and parent.id in request.parent_contract_ids

        def forbidden(*args, **kwargs):
            raise AssertionError("mixed request must not silently read all history")

        monkeypatch.setattr(
            projection_context, "load_effect_projection_context", forbidden
        )
        decision = commitment.CandidateCommitter(store, store).commit(
            candidate, runtime.genesis
        )
        assert not decision.accepted and "unsupported effect" in _reason(decision)
        assert not store.get_all_assets()
        assert len(store.get_all_contracts()) == 1
