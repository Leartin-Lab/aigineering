"""Feature and generic-proof boundaries for accepted recovery replay."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from conftest import candidate_runtime

from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
)
from aigineering.core.domain import initialize_genesis
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.core.publication_history import (
    index_published_records,
    validate_published_member,
)
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace_manager import TraceManager
from aigineering.plugins.completion_projection import TaskCompletionContext
from aigineering.plugins.recovery import (
    schedule_method_result_recovery,
    schedule_projection_recovery,
)
from aigineering.plugins.recovery_history import (
    RecoveryHistorySnapshot,
    prove_recovery_publication,
)
from aigineering.plugins.task_semantics import system_asset
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    actor_rotation_effect,
    actor_revocation_effect,
)
from aigineering.protocol.types import Asset, Contract


@dataclass
class RecoveryEnvironment:
    store: SQLiteStore
    genesis: object
    setup: object
    root: Contract
    failed: Contract
    recovery_key: ActorKey
    recovery_signer: object
    publisher: CandidatePublisher
    publishers: CandidatePublisherRegistry
    context: TaskCompletionContext


@dataclass
class RecoveryIntent:
    env: RecoveryEnvironment
    recovery: Contract
    context_template: Asset
    effects: tuple
    idempotency_key: str
    causal_parents: tuple[str, ...]


def _environment(name: str = "history") -> RecoveryEnvironment:
    store = SQLiteStore(":memory:")
    setup_signer = Ed25519Signer()
    setup_key = ActorKey(
        f"test:recovery-{name}",
        "setup",
        setup_signer.kind,
        setup_signer.signer_id,
        (
            "actor.authorize",
            "actor.revoke",
            "actor.rotate",
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
            "worker.register",
        ),
    )
    recovery_signer = Ed25519Signer()
    recovery_key = ActorKey(
        f"plugin:recovery.publish.{name}",
        "recovery-v1",
        recovery_signer.kind,
        recovery_signer.signer_id,
        (
            "actor.revoke",
            "actor.rotate",
            "asset.publish",
            "asset.publish.protected",
            "contract.publish",
            "contract.publish.protected",
        ),
    )
    genesis = create_genesis_manifest(
        f"recovery-{name}", (setup_key, recovery_key), f"policy:recovery-{name}"
    )
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
    publishers = CandidatePublisherRegistry((("recovery.publish.v1", publisher),))
    context = TaskCompletionContext(store, TraceManager(store), publishers)
    return RecoveryEnvironment(
        store,
        genesis,
        setup,
        root,
        failed,
        recovery_key,
        recovery_signer,
        publisher,
        publishers,
        context,
    )


def _published_intent(name: str = "history") -> RecoveryIntent:
    env = _environment(name)
    recovery = schedule_projection_recovery(
        env.context,
        failed_contract=env.failed,
        candidate_raw="malformed output",
        rejections=[
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    )
    assert recovery is not None
    published_asset = env.store.get_assets_by_name(f"_fail_context_{env.failed.id}")[0]
    template = system_asset(
        name=published_asset.name,
        content=published_asset.content,
        created_by=published_asset.created_by,
        promptable=True,
    )
    return RecoveryIntent(
        env,
        recovery,
        template,
        (
            contract_declaration_effect(recovery),
            asset_proposal_effect(template),
        ),
        f"projection-recovery:{env.failed.id}",
        (env.failed.id,),
    )


def _snapshot(env: RecoveryEnvironment) -> RecoveryHistorySnapshot:
    return env.context.capture_recovery_history()


def _members(snapshot: RecoveryHistorySnapshot, intent: RecoveryIntent):
    rows = snapshot.context.runtime_records
    contract_member = next(
        row
        for row in rows
        if row.record_type == "contract.declared"
        and row.payload["contract"]["id"] == intent.recovery.id
    )
    receipt_id = contract_member.causal_parents[0]
    asset_member = next(
        row
        for row in rows
        if row.record_type == "asset.committed"
        and receipt_id in row.causal_parents
        and row.payload["asset"]["name"] == intent.context_template.name
    )
    return contract_member, asset_member


def _assert_generic_members(snapshot: RecoveryHistorySnapshot, intent: RecoveryIntent):
    rows = snapshot.context.runtime_records
    contract_member, asset_member = _members(snapshot, intent)
    candidate_id = contract_member.payload["candidate_id"]
    receipt, head = validate_published_member(
        rows,
        contract_member,
        expected_candidate=candidate_id,
        domain_id=snapshot.store_domain_id,
        record_index=index_published_records(rows),
    )
    asset_receipt, asset_head = validate_published_member(
        rows,
        asset_member,
        expected_candidate=candidate_id,
        expected_receipt=receipt.id,
        expected_head=head.id,
        domain_id=snapshot.store_domain_id,
    )
    assert asset_receipt.id == receipt.id
    assert asset_head.id == head.id


def _publish_alternative(intent: RecoveryIntent, *, variant: str):
    source = intent.env
    store = SQLiteStore(":memory:")
    initialize_genesis(store, source.genesis)
    setup = candidate_runtime(
        store,
        genesis=source.genesis,
        actor_key=source.setup.actor_key,
        signer=source.setup.signer,
    )
    root = setup.accept_contract(
        Contract(
            id=f"task:{variant}:root",
            name=source.root.name,
            outputs=("report",),
            budget=4,
        )
    )
    assert root.id == source.root.id
    failed = setup.accept_contract(
        Contract(
            id=f"task:{variant}:failed",
            name=source.failed.name,
            parent_id=source.failed.parent_id,
            outputs=("report",),
            budget=1,
        )
    )
    assert failed.id == source.failed.id
    publisher = CandidatePublisher(
        store,
        store,
        source.genesis,
        source.recovery_key,
        source.recovery_signer,
    )
    effects = intent.effects
    parents = intent.causal_parents
    idempotency_key = intent.idempotency_key
    if variant == "wrong_parents":
        parents = (store.scan_runtime_records(record_type="domain.genesis")[0][1].id,)
    elif variant == "wrong_effect_order":
        effects = tuple(reversed(effects))
    elif variant == "wrong_idempotency":
        idempotency_key += ":different"
    decision = publisher.publish(
        effects,
        idempotency_key=idempotency_key,
        causal_parents=parents,
    )
    assert decision.accepted
    context = TaskCompletionContext(
        store,
        TraceManager(store),
        CandidatePublisherRegistry((("recovery.publish.v1", publisher),)),
    )
    env = replace(
        source,
        store=store,
        setup=setup,
        failed=failed,
        publisher=publisher,
        context=context,
    )
    return env


@pytest.mark.parametrize(
    "variant", ["wrong_parents", "wrong_effect_order", "wrong_idempotency"]
)
def test_signed_alternative_recovery_intents_pass_generic_proof_then_conflict(variant):
    intent = _published_intent(f"intent-{variant}")
    env = _publish_alternative(intent, variant=variant)
    snapshot = _snapshot(env)
    _assert_generic_members(snapshot, intent)

    proof = prove_recovery_publication(
        snapshot,
        recovery=intent.recovery,
        context_template=intent.context_template,
        effects=intent.effects,
        idempotency_key=intent.idempotency_key,
        causal_parents=intent.causal_parents,
    )

    assert proof.status == "conflict"
    assert not any(
        record.record_type == "candidate_rejection.recovery_scheduled"
        for _, record in env.store.scan_runtime_records()
    )
    env.store.close()
    intent.env.store.close()


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_receipt",
        "missing_head",
        "duplicate_receipt",
        "duplicate_head",
        "duplicate_declaration",
        "missing_context_member",
        "duplicate_context_member",
    ],
)
def test_generic_publication_history_corruption_is_not_accepted_as_recovery(corruption):
    intent = _published_intent(f"generic-{corruption}")
    snapshot = _snapshot(intent.env)
    rows = list(snapshot.context.runtime_records)
    contract_member, asset_member = _members(snapshot, intent)
    receipt = next(
        row
        for row in rows
        if row.record_type == "candidate.received"
        and row.payload.get("candidate_id") == contract_member.payload["candidate_id"]
    )
    head = next(
        row
        for row in rows
        if row.record_type == "candidate.committed"
        and row.payload.get("candidate_id") == contract_member.payload["candidate_id"]
    )
    if corruption == "missing_receipt":
        rows.remove(receipt)
    elif corruption == "missing_head":
        rows.remove(head)
    elif corruption == "duplicate_receipt":
        rows.append(receipt)
    elif corruption == "duplicate_head":
        rows.append(head)
    elif corruption == "duplicate_declaration":
        rows.append(contract_member)
    elif corruption == "missing_context_member":
        rows.remove(asset_member)
    else:
        rows.append(asset_member)
    damaged = replace(
        snapshot, context=replace(snapshot.context, runtime_records=tuple(rows))
    )
    with pytest.raises((TypeError, ValueError)):
        if corruption == "missing_context_member":
            validate_published_member(
                damaged.context.runtime_records,
                contract_member,
                expected_candidate=contract_member.payload["candidate_id"],
                domain_id=damaged.store_domain_id,
                record_index=index_published_records(damaged.context.runtime_records),
            )
        else:
            _assert_generic_members(damaged, intent)

    proof = prove_recovery_publication(
        damaged,
        recovery=intent.recovery,
        context_template=intent.context_template,
        effects=intent.effects,
        idempotency_key=intent.idempotency_key,
        causal_parents=intent.causal_parents,
    )
    assert proof.status == "conflict"
    intent.env.store.close()


@pytest.mark.parametrize(
    "mismatch", ["missing_context", "duplicate_context", "contract", "asset"]
)
def test_recovery_materialization_must_match_valid_generic_history(mismatch):
    intent = _published_intent(f"materialized-{mismatch}")
    snapshot = _snapshot(intent.env)
    _assert_generic_members(snapshot, intent)
    if mismatch == "missing_context":
        context = replace(
            snapshot.context,
            assets=tuple(
                asset
                for asset in snapshot.context.assets
                if asset.name != intent.context_template.name
            ),
        )
    elif mismatch == "duplicate_context":
        matching = next(
            asset
            for asset in snapshot.context.assets
            if asset.name == intent.context_template.name
        )
        context = replace(snapshot.context, assets=(*snapshot.context.assets, matching))
    elif mismatch == "contract":
        context = replace(
            snapshot.context,
            contracts=tuple(
                replace(contract, name="changed")
                if contract.id == intent.recovery.id
                else contract
                for contract in snapshot.context.contracts
            ),
        )
    else:
        context = replace(
            snapshot.context,
            assets=tuple(
                replace(asset, content="changed")
                if asset.name == intent.context_template.name
                else asset
                for asset in snapshot.context.assets
            ),
        )
    damaged = replace(snapshot, context=context)
    proof = prove_recovery_publication(
        damaged,
        recovery=intent.recovery,
        context_template=intent.context_template,
        effects=intent.effects,
        idempotency_key=intent.idempotency_key,
        causal_parents=intent.causal_parents,
    )
    assert proof.status == "conflict"
    intent.env.store.close()


def test_exact_context_residue_with_missing_contract_is_conflict_and_not_republished(
    monkeypatch,
):
    intent = _published_intent("exact-context-residue")
    snapshot = _snapshot(intent.env)
    contract_member, asset_member = _members(snapshot, intent)
    damaged_rows = tuple(
        row for row in snapshot.context.runtime_records if row.id != contract_member.id
    )
    damaged_context = replace(
        snapshot.context,
        contracts=tuple(
            contract
            for contract in snapshot.context.contracts
            if contract.id != intent.recovery.id
        ),
        runtime_records=damaged_rows,
    )
    damaged = replace(snapshot, context=damaged_context)
    proof = prove_recovery_publication(
        damaged,
        recovery=intent.recovery,
        context_template=intent.context_template,
        effects=intent.effects,
        idempotency_key=intent.idempotency_key,
        causal_parents=intent.causal_parents,
    )
    assert proof.status == "conflict"
    assert asset_member.id in {row.id for row in damaged.context.runtime_records}
    assert any(
        asset.id == asset_member.payload["asset"]["id"]
        for asset in damaged.context.assets
    )

    receipts_before = len(
        [
            record
            for _, record in intent.env.store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("actor_id") == intent.env.recovery_key.actor_id
        ]
    )
    monkeypatch.setattr(intent.env.context, "capture_recovery_history", lambda: damaged)
    result = schedule_projection_recovery(
        intent.env.context,
        failed_contract=intent.env.failed,
        candidate_raw="malformed output",
        rejections=[
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    )
    receipts_after = len(
        [
            record
            for _, record in intent.env.store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("actor_id") == intent.env.recovery_key.actor_id
        ]
    )
    assert result is None
    assert receipts_after == receipts_before
    intent.env.store.close()


def test_foreign_same_name_context_does_not_block_fresh_recovery_or_exact_replay():
    env = _environment("foreign-context")
    context_name = f"_fail_context_{env.failed.id}"
    foreign_context = env.setup.accept_asset(
        system_asset(
            name=context_name,
            content="preemptive context from another publication",
            created_by=env.failed.id,
            promptable=True,
        )
    )
    env.setup.accept_contract(
        Contract(
            id="task:foreign-preemptive-recovery",
            name="preemptive recovery candidate",
            inputs=(context_name,),
            outputs=("report",),
            budget=1,
            origin="recovery",
        )
    )
    args = {
        "runtime": env.context,
        "failed_contract": env.failed,
        "candidate_raw": "malformed output",
        "rejections": [
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    }

    first = schedule_projection_recovery(**args)
    assert first is not None
    recovery_assets = [
        asset
        for asset in env.store.get_assets_by_name(context_name)
        if asset.signed_by == env.recovery_key.actor_id
    ]
    assert len(recovery_assets) == 1
    assert recovery_assets[0].id != foreign_context.id

    snapshot = _snapshot(env)
    intent = RecoveryIntent(
        env,
        first,
        system_asset(
            name=recovery_assets[0].name,
            content=recovery_assets[0].content,
            created_by=recovery_assets[0].created_by,
            promptable=recovery_assets[0].promptable,
        ),
        (
            contract_declaration_effect(first),
            asset_proposal_effect(
                system_asset(
                    name=recovery_assets[0].name,
                    content=recovery_assets[0].content,
                    created_by=recovery_assets[0].created_by,
                    promptable=recovery_assets[0].promptable,
                )
            ),
        ),
        f"projection-recovery:{env.failed.id}",
        (env.failed.id,),
    )
    _assert_generic_members(snapshot, intent)
    proof = prove_recovery_publication(
        snapshot,
        recovery=first,
        context_template=intent.context_template,
        effects=intent.effects,
        idempotency_key=intent.idempotency_key,
        causal_parents=intent.causal_parents,
    )
    assert proof.status == "matched"
    assert proof.context_asset is not None
    assert proof.context_asset.id == recovery_assets[0].id
    assert proof.context_asset.id != foreign_context.id

    receipts_before = len(
        [
            record
            for _, record in env.store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("actor_id") == env.recovery_key.actor_id
        ]
    )
    second = schedule_projection_recovery(**args)
    receipts_after = len(
        [
            record
            for _, record in env.store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("actor_id") == env.recovery_key.actor_id
        ]
    )
    assert second == first
    assert receipts_after == receipts_before
    env.store.close()


def test_same_actor_rotation_recognizes_old_accepted_publication_without_resigning():
    intent = _published_intent("rotation")
    env = intent.env
    replacement_signer = Ed25519Signer()
    replacement_key = ActorKey(
        env.recovery_key.actor_id,
        "recovery-v2",
        replacement_signer.kind,
        replacement_signer.signer_id,
        env.recovery_key.capabilities,
    )
    rotation = env.publisher.publish(
        (
            actor_rotation_effect(
                env.recovery_key.key_id, replacement_key, "fixture rotation"
            ),
        ),
        idempotency_key="rotate:recovery-history",
    )
    assert rotation.accepted
    rotated_publisher = CandidatePublisher(
        env.store, env.store, env.genesis, replacement_key, replacement_signer
    )
    rotated_context = TaskCompletionContext(
        env.store,
        TraceManager(env.store),
        CandidatePublisherRegistry((("recovery.publish.v1", rotated_publisher),)),
    )
    before = len(
        env.store.scan_runtime_records(record_type="candidate.authentication_rejected")
    )
    result = schedule_projection_recovery(
        rotated_context,
        failed_contract=env.failed,
        candidate_raw="malformed output",
        rejections=[
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    )
    assert result == intent.recovery
    assert (
        len(
            [
                record
                for _, record in env.store.scan_runtime_records(
                    record_type="candidate.received"
                )
                if record.payload.get("actor_id") == env.recovery_key.actor_id
            ]
        )
        == 2
    )  # original publication plus the explicit rotation
    assert (
        len(
            env.store.scan_runtime_records(
                record_type="candidate.authentication_rejected"
            )
        )
        == before
    )
    env.store.close()


def test_revoked_recovery_key_before_first_acceptance_remains_unavailable():
    env = _environment("revoked-before")
    revocation = env.publisher.publish(
        (
            actor_revocation_effect(
                env.recovery_key.actor_id, env.recovery_key.key_id, "fixture revoke"
            ),
        ),
        idempotency_key="revoke:before-recovery",
    )
    assert revocation.accepted
    result = schedule_projection_recovery(
        env.context,
        failed_contract=env.failed,
        candidate_raw="malformed output",
        rejections=[
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    )
    assert result is None
    assert not any(
        contract.origin == "recovery" for contract in env.store.get_all_contracts()
    )
    assert env.store.scan_runtime_records(
        record_type="candidate.authentication_rejected"
    )
    env.store.close()


def test_method_result_recovery_replays_the_same_result_asset_parent():
    env = _environment("method-result")
    result_asset = env.setup.accept_asset(
        system_asset(
            name=f"_plan_result_{env.failed.parent_id}",
            content="malformed result",
            created_by=env.failed.id,
            promptable=True,
        )
    )
    args = {
        "runtime": env.context,
        "method_type": "plan",
        "parent_id": env.failed.parent_id,
        "failed_contract": env.failed,
        "result_asset": result_asset,
        "rejections": [
            {"field": "contracts", "reason": "invalid", "recoverable": True}
        ],
    }
    first = schedule_method_result_recovery(**args)
    before = len(
        [
            record
            for _, record in env.store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("actor_id") == env.recovery_key.actor_id
        ]
    )
    second = schedule_method_result_recovery(**args)
    assert first is not None
    assert second == first
    assert first.outputs == (f"_plan_result_{env.failed.parent_id}",)
    assert (
        len(
            [
                record
                for _, record in env.store.scan_runtime_records(
                    record_type="candidate.received"
                )
                if record.payload.get("actor_id") == env.recovery_key.actor_id
            ]
        )
        == before
    )
    env.store.close()


def test_recovery_capture_retries_revision_change_and_fails_closed_if_unstable(
    monkeypatch,
):
    intent = _published_intent("capture")
    store = intent.env.store
    original = store.get_runtime_revision
    revision = original()
    values = iter((revision, revision + 1, revision + 2, revision + 2))
    monkeypatch.setattr(store, "get_runtime_revision", lambda: next(values))
    snapshot = intent.env.context.capture_recovery_history()
    assert snapshot.revision == revision + 2

    values = iter((1, 2, 3, 4, 5, 6))
    monkeypatch.setattr(store, "get_runtime_revision", lambda: next(values))
    receipts_before = len(store.scan_runtime_records(record_type="candidate.received"))
    result = schedule_projection_recovery(
        intent.env.context,
        failed_contract=intent.env.failed,
        candidate_raw="malformed output",
        rejections=[
            {"category": "fixture", "name": "report", "reject_reason": "invalid"}
        ],
    )
    assert result is None
    assert (
        len(store.scan_runtime_records(record_type="candidate.received"))
        == receipts_before
    )
    assert not store.scan_runtime_records(record_type="recovery_scheduled")
    monkeypatch.setattr(store, "get_runtime_revision", original)
    intent.env.store.close()


def test_store_read_failure_propagates_and_leaves_recovery_unresolved(monkeypatch):
    intent = _published_intent("read-error")
    store = intent.env.store
    before = len(store.scan_runtime_records())
    monkeypatch.setattr(
        store,
        "get_runtime_revision",
        lambda: (_ for _ in ()).throw(OSError("fixture Store read failure")),
    )
    with pytest.raises(OSError, match="fixture Store read failure"):
        schedule_projection_recovery(
            intent.env.context,
            failed_contract=intent.env.failed,
            candidate_raw="malformed output",
            rejections=[
                {"category": "fixture", "name": "report", "reject_reason": "invalid"}
            ],
        )
    assert len(store.scan_runtime_records()) == before
    assert not store.scan_runtime_records(
        record_type="worker_failure.recovery_unavailable"
    )
    intent.env.store.close()
