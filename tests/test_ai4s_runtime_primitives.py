"""Runtime-only AI4S/Fleet acceptance without importing the example audit code."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from aigineering.agent.tool_worker import ToolWorker
from aigineering.agent.tool_registry_loader import load_tool_registry
from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.types import Candidate, Contract


ROOT = Path(__file__).parents[1]
TOOLS = ROOT / "examples" / "ai4s" / "tools.py"
FIXTURE = (
    ROOT / "examples" / "literature-evidence" / "assets" / "openalex-response.json"
)


class _RuntimeReasoner:
    """Deterministic reasoning Worker that must consume a tool observation first."""

    def __init__(self) -> None:
        self.worker_id = "runtime:reasoner"

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(
            self.worker_id,
            capabilities=("runtime.orchestrate",),
            pools=("reasoning",),
            profile_id="runtime-test-reasoner",
        )

    def invoke(self, contract: Contract, disclosed_assets: list) -> Candidate:
        labels = set(contract.labels)
        if "plugin:plan.draft" in labels:
            content = {
                "goals": ["produce bounded evidence report"],
                "evidence_needs": ["one literature lookup"],
                "uncertainties": ["fixture is metadata-only"],
                "proposed_steps": ["retrieve", "report", "verify"],
            }
            return Candidate(
                worker_id=self.worker_id,
                raw_output="/exec "
                + json.dumps(
                    {
                        "outputs": {
                            contract.outputs[0]: json.dumps(content, sort_keys=True)
                        }
                    },
                    sort_keys=True,
                ),
            )
        if "plugin:plan.dependencies" in labels:
            content = {
                "producers": ["retrieve_report"],
                "consumers": [{"consumer": "verify_report", "input": "report"}],
                "missing_inputs": [],
                "cycles": [],
                "parallel_groups": [],
                "capability_needs": ["runtime.orchestrate", "runtime.verify"],
                "authority_risks": [],
                "allowance_needs": {"retrieve_report": 2, "verify_report": 1},
            }
            return Candidate(
                worker_id=self.worker_id,
                raw_output="/exec "
                + json.dumps(
                    {
                        "outputs": {
                            contract.outputs[0]: json.dumps(content, sort_keys=True)
                        }
                    },
                    sort_keys=True,
                ),
            )
        if "plugin:plan.compile" in labels:
            blueprint = {
                "contracts": [
                    {
                        "name": "retrieve_report",
                        "description": "Retrieve bounded literature evidence and publish the report.",
                        "inputs": [],
                        "outputs": ["report"],
                        "activation": "",
                        "budget": 2,
                        "tool_scope": ["openalex_search"],
                        "labels": [],
                        "capability_needs": ["runtime.orchestrate"],
                        "pool_needs": ["reasoning"],
                        "delegation_capabilities": [],
                        "delegation_pools": [],
                    },
                    {
                        "name": "verify_report",
                        "description": "Independently verify the exact report Asset.",
                        "inputs": ["report"],
                        "outputs": ["verification_receipt"],
                        "activation": "report",
                        "budget": 1,
                        "tool_scope": [],
                        "labels": [],
                        "capability_needs": ["runtime.verify"],
                        "pool_needs": ["verification"],
                        "delegation_capabilities": [],
                        "delegation_pools": [],
                    },
                ]
            }
            return Candidate(
                worker_id=self.worker_id,
                raw_output="/exec "
                + json.dumps(
                    {
                        "outputs": {
                            "planning_blueprint": json.dumps(blueprint, sort_keys=True)
                        }
                    },
                    sort_keys=True,
                ),
            )
        if contract.parent_id is None:
            return Candidate(
                worker_id=self.worker_id,
                raw_output='/plan {"reason":"stage the evidence and verification work"}',
                metadata={
                    "model": "offline-fixture",
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            )
        if contract.origin == "continuation":
            observations = [
                asset
                for asset in disclosed_assets
                if asset.name.startswith("tool_observation_")
            ]
            assert observations, (
                "continuation must receive a committed tool observation"
            )
            report = {
                "answer": "bounded runtime evidence",
                "citations": ["https://openalex.org/W0000000001"],
                "limitations": ["metadata only"],
            }
            return Candidate(
                worker_id=self.worker_id,
                raw_output="/exec "
                + json.dumps(
                    {"outputs": {"report": json.dumps(report, sort_keys=True)}}
                ),
            )
        return Candidate(
            worker_id=self.worker_id,
            raw_output='/tool {"name":"openalex_search","args":{"query":"runtime evidence","max_records":1}}',
        )


class _RuntimeVerifier:
    """Verifier Worker that submits /attest through WorkerHost compilation."""

    def __init__(self, target_contract_id: str) -> None:
        self.worker_id = "runtime:verifier"
        self.target_contract_id = target_contract_id

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(
            self.worker_id,
            capabilities=("runtime.verify",),
            pools=("verification",),
            profile_id="runtime-test-verifier",
        )

    def invoke(self, contract: Contract, disclosed_assets: list) -> Candidate:
        report = next(asset for asset in disclosed_assets if asset.name == "report")
        payload = {
            "contract_id": self.target_contract_id,
            "output_name": "report",
            "asset_id": report.id,
            "verdict": "accepted",
            "outputs": {"verification_receipt": "report accepted"},
        }
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/attest " + json.dumps(payload, sort_keys=True),
        )


def _write_fleet_config(path: Path, registry_ref: str) -> None:
    path.write_text(
        """
[fleet]
db_path = ".aig/store.db"
poll_interval = 0.01

[[workers]]
id = "runtime:reasoner"
kind = "llm"
model = "offline-fixture"
capabilities = ["runtime.orchestrate"]
pools = ["reasoning"]

[[workers]]
id = "runtime:tools"
kind = "tool"
tool_registry = "{registry_ref}"

[[workers]]
id = "runtime:verifier"
kind = "llm"
model = "offline-fixture"
capabilities = ["runtime.verify"]
pools = ["verification"]
effect_capabilities = ["asset.attest", "runtime.verify"]
""".format(registry_ref=registry_ref).strip(),
        encoding="utf-8",
    )


def test_runtime_ai4s_tool_continuation_attest_reopen_via_fleet_cli(
    tmp_path: Path, monkeypatch
) -> None:
    """Exercise the real tool/continuation/attestation loop through Fleet CLI."""
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cli, ["domain", "init"]).exit_code == 0

    policy = json.dumps(
        {
            "mode": "independent",
            "policy_version": "runtime-ai4s-v1",
            "required_attestations": 1,
            "verifier_capabilities": ["runtime.verify"],
            "output_shapes": {
                "report": {
                    "answer": "nonempty_string",
                    "citations": ["nonempty_string"],
                    "limitations": ["nonempty_string"],
                }
            },
        },
        separators=(",", ":"),
    )
    created = runner.invoke(
        cli,
        [
            "task",
            "create",
            "--name",
            "runtime_ai4s",
            "--description",
            "Use one bounded retrieval tool, then publish and verify a report.",
            "--output",
            "report",
            "--budget",
            "10",
            "--tool",
            "openalex_search",
            "--requires-capability",
            "runtime.orchestrate",
            "--worker-pool",
            "reasoning",
            "--delegate-capability",
            "runtime.orchestrate",
            "--delegate-capability",
            "runtime.verify",
            "--delegate-pool",
            "reasoning",
            "--delegate-pool",
            "verification",
            "--acceptance-policy",
            policy,
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    root_id = json.loads(created.output)["contract_id"]

    registry = load_tool_registry(f"{TOOLS}:build_registry")

    def build_worker(spec):
        if spec.worker_id == "runtime:reasoner":
            return _RuntimeReasoner()
        if spec.worker_id == "runtime:verifier":
            return _RuntimeVerifier(root_id)
        assert spec.worker_id == "runtime:tools"
        return ToolWorker(registry, worker_id=spec.worker_id)

    monkeypatch.setattr("aigineering.cli.fleet.build_fleet_worker", build_worker)
    config = tmp_path / "workers.toml"
    _write_fleet_config(config, f"{TOOLS}:build_registry")
    result = runner.invoke(
        cli,
        [
            "fleet",
            "run",
            "--config",
            str(config),
            "--task",
            root_id,
            "--wait-timeout",
            "5",
            "--json",
        ],
        env={
            "AIGINEERING_AI4S_OPENALEX_FIXTURE": str(FIXTURE),
            "AIGINEERING_AI4S_RETRIEVED_AT": "2026-08-23T00:00:00+00:00",
        },
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "complete"

    reopened = SQLiteStore(".aig/store.db")
    try:
        restored = reopened.get_contract(root_id)
        assert restored is not None
        status = runner.invoke(cli, ["task", "status", root_id, "--json"])
        assert status.exit_code == 0, status.output
        status_payload = json.loads(status.output)
        assert status_payload["status"] == "completed"
        audit = runner.invoke(cli, ["task", "audit", root_id, "--json"])
        assert audit.exit_code == 0, audit.output
        productivity = json.loads(audit.output)["productivity"]
        assert productivity["tool_calls"]["succeeded"] == 1
        tool_usage = next(
            row["usage"]
            for row in productivity["usage_records"]
            if row["kind"] == "tool" and row["usage"].get("tool") == "openalex_search"
        )
        assert tool_usage["contract_id"]
        assert tool_usage["tool_version"] == "1.0.0"
        assert isinstance(tool_usage["duration_ms"], int)
        assert tool_usage["duration_ms"] >= 0
        assert tool_usage["result_bytes"] > 0
        assert tool_usage["error_type"] == ""
        assert tool_usage["retryable"] is False
        assert productivity["token_totals"] == {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        }
        contracts = reopened.get_all_contracts()
        assert any("plugin:plan.compile" in item.labels for item in contracts)
        producer = next(item for item in contracts if item.name == "retrieve_report")
        verifier = next(item for item in contracts if item.name == "verify_report")
        assert producer.parent_id == verifier.parent_id
        assert producer.tool_scope == ("openalex_search",)
        assert any(item.origin == "continuation" for item in contracts)
        attestations = reopened.scan_runtime_records(record_type="asset.attested")
        assert any(
            record.payload.get("contract_id") == root_id for _, record in attestations
        )
        assert reopened.scan_runtime_records(record_type="output.qualified")
        assert reopened.get_assets_by_name("report")

        before_digest = reopened.runtime_materialization_digest()
        rebuilt_digest = reopened.rebuild_runtime_materializations()
        assert rebuilt_digest == before_digest
        rebuilt_audit = runner.invoke(cli, ["task", "audit", root_id, "--json"])
        assert rebuilt_audit.exit_code == 0, rebuilt_audit.output
        rebuilt_payload = json.loads(rebuilt_audit.output)
        assert rebuilt_payload["task"]["status"] == "completed"
        assert rebuilt_payload["productivity"] == productivity
    finally:
        reopened.close()
