"""Engine-as-Worker isolation and outer-boundary conformance."""

from __future__ import annotations

import json
from conftest import candidate_runtime, hosted_worker
import pytest

from aigineering.agent.engine_worker import EngineWorker
from aigineering.agent.mock import MockWorker
from aigineering.agent.worker import WorkerHost
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.runtime import submit_worker_proposal
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import CandidateClaimBinding
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.types import Asset, Contract


def test_engine_worker_exports_only_declared_candidate_outputs():
    store = SQLiteStore(":memory:")
    ingress = candidate_runtime(store)
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
    contract = ingress.accept_contract(contract)
    delegate = MockWorker(
        {"nested_task": '/exec {"outputs": {"report": "isolated result"}}'}
    )
    worker = EngineWorker(delegate)
    host = hosted_worker(
        store,
        worker,
        genesis=ingress.genesis,
        authority_key=ingress.actor_key,
        authority_signer=ingress.signer,
    )
    preview = worker.invoke(contract, [evidence])

    assert preview.parsed_action == {
        "type": "exec",
        "outputs": {"report": "isolated result"},
    }
    assert "trace" not in preview.raw_output
    assert "asset:evidence" not in preview.raw_output

    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
    )
    assert claimed is not None
    result = execute_claimed_package(claimed, host, store)

    assert not result["rejected"], result["rejected"]
    assert result["status"] == "accepted", result
    committed = store.get_assets_by_name("report")
    assert len(committed) == 1
    assert committed[0].content == "isolated result"
    assert committed[0].created_by == contract.id
    assert json.loads(preview.raw_output)["outputs"] == {"report": "isolated result"}
    store.close()


def test_engine_worker_unfinished_inner_run_fails_visibly_at_outer_boundary():
    store = SQLiteStore(":memory:")
    contract = Contract(
        id="task:outer-failure", name="nested", outputs=("report",), budget=1
    )
    runtime = candidate_runtime(store)
    contract = runtime.accept_contract(contract)
    worker = EngineWorker(MockWorker(), max_steps=1)
    host = hosted_worker(
        store,
        worker,
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    claimed = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=contract.id,
    )
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert result["status"] == "rejected"
    assert store.get_assets_by_name("report") == []
    terminal = store.scan_runtime_records(record_type="lifecycle.terminal")
    assert terminal[-1][1].payload["terminal"] == "failed"
    store.close()


def test_engine_worker_restarts_from_inner_facts_and_fences_late_outer_result(
    tmp_path,
):
    outer = SQLiteStore(":memory:")
    runtime = candidate_runtime(outer)
    evidence = runtime.accept_asset(
        Asset(id="bridge:evidence", name="evidence", content="durable input")
    )
    root = runtime.accept_contract(
        Contract(
            id="bridge:root",
            name="durable_bridge",
            inputs=("evidence",),
            outputs=("report",),
            activation="evidence",
            budget=2,
        )
    )
    inner_path = str(tmp_path / "engine-worker-inner.db")
    inner_private_key = Ed25519Signer().private_key_hex
    first = EngineWorker(
        MockWorker({"durable_bridge": '/exec {"outputs":{"report":"inner result"}}'}),
        inner_store_factory=lambda: SQLiteStore(inner_path),
        inner_signer=Ed25519Signer.from_private_key_hex(inner_private_key),
    )
    host = hosted_worker(
        outer,
        first,
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    claimed = claim_next_package(outer, worker_id=host.worker_id, contract_id=root.id)
    assert claimed is not None
    binding = CandidateClaimBinding(
        contract_id=root.id,
        claim_id=claimed.package.claim_id,
        claim_epoch=claimed.package.claim_epoch,
        package_id=claimed.package.package_id,
    )
    inner_result = first.invoke_claimed(root, [evidence], binding)
    envelope = CandidateEnvelope(
        contract_id=root.id,
        worker_id=host.worker_id,
        raw_output=inner_result.raw_output,
        parsed_action=inner_result.parsed_action,
        claim_id=binding.claim_id,
        claim_epoch=binding.claim_epoch,
        package_id=binding.package_id,
        idempotency_key=f"run-{binding.package_id}",
    )
    stale = host.sign_envelope(envelope, contract=root)
    outer.mark_claim_released(binding.claim_id)

    with pytest.raises(ValueError, match="active worker claim predicate failed"):
        submit_worker_proposal(stale, outer)
    assert outer.get_assets_by_name("report") == []

    replacement = runtime.accept_contract(
        Contract(
            id="bridge:replacement",
            parent_id=root.id,
            name=root.name,
            description=root.description,
            inputs=root.inputs,
            outputs=root.outputs,
            activation=root.activation,
            budget=root.budget,
        )
    )

    class _MustNotRun:
        worker_id = "mock_worker"

        def invoke(self, contract, disclosed_assets):
            raise AssertionError("accepted inner facts should be reused after restart")

    restarted = EngineWorker(
        _MustNotRun(),
        inner_store_factory=lambda: SQLiteStore(inner_path),
        inner_signer=Ed25519Signer.from_private_key_hex(inner_private_key),
    )
    restarted_host = WorkerHost(restarted, host.genesis, host.actor_key, host.signer)
    replacement_claim = claim_next_package(
        outer,
        worker_id=restarted_host.worker_id,
        contract_id=replacement.id,
    )
    assert replacement_claim is not None

    result = execute_claimed_package(replacement_claim, restarted_host, outer)

    assert result["status"] == "accepted"
    assert outer.get_assets_by_name("report")[0].content == "inner result"
    inner = SQLiteStore(inner_path)
    assert (
        len(
            [
                asset
                for asset in inner.get_all_assets()
                if asset.name.startswith("bridge_operation:")
            ]
        )
        == 2
    )
    assert len(inner.get_assets_by_name("report")) == 1
    inner.close()
    outer.close()
