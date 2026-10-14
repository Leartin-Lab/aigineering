"""Causal allowance is append-only, reconstructable and race-safe."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aigineering.core.candidate_publisher import publish_effect, publish_effects
from aigineering.core.ids import hash_contract_v3
from aigineering.core.runtime_projection import (
    TERMINAL_EVENTS as RUNTIME_TERMINAL_EVENTS,
    RuntimeProjection,
)
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.terminal_types import TERMINAL_EVENTS
from aigineering.core.trace import MemoryTraceStore
from aigineering.plugins import PluginRequest, StagedPlanningPlugin
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateEffect,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Contract


def test_terminal_event_values_have_one_dependency_neutral_owner():
    core = Path(__file__).resolve().parents[2] / "src" / "aigineering" / "core"
    definitions = []
    for path in core.glob("*.py"):
        tree = ast.parse(path.read_text())
        if any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "TERMINAL_EVENTS"
                for target in node.targets
            )
            for node in tree.body
        ):
            definitions.append(path.name)

    assert definitions == ["terminal_types.py"]
    assert TERMINAL_EVENTS == frozenset(
        {"complete", "failed", "cancelled", "unreachable"}
    )
    assert RUNTIME_TERMINAL_EVENTS is TERMINAL_EVENTS
    allowance_tree = ast.parse((core / "causal_allowance.py").read_text())
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "aigineering.core.terminal_types"
        and any(alias.name == "TERMINAL_EVENTS" for alias in node.names)
        for node in allowance_tree.body
    )
    terminal_tree = ast.parse((core / "terminal_types.py").read_text())
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.module
        in {
            "aigineering.core.causal_allowance",
            "aigineering.core.lifecycle_facts",
            "aigineering.core.runtime_projection",
        }
        for node in terminal_tree.body
    )


def test_allowance_second_writer_check_uses_complete_pending_records():
    core = Path(__file__).resolve().parents[2] / "src" / "aigineering" / "core"
    tree = ast.parse((core / "sqlite_store.py").read_text())
    commit = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SQLiteStore"
    )
    method = next(
        node
        for node in commit.body
        if isinstance(node, ast.FunctionDef) and node.name == "commit_ingress_batch"
    )
    merge_line = next(
        node.lineno
        for node in ast.walk(method)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "runtime_records"
            for target in node.targets
        )
        and isinstance(node.value, ast.BinOp)
        and isinstance(node.value.op, ast.Add)
        and isinstance(node.value.left, ast.Name)
        and node.value.left.id == "runtime_records"
        and isinstance(node.value.right, ast.Name)
        and node.value.right.id == "derived_records"
    )
    complete_pending_checks = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_validate_allowance_records"
        and node.lineno > merge_line
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "runtime_records"
    ]

    assert complete_pending_checks


def test_all_terminal_store_writers_use_the_shared_allowance_materializer():
    core = Path(__file__).resolve().parents[2] / "src" / "aigineering" / "core"

    def class_method(path, class_name, method_name):
        tree = ast.parse(path.read_text())
        owner = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        return next(
            node
            for node in owner.body
            if isinstance(node, ast.FunctionDef) and node.name == method_name
        )

    sqlite = core / "sqlite_store.py"
    for name in (
        "commit_ingress_batch",
        "commit_worker_invocation_failure",
        "commit_claim_expiration",
    ):
        method = class_method(sqlite, "SQLiteStore", name)
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_commit_time_ingress_consequences"
            for node in ast.walk(method)
        )
    consequence_owner = class_method(
        sqlite, "SQLiteStore", "_commit_time_ingress_consequences"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "materialize_terminal_allowance"
        for node in ast.walk(consequence_owner)
    )
    memory = class_method(core / "store.py", "MemoryStore", "commit_ingress_batch")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "materialize_terminal_allowance"
        for node in ast.walk(memory)
    )


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
    # Staged planning deliberately leaves three causal units available so a
    # rejected draft, dependency analysis, or compile result can publish one
    # repair task instead of ending the root silently.
    assert RuntimeProjection(store, trace).contract_view(root).budget_remaining == 3
    compile_contract = next(
        contract
        for contract in stage_decision.contracts
        if "plugin:plan.compile" in contract.labels
    )
    assert (
        RuntimeProjection(store, trace).contract_view(compile_contract).budget_remaining
        == 3
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
