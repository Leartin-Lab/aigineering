"""Tests for aig recover CLI command."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.ids import hash_contract, hash_contract_v3
from aigineering.core.runtime_ingress import RuntimeIngress
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry
from aigineering.protocol.types import Contract


@pytest.fixture(autouse=True)
def initialize_candidate_domain_before_recreate(monkeypatch):
    original = CliRunner.invoke

    def invoke(runner, command, args=None, *positional, **kwargs):
        effective = list(args or ())
        if (
            effective[:1] == ["recover"]
            and "--recreate" in effective
            and not Path(".aig/identity/root.ed25519").exists()
        ):
            initialized = original(runner, command, ["domain", "init"])
            assert initialized.exit_code == 0, initialized.output
        return original(runner, command, args, *positional, **kwargs)

    monkeypatch.setattr(CliRunner, "invoke", invoke)


def _make_recovery_scenario(db_path: str) -> tuple[SQLiteStore, Contract, Contract]:
    """Populate a SQLiteStore with two contracts and recovery_required trace
    entries.  Returns (store, contract_a, contract_b)."""
    store = SQLiteStore(db_path=db_path)

    # Create two contracts via RuntimeIngress so they are properly registered.
    ingress = RuntimeIngress(store, store)

    contract_a = Contract(
        id=hash_contract(
            name="task_a",
            description="First recovery target",
            inputs=[],
            outputs=["result_a"],
            activation="",
            budget=3,
            tool_scope=[],
            labels=[],
            origin="human",
        ),
        name="task_a",
        outputs=["result_a"],
        budget=3,
    )
    contract_b = Contract(
        id=hash_contract(
            name="task_b",
            description="Second recovery target",
            inputs=[],
            outputs=["result_b"],
            activation="",
            budget=5,
            tool_scope=[],
            labels=[],
            origin="human",
        ),
        name="task_b",
        outputs=["result_b"],
        budget=5,
    )

    ingress.accept_contract(contract_a)
    ingress.accept_contract(contract_b)

    # Append recovery_required trace entries (like startup_check does).
    for c in (contract_a, contract_b):
        entry = create_entry(
            contract_id=c.id,
            event_type="recovery_required",
            sequence=len(store.get_all()),
            worker_id="test",
            relation_type="startup_self_check",
            relation_target="test_runtime",
            rejected_fragments=[
                "[recovery_required] startup_self_check: "
                "orphaned runtime(s); unclaimed claimable task marked for recovery"
            ],
        )
        store.append(entry)

    return store, contract_a, contract_b


# ---------------------------------------------------------------------------
# Default (list-only) behaviour
# ---------------------------------------------------------------------------


def test_recover_lists_recovery_required():
    """aig recover lists contracts with recovery_required trace events."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        _make_recovery_scenario(".aig/store.db")

        result = runner.invoke(cli, ["recover"])

        assert result.exit_code == 0
        assert "Recovery-required contracts: 2" in result.output
        assert "task_a" in result.output
        assert "task_b" in result.output


def test_recover_json_lists_recovery_required():
    """aig recover --json returns JSON with recovery_required contract IDs."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store, ca, cb = _make_recovery_scenario(".aig/store.db")

        result = runner.invoke(cli, ["recover", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["recovery_required"]) == 2
        assert ca.id in data["recovery_required"]
        assert cb.id in data["recovery_required"]
        assert data["cancelled"] == []
        assert data["recreated"] == []


def test_recover_no_recovery_required():
    """aig recover returns empty when no recovery_required entries exist."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["recover"])

        assert result.exit_code == 0
        assert "No recovery-required contracts found." in result.output


def test_recover_json_no_recovery_required():
    """aig recover --json returns empty-lists JSON when nothing to recover."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["recover", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["recovery_required"] == []
        assert data["cancelled"] == []
        assert data["recreated"] == []


# ---------------------------------------------------------------------------
# --cancel
# ---------------------------------------------------------------------------


def test_recover_cancel():
    """aig recover --cancel emits terminal cancellation through method ingress."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store, ca, cb = _make_recovery_scenario(".aig/store.db")

        result = runner.invoke(cli, ["recover", "--cancel"])

        assert result.exit_code == 0
        assert "Cancelled: 2" in result.output

        cancelled = store.get_by_event_type("cancelled")
        assert {entry.contract_id for entry in cancelled} >= {ca.id, cb.id}
        assert all(entry.relation_type == "recover" for entry in cancelled)

        follow_up = runner.invoke(cli, ["recover", "--json"])
        assert follow_up.exit_code == 0
        assert json.loads(follow_up.output)["recovery_required"] == []


def test_recover_cancel_json():
    """aig recover --cancel --json returns JSON with cancelled contract IDs."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store, ca, cb = _make_recovery_scenario(".aig/store.db")

        result = runner.invoke(cli, ["recover", "--cancel", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["cancelled"]) == 2
        assert ca.id in data["cancelled"]
        assert cb.id in data["cancelled"]


# ---------------------------------------------------------------------------
# --recreate
# ---------------------------------------------------------------------------


def test_recover_recreate():
    """aig recover --recreate creates new replacement contracts."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store, ca, cb = _make_recovery_scenario(".aig/store.db")

        result = runner.invoke(cli, ["recover", "--recreate"])

        assert result.exit_code == 0
        assert "Recreated: 2" in result.output

        # Verify new contracts exist in store
        new_contracts = store.get_all_contracts()
        new_ids = {c.id for c in new_contracts}
        # Should have 4 contracts now (2 originals + 2 recreated)
        assert len(new_contracts) == 4
        assert ca.id in new_ids
        assert cb.id in new_ids

        # Find the recreated contracts (they have parent_id set)
        recreated = [c for c in new_contracts if c.parent_id is not None]
        assert len(recreated) == 2

        # Verify they have expected origin
        for c in recreated:
            assert c.origin == "recovery"
            assert c.parent_id in (ca.id, cb.id)
            # Same name as original
            assert c.name in ("task_a", "task_b")

        # Verify IDs bind the full recreated recovery entity.
        for c in recreated:
            expected_id = hash_contract_v3(
                name=c.name,
                description="",  # original has no description
                inputs=[],
                outputs=list(c.outputs),
                activation="",
                budget=c.budget,
                tool_scope=[],
                labels=[],
                origin="recovery",
                parent_id=c.parent_id,
            )
            assert c.id == expected_id
            actual = store.get_contract(c.id)
            assert actual is not None


def test_recover_recreate_json():
    """aig recover --recreate --json returns mapping of original→new IDs."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store, ca, cb = _make_recovery_scenario(".aig/store.db")

        result = runner.invoke(cli, ["recover", "--recreate", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["recreated"]) == 2

        orig_ids = {r["original_id"] for r in data["recreated"]}
        assert ca.id in orig_ids
        assert cb.id in orig_ids

        new_ids = {r["new_id"] for r in data["recreated"]}
        for nid in new_ids:
            assert store.get_contract(nid) is not None, (
                f"Recreated contract {nid} not found in store"
            )
            actual = store.get_contract(nid)
            assert actual is not None
            assert actual.origin == "recovery"
            assert actual.parent_id in orig_ids


# ---------------------------------------------------------------------------
# Combined flags
# ---------------------------------------------------------------------------


def test_recover_cancel_and_recreate():
    """aig recover --cancel --recreate both cancels and recreates."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store, ca, cb = _make_recovery_scenario(".aig/store.db")

        result = runner.invoke(cli, ["recover", "--cancel", "--recreate"])

        assert result.exit_code == 0
        assert "Cancelled: 2" in result.output
        assert "Recreated: 2" in result.output

        cancelled = store.get_by_event_type("cancelled")
        assert {entry.contract_id for entry in cancelled} >= {ca.id, cb.id}

        # Verify new contracts (2 originals + 2 recreated = 4)
        assert len(store.get_all_contracts()) == 4


def test_recover_cancel_and_recreate_json():
    """aig recover --cancel --recreate --json returns both lists."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store, ca, cb = _make_recovery_scenario(".aig/store.db")

        result = runner.invoke(cli, ["recover", "--cancel", "--recreate", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["cancelled"]) == 2
        assert len(data["recreated"]) == 2
        assert len(data["recovery_required"]) == 2


# ---------------------------------------------------------------------------
# Guard: no flags = list-only (no mutation)
# ---------------------------------------------------------------------------


def test_recover_default_does_not_mutate():
    """aig recover with no flags only lists, never mutates state."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store, ca, cb = _make_recovery_scenario(".aig/store.db")

        # Count trace and contract entries before
        before_traces = len(store.get_all())
        before_contracts = len(store.get_all_contracts())

        result = runner.invoke(cli, ["recover"])

        assert result.exit_code == 0

        after_traces = len(store.get_all())
        after_contracts = len(store.get_all_contracts())

        # No mutation
        assert after_traces == before_traces
        assert after_contracts == before_contracts

        # No cancelled or recreated entries
        assert "Cancelled:" not in result.output
        assert "Recreated:" not in result.output
