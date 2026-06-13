"""Tests for worker operational commands: package and submit."""

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.idempotency_store import IdempotencyStore
from aigineering.core.ids import hash_asset_content, hash_asset_definition, hash_contract
from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import contract_to_dict


def _seed_contract(store: SQLiteStore) -> Contract:
    """Add a test contract and return it."""
    contract = Contract(
        id=hash_contract(
            name="test_contract",
            description="A test contract",
            inputs=["input1"],
            outputs=["final_report"],
            activation="input1",
            budget=5,
            tool_scope=[],
            labels=[],
            origin="human",
        ),
        name="test_contract",
        inputs=["input1"],
        outputs=["final_report"],
        activation="input1",
        budget=5,
    )
    store.add_contract(contract)
    return contract


def _seed_contract_with_asset(store: SQLiteStore) -> tuple[Contract, Asset]:
    """Add a test contract and an input asset, return both."""
    contract = _seed_contract(store)
    asset = sign_asset(Asset(
        id=hash_asset_content("input1", "test input content"),
        name="input1",
        content="test input content",
        definition_hash=hash_asset_definition("input1"),
        content_hash=hash_asset_content("input1", "test input content"),
        origin="human",
        trust_tier="human",
    ))
    store.add_asset(asset)
    return contract, asset


def _valid_envelope_json(contract_id: str) -> str:
    """Build a valid CandidateEnvelope JSON string."""
    envelope = CandidateEnvelope(
        contract_id=contract_id,
        worker_id="test_worker",
        raw_output="final_report: This is the report content",
    )
    return envelope.to_json()


# ---------------------------------------------------------------------------
# Worker package tests
# ---------------------------------------------------------------------------


def test_worker_package_creation():
    """aig worker package --contract <id> --json returns valid WorkerPackage JSON."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        result = runner.invoke(
            cli, ["worker", "package", "--contract", contract.id, "--json"]
        )

        assert result.exit_code == 0, f"stderr: {result.output}"

        data = json.loads(result.output)
        assert data["contract_id"] == contract.id
        assert isinstance(data["contract"], dict)
        assert data["contract"]["name"] == "test_contract"
        assert isinstance(data["disclosed_assets"], list)
        assert len(data["disclosed_assets"]) == 1
        assert data["disclosed_assets"][0]["name"] == "input1"
        assert data["tool_scope"] == []
        assert data["budget_remaining"] == 5

        # Round-trip: deserialize should succeed
        pkg = WorkerPackage.from_json(json.dumps(data))
        assert pkg.contract_id == contract.id


def test_worker_package_missing_contract():
    """aig worker package --contract <nonexistent> reports error."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli, ["worker", "package", "--contract", "nonexistent", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "error" in data
        assert "not found" in data["error"]


# ---------------------------------------------------------------------------
# Worker submit tests
# ---------------------------------------------------------------------------


def test_submit_creates_projection_assets():
    """valid submit creates assets in store and returns accepted status."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        envelope_json = _valid_envelope_json(contract.id)
        result = runner.invoke(
            cli, ["worker", "submit", "--json", envelope_json]
        )

        assert result.exit_code == 0, f"stderr: {result.output}"

        data = json.loads(result.output)
        assert data["contract_id"] == contract.id
        assert data["status"] == "accepted"
        assert not data["duplicate"]
        assert "accepted_assets" in data
        assert len(data["accepted_assets"]) == 1
        assert data["accepted_assets"][0]["name"] == "final_report"
        assert "trace_id" in data

        # Verify asset is actually in the store
        stored = SQLiteStore(".aig/store.db")
        assets = stored.get_assets_by_name("final_report")
        assert len(assets) == 1
        assert assets[0].name == "final_report"


def test_idempotent_duplicate_submit():
    """Same idempotency key twice returns identical cached result, no duplicate assets."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        envelope_json = _valid_envelope_json(contract.id)
        idem_key = "idem-abc-123"

        # First submit
        result1 = runner.invoke(
            cli, ["worker", "submit", "--json", envelope_json,
                  "--idempotency-key", idem_key]
        )
        assert result1.exit_code == 0
        data1 = json.loads(result1.output)
        assert data1["status"] == "accepted"
        assert not data1["duplicate"]

        # Second submit with same key
        result2 = runner.invoke(
            cli, ["worker", "submit", "--json", envelope_json,
                  "--idempotency-key", idem_key]
        )
        assert result2.exit_code == 0
        data2 = json.loads(result2.output)
        assert data2["duplicate"] is True
        # Same accepted assets (no new assets created)
        assert data2["accepted_assets"] == data1["accepted_assets"]
        assert data2["status"] == data1["status"]

        # Verify only one asset exists (not duplicated)
        stored = SQLiteStore(".aig/store.db")
        assets = stored.get_assets_by_name("final_report")
        assert len(assets) == 1


def test_different_key_conflict():
    """Different idempotency key after existing submission returns conflict error."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        envelope_json = _valid_envelope_json(contract.id)

        # First submit with key A
        result1 = runner.invoke(
            cli, ["worker", "submit", "--json", envelope_json,
                  "--idempotency-key", "key-A"]
        )
        assert result1.exit_code == 0

        # Second submit with key B → conflict
        result2 = runner.invoke(
            cli, ["worker", "submit", "--json", envelope_json,
                  "--idempotency-key", "key-B"]
        )
        assert result2.exit_code == 0
        data2 = json.loads(result2.output)
        assert data2.get("status") == "conflict"
        assert "already has a submission" in data2.get("error", "")


def test_submit_without_idempotency_key():
    """Submit without idempotency key works, but duplicate detection is disabled."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        envelope_json = _valid_envelope_json(contract.id)

        # First submit without idempotency key
        result1 = runner.invoke(
            cli, ["worker", "submit", "--json", envelope_json]
        )
        assert result1.exit_code == 0
        data1 = json.loads(result1.output)
        assert data1["status"] == "accepted"
        assert not data1["duplicate"]

        # Second submit without idempotency key (no dedup)
        result2 = runner.invoke(
            cli, ["worker", "submit", "--json", envelope_json]
        )
        assert result2.exit_code == 0
        data2 = json.loads(result2.output)
        assert data2["status"] == "accepted"
        assert not data2["duplicate"]


def test_sealed_config_not_in_output():
    """Submit output JSON never contains config_snapshot or worker_snapshot."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        envelope_json = _valid_envelope_json(contract.id)
        result = runner.invoke(
            cli, ["worker", "submit", "--json", envelope_json]
        )

        assert result.exit_code == 0

        data = json.loads(result.output)
        output_str = json.dumps(data)
        assert "config_snapshot" not in output_str
        assert "worker_snapshot" not in output_str

        # Also check nested accepted_assets for sealed fields
        for a in data.get("accepted_assets", []):
            a_str = json.dumps(a)
            assert "config_snapshot" not in a_str


def test_submit_invalid_envelope():
    """Submit with invalid envelope JSON returns error."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli, ["worker", "submit", "--json", "not valid json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "error" in data


def test_submit_missing_contract():
    """Submit to nonexistent contract returns error."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        envelope = CandidateEnvelope(
            contract_id="nonexistent_contract",
            worker_id="test_worker",
            raw_output="result: ok",
        )
        result = runner.invoke(
            cli, ["worker", "submit", "--json", envelope.to_json()]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "error" in data
        assert "not found" in data["error"]


def test_submit_rejected_candidate():
    """Submit candidate with undeclared output returns rejected status."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        # Output "secret_data" is not in contract.outputs ["final_report"]
        bad_envelope = CandidateEnvelope(
            contract_id=contract.id,
            worker_id="test_worker",
            raw_output="secret_data: hidden result",
        )
        result = runner.invoke(
            cli, ["worker", "submit", "--json", bad_envelope.to_json()]
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "rejected"
        assert len(data["accepted_assets"]) == 0
        assert len(data["rejected_candidates"]) > 0


def test_submit_rejected_preserves_trace():
    """Rejected submissions still record a trace entry."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        bad_envelope = CandidateEnvelope(
            contract_id=contract.id,
            worker_id="test_worker",
            raw_output="undeclared: bad",
        )
        result = runner.invoke(
            cli, ["worker", "submit", "--json", bad_envelope.to_json()]
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "rejected"
        assert "trace_id" in data

        # Check trace file exists
        trace_file = Path(".aig/traces") / f"worker_{contract.id}.jsonl"
        assert trace_file.exists()

        with open(trace_file) as f:
            lines = [line for line in f if line.strip()]
        assert len(lines) >= 1


# ---------------------------------------------------------------------------
# Worker next tests
# ---------------------------------------------------------------------------


def test_worker_next_returns_package():
    """aig worker next --json returns a WorkerPackage for a ready contract."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        result = runner.invoke(cli, ["worker", "next", "--json"])

        assert result.exit_code == 0, f"stderr: {result.output}"

        data = json.loads(result.output)
        # Should return a package, not null
        assert data is not None
        assert data["contract_id"] == contract.id
        assert isinstance(data["contract"], dict)
        assert data["contract"]["name"] == "test_contract"
        assert isinstance(data["disclosed_assets"], list)
        assert len(data["disclosed_assets"]) == 1
        assert data["disclosed_assets"][0]["name"] == "input1"
        assert data["budget_remaining"] == contract.budget

        # Round-trip deserialization
        pkg = WorkerPackage.from_json(json.dumps(data))
        assert pkg.contract_id == contract.id


def test_worker_next_null_when_idle():
    """aig worker next --json returns null when no ready contracts exist."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        # No contracts in store → null
        result = runner.invoke(cli, ["worker", "next", "--json"])
        assert result.exit_code == 0

        data = json.loads(result.output)
        assert data is None


def test_worker_next_skips_completed_contract():
    """worker next does not return a contract whose outputs are all satisfied."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        # Submit once to satisfy the output
        envelope_json = _valid_envelope_json(contract.id)
        runner.invoke(cli, ["worker", "submit", "--json", envelope_json])

        # Now next should return null (contract completed)
        result = runner.invoke(cli, ["worker", "next", "--json"])
        assert result.exit_code == 0

        data = json.loads(result.output)
        assert data is None


def test_worker_next_submit_full_cycle():
    """Full cycle: next → submit → trace entries include projection and complete."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        # ── Step 1: worker next — get a package ─────────────────────────
        result = runner.invoke(cli, ["worker", "next", "--json"])
        assert result.exit_code == 0
        pkg_data = json.loads(result.output)
        assert pkg_data is not None
        assert pkg_data["contract_id"] == contract.id

        # ── Step 2: worker submit — send candidate result ───────────────
        envelope_json = _valid_envelope_json(contract.id)
        result = runner.invoke(
            cli, ["worker", "submit", "--json", envelope_json]
        )
        assert result.exit_code == 0
        submit_data = json.loads(result.output)
        assert submit_data["status"] == "accepted"
        assert len(submit_data["accepted_assets"]) == 1
        assert submit_data["accepted_assets"][0]["name"] == "final_report"

        # ── Step 3: verify trace entries ────────────────────────────────
        trace_file = Path(".aig/traces") / f"worker_{contract.id}.jsonl"
        assert trace_file.exists()

        with open(trace_file) as f:
            lines = [json.loads(line) for line in f if line.strip()]

        event_types = [e["event_type"] for e in lines]
        assert "projection" in event_types, f"trace events: {event_types}"
        assert "complete" in event_types, f"trace events: {event_types}"

        # ── Step 4: next after completion returns null ──────────────────
        result = runner.invoke(cli, ["worker", "next", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data is None


def test_no_push_semantics():
    """Verify no push_work or dispatch_work functions exist in the codebase."""
    import os

    src_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "src", "aigineering"
    )

    forbidden = {"push_work", "dispatch_work", "push_contract", "auto_assign"}

    for root, dirs, files in os.walk(src_dir):
        # Skip __pycache__
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            with open(fpath, "r") as f:
                content = f.read()
            for term in forbidden:
                assert term not in content, (
                    f"Found push semantic '{term}' in {fpath}"
                )
