"""Pure projector, history, and SQLite writer arbitration for priority facts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.projection_context import (
    EffectProjectionContext,
    load_effect_projection_context,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import (
    ensure_local_domain,
    ensure_local_plugin_publisher,
)
from aigineering.plugins.task_priority import (
    PRIORITY_FACT,
    TASK_PRIORITY_EFFECTS,
    priority_history,
)
from aigineering.protocol.effect_builders import task_priority_effect
from aigineering.protocol.candidate import create_candidate_proposal
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.task_publication import publish_task_contract
from aigineering.task_priority import (
    _require_materialized_priority_contract,
    order_task_contract_ids,
    publish_task_priority,
    validate_task_priority,
)
from aigineering.task_views import project_task_status
from aigineering.task_view_query import load_task_view_query_context


def _contract(name: str):
    return build_control_plane_contract(name=name, description="", budget=3)


def _store(tmp_path: Path, monkeypatch):
    database = tmp_path / "store.db"
    monkeypatch.setenv(
        "AIG_ACTOR_KEY_FILE", str(tmp_path / "identity" / "root.ed25519")
    )
    store = SQLiteStore(database)
    ensure_local_domain(store)
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_k: None
    )
    return database, store


def _priority_publisher(store):
    publisher = ensure_local_plugin_publisher(
        store, "task.priority.operator.v1", ("task.priority.manage",)
    )
    return replace(publisher, additional_effects=TASK_PRIORITY_EFFECTS)


def test_missing_materialized_contract_is_lookup_error_before_priority_resolution(
    tmp_path, monkeypatch
):
    _database, store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr("aigineering.task_priority.require_redis", lambda: None)

    def unexpected_resolution(*_args, **_kwargs):
        raise AssertionError("absent targets must be classified before history lookup")

    monkeypatch.setattr(
        "aigineering.task_priority.priority_declaration", unexpected_resolution
    )
    monkeypatch.setattr(
        "aigineering.task_priority.priority_history", unexpected_resolution
    )
    monkeypatch.setattr(
        "aigineering.task_priority._exact_replay_publisher", unexpected_resolution
    )
    monkeypatch.setattr("aigineering.task_priority._publisher", unexpected_resolution)
    missing = "task:v6:priority-target-does-not-exist"
    with pytest.raises(LookupError, match="unknown Contract"):
        validate_task_priority(store, missing, 17, "absent target")
    with pytest.raises(LookupError, match="unknown Contract"):
        publish_task_priority(
            store,
            missing,
            17,
            "absent target",
            idempotency_key="missing-priority-key",
        )
    assert store.scan_runtime_records(record_type=PRIORITY_FACT) == []
    assert store.scan_runtime_records(record_type="candidate.received") == []
    store.close()


@pytest.mark.parametrize(
    "record_type,payload",
    [
        ("contract.declared", {"contract": {"id": "target"}}),
        (PRIORITY_FACT, {"contract_id": "target"}),
    ],
)
def test_orphaned_selected_priority_history_stays_integrity_error(
    tmp_path, monkeypatch, record_type, payload
):
    _database, store = _store(tmp_path, monkeypatch)
    monkeypatch.setattr("aigineering.task_priority.require_redis", lambda: None)
    missing = "target"
    selected = create_runtime_record(record_type, payload)
    rejected_audit = create_runtime_record(
        "candidate.authentication_rejected",
        {
            "claimed_actor_id": "worker:fixture-only",
            "claimed_candidate_id": missing,
            "reason": "fixture authentication rejection",
            "signature_kind": "ed25519",
        },
    )
    audit_only_context = replace(
        load_effect_projection_context(store), runtime_records=(rejected_audit,)
    )
    with pytest.raises(LookupError, match="unknown Contract"):
        _require_materialized_priority_contract(audit_only_context, missing)
    context = replace(
        load_effect_projection_context(store),
        runtime_records=(rejected_audit, selected),
    )
    monkeypatch.setattr(
        "aigineering.task_priority.load_effect_projection_context",
        lambda _store: context,
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("orphan history must stop before publication or lookup")

    monkeypatch.setattr("aigineering.task_priority.priority_declaration", unexpected)
    monkeypatch.setattr("aigineering.task_priority.priority_history", unexpected)
    monkeypatch.setattr("aigineering.task_priority._publisher", unexpected)
    monkeypatch.setattr("aigineering.task_priority._exact_replay_publisher", unexpected)
    with pytest.raises(
        ValueError, match="selected history but no materialized Contract"
    ):
        validate_task_priority(store, missing, 17, "orphan selected history")
    with pytest.raises(
        ValueError, match="selected history but no materialized Contract"
    ):
        publish_task_priority(
            store,
            missing,
            17,
            "orphan selected history",
            idempotency_key="orphan-priority-key",
        )
    store.close()


def test_unrelated_typed_history_does_not_hide_missing_priority_target():
    context = EffectProjectionContext(
        runtime_records=(
            create_runtime_record("contract.declared", {"contract": {"id": "other"}}),
            create_runtime_record(PRIORITY_FACT, {"contract_id": "other"}),
        ),
    )
    with pytest.raises(LookupError, match="unknown Contract"):
        _require_materialized_priority_contract(context, "missing")


def test_materialized_contract_with_missing_declaration_stays_integrity_value_error(
    tmp_path, monkeypatch
):
    _database, store = _store(tmp_path, monkeypatch)
    contract = _contract("materialized-without-declaration")
    publish_task_contract(store, contract)
    context = load_effect_projection_context(store)
    duplicated = replace(context, contracts=(*context.contracts, contract))
    with pytest.raises(ValueError, match="ambiguous in the current projection"):
        _require_materialized_priority_contract(duplicated, contract.id)
    corrupted = replace(
        context,
        runtime_records=tuple(
            record
            for record in context.runtime_records
            if not (
                record.record_type == "contract.declared"
                and record.payload["contract"]["id"] == contract.id
            )
        ),
    )
    monkeypatch.setattr(
        "aigineering.task_priority.load_effect_projection_context",
        lambda _store: corrupted,
    )
    monkeypatch.setattr("aigineering.task_priority.require_redis", lambda: None)
    with pytest.raises(ValueError, match="no unique canonical declaration"):
        validate_task_priority(store, contract.id, 17, "integrity remains closed")
    with pytest.raises(ValueError, match="no unique canonical declaration"):
        publish_task_priority(store, contract.id, 17, "integrity remains closed")
    store.close()


def test_priority_map_is_strict_and_exclusive():
    handler = TASK_PRIORITY_EFFECTS["task.priority.set"]
    assert handler.capability == "task.priority.manage"
    assert handler.direct_output_fact_types == frozenset({PRIORITY_FACT})
    assert handler.reserved_fact_types == frozenset({PRIORITY_FACT})
    assert tuple(TASK_PRIORITY_EFFECTS) == ("task.priority.set",)


def test_order_helper_preserves_supplied_ids_and_uses_priority_then_declaration(
    tmp_path, monkeypatch
):
    _database, store = _store(tmp_path, monkeypatch)
    first, second, third = (_contract(name) for name in ("first", "second", "third"))
    for contract in (first, second, third):
        publish_task_contract(store, contract)
    context = load_effect_projection_context(store)
    declarations = {
        record.payload["contract"]["id"]: record.id
        for record in context.runtime_records
        if record.record_type == "contract.declared"
    }
    publisher = _priority_publisher(store)
    publisher.publish(
        (task_priority_effect(first.id, -5, "", "lower"),),
        idempotency_key="order-first",
        causal_parents=(declarations[first.id],),
    )
    publisher.publish(
        (task_priority_effect(third.id, 5, "", "higher"),),
        idempotency_key="order-third",
        causal_parents=(declarations[third.id],),
    )
    supplied = (first.id, second.id, third.id)
    assert order_task_contract_ids(store, supplied) == (third.id, second.id, first.id)
    assert order_task_contract_ids(store, (second.id, first.id)) == (
        second.id,
        first.id,
    )
    store.close()


def test_read_view_defaults_legacy_contract_to_zero_but_fails_closed_on_fact(
    tmp_path, monkeypatch
):
    _database, store = _store(tmp_path, monkeypatch)
    contract = _contract("legacy-unpublished-declaration")
    store.add_contract(contract)
    view = project_task_status(contract, store, check_integrity=True)
    assert view["priority"] == 0
    assert view["priority_fact_id"] is None
    assert "integrity" in view

    store.append_runtime_record(
        create_runtime_record(
            PRIORITY_FACT,
            {
                "actor_id": "operator:priority",
                "candidate_id": "candidate:v1:orphan-priority",
                "contract_id": contract.id,
                "key_id": "priority-key",
                "previous_priority_id": "",
                "priority": 4,
                "reason": "orphan fact",
            },
        )
    )
    with pytest.raises(ValueError, match="unique canonical declaration"):
        project_task_status(contract, store)
    store.close()


def test_malformed_priority_fact_history_fails_closed(tmp_path, monkeypatch):
    _database, store = _store(tmp_path, monkeypatch)
    contract = _contract("malformed-history")
    publish_task_contract(store, contract)
    context = load_effect_projection_context(store)
    declaration = next(
        record
        for record in context.runtime_records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == contract.id
    )
    publisher = _priority_publisher(store)
    decision = publisher.publish(
        (task_priority_effect(contract.id, 3, "", "initial"),),
        idempotency_key="priority-init",
        causal_parents=(declaration.id,),
    )
    assert decision.accepted
    context = load_effect_projection_context(store)
    fact = next(r for r in context.runtime_records if r.record_type == PRIORITY_FACT)
    malformed_payload = deep_thaw(fact.payload)
    malformed_payload["reason"] = ""
    malformed = create_runtime_record(
        PRIORITY_FACT,
        malformed_payload,
        causal_parents=fact.causal_parents,
    )
    corrupted = replace(
        context,
        runtime_records=tuple(
            item for item in context.runtime_records if item.id != fact.id
        )
        + (malformed,),
    )
    with pytest.raises(ValueError, match="priority provenance"):
        priority_history(corrupted, contract.id)

    store.append_runtime_record(malformed)
    monkeypatch.setattr("aigineering.task_priority.require_redis", lambda: None)
    with pytest.raises(ValueError, match="priority provenance"):
        validate_task_priority(store, contract.id, 5, "integrity remains closed")
    with pytest.raises(ValueError, match="priority provenance"):
        publish_task_priority(store, contract.id, 5, "integrity remains closed")
    with pytest.raises(ValueError, match="priority provenance") as legacy_error:
        project_task_status(contract, store)
    query_context = load_task_view_query_context(store)
    with pytest.raises(ValueError, match="priority provenance") as query_error:
        project_task_status(
            contract,
            store,
            projection_context=query_context.effect_projection,
            query_context=query_context,
        )
    assert str(query_error.value) == str(legacy_error.value)
    store.close()


def test_signed_priority_candidate_with_wrong_declaration_parent_is_rejected(
    tmp_path, monkeypatch
):
    _database, store = _store(tmp_path, monkeypatch)
    contract = _contract("wrong-priority-parent")
    publish_task_contract(store, contract)
    decision = _priority_publisher(store).publish(
        (task_priority_effect(contract.id, 4, "", "wrong parent"),),
        idempotency_key="priority-wrong-parent",
        causal_parents=(),
    )
    assert not decision.accepted
    assert not store.scan_runtime_records(record_type=PRIORITY_FACT)
    assert store.scan_runtime_records(record_type="candidate.rejected")
    store.close()


def test_existing_priority_fact_without_receipt_is_not_exact_replay(
    tmp_path, monkeypatch
):
    _database, store = _store(tmp_path, monkeypatch)
    contract = _contract("missing-priority-receipt")
    publish_task_contract(store, contract)
    context = load_effect_projection_context(store)
    declaration = next(
        record
        for record in context.runtime_records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == contract.id
    )
    publisher = _priority_publisher(store)
    effect = task_priority_effect(contract.id, 6, "", "initial")
    candidate = create_candidate_proposal(
        domain_id=publisher.genesis.id,
        actor_id=publisher.actor_key.actor_id,
        key_id=publisher.actor_key.key_id,
        effects=(effect,),
        signer=publisher.signer,
        causal_parents=(declaration.id,),
        idempotency_key="priority-missing-receipt",
    )
    decision = publisher.publish(
        (effect,),
        idempotency_key="priority-missing-receipt",
        causal_parents=(declaration.id,),
    )
    receipt = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.received"
    )
    full = load_effect_projection_context(store)
    corrupted = replace(
        full,
        runtime_records=tuple(
            item for item in full.runtime_records if item.id != receipt.id
        ),
    )
    from aigineering.plugins.task_priority import task_priority_projector

    with pytest.raises(ValueError, match="receipt"):
        task_priority_projector(effect, candidate, receipt.id, corrupted)
    committed = next(
        record
        for record in full.runtime_records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate.id
    )
    missing_head = replace(
        full,
        runtime_records=tuple(
            item for item in full.runtime_records if item.id != committed.id
        ),
    )
    with pytest.raises(ValueError, match="commitment"):
        task_priority_projector(effect, candidate, receipt.id, missing_head)
    changed_head_record = create_runtime_record(
        "candidate.committed",
        {
            "candidate_id": candidate.id,
            "committed_record_ids": ["record:wrong-member"],
        },
        causal_parents=committed.causal_parents,
    )
    changed_head = replace(
        full,
        runtime_records=tuple(
            changed_head_record if item.id == committed.id else item
            for item in full.runtime_records
        ),
    )
    with pytest.raises(ValueError, match="committed member"):
        task_priority_projector(effect, candidate, receipt.id, changed_head)
    duplicate_head = replace(
        full, runtime_records=(*full.runtime_records, changed_head_record)
    )
    with pytest.raises(ValueError, match="commitment"):
        task_priority_projector(effect, candidate, receipt.id, duplicate_head)
    store.close()


def test_two_sqlite_connections_arbitrate_opposing_updates_from_same_head(
    tmp_path, monkeypatch
):
    database, store = _store(tmp_path, monkeypatch)
    contract = _contract("priority-race")
    publish_task_contract(store, contract)
    context = load_effect_projection_context(store)
    declaration = next(
        record
        for record in context.runtime_records
        if record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == contract.id
    )
    publisher_one = _priority_publisher(store)
    store_two = SQLiteStore(database)
    publisher_two = _priority_publisher(store_two)
    gate = Barrier(2)

    def pause_at_writer(store_instance):
        original = store_instance.commit_ingress_batch
        paused = False

        def commit(*args, **kwargs):
            nonlocal paused
            if kwargs.get("validate_snapshot") is not None and not paused:
                paused = True
                gate.wait(timeout=5)
            return original(*args, **kwargs)

        store_instance.commit_ingress_batch = commit

    pause_at_writer(store)
    pause_at_writer(store_two)

    def commit(publisher, value: int, key: str):
        return publisher.publish(
            (task_priority_effect(contract.id, value, "", f"set {value}"),),
            idempotency_key=key,
            causal_parents=(declaration.id,),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(commit, publisher_one, 11, "priority-race-a"),
            executor.submit(commit, publisher_two, -11, "priority-race-b"),
        ]
        decisions = [future.result(timeout=10) for future in futures]
    assert sum(decision.accepted for decision in decisions) == 1
    assert sum(not decision.accepted for decision in decisions) == 1
    snapshot = SQLiteStore.snapshot_from_path(database)
    try:
        assert len(snapshot.scan_runtime_records(record_type=PRIORITY_FACT)) == 1
        assert len(snapshot.scan_runtime_records(record_type="candidate.rejected")) == 1
    finally:
        snapshot.close()
        store_two.close()
        store.close()


def test_postcommit_redis_pending_keeps_priority_fact(tmp_path, monkeypatch):
    from aigineering.runtime_services import RedisSyncPending
    from aigineering.task_priority import publish_task_priority

    database, store = _store(tmp_path, monkeypatch)
    contract = _contract("priority-relay-pending")
    publish_task_contract(store, contract)
    calls = 0

    def sync(_store, *, accepted=None):
        nonlocal calls
        calls += 1
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr("aigineering.task_priority.require_redis", lambda: None)
    monkeypatch.setattr("aigineering.task_priority.sync_runtime", sync)
    with pytest.raises(RedisSyncPending) as pending:
        publish_task_priority(
            store,
            contract.id,
            -3,
            "relay pending",
            idempotency_key="priority-relay-pending",
        )
    assert pending.value.accepted is True
    assert calls == 2
    assert len(store.scan_runtime_records(record_type=PRIORITY_FACT)) == 1
    store.close()


def test_priority_edit_does_not_reset_an_existing_claim(tmp_path, monkeypatch):
    from aigineering.task_priority import publish_task_priority

    database, store = _store(tmp_path, monkeypatch)
    contract = _contract("priority-claimed-task")
    publish_task_contract(store, contract)
    store.register_worker(WorkerRegistration("worker:priority-claim", capacity=1))
    claim = store.claim_contract(
        contract.id, "worker:priority-claim", package_id="priority-claim-fixture"
    )
    assert claim is not None

    monkeypatch.setattr("aigineering.task_priority.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.task_priority.sync_runtime", lambda *_args, **_kwargs: None
    )
    publish_task_priority(
        store,
        contract.id,
        13,
        "reprioritize claimed work",
        idempotency_key="priority-claimed-edit",
    )
    after = store.get_claim(contract.id)
    assert after is not None
    for name in ("claim_id", "epoch", "worker_id", "status"):
        assert after[name] == claim[name]
    store.close()


def test_signed_claim_and_priority_writer_race_preserves_claim(tmp_path, monkeypatch):
    """Compete the signed claim writer with priority after both projections."""
    from aigineering.agent.mock import MockWorker
    from aigineering.agent.worker import WorkerHost
    from aigineering.core.signing import Ed25519Signer
    from aigineering.core.worker_routing import WorkerRegistration
    from aigineering.local_identity import _local_root_publisher, ensure_local_domain
    from aigineering.protocol.candidate import ActorKey
    from aigineering.protocol.effect_builders import (
        actor_authorization_effect,
        worker_registration_effect,
    )
    from aigineering.runtime_services import claim_next_application_package
    from aigineering.task_priority import publish_task_priority

    database, store_one = _store(tmp_path, monkeypatch)
    store_two = SQLiteStore(database)
    contract = _contract("signed-priority-claim-race")
    publish_task_contract(store_one, contract)

    genesis = ensure_local_domain(store_one)
    root_publisher = _local_root_publisher(store_one)[1]
    worker_signer = Ed25519Signer()
    worker_key = ActorKey(
        "worker:priority-race",
        "worker-priority-race-key",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    authorized = root_publisher.publish(
        (actor_authorization_effect(worker_key),),
        idempotency_key="authorize-priority-race-worker",
    )
    assert authorized.accepted
    registered = root_publisher.publish(
        (
            worker_registration_effect(
                WorkerRegistration(
                    worker_key.actor_id,
                    capacity=1,
                    version="1",
                    actor_id=worker_key.actor_id,
                    key_id=worker_key.key_id,
                )
            ),
        ),
        idempotency_key="register-priority-race-worker",
    )
    assert registered.accepted
    host = WorkerHost(
        MockWorker(worker_id=worker_key.actor_id),
        genesis,
        worker_key,
        worker_signer,
        registration_version="1",
    )
    # Provision the operator before the race; no authorization work is part of
    # either writer window.
    _priority_publisher(store_one)
    monkeypatch.setattr("aigineering.task_priority.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.task_priority.sync_runtime", lambda *_args, **_kwargs: None
    )

    writer_gate = Barrier(2)
    paused = {"claim": False, "priority": False}
    original_claim = store_two.claim_contract
    original_commit = store_one.commit_ingress_batch

    def claim_at_writer(*args, **kwargs):
        if not paused["claim"]:
            paused["claim"] = True
            writer_gate.wait(timeout=10)
        return original_claim(*args, **kwargs)

    def priority_at_writer(*args, **kwargs):
        if kwargs.get("validate_snapshot") is not None and not paused["priority"]:
            paused["priority"] = True
            writer_gate.wait(timeout=10)
        return original_commit(*args, **kwargs)

    store_two.claim_contract = claim_at_writer
    store_one.commit_ingress_batch = priority_at_writer

    def claim_task():
        return claim_next_application_package(
            store_two,
            worker_id=host.worker_id,
            contract_id=contract.id,
            expected_registration_version=host.registration_version,
            claim_candidate=host.claim_candidate(
                request_id="signed-priority-claim-race",
                contract_id=contract.id,
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(claim_task)
        priority_future = executor.submit(
            publish_task_priority,
            store_one,
            contract.id,
            17,
            "priority update races with signed claim",
            idempotency_key="priority-update-races-with-claim",
        )
        package = claim_future.result(timeout=15)
        priority_view, _revision = priority_future.result(timeout=15)

    assert paused == {"claim": True, "priority": True}
    assert package is not None and package.contract.id == contract.id
    assert priority_view["priority"] == 17
    claim_after = store_one.get_claim(contract.id)
    assert claim_after is not None
    assert claim_after["status"] == "active"
    assert claim_after["worker_id"] == host.worker_id
    claim_grants = [
        record
        for _revision, record in store_one.scan_runtime_records(
            record_type="claim.granted"
        )
        if record.payload.get("contract_id") == contract.id
    ]
    claim_requests = [
        record
        for _revision, record in store_one.scan_runtime_records(
            record_type="worker.claim.requested"
        )
        if record.payload.get("contract_id") == contract.id
    ]
    priority_facts = [
        record
        for _revision, record in store_one.scan_runtime_records(
            record_type=PRIORITY_FACT
        )
        if record.payload.get("contract_id") == contract.id
    ]
    assert len(claim_grants) == 1
    assert len(claim_requests) == 1, [
        (record.record_type, dict(record.payload))
        for _revision, record in store_one.scan_runtime_records()
        if "claim" in record.record_type
    ]
    assert claim_grants[0].payload["claim_id"] == claim_after["claim_id"]
    assert claim_grants[0].payload["epoch"] == claim_after["epoch"]
    assert claim_grants[0].causal_parents == (claim_requests[0].id,)
    receipts = [
        record
        for _revision, record in store_one.scan_runtime_records(
            record_type="candidate.received"
        )
        if record.payload.get("candidate_id")
        == claim_requests[0].payload["candidate_id"]
    ]
    assert len(receipts) == 1
    assert claim_requests[0].causal_parents == (receipts[0].id,)
    assert len(priority_facts) == 1
    assert priority_facts[0].payload["priority"] == 17
    assert store_two.get_claim(contract.id) == claim_after
    store_two.close()
    store_one.close()
