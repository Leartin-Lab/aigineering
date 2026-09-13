"""End-to-end method validation through the local Fleet boundary."""

import json
from pathlib import Path

from aigineering.cli._candidate import commit_local_effects, require_accepted
from aigineering.core.control_plane import build_control_plane_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.ids import hash_contract_current
from aigineering.fleet_config import build_fleet_worker, load_fleet_config
from aigineering.local_fleet import FleetHost, run_local_fleet
from aigineering.local_identity import (
    ensure_local_domain,
    ensure_local_plugin_publisher,
    ensure_local_worker_host,
    ensure_local_runtime_publishers,
)
from aigineering.methods.service import MethodService
from aigineering.protocol.effect_builders import asset_proposal_effect
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Contract
from aigineering.business.claim_check_worker import create_structural_gate_worker
from aigineering.plugins import default_completion_registry
from aigineering.runtime import (
    claim_next_package,
    execute_claimed_package,
    run_runtime_maintenance_step,
)


ROOT = Path(__file__).parents[1]


def _publish(store, asset):
    return require_accepted(
        commit_local_effects(
            store,
            (asset_proposal_effect(asset),),
            idempotency_key=f"test:{asset.id}",
        )
    ).assets[0]


def test_claim_validation_methods_run_in_local_fleet_and_rebuild(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "store.db"
    store = SQLiteStore(str(db))
    ensure_local_domain(store)
    service = MethodService(
        store, lambda effects, **kw: commit_local_effects(store, effects, **kw)
    )
    try:
        pkg_dir = ROOT / "examples" / "claim-validation"
        original = service.import_package(
            (pkg_dir / "original-text.method.json").read_text()
        )
        suite = service.import_cases(
            (pkg_dir / "original-text.cases.json").read_text(), "original-cases"
        )
        run, root = service.prepare_tests(
            original.id, suite.id, name="original", budget=1
        )
        config = load_fleet_config(pkg_dir / "workers.toml")
        hosts = tuple(
            FleetHost(
                ensure_local_worker_host(store, build_fleet_worker(spec)),
                capacity=spec.capacity,
            )
            for spec in config.workers
            if "claim.check.original-text" in spec.capabilities
        )
        result = run_local_fleet(
            str(db), hosts, target_contract_id=root.id, timeout=10, poll_interval=0.01
        )
        assert result.completed, result
        evaluation, _ = service.assess(run.id)
        assert json.loads(evaluation.content)["passed"] is True
        publisher = ensure_local_plugin_publisher(
            store, "method.review.v1", ("asset.attest", "method.verify")
        )
        assert require_accepted(
            publisher.publish(
                (service.attestation_effect(evaluation.id),),
                idempotency_key=f"test-attest:{evaluation.id}",
            )
        ).accepted
        source = _publish(
            store,
            build_control_plane_asset(
                name="source", content=(pkg_dir / "gate-source.json").read_text()
            ),
        )
        report = _publish(
            store,
            build_control_plane_asset(
                name="report", content=(pkg_dir / "gate-report.json").read_text()
            ),
        )
        reused = service.instantiate(
            original.id,
            inputs={"source": source.id, "report": report.id},
            outputs={"assessment": "assessment"},
            name="reuse",
            budget=1,
            evaluation_id=evaluation.id,
        )
        reused_result = run_local_fleet(
            str(db), hosts, target_contract_id=reused.id, timeout=3, poll_interval=0.01
        )
        assert reused_result.completed
        assert json.loads(store.get_assets_by_name("assessment")[0].content)["passed"]
        before = store.runtime_materialization_digest()
        assert store.rebuild_runtime_materializations() == before
    finally:
        store.close()


def test_structural_gate_failure_is_durable_and_has_no_receipt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "gate.db"
    store = SQLiteStore(str(db))
    ensure_local_domain(store)
    try:
        _publish(
            store,
            build_control_plane_asset(
                name="research_result",
                content=json.dumps(
                    {
                        "claims": [{"id": "c1", "text": "original"}],
                        "source_excerpts": [{"id": "e1", "text": "original"}],
                    }
                ),
            ),
        )
        _publish(
            store,
            build_control_plane_asset(
                name="review_report",
                content=json.dumps(
                    {
                        "claims": [
                            {
                                "claim_id": "c1",
                                "original_text": "replaced",
                                "evidence_excerpt_ids": ["e1"],
                                "evidence_bindings": [
                                    {"excerpt_id": "e1", "quote": "original"}
                                ],
                            }
                        ]
                    }
                ),
            ),
        )
        fields = {
            "name": "structural_gate",
            "description": "Fail closed on structural claim defects.",
            "inputs": ("research_result", "review_report"),
            "outputs": ("receipt",),
            "activation": "",
            "budget": 1,
            "tool_scope": (),
            "labels": (),
            "context_asset_ids": (),
            "worker_capabilities": ("claim.check.gate",),
            "worker_pools": (),
            "origin": "human",
            "parent_id": None,
        }
        contract = Contract(id=hash_contract_current(**fields), **fields)
        require_accepted(
            commit_local_effects(
                store,
                (contract_declaration_effect(contract),),
                idempotency_key="gate-contract",
            )
        )
        ensure_local_runtime_publishers(store)
        host = ensure_local_worker_host(
            store, create_structural_gate_worker(worker_id="claim-gate")
        )
        claimed = claim_next_package(
            store, worker_id=host.worker_id, contract_id=contract.id
        )
        assert claimed is not None
        execute_claimed_package(claimed, host, store)
        run_runtime_maintenance_step(
            store,
            default_completion_registry(),
            candidate_publishers=ensure_local_runtime_publishers(store),
        )
        failures = [
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("contract_id") == contract.id
            and str(record.payload.get("raw_output", "")).startswith("/fail ")
        ]
        assert failures
        result = run_local_fleet(
            str(tmp_path / "gate.db"),
            (FleetHost(host),),
            target_contract_id=contract.id,
            timeout=3,
            poll_interval=0.01,
        )
        assert result.status == "failed"
        assert not result.timed_out
        assert len(store.get_all_contracts()) == 2
        assert not store.get_assets_by_name("receipt")
    finally:
        store.close()
