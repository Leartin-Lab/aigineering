"""Two-connection writer-window races for expanded handoff."""

from __future__ import annotations

from threading import Event, Thread

from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.collaboration import COLLABORATION_EFFECTS
from aigineering.plugins.handoff import HANDOFF_EFFECTS, project_task_handoffs
from aigineering.protocol.effect_builders import (
    actor_revocation_effect,
    contract_declaration_effect,
    task_block_effect,
    task_handoff_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Asset

from test_task_handoff_expanded import _published_tree


def _pause_writer(store):
    reached = Event()
    release = Event()
    original = store.commit_ingress_batch

    def paused(*args, **kwargs):
        if kwargs.get("validate_snapshot") is not None and not reached.is_set():
            reached.set()
            if not release.wait(10):
                raise TimeoutError("handoff writer barrier timed out")
        return original(*args, **kwargs)

    store.commit_ingress_batch = paused
    return reached, release


def _start_handoff(store, signer, actor, genesis, source, key):
    results = []
    errors = []

    def run():
        try:
            results.append(
                publish_effect(
                    store,
                    store,
                    genesis,
                    actor,
                    signer,
                    task_handoff_effect(source.id, "Continue the root obligation"),
                    idempotency_key=key,
                    additional_effects=HANDOFF_EFFECTS,
                )
            )
        except Exception as exc:  # surface worker-thread failures to pytest
            errors.append(exc)

    thread = Thread(target=run)
    thread.start()
    return thread, results, errors


def _handoff(store, signer, actor, genesis, source, key):
    return publish_effect(
        store,
        store,
        genesis,
        actor,
        signer,
        task_handoff_effect(source.id, "Continue the root obligation"),
        idempotency_key=key,
        additional_effects=HANDOFF_EFFECTS,
    )


def test_competing_expanded_handoffs_commit_one_return_tree(tmp_path):
    store, signer, actor, genesis, source, parent, *_descendants = _published_tree(
        tmp_path
    )
    competing = SQLiteStore(store._db_path)
    reached, release = _pause_writer(store)
    thread, results, errors = _start_handoff(
        store, signer, actor, genesis, source, "expanded-loser"
    )
    assert reached.wait(10), "first handoff did not reach writer gate"
    try:
        winner = _handoff(competing, signer, actor, genesis, source, "expanded-winner")
        assert winner.accepted, winner
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1 and not results[0].accepted
    links = store.scan_runtime_records(record_type="task.handed_off")
    returns = store.scan_runtime_records(record_type="allowance.returned")
    assert len(links) == 1
    winner_id = links[0][1].payload["candidate_id"]
    assert {record.payload["candidate_id"] for _, record in returns} == {winner_id}
    assert len(returns) == 3
    assert (
        len(
            [
                contract
                for contract in store.get_all_contracts()
                if contract.parent_id == source.id
            ]
        )
        == 2
    )  # original child plus one successor
    store.close()
    competing.close()


def test_child_claim_acquired_after_projection_is_fenced_by_handoff(tmp_path):
    store, signer, actor, genesis, source, _child, grandchild, *_rest = _published_tree(
        tmp_path
    )
    competing = SQLiteStore(store._db_path)
    reached, release = _pause_writer(store)
    thread, results, errors = _start_handoff(
        store, signer, actor, genesis, source, "claim-window-handoff"
    )
    assert reached.wait(10), "handoff did not reach writer gate"
    try:
        assert competing.claim_contract(grandchild.id, "worker:already-running")
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1 and results[0].accepted
    assert store.get_claim(grandchild.id)["status"] != "active"
    assert store.get_contract(grandchild.id) is not None
    links = project_task_handoffs(
        results[0].runtime_records, source_contract_id=source.id
    )
    assert len(links) == 1
    assert grandchild.id in links[0].payload["cancelled_contract_ids"]
    store.close()
    competing.close()


def test_selected_descendant_hold_arriving_at_writer_gate_blocks_handoff(tmp_path):
    store, signer, actor, genesis, source, _child, grandchild, *_rest = _published_tree(
        tmp_path
    )
    competing = SQLiteStore(store._db_path)
    reached, release = _pause_writer(store)
    thread, results, errors = _start_handoff(
        store, signer, actor, genesis, source, "hold-window-handoff"
    )
    assert reached.wait(10), "handoff did not reach writer gate"
    try:
        held = publish_effect(
            competing,
            competing,
            genesis,
            actor,
            signer,
            task_block_effect(grandchild.id, "pause before transfer"),
            idempotency_key="hold-before-expanded-handoff",
            additional_effects=COLLABORATION_EFFECTS,
        )
        assert held.accepted, held
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1 and not results[0].accepted
    assert store.scan_runtime_records(record_type="task.handed_off") == []
    assert store.scan_runtime_records(record_type="allowance.returned") == []
    store.close()
    competing.close()


def test_new_descendant_reservation_at_writer_gate_is_never_omitted(tmp_path):
    store, signer, actor, genesis, source, parent, *_descendants = _published_tree(
        tmp_path
    )
    competing = SQLiteStore(store._db_path)
    reached, release = _pause_writer(store)
    thread, results, errors = _start_handoff(
        store, signer, actor, genesis, source, "descendant-window-handoff"
    )
    assert reached.wait(10), "handoff did not reach writer gate"
    child = contract_from_fields(
        name="concurrent tree child",
        description="Created before the handoff writer acquired its snapshot.",
        parent_id=parent.id,
        inputs=(),
        outputs=("parallel_result",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    try:
        created = publish_effect(
            competing,
            competing,
            genesis,
            actor,
            signer,
            contract_declaration_effect(child),
            idempotency_key="concurrent-tree-child",
        )
        assert created.accepted, created
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1
    if results[0].accepted:
        links = project_task_handoffs(
            results[0].runtime_records, source_contract_id=source.id
        )
        assert len(links) == 1
        assert child.id in links[0].payload["cancelled_contract_ids"]
    else:
        assert store.scan_runtime_records(record_type="task.handed_off") == []
        assert store.scan_runtime_records(record_type="allowance.returned") == []
    assert store.get_contract(child.id) == child
    store.close()
    competing.close()


def test_operator_key_revoked_at_writer_gate_rejects_expanded_handoff(tmp_path):
    store, signer, actor, genesis, source, *_descendants = _published_tree(tmp_path)
    competing = SQLiteStore(store._db_path)
    revoker = next(key for key in genesis.root_keys if key.actor_id == "human:revoker")
    reached, release = _pause_writer(store)
    thread, results, errors = _start_handoff(
        store, signer, actor, genesis, source, "revoke-window-handoff"
    )
    assert reached.wait(10), "handoff did not reach writer gate"
    try:
        revoked = publish_effect(
            competing,
            competing,
            genesis,
            revoker,
            signer,
            actor_revocation_effect(
                actor.actor_id, actor.key_id, "Operator authority withdrawn"
            ),
            idempotency_key="revoke-handoff-operator",
        )
        assert revoked.accepted, revoked
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1 and not results[0].accepted
    assert store.scan_runtime_records(record_type="task.handed_off") == []
    assert store.scan_runtime_records(record_type="allowance.returned") == []
    assert any(
        record.record_type == "candidate.authentication_rejected"
        for record in results[0].runtime_records
    )
    store.close()
    competing.close()


def test_completed_output_arriving_at_writer_gate_is_not_cancelled_or_refunded(
    tmp_path,
):
    store, signer, actor, genesis, source, child, *_rest = _published_tree(
        tmp_path, child_outputs=("draft",)
    )
    competing = SQLiteStore(store._db_path)
    reached, release = _pause_writer(store)
    thread, results, errors = _start_handoff(
        store, signer, actor, genesis, source, "output-window-handoff"
    )
    assert reached.wait(10), "handoff did not reach writer gate"
    asset = Asset(
        id="asset:arrived-at-gate",
        name="draft",
        content="The output completed before the writer committed.",
        created_by=child.id,
        origin="human",
    )
    try:
        from aigineering.core.provenance import sign_asset

        competing.add_asset(sign_asset(asset, signed_by="human"))
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1
    if results[0].accepted:
        links = project_task_handoffs(
            results[0].runtime_records, source_contract_id=source.id
        )
        assert len(links) == 1
        assert child.id in links[0].payload["repair_contract_ids"]
        assert child.id not in links[0].payload["cancelled_contract_ids"]
        assert child.id not in {
            record.payload.get("from_contract_id")
            for _, record in store.scan_runtime_records(
                record_type="allowance.returned"
            )
        }
    else:
        assert store.scan_runtime_records(record_type="task.handed_off") == []
        assert store.scan_runtime_records(record_type="allowance.returned") == []
    store.close()
    competing.close()
