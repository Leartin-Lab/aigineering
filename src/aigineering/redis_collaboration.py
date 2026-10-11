"""Redis collaboration index and wake stream derived from SQLite RuntimeRecords."""

from __future__ import annotations

import json
import math
import time
from typing import Any

from aigineering.core.ids import compute_content_hash
from aigineering.core.session import (
    runtime_record_contract_references,
    runtime_record_worker_references,
)
from aigineering.adapters.redis_query import QueryProjectionUnavailable

COLLABORATION_SCHEMA = "v2"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"

_APPEND_EVENT = """
local current = tonumber(redis.call('HGET', KEYS[2], 'watermark') or '0')
local target = tonumber(ARGV[1])
if current >= target then
    return 0
end
if current ~= tonumber(ARGV[7]) then
    return -1
end
redis.call('XADD', KEYS[1], ARGV[1] .. '-0',
    'revision', ARGV[1],
    'record_id', ARGV[2],
    'record_type', ARGV[3],
    'contract_ids', ARGV[4],
    'worker_ids', ARGV[5])
redis.call('HSET', KEYS[2], 'watermark', ARGV[1], 'schema', ARGV[6])
return 1
"""

_READ_STATE = """
local watermark = redis.call('HGET', KEYS[2], 'watermark') or '0'
local schema = redis.call('HGET', KEYS[2], 'schema') or ''
local tail = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
local tail_id = ''
if #tail > 0 then tail_id = tail[1][1] end
return {watermark, schema, tail_id}
"""
_CLEAR_IF_STATE = """
local watermark = redis.call('HGET', KEYS[2], 'watermark') or '0'
local schema = redis.call('HGET', KEYS[2], 'schema') or ''
local tail = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
local tail_id = ''
if #tail > 0 then tail_id = tail[1][1] end
if watermark ~= ARGV[1] or schema ~= ARGV[2] or tail_id ~= ARGV[3] then
    return 0
end
redis.call('DEL', KEYS[1], KEYS[2])
return 1
"""


class RedisCollaborationUnavailable(QueryProjectionUnavailable):
    """Required Redis collaboration service is unavailable or inconsistent."""


def _record_contract_ids(record, records_by_id=None) -> tuple[str, ...]:
    """Reuse the typed fact-reference resolver; never traverse user content."""
    return tuple(
        sorted(
            reference
            for reference in runtime_record_contract_references(record, records_by_id)
            if reference.startswith("task:v")
        )
    )


def _record_worker_ids(record, records_by_id=None) -> tuple[str, ...]:
    """Return only typed Worker references from the shared fact reader."""
    return tuple(sorted(runtime_record_worker_references(record, records_by_id)))


def _decode_text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class RedisCollaborationService:
    """Idempotently relay committed SQLite record references to a Redis Stream."""

    def __init__(
        self,
        store,
        client,
        *,
        domain_id: str | None,
        redis_errors: tuple[type[BaseException], ...],
    ) -> None:
        self._store = store
        self._client = client
        self._domain_id = domain_id
        self._redis_errors = redis_errors
        domain_token = compute_content_hash(domain_id or "domain_uninitialized")
        self._root = f"aig:collaboration:{COLLABORATION_SCHEMA}:{domain_token}"
        self.stream_key = f"{self._root}:events"
        self.metadata_key = f"{self._root}:meta"

    def _require_domain(self) -> str:
        if self._domain_id is None:
            raise RedisCollaborationUnavailable(
                "Redis collaboration domain_uninitialized; run 'aig domain init'"
            )
        return self._domain_id

    def sync(self) -> dict[str, int | bool]:
        """Relay all authoritative revisions through the current SQLite watermark."""
        self._require_domain()
        try:
            for _attempt in range(16):
                authoritative = int(self._store.get_runtime_revision())
                watermark, schema, tail_id = self._state_snapshot()
                if schema not in ("", COLLABORATION_SCHEMA):
                    raise RedisCollaborationUnavailable(
                        "Redis collaboration schema mismatch"
                    )
                if watermark < 0:
                    raise RedisCollaborationUnavailable(
                        "Redis collaboration watermark is inconsistent"
                    )
                consistent_tail = (
                    not watermark and not tail_id
                ) or tail_id == f"{watermark}-0"
                if not consistent_tail:
                    if not self._clear_if_state(watermark, schema, tail_id):
                        continue
                    continue
                if watermark >= authoritative:
                    return {
                        "authoritative_revision": authoritative,
                        "watermark": watermark,
                        "lag": 0,
                        "synced": True,
                    }
                revisions = self._store.scan_runtime_records(
                    after_revision=watermark,
                    through_revision=authoritative,
                )
                records_by_id = {
                    record.id: record
                    for _, record in self._store.scan_runtime_records(
                        through_revision=authoritative
                    )
                }
                expected = watermark
                for revision, record in revisions:
                    if int(revision) <= expected:
                        raise RedisCollaborationUnavailable(
                            "SQLite runtime revisions are inconsistent"
                        )
                    event_contracts = json.dumps(
                        _record_contract_ids(record, records_by_id),
                        separators=(",", ":"),
                    )
                    event_workers = json.dumps(
                        _record_worker_ids(record, records_by_id),
                        separators=(",", ":"),
                    )
                    result = int(
                        self._client.eval(
                            _APPEND_EVENT,
                            2,
                            self.stream_key,
                            self.metadata_key,
                            str(revision),
                            record.id,
                            record.record_type,
                            event_contracts,
                            event_workers,
                            COLLABORATION_SCHEMA,
                            str(expected),
                        )
                    )
                    if result < 0:
                        break
                    expected = int(revision)
                else:
                    final_watermark, final_schema, _ = self._state_snapshot()
                    if final_schema not in ("", COLLABORATION_SCHEMA):
                        raise RedisCollaborationUnavailable(
                            "Redis collaboration schema mismatch"
                        )
                    if final_watermark == 0:
                        self._client.hset(
                            self.metadata_key,
                            mapping={"watermark": "0", "schema": COLLABORATION_SCHEMA},
                        )
                    return {
                        "authoritative_revision": authoritative,
                        "watermark": final_watermark,
                        "lag": max(0, authoritative - final_watermark),
                        "synced": final_watermark >= authoritative,
                    }
            raise RedisCollaborationUnavailable("Redis collaboration sync contention")
        except self._redis_errors as exc:
            raise RedisCollaborationUnavailable(
                "Redis collaboration service unavailable"
            ) from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise RedisCollaborationUnavailable(
                "Redis collaboration metadata is invalid"
            ) from exc

    def _state_snapshot(self) -> tuple[int, str, str]:
        values = self._client.eval(_READ_STATE, 2, self.stream_key, self.metadata_key)
        if not isinstance(values, (list, tuple)) or len(values) != 3:
            raise RedisCollaborationUnavailable(
                "Redis collaboration metadata is invalid"
            )
        watermark = int(_decode_text(values[0]))
        schema = _decode_text(values[1])
        tail_id = _decode_text(values[2])
        return watermark, schema, tail_id

    def _clear_if_state(self, watermark: int, schema: str, tail_id: str) -> bool:
        return bool(
            self._client.eval(
                _CLEAR_IF_STATE,
                2,
                self.stream_key,
                self.metadata_key,
                str(watermark),
                schema,
                tail_id,
            )
        )

    def rebuild(self) -> dict[str, int | bool]:
        """Recreate this domain's event projection from immutable SQLite records."""
        self._require_domain()
        try:
            for _attempt in range(16):
                watermark, schema, tail_id = self._state_snapshot()
                if self._clear_if_state(watermark, schema, tail_id):
                    return self.sync()
            raise RedisCollaborationUnavailable(
                "Redis collaboration rebuild contention"
            )
        except self._redis_errors as exc:
            raise RedisCollaborationUnavailable(
                "Redis collaboration service unavailable"
            ) from exc
        except (TypeError, ValueError, OverflowError) as exc:
            raise RedisCollaborationUnavailable(
                "Redis collaboration metadata is invalid"
            ) from exc

    def events(
        self,
        after_revision: int = 0,
        count: int = 100,
        block_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return sanitized stream events with revision greater than the cursor."""
        if (
            isinstance(after_revision, bool)
            or not isinstance(after_revision, int)
            or after_revision < 0
        ):
            raise ValueError("after_revision must be a non-negative integer")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be a positive integer")
        if block_ms is not None and (
            isinstance(block_ms, bool) or not isinstance(block_ms, int) or block_ms < 0
        ):
            raise ValueError("block_ms must be a non-negative integer")
        snapshot = self.sync()
        max_revision = int(snapshot["authoritative_revision"])
        try:
            response = self._client.xread(
                {self.stream_key: f"{after_revision}-0"},
                count=count,
                block=block_ms,
            )
            return [
                event
                for event in self._parse_stream_response(response)
                if event["revision"] <= max_revision
            ]
        except self._redis_errors as exc:
            raise RedisCollaborationUnavailable(
                "Redis collaboration service unavailable"
            ) from exc

    def wait(self, after_revision: int, timeout: float) -> list[dict[str, Any]]:
        """Wait for a bounded period for a committed-record wake event."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("timeout must be a finite non-negative number")
        deadline = time.monotonic() + float(timeout)
        while True:
            events = self.events(after_revision=after_revision, count=100)
            if events or time.monotonic() >= deadline:
                return events
            block = max(1, min(250, int((deadline - time.monotonic()) * 1000)))
            events = self.events(
                after_revision=after_revision,
                count=100,
                block_ms=block,
            )
            if events or time.monotonic() >= deadline:
                return events

    def status(self) -> dict[str, object]:
        """Return health and revision lag without exposing connection details."""
        revision = int(self._store.get_runtime_revision())
        if self._domain_id is None:
            try:
                available = bool(self._client.ping())
            except self._redis_errors + (RedisCollaborationUnavailable,):
                available = False
            return {
                "available": available,
                "backend": "redis",
                "configured": True,
                "current": False,
                "authoritative_revision": revision,
                "watermark": 0,
                "lag": revision,
                "reason": "domain_uninitialized",
                "schema": COLLABORATION_SCHEMA,
            }
        try:
            if not self._client.ping():
                raise RedisCollaborationUnavailable(
                    "Redis collaboration service unavailable"
                )
            watermark, schema, tail_id = self._state_snapshot()
            tail_valid = (watermark == 0 and not tail_id) or tail_id == f"{watermark}-0"
            current = watermark >= revision and tail_valid
            if schema and schema != COLLABORATION_SCHEMA:
                return {
                    "available": True,
                    "backend": "redis",
                    "configured": True,
                    "current": False,
                    "authoritative_revision": revision,
                    "watermark": watermark,
                    "lag": max(0, revision - watermark),
                    "reason": "schema_mismatch",
                    "schema": schema,
                }
            lag = max(0, revision - watermark)
            return {
                "available": True,
                "backend": "redis",
                "configured": True,
                "current": current,
                "authoritative_revision": revision,
                "watermark": watermark,
                "lag": lag,
                "reason": "stream_missing"
                if not tail_valid
                else ("revision_lag" if lag else ""),
                "schema": schema or COLLABORATION_SCHEMA,
            }
        except self._redis_errors:
            return {
                "available": False,
                "backend": "redis",
                "configured": True,
                "current": False,
                "authoritative_revision": revision,
                "watermark": 0,
                "lag": revision,
                "reason": "redis_unavailable",
                "schema": COLLABORATION_SCHEMA,
            }
        except RedisCollaborationUnavailable:
            return {
                "available": False,
                "backend": "redis",
                "configured": True,
                "current": False,
                "authoritative_revision": revision,
                "watermark": 0,
                "lag": revision,
                "reason": "redis_unavailable",
                "schema": COLLABORATION_SCHEMA,
            }
        except (TypeError, ValueError, OverflowError):
            return {
                "available": True,
                "backend": "redis",
                "configured": True,
                "current": False,
                "authoritative_revision": revision,
                "watermark": 0,
                "lag": revision,
                "reason": "metadata_invalid",
                "schema": COLLABORATION_SCHEMA,
            }

    def _parse_stream_response(self, response) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for _stream, entries in response or ():
            for event_id, fields in entries:
                try:
                    revision = int(fields["revision"])
                    decoded_id = _decode_text(event_id)
                    record_id = _decode_text(fields["record_id"])
                    record_type = _decode_text(fields["record_type"])
                    contracts = json.loads(_decode_text(fields["contract_ids"]))
                    workers = json.loads(_decode_text(fields["worker_ids"]))
                    if (
                        revision < 1
                        or decoded_id != f"{revision}-0"
                        or not record_id
                        or not record_type
                    ):
                        raise ValueError("invalid event metadata")
                    if not isinstance(contracts, list) or not all(
                        isinstance(item, str) and item.startswith("task:v")
                        for item in contracts
                    ):
                        raise ValueError("invalid Contract references")
                    if (
                        not isinstance(workers, list)
                        or workers != sorted(set(workers))
                        or not all(isinstance(item, str) and item for item in workers)
                    ):
                        raise ValueError("invalid Worker references")
                    events.append(
                        {
                            "stream_id": decoded_id,
                            "revision": revision,
                            "record_id": record_id,
                            "record_type": record_type,
                            "contract_ids": tuple(contracts),
                            "worker_ids": tuple(workers),
                        }
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise RedisCollaborationUnavailable(
                        "Redis collaboration event is invalid"
                    ) from exc
        return events
