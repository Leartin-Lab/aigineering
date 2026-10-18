"""Disposable Redis query projection over authoritative Store facts."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from aigineering.core.ids import canonical_json, compute_content_hash
from aigineering.core.query_projection import (
    QUERY_PROJECTION_SCHEMA,
    QuerySnapshot,
    StoreQueryProjection,
    asset_from_query_json,
    build_query_snapshot,
    contract_from_query_json,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import runtime_record_effective_payload

_PUBLISH_QUERY = """
redis.call('HSET', KEYS[1], 'status', 'repairing')
local deletes = cjson.decode(ARGV[3])
local batch = {}
for i = 1, #deletes do
    local key = deletes[i]
    if string.sub(key, 1, string.len(KEYS[3]) + 1) == KEYS[3] .. ':' and key ~= KEYS[1] then
        batch[#batch + 1] = key
        if #batch == 128 then
            redis.call('DEL', unpack(batch))
            batch = {}
        end
    end
end
if #batch > 0 then redis.call('DEL', unpack(batch)) end
local operations = cjson.decode(ARGV[1])
for i = 1, #operations do
    local operation = operations[i]
    if operation[1] == 'SET' then
        redis.call('SET', operation[2], operation[3])
    elseif operation[1] == 'SADD' then
        redis.call('SADD', operation[2], operation[3])
    else
        return redis.error_reply('unsupported query projection operation')
    end
end
local metadata = cjson.decode(ARGV[2])
for field, value in pairs(metadata) do
    if field ~= 'status' then redis.call('HSET', KEYS[1], field, value) end
end
if ARGV[6] == 'counts' then
    redis.call('HSET', KEYS[1], 'asset_count', redis.call('SCARD', KEYS[4]))
    redis.call('HSET', KEYS[1], 'contract_count', redis.call('SCARD', KEYS[5]))
    redis.call('HSET', KEYS[1], 'content_count', redis.call('SCARD', KEYS[6]))
    redis.call('HSET', KEYS[1], 'definition_count', redis.call('SCARD', KEYS[7]))
    redis.call('HSET', KEYS[1], 'assertion_count', redis.call('SCARD', KEYS[8]))
end
redis.call('HSET', KEYS[1], 'status', 'ready')
if ARGV[4] == '1' then redis.call('SET', KEYS[2], ARGV[5]) end
return 1
"""

_logger = logging.getLogger(__name__)
_MAX_QUERY_RETRIES = 3


class _RetryQueryRead(RuntimeError):
    """A bounded query attempt observed a source or Redis fence change."""


def _is_watch_error(exc: BaseException) -> bool:
    return any(base.__name__.endswith("WatchError") for base in type(exc).__mro__)


def _is_response_error(exc: BaseException) -> bool:
    return any(base.__name__.endswith("ResponseError") for base in type(exc).__mro__)


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
        self._prefix_store = store
        self._prefix_tip_revision = 0
        self._prefix_tip_digest = self._prefix_seed()
        self._prefix_checkpoints = {0: self._prefix_tip_digest}

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

    def _prefix_seed(self) -> str:
        domain = self._domain_id.encode("utf-8")
        material = (
            b"aigineering-query-prefix-v1\0" + len(domain).to_bytes(8, "big") + domain
        )
        return hashlib.sha256(material).hexdigest()

    def _reset_prefix_cache(self) -> None:
        self._prefix_store = self._store
        self._prefix_tip_revision = 0
        self._prefix_tip_digest = self._prefix_seed()
        self._prefix_checkpoints = {0: self._prefix_tip_digest}

    def _prefix_fingerprint(self, through_revision: int) -> str:
        """Hash an exact ordered source prefix, retaining bounded real-row checkpoints."""
        if self._prefix_store is not self._store:
            self._reset_prefix_cache()
        source_revision = self._store.get_runtime_revision()
        if source_revision < self._prefix_tip_revision:
            self._reset_prefix_cache()
        if through_revision < 0 or source_revision < through_revision:
            raise QueryProjectionUnavailable("Redis query projection source changed")
        if through_revision == self._prefix_tip_revision:
            return self._prefix_tip_digest

        if through_revision > self._prefix_tip_revision:
            cursor = self._prefix_tip_revision
            digest = self._prefix_tip_digest
        else:
            cursor = max(
                (
                    revision
                    for revision in self._prefix_checkpoints
                    if revision <= through_revision
                ),
                default=0,
            )
            digest = self._prefix_checkpoints[cursor]

        last_record_revision = cursor
        window = 256
        window_end = cursor
        while window_end < through_revision:
            window_end = min(through_revision, window_end + window)
            records = self._store.scan_runtime_records(
                after_revision=last_record_revision,
                through_revision=window_end,
            )
            for revision, record in records:
                revision = int(revision)
                if revision <= last_record_revision or revision > window_end:
                    raise QueryProjectionUnavailable(
                        "Redis query projection source history is invalid"
                    )
                effective = canonical_json(
                    runtime_record_effective_payload(record)
                ).encode("utf-8")
                record_id = str(record.id).encode("utf-8")
                framed = (
                    bytes.fromhex(digest)
                    + revision.to_bytes(8, "big")
                    + len(record_id).to_bytes(8, "big")
                    + record_id
                    + len(effective).to_bytes(8, "big")
                    + effective
                )
                digest = hashlib.sha256(framed).hexdigest()
                last_record_revision = revision
            cursor = last_record_revision
            if cursor > 0 and (
                window_end % window == 0 or window_end == through_revision
            ):
                self._prefix_checkpoints[cursor] = digest
                if len(self._prefix_checkpoints) > 17:
                    retained = [0, *sorted(self._prefix_checkpoints)[-16:]]
                    self._prefix_checkpoints = {
                        revision: self._prefix_checkpoints[revision]
                        for revision in retained
                        if revision in self._prefix_checkpoints
                    }

        if through_revision > 0 and last_record_revision != through_revision:
            raise QueryProjectionUnavailable(
                "Redis query projection terminal source record is missing"
            )
        if through_revision == source_revision:
            self._prefix_tip_revision = through_revision
            self._prefix_tip_digest = digest
        return digest

    def _generation_root(self, digest: str) -> str:
        return f"{self._root}:gen:{digest}"

    @staticmethod
    def _index_token(value: str) -> str:
        return compute_content_hash(value)

    def rebuild(self, *, fresh_store_factory=None) -> QuerySnapshot:
        """Build one certified generation; routed replacement requires a fresh source."""
        try:
            return self._rebuild(fresh_store_factory=fresh_store_factory)
        except self._redis_errors as exc:
            message = (
                "Redis query projection publication failed"
                if _is_response_error(exc)
                else "Redis query projection unavailable"
            )
            raise QueryProjectionUnavailable(message) from exc

    def _snapshot_operations(
        self, snapshot: QuerySnapshot, generation: str
    ) -> list[list[str]]:
        operations: list[list[str]] = []

        def add(command: str, key: str, value: str) -> None:
            operations.append([command, key, value])

        for asset_id, payload in snapshot.assets:
            add("SET", f"{generation}:asset:{asset_id}", payload)
            add("SADD", f"{generation}:assets", asset_id)
        for name, asset_ids in snapshot.asset_names:
            key = f"{generation}:asset-name:{self._index_token(name)}"
            for asset_id in asset_ids:
                add("SADD", key, asset_id)
        for definition, asset_ids in snapshot.asset_definitions:
            key = f"{generation}:asset-definition:{self._index_token(definition)}"
            for asset_id in asset_ids:
                add("SADD", key, asset_id)
        for contract_id, payload in snapshot.contracts:
            add("SET", f"{generation}:contract:{contract_id}", payload)
            add("SADD", f"{generation}:contracts", contract_id)
        for content_id, payload in snapshot.contents:
            add("SET", f"{generation}:content:{content_id}", payload)
            add("SADD", f"{generation}:contents", content_id)
        for definition_id, payload in snapshot.signed_definitions:
            add("SET", f"{generation}:definition:{definition_id}", payload)
            add("SADD", f"{generation}:definitions", definition_id)
        for assertion_id, payload in snapshot.assertions:
            assertion = json.loads(payload)
            add("SET", f"{generation}:assertion:{assertion_id}", payload)
            add("SADD", f"{generation}:assertions", assertion_id)
            add(
                "SADD",
                f"{generation}:assertion-definition:{self._index_token(str(assertion['definition_id']))}",
                assertion_id,
            )
            add(
                "SADD",
                f"{generation}:assertion-content:{self._index_token(str(assertion['content_id']))}",
                assertion_id,
            )
        return operations

    @staticmethod
    def _metadata_json(meta: dict[str, str]) -> str:
        return json.dumps(meta, sort_keys=True, separators=(",", ":"))

    def _generation_keys(self, generation: str) -> list[str]:
        return sorted(
            {
                str(key)
                for key in self._client.scan_iter(match=f"{generation}:*")
                if str(key) != f"{generation}:meta"
            }
        )

    def _repair_metadata_revision(
        self,
        metadata: dict[str, str],
        *,
        digest: str,
        allow_nonready: bool,
        allow_initial_fence: bool = False,
    ) -> int | None:
        if not metadata:
            return None
        if (
            allow_initial_fence
            and allow_nonready
            and metadata == {"status": "repairing"}
        ):
            return None
        try:
            revision = int(metadata["revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QueryProjectionUnavailable(
                "Redis query projection metadata is invalid"
            ) from exc
        if (
            revision < 0
            or metadata.get("digest") != digest
            or metadata.get("domain_id") != self._domain_id
            or metadata.get("schema") != QUERY_PROJECTION_SCHEMA
            or not metadata.get("source_fingerprint")
            or (metadata.get("status") != "ready" and not allow_nonready)
        ):
            raise QueryProjectionUnavailable(
                "Redis query projection metadata is invalid"
            )
        return revision

    def _rebuild(self, *, fresh_store_factory=None) -> QuerySnapshot:
        """Publish a complete generation under pointer/meta WATCH and one Lua EXEC."""
        initial_store = self._store
        pending_store = None
        lost_repair_privilege = False
        for attempt in range(_MAX_QUERY_RETRIES):
            source_store = pending_store or initial_store
            pending_store = None
            self._store = source_store
            self._fallback = StoreQueryProjection(source_store)
            if self._prefix_store is not source_store:
                self._reset_prefix_cache()

            source_revision = source_store.get_runtime_revision()
            snapshot = build_query_snapshot(source_store, domain_id=self._domain_id)
            if (
                snapshot.revision != source_revision
                or source_store.get_runtime_revision() != source_revision
            ):
                if attempt + 1 < _MAX_QUERY_RETRIES:
                    pending_store = source_store
                    continue
                raise QueryProjectionUnavailable(
                    "Redis query projection source changed during capture"
                )
            source_fingerprint = self._prefix_fingerprint(source_revision)
            if source_store.get_runtime_revision() != source_revision:
                if attempt + 1 < _MAX_QUERY_RETRIES:
                    pending_store = source_store
                    continue
                raise QueryProjectionUnavailable(
                    "Redis query projection source changed during capture"
                )

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
                    f"{active_generation}:meta" if active_generation else None
                )
                if active_meta_key is not None:
                    pipeline.watch(active_meta_key)
                active_meta = (
                    pipeline.hgetall(active_meta_key) if active_meta_key else {}
                )
                if target_meta_key != active_meta_key:
                    pipeline.watch(target_meta_key)
                target_meta = pipeline.hgetall(target_meta_key)

                # Freeze both Redis metadata fences before confirming routed source P.
                explicit_repair = (
                    fresh_store_factory is not None and not lost_repair_privilege
                )
                if fresh_store_factory is not None:
                    routed_store = fresh_store_factory()
                    from aigineering.core.domain import load_genesis

                    if load_genesis(routed_store).id != self._domain_id:
                        raise QueryProjectionUnavailable(
                            "Routed query source belongs to a different domain"
                        )
                    self._store = routed_store
                    self._fallback = StoreQueryProjection(routed_store)
                    self._reset_prefix_cache()
                    routed_revision = routed_store.get_runtime_revision()
                    routed_fingerprint = self._prefix_fingerprint(routed_revision)
                    if routed_store.get_runtime_revision() != routed_revision:
                        if attempt + 1 < _MAX_QUERY_RETRIES:
                            pending_store = routed_store
                            pipeline.multi()
                            pipeline.execute()
                            continue
                        raise QueryProjectionUnavailable(
                            "Routed query source changed during capture"
                        )
                    if (
                        routed_revision != source_revision
                        or routed_fingerprint != source_fingerprint
                    ):
                        if attempt + 1 < _MAX_QUERY_RETRIES:
                            pending_store = routed_store
                            pipeline.multi()
                            pipeline.execute()
                            continue
                        raise QueryProjectionUnavailable(
                            "Routed query source changed during capture"
                        )
                    source_store = routed_store
                    source_revision = routed_revision
                    source_fingerprint = routed_fingerprint

                if active_digest and not active_meta:
                    raise QueryProjectionUnavailable(
                        "Redis query projection metadata is missing"
                    )
                active_revision = (
                    self._repair_metadata_revision(
                        active_meta,
                        digest=str(active_digest),
                        allow_nonready=explicit_repair,
                    )
                    if active_digest
                    else None
                )
                target_revision = self._repair_metadata_revision(
                    target_meta,
                    digest=snapshot.digest,
                    allow_nonready=explicit_repair,
                    allow_initial_fence=explicit_repair,
                )
                stale_revision = max(
                    (
                        revision
                        for revision in (active_revision, target_revision)
                        if revision is not None
                    ),
                    default=-1,
                )
                if stale_revision > source_revision and not explicit_repair:
                    raise QueryProjectionUnavailable(
                        "Redis query projection source is stale"
                    )
                for observed_meta, observed_revision in (
                    (active_meta, active_revision),
                    (target_meta, target_revision),
                ):
                    if (
                        observed_revision is not None
                        and observed_revision <= source_revision
                    ):
                        matches_source = self._prefix_fingerprint(
                            observed_revision
                        ) == observed_meta.get("source_fingerprint")
                        if not matches_source and not explicit_repair:
                            raise QueryProjectionUnavailable(
                                "Redis query projection source history diverged"
                            )

                delete_keys = self._generation_keys(generation)
                if (
                    source_store.get_runtime_revision() != source_revision
                    or self._prefix_fingerprint(source_revision) != source_fingerprint
                ):
                    if attempt + 1 < _MAX_QUERY_RETRIES:
                        pending_store = source_store
                        pipeline.multi()
                        pipeline.execute()
                        continue
                    raise QueryProjectionUnavailable(
                        "Redis query projection source changed during publication"
                    )
                metadata = {
                    "digest": snapshot.digest,
                    "domain_id": snapshot.domain_id,
                    "asset_count": str(len(snapshot.assets)),
                    "contract_count": str(len(snapshot.contracts)),
                    "content_count": str(len(snapshot.contents)),
                    "definition_count": str(len(snapshot.signed_definitions)),
                    "assertion_count": str(len(snapshot.assertions)),
                    "revision": str(snapshot.revision),
                    "schema": QUERY_PROJECTION_SCHEMA,
                    "source_fingerprint": source_fingerprint,
                    "status": "ready",
                }
                operations_json = json.dumps(
                    self._snapshot_operations(snapshot, generation),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                pipeline.multi()
                pipeline.eval(
                    _PUBLISH_QUERY,
                    3,
                    target_meta_key,
                    self.active_key,
                    generation,
                    operations_json,
                    self._metadata_json(metadata),
                    json.dumps(delete_keys, separators=(",", ":")),
                    "1",
                    snapshot.digest,
                    "full",
                )
                pipeline.execute()
                return snapshot
            except self._redis_errors as exc:
                if not _is_watch_error(exc):
                    raise
                lost_repair_privilege = True
                pending_store = pending_store or source_store
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
    ) -> int | None:
        if not meta:
            return None
        try:
            revision = int(meta["revision"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QueryProjectionUnavailable(
                "Redis query projection metadata is invalid"
            ) from exc
        if (
            revision < 0
            or not meta.get("source_fingerprint")
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
            or not meta.get("source_fingerprint")
        ):
            raise QueryProjectionUnavailable(
                "Redis query projection metadata is invalid"
            )
        try:
            int(meta.get("revision", "-1"))
        except (TypeError, ValueError) as exc:
            raise QueryProjectionUnavailable(
                "Redis query projection metadata is invalid"
            ) from exc
        return generation, meta

    @staticmethod
    def _record_payload(record) -> dict[str, Any]:
        return deep_thaw(record.payload)

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
                expected_before = self._prefix_fingerprint(after_revision)
                if meta.get("source_fingerprint") != expected_before:
                    raise QueryProjectionUnavailable(
                        "Redis query projection source history diverged"
                    )
                if current_revision != after_revision:
                    if (
                        current_revision > after_revision
                        and current_revision <= target_revision
                    ):
                        return current_revision
                    raise QueryProjectionUnavailable(
                        "Redis query projection generation changed during catch-up"
                    )

                operations: list[list[str]] = []

                def add(command: str, key: str, value: str) -> None:
                    operations.append([command, key, value])

                for _, record in records:
                    payload = self._record_payload(record)
                    if record.record_type == "asset.committed":
                        asset = payload.get("asset")
                        if isinstance(asset, dict):
                            asset_from_query_json(json.dumps(asset))
                            asset_id = str(asset["id"])
                            asset_json = json.dumps(
                                asset,
                                sort_keys=True,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            add("SET", f"{generation}:asset:{asset_id}", asset_json)
                            add("SADD", f"{generation}:assets", asset_id)
                            add(
                                "SADD",
                                f"{generation}:asset-name:{self._index_token(str(asset['name']))}",
                                asset_id,
                            )
                            add(
                                "SADD",
                                f"{generation}:asset-definition:{self._index_token(str(asset['definition_hash']))}",
                                asset_id,
                            )
                    elif record.record_type == "contract.declared":
                        contract = payload.get("contract")
                        if isinstance(contract, dict):
                            contract_from_query_json(json.dumps(contract))
                            contract_id = str(contract["id"])
                            contract_json = json.dumps(
                                contract,
                                sort_keys=True,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            add(
                                "SET",
                                f"{generation}:contract:{contract_id}",
                                contract_json,
                            )
                            add("SADD", f"{generation}:contracts", contract_id)
                source_fingerprint = self._prefix_fingerprint(target_revision)
                updated_meta = {
                    "revision": str(target_revision),
                    "source_fingerprint": source_fingerprint,
                }
                operations_json = json.dumps(
                    operations, ensure_ascii=False, separators=(",", ":")
                )
                pipeline.multi()
                pipeline.eval(
                    _PUBLISH_QUERY,
                    8,
                    meta_key,
                    self.active_key,
                    generation,
                    f"{generation}:assets",
                    f"{generation}:contracts",
                    f"{generation}:contents",
                    f"{generation}:definitions",
                    f"{generation}:assertions",
                    operations_json,
                    self._metadata_json(updated_meta),
                    "[]",
                    "0",
                    "",
                    "counts",
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
        expected_fingerprint = self._prefix_fingerprint(cached_revision)
        if meta.get("source_fingerprint") != expected_fingerprint:
            raise QueryProjectionUnavailable(
                "Redis query projection source history diverged"
            )
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
            if refreshed.get("source_fingerprint") != self._prefix_fingerprint(
                source_after
            ):
                raise QueryProjectionUnavailable(
                    "Redis query projection source history diverged"
                )
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
                    pipeline.watch(self.active_key)
                    active_digest = pipeline.get(self.active_key)
                    expected_digest = generation.rsplit(":gen:", 1)[-1]
                    if active_digest != expected_digest:
                        raise _RetryQueryRead("active query generation changed")
                    pipeline.watch(f"{generation}:meta")
                    meta = pipeline.hgetall(f"{generation}:meta")
                    cached_revision = self._fence_revision(
                        meta,
                        expected_digest=expected_digest,
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
                    expected_fingerprint = self._prefix_fingerprint(source_revision)
                    if meta.get("source_fingerprint") != expected_fingerprint:
                        raise QueryProjectionUnavailable(
                            "Redis query projection source history diverged"
                        )
                    value = operation(pipeline, generation, source_revision)
                    pipeline.multi()
                    pipeline.execute()
                    source_after = self._store.get_runtime_revision()
                    if source_after != source_revision:
                        raise _RetryQueryRead("source advanced during query read")
                    if self._prefix_fingerprint(source_after) != expected_fingerprint:
                        raise _RetryQueryRead(
                            "source history changed during query read"
                        )
                    return value
                finally:
                    pipeline.reset()
            except _RetryQueryRead:
                continue
            except self._redis_errors as exc:
                if _is_watch_error(exc):
                    continue
                if self._strict:
                    message = (
                        "Redis query projection publication failed"
                        if _is_response_error(exc)
                        else "Redis query projection unavailable"
                    )
                    raise QueryProjectionUnavailable(message) from exc
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
        """Memoize one view under the exact source-prefix certificate."""
        for _attempt in range(_MAX_QUERY_RETRIES):
            source_revision = self._store.get_runtime_revision()
            try:
                generation = self._current_generation()
                source_fingerprint = self._prefix_fingerprint(source_revision)
                pipeline = self._client.pipeline(transaction=True)
                try:
                    pipeline.watch(self.active_key)
                    active_digest = pipeline.get(self.active_key)
                    expected_digest = generation.rsplit(":gen:", 1)[-1]
                    if active_digest != expected_digest:
                        raise _RetryQueryRead("active query generation changed")
                    meta_key = f"{generation}:meta"
                    pipeline.watch(meta_key)
                    meta = pipeline.hgetall(meta_key)
                    cached_revision = self._fence_revision(
                        meta, expected_digest=expected_digest
                    )
                    if (
                        cached_revision != source_revision
                        or meta.get("source_fingerprint") != source_fingerprint
                    ):
                        raise _RetryQueryRead(
                            "source certificate changed during JSON read"
                        )
                    key = (
                        f"{generation}:view:{source_fingerprint}:{source_revision}:"
                        f"{self._index_token(view_name)}:{self._index_token(identity)}"
                    )
                    payload = pipeline.get(key)
                    built = payload is None
                    if built:
                        if self._store.get_runtime_revision() != source_revision:
                            raise _RetryQueryRead(
                                "source changed before JSON view build"
                            )
                        value = build()
                        if self._store.get_runtime_revision() != source_revision:
                            raise _RetryQueryRead(
                                "source changed during JSON view build"
                            )
                        if not isinstance(value, dict):
                            raise CorruptQueryProjection("JSON view must be an object")
                        encoded = json.dumps(
                            value,
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    else:
                        try:
                            value = json.loads(payload)
                        except (TypeError, ValueError) as exc:
                            raise CorruptQueryProjection(
                                "invalid cached JSON view"
                            ) from exc
                        if not isinstance(value, dict):
                            raise CorruptQueryProjection(
                                "cached JSON view must be an object"
                            )
                    pipeline.multi()
                    if built:
                        pipeline.set(key, encoded)
                        # Same-value HSET is the watched invalidation for SCAN cleanup.
                        pipeline.hset(
                            meta_key, mapping={"source_fingerprint": source_fingerprint}
                        )
                    pipeline.execute()
                    source_after = self._store.get_runtime_revision()
                    if source_after != source_revision:
                        raise _RetryQueryRead("source advanced during JSON read")
                    if self._prefix_fingerprint(source_after) != source_fingerprint:
                        raise _RetryQueryRead("source history changed during JSON read")
                    return value
                finally:
                    pipeline.reset()
            except _RetryQueryRead:
                continue
            except self._redis_errors as exc:
                if _is_watch_error(exc):
                    continue
                if self._strict:
                    message = (
                        "Redis query projection publication failed"
                        if _is_response_error(exc)
                        else "Redis query projection unavailable"
                    )
                    raise QueryProjectionUnavailable(message) from exc
                self._on_degraded(exc)
                return self._consistent_fallback(build)
            except (QueryProjectionUnavailable, CorruptQueryProjection):
                if self._strict:
                    raise
                self._on_degraded(ConnectionError("Redis query projection unavailable"))
                return self._consistent_fallback(build)
        if self._strict:
            raise QueryProjectionUnavailable(
                "Redis query projection changed during JSON read"
            )
        self._on_degraded(
            ConnectionError("Redis query projection changed during JSON read")
        )
        return self._consistent_fallback(build)

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
        except QueryProjectionUnavailable:
            return {
                "authoritative_revision": authoritative_revision,
                "available": True,
                "backend": "redis",
                "configured": True,
                "current": False,
                "reason": "projection_invalid",
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
        try:
            if self._prefix_fingerprint(projection_revision) != meta.get(
                "source_fingerprint"
            ):
                raise QueryProjectionUnavailable(
                    "Redis query projection source history diverged"
                )
        except QueryProjectionUnavailable:
            return {
                "active_generation": generation.rsplit(":", 1)[-1],
                "authoritative_revision": authoritative_revision,
                "available": True,
                "backend": "redis",
                "configured": True,
                "current": False,
                "projection_revision": projection_revision,
                "reason": "source_diverged",
                "schema": QUERY_PROJECTION_SCHEMA,
            }
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
