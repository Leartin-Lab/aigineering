"""Local heterogeneous fleet tests over independent SQLite connections."""

from __future__ import annotations

import json
from threading import Barrier

from aigineering.cli._candidate import commit_local_effects, require_accepted
from aigineering.core.control_plane import (
    bind_contract_label_assets,
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.ids import hash_contract_current
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tools import ToolRegistry
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.fleet_config import load_fleet_config
from aigineering.local_fleet import FleetHost, run_local_fleet
from aigineering.local_identity import ensure_local_domain, ensure_local_worker_host
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Candidate, Contract, ToolSpec
from aigineering.agent.tool_worker import ToolWorker


class _ConcurrentWorker:
    def __init__(self, worker_id: str, capability: str, barrier: Barrier) -> None:
        self.worker_id = worker_id
        self.capability = capability
        self.barrier = barrier

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(
            self.worker_id,
            capabilities=(self.capability,),
            capacity=1,
            profile_id=f"test:{self.capability}",
        )

    def invoke(self, contract: Contract, disclosed_assets: list) -> Candidate:
        del disclosed_assets
        self.barrier.wait(timeout=2)
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps(
                {
                    "outputs": {
                        name: f"{self.worker_id}:{name}" for name in contract.outputs
                    }
                }
            ),
        )


class _RepairingWorker:
    def __init__(self) -> None:
        self.worker_id = "worker:repairing"
        self.disclosures: list[tuple[str, ...]] = []

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(self.worker_id, capabilities=("text.extract",))

    def invoke(self, contract: Contract, disclosed_assets: list) -> Candidate:
        self.disclosures.append(tuple(asset.name for asset in disclosed_assets))
        if contract.origin != "recovery":
            return Candidate(
                worker_id=self.worker_id,
                raw_output='/exec {"outputs":{"wrong_name":"malformed"}}',
            )
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps(
                {"outputs": {name: "repaired with skill" for name in contract.outputs}}
            ),
        )


class _ParallelToolOrchestrator:
    worker_id = "worker:parallel-orchestrator"

    def __init__(self) -> None:
        self.disclosures: list[tuple[str, ...]] = []

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(self.worker_id, capabilities=("orchestrate",))

    def invoke(self, contract: Contract, disclosed_assets: list) -> Candidate:
        names = tuple(asset.name for asset in disclosed_assets)
        self.disclosures.append(names)
        if contract.origin != "continuation":
            return Candidate(
                worker_id=self.worker_id,
                raw_output="/parallel_tool "
                + json.dumps(
                    {
                        "calls": [
                            {"id": "search", "name": "search", "args": {"q": "a"}},
                            {"id": "lookup", "name": "lookup", "args": {"q": "b"}},
                        ],
                        "join": "all",
                    }
                ),
            )
        return Candidate(
            worker_id=self.worker_id,
            raw_output='/exec {"outputs":{"report":"joined observations"}}',
        )


def test_fleet_config_parses_explicit_worker_profiles(tmp_path):
    config = tmp_path / "workers.toml"
    config.write_text(
        """
[fleet]
db_path = ".aig/test.db"
poll_interval = 0.05

[[workers]]
id = "cheap"
kind = "llm"
model = "cheap-model"
capabilities = ["text.extract"]
pools = ["economy"]
capacity = 4
max_output_tokens = 3072
thinking_mode = "disabled"
effect_capabilities = ["asset.attest"]

[[workers]]
id = "tools"
kind = "tool"
tool_registry = "examples.ai4s.tools:build_registry"
capacity = 2
""".strip(),
        encoding="utf-8",
    )

    parsed = load_fleet_config(config)

    assert parsed.poll_interval == 0.05
    assert parsed.workers[0].worker_id == "cheap"
    assert parsed.workers[0].capabilities == ("text.extract",)
    assert parsed.workers[0].capacity == 4
    assert parsed.workers[0].max_output_tokens == 3072
    assert parsed.workers[0].thinking_mode == "disabled"
    assert parsed.workers[0].effect_capabilities == ("asset.attest",)
    assert parsed.workers[1].kind == "tool"


def test_local_fleet_executes_specialized_tasks_concurrently(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_path = str(tmp_path / "fleet.db")
    store = SQLiteStore(db_path)
    ensure_local_domain(store)
    root = build_control_plane_contract(
        name="root",
        outputs=("cheap_result", "deep_result"),
        budget=2,
        worker_capabilities=("root.orchestrator",),
        delegation_capabilities=("text.extract", "reasoning.deep"),
    )

    def child(name: str, output: str, capability: str) -> Contract:
        fields = {
            "name": name,
            "description": f"Produce {output}",
            "inputs": (),
            "outputs": (output,),
            "activation": "",
            "budget": 1,
            "tool_scope": (),
            "labels": (),
            "worker_capabilities": (capability,),
            "worker_pools": (),
            "origin": "plan",
            "parent_id": root.id,
        }
        return Contract(id=hash_contract_current(**fields), **fields)

    cheap_task = child("cheap", "cheap_result", "text.extract")
    deep_task = child("deep", "deep_result", "reasoning.deep")
    require_accepted(
        commit_local_effects(
            store,
            tuple(
                contract_declaration_effect(item)
                for item in (root, cheap_task, deep_task)
            ),
            idempotency_key="fleet-concurrency-setup",
        )
    )
    barrier = Barrier(2)
    cheap = _ConcurrentWorker("worker:cheap", "text.extract", barrier)
    deep = _ConcurrentWorker("worker:deep", "reasoning.deep", barrier)
    hosts = tuple(
        FleetHost(ensure_local_worker_host(store, worker)) for worker in (cheap, deep)
    )
    store.close()

    result = run_local_fleet(
        db_path,
        hosts,
        target_contract_id=root.id,
        timeout=5,
        poll_interval=0.01,
    )

    assert result.completed is True
    assert result.status == "complete"
    reopened = SQLiteStore(db_path)
    try:
        assert reopened.get_assets_by_name("cheap_result")
        assert reopened.get_assets_by_name("deep_result")
    finally:
        reopened.close()


def test_local_fleet_recovers_bad_output_with_frozen_skill_context(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    db_path = str(tmp_path / "recovery-fleet.db")
    store = SQLiteStore(db_path)
    ensure_local_domain(store)
    skill = build_control_plane_asset(
        name="_skill_content_extract_evidence",
        content="Always emit exactly the declared evidence output.",
        origin="skill",
        trust_tier="configured",
        allow_protected=True,
    )
    require_accepted(
        commit_local_effects(
            store,
            (asset_proposal_effect(skill),),
            idempotency_key="fleet-recovery-skill",
        )
    )
    stored_skill = store.get_assets_by_name(skill.name)[0]
    root = build_control_plane_contract(
        name="recoverable_root",
        outputs=("evidence",),
        budget=2,
        labels=(skill.name,),
        context_asset_ids=(stored_skill.id,),
        worker_capabilities=("root.orchestrator",),
        delegation_capabilities=("text.extract",),
    )
    fields = {
        "name": "extract_evidence",
        "description": "Extract one evidence fact using the bound Skill.",
        "inputs": (),
        "outputs": ("evidence",),
        "activation": "",
        "budget": 1,
        "tool_scope": (),
        "labels": (skill.name,),
        "context_asset_ids": (stored_skill.id,),
        "worker_capabilities": ("text.extract",),
        "worker_pools": (),
        "origin": "plan",
        "parent_id": root.id,
    }
    task = Contract(id=hash_contract_current(**fields), **fields)
    task = bind_contract_label_assets(task, store)
    require_accepted(
        commit_local_effects(
            store,
            tuple(contract_declaration_effect(item) for item in (root, task)),
            idempotency_key="fleet-recovery-tasks",
        )
    )
    worker = _RepairingWorker()
    host = FleetHost(ensure_local_worker_host(store, worker))
    store.close()

    result = run_local_fleet(
        db_path,
        (host,),
        target_contract_id=root.id,
        timeout=5,
        poll_interval=0.01,
    )

    assert result.completed is True
    reopened = SQLiteStore(db_path)
    try:
        recovery = next(
            contract
            for contract in reopened.get_all_contracts()
            if contract.origin == "recovery"
        )
        assert recovery.labels == task.labels
        assert recovery.context_asset_ids == task.context_asset_ids
        assert reopened.get_assets_by_name("evidence")[0].created_by == recovery.id
        assert reopened.scan_runtime_records(record_type="candidate.rejected")
        assert reopened.scan_runtime_records(
            record_type="candidate_rejection.recovery_scheduled"
        )
    finally:
        reopened.close()
    assert any(skill.name in names for names in worker.disclosures)
    assert any(
        skill.name in names and any(name.startswith("_fail_context_") for name in names)
        for names in worker.disclosures
    )


def test_parallel_tool_method_runs_calls_concurrently_then_joins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_path = str(tmp_path / "parallel-tools.db")
    store = SQLiteStore(db_path)
    ensure_local_domain(store)
    registry = ToolRegistry()
    barrier = Barrier(2)

    def tool_result(args):
        barrier.wait(timeout=2)
        return f"result:{args['q']}"

    for name in ("search", "lookup"):
        registry.register(
            ToolSpec(name=name, description=name, input_schema={"type": "object"}),
            tool_result,
        )
    descriptors = tuple(
        create_tool_descriptor(name, name, {"type": "object"})
        for name in ("search", "lookup")
    )
    require_accepted(
        commit_local_effects(
            store,
            tuple(asset_proposal_effect(asset) for asset in descriptors),
            idempotency_key="parallel-tool-descriptors",
        )
    )
    root = build_control_plane_contract(
        name="parallel_research",
        outputs=("report",),
        budget=4,
        tool_scope=("search", "lookup"),
        worker_capabilities=("orchestrate",),
    )
    require_accepted(
        commit_local_effects(
            store,
            (contract_declaration_effect(root),),
            idempotency_key="parallel-tool-root",
        )
    )
    orchestrator = _ParallelToolOrchestrator()
    tool_worker = ToolWorker(registry, worker_id="worker:parallel-tools", capacity=2)
    hosts = (
        FleetHost(ensure_local_worker_host(store, orchestrator)),
        FleetHost(ensure_local_worker_host(store, tool_worker), capacity=2),
    )
    store.close()

    result = run_local_fleet(
        db_path,
        hosts,
        target_contract_id=root.id,
        timeout=5,
        poll_interval=0.01,
    )

    assert result.completed is True
    reopened = SQLiteStore(db_path)
    try:
        tool_items = [
            contract
            for contract in reopened.get_all_contracts()
            if "plugin:parallel_tool_item" in contract.labels
        ]
        assert len(tool_items) == 2
        assert all(
            RuntimeProjection(reopened, reopened).contract_view(item).terminal
            == "complete"
            for item in tool_items
        )
        continuation = next(
            contract
            for contract in reopened.get_all_contracts()
            if "plugin:parallel_tool.continuation" in contract.labels
        )
        assert continuation.activation == " AND ".join(continuation.inputs)
        assert reopened.get_assets_by_name("report")
    finally:
        reopened.close()
    assert any(
        len([name for name in names if name.startswith("tool_observation_")]) == 2
        for names in orchestrator.disclosures
    )
