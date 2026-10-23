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
    _history_status_disposition,
    schedule_method_result_recovery,
    schedule_projection_recovery,
)
from aigineering.plugins.recovery_history import (
    RecoveryHistorySnapshot,
    prove_recovery_publication,
)
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    actor_rotation_effect,
    actor_revocation_effect,
)
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.actions import WorkerAction
from aigineering.plugins.task_semantics import (
    method_contract,
    method_payload,
    method_context_content,
    system_asset,
)


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


@pytest.mark.parametrize(
    ("legacy", "current", "expected"),
    [
        ("conflict", "matched", "recovery_history_conflict"),
        ("matched", "conflict", "recovery_history_conflict"),
        ("matched", "matched", "recovery_history_ambiguous"),
        ("matched", "absent", "legacy_method_intent_missing"),
        ("absent", "matched", ""),
        ("absent", "absent", ""),
    ],
)
def test_dual_recovery_history_status_matrix(legacy, current, expected):
    assert _history_status_disposition(legacy, current) == expected


def _environment(
    name: str = "history", *, store_path=":memory:"
) -> RecoveryEnvironment:
    store = SQLiteStore(store_path)
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
        "plugin:recovery.publish.v1",
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


def test_signed_v6_recovery_publication_matches_exact_history():
    from aigineering.core.ids import contract_from_fields
    from aigineering.protocol.wire import contract_to_dict

    env = _environment("signed-v6-recovery-history")
    dataset = env.setup.accept_asset(
        system_asset("dataset", "pinned recovery input", created_by="fixture")
    )
    recovery = contract_from_fields(
        name="signed-v6-recovery-history.recover",
        description='{"method":"tool","tool_name":"probe_tool"}',
        inputs=("dataset",),
        outputs=("report",),
        activation="dataset",
        budget=1,
        tool_scope=("probe_tool",),
        labels=(),
        origin="system",
        parent_id=env.root.id,
        minting_authority=("report",),
        sensitive_input_policy={"input_asset_bindings": {"dataset": [dataset.id]}},
    )
    context_template = system_asset(
        name="_fail_context_signed-v6-recovery-history",
        content='{"trigger":"verified_task_failure"}',
        created_by=env.failed.id,
        promptable=True,
    )
    effects = (
        contract_declaration_effect(recovery),
        asset_proposal_effect(context_template),
    )
    key = "projection-recovery:signed-v6-recovery-history"
    decision = env.publisher.publish(
        effects,
        idempotency_key=key,
        causal_parents=(env.failed.id,),
    )
    assert decision.accepted

    snapshot = _snapshot(env)
    declaration = next(
        record
        for record in snapshot.context.runtime_records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == recovery.id
    )
    frozen_binding = declaration.payload["contract"]["sensitive_input_policy"][
        "input_asset_bindings"
    ]["dataset"]
    wire_binding = contract_to_dict(recovery)["sensitive_input_policy"][
        "input_asset_bindings"
    ]["dataset"]
    assert isinstance(frozen_binding, tuple)
    assert isinstance(wire_binding, list)
    assert tuple(frozen_binding) == tuple(wire_binding)
    proof = prove_recovery_publication(
        snapshot,
        recovery=recovery,
        context_template=context_template,
        effects=effects,
        idempotency_key=key,
        causal_parents=(env.failed.id,),
    )

    assert recovery.id.startswith("task:v6:")
    assert proof.status == "matched", proof.reason
    assert proof.contract == recovery
    env.store.close()


def test_signed_v6_system_tool_recovery_reuses_exact_history_proof(monkeypatch):
    from aigineering.agent.worker import WorkerExecutionError
    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.core.ids import contract_from_fields
    from aigineering.plugins.recovery import (
        MethodRecoveryIntent,
        _recovery_effects,
        prepare_method_recovery_intent,
    )
    from aigineering.runtime import (
        WorkerInvocationError,
        claim_next_package,
        execute_claimed_package,
    )
    from aigineering.task_diagnostics import task_recovery_source_binding
    from conftest import hosted_worker
    import aigineering.runtime_services as runtime_services_module

    monkeypatch.setattr(
        runtime_services_module, "sync_runtime", lambda *args, **kwargs: None
    )

    class FailingToolWorker:
        worker_id = "worker:signed-v6-recovery-history"

        def registration(self):
            from aigineering.core.worker_routing import WorkerRegistration

            return WorkerRegistration(
                self.worker_id, capabilities=("tool-execution", "tool:probe_tool")
            )

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise WorkerExecutionError("fixture_tool_failure", "safe fixture failure")

    env = _environment("signed-v6-system-tool-recovery")
    dataset = env.setup.accept_asset(
        system_asset("dataset", "pinned tool recovery input", created_by="fixture")
    )
    descriptor = env.setup.accept_asset(
        create_tool_descriptor("probe_tool", "fixture probe", {"type": "object"})
    )
    parent = env.setup.accept_contract(
        contract_from_fields(
            name="signed-v6-system-tool-parent",
            description="Use the pinned dataset.",
            inputs=("dataset", "_tool_capability_probe_tool"),
            outputs=("report",),
            activation="dataset",
            budget=3,
            tool_scope=("probe_tool",),
            labels=(),
            origin="human",
            parent_id=env.root.id,
            minting_authority=("report",),
            sensitive_input_policy={
                "input_asset_bindings": {
                    "dataset": [dataset.id],
                    "_tool_capability_probe_tool": [descriptor.id],
                }
            },
        )
    )
    action = WorkerAction(
        type="tool", payload={"name": "probe_tool", "args": {"sample": 7}}
    )
    source_template = method_contract(parent, action)
    method_context = system_asset(
        name=f"_method_ctx_{parent.id}",
        content=method_context_content(parent, action, source_template),
        created_by=parent.id,
        promptable=True,
    )
    source_decision = env.setup.publisher.publish(
        (
            contract_declaration_effect(source_template),
            asset_proposal_effect(method_context),
        ),
        idempotency_key="signed-v6-system-tool-source",
    )
    assert source_decision.accepted and source_decision.contract is not None
    source = source_decision.contract
    assert source.id.startswith("task:v6:")
    host = hosted_worker(
        env.store,
        FailingToolWorker(),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claimed = claim_next_package(
        env.store,
        worker_id=host.worker_id,
        contract_id=source.id,
        candidate_publishers=env.publishers,
        claim_candidate=host.claim_candidate(
            request_id="claim:signed-v6-system-tool-recovery", contract_id=source.id
        ),
    )
    assert claimed is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(
            claimed, host, env.store, candidate_publishers=env.publishers
        )

    snapshot = _snapshot(env)
    failure = next(
        record
        for _, record in env.store.scan_runtime_records(
            record_type="worker.invocation_failed"
        )
        if record.payload.get("contract_id") == source.id
    )
    binding = task_recovery_source_binding(
        env.store,
        source.id,
        claimed.package.claim_id,
        failure.id,
        "worker.invocation_failed",
        query_context=snapshot.query_context,
    )
    intent = prepare_method_recovery_intent(
        snapshot, failed_contract=source, source_binding=binding
    )
    assert isinstance(intent, MethodRecoveryIntent)
    proof = prove_recovery_publication(
        snapshot,
        recovery=intent.recovery,
        context_template=intent.context_asset,
        effects=_recovery_effects(intent.recovery, intent.context_asset),
        idempotency_key=intent.idempotency_key,
        causal_parents=intent.causal_parents,
        source_record_ids=intent.source_record_ids,
    )
    assert intent.recovery.id.startswith("task:v6:")
    assert proof.status == "matched", proof.reason
    assert proof.contract == intent.recovery
    assert proof.contract.sensitive_input_policy == source.sensitive_input_policy
    env.store.close()


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
    values = iter(
        (
            revision,
            revision,
            revision,
            revision + 1,
            revision + 2,
            revision + 2,
            revision + 2,
            revision + 2,
        )
    )
    monkeypatch.setattr(store, "get_runtime_revision", lambda: next(values))
    snapshot = intent.env.context.capture_recovery_history()
    assert snapshot.revision == revision + 2

    values = iter((1, 1, 1, 2, 3, 3, 3, 4, 5, 5, 5, 6))
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


def test_signed_tool_worker_failure_publishes_method_preserving_recovery(
    monkeypatch, tmp_path
):
    from aigineering.agent.worker import WorkerExecutionError
    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.runtime import (
        WorkerInvocationError,
        claim_next_package,
        execute_claimed_package,
        process_worker_failures,
    )
    import aigineering.runtime as runtime_module
    from conftest import hosted_worker
    import aigineering.runtime_services as runtime_services_module

    monkeypatch.setattr(
        runtime_services_module, "sync_runtime", lambda *args, **kwargs: None
    )

    class FailingToolWorker:
        worker_id = "worker:tool-method-recovery-failure"

        def registration(self):
            from aigineering.core.worker_routing import WorkerRegistration

            return WorkerRegistration(
                self.worker_id, capabilities=("tool-execution", "tool:probe_tool")
            )

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise WorkerExecutionError("fixture_tool_failure", "safe fixture failure")

    source_path = tmp_path / "signed-tool-method.sqlite3"
    env = _environment("signed-tool-method-intent", store_path=source_path)
    env.setup.accept_asset(
        create_tool_descriptor("probe_tool", "fixture probe", {"type": "object"})
    )
    parent = env.setup.accept_contract(
        Contract(
            id="task:tool-method-parent",
            name="tool-method-parent",
            outputs=("report",),
            budget=4,
            tool_scope=("probe_tool",),
        )
    )
    action = WorkerAction(
        type="tool", payload={"name": "probe_tool", "args": {"sample": 7}}
    )
    source_template = method_contract(parent, action)
    method_context = system_asset(
        name=f"_method_ctx_{parent.id}",
        content=method_context_content(parent, action, source_template),
        created_by=parent.id,
        promptable=True,
    )
    source_decision = env.setup.publisher.publish(
        (
            contract_declaration_effect(source_template),
            asset_proposal_effect(method_context),
        ),
        idempotency_key="signed-tool-method-source",
    )
    assert source_decision.accepted and source_decision.contract is not None
    source = source_decision.contract
    host = hosted_worker(
        env.store,
        FailingToolWorker(),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claimed = claim_next_package(
        env.store,
        worker_id=host.worker_id,
        contract_id=source.id,
        candidate_publishers=env.publishers,
        claim_candidate=host.claim_candidate(
            request_id="claim:signed-tool-method-intent", contract_id=source.id
        ),
    )
    assert claimed is not None
    original_commit_outcome = runtime_module._commit_recovery_outcome

    def interrupted_outcome(*args, **kwargs):
        raise RuntimeError("fixture interruption after recovery publication")

    monkeypatch.setattr(runtime_module, "_commit_recovery_outcome", interrupted_outcome)
    with pytest.raises(RuntimeError, match="fixture interruption"):
        execute_claimed_package(
            claimed, host, env.store, candidate_publishers=env.publishers
        )
    monkeypatch.setattr(
        runtime_module, "_commit_recovery_outcome", original_commit_outcome
    )
    env.setup.accept_asset(
        system_asset(
            name="unrelated_replay_witness",
            content="unrelated signed fact",
            created_by="fixture",
            promptable=False,
        )
    )
    assert process_worker_failures(env.store, candidate_publishers=env.publishers) == [
        source.id
    ]

    source_failures = [
        record
        for _, record in env.store.scan_runtime_records(
            record_type="worker.invocation_failed"
        )
        if record.payload.get("contract_id") == source.id
    ]
    assert len(source_failures) == 1
    assert source_failures[0].payload["claim_id"] == claimed.package.claim_id
    assert source_failures[0].payload["package_id"] == claimed.package.package_id
    source_terminals = [
        record
        for _, record in env.store.scan_runtime_records(
            record_type="lifecycle.terminal"
        )
        if record.payload.get("contract_id") == source.id
    ]
    assert len(source_terminals) == 1
    assert source_terminals[0].causal_parents == (source_failures[0].id,)

    recoveries = [
        contract
        for contract in env.store.get_all_contracts()
        if contract.name == f"{source.name}.recover"
    ]
    assert len(recoveries) == 1, [
        (record.record_type, dict(record.payload))
        for _, record in env.store.scan_runtime_records()
        if "recovery_" in record.record_type
    ]
    recovery = recoveries[0]
    payload = method_payload(recovery)
    assert payload.get("method") == "tool"
    assert payload.get("payload") == {"name": "probe_tool", "args": {"sample": 7}}
    assert payload.get("recovery_intent_version") == 1
    assert payload.get("recovery_source_contract_id") == source.id
    assert recovery.origin == "system"
    assert recovery.parent_id == source.parent_id
    assert recovery.inputs == source.inputs
    assert recovery.activation == source.activation
    assert recovery.outputs == source.outputs
    assert recovery.tool_scope == source.tool_scope
    assert recovery.worker_capabilities == source.worker_capabilities
    assert recovery.sensitive_input_policy == source.sensitive_input_policy
    assert recovery.acceptance_policy == source.acceptance_policy
    assert recovery.context_asset_ids == source.context_asset_ids
    foreign_recursive_context = env.setup.accept_asset(
        system_asset(
            name=f"_fail_context_{source.id}.recovery",
            content="foreign same-name historical context",
            created_by=source.id,
            promptable=True,
        )
    )
    accepted_recursive_contexts = [
        asset
        for asset in env.store.get_assets_by_name(foreign_recursive_context.name)
        if asset.signed_by == env.recovery_key.actor_id
    ]
    assert len(accepted_recursive_contexts) == 1
    assert accepted_recursive_contexts[0].id != foreign_recursive_context.id

    failed_recovery_host = host
    failed_recovery_claim = claim_next_package(
        env.store,
        worker_id=failed_recovery_host.worker_id,
        contract_id=recovery.id,
        candidate_publishers=env.publishers,
        claim_candidate=failed_recovery_host.claim_candidate(
            request_id="claim:recovered-tool-method-fails-once",
            contract_id=recovery.id,
        ),
    )
    assert failed_recovery_claim is not None
    with pytest.raises(WorkerInvocationError, match="recovery was evaluated"):
        execute_claimed_package(
            failed_recovery_claim,
            failed_recovery_host,
            env.store,
            candidate_publishers=env.publishers,
        )
    assert process_worker_failures(env.store, candidate_publishers=env.publishers) in (
        [],
        [recovery.id],
    )
    recursive_recovery = next(
        (
            item
            for item in env.store.get_all_contracts()
            if item.name == f"{recovery.name}.recover"
        ),
        None,
    )
    assert recursive_recovery is not None, [
        (record.record_type, dict(record.payload))
        for _, record in env.store.scan_runtime_records()
        if record.payload.get("contract_id") == recovery.id
        and "recovery_" in record.record_type
    ]
    assert (
        method_payload(recursive_recovery)["recovery_source_contract_id"] == recovery.id
    )

    from aigineering.agent.tool_worker import ToolWorker
    from aigineering.core.tools import ToolRegistry, ToolSpec

    observed_args = []
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="probe_tool", input_schema={"type": "object"}),
        lambda args: observed_args.append(args) or "fixture result",
    )
    descriptor = next(
        asset
        for asset in env.store.get_all_assets()
        if asset.name == "_tool_capability_probe_tool"
    )
    assert descriptor is not None
    tool_worker = ToolWorker(registry, worker_id="worker:tool-method-recovery")
    tool_host = hosted_worker(
        env.store,
        tool_worker,
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    recovered_claim = claim_next_package(
        env.store,
        worker_id=tool_host.worker_id,
        contract_id=recursive_recovery.id,
        candidate_publishers=env.publishers,
        claim_candidate=tool_host.claim_candidate(
            request_id="claim:recovered-tool-method", contract_id=recursive_recovery.id
        ),
    )
    assert recovered_claim is not None
    result = execute_claimed_package(
        recovered_claim,
        tool_host,
        env.store,
        candidate_publishers=env.publishers,
    )
    assert result["status"] == "accepted"
    assert observed_args == [{"sample": 7}]
    from aigineering.application import default_completion_registry
    from aigineering.runtime import process_task_completions

    completion_publishers = CandidatePublisherRegistry(
        (("continuation.publish.v1", env.publisher),)
    )
    assert process_task_completions(
        env.store,
        default_completion_registry(),
        candidate_publishers=completion_publishers,
    ) == [recursive_recovery.id]
    continuation = next(
        item
        for item in env.store.get_all_contracts()
        if item.parent_id == parent.id and item.origin == "continuation"
    )
    from aigineering.agent.mock import MockWorker

    final_worker = MockWorker(worker_id="worker:recovered-method-final")
    final_worker.set_output(
        continuation.name,
        '/exec {"outputs":{"report":"healthy business result"}}',
    )
    final_host = hosted_worker(
        env.store,
        final_worker,
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    final_claim = claim_next_package(
        env.store,
        worker_id=final_host.worker_id,
        contract_id=continuation.id,
        candidate_publishers=env.publishers,
        claim_candidate=final_host.claim_candidate(
            request_id="claim:recovered-method-final", contract_id=continuation.id
        ),
    )
    assert final_claim is not None
    final_result = execute_claimed_package(
        final_claim,
        final_host,
        env.store,
        candidate_publishers=env.publishers,
    )
    assert final_result["status"] == "accepted"
    from aigineering.cli.task_state import project_task_status

    assert project_task_status(parent, env.store)["status"] == "completed"
    from aigineering.task_lineage import task_lineage

    lineage = task_lineage(env.store, recursive_recovery.id)["data"]
    assert any(
        edge.get("kind") == "recovery_publication"
        and edge.get("source_contract_id") == recovery.id
        and edge.get("target_contract_id") == recursive_recovery.id
        for edge in lineage["edges"]
    ), lineage["issues"]
    before_digest = env.store.runtime_materialization_digest()
    env.store.close()
    import shutil

    rebuilt_path = tmp_path / "signed-tool-method-rebuilt.sqlite3"
    shutil.copy2(source_path, rebuilt_path)
    reopened = SQLiteStore(rebuilt_path)
    assert reopened.runtime_materialization_digest() == before_digest
    assert reopened.rebuild_runtime_materializations(strict=True) == before_digest
    assert (
        len(
            [
                item
                for item in reopened.get_all_contracts()
                if item.name
                in {f"{source.name}.recover", f"{source.name}.recover.recover"}
            ]
        )
        == 2
    )
    assert any(
        edge.get("kind") == "recovery_publication"
        and edge.get("target_contract_id") == recursive_recovery.id
        for edge in task_lineage(reopened, recursive_recovery.id)["data"]["edges"]
    )
    reopened.close()


@pytest.mark.parametrize(
    ("version", "source_id", "expected_reason"),
    [
        (77, "task:forged-source", "unsupported_method_intent"),
        (1, "task:forged-source", "method_source_history_unverified"),
    ],
)
def test_authenticated_unknown_method_marker_is_observable_without_publication(
    monkeypatch, version, source_id, expected_reason
):
    import json

    from aigineering.agent.worker import WorkerExecutionError
    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.core.ids import contract_from_fields
    from aigineering.plugins.task_semantics import method_context_content, system_asset
    from aigineering.runtime import (
        WorkerInvocationError,
        claim_next_package,
        execute_claimed_package,
    )
    from conftest import hosted_worker
    import aigineering.runtime_services as runtime_services_module

    monkeypatch.setattr(
        runtime_services_module, "sync_runtime", lambda *args, **kwargs: None
    )

    class FailingWorker:
        worker_id = "worker:unknown-method-marker"

        def registration(self):
            from aigineering.core.worker_routing import WorkerRegistration

            return WorkerRegistration(
                self.worker_id, capabilities=("tool-execution", "tool:probe_tool")
            )

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise WorkerExecutionError("fixture_failure", "safe fixture")

    env = _environment("unknown-method-marker")
    env.setup.accept_asset(
        create_tool_descriptor("probe_tool", "fixture probe", {"type": "object"})
    )
    parent = env.setup.accept_contract(
        Contract(
            id="task:unknown-method-marker-parent",
            name="unknown-method-marker-parent",
            outputs=("report",),
            budget=4,
            tool_scope=("probe_tool",),
        )
    )
    action = WorkerAction(type="tool", payload={"name": "probe_tool", "args": {}})
    template = method_contract(parent, action)
    payload = method_payload(template)
    payload["recovery_intent_version"] = version
    payload["recovery_source_contract_id"] = source_id
    source = contract_from_fields(
        name=template.name,
        description=json.dumps(payload, sort_keys=True),
        inputs=template.inputs,
        outputs=template.outputs,
        activation=template.activation,
        budget=template.budget,
        tool_scope=template.tool_scope,
        labels=template.labels,
        worker_capabilities=template.worker_capabilities,
        worker_pools=template.worker_pools,
        delegation_capabilities=template.delegation_capabilities,
        delegation_pools=template.delegation_pools,
        origin="system",
        parent_id=template.parent_id,
        minting_authority=template.minting_authority,
        sensitive_input_policy=template.sensitive_input_policy,
        acceptance_policy=template.acceptance_policy,
        context_asset_ids=template.context_asset_ids,
    )
    context = system_asset(
        name=f"_method_ctx_{parent.id}",
        content=method_context_content(parent, action, template),
        created_by=parent.id,
        promptable=True,
    )
    published = env.setup.publisher.publish(
        (contract_declaration_effect(source), asset_proposal_effect(context)),
        idempotency_key="unknown-method-marker-source",
    )
    assert published.accepted and published.contract is not None
    source = published.contract
    host = hosted_worker(
        env.store,
        FailingWorker(),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claimed = claim_next_package(
        env.store,
        worker_id=host.worker_id,
        contract_id=source.id,
        candidate_publishers=env.publishers,
        claim_candidate=host.claim_candidate(
            request_id="claim:unknown-method-marker", contract_id=source.id
        ),
    )
    assert claimed is not None
    with pytest.raises(WorkerInvocationError, match="recovery was evaluated"):
        execute_claimed_package(
            claimed, host, env.store, candidate_publishers=env.publishers
        )
    outcomes = [
        record
        for _, record in env.store.scan_runtime_records(
            record_type="worker_failure.recovery_unavailable"
        )
        if record.payload.get("contract_id") == source.id
    ]
    assert len(outcomes) == 1
    assert outcomes[0].payload["reason"] == expected_reason
    assert not any(
        item.name.startswith(f"{source.name}.recover")
        for item in env.store.get_all_contracts()
    )
    env.store.close()


def test_unverified_method_failure_header_does_not_write_recovery_outcome():
    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.runtime import process_worker_failures
    from aigineering.protocol.runtime_record import create_runtime_record
    from aigineering.task_diagnostics import TaskDiagnosticError

    env = _environment("unverified-method-failure")
    env.setup.accept_asset(
        create_tool_descriptor("probe_tool", "fixture probe", {"type": "object"})
    )
    parent = env.setup.accept_contract(
        Contract(
            id="task:unverified-method-failure-parent",
            name="unverified-method-failure-parent",
            outputs=("report",),
            budget=4,
            tool_scope=("probe_tool",),
        )
    )
    source_template = method_contract(
        parent, WorkerAction(type="tool", payload={"name": "probe_tool", "args": {}})
    )
    source = env.setup.accept_contract(source_template)
    failure = create_runtime_record(
        "worker.invocation_failed",
        {
            "contract_id": source.id,
            "claim_id": "claim:unverified",
            "package_id": "package:unverified",
            "worker_id": "worker:unverified",
            "category": "fixture_failure",
            "retryable": True,
            "status_code": 500,
        },
    )
    env.store.append_runtime_record(failure)
    before = tuple(env.store.scan_runtime_records())
    with pytest.raises(TaskDiagnosticError, match="claim"):
        process_worker_failures(env.store, candidate_publishers=env.publishers)
    assert tuple(env.store.scan_runtime_records()) == before
    assert not env.store.scan_runtime_records(
        record_type="worker_failure.recovery_unavailable"
    )
    assert not env.store.scan_runtime_records(record_type="lifecycle.terminal")
    assert not any(
        item.name.startswith(f"{source.id}.recover")
        for item in env.store.get_all_contracts()
    )
    env.store.close()


def test_signed_claimed_source_rejects_noncanonical_two_level_marker(monkeypatch):
    import json

    from aigineering.agent.worker import WorkerExecutionError
    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.core.ids import contract_from_fields
    from aigineering.plugins.delegation import _claimed_method_contract
    from aigineering.protocol.wire import contract_to_dict
    from aigineering.runtime import (
        WorkerInvocationError,
        claim_next_package,
        execute_claimed_package,
        process_worker_failures,
    )
    from conftest import hosted_worker
    import aigineering.runtime_services as runtime_services_module

    monkeypatch.setattr(
        runtime_services_module, "sync_runtime", lambda *args, **kwargs: None
    )

    class LocalFailure:
        worker_id = "worker:claimed-source-recovery-control"

        def registration(self):
            from aigineering.core.worker_routing import WorkerRegistration

            return WorkerRegistration(
                self.worker_id,
                capabilities=("tool-execution", "tool:probe_tool"),
            )

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise WorkerExecutionError("local_fixture_failure", "fixture failure")

    env = _environment("claimed-source-two-level-marker")
    env.setup.accept_asset(
        create_tool_descriptor("probe_tool", "fixture probe", {"type": "object"})
    )
    parent = env.setup.accept_contract(
        Contract(
            id="task:claimed-source-two-level-parent",
            name="claimed-source-two-level-parent",
            outputs=("report",),
            budget=8,
            tool_scope=("probe_tool",),
        )
    )
    source_template = _claimed_method_contract(
        parent, WorkerAction(type="tool", payload={"name": "probe_tool", "args": {}})
    )
    source_decision = env.setup.publisher.publish(
        (contract_declaration_effect(source_template),),
        idempotency_key="claimed-source-two-level-source",
    )
    assert source_decision.accepted and source_decision.contract is not None
    source = source_decision.contract
    host = hosted_worker(
        env.store,
        LocalFailure(),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )

    def fail(contract, request_id):
        claimed = claim_next_package(
            env.store,
            worker_id=host.worker_id,
            contract_id=contract.id,
            candidate_publishers=env.publishers,
            claim_candidate=host.claim_candidate(
                request_id=request_id, contract_id=contract.id
            ),
        )
        assert claimed is not None
        with pytest.raises(WorkerInvocationError):
            execute_claimed_package(
                claimed, host, env.store, candidate_publishers=env.publishers
            )
        process_worker_failures(env.store, candidate_publishers=env.publishers)

    fail(source, "claimed-source-two-level-source-failure")
    first = next(
        contract
        for contract in env.store.get_all_contracts()
        if method_payload(contract).get("recovery_source_contract_id") == source.id
    )
    first_payload = method_payload(first)
    first_payload["recovery_source_contract_id"] = first.id
    fields = contract_to_dict(first)
    fields.pop("id")
    fields["name"] = "operator-marker-task-not-original-recovery"
    fields["description"] = json.dumps(first_payload, sort_keys=True)
    forged = contract_from_fields(**fields)
    forged_decision = env.setup.publisher.publish(
        (contract_declaration_effect(forged),),
        idempotency_key="claimed-source-two-level-forged-marker",
    )
    assert forged_decision.accepted and forged_decision.contract is not None
    forged = forged_decision.contract

    fail(forged, "claimed-source-two-level-forged-failure")
    assert not any(
        method_payload(contract).get("recovery_source_contract_id") == forged.id
        for contract in env.store.get_all_contracts()
    )
    unavailable = [
        record
        for _, record in env.store.scan_runtime_records(
            record_type="worker_failure.recovery_unavailable"
        )
        if record.payload.get("contract_id") == forged.id
    ]
    assert len(unavailable) == 1
    assert unavailable[0].payload["reason"] == "method_source_history_unverified"
    env.store.close()


@pytest.mark.parametrize("claim_ids", [(), ("claim:a", "claim:b")])
def test_projection_recovery_missing_or_ambiguous_claim_is_observable_no_write(
    claim_ids,
):
    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.protocol.runtime_record import create_runtime_record
    from aigineering.runtime import process_rejected_submissions
    from aigineering.task_diagnostics import TaskDiagnosticError

    env = _environment(f"projection-source-claim-{len(claim_ids)}")
    env.setup.accept_asset(
        create_tool_descriptor("probe_tool", "fixture probe", {"type": "object"})
    )
    source = env.setup.accept_contract(
        method_contract(
            env.root,
            WorkerAction(type="tool", payload={"name": "probe_tool", "args": {}}),
        )
    )
    candidate_id = "candidate:v1:fixture-projection-source"
    env.store.append_runtime_record(
        create_runtime_record(
            "candidate.received",
            {"candidate_id": candidate_id, "raw_output": "fixture output"},
        )
    )
    projection = create_runtime_record(
        "projection.decided",
        {
            "candidate_id": candidate_id,
            "contract_id": source.id,
            "status": "rejected",
            "rejections": [],
        },
    )
    env.store.append_runtime_record(projection)
    for claim_id in claim_ids:
        env.store.append_runtime_record(
            create_runtime_record(
                "candidate.rejected",
                {
                    "candidate_id": candidate_id,
                    "contract_id": source.id,
                    "claim_id": claim_id,
                },
            )
        )
    before = tuple(env.store.scan_runtime_records())
    expected_code = (
        "recovery_projection_claim_ambiguous"
        if len(claim_ids) > 1
        else "recovery_projection_claim_missing"
    )
    with pytest.raises(TaskDiagnosticError) as exc_info:
        process_rejected_submissions(env.store, candidate_publishers=env.publishers)
    assert exc_info.value.code == expected_code
    assert tuple(env.store.scan_runtime_records()) == before
    assert not env.store.scan_runtime_records(
        record_type="projection_rejection.recovery_unavailable"
    )
    env.store.close()


def test_signed_v6_business_failure_preserves_exact_inputs_and_context(monkeypatch):
    from aigineering.agent.worker import WorkerExecutionError
    from aigineering.runtime import (
        WorkerInvocationError,
        claim_next_package,
        execute_claimed_package,
    )
    from aigineering.core.ids import contract_from_fields
    from conftest import hosted_worker
    import aigineering.runtime_services as runtime_services_module

    monkeypatch.setattr(
        runtime_services_module, "sync_runtime", lambda *args, **kwargs: None
    )

    class FailingBusinessWorker:
        worker_id = "worker:v6-business-failure"

        def registration(self):
            from aigineering.core.worker_routing import WorkerRegistration

            return WorkerRegistration(self.worker_id, capabilities=())

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise WorkerExecutionError("fixture_business_failure", "safe fixture")

    env = _environment("signed-v6-business-failure")
    dataset = env.setup.accept_asset(
        system_asset("dataset", "frozen input", created_by="fixture")
    )
    source = contract_from_fields(
        name="v6 business failure",
        description="",
        inputs=("dataset",),
        outputs=("report",),
        activation="dataset",
        budget=1,
        tool_scope=(),
        labels=(),
        origin="human",
        parent_id=env.root.id,
        minting_authority=("report",),
        sensitive_input_policy={"input_asset_bindings": {"dataset": [dataset.id]}},
    )
    env.setup.accept_contract(source)
    host = hosted_worker(
        env.store,
        FailingBusinessWorker(),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claimed = claim_next_package(
        env.store,
        worker_id=host.worker_id,
        contract_id=source.id,
        candidate_publishers=env.publishers,
        claim_candidate=host.claim_candidate(
            request_id="claim:v6-business-failure", contract_id=source.id
        ),
    )
    assert claimed is not None
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(
            claimed, host, env.store, candidate_publishers=env.publishers
        )
    recoveries = [
        contract
        for contract in env.store.get_all_contracts()
        if contract.name == f"{source.name}.recover"
    ]
    assert recoveries, [
        record.payload
        for _, record in env.store.scan_runtime_records()
        if record.record_type.endswith("recovery_unavailable")
        or record.record_type == "candidate.rejected"
    ]
    recovery = recoveries[0]
    bindings = recovery.sensitive_input_policy["input_asset_bindings"]
    context_name = f"_fail_context_{source.id}"
    context = next(
        asset for asset in env.store.get_all_assets() if asset.name == context_name
    )
    assert recovery.id.startswith("task:v6:")
    assert bindings["dataset"] == (dataset.id,)
    assert context_name not in bindings
    assert recovery.inputs == (context_name, "dataset")
    from aigineering.core.input_bindings import read_contract_input_assets

    assert {asset.id for asset in read_contract_input_assets(recovery, env.store)} == {
        context.id,
        dataset.id,
    }
    failure_id = next(
        record.id
        for _, record in env.store.scan_runtime_records(
            record_type="worker.invocation_failed"
        )
        if record.payload.get("contract_id") == source.id
    )
    generic_outcome = next(
        record
        for _, record in env.store.scan_runtime_records(
            record_type="worker_failure.recovery_scheduled"
        )
        if record.payload.get("contract_id") == source.id
    )
    assert generic_outcome.payload == {
        "contract_id": source.id,
        "failure_id": failure_id,
    }
    env.store.close()


def test_ordinary_reserved_marker_json_keeps_legacy_recovery_and_outcome_shape():
    import json

    from aigineering.core.ids import contract_from_fields
    from aigineering.protocol.runtime_record import create_runtime_record
    from aigineering.runtime import _commit_recovery_outcome

    env = _environment("ordinary-reserved-marker-json")
    source = contract_from_fields(
        name="ordinary metadata marker",
        description=json.dumps(
            {
                "recovery_intent_version": 1,
                "recovery_source_contract_id": "task:ordinary-metadata",
            },
            sort_keys=True,
        ),
        inputs=(),
        outputs=("report",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=(),
        origin="human",
        parent_id=env.root.id,
        minting_authority=("report",),
    )
    env.setup.accept_contract(source)
    recovery = schedule_projection_recovery(
        env.context,
        failed_contract=source,
        candidate_raw="malformed output",
        rejections=[{"category": "fixture", "name": "report", "reject_reason": "bad"}],
    )
    assert recovery is not None and recovery.origin == "recovery"
    source_record = create_runtime_record(
        "worker.invocation_failed",
        {"contract_id": source.id, "claim_id": "claim:legacy-wire"},
    )
    _commit_recovery_outcome(
        env.store,
        source,
        recovery,
        source_record=source_record,
        record_prefix="worker_failure",
        source_field="failure_id",
        record_terminal=False,
    )
    outcome = next(
        record
        for _, record in env.store.scan_runtime_records(
            record_type="worker_failure.recovery_scheduled"
        )
        if record.payload.get("contract_id") == source.id
    )
    assert outcome.payload == {
        "contract_id": source.id,
        "failure_id": source_record.id,
    }
    env.store.close()


@pytest.mark.parametrize("published_intent", ["current", "legacy"])
def test_rejected_publish_recaptures_both_signed_history_proofs(
    monkeypatch, published_intent
):
    from aigineering.agent.worker import WorkerExecutionError
    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.runtime import (
        WorkerInvocationError,
        claim_next_package,
        execute_claimed_package,
    )
    from aigineering.task_diagnostics import task_recovery_source_binding
    import aigineering.plugins.recovery_history as recovery_history_module
    import aigineering.runtime as runtime_module
    from conftest import hosted_worker
    import aigineering.runtime_services as runtime_services_module

    monkeypatch.setattr(
        runtime_services_module, "sync_runtime", lambda *args, **kwargs: None
    )

    class LocalFailure:
        worker_id = "worker:post-reject-recapture"

        def registration(self):
            from aigineering.core.worker_routing import WorkerRegistration

            return WorkerRegistration(
                self.worker_id,
                capabilities=("tool-execution", "tool:probe_tool"),
            )

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise WorkerExecutionError("local_fixture_failure", "fixture failure")

    env = _environment(f"post-reject-recapture-{published_intent}")
    env.setup.accept_asset(
        create_tool_descriptor("probe_tool", "fixture probe", {"type": "object"})
    )
    parent = env.setup.accept_contract(
        Contract(
            id=f"task:post-reject-{published_intent}-parent",
            name="post-reject-parent",
            outputs=("report",),
            budget=4,
            tool_scope=("probe_tool",),
        )
    )
    action = WorkerAction(type="tool", payload={"name": "probe_tool", "args": {}})
    source_template = method_contract(parent, action)
    method_context = system_asset(
        name=f"_method_ctx_{parent.id}",
        content=method_context_content(parent, action, source_template),
        created_by=parent.id,
        promptable=True,
    )
    source_decision = env.setup.publisher.publish(
        (
            contract_declaration_effect(source_template),
            asset_proposal_effect(method_context),
        ),
        idempotency_key=f"post-reject-source-{published_intent}",
    )
    assert source_decision.accepted and source_decision.contract is not None
    source = source_decision.contract
    host = hosted_worker(
        env.store,
        LocalFailure(),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claimed = claim_next_package(
        env.store,
        worker_id=host.worker_id,
        contract_id=source.id,
        candidate_publishers=env.publishers,
        claim_candidate=host.claim_candidate(
            request_id=f"post-reject-claim-{published_intent}",
            contract_id=source.id,
        ),
    )
    assert claimed is not None
    original_process_failures = runtime_module.process_worker_failures
    monkeypatch.setattr(
        runtime_module,
        "process_worker_failures",
        lambda *args, **kwargs: [],
    )
    with pytest.raises(WorkerInvocationError):
        execute_claimed_package(
            claimed, host, env.store, candidate_publishers=env.publishers
        )
    monkeypatch.setattr(
        runtime_module, "process_worker_failures", original_process_failures
    )
    failure = next(
        record
        for _, record in env.store.scan_runtime_records(
            record_type="worker.invocation_failed"
        )
        if record.payload.get("contract_id") == source.id
    )
    snapshot = env.context.capture_recovery_history()
    binding = task_recovery_source_binding(
        env.store,
        source.id,
        claimed.package.claim_id,
        failure.id,
        "worker.invocation_failed",
        query_context=snapshot.query_context,
    )
    from aigineering.plugins.recovery import (
        RecoveryDisposition,
        _recovery_effects,
        _schedule_tool_method_recovery,
        prepare_method_recovery_intent,
    )

    intent = prepare_method_recovery_intent(
        snapshot,
        failed_contract=source,
        source_binding=binding,
        candidate_raw="fixture failure",
        rejections=[
            {"category": "fixture", "name": "(provider)", "reject_reason": "bad"}
        ],
    )
    assert not isinstance(intent, RecoveryDisposition)
    expected_contract = (
        intent.recovery if published_intent == "current" else intent.legacy_recovery
    )
    expected_context = (
        intent.context_asset if published_intent == "current" else intent.legacy_context
    )
    original_publish = env.context.publish_task_effects

    def accepted_but_report_rejected(
        plugin_id, effects, *, idempotency_key, causal_parents
    ):
        if plugin_id == "recovery.publish.v1":
            accepted = env.publisher.publish(
                _recovery_effects(expected_contract, expected_context),
                idempotency_key=intent.idempotency_key,
                causal_parents=intent.causal_parents,
            )
            assert accepted.accepted
            return None
        return original_publish(
            plugin_id,
            effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )

    monkeypatch.setattr(
        env.context, "publish_task_effects", accepted_but_report_rejected
    )
    proof_snapshots = []
    original_prove = recovery_history_module.prove_recovery_publication

    def record_proof(snapshot, **kwargs):
        proof_snapshots.append(snapshot)
        return original_prove(snapshot, **kwargs)

    monkeypatch.setattr(
        recovery_history_module, "prove_recovery_publication", record_proof
    )
    result = _schedule_tool_method_recovery(
        env.context, intent=intent, recovery_snapshot=snapshot
    )
    assert len(proof_snapshots) == 4
    assert proof_snapshots[0] is proof_snapshots[1]
    assert proof_snapshots[2] is proof_snapshots[3]
    assert proof_snapshots[0] is not proof_snapshots[2]
    assert proof_snapshots[2].revision > proof_snapshots[0].revision
    if published_intent == "current":
        assert result.contract == intent.recovery
    else:
        assert result.contract is None
        assert result.reason == "legacy_method_intent_missing"
        assert result.recovery_contract_id == intent.legacy_recovery.id
    env.store.close()


@pytest.mark.parametrize("legacy_only", [False, True])
def test_signed_expired_method_claim_schedules_or_rejects_legacy_target(
    monkeypatch, legacy_only
):
    import time

    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.runtime import claim_next_package, process_expired_claims
    import aigineering.runtime as runtime_module
    from aigineering.plugins.recovery import _legacy_projection_intent
    from conftest import hosted_worker
    import aigineering.runtime_services as runtime_services_module

    monkeypatch.setattr(
        runtime_services_module, "sync_runtime", lambda *args, **kwargs: None
    )
    env = _environment("signed-expired-tool-method")
    env.setup.accept_asset(
        create_tool_descriptor("probe_tool", "fixture probe", {"type": "object"})
    )
    action = WorkerAction(
        type="tool", payload={"name": "probe_tool", "args": {"sample": 11}}
    )
    source_template = method_contract(env.root, action)
    method_context = system_asset(
        name=f"_method_ctx_{env.root.id}",
        content=method_context_content(env.root, action, source_template),
        created_by=env.root.id,
        promptable=True,
    )
    source_decision = env.setup.publisher.publish(
        (
            contract_declaration_effect(source_template),
            asset_proposal_effect(method_context),
        ),
        idempotency_key="signed-expired-tool-method-source",
    )
    assert source_decision.accepted and source_decision.contract is not None
    source = source_decision.contract

    class NonInvokingToolWorker:
        worker_id = "worker:expired-tool-method"

        def registration(self):
            from aigineering.core.worker_routing import WorkerRegistration

            return WorkerRegistration(
                self.worker_id, capabilities=("tool-execution", "tool:probe_tool")
            )

    host = hosted_worker(
        env.store,
        NonInvokingToolWorker(),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claimed = claim_next_package(
        env.store,
        worker_id=host.worker_id,
        contract_id=source.id,
        lease_seconds=1,
        candidate_publishers=env.publishers,
        claim_candidate=host.claim_candidate(
            request_id="claim:expired-tool-method",
            contract_id=source.id,
            lease_seconds=1,
        ),
    )
    assert claimed is not None
    time.sleep(1.05)
    if legacy_only:
        original_schedule = runtime_module._schedule_rejected_recovery

        def publish_legacy_then_schedule(
            contract, candidate_raw, rejections, store, trace, **kwargs
        ):
            legacy_contract, legacy_context = _legacy_projection_intent(
                contract, candidate_raw, rejections
            )
            legacy = env.publisher.publish(
                (
                    contract_declaration_effect(legacy_contract),
                    asset_proposal_effect(legacy_context),
                ),
                idempotency_key=f"projection-recovery:{contract.id}",
                causal_parents=(contract.id,),
            )
            assert legacy.accepted
            return original_schedule(
                contract, candidate_raw, rejections, store, trace, **kwargs
            )

        monkeypatch.setattr(
            runtime_module, "_schedule_rejected_recovery", publish_legacy_then_schedule
        )
    assert process_expired_claims(env.store, candidate_publishers=env.publishers) == [
        source.id
    ]
    expiration = next(
        record
        for _, record in env.store.scan_runtime_records(record_type="claim.expired")
        if record.payload.get("claim_id") == claimed.package.claim_id
    )
    terminal = next(
        record
        for _, record in env.store.scan_runtime_records(
            record_type="lifecycle.terminal"
        )
        if record.payload.get("contract_id") == source.id
    )
    assert expiration.id in terminal.causal_parents
    outcome = next(
        record
        for _, record in env.store.scan_runtime_records()
        if record.record_type
        in {
            "claim_expiration.recovery_scheduled",
            "claim_expiration.recovery_unavailable",
        }
        if record.payload.get("expiration_id") == expiration.id
    )
    if legacy_only:
        legacy_target = outcome.payload["recovery_contract_id"]
        assert outcome.record_type == "claim_expiration.recovery_unavailable"
        assert outcome.payload["reason"] == "legacy_method_intent_missing"
        target = env.store.get_contract(legacy_target)
        assert target is not None and target.origin == "recovery"
        from aigineering.task_diagnostics import task_diagnostics
        from aigineering.task_lineage import task_lineage

        before = tuple(env.store.scan_runtime_records())
        diagnostic = task_diagnostics(env.store, source.id, claimed.package.claim_id)[
            "data"
        ]["events"]
        unavailable_event = next(
            row for row in diagnostic if row["record_id"] == outcome.id
        )
        assert unavailable_event["reason"] == "legacy_method_intent_missing"
        assert unavailable_event["reported_recovery_contract_id"] == legacy_target
        assert unavailable_event["target_verification"] == "unverified_observation"
        lineage = task_lineage(env.store, source.id)["data"]
        observations = [
            row
            for item in lineage["source_diagnostics"]
            if item["contract_id"] == source.id
            for row in item.get("recovery_unavailable_observations", ())
        ]
        assert any(
            row["record_id"] == outcome.id
            and row["reason"] == "legacy_method_intent_missing"
            and row["reported_recovery_contract_id"] == legacy_target
            and row["target_verification"] == "unverified_observation"
            and row["verification"] == "unverified_observation"
            for row in observations
        )
        assert not any(
            edge.get("kind") == "recovery_publication"
            and edge.get("source_contract_id") == source.id
            and edge.get("target_contract_id") == legacy_target
            for edge in lineage["edges"]
        )
        assert tuple(env.store.scan_runtime_records()) == before
        import click
        from click.testing import CliRunner

        from aigineering.cli.task_diagnostics import register_task_diagnostic_commands
        from aigineering.cli.task_lineage import register_task_lineage_command

        class Collaboration:
            def sync(self):
                return {"ok": True}

        monkeypatch.setattr(
            "aigineering.cli.task_diagnostics._persistent_snapshot",
            lambda: env.store.snapshot(),
        )
        monkeypatch.setattr(
            "aigineering.cli.task_diagnostics.collaboration_service",
            lambda _store: Collaboration(),
        )
        monkeypatch.setattr(
            "aigineering.cli.task_lineage._persistent_snapshot",
            lambda: env.store.snapshot(),
        )
        monkeypatch.setattr(
            "aigineering.cli.task_lineage.collaboration_service",
            lambda _store: Collaboration(),
        )
        root = click.Group()
        task = click.Group("task")
        register_task_diagnostic_commands(task)
        register_task_lineage_command(task)
        root.add_command(task)
        diagnostic_cli = CliRunner().invoke(
            root,
            [
                "task",
                "diagnostics",
                source.id,
                "--claim",
                claimed.package.claim_id,
                "--json",
            ],
        )
        assert diagnostic_cli.exit_code == 0, diagnostic_cli.output
        diagnostic_envelope = __import__("json").loads(diagnostic_cli.output)
        diagnostic_row = next(
            row
            for row in diagnostic_envelope["data"]["events"]
            if row["record_id"] == outcome.id
        )
        assert diagnostic_row["reason"] == "legacy_method_intent_missing"
        assert diagnostic_row["target_verification"] == "unverified_observation"
        lineage_cli = CliRunner().invoke(root, ["task", "lineage", source.id, "--json"])
        assert lineage_cli.exit_code == 0, lineage_cli.output
        lineage_envelope = __import__("json").loads(lineage_cli.output)
        assert any(
            row["record_id"] == outcome.id
            and row["verification"] == "unverified_observation"
            for item in lineage_envelope["data"]["source_diagnostics"]
            for row in item.get("recovery_unavailable_observations", ())
        )
        assert tuple(env.store.scan_runtime_records()) == before
        from aigineering.protocol.runtime_record import create_runtime_record

        unknown_target = "private-unverified-recovery-reference"
        unknown = create_runtime_record(
            "claim_expiration.recovery_unavailable",
            {
                "contract_id": source.id,
                "expiration_id": expiration.id,
                "reason": "private-unrecognized-reason",
                "recovery_contract_id": unknown_target,
            },
            causal_parents=(expiration.id,),
        )
        env.store.append_runtime_record(unknown)
        unknown_event = next(
            row
            for row in task_diagnostics(env.store, source.id, claimed.package.claim_id)[
                "data"
            ]["events"]
            if row["record_id"] == unknown.id
        )
        assert "reason" not in unknown_event
        assert unknown_event["reason_status"] == "unsupported_observation"
        assert unknown_event["target_verification"] == "missing_or_invalid_reference"
        assert unknown_target not in str(unknown_event)
        unknown_lineage = task_lineage(env.store, source.id)["data"]
        unknown_observation = next(
            row
            for item in unknown_lineage["source_diagnostics"]
            if item["contract_id"] == source.id
            for row in item.get("recovery_unavailable_observations", ())
            if row["record_id"] == unknown.id
        )
        assert "reason" not in unknown_observation
        assert unknown_observation["reason_status"] == "unsupported_observation"
        assert unknown_target not in str(unknown_observation)
    else:
        recovery = env.store.get_contract(outcome.payload["recovery_contract_id"])
        assert recovery is not None and recovery.origin == "system"
        assert method_payload(recovery)["payload"] == {
            "name": "probe_tool",
            "args": {"sample": 11},
        }
        from aigineering.task_lineage import task_lineage

        lineage = task_lineage(env.store, recovery.id)["data"]
        assert any(
            edge.get("kind") == "recovery_publication"
            and edge.get("source_contract_id") == source.id
            and edge.get("target_contract_id") == recovery.id
            for edge in lineage["edges"]
        )
    env.store.close()


def test_signed_admitted_rejected_method_candidate_schedules_exact_successor(
    monkeypatch,
):
    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.protocol.types import Candidate
    from aigineering.runtime import (
        claim_next_package,
        execute_claimed_package,
        process_rejected_submissions,
    )
    from conftest import hosted_worker
    import aigineering.runtime_services as runtime_services_module

    monkeypatch.setattr(
        runtime_services_module, "sync_runtime", lambda *args, **kwargs: None
    )
    env = _environment("signed-rejected-tool-method")
    env.setup.accept_asset(
        create_tool_descriptor("probe_tool", "fixture probe", {"type": "object"})
    )
    action = WorkerAction(
        type="tool", payload={"name": "probe_tool", "args": {"sample": 13}}
    )
    source_template = method_contract(env.root, action)
    method_context = system_asset(
        name=f"_method_ctx_{env.root.id}",
        content=method_context_content(env.root, action, source_template),
        created_by=env.root.id,
        promptable=True,
    )
    source_decision = env.setup.publisher.publish(
        (
            contract_declaration_effect(source_template),
            asset_proposal_effect(method_context),
        ),
        idempotency_key="signed-rejected-tool-method-source",
    )
    assert source_decision.accepted and source_decision.contract is not None
    source = source_decision.contract

    class RejectedToolWorker:
        worker_id = "worker:rejected-tool-method"

        def registration(self):
            from aigineering.core.worker_routing import WorkerRegistration

            return WorkerRegistration(
                self.worker_id, capabilities=("tool-execution", "tool:probe_tool")
            )

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            return Candidate(
                worker_id=self.worker_id,
                raw_output='/exec {"outputs":{"undeclared":"fixture"}}',
            )

    host = hosted_worker(
        env.store,
        RejectedToolWorker(),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    claimed = claim_next_package(
        env.store,
        worker_id=host.worker_id,
        contract_id=source.id,
        candidate_publishers=env.publishers,
        claim_candidate=host.claim_candidate(
            request_id="claim:rejected-tool-method", contract_id=source.id
        ),
    )
    assert claimed is not None
    rejected_result = execute_claimed_package(
        claimed, host, env.store, candidate_publishers=env.publishers
    )
    assert rejected_result["status"] == "rejected"
    rejections = [
        record
        for _, record in env.store.scan_runtime_records(
            record_type="candidate.rejected"
        )
        if record.payload.get("contract_id") == source.id
    ]
    assert len(rejections) == 1
    assert (
        process_rejected_submissions(env.store, candidate_publishers=env.publishers)
        == []
    )
    outcome = next(
        record
        for _, record in env.store.scan_runtime_records(
            record_type="candidate_rejection.recovery_scheduled"
        )
        if record.payload.get("rejection_id") == rejections[0].id
    )
    recovery = env.store.get_contract(outcome.payload["recovery_contract_id"])
    assert recovery is not None and recovery.origin == "system"
    assert method_payload(recovery)["payload"] == {
        "name": "probe_tool",
        "args": {"sample": 13},
    }
    from aigineering.task_lineage import task_lineage

    lineage = task_lineage(env.store, recovery.id)["data"]
    assert any(
        edge.get("kind") == "recovery_publication"
        and edge.get("source_contract_id") == source.id
        and edge.get("target_contract_id") == recovery.id
        for edge in lineage["edges"]
    )
    env.store.close()


def test_parallel_method_recovery_preserves_healthy_sibling_and_join(monkeypatch):
    import json

    from aigineering.agent.mock import MockWorker
    from aigineering.agent.tool_worker import ToolWorker
    from aigineering.core.capability_descriptors import create_tool_descriptor
    from aigineering.core.tools import ToolRegistry, ToolSpec
    from aigineering.plugins.delegation import TaskDelegationPlugin
    from aigineering.runtime import (
        claim_next_package,
        execute_claimed_package,
        process_task_completions,
        process_worker_failures,
        WorkerInvocationError,
    )
    from aigineering.application import default_completion_registry
    from aigineering.agent.worker import WorkerExecutionError
    from conftest import hosted_worker
    import aigineering.runtime_services as runtime_services_module

    monkeypatch.setattr(
        runtime_services_module, "sync_runtime", lambda *args, **kwargs: None
    )
    env = _environment("parallel-method-recovery")
    for name in ("probe_tool", "healthy_tool"):
        env.setup.accept_asset(
            create_tool_descriptor(name, f"{name} fixture", {"type": "object"})
        )
    parent = env.setup.accept_contract(
        Contract(
            id="task:parallel-method-recovery-parent",
            name="parallel-method-recovery-parent",
            outputs=("report",),
            budget=8,
            tool_scope=("probe_tool", "healthy_tool"),
        )
    )
    action = WorkerAction(
        type="parallel_tool",
        payload={
            "calls": [
                {"id": "fragile", "name": "probe_tool", "args": {"n": 3}},
                {"id": "healthy", "name": "healthy_tool", "args": {}},
            ]
        },
    )
    proposal = TaskDelegationPlugin().propose_claimed(parent, action, allowance=7)
    published = env.setup.publisher.publish(
        proposal.effects, idempotency_key="signed-parallel-method-recovery"
    )
    assert published.accepted and len(published.contracts) == 3
    children = published.contracts
    failed_item = next(
        child
        for child in children
        if method_payload(child).get("method") == "parallel_tool_item"
        and method_payload(child).get("parallel_call_id") == "fragile"
    )
    healthy_item = next(
        child
        for child in children
        if method_payload(child).get("method") == "parallel_tool_item"
        and method_payload(child).get("parallel_call_id") == "healthy"
    )
    join = next(child for child in children if child.origin == "continuation")

    class FailingParallelWorker:
        worker_id = "worker:parallel-method-failure"

        def registration(self):
            from aigineering.core.worker_routing import WorkerRegistration

            return WorkerRegistration(
                self.worker_id, capabilities=("tool-execution", "tool:probe_tool")
            )

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise WorkerExecutionError("fixture_parallel_tool_failure", "safe fixture")

    failed_host = hosted_worker(
        env.store,
        FailingParallelWorker(),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    failed_claim = claim_next_package(
        env.store,
        worker_id=failed_host.worker_id,
        contract_id=failed_item.id,
        candidate_publishers=env.publishers,
        claim_candidate=failed_host.claim_candidate(
            request_id="claim:parallel-method-failure", contract_id=failed_item.id
        ),
    )
    assert failed_claim is not None
    with pytest.raises(WorkerInvocationError, match="recovery was evaluated"):
        execute_claimed_package(
            failed_claim,
            failed_host,
            env.store,
            candidate_publishers=env.publishers,
        )
    assert process_worker_failures(env.store, candidate_publishers=env.publishers) == []

    registry = ToolRegistry()
    observed = []
    registry.register(
        ToolSpec(name="probe_tool", input_schema={"type": "object"}),
        lambda args: observed.append(("probe_tool", args)) or json.dumps({"ok": True}),
    )
    registry.register(
        ToolSpec(name="healthy_tool", input_schema={"type": "object"}),
        lambda args: (
            observed.append(("healthy_tool", args)) or json.dumps({"found": False})
        ),
    )
    healthy_host = hosted_worker(
        env.store,
        ToolWorker(registry, worker_id="worker:parallel-healthy"),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    healthy_claim = claim_next_package(
        env.store,
        worker_id=healthy_host.worker_id,
        contract_id=healthy_item.id,
        candidate_publishers=env.publishers,
        claim_candidate=healthy_host.claim_candidate(
            request_id="claim:parallel-healthy", contract_id=healthy_item.id
        ),
    )
    assert healthy_claim is not None
    assert (
        execute_claimed_package(
            healthy_claim,
            healthy_host,
            env.store,
            candidate_publishers=env.publishers,
        )["status"]
        == "accepted"
    )
    assert observed == [("healthy_tool", {})]
    healthy_observations = env.store.get_assets_by_name(healthy_item.outputs[0])
    assert len(healthy_observations) == 1
    healthy_payload = json.loads(healthy_observations[0].content)
    assert healthy_payload["ok"] is True
    assert json.loads(healthy_payload["result"]) == {"found": False}
    assert (
        claim_next_package(
            env.store, worker_id="worker:join-before-recovery", contract_id=join.id
        )
        is None
    )

    recovery = next(
        contract
        for contract in env.store.get_all_contracts()
        if contract.name == f"{failed_item.name}.recover"
    )
    recovered_host = hosted_worker(
        env.store,
        ToolWorker(registry, worker_id="worker:parallel-recovered"),
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    recovered_claim = claim_next_package(
        env.store,
        worker_id=recovered_host.worker_id,
        contract_id=recovery.id,
        candidate_publishers=env.publishers,
        claim_candidate=recovered_host.claim_candidate(
            request_id="claim:parallel-recovered", contract_id=recovery.id
        ),
    )
    assert recovered_claim is not None
    assert (
        execute_claimed_package(
            recovered_claim,
            recovered_host,
            env.store,
            candidate_publishers=env.publishers,
        )["status"]
        == "accepted"
    )
    assert observed == [("healthy_tool", {}), ("probe_tool", {"n": 3})]
    recovered_observations = env.store.get_assets_by_contract(recovery.id)
    assert len(recovered_observations) == 1
    recovered_payload = json.loads(recovered_observations[0].content)
    assert recovered_payload["ok"] is True
    assert json.loads(recovered_payload["result"]) == {"ok": True}
    assert not any(
        asset.name in failed_item.outputs
        for asset in env.store.get_assets_by_contract(failed_item.id)
    )
    assert process_task_completions(
        env.store,
        default_completion_registry(),
        candidate_publishers=CandidatePublisherRegistry(
            (("continuation.publish.v1", env.publisher),)
        ),
    )
    join_worker = MockWorker(worker_id="worker:parallel-join")
    join_worker.set_output(join.name, '/exec {"outputs":{"report":"joined"}}')
    join_host = hosted_worker(
        env.store,
        join_worker,
        genesis=env.genesis,
        authority_key=env.setup.actor_key,
        authority_signer=env.setup.signer,
    )
    joined = claim_next_package(
        env.store,
        worker_id=join_host.worker_id,
        contract_id=join.id,
        candidate_publishers=env.publishers,
        claim_candidate=join_host.claim_candidate(
            request_id="claim:parallel-join", contract_id=join.id
        ),
    )
    assert joined is not None
    assert (
        execute_claimed_package(
            joined, join_host, env.store, candidate_publishers=env.publishers
        )["status"]
        == "accepted"
    )
    from aigineering.cli.task_state import project_task_status

    assert project_task_status(parent, env.store)["status"] == "completed"
    env.store.close()
