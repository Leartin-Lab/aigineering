"""Tests for aig recover CLI command."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.ids import hash_contract, hash_contract_v3
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry
from aigineering.protocol.types import Contract
from aigineering.protocol.immutability import deep_thaw


@pytest.fixture(autouse=True)
def initialize_candidate_domain_before_recreate(monkeypatch):
    original = CliRunner.invoke

    def invoke(runner, command, args=None, *positional, **kwargs):
        effective = list(args or ())
        if (
            effective[:1] == ["recover"]
            and ({"--recreate", "--cancel"} & set(effective))
            and not Path(".aig/identity/root.ed25519").exists()
        ):
            initialized = original(runner, command, ["domain", "init"])
            assert initialized.exit_code == 0, initialized.output
        return original(runner, command, args, *positional, **kwargs)

    monkeypatch.setattr(CliRunner, "invoke", invoke)


def _make_recovery_scenario(
    db_path: str, *, acceptance_policy: dict | None = None
) -> tuple[SQLiteStore, Contract, Contract]:
    """Populate a SQLiteStore with two contracts and recovery_required trace
    entries.  Returns (store, contract_a, contract_b)."""
    store = SQLiteStore(db_path=db_path)

    identity_fields = dict(
        name="task_a",
        description="First recovery target",
        inputs=[],
        outputs=["result_a"],
        activation="",
        budget=3,
        tool_scope=[],
        labels=[],
        origin="human",
    )
    identity = hash_contract_v3 if acceptance_policy is not None else hash_contract
    if acceptance_policy is not None:
        identity_fields.update(minting_authority=[], sensitive_input_policy=None)
        identity_fields["acceptance_policy"] = acceptance_policy
    contract_a = Contract(
        id=identity(**identity_fields),
        name="task_a",
        **({"description": "First recovery target"} if acceptance_policy else {}),
        outputs=["result_a"],
        budget=3,
        activation="",
        tool_scope=[],
        labels=[],
        acceptance_policy=acceptance_policy,
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

    store.add_contract(contract_a)
    store.add_contract(contract_b)

    # Append legacy recovery_required trace entries for compatibility input.
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


def _cancelled_contract_ids(store: SQLiteStore) -> set[str]:
    return {
        str(record.payload["contract_id"])
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload["terminal"] == "cancelled"
    }


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
    """aig recover --cancel emits terminal cancellation through a Candidate."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        store, ca, cb = _make_recovery_scenario(".aig/store.db")

        result = runner.invoke(cli, ["recover", "--cancel"])

        assert result.exit_code == 0
        assert "Cancelled: 2" in result.output

        assert _cancelled_contract_ids(store) >= {ca.id, cb.id}

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


def test_recover_recreate_preserves_acceptance_policy():
    runner = CliRunner()
    policy = {
        "mode": "mechanical",
        "verifier_capabilities": ["independent-review"],
        "output_shapes": {"result_a": {"type": "object", "required": ["answer"]}},
    }
    with runner.isolated_filesystem():
        store, original, _ = _make_recovery_scenario(
            ".aig/store.db", acceptance_policy=policy
        )

        result = runner.invoke(cli, ["recover", "--recreate"])

        assert result.exit_code == 0, result.output
        recreated = store.get_contract(
            next(c.id for c in store.get_all_contracts() if c.parent_id == original.id)
        )
        assert recreated is not None
        assert deep_thaw(recreated.acceptance_policy) == policy


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

        assert _cancelled_contract_ids(store) >= {ca.id, cb.id}

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


def test_combined_recovery_resumes_after_interruption_without_repeating_effects(
    monkeypatch,
):
    import importlib

    recovery = importlib.import_module("aigineering.cli.recover")
    runner = CliRunner()
    args = ["recover", "--cancel", "--recreate", "--json"]
    with runner.isolated_filesystem():
        store, first, second = _make_recovery_scenario(".aig/store.db")
        commit = recovery.commit_local_effects
        calls = []

        def interrupt_second(*args, **kwargs):
            calls.append(tuple(effect.effect_type for effect in args[1]))
            if len(calls) == 2:
                raise ValueError("interrupted before commit")
            return commit(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(recovery, "commit_local_effects", interrupt_second)
            failed = runner.invoke(cli, args)
        assert failed.exit_code == 1
        assert calls == [("contract.declare", "contract.cancel")] * 2
        assert _cancelled_contract_ids(store) == {first.id}
        assert len(store.get_child_contracts(first.id)) == 1
        assert not store.get_child_contracts(second.id)

        retried = runner.invoke(cli, args)
        assert retried.exit_code == 0, retried.output
        assert set(json.loads(retried.output)["cancelled"]) == {first.id, second.id}
        revision = store.get_runtime_revision()
        repeated = runner.invoke(cli, args)
        assert repeated.exit_code == 0, repeated.output
        assert json.loads(repeated.output) == json.loads(retried.output)
        assert store.get_runtime_revision() == revision
        store.close()


def test_combined_recovery_finishes_legacy_half_action_without_another_reservation():
    runner = CliRunner()
    args = ["recover", "--cancel", "--recreate", "--json"]
    with runner.isolated_filesystem():
        store, _, _ = _make_recovery_scenario(".aig/store.db")
        legacy = runner.invoke(cli, ["recover", "--recreate", "--json"])
        assert legacy.exit_code == 0, legacy.output
        reservations = store.scan_runtime_records(record_type="allowance.reserved")
        result = runner.invoke(cli, args)
        assert result.exit_code == 0, result.output
        assert (
            json.loads(result.output)["recreated"]
            == json.loads(legacy.output)["recreated"]
        )
        assert (
            store.scan_runtime_records(record_type="allowance.reserved") == reservations
        )
        revision = store.get_runtime_revision()
        replayed = runner.invoke(cli, args)
        assert replayed.exit_code == 0, replayed.output
        assert json.loads(replayed.output) == json.loads(result.output)
        assert store.get_runtime_revision() == revision
        store.close()


def test_combined_recovery_rejects_unfunded_replacement_without_cancel():
    from dataclasses import replace

    from aigineering.core.control_plane import build_control_plane_contract
    from aigineering.local_publication import commit_local_effect
    from aigineering.protocol.effect_builders import contract_declaration_effect
    from aigineering.protocol.identity import contract_identity_v3

    runner = CliRunner()
    with runner.isolated_filesystem():
        assert runner.invoke(cli, ["domain", "init"]).exit_code == 0
        with SQLiteStore(".aig/store.db") as store:
            root = build_control_plane_contract(name="funded root", budget=1)
            child = replace(
                build_control_plane_contract(name="allocated elsewhere", budget=1),
                parent_id=root.id,
            )
            child = replace(child, id=contract_identity_v3(child))
            for item in (root, child):
                assert commit_local_effect(
                    store, contract_declaration_effect(item), idempotency_key=item.id
                ).accepted
            store.append(
                create_entry(
                    contract_id=root.id, event_type="recovery_required", sequence=1
                )
            )
            result = runner.invoke(cli, ["recover", "--cancel", "--recreate", "--json"])
            assert result.exit_code == 1
            assert "allowance" in result.output
            assert store.get_child_contracts(root.id) == [child]
            assert not _cancelled_contract_ids(store)


def test_combined_recovery_reports_existing_child_without_exact_publication():
    from aigineering.cli.recover import _replacement
    from aigineering.local_publication import commit_local_effect
    from aigineering.protocol.effect_builders import contract_declaration_effect

    runner = CliRunner()
    with runner.isolated_filesystem():
        store, first, second = _make_recovery_scenario(".aig/store.db")
        with store:
            assert runner.invoke(cli, ["domain", "init"]).exit_code == 0
            replacement = _replacement(first)
            assert commit_local_effect(
                store,
                contract_declaration_effect(replacement),
                idempotency_key="different-recovery-operation",
            ).accepted
            result = runner.invoke(cli, ["recover", "--cancel", "--recreate", "--json"])
            assert result.exit_code == 0, result.output
            report = json.loads(result.output)
            assert set(report["recovery_required"]) == {first.id, second.id}
            assert report["cancelled"] == [second.id]
            assert report["blocked"] == [
                {
                    "original_id": first.id,
                    "existing_recovery_ids": [replacement.id],
                    "reason": "missing_exact_recovery_publication",
                }
            ]
            assert _cancelled_contract_ids(store) == {second.id}
            assert store.get_child_contracts(first.id) == [replacement]


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
