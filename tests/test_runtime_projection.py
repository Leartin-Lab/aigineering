"""Deterministic runtime projection tests."""

from __future__ import annotations

import pytest

from aigineering.core.provenance import sign_asset
from conftest import candidate_runtime
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


def test_declared_input_is_a_natural_blocker_without_activation_expression():
    store = MemoryStore()
    contract = Contract(id="task:input-blocker", inputs=("evidence",), budget=1)
    store.add_contract(contract)

    view = RuntimeProjection(store, MemoryTraceStore()).contract_view(contract)

    assert view.enabled is False
    assert view.inputs_satisfied is False
    assert view.missing_assets == ("evidence",)
    assert view.blockers == ("missing_asset:evidence",)


def test_historical_invalid_activation_is_an_explicit_blocker():
    store = MemoryStore()
    contract = Contract(
        id="task:bad-activation", activation="input_a&input_b", budget=1
    )
    store.add_contract(contract)

    view = RuntimeProjection(store, MemoryTraceStore()).contract_view(contract)

    assert view.enabled is False
    assert view.activation_satisfied is False
    assert view.blockers == ("invalid_activation",)


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


def test_projection_snapshot_reuses_runtime_records_across_contract_views():
    store = MemoryStore()
    contracts = (Contract(id="one", budget=1), Contract(id="two", budget=1))
    for contract in contracts:
        store.add_contract(contract)
    snapshot = tuple(store.scan_runtime_records())
    projection = RuntimeProjection(store, MemoryTraceStore(), runtime_records=snapshot)

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("runtime fact snapshot must be reused")

    store.scan_runtime_records = unexpected_scan

    assert [projection.contract_view(contract).enabled for contract in contracts] == [
        True,
        True,
    ]


def test_trace_terminal_events_do_not_affect_projection():
    store = MemoryStore()
    trace = MemoryTraceStore()
    contract = Contract(id="c1", budget=1)
    store.add_contract(contract)
    trace.append(create_entry("c1", "complete", sequence=0))
    trace.append(create_entry("c1", "failed", sequence=1))

    view = RuntimeProjection(store, trace).contract_view(contract)
    assert view.enabled is True
    assert view.terminal is None
    assert view.terminal_events == ()
    assert view.blockers == ()


def test_projection_as_of_uses_lifecycle_terminal_facts_not_trace():
    store = MemoryStore()
    trace = MemoryTraceStore()
    ingress = candidate_runtime(store, trace)
    contract = ingress.accept_contract(Contract(id="c1", budget=1))
    first = create_entry(contract.id, "activation", sequence=0)
    trace.append(first)
    trace.append(create_entry(contract.id, "complete", sequence=1, parent_id=first.id))
    terminal_revision = store.get_runtime_revision()
    from aigineering.core.lifecycle_facts import create_terminal_record

    store.append_runtime_record(create_terminal_record(contract.id, "complete"))

    before_terminal = RuntimeProjection(
        store, trace, as_of_revision=terminal_revision
    ).contract_view(contract)
    current = RuntimeProjection(store, trace).contract_view(contract)
    assert before_terminal.terminal is None
    assert current.terminal == "complete"
    assert current.blockers == ("terminal:complete",)
    assert before_terminal.projection_hash != current.projection_hash


def test_projection_as_of_revision_excludes_future_asset_fact():
    store = MemoryStore()
    trace = MemoryTraceStore()
    ingress = candidate_runtime(store, trace)
    contract = ingress.accept_contract(
        Contract(id="c-history", activation="input", outputs=["report"], budget=1)
    )
    before_asset = store.get_runtime_revision()
    ingress.accept_asset(
        Asset(id="input-history", name="input", content="future", origin="human")
    )

    historical = RuntimeProjection(
        store, trace, as_of_revision=before_asset
    ).contract_view(contract)
    current = RuntimeProjection(store, trace).contract_view(contract)

    assert historical.activation_satisfied is False
    assert historical.missing_assets == ("input",)
    assert current.activation_satisfied is True


def test_historical_projection_fails_closed_for_unrecorded_assets():
    store = MemoryStore()
    trace = MemoryTraceStore()
    ingress = candidate_runtime(store, trace)
    contract = ingress.accept_contract(Contract(id="c-incomplete", budget=1))
    store.add_asset(
        sign_asset(
            Asset(id="legacy", name="legacy", content="unlogged", origin="human"),
            signed_by="human",
        )
    )

    with pytest.raises(RuntimeError, match="unrecorded asset"):
        RuntimeProjection(
            store, trace, as_of_revision=store.get_runtime_revision()
        ).contract_view(contract)
