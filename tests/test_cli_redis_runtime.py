"""Operational CLI checks against an actual isolated Redis service."""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.application import collaboration_service
from aigineering.core.sqlite_store import SQLiteStore


@pytest.fixture
def live_cli(monkeypatch, tmp_path):
    url = os.getenv("AIG_REDIS_TEST_URL")
    if not url:
        pytest.skip("set AIG_REDIS_TEST_URL for actual Redis operational acceptance")
    monkeypatch.setenv("AIGINEERING_REDIS_URL", url)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    initialized = runner.invoke(cli, ["domain", "init", "--json"])
    assert initialized.exit_code == 0, initialized.output
    created = runner.invoke(
        cli,
        [
            "task",
            "create",
            "--name",
            "report",
            "--description",
            "Write a report",
            "--output",
            "report",
            "--json",
        ],
    )
    assert created.exit_code == 0, created.output
    return runner, json.loads(created.output)["contract_id"], tmp_path


def test_events_and_watch_are_real_read_only_redis_feeds(live_cli):
    runner, task_id, workspace = live_cli
    store = SQLiteStore.snapshot_from_path(workspace / ".aig/store.db")
    before = store.get_runtime_revision()
    store.close()
    read = runner.invoke(cli, ["task", "events", task_id, "--limit", "1000", "--json"])
    assert read.exit_code == 0, read.output
    payload = json.loads(read.output)
    assert payload["data"]
    assert any(event["record_type"] == "allowance.granted" for event in payload["data"])
    assert all(task_id in event["contract_ids"] for event in payload["data"])
    assert all(
        set(event)
        == {
            "stream_id",
            "revision",
            "record_id",
            "record_type",
            "contract_ids",
            "worker_ids",
        }
        for event in payload["data"]
    )
    assert all(isinstance(event["worker_ids"], list) for event in payload["data"])
    assert all(
        event["worker_ids"] == sorted(set(event["worker_ids"]))
        and all(isinstance(worker_id, str) for worker_id in event["worker_ids"])
        for event in payload["data"]
    )
    observed = runner.invoke(
        cli, ["task", "watch", task_id, "--timeout", "0", "--json"]
    )
    assert observed.exit_code == 0, observed.output
    lines = [json.loads(line) for line in observed.output.splitlines()]
    assert lines[-1]["kind"] == "watch_finished"
    assert any(line["kind"] == "event" for line in lines)
    store = SQLiteStore.snapshot_from_path(workspace / ".aig/store.db")
    assert store.get_runtime_revision() == before
    store.close()


def test_redis_status_rebuild_and_workspace_isolation(live_cli, monkeypatch, tmp_path):
    runner, _task_id, workspace = live_cli
    synced = runner.invoke(cli, ["redis", "sync", "--json"])
    assert synced.exit_code == 0, synced.output
    rebuilt = runner.invoke(cli, ["redis", "rebuild", "--json"])
    assert rebuilt.exit_code == 0, rebuilt.output
    payload = json.loads(rebuilt.output)
    assert payload["ok"] and payload["current"] and payload["rebuilt_digest"]
    store = SQLiteStore.snapshot_from_path(workspace / ".aig/store.db")
    original_stream = collaboration_service(store).stream_key
    original_events = collaboration_service(store).events(count=1000)
    store.close()
    second = tmp_path / "second"
    second.mkdir()
    monkeypatch.chdir(second)
    initialized = runner.invoke(cli, ["domain", "init", "--json"])
    assert initialized.exit_code == 0, initialized.output
    store = SQLiteStore.snapshot_from_path(second / ".aig/store.db")
    other = collaboration_service(store)
    other.rebuild()
    assert other.stream_key != original_stream
    store.close()
    monkeypatch.chdir(workspace)
    store = SQLiteStore.snapshot_from_path(workspace / ".aig/store.db")
    assert collaboration_service(store).events(count=1000) == original_events
    store.close()


def test_rebuild_cli_uses_latest_routed_source_for_query_and_final_status(monkeypatch):
    from types import SimpleNamespace

    import aigineering.cli.redis as redis_cli

    class Snapshot:
        def __init__(self, revision):
            self.revision = revision
            self.closed = False

        def get_runtime_revision(self):
            return self.revision

        def close(self):
            self.closed = True

    original = Snapshot(10)
    collaboration_fresh = Snapshot(11)
    query_fresh = Snapshot(12)
    snapshots = iter((original, collaboration_fresh, query_fresh))
    query_stores = []
    service_stores = []
    routed_sources = []

    class Service:
        def __init__(self, store):
            self.store = store
            service_stores.append(store)

        def rebuild(self, *, fresh_store_factory):
            assert fresh_store_factory() is collaboration_fresh

        def status(self):
            return {
                "available": True,
                "current": True,
                "authoritative_revision": self.store.get_runtime_revision(),
                "watermark": self.store.get_runtime_revision(),
                "lag": 0,
            }

    class Query:
        def __init__(self, store):
            query_stores.append(store)

        def rebuild(self, *, fresh_store_factory):
            routed_sources.append(fresh_store_factory())
            return SimpleNamespace(digest="fresh-digest")

    monkeypatch.setattr(redis_cli, "require_redis", lambda: None)
    monkeypatch.setattr(
        redis_cli, "_persistent_snapshot", lambda _path: next(snapshots)
    )
    monkeypatch.setattr(redis_cli, "collaboration_service", Service)
    monkeypatch.setattr(redis_cli, "query_projection", Query)

    result = CliRunner().invoke(cli, ["redis", "rebuild", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["authoritative_revision"] == 12
    assert payload["rebuilt_digest"] == "fresh-digest"
    assert query_stores == [original]
    assert service_stores == [original, query_fresh]
    assert routed_sources == [query_fresh]
    assert original.closed and collaboration_fresh.closed and query_fresh.closed


def test_redis_sync_renders_query_source_conflict_as_available_noncurrent_json(
    monkeypatch,
):
    import aigineering.cli.redis as redis_cli
    from aigineering.adapters.redis_query import QueryProjectionUnavailable

    class Snapshot:
        def close(self):
            pass

    class Service:
        def __init__(self, _store):
            pass

        def sync(self):
            return {}

    class Query:
        def __init__(self, _store):
            pass

        def get_all_contracts(self):
            raise QueryProjectionUnavailable(
                "Redis query projection source changed during capture"
            )

    monkeypatch.setattr(redis_cli, "require_redis", lambda: None)
    monkeypatch.setattr(redis_cli, "_persistent_snapshot", lambda _path: Snapshot())
    monkeypatch.setattr(redis_cli, "collaboration_service", Service)
    monkeypatch.setattr(redis_cli, "query_projection", Query)

    result = CliRunner().invoke(cli, ["redis", "sync", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "ok": False,
        "backend": "redis",
        "available": True,
        "required": True,
        "current": False,
        "reason": "source_conflict",
    }


def test_redis_sync_renders_conflict_reason_as_available_noncurrent_json(monkeypatch):
    import aigineering.cli.redis as redis_cli
    from aigineering.redis_collaboration import RedisCollaborationUnavailable

    class Snapshot:
        def close(self):
            pass

    monkeypatch.setattr(redis_cli, "require_redis", lambda: None)
    monkeypatch.setattr(redis_cli, "_persistent_snapshot", lambda _path: Snapshot())

    def conflicted(_store):
        raise RedisCollaborationUnavailable("ahead", reason="redis_ahead")

    monkeypatch.setattr(redis_cli, "collaboration_service", conflicted)

    result = CliRunner().invoke(cli, ["redis", "sync", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "ok": False,
        "backend": "redis",
        "available": True,
        "required": True,
        "current": False,
        "reason": "redis_ahead",
    }


def test_outage_fails_before_publication_and_offline_audit_still_works(
    live_cli, monkeypatch
):
    runner, task_id, workspace = live_cli
    before = SQLiteStore.snapshot_from_path(workspace / ".aig/store.db")
    revision = before.get_runtime_revision()
    before.close()
    monkeypatch.setenv(
        "AIGINEERING_REDIS_URL", "redis://private-user:private-password@127.0.0.1:1/0"
    )
    failed = runner.invoke(
        cli,
        ["task", "create", "--name", "never-published", "--output", "later", "--json"],
    )
    assert failed.exit_code == 1
    assert json.loads(failed.output)["error"] == "redis_unavailable"
    assert (
        "private-password" not in failed.output and "private-user" not in failed.output
    )
    status = runner.invoke(cli, ["redis", "status", "--json"])
    assert status.exit_code == 1 and not json.loads(status.output)["available"]
    audited = runner.invoke(cli, ["--offline", "task", "audit", task_id, "--json"])
    assert audited.exit_code == 0, audited.output
    prohibited = runner.invoke(
        cli, ["--offline", "task", "cancel", task_id, "--reason", "offline", "--json"]
    )
    assert prohibited.exit_code == 2
    after = SQLiteStore.snapshot_from_path(workspace / ".aig/store.db")
    assert after.get_runtime_revision() == revision
    after.close()


def test_help_does_not_need_redis(monkeypatch):
    monkeypatch.setenv("AIGINEERING_REDIS_URL", "redis://127.0.0.1:1/0")
    for args in (
        ["--help"],
        ["task", "--help"],
        ["task", "create", "--help"],
        ["run", "--help"],
    ):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output


def test_offline_session_and_trace_inspection_do_not_create_directories(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIGINEERING_REDIS_URL", "redis://127.0.0.1:1/0")
    for command in (
        ["--offline", "session", "ls", "--json"],
        ["--offline", "session", "show", "session_missing"],
        ["--offline", "trace", "--session", "session_missing"],
    ):
        CliRunner().invoke(cli, command)
        assert not (tmp_path / ".aig").exists()


def test_committed_decision_with_relay_failure_is_not_reported_as_rejection(
    live_cli, monkeypatch
):
    from aigineering.adapters.redis_query import QueryProjectionUnavailable

    runner, _task_id, workspace = live_cli

    class InterruptedRelay:
        def sync(self):
            raise QueryProjectionUnavailable("Redis relay interrupted")

    with monkeypatch.context() as partial:
        partial.setattr(
            "aigineering.runtime_services.collaboration_service",
            lambda _store: InterruptedRelay(),
        )
        result = runner.invoke(
            cli,
            [
                "task",
                "create",
                "--name",
                "retained-decision",
                "--output",
                "retained-output",
                "--json",
            ],
        )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "redis_sync_pending"
    assert payload["facts_retained"] and payload["candidate_accepted"]
    store = SQLiteStore.snapshot_from_path(workspace / ".aig/store.db")
    committed = [
        contract
        for contract in store.get_all_contracts()
        if contract.name == "retained-decision"
    ]
    assert len(committed) == 1
    store.close()
    recovered = runner.invoke(cli, ["redis", "sync", "--json"])
    assert recovered.exit_code == 0, recovered.output
    visible = runner.invoke(cli, ["task", "show", committed[0].id, "--json"])
    assert visible.exit_code == 0, visible.output
    assert json.loads(visible.output)["data"]["task"]["status"] == "ready"
