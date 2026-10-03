"""Quick-run output and claim isolation at the CLI boundary."""

import json
from click.testing import CliRunner

from aigineering.agent.mock import MockWorker
from aigineering.cli._common import _claim_next_root_tree_package
from aigineering.cli.main import cli
from aigineering.cli.task_state import project_task_status
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.candidate_publisher import CandidatePublisherRegistry
from aigineering.core.session import SessionStore
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Candidate
from aigineering.runtime import execute_claimed_package, process_rejected_submissions
from conftest import candidate_runtime, hosted_worker


def test_repeated_quick_runs_publish_independent_outputs_and_rebuild(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        results = [
            runner.invoke(cli, ["run", goal, "--worker", "mock", "--json"])
            for goal in ("same goal", "same goal", "different goal")
        ]
        assert [result.exit_code for result in results] == [0, 0, 0], [
            result.output for result in results
        ]
        payloads = [json.loads(result.output) for result in results]

        store = SQLiteStore(".aig/store.db")
        roots = [store.get_contract(payload["contract_id"]) for payload in payloads]
        assert all(root is not None for root in roots)
        root_contracts = [root for root in roots if root is not None]
        output_names = [root.outputs[0] for root in root_contracts]
        assert len(set(output_names)) == len(output_names)
        assert all(name.startswith("final_report_") for name in output_names)
        assert len({root.id for root in root_contracts}) == len(root_contracts)

        sessions_by_id = {
            session.id: session for session in SessionStore().list_sessions()
        }
        for payload, root in zip(payloads, root_contracts, strict=True):
            session = sessions_by_id[payload["session_id"]]
            assets = store.get_assets_by_name(root.outputs[0])
            assert assets
            assert any(asset.created_by == root.id for asset in assets)
            assert {asset.id for asset in assets} <= set(session.produced_asset_ids)
            assert payload["trace_ids"]
            assert session.produced_asset_ids

        first_produced = set(
            sessions_by_id[payloads[0]["session_id"]].produced_asset_ids
        )
        assert all(
            first_produced.isdisjoint(
                sessions_by_id[payload["session_id"]].produced_asset_ids
            )
            for payload in payloads[1:]
        )
        digest = store.runtime_materialization_digest()
        store.close()

        reopened = SQLiteStore(".aig/store.db")
        try:
            assert reopened.rebuild_runtime_materializations() == digest
            for root in root_contracts:
                assert reopened.get_assets_by_name(root.outputs[0])
        finally:
            reopened.close()


def test_old_fixed_output_and_unrelated_ready_task_do_not_satisfy_or_get_claimed(
    tmp_path,
):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        old_asset = runner.invoke(
            cli,
            ["asset", "add", "--name", "final_report", "--content", "historical"],
        )
        assert old_asset.exit_code == 0, old_asset.output
        unrelated = runner.invoke(
            cli,
            [
                "task",
                "create",
                "--name",
                "unrelated_ready",
                "--output",
                "unrelated_result",
                "--json",
            ],
        )
        assert unrelated.exit_code == 0, unrelated.output
        unrelated_id = json.loads(unrelated.output)["contract_id"]

        result = runner.invoke(
            cli, ["run", "isolated goal", "--worker", "mock", "--json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)

        store = SQLiteStore(".aig/store.db")
        try:
            root = store.get_contract(payload["contract_id"])
            assert root is not None
            assert root.outputs[0] != "final_report"
            assert store.get_assets_by_name("final_report")
            assert any(
                asset.created_by == root.id
                for asset in store.get_assets_by_name(root.outputs[0])
            )
            claim_contract_ids = {
                str(record.payload.get("contract_id", ""))
                for _, record in store.scan_runtime_records(record_type="claim.granted")
            }
            assert root.id in claim_contract_ids
            assert unrelated_id not in claim_contract_ids
            unrelated_contract = store.get_contract(unrelated_id)
            assert unrelated_contract is not None
            assert project_task_status(unrelated_contract, store)["status"] == "ready"
            assert not store.get_claim(unrelated_id)
        finally:
            store.close()


def test_quick_run_preserves_root_failure_and_publishes_recovery_successor(
    monkeypatch, tmp_path
):
    class RejectingWorker:
        worker_id = "quick-run-rejecting-worker"

        def invoke(self, contract, disclosed_assets):
            del disclosed_assets
            return Candidate(
                worker_id=self.worker_id,
                raw_output="undeclared_output: preserve this rejection",
            )

    worker = RejectingWorker()
    monkeypatch.setattr(
        "aigineering.cli._common.build_worker", lambda *args, **kwargs: worker
    )
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            cli, ["run", "failed quick goal", "--worker", "mock", "--json"]
        )
        assert result.exit_code == 1, result.output
        assert "did not complete declared outputs" in result.output
        # Click renders a concise error, while runtime evidence remains durable.
        store = SQLiteStore(".aig/store.db")
        try:
            roots = [
                contract
                for contract in store.get_all_contracts()
                if contract.name == "build_report"
            ]
            assert len(roots) == 1
            root = roots[0]
            assert project_task_status(root, store)["status"] == "failed"
            rejected = [
                record
                for _, record in store.scan_runtime_records(
                    record_type="candidate.rejected"
                )
                if record.payload.get("contract_id") == root.id
            ]
            assert rejected
            recoveries = [
                contract
                for contract in store.get_all_contracts()
                if contract.origin == "recovery"
            ]
            assert len(recoveries) == 1
            assert recoveries[0].parent_id is None
            claimed_ids = {
                str(record.payload.get("contract_id", ""))
                for _, record in store.scan_runtime_records(record_type="claim.granted")
            }
            assert claimed_ids == {root.id}
        finally:
            store.close()


def test_quick_run_claim_selection_executes_expansion_and_recovery_descendants(
    temp_sqlite_store,
):
    setup = candidate_runtime(temp_sqlite_store)
    root = setup.accept_contract(
        build_control_plane_contract(
            name="quick_root",
            outputs=("root_result",),
            activation="unavailable_input",
            budget=5,
        )
    )
    child = setup.accept_contract(
        contract_from_fields(
            name="quick_expanded_child",
            description="A reachable ordinary child.",
            inputs=(),
            outputs=("root_result",),
            activation="",
            budget=2,
            tool_scope=(),
            labels=(),
            origin="human",
            parent_id=root.id,
        )
    )
    unrelated = setup.accept_contract(
        build_control_plane_contract(
            name="unrelated_ready",
            outputs=("unrelated_result",),
            budget=3,
        )
    )
    worker = MockWorker(worker_id="quick-run-test-worker")
    host = hosted_worker(
        temp_sqlite_store,
        worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    publishers = CandidatePublisherRegistry((("recovery.publish.v1", setup.publisher),))
    worker.set_output(child.name, "wrong_output: rejected on purpose")

    claimed = _claim_next_root_tree_package(
        temp_sqlite_store,
        worker_id=host.worker_id,
        root_contract_id=root.id,
        candidate_publishers=publishers,
    )
    assert claimed is not None
    assert claimed.contract.id == child.id
    rejected = execute_claimed_package(claimed, host, temp_sqlite_store)
    assert rejected["status"] == "rejected"

    assert process_rejected_submissions(
        temp_sqlite_store, candidate_publishers=publishers
    ) == [child.id]
    recoveries = [
        contract
        for contract in temp_sqlite_store.get_all_contracts()
        if contract.origin == "recovery" and contract.parent_id == root.id
    ]
    assert len(recoveries) == 1
    recovery = recoveries[0]
    worker.set_output(recovery.name, "root_result: recovered")

    recovered_claim = _claim_next_root_tree_package(
        temp_sqlite_store,
        worker_id=host.worker_id,
        root_contract_id=root.id,
        candidate_publishers=publishers,
    )
    assert recovered_claim is not None
    assert recovered_claim.contract.id == recovery.id
    assert (
        execute_claimed_package(recovered_claim, host, temp_sqlite_store)["status"]
        == "accepted"
    )

    claim_contract_ids = {
        str(record.payload.get("contract_id", ""))
        for _, record in temp_sqlite_store.scan_runtime_records(
            record_type="claim.granted"
        )
    }
    assert child.id in claim_contract_ids
    assert recovery.id in claim_contract_ids
    assert unrelated.id not in claim_contract_ids
    assert not temp_sqlite_store.get_claim(unrelated.id)
    assert project_task_status(unrelated, temp_sqlite_store)["status"] == "ready"
    assert any(
        asset.created_by == recovery.id
        for asset in temp_sqlite_store.get_assets_by_name("root_result")
    )
    root_view = project_task_status(root, temp_sqlite_store)
    assert root_view["status"] == "completed"
