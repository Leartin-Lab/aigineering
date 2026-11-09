"""Recovery history follows actual signed publications and source evidence."""

from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import candidate_runtime

from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.recovery import schedule_projection_recovery
from aigineering.plugins.recovery_history import (
    capture_recovery_history,
    prove_recovery_publication,
)
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Contract


def _environment(tmp_path, name="recovery-history"):
    store = SQLiteStore(tmp_path / f"{name}.sqlite3")
    setup_signer = Ed25519Signer()
    setup_key = ActorKey(
        f"human:{name}",
        "key:setup",
        setup_signer.kind,
        setup_signer.signer_id,
        (
            "actor.authorize",
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    recovery_signer = Ed25519Signer()
    recovery_key = ActorKey(
        "plugin:recovery.publish.v1",
        "key:recovery",
        recovery_signer.kind,
        recovery_signer.signer_id,
        (
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    genesis = create_genesis_manifest(name, (setup_key, recovery_key), f"policy:{name}")
    initialize_genesis(store, genesis)
    setup = candidate_runtime(
        store, genesis=genesis, actor_key=setup_key, signer=setup_signer
    )
    root = setup.accept_contract(
        Contract(
            id=f"task:{name}:root",
            name=f"{name}-root",
            outputs=("report",),
            budget=4,
        )
    )
    failed = setup.accept_contract(
        Contract(
            id=f"task:{name}:failed",
            name=f"{name}-failed",
            parent_id=root.id,
            outputs=("report",),
            budget=1,
        )
    )
    publisher = CandidatePublisher(store, store, genesis, recovery_key, recovery_signer)
    declaration_id = next(
        record.id
        for _revision, record in store.scan_runtime_records(
            record_type="contract.declared"
        )
        if record.payload["contract"]["id"] == failed.id
    )
    return store, setup, root, failed, publisher, declaration_id


def _published_generic_recovery(tmp_path, name="recovery-history"):
    store, setup, root, failed, publisher, declaration_id = _environment(tmp_path, name)
    recovery = schedule_projection_recovery(
        store,
        publisher,
        failed_contract=failed,
        candidate_raw="malformed output",
        rejections=[{"category": "fixture", "name": "report", "reject_reason": "bad"}],
        source_record_ids=(declaration_id,),
        include_disposition=True,
    )
    assert recovery is not None and recovery.contract is not None, recovery
    context_asset = store.get_assets_by_name(f"_fail_context_{failed.id}")[0]
    return store, failed, publisher, declaration_id, recovery.contract, context_asset


def _prove(snapshot, recovery, context_asset, failed, source_id):
    return prove_recovery_publication(
        snapshot,
        recovery=recovery,
        context_template=context_asset,
        effects=(
            contract_declaration_effect(recovery),
            asset_proposal_effect(context_asset),
        ),
        idempotency_key=f"projection-recovery:{failed.id}",
        causal_parents=(failed.id, source_id),
        source_record_ids=(source_id,),
    )


def test_generic_recovery_is_proven_from_one_actual_receipt_and_head(tmp_path):
    store, failed, publisher, source_id, recovery, context_asset = (
        _published_generic_recovery(tmp_path)
    )
    snapshot = capture_recovery_history(store, publisher)
    proof = _prove(snapshot, recovery, context_asset, failed, source_id)
    assert proof.status == "matched", proof.reason
    assert proof.contract == recovery
    assert proof.context_asset == context_asset
    assert proof.receipt_id is not None
    store.close()


@pytest.mark.parametrize("damage", ["receipt", "head", "asset_member"])
def test_recovery_history_rejects_missing_or_forged_publication_members(
    tmp_path, damage
):
    store, failed, publisher, source_id, recovery, context_asset = (
        _published_generic_recovery(tmp_path, f"recovery-history-{damage}")
    )
    snapshot = capture_recovery_history(store, publisher)
    rows = list(snapshot.context.runtime_records)
    declaration = next(
        row
        for row in rows
        if row.record_type == "contract.declared"
        and row.payload["contract"]["id"] == recovery.id
    )
    receipt = next(
        row
        for row in rows
        if row.record_type == "candidate.received"
        and row.payload["candidate_id"] == declaration.payload["candidate_id"]
    )
    head = next(
        row
        for row in rows
        if row.record_type == "candidate.committed"
        and row.payload["candidate_id"] == receipt.payload["candidate_id"]
    )
    if damage == "receipt":
        rows[rows.index(receipt)] = replace(
            receipt, causal_parents=(failed.id, "record:forged-source")
        )
    elif damage == "head":
        rows.remove(head)
    else:
        asset_member = next(
            row
            for row in rows
            if row.record_type == "asset.committed"
            and row.payload["asset"]["id"] == context_asset.id
        )
        rows.remove(asset_member)
    damaged = replace(
        snapshot,
        context=replace(snapshot.context, runtime_records=tuple(rows)),
    )

    proof = _prove(damaged, recovery, context_asset, failed, source_id)
    assert proof.status == "conflict", damage
    store.close()


def test_tool_recovery_without_authenticated_source_claim_publishes_nothing(tmp_path):
    from aigineering.plugins.delegation import TaskDelegationPlugin
    from aigineering.protocol.actions import WorkerAction

    store, setup, root, _failed, publisher, declaration_id = _environment(
        tmp_path, "missing-source-claim"
    )
    source = TaskDelegationPlugin().propose_claimed(
        root,
        WorkerAction(type="tool", payload={"name": "lookup", "args": {}}),
        allowance=root.budget,
    )
    source_contract = Contract(**source.effects[0].payload["contract"])
    before = store.get_runtime_revision()

    disposition = schedule_projection_recovery(
        store,
        publisher,
        failed_contract=source_contract,
        candidate_raw="tool did not run",
        rejections=[],
        source_record_ids=(declaration_id,),
        include_disposition=True,
    )

    assert disposition.contract is None
    assert disposition.reason == "source_proof_missing"
    assert store.get_runtime_revision() == before
    store.close()
