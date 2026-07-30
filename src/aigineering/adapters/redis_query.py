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
    return 1
end
return 0
"""

_logger = logging.getLogger(__name__)


class CorruptQueryProjection(ValueError):
    """A cache generation is structurally incomplete or invalid."""


@lru_cache(maxsize=8)
def _client_from_url(redis_url: str):
    import redis

    return redis.Redis.from_url(redis_url, decode_responses=True)


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
    ) -> None:
        self._store = store
        self._fallback = StoreQueryProjection(store)
        self._client = client
        self._domain_id = domain_id
        self._redis_errors = redis_errors
        self._on_degraded = on_degraded or self._log_degraded
        domain_key = compute_content_hash(domain_id)
        self._root = f"aig:q:{QUERY_PROJECTION_SCHEMA}:{domain_key}"

    @classmethod
    def from_url(cls, store, *, domain_id: str, redis_url: str):
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
        """Atomically publish a complete generation derived from SQLite."""
        snapshot = build_query_snapshot(self._store, domain_id=self._domain_id)
        generation = self._generation_root(snapshot.digest)
        pipeline = self._client.pipeline(transaction=True)

        for asset_id, payload in snapshot.assets:
            pipeline.set(f"{generation}:asset:{asset_id}", payload)
            pipeline.sadd(f"{generation}:assets", asset_id)
        for name, asset_ids in snapshot.asset_names:
            key = f"{generation}:asset-name:{self._index_token(name)}"
            if asset_ids:
                pipeline.sadd(key, *asset_ids)
        for definition, asset_ids in snapshot.asset_definitions:
            key = f"{generation}:asset-definition:{self._index_token(definition)}"
            if asset_ids:
                pipeline.sadd(key, *asset_ids)
        for contract_id, payload in snapshot.contracts:
            pipeline.set(f"{generation}:contract:{contract_id}", payload)
            pipeline.sadd(f"{generation}:contracts", contract_id)
        pipeline.hset(
            f"{generation}:meta",
            mapping={
                "digest": snapshot.digest,
                "domain_id": snapshot.domain_id,
                "asset_count": str(len(snapshot.assets)),
                "contract_count": str(len(snapshot.contracts)),
                "revision": str(snapshot.revision),
                "schema": QUERY_PROJECTION_SCHEMA,
                "status": "ready",
            },
        )
        pipeline.set(self.active_key, snapshot.digest)
        pipeline.execute()
        return snapshot

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

    def catch_up(self, generation: str, *, after_revision: int) -> int:
        """Apply immutable entity records and advance one monotonic watermark."""
        records = self._store.scan_runtime_records(after_revision=after_revision)
        if not records:
            return after_revision
        pipeline = self._client.pipeline(transaction=True)
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
        target_revision = int(records[-1][0])
        pipeline.eval(
            _ADVANCE_REVISION,
            3,
            f"{generation}:meta",
            f"{generation}:assets",
            f"{generation}:contracts",
            str(target_revision),
        )
        pipeline.execute()
        return target_revision

    def _current_generation(self) -> str:
        active = self._active_metadata()
        authoritative_revision = self._store.get_runtime_revision()
        if active is not None:
            generation, meta = active
            cached_revision = int(meta["revision"])
            if cached_revision == authoritative_revision:
                return generation
            if cached_revision < authoritative_revision:
                applied = self.catch_up(
                    generation,
                    after_revision=cached_revision,
                )
                refreshed = self._client.hgetall(f"{generation}:meta")
                if (
                    applied == authoritative_revision
                    and int(refreshed.get("revision", "-1")) == authoritative_revision
                ):
                    return generation
        return self._generation_root(self.rebuild().digest)

    def _cached(self, operation: Callable[[str], Any], fallback: Callable[[], Any]):
        try:
            return operation(self._current_generation())
        except self._redis_errors as exc:
            self._on_degraded(exc)
            return fallback()
        except CorruptQueryProjection:
            try:
                generation = self._generation_root(self.rebuild().digest)
                return operation(generation)
            except self._redis_errors as exc:
                self._on_degraded(exc)
                return fallback()
            except CorruptQueryProjection:
                return fallback()

    def get_asset(self, asset_id: str):
        def read(generation: str):
            payload = self._client.get(f"{generation}:asset:{asset_id}")
            if payload is None:
                return self._fallback.get_asset(asset_id)
            try:
                return asset_from_query_json(payload)
            except (TypeError, ValueError) as exc:
                raise CorruptQueryProjection("invalid cached Asset") from exc

        return self._cached(read, lambda: self._fallback.get_asset(asset_id))

    def _assets_for_ids(self, generation: str, asset_ids) -> list:
        ordered_ids = sorted(str(asset_id) for asset_id in asset_ids)
        if not ordered_ids:
            return []
        payloads = self._client.mget(
            [f"{generation}:asset:{asset_id}" for asset_id in ordered_ids]
        )
        if any(payload is None for payload in payloads):
            raise CorruptQueryProjection("asset index references a missing payload")
        try:
            return [asset_from_query_json(payload) for payload in payloads]
        except (TypeError, ValueError) as exc:
            raise CorruptQueryProjection("invalid cached Asset") from exc

    def get_assets_by_name(self, name: str) -> list:
        def read(generation: str):
            ids = self._client.smembers(
                f"{generation}:asset-name:{self._index_token(name)}"
            )
            if not ids:
                return self._fallback.get_assets_by_name(name)
            return self._assets_for_ids(generation, ids)

        return self._cached(read, lambda: self._fallback.get_assets_by_name(name))

    def get_assets_by_definition(self, definition_hash: str) -> list:
        def read(generation: str):
            ids = self._client.smembers(
                f"{generation}:asset-definition:{self._index_token(definition_hash)}"
            )
            if not ids:
                return self._fallback.get_assets_by_definition(definition_hash)
            return self._assets_for_ids(generation, ids)

        return self._cached(
            read,
            lambda: self._fallback.get_assets_by_definition(definition_hash),
        )

    def get_all_assets(self) -> list:
        def read(generation: str):
            ids = self._client.smembers(f"{generation}:assets")
            meta = self._client.hgetall(f"{generation}:meta")
            if len(ids) != int(meta.get("asset_count", "-1")):
                raise CorruptQueryProjection("asset index count mismatch")
            return self._assets_for_ids(generation, ids)

        return self._cached(read, self._fallback.get_all_assets)

    def get_contract(self, contract_id: str):
        def read(generation: str):
            payload = self._client.get(f"{generation}:contract:{contract_id}")
            if payload is None:
                return self._fallback.get_contract(contract_id)
            try:
                return contract_from_query_json(payload)
            except (TypeError, ValueError) as exc:
                raise CorruptQueryProjection("invalid cached Contract") from exc

        return self._cached(read, lambda: self._fallback.get_contract(contract_id))

    def get_all_contracts(self) -> list:
        def read(generation: str):
            ids = sorted(self._client.smembers(f"{generation}:contracts"))
            meta = self._client.hgetall(f"{generation}:meta")
            if len(ids) != int(meta.get("contract_count", "-1")):
                raise CorruptQueryProjection("contract index count mismatch")
            if not ids:
                return []
            payloads = self._client.mget(
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

    def memoize_json(
        self,
        view_name: str,
        identity: str,
        build: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Memoize one revision-bound read view without making it truth."""

        def read(generation: str):
            meta = self._client.hgetall(f"{generation}:meta")
            revision = int(meta["revision"])
            key = (
                f"{generation}:view:{revision}:"
                f"{self._index_token(view_name)}:{self._index_token(identity)}"
            )
            payload = self._client.get(key)
            if payload is not None:
                try:
                    value = json.loads(payload)
                except (TypeError, ValueError) as exc:
                    raise CorruptQueryProjection("invalid cached JSON view") from exc
                if not isinstance(value, dict):
                    raise CorruptQueryProjection("cached JSON view must be an object")
                return value
            value = build()
            self._client.set(
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
