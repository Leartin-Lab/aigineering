"""Tests for worker operational commands: package and submit."""

import json

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.ids import (
    hash_asset_content,
    hash_asset_definition,
    hash_contract,
)
from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.envelope import CandidateEnvelope
from aigineering.protocol.package import WorkerPackage
from aigineering.protocol.types import Asset, Contract, TraceEntry


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
    asset = sign_asset(
        Asset(
            id=hash_asset_content("input1", "test input content"),
            name="input1",
            content="test input content",
            definition_hash=hash_asset_definition("input1"),
            content_hash=hash_asset_content("input1", "test input content"),
            origin="human",
            trust_tier="human",
        )
    )
    store.add_asset(asset)
    return contract, asset


def _seed_continuation_with_method_context(
    store: SQLiteStore,
    *,
    disclosure_view: str = "original",
) -> tuple[Contract, Contract, Asset]:
    """Add parent, continuation, and durable method context observation."""
    parent = Contract(
        id="contract_parent",
        name="root",
        outputs=["report"],
        activation="",
        budget=5,
    )
    continuation = Contract(
        id="contract_continue",
        parent_id=parent.id,
        name="root.tool.continue",
        outputs=["report"],
        activation="",
        budget=4,
        origin="continuation",
    )
    obs_name = "_tool_obs_contract_parent"
    obs = sign_asset(
        Asset(
            id=hash_asset_content(obs_name, "value:x"),
            name=obs_name,
            content="value:x",
            definition_hash=hash_asset_definition(obs_name),
            content_hash=hash_asset_content(obs_name, "value:x"),
            origin="system",
            trust_tier="system",
            created_by="tool_contract",
            disclosure_view=disclosure_view,
        )
    )
    store.add_contract(parent)
    store.add_contract(continuation)
    store._add_system_asset(obs)
    store.append_trace_entry(
        TraceEntry(
            id="evt_continue",
            contract_id=parent.id,
            event_type="method_continuation_scheduled",
            relation_type="tool",
            relation_target=continuation.id,
            disclosed_assets=[obs.id],
        )
    )
    return parent, continuation, obs


def _valid_envelope_json(contract_id: str) -> str:
    """Build a valid CandidateEnvelope JSON string."""
    envelope = CandidateEnvelope(
        contract_id=contract_id,
        worker_id="test_worker",
        raw_output="final_report: This is the report content",
    )
    return envelope.to_json()


def _claimed_package(runner: CliRunner) -> dict:
    """Claim the next ready contract through the operational worker protocol."""
    result = runner.invoke(
        cli, ["worker", "next", "--worker-id", "cli-worker", "--json"]
    )
    assert result.exit_code == 0, f"stderr: {result.output}"
    data = json.loads(result.output)
    assert data is not None
    assert data["claim_id"]
    assert data["package_id"]
    return data


def _envelope_json_from_package(
    pkg_data: dict,
    raw_output: str = "final_report: This is the report content",
    idempotency_key: str | None = None,
) -> str:
    """Build a CandidateEnvelope bound to a worker package claim."""
    envelope = CandidateEnvelope(
        contract_id=pkg_data["contract_id"],
        worker_id="cli-worker",
        raw_output=raw_output,
        package_id=pkg_data.get("package_id", ""),
        claim_id=pkg_data.get("claim_id", ""),
        claim_epoch=int(pkg_data.get("claim_epoch", 0)),
        idempotency_key=(
            f"idem-{pkg_data['contract_id']}"
            if idempotency_key is None
            else idempotency_key
        ),
    )
    return envelope.to_json()


# ---------------------------------------------------------------------------
# Worker package tests
# ---------------------------------------------------------------------------


def test_worker_package_creation():
    """Unclaimed package command returns a non-submittable operator preview."""
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
        assert data["view"] == "operator_package_preview"
        assert data["submittable"] is False
        assert "package_id" not in data
        assert "claim_id" not in data
        assert isinstance(data["disclosed_assets"], list)
        assert len(data["disclosed_assets"]) == 1
        assert data["disclosed_assets"][0]["name"] == "input1"
        assert "content" not in data["disclosed_assets"][0]
        assert asset.content not in result.output
        assert data["tool_scope"] == []
        assert data["budget_remaining"] == 5


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


def test_worker_package_includes_continuation_method_context():
    """Operator preview includes only method-context metadata."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        _parent, continuation, obs = _seed_continuation_with_method_context(store)

        result = runner.invoke(
            cli, ["worker", "package", "--contract", continuation.id, "--json"]
        )

        assert result.exit_code == 0, f"stderr: {result.output}"
        data = json.loads(result.output)
        assert data["contract_id"] == continuation.id
        assert [a["name"] for a in data["method_context_assets"]] == [obs.name]
        assert "content" not in data["method_context_assets"][0]
        assert obs.content not in result.output


def test_worker_package_never_emits_method_context_content():
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        _parent, continuation, obs = _seed_continuation_with_method_context(
            store, disclosure_view="redacted"
        )

        result = runner.invoke(
            cli, ["worker", "package", "--contract", continuation.id, "--json"]
        )
        assert result.exit_code == 0
        context = json.loads(result.output)["method_context_assets"]
        assert context[0]["id"] == obs.id
        assert "content" not in context[0]
        assert "value:x" not in result.output


# ---------------------------------------------------------------------------
# Worker submit tests
# ---------------------------------------------------------------------------


def test_submit_creates_projection_assets():
    """claim-bound submit creates assets in store and returns accepted status."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        pkg_data = _claimed_package(runner)
        envelope_json = _envelope_json_from_package(pkg_data)
        result = runner.invoke(cli, ["worker", "submit", "--json", envelope_json])

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
        records = [record for _, record in stored.scan_runtime_records()]
        record_types = [record.record_type for record in records]
        assert "candidate.received" in record_types
        assert "projection.decided" in record_types
        assert "asset.committed" in record_types
        assert "budget.consumed" in record_types
        assert "lifecycle.terminal" in record_types
        projection = next(
            record for record in records if record.record_type == "projection.decided"
        )
        claim_submitted = next(
            record for record in records if record.record_type == "claim.submitted"
        )
        assert projection.id in claim_submitted.causal_parents


def test_submit_without_claim_rejected_for_sqlite_store():
    """SQLite operational submit is claim-bound and rejects unclaimed envelopes."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        envelope_json = _valid_envelope_json(contract.id)
        result = runner.invoke(cli, ["worker", "submit", "--json", envelope_json])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert "requires claim-bound submission" in data["error"]
        stored = SQLiteStore(".aig/store.db")
        assert stored.get_assets_by_name("final_report") == []


def test_idempotent_duplicate_submit():
    """Same idempotency key twice returns identical cached result, no duplicate assets."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        pkg_data = _claimed_package(runner)
        envelope_json = _envelope_json_from_package(pkg_data)
        idem_key = "idem-abc-123"

        # First submit
        result1 = runner.invoke(
            cli,
            [
                "worker",
                "submit",
                "--json",
                envelope_json,
                "--idempotency-key",
                idem_key,
            ],
        )
        assert result1.exit_code == 0
        data1 = json.loads(result1.output)
        assert data1["status"] == "accepted"
        assert not data1["duplicate"]

        # Second submit with same key
        result2 = runner.invoke(
            cli,
            [
                "worker",
                "submit",
                "--json",
                envelope_json,
                "--idempotency-key",
                idem_key,
            ],
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

        pkg_data = _claimed_package(runner)
        envelope_json = _envelope_json_from_package(pkg_data)

        # First submit with key A
        result1 = runner.invoke(
            cli,
            ["worker", "submit", "--json", envelope_json, "--idempotency-key", "key-A"],
        )
        assert result1.exit_code == 0

        # Second submit with key B → conflict
        result2 = runner.invoke(
            cli,
            ["worker", "submit", "--json", envelope_json, "--idempotency-key", "key-B"],
        )
        assert result2.exit_code == 0
        data2 = json.loads(result2.output)
        assert data2.get("status") == "conflict"
        assert "already has a submission" in data2.get("error", "")


def test_submit_without_idempotency_key_is_claim_bound():
    """Submit without idempotency key works once, but old claim replay is rejected."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        pkg_data = _claimed_package(runner)
        envelope_json = _envelope_json_from_package(pkg_data, idempotency_key="")

        # First submit without idempotency key
        result1 = runner.invoke(cli, ["worker", "submit", "--json", envelope_json])
        assert result1.exit_code == 0
        data1 = json.loads(result1.output)
        assert data1["status"] == "accepted"
        assert not data1["duplicate"]

        # Second submit without idempotency key reuses a submitted claim and is rejected.
        result2 = runner.invoke(cli, ["worker", "submit", "--json", envelope_json])
        assert result2.exit_code == 0
        data2 = json.loads(result2.output)
        assert data2["status"] == "error"
        assert "claim status" in data2["error"] or "claim" in data2["error"]


def test_sealed_config_not_in_output():
    """Submit output JSON never contains config_snapshot or worker_snapshot."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        contract, asset = _seed_contract_with_asset(store)

        pkg_data = _claimed_package(runner)
        envelope_json = _envelope_json_from_package(pkg_data)
        result = runner.invoke(cli, ["worker", "submit", "--json", envelope_json])

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
        result = runner.invoke(cli, ["worker", "submit", "--json", "not valid json"])
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
        result = runner.invoke(cli, ["worker", "submit", "--json", envelope.to_json()])
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

        pkg_data = _claimed_package(runner)
        # Output "secret_data" is not in contract.outputs ["final_report"]
        bad_envelope = CandidateEnvelope(
            contract_id=contract.id,
            worker_id="cli-worker",
            raw_output="secret_data: hidden result",
            package_id=pkg_data["package_id"],
            claim_id=pkg_data["claim_id"],
            claim_epoch=pkg_data["claim_epoch"],
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

        pkg_data = _claimed_package(runner)
        bad_envelope = CandidateEnvelope(
            contract_id=contract.id,
            worker_id="cli-worker",
            raw_output="undeclared: bad",
            package_id=pkg_data["package_id"],
            claim_id=pkg_data["claim_id"],
            claim_epoch=pkg_data["claim_epoch"],
        )
        result = runner.invoke(
            cli, ["worker", "submit", "--json", bad_envelope.to_json()]
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "rejected"
        assert "trace_id" in data

        stored = SQLiteStore(".aig/store.db")
        trace_entries = stored.get_trace_events(contract.id)
        assert len(trace_entries) >= 1
        event_types = [entry.event_type for entry in trace_entries]
        assert "projection" in event_types
        assert "budget_consumed" in event_types


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
        assert data["claim_id"]
        assert data["lease_until"]

        # Round-trip deserialization
        pkg = WorkerPackage.from_json(json.dumps(data))
        assert pkg.contract_id == contract.id

        persisted = SQLiteStore(".aig/store.db")
        claim = persisted.get_claim(contract.id)
        assert claim is not None
        assert claim["status"] == "active"
        assert claim["package_id"] == pkg.package_id

        second = runner.invoke(cli, ["worker", "next", "--json"])
        assert second.exit_code == 0, f"stderr: {second.output}"
        assert json.loads(second.output) is None


def test_worker_next_returns_continuation_with_method_context():
    """worker next exposes continuation method context from durable trace."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store = SQLiteStore(".aig/store.db")
        _parent, continuation, obs = _seed_continuation_with_method_context(store)

        result = runner.invoke(cli, ["worker", "next", "--json"])

        assert result.exit_code == 0, f"stderr: {result.output}"
        data = json.loads(result.output)
        assert data["contract_id"] == continuation.id
        assert [a["name"] for a in data["method_context_assets"]] == [obs.name]
        assert data["claim_id"]

        persisted = SQLiteStore(".aig/store.db")
        claim = persisted.get_claim(continuation.id)
        assert claim is not None
        assert claim["package_id"] == data["package_id"]


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
        next_result = runner.invoke(cli, ["worker", "next", "--json"])
        pkg_data = json.loads(next_result.output)
        envelope_json = _envelope_json_from_package(pkg_data)
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
        envelope_json = _envelope_json_from_package(pkg_data)
        result = runner.invoke(cli, ["worker", "submit", "--json", envelope_json])
        assert result.exit_code == 0
        submit_data = json.loads(result.output)
        assert submit_data["status"] == "accepted"
        assert len(submit_data["accepted_assets"]) == 1
        assert submit_data["accepted_assets"][0]["name"] == "final_report"

        # ── Step 3: verify trace entries ────────────────────────────────
        stored = SQLiteStore(".aig/store.db")
        event_types = [e.event_type for e in stored.get_trace_events(contract.id)]
        assert "projection" in event_types, f"trace events: {event_types}"
        assert "budget_consumed" in event_types, f"trace events: {event_types}"
        assert "complete" in event_types, f"trace events: {event_types}"

        from aigineering.agent.mock import MockWorker
        from aigineering.core.engine import Engine

        restored = Engine.restore_from_store(stored, MockWorker(), stored)
        assert restored._budget[contract.id] == contract.budget - 1

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
                assert term not in content, f"Found push semantic '{term}' in {fpath}"
