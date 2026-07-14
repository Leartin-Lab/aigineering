"""Deterministic runtime projection tests."""

from __future__ import annotations

from aigineering.core.provenance import sign_asset
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore, create_entry
from aigineering.protocol.types import Asset, Contract


def test_contract_view_reports_fact_blockers_and_enables_monotonically():
    store = MemoryStore()
    trace = MemoryTraceStore()
    contract = Contract(
        id="c1", activation="input_a AND input_b", outputs=["report"], budget=2
    )
    store.add_contract(contract)
    projection = RuntimeProjection(store, trace)

    blocked = projection.contract_view(contract)
    assert blocked.enabled is False
    assert blocked.missing_assets == ("input_a", "input_b")

    for asset_id, name in (("a", "input_a"), ("b", "input_b")):
        store.add_asset(
            sign_asset(
                Asset(id=asset_id, name=name, content=name, origin="human"),
                signed_by="human",
            )
        )
    enabled = projection.contract_view(contract)
    assert enabled.enabled is True
    assert enabled.blockers == ()


def test_projection_is_reconstructable_across_store_adapters():
    memory = MemoryStore()
    memory_trace = MemoryTraceStore()
    sqlite = SQLiteStore(":memory:")
    contract = Contract(id="c1", activation="input", outputs=["report"], budget=3)
    asset = sign_asset(
        Asset(id="input-a", name="input", content="ready", origin="human"),
        signed_by="human",
    )
    budget = create_entry("c1", "budget_consumed", sequence=0, budget_remaining=2)

    for store, trace in ((memory, memory_trace), (sqlite, sqlite)):
        store.add_contract(contract)
        store.add_asset(asset)
        trace.append(budget)

    memory_view = RuntimeProjection(memory, memory_trace).contract_view(contract)
    sqlite_view = RuntimeProjection(sqlite, sqlite).contract_view(contract)
    assert memory_view == sqlite_view
    sqlite.close()


def test_conflicting_terminal_facts_fail_closed():
    store = MemoryStore()
    trace = MemoryTraceStore()
    contract = Contract(id="c1", budget=1)
    store.add_contract(contract)
    trace.append(create_entry("c1", "complete", sequence=0))
    trace.append(create_entry("c1", "failed", sequence=1))

    view = RuntimeProjection(store, trace).contract_view(contract)
    assert view.enabled is False
    assert view.terminal == "conflict"
    assert view.terminal_events == ("complete", "failed")
    assert "terminal_conflict" in view.blockers


def test_projection_as_of_excludes_later_terminal_event():
    store = MemoryStore()
    trace = MemoryTraceStore()
    contract = Contract(id="c1", budget=1)
    store.add_contract(contract)
    first = create_entry("c1", "activation", sequence=0)
    terminal = create_entry("c1", "complete", sequence=1, parent_id=first.id)
    trace.append(first)
    trace.append(terminal)

    before_terminal = RuntimeProjection(
        store, trace, as_of=first.timestamp
    ).contract_view(contract)
    current = RuntimeProjection(store, trace).contract_view(contract)
    assert before_terminal.terminal is None
    assert current.terminal == "complete"
    assert before_terminal.projection_hash != current.projection_hash
