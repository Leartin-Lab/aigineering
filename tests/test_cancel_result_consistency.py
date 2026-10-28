"""Signed cancellation results retain exact durable rejection outcomes."""

from __future__ import annotations

import json
from copy import copy
from inspect import unwrap

import click
from click.testing import CliRunner

from aigineering.cli import task_board
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import load_genesis
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain, load_actor_signer
from aigineering.protocol.candidate import create_candidate_proposal
from aigineering.protocol.effect_builders import contract_cancellation_effect
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_publication import publish_task_contract


class _Collaboration:
    def sync(self):
        return None


def _task_group():
    group = click.Group("task")
    task_board.register_task_board_commands(group)
    group.commands = {
        name: _unwrapped(command) for name, command in group.commands.items()
    }
    return group


def _unwrapped(command):
    local = copy(command)
    local.callback = unwrap(local.callback)
    return local


def _workspace(tmp_path, monkeypatch, names=("first", "second")):
    import aigineering.application as application
    import aigineering.cli._task_batch as task_batch
    import aigineering.local_publication as local_publication
    import aigineering.runtime_services as runtime_services
    import aigineering.task_publication as task_publication

    database = tmp_path / ".aig" / "store.db"
    key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    database.parent.mkdir(parents=True, exist_ok=True)

    def open_store():
        return SQLiteStore(database)

    monkeypatch.setattr(application, "require_redis", lambda: None)
    monkeypatch.setattr(local_publication, "require_redis", lambda: None)
    monkeypatch.setattr(local_publication, "sync_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(task_publication, "sync_runtime", lambda *_a, **_k: None)
    monkeypatch.setattr(task_board, "persistent_store", open_store)
    monkeypatch.setattr(
        task_board,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(task_board, "require_redis", lambda: None)
    monkeypatch.setattr(
        task_board, "collaboration_service", lambda _s: _Collaboration()
    )
    monkeypatch.setattr(task_batch, "require_redis", lambda: None)
    monkeypatch.setattr(
        task_batch, "collaboration_service", lambda _s: _Collaboration()
    )

    with open_store() as store:
        ensure_local_domain(store)
        contracts = tuple(
            publish_task_contract(
                store,
                build_control_plane_contract(
                    name=name,
                    description=f"Cancellation result target {name}.",
                    outputs=(f"{name}-result",),
                    budget=3,
                ),
            )
            for name in names
        )
    return database, key_path, contracts


def _invoke(args):
    return CliRunner().invoke(_task_group(), args)


def _capture_commit_decisions(monkeypatch, keys):
    import aigineering.task_publication as task_publication

    original = task_publication.commit_local_effects
    observed = {}

    def capture(store, effects, *, idempotency_key, causal_parents=()):
        decision = original(
            store,
            effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )
        if idempotency_key in keys:
            observed[idempotency_key] = (decision, store.get_runtime_revision())
        return decision

    monkeypatch.setattr(task_publication, "commit_local_effects", capture)
    return observed


def _inject_one_writer_conflict(monkeypatch, contract_id):
    """Inject one typed writer refusal after the cancellation was projected."""
    from aigineering.core.record_conflict import ClaimBindingConflict

    original = SQLiteStore.commit_ingress_batch
    injected = {"done": False}

    def commit(store, *args, **kwargs):
        proposal = kwargs.get("candidate_proposal")
        records = tuple(kwargs.get("runtime_records", ()))
        is_cancel = any(
            record.record_type == "lifecycle.terminal"
            and record.payload.get("contract_id") == contract_id
            and record.payload.get("terminal") == "cancelled"
            for record in records
        )
        if proposal is not None and is_cancel and not injected["done"]:
            injected["done"] = True
            raise ClaimBindingConflict("fixture writer-boundary refusal")
        return original(store, *args, **kwargs)

    monkeypatch.setattr(SQLiteStore, "commit_ingress_batch", commit)
    return injected


def _assert_exact_rejection(database, decision, revision, row_or_data, *, bulk=False):
    from aigineering.core.commitment import CommitmentDecision
    from aigineering.local_publication import verify_durable_rejection_member

    assert isinstance(decision, CommitmentDecision)
    assert decision.accepted is False
    store = SQLiteStore.snapshot_from_path(database)
    try:
        rejection = verify_durable_rejection_member(store, decision)
        matching = [
            record
            for record in decision.runtime_records
            if record.record_type == rejection.record_type and record.id == rejection.id
        ]
        assert len(matching) == 1
        receipts = [
            record
            for record in decision.runtime_records
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == decision.candidate_id
        ]
        assert rejection.record_type == "candidate.rejected"
        assert len(receipts) == 1
        assert rejection.payload.get("candidate_id") == decision.candidate_id
        assert rejection.causal_parents == (receipts[0].id,)
        assert type(revision) is int
        if bulk:
            assert revision <= store.get_runtime_revision()
        else:
            assert revision == store.get_runtime_revision()
        data = row_or_data.get("data", row_or_data) if bulk else row_or_data
        assert data["candidate_id"] == decision.candidate_id
        assert data["rejection_record_id"] == rejection.id
        assert data["fact_ids"] == [rejection.id]
        assert data["idempotency_key"]
        if bulk:
            assert row_or_data["candidate_id"] == decision.candidate_id
            assert row_or_data["fact_ids"] == [rejection.id]
            assert row_or_data["revision"] == revision
        return rejection
    finally:
        store.close()


def test_single_cancel_rejection_returns_exact_signed_durable_result(
    tmp_path, monkeypatch
):
    database, _key_path, (_other, contract) = _workspace(tmp_path, monkeypatch)
    first = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "operator stop",
            "--idempotency-key",
            "cancel-first",
            "--json",
        ]
    )
    assert first.exit_code == 0, first.output
    observed = _capture_commit_decisions(monkeypatch, {"cancel-second"})

    result = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "operator stop",
            "--idempotency-key",
            "cancel-second",
            "--json",
        ]
    )
    assert result.exit_code == 1, result.output
    envelope = json.loads(result.stdout)
    assert envelope["error"]["code"] == "cancel_rejected"
    assert envelope["candidate_accepted"] is False
    assert envelope["facts_retained"] is True
    decision, revision = observed["cancel-second"]
    data = envelope["data"]
    _assert_exact_rejection(database, decision, revision, data)
    assert data["idempotency_key"] == "cancel-second"
    assert envelope["revision"] == revision
    assert data["accepted"] is False
    assert "claimed_candidate_id" not in data
    with SQLiteStore.snapshot_from_path(database) as store:
        rejection_count_before = len(
            store.scan_runtime_records(record_type="candidate.rejected")
        )
    replay = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "operator stop",
            "--idempotency-key",
            "cancel-second",
            "--json",
        ]
    )
    assert replay.exit_code == 1
    replay_payload = json.loads(replay.stdout)
    assert replay_payload["data"]["candidate_id"] == data["candidate_id"]
    assert replay_payload["data"]["rejection_record_id"] == data["rejection_record_id"]
    assert replay_payload["data"]["fact_ids"] == data["fact_ids"]
    assert replay_payload["revision"] == revision
    with SQLiteStore.snapshot_from_path(database) as store:
        assert (
            len(store.scan_runtime_records(record_type="candidate.rejected"))
            == rejection_count_before
        )


def test_human_cancel_rejection_includes_durable_audit_reference(tmp_path, monkeypatch):
    _database, _key_path, (_other, contract) = _workspace(tmp_path, monkeypatch)
    first = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "operator stop",
            "--idempotency-key",
            "human-cancel-first",
            "--json",
        ]
    )
    assert first.exit_code == 0
    rejected = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "operator stop",
            "--idempotency-key",
            "human-cancel-rejected",
        ]
    )
    assert rejected.exit_code == 1
    assert "rejection audit record record:" in rejected.output


def test_bulk_cancel_rejection_keeps_exact_row_and_continues(tmp_path, monkeypatch):
    database, _key_path, (first, second) = _workspace(tmp_path, monkeypatch)
    seeded = _invoke(
        [
            "cancel",
            first.id,
            "--reason",
            "operator stop",
            "--idempotency-key",
            "cancel-first",
            "--json",
        ]
    )
    assert seeded.exit_code == 0, seeded.output
    from aigineering.cli._task_batch import target_key

    batch_base = "cancel-batch-proof"
    expected_key = target_key("cancel", batch_base, first.id)
    observed = _capture_commit_decisions(monkeypatch, {expected_key})
    result = _invoke(
        [
            "cancel",
            first.id,
            second.id,
            "--reason",
            "operator stop",
            "--idempotency-key",
            batch_base,
            "--json",
        ]
    )
    assert result.exit_code == 1, result.output
    envelope = json.loads(result.stdout)
    rows = envelope["data"]["results"]
    assert [row["outcome"] for row in rows] == ["rejected", "accepted"]
    decision, revision = observed[expected_key]
    rejection = _assert_exact_rejection(
        database, decision, revision, rows[0], bulk=True
    )
    assert rows[0]["data"]["idempotency_key"] == expected_key
    assert rows[0]["error"]["code"] == "rejected"
    assert rows[1]["data"]["accepted"] is True
    assert rejection.id in rows[0]["fact_ids"]


def test_writer_conflict_cancel_rejection_is_persisted_and_returned_exactly(
    tmp_path, monkeypatch
):
    database, _key_path, (_other, contract) = _workspace(tmp_path, monkeypatch)
    injected = _inject_one_writer_conflict(monkeypatch, contract.id)
    result = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "operator stop",
            "--idempotency-key",
            "writer-conflict-cancel",
            "--json",
        ]
    )
    assert injected["done"] is True
    assert result.exit_code == 1, result.output
    envelope = json.loads(result.stdout)
    assert envelope["error"]["code"] == "cancel_rejected"
    assert envelope["candidate_accepted"] is False
    assert envelope["facts_retained"] is True
    with SQLiteStore.snapshot_from_path(database) as store:
        candidate_id = envelope["data"]["candidate_id"]
        rows = store.scan_runtime_records(record_type="candidate.rejected")
        rejection_rows = [
            row
            for _revision, row in rows
            if row.payload.get("candidate_id") == candidate_id
        ]
        assert len(rejection_rows) == 1
        # The CLI result must bind to the exact authenticated durable rejection.
        rejected = rejection_rows[0]
        receipts = [
            row
            for _revision, row in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if row.payload.get("candidate_id") == candidate_id
        ]
        assert len(receipts) == 1
        assert rejected.payload.get("candidate_id") == candidate_id
        assert rejected.causal_parents == (receipts[0].id,)
        from aigineering.core.candidate_decision import CommitmentDecision
        from aigineering.local_publication import verify_durable_rejection_member

        verified = verify_durable_rejection_member(
            store,
            CommitmentDecision(
                candidate_id=candidate_id,
                accepted=False,
                runtime_records=(receipts[0], rejected),
                trace_entries=(),
            ),
        )
        assert verified.id == rejected.id
        assert envelope["data"]["rejection_record_id"] == rejected.id
        assert envelope["data"]["fact_ids"] == [rejected.id]
        assert envelope["data"]["rejection_reason"] == (
            "fixture writer-boundary refusal"
        )
        assert envelope["data"]["idempotency_key"] == "writer-conflict-cancel"
        assert envelope["revision"] == store.get_runtime_revision()
        assert "fixture writer-boundary refusal" in envelope["data"]["rejection_reason"]


def test_plain_value_error_is_internal_for_single_and_unknown_stop_for_bulk(
    tmp_path, monkeypatch
):
    _database, _key_path, (first, second) = _workspace(tmp_path, monkeypatch)
    marker = "private-cancel-programming-marker"
    import aigineering.task_publication as task_publication

    def broken(*_args, **_kwargs):
        raise ValueError(marker)

    monkeypatch.setattr(task_publication, "publish_task_cancel", broken)
    single = _invoke(
        [
            "cancel",
            first.id,
            "--reason",
            "stop",
            "--idempotency-key",
            "single",
            "--json",
        ]
    )
    assert single.exit_code == 1
    single_payload = json.loads(single.stdout)
    assert single_payload["error"]["code"] == "internal_error"
    assert marker not in single.stdout

    batch = _invoke(
        [
            "cancel",
            first.id,
            second.id,
            "--reason",
            "stop",
            "--idempotency-key",
            "bulk",
            "--json",
        ]
    )
    assert batch.exit_code == 1
    payload = json.loads(batch.stdout)
    rows = payload["data"]["results"]
    assert [row["outcome"] for row in rows] == ["outcome_unknown", "not_attempted"]
    assert rows[0]["error"] == {
        "code": "internal_error",
        "exception_class": "ValueError",
    }
    assert rows[1]["command_attempted"] is False
    assert marker not in batch.stdout


def test_unavailable_rejection_proof_is_unknown_and_stops_bulk(tmp_path, monkeypatch):
    _database, _key_path, (first, second) = _workspace(tmp_path, monkeypatch)
    seeded = _invoke(
        [
            "cancel",
            first.id,
            "--reason",
            "stop",
            "--idempotency-key",
            "proof-seed",
            "--json",
        ]
    )
    assert seeded.exit_code == 0
    import aigineering.task_publication as task_publication

    marker = "private-rejection-proof-marker"

    def proof_unavailable(*_args, **_kwargs):
        raise RuntimeError(marker)

    monkeypatch.setattr(task_publication, "_cancel_rejection_result", proof_unavailable)
    single = _invoke(
        [
            "cancel",
            first.id,
            "--reason",
            "stop",
            "--idempotency-key",
            "proof-single",
            "--json",
        ]
    )
    assert single.exit_code == 1
    single_payload = json.loads(single.stdout)
    assert single_payload["error"]["code"] == "internal_error"
    assert "data" not in single_payload
    assert marker not in single.stdout

    batch = _invoke(
        [
            "cancel",
            first.id,
            second.id,
            "--reason",
            "stop",
            "--idempotency-key",
            "proof-batch",
            "--json",
        ]
    )
    assert batch.exit_code == 1
    rows = json.loads(batch.stdout)["data"]["results"]
    assert [row["outcome"] for row in rows] == ["outcome_unknown", "not_attempted"]
    assert "data" not in rows[0]
    assert "fact_ids" not in rows[0]
    assert rows[1]["command_attempted"] is False
    assert marker not in batch.stdout


def test_missing_cancel_target_remains_not_found(tmp_path, monkeypatch):
    _database, _key_path, _contracts = _workspace(tmp_path, monkeypatch)
    missing = _invoke(
        [
            "cancel",
            "task:v6:missing-cancel-target",
            "--reason",
            "stop",
            "--json",
        ]
    )
    assert missing.exit_code == 1
    payload = json.loads(missing.stdout)
    assert payload["error"]["code"] == "not_found"


def test_cancel_authentication_rejection_exposes_claimed_id_only(tmp_path, monkeypatch):
    database, key_path, (_other, contract) = _workspace(tmp_path, monkeypatch)
    from dataclasses import replace
    from aigineering.task_publication import _cancel_rejection_result

    signer = load_actor_signer(key_path)
    with SQLiteStore(database) as store:
        genesis = load_genesis(store)
        key = next(
            item for item in genesis.root_keys if item.public_key == signer.signer_id
        )
        proposal = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=key.actor_id,
            key_id=key.key_id,
            effects=(contract_cancellation_effect(contract.id, "stop"),),
            signer=signer,
            idempotency_key="bad-signature-cancel",
            causal_parents=(contract.id,),
        )
        decision = CandidateCommitter(store, store).commit(
            replace(proposal, signature="invalid-test-signature")
        )
        result = _cancel_rejection_result(
            store, decision, contract.id, "bad-signature-cancel", "stop"
        )
        assert result["accepted"] is False
        assert result["fact_ids"] == [result["rejection_record_id"]]
        assert result["claimed_candidate_id"] == proposal.id
        assert "candidate_id" not in result


def test_cancel_accepted_postcommit_pending_replays_original_fact(
    tmp_path, monkeypatch
):
    import aigineering.local_publication as local_publication

    database, _key_path, (_other, contract) = _workspace(tmp_path, monkeypatch)
    state = {"pending": True}

    def pending_after_commit(_store, *, accepted=None):
        if accepted is True and state["pending"]:
            state["pending"] = False
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr(local_publication, "sync_runtime", pending_after_commit)
    pending = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "stop",
            "--idempotency-key",
            "cancel-pending",
            "--json",
        ]
    )
    assert pending.exit_code == 1
    result = json.loads(pending.stdout)
    assert result["error"]["code"] == "redis_sync_pending"
    assert result["error"]["candidate_accepted"] is True
    assert result["error"]["facts_retained"] is True
    original_data = result["data"]
    assert original_data["idempotency_key"] == "cancel-pending"
    original_revision = result["revision"]
    with SQLiteStore.snapshot_from_path(database) as store:
        count_before = len(
            [
                row
                for _revision, row in store.scan_runtime_records(
                    record_type="lifecycle.terminal"
                )
                if row.payload.get("contract_id") == contract.id
            ]
        )

    replay = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "stop",
            "--idempotency-key",
            "cancel-pending",
            "--json",
        ]
    )
    assert replay.exit_code == 0, replay.output
    replay_payload = json.loads(replay.stdout)
    assert replay_payload["data"]["record_id"] == original_data["record_id"]
    assert replay_payload["data"]["candidate_id"] == original_data["candidate_id"]
    assert type(original_revision) is int
    with SQLiteStore.snapshot_from_path(database) as store:
        count_after = len(
            [
                row
                for _revision, row in store.scan_runtime_records(
                    record_type="lifecycle.terminal"
                )
                if row.payload.get("contract_id") == contract.id
            ]
        )
        assert count_after == count_before == 1


def test_single_cancel_pending_keeps_operation_revision_after_store_advances(
    tmp_path, monkeypatch
):
    import aigineering.local_publication as local_publication
    import aigineering.task_publication as task_publication

    database, _key_path, (first, second) = _workspace(tmp_path, monkeypatch)
    state = {"pending": True}

    def pending_after_commit(_store, *, accepted=None):
        if accepted is True and state["pending"]:
            state["pending"] = False
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr(local_publication, "sync_runtime", pending_after_commit)
    original_publish = task_publication.publish_task_cancel
    observed = {}

    def publish_then_advance(store, contract_id, reason, *, idempotency_key=None):
        try:
            return original_publish(
                store,
                contract_id,
                reason,
                idempotency_key=idempotency_key,
            )
        except RedisSyncPending as pending:
            if contract_id == first.id:
                observed["operation_revision"] = pending.revision
                original_publish(
                    store,
                    second.id,
                    "advance after first durable cancellation",
                    idempotency_key="revision-advance-cancel",
                )
                observed["later_revision"] = store.get_runtime_revision()
            raise

    monkeypatch.setattr(task_publication, "publish_task_cancel", publish_then_advance)
    result = _invoke(
        [
            "cancel",
            first.id,
            "--reason",
            "operation revision remains fixed",
            "--idempotency-key",
            "revision-bound-cancel",
            "--json",
        ]
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "redis_sync_pending"
    assert type(observed["operation_revision"]) is int
    assert observed["later_revision"] > observed["operation_revision"]
    assert payload["revision"] == observed["operation_revision"]
    with SQLiteStore.snapshot_from_path(database) as store:
        assert store.get_runtime_revision() == observed["later_revision"]


def test_single_cancel_prepublication_redis_failure_has_no_operation_result(
    tmp_path, monkeypatch
):
    database, _key_path, (_other, contract) = _workspace(tmp_path, monkeypatch)
    import aigineering.runtime_services as runtime_services

    def unavailable_before_publish(_store, *, accepted=None):
        assert accepted is None
        raise RedisSyncPending()

    monkeypatch.setattr(runtime_services, "sync_runtime", unavailable_before_publish)

    with SQLiteStore.snapshot_from_path(database) as store:
        before_revision = store.get_runtime_revision()
        before_records = tuple(store.scan_runtime_records())
    result = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "must not mint without Redis preflight",
            "--idempotency-key",
            "preflight-no-operation",
            "--json",
        ]
    )
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope == {
        "ok": False,
        "error": {
            "code": "redis_sync_pending",
            "message": "Redis synchronization failed before cancellation "
            "publication; no operation was attempted.",
            "facts_retained": False,
        },
    }
    with SQLiteStore.snapshot_from_path(database) as store:
        assert store.get_runtime_revision() == before_revision
        assert tuple(store.scan_runtime_records()) == before_records


def test_cancel_rejected_postcommit_pending_keeps_rejection_data(tmp_path, monkeypatch):
    import aigineering.local_publication as local_publication

    _database, _key_path, (_other, contract) = _workspace(tmp_path, monkeypatch)
    first = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "stop",
            "--idempotency-key",
            "cancel-first",
            "--json",
        ]
    )
    assert first.exit_code == 0
    state = {"pending": True}

    def pending_rejection(_store, *, accepted=None):
        if accepted is False and state["pending"]:
            state["pending"] = False
            raise RedisSyncPending(accepted=False)

    monkeypatch.setattr(local_publication, "sync_runtime", pending_rejection)
    result = _invoke(
        [
            "cancel",
            contract.id,
            "--reason",
            "stop",
            "--idempotency-key",
            "cancel-rejected-pending",
            "--json",
        ]
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "redis_sync_pending"
    assert payload["error"]["candidate_accepted"] is False
    assert payload["error"]["facts_retained"] is True
    assert payload["data"]["accepted"] is False
    assert payload["data"]["idempotency_key"] == "cancel-rejected-pending"
    assert payload["data"]["rejection_record_id"]
    assert payload["data"]["fact_ids"] == [payload["data"]["rejection_record_id"]]
