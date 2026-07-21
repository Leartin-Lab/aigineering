"""CLI proof for Contract-bound independent output acceptance."""

from __future__ import annotations

import json

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_worker_host
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package


def test_cli_independent_output_requires_separate_attestation():
    runner = CliRunner()
    with runner.isolated_filesystem():
        initialized = runner.invoke(cli, ("domain", "init"))
        assert initialized.exit_code == 0, initialized.output
        policy = json.dumps(
            {
                "mode": "independent",
                "policy_version": "review-v1",
                "required_attestations": 1,
                "verifier_capabilities": ["verify.human"],
            }
        )
        created = runner.invoke(
            cli,
            (
                "task",
                "create",
                "--name",
                "reviewed-report",
                "--output",
                "report",
                "--acceptance-policy",
                policy,
                "--json",
            ),
        )
        assert created.exit_code == 0, created.output
        contract_id = json.loads(created.output)["contract_id"]
        store = SQLiteStore(".aig/store.db")

        class _HumanProducer:
            worker_id = "human:producer"

            def invoke(self, contract, disclosed_assets):
                del contract, disclosed_assets
                return Candidate(
                    worker_id=self.worker_id,
                    raw_output='/exec {"outputs":{"report":"candidate report"}}',
                )

        host = ensure_local_worker_host(store, _HumanProducer())
        claimed = claim_next_package(store, worker_id=host.worker_id)
        assert claimed is not None
        result = execute_claimed_package(claimed, host, store)
        assert result["status"] == "accepted"
        asset_id = store.get_assets_by_name("report")[0].id
        store.close()

        before = runner.invoke(cli, ("task", "status", contract_id, "--json"))
        assert before.exit_code == 0, before.output
        assert json.loads(before.output)["ok"] is False

        attested = runner.invoke(
            cli,
            (
                "verify",
                "attest",
                "--contract",
                contract_id,
                "--output",
                "report",
                "--asset",
                asset_id,
                "--json",
            ),
        )
        assert attested.exit_code == 0, attested.output
        assert json.loads(attested.output)["qualified"] is True

        after = runner.invoke(cli, ("task", "status", contract_id, "--json"))
        assert after.exit_code == 0, after.output
        status = json.loads(after.output)
        assert status["ok"] is True
        assert status["status"] == "completed"
        assert status["terminal"] is True
