"""One retry result contract across the legacy and task CLI spellings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain
from aigineering.runtime_services import RedisSyncPending
from aigineering.task_publication import publish_task_contract


class _Projection:
    def __init__(self, store):
        self.store = store

    def get_contract(self, contract_id):
        return self.store.get_contract(contract_id)


class _Collaboration:
    def sync(self):
        return None


def _workspace(tmp_path: Path, monkeypatch):
    import aigineering.application as application
    import aigineering.cli.infrastructure as infrastructure
    import aigineering.cli.retry as retry_cli
    import aigineering.cli.task_board as task_board
    import aigineering.local_publication as local_publication

    monkeypatch.chdir(tmp_path)
    database = tmp_path / ".aig" / "store.db"
    key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(key_path))
    monkeypatch.setattr(application, "require_redis", lambda: None)
    monkeypatch.setattr(infrastructure, "require_redis", lambda: None)
    monkeypatch.setattr(task_board, "require_redis", lambda: None)
    monkeypatch.setattr(local_publication, "require_redis", lambda: None)
    monkeypatch.setattr(local_publication, "sync_runtime", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        task_board, "collaboration_service", lambda _store: _Collaboration()
    )
    monkeypatch.setattr(
        task_board, "query_projection", lambda store: _Projection(store)
    )
    monkeypatch.setattr(task_board, "persistent_store", lambda: SQLiteStore(database))
    monkeypatch.setattr(retry_cli, "_persistent_store", lambda: SQLiteStore(database))

    database.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteStore(database) as store:
        ensure_local_domain(store)
        original = publish_task_contract(
            store,
            build_control_plane_contract(
                name="retry consistency target",
                description="Preserve this exact obligation while publishing a retry.",
                outputs=("answer",),
                budget=3,
            ),
        )
    return database, original


def _invoke(spelling: str, contract_id: str, *, json_output: bool = True):
    if spelling == "legacy":
        args = ["retry", "--contract", contract_id]
    else:
        args = ["task", "retry", contract_id]
    if json_output:
        args.append("--json")
    return CliRunner().invoke(cli, args)


def _snapshot(database: Path):
    return SQLiteStore.snapshot_from_path(database)


@pytest.mark.parametrize("spelling", ["legacy", "task"])
def test_retry_success_uses_one_structured_envelope_and_preserves_original(
    tmp_path, monkeypatch, spelling
):
    database, original = _workspace(tmp_path, monkeypatch)
    with _snapshot(database) as store:
        before_claim_history = tuple(
            (revision, record.id, record.record_type)
            for revision, record in store.scan_runtime_records()
            if record.record_type.startswith("claim.")
            and record.payload.get("contract_id") == original.id
        )

    result = _invoke(spelling, original.id)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["original_contract_id"] == original.id
    assert data["retry_contract_id"]
    assert data["budget"] == original.budget
    assert data["status"] == "declared"
    assert data["candidate_id"].startswith("candidate:v1:")
    assert data["record_id"]
    assert data["fact_ids"] == [data["record_id"]]
    assert data["idempotency_key"] == (
        f"retry:{original.id}:{data['retry_contract_id']}"
    )
    assert payload["original_contract_id"] == data["original_contract_id"]
    assert payload["retry_contract_id"] == data["retry_contract_id"]
    assert type(payload["revision"]) is int

    with _snapshot(database) as store:
        retry_facts = [
            record
            for _revision, record in store.scan_runtime_records(
                record_type="contract.declared"
            )
            if record.payload.get("contract", {}).get("id") == data["retry_contract_id"]
        ]
        assert len(retry_facts) == 1
        assert retry_facts[0].id == data["record_id"]
        assert retry_facts[0].payload["candidate_id"] == data["candidate_id"]
        receipts = [
            record
            for _revision, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("candidate_id") == data["candidate_id"]
        ]
        assert len(receipts) == 1
        assert "idempotency_key" not in receipts[0].payload
        assert (
            data["idempotency_key"]
            == f"retry:{original.id}:{data['retry_contract_id']}"
        )
        assert store.get_contract(original.id) == original
        after_claim_history = tuple(
            (revision, record.id, record.record_type)
            for revision, record in store.scan_runtime_records()
            if record.record_type.startswith("claim.")
            and record.payload.get("contract_id") == original.id
        )
        assert after_claim_history == before_claim_history


@pytest.mark.parametrize("spelling", ["legacy", "task"])
def test_retry_human_output_preserves_legacy_success_phrase(
    tmp_path, monkeypatch, spelling
):
    _database, original = _workspace(tmp_path, monkeypatch)
    result = _invoke(spelling, original.id, json_output=False)
    assert result.exit_code == 0, result.output
    from aigineering.plugins.task_semantics import retry_contract

    expected_id = retry_contract(original).id
    assert result.output.splitlines()[0] == f"Retry contract created: {expected_id}"
    assert result.output.splitlines()[1] == f"  Budget: {original.budget}"


@pytest.mark.parametrize("spelling", ["legacy", "task"])
def test_retry_not_found_uses_same_json_error_shape(tmp_path, monkeypatch, spelling):
    _database, _original = _workspace(tmp_path, monkeypatch)
    result = _invoke(spelling, "task:v6:missing-retry-target")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["not_found"] is True
    assert payload["error"]["code"] == "not_found"
    assert payload["revision"] is None or type(payload["revision"]) is int


@pytest.mark.parametrize("spelling", ["legacy", "task"])
def test_retry_preflight_redis_failure_has_no_operation_result(
    tmp_path, monkeypatch, spelling
):
    from aigineering.adapters.redis_query import QueryProjectionUnavailable
    import aigineering.local_publication as local_publication

    database, original = _workspace(tmp_path, monkeypatch)
    marker = "retry-preflight-private-marker"

    def unavailable():
        raise QueryProjectionUnavailable(marker)

    monkeypatch.setattr(local_publication, "require_redis", unavailable)
    with _snapshot(database) as store:
        contract_ids_before = {contract.id for contract in store.get_all_contracts()}
    result = _invoke(spelling, original.id)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "redis_unavailable"
    assert "data" not in payload
    assert "candidate_accepted" not in payload
    assert marker not in result.output
    with _snapshot(database) as store:
        assert {
            contract.id for contract in store.get_all_contracts()
        } == contract_ids_before


@pytest.mark.parametrize("spelling", ["legacy", "task"])
def test_retry_postcommit_pending_exposes_exact_result_and_replays(
    tmp_path, monkeypatch, spelling
):
    import aigineering.local_publication as local_publication

    database, original = _workspace(tmp_path, monkeypatch)
    state = {"pending": True}

    def sync_after_commit(_store, *, accepted=None):
        if accepted is True and state["pending"]:
            state["pending"] = False
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr(local_publication, "sync_runtime", sync_after_commit)
    failed = _invoke(spelling, original.id)
    assert failed.exit_code == 1
    pending = json.loads(failed.output)
    assert pending["ok"] is False
    assert pending["error"]["code"] == "redis_sync_pending"
    assert pending["candidate_accepted"] is True
    assert pending["facts_retained"] is True
    data = pending["data"]
    assert data["original_contract_id"] == original.id
    assert data["candidate_id"].startswith("candidate:v1:")
    assert data["fact_ids"] == [data["record_id"]]
    assert data["idempotency_key"] == (
        f"retry:{original.id}:{data['retry_contract_id']}"
    )
    assert type(pending["revision"]) is int

    with _snapshot(database) as store:
        retry_facts_before = [
            record
            for _revision, record in store.scan_runtime_records(
                record_type="contract.declared"
            )
            if record.payload.get("contract", {}).get("id") == data["retry_contract_id"]
        ]
        assert len(retry_facts_before) == 1
        assert retry_facts_before[0].id == data["record_id"]
        assert retry_facts_before[0].payload["candidate_id"] == data["candidate_id"]

    replay = _invoke(spelling, original.id)
    assert replay.exit_code == 0, replay.output
    replay_payload = json.loads(replay.output)
    assert replay_payload["data"] == data
    with _snapshot(database) as store:
        retry_facts_after = [
            record
            for _revision, record in store.scan_runtime_records(
                record_type="contract.declared"
            )
            if record.payload.get("contract", {}).get("id") == data["retry_contract_id"]
        ]
        assert len(retry_facts_after) == 1


@pytest.mark.parametrize("spelling", ["legacy", "task"])
def test_rejected_retry_reports_only_exact_rejection_ids(
    tmp_path, monkeypatch, spelling
):
    import aigineering.cli.retry as retry_cli
    import aigineering.cli.task_board as task_board
    from aigineering.task_publication import RetryPublicationRejected, retry_contract

    _database, original = _workspace(tmp_path, monkeypatch)
    rejection_id = "record:retry-rejected"
    actual_key = f"retry:{original.id}:{retry_contract(original).id}"

    def reject(_store, _contract_id):
        raise RetryPublicationRejected(
            {
                "rejection_fact_id": rejection_id,
                "fact_ids": [rejection_id],
                "idempotency_key": actual_key,
            },
            19,
        )

    owner = retry_cli if spelling == "legacy" else task_board
    monkeypatch.setattr(owner, "retry_local_task_result", reject)
    result = _invoke(spelling, original.id)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "retry_rejected"
    assert payload["candidate_accepted"] is False
    assert payload["facts_retained"] is True
    assert payload["data"] == {
        "rejection_fact_id": rejection_id,
        "fact_ids": [rejection_id],
        "idempotency_key": actual_key,
    }
    assert payload["revision"] == 19


@pytest.mark.parametrize("spelling", ["legacy", "task"])
def test_unexpected_retry_error_is_safe_and_stops(tmp_path, monkeypatch, spelling):
    import aigineering.cli.retry as retry_cli
    import aigineering.cli.task_board as task_board

    _database, original = _workspace(tmp_path, monkeypatch)
    marker = "retry-private-internal-marker"

    def fail(_store, _contract_id):
        try:
            raise KeyError(marker)
        except KeyError as cause:
            raise RuntimeError("internal retry failure") from cause

    owner = retry_cli if spelling == "legacy" else task_board
    monkeypatch.setattr(owner, "retry_local_task_result", fail)
    result = _invoke(spelling, original.id)
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"]["code"] == "internal_error"
    assert marker not in result.output
