"""Bulk reassignment uses one exact signed assignment per target."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner
import pytest

from aigineering.cli.task_assignment import register_task_assignment_commands
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.local_identity import ensure_local_domain, load_actor_signer
from aigineering.protocol.effect_builders import contract_declaration_effect


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_assignment_commands(task)
    root.add_command(task)
    return root


def _workspace(tmp_path: Path, monkeypatch, count: int = 3):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key = database.parent / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key))
    store = SQLiteStore(database)
    genesis = ensure_local_domain(store)
    root_signer = load_actor_signer(root_key)
    contracts = []
    for index in range(count):
        contract = build_control_plane_contract(
            name=f"bulk reassignment target {index}",
            description="A bulk reassignment test target.",
            outputs=("result",),
            budget=3,
        )
        declaration = publish_effect(
            store,
            store,
            genesis,
            genesis.root_keys[0],
            root_signer,
            contract_declaration_effect(contract),
            idempotency_key=f"reassignment-contract-{index}",
        )
        assert declaration.accepted
        contracts.append(contract)
    store.register_worker(WorkerRegistration("worker:one", capacity=4))
    store.register_worker(WorkerRegistration("worker:two", capacity=4))
    store.close()

    import aigineering.application
    import aigineering.cli.task_assignment as cli_assignment
    import aigineering.task_assignment as app_assignment

    class _Collaboration:
        def sync(self):
            return None

    for module in (aigineering.application, cli_assignment, app_assignment):
        monkeypatch.setattr(module, "require_redis", lambda: None)
        monkeypatch.setattr(
            module, "collaboration_service", lambda _store: _Collaboration()
        )
    monkeypatch.setattr(app_assignment, "sync_runtime", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        cli_assignment,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        cli_assignment, "_persistent_store", lambda: SQLiteStore(database)
    )
    return database, tuple(contracts)


def _json_result(runner: CliRunner, args: list[str], *, expected: int = 0) -> dict:
    result = runner.invoke(_app(), args)
    assert result.exit_code == expected, result.output
    return json.loads(result.output)


def _assignment_facts(database: Path):
    store = SQLiteStore.snapshot_from_path(database)
    try:
        return tuple(
            record
            for _revision, record in store.scan_runtime_records(
                record_type="claim.assignment.changed"
            )
        )
    finally:
        store.close()


def _seed_assignments(runner: CliRunner, contracts):
    seeded = {}
    for index, contract in enumerate(contracts):
        result = _json_result(
            runner,
            [
                "task",
                "assign",
                contract.id,
                "worker:one",
                "--reason",
                "seed current owner",
                "--idempotency-key",
                f"seed-assignment-{index}",
                "--json",
            ],
        )
        seeded[contract.id] = result["data"]["record_id"]
    return seeded


def _batch_args(
    contract_ids,
    previous_by_target,
    *,
    worker_id="worker:two",
    clear_assignment=False,
    reason="rotate owners",
    key="reassign-base",
):
    args = ["task", "reassign", *contract_ids]
    if clear_assignment:
        args.append("--clear")
    else:
        args.extend(("--worker", worker_id))
    for contract_id in contract_ids:
        args.extend(
            ("--previous-for", f"{contract_id}={previous_by_target[contract_id]}")
        )
    args.extend(("--reason", reason, "--idempotency-key", key, "--json"))
    return args


def test_bulk_reassign_rows_echo_exact_requested_heads_and_signed_facts(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    seeded = {}
    for index, contract in enumerate(contracts[:2]):
        result = _json_result(
            runner,
            [
                "task",
                "assign",
                contract.id,
                "worker:one",
                "--reason",
                "seed current owner",
                "--idempotency-key",
                f"seed-assignment-{index}",
                "--json",
            ],
        )
        seeded[contract.id] = result["data"]["record_id"]

    ids = [contracts[0].id, contracts[1].id]
    result = _json_result(
        runner,
        [
            "task",
            "reassign",
            *ids,
            "--worker",
            "worker:two",
            "--previous-for",
            f"{ids[0]}={seeded[ids[0]]}",
            "--previous-for",
            f"{ids[1]}={seeded[ids[1]]}",
            "--reason",
            "rotate owners",
            "--idempotency-key",
            "reassign-base",
            "--json",
        ],
    )

    rows = result["data"]["results"]
    assert result["ok"] is True
    assert [row["contract_id"] for row in rows] == ids
    assert [row["requested_previous_assignment_id"] for row in rows] == [
        seeded[item] for item in ids
    ]
    facts = {fact.payload["contract_id"]: fact for fact in _assignment_facts(database)}
    for contract_id, row in zip(ids, rows, strict=True):
        fact = facts[contract_id]
        assert fact.payload["previous_assignment_id"] == seeded[contract_id]
        assert fact.payload["worker_id"] == "worker:two"
        assert row["candidate_id"] == fact.payload["candidate_id"]
        assert row["fact_ids"] == [fact.id]


def test_bulk_clear_uses_clear_fact_as_next_exact_head(tmp_path, monkeypatch):
    database, contracts = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    ids = [contract.id for contract in contracts[:2]]
    seeded = _seed_assignments(runner, contracts[:2])

    cleared = _json_result(
        runner,
        _batch_args(ids, seeded, clear_assignment=True, key="clear-base"),
    )
    clear_rows = cleared["data"]["results"]
    assert cleared["ok"] is True
    assert [row["requested_previous_assignment_id"] for row in clear_rows] == [
        seeded[item] for item in ids
    ]
    clear_facts = {
        fact.payload["contract_id"]: fact for fact in _assignment_facts(database)
    }
    for contract_id in ids:
        fact = clear_facts[contract_id]
        assert fact.payload["previous_assignment_id"] == seeded[contract_id]
        assert fact.payload["worker_id"] == ""

    contract_id = ids[0]
    clear_head = clear_facts[contract_id].id
    set_again = _json_result(
        runner,
        [
            "task",
            "reassign",
            contract_id,
            "--worker",
            "worker:two",
            "--previous",
            clear_head,
            "--reason",
            "set after clear",
            "--idempotency-key",
            "set-after-clear",
            "--json",
        ],
    )
    assert set_again["data"]["previous_assignment_id"] == clear_head
    assert set_again["data"]["worker_id"] == "worker:two"


def test_bulk_reassign_keys_replay_reordered_subset_and_later_head(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    ids = [contract.id for contract in contracts]
    seeded = _seed_assignments(runner, contracts)
    args = _batch_args(ids, seeded, key="stable-head-base")
    first = _json_result(runner, args)
    first_rows = {row["contract_id"]: row for row in first["data"]["results"]}
    before = _assignment_facts(database)

    reordered_ids = [ids[2], ids[0]]
    reordered = _json_result(
        runner,
        _batch_args(reordered_ids, seeded, key="stable-head-base"),
    )
    reordered_rows = reordered["data"]["results"]
    assert [row["contract_id"] for row in reordered_rows] == reordered_ids
    for row in reordered_rows:
        original = first_rows[row["contract_id"]]
        assert row["idempotency_key"] == original["idempotency_key"]
        assert row["candidate_id"] == original["candidate_id"]
        assert row["fact_ids"] == original["fact_ids"]
    assert len(_assignment_facts(database)) == len(before)

    member = first_rows[ids[0]]
    single_replay = _json_result(
        runner,
        [
            "task",
            "reassign",
            ids[0],
            "--worker",
            "worker:two",
            "--previous",
            seeded[ids[0]],
            "--reason",
            "rotate owners",
            "--idempotency-key",
            member["idempotency_key"],
            "--json",
        ],
    )
    assert single_replay["data"]["record_id"] == member["fact_ids"][0]
    assert single_replay["data"]["candidate_id"] == member["candidate_id"]

    newer = _json_result(
        runner,
        [
            "task",
            "reassign",
            ids[0],
            "--clear",
            "--previous",
            member["fact_ids"][0],
            "--reason",
            "later clear",
            "--idempotency-key",
            "later-clear",
            "--json",
        ],
    )
    assert newer["data"]["record_id"] != member["fact_ids"][0]
    replay_after_later_head = _json_result(runner, args)
    replay_rows = {
        row["contract_id"]: row for row in replay_after_later_head["data"]["results"]
    }
    assert replay_rows[ids[0]]["fact_ids"] == member["fact_ids"]
    assert replay_rows[ids[0]]["candidate_id"] == member["candidate_id"]
    assert len(_assignment_facts(database)) == len(before) + 1


def test_bulk_reassign_continues_known_not_found_and_echoes_all_heads(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    first_id, last_id = contracts[0].id, contracts[2].id
    seeded = _seed_assignments(runner, (contracts[0], contracts[2]))
    ids = [first_id, "task:missing", last_id]
    heads = {
        first_id: seeded[first_id],
        "task:missing": "head:expected",
        last_id: seeded[last_id],
    }
    result = _json_result(
        runner,
        _batch_args(ids, heads, key="partial-reassign"),
        expected=1,
    )
    rows = result["data"]["results"]
    assert [row["contract_id"] for row in rows] == ids
    assert [row["outcome"] for row in rows] == ["accepted", "not_found", "accepted"]
    assert [row["requested_previous_assignment_id"] for row in rows] == [
        heads[item] for item in ids
    ]
    assert len(_assignment_facts(database)) == 4


def test_bulk_reassign_stale_head_is_one_known_partial_rejection(tmp_path, monkeypatch):
    database, contracts = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    ids = [contract.id for contract in contracts]
    seeded = _seed_assignments(runner, contracts)
    advanced = _json_result(
        runner,
        [
            "task",
            "reassign",
            ids[1],
            "--worker",
            "worker:two",
            "--previous",
            seeded[ids[1]],
            "--reason",
            "competing update",
            "--idempotency-key",
            "competing-update",
            "--json",
        ],
    )
    assert advanced["data"]["previous_assignment_id"] == seeded[ids[1]]

    mixed = _json_result(
        runner,
        _batch_args(ids, seeded, key="stale-head-batch"),
        expected=1,
    )
    rows = mixed["data"]["results"]
    assert [row["outcome"] for row in rows] == ["accepted", "rejected", "accepted"]
    assert [row["requested_previous_assignment_id"] for row in rows] == [
        seeded[item] for item in ids
    ]
    facts = {fact.payload["contract_id"]: fact for fact in _assignment_facts(database)}
    assert facts[ids[1]].id == advanced["data"]["record_id"]
    assert facts[ids[1]].payload["reason"] == "competing update"


def test_bulk_reassign_unknown_exception_stops_and_sanitizes_output(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    ids = [contract.id for contract in contracts]
    seeded = _seed_assignments(runner, contracts)
    import aigineering.cli.task_assignment as cli_assignment

    publish = cli_assignment.publish_task_assignment
    calls = 0

    def fail_second(store, contract_id, worker_id, reason, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("private unexpected marker")
        return publish(store, contract_id, worker_id, reason, **kwargs)

    monkeypatch.setattr(cli_assignment, "publish_task_assignment", fail_second)
    result = CliRunner().invoke(
        _app(),
        _batch_args(ids, seeded, key="unknown-stop"),
    )
    assert result.exit_code == 1
    assert "private unexpected marker" not in result.output
    rows = json.loads(result.output)["data"]["results"]
    assert [row["outcome"] for row in rows] == [
        "accepted",
        "outcome_unknown",
        "not_attempted",
    ]
    assert rows[1]["error"]["exception_class"] == "ValueError"
    assert rows[2]["requested_previous_assignment_id"] == seeded[ids[2]]
    facts = _assignment_facts(database)
    assert len(facts) == 4
    assert {fact.payload["contract_id"] for fact in facts} == {ids[0], ids[1], ids[2]}


def test_bulk_reassign_exact_pending_continues_and_sustained_outage_skips_targets(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    ids = [contract.id for contract in contracts]
    seeded = _seed_assignments(runner, contracts)
    import aigineering.task_assignment as app_assignment
    from aigineering.runtime_services import RedisSyncPending

    state = {"committed_pending": False, "restored": False}

    def relay(_store, *, accepted=None):
        if accepted is True and not state["committed_pending"]:
            state["committed_pending"] = True
            raise RedisSyncPending(accepted=True)
        if state["committed_pending"] and not state["restored"]:
            raise RedisSyncPending(accepted=accepted)

    monkeypatch.setattr(app_assignment, "sync_runtime", relay)
    args = _batch_args(ids, seeded, key="pending-reassign")
    pending = _json_result(runner, args, expected=1)
    rows = pending["data"]["results"]
    assert [row["outcome"] for row in rows] == [
        "redis_sync_pending",
        "not_attempted",
        "not_attempted",
    ]
    first = rows[0]
    assert first["candidate_accepted"] is True
    assert first["facts_retained"] is True
    assert first["requested_previous_assignment_id"] == seeded[ids[0]]
    assert first["candidate_id"]
    assert len(first["fact_ids"]) == 1
    assert first["data"]["previous_assignment_id"] == seeded[ids[0]]
    assert [row["requested_previous_assignment_id"] for row in rows] == [
        seeded[item] for item in ids
    ]
    assert len(_assignment_facts(database)) == 4

    state["restored"] = True
    recovered = _json_result(runner, args)
    recovered_rows = recovered["data"]["results"]
    assert [row["outcome"] for row in recovered_rows] == [
        "accepted",
        "accepted",
        "accepted",
    ]
    assert recovered_rows[0]["candidate_id"] == first["candidate_id"]
    assert recovered_rows[0]["fact_ids"] == first["fact_ids"]
    assert len(_assignment_facts(database)) == 6


@pytest.mark.parametrize(
    "targets,previous_for",
    [
        (("task:a", "task:a"), ("task:a=head:1", "task:a=head:1")),
        (("task:a", "task:b"), ("task:a=head:1",)),
        (("task:a", "task:b"), ("task:a=head:1", "task:c=head:2")),
        (("task:a", "task:b"), ("task:a=", "task:b=head:2")),
    ],
)
def test_bulk_reassign_shape_errors_precede_redis_and_store(
    monkeypatch, targets, previous_for
):
    import aigineering.cli.task_assignment as cli_assignment

    def forbidden():
        raise AssertionError("invalid batch opened the Store")

    monkeypatch.setattr(cli_assignment, "_persistent_snapshot", forbidden)
    monkeypatch.setattr(
        cli_assignment,
        "require_redis",
        lambda: (_ for _ in ()).throw(AssertionError("invalid batch contacted Redis")),
    )
    args = ["task", "reassign", *targets, "--worker", "worker:two"]
    for item in previous_for:
        args.extend(("--previous-for", item))
    args.extend(("--reason", "shape validation", "--json"))
    result = CliRunner().invoke(_app(), args)
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid_input"


@pytest.mark.parametrize(
    "targets",
    [
        tuple(f"task:{index}" for index in range(65)),
        ("task:a", "task:a"),
        ("task:a", ""),
        ("task:\ud800", "task:b"),
    ],
)
def test_bulk_reassign_invalid_global_ids_do_not_reach_redis(monkeypatch, targets):
    import aigineering.cli.task_assignment as cli_assignment

    monkeypatch.setattr(
        cli_assignment,
        "require_redis",
        lambda: (_ for _ in ()).throw(AssertionError("shape error reached Redis")),
    )
    args = ["task", "reassign", *targets, "--worker", "worker:two"]
    for target in targets:
        args.extend(("--previous-for", f"{target}=head:{target}"))
    args.extend(("--reason", "shape validation", "--json"))
    result = CliRunner().invoke(_app(), args)
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid_input"


def test_bulk_reassign_accepts_the_64_target_shape_before_preflight(monkeypatch):
    import click
    import aigineering.cli.task_assignment as cli_assignment

    calls = []

    def stop_at_preflight(_as_json):
        calls.append(True)
        raise click.ClickException("validated 64 targets")

    monkeypatch.setattr(cli_assignment, "_require_redis", stop_at_preflight)
    targets = tuple(f"task:{index}" for index in range(64))
    args = ["task", "reassign", *targets, "--worker", "worker:two"]
    for target in targets:
        args.extend(("--previous-for", f"{target}=head:{target}"))
    args.extend(("--reason", "shape validation", "--json"))
    result = CliRunner().invoke(_app(), args)
    assert result.exit_code == 1
    assert calls == [True]
    assert "validated 64 targets" in result.output


def test_bulk_reassign_rejects_non_utf8_head_before_redis(monkeypatch):
    import aigineering.cli.task_assignment as cli_assignment

    monkeypatch.setattr(
        cli_assignment,
        "require_redis",
        lambda: (_ for _ in ()).throw(AssertionError("shape error reached Redis")),
    )
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "reassign",
            "task:a",
            "task:b",
            "--worker",
            "worker:two",
            "--previous-for",
            "task:a=head:\ud800",
            "--previous-for",
            "task:b=head:2",
            "--reason",
            "shape validation",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid_input"


def test_reassign_legacy_missing_previous_remains_click_usage_error():
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "reassign",
            "task:one",
            "--worker",
            "worker:two",
            "--reason",
            "missing head",
        ],
    )
    assert result.exit_code == 2
    assert "Missing option '--previous'." in result.output
