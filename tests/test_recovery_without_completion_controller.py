"""Recovery publication uses ordinary signed effects, without a controller."""

from __future__ import annotations

from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.recovery import (
    RecoveryDisposition,
    schedule_projection_recovery,
)
from aigineering.plugins.recovery_history import (
    capture_recovery_history,
    prove_recovery_publication,
)
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    contract_declaration_effect,
    asset_proposal_effect,
)
from aigineering.protocol.identity import contract_from_fields


def _environment(tmp_path):
    store = SQLiteStore(tmp_path / "recovery-without-controller.sqlite")
    setup_signer = Ed25519Signer()
    recovery_signer = Ed25519Signer()
    setup_key = ActorKey(
        "human:setup",
        "key:setup",
        setup_signer.kind,
        setup_signer.signer_id,
        ("actor.authorize", "contract.publish", "asset.publish"),
    )
    recovery_key = ActorKey(
        "plugin:recovery.publish.v1",
        "key:recovery",
        recovery_signer.kind,
        recovery_signer.signer_id,
        (
            "contract.publish",
            "contract.publish.protected",
            "asset.publish",
            "asset.publish.protected",
        ),
    )
    genesis = create_genesis_manifest(
        "recovery-without-controller", (setup_key, recovery_key), "policy:test"
    )
    initialize_genesis(store, genesis)
    setup = CandidatePublisher(store, store, genesis, setup_key, setup_signer)
    recovery_publisher = CandidatePublisher(
        store, store, genesis, recovery_key, recovery_signer
    )
    root = contract_from_fields(
        name="root",
        description="Root contract for recovery tests.",
        inputs=(),
        outputs=("report",),
        activation="",
        budget=4,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    failed = contract_from_fields(
        name="failed-worker-task",
        description="Rejected worker task.",
        parent_id=root.id,
        inputs=(),
        outputs=("report",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    root_decision = setup.publish(
        (contract_declaration_effect(root),), idempotency_key="declare-root"
    )
    assert root_decision.accepted, [
        (record.record_type, record.payload.get("reason"))
        for record in root_decision.runtime_records
    ]
    failed_decision = setup.publish(
        (contract_declaration_effect(failed),), idempotency_key="declare-failed"
    )
    assert failed_decision.accepted, failed_decision.runtime_records
    failed_declaration = next(
        record
        for _revision, record in store.scan_runtime_records(
            record_type="contract.declared"
        )
        if record.payload["contract"]["id"] == failed.id
    )
    return store, recovery_publisher, failed, failed_declaration.id


def test_recovery_is_one_signed_declaration_and_context_asset_candidate(tmp_path):
    store, publisher, failed, source_record_id = _environment(tmp_path)
    before = store.get_runtime_revision()

    recovery = schedule_projection_recovery(
        store,
        publisher,
        failed_contract=failed,
        candidate_raw="malformed worker output",
        rejections=[
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
        source_record_ids=(source_record_id,),
    )

    assert recovery is not None
    context_asset = store.get_assets_by_name(f"_fail_context_{failed.id}")[0]
    new_records = tuple(
        record for revision, record in store.scan_runtime_records() if revision > before
    )
    receipts = [
        record for record in new_records if record.record_type == "candidate.received"
    ]
    heads = [
        record for record in new_records if record.record_type == "candidate.committed"
    ]
    assert len(receipts) == len(heads) == 1
    assert receipts[0].causal_parents == (failed.id, source_record_id)
    member_ids = tuple(heads[0].payload["committed_record_ids"])
    members = [store.get_runtime_record(record_id) for record_id in member_ids]
    assert any(
        record is not None
        and record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == recovery.id
        for record in members
    )
    assert any(
        record is not None
        and record.record_type == "asset.committed"
        and record.payload["asset"]["id"] == context_asset.id
        for record in members
    )
    assert not any(
        record.record_type == "lifecycle.terminal"
        or record.record_type.endswith(".recovery_scheduled")
        or (
            record.record_type == "trace.recorded"
            and record.payload.get("trace", {}).get("event_type")
            in {"recovery_scheduled", "method_recovery_scheduled"}
        )
        for record in new_records
    )
    assert any(
        record.record_type == "allowance.reserved"
        and record.payload.get("child_contract_id") == recovery.id
        for record in new_records
    )

    snapshot = capture_recovery_history(store, publisher)
    proof = prove_recovery_publication(
        snapshot,
        recovery=recovery,
        context_template=context_asset,
        effects=(
            contract_declaration_effect(recovery),
            asset_proposal_effect(context_asset),
        ),
        idempotency_key=f"projection-recovery:{failed.id}",
        causal_parents=(failed.id, source_record_id),
        source_record_ids=(source_record_id,),
    )
    assert proof.status == "matched", proof.reason
    assert proof.receipt_id == receipts[0].id
    store.close()


def test_exact_recovery_replay_is_read_only_and_no_method_result_maker(tmp_path):
    store, publisher, failed, source_record_id = _environment(tmp_path)
    arguments = {
        "store": store,
        "publisher": publisher,
        "failed_contract": failed,
        "candidate_raw": "malformed worker output",
        "rejections": [
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
        "source_record_ids": (source_record_id,),
        "include_disposition": True,
    }
    first = schedule_projection_recovery(**arguments)
    assert isinstance(first, RecoveryDisposition) and first.contract is not None
    revision = store.get_runtime_revision()
    second = schedule_projection_recovery(**arguments)

    assert isinstance(second, RecoveryDisposition)
    assert second.contract == first.contract
    assert store.get_runtime_revision() == revision
    import aigineering.plugins.recovery as recovery_module

    assert not hasattr(recovery_module, "schedule_method_result_recovery")
    assert not any(
        record.record_type == "lifecycle.terminal"
        or record.record_type.endswith(".recovery_scheduled")
        for _revision, record in store.scan_runtime_records()
    )
    store.close()
