"""Disposable Redis query projection over authoritative Store facts."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from aigineering.core.ids import compute_content_hash
from aigineering.core.query_projection import (
    QUERY_PROJECTION_SCHEMA,
    QuerySnapshot,
    StoreQueryProjection,
    asset_from_query_json,
    build_query_snapshot,
    contract_from_query_json,
)
from aigineering.protocol.immutability import deep_thaw

_ADVANCE_REVISION = """
local current = tonumber(redis.call('HGET', KEYS[1], 'revision') or '-1')
local target = tonumber(ARGV[1])
if current <= target then
    redis.call('HSET', KEYS[1], 'revision', ARGV[1])
    redis.call('HSET', KEYS[1], 'asset_count', redis.call('SCARD', KEYS[2]))
    redis.call('HSET', KEYS[1], 'contract_count', redis.call('SCARD', KEYS[3]))
    redis.call('HSET', KEYS[1], 'content_count', redis.call('SCARD', KEYS[4]))
    redis.call('HSET', KEYS[1], 'definition_count', redis.call('SCARD', KEYS[5]))
    redis.call('HSET', KEYS[1], 'assertion_count', redis.call('SCARD', KEYS[6]))
    return 1
end
return 0
"""

_logger = logging.getLogger(__name__)
_MAX_QUERY_RETRIES = 3


class _RetryQueryRead(RuntimeError):
    """A bounded query attempt observed a source or Redis fence change."""


def _is_watch_error(exc: BaseException) -> bool:
    return any(base.__name__.endswith("WatchError") for base in type(exc).__mro__)


class CorruptQueryProjection(ValueError):
    """A cache generation is structurally incomplete or invalid."""


class QueryProjectionUnavailable(RuntimeError):
    """The configured Redis query projection cannot be reached."""


@lru_cache(maxsize=8)
def _client_from_url(redis_url: str):
    import redis

    return redis.Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=2.0,
    )


def redis_client(redis_url: str):
    """Return the bounded-timeout Redis client used by application adapters."""
    return _client_from_url(redis_url)


def redis_errors() -> tuple[type[BaseException], ...]:
    """Return redis-py's stable exception family without importing it at module load."""
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError("Redis support requires the redis package") from exc
    return (redis.RedisError,)


class RedisQueryProjection:
    """Serve current read views from Redis with authoritative fallback."""

    def __init__(
        self,
        store,
        client,
        *,
        domain_id: str,
        redis_errors: tuple[type[BaseException], ...] = (ConnectionError,),
        on_degraded: Callable[[BaseException], None] | None = None,
        strict: bool = False,
    ) -> None:
        self._store = store
        self._fallback = StoreQueryProjection(store)
        self._client = client
        self._domain_id = domain_id
        self._redis_errors = redis_errors
        self._on_degraded = on_degraded or self._log_degraded
        self._strict = strict
        domain_key = compute_content_hash(domain_id)
        self._root = f"aig:q:{QUERY_PROJECTION_SCHEMA}:{domain_key}"

    @classmethod
    def from_url(cls, store, *, domain_id: str, redis_url: str, strict: bool = False):
        """Create the adapter without making redis-py a base dependency."""
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError(
                "Redis query projection requires 'aigineering[redis]'"
            ) from exc
        client = _client_from_url(redis_url)
        return cls(
            store,
            client,
            domain_id=domain_id,
            redis_errors=(redis.RedisError,),
            strict=strict,
        )

    @property
    def active_key(self) -> str:
        return f"{self._root}:active"

    def _log_degraded(self, exc: BaseException) -> None:
        _logger.warning("Redis query projection degraded to SQLite: %s", exc)

    def _generation_root(self, digest: str) -> str:
        return f"{self._root}:gen:{digest}"

    @staticmethod
    def _index_token(value: str) -> str:
        return compute_content_hash(value)

    def rebuild(self) -> QuerySnapshot:
        """Rebuild a generation or raise one stable adapter error."""
        try:
            return self._rebuild()
        except self._redis_errors as exc:
            raise QueryProjectionUnavailable(
                "Redis query projection unavailable"
            ) from exc

    def _rebuild(self) -> QuerySnapshot:
        """Rebuild one generation behind Redis active and target WATCH fences."""
        for attempt in range(_MAX_QUERY_RETRIES):
            snapshot = build_query_snapshot(self._store, domain_id=self._domain_id)
            generation = self._generation_root(snapshot.digest)
            target_meta_key = f"{generation}:meta"
            pipeline = self._client.pipeline(transaction=True)
            try:
                pipeline.watch(self.active_key)
                active_digest = pipeline.get(self.active_key)
                active_generation = (
                    self._generation_root(str(active_digest)) if active_digest else None
                )
                active_meta_key = (
                    f"{active_generation}:meta"
                    if active_generation is not None
                    else None
                )
                watched_meta_keys = [target_meta_key]
                if active_meta_key is not None and active_meta_key != target_meta_key:
                    watched_meta_keys.append(active_meta_key)
                pipeline.watch(*watched_meta_keys)
                active_meta = (
                    pipeline.hgetall(active_meta_key)
                    if active_generation is not None
                    else {}
                )
                target_meta = pipeline.hgetall(target_meta_key)
                active_revision = self._fence_revision(
                    active_meta,
                    expected_digest=str(active_digest) if active_digest else None,
                    allow_incomplete=active_generation != generation,
                )
                target_revision = self._fence_revision(
                    target_meta, expected_digest=snapshot.digest
                )
                stale_revision = max(
                    (
                        revision
                        for revision in (active_revision, target_revision)
                        if revision is not None
                    ),
                    default=-1,
                )
                if stale_revision > snapshot.revision:
                    source_revision = self._store.get_runtime_revision()
                    if source_revision > snapshot.revision:
                        continue
                    raise QueryProjectionUnavailable(
                        "Redis query projection source is stale"
                    )

                pipeline.multi()
                for asset_id, payload in snapshot.assets:
                    pipeline.set(f"{generation}:asset:{asset_id}", payload)
                    pipeline.sadd(f"{generation}:assets", asset_id)
                for name, asset_ids in snapshot.asset_names:
                    key = f"{generation}:asset-name:{self._index_token(name)}"
                    if asset_ids:
                        pipeline.sadd(key, *asset_ids)
                for definition, asset_ids in snapshot.asset_definitions:
                    key = (
                        f"{generation}:asset-definition:{self._index_token(definition)}"
                    )
                    if asset_ids:
                        pipeline.sadd(key, *asset_ids)
                for contract_id, payload in snapshot.contracts:
                    pipeline.set(f"{generation}:contract:{contract_id}", payload)
                    pipeline.sadd(f"{generation}:contracts", contract_id)
                for content_id, payload in snapshot.contents:
                    pipeline.set(f"{generation}:content:{content_id}", payload)
                    pipeline.sadd(f"{generation}:contents", content_id)
                for definition_id, payload in snapshot.signed_definitions:
                    pipeline.set(f"{generation}:definition:{definition_id}", payload)
                    pipeline.sadd(f"{generation}:definitions", definition_id)
                for assertion_id, payload in snapshot.assertions:
                    assertion = json.loads(payload)
                    pipeline.set(f"{generation}:assertion:{assertion_id}", payload)
                    pipeline.sadd(f"{generation}:assertions", assertion_id)
                    pipeline.sadd(
                        f"{generation}:assertion-definition:"
                        f"{self._index_token(str(assertion['definition_id']))}",
                        assertion_id,
                    )
                    pipeline.sadd(
                        f"{generation}:assertion-content:"
                        f"{self._index_token(str(assertion['content_id']))}",
                        assertion_id,
                    )
                pipeline.hset(
                    target_meta_key,
                    mapping={
                        "digest": snapshot.digest,
                        "domain_id": snapshot.domain_id,
                        "asset_count": str(len(snapshot.assets)),
                        "contract_count": str(len(snapshot.contracts)),
                        "content_count": str(len(snapshot.contents)),
                        "definition_count": str(len(snapshot.signed_definitions)),
                        "assertion_count": str(len(snapshot.assertions)),
                        "revision": str(snapshot.revision),
                        "schema": QUERY_PROJECTION_SCHEMA,
                        "status": "ready",
                    },
                )
                pipeline.set(self.active_key, snapshot.digest)
                pipeline.execute()
                return snapshot
            except self._redis_errors as exc:
                if not _is_watch_error(exc):
                    raise
                if attempt + 1 == _MAX_QUERY_RETRIES:
                    raise QueryProjectionUnavailable(
                        "Redis query projection changed during rebuild"
                    ) from None
            finally:
                pipeline.reset()
        raise QueryProjectionUnavailable(
            "Redis query projection changed during rebuild"
        )

    def _fence_revision(
        self,
        meta: dict[str, str],
        *,
        expected_digest: str | None,
        allow_incomplete: bool = False,
    ) -> int | None:
        if not meta:
            return None
        if (
            allow_incomplete
            and meta.get("status") != "ready"
            and meta.get("digest") != expected_digest
        ):
            return None
        try:
            revision = int(meta["revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QueryProjectionUnavailable(
                "Redis query projection metadata is invalid"
            ) from exc
        if (
            revision < 0
            or meta.get("status") != "ready"
            or meta.get("domain_id") != self._domain_id
            or meta.get("schema") != QUERY_PROJECTION_SCHEMA
            or meta.get("digest") != expected_digest
        ):
            raise QueryProjectionUnavailable(
                "Redis query projection metadata is invalid"
            )
        return revision

    def _active_metadata(self) -> tuple[str, dict[str, str]] | None:
        digest = self._client.get(self.active_key)
        if not digest:
            return None
        generation = self._generation_root(str(digest))
        meta = self._client.hgetall(f"{generation}:meta")
        if (
            meta.get("status") != "ready"
            or meta.get("domain_id") != self._domain_id
            or meta.get("schema") != QUERY_PROJECTION_SCHEMA
        ):
            return None
        try:
            int(meta.get("revision", "-1"))
        except (TypeError, ValueError):
            return None
        return generation, meta

    @staticmethod
    def _record_payload(record) -> dict[str, Any]:
        return deep_thaw(record.payload)

    def _queue_asset(self, pipeline, generation: str, asset: dict[str, Any]) -> None:
        asset_id = str(asset["id"])
        pipeline.set(
            f"{generation}:asset:{asset_id}",
            json.dumps(
                asset,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        pipeline.sadd(f"{generation}:assets", asset_id)
        pipeline.sadd(
            f"{generation}:asset-name:{self._index_token(str(asset['name']))}",
            asset_id,
        )
        pipeline.sadd(
            f"{generation}:asset-definition:"
            f"{self._index_token(str(asset['definition_hash']))}",
            asset_id,
        )

    def _queue_contract(
        self, pipeline, generation: str, contract: dict[str, Any]
    ) -> None:
        contract_id = str(contract["id"])
        pipeline.set(
            f"{generation}:contract:{contract_id}",
            json.dumps(
                contract,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        pipeline.sadd(f"{generation}:contracts", contract_id)

    def _queue_graph_payload(
        self,
        pipeline,
        generation: str,
        kind: str,
        value: dict[str, Any],
    ) -> None:
        object_id = str(value["id"])
        pipeline.set(
            f"{generation}:{kind}:{object_id}",
            json.dumps(
                value,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        pipeline.sadd(f"{generation}:{kind}s", object_id)
        if kind == "assertion":
            pipeline.sadd(
                f"{generation}:assertion-definition:"
                f"{self._index_token(str(value['definition_id']))}",
                object_id,
            )
            pipeline.sadd(
                f"{generation}:assertion-content:"
                f"{self._index_token(str(value['content_id']))}",
                object_id,
            )

    def catch_up(
        self,
        generation: str,
        *,
        after_revision: int,
        active_bound: bool = False,
    ) -> int:
        """Apply a bounded immutable-record range behind a metadata WATCH fence."""
        meta_key = f"{generation}:meta"
        digest = generation.rsplit(":gen:", 1)[-1]
        for attempt in range(_MAX_QUERY_RETRIES):
            target_revision = self._store.get_runtime_revision()
            if target_revision < after_revision:
                raise QueryProjectionUnavailable(
                    "Redis query projection source is stale"
                )
            records = self._store.scan_runtime_records(
                after_revision=after_revision,
                through_revision=target_revision,
            )
            if any(
                record.record_type
                in {
                    "asset.content.published",
                    "asset.definition.published",
                    "asset.definition-content.asserted",
                    "asset.legacy-graph.migrated",
                }
                for _, record in records
            ):
                return -1
            if not records:
                if target_revision == after_revision:
                    return after_revision
                raise QueryProjectionUnavailable(
                    "Redis query projection source changed during catch-up"
                )

            record_revision = int(records[-1][0])
            if record_revision != target_revision:
                target_revision = record_revision
            pipeline = self._client.pipeline(transaction=True)
            watch_keys = [meta_key]
            if active_bound:
                watch_keys.append(self.active_key)
            try:
                pipeline.watch(*watch_keys)
                meta = pipeline.hgetall(meta_key)
                current_revision = self._fence_revision(meta, expected_digest=digest)
                if current_revision is None:
                    raise QueryProjectionUnavailable(
                        "Redis query projection metadata is missing"
                    )
                if active_bound and pipeline.get(self.active_key) != digest:
                    raise _RetryQueryRead("active query generation changed")
                if current_revision != after_revision:
                    if (
                        current_revision > after_revision
                        and current_revision <= target_revision
                    ):
                        return current_revision
                    raise QueryProjectionUnavailable(
                        "Redis query projection generation changed during catch-up"
                    )

                pipeline.multi()
                for _, record in records:
                    payload = self._record_payload(record)
                    if record.record_type == "asset.committed":
                        asset = payload.get("asset")
                        if isinstance(asset, dict):
                            asset_from_query_json(json.dumps(asset))
                            self._queue_asset(pipeline, generation, asset)
                    elif record.record_type == "contract.declared":
                        contract = payload.get("contract")
                        if isinstance(contract, dict):
                            contract_from_query_json(json.dumps(contract))
                            self._queue_contract(pipeline, generation, contract)
                    elif record.record_type == "asset.content.published":
                        self._queue_graph_payload(
                            pipeline, generation, "content", payload["content"]
                        )
                    elif record.record_type == "asset.definition.published":
                        self._queue_graph_payload(
                            pipeline, generation, "definition", payload["definition"]
                        )
                    elif record.record_type == "asset.definition-content.asserted":
                        self._queue_graph_payload(
                            pipeline, generation, "assertion", payload["assertion"]
                        )
                    elif record.record_type == "asset.legacy-graph.migrated":
                        for kind in ("content", "definition", "assertion"):
                            self._queue_graph_payload(
                                pipeline, generation, kind, payload[kind]
                            )
                pipeline.eval(
                    _ADVANCE_REVISION,
                    6,
                    meta_key,
                    f"{generation}:assets",
                    f"{generation}:contracts",
                    f"{generation}:contents",
                    f"{generation}:definitions",
                    f"{generation}:assertions",
                    str(target_revision),
                )
                pipeline.execute()
                return target_revision
            except self._redis_errors as exc:
                if not _is_watch_error(exc):
                    raise
                if attempt + 1 == _MAX_QUERY_RETRIES:
                    raise QueryProjectionUnavailable(
                        "Redis query projection changed during catch-up"
                    ) from None
            finally:
                pipeline.reset()
        raise QueryProjectionUnavailable(
            "Redis query projection changed during catch-up"
        )

    def _current_generation(self) -> str:
        active = self._active_metadata()
        authoritative_revision = self._store.get_runtime_revision()
        if active is None:
            return self._generation_root(self._rebuild().digest)
        generation, meta = active
        cached_revision = int(meta["revision"])
        if cached_revision == authoritative_revision:
            return generation
        if cached_revision > authoritative_revision:
            raise QueryProjectionUnavailable("Redis query projection source is stale")
        try:
            applied = self.catch_up(
                generation,
                after_revision=cached_revision,
                active_bound=True,
            )
        except _RetryQueryRead:
            raise
        except QueryProjectionUnavailable:
            if self._store.get_runtime_revision() != authoritative_revision:
                raise _RetryQueryRead("source advanced during catch-up") from None
            raise
        if applied == -1:
            return self._generation_root(self._rebuild().digest)
        source_after = self._store.get_runtime_revision()
        refreshed = self._client.hgetall(f"{generation}:meta")
        refreshed_revision = int(refreshed.get("revision", "-1"))
        if refreshed_revision == source_after:
            return generation
        if refreshed_revision > source_after:
            raise QueryProjectionUnavailable("Redis query projection source is stale")
        if source_after != authoritative_revision:
            raise _RetryQueryRead("source advanced during catch-up")
        raise QueryProjectionUnavailable(
            "Redis query projection changed during refresh"
        )

    def _consistent_fallback(self, fallback: Callable[[], Any]):
        for _attempt in range(_MAX_QUERY_RETRIES):
            before = self._store.get_runtime_revision()
            value = fallback()
            after = self._store.get_runtime_revision()
            if before == after:
                return value
        raise QueryProjectionUnavailable(
            "Redis query projection source changed during fallback"
        )

    def _cached(
        self,
        operation: Callable[[Any, str, int], Any],
        fallback: Callable[[], Any],
    ):
        corruption_rebuilt = False
        for _attempt in range(_MAX_QUERY_RETRIES):
            source_revision = self._store.get_runtime_revision()
            try:
                generation = self._current_generation()
                pipeline = self._client.pipeline(transaction=True)
                try:
                    pipeline.watch(f"{generation}:meta")
                    meta = pipeline.hgetall(f"{generation}:meta")
                    cached_revision = self._fence_revision(
                        meta,
                        expected_digest=generation.rsplit(":gen:", 1)[-1],
                    )
                    if cached_revision is None:
                        raise CorruptQueryProjection("generation metadata is missing")
                    if cached_revision != source_revision:
                        source_after = self._store.get_runtime_revision()
                        if source_after != source_revision:
                            raise _RetryQueryRead(
                                "source advanced during generation selection"
                            )
                        if cached_revision > source_revision:
                            raise QueryProjectionUnavailable(
                                "Redis query projection source is stale"
                            )
                        raise _RetryQueryRead(
                            "generation is behind the captured source"
                        )
                    value = operation(pipeline, generation, source_revision)
                    pipeline.multi()
                    pipeline.execute()
                    source_after = self._store.get_runtime_revision()
                    if source_after != source_revision:
                        raise _RetryQueryRead("source advanced during query read")
                    return value
                finally:
                    pipeline.reset()
            except _RetryQueryRead:
                continue
            except self._redis_errors as exc:
                if _is_watch_error(exc):
                    continue
                if self._strict:
                    raise QueryProjectionUnavailable(
                        "Redis query projection unavailable"
                    ) from exc
                self._on_degraded(exc)
                return self._consistent_fallback(fallback)
            except QueryProjectionUnavailable:
                if self._strict:
                    raise
                self._on_degraded(ConnectionError("Redis query projection unavailable"))
                return self._consistent_fallback(fallback)
            except CorruptQueryProjection:
                if not corruption_rebuilt:
                    corruption_rebuilt = True
                    try:
                        self._rebuild()
                    except self._redis_errors as exc:
                        if self._strict:
                            raise QueryProjectionUnavailable(
                                "Redis query projection unavailable"
                            ) from exc
                        self._on_degraded(exc)
                        return self._consistent_fallback(fallback)
                    except QueryProjectionUnavailable:
                        if self._strict:
                            raise QueryProjectionUnavailable(
                                "Redis query projection unavailable"
                            ) from None
                        self._on_degraded(
                            ConnectionError("Redis query projection unavailable")
                        )
                        return self._consistent_fallback(fallback)
                    continue
                if self._strict:
                    raise CorruptQueryProjection(
                        "Redis query projection is corrupt"
                    ) from None
                return self._consistent_fallback(fallback)
        if self._strict:
            raise QueryProjectionUnavailable(
                "Redis query projection changed during read"
            ) from None
        self._on_degraded(ConnectionError("Redis query projection changed during read"))
        return self._consistent_fallback(fallback)

    def get_asset(self, asset_id: str):
        def read(client, generation: str, _source_revision: int):
            payload = client.get(f"{generation}:asset:{asset_id}")
            if payload is None:
                return self._fallback.get_asset(asset_id)
            try:
                return asset_from_query_json(payload)
            except (TypeError, ValueError) as exc:
                raise CorruptQueryProjection("invalid cached Asset") from exc

        return self._cached(read, lambda: self._fallback.get_asset(asset_id))

    def _assets_for_ids(self, client, generation: str, asset_ids) -> list:
        ordered_ids = sorted(str(asset_id) for asset_id in asset_ids)
        if not ordered_ids:
            return []
        payloads = client.mget(
            [f"{generation}:asset:{asset_id}" for asset_id in ordered_ids]
        )
        if any(payload is None for payload in payloads):
            raise CorruptQueryProjection("asset index references a missing payload")
        try:
            return [asset_from_query_json(payload) for payload in payloads]
        except (TypeError, ValueError) as exc:
            raise CorruptQueryProjection("invalid cached Asset") from exc

    def get_assets_by_name(self, name: str) -> list:
        def read(client, generation: str, _source_revision: int):
            ids = client.smembers(f"{generation}:asset-name:{self._index_token(name)}")
            if not ids:
                return self._fallback.get_assets_by_name(name)
            return self._assets_for_ids(client, generation, ids)

        return self._cached(read, lambda: self._fallback.get_assets_by_name(name))

    def get_assets_by_definition(self, definition_hash: str) -> list:
        def read(client, generation: str, _source_revision: int):
            ids = client.smembers(
                f"{generation}:asset-definition:{self._index_token(definition_hash)}"
            )
            if not ids:
                return self._fallback.get_assets_by_definition(definition_hash)
            return self._assets_for_ids(client, generation, ids)

        return self._cached(
            read,
            lambda: self._fallback.get_assets_by_definition(definition_hash),
        )

    def get_all_assets(self) -> list:
        def read(client, generation: str, _source_revision: int):
            ids = client.smembers(f"{generation}:assets")
            meta = client.hgetall(f"{generation}:meta")
            if len(ids) != int(meta.get("asset_count", "-1")):
                raise CorruptQueryProjection("asset index count mismatch")
            return self._assets_for_ids(client, generation, ids)

        return self._cached(read, self._fallback.get_all_assets)

    def get_contract(self, contract_id: str):
        def read(client, generation: str, _source_revision: int):
            payload = client.get(f"{generation}:contract:{contract_id}")
            if payload is None:
                return self._fallback.get_contract(contract_id)
            try:
                return contract_from_query_json(payload)
            except (TypeError, ValueError) as exc:
                raise CorruptQueryProjection("invalid cached Contract") from exc

        return self._cached(read, lambda: self._fallback.get_contract(contract_id))

    def get_all_contracts(self) -> list:
        def read(client, generation: str, _source_revision: int):
            ids = sorted(client.smembers(f"{generation}:contracts"))
            meta = client.hgetall(f"{generation}:meta")
            if len(ids) != int(meta.get("contract_count", "-1")):
                raise CorruptQueryProjection("contract index count mismatch")
            if not ids:
                return []
            payloads = client.mget(
                [f"{generation}:contract:{contract_id}" for contract_id in ids]
            )
            if any(payload is None for payload in payloads):
                raise CorruptQueryProjection(
                    "contract index references a missing payload"
                )
            try:
                return [contract_from_query_json(payload) for payload in payloads]
            except (TypeError, ValueError) as exc:
                raise CorruptQueryProjection("invalid cached Contract") from exc

        return self._cached(read, self._fallback.get_all_contracts)

    def _graph_values(self, client, generation: str, kind: str) -> list[dict]:
        ids = sorted(client.smembers(f"{generation}:{kind}s"))
        meta = client.hgetall(f"{generation}:meta")
        if len(ids) != int(meta.get(f"{kind}_count", "-1")):
            raise CorruptQueryProjection(f"{kind} index count mismatch")
        if not ids:
            return []
        payloads = client.mget(
            [f"{generation}:{kind}:{object_id}" for object_id in ids]
        )
        if any(payload is None for payload in payloads):
            raise CorruptQueryProjection(f"{kind} index references a missing payload")
        try:
            values = [json.loads(payload) for payload in payloads]
        except (TypeError, ValueError) as exc:
            raise CorruptQueryProjection(f"invalid cached {kind}") from exc
        if not all(isinstance(value, dict) for value in values):
            raise CorruptQueryProjection(f"cached {kind} must be an object")
        return values

    def get_content_objects(self) -> list[dict]:
        return self._cached(
            lambda client, generation, _source_revision: self._graph_values(
                client, generation, "content"
            ),
            self._fallback.get_content_objects,
        )

    def get_asset_definitions(self) -> list[dict]:
        return self._cached(
            lambda client, generation, _source_revision: self._graph_values(
                client, generation, "definition"
            ),
            self._fallback.get_asset_definitions,
        )

    def get_definition_content_assertions(
        self, *, definition_id: str = "", content_id: str = ""
    ) -> list[dict]:
        def read(client, generation: str, _source_revision: int):
            values = self._graph_values(client, generation, "assertion")
            return [
                value
                for value in values
                if (not definition_id or value["definition_id"] == definition_id)
                and (not content_id or value["content_id"] == content_id)
            ]

        return self._cached(
            read,
            lambda: self._fallback.get_definition_content_assertions(
                definition_id=definition_id, content_id=content_id
            ),
        )

    def memoize_json(
        self,
        view_name: str,
        identity: str,
        build: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Memoize one revision-bound read view without making it truth."""

        def read(client, generation: str, source_revision: int):
            revision = source_revision
            key = (
                f"{generation}:view:{revision}:"
                f"{self._index_token(view_name)}:{self._index_token(identity)}"
            )
            payload = client.get(key)
            if payload is not None:
                try:
                    value = json.loads(payload)
                except (TypeError, ValueError) as exc:
                    raise CorruptQueryProjection("invalid cached JSON view") from exc
                if not isinstance(value, dict):
                    raise CorruptQueryProjection("cached JSON view must be an object")
                return value
            if self._store.get_runtime_revision() != revision:
                raise _RetryQueryRead("source changed before JSON view build")
            value = build()
            if self._store.get_runtime_revision() != revision:
                raise _RetryQueryRead("source changed during JSON view build")
            client.set(
                key,
                json.dumps(
                    value,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            return value

        return self._cached(read, build)

    def status(self) -> dict[str, object]:
        """Return observable cache state without triggering rebuild."""
        authoritative_revision = self._store.get_runtime_revision()
        try:
            active = self._active_metadata()
        except self._redis_errors:
            return {
                "authoritative_revision": authoritative_revision,
                "available": False,
                "backend": "redis",
                "configured": True,
                "current": False,
                "reason": "redis_unavailable" if self._strict else "unavailable",
            }
        if active is None:
            return {
                "authoritative_revision": authoritative_revision,
                "available": True,
                "backend": "redis",
                "configured": True,
                "current": False,
                "reason": "projection_missing_or_invalid",
            }
        generation, meta = active
        projection_revision = int(meta["revision"])
        return {
            "active_generation": generation.rsplit(":", 1)[-1],
            "authoritative_revision": authoritative_revision,
            "available": True,
            "backend": "redis",
            "configured": True,
            "current": projection_revision == authoritative_revision,
            "projection_revision": projection_revision,
            "reason": "",
            "schema": QUERY_PROJECTION_SCHEMA,
        }
