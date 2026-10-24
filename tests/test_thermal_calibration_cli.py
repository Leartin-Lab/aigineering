from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.configured_run import prepare_configured_run
from aigineering.protocol.actions import parse_action
from aigineering.protocol.types import Asset, Contract

from examples.thermal_calibration.oracle import evaluate_report
from examples.thermal_calibration.tools import build_registry
from examples.thermal_calibration.workers import build_verifier_worker

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "thermal_calibration"
FIXTURES = EXAMPLE / "fixtures" / "train"


def _configure_workspace(
    tmp_path,
    monkeypatch,
    *,
    fail_first_tool: bool = False,
    include_verifier: bool = True,
):
    tests_path = str(Path(__file__).resolve().parent)
    repo_path = str(ROOT)
    monkeypatch.syspath_prepend(tests_path)
    monkeypatch.syspath_prepend(repo_path)
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(item for item in (repo_path, tests_path, existing) if item),
    )
    if not os.environ.get("AIGINEERING_REDIS_URL"):
        pytest.skip("configured CLI integration requires the coordinated Redis URL")

    marker = tmp_path / "normalize-failed-once.marker"
    if fail_first_tool:
        monkeypatch.setenv("AIG_CALIBRATION_FAIL_FIRST_TOOL_MARKER", str(marker))
    else:
        monkeypatch.delenv("AIG_CALIBRATION_FAIL_FIRST_TOOL_MARKER", raising=False)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    preset = tmp_path / "preset"
    shutil.copytree(EXAMPLE, preset)
    workers = preset / "workers.toml"
    registry = (
        "fixtures.thermal_calibration_workers:build_failure_registry"
        if fail_first_tool
        else "examples.thermal_calibration.tools:build_registry"
    )
    worker_config = f'''[fleet]
db_path = ".aig/store.db"
poll_interval = 0.02

[[workers]]
id = "test-calibration-planner"
kind = "local"
worker_factory = "fixtures.thermal_calibration_workers:build_calibration_worker"
capabilities = ["calibration.plan"]
pools = ["planning"]

[[workers]]
id = "test-calibration-researcher"
kind = "local"
worker_factory = "fixtures.thermal_calibration_workers:build_calibration_worker"
capabilities = ["calibration.research"]
pools = ["research"]

[[workers]]
id = "calibration-tools"
kind = "tool"
tool_registry = "{registry}"

'''
    verifier_config = """[[workers]]
id = "test-calibration-verifier"
kind = "local"
worker_factory = "fixtures.thermal_calibration_workers:build_verifier_worker"
capabilities = ["calibration.verify"]
pools = ["verification"]
effect_capabilities = ["asset.attest", "calibration.verify"]
"""
    complete_config = worker_config + verifier_config
    workers.write_text(
        complete_config if include_verifier else worker_config,
        encoding="utf-8",
    )
    return workspace, preset / "run.toml", marker, workers, complete_config


def _invoke_run(workspace, config, *, timeout=60, resume_id=None):
    args = ["-C", str(workspace), "run", "--config", str(config)]
    if resume_id is not None:
        args.extend(["--resume", resume_id])
    args.extend(["--wait-timeout", str(timeout), "--json"])
    return CliRunner().invoke(cli, args)


def _assets_by_name(store, name):
    found = store.get_assets_by_name(name)
    assert len(found) == 1, (name, [asset.id for asset in found])
    return found[0]


def _tool_observations(store):
    found = []
    for asset in store.get_all_assets():
        if not asset.name.startswith(("_tool_obs_", "tool_observation_")):
            continue
        try:
            observation = json.loads(asset.content)
        except (TypeError, ValueError):
            continue
        if isinstance(observation, dict) and observation.get("tool") in {
            "normalize_calibration",
            "analyze_calibration",
        }:
            found.append((asset, observation))
    return found


def _assert_tool_history_is_signed_and_committed(store, expected_tools):
    observations = _tool_observations(store)
    by_tool = {}
    for asset, observation in observations:
        if observation.get("ok") is True:
            by_tool.setdefault(observation["tool"], []).append((asset, observation))
    assert set(by_tool) == set(expected_tools)
    committed = {
        record.payload["asset"]["id"]: record
        for _, record in store.scan_runtime_records(record_type="asset.committed")
    }
    for tool_name in expected_tools:
        assert len(by_tool[tool_name]) == 1
        asset, observation = by_tool[tool_name][0]
        assert asset.origin == "worker"
        assert asset.signed_by == "calibration-tools"
        assert verify_asset_seal(asset)
        assert asset.id in committed
        fact = committed[asset.id]
        assert fact.payload["contract_id"] == asset.created_by
        assert fact.payload["asset"]["content"] == asset.content
        assert observation["tool"] == tool_name
        assert observation["ok"] is True
    return {name: by_tool[name][0] for name in expected_tools}


def _assert_claims_and_acceptance(store, root_id):
    contracts = {item.id: item for item in store.get_all_contracts()}
    claims = [
        record
        for _, record in store.scan_runtime_records(record_type="claim.submitted")
    ]
    assert claims
    for claim in claims:
        assert claim.payload["contract_id"] in contracts
        assert claim.payload["worker_id"]

    report = _assets_by_name(store, "calibration_report")
    case = _assets_by_name(store, "calibration_case")
    standard = _assets_by_name(store, "calibration_standard")
    report_claims = [
        claim
        for claim in claims
        if claim.payload["contract_id"] == report.created_by
        and claim.payload["worker_id"] == report.signed_by
    ]
    assert len(report_claims) == 1
    verifier = next(
        item for item in contracts.values() if item.name == "calibration_verifier"
    )
    verifier_claims = [
        claim
        for claim in claims
        if claim.payload["contract_id"] == verifier.id
        and claim.payload["worker_id"] == "test-calibration-verifier"
    ]
    assert len(verifier_claims) == 1

    attestations = [
        record
        for _, record in store.scan_runtime_records(record_type="asset.attested")
        if record.payload.get("contract_id") == root_id
        and record.payload.get("output_name") == "calibration_report"
    ]
    assert len(attestations) == 1
    attestation = attestations[0]
    assert attestation.payload["asset_id"] == report.id
    assert attestation.payload["verdict"] == "accepted"
    assert attestation.payload["evidence_asset_ids"] == tuple(
        sorted((case.id, standard.id))
    )
    assert attestation.payload["verifier_actor_id"] == "test-calibration-verifier"
    assert report.signed_by == "test-calibration-researcher"
    assert attestation.payload["verifier_actor_id"] != report.signed_by

    qualified = [
        record
        for _, record in store.scan_runtime_records(record_type="output.qualified")
        if record.payload.get("contract_id") == root_id
        and record.payload.get("output_name") == "calibration_report"
    ]
    assert len(qualified) == 1
    assert qualified[0].payload["asset_id"] == report.id
    assert qualified[0].payload["verifier_actor_ids"] == (
        attestation.payload["verifier_actor_id"],
    )
    return report, case, standard


def _assert_strict_snapshot_rebuild_is_read_only(store):
    digest_before = store.runtime_materialization_digest()
    records_before = [
        (revision, record.id, record.record_type)
        for revision, record in store.scan_runtime_records()
    ]
    snapshot = store.snapshot()
    try:
        assert snapshot.runtime_materialization_digest() == digest_before
        assert snapshot.rebuild_runtime_materializations(strict=True) == digest_before
    finally:
        snapshot.close()
    assert store.runtime_materialization_digest() == digest_before
    assert [
        (revision, record.id, record.record_type)
        for revision, record in store.scan_runtime_records()
    ] == records_before


def _export_report(workspace, root_id, destination):
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
    return result


def _finish_and_check(workspace, root_id, tmp_path):
    reopened = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        root = reopened.get_contract(root_id)
        assert root is not None
        assert root.id.startswith("task:v6:")
        assert root.inputs == ("calibration_case", "calibration_standard")
        assert root.outputs == ("calibration_report",)
        report, case, standard = _assert_claims_and_acceptance(reopened, root_id)
        assert dict(root.acceptance_policy or {}) == {
            "mode": "independent",
            "policy_version": "synthetic-temperature-calibration-v1",
            "required_attestations": 1,
            "verifier_capabilities": ("calibration.verify",),
            "evidence_asset_ids": tuple(sorted((case.id, standard.id))),
        }
        evidence = _assets_by_name(reopened, "calibration_evidence")
        evidence_value = json.loads(evidence.content)
        assert evidence_value["calibration_case_asset_id"] == case.id
        assert evidence_value["calibration_standard_asset_id"] == standard.id
        assert len(evidence_value["observation_asset_ids"]) == 2
        assert len(set(evidence_value["observation_asset_ids"])) == 2
        history = _assert_tool_history_is_signed_and_committed(
            reopened, {"normalize_calibration", "analyze_calibration"}
        )
        assert evidence_value["observation_asset_ids"] == [
            history[name][0].id
            for name in ("normalize_calibration", "analyze_calibration")
        ]
        evidence_rows = evidence_value["observations"]
        assert [row["asset_id"] for row in evidence_rows] == evidence_value[
            "observation_asset_ids"
        ]
        for row, tool_name in zip(
            evidence_rows,
            ("normalize_calibration", "analyze_calibration"),
            strict=True,
        ):
            assert row["observation"] == history[tool_name][1]
        result = evaluate_report(case.content, standard.content, report.content)
        assert result["passed"] is True, result
        _assert_strict_snapshot_rebuild_is_read_only(reopened)
        report_content = report.content
    finally:
        reopened.close()

    destination = tmp_path / "calibration-report.json"
    exported = _export_report(workspace, root_id, destination)
    assert exported.exit_code == 0, exported.output
    envelope = json.loads(exported.output)
    assert envelope["ok"] is True
    assert envelope["data"]["status"] == "completed"
    assert destination.read_text(encoding="utf-8") == report_content
    return destination


def test_local_verifier_uses_exact_oracle_and_rejects_copied_evidence_gap():
    case = json.loads(
        (FIXTURES / "mix-three-point.case.json").read_text(encoding="utf-8")
    )
    standard = json.loads(
        (FIXTURES / "mix-three-point.standard.json").read_text(encoding="utf-8")
    )
    registry = build_registry()
    normalized_text = registry.run(
        "normalize_calibration", {"case": case, "standard": standard}
    )
    report_text = registry.run(
        "analyze_calibration", {"normalized": json.loads(normalized_text)}
    )
    normalized_observation = {
        "ok": True,
        "tool": "normalize_calibration",
        "result": normalized_text,
        "error": "",
        "error_type": "",
        "retryable": False,
    }
    analysis_observation = {
        "ok": True,
        "tool": "analyze_calibration",
        "result": report_text,
        "error": "",
        "error_type": "",
        "retryable": False,
    }
    case_asset = Asset(
        id="asset:case", name="calibration_case", content=json.dumps(case)
    )
    standard_asset = Asset(
        id="asset:standard", name="calibration_standard", content=json.dumps(standard)
    )
    report_asset = Asset(
        id="asset:report", name="calibration_report", content=report_text
    )
    evidence_asset = Asset(
        id="asset:evidence",
        name="calibration_evidence",
        content=json.dumps(
            {
                "schema": "thermal-calibration-evidence-v1",
                "calibration_case_asset_id": case_asset.id,
                "calibration_standard_asset_id": standard_asset.id,
                "observation_asset_ids": ["asset:normalize", "asset:analyze"],
                "observations": [
                    {
                        "asset_id": "asset:normalize",
                        "observation": normalized_observation,
                    },
                    {"asset_id": "asset:analyze", "observation": analysis_observation},
                ],
            },
            sort_keys=True,
        ),
    )
    contract = Contract(
        id="task:verifier",
        parent_id="task:planning-compile",
        name="calibration_verifier",
        description=json.dumps(
            {
                "schema": "thermal-calibration-verifier-target-v1",
                "root_contract_id": "task:root",
                "calibration_case_asset_id": case_asset.id,
                "calibration_standard_asset_id": standard_asset.id,
                "policy_version": "synthetic-temperature-calibration-v1",
            },
            sort_keys=True,
        ),
        inputs=(
            "calibration_case",
            "calibration_standard",
            "calibration_report",
            "calibration_evidence",
        ),
        outputs=("calibration_receipt",),
        worker_capabilities=("calibration.verify",),
        worker_pools=("verification",),
    )
    verifier = build_verifier_worker(worker_id="unit-verifier")
    accepted = verifier.invoke(
        contract,
        [case_asset, standard_asset, report_asset, evidence_asset],
    )
    action = parse_action(accepted.raw_output)
    assert action.type == "attest"
    assert action.payload["contract_id"] == "task:root"
    assert action.payload["asset_id"] == report_asset.id
    assert action.payload["evidence_asset_ids"] == tuple(
        sorted((case_asset.id, standard_asset.id))
    )

    incomplete = json.loads(evidence_asset.content)
    incomplete["observations"].pop()
    changed_evidence = Asset(
        id="asset:evidence-incomplete",
        name="calibration_evidence",
        content=json.dumps(incomplete),
    )
    rejected = verifier.invoke(
        contract,
        [case_asset, standard_asset, report_asset, changed_evidence],
    )
    rejected_action = parse_action(rejected.raw_output)
    assert rejected_action.type == "fail"

    def rejected_evidence(mutator):
        changed = json.loads(evidence_asset.content)
        mutator(changed)
        changed_asset = Asset(
            id="asset:evidence-mutated",
            name="calibration_evidence",
            content=json.dumps(changed, sort_keys=True),
        )
        action = parse_action(
            verifier.invoke(
                contract,
                [case_asset, standard_asset, report_asset, changed_asset],
            ).raw_output
        )
        assert action.type == "fail"

    def change_normalized_hash(value):
        observation = value["observations"][0]["observation"]
        normalized = json.loads(observation["result"])
        normalized["case_sha256"] = "0" * 64
        observation["result"] = json.dumps(normalized, sort_keys=True)

    def reverse_observations(value):
        value["observations"].reverse()
        value["observation_asset_ids"].reverse()

    def change_analysis_bool_type(value):
        observation = value["observations"][1]["observation"]
        analyzed = json.loads(observation["result"])
        analyzed["within_tolerance"] = int(analyzed["within_tolerance"])
        observation["result"] = json.dumps(analyzed, sort_keys=True)

    rejected_evidence(change_normalized_hash)
    rejected_evidence(reverse_observations)
    rejected_evidence(change_analysis_bool_type)

    duplicate_normalized = json.loads(evidence_asset.content)
    normalized_observation = duplicate_normalized["observations"][0]["observation"]
    normalized_observation["result"] = normalized_observation["result"].replace(
        '"samples":', '"samples": [], "samples":', 1
    )
    duplicate_normalized_asset = Asset(
        id="asset:evidence-duplicate-normalized",
        name="calibration_evidence",
        content=json.dumps(duplicate_normalized, sort_keys=True),
    )
    assert (
        parse_action(
            verifier.invoke(
                contract,
                [case_asset, standard_asset, report_asset, duplicate_normalized_asset],
            ).raw_output
        ).type
        == "fail"
    )

    duplicate_outer_content = evidence_asset.content.replace(
        '"observation_asset_ids":',
        '"observation_asset_ids": ["duplicate"], "observation_asset_ids":',
        1,
    )
    duplicate_outer_asset = Asset(
        id="asset:evidence-duplicate-key",
        name="calibration_evidence",
        content=duplicate_outer_content,
    )
    assert (
        parse_action(
            verifier.invoke(
                contract,
                [case_asset, standard_asset, report_asset, duplicate_outer_asset],
            ).raw_output
        ).type
        == "fail"
    )


def test_production_preset_discovers_checkout_local_verifier_factory():
    prepared = prepare_configured_run(EXAMPLE / "run.toml")
    verifier = next(
        (spec, worker)
        for spec, worker in prepared.workers
        if spec.worker_id == "calibration-verifier"
    )
    assert verifier[0].kind == "local"
    assert verifier[0].worker_factory == (
        "examples.thermal_calibration.workers:build_verifier_worker"
    )
    assert callable(verifier[1].invoke)


def test_test_fleet_factory_references_load_without_starting_runtime(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AIGINEERING_REDIS_URL", "redis://127.0.0.1:1/0")
    _workspace, config, _marker, _workers_path, _complete = _configure_workspace(
        tmp_path,
        monkeypatch,
        fail_first_tool=True,
        include_verifier=False,
    )
    prepared = prepare_configured_run(config)
    assert prepared.db_path == ".aig/store.db"
    assert prepared.contract is not None
    assert prepared.contract.outputs == ("calibration_report",)
    assert {spec.worker_id for spec, _worker in prepared.workers} == {
        "test-calibration-planner",
        "test-calibration-researcher",
        "calibration-tools",
    }


def test_cli_calibration_completes_accepts_and_exports_after_reopen(
    tmp_path, monkeypatch
):
    workspace, config, _marker, _workers, _complete = _configure_workspace(
        tmp_path, monkeypatch
    )
    result = _invoke_run(workspace, config)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["completed"] is True
    root_id = payload["contract_id"]
    destination = _finish_and_check(workspace, root_id, tmp_path)
    assert (
        json.loads(destination.read_text(encoding="utf-8"))["case_id"]
        == "mix-three-point"
    )


def test_cli_calibration_first_tool_schema_failure_runs_runtime_recovery(
    tmp_path, monkeypatch
):
    workspace, config, marker, _workers, _complete = _configure_workspace(
        tmp_path, monkeypatch, fail_first_tool=True
    )
    result = _invoke_run(workspace, config)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["completed"] is True
    assert marker.read_text(encoding="utf-8") == ""

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
        failure = failures[0]
        scheduled = [
            record
            for _, record in store.scan_runtime_records(
                record_type="worker_failure.recovery_scheduled"
            )
            if record.payload.get("failure_id") == failure.id
        ]
        assert len(scheduled) == 1
        recovery_id = scheduled[0].payload["recovery_contract_id"]
        recovery = store.get_contract(recovery_id)
        assert recovery is not None
        assert recovery.origin == "system"
        assert "normalize_calibration" in recovery.tool_scope
        assert any(
            entry.event_type == "candidate_committed"
            for entry in store.get_by_contract(recovery.id)
        )
        assert any(
            entry.event_type == "complete"
            for entry in store.get_by_contract(recovery.id)
        )
        assert any(
            record.payload.get("contract_id") == recovery_id
            and record.payload.get("worker_id") == "calibration-tools"
            for _, record in store.scan_runtime_records(record_type="claim.submitted")
        )
        tool_history = _assert_tool_history_is_signed_and_committed(
            store, {"normalize_calibration", "analyze_calibration"}
        )
        normalize_asset = tool_history["normalize_calibration"][0]
        assert normalize_asset.created_by == recovery_id
        recovery_claims = [
            record
            for _, record in store.scan_runtime_records(record_type="claim.submitted")
            if record.payload.get("contract_id") == recovery_id
            and record.payload.get("worker_id") == "calibration-tools"
        ]
        assert len(recovery_claims) == 1
    finally:
        store.close()
    _finish_and_check(workspace, payload["contract_id"], tmp_path)


def test_cli_calibration_missing_verifier_stays_unqualified_then_resumes_same_root(
    tmp_path, monkeypatch
):
    workspace, config, _marker, workers_path, complete_config = _configure_workspace(
        tmp_path, monkeypatch, include_verifier=False
    )
    waiting = _invoke_run(workspace, config, timeout=5)
    assert waiting.exit_code == 1, waiting.output
    pending = json.loads(waiting.output)
    root_id = pending["contract_id"]
    assert pending["status"] == "timed_out"

    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        reports = store.get_assets_by_name("calibration_report")
        assert len(reports) == 1
        report_before = reports[0]
        assert not any(
            record.payload.get("contract_id") == root_id
            and record.payload.get("output_name") == "calibration_report"
            for _, record in store.scan_runtime_records(record_type="output.qualified")
        )
        _assert_tool_history_is_signed_and_committed(
            store, {"normalize_calibration", "analyze_calibration"}
        )
    finally:
        store.close()

    destination = tmp_path / "unqualified-calibration-report.json"
    rejected_export = _export_report(workspace, root_id, destination)
    assert rejected_export.exit_code == 1, rejected_export.output
    assert json.loads(rejected_export.output)["ok"] is False
    assert not destination.exists()

    workers_path.write_text(complete_config, encoding="utf-8")
    resumed = _invoke_run(workspace, config, resume_id=root_id)
    assert resumed.exit_code == 0, resumed.output
    resumed_payload = json.loads(resumed.output)
    assert resumed_payload["contract_id"] == root_id
    assert resumed_payload["completed"] is True

    store = SQLiteStore(workspace / ".aig" / "store.db")
    try:
        reports_after = store.get_assets_by_name("calibration_report")
        assert len(reports_after) == 1
        assert reports_after[0].id == report_before.id
        assert reports_after[0].created_by == report_before.created_by
    finally:
        store.close()
    _finish_and_check(workspace, root_id, tmp_path)
