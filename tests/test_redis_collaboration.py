from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace

import pytest

from aigineering.redis_collaboration import (
    RedisCollaborationService,
    RedisCollaborationUnavailable,
)


class MemoryRedis:
    def __init__(self):
        self.hashes = {}
        self.streams = {}
        self.available = True

    def _check(self):
        if not self.available:
            raise ConnectionError("redis://private-secret")

    def ping(self):
        self._check()
        return True

    def hget(self, key, field):
        self._check()
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        self._check()
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        self._check()
        target = self.hashes.setdefault(key, {})
        if mapping is not None:
            target.update(mapping)
        elif field is not None:
            target[field] = value

    def eval(self, script, _key_count, *args):
        self._check()
        if "XREVRANGE" in script and "return {watermark" in script:
            stream, metadata = args[:2]
            watermark = self.hashes.get(metadata, {}).get("watermark", "0")
            schema = self.hashes.get(metadata, {}).get("schema", "")
            tail = self.xrevrange(stream, count=1)
            return [watermark, schema, tail[0][0] if tail else ""]
        if "tail_id ~= ARGV[3]" in script:
            stream, metadata, expected_watermark, expected_schema, expected_tail = args
            current_watermark = self.hashes.get(metadata, {}).get("watermark", "0")
            current_schema = self.hashes.get(metadata, {}).get("schema", "")
            tail = self.xrevrange(stream, count=1)
            current_tail = tail[0][0] if tail else ""
            if (current_watermark, current_schema, current_tail) != (
                expected_watermark,
                expected_schema,
                expected_tail,
            ):
                return 0
            self.streams.pop(stream, None)
            self.hashes.pop(metadata, None)
            return 1
        stream, metadata, revision, record_id, kind, refs, workers, schema, expected = (
            args
        )
        current = int(self.hashes.get(metadata, {}).get("watermark", "0"))
        target = int(revision)
        if current >= target:
            return 0
        if current != int(expected):
            return -1
        self.streams.setdefault(stream, []).append(
            (
                f"{target}-0",
                {
                    "revision": revision,
                    "record_id": record_id,
                    "record_type": kind,
                    "contract_ids": refs,
                    "worker_ids": workers,
                },
            )
        )
        self.hashes[metadata] = {"watermark": revision, "schema": schema}
        return 1

    def xrevrange(self, stream, count=1):
        self._check()
        return list(reversed(self.streams.get(stream, [])))[:count]

    def xread(self, streams, count=100, block=None):
        self._check()
        key, cursor = next(iter(streams.items()))
        entries = [
            entry for entry in self.streams.get(key, []) if _id_gt(entry[0], cursor)
        ]
        return [(key, entries[:count])] if entries else []

    def scan(self, cursor=0, match=None, count=500):
        self._check()
        import fnmatch

        keys = [*self.hashes, *self.streams]
        return 0, [key for key in keys if fnmatch.fnmatch(key, match or "*")]

    def delete(self, *keys):
        self._check()
        for key in keys:
            self.hashes.pop(key, None)
            self.streams.pop(key, None)


def _id_gt(left: str, right: str) -> bool:
    return tuple(map(int, left.split("-"))) > tuple(map(int, right.split("-")))


class MemoryStore:
    def __init__(self, records=()):
        self.records = list(records)

    def get_runtime_revision(self):
        return self.records[-1][0] if self.records else 0

    def scan_runtime_records(self, *, after_revision=0, through_revision=None):
        through_revision = through_revision or self.get_runtime_revision()
        return [
            (revision, record)
            for revision, record in self.records
            if after_revision < revision <= through_revision
        ]


def _record(revision: int):
    return SimpleNamespace(
        id=f"record:{revision}",
        record_type="contract.declared",
        causal_parents=(),
        payload={
            "contract": {"id": f"task:v1:{revision}"},
            "secret": "sealed-private-content",
            "prompt": "must never enter Redis",
            "custom_json": {"contract_id": "task:v1:untrusted-body"},
        },
    )


def _service(store, client, domain="domain:one"):
    return RedisCollaborationService(
        store,
        client,
        domain_id=domain,
        redis_errors=(ConnectionError,),
    )


def test_sync_is_idempotent_and_stream_contains_only_public_metadata():
    store = MemoryStore([(1, _record(1)), (2, _record(2))])
    redis = MemoryRedis()
    service = _service(store, redis)

    assert service.sync()["watermark"] == 2
    assert service.sync()["watermark"] == 2
    events = service.events(after_revision=0)
    assert [event["revision"] for event in events] == [1, 2]
    assert all(event["contract_ids"] for event in events)
    serialized = json.dumps(redis.streams)
    assert "sealed-private-content" not in serialized
    assert "must never enter Redis" not in serialized


def test_task_event_references_include_budget_qualification_and_claim_renewal():
    from aigineering.protocol.runtime_record import create_runtime_record

    task_id = "task:v3:task-reference"
    child_id = "task:v3:child-reference"
    grant = create_runtime_record(
        "claim.granted", {"contract_id": task_id, "claim_id": "claim:test"}
    )
    renewed = create_runtime_record(
        "claim.renewed", {"claim_id": "claim:test"}, causal_parents=(grant.id,)
    )
    records = (
        grant,
        renewed,
        create_runtime_record(
            "allowance.reserved",
            {"source_contract_id": task_id, "child_contract_id": child_id},
        ),
        create_runtime_record(
            "allowance.returned",
            {
                "source_contract_id": task_id,
                "from_contract_id": child_id,
                "amount": 2,
            },
        ),
        create_runtime_record("allowance.granted", {"contract_id": task_id}),
        create_runtime_record("asset.attested", {"contract_id": task_id}),
        create_runtime_record("attempt.closed", {"contract_id": task_id}),
        create_runtime_record(
            "task_completion.projected", {"source_contract_id": task_id}
        ),
        create_runtime_record(
            "worker.invocation_failed",
            {
                "contract_id": task_id,
                "diagnostic": {"contract_id": "task:v3:untrusted"},
            },
        ),
    )
    store = MemoryStore(list(enumerate(records, 1)))
    service = _service(store, MemoryRedis())
    events = service.events(count=100)
    assert len(events) == len(records)
    assert all(task_id in event["contract_ids"] for event in events)
    reserved = next(
        event for event in events if event["record_type"] == "allowance.reserved"
    )
    assert child_id in reserved["contract_ids"]
    returned = next(
        event for event in events if event["record_type"] == "allowance.returned"
    )
    assert set(returned["contract_ids"]) == {task_id, child_id}
    assert all("task:v3:untrusted" not in event["contract_ids"] for event in events)


def test_assignment_worker_references_are_typed_and_survive_event_rebuild():
    from aigineering.protocol.runtime_record import create_runtime_record

    task_id = "task:v3:assigned"
    assignment = create_runtime_record(
        "claim.assignment.changed",
        {
            "actor_id": "plugin:task.assignment.operator.v1",
            "candidate_id": "candidate:assignment",
            "contract_id": task_id,
            "key_id": "plugin:key",
            "previous_assignment_id": "",
            "reason": "route work",
            "worker_id": "worker:one",
        },
    )
    forged = create_runtime_record(
        "user.note", {"contract_id": task_id, "worker_id": "worker:forged"}
    )
    store = MemoryStore([(1, assignment), (2, forged)])
    redis = MemoryRedis()
    service = _service(store, redis)

    events = service.events()
    assert events[0]["worker_ids"] == ("worker:one",)
    assert events[1]["worker_ids"] == ()
    before = events
    service.rebuild()
    assert service.events() == before


def test_rebuild_replays_only_its_domain_namespace():
    store = MemoryStore([(1, _record(1))])
    redis = MemoryRedis()
    first = _service(store, redis, "domain:one")
    other = _service(store, redis, "domain:two")
    first.sync()
    other.sync()
    before = other.events()

    first.rebuild()

    assert first.status()["watermark"] == 1
    assert other.events() == before


def test_missing_stream_is_rebuilt_from_sqlite_records():
    store = MemoryStore([(1, _record(1))])
    redis = MemoryRedis()
    service = _service(store, redis)
    service.sync()
    redis.streams.clear()

    assert service.sync()["watermark"] == 1
    assert [event["revision"] for event in service.events()] == [1]


def test_concurrent_syncs_are_fenced_and_never_duplicate_stream_entries():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = MemoryStore([(revision, _record(revision)) for revision in range(1, 21)])
    redis = MemoryRedis()
    services = [_service(store, redis) for _ in range(8)]
    barrier = Barrier(len(services))

    def sync(service):
        barrier.wait()
        return service.sync()

    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        results = list(pool.map(sync, services))

    revisions = [event["revision"] for event in services[0].events()]
    assert revisions == list(range(1, 21))
    assert all(result["watermark"] == 20 for result in results)


def test_unavailable_service_raises_stable_error_without_connection_details():
    redis = MemoryRedis()
    redis.available = False
    service = _service(MemoryStore([(1, _record(1))]), redis)

    with pytest.raises(RedisCollaborationUnavailable) as exc:
        service.sync()

    assert str(exc.value) == "Redis collaboration service unavailable"
    assert "private-secret" not in str(exc.value)


def test_application_preflight_sanitizes_redis_connection_errors(monkeypatch):
    import redis as redis_module

    from aigineering import application

    class BrokenClient:
        def ping(self):
            raise redis_module.ConnectionError("redis://secret-user:secret-pass@host")

    monkeypatch.setattr(
        "aigineering.adapters.redis_query.redis_client", lambda _url: BrokenClient()
    )

    with pytest.raises(RedisCollaborationUnavailable) as exc:
        application.require_redis("redis://secret-host")

    assert str(exc.value) == (
        "Redis collaboration service unavailable; check Redis installation and health"
    )
    assert "secret" not in str(exc.value)


def test_uninitialized_domain_is_explicit():
    service = RedisCollaborationService(
        MemoryStore(), MemoryRedis(), domain_id=None, redis_errors=(ConnectionError,)
    )
    assert service.status()["reason"] == "domain_uninitialized"
    with pytest.raises(RedisCollaborationUnavailable, match="domain_uninitialized"):
        service.sync()


def test_redis_integration_replay_and_isolation():
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL to run Redis integration")
    redis_module = pytest.importorskip("redis")
    client = redis_module.Redis.from_url(
        redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2
    )
    run_id = uuid.uuid4().hex
    store = MemoryStore([(1, _record(1)), (2, _record(2))])
    service = RedisCollaborationService(
        store,
        client,
        domain_id=f"integration-domain-{run_id}",
        redis_errors=(redis_module.RedisError,),
    )
    assert service.sync()["watermark"] == 2
    assert service.sync()["watermark"] == 2
    assert [item["revision"] for item in service.events()] == [1, 2]
    service.rebuild()
    assert service.status()["watermark"] == 2

    isolated_service = RedisCollaborationService(
        MemoryStore([(1, _record(1))]),
        client,
        domain_id=f"integration-isolated-{run_id}",
        redis_errors=(redis_module.RedisError,),
    )
    assert isolated_service.stream_key != service.stream_key
    assert isolated_service.metadata_key != service.metadata_key
    assert [item["revision"] for item in isolated_service.events()] == [1]
    assert [item["revision"] for item in service.events()] == [1, 2]
    isolated_service.rebuild()
    assert isolated_service.status()["watermark"] == 1
    assert service.status()["watermark"] == 2

    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    concurrent_store = MemoryStore(
        [(revision, _record(revision)) for revision in range(1, 41)]
    )
    concurrent_services = [
        RedisCollaborationService(
            concurrent_store,
            client,
            domain_id=f"integration-concurrent-{run_id}",
            redis_errors=(redis_module.RedisError,),
        )
        for _ in range(8)
    ]
    barrier = Barrier(len(concurrent_services))

    def concurrent_sync(item):
        barrier.wait()
        return item.sync()

    with ThreadPoolExecutor(max_workers=len(concurrent_services)) as pool:
        results = list(pool.map(concurrent_sync, concurrent_services))
    revisions = [item["revision"] for item in concurrent_services[0].events()]
    assert revisions == list(range(1, 41))
    assert all(item["watermark"] == 40 for item in results)
    assert [item["revision"] for item in service.events()] == [1, 2]
    assert [item["revision"] for item in isolated_service.events()] == [1]
