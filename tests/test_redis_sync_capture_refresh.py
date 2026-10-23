from __future__ import annotations

import os
import uuid

import pytest

from aigineering.redis_collaboration import (
    RedisCollaborationService,
    RedisCollaborationUnavailable,
)
from test_redis_collaboration import MemoryRedis, MemoryStore, _record, _service


def test_sync_refreshes_same_sqlite_store_after_concurrent_relay(tmp_path, monkeypatch):
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL to an isolated Redis test database")
    redis_module = pytest.importorskip("redis")
    client = redis_module.Redis.from_url(
        redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2
    )
    client.ping()

    from aigineering.core.ids import hash_asset_content
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.protocol.types import Asset
    from conftest import candidate_runtime

    path = tmp_path / "concurrent-relay.sqlite3"
    store = SQLiteStore(path)
    setup = candidate_runtime(store)
    second_store = SQLiteStore(path)
    second_setup = candidate_runtime(
        second_store,
        genesis=setup.genesis,
        actor_key=setup.actor_key,
        signer=setup.signer,
    )
    run_id = uuid.uuid4().hex
    domain_id = f"{setup.genesis.id}:{run_id}"
    first = RedisCollaborationService(
        store,
        client,
        domain_id=domain_id,
        redis_errors=(redis_module.RedisError,),
    )
    second = RedisCollaborationService(
        second_store,
        client,
        domain_id=domain_id,
        redis_errors=(redis_module.RedisError,),
    )
    original_capture = first._capture_events
    captured_revisions = []
    relayed = False

    def capture_then_race(source, through=None):
        nonlocal relayed
        result = original_capture(source, through=through)
        captured_revisions.append(result[0])
        if not relayed:
            relayed = True
            content = "newly committed signed source fact"
            asset = Asset(
                id=hash_asset_content("concurrent-source-fact", content),
                name="concurrent-source-fact",
                content=content,
                origin="test",
            )
            accepted = second_setup.accept_asset(asset)
            assert accepted.content == content
            second.sync()
        return result

    monkeypatch.setattr(first, "_capture_events", capture_then_race)
    try:
        result = first.sync()
        current_revision = store.get_runtime_revision()
        assert current_revision > captured_revisions[0]
        assert captured_revisions[1] == current_revision
        assert result["watermark"] == current_revision
        assert first.status()["current"] is True
        assert any(
            event["record_type"] == "asset.committed" for event in first.events()
        )
    finally:
        client.delete(first.stream_key, first.metadata_key)
        second_store.close()
        store.close()


def test_sync_rejects_refresh_when_original_source_prefix_diverged():
    store = MemoryStore([(1, _record(1)), (2, _record(2))])
    redis = MemoryRedis()
    first = _service(store, redis, domain="domain:refresh-diverged")
    second = _service(store, redis, domain="domain:refresh-diverged")
    original_capture = first._capture_events
    calls = 0
    redis_snapshot = None

    def capture_then_fork(source, through=None):
        nonlocal calls, redis_snapshot
        calls += 1
        if calls == 1:
            captured = original_capture(source, through=1)
            assert second.sync()["watermark"] == 2
            redis_snapshot = (
                list(redis.streams[first.stream_key]),
                dict(redis.hashes[first.metadata_key]),
            )
            # This fake-only source models an immutable fork with a valid later
            # revision but a different committed prefix. Durable SQLite facts
            # are never edited to create the test state.
            source.records = [(1, _record(9)), (2, _record(2))]
            return captured
        return original_capture(source, through=through)

    first._capture_events = capture_then_fork
    with pytest.raises(RedisCollaborationUnavailable) as exc:
        first.sync()

    assert exc.value.reason == "source_diverged"
    assert calls == 2
    assert (
        redis.streams[first.stream_key],
        redis.hashes[first.metadata_key],
    ) == redis_snapshot


def test_genuine_redis_ahead_keeps_original_snapshot_and_bytes():
    store = MemoryStore([(1, _record(1))])
    redis = MemoryRedis()
    service = _service(store, redis, domain="domain:genuine-ahead")
    service.sync()
    redis.xadd(
        service.stream_key,
        {
            "revision": "2",
            "record_id": "record:2",
            "record_type": "asset.committed",
            "contract_ids": "[]",
            "worker_ids": "[]",
        },
        id="2-0",
    )
    redis.hset(
        service.metadata_key,
        mapping={"watermark": "2", "schema": "v2"},
    )
    before = (
        list(redis.streams[service.stream_key]),
        dict(redis.hashes[service.metadata_key]),
    )
    original_capture = service._capture_events
    captured_revisions = []

    def capture(source, through=None):
        result = original_capture(source, through=through)
        captured_revisions.append(result[0])
        return result

    service._capture_events = capture
    with pytest.raises(RedisCollaborationUnavailable) as exc:
        service.sync()

    assert exc.value.reason == "redis_ahead"
    assert captured_revisions == [1, 1]
    assert (
        redis.streams[service.stream_key],
        redis.hashes[service.metadata_key],
    ) == before


def test_sync_contention_budget_does_not_recapture_after_final_attempt():
    store = MemoryStore([(1, _record(1))])
    service = _service(store, MemoryRedis(), domain="domain:refresh-contention")
    original_capture = service._capture_events
    captured_revisions = []
    attempted_revisions = []

    def capture_and_advance(source, through=None):
        if captured_revisions:
            next_revision = source.records[-1][0] + 1
            source.records.append((next_revision, _record(next_revision)))
        result = original_capture(source, through=through)
        captured_revisions.append(result[0])
        return result

    def persistent_ahead(revision, entries):
        attempted_revisions.append(revision)
        raise RedisCollaborationUnavailable(
            "fixture concurrent relay", reason="redis_ahead"
        )

    service._capture_events = capture_and_advance
    service._sync_attempt = persistent_ahead
    with pytest.raises(RedisCollaborationUnavailable) as exc:
        service.sync()

    assert exc.value.reason == "sync_contention"
    assert captured_revisions == [1, 2, 3]
    assert attempted_revisions == [1, 2, 3]


def test_events_reproves_frozen_prefix_after_sync_contention():
    store = MemoryStore([(1, _record(1)), (2, _record(2))])
    redis = MemoryRedis()
    events_service = _service(store, redis, domain="domain:events-contention-retry")
    concurrent_service = _service(store, redis, domain="domain:events-contention-retry")
    original_capture = events_service._capture_events
    captured_revisions = []

    def capture(source, through=None):
        result = original_capture(source, through=through)
        captured_revisions.append(result[0])
        return result

    events_service._capture_events = capture
    sync_calls = []

    def concurrent_sync_contention():
        sync_calls.append(True)
        store.records.append((3, _record(3)))
        assert concurrent_service.sync()["watermark"] == 3
        raise RedisCollaborationUnavailable(
            "fixture concurrent relay contention", reason="sync_contention"
        )

    events_service.sync = concurrent_sync_contention

    events = events_service.events()

    assert [event["revision"] for event in events] == [1, 2]
    assert captured_revisions == [2]
    assert sync_calls == [True]
    assert [event["revision"] for event in concurrent_service.events()] == [1, 2, 3]


def test_events_sync_contention_budget_remains_bounded():
    store = MemoryStore([(1, _record(1))])
    redis = MemoryRedis()
    service = _service(store, redis, domain="domain:events-contention-budget")
    original_capture = service._capture_events
    captured_revisions = []

    def capture(source, through=None):
        result = original_capture(source, through=through)
        captured_revisions.append(result[0])
        return result

    service._capture_events = capture
    calls = []

    def contention():
        calls.append(True)
        raise RedisCollaborationUnavailable(
            "fixture relay contention", reason="sync_contention"
        )

    service.sync = contention
    with pytest.raises(RedisCollaborationUnavailable) as exc:
        service.events()

    assert exc.value.reason == "sync_contention"
    assert len(calls) == 3
    assert captured_revisions == [1]
    assert redis.streams == {}
    assert redis.hashes == {}


def test_events_refuses_noncontention_sync_error_without_retry_or_write():
    store = MemoryStore([(1, _record(1))])
    redis = MemoryRedis()
    service = _service(store, redis, domain="domain:events-noncontention")
    calls = []

    def unrelated_failure():
        calls.append(True)
        raise RedisCollaborationUnavailable(
            "fixture source mismatch", reason="source_diverged"
        )

    service.sync = unrelated_failure
    with pytest.raises(RedisCollaborationUnavailable) as exc:
        service.events()

    assert exc.value.reason == "source_diverged"
    assert calls == [True]
    assert redis.streams == {}
    assert redis.hashes == {}
