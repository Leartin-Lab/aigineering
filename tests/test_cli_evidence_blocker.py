"""A missing declared input blocks an ordinary target run observably."""

from __future__ import annotations

import json

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore


def test_missing_declared_input_blocks_cli_target_without_claim_or_output(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        created = runner.invoke(
            cli,
            [
                "task",
                "create",
                "--name",
                "missing_evidence",
                "--input",
                "evidence",
                "--output",
                "report",
                "--activation",
                "evidence",
                "--json",
            ],
        )
        assert created.exit_code == 0, created.output
        root_id = json.loads(created.output)["contract_id"]

        result = runner.invoke(
            cli, ["run", "--task", root_id, "--worker", "mock", "--json"]
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["contract_id"] == root_id
        assert payload["status"] == "blocked"
        assert payload["stop_reason"] == "idle"
        assert payload["timed_out"] is False
        assert any("evidence" in str(blocker) for blocker in payload["blockers"])
        assert payload["next_step"]

        status = runner.invoke(cli, ["task", "status", root_id, "--json"])
        assert status.exit_code == 0, status.output
        status_payload = json.loads(status.output)
        assert status_payload["terminal"] is False
        assert status_payload["status"] == "blocked"
        assert any("evidence" in str(blocker) for blocker in status_payload["blockers"])

        store = SQLiteStore(".aig/store.db")
        try:
            assert store.get_assets_by_name("report") == []
            assert not [
                record
                for _, record in store.scan_runtime_records(record_type="claim.granted")
                if record.payload.get("contract_id") == root_id
            ]
            assert not [
                record
                for _, record in store.scan_runtime_records(
                    record_type="output.qualified"
                )
                if record.payload.get("contract_id") == root_id
            ]
        finally:
            store.close()
