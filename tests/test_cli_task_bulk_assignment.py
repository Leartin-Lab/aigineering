"""Initial multi-Contract assignment delegates to the signed assignment owner."""

from __future__ import annotations

import json
from pathlib import Path

from click import Group
from click.testing import CliRunner

from aigineering.cli.task_assignment import register_task_assignment_commands
from aigineering.cli._task_batch import collect_task_batch, target_key
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
            name=f"assignment bulk target {index}",
            description="A bulk assignment test target.",
            outputs=("result",),
            budget=3,
        )
        declared = publish_effect(
            store,
            store,
            genesis,
            genesis.root_keys[0],
            root_signer,
            contract_declaration_effect(contract),
            idempotency_key=f"assignment-bulk-contract-{index}",
        )
        assert declared.accepted
        contracts.append(contract)
    store.register_worker(WorkerRegistration("worker:one", capacity=4))
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


def _read_assignment_facts(database: Path):
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


def test_bulk_assignment_preserves_owner_empty_previous_and_target_keys(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    ids = [contracts[0].id, contracts[1].id]
    args = [
        "task",
        "assign",
        ids[0],
        "--reason",
        "initial bulk owner",
        ids[1],
        "worker:one",
        "--idempotency-key",
        "bulk-base",
        "--json",
    ]
    result = CliRunner().invoke(_app(), args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    rows = payload["data"]["results"]
    assert [row["contract_id"] for row in rows] == ids
    assert [row["outcome"] for row in rows] == ["accepted", "accepted"]
    expected_keys = [target_key("assign", "bulk-base", item) for item in ids]
    assert [row["idempotency_key"] for row in rows] == expected_keys
    assert payload["data"]["batch_idempotency_key"] == "bulk-base"
    facts = _read_assignment_facts(database)
    assert len(facts) == 2
    by_contract = {fact.payload["contract_id"]: fact for fact in facts}
    for contract_id, key in zip(ids, expected_keys, strict=True):
        fact = by_contract[contract_id]
        assert fact.payload["previous_assignment_id"] == ""
        assert fact.payload["worker_id"] == "worker:one"
        assert fact.payload["reason"] == "initial bulk owner"
        row = next(row for row in rows if row["contract_id"] == contract_id)
        assert row["candidate_id"] == fact.payload["candidate_id"]
        assert row["fact_ids"] == [fact.id]
        assert row["idempotency_key"] == key


def test_generated_base_is_returned_and_single_member_replay_uses_actual_key(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        _app(),
        [
            "task",
            "assign",
            contracts[0].id,
            contracts[1].id,
            "worker:one",
            "--reason",
            "generated bulk base",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    base = envelope["data"]["batch_idempotency_key"]
    assert base.startswith("task-bulk-v1:assign:")
    member = envelope["data"]["results"][0]
    before = tuple(fact.id for fact in _read_assignment_facts(database))
    replay = runner.invoke(
        _app(),
        [
            "task",
            "assign",
            contracts[0].id,
            "worker:one",
            "--reason",
            "generated bulk base",
            "--idempotency-key",
            member["idempotency_key"],
            "--json",
        ],
    )
    assert replay.exit_code == 0, replay.output
    replay_data = json.loads(replay.output)["data"]
    assert replay_data["idempotency_key"] == member["idempotency_key"]
    assert replay_data["candidate_id"] == member["candidate_id"]
    assert replay_data["fact_ids"] == member["fact_ids"]
    assert tuple(fact.id for fact in _read_assignment_facts(database)) == before


def test_bulk_assignment_keeps_ordered_partial_results_and_replays_subset(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    ids = [contracts[0].id, "task:missing", contracts[2].id]
    runner = CliRunner()
    args = [
        "task",
        "assign",
        *ids,
        "worker:one",
        "--reason",
        "partial bulk",
        "--idempotency-key",
        "partial-base",
        "--json",
    ]
    first = runner.invoke(_app(), args)
    assert first.exit_code == 1, first.output
    result = json.loads(first.output)
    rows = result["data"]["results"]
    assert [row["contract_id"] for row in rows] == ids
    assert [row["outcome"] for row in rows] == ["accepted", "not_found", "accepted"]
    before = {
        fact.payload["contract_id"]: fact for fact in _read_assignment_facts(database)
    }
    retry_ids = [contracts[2].id, contracts[0].id]
    retry = runner.invoke(
        _app(),
        [
            "task",
            "assign",
            *retry_ids,
            "worker:one",
            "--reason",
            "partial bulk",
            "--idempotency-key",
            "partial-base",
            "--json",
        ],
    )
    assert retry.exit_code == 0, retry.output
    retry_rows = json.loads(retry.output)["data"]["results"]
    assert [row["contract_id"] for row in retry_rows] == retry_ids
    assert [row["data"]["record_id"] for row in retry_rows] == [
        before[contract_id].id for contract_id in retry_ids
    ]
    assert [row["candidate_id"] for row in retry_rows] == [
        before[contract_id].payload["candidate_id"] for contract_id in retry_ids
    ]
    assert [row["fact_ids"] for row in retry_rows] == [
        [before[contract_id].id] for contract_id in retry_ids
    ]
    assert [row["idempotency_key"] for row in retry_rows] == [
        target_key("assign", "partial-base", contract_id) for contract_id in retry_ids
    ]
    assert len(_read_assignment_facts(database)) == 2

    changed_intent = runner.invoke(
        _app(),
        [
            "task",
            "assign",
            contracts[0].id,
            contracts[2].id,
            "worker:one",
            "--reason",
            "changed reason",
            "--idempotency-key",
            "partial-base",
            "--json",
        ],
    )
    assert changed_intent.exit_code == 1
    changed_rows = json.loads(changed_intent.output)["data"]["results"]
    assert [row["outcome"] for row in changed_rows] == ["rejected", "rejected"]
    assert len(_read_assignment_facts(database)) == 2


def test_known_already_assigned_target_does_not_stop_later_target(
    tmp_path, monkeypatch
):
    database, contracts = _workspace(tmp_path, monkeypatch)
    runner = CliRunner()
    first = runner.invoke(
        _app(),
        [
            "task",
            "assign",
            contracts[0].id,
            "worker:one",
            "--reason",
            "existing assignment",
            "--idempotency-key",
            "existing-assignment",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.output
    result = runner.invoke(
        _app(),
        [
            "task",
            "assign",
            contracts[0].id,
            contracts[1].id,
            "worker:one",
            "--reason",
            "batch continues",
            "--idempotency-key",
            "batch-continue",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    rows = json.loads(result.output)["data"]["results"]
    assert [row["outcome"] for row in rows] == ["rejected", "accepted"]
    assert len(_read_assignment_facts(database)) == 2


def test_invalid_batch_shape_precedes_redis_and_store_open(monkeypatch):
    import aigineering.cli.task_assignment as cli_assignment

    def unexpected(*_args, **_kwargs):
        raise AssertionError("batch shape validation must precede preflight")

    monkeypatch.setattr(cli_assignment, "require_redis", unexpected)
    monkeypatch.setattr(cli_assignment, "_persistent_snapshot", unexpected)
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "assign",
            "task:duplicate",
            "task:duplicate",
            "worker:one",
            "--reason",
            "bad batch",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid_input"

    too_many = CliRunner().invoke(
        _app(),
        [
            "task",
            "assign",
            *(f"task:{index}" for index in range(65)),
            "worker:one",
            "--reason",
            "too many",
            "--json",
        ],
    )
    assert too_many.exit_code == 1
    assert json.loads(too_many.output)["error"]["code"] == "invalid_input"


def test_global_redis_preflight_failure_has_no_target_calls(monkeypatch):
    import aigineering.cli.task_assignment as cli_assignment

    def unavailable():
        from aigineering.adapters.redis_query import QueryProjectionUnavailable

        raise QueryProjectionUnavailable("private outage detail")

    def no_store():
        raise AssertionError("Redis preflight must precede opening a Store")

    monkeypatch.setattr(cli_assignment, "require_redis", unavailable)
    monkeypatch.setattr(cli_assignment, "_persistent_snapshot", no_store)
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "assign",
            "task:one",
            "task:two",
            "worker:one",
            "--reason",
            "preflight outage",
            "--json",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "redis_unavailable"
    assert "private outage detail" not in result.output


def test_cli_unknown_value_error_stops_without_leaking_message(tmp_path, monkeypatch):
    database, contracts = _workspace(tmp_path, monkeypatch)
    import aigineering.cli.task_assignment as cli_assignment

    real_publish = cli_assignment.publish_task_assignment
    calls = []

    def publish(store, contract_id, worker_id, reason, **kwargs):
        calls.append(contract_id)
        if contract_id == contracts[1].id:
            raise ValueError("private programming marker")
        return real_publish(store, contract_id, worker_id, reason, **kwargs)

    monkeypatch.setattr(cli_assignment, "publish_task_assignment", publish)
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "assign",
            *(contract.id for contract in contracts),
            "worker:one",
            "--reason",
            "unknown failure stop",
            "--idempotency-key",
            "unknown-stop",
            "--json",
        ],
    )
    assert result.exit_code == 1
    assert calls == [contracts[0].id, contracts[1].id]
    assert "private programming marker" not in result.output
    rows = json.loads(result.output)["data"]["results"]
    assert [row["outcome"] for row in rows] == [
        "accepted",
        "outcome_unknown",
        "not_attempted",
    ]
    assert rows[1]["error"]["exception_class"] == "ValueError"
    assert len(_read_assignment_facts(database)) == 1


def test_assignment_batch_unknown_value_error_stops_and_legacy_default_remains():
    calls = []
    cause = RuntimeError("private chained cause")

    def invoke(contract_id, _key):
        calls.append(contract_id)
        if contract_id == "bad":
            try:
                raise ValueError("private programming marker") from cause
            except ValueError as exc:
                raise exc
        return ({"record_id": "record:1"}, 1)

    result = collect_task_batch(
        ("good", "bad", "later"),
        action="assign",
        batch_key_base="base",
        invoke=invoke,
        expected_rejection_types=(RuntimeError,),
    )
    assert calls == ["good", "bad"]
    rows = result.data["results"]
    assert [row["outcome"] for row in rows] == [
        "accepted",
        "outcome_unknown",
        "not_attempted",
    ]
    assert rows[1]["error"]["exception_class"] == "ValueError"
    assert "private programming marker" not in json.dumps(result.data)
    assert result.unexpected is not None
    assert result.unexpected.__cause__ is cause
    legacy = collect_task_batch(
        ("bad", "later"),
        action="block",
        batch_key_base="base",
        invoke=lambda _contract, _key: (_ for _ in ()).throw(ValueError("known")),
    )
    assert [row["outcome"] for row in legacy.data["results"]] == [
        "rejected",
        "rejected",
    ]


def test_accepted_pending_without_operation_result_stops_remaining_targets():
    from aigineering.runtime_services import RedisSyncPending

    calls = []

    def invoke(contract_id, _key):
        calls.append(contract_id)
        if contract_id == "pending":
            raise RedisSyncPending(accepted=True, revision=7)
        return ({"record_id": "record:later"}, 9)

    result = collect_task_batch(
        ("pending", "later"),
        action="assign",
        batch_key_base="base",
        invoke=invoke,
        expected_rejection_types=(RuntimeError,),
    )
    assert calls == ["pending"]
    rows = result.data["results"]
    assert [row["outcome"] for row in rows] == [
        "outcome_unknown",
        "not_attempted",
    ]
    assert rows[0]["candidate_accepted"] is True
    assert rows[0]["facts_retained"] is True
    assert "candidate_id" not in rows[0]
    assert "fact_ids" not in rows[0]
    assert rows[0]["error"]["code"] == "missing_result"
    assert rows[1]["idempotency_key"] == target_key("assign", "base", "later")
    assert result.revision == 7
    assert isinstance(result.unexpected, RedisSyncPending)


def test_assignment_pending_with_exact_result_keeps_key_ids_and_continues():
    from aigineering.runtime_services import RedisSyncPending

    calls = []

    def invoke(contract_id, key):
        calls.append(contract_id)
        if contract_id == "pending":
            raise RedisSyncPending(
                accepted=True,
                data={
                    "contract_id": contract_id,
                    "record_id": "record:pending",
                    "candidate_id": "candidate:pending",
                    "fact_ids": ["record:pending"],
                    "idempotency_key": key,
                },
                revision=11,
            )
        return (
            {
                "contract_id": contract_id,
                "record_id": "record:later",
                "candidate_id": "candidate:later",
                "idempotency_key": key,
            },
            13,
        )

    result = collect_task_batch(
        ("pending", "later"),
        action="assign",
        batch_key_base="base",
        invoke=invoke,
        expected_rejection_types=(RuntimeError,),
    )
    rows = result.data["results"]
    assert calls == ["pending", "later"]
    assert [row["outcome"] for row in rows] == [
        "redis_sync_pending",
        "accepted",
    ]
    assert rows[0]["candidate_accepted"] is True
    assert rows[0]["candidate_id"] == "candidate:pending"
    assert rows[0]["fact_ids"] == ["record:pending"]
    assert rows[0]["idempotency_key"] == target_key("assign", "base", "pending")
    assert result.revision == 13


def test_batch_id_validation_accepts_sixty_four_and_rejects_invalid_text():
    from aigineering.cli._task_batch import validate_task_ids

    assert len(validate_task_ids(tuple(f"task:{index}" for index in range(64)))) == 64
    try:
        validate_task_ids(("task:valid", "task:\ud800"))
    except ValueError as exc:
        assert "UTF-8" in str(exc)
    else:
        raise AssertionError("non-UTF-8 target ID must be rejected")


def test_assignment_collector_preserves_click_control_flow():
    import click

    failure = click.ClickException("expected CLI control flow")

    def invoke(_contract_id, _key):
        raise failure

    try:
        collect_task_batch(
            ("task:a", "task:b"),
            action="assign",
            batch_key_base="base",
            invoke=invoke,
            passthrough_exceptions=(click.ClickException, click.exceptions.Exit),
        )
    except click.ClickException as exc:
        assert exc is failure
    else:
        raise AssertionError("expected ClickException to preserve control flow")


def test_batch_human_base_does_not_echo_untrusted_key_text(tmp_path, monkeypatch):
    _database, contracts = _workspace(tmp_path, monkeypatch, count=2)
    base = "private\nmarker"
    result = CliRunner().invoke(
        _app(),
        [
            "task",
            "assign",
            contracts[0].id,
            contracts[1].id,
            "worker:one",
            "--reason",
            "safe rendering",
            "--idempotency-key",
            base,
        ],
    )
    assert result.exit_code == 0, result.output
    assert base not in result.output
    assert "use --json to retrieve the exact key" in result.output

    json_result = CliRunner().invoke(
        _app(),
        [
            "task",
            "assign",
            contracts[0].id,
            contracts[1].id,
            "worker:one",
            "--reason",
            "safe rendering",
            "--idempotency-key",
            base,
            "--json",
        ],
    )
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)["data"]["batch_idempotency_key"] == base
