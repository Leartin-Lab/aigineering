"""Killer integration test — ACM boundary prevents hallucinated facts."""

import json

from aigineering.core.store import MemoryStore
from aigineering.core.engine import Engine
from aigineering.core.tools import ToolRegistry
from aigineering.core.trace import TraceStore
from aigineering.agent.mock import MockWorker
from aigineering.core.ids import asset_id, contract_id
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


def _asset_canonical(name: str, content: str) -> str:
    return json.dumps(
        {"name": name, "content": content, "content_type": "text",
         "created_by": "", "origin": "human"},
        sort_keys=True, ensure_ascii=False,
    )


def _contract_canonical(name: str, inputs: list[str], outputs: list[str], activation: str) -> str:
    return json.dumps(
        {"parent_id": None, "name": name, "description": "",
         "inputs": sorted(inputs), "outputs": sorted(outputs),
         "activation": activation, "budget": 5, "tool_scope": [], "origin": "human"},
        sort_keys=True, ensure_ascii=False,
    )


def test_hallucinated_output_cannot_become_runtime_fact():
    """
    The ACM boundary must reject undeclared outputs from the worker.

    Scenario:
      - Contract 'build_report' declares outputs: [final_report]
      - Mock worker produces: final_report AND citation_summary (hallucinated)
      - Authority REJECTS citation_summary (not in declared outputs)
      - Trace records the rejection
    """
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker()

    # Input assets
    data_file = Asset(
        id=asset_id(_asset_canonical("data_file", "Sample data")),
        name="data_file", content="Sample data",
    )
    citation_db = Asset(
        id=asset_id(_asset_canonical("citation_db", "Sample citations")),
        name="citation_db", content="Sample citations",
    )

    # Contract with only final_report as declared output
    contract = Contract(
        id=contract_id(_contract_canonical(
            "build_report", ["data_file", "citation_db"],
            ["final_report"], "data_file AND citation_db",
        )),
        name="build_report",
        inputs=["data_file", "citation_db"],
        outputs=["final_report"],
        activation="data_file AND citation_db",
        budget=5,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(data_file)
    engine.add_asset(citation_db)
    engine.run()

    # ── Verify ──────────────────────────────────────────────

    # 1. final_report EXISTS in the store
    final_reports = store.get_assets_by_name("final_report")
    assert len(final_reports) == 1, "final_report should be committed"
    assert "thermal efficiency" in final_reports[0].content

    # 2. citation_summary does NOT exist in the store
    citation_assets = store.get_assets_by_name("citation_summary")
    assert len(citation_assets) == 0, (
        "citation_summary must NOT be committed — it was not a declared output"
    )

    # 3. Trace records the REJECTION
    projections = trace_store.get_by_event_type("projection")
    assert len(projections) == 1
    projection = projections[0]

    assert len(projection.accepted_fragments) >= 1, "should have accepted final_report"
    assert any("citation_summary" in r for r in projection.rejected_fragments), (
        "citation_summary should appear in rejected_fragments"
    )
    assert projection.authority_result == "partial", (
        "authority_result should be 'partial' because citation_summary was rejected "
        "but final_report was accepted"
    )

    # 4. Trace has all expected event types
    event_types = {e.event_type for e in trace_store.get_all()}
    assert "activation" in event_types
    assert "disclosure" in event_types
    assert "projection" in event_types
    assert "complete" in event_types


def test_duplicate_conflicting_outputs_are_all_rejected():
    """
    If worker produces multiple outputs with same name but different content,
    NONE should be committed — all instances are rejected.
    """
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker()
    worker.set_output("test", "final_report: version A\nfinal_report: version B")

    input_asset = Asset(
        id=asset_id(_asset_canonical("x", "y")),
        name="x", content="y",
    )
    contract = Contract(
        id=contract_id(_contract_canonical(
            "test", ["x"], ["final_report"], "x",
        )),
        name="test", inputs=["x"], outputs=["final_report"],
        activation="x", budget=5,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(input_asset)
    engine.run()

    final_reports = store.get_assets_by_name("final_report")
    assert len(final_reports) == 0, (
        "No final_report should be committed — duplicate with conflicting content"
    )

    projections = trace_store.get_by_event_type("projection")
    assert len(projections) >= 1
    proj = projections[0]
    assert len(proj.accepted_fragments) == 0
    assert "duplicate" in str(proj.rejected_fragments).lower() or any(
        "duplicate" in r.lower() for r in proj.rejected_fragments
    )


def test_parse_rejection_recorded_in_trace():
    """Lines without colon separator should appear as parse rejections in trace."""
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker()
    worker.set_output("test", "valid: content\nthis line has no colon\n# comment")

    input_asset = Asset(
        id=asset_id(_asset_canonical("x", "y")),
        name="x", content="y",
    )
    contract = Contract(
        id=contract_id(_contract_canonical(
            "test", ["x"], ["valid"], "x",
        )),
        name="test", inputs=["x"], outputs=["valid"],
        activation="x", budget=5,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(input_asset)
    engine.run()

    projections = trace_store.get_by_event_type("projection")
    proj = projections[0]
    assert len(proj.accepted_fragments) == 1
    assert "no colon" in str(proj.rejected_fragments).lower()


def test_method_action_schedules_subcontract_without_projection():
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker({"root": '/plan {"reason": "split work"}'})
    input_asset = Asset(
        id=asset_id(_asset_canonical("x", "y")),
        name="x",
        content="y",
    )
    contract = Contract(
        id=contract_id(_contract_canonical("root", ["x"], ["report"], "x")),
        name="root",
        inputs=["x"],
        outputs=["report"],
        activation="x",
        budget=5,
    )

    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)
    engine.add_asset(input_asset)
    engine.run()

    contracts = store.get_all_contracts()
    child_contracts = [c for c in contracts if c.parent_id == contract.id]
    assert len(child_contracts) == 1
    assert child_contracts[0].name == "root.plan"
    assert child_contracts[0].outputs == [f"_plan_result_{contract.id}"]

    context_assets = store.get_assets_by_name(f"_method_ctx_{contract.id}")
    assert len(context_assets) == 1
    assert context_assets[0].origin == "system"
    assert context_assets[0].minted_by == "engine"

    assert store.get_assets_by_name("report") == []
    scheduled = trace_store.get_by_event_type("method_scheduled")
    assert len(scheduled) == 1
    assert scheduled[0].relation_type == "plan"
    assert scheduled[0].relation_target == child_contracts[0].id


def test_method_scheduling_deduplicates_by_child_contract_id():
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker({"root": '/tool {"name": "search", "args": {"q": "a"}}'})
    contract = Contract(
        id="contract_parent",
        name="root",
        activation="",
        budget=5,
        tool_scope=["search"],
    )
    engine = Engine(store, worker, trace_store)
    engine.add_contract(contract)

    engine.run()
    first_children = [c for c in store.get_all_contracts() if c.parent_id == contract.id]
    assert len(first_children) == 1

    engine._suspended.clear()
    worker.set_output("root", '/tool {"name": "search", "args": {"q": "b"}}')
    engine.run()

    children = [c for c in store.get_all_contracts() if c.parent_id == contract.id]
    assert len(children) == 2
    assert {c.id for c in children} != {first_children[0].id}


def test_tool_method_executes_registry_and_commits_observation():
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker({"root": '/tool {"name": "lookup", "args": {"key": "x"}}'})
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

    assert store.get_assets_by_name("report") == []
    call_assets = [
        asset for asset in store.get_all_assets()
        if asset.name.startswith("_tool_call_")
    ]
    obs_assets = store.get_assets_by_name(f"_tool_obs_{contract.id}")
    assert len(call_assets) == 1
    assert call_assets[0].promptable is False
    assert len(obs_assets) == 1
    assert obs_assets[0].origin == "system"
    assert "value:x" in obs_assets[0].content

    tool_events = trace_store.get_by_event_type("tool_executed")
    assert len(tool_events) == 1
    assert tool_events[0].relation_target == "lookup"
    assert tool_events[0].authority_result == "accepted"


def test_tool_method_records_error_observation_for_out_of_scope_tool():
    store = MemoryStore()
    trace_store = TraceStore()
    worker = MockWorker({"root": '/tool {"name": "lookup", "args": {"key": "x"}}'})
    tools = ToolRegistry()
    tools.register(ToolSpec(name="lookup"), lambda args: "value")
    contract = Contract(
        id="contract_parent",
        name="root",
        activation="",
        budget=5,
        tool_scope=[],
    )
    engine = Engine(store, worker, trace_store, tools=tools)
    engine.add_contract(contract)

    engine.run()

    obs_assets = store.get_assets_by_name(f"_tool_obs_{contract.id}")
    assert len(obs_assets) == 1
    assert "not in contract.tool_scope" in obs_assets[0].content
    tool_events = trace_store.get_by_event_type("tool_executed")
    assert tool_events[0].authority_result == "rejected"


def test_tool_observation_resumes_parent_without_satisfying_output():
    store = MemoryStore()
    trace_store = TraceStore()
    worker = SequenceWorker([
        '/tool {"name": "lookup", "args": {"key": "x"}}',
        '/exec {"outputs": {"report": "final after tool"}}',
    ])
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

    reports = store.get_assets_by_name("report")
    assert len(reports) == 1
    assert reports[0].content == "final after tool"
    obs_assets = store.get_assets_by_name(f"_tool_obs_{contract.id}")
    assert len(obs_assets) == 1

    parent_calls = [call for call in worker.calls if call[0].id == contract.id]
    assert len(parent_calls) == 2
    second_scope_names = {asset.name for asset in parent_calls[1][1]}
    assert f"_tool_obs_{contract.id}" in second_scope_names

    resumed = trace_store.get_by_event_type("method_resumed")
    assert len(resumed) == 1
    assert resumed[0].relation_type == "tool"
    assert resumed[0].relation_target != contract.id
