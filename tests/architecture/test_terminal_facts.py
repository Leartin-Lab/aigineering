"""Single-assignment terminal facts across stores and replicas."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.core.fact_reducer import FactReducer
from aigineering.core.ids import contract_identity_v3
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset, Contract


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_contract_terminal_is_single_assignment_and_exact_replay_is_idempotent(kind):
    store = MemoryStore() if kind == "memory" else SQLiteStore(":memory:")
    store.add_contract(Contract(id="task:one", name="one"))
    complete = create_runtime_record(
        "lifecycle.terminal", {"contract_id": "task:one", "terminal": "complete"}
    )
    failed = create_runtime_record(
        "lifecycle.terminal", {"contract_id": "task:one", "terminal": "failed"}
    )

    revision = store.append_runtime_record(complete)
    assert store.append_runtime_record(complete) == revision
    with pytest.raises(ImmutableRecordConflict, match="contract terminal"):
        store.append_runtime_record(failed)

    if isinstance(store, SQLiteStore):
        store.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_non_terminal_insert_does_not_scan_terminal_history(kind, monkeypatch):
    store = MemoryStore() if kind == "memory" else SQLiteStore(":memory:")
    original_scan = store.scan_runtime_records

    def guarded_scan(*, after_revision=0, record_type=None):
        if record_type == "lifecycle.terminal":
            raise AssertionError("non-terminal append scanned terminal history")
        return original_scan(after_revision=after_revision, record_type=record_type)

    monkeypatch.setattr(store, "scan_runtime_records", guarded_scan)
    record = create_runtime_record("test.observed", {"value": "ok"})

    assert store.append_runtime_record(record) == 1
    assert store.append_runtime_record(record) == 1

    if isinstance(store, SQLiteStore):
        store.close()


def test_sqlite_terminal_uniqueness_arbitrates_competing_replicas(tmp_path):
    path = str(tmp_path / "terminal.db")
    initial = SQLiteStore(path)
    initial.add_contract(Contract(id="task:shared", name="shared"))
    initial.close()
    records = (
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": "task:shared", "terminal": "complete"},
        ),
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": "task:shared", "terminal": "failed"},
        ),
    )

    def append(record):
        store = SQLiteStore(path)
        try:
            store.append_runtime_record(record)
            return "committed"
        except ImmutableRecordConflict:
            return "conflict"
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, records))

    assert sorted(results) == ["committed", "conflict"]
    reopened = SQLiteStore(path)
    assert len(reopened.scan_runtime_records(record_type="lifecycle.terminal")) == 1
    reopened.close()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_terminal_fact_cannot_precede_its_contract(kind):
    store = MemoryStore() if kind == "memory" else SQLiteStore(":memory:")
    terminal = create_runtime_record(
        "lifecycle.terminal",
        {"contract_id": "task:missing", "terminal": "cancelled"},
    )

    with pytest.raises(ValueError, match="unknown Contract"):
        store.append_runtime_record(terminal)

    if isinstance(store, SQLiteStore):
        store.close()


def test_atomic_fact_batch_does_not_cancel_child_satisfied_by_same_batch():
    store = MemoryStore()
    parent = build_control_plane_contract(
        name="parent", outputs=("shared_result",), budget=2
    )
    child = replace(
        build_control_plane_contract(
            name="child", outputs=("shared_result",), budget=1
        ),
        parent_id=parent.id,
    )
    child = replace(child, id=contract_identity_v3(child))
    store.add_contract(parent)
    store.add_contract(child)
    asset = Asset(
        id="asset:shared",
        name="shared_result",
        content="done",
        created_by=child.id,
        content_hash="hash",
    )

    events = FactReducer(store, MemoryTraceStore()).on_assets_created((asset,))

    terminals = [(event.type, event.contract_id) for event in events]
    assert ("contract_complete", parent.id) in terminals
    assert ("contract_complete", child.id) in terminals
    assert ("child_cancelled", child.id) not in terminals


def test_fact_reducer_does_not_repeat_a_recorded_terminal():
    store = MemoryStore()
    contract = build_control_plane_contract(
        name="finished", outputs=("result",), budget=1
    )
    store.add_contract(contract)
    store.append_runtime_record(
        create_runtime_record(
            "lifecycle.terminal",
            {"contract_id": contract.id, "terminal": "complete"},
        )
    )
    asset = Asset(
        id="asset:later",
        name="result",
        content="later observation",
        created_by=contract.id,
        content_hash="hash",
    )

    events = FactReducer(store, MemoryTraceStore()).on_assets_created((asset,))

    assert not [event for event in events if event.contract_id == contract.id]


def test_method_result_event_requires_declaring_contract_provenance():
    store = MemoryStore()
    result_name = "_plan_result_parent"
    plan_task = Contract(
        id="plan-task",
        name="parent.plan",
        outputs=(result_name,),
        origin="system",
    )
    store.add_contract(plan_task)
    forged = Asset(
        id="forged-plan",
        name=result_name,
        content='{"contracts": []}',
        created_by="plugin:planning",
    )
    authentic = replace(forged, id="authentic-plan", created_by=plan_task.id)
    reducer = FactReducer(store, MemoryTraceStore())

    assert not [
        event
        for event in reducer.on_assets_created((forged,))
        if event.type == "method_result_detected"
    ]
    detected = [
        event
        for event in reducer.on_assets_created((authentic,))
        if event.type == "method_result_detected"
    ]
    assert [(event.contract_id, event.asset_name) for event in detected] == [
        (plan_task.id, result_name)
    ]
