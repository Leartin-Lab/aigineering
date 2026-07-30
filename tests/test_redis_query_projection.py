"""Redis query projections remain disposable views over committed facts."""

from __future__ import annotations

import os
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from aigineering.adapters.redis_query import RedisQueryProjection
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.query_projection import build_query_snapshot
from aigineering.core.query_projection import QUERY_PROJECTION_SCHEMA
from aigineering.core.sqlite_store import SQLiteStore
from conftest import candidate_runtime
from aigineering.protocol.asset_graph import (
    create_content_object,
    create_definition_content_assertion,
    create_signed_definition,
)
from aigineering.protocol.effect_builders import (
    content_publication_effect,
    definition_content_assertion_effect,
    definition_publication_effect,
)


class FakeRedisError(Exception):
    pass


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise FakeRedisError("unavailable")

    def get(self, key: str):
        self._check()
        return self.strings.get(key)

    def set(self, key: str, value: str):
        self._check()
        self.strings[key] = str(value)
        return True

    def hset(self, key: str, *, mapping: dict[str, str]):
        self._check()
        self.hashes.setdefault(key, {}).update(
            {str(name): str(value) for name, value in mapping.items()}
        )
        return len(mapping)

    def hgetall(self, key: str):
        self._check()
        return dict(self.hashes.get(key, {}))

    def sadd(self, key: str, *values: str):
        self._check()
        target = self.sets.setdefault(key, set())
        before = len(target)
        target.update(str(value) for value in values)
        return len(target) - before

    def smembers(self, key: str):
        self._check()
        return set(self.sets.get(key, set()))

    def mget(self, keys: list[str]):
        self._check()
        return [self.strings.get(key) for key in keys]

    def eval(
        self,
        _script: str,
        _numkeys: int,
        key: str,
        assets_key: str,
        contracts_key: str,
        contents_key: str,
        definitions_key: str,
        assertions_key: str,
        target: str,
    ):
        self._check()
        current = int(self.hashes.get(key, {}).get("revision", "-1"))
        if current <= int(target):
            meta = self.hashes.setdefault(key, {})
            meta["revision"] = str(target)
            meta["asset_count"] = str(len(self.sets.get(assets_key, set())))
            meta["contract_count"] = str(len(self.sets.get(contracts_key, set())))
            meta["content_count"] = str(len(self.sets.get(contents_key, set())))
            meta["definition_count"] = str(len(self.sets.get(definitions_key, set())))
            meta["assertion_count"] = str(len(self.sets.get(assertions_key, set())))
            return 1
        return 0

    def pipeline(self, *, transaction: bool):
        self._check()
        assert transaction is True
        return FakePipeline(self)

    def flushall(self):
        self.strings.clear()
        self.hashes.clear()
        self.sets.clear()


class FakePipeline:
    def __init__(self, client: FakeRedis) -> None:
        self._client = client
        self._commands: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def queue(*args, **kwargs):
            self._commands.append((name, args, kwargs))
            return self

        return queue

    def execute(self):
        self._client._check()
        snapshot = (
            deepcopy(self._client.strings),
            deepcopy(self._client.hashes),
            deepcopy(self._client.sets),
        )
        try:
            return [
                getattr(self._client, name)(*args, **kwargs)
                for name, args, kwargs in self._commands
            ]
        except FakeRedisError:
            (
                self._client.strings,
                self._client.hashes,
                self._client.sets,
            ) = snapshot
            raise


def _facts(store):
    runtime = candidate_runtime(store)
    asset = runtime.accept_asset(
        build_control_plane_asset(name="source", content="evidence")
    )
    contract = runtime.accept_contract(
        build_control_plane_contract(
            name="review",
            description="Review the evidence.",
            inputs=("source",),
            outputs=("report",),
            activation="source",
        )
    )
    return runtime, asset, contract


def _projection(store, runtime, client, degraded=None):
    return RedisQueryProjection(
        store,
        client,
        domain_id=runtime.genesis.id,
        redis_errors=(FakeRedisError,),
        on_degraded=(degraded or (lambda _exc: None)),
    )


def test_snapshot_is_deterministic_and_binds_domain_and_revision(temp_sqlite_store):
    runtime, _, _ = _facts(temp_sqlite_store)

    first = build_query_snapshot(temp_sqlite_store, domain_id=runtime.genesis.id)
    second = build_query_snapshot(temp_sqlite_store, domain_id=runtime.genesis.id)

    assert first == second
    assert first.revision == temp_sqlite_store.get_runtime_revision()
    assert first.domain_id == runtime.genesis.id
    assert first.digest


def test_redis_projection_rebuilds_and_serves_entity_indexes(temp_sqlite_store):
    runtime, asset, contract = _facts(temp_sqlite_store)
    client = FakeRedis()
    projection = _projection(temp_sqlite_store, runtime, client)

    snapshot = projection.rebuild()

    assert client.get(projection.active_key) == snapshot.digest
    assert projection.get_asset(asset.id) == asset
    assert projection.get_assets_by_name("source") == [asset]
    assert projection.get_assets_by_definition(asset.definition_hash) == [asset]
    assert projection.get_all_assets() == [asset]
    assert projection.get_contract(contract.id) == contract
    assert projection.get_all_contracts() == [contract]


def test_current_read_detects_stale_revision_and_rebuilds(temp_sqlite_store):
    runtime, first, _ = _facts(temp_sqlite_store)
    client = FakeRedis()
    projection = _projection(temp_sqlite_store, runtime, client)
    old = projection.rebuild()
    second = runtime.accept_asset(
        build_control_plane_asset(name="second", content="new evidence")
    )

    assets = projection.get_all_assets()

    assert {asset.id for asset in assets} == {first.id, second.id}
    assert client.get(projection.active_key) == old.digest
    generation = projection._generation_root(old.digest)
    assert int(client.hgetall(f"{generation}:meta")["revision"]) == (
        temp_sqlite_store.get_runtime_revision()
    )


def test_flushed_or_partial_cache_is_rebuilt_before_read(temp_sqlite_store):
    runtime, asset, _ = _facts(temp_sqlite_store)
    client = FakeRedis()
    projection = _projection(temp_sqlite_store, runtime, client)
    projection.rebuild()
    client.flushall()
    client.set(projection.active_key, "partial")
    partial_root = projection._generation_root("partial")
    client.hset(
        f"{partial_root}:meta",
        mapping={
            "domain_id": runtime.genesis.id,
            "revision": str(temp_sqlite_store.get_runtime_revision()),
            "schema": QUERY_PROJECTION_SCHEMA,
            "status": "building",
        },
    )

    assert projection.get_asset(asset.id) == asset
    active = client.get(projection.active_key)
    assert active and active != "partial"


def test_missing_indexed_payload_triggers_complete_rebuild(temp_sqlite_store):
    runtime, asset, _ = _facts(temp_sqlite_store)
    client = FakeRedis()
    projection = _projection(temp_sqlite_store, runtime, client)
    snapshot = projection.rebuild()
    generation = projection._generation_root(snapshot.digest)
    client.strings.pop(f"{generation}:asset:{asset.id}")

    assert projection.get_all_assets() == [asset]
    assert client.get(f"{generation}:asset:{asset.id}") is not None


def test_json_views_are_memoized_only_for_one_authoritative_revision(
    temp_sqlite_store,
):
    runtime, _, contract = _facts(temp_sqlite_store)
    client = FakeRedis()
    projection = _projection(temp_sqlite_store, runtime, client)
    calls = 0

    def build():
        nonlocal calls
        calls += 1
        return {"contract_id": contract.id, "call": calls}

    assert projection.memoize_json("task.status", contract.id, build)["call"] == 1
    assert projection.memoize_json("task.status", contract.id, build)["call"] == 1
    runtime.accept_asset(build_control_plane_asset(name="second", content="new"))
    assert projection.memoize_json("task.status", contract.id, build)["call"] == 2


def test_redis_graph_rebuild_and_incremental_catchup(temp_sqlite_store):
    runtime, _, _ = _facts(temp_sqlite_store)
    client = FakeRedis()
    projection = _projection(temp_sqlite_store, runtime, client)
    projection.rebuild()

    content = create_content_object("graph")
    definition = create_signed_definition(
        domain_id=runtime.genesis.id,
        name="graph-report",
        description="Graph report",
        content_type="text",
        source_kind="contract-output",
        source_uri="task:v3:graph",
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        signer=runtime.signer,
    )
    runtime._publish(content_publication_effect(content))
    runtime._publish(definition_publication_effect(definition))
    assertion = create_definition_content_assertion(
        domain_id=runtime.genesis.id,
        definition_id=definition.id,
        content_id=content.id,
        relation_type="satisfies",
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        signer=runtime.signer,
    )
    runtime._publish(definition_content_assertion_effect(assertion))

    assert content.id in {value["id"] for value in projection.get_content_objects()}
    assert definition.id in {
        value["id"] for value in projection.get_asset_definitions()
    }
    assert projection.get_definition_content_assertions(
        definition_id=definition.id
    ) == temp_sqlite_store.get_definition_content_assertions(
        definition_id=definition.id
    )

    client.flushall()
    rebuilt = projection.rebuild()
    assert (
        rebuilt.digest
        == build_query_snapshot(temp_sqlite_store, domain_id=runtime.genesis.id).digest
    )


def test_store_domains_use_disjoint_redis_namespaces():
    first_store = SQLiteStore(":memory:")
    second_store = SQLiteStore(":memory:")
    first_runtime = candidate_runtime(first_store)
    second_runtime = candidate_runtime(second_store)
    first = first_runtime.accept_asset(
        build_control_plane_asset(name="source", content="first")
    )
    second = second_runtime.accept_asset(
        build_control_plane_asset(name="source", content="second")
    )
    client = FakeRedis()
    first_projection = _projection(first_store, first_runtime, client)
    second_projection = _projection(second_store, second_runtime, client)

    first_projection.rebuild()
    second_projection.rebuild()

    assert first_projection.active_key != second_projection.active_key
    assert first_projection.get_assets_by_name("source") == [first]
    assert second_projection.get_assets_by_name("source") == [second]
    first_store.close()
    second_store.close()


def test_redis_outage_falls_back_without_changing_results(temp_sqlite_store):
    runtime, asset, contract = _facts(temp_sqlite_store)
    client = FakeRedis()
    failures: list[str] = []
    projection = _projection(
        temp_sqlite_store,
        runtime,
        client,
        degraded=lambda exc: failures.append(str(exc)),
    )
    client.fail = True

    assert projection.get_asset(asset.id) == asset
    assert projection.get_all_contracts() == [contract]
    assert failures == ["unavailable", "unavailable"]
    assert projection.status() == {
        "authoritative_revision": temp_sqlite_store.get_runtime_revision(),
        "available": False,
        "backend": "redis",
        "configured": True,
        "current": False,
        "reason": "unavailable",
    }


def test_real_redis_flush_rebuild_and_stale_catchup(temp_sqlite_store):
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL to run Redis integration")
    redis = pytest.importorskip("redis")
    runtime, first, contract = _facts(temp_sqlite_store)
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.flushdb()
    projection = RedisQueryProjection(
        temp_sqlite_store,
        client,
        domain_id=runtime.genesis.id,
        redis_errors=(redis.RedisError,),
    )

    initial = projection.rebuild()
    assert projection.get_asset(first.id) == first
    assert projection.get_contract(contract.id) == contract

    client.flushdb()
    assert projection.get_assets_by_name("source") == [first]
    assert client.get(projection.active_key) == initial.digest

    second = runtime.accept_asset(
        build_control_plane_asset(name="second", content="new evidence")
    )
    assert {asset.id for asset in projection.get_all_assets()} == {
        first.id,
        second.id,
    }
    assert client.get(projection.active_key) == initial.digest
    generation = projection._generation_root(initial.digest)
    meta = client.hgetall(f"{generation}:meta")
    assert int(meta["revision"]) == temp_sqlite_store.get_runtime_revision()


def test_real_redis_shared_readers_rebuild_convergently(tmp_path):
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL to run Redis integration")
    redis = pytest.importorskip("redis")
    path = tmp_path / "shared.db"
    writer = SQLiteStore(str(path))
    runtime, first, contract = _facts(writer)
    reader = SQLiteStore(str(path))
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.flushdb()
    writer_projection = RedisQueryProjection(
        writer,
        client,
        domain_id=runtime.genesis.id,
        redis_errors=(redis.RedisError,),
    )
    reader_projection = RedisQueryProjection(
        reader,
        client,
        domain_id=runtime.genesis.id,
        redis_errors=(redis.RedisError,),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = tuple(
            pool.map(
                lambda projection: projection.rebuild(),
                (writer_projection, reader_projection),
            )
        )
    assert snapshots[0].digest == snapshots[1].digest
    assert reader_projection.get_asset(first.id) == first
    assert reader_projection.get_contract(contract.id) == contract

    second = runtime.accept_asset(
        build_control_plane_asset(name="second", content="shared update")
    )
    assert {asset.id for asset in reader_projection.get_all_assets()} == {
        first.id,
        second.id,
    }

    reader.close()
    reopened = SQLiteStore(str(path))
    reopened_projection = RedisQueryProjection(
        reopened,
        client,
        domain_id=runtime.genesis.id,
        redis_errors=(redis.RedisError,),
    )
    assert reopened_projection.get_assets_by_name("second") == [second]
    reopened.close()
    writer.close()
