"""Redis query projections remain disposable views over committed facts."""

from __future__ import annotations

import os
import sqlite3
import threading
from uuid import uuid4
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from aigineering.adapters.redis_query import (
    QueryProjectionUnavailable,
    RedisQueryProjection,
)
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


class FakeWatchError(FakeRedisError):
    pass


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.fail = False
        self._versions: dict[str, int] = {}

    def _check(self) -> None:
        if self.fail:
            raise FakeRedisError("unavailable")

    def _changed(self, key: str) -> None:
        self._versions[key] = self._versions.get(key, 0) + 1

    def get(self, key: str):
        self._check()
        return self.strings.get(key)

    def set(self, key: str, value: str):
        self._check()
        self.strings[key] = str(value)
        self._changed(key)
        return True

    def hset(self, key: str, *, mapping: dict[str, str]):
        self._check()
        self.hashes.setdefault(key, {}).update(
            {str(name): str(value) for name, value in mapping.items()}
        )
        self._changed(key)
        return len(mapping)

    def hgetall(self, key: str):
        self._check()
        return dict(self.hashes.get(key, {}))

    def sadd(self, key: str, *values: str):
        self._check()
        target = self.sets.setdefault(key, set())
        before = len(target)
        target.update(str(value) for value in values)
        if len(target) != before:
            self._changed(key)
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
            self._changed(key)
            return 1
        return 0

    def pipeline(self, *, transaction: bool):
        self._check()
        assert transaction is True
        return FakePipeline(self)

    def flushall(self):
        keys = set(self.strings) | set(self.hashes) | set(self.sets)
        self.strings.clear()
        self.hashes.clear()
        self.sets.clear()
        for key in keys:
            self._changed(key)


class FakePipeline:
    def __init__(self, client: FakeRedis) -> None:
        self._client = client
        self._commands: list[tuple[str, tuple, dict]] = []
        self._watched: dict[str, int] = {}
        self._queueing = False

    def watch(self, *keys: str):
        self._client._check()
        if self._queueing:
            raise AssertionError("WATCH must precede MULTI")
        for key in keys:
            self._watched.setdefault(key, self._client._versions.get(key, 0))
        return True

    def multi(self):
        self._queueing = True
        return self

    def reset(self):
        self._commands.clear()
        self._watched.clear()
        self._queueing = False

    def __getattr__(self, name: str):
        def command(*args, **kwargs):
            if self._queueing:
                self._commands.append((name, args, kwargs))
                return self
            return getattr(self._client, name)(*args, **kwargs)

        return command

    def execute(self):
        self._client._check()
        if any(
            self._client._versions.get(key, 0) != version
            for key, version in self._watched.items()
        ):
            raise FakeWatchError("watched key changed")
        results = [
            getattr(self._client, name)(*args, **kwargs)
            for name, args, kwargs in self._commands
        ]
        self._commands.clear()
        self._watched.clear()
        self._queueing = False
        return results


class _NativePipelineHooks:
    def __init__(
        self,
        client,
        *,
        pause_first_execute=False,
        pause_first_read=False,
        before_execute=None,
        namespace_reader=None,
    ):
        self.client = client
        self.pause_first_execute = pause_first_execute
        self.pause_first_read = pause_first_read
        self.before_execute = before_execute
        self.namespace_reader = namespace_reader
        self.entered = threading.Event()
        self.release = threading.Event()
        self.execute_calls = 0
        self.watch_errors = 0
        self.read_calls: list[str] = []
        self.before_abort: list[object] = []
        self.after_abort: list[object] = []

    def wrap(self, pipeline):
        hooks = self

        class WrappedPipeline:
            def __getattr__(self, name):
                method = getattr(pipeline, name)
                if name == "execute":

                    def execute(*args, **kwargs):
                        hooks.execute_calls += 1
                        attempt = hooks.execute_calls
                        if hooks.before_execute is not None:
                            hooks.before_execute(attempt)
                        if hooks.pause_first_execute and attempt == 1:
                            hooks.entered.set()
                            if not hooks.release.wait(timeout=15):
                                raise TimeoutError("native EXEC barrier timed out")
                        try:
                            return method(*args, **kwargs)
                        except Exception as exc:
                            if any(
                                base.__name__.endswith("WatchError")
                                for base in type(exc).__mro__
                            ):
                                hooks.watch_errors += 1
                                if hooks.namespace_reader is not None:
                                    hooks.after_abort.append(hooks.namespace_reader())
                            raise

                    return execute
                if name in {"get", "hgetall", "smembers", "mget"}:

                    def read(*args, **kwargs):
                        hooks.read_calls.append(name)
                        if (
                            name == "smembers"
                            and hooks.pause_first_read
                            and not hooks.entered.is_set()
                        ):
                            hooks.entered.set()
                            if not hooks.release.wait(timeout=15):
                                raise TimeoutError("native read barrier timed out")
                        return method(*args, **kwargs)

                    return read
                return method

        return WrappedPipeline()


class _NativeHookClient:
    def __init__(self, client, hooks: _NativePipelineHooks):
        self._client = client
        self._hooks = hooks

    def __getattr__(self, name):
        return getattr(self._client, name)

    def pipeline(self, *, transaction: bool):
        return self._hooks.wrap(self._client.pipeline(transaction=transaction))


def _native_projection(store, runtime, client, *, strict=True):
    redis = pytest.importorskip("redis")
    return RedisQueryProjection(
        store,
        client,
        domain_id=runtime.genesis.id,
        redis_errors=(redis.RedisError,),
        strict=strict,
    )


def _native_client():
    redis_url = os.getenv("AIG_REDIS_TEST_URL")
    if not redis_url:
        pytest.skip("set AIG_REDIS_TEST_URL to run Redis concurrency integration")
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    assert client.ping()
    return client


def _native_signed_store_pair(tmp_path):
    from aigineering.core.domain import initialize_genesis
    from aigineering.core.signing import Ed25519Signer
    from aigineering.protocol.candidate import ActorKey, create_genesis_manifest

    path = tmp_path / f"query-{uuid4().hex}.sqlite"
    source = SQLiteStore(str(path))
    signer = Ed25519Signer()
    nonce = uuid4().hex
    actor_key = ActorKey(
        f"test:query:{nonce}",
        f"key:{nonce}",
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
        f"query-concurrency-{nonce}", (actor_key,), f"policy:query:{nonce}"
    )
    initialize_genesis(source, genesis)
    runtime = candidate_runtime(
        source, genesis=genesis, actor_key=actor_key, signer=signer
    )
    asset = runtime.accept_asset(
        build_control_plane_asset(name="source", content="signed source evidence")
    )
    contract = runtime.accept_contract(
        build_control_plane_contract(
            name="initial",
            description="Initial signed query fixture.",
            inputs=("source",),
            outputs=("output-initial",),
            activation="source",
        )
    )
    writer = SQLiteStore(str(path))
    writer_runtime = candidate_runtime(
        writer, genesis=genesis, actor_key=actor_key, signer=signer
    )
    writer_runtime._sequence = runtime._sequence
    return path, source, runtime, asset, contract, writer, writer_runtime


def _native_late_contracts(writer_runtime, prefix: str, count: int = 3):
    return [
        writer_runtime.accept_contract(
            build_control_plane_contract(
                name=f"{prefix}-{index}",
                description=f"Signed graph-free query Contract {prefix}-{index}.",
                inputs=("source",),
                outputs=(f"output-{prefix}-{index}",),
                activation="source",
            )
        )
        for index in range(count)
    ]


def _native_namespace(client, projection):
    result = {}
    for key in sorted(client.scan_iter(match=f"{projection._root}:*")):
        key_type = client.type(key)
        if key_type == "string":
            value = client.get(key)
        elif key_type == "hash":
            value = client.hgetall(key)
        elif key_type == "set":
            value = sorted(client.smembers(key))
        else:
            value = {"type": key_type}
        result[key] = value
    return result


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


def test_query_snapshot_includes_signed_commit_before_backup(temp_sqlite_store):
    runtime, first, _ = _facts(temp_sqlite_store)
    writer = SQLiteStore(temp_sqlite_store._db_path)
    writer_runtime = candidate_runtime(
        writer,
        genesis=runtime.genesis,
        actor_key=runtime.actor_key,
        signer=runtime.signer,
    )
    try:
        second = writer_runtime.accept_asset(
            build_control_plane_asset(name="before-backup", content="new fact")
        )
        source_digest = temp_sqlite_store.runtime_materialization_digest()
        source_revision = temp_sqlite_store.get_runtime_revision()

        snapshot = build_query_snapshot(temp_sqlite_store, domain_id=runtime.genesis.id)

        assert snapshot.revision == source_revision
        assert {item[0] for item in snapshot.assets} == {first.id, second.id}
        assert temp_sqlite_store.runtime_materialization_digest() == source_digest
        assert temp_sqlite_store.get_runtime_revision() == source_revision
    finally:
        writer.close()


def test_rebuild_uses_one_closed_sqlite_snapshot_during_signed_interleave(
    temp_sqlite_store, monkeypatch
):
    runtime, first, _ = _facts(temp_sqlite_store)
    writer = SQLiteStore(temp_sqlite_store._db_path)
    writer_runtime = candidate_runtime(
        writer,
        genesis=runtime.genesis,
        actor_key=runtime.actor_key,
        signer=runtime.signer,
    )
    client = FakeRedis()
    projection = _projection(temp_sqlite_store, runtime, client)
    interleaved_asset = build_control_plane_asset(
        name="after-snapshot-copy", content="signed concurrent fact"
    )
    source_revision_before = temp_sqlite_store.get_runtime_revision()
    source_digest_before = temp_sqlite_store.runtime_materialization_digest()
    captured_source_state = []
    captured_copies = []
    source_snapshot = temp_sqlite_store.snapshot

    def snapshot_with_interleaved_writer():
        copied_store = source_snapshot()
        captured_copies.append(copied_store)
        original_get_assets = copied_store.get_all_assets
        original_close = copied_store.close
        closed = []
        captured_copies.append(closed)

        def read_assets_then_publish():
            assets = original_get_assets()
            if not captured_source_state:
                accepted = writer_runtime.accept_asset(interleaved_asset)
                captured_source_state.append(
                    (
                        accepted,
                        temp_sqlite_store.get_runtime_revision(),
                        temp_sqlite_store.runtime_materialization_digest(),
                    )
                )
            return assets

        def close_copy():
            original_close()
            closed.append(True)

        copied_store.get_all_assets = read_assets_then_publish
        copied_store.close = close_copy
        return copied_store

    monkeypatch.setattr(temp_sqlite_store, "snapshot", snapshot_with_interleaved_writer)
    try:
        snapshot = projection.rebuild()

        accepted, source_revision_after, source_digest_after = captured_source_state[0]
        assert accepted.name == interleaved_asset.name
        assert source_revision_after > source_revision_before
        assert snapshot.revision == source_revision_before
        assert snapshot.revision < source_revision_after
        assert {item[0] for item in snapshot.assets} == {first.id}
        assert temp_sqlite_store.get_runtime_revision() == source_revision_after
        assert temp_sqlite_store.runtime_materialization_digest() == source_digest_after
        assert source_digest_after != source_digest_before
        with pytest.raises(sqlite3.ProgrammingError):
            captured_copies[0].get_runtime_revision()
        assert captured_copies[1] == [True]

        # The rebuilt generation is coherent but older than the concurrent commit.
        # The ordinary freshness path catches up on the next indexed read.
        assert projection.get_assets_by_name(interleaved_asset.name) == [accepted]
        active_generation = projection._generation_root(
            client.get(projection.active_key)
        )
        assert client.smembers(
            f"{active_generation}:asset-name:{projection._index_token(interleaved_asset.name)}"
        ) == {accepted.id}
        assert client.get(f"{active_generation}:asset:{accepted.id}") is not None
        assert client.hgetall(f"{active_generation}:meta")["revision"] == str(
            source_revision_after
        )
    finally:
        writer.close()


def test_snapshot_projection_failure_closes_copy_without_publishing_generation(
    temp_sqlite_store, monkeypatch
):
    runtime, _, _ = _facts(temp_sqlite_store)
    client = FakeRedis()
    projection = _projection(temp_sqlite_store, runtime, client)
    active = projection.rebuild().digest
    redis_state_before = (
        deepcopy(client.strings),
        deepcopy(client.hashes),
        deepcopy(client.sets),
    )
    source_revision = temp_sqlite_store.get_runtime_revision()
    source_digest = temp_sqlite_store.runtime_materialization_digest()
    source_snapshot = temp_sqlite_store.snapshot
    captured = []

    def failing_snapshot():
        copied_store = source_snapshot()
        captured.append(copied_store)
        original_close = copied_store.close
        close_count = []

        def close_copy():
            close_count.append(True)
            original_close()

        def fail_read():
            raise RuntimeError("injected query projection read failure")

        copied_store.get_all_contracts = fail_read
        copied_store.close = close_copy
        captured.append(close_count)
        return copied_store

    monkeypatch.setattr(temp_sqlite_store, "snapshot", failing_snapshot)
    with pytest.raises(RuntimeError, match="injected query projection read failure"):
        projection.rebuild()

    assert captured[1] == [True]
    with pytest.raises(sqlite3.ProgrammingError):
        captured[0].get_runtime_revision()
    assert client.get(projection.active_key) == active
    assert (
        client.strings,
        client.hashes,
        client.sets,
    ) == redis_state_before
    assert temp_sqlite_store.get_runtime_revision() == source_revision
    assert temp_sqlite_store.runtime_materialization_digest() == source_digest


def test_query_snapshot_refuses_reader_without_snapshot_capability(temp_sqlite_store):
    runtime, _, _ = _facts(temp_sqlite_store)

    class LiveReader:
        def __getattr__(self, name):
            if name == "snapshot":
                raise AttributeError(name)
            return getattr(temp_sqlite_store, name)

    with pytest.raises(TypeError, match="must provide a consistent snapshot"):
        build_query_snapshot(LiveReader(), domain_id=runtime.genesis.id)


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
    active = client.get(projection.active_key)
    assert active and active != old.digest
    generation = projection._generation_root(active)
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


def test_json_view_discards_build_when_source_advances(temp_sqlite_store):
    runtime, _, contract = _facts(temp_sqlite_store)
    writer = SQLiteStore(temp_sqlite_store._db_path)
    writer_runtime = candidate_runtime(
        writer,
        genesis=runtime.genesis,
        actor_key=runtime.actor_key,
        signer=runtime.signer,
    )
    writer_runtime._sequence = runtime._sequence
    projection = _projection(temp_sqlite_store, runtime, FakeRedis())
    projection.rebuild()
    calls = 0

    def build():
        nonlocal calls
        calls += 1
        if calls == 1:
            writer_runtime.accept_contract(
                build_control_plane_contract(
                    name="during-json-build",
                    description="Signed concurrent build.",
                    inputs=("source",),
                    outputs=("json-output",),
                    activation="source",
                )
            )
        return {"call": calls}

    try:
        result = projection.memoize_json("task.status", contract.id, build)
        assert calls == 2
        assert result == {"call": 2}
        current = temp_sqlite_store.get_runtime_revision()
        assert any(f":view:{current}:" in key for key in projection._client.strings)
        assert not any(
            f":view:{current - 1}:" in key for key in projection._client.strings
        )
    finally:
        writer.close()


def test_compatibility_fallback_retries_source_revision_change(temp_sqlite_store):
    runtime, _, initial = _facts(temp_sqlite_store)
    writer = SQLiteStore(temp_sqlite_store._db_path)
    writer_runtime = candidate_runtime(
        writer,
        genesis=runtime.genesis,
        actor_key=runtime.actor_key,
        signer=runtime.signer,
    )
    writer_runtime._sequence = runtime._sequence
    client = FakeRedis()
    degraded = []
    projection = RedisQueryProjection(
        temp_sqlite_store,
        client,
        domain_id=runtime.genesis.id,
        redis_errors=(FakeRedisError,),
        strict=False,
        on_degraded=degraded.append,
    )
    projection.rebuild()
    original = projection._fallback.get_all_contracts
    calls = 0

    def interleaved_fallback():
        nonlocal calls
        calls += 1
        result = original()
        if calls == 1:
            writer_runtime.accept_contract(
                build_control_plane_contract(
                    name="during-fallback",
                    description="Signed fallback interleave.",
                    inputs=("source",),
                    outputs=("fallback-output",),
                    activation="source",
                )
            )
        return result

    projection._fallback.get_all_contracts = interleaved_fallback
    client.fail = True
    try:
        result = projection.get_all_contracts()
        assert calls == 2
        assert {contract.name for contract in result} >= {
            initial.name,
            "during-fallback",
        }
        assert degraded
    finally:
        writer.close()


def test_unchanged_current_read_avoids_snapshot_and_full_record_scan(
    temp_sqlite_store, monkeypatch
):
    runtime, _, _ = _facts(temp_sqlite_store)
    projection = _projection(temp_sqlite_store, runtime, FakeRedis())
    projection.rebuild()

    def unexpected(*_args, **_kwargs):
        raise AssertionError("unchanged current reads must not snapshot or scan")

    monkeypatch.setattr(
        "aigineering.adapters.redis_query.build_query_snapshot", unexpected
    )
    monkeypatch.setattr(temp_sqlite_store, "scan_runtime_records", unexpected)
    assert projection.get_all_contracts()


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
    active = client.get(projection.active_key)
    assert active and active != initial.digest
    generation = projection._generation_root(active)
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


def test_native_rebuild_retries_after_active_metadata_watch_invalidation(
    tmp_path,
):
    client = _native_client()
    _, source, runtime, _, _, writer, _ = _native_signed_store_pair(tmp_path)
    try:
        projection = _native_projection(source, runtime, client)
        projection.rebuild()
        before = _native_namespace(client, projection)
        hooks = _NativePipelineHooks(
            client,
            pause_first_execute=True,
            namespace_reader=lambda: _native_namespace(client, projection),
        )
        projection._client = _NativeHookClient(client, hooks)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(projection._rebuild)
            assert hooks.entered.wait(timeout=10)
            active_meta_key = (
                f"{projection._generation_root(client.get(projection.active_key))}:meta"
            )
            client.hset(active_meta_key, mapping=client.hgetall(active_meta_key))
            hooks.release.set()
            rebuilt = future.result(timeout=15)

        assert rebuilt.revision == source.get_runtime_revision()
        assert hooks.execute_calls == 2
        assert hooks.watch_errors == 1
        assert hooks.after_abort == [before]
        assert _native_namespace(client, projection) == before
    finally:
        source.close()
        writer.close()


def test_native_read_rejects_generation_advanced_after_selection(tmp_path):
    client = _native_client()
    _, source, runtime, _, _, writer, writer_runtime = _native_signed_store_pair(
        tmp_path
    )
    try:
        projection = _native_projection(source, runtime, client)
        projection.rebuild()
        initial_revision = source.get_runtime_revision()

        class FrozenRevisionStore:
            def __getattr__(self, name):
                return getattr(source, name)

            def get_runtime_revision(self):
                return initial_revision

        stale_reader = _native_projection(FrozenRevisionStore(), runtime, client)
        hooks = _NativePipelineHooks(client, pause_first_read=True)
        stale_reader._client = _NativeHookClient(client, hooks)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stale_reader.get_all_contracts)
            assert hooks.entered.wait(timeout=10)
            later = _native_late_contracts(writer_runtime, "read-race")
            winner = _native_projection(writer, runtime, client)
            winner.catch_up(
                projection._generation_root(client.get(projection.active_key)),
                after_revision=initial_revision,
            )
            winner_namespace = _native_namespace(client, projection)
            hooks.release.set()
            with pytest.raises(QueryProjectionUnavailable):
                result = future.result(timeout=15)
                assert {contract.id for contract in result}.isdisjoint(
                    {contract.id for contract in later}
                )
        assert _native_namespace(client, projection) == winner_namespace
    finally:
        source.close()
        writer.close()


def test_native_catch_up_loser_preserves_winning_generation_bytes(tmp_path):
    client = _native_client()
    _, source, runtime, _, _, writer, writer_runtime = _native_signed_store_pair(
        tmp_path
    )
    try:
        projection = _native_projection(source, runtime, client)
        projection.rebuild()
        initial_revision = source.get_runtime_revision()
        earlier = _native_late_contracts(writer_runtime, "catchup-first", count=1)
        earlier_revision = source.get_runtime_revision()

        class FrozenTargetStore:
            def __getattr__(self, name):
                return getattr(source, name)

            def get_runtime_revision(self):
                return earlier_revision

        stale = _native_projection(FrozenTargetStore(), runtime, client)
        hooks = _NativePipelineHooks(
            client,
            pause_first_execute=True,
            namespace_reader=lambda: _native_namespace(client, projection),
        )
        stale._client = _NativeHookClient(client, hooks)
        generation = projection._generation_root(client.get(projection.active_key))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                stale.catch_up, generation, after_revision=initial_revision
            )
            assert hooks.entered.wait(timeout=10)
            later = _native_late_contracts(writer_runtime, "catchup-later", count=2)
            winner = _native_projection(writer, runtime, client)
            winner.catch_up(generation, after_revision=initial_revision)
            winner_namespace = _native_namespace(client, projection)
            hooks.release.set()
            with pytest.raises(QueryProjectionUnavailable):
                future.result(timeout=15)

        assert hooks.execute_calls == 1
        assert hooks.watch_errors == 1
        assert hooks.after_abort == [winner_namespace]
        assert _native_namespace(client, projection) == winner_namespace
        active = projection._generation_root(client.get(projection.active_key))
        assert client.hgetall(f"{active}:meta")["revision"] == str(
            source.get_runtime_revision()
        )
        assert {contract.id for contract in earlier + later}.issubset(
            client.smembers(f"{active}:contracts")
        )
    finally:
        source.close()
        writer.close()


def test_native_live_read_discards_postfetch_result_and_retries(tmp_path):
    client = _native_client()
    _, source, runtime, _, _, writer, writer_runtime = _native_signed_store_pair(
        tmp_path
    )
    try:
        projection = _native_projection(source, runtime, client)
        projection.rebuild()
        hooks = _NativePipelineHooks(client, pause_first_execute=True)
        projection._client = _NativeHookClient(client, hooks)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(projection.get_all_contracts)
            assert hooks.entered.wait(timeout=10)
            later = _native_late_contracts(writer_runtime, "live-read-race", count=2)
            winner = _native_projection(writer, runtime, client)
            generation = projection._generation_root(client.get(projection.active_key))
            earlier_revision = int(client.hgetall(f"{generation}:meta")["revision"])
            winner.catch_up(generation, after_revision=earlier_revision)
            hooks.release.set()
            result = future.result(timeout=15)

        assert hooks.watch_errors == 1
        assert hooks.execute_calls >= 2
        result_ids = {contract.id for contract in result}
        assert {contract.id for contract in later}.issubset(result_ids)
        assert len(result_ids) == len(result)
        active = projection._generation_root(client.get(projection.active_key))
        assert client.hgetall(f"{active}:meta")["revision"] == str(
            source.get_runtime_revision()
        )
    finally:
        source.close()
        writer.close()


def test_native_rebuild_from_frozen_source_cannot_replace_newer_winner(tmp_path):
    client = _native_client()
    _, source, runtime, _, _, writer, writer_runtime = _native_signed_store_pair(
        tmp_path
    )
    pinned = source.snapshot()
    try:
        projection = _native_projection(source, runtime, client)
        projection.rebuild()
        initial_revision = source.get_runtime_revision()

        class FrozenStore:
            def __getattr__(self, name):
                return getattr(pinned, name)

            def snapshot(self):
                return pinned.snapshot()

        frozen = _native_projection(FrozenStore(), runtime, client)
        hooks = _NativePipelineHooks(
            client,
            pause_first_execute=True,
            namespace_reader=lambda: _native_namespace(client, projection),
        )
        frozen._client = _NativeHookClient(client, hooks)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(frozen._rebuild)
            assert hooks.entered.wait(timeout=10)
            _native_late_contracts(writer_runtime, "rebuild-winner", count=2)
            winner = _native_projection(writer, runtime, client)
            winner.rebuild()
            winner_namespace = _native_namespace(client, projection)
            hooks.release.set()
            with pytest.raises(QueryProjectionUnavailable):
                future.result(timeout=15)

        assert hooks.execute_calls == 1
        assert hooks.watch_errors == 1
        assert hooks.after_abort == [winner_namespace]
        assert _native_namespace(client, projection) == winner_namespace
        active = projection._generation_root(client.get(projection.active_key))
        assert client.hgetall(f"{active}:meta")["revision"] == str(
            source.get_runtime_revision()
        )
        assert source.get_runtime_revision() > initial_revision
    finally:
        pinned.close()
        source.close()
        writer.close()


def test_native_rebuild_target_metadata_touch_aborts_before_retry(tmp_path):
    client = _native_client()
    _, source, runtime, _, _, writer, writer_runtime = _native_signed_store_pair(
        tmp_path
    )
    try:
        projection = _native_projection(source, runtime, client)
        projection.rebuild()
        old_digest = client.get(projection.active_key)
        _native_late_contracts(writer_runtime, "target-meta", count=1)
        projection.rebuild()
        target_digest = client.get(projection.active_key)
        assert target_digest != old_digest
        client.set(projection.active_key, old_digest)
        before = _native_namespace(client, projection)
        target_meta_key = f"{projection._generation_root(target_digest)}:meta"
        hooks = _NativePipelineHooks(
            client,
            pause_first_execute=True,
            namespace_reader=lambda: _native_namespace(client, projection),
        )
        projection._client = _NativeHookClient(client, hooks)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(projection._rebuild)
            assert hooks.entered.wait(timeout=10)
            client.hset(target_meta_key, mapping=client.hgetall(target_meta_key))
            hooks.release.set()
            rebuilt = future.result(timeout=15)

        assert hooks.execute_calls == 2
        assert hooks.watch_errors == 1
        assert hooks.after_abort == [before]
        assert rebuilt.digest == target_digest
        assert _native_namespace(client, projection) != before
        assert client.get(projection.active_key) == target_digest
    finally:
        source.close()
        writer.close()


def test_native_catch_up_exhausts_three_metadata_conflicts_without_writes(tmp_path):
    client = _native_client()
    _, source, runtime, _, _, writer, writer_runtime = _native_signed_store_pair(
        tmp_path
    )
    try:
        projection = _native_projection(source, runtime, client)
        projection.rebuild()
        initial_revision = source.get_runtime_revision()
        _native_late_contracts(writer_runtime, "exhaustion", count=1)
        generation = projection._generation_root(client.get(projection.active_key))
        meta_key = f"{generation}:meta"
        before = _native_namespace(client, projection)

        def touch_meta(attempt):
            if attempt <= 3:
                client.hset(meta_key, mapping=client.hgetall(meta_key))

        hooks = _NativePipelineHooks(
            client,
            before_execute=touch_meta,
            namespace_reader=lambda: _native_namespace(client, projection),
        )
        projection._client = _NativeHookClient(client, hooks)
        with pytest.raises(QueryProjectionUnavailable):
            projection.catch_up(generation, after_revision=initial_revision)

        assert hooks.execute_calls == 3
        assert hooks.watch_errors == 3
        assert hooks.after_abort == [before, before, before]
        assert _native_namespace(client, projection) == before
    finally:
        source.close()
        writer.close()


def test_native_live_read_rechecks_source_after_validating_exec(tmp_path):
    client = _native_client()
    _, source, runtime, _, _, writer, writer_runtime = _native_signed_store_pair(
        tmp_path
    )
    try:
        projection = _native_projection(source, runtime, client)
        projection.rebuild()
        committed = []

        def commit_before_exec(attempt):
            if attempt == 1:
                committed.extend(
                    _native_late_contracts(writer_runtime, "exec-source-race")
                )

        hooks = _NativePipelineHooks(client, before_execute=commit_before_exec)
        projection._client = _NativeHookClient(client, hooks)
        result = projection.get_all_contracts()

        assert hooks.execute_calls >= 3
        assert hooks.watch_errors == 0
        assert committed
        result_ids = {contract.id for contract in result}
        assert {contract.id for contract in committed}.issubset(result_ids)
        assert len(result_ids) == len(result)
    finally:
        source.close()
        writer.close()
