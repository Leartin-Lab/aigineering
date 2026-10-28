from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from click.testing import CliRunner

from aigineering import fleet_config
from aigineering.cli.main import cli
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.ids import compute_content_hash
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.immutability import deep_thaw
from aigineering.task_lineage import task_lineage
from aigineering.task_views import project_task_status

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "thermal_calibration"
EXPECTED_REDIS = "redis://127.0.0.1:32770/4"


def _configure(tmp_path, monkeypatch, scenario):
    redis_url = os.environ.get("AIG_REDIS_TEST_URL", EXPECTED_REDIS)
    monkeypatch.setenv("AIGINEERING_REDIS_URL", redis_url)
    monkeypatch.setenv("AIG_REDIS_TEST_URL", redis_url)
    tests_path = str(ROOT / "tests")
    repo_path = str(ROOT)
    monkeypatch.syspath_prepend(tests_path)
    monkeypatch.syspath_prepend(repo_path)
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(item for item in (repo_path, tests_path, existing) if item),
    )

    def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("provider Worker construction is forbidden in F1c")

    monkeypatch.setattr(fleet_config, "LLMWorker", forbidden_provider)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    preset = tmp_path / "preset"
    shutil.copytree(EXAMPLE, preset)
    planner_factory = "fixtures.thermal_calibration_workers:build_calibration_worker"
    researcher_factory = "fixtures.thermal_calibration_workers:build_calibration_worker"
    verifier_factory = (
        "fixtures.thermal_calibration_cli_failures:build_failure_boundary_verifier"
    )
    if scenario == "malformed-root":
        planner_factory = (
            "fixtures.thermal_calibration_cli_failures:build_malformed_root_worker"
        )
    elif scenario == "missing-observation":
        planner_factory = (
            "fixtures.thermal_calibration_cli_failures:build_verifier_fail_planner"
        )
        researcher_factory = (
            "fixtures.thermal_calibration_cli_failures:build_missing_observation_worker"
        )
        verifier_factory = "examples.thermal_calibration.workers:build_verifier_worker"
    elif scenario == "budget-exhaustion":
        planner_factory = (
            "fixtures.thermal_calibration_cli_failures:build_two_unit_planner"
        )
    else:
        raise AssertionError(f"unknown fixture scenario: {scenario}")

    worker_config = f'''[fleet]
db_path = ".aig/store.db"
poll_interval = 0.02

[[workers]]
id = "test-calibration-planner"
kind = "local"
worker_factory = "{planner_factory}"
capabilities = ["calibration.plan"]
pools = ["planning"]

[[workers]]
id = "test-calibration-researcher"
kind = "local"
worker_factory = "{researcher_factory}"
capabilities = ["calibration.research"]
pools = ["research"]

[[workers]]
id = "calibration-tools"
kind = "tool"
tool_registry = "examples.thermal_calibration.tools:build_registry"

[[workers]]
id = "test-calibration-verifier"
kind = "local"
worker_factory = "{verifier_factory}"
capabilities = ["calibration.verify"]
pools = ["verification"]
effect_capabilities = ["asset.attest", "calibration.verify"]
'''
    workers_path = preset / "workers.toml"
    workers_path.write_text(worker_config, encoding="utf-8")
    config = preset / "run.toml"
    return workspace, config, workers_path


def _run(workspace, config, evidence_dir, *, timeout=8):
    result = CliRunner().invoke(
        cli,
        [
            "-C",
            str(workspace),
            "run",
            "--config",
            str(config),
            "--wait-timeout",
            str(timeout),
            "--json",
        ],
    )
    _save_result(result, evidence_dir / "run")
    return result


def _save_result(result, directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stdout.txt").write_text(
        getattr(result, "stdout", result.output), encoding="utf-8"
    )
    (directory / "stderr.txt").write_text(
        getattr(result, "stderr", ""), encoding="utf-8"
    )
    (directory / "exit.txt").write_text(f"{result.exit_code}\n", encoding="ascii")
    (directory / "exception_type.txt").write_text(
        type(result.exception).__name__ if result.exception else "none",
        encoding="ascii",
    )


def _all_records(store):
    return [record for _revision, record in store.scan_runtime_records()]


def _semantic_snapshot(store):
    records = tuple(
        (
            revision,
            record.id,
            record.record_type,
            tuple(record.causal_parents),
            json.dumps(
                deep_thaw(record.payload), sort_keys=True, separators=(",", ":")
            ),
        )
        for revision, record in store.scan_runtime_records()
    )
    assets = tuple(
        sorted(
            (
                asset.id,
                asset.name,
                asset.content,
                asset.created_by,
                asset.signed_by,
                asset.origin,
            )
            for asset in store.get_all_assets()
        )
    )
    contracts = tuple(
        sorted(
            (
                contract.id,
                contract.name,
                contract.parent_id,
                contract.origin,
                contract.budget,
                tuple(contract.inputs),
                tuple(contract.outputs),
                tuple(contract.labels),
                tuple(contract.tool_scope),
                contract.description,
            )
            for contract in store.get_all_contracts()
        )
    )
    return store.runtime_materialization_digest(), records, assets, contracts


def _contracts_by_id(store):
    return {contract.id: contract for contract in store.get_all_contracts()}


def _committed_asset_ids(store):
    return {
        record.payload["asset"]["id"]
        for _, record in store.scan_runtime_records(record_type="asset.committed")
    }


def _tool_observations(store):
    observations = {}
    committed_ids = _committed_asset_ids(store)
    for asset in store.get_all_assets():
        if not asset.name.startswith(("_tool_obs_", "tool_observation_")):
            continue
        try:
            value = json.loads(asset.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict) or value.get("tool") not in {
            "normalize_calibration",
            "analyze_calibration",
        }:
            continue
        observations.setdefault(value["tool"], []).append((asset, value))
    assert set(observations) == {"normalize_calibration", "analyze_calibration"}
    for tool_name, rows in observations.items():
        assert len(rows) == 1
        asset, value = rows[0]
        assert value["ok"] is True
        assert asset.origin == "worker"
        assert asset.signed_by == "calibration-tools"
        assert verify_asset_seal(asset)
        assert asset.id in committed_ids
        assert asset.created_by in _contracts_by_id(store)
    return {name: rows[0] for name, rows in observations.items()}


def _assert_readonly_rebuild(store, expected):
    snapshot = store.snapshot()
    try:
        assert snapshot.runtime_materialization_digest() == expected[0]
        assert snapshot.rebuild_runtime_materializations(strict=True) == expected[0]
    finally:
        snapshot.close()
    assert _semantic_snapshot(store) == expected


def _task_result(workspace, root_id, evidence_dir, destination):
    result = CliRunner().invoke(
        cli,
        [
            "-C",
            str(workspace),
            "task",
            "result",
            root_id,
            "--output",
            "calibration_report",
            "--to",
            str(destination),
            "--json",
        ],
    )
    _save_result(result, evidence_dir / "task-result")
    return result


def _assert_rejected_export_is_read_only(workspace, root_id, tmp_path, evidence_dir):
    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        before = _semantic_snapshot(store)
        root = store.get_contract(root_id)
        assert root is not None
        status = project_task_status(root, store, check_integrity=True)
        assert status["integrity"]["consistent"] is True
        _assert_readonly_rebuild(store, before)
    finally:
        store.close()

    destination = tmp_path / "not-accepted-calibration-report.json"
    result = _task_result(workspace, root_id, evidence_dir, destination)
    assert result.exit_code == 1, result.output
    envelope = json.loads(result.output)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "not_completed"
    assert not destination.exists()

    reopened = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        assert _semantic_snapshot(reopened) == before
    finally:
        reopened.close()


def test_malformed_root_candidate_schedules_but_does_not_execute_recovery(
    tmp_path, monkeypatch
):
    workspace, config, _workers = _configure(tmp_path, monkeypatch, "malformed-root")
    evidence_dir = tmp_path / "evidence"
    result = _run(workspace, config, evidence_dir)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["completed"] is False
    assert payload["status"] == "failed"
    root_id = payload["contract_id"]

    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        before = _semantic_snapshot(store)
        contracts = _contracts_by_id(store)
        root = contracts[root_id]
        assert root.origin == "human"
        failures = [
            record
            for _, record in store.scan_runtime_records(
                record_type="worker.invocation_failed"
            )
            if record.payload.get("contract_id") == root_id
            and record.payload.get("category")
            == "worker_error:candidate_encoding_failure"
        ]
        assert len(failures) == 1
        failure = failures[0]
        root_grants = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.granted")
            if record.payload.get("contract_id") == root_id
            and record.payload.get("worker_id") == "test-calibration-planner"
        ]
        assert len(root_grants) == 1
        grant = root_grants[0]
        assert failure.payload.get("claim_id") == grant.payload.get("claim_id")
        assert failure.payload.get("package_id") == grant.payload.get("package_id")
        releases = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.released")
            if record.payload.get("contract_id") == root_id
            and record.payload.get("claim_id") == grant.payload.get("claim_id")
        ]
        assert len(releases) == 1
        terminals = [
            record
            for _, record in store.scan_runtime_records(
                record_type="lifecycle.terminal"
            )
            if record.payload.get("contract_id") == root_id
        ]
        assert len(terminals) == 1
        assert terminals[0].payload["terminal"] == "failed"

        schedules = [
            record
            for _, record in store.scan_runtime_records(
                record_type="worker_failure.recovery_scheduled"
            )
            if record.payload.get("contract_id") == root_id
            and record.payload.get("failure_id") == failure.id
        ]
        assert len(schedules) == 1
        # The read-only history owner proves which recovery publication belongs
        # to the authenticated failed claim; the schedule itself has no target ID.
        lineage = task_lineage(store, root_id)
        recovery_edges = [
            edge
            for edge in lineage["data"]["edges"]
            if edge["kind"] == "recovery_publication"
            and edge["source_contract_id"] == root_id
            and schedules[0].id in edge["diagnostic_schedule_record_ids"]
        ]
        assert len(recovery_edges) == 1
        recovery = contracts[recovery_edges[0]["target_contract_id"]]
        assert recovery.origin == "recovery"
        assert not any(
            record.payload.get("contract_id") == recovery.id
            for _, record in store.scan_runtime_records(record_type="claim.submitted")
        )
        assert not store.get_assets_by_name("calibration_report")
        assert (
            project_task_status(root, store, check_integrity=True)["status"] == "failed"
        )
        _assert_readonly_rebuild(store, before)
    finally:
        store.close()

    _assert_rejected_export_is_read_only(workspace, root_id, tmp_path, evidence_dir)


def test_missing_copied_observation_fails_claimed_verifier_and_blocks_export(
    tmp_path, monkeypatch
):
    workspace, config, _workers = _configure(
        tmp_path, monkeypatch, "missing-observation"
    )
    evidence_dir = tmp_path / "evidence"
    result = _run(workspace, config, evidence_dir)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["completed"] is False
    assert payload["status"] == "timed_out"
    root_id = payload["contract_id"]

    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        before = _semantic_snapshot(store)
        contracts = _contracts_by_id(store)
        root = contracts[root_id]
        assert root.origin == "human"
        research = [
            contract
            for contract in contracts.values()
            if contract.name == "calibration_research"
        ]
        verifier = [
            contract
            for contract in contracts.values()
            if contract.name == "calibration_verifier"
        ]
        assert len(research) == len(verifier) == 1
        research_contract = research[0]
        verifier_contract = verifier[0]
        assert research_contract.outputs == (
            "calibration_report",
            "calibration_evidence",
        )
        assert verifier_contract.outputs == ("calibration_receipt",)
        assert verifier_contract.description
        assert json.loads(verifier_contract.description)["root_contract_id"] == root_id
        ancestor = verifier_contract
        seen = set()
        while ancestor.parent_id and ancestor.parent_id not in seen:
            seen.add(ancestor.id)
            ancestor = contracts[ancestor.parent_id]
        assert ancestor.id == root_id

        report_rows = store.get_assets_by_name("calibration_report")
        evidence_rows = store.get_assets_by_name("calibration_evidence")
        assert len(report_rows) == len(evidence_rows) == 1
        report_asset = report_rows[0]
        evidence_asset = evidence_rows[0]
        assert report_asset.created_by == evidence_asset.created_by
        assert report_asset.signed_by == "test-calibration-researcher"
        assert evidence_asset.signed_by == report_asset.signed_by
        producer = contracts[report_asset.created_by]
        assert producer.origin in {"continuation", "plan"}
        ancestor = producer
        research_is_ancestor = False
        seen_ancestors = set()
        while ancestor.id not in seen_ancestors:
            seen_ancestors.add(ancestor.id)
            if ancestor.id == research_contract.id:
                research_is_ancestor = True
                break
            if not ancestor.parent_id:
                break
            ancestor = contracts[ancestor.parent_id]
        assert research_is_ancestor
        committed_asset_ids = _committed_asset_ids(store)
        assert report_asset.id in committed_asset_ids
        assert evidence_asset.id in committed_asset_ids
        evidence = json.loads(evidence_asset.content)
        assert len(evidence["observations"]) == 1
        assert len(evidence["observation_asset_ids"]) == 1
        actual_observations = _tool_observations(store)
        actual_ids = {asset.id for asset, _value in actual_observations.values()}
        assert len(actual_ids) == 2
        assert set(evidence["observation_asset_ids"]) < actual_ids
        assert evidence["observations"][0]["asset_id"] in actual_ids

        researcher_claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.submitted")
            if record.payload.get("contract_id") == research_contract.id
            and record.payload.get("worker_id") == "test-calibration-researcher"
        ]
        verifier_claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.submitted")
            if record.payload.get("contract_id") == verifier_contract.id
            and record.payload.get("worker_id") == "test-calibration-verifier"
        ]
        assert len(researcher_claims) == len(verifier_claims) == 1
        producer_claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.submitted")
            if record.payload.get("contract_id") == producer.id
            and record.payload.get("worker_id") == "test-calibration-researcher"
        ]
        assert len(producer_claims) == 1
        verifier_candidates = [
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("contract_id") == verifier_contract.id
            and record.payload.get("actor_id") == "test-calibration-verifier"
        ]
        assert len(verifier_candidates) == 1
        verifier_action = verifier_candidates[0].payload["raw_output"]
        assert verifier_action.startswith("/fail ")
        assert json.loads(verifier_action.split(" ", 1)[1])["reason"] == (
            "calibration evidence check failed"
        )
        fail_children = [
            contract
            for contract in contracts.values()
            if contract.parent_id == verifier_contract.id
            and "plugin:fail" in contract.labels
        ]
        assert len(fail_children) == 1
        fail_child = fail_children[0]
        assert fail_child.origin == "system"
        assert fail_child.parent_id == verifier_contract.id
        assert json.loads(fail_child.description)["parent_contract_id"] == (
            verifier_contract.id
        )
        assert fail_child.outputs == (
            f"failure_result_{compute_content_hash(fail_child.description)[:24]}",
        )
        fail_child_candidates = [
            record
            for _, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("contract_id") == fail_child.id
            and record.payload.get("actor_id") == "test-calibration-verifier"
        ]
        assert len(fail_child_candidates) == 1
        fail_child_action = fail_child_candidates[0].payload["raw_output"]
        assert fail_child_action.startswith("/exec ")
        fail_child_output = json.loads(fail_child_action.split(" ", 1)[1])
        assert tuple(fail_child_output["outputs"]) == fail_child.outputs
        assert json.loads(fail_child_output["outputs"][fail_child.outputs[0]]) == {
            "method": "fail",
            "reason": "calibration evidence check failed",
        }
        assert not [
            record
            for _, record in store.scan_runtime_records(
                record_type="worker.invocation_failed"
            )
            if record.payload.get("contract_id") == fail_child.id
        ]
        fail_terminals = [
            record
            for _, record in store.scan_runtime_records(
                record_type="lifecycle.terminal"
            )
            if record.payload.get("contract_id") == fail_child.id
        ]
        assert len(fail_terminals) == 1
        assert fail_terminals[0].payload["terminal"] == "complete"
        verifier_terminals = [
            record
            for _, record in store.scan_runtime_records(
                record_type="lifecycle.terminal"
            )
            if record.payload.get("contract_id") == verifier_contract.id
        ]
        assert len(verifier_terminals) == 1
        assert verifier_terminals[0].payload["terminal"] == "failed"
        assert not store.get_assets_by_name("calibration_receipt")
        assert not [
            record
            for _, record in store.scan_runtime_records(record_type="asset.attested")
            if record.payload.get("contract_id") == root_id
            and record.payload.get("asset_id") == report_asset.id
        ]
        assert not [
            record
            for _, record in store.scan_runtime_records(record_type="output.qualified")
            if record.payload.get("contract_id") == root_id
            and record.payload.get("asset_id") == report_asset.id
        ]
        status = project_task_status(root, store, check_integrity=True)
        assert status["status"] == "expanded"
        assert f"descendant_failed:{verifier_contract.id}" in status["blockers"]
        assert "claim:submitted" in status["blockers"]
        _assert_readonly_rebuild(store, before)
    finally:
        store.close()

    _assert_rejected_export_is_read_only(workspace, root_id, tmp_path, evidence_dir)


def test_budget_two_is_spent_then_continuation_request_is_rejected(
    tmp_path, monkeypatch
):
    workspace, config, _workers = _configure(tmp_path, monkeypatch, "budget-exhaustion")
    evidence_dir = tmp_path / "evidence"
    result = _run(workspace, config, evidence_dir)
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["completed"] is False
    assert payload["status"] == "timed_out"
    root_id = payload["contract_id"]

    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        before = _semantic_snapshot(store)
        contracts = _contracts_by_id(store)
        root = contracts[root_id]
        research_rows = [
            contract
            for contract in contracts.values()
            if contract.name == "calibration_research"
        ]
        assert len(research_rows) == 1
        research = research_rows[0]
        assert research.budget == 2
        grants = [
            record
            for _, record in store.scan_runtime_records(record_type="allowance.granted")
            if record.payload.get("contract_id") == research.id
            and record.payload.get("amount") == 2
        ]
        reservations = [
            record
            for _, record in store.scan_runtime_records(
                record_type="allowance.reserved"
            )
            if record.payload.get("child_contract_id") == research.id
            and record.payload.get("amount") == 2
        ]
        assert len(grants) == len(reservations) == 1
        tool_history = _tool_observations(store)
        assert set(tool_history) == {"normalize_calibration", "analyze_calibration"}

        records = _all_records(store)
        rejected = [
            record
            for record in records
            if record.record_type == "candidate.rejected"
            and isinstance(record.payload.get("reason"), str)
            and "causal allowance exceeded" in record.payload["reason"]
            and "requested 1, available 0" in record.payload["reason"]
        ]
        assert len(rejected) == 1
        rejection = rejected[0]
        assert rejection.payload.get("effect_types") == ("contract.declare",)
        assert len(rejection.causal_parents) == 1
        by_id = {record.id: record for record in records}
        received = by_id[rejection.causal_parents[0]]
        assert received.record_type == "candidate.received"
        assert received.payload.get("candidate_id") == rejection.payload.get(
            "candidate_id"
        )
        assert received.payload.get("actor_id") == "plugin:continuation.publish.v1"

        reason = rejection.payload["reason"]
        source_id = next(
            contract_id for contract_id in contracts if f"'{contract_id}'" in reason
        )
        source = contracts[source_id]
        assert source.origin == "continuation"
        assert source.parent_id == research.id
        assert source.budget == 1
        source_terminals = [
            record
            for record in records
            if record.record_type == "lifecycle.terminal"
            and record.payload.get("contract_id") == source.id
        ]
        assert len(source_terminals) == 1
        assert source_terminals[0].payload.get("terminal") == "failed"
        continuation_claims = [
            record
            for record in records
            if record.record_type == "claim.submitted"
            and record.payload.get("contract_id") == source.id
            and record.payload.get("worker_id") == "test-calibration-researcher"
        ]
        assert len(continuation_claims) == 1
        assert allowance_available(research, records) == 0
        assert allowance_available(source, records) == 0
        assert not store.get_assets_by_name("calibration_report")
        assert not [
            record
            for record in records
            if record.record_type in {"asset.attested", "output.qualified"}
            and record.payload.get("contract_id") == root_id
        ]
        status = project_task_status(root, store, check_integrity=True)
        assert status["status"] == "expanded"
        assert "claim:submitted" in status["blockers"]
        assert any(
            blocker.startswith("descendant_failed:") for blocker in status["blockers"]
        )
        _assert_readonly_rebuild(store, before)
    finally:
        store.close()

    _assert_rejected_export_is_read_only(workspace, root_id, tmp_path, evidence_dir)
