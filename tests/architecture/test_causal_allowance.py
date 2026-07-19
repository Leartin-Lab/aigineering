"""Causal allowance is append-only, reconstructable and race-safe."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from aigineering.core.candidate_publisher import publish_effect, publish_effects
from aigineering.core.ids import hash_contract_v3
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.plugins import PluginRequest, StagedPlanningPlugin
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Contract


def _contract(
    name: str,
    *,
    budget: int,
    parent_id: str | None = None,
    labels: tuple[str, ...] = (),
) -> Contract:
    fields = {
        "name": name,
        "description": f"Perform {name}",
        "inputs": (),
        "outputs": (f"{name}_result",),
        "activation": "",
        "budget": budget,
        "tool_scope": (),
        "labels": labels,
        "origin": "human" if parent_id is None else "plugin",
        "parent_id": parent_id,
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


def _identity():
    signer = Ed25519Signer()
    actor = ActorKey(
        "human:allowance-owner",
        "allowance-key",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "contract.publish.protected", "contract.cancel"),
    )
    return (
        signer,
        actor,
        create_genesis_manifest("causal-allowance", (actor,), "policy:test"),
    )


def _records(store, record_type: str):
    return [record for _, record in store.scan_runtime_records(record_type=record_type)]


def test_root_grant_and_staged_planning_are_reconstructed_from_facts():
    signer, actor, genesis = _identity()
    store = MemoryStore()
    trace = MemoryTraceStore()
    root = _contract("release", budget=8)
    root_decision = publish_effect(
        store,
        trace,
        genesis,
        actor,
        signer,
        contract_declaration_effect(root),
        idempotency_key="root",
    )
    stages = StagedPlanningPlugin().propose(PluginRequest(parent=root, allowance=8))
    stage_decision = publish_effects(
        store,
        trace,
        genesis,
        actor,
        signer,
        stages.effects,
        idempotency_key="stages",
    )

    assert root_decision.accepted is True
    assert stage_decision.accepted is True
    assert [
        record.payload["amount"] for record in _records(store, "allowance.granted")
    ][0] == 8
    reservations = _records(store, "allowance.reserved")
    assert len(reservations) == 3
    assert {record.payload["purpose"] for record in reservations} == {"planning"}
    assert RuntimeProjection(store, trace).contract_view(root).budget_remaining == 0
    compile_contract = next(
        contract
        for contract in stage_decision.contracts
        if "plugin:plan.compile" in contract.labels
    )
    assert (
        RuntimeProjection(store, trace).contract_view(compile_contract).budget_remaining
        == 6
    )


def test_overallocation_rejects_the_whole_candidate_batch():
    signer, actor, genesis = _identity()
    store = MemoryStore()
    trace = MemoryTraceStore()
    root = _contract("bounded", budget=3)
    publish_effect(
        store,
        trace,
        genesis,
        actor,
        signer,
        contract_declaration_effect(root),
        idempotency_key="bounded-root",
    )
    children = (
        _contract("child-a", budget=2, parent_id=root.id),
        _contract("child-b", budget=2, parent_id=root.id),
    )

    decision = publish_effects(
        store,
        trace,
        genesis,
        actor,
        signer,
        tuple(contract_declaration_effect(child) for child in children),
        idempotency_key="too-much",
    )

    assert decision.accepted is False
    assert all(store.get_contract(child.id) is None for child in children)
    assert _records(store, "allowance.reserved") == []
    assert _records(store, "candidate.rejected")


def test_sqlite_commit_rechecks_competing_reservations(tmp_path):
    signer, actor, genesis = _identity()
    path = tmp_path / "allowance-race.db"
    setup = SQLiteStore(str(path))
    root = _contract("race", budget=3)
    publish_effect(
        setup,
        setup,
        genesis,
        actor,
        signer,
        contract_declaration_effect(root),
        idempotency_key="race-root",
    )
    setup.close()
    children = (
        _contract("racer-a", budget=2, parent_id=root.id),
        _contract("racer-b", budget=2, parent_id=root.id),
    )

    def publish(child: Contract):
        store = SQLiteStore(str(path))
        try:
            return publish_effect(
                store,
                store,
                genesis,
                actor,
                signer,
                contract_declaration_effect(child),
                idempotency_key=child.id,
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(publish, children))

    reopened = SQLiteStore(str(path))
    try:
        reservations = _records(reopened, "allowance.reserved")
        assert sum(decision.accepted for decision in decisions) == 1
        assert sum(int(record.payload["amount"]) for record in reservations) == 2
        assert len(_records(reopened, "candidate.rejected")) == 1
        assert (
            RuntimeProjection(reopened, reopened).contract_view(root).budget_remaining
            == 1
        )
    finally:
        reopened.close()


def test_terminal_extinguishes_remaining_allowance_and_replay_is_stable():
    signer, actor, genesis = _identity()
    store = MemoryStore()
    trace = MemoryTraceStore()
    root = _contract("cancelled", budget=4)
    publish_effect(
        store,
        trace,
        genesis,
        actor,
        signer,
        contract_declaration_effect(root),
        idempotency_key="cancelled-root",
    )
    cancellation = CandidateEffect(
        "contract.cancel", {"contract_id": root.id, "reason": "superseded"}
    )
    first = publish_effect(
        store,
        trace,
        genesis,
        actor,
        signer,
        cancellation,
        idempotency_key="cancel",
    )
    revision = store.get_runtime_revision()
    second = publish_effect(
        store,
        trace,
        genesis,
        actor,
        signer,
        cancellation,
        idempotency_key="cancel",
    )

    assert first.accepted is second.accepted is True
    assert store.get_runtime_revision() == revision
    extinguished = _records(store, "allowance.extinguished")
    assert len(extinguished) == 1
    assert extinguished[0].payload["amount"] == 4
    assert RuntimeProjection(store, trace).contract_view(root).budget_remaining == 0


def test_sqlite_serializes_child_publication_against_terminal_extinguishment(
    tmp_path,
):
    signer, actor, genesis = _identity()
    path = tmp_path / "allowance-terminal-race.db"
    setup = SQLiteStore(str(path))
    root = _contract("terminal-race", budget=3)
    publish_effect(
        setup,
        setup,
        genesis,
        actor,
        signer,
        contract_declaration_effect(root),
        idempotency_key="terminal-race-root",
    )
    setup.close()
    child = _contract("late-child", budget=2, parent_id=root.id)
    cancellation = CandidateEffect(
        "contract.cancel", {"contract_id": root.id, "reason": "concurrent stop"}
    )

    def publish(effect, key: str):
        store = SQLiteStore(str(path))
        try:
            return publish_effect(
                store,
                store,
                genesis,
                actor,
                signer,
                effect,
                idempotency_key=key,
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(
                publish, contract_declaration_effect(child), "terminal-race-child"
            ),
            pool.submit(publish, cancellation, "terminal-race-cancel"),
        )
        decisions = [future.result() for future in futures]

    reopened = SQLiteStore(str(path))
    try:
        reservations = _records(reopened, "allowance.reserved")
        extinguished = _records(reopened, "allowance.extinguished")
        terminals = _records(reopened, "lifecycle.terminal")
        accepted = sum(decision.accepted for decision in decisions)
        assert accepted in {1, 2}
        assert len(_records(reopened, "candidate.rejected")) == 2 - accepted
        if terminals:
            reserved = sum(int(record.payload["amount"]) for record in reservations)
            consumed = sum(int(record.payload["amount"]) for record in extinguished)
            assert reserved + consumed == 3
        else:
            assert [record.payload["amount"] for record in reservations] == [2]
            assert extinguished == []
    finally:
        reopened.close()
