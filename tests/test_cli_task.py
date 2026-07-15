"""Tests for agent-facing task CLI commands."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.agent.llm import LLMWorker, ProviderError
from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry


@pytest.fixture(autouse=True)
def initialize_candidate_domain_before_task_publication(monkeypatch):
    original = CliRunner.invoke

    def invoke(runner, command, args=None, *positional, **kwargs):
        effective = list(args or ())
        if (
            effective[:2] in (["asset", "add"], ["task", "create"])
            and not Path(".aig/identity/root.ed25519").exists()
        ):
            initialized = original(runner, command, ["domain", "init"])
            assert initialized.exit_code == 0, initialized.output
        return original(runner, command, args, *positional, **kwargs)

    monkeypatch.setattr(CliRunner, "invoke", invoke)


def _seed_build_report_task(runner: CliRunner) -> str:
    runner.invoke(cli, ["asset", "add", "--name", "data_file", "--content", "data"])
    runner.invoke(
        cli,
        ["asset", "add", "--name", "citation_db", "--content", "citations"],
    )
    result = runner.invoke(
        cli,
        [
            "task",
            "create",
            "--name",
            "build_report",
            "--input",
            "data_file",
            "--input",
            "citation_db",
            "--output",
            "final_report",
            "--activation",
            "data_file AND citation_db",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["contract_id"]


def test_task_create_status_json():
    runner = CliRunner()
    with runner.isolated_filesystem():
        contract_id = _seed_build_report_task(runner)
        result = runner.invoke(cli, ["task", "status", contract_id, "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["contract_id"] == contract_id
        assert data["status"] == "ready"
        assert data["terminal"] is False


def test_run_once_executes_next_ready_task():
    runner = CliRunner()
    with runner.isolated_filesystem():
        contract_id = _seed_build_report_task(runner)

        result = runner.invoke(cli, ["run", "--once", "--worker", "mock", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["status"] == "completed"
        assert data["submission_status"] in {"accepted", "partial"}
        assert contract_id in data["cycles"][0]["contracts"]

        status_result = runner.invoke(cli, ["task", "status", contract_id, "--json"])
        status = json.loads(status_result.output)
        assert status["outputs"]["final_report"]

        store = SQLiteStore(".aig/store.db")
        record_types = [
            record.record_type for _, record in store.scan_runtime_records()
        ]
        assert "claim.granted" in record_types
        assert "candidate.received" in record_types
        assert "claim.submitted" in record_types


def test_run_once_idle_is_visible_and_nonzero():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["run", "--once", "--worker", "mock", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["ok"] is False
        assert data["status"] == "idle"


def test_run_once_provider_failure_is_json_and_has_no_traceback(monkeypatch):
    def fail_provider(self, contract, disclosed_assets):
        del self, contract, disclosed_assets
        raise ProviderError(503, "upstream-secret-detail")

    monkeypatch.setattr(LLMWorker, "invoke", fail_provider)
    runner = CliRunner()
    with runner.isolated_filesystem():
        contract_id = _seed_build_report_task(runner)
        result = runner.invoke(
            cli,
            [
                "run",
                "--once",
                "--worker",
                "llm",
                "--model",
                "test-model",
                "--json",
            ],
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["ok"] is False
        assert data["status"] == "failed"
        assert "claim was released" in data["error"]
        assert "Traceback" not in result.output
        assert "upstream-secret-detail" not in result.output
        store = SQLiteStore(".aig/store.db")
        assert store.get_claim(contract_id)["status"] == "released"


def test_run_task_waits_until_target_complete():
    runner = CliRunner()
    with runner.isolated_filesystem():
        contract_id = _seed_build_report_task(runner)

        result = runner.invoke(
            cli,
            [
                "run",
                "--task",
                contract_id,
                "--worker",
                "mock",
                "--json",
                "--wait-timeout",
                "5",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["status"] == "completed"
        assert data["outputs"]["final_report"]


def test_task_wait_and_audit_json_after_run():
    runner = CliRunner()
    with runner.isolated_filesystem():
        contract_id = _seed_build_report_task(runner)
        runner.invoke(cli, ["run", "--task", contract_id, "--worker", "mock"])

        wait_result = runner.invoke(cli, ["task", "wait", contract_id, "--json"])
        audit_result = runner.invoke(cli, ["task", "audit", contract_id, "--json"])

        assert wait_result.exit_code == 0, wait_result.output
        wait_data = json.loads(wait_result.output)
        assert wait_data["status"] == "completed"

        assert audit_result.exit_code == 0, audit_result.output
        audit_data = json.loads(audit_result.output)
        assert audit_data["task"]["contract_id"] == contract_id
        assert audit_data["task"]["outputs"]["final_report"]
        assert len(audit_data["trace"]) > 0


def test_run_task_uses_engine_method_path_for_plan():
    runner = CliRunner()
    with runner.isolated_filesystem():
        create = runner.invoke(
            cli,
            [
                "task",
                "create",
                "--name",
                "root",
                "--output",
                "report",
                "--json",
            ],
        )
        assert create.exit_code == 0, create.output
        contract_id = json.loads(create.output)["contract_id"]
        empty_plan = json.dumps({"contracts": []}, sort_keys=True)
        plan_result = (
            f'/exec {{"outputs": {{"_plan_result_{contract_id}": '
            f"{json.dumps(empty_plan)}}}}}"
        )

        result = runner.invoke(
            cli,
            [
                "run",
                "--task",
                contract_id,
                "--worker",
                "mock",
                "--mock-preset",
                'root=/plan {"reason": "need context"}',
                "--mock-preset",
                f"root.plan={plan_result}",
                "--wait-timeout",
                "1",
                "--json",
            ],
        )

        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["status"] == "blocked_method"
        audit = runner.invoke(cli, ["task", "audit", contract_id, "--json"])
        data = json.loads(audit.output)
        event_types = {entry["event_type"] for entry in data["trace"]}
        assert "method_scheduled" in event_types
        store_audit = runner.invoke(cli, ["task", "audit", contract_id, "--json"])
        trace = json.loads(store_audit.output)["trace"]
        child_id = next(
            entry["relation_target"]
            for entry in trace
            if entry["event_type"] == "method_scheduled"
        )
        child_audit = runner.invoke(cli, ["task", "audit", child_id, "--json"])
        child_data = json.loads(child_audit.output)
        child_events = {entry["event_type"] for entry in child_data["trace"]}
        assert "projection" in child_events
        assert child_data["task"]["status"] == "completed"


def test_run_task_rejected_output_schedules_recovery():
    runner = CliRunner()
    with runner.isolated_filesystem():
        create = runner.invoke(
            cli,
            [
                "task",
                "create",
                "--name",
                "write_report",
                "--output",
                "report",
                "--json",
            ],
        )
        assert create.exit_code == 0, create.output
        contract_id = json.loads(create.output)["contract_id"]

        result = runner.invoke(
            cli,
            [
                "run",
                "--task",
                contract_id,
                "--worker",
                "mock",
                "--mock-preset",
                "write_report=wrong_output: nope",
                "--wait-timeout",
                "0",
                "--json",
            ],
        )

        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["status"] == "failed"
        audit = runner.invoke(cli, ["task", "audit", contract_id, "--json"])
        data = json.loads(audit.output)
        event_types = {entry["event_type"] for entry in data["trace"]}
        assert "recovery_scheduled" in event_types
        assert data["task"]["recovery_count"] == 1


def test_task_status_reports_submitted_without_recovery_risk():
    runner = CliRunner()
    with runner.isolated_filesystem():
        create = runner.invoke(
            cli,
            [
                "task",
                "create",
                "--name",
                "orphan_projection",
                "--output",
                "report",
                "--json",
            ],
        )
        assert create.exit_code == 0, create.output
        contract_id = json.loads(create.output)["contract_id"]

        store = SQLiteStore(".aig/store.db")
        store.append(
            create_entry(
                contract_id=contract_id,
                event_type="projection",
                rejected_fragments=["[parse_error] (empty): no output"],
                authority_result="rejected",
                budget_remaining=0,
            )
        )
        store.append(
            create_entry(
                contract_id=contract_id,
                event_type="budget_consumed",
                budget_remaining=0,
            )
        )

        status = runner.invoke(cli, ["task", "status", contract_id, "--json"])
        assert status.exit_code == 0, status.output
        data = json.loads(status.output)
        assert data["status"] == "submitted"
        assert data["ok"] is False
        codes = {risk["code"] for risk in data["silent_failure_risks"]}
        assert "budget_exhausted" in codes
        assert "submitted_without_recovery" in codes
