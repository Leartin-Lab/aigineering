"""Signed, bounded task handoff behavior."""

from __future__ import annotations

import pytest
from threading import Barrier, Event, Thread

from aigineering.core.acceptance import acceptance_policy_id
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.commitment import CandidateCommitRejected, CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.provenance import sign_asset
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.session import runtime_record_contract_references
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.handoff import HANDOFF_EFFECTS, project_task_handoffs
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    asset_attestation_effect,
    contract_declaration_effect,
    task_comment_effect,
    task_handoff_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Asset
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.task_views import project_task_status
from aigineering.task_view_query import load_task_view_query_context


def test_handoff_link_is_scoped_to_source_and_successor_only():
    link = create_runtime_record(
        "task.handed_off",
        {
            "contract_id": "task:source",
            "successor_id": "task:successor",
            "reason": "handoff context mentions task:unrelated",
        },
    )

    assert runtime_record_contract_references(link) == {
        "task:source",
        "task:successor",
    }


def _workspace(tmp_path, *, independent_policy=True):
    store = SQLiteStore(tmp_path / "handoff.sqlite")
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        (
            "contract.cancel",
            "contract.publish",
            "task.handoff",
            "task.block",
            "task.comment",
            "worker.submit",
            "asset.publish",
        ),
    )
    limited = ActorKey(
        "human:cancel-only",
        "cancel-key",
        signer.kind,
        signer.signer_id,
        ("contract.cancel", "contract.publish"),
    )
    missing_cancel = ActorKey(
        "human:missing-cancel",
        "missing-cancel-key",
        signer.kind,
        signer.signer_id,
        ("task.handoff", "contract.publish"),
    )
    missing_publish = ActorKey(
        "human:missing-publish",
        "missing-publish-key",
        signer.kind,
        signer.signer_id,
        ("task.handoff", "contract.cancel"),
    )
    missing_block = ActorKey(
        "human:missing-block",
        "missing-block-key",
        signer.kind,
        signer.signer_id,
        ("task.handoff", "contract.cancel", "contract.publish"),
    )
    reviewer = ActorKey(
        "human:reviewer",
        "review-key",
        signer.kind,
        signer.signer_id,
        ("asset.attest", "review"),
    )
    genesis = create_genesis_manifest(
        "handoff-test",
        (actor, limited, missing_cancel, missing_publish, missing_block, reviewer),
        "policy:test",
    )
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name="research task",
        description="Complete a two-part report",
        outputs=("draft", "final"),
        budget=5,
        acceptance_policy=(
            {
                "mode": "independent",
                "policy_version": "review-v1",
                "rubric_asset_ids": [],
                "evidence_asset_ids": [],
                "required_attestations": 1,
                "verifier_capabilities": ["review"],
            }
            if independent_policy
            else None
        ),
        tool_scope=("search",),
        worker_capabilities=("research", "worker.submit", "asset.publish"),
        labels=("research",),
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(source),
        idempotency_key="declare-source",
    )
    assert declared.accepted
    return store, signer, actor, genesis, source


def _publish_handoff(store, signer, actor, genesis, source, key, *, hold_reason=""):
    return publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_handoff_effect(
            source.id, "Continue the unfinished report", hold_reason=hold_reason
        ),
        idempotency_key=key,
        additional_effects=HANDOFF_EFFECTS,
    )


def test_active_claim_handoff_is_atomic_bounded_held_and_replayable(tmp_path):
    store, signer, actor, genesis, source = _workspace(tmp_path)
    assert store.claim_contract(source.id, "worker:already-running") is not None

    decision = _publish_handoff(
        store,
        signer,
        actor,
        genesis,
        source,
        "handoff-once",
        hold_reason="Wait for source evidence",
    )
    assert decision.accepted
    link = project_task_handoffs(
        (record for _, record in store.scan_runtime_records()),
        source_contract_id=source.id,
    )[0]
    assert link.payload["completed_outputs"] == ()
    assert link.payload["retained_asset_ids"] == ()
    assert link.payload["hold_id"]
    successor_id = link.payload["successor_id"]
    successor = store.get_contract(successor_id)
    assert successor is not None
    assert successor.parent_id == source.id
    assert successor.outputs == source.outputs
    assert successor.budget == link.payload["budget"]
    assert successor.tool_scope == source.tool_scope
    assert successor.worker_capabilities == source.worker_capabilities
    assert successor.acceptance_policy == source.acceptance_policy
    assert successor.description.endswith(
        "Task handoff reason: Continue the unfinished report"
    )
    assert successor.inputs == source.inputs
    assert successor.context_asset_ids == source.context_asset_ids
    assert store.get_claim(source.id)["status"] != "active"
    assert store.claim_contract(successor_id, "worker:before-unblock") is None

    current = RuntimeProjection(store, store).contract_view(source)
    assert current.terminal == "cancelled"
    revisions = {
        record.id: revision for revision, record in store.scan_runtime_records()
    }
    link_revision = revisions[link.id]
    assert all(revisions[parent] < link_revision for parent in link.causal_parents)
    replay = _publish_handoff(
        store,
        signer,
        actor,
        genesis,
        source,
        "handoff-once",
        hold_reason="Wait for source evidence",
    )
    assert replay.accepted
    replay_link = project_task_handoffs(
        replay.runtime_records, source_contract_id=source.id
    )[0]
    assert replay_link.id == link.id
    assert (
        len(
            project_task_handoffs(
                (record for _, record in store.scan_runtime_records()),
                source_contract_id=source.id,
            )
        )
        == 1
    )
    store.close()


def test_handoff_requires_dedicated_capability_and_only_accepts_one_effect(tmp_path):
    store, signer, actor, genesis, source = _workspace(tmp_path)
    no_handoff = next(
        key for key in genesis.root_keys if key.actor_id == "human:cancel-only"
    )
    from aigineering.core.candidate_publisher import publish_effects

    rejected = publish_effect(
        store,
        store,
        genesis,
        no_handoff,
        signer,
        task_handoff_effect(source.id, "Continue"),
        idempotency_key="no-cap",
        additional_effects=HANDOFF_EFFECTS,
    )
    assert not rejected.accepted
    assert not any(
        record.record_type == "task.handed_off" for record in rejected.runtime_records
    )
    assert (
        project_task_handoffs(
            (record for _, record in store.scan_runtime_records()),
            source_contract_id=source.id,
        )
        == ()
    )

    mixed = publish_effects(
        store,
        store,
        genesis,
        actor,
        signer,
        (
            task_handoff_effect(source.id, "Continue"),
            task_comment_effect(source.id, "comment"),
        ),
        idempotency_key="mixed",
        additional_effects=HANDOFF_EFFECTS,
    )
    assert not mixed.accepted
    assert store.get_contract(source.id) is not None
    store.close()


def test_each_handoff_authority_capability_is_required(tmp_path):
    store, signer, actor, genesis, source = _workspace(tmp_path)
    denied = (
        ("human:cancel-only", "no-handoff-capability", ""),
        ("human:missing-cancel", "no-cancel-capability", ""),
        ("human:missing-publish", "no-publish-capability", ""),
        ("human:missing-block", "no-block-capability", "Wait"),
    )
    for actor_id, idempotency_key, hold_reason in denied:
        key = next(item for item in genesis.root_keys if item.actor_id == actor_id)
        result = publish_effect(
            store,
            store,
            genesis,
            key,
            signer,
            task_handoff_effect(source.id, "Continue safely", hold_reason=hold_reason),
            idempotency_key=idempotency_key,
            additional_effects=HANDOFF_EFFECTS,
        )
        assert not result.accepted
        assert not any(
            record.record_type == "task.handed_off" for record in result.runtime_records
        )
    assert (
        project_task_handoffs(
            (record for _, record in store.scan_runtime_records()),
            source_contract_id=source.id,
        )
        == ()
    )
    store.close()


def test_late_worker_submission_from_cancelled_claim_is_fenced(tmp_path):
    store, signer, actor, genesis, source = _workspace(tmp_path)
    store.register_worker(
        WorkerRegistration(
            actor.actor_id,
            actor_id=actor.actor_id,
            key_id=actor.key_id,
        )
    )
    claim = store.claim_contract(source.id, actor.actor_id, package_id="pkg:active")
    assert claim is not None
    _publish_handoff(store, signer, actor, genesis, source, "fence-active-claim")
    stale = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=(
            asset_proposal_effect(
                Asset(
                    id="asset:late", name="draft", content="late output", origin="tool"
                )
            ),
        ),
        signer=signer,
        idempotency_key="late-submit",
        claim_binding=CandidateClaimBinding(
            contract_id=source.id,
            claim_id=claim["claim_id"],
            claim_epoch=claim["epoch"],
            package_id=claim["package_id"],
        ),
    )
    with pytest.raises(CandidateCommitRejected):
        CandidateCommitter(store, store).commit(stale, genesis)
    assert store.get_assets_by_name("draft") == []
    assert store.get_contract(source.id) is not None
    store.close()


def test_successor_executes_and_keeps_independent_acceptance_gate(tmp_path):
    store, signer, actor, genesis, source = _workspace(tmp_path)
    handoff = _publish_handoff(store, signer, actor, genesis, source, "execute-child")
    assert handoff.accepted
    link = project_task_handoffs(handoff.runtime_records, source_contract_id=source.id)[
        0
    ]
    child = store.get_contract(link.payload["successor_id"])
    assert child is not None and child.acceptance_policy == source.acceptance_policy
    store.register_worker(
        WorkerRegistration(
            actor.actor_id,
            actor_id=actor.actor_id,
            key_id=actor.key_id,
        )
    )
    claim = store.claim_contract(child.id, actor.actor_id, package_id="pkg:child")
    assert claim is not None
    binding = CandidateClaimBinding(
        contract_id=child.id,
        claim_id=claim["claim_id"],
        claim_epoch=claim["epoch"],
        package_id=claim["package_id"],
    )
    assets = []
    proposal = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=tuple(
            asset_proposal_effect(
                Asset(id=f"asset:{output}", name=output, content=f"{output} result")
            )
            for output in child.outputs
        ),
        signer=signer,
        idempotency_key="child-output-batch",
        claim_binding=binding,
    )
    committed = CandidateCommitter(store, store).commit(proposal, genesis)
    assert committed.accepted, next(
        record.payload["reason"]
        for record in committed.runtime_records
        if record.record_type == "candidate.rejected"
    )
    for output in child.outputs:
        assets.extend(store.get_assets_by_name(output))

    assert not all_outputs_satisfied(child, store)
    reviewer = next(
        key for key in genesis.root_keys if key.actor_id == "human:reviewer"
    )
    policy = child.acceptance_policy
    assert policy is not None
    for output in child.outputs:
        asset = next(item for item in assets if item.name == output)
        attested = publish_effect(
            store,
            store,
            genesis,
            reviewer,
            signer,
            asset_attestation_effect(
                child.id,
                output,
                asset.id,
                policy_id=acceptance_policy_id(policy),
                policy_version=policy["policy_version"],
            ),
            idempotency_key=f"review-{output}",
        )
        assert attested.accepted
    assert all_outputs_satisfied(child, store)
    assert RuntimeProjection(store, store).contract_view(child).terminal == "complete"
    assert RuntimeProjection(store, store).contract_view(source).terminal == "cancelled"
    legacy_status = project_task_status(source, store)
    assert legacy_status["status"] == "cancelled"
    query_context = load_task_view_query_context(store)
    assert (
        project_task_status(
            source,
            store,
            projection_context=query_context.effect_projection,
            query_context=query_context,
        )
        == legacy_status
    )
    store.close()


def test_cancelled_contract_remains_typed_terminal_in_current_projection(tmp_path):
    store, signer, actor, genesis, source = _workspace(tmp_path)
    declaration_revision = store.get_runtime_revision()
    decision = _publish_handoff(
        store, signer, actor, genesis, source, "cancel-terminal"
    )
    assert decision.accepted
    assert RuntimeProjection(store, store).contract_view(source).terminal == "cancelled"
    assert (
        RuntimeProjection(store, store, as_of_revision=declaration_revision)
        .contract_view(source)
        .terminal
        is None
    )
    store.close()


def test_commit_time_projection_rechecks_source_before_handoff_write(
    tmp_path, monkeypatch
):
    store, signer, actor, genesis, source = _workspace(tmp_path)
    original_commit = store.commit_ingress_batch
    injected = False

    def terminal_wins(*args, **kwargs):
        nonlocal injected
        if not injected and kwargs.get("candidate_id"):
            injected = True
            store.append_runtime_record(
                create_terminal_record(source.id, "failed", actor_id="operator")
            )
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(store, "commit_ingress_batch", terminal_wins)
    decision = _publish_handoff(
        store, signer, actor, genesis, source, "handoff-loses-snapshot"
    )
    assert not decision.accepted
    assert (
        project_task_handoffs(
            (record for _, record in store.scan_runtime_records()),
            source_contract_id=source.id,
        )
        == ()
    )
    assert store.get_contract(source.id) == source
    store.close()


def test_partial_handoff_retains_exact_completed_asset_and_unfinished_slot(tmp_path):
    store, signer, actor, genesis, source = _workspace(
        tmp_path, independent_policy=False
    )
    completed = sign_asset(
        Asset(
            id="asset:finished-draft",
            name="draft",
            content="Finished draft",
            origin="human",
            created_by=source.id,
        ),
        signed_by="human",
    )
    store.add_asset(completed)

    decision = _publish_handoff(store, signer, actor, genesis, source, "partial")
    assert decision.accepted
    link = project_task_handoffs(
        decision.runtime_records, source_contract_id=source.id
    )[0]
    successor = store.get_contract(link.payload["successor_id"])
    assert successor is not None
    assert successor.outputs == ("final",)
    assert link.payload["completed_outputs"] == ("draft",)
    assert link.payload["retained_asset_ids"] == (completed.id,)
    assert successor.inputs == source.inputs
    assert completed.content not in successor.description
    assert completed.id not in successor.context_asset_ids
    store.close()


@pytest.mark.parametrize("race", ["terminal", "unfinished-child"])
def test_two_connection_writer_race_rechecks_handoff_snapshot(tmp_path, race):
    case = tmp_path / race
    case.mkdir()
    store, signer, actor, genesis, source = _workspace(case)
    concurrent = SQLiteStore(case / "handoff.sqlite")
    at_commit = Barrier(2)
    continue_commit = Event()
    original_commit = store.commit_ingress_batch

    def paused_commit(*args, **kwargs):
        if kwargs.get("validate_snapshot") is not None:
            at_commit.wait(timeout=5)
            assert continue_commit.wait(timeout=5)
        return original_commit(*args, **kwargs)

    store.commit_ingress_batch = paused_commit
    results = []

    def handoff():
        results.append(
            _publish_handoff(store, signer, actor, genesis, source, f"race-{race}")
        )

    thread = Thread(target=handoff)
    thread.start()
    at_commit.wait(timeout=5)
    if race == "terminal":
        concurrent.append_runtime_record(
            create_terminal_record(source.id, "failed", actor_id="operator")
        )
    else:
        child = contract_from_fields(
            name="parallel child",
            description="Concurrent unfinished child",
            parent_id=source.id,
            inputs=(),
            outputs=("child_result",),
            activation="",
            budget=1,
            tool_scope=(),
            labels=(),
            origin="human",
        )
        created = publish_effect(
            concurrent,
            concurrent,
            genesis,
            actor,
            signer,
            contract_declaration_effect(child),
            idempotency_key=f"parallel-child-{race}",
        )
        assert created.accepted
    continue_commit.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(results) == 1 and not results[0].accepted
    assert store.scan_runtime_records(record_type="task.handed_off") == []
    assert len([c for c in store.get_all_contracts() if c.parent_id == source.id]) == (
        1 if race == "unfinished-child" else 0
    )
    store.close()
    concurrent.close()


def test_handoff_fault_after_staging_rolls_back_terminal_child_and_claim(tmp_path):
    store, signer, actor, genesis, source = _workspace(tmp_path)
    claim = store.claim_contract(source.id, "worker:before-fault")
    assert claim is not None
    original_insert = store._insert_runtime_record

    def fail_at_link(record, *args, **kwargs):
        if record.record_type == "task.handed_off":
            raise RuntimeError("fault after terminal and successor staging")
        return original_insert(record, *args, **kwargs)

    store._insert_runtime_record = fail_at_link
    with pytest.raises(RuntimeError, match="fault after terminal"):
        _publish_handoff(store, signer, actor, genesis, source, "fault-rollback")
    assert store.scan_runtime_records(record_type="task.handed_off") == []
    assert store.scan_runtime_records(record_type="lifecycle.terminal") == []
    assert [c for c in store.get_all_contracts() if c.parent_id == source.id] == []
    assert store.get_claim(source.id)["status"] == "active"
    store.close()


def test_ordinary_planner_origin_task_can_be_handed_off(tmp_path):
    store, signer, actor, genesis, _source = _workspace(tmp_path)
    source = contract_from_fields(
        name="planner business task",
        description="An ordinary delegated task",
        parent_id=None,
        inputs=(),
        outputs=("planner_result",),
        activation="",
        budget=3,
        tool_scope=("search",),
        labels=("research",),
        context_asset_ids=(),
        worker_capabilities=("research",),
        worker_pools=(),
        delegation_capabilities=(),
        delegation_pools=(),
        origin="planner",
        minting_authority=(),
        sensitive_input_policy=None,
        acceptance_policy=None,
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        contract_declaration_effect(source),
        idempotency_key="declare-planner-source",
    )
    assert declared.accepted
    decision = _publish_handoff(
        store, signer, actor, genesis, source, "planner-handoff"
    )
    assert decision.accepted
    child = decision.contract
    assert child is not None and child.origin == "planner"
    store.close()
