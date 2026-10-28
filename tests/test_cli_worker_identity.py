"""Explicit run identities use the ordinary durable WorkerHost boundary."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.application import build_worker
from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


def test_explicit_producer_and_reviewer_ids_are_durable_across_resume(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        assert runner.invoke(cli, ["domain", "init"]).exit_code == 0
        for index, worker_id in enumerate(("producer", "reviewer", "reviewer")):
            output = f"identity-output-{index}"
            created = runner.invoke(
                cli,
                ["task", "create", "--name", output, "--output", output, "--json"],
            )
            assert created.exit_code == 0, created.output
            contract_id = json.loads(created.output)["contract_id"]
            executed = runner.invoke(
                cli,
                [
                    "run",
                    "--resume",
                    contract_id,
                    "--worker",
                    "mock",
                    "--worker-id",
                    worker_id,
                    "--json",
                ],
            )
            assert executed.exit_code == 0, executed.output
            store = SQLiteStore(".aig/store.db")
            try:
                claims = [
                    record
                    for _, record in store.scan_runtime_records(
                        record_type="claim.granted"
                    )
                    if record.payload.get("contract_id") == contract_id
                ]
                assert len(claims) == 1
                assert claims[0].payload["worker_id"] == worker_id
                assets = store.get_assets_by_name(output)
                assert len(assets) == 1
                assert assets[0].signed_by == worker_id
                assert assets[0].created_by == contract_id
                worker_keys = [
                    record
                    for _, record in store.scan_runtime_records(
                        record_type="actor.authorized"
                    )
                    if record.payload.get("actor_id") == worker_id
                ]
                # Resuming with the same ID cannot create or rebind a new key.
                assert len(worker_keys) == 1
            finally:
                store.close()


def test_quick_goal_uses_explicit_worker_identity(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            cli,
            [
                "run",
                "Describe this task",
                "--worker",
                "mock",
                "--worker-id",
                "quick-author",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        store = SQLiteStore(".aig/store.db")
        try:
            assert payload["outputs"]
            for item in payload["outputs"]:
                assert store.get_asset(item["asset_id"]).signed_by == "quick-author"
        finally:
            store.close()


@pytest.mark.parametrize("worker_id", ["", "  ", "a" * 257, "\ud800"])
def test_invalid_worker_identity_fails_before_workspace_creation(tmp_path, worker_id):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        result = runner.invoke(
            cli, ["run", "--once", "--worker", "mock", "--worker-id", worker_id]
        )
        assert result.exit_code == 2, result.output
        assert "--worker-id" in result.output
        assert not Path(".aig").exists()


def test_configured_identity_cannot_be_overridden(tmp_path):
    config = tmp_path / "workers.toml"
    config.write_text("", encoding="utf-8")
    result = CliRunner().invoke(
        cli, ["run", "--config", str(config), "--worker-id", "reviewer"]
    )
    assert result.exit_code == 2, result.output
    assert "--config cannot be combined with --worker-id" in result.output


def test_explicit_llm_identity_does_not_change_the_provider_model():
    worker = build_worker("llm", model="deepseek-flash", worker_id="reviewer")
    assert worker.worker_id == "reviewer"
    assert worker.model == "deepseek-flash"
