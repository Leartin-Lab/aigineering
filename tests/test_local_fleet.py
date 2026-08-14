"""Local heterogeneous fleet tests over independent SQLite connections."""

from __future__ import annotations

import json
from threading import Barrier

from aigineering.cli._candidate import commit_local_effects, require_accepted
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.ids import hash_contract_current
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.fleet_config import load_fleet_config
from aigineering.local_fleet import FleetHost, run_local_fleet
from aigineering.local_identity import ensure_local_domain, ensure_local_worker_host
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Candidate, Contract


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
