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
        self.versions = {}
        self.expirations = {}
        self.before_execute = None

    def _touch(self, *keys):
        for key in keys:
            self.versions[key] = self.versions.get(key, 0) + 1

    def _check(self):
        if not self.available:
            raise ConnectionError("redis://private-secret")

    def ping(self):
        self._check()
        return True

    def pipeline(self, transaction=True):
        self._check()
        return MemoryPipeline(self)

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
        self._touch(key)

    def xadd(self, stream, fields, id="*"):
        self._check()
        self.streams.setdefault(stream, []).append((id, dict(fields)))
        self._touch(stream)

    def expire(self, key, seconds):
        self._check()
        self.expirations[key] = seconds
        return True

    def eval(self, script, _key_count, *args):
        self._check()
        if "return {st, mt, watermark, schema, tail_id}" in script:
            stream, metadata = args[:2]
            stream_type = "stream" if stream in self.streams else "none"
            meta_type = "hash" if metadata in self.hashes else "none"
            fields = self.hashes.get(metadata, {})
            tail = self.xrevrange(stream, count=1)
            return [
                stream_type,
                meta_type,
                fields.get("watermark", ""),
                fields.get("schema", ""),
                tail[0][0] if tail else "",
            ]
        if "local live_stream = KEYS[1]" in script:
            live_stream, live_meta, stage_stream, stage_meta = args[:4]
            target, schema, empty, lst, lmt, wm, sch, tail, count, _repair = args[4:]
            actual_tail = self.xrevrange(live_stream, count=1)
            if (
                ("stream" if live_stream in self.streams else "none") != lst
                or ("hash" if live_meta in self.hashes else "none") != lmt
                or self.hashes.get(live_meta, {}).get("watermark", "") != wm
                or self.hashes.get(live_meta, {}).get("schema", "") != sch
                or (actual_tail[0][0] if actual_tail else "") != tail
            ):
                return -8
            stage_events = self.streams.get(stage_stream, [])
            if empty == "1":
                if stage_events or stage_meta in self.hashes or target != "0":
                    return -4
            elif (
                len(stage_events) != int(count)
                or not stage_events
                or stage_events[-1][0] != f"{target}-0"
                or self.hashes.get(stage_meta, {}).get("watermark") != target
                or self.hashes.get(stage_meta, {}).get("schema") != schema
            ):
                return -6
            self.streams.pop(live_stream, None)
            self.hashes.pop(live_meta, None)
            if empty != "1":
                self.streams[live_stream] = self.streams.pop(stage_stream)
                self.hashes[live_meta] = self.hashes.pop(stage_meta)
            self._touch(live_stream, live_meta, stage_stream, stage_meta)
            return 1
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

    def xrange(self, stream, min="-", max="+", count=None):
        self._check()
        entries = self.streams.get(stream, [])
        min_value = min[1:] if min.startswith("(") else min
        result = [
            item
            for item in entries
            if (min in ("-", "0-0") or _id_gt(item[0], min_value))
            and (max == "+" or not _id_gt(item[0], max))
        ]
        return result[:count] if count is not None else result

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
            self._touch(key)


class MemoryPipeline:
    def __init__(self, client):
        self.client = client
        self.commands = []
        self.in_multi = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def watch(self, *_keys):
        self.client._check()
        self.watched = {key: self.client.versions.get(key, 0) for key in _keys}

    def eval(self, *args):
        if self.in_multi:
            self.commands.append(("eval", args, {}))
            return self
        return self.client.eval(*args)

    def xrange(self, *args, **kwargs):
        return self.client.xrange(*args, **kwargs)

    def multi(self):
        self.in_multi = True

    def xadd(self, *args, **kwargs):
        self.commands.append(("xadd", args, kwargs))

    def hset(self, *args, **kwargs):
        self.commands.append(("hset", args, kwargs))

    def ping(self):
        self.commands.append(("ping", (), {}))

    def expire(self, *args, **kwargs):
        self.commands.append(("expire", args, kwargs))

    def execute(self):
        callback = self.client.before_execute if self.in_multi else None
        if callback is not None:
            self.client.before_execute = None
            callback()
        if self.in_multi and any(
            self.client.versions.get(key, 0) != version
            for key, version in getattr(self, "watched", {}).items()
        ):
            raise WatchError("watched key changed")
        if not self.commands:
            return []
        result = []
        for name, args, kwargs in self.commands:
            result.append(getattr(self.client, name)(*args, **kwargs))
        self.commands.clear()
        return result


class WatchError(ConnectionError):
    pass


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


def test_lagging_sync_reuses_capture_and_resolves_pre_watermark_claim_parent(
    tmp_path, monkeypatch, request
):
    from aigineering.agent.harness import HarnessCandidateAdapter
    from aigineering.core.candidate_publisher import CandidatePublisher
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.ids import hash_contract_v3
    from aigineering.core.signing import Ed25519Signer
    from aigineering.core.sqlite_store import SQLiteStore
    from aigineering.core.worker_coordination import authenticate_worker_command
    from aigineering.core.worker_routing import WorkerRegistration
    from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
    from aigineering.protocol.effect_builders import (
        actor_authorization_effect,
        contract_declaration_effect,
        worker_registration_effect,
    )
    from aigineering.protocol.types import Contract
    from aigineering.runtime import claim_next_package

    store = SQLiteStore(str(tmp_path / "sync-record-capture.db"))
    request.addfinalizer(store.close)
    admin_signer = Ed25519Signer()
    admin = ActorKey(
        "test:sync-record-admin",
        "admin-key",
        admin_signer.kind,
        admin_signer.signer_id,
        ("actor.authorize", "worker.register", "contract.publish"),
    )
    genesis = create_genesis_manifest(
        "sync-record-capture", (admin,), "policy:sync-record-capture"
    )
    initialize_genesis(store, genesis)
    publisher = CandidatePublisher(store, store, genesis, admin, admin_signer)
    worker_signer = Ed25519Signer()
    worker_key = ActorKey(
        "worker:sync-record",
        "worker-key-1",
        worker_signer.kind,
        worker_signer.signer_id,
        ("worker.submit",),
    )
    registration = WorkerRegistration(
        worker_key.actor_id,
        capacity=2,
        version="1",
        actor_id=worker_key.actor_id,
        key_id=worker_key.key_id,
    )
    registration_decision = publisher.publish(
        (
            actor_authorization_effect(worker_key),
            worker_registration_effect(registration),
        ),
        idempotency_key="sync-record-worker-registration",
    )
    assert registration_decision.accepted

    contract_fields = {
        "name": "sync-record-contract",
        "description": "Signed fixture for sync record reuse.",
        "inputs": (),
        "outputs": ("sync-record-output",),
        "activation": "",
        "budget": 3,
        "tool_scope": (),
        "labels": (),
        "origin": "human",
    }
    contract = Contract(id=hash_contract_v3(**contract_fields), **contract_fields)
    contract_decision = publisher.publish(
        (contract_declaration_effect(contract),),
        idempotency_key="sync-record-contract",
    )
    assert contract_decision.accepted

    adapter = HarnessCandidateAdapter(genesis.id, worker_key, worker_signer)
    claim_candidate = adapter.claim_candidate(
        request_id="sync-record-claim",
        contract_id=contract.id,
        lease_seconds=60,
    )
    claimed = claim_next_package(
        store,
        worker_id=worker_key.actor_id,
        contract_id=contract.id,
        claim_candidate=claim_candidate,
    )
    assert claimed is not None
    grant_records = store.scan_runtime_records(record_type="claim.granted")
    assert len(grant_records) == 1

    redis = MemoryRedis()
    service = _service(store, redis)
    seeded = service.sync()
    watermark_before_renewal = seeded["watermark"]
    assert grant_records[0][0] <= watermark_before_renewal

    renewal_candidate = adapter.renewal_candidate(
        claimed.package, request_id="sync-record-renewal", lease_seconds=90
    )
    command = authenticate_worker_command(
        renewal_candidate, "worker.claim.renew", store
    )
    renewed = store.renew_claim(
        claimed.package.claim_id,
        claimed.package.claim_epoch,
        worker_key.actor_id,
        lease_seconds=90,
        runtime_records=command.runtime_records,
        claim_candidate=renewal_candidate,
    )
    assert renewed is not None
    renewed_records = store.scan_runtime_records(record_type="claim.renewed")
    assert len(renewed_records) == 1
    assert renewed_records[0][0] > watermark_before_renewal

    original_scan = store.scan_runtime_records
    scan_calls = []

    def scan_spy(*, after_revision=0, through_revision=None, record_type=None):
        scan_calls.append((after_revision, through_revision, record_type))
        return original_scan(
            after_revision=after_revision,
            through_revision=through_revision,
            record_type=record_type,
        )

    monkeypatch.setattr(store, "scan_runtime_records", scan_spy)
    final_revision = store.get_runtime_revision()
    result = service.sync()

    assert result["synced"] is True
    assert result["watermark"] == final_revision
    assert scan_calls == [(0, final_revision, None)]
    events = service.events(after_revision=watermark_before_renewal)
    renewal_event = next(
        event for event in events if event["record_type"] == "claim.renewed"
    )
    # claim.renewed has no contract_id payload; this reference comes through
    # its pre-watermark claim.granted parent in the full-history record map.
    assert renewal_event["contract_ids"] == (contract.id,)
    assert renewal_event["worker_ids"] == (worker_key.actor_id,)

    scan_calls.clear()
    noop = service.sync()
    assert noop["synced"] is True
    assert noop["watermark"] == final_revision
    assert scan_calls == [(0, final_revision, None)]


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


def test_ordinary_sync_refuses_partial_projection_and_explicit_rebuild_repairs(
    monkeypatch,
):
    from types import SimpleNamespace

    import aigineering.core.domain as domain_module

    monkeypatch.setattr(
        domain_module,
        "load_genesis",
        lambda _store: SimpleNamespace(id="domain:one"),
    )
    store = MemoryStore([(1, _record(1))])
    redis = MemoryRedis()
    service = _service(store, redis)
    service.sync()
    redis.streams.clear()

    with pytest.raises(RedisCollaborationUnavailable) as exc:
        service.sync()
    assert exc.value.reason == "projection_inconsistent"
    assert redis.hashes[service.metadata_key]["watermark"] == "1"
    assert service.rebuild(fresh_store_factory=lambda: store)["watermark"] == 1
    assert [event["revision"] for event in service.events()] == [1]


def test_sync_rejects_field_tampering_without_mutating_live_pair():
    store = MemoryStore([(1, _record(1))])
    redis = MemoryRedis()
    service = _service(store, redis)
    service.sync()
    stream_before = [
        (event_id, dict(fields))
        for event_id, fields in redis.streams[service.stream_key]
    ]
    metadata_before = dict(redis.hashes[service.metadata_key])
    redis.streams[service.stream_key][0][1]["record_id"] = "record:forged"

    tampered_stream = list(redis.streams[service.stream_key])
    with pytest.raises(RedisCollaborationUnavailable) as exc:
        service.sync()

    assert exc.value.reason == "source_diverged"
    assert redis.streams[service.stream_key] == tampered_stream
    assert redis.hashes[service.metadata_key] == metadata_before
    assert stream_before != tampered_stream


def test_ahead_snapshot_status_is_noncurrent_but_events_return_known_prefix():
    store = MemoryStore([(1, _record(1))])
    redis = MemoryRedis()
    service = _service(store, redis)
    service.sync()
    redis.xadd(
        service.stream_key,
        {
            "revision": "2",
            "record_id": "record:2",
            "record_type": "asset.proposed",
            "contract_ids": "[]",
            "worker_ids": "[]",
        },
        id="2-0",
    )
    redis.hset(
        service.metadata_key,
        mapping={"watermark": "2", "schema": "v2"},
    )

    status = service.status()
    assert status["current"] is False
    assert status["reason"] == "redis_ahead"
    assert [event["revision"] for event in service.events()] == [1]
    with pytest.raises(RedisCollaborationUnavailable) as exc:
        service.sync()
    assert exc.value.reason == "redis_ahead"


def test_post_snapshot_malformed_suffix_does_not_invalidate_frozen_events():
    store = MemoryStore([(1, _record(1))])
    redis = MemoryRedis()
    service = _service(store, redis)
    service.sync()
    redis.xadd(
        service.stream_key,
        {
            "revision": "not-a-revision",
            "record_id": "future-record",
            "record_type": "asset.proposed",
            "contract_ids": "[]",
            "worker_ids": "[]",
        },
        id="2-0",
    )
    redis.hset(
        service.metadata_key,
        mapping={"watermark": "2", "schema": "v2"},
    )

    assert [event["revision"] for event in service.events()] == [1]
    status = service.status()
    assert status["current"] is False
    assert status["reason"] == "projection_inconsistent"


def test_post_commit_pending_transports_conflict_reason(monkeypatch):
    import aigineering.runtime_services as runtime_services

    class Service:
        def sync(self):
            raise RedisCollaborationUnavailable(
                "prefix conflict", reason="source_diverged"
            )

    monkeypatch.setattr(
        runtime_services, "collaboration_service", lambda _store: Service()
    )

    with pytest.raises(runtime_services.RedisSyncPending) as exc:
        runtime_services.sync_runtime(object(), accepted=True)

    assert exc.value.accepted is True
    assert exc.value.reason == "source_diverged"


def test_explicit_rebuild_uses_fresh_factory_snapshot_after_capturing_original(
    monkeypatch,
):
    from types import SimpleNamespace

    import aigineering.core.domain as domain_module

    monkeypatch.setattr(
        domain_module,
        "load_genesis",
        lambda _store: SimpleNamespace(id="domain:one"),
    )

    class OriginalSnapshot(MemoryStore):
        closed_for_reads = False

        def get_runtime_revision(self):
            assert not self.closed_for_reads
            return super().get_runtime_revision()

        def scan_runtime_records(self, **kwargs):
            assert not self.closed_for_reads
            return super().scan_runtime_records(**kwargs)

    source = OriginalSnapshot([(1, _record(1))])
    fresh = MemoryStore([(1, _record(1)), (2, _record(2))])
    redis = MemoryRedis()
    service = _service(source, redis)

    def fresh_factory():
        source.closed_for_reads = True
        return fresh

    result = service.rebuild(fresh_store_factory=fresh_factory)

    assert result["watermark"] == 2
    assert _service(fresh, redis).status()["authoritative_revision"] == 2


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


def test_watch_conflict_retries_after_a_concurrent_valid_prefix_append():
    store = MemoryStore([(1, _record(1)), (2, _record(2))])
    redis = MemoryRedis()
    service = _service(store, redis)
    fields = service._event_fields(
        1, store.records[0][1], {record.id: record for _, record in store.records}
    )
    conflicts = []

    def concurrent_append():
        conflicts.append(True)
        redis.xadd(service.stream_key, fields, id="1-0")
        redis.hset(
            service.metadata_key,
            mapping={"watermark": "1", "schema": "v2"},
        )

    redis.before_execute = concurrent_append

    result = service.sync()

    assert conflicts == [True]
    assert result["watermark"] == 2
    assert [event_id for event_id, _fields in redis.streams[service.stream_key]] == [
        "1-0",
        "2-0",
    ]


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

    staged_service = RedisCollaborationService(
        MemoryStore([(revision, _record(revision)) for revision in range(1, 301)]),
        client,
        domain_id=f"integration-stage-{run_id}",
        redis_errors=(redis_module.RedisError,),
    )
    assert staged_service.sync()["watermark"] == 300
    assert staged_service.status()["current"] is True
    assert len(staged_service.events(count=300)) == 300


def _native_redis_client():
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL to run Redis integration")
    redis_module = pytest.importorskip("redis")
    return redis_module.Redis.from_url(
        redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2
    ), redis_module


def _native_domain(monkeypatch):
    import aigineering.core.domain as domain_module

    monkeypatch.setattr(
        domain_module,
        "load_genesis",
        lambda _store: SimpleNamespace(id="domain:native-r2"),
    )


def test_native_initial_repair_handles_ahead_wrong_types_and_empty_state(monkeypatch):
    client, redis_module = _native_redis_client()
    _native_domain(monkeypatch)
    run_id = uuid.uuid4().hex
    source = MemoryStore([(1, _record(1))])
    service = RedisCollaborationService(
        source,
        client,
        domain_id=f"domain:native-r2:{run_id}",
        redis_errors=(redis_module.RedisError,),
    )
    # load_genesis is a fixed test stub; service's domain must match it.
    service._domain_id = "domain:native-r2"
    try:
        service.sync()
        client.xadd(
            service.stream_key,
            {
                "revision": "2",
                "record_id": "record:2",
                "record_type": "asset.proposed",
                "contract_ids": "[]",
                "worker_ids": "[]",
            },
            id="2-0",
        )
        client.hset(service.metadata_key, mapping={"watermark": "2", "schema": "v2"})
        ahead_stream = client.xrange(service.stream_key)
        ahead_meta = client.hgetall(service.metadata_key)
        with pytest.raises(RedisCollaborationUnavailable) as ahead:
            service.sync()
        assert ahead.value.reason == "redis_ahead"
        assert client.xrange(service.stream_key) == ahead_stream
        assert client.hgetall(service.metadata_key) == ahead_meta
        assert service.rebuild(fresh_store_factory=lambda: source)["watermark"] == 1
        assert service.status()["current"] is True

        # Explicit first repair may replace wrong-type destinations under F.
        client.set(service.stream_key, "wrong-stream-type")
        client.set(service.metadata_key, "wrong-metadata-type")
        wrong_stream = client.get(service.stream_key)
        wrong_meta = client.get(service.metadata_key)
        with pytest.raises(RedisCollaborationUnavailable) as wrong_type:
            service.sync()
        assert wrong_type.value.reason == "projection_inconsistent"
        assert client.get(service.stream_key) == wrong_stream
        assert client.get(service.metadata_key) == wrong_meta
        assert service.rebuild(fresh_store_factory=lambda: source)["watermark"] == 1
        assert service.status()["current"] is True
        assert client.type(service.stream_key) == "stream"
        assert client.type(service.metadata_key) == "hash"

        # Positive pairs with missing schema fail closed and remain byte-for-byte unchanged.
        client.hdel(service.metadata_key, "schema")
        before_stream = client.xrange(service.stream_key)
        before_meta = client.hgetall(service.metadata_key)
        with pytest.raises(RedisCollaborationUnavailable) as missing_schema:
            service.sync()
        assert missing_schema.value.reason == "schema_mismatch"
        assert client.xrange(service.stream_key) == before_stream
        assert client.hgetall(service.metadata_key) == before_meta

        empty = RedisCollaborationService(
            MemoryStore(),
            client,
            domain_id="domain:native-r2",
            redis_errors=(redis_module.RedisError,),
        )
        assert (
            empty.rebuild(fresh_store_factory=lambda: MemoryStore())["watermark"] == 0
        )
        assert client.exists(empty.stream_key, empty.metadata_key) == 0
        assert empty.status()["current"] is True
        assert empty.sync()["watermark"] == 0
        assert empty.events() == []
    finally:
        client.delete(service.stream_key, service.metadata_key)


def test_native_same_tail_earlier_identity_tamper_is_rejected_without_mutation():
    client, redis_module = _native_redis_client()
    run_id = uuid.uuid4().hex
    store = MemoryStore([(1, _record(1)), (2, _record(2))])
    service = RedisCollaborationService(
        store,
        client,
        domain_id=f"same-tail-tamper-{run_id}",
        redis_errors=(redis_module.RedisError,),
    )
    try:
        service.sync()
        # Replace only the earlier ID with valid-shaped but source-diverged fields;
        # the final stream ID and metadata watermark remain unchanged.
        client.delete(service.stream_key)
        client.xadd(
            service.stream_key,
            {
                "revision": "1",
                "record_id": "record:forged",
                "record_type": "contract.declared",
                "contract_ids": '["task:v1:1"]',
                "worker_ids": "[]",
            },
            id="1-0",
        )
        client.xadd(
            service.stream_key,
            {
                "revision": "2",
                "record_id": "record:2",
                "record_type": "contract.declared",
                "contract_ids": '["task:v1:2"]',
                "worker_ids": "[]",
            },
            id="2-0",
        )
        before_stream = client.xrange(service.stream_key)
        before_meta = client.hgetall(service.metadata_key)
        assert before_stream[-1][0] == "2-0"
        with pytest.raises(RedisCollaborationUnavailable) as exc:
            service.sync()
        assert exc.value.reason == "source_diverged"
        assert client.xrange(service.stream_key) == before_stream
        assert client.hgetall(service.metadata_key) == before_meta
    finally:
        client.delete(service.stream_key, service.metadata_key)


def test_native_stage_loss_is_rejected_without_replacing_old_pair(monkeypatch):
    client, redis_module = _native_redis_client()
    run_id = uuid.uuid4().hex
    source = MemoryStore([(1, _record(1))])
    service = RedisCollaborationService(
        source,
        client,
        domain_id=f"stage-loss-{run_id}",
        redis_errors=(redis_module.RedisError,),
    )
    service.sync()
    old_stream = client.xrange(service.stream_key)
    old_meta = client.hgetall(service.metadata_key)
    source.records = [(revision, _record(revision)) for revision in range(1, 301)]
    original_pipeline = client.pipeline
    deleted = False

    class DeleteAfterFirstStagePage:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *exc):
            return self.wrapped.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def execute(self):
            nonlocal deleted
            queued = list(getattr(self.wrapped, "command_stack", ()))
            result = self.wrapped.execute()
            if not deleted and any(
                command[0][0].upper() == "XADD"
                and command[0][1].startswith(service._root + ":stage:")
                for command in queued
            ):
                stage_key = next(
                    command[0][1]
                    for command in queued
                    if command[0][0].upper() == "XADD"
                    and command[0][1].startswith(service._root + ":stage:")
                )
                client.delete(stage_key)
                deleted = True
            return result

    monkeypatch.setattr(
        client,
        "pipeline",
        lambda *args, **kwargs: DeleteAfterFirstStagePage(
            original_pipeline(*args, **kwargs)
        ),
    )
    try:
        with pytest.raises(RedisCollaborationUnavailable) as exc:
            service.sync()
        assert deleted is True
        assert exc.value.reason == "sync_contention"
        assert client.xrange(service.stream_key) == old_stream
        assert client.hgetall(service.metadata_key) == old_meta
        assert client.keys(service._root + ":stage:*") == []
    finally:
        client.delete(service.stream_key, service.metadata_key)


def test_native_stage_pages_expire_atomically_and_last_page_is_cleaned(monkeypatch):
    client, redis_module = _native_redis_client()
    import aigineering.redis_collaboration as collaboration_module

    monkeypatch.setattr(collaboration_module, "_MAX_APPEND_BYTES", 1)
    run_id = uuid.uuid4().hex
    source = MemoryStore([(1, _record(1)), (2, _record(2))])
    service = RedisCollaborationService(
        source,
        client,
        domain_id=f"stage-ttl-{run_id}",
        redis_errors=(redis_module.RedisError,),
    )
    original_write_page = service._write_stage_page
    observed_ttls = []

    def check_page_ttl(stage_stream, page):
        original_write_page(stage_stream, page)
        observed_ttls.append(client.ttl(stage_stream))

    monkeypatch.setattr(service, "_write_stage_page", check_page_ttl)
    try:
        assert service.sync()["watermark"] == 2
        assert len(observed_ttls) == 2
        assert all(ttl > 0 for ttl in observed_ttls)
        assert client.ttl(service.stream_key) == -1
        assert client.keys(service._root + ":stage:*") == []
        assert service.status()["current"] is True
    finally:
        client.delete(service.stream_key, service.metadata_key)


def test_native_explicit_rebuild_retries_watch_mutation_and_refuses_new_ahead_prefix(
    monkeypatch,
):
    client, redis_module = _native_redis_client()
    _native_domain(monkeypatch)
    source_s = MemoryStore([(1, _record(1))])
    source_p = MemoryStore([(1, _record(1)), (2, _record(2))])
    service = RedisCollaborationService(
        source_s,
        client,
        domain_id="domain:native-r2",
        redis_errors=(redis_module.RedisError,),
    )
    original_pipeline = client.pipeline
    mutated = False

    class MutateBeforeSwapExec:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *exc):
            return self.wrapped.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def execute(self):
            nonlocal mutated
            queued = list(getattr(self.wrapped, "command_stack", ()))
            is_swap = any(
                command[0][0].upper() == "EVAL"
                and "local live_stream = KEYS[1]" in command[0][1]
                for command in queued
            )
            if is_swap and not mutated:
                mutated = True
                client.xadd(
                    service.stream_key,
                    {
                        "revision": "1",
                        "record_id": "record:1",
                        "record_type": "contract.declared",
                        "contract_ids": '["task:v1:1"]',
                        "worker_ids": "[]",
                    },
                    id="1-0",
                )
                client.xadd(
                    service.stream_key,
                    {
                        "revision": "2",
                        "record_id": "record:2",
                        "record_type": "contract.declared",
                        "contract_ids": '["task:v1:2"]',
                        "worker_ids": "[]",
                    },
                    id="2-0",
                )
                client.hset(
                    service.metadata_key,
                    mapping={"watermark": "2", "schema": "v2"},
                )
            return self.wrapped.execute()

    monkeypatch.setattr(
        client,
        "pipeline",
        lambda *args, **kwargs: MutateBeforeSwapExec(
            original_pipeline(*args, **kwargs)
        ),
    )
    try:
        assert service.rebuild(fresh_store_factory=lambda: source_p)["watermark"] == 2
        assert mutated is True
        assert (
            RedisCollaborationService(
                source_p,
                client,
                domain_id="domain:native-r2",
                redis_errors=(redis_module.RedisError,),
            ).status()["current"]
            is True
        )
    finally:
        client.delete(service.stream_key, service.metadata_key)

    monkeypatch.undo()
    _native_domain(monkeypatch)

    # A mutation inside the fresh-P factory occurs after F was read and must
    # invalidate that watched attempt, then be proved against a new P.
    service = RedisCollaborationService(
        source_s,
        client,
        domain_id="domain:native-r2",
        redis_errors=(redis_module.RedisError,),
    )
    factory_calls = 0

    def mutate_before_fresh_p():
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 1:
            for revision in (1, 2):
                client.xadd(
                    service.stream_key,
                    {
                        "revision": str(revision),
                        "record_id": f"record:{revision}",
                        "record_type": "contract.declared",
                        "contract_ids": f'["task:v1:{revision}"]',
                        "worker_ids": "[]",
                    },
                    id=f"{revision}-0",
                )
            client.hset(
                service.metadata_key,
                mapping={"watermark": "2", "schema": "v2"},
            )
        return source_p

    try:
        assert (
            service.rebuild(fresh_store_factory=mutate_before_fresh_p)["watermark"] == 2
        )
        assert factory_calls == 2
        assert (
            RedisCollaborationService(
                source_p,
                client,
                domain_id="domain:native-r2",
                redis_errors=(redis_module.RedisError,),
            ).status()["current"]
            is True
        )
    finally:
        client.delete(service.stream_key, service.metadata_key)

    # A retry may not overwrite a new valid-but-ahead live prefix with stale P.
    service = RedisCollaborationService(
        MemoryStore([(1, _record(1))]),
        client,
        domain_id="domain:native-r2",
        redis_errors=(redis_module.RedisError,),
    )
    service.sync()
    client.xadd(
        service.stream_key,
        {
            "revision": "2",
            "record_id": "record:2",
            "record_type": "asset.proposed",
            "contract_ids": "[]",
            "worker_ids": "[]",
        },
        id="2-0",
    )
    client.hset(service.metadata_key, mapping={"watermark": "2", "schema": "v2"})
    original_pipeline = client.pipeline
    mutated = False

    class AdvanceAheadBeforeExec:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *exc):
            return self.wrapped.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def execute(self):
            nonlocal mutated
            queued = list(getattr(self.wrapped, "command_stack", ()))
            is_swap = any(command[0][0].upper() == "EVAL" for command in queued)
            if is_swap and not mutated:
                mutated = True
                client.xadd(
                    service.stream_key,
                    {
                        "revision": "3",
                        "record_id": "record:3",
                        "record_type": "asset.proposed",
                        "contract_ids": "[]",
                        "worker_ids": "[]",
                    },
                    id="3-0",
                )
                client.hset(
                    service.metadata_key, mapping={"watermark": "3", "schema": "v2"}
                )
            return self.wrapped.execute()

    monkeypatch.setattr(
        client,
        "pipeline",
        lambda *args, **kwargs: AdvanceAheadBeforeExec(
            original_pipeline(*args, **kwargs)
        ),
    )
    try:
        with pytest.raises(RedisCollaborationUnavailable) as retry_ahead:
            service.rebuild(fresh_store_factory=lambda: MemoryStore([(1, _record(1))]))
        assert retry_ahead.value.reason == "redis_ahead"
        assert [entry[0] for entry in client.xrange(service.stream_key)] == [
            "1-0",
            "2-0",
            "3-0",
        ]
        assert client.hget(service.metadata_key, "watermark") == "3"
    finally:
        client.delete(service.stream_key, service.metadata_key)


def test_single_canonical_capture_preserves_revision_gaps_across_proof_paths(
    monkeypatch,
):
    import aigineering.core.domain as domain_module

    monkeypatch.setattr(
        domain_module,
        "load_genesis",
        lambda _store: SimpleNamespace(id="domain:one"),
    )
    store = MemoryStore([(1, _record(1)), (3, _record(3))])
    redis = MemoryRedis()
    service = _service(store, redis)
    calls = []
    original = service._event_fields

    def count_derivation(revision, record, records_by_id):
        calls.append(revision)
        return original(revision, record, records_by_id)

    monkeypatch.setattr(service, "_event_fields", count_derivation)

    assert service.sync()["watermark"] == 3
    assert calls == [1, 3]
    assert [event[0] for event in redis.streams[service.stream_key]] == ["1-0", "3-0"]

    calls.clear()
    assert service.status()["current"] is True
    assert calls == [1, 3]

    calls.clear()
    assert [item["revision"] for item in service.events()] == [1, 3]
    assert calls == [1, 3]

    calls.clear()
    assert service.rebuild(fresh_store_factory=lambda: store)["watermark"] == 3
    assert calls == [1, 3, 1, 3]


def test_capture_builds_one_reference_context_and_preserves_event_bytes(monkeypatch):
    import aigineering.redis_collaboration as redis_module
    from aigineering.core.trace import create_entry, trace_record_from_entry
    from aigineering.protocol.runtime_record import create_runtime_record

    declaration = create_runtime_record(
        "contract.declared",
        {"contract": {"id": "task:v1:root", "parent_id": None}},
    )
    committed = create_runtime_record(
        "candidate.committed",
        {"candidate_id": "candidate-1", "committed_record_ids": [declaration.id]},
    )
    trace = trace_record_from_entry(
        create_entry(
            "commitment",
            "candidate_committed",
            accepted_fragments=[json.dumps({"candidate_id": "candidate-1"})],
        )
    )
    store = MemoryStore([(1, declaration), (3, committed), (4, trace)])
    service = _service(store, MemoryRedis())
    calls = []
    original_builder = redis_module.build_runtime_record_reference_context

    def observe_builder(records_by_id):
        context = original_builder(records_by_id)
        calls.append((records_by_id, context))
        return context

    monkeypatch.setattr(
        redis_module, "build_runtime_record_reference_context", observe_builder
    )

    revision, entries = service._capture_events(store)
    _same_revision, rows, records_by_id = service._records(store)
    legacy_entries = [
        (row_revision, service._event_fields(row_revision, record, records_by_id))
        for row_revision, record in rows
    ]

    assert revision == 4
    assert len(calls) == 1
    assert calls[0][0] is calls[0][1].records_by_id
    assert entries == legacy_entries
    assert all(
        list(fields)
        == ["revision", "record_id", "record_type", "contract_ids", "worker_ids"]
        for _row_revision, fields in entries
    )
    assert json.loads(entries[-1][1]["contract_ids"]) == ["task:v1:root"]
