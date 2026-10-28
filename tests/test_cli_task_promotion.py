"""End-to-end signed task promotion tests using isolated SQLite facts."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from click import Group
from click.testing import CliRunner
import pytest
from aigineering.cli.task_collaboration import register_task_collaboration_commands
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.ids import compute_content_hash
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain, load_actor_signer
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Contract
from aigineering.task_collaboration import publish_task_block


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_collaboration_commands(task)
    root.add_command(task)
    return root


def _contract(name: str) -> Contract:
    return build_control_plane_contract(
        name=name,
        description="promotion test",
        outputs=("result",),
        budget=3,
    )


def _workspace(tmp_path: Path, monkeypatch, names=("target",)):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key_path = database.parent / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key_path))
    store = SQLiteStore(database)
    genesis = ensure_local_domain(store)
    signer = load_actor_signer(root_key_path)
    key = next(
        item for item in genesis.root_keys if item.public_key == signer.signer_id
    )
    contracts = tuple(_contract(name) for name in names)
    for index, contract in enumerate(contracts):
        decision = publish_effect(
            store,
            store,
            genesis,
            key,
            signer,
            contract_declaration_effect(contract),
            idempotency_key=f"declare-{index}-{name_for(contract)}",
        )
        assert decision.accepted, [
            (record.record_type, record.payload.get("reason"))
            for record in decision.runtime_records
        ]
    store.close()

    monkeypatch.setattr(
        "aigineering.cli.task_collaboration._persistent_store",
        lambda: SQLiteStore(database),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration._persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr("aigineering.task_collaboration.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration.require_redis", lambda: None
    )
    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", lambda: None)

    class _Collaboration:
        def sync(self):
            return None

    monkeypatch.setattr(
        "aigineering.application.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration.collaboration_service",
        lambda _store: _Collaboration(),
    )
    monkeypatch.setattr(
        "aigineering.task_collaboration.sync_runtime", lambda *_a, **_k: None
    )
    return database, root_key_path, contracts


def name_for(contract: Contract) -> str:
    return contract.name or contract.id


def _invoke(runner, args):
    result = runner.invoke(_app(), args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    return payload


def _holds_plugin_key_path(root_key_path: Path) -> Path:
    actor_id = "plugin:task.collaboration.holds.operator.v1"
    return root_key_path.parent / (
        f"plugin-{compute_content_hash(actor_id)[:16]}.ed25519"
    )


def _place_holds(database, contract, count: int, *, prefix="place") -> tuple[str, ...]:
    store = SQLiteStore(database)
    try:
        ids = tuple(
            publish_task_block(
                store,
                contract.id,
                f"hold reason {index}",
                idempotency_key=f"{prefix}-{index}",
            )[0]["hold_id"]
            for index in range(count)
        )
    finally:
        store.close()
    return tuple(sorted(ids))


def test_promote_releases_exact_full_set_and_replay_preserves_later_hold(
    tmp_path, monkeypatch
):
    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    hold_ids = _place_holds(database, contract, 2)
    runner = CliRunner()
    promoted = _invoke(
        runner,
        ["task", "promote", contract.id, "--reason", "Ready to resume", "--json"],
    )
    data = promoted["data"]
    assert data["record_type"] == "task.promoted"
    assert data["hold_ids"] == list(hold_ids)
    assert len(data["release_record_ids"]) == 2
    assert data["status"] == "ready"
    assert data["remaining_hold_ids"] == []
    assert data["idempotency_key"].startswith("task-promote:")

    current_hold = _place_holds(database, contract, 1, prefix="later")[0]
    replay = _invoke(
        runner,
        [
            "task",
            "promote",
            contract.id,
            "--reason",
            "Ready to resume",
            "--hold",
            hold_ids[0],
            "--hold",
            hold_ids[1],
            "--idempotency-key",
            data["idempotency_key"],
            "--json",
        ],
    )["data"]
    assert replay["record_id"] == data["record_id"]
    assert replay["release_record_ids"] == data["release_record_ids"]
    assert replay["recorded_at"] == data["recorded_at"]
    assert replay["candidate_id"] == data["candidate_id"]
    assert replay["remaining_hold_ids"] == [current_hold]
    assert replay["status"] == "blocked"
    history = _invoke(runner, ["task", "promotions", contract.id, "--json"])["data"]
    assert [item["record_id"] for item in history["history"]] == [data["record_id"]]


def test_promote_refuses_no_hold_and_terminal_before_identity_or_candidate(
    tmp_path, monkeypatch
):
    database, root_key_path, (contract,) = _workspace(tmp_path, monkeypatch)
    plugin_key = _holds_plugin_key_path(root_key_path)
    runner = CliRunner()
    no_holds = runner.invoke(
        _app(), ["task", "promote", contract.id, "--reason", "Resume", "--json"]
    )
    assert no_holds.exit_code == 1
    assert json.loads(no_holds.output)["error"]["code"] == "rejected"
    assert "no open holds" in json.loads(no_holds.output)["error"]["message"]
    assert not plugin_key.exists()

    _place_holds(database, contract, 1)
    store = SQLiteStore(database)
    from aigineering.core.lifecycle_facts import create_terminal_record

    store.append_runtime_record(
        create_terminal_record(
            contract.id, "complete", actor_id="fixture", reason="already complete"
        )
    )
    store.close()
    before = SQLiteStore.snapshot_from_path(database)
    before_count = len(before.scan_runtime_records(record_type="task.promoted"))
    before.close()
    rejected = runner.invoke(
        _app(), ["task", "promote", contract.id, "--reason", "Resume", "--json"]
    )
    assert rejected.exit_code == 1
    assert json.loads(rejected.output)["error"]["code"] == "rejected"
    after = SQLiteStore.snapshot_from_path(database)
    assert len(after.scan_runtime_records(record_type="task.promoted")) == before_count
    after.close()


def test_promote_multi_target_is_per_task_atomic_and_reports_partial_success(
    tmp_path, monkeypatch
):
    database, _root_key, (first, second) = _workspace(
        tmp_path, monkeypatch, names=("first", "second")
    )
    _place_holds(database, first, 2)
    runner = CliRunner()
    result = runner.invoke(
        _app(),
        ["task", "promote", first.id, second.id, "--reason", "Resume", "--json"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["data"]["success_count"] == 1
    assert payload["data"]["failed_count"] == 1
    assert payload["data"]["results"][0]["data"]["hold_ids"] == list(
        sorted(
            item["hold_id"]
            for item in _invoke(runner, ["task", "holds", first.id, "--json"])["data"]
        )
    )


def test_duplicate_or_multitarget_explicit_hold_inputs_reject_before_identity(
    tmp_path, monkeypatch
):
    database, root_key_path, (first, second) = _workspace(
        tmp_path, monkeypatch, names=("first", "second")
    )
    runner = CliRunner()
    duplicate = runner.invoke(
        _app(),
        [
            "task",
            "promote",
            first.id,
            first.id,
            "--reason",
            "Resume",
            "--json",
        ],
    )
    assert duplicate.exit_code == 1
    assert json.loads(duplicate.output)["error"]["code"] == "invalid_input"
    multi_hold = runner.invoke(
        _app(),
        [
            "task",
            "promote",
            first.id,
            second.id,
            "--hold",
            "record:missing",
            "--reason",
            "Resume",
            "--json",
        ],
    )
    assert multi_hold.exit_code == 1
    assert json.loads(multi_hold.output)["error"]["code"] == "invalid_input"
    assert not _holds_plugin_key_path(root_key_path).exists()
    snapshot = SQLiteStore.snapshot_from_path(database)
    assert not snapshot.scan_runtime_records(record_type="task.promoted")
    snapshot.close()


def test_missing_workspace_and_invalid_hold_ids_do_not_provision_identity(
    tmp_path, monkeypatch
):
    root_key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key_path))
    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.cli.task_collaboration.require_redis", lambda: None
    )

    def missing_snapshot():
        raise FileNotFoundError("no store")

    monkeypatch.setattr(
        "aigineering.cli.task_collaboration._persistent_snapshot", missing_snapshot
    )
    result = CliRunner().invoke(
        _app(),
        ["task", "promote", "task:v3:missing", "--reason", "Resume", "--json"],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "workspace_not_initialized"
    assert not (tmp_path / ".aig" / "store.db").exists()
    assert not _holds_plugin_key_path(root_key_path).exists()


def test_postcommit_redis_pending_preserves_promotion_and_reports_acceptance(
    tmp_path, monkeypatch
):
    from aigineering.runtime_services import RedisSyncPending

    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    _place_holds(database, contract, 2)

    def sync(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr("aigineering.task_collaboration.sync_runtime", sync)
    result = CliRunner().invoke(
        _app(),
        ["task", "promote", contract.id, "--reason", "Relay outage", "--json"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "redis_sync_pending"
    assert payload["facts_retained"] is True
    assert payload["candidate_accepted"] is True
    store = SQLiteStore.snapshot_from_path(database)
    assert len(store.scan_runtime_records(record_type="task.promoted")) == 1
    assert len(store.scan_runtime_records(record_type="claim.hold.released")) == 2
    store.close()


def test_promote_65_holds_requires_partial_drain_then_releases_64_atomically(
    tmp_path, monkeypatch
):
    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    hold_ids = _place_holds(database, contract, 65)
    runner = CliRunner()
    rejected = runner.invoke(
        _app(), ["task", "promote", contract.id, "--reason", "Resume", "--json"]
    )
    assert rejected.exit_code == 1
    assert "at most 64" in json.loads(rejected.output)["error"]["message"]
    snapshot = SQLiteStore.snapshot_from_path(database)
    assert not snapshot.scan_runtime_records(record_type="task.promoted")
    snapshot.close()

    store = SQLiteStore(database)
    from aigineering.task_collaboration import publish_task_unblock

    publish_task_unblock(
        store,
        contract.id,
        hold_ids[0],
        "Drain one legacy hold",
        idempotency_key="drain-one",
    )
    store.close()
    remaining = hold_ids[1:]
    promoted = _invoke(
        runner,
        ["task", "promote", contract.id, "--reason", "Resume", "--json"],
    )["data"]
    assert promoted["hold_ids"] == list(remaining)
    assert len(promoted["release_record_ids"]) == 64


@pytest.mark.parametrize("competitor", ("place", "release"))
def test_hold_change_at_writer_window_rejects_stale_exact_set_promotion(
    tmp_path, monkeypatch, competitor
):
    """The plugin rechecks the complete hold set under SQLite's writer lock."""
    from aigineering.task_collaboration import publish_task_promote

    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    initial_holds = _place_holds(database, contract, 2)
    original_hold = initial_holds[0]
    promote_store = SQLiteStore(database)
    original_commit = promote_store.commit_ingress_batch
    projected = Event()
    continue_writer = Event()

    def pause_before_writer(*args, **kwargs):
        runtime_records = kwargs.get("runtime_records", ())
        if any(record.record_type == "task.promoted" for record in runtime_records):
            projected.set()
            assert continue_writer.wait(timeout=10)
        return original_commit(*args, **kwargs)

    promote_store.commit_ingress_batch = pause_before_writer

    def promote():
        try:
            publish_task_promote(
                promote_store,
                contract.id,
                "Resume after all holds",
                hold_ids=initial_holds,
                idempotency_key="promote-stale-hold-set",
            )
        except Exception as exc:  # durable rejection is surfaced by the service
            return exc
        return None

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(promote)
            assert projected.wait(timeout=10), "promotion never reached writer ingress"
            competing_store = SQLiteStore(database)
            if competitor == "place":
                later_hold = _place_holds(
                    database, contract, 1, prefix="concurrent-placement"
                )[0]
            else:
                from aigineering.task_collaboration import publish_task_unblock

                publish_task_unblock(
                    competing_store,
                    contract.id,
                    original_hold,
                    "Competing release",
                    idempotency_key="concurrent-release",
                )
                later_hold = ""
            competing_store.close()
            continue_writer.set()
            outcome = pending.result(timeout=20)
    finally:
        continue_writer.set()

    assert outcome is not None, "stale complete-set promotion must be rejected"
    records = promote_store.scan_runtime_records()
    assert not [
        record for _revision, record in records if record.record_type == "task.promoted"
    ]
    releases = [
        record
        for _revision, record in records
        if record.record_type == "claim.hold.released"
    ]
    rejected = [
        record
        for _revision, record in records
        if record.record_type == "candidate.rejected"
        and tuple(record.payload.get("effect_types", ())) == ("task.promote",)
    ]
    assert len(rejected) == 1, (
        repr(outcome),
        [
            (record.record_type, dict(record.payload))
            for _revision, record in records
            if record.record_type == "candidate.rejected"
        ],
    )
    active = _invoke(CliRunner(), ["task", "holds", contract.id, "--json"])["data"]
    expected_active = set(initial_holds)
    if competitor == "place":
        expected_active.add(later_hold)
        assert releases == []
    else:
        expected_active.remove(original_hold)
        assert len(releases) == 1
        assert releases[0].payload["hold_id"] == original_hold
    assert {item["hold_id"] for item in active if item["active"]} == expected_active
    promote_store.close()


def test_bulk_promote_exact_hold_maps_preserve_legacy_keys_and_replay_snapshot(
    tmp_path, monkeypatch
):
    database, _root_key, (first, second) = _workspace(
        tmp_path, monkeypatch, names=("first", "second")
    )
    first_holds = _place_holds(database, first, 2, prefix="bulk-first")
    second_holds = _place_holds(database, second, 1, prefix="bulk-second")
    runner = CliRunner()
    args = [
        "task",
        "promote",
        first.id,
        second.id,
        "--hold-for",
        f"{first.id}={first_holds[0]}",
        "--hold-for",
        f"{first.id}={first_holds[1]}",
        "--hold-for",
        f"{second.id}={second_holds[0]}",
        "--reason",
        "resume exact sets",
        "--idempotency-key",
        "legacy-bulk",
        "--json",
    ]
    first_result = runner.invoke(_app(), args)
    assert first_result.exit_code == 0, first_result.output
    rows = json.loads(first_result.output)["data"]["results"]
    assert [row["data"]["idempotency_key"] for row in rows] == [
        f"legacy-bulk:promote:{first.id}",
        f"legacy-bulk:promote:{second.id}",
    ]
    assert rows[0]["data"]["hold_ids"] == list(sorted(first_holds))

    store = SQLiteStore(database)
    try:
        later, _revision = publish_task_block(
            store, first.id, "later hold", idempotency_key="later-hold"
        )
    finally:
        store.close()
    replay = runner.invoke(_app(), args)
    assert replay.exit_code == 0, replay.output
    replayed = json.loads(replay.output)["data"]["results"][0]["data"]
    assert replayed["record_id"] == rows[0]["data"]["record_id"]
    assert replayed["hold_ids"] == list(sorted(first_holds))
    assert later["hold_id"] in replayed["remaining_hold_ids"]


def test_bulk_cancel_collects_missing_target_then_signed_success(tmp_path, monkeypatch):
    database, _root_key, (_first, second) = _workspace(
        tmp_path, monkeypatch, names=("first", "second")
    )
    from copy import copy
    from inspect import unwrap
    import click
    from aigineering.cli import _common, _task_batch, task_board

    task = click.Group("task")
    task_board.register_task_board_commands(task)
    task.commands = {
        name: (
            lambda command: (
                setattr(command, "callback", unwrap(command.callback)),
                command,
            )[1]
        )(copy(command))
        for name, command in task.commands.items()
    }
    root = click.Group()
    root.add_command(task)

    def open_store():
        return SQLiteStore(database)

    monkeypatch.setattr(task_board, "persistent_store", open_store)
    monkeypatch.setattr(
        task_board,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(_common, "_persistent_store", open_store)
    monkeypatch.setattr(_task_batch, "require_redis", lambda: None)

    class _Sync:
        def sync(self):
            return None

    monkeypatch.setattr(_task_batch, "collaboration_service", lambda _store: _Sync())
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_k: None
    )

    result = CliRunner().invoke(
        root,
        [
            "task",
            "cancel",
            "missing",
            second.id,
            "--reason",
            "bulk cancel",
            "--idempotency-key",
            "cancel-base",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    rows = json.loads(result.output)["data"]["results"]
    assert [row["outcome"] for row in rows] == ["not_found", "accepted"]
    assert rows[1]["data"]["status"] == "cancelled"
    assert rows[1]["data"]["candidate_id"]
    assert rows[1]["fact_ids"] == rows[1]["data"]["fact_ids"]


def test_single_cancel_preserves_legacy_key_reason_and_parent(tmp_path, monkeypatch):
    database, _root_key, (contract,) = _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_a, **_k: None
    )
    from hashlib import sha256
    from aigineering.task_publication import publish_task_cancel

    store = SQLiteStore(database)
    try:
        result, _revision = publish_task_cancel(store, contract.id, "  stable reason  ")
        assert (
            result["idempotency_key"]
            == f"task-cancel:{contract.id}:{sha256(b'stable reason').hexdigest()}"
        )
        assert result["reason"] == "stable reason"
        assert result["status"] == "cancelled"
        receipt = next(
            record
            for _rev, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("candidate_id") == result["candidate_id"]
        )
        assert receipt.causal_parents == (contract.id,)
    finally:
        store.close()
