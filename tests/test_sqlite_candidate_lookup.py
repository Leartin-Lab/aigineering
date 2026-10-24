"""The existing committed-Candidate predicate uses an exact, rebuildable index."""

from __future__ import annotations

from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)


INDEX = "idx_runtime_records_committed_candidate"
LOOKUP = (
    "SELECT 1 FROM runtime_records WHERE record_type = 'candidate.committed' "
    "AND json_extract(payload_json, '$.candidate_id') = ? LIMIT 1"
)


def _publisher(store, *, signer=None, genesis=None, actor=None):
    signer = signer or Ed25519Signer()
    actor = actor or ActorKey(
        "lookup:root", "root-1", signer.kind, signer.signer_id, ("contract.publish",)
    )
    if genesis is None:
        genesis = create_genesis_manifest("lookup-test", (actor,), "policy:lookup-v1")
        initialize_genesis(store, genesis)
    return (
        CandidatePublisher(store, store, genesis, actor, signer),
        signer,
        genesis,
        actor,
    )


def _publish(publisher, index):
    contract = build_control_plane_contract(
        name=f"lookup-{index}", outputs=(f"lookup_output_{index}",), budget=1
    )
    effect = contract_declaration_effect(contract)
    decision = publisher.publish((effect,), idempotency_key=f"lookup-{index}")
    assert decision.accepted
    return effect, decision


def _missing_lookup_vm_steps(store):
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    store._conn.set_progress_handler(progress, 1)
    try:
        assert not store._candidate_already_committed("candidate:never-published")
    finally:
        store._conn.set_progress_handler(None, 0)
    return steps


def test_writable_reopen_indexes_old_heads_without_changing_facts(tmp_path):
    path = str(tmp_path / "lookup.db")
    store = SQLiteStore(path)
    try:
        publisher, *_ = _publisher(store)
        for index in range(64):
            _publish(publisher, index)
        facts = tuple(record.id for _, record in store.scan_runtime_records())
        schema_version = store.schema_version
        # Simulate an existing supported Store that predates the optional index.
        store._conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
        store._conn.commit()
        unindexed_steps = _missing_lookup_vm_steps(store)
    finally:
        store.close()

    reopened = SQLiteStore(path)
    try:
        assert reopened.schema_version == schema_version
        assert (
            tuple(record.id for _, record in reopened.scan_runtime_records()) == facts
        )
        plan = [
            row["detail"]
            for row in reopened._conn.execute(
                "EXPLAIN QUERY PLAN " + LOOKUP, ("candidate:never-published",)
            )
        ]
        assert any(INDEX in detail for detail in plan), plan
        # VM instructions proxy database work, not wall time or exact row scans.
        indexed_steps = _missing_lookup_vm_steps(reopened)
        assert indexed_steps * 4 <= unindexed_steps
        statements = []
        reopened._conn.set_trace_callback(statements.append)
        try:
            assert not reopened._candidate_already_committed("")
        finally:
            reopened._conn.set_trace_callback(None)
        assert statements == []
    finally:
        reopened.close()


def test_exact_lookup_keeps_rejections_and_cross_connection_replay_distinct(tmp_path):
    path = str(tmp_path / "replay.db")
    first = SQLiteStore(path)
    second = None
    try:
        publisher, signer, genesis, actor = _publisher(first)
        effect, accepted = _publish(publisher, 0)
        assert first._candidate_already_committed(accepted.candidate_id)
        # The authenticated actor lacks asset.publish; its audit is not a head.
        rejected = publisher.publish(
            (
                asset_proposal_effect(
                    build_control_plane_asset(name="forbidden", content="x")
                ),
            ),
            idempotency_key="rejected-publication",
        )
        assert not rejected.accepted
        assert not first._candidate_already_committed(rejected.candidate_id)
        facts = tuple(record.id for _, record in first.scan_runtime_records())
        second = SQLiteStore(path)
        second_publisher, *_ = _publisher(
            second, signer=signer, genesis=genesis, actor=actor
        )
        replay = second_publisher.publish((effect,), idempotency_key="lookup-0")
        assert replay.accepted
        assert replay.candidate_id == accepted.candidate_id
        assert second._candidate_already_committed(accepted.candidate_id)
        assert not second._candidate_already_committed(rejected.candidate_id)
        assert tuple(record.id for _, record in second.scan_runtime_records()) == facts
    finally:
        if second is not None:
            second.close()
        first.close()
