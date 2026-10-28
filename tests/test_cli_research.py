from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "cli_research"
REDIS_URL = "redis://127.0.0.1:6379/13"


def _configured_workspace(
    tmp_path, monkeypatch, *, fail_once: bool, include_verifier: bool = True
):
    redis_url = os.environ.get("AIG_REDIS_TEST_URL", REDIS_URL)
    test_root = str(Path(__file__).resolve().parent)
    monkeypatch.syspath_prepend(test_root)
    pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(item for item in (test_root, pythonpath) if item),
    )
    monkeypatch.setenv("AIGINEERING_REDIS_URL", redis_url)
    marker = tmp_path / "summarize-failed-once.marker"
    if fail_once:
        monkeypatch.setenv("AIG_MEASUREMENT_FAIL_ONCE_FILE", str(marker))
    else:
        monkeypatch.delenv("AIG_MEASUREMENT_FAIL_ONCE_FILE", raising=False)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    preset = tmp_path / "preset"
    shutil.copytree(EXAMPLE, preset)
    workers = preset / "workers.toml"
    worker_config = (
        '[fleet]\ndb_path=".aig/store.db"\npoll_interval=0.02\n'
        '[[workers]]\nid="test-planner"\nkind="local"\n'
        'worker_factory="fixtures.cli_research_workers:build_research_worker"\n'
        'capabilities=["measurement.plan"]\npools=["planning"]\n'
        '[[workers]]\nid="test-researcher"\nkind="local"\n'
        'worker_factory="fixtures.cli_research_workers:build_research_worker"\n'
        'capabilities=["measurement.research"]\npools=["research"]\n'
        '[[workers]]\nid="measurement-tools"\nkind="tool"\n'
        'tool_registry="examples.cli_research.tools:build_registry"\n'
    )
    verifier_config = (
        '[[workers]]\nid="test-verifier"\nkind="local"\n'
        'worker_factory="fixtures.cli_research_workers:build_verifier_worker"\n'
        'capabilities=["measurement.verify"]\npools=["verification"]\n'
        'effect_capabilities=["asset.attest", "measurement.verify"]\n'
    )
    complete_worker_config = worker_config + verifier_config
    workers.write_text(
        complete_worker_config if include_verifier else worker_config,
        encoding="utf-8",
    )
    return workspace, preset / "run.toml", marker, workers, complete_worker_config


def _run_configured(workspace, config, *, timeout=60, resume_id=None):
    arguments = [
        "-C",
        str(workspace),
        "run",
        "--config",
        str(config),
    ]
    if resume_id is not None:
        arguments.extend(["--resume", resume_id])
    arguments.extend(["--wait-timeout", str(timeout), "--json"])
    return CliRunner().invoke(
        cli,
        arguments,
    )


def _assert_result_and_reopen(workspace, root_id, tmp_path):
    reopened = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        root = reopened.get_contract(root_id)
        assert root is not None
        assert root.name == "bounded_measurement_research"
        assert root.outputs == ("measurement_report",)
        children = [
            item for item in reopened.get_all_contracts() if item.parent_id == root_id
        ]
        assert {
            "plugin:plan.draft",
            "plugin:plan.dependencies",
            "plugin:plan.compile",
        } <= {label for child in children for label in child.labels}
        declarations = reopened.get_all_contracts()
        research = next(
            item for item in declarations if item.name == "measurement_research"
        )
        verifier = next(
            item for item in declarations if item.name == "measurement_verifier"
        )
        assert "measurement.research" in research.worker_capabilities
        assert "measurement.verify" in verifier.worker_capabilities
    finally:
        reopened.close()

    destination = tmp_path / "measurement-report.json"
    exported = CliRunner().invoke(
        cli,
        [
            "-C",
            str(workspace),
            "task",
            "result",
            root_id,
            "--output",
            "measurement_report",
            "--to",
            str(destination),
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.output
    envelope = json.loads(exported.output)
    assert envelope["ok"] is True
    assert envelope["data"]["status"] == "completed"
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report == {
        "count": 4,
        "dataset": "synthetic-temperature-sample-v1",
        "limitations": [
            "Synthetic measurements; no inference about a real population."
        ],
        "mean_c": 15.0,
        "mean_f": 59.0,
        "observation_asset_ids": report["observation_asset_ids"],
        "sample_asset_id": report["sample_asset_id"],
    }
    assert len(report["observation_asset_ids"]) == 2
    assert report["sample_asset_id"].startswith("asset:")


def test_cli_research_configured_run_completes_and_exports_after_reopen(
    tmp_path, monkeypatch
):
    workspace, config, _marker, workers_path, complete_workers = _configured_workspace(
        tmp_path,
        monkeypatch,
        fail_once=False,
        include_verifier=False,
    )

    waiting = _run_configured(workspace, config, timeout=10)

    assert waiting.exit_code == 1, waiting.output
    pending = json.loads(waiting.output)
    assert pending["contract_id"]
    assert pending["status"] == "timed_out", waiting.output
    root_id = pending["contract_id"]
    pending_store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        pending_root = pending_store.get_contract(root_id)
        assert pending_root is not None
        report_candidates = pending_store.get_assets_by_name("measurement_report")
        assert len(report_candidates) == 1
        report_before = report_candidates[0]
        assert report_before.created_by
        assert not any(
            record.payload.get("contract_id") == root_id
            and record.payload.get("output_name") == "measurement_report"
            for _, record in pending_store.scan_runtime_records(
                record_type="output.qualified"
            )
        )
    finally:
        pending_store.close()

    destination = tmp_path / "unqualified-report.json"
    rejected_export = CliRunner().invoke(
        cli,
        [
            "-C",
            str(workspace),
            "task",
            "result",
            root_id,
            "--output",
            "measurement_report",
            "--to",
            str(destination),
            "--json",
        ],
    )
    assert rejected_export.exit_code == 1, rejected_export.output
    assert json.loads(rejected_export.output)["ok"] is False
    assert not destination.exists()

    workers_path.write_text(complete_workers, encoding="utf-8")
    result = _run_configured(workspace, config, resume_id=root_id)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["contract_id"] == root_id
    assert payload["completed"] is True
    assert payload["status"] == "complete"
    _assert_result_and_reopen(workspace, root_id, tmp_path)

    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        root = store.get_contract(root_id)
        assert root is not None
        tasks = store.get_all_contracts()
        research = next(
            item
            for item in tasks
            if item.name == "measurement_research" and item.parent_id is not None
        )
        assert research.budget >= 8
        assert research.outputs == ("measurement_report", "measurement_evidence")
        assert set(research.tool_scope) == {"summarize_sample", "convert_mean"}
        assert research.worker_capabilities == ("measurement.research",)
        verifier = next(item for item in tasks if item.name == "measurement_verifier")
        assert verifier.outputs == ("verification_receipt",)
        assert set(verifier.inputs) == {
            "sample",
            "measurement_report",
            "measurement_evidence",
        }
        assert verifier.worker_capabilities == ("measurement.verify",)
        assert verifier.worker_pools == ("verification",)
        verification = store.get_assets_by_name("verification_receipt")
        assert len(verification) == 1

        report_assets = store.get_assets_by_name("measurement_report")
        assert len(report_assets) == 1
        report_asset = report_assets[0]
        assert report_asset.id == report_before.id
        assert report_asset.created_by == report_before.created_by
        assert report_asset.signed_by == "test-researcher"

        sample_assets = store.get_assets_by_name("sample")
        assert len(sample_assets) == 1
        attestations = [
            record
            for _, record in store.scan_runtime_records(record_type="asset.attested")
            if record.payload.get("contract_id") == root_id
            and record.payload.get("output_name") == "measurement_report"
        ]
        assert len(attestations) == 1
        attestation = attestations[0]
        assert attestation.payload.get("asset_id") == report_asset.id
        assert attestation.payload.get("evidence_asset_ids") == (sample_assets[0].id,)
        assert attestation.payload.get("verdict") == "accepted"
        assert attestation.payload.get("verifier_actor_id") != report_asset.signed_by

        qualifications = [
            record
            for _, record in store.scan_runtime_records(record_type="output.qualified")
            if record.payload.get("contract_id") == root_id
            and record.payload.get("output_name") == "measurement_report"
        ]
        assert len(qualifications) == 1
        assert qualifications[0].payload.get("asset_id") == report_asset.id
        assert qualifications[0].payload.get("verifier_actor_ids") == (
            attestation.payload.get("verifier_actor_id"),
        )

        report_claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.submitted")
            if record.payload.get("contract_id") == report_asset.created_by
            and record.payload.get("worker_id") == report_asset.signed_by
        ]
        assert len(report_claims) == 1
        verifier_claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.submitted")
            if record.payload.get("contract_id") == verifier.id
            and record.payload.get("worker_id")
            == attestation.payload.get("verifier_actor_id")
        ]
        assert len(verifier_claims) == 1
    finally:
        store.close()


def test_cli_research_tool_schema_failure_recovers_through_runtime(
    tmp_path, monkeypatch
):
    workspace, config, marker, _workers_path, _complete_workers = _configured_workspace(
        tmp_path, monkeypatch, fail_once=True
    )

    result = _run_configured(workspace, config)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["completed"] is True
    assert marker.read_text(encoding="utf-8") == "simulated backend failure\n"

    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        failures = [
            record
            for _, record in store.scan_runtime_records(
                record_type="worker.invocation_failed"
            )
            if record.payload.get("category") == "worker_error:tool_schema_invalid"
        ]
        assert len(failures) == 1
        failed_id = failures[0].payload["contract_id"]
        failed_contract = store.get_contract(failed_id)
        assert failed_contract is not None
        assert failed_contract.origin != "recovery"
        scheduled = [
            record
            for _, record in store.scan_runtime_records(
                record_type="worker_failure.recovery_scheduled"
            )
            if record.payload.get("failure_id") == failures[0].id
        ]
        assert len(scheduled) == 1
        recovery_id = scheduled[0].payload["recovery_contract_id"]
        recoveries = [
            item for item in store.get_all_contracts() if item.id == recovery_id
        ]
        assert len(recoveries) == 1
        recovered = recoveries[0]
        assert recovered.origin == "system"
        assert "summarize_sample" in recovered.tool_scope
        assert recovered.parent_id == failed_contract.parent_id
        recovery_traces = store.get_by_contract(recovered.id)
        assert any(
            entry.event_type == "candidate_committed" for entry in recovery_traces
        )
        assert any(entry.event_type == "complete" for entry in recovery_traces)
        assert any(
            entry.event_type == "worker_invocation_failed"
            for entry in store.get_by_contract(failed_id)
        )
        successful_summary_observations = []
        for asset in store.get_all_assets():
            if not asset.name.startswith(("_tool_obs_", "tool_observation_")):
                continue
            try:
                observation = json.loads(asset.content)
            except (TypeError, ValueError):
                continue
            if (
                observation.get("tool") == "summarize_sample"
                and observation.get("ok") is True
            ):
                successful_summary_observations.append(asset)
        assert len(successful_summary_observations) == 1
    finally:
        store.close()

    _assert_result_and_reopen(workspace, payload["contract_id"], tmp_path)
