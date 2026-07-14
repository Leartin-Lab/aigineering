"""Cross-adapter conformance for immutable runtime facts and projections."""

from __future__ import annotations

from aigineering.core.fact_reducer import FactReducer
from aigineering.core.provenance import sign_asset
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore, create_entry
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import asset_to_dict, contract_to_dict


def _canonical_store_snapshot(store) -> dict[str, object]:
    return {
        "assets": sorted(
            (asset_to_dict(asset) for asset in store.get_all_assets()),
            key=lambda item: item["id"],
        ),
        "contracts": sorted(
            (contract_to_dict(contract) for contract in store.get_all_contracts()),
            key=lambda item: item["id"],
        ),
        "waiting_for_input_a": sorted(store.get_contracts_waiting_for("input_a")),
        "declares_report": sorted(store.get_contracts_declaring_output("report")),
        "index_digest": store.projection_index_digest(),
    }


def _event_snapshot(events) -> list[tuple[str, str, str, dict[str, object]]]:
    return [
        (event.type, event.contract_id, event.asset_name, dict(event.details))
        for event in events
    ]


def test_memory_and_sqlite_have_identical_fact_and_index_semantics():
    memory = MemoryStore()
    sqlite = SQLiteStore(":memory:")
    contract = Contract(
        id="c1",
        name="build-report",
        activation="input_a AND input_b",
        inputs=["input_a", "input_b"],
        outputs=["report"],
        budget=3,
        worker_capabilities=["reasoning"],
        worker_pools=["default"],
    )
    assets = [
        sign_asset(
            Asset(id="a-input", name="input_a", content="A", origin="human"),
            signed_by="human",
        ),
        sign_asset(
            Asset(id="b-input", name="input_b", content="B", origin="human"),
            signed_by="human",
        ),
    ]

    for store in (memory, sqlite):
        store.add_contract(contract)
        for asset in assets:
            store.add_asset(asset)

    assert _canonical_store_snapshot(memory) == _canonical_store_snapshot(sqlite)
    sqlite.close()


def test_memory_and_sqlite_reducers_project_same_pending_fact():
    memory = MemoryStore()
    sqlite = SQLiteStore(":memory:")
    contract = Contract(id="c1", outputs=["report"])
    pending = sign_asset(
        Asset(
            id="report-a",
            name="report",
            content="done",
            created_by="c1",
            origin="worker",
        ),
        signed_by="worker",
    )

    memory.add_contract(contract)
    sqlite.add_contract(contract)
    memory_events = FactReducer(memory, MemoryTraceStore()).on_asset_created(pending)
    sqlite_events = FactReducer(sqlite, sqlite).on_asset_created(pending)

    assert _event_snapshot(memory_events) == _event_snapshot(sqlite_events)
    assert [event.type for event in memory_events] == [
        "output_satisfied",
        "contract_complete",
    ]
    sqlite.close()


def test_memory_and_sqlite_trace_replay_semantics_match():
    memory = MemoryTraceStore()
    sqlite = SQLiteStore(":memory:")
    entry = create_entry(
        "c1",
        "projection",
        sequence=0,
        candidate_raw="report: done",
        accepted_asset_names=["report"],
        authority_result="accepted",
    )

    for trace in (memory, sqlite):
        trace.append(entry)
        trace.append(entry)

    assert memory.get_all() == sqlite.get_all()
    assert memory.sequence == 1
    assert len(sqlite.get_all()) == 1
    sqlite.close()


def test_ingress_appends_typed_facts_on_both_adapters():
    memory = MemoryStore()
    memory_trace = MemoryTraceStore()
    sqlite = SQLiteStore(":memory:")
    contract = Contract(id="c-runtime", outputs=["report"])
    asset = Asset(id="a-runtime", name="input", content="value", origin="human")

    RuntimeIngress(memory, memory_trace).accept_contract(contract)
    RuntimeIngress(memory, memory_trace).accept_asset(asset)
    RuntimeIngress(sqlite, sqlite).accept_contract(contract)
    RuntimeIngress(sqlite, sqlite).accept_asset(asset)

    for store in (memory, sqlite):
        record_types = [
            record.record_type for _, record in store.scan_runtime_records()
        ]
        assert record_types.count("contract.declared") == 1
        assert record_types.count("asset.committed") == 1
        assert record_types.count("trace.recorded") == 2
    sqlite.close()
