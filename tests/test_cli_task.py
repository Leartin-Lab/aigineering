"""Tests for agent-facing task CLI commands."""

import json

from click.testing import CliRunner

from aigineering.cli.main import cli


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
        assert data["cycles"][0]["contract_id"] == contract_id
        assert data["accepted_assets"][0]["name"] == "final_report"


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
