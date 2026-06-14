"""Engine recovery tests — save_state, restore, and restore_from_store."""

import json

from aigineering.agent.mock import MockWorker
from aigineering.core.engine import Engine
from aigineering.core.ids import hash_asset_content, hash_contract
from aigineering.core.store import MemoryStore
from aigineering.core.tools import ToolRegistry
from aigineering.core.trace import TraceStore
from aigineering.protocol.types import Asset, Contract, ToolSpec


class SequenceWorker:
    worker_id = "sequence_worker"

    def __init__(self, outputs: list[str]) -> None:
        self._outputs = outputs
        self.calls: list[tuple[Contract, list[Asset]]] = []

    def invoke(self, contract: Contract, disclosed_assets: list[Asset]):
        self.calls.append((contract, disclosed_assets))
        raw_output = self._outputs.pop(0) if self._outputs else ""
        from aigineering.protocol.types import Candidate

        return Candidate(worker_id=self.worker_id, raw_output=raw_output)


# ── save_state / restore roundtrip ────────────────────────────────────


def test_save_restore_roundtrip():
    """save_state → restore produces engine with matching budgets, completed, suspended."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker({"test": "report: thermal efficiency improved by 47%"})

    data_asset = Asset(
        id=hash_asset_content("data_file", "Sample data"),
        name="data_file",
        content="Sample data",
    )
    contract = Contract(
        id=hash_contract(
            "test", "", ["data_file"], ["report"], "data_file", 3, [], [], "human"
        ),
        name="test",
        inputs=["data_file"],
        outputs=["report"],
        activation="data_file",
        budget=3,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(data_asset)
    engine.run()

    state = engine.save_state()

    # Create fresh engine with same store and trace, restore state
    engine2 = Engine.restore(store, worker, state, trace_store=trace_store)

    assert engine2._budget == engine._budget
    assert engine2._completed == engine._completed
    assert engine2._suspended == engine._suspended
    assert engine2._method_scheduled == engine._method_scheduled
    assert engine2._contract_last_entry == engine._contract_last_entry

    # Both engines should agree the contract is complete
    assert contract.id in engine2._completed
    assert engine2._budget[contract.id] >= 0


def test_save_restore_preserves_method_context():
    """Method context assets survive save/restore roundtrip."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker(
        [
            '/tool {"name": "lookup", "args": {"key": "x"}}',
            '/exec {"outputs": {"report": "done after tool"}}',
        ]
    )
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["lookup"],
    )
    engine = Engine(store, worker, trace_store, tools=tools)
    engine.add_contract(contract)
    engine.run()

    state = engine.save_state()

    engine2 = Engine.restore(store, worker, state, trace_store=trace_store, tools=tools)

    # Method context should contain the tool observation asset
    assert contract.id in engine2._method_context
    ctx_assets = engine2._method_context[contract.id]
    obs_names = [a.name for a in ctx_assets]
    assert any("_tool_obs_" in n for n in obs_names)


# ── Recovery after method scheduling ──────────────────────────────────


def test_recovery_after_method_scheduling():
    """Engine with pending method → save → restore → method still pending."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker({"root": '/plan {"reason": "split work"}'})

    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
    )
    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.run()

    # After run, parent should be suspended (child method not yet complete)
    assert contract.id in engine._suspended
    assert len(engine._method_scheduled) > 0

    state = engine.save_state()

    engine2 = Engine.restore(store, worker, state, trace_store=trace_store)

    assert contract.id in engine2._suspended
    assert engine2._method_scheduled == engine._method_scheduled
    assert engine2._budget[contract.id] == engine._budget[contract.id]


# ── Recovery after tool observation ───────────────────────────────────


def test_recovery_after_tool_observation():
    """Engine with tool observation → save → restore → observation preserved."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker(
        [
            '/tool {"name": "lookup", "args": {"key": "x"}}',
            '/exec {"outputs": {"report": "final after tool"}}',
        ]
    )
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["lookup"],
    )
    engine = Engine(store, worker, trace_store, tools=tools)
    engine.add_contract(contract)
    engine.run()

    # Tool observation assets should exist
    obs_assets = store.get_assets_by_name(f"_tool_obs_{contract.id}")
    assert len(obs_assets) == 1

    state = engine.save_state()

    engine2 = Engine.restore(store, worker, state, trace_store=trace_store, tools=tools)

    # Verify method context contains the tool observation asset (by name, not ID)
    ctx_assets = engine2._method_context.get(contract.id, [])
    obs_names = [a.name for a in ctx_assets]
    assert any("_tool_obs_" in n for n in obs_names), (
        f"Expected tool observation in method context, got names: {obs_names}"
    )

    # Restored engine should see the same completed state
    assert contract.id in engine2._completed
    assert engine2._budget[contract.id] == engine._budget[contract.id]


# ── Recovery after plan expansion ─────────────────────────────────────


def test_recovery_after_plan_expansion():
    """Engine after plan expansion → save → restore → child contracts present."""
    plan_content = json.dumps(
        {
            "contracts": [
                {
                    "name": "draft",
                    "description": "Draft the report.",
                    "inputs": ["source"],
                    "outputs": ["draft_report"],
                    "activation": "source",
                    "budget": 2,
                }
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "split work"}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(plan_content)}}}}}',
            "",
        ]
    )
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
    )
    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.run()

    planned = [
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "draft"
    ]
    assert len(planned) == 1

    state = engine.save_state()

    engine2 = Engine.restore(store, worker, state, trace_store=trace_store)

    # Restored engine should see same completed/suspended state
    assert engine2._completed == engine._completed
    assert engine2._suspended == engine._suspended
    assert engine2._budget == engine._budget

    # Child contracts still in store
    planned2 = [
        c
        for c in store.get_all_contracts()
        if c.parent_id == contract.id and c.name == "draft"
    ]
    assert len(planned2) == 1


# ── Recovery after accepted projection ────────────────────────────────


def test_recovery_after_accepted_projection():
    """Engine after projection → save → restore → projected assets present."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker()

    data_asset = Asset(
        id=hash_asset_content("data_file", "Sample data"),
        name="data_file",
        content="Sample data",
    )
    citation_asset = Asset(
        id=hash_asset_content("citation_db", "Sample citations"),
        name="citation_db",
        content="Sample citations",
    )
    contract = Contract(
        id=hash_contract(
            "build_report",
            "",
            ["data_file", "citation_db"],
            ["final_report"],
            "data_file AND citation_db",
            5,
            [],
            [],
            "human",
        ),
        name="build_report",
        inputs=["data_file", "citation_db"],
        outputs=["final_report"],
        activation="data_file AND citation_db",
        budget=5,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(data_asset)
    engine.add_asset(citation_asset)
    engine.run()

    # Projected asset should exist
    final_reports = store.get_assets_by_name("final_report")
    assert len(final_reports) == 1

    state = engine.save_state()

    engine2 = Engine.restore(store, worker, state, trace_store=trace_store)

    assert engine2._completed == engine._completed
    assert contract.id in engine2._completed
    assert engine2._budget[contract.id] == engine._budget[contract.id]


# ── Recovery after rejected candidate ─────────────────────────────────


def test_recovery_after_rejected_candidate():
    """Engine after rejection → save → restore → rejection in trace."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker(
        {
            "test": (
                "final_report: According to Smith 2025, thermal efficiency improved.\n"
                "citation_summary: Key finding from Smith 2025."
            )
        }
    )

    input_asset = Asset(
        id=hash_asset_content("x", "y"),
        name="x",
        content="y",
    )
    contract = Contract(
        id=hash_contract("test", "", ["x"], ["final_report"], "x", 5, [], [], "human"),
        name="test",
        inputs=["x"],
        outputs=["final_report"],
        activation="x",
        budget=5,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(input_asset)
    engine.run()

    projections = trace_store.get_by_event_type("projection")
    assert len(projections) >= 1
    proj = projections[0]
    assert any("citation_summary" in r for r in proj.rejected_fragments), (
        "citation_summary should appear in rejected_fragments"
    )

    state = engine.save_state()

    engine2 = Engine.restore(store, worker, state, trace_store=trace_store)

    assert engine2._completed == engine._completed
    assert engine2._suspended == engine._suspended
    # Verify the rejection is still in the trace
    projections2 = trace_store.get_by_event_type("projection")
    assert len(projections2) >= 1
    assert any("citation_summary" in r for r in projections2[0].rejected_fragments)


# ── restore_from_store ────────────────────────────────────────────────


def test_restore_from_store():
    """Restore engine state from store records alone."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker()

    data_asset = Asset(
        id=hash_asset_content("data_file", "Sample data"),
        name="data_file",
        content="Sample data",
    )
    contract = Contract(
        id=hash_contract(
            "test", "", ["data_file"], ["report"], "data_file", 3, [], [], "human"
        ),
        name="test",
        inputs=["data_file"],
        outputs=["report"],
        activation="data_file",
        budget=3,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(data_asset)
    engine.run()

    # Restore from the same store and trace_store
    engine2 = Engine.restore_from_store(store, worker, trace_store)

    assert engine2._budget == engine._budget
    assert engine2._completed == engine._completed
    assert engine2._suspended == engine._suspended
    assert engine2._method_scheduled == engine._method_scheduled


def test_restore_from_store_with_method_scheduling():
    """restore_from_store correctly derives suspended state from method scheduling."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker({"root": '/plan {"reason": "split work"}'})

    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
    )
    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.run()

    assert contract.id in engine._suspended

    engine2 = Engine.restore_from_store(store, worker, trace_store)

    assert contract.id in engine2._suspended
    assert engine2._method_scheduled == engine._method_scheduled
    assert engine2._budget[contract.id] == engine._budget[contract.id]


def test_restore_from_store_with_tool_observation():
    """restore_from_store preserves tool observation in method context."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker(
        [
            '/tool {"name": "lookup", "args": {"key": "x"}}',
            '/exec {"outputs": {"report": "final after tool"}}',
        ]
    )
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: f"value:{args['key']}")

    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
        tool_scope=["lookup"],
    )
    engine = Engine(store, worker, trace_store, tools=tools)
    engine.add_contract(contract)
    engine.run()

    engine2 = Engine.restore_from_store(store, worker, trace_store, tools=tools)

    assert contract.id in engine2._completed
    assert engine2._budget == engine._budget
    # Method context should contain the tool observation asset
    assert contract.id in engine2._method_context
    obs_names = [a.name for a in engine2._method_context[contract.id]]
    assert any("_tool_obs_" in n for n in obs_names)


def test_restore_from_store_with_plan_expansion():
    """restore_from_store correctly derives state after plan expansion."""
    plan_content = json.dumps(
        {
            "contracts": [
                {
                    "name": "draft",
                    "description": "Draft the report.",
                    "inputs": ["source"],
                    "outputs": ["draft_report"],
                    "activation": "source",
                    "budget": 2,
                }
            ]
        },
        sort_keys=True,
    )
    worker = SequenceWorker(
        [
            '/plan {"reason": "split work"}',
            f'/exec {{"outputs": {{"_plan_result_contract_parent": {json.dumps(plan_content)}}}}}',
            "",
        ]
    )
    store = MemoryStore()
    trace_store = TraceStore()
    contract = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
    )
    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.run()

    engine2 = Engine.restore_from_store(store, worker, trace_store)

    assert engine2._budget == engine._budget
    assert engine2._completed == engine._completed
    assert engine2._suspended == engine._suspended


def test_restore_from_store_empty():
    """restore_from_store on empty store returns clean engine."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker()

    engine = Engine.restore_from_store(store, worker, trace_store)

    assert engine._budget == {}
    assert engine._completed == set()
    assert engine._suspended == set()
    assert engine._method_scheduled == set()
    assert engine._method_context == {}
    assert engine._label_context == {}
    assert engine._contract_last_entry == {}
