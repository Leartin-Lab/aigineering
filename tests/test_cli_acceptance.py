"""CLI proof for Contract-bound independent output acceptance."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.application import collaboration_service
from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_worker_host
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package


@pytest.mark.parametrize("relay_interrupted", [False, True])
def test_cli_independent_output_requires_separate_attestation(
    monkeypatch, relay_interrupted
):
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

        command = (
            "verify",
            "attest",
            "--contract",
            contract_id,
            "--output",
            "report",
            "--asset",
            asset_id,
            "--json",
        )

        with monkeypatch.context() as relay:
            if relay_interrupted:
                synchronized = runner.invoke(cli, ("redis", "sync", "--json"))
                assert synchronized.exit_code == 0, synchronized.output
                with SQLiteStore.snapshot_from_path(".aig/store.db") as persisted:
                    before_watermark = collaboration_service(persisted).status()[
                        "watermark"
                    ]

                class InterruptedRelay:
                    def sync(self):
                        raise QueryProjectionUnavailable(
                            "attestation relay interrupted"
                        )

                relay.setattr(
                    "aigineering.runtime_services.collaboration_service",
                    lambda _store: InterruptedRelay(),
                )
            attested = runner.invoke(cli, command)

        with SQLiteStore.snapshot_from_path(".aig/store.db") as persisted:
            committed_revision = persisted.get_runtime_revision()
            relay_status = collaboration_service(persisted).status()
            if relay_interrupted:
                assert attested.exit_code == 1
                pending = json.loads(attested.output)
                assert pending["error"] == "redis_sync_pending"
                assert pending["facts_retained"] and pending["candidate_accepted"]
                assert pending["contract_ids"] == [contract_id]
                assert pending["asset_ids"] == [asset_id]
                assert pending["revision"] == committed_revision
                assert (
                    relay_status["watermark"] == before_watermark < committed_revision
                )
                history = {
                    record.record_type: record
                    for record in persisted.read_candidate_publication(
                        pending["candidate_id"]
                    )
                }
                assert pending["receipt_id"] == history["candidate.received"].id
                assert pending["head_id"] == history["candidate.committed"].id
                assert history["asset.attested"].payload["asset_id"] == asset_id
                assert "candidate report" not in attested.output
            else:
                assert relay_status["watermark"] == committed_revision

        if relay_interrupted:
            synchronized = runner.invoke(cli, ("redis", "sync", "--json"))
            assert synchronized.exit_code == 0, synchronized.output
            attested = runner.invoke(cli, command)
            with SQLiteStore.snapshot_from_path(".aig/store.db") as persisted:
                assert persisted.get_runtime_revision() == committed_revision
                assert (
                    collaboration_service(persisted).status()["watermark"]
                    == committed_revision
                )
                assert (
                    len(persisted.scan_runtime_records(record_type="asset.attested"))
                    == 1
                )
        assert attested.exit_code == 0, attested.output
        assert json.loads(attested.output)["qualified"] is True

        after = runner.invoke(cli, ("task", "status", contract_id, "--json"))
        assert after.exit_code == 0, after.output
        status = json.loads(after.output)
        assert status["ok"] is True
        assert status["status"] == "completed"
        assert status["terminal"] is True
