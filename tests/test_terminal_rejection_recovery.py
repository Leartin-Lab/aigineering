"""Rejected Worker output must not rewrite a terminal task's outcome."""

from __future__ import annotations

import pytest
from threading import Event, Thread

from aigineering.agent.mock import MockWorker
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
    publish_effect,
)
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.handoff import HANDOFF_EFFECTS, project_task_handoffs
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    asset_proposal_effect,
    task_handoff_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Contract
from aigineering.protocol.types import Asset
from aigineering.runtime import (
    claim_next_package,
    execute_claimed_package,
    process_rejected_submissions,
)
from aigineering.task_views import project_task_status
from conftest import hosted_worker


def _domain(path):
    store = SQLiteStore(path)
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        (
            "actor.authorize",
            "contract.cancel",
            "contract.publish",
            "task.handoff",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest("terminal-rejection", (actor,), "policy:test")
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name="active source",
        description="The source attempt must stay cancelled.",
        outputs=("report",),
        budget=4,
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


def test_late_signed_submit_stays_audited_without_recovery_and_survives_reopen(
    tmp_path,
):
    database = tmp_path / "terminal-rejection.sqlite"
    store, signer, actor, genesis, source = _domain(database)
    worker = MockWorker(worker_id="worker:late")
    worker.set_output(source.name, '/exec {"outputs":{"report":"stale result"}}')
    host = hosted_worker(
        store,
        worker,
        genesis=genesis,
        authority_key=actor,
        authority_signer=signer,
    )
    claimed = claim_next_package(
        store, worker_id=worker.worker_id, contract_id=source.id
    )
    assert claimed is not None

    handoff = publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_handoff_effect(source.id, "Keep the cancelled source immutable"),
        idempotency_key="cancel-source",
        additional_effects=HANDOFF_EFFECTS,
    )
    assert handoff.accepted
    link = project_task_handoffs(handoff.runtime_records, source_contract_id=source.id)[
        0
    ]
    with pytest.raises(CandidateCommitRejected):
        execute_claimed_package(claimed, host, store)
    assert store.scan_runtime_records(record_type="candidate.rejected")

    assert process_rejected_submissions(store) == [source.id]
    unavailable = store.scan_runtime_records(
        record_type="candidate_rejection.recovery_unavailable"
    )
    assert len(unavailable) == 1
    assert unavailable[0][1].payload["reason"] == "terminal_preserved"
    assert unavailable[0][1].payload["terminal"] == "cancelled"
    assert not store.scan_runtime_records(
        record_type="candidate_rejection.recovery_scheduled"
    )
    assert not any(item.origin == "recovery" for item in store.get_all_contracts())
    assert RuntimeProjection(store, store).contract_view(source).terminal == "cancelled"
    store.close()

    reopened = SQLiteStore(database)
    assert process_rejected_submissions(reopened) == []
    assert (
        len(
            reopened.scan_runtime_records(
                record_type="candidate_rejection.recovery_unavailable"
            )
        )
        == 1
    )
    assert project_task_status(source, reopened)["status"] == "cancelled"
    successor = reopened.get_contract(link.payload["successor_id"])
    assert successor is not None
    worker.set_output(successor.name, '/exec {"outputs":{"report":"successor result"}}')
    next_claim = claim_next_package(
        reopened, worker_id=worker.worker_id, contract_id=successor.id
    )
    assert next_claim is not None
    result = execute_claimed_package(next_claim, host, reopened)
    assert result["accepted"] == ["report"]
    assert project_task_status(successor, reopened)["status"] == "completed"
    assert project_task_status(source, reopened)["status"] == "cancelled"
    terminals = {
        str(record.payload["contract_id"]): str(record.payload["terminal"])
        for _, record in reopened.scan_runtime_records(record_type="lifecycle.terminal")
    }
    assert terminals[source.id] == "cancelled"
    assert terminals[successor.id] == "complete"
    reopened.close()


@pytest.mark.parametrize("terminal", ["complete", "cancelled", "unreachable"])
def test_nonfailed_terminal_rejection_is_marked_unavailable_idempotently(
    tmp_path, terminal
):
    store = SQLiteStore(tmp_path / f"{terminal}.sqlite")
    source = Contract(id=f"task:{terminal}", outputs=("report",), budget=2)
    store.add_contract(source)
    received = create_runtime_record(
        "candidate.received",
        {
            "candidate_id": f"candidate:{terminal}",
            "contract_id": source.id,
            "raw_output": "opaque worker output",
        },
    )
    rejected = create_runtime_record(
        "candidate.rejected",
        {
            "candidate_id": f"candidate:{terminal}",
            "contract_id": source.id,
            "reason": "stale claim",
        },
        causal_parents=(received.id,),
    )
    store.append_runtime_record(received)
    store.append_runtime_record(rejected)
    store.append_runtime_record(
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": source.id, "terminal": terminal},
            causal_parents=(rejected.id,),
        )
    )

    assert process_rejected_submissions(store) == [source.id]
    assert process_rejected_submissions(store) == []
    marker = store.scan_runtime_records(
        record_type="candidate_rejection.recovery_unavailable"
    )
    assert len(marker) == 1
    assert marker[0][1].payload["terminal"] == terminal
    assert marker[0][1].payload["reason"] == "terminal_preserved"
    assert any(contract.id == source.id for contract in store.get_all_contracts())
    assert not store.scan_runtime_records(
        record_type="candidate_rejection.recovery_scheduled"
    )
    store.close()


def test_failed_terminal_rejection_still_schedules_recovery(tmp_path):
    store = SQLiteStore(tmp_path / "failed-recovery.sqlite")
    operator_signer = Ed25519Signer()
    operator = ActorKey(
        "human:operator",
        "operator-key",
        operator_signer.kind,
        operator_signer.signer_id,
        ("contract.publish",),
    )
    recovery_signer = Ed25519Signer()
    recovery_actor = ActorKey(
        "plugin:recovery.publish.v1",
        "recovery-key",
        recovery_signer.kind,
        recovery_signer.signer_id,
        (
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    genesis = create_genesis_manifest(
        "failed-recovery", (operator, recovery_actor), "policy:test"
    )
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name="failed source", outputs=("report",), budget=1
    )
    assert publish_effect(
        store,
        store,
        genesis,
        operator,
        operator_signer,
        contract_declaration_effect(source),
        idempotency_key="declare-failed-source",
    ).accepted
    candidate = create_runtime_record(
        "candidate.received",
        {
            "candidate_id": "candidate:failed",
            "contract_id": source.id,
            "raw_output": "undeclared: rejected",
            "worker_id": "worker",
        },
    )
    rejection = create_runtime_record(
        "candidate.rejected",
        {
            "candidate_id": "candidate:failed",
            "contract_id": source.id,
            "reason": "undeclared output",
        },
        causal_parents=(candidate.id,),
    )
    terminal = create_runtime_record(
        "lifecycle.terminal",
        {"contract_id": source.id, "terminal": "failed"},
        causal_parents=(rejection.id,),
    )
    for record in (candidate, rejection, terminal):
        store.append_runtime_record(record)

    registry = CandidatePublisherRegistry(
        (
            (
                "recovery.publish.v1",
                CandidatePublisher(
                    store, store, genesis, recovery_actor, recovery_signer
                ),
            ),
        )
    )
    assert process_rejected_submissions(store, candidate_publishers=registry) == [
        source.id
    ]
    assert store.scan_runtime_records(
        record_type="candidate_rejection.recovery_scheduled"
    )
    assert any(
        contract.origin == "recovery" and contract.name == f"{source.name}.recover"
        for contract in store.get_all_contracts()
    )
    assert not store.scan_runtime_records(
        record_type="candidate_rejection.recovery_unavailable"
    )
    store.close()


@pytest.mark.parametrize("race", ["revocation", "partial-output"])
def test_handoff_snapshot_race_is_durably_rejected(tmp_path, race):
    database = tmp_path / f"{race}.sqlite"
    store = SQLiteStore(database)
    concurrent = SQLiteStore(database)
    signer = Ed25519Signer()
    handoff_actor = ActorKey(
        "human:handoff",
        "handoff-key",
        signer.kind,
        signer.signer_id,
        ("task.handoff", "contract.cancel", "contract.publish"),
    )
    admin = ActorKey(
        "human:admin",
        "admin-key",
        signer.kind,
        signer.signer_id,
        ("actor.revoke", "contract.publish", "asset.publish"),
    )
    genesis = create_genesis_manifest(
        f"snapshot-{race}", (handoff_actor, admin), "policy:test"
    )
    initialize_genesis(store, genesis)
    source = build_control_plane_contract(
        name=f"snapshot {race}",
        description="Concurrent facts must invalidate the earlier handoff projection.",
        outputs=("draft", "final"),
        budget=5,
    )
    assert publish_effect(
        store,
        store,
        genesis,
        admin,
        signer,
        contract_declaration_effect(source),
        idempotency_key=f"declare-{race}",
    ).accepted

    reached = Event()
    release = Event()
    original_commit = store.commit_ingress_batch

    def pause_at_snapshot(*args, **kwargs):
        if kwargs.get("validate_snapshot") is not None and not reached.is_set():
            reached.set()
            if not release.wait(10):
                raise TimeoutError("snapshot race release timed out")
        return original_commit(*args, **kwargs)

    store.commit_ingress_batch = pause_at_snapshot
    decisions = []
    errors = []

    def publish_handoff():
        try:
            proposal = create_candidate_proposal(
                domain_id=genesis.id,
                actor_id=handoff_actor.actor_id,
                key_id=handoff_actor.key_id,
                effects=(task_handoff_effect(source.id, "snapshot race"),),
                signer=signer,
                idempotency_key=f"handoff-{race}",
            )
            decisions.append(
                CandidateCommitter(
                    store, store, additional_effects=HANDOFF_EFFECTS
                ).commit(proposal, genesis)
            )
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=publish_handoff)
    thread.start()
    assert reached.wait(10), "handoff did not reach its transactional snapshot check"
    try:
        if race == "revocation":
            concurrent_decision = publish_effect(
                concurrent,
                concurrent,
                genesis,
                admin,
                signer,
                CandidateEffect(
                    "actor.revoke",
                    {
                        "actor_id": handoff_actor.actor_id,
                        "key_id": handoff_actor.key_id,
                        "reason": "concurrent revocation",
                    },
                ),
                idempotency_key="revoke-handoff-actor",
            )
        else:
            concurrent_decision = publish_effect(
                concurrent,
                concurrent,
                genesis,
                admin,
                signer,
                asset_proposal_effect(
                    Asset(id="asset:snapshot-draft", name="draft", content="draft")
                ),
                idempotency_key="publish-partial-output",
            )
        assert concurrent_decision.accepted
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors, errors
    assert len(decisions) == 1
    assert not decisions[0].accepted
    assert not store.scan_runtime_records(record_type="task.handed_off")
    assert not any(
        record.payload.get("contract_id") == source.id
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    )
    candidate_id = decisions[0].candidate_id
    if race == "revocation":
        auth_rejections = [
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.authentication_rejected"
            )
            if record.payload.get("claimed_candidate_id") == candidate_id
        ]
        assert len(auth_rejections) == 1
        assert "revok" in auth_rejections[0].payload["reason"].lower()
        assert (
            store.scan_runtime_records(
                record_type="candidate_rejection.recovery_unavailable"
            )
            == []
        )
    else:
        candidate_rejections = [
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.rejected"
            )
            if record.payload.get("candidate_id") == candidate_id
        ]
        assert len(candidate_rejections) == 1
        assert "projection" in candidate_rejections[0].payload["reason"].lower()
        assert len(store.get_assets_by_name("draft")) == 1
    store.close()
    concurrent.close()
