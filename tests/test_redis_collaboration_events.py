from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from aigineering.application import collaboration_service
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import ActorKey, create_genesis_manifest
from aigineering.protocol.types import Asset


def _publish_signed_asset(store, genesis, actor_key, signer, *, name, key):
    from aigineering.protocol.effect_builders import asset_proposal_effect

    decision = CandidatePublisher(store, store, genesis, actor_key, signer).publish(
        (asset_proposal_effect(Asset(id="", name=name, content=name)),),
        idempotency_key=key,
    )
    assert decision.accepted


def test_events_include_commits_arriving_during_xread_but_cap_frozen_snapshot(
    tmp_path, monkeypatch
):
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL for native Redis blocking-read coverage")
    redis_module = pytest.importorskip("redis")
    database = tmp_path / "store.db"
    run_id = uuid4().hex

    source = SQLiteStore(str(database))
    signer = Ed25519Signer()
    actor_key = ActorKey(
        "test:redis-events",
        f"test-redis-events-{run_id}",
        signer.kind,
        signer.signer_id,
        (
            "actor.authorize",
            "asset.publish",
            "asset.publish.protected",
            "asset.relate",
            "contract.publish",
            "contract.publish.protected",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest(
        f"test-redis-events-{run_id}", (actor_key,), "policy:test-redis-events"
    )
    initialize_genesis(source, genesis)
    _publish_signed_asset(
        source,
        genesis,
        actor_key,
        signer,
        name="before-block",
        key=f"before-block-{run_id}",
    )
    source.close()

    live_store = SQLiteStore(str(database))
    frozen_store = SQLiteStore.snapshot_from_path(database)
    service = collaboration_service(live_store, redis_url=redis_url)
    frozen_service = collaboration_service(frozen_store, redis_url=redis_url)
    client = redis_module.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=8,
    )
    entered_xread = Event()
    original_xread = service._client.xread

    def signaled_xread(streams, *, count=100, block=None):
        entered_xread.set()
        return original_xread(streams, count=count, block=block)

    monkeypatch.setattr(service._client, "xread", signaled_xread)
    try:
        old_revision = live_store.get_runtime_revision()
        assert service.sync()["watermark"] == old_revision

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_read = pool.submit(
                service.events,
                after_revision=old_revision,
                count=20,
                block_ms=5000,
            )
            assert entered_xread.wait(timeout=3), (
                "events() did not enter blocking XREAD"
            )

            def commit_and_relay():
                writer = SQLiteStore(str(database))
                try:
                    _publish_signed_asset(
                        writer,
                        genesis,
                        actor_key,
                        signer,
                        name=f"during-block-{run_id}",
                        key=f"during-block-{run_id}",
                    )
                    revision = writer.get_runtime_revision()
                    collaboration_service(writer, redis_url=redis_url).sync()
                    return revision
                finally:
                    writer.close()

            new_revision = pool.submit(commit_and_relay).result(timeout=5)
            events = pending_read.result(timeout=7)

        assert events
        assert events[0]["revision"] == old_revision + 1
        assert events[0]["revision"] <= new_revision
        assert events[0]["stream_id"] == f"{events[0]['revision']}-0"
        assert set(events[0]) == {
            "stream_id",
            "revision",
            "record_id",
            "record_type",
            "contract_ids",
            "worker_ids",
        }

        # The pre-commit snapshot must not reveal a Redis event beyond its source.
        assert frozen_store.get_runtime_revision() == old_revision
        assert frozen_service.events(after_revision=old_revision, count=20) == []
        assert frozen_service.wait(after_revision=old_revision, timeout=0) == []
    finally:
        client.delete(service.stream_key, service.metadata_key)
        live_store.close()
        frozen_store.close()
        client.close()
