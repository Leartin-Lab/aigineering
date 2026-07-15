"""Engine-as-Worker isolation and outer-boundary conformance."""

from __future__ import annotations

import json

from aigineering.agent.engine_worker import EngineWorker
from aigineering.agent.mock import MockWorker
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Asset, Contract


def test_engine_worker_exports_only_declared_candidate_outputs():
    store = SQLiteStore(":memory:")
    ingress = RuntimeIngress(store, store)
    evidence = ingress.accept_asset(
        Asset(id="asset:evidence", name="evidence", content="source material"),
        source="test",
    )
    contract = Contract(
        id="task:outer",
        name="nested_task",
        inputs=("evidence",),
        outputs=("report",),
        activation="evidence",
        budget=2,
    )
    ingress.accept_contract(contract)
    delegate = MockWorker(
        {"nested_task": '/exec {"outputs": {"report": "isolated result"}}'}
    )
    worker = EngineWorker(delegate)
    preview = worker.invoke(contract, [evidence])

    assert preview.parsed_action == {
        "type": "exec",
        "outputs": {"report": "isolated result"},
    }
    assert "trace" not in preview.raw_output
    assert "asset:evidence" not in preview.raw_output

    claimed = claim_next_package(
        store,
        worker_id="engine_worker:nested",
        contract_id=contract.id,
    )
    assert claimed is not None
    result = execute_claimed_package(claimed, worker, store)

    assert result["status"] == "accepted"
    committed = store.get_assets_by_name("report")
    assert len(committed) == 1
    assert committed[0].content == "isolated result"
    assert committed[0].created_by == contract.id
    assert json.loads(preview.raw_output)["outputs"] == {"report": "isolated result"}
    store.close()


def test_engine_worker_unfinished_inner_run_fails_visibly_at_outer_boundary():
    store = SQLiteStore(":memory:")
    contract = Contract(id="task:outer-failure", name="nested", outputs=("report",))
    RuntimeIngress(store, store).accept_contract(contract)
    worker = EngineWorker(MockWorker(), max_steps=1)
    claimed = claim_next_package(
        store,
        worker_id="engine_worker:nested",
        contract_id=contract.id,
    )
    assert claimed is not None

    result = execute_claimed_package(claimed, worker, store)

    assert result["status"] == "rejected"
    assert store.get_assets_by_name("report") == []
    terminal = store.scan_runtime_records(record_type="lifecycle.terminal")
    assert terminal[-1][1].payload["terminal"] == "failed"
    store.close()
