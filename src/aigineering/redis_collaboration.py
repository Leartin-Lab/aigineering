"""Redis collaboration index and wake stream derived from SQLite RuntimeRecords."""

from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from aigineering.core.ids import compute_content_hash
from aigineering.core.session import (
    RuntimeRecordReferenceContext,
    build_runtime_record_reference_context,
    runtime_record_contract_references,
    runtime_record_worker_references,
)
from aigineering.adapters.redis_query import QueryProjectionUnavailable

COLLABORATION_SCHEMA = "v2"
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"

_READ_FENCE = """
local st = redis.call('TYPE', KEYS[1]).ok
local mt = redis.call('TYPE', KEYS[2]).ok
local watermark = ''
local schema = ''
local tail_id = ''
if mt == 'hash' then
    watermark = redis.call('HGET', KEYS[2], 'watermark') or ''
    schema = redis.call('HGET', KEYS[2], 'schema') or ''
end
if st == 'stream' then
    local tail = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
    if #tail > 0 then tail_id = tail[1][1] end
end
return {st, mt, watermark, schema, tail_id}
"""

_SWAP_STAGED = """
local live_stream = KEYS[1]
local live_meta = KEYS[2]
local stage_stream = KEYS[3]
local stage_meta = KEYS[4]
local seen = {}
for _, key in ipairs(KEYS) do
    if seen[key] then return -1 end
    seen[key] = true
end
local function keytype(key) return redis.call('TYPE', key).ok end
local lst = keytype(live_stream)
local lmt = keytype(live_meta)
local sst = keytype(stage_stream)
local smt = keytype(stage_meta)
local repair = ARGV[10] == '1'
if not repair and (lst ~= 'none' and lst ~= 'stream') then return -2 end
if not repair and (lmt ~= 'none' and lmt ~= 'hash') then return -2 end
local target = ARGV[1]
local schema = ARGV[2]
local empty = ARGV[3] == '1'
local expected_count = tonumber(ARGV[9])
if not expected_count or expected_count < 0 then return -3 end
if empty then
    if sst ~= 'none' or smt ~= 'none' or target ~= '0' or expected_count ~= 0 then return -4 end
else
    if sst ~= 'stream' or smt ~= 'hash' then return -4 end
end
if not empty then
    local staged_watermark = redis.call('HGET', stage_meta, 'watermark') or ''
    local staged_schema = redis.call('HGET', stage_meta, 'schema') or ''
    if staged_watermark ~= target or staged_schema ~= schema then return -5 end
    if redis.call('XLEN', stage_stream) ~= expected_count then return -6 end
    local staged_tail = ''
    local stail = redis.call('XREVRANGE', stage_stream, '+', '-', 'COUNT', 1)
    if #stail > 0 then staged_tail = stail[1][1] end
    if staged_tail ~= target .. '-0' then return -7 end
end
local wm = ''
local sch = ''
local tail = ''
if lmt == 'hash' then
    wm = redis.call('HGET', live_meta, 'watermark') or ''
    sch = redis.call('HGET', live_meta, 'schema') or ''
end
if lst == 'stream' then
    local ltail = redis.call('XREVRANGE', live_stream, '+', '-', 'COUNT', 1)
    if #ltail > 0 then tail = ltail[1][1] end
end
if lst ~= ARGV[4] or lmt ~= ARGV[5] or wm ~= ARGV[6] or sch ~= ARGV[7] or tail ~= ARGV[8] then return -8 end
-- Every operand and required stage invariant has been checked before the first live write.
if empty then
    redis.call('DEL', live_stream, live_meta)
    return 1
end
redis.call('PERSIST', stage_stream)
redis.call('PERSIST', stage_meta)
redis.call('RENAME', stage_stream, live_stream)
redis.call('RENAME', stage_meta, live_meta)
return 1
"""

_MAX_APPEND_EVENTS = 256
_MAX_APPEND_BYTES = 1_048_576
_XRANGE_PAGE_SIZE = 256
_STAGE_TTL_SECONDS = 600
_MAX_WATCH_ATTEMPTS = 3


class RedisCollaborationUnavailable(QueryProjectionUnavailable):
    """Required Redis collaboration service is unavailable or inconsistent."""

    def __init__(self, message: str, *, reason: str = "redis_unavailable") -> None:
        super().__init__(message)
        self.reason = reason


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
    if isinstance(records_by_id, RuntimeRecordReferenceContext):
        records_by_id = records_by_id.records_by_id
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

    def _records(self, store, through: int | None = None):
        revision = int(store.get_runtime_revision())
        if through is not None:
            revision = min(revision, through)
        rows = store.scan_runtime_records(through_revision=revision)
        rows = [(int(row_revision), record) for row_revision, record in rows]
        if any(left[0] >= right[0] for left, right in zip(rows, rows[1:])):
            raise RedisCollaborationUnavailable(
                "SQLite runtime revisions are inconsistent",
                reason="source_inconsistent",
            )
        by_id = {record.id: record for _, record in rows}
        return revision, rows, by_id

    @staticmethod
    def _event_fields(revision, record, records_by_id):
        return {
            "revision": str(revision),
            "record_id": record.id,
            "record_type": record.record_type,
            "contract_ids": json.dumps(
                _record_contract_ids(record, records_by_id), separators=(",", ":")
            ),
            "worker_ids": json.dumps(
                _record_worker_ids(record, records_by_id), separators=(",", ":")
            ),
        }

    @staticmethod
    def _resp_command_size(parts):
        # RESP2 array and bulk-string framing for one exact command.
        size = len(f"*{len(parts)}\r\n".encode("ascii"))
        for value in parts:
            encoded = str(value).encode("utf-8")
            size += len(f"${len(encoded)}\r\n".encode("ascii"))
            size += len(encoded) + 2
        return size

    def _resp_size(self, event_id, fields):
        parts = ["XADD", self.stream_key, event_id]
        for key, value in fields.items():
            parts.extend((key, value))
        return self._resp_command_size(parts)

    def _read_fence(self, pipe):
        values = pipe.eval(_READ_FENCE, 2, self.stream_key, self.metadata_key)
        if not isinstance(values, (list, tuple)) or len(values) != 5:
            raise RedisCollaborationUnavailable(
                "Redis collaboration metadata is invalid", reason="metadata_invalid"
            )
        return tuple(_decode_text(value) for value in values)

    def _capture_events(self, store, through=None):
        """Return the source revision and one canonical, ordered event capture.

        Raw RuntimeRecord rows and the typed reference map stay local to this
        function. Callers retain only (revision, sanitized fields) entries.
        """
        revision, rows, records_by_id = self._records(store, through=through)
        reference_context = build_runtime_record_reference_context(records_by_id)
        entries = [
            (
                row_revision,
                self._event_fields(row_revision, record, reference_context),
            )
            for row_revision, record in rows
        ]
        return revision, entries

    @staticmethod
    def _iter_events(entries, after_revision=0):
        for revision, fields in entries:
            if revision > after_revision:
                yield f"{revision}-0", fields

    @staticmethod
    def _entries_preserve_prefix(entries, prefix, through_revision):
        index = 0
        for revision, fields in entries:
            if revision > through_revision:
                break
            if index >= len(prefix) or prefix[index] != (revision, fields):
                return False
            index += 1
        return index == len(prefix)

    def _prove_prefix(self, pipe, entries, state):
        stream_type, meta_type, watermark_text, schema, tail = state
        if stream_type not in ("none", "stream") or meta_type not in ("none", "hash"):
            raise RedisCollaborationUnavailable(
                "Redis collaboration projection is inconsistent",
                reason="projection_inconsistent",
            )
        if (
            stream_type != "none" or meta_type != "none"
        ) and schema != COLLABORATION_SCHEMA:
            raise RedisCollaborationUnavailable(
                "Redis collaboration schema mismatch", reason="schema_mismatch"
            )
        if schema not in ("", COLLABORATION_SCHEMA):
            raise RedisCollaborationUnavailable(
                "Redis collaboration schema mismatch", reason="schema_mismatch"
            )
        try:
            watermark = int(watermark_text or 0)
        except ValueError as exc:
            raise RedisCollaborationUnavailable(
                "Redis collaboration metadata is invalid", reason="metadata_invalid"
            ) from exc
        if watermark < 0 or (stream_type == "none") != (meta_type == "none"):
            raise RedisCollaborationUnavailable(
                "Redis collaboration projection is inconsistent",
                reason="projection_inconsistent",
            )
        if stream_type == "none":
            if entries:
                return watermark, 0, "empty"
            return watermark, 0, "equal"
        if not watermark or tail != f"{watermark}-0":
            raise RedisCollaborationUnavailable(
                "Redis collaboration projection is inconsistent",
                reason="projection_inconsistent",
            )
        source_revision = entries[-1][0] if entries else 0
        source_cap = min(watermark, source_revision)
        expected_count = 0
        while (
            expected_count < len(entries) and entries[expected_count][0] <= source_cap
        ):
            expected_count += 1
        # Bound before parsing and compare pages directly against the one ordered
        # canonical capture; do not build a second event list or source-ID map.
        cursor = "-"
        observed = 0
        while True:
            page = pipe.xrange(
                self.stream_key,
                min=cursor,
                max=f"{source_cap}-0" if source_cap else "0-0",
                count=_XRANGE_PAGE_SIZE,
            )
            if not page:
                break
            for event_id, fields in page:
                if observed >= expected_count:
                    raise RedisCollaborationUnavailable(
                        "Redis collaboration source prefix differs",
                        reason="source_diverged",
                    )
                decoded_id = _decode_text(event_id)
                expected_revision, expected_fields = entries[observed]
                decoded_fields = {
                    _decode_text(key): _decode_text(value)
                    for key, value in fields.items()
                }
                if (
                    decoded_id != f"{expected_revision}-0"
                    or decoded_fields != expected_fields
                ):
                    raise RedisCollaborationUnavailable(
                        "Redis collaboration source prefix differs",
                        reason="source_diverged",
                    )
                observed += 1
                cursor = f"({decoded_id}"
            if len(page) < _XRANGE_PAGE_SIZE:
                break
        if observed != expected_count:
            raise RedisCollaborationUnavailable(
                "Redis collaboration projection is missing source events",
                reason="projection_inconsistent",
            )
        expected_watermark = source_revision
        if watermark > expected_watermark:
            return watermark, observed, "ahead"
        if watermark != expected_watermark:
            return watermark, observed, "lag"
        return watermark, observed, "equal"

    def _append(self, pipe, expected_state, events, target_revision):
        pipe.multi()
        for event_id, fields in events:
            pipe.xadd(self.stream_key, fields, id=event_id)
        pipe.hset(
            self.metadata_key,
            mapping={"watermark": str(target_revision), "schema": COLLABORATION_SCHEMA},
        )
        pipe.execute()

    def _stage_and_swap(
        self, pipe, expected_state, entries, target_revision, *, allow_damaged=False
    ):
        suffix = uuid.uuid4().hex
        stage_stream = f"{self._root}:stage:{suffix}:events"
        stage_meta = f"{self._root}:stage:{suffix}:meta"
        try:
            if entries:
                page: list[tuple[str, dict[str, str]]] = []
                page_bytes = 0
                for event_id, fields in self._iter_events(entries):
                    size = self._resp_size(event_id, fields)
                    if page and (
                        len(page) >= _MAX_APPEND_EVENTS
                        or page_bytes + size > _MAX_APPEND_BYTES
                    ):
                        self._write_stage_page(stage_stream, page)
                        page, page_bytes = [], 0
                    page.append((event_id, fields))
                    page_bytes += size
                if page:
                    self._write_stage_page(stage_stream, page)
                with self._client.pipeline(transaction=True) as stage_meta_pipe:
                    stage_meta_pipe.hset(
                        stage_meta,
                        mapping={
                            "watermark": str(target_revision),
                            "schema": COLLABORATION_SCHEMA,
                        },
                    )
                    stage_meta_pipe.expire(stage_meta, _STAGE_TTL_SECONDS)
                    stage_meta_pipe.execute()
            stream_type, meta_type, wm, schema, tail = expected_state
            pipe.multi()
            pipe.eval(
                _SWAP_STAGED,
                4,
                self.stream_key,
                self.metadata_key,
                stage_stream,
                stage_meta,
                str(target_revision),
                COLLABORATION_SCHEMA,
                "0" if entries else "1",
                stream_type,
                meta_type,
                wm,
                schema,
                tail,
                str(len(entries)),
                "1" if allow_damaged else "0",
            )
            results = pipe.execute()
            if not results or int(results[0]) != 1:
                raise RedisCollaborationUnavailable(
                    "Redis collaboration sync contention",
                    reason="sync_contention",
                )
        finally:
            self._client.delete(stage_stream, stage_meta)

    def _write_stage_page(self, stage_stream, page):
        # Each bounded page and its TTL commit together, including the final page.
        with self._client.pipeline(transaction=True) as stage:
            for event_id, fields in page:
                stage.xadd(stage_stream, fields, id=event_id)
            stage.expire(stage_stream, _STAGE_TTL_SECONDS)
            stage.execute()

    def sync(self) -> dict[str, int | bool]:
        """Prove and relay one canonical capture of the caller's source snapshot."""
        self._require_domain()
        try:
            revision, entries = self._capture_events(self._store)
            for attempt in range(_MAX_WATCH_ATTEMPTS):
                try:
                    return self._sync_attempt(revision, entries)
                except RedisCollaborationUnavailable as exc:
                    if exc.reason != "redis_ahead":
                        raise
                    if attempt + 1 >= _MAX_WATCH_ATTEMPTS:
                        raise RedisCollaborationUnavailable(
                            "Redis collaboration sync contention",
                            reason="sync_contention",
                        ) from exc
                    refreshed = self._refresh_capture_if_advanced(revision, entries)
                    if refreshed is None:
                        raise
                    revision, entries = refreshed
                except self._redis_errors as exc:
                    if exc.__class__.__name__ != "WatchError":
                        raise
                    if attempt + 1 >= _MAX_WATCH_ATTEMPTS:
                        continue
                    refreshed = self._refresh_capture_if_advanced(revision, entries)
                    if refreshed is not None:
                        revision, entries = refreshed
            raise RedisCollaborationUnavailable(
                "Redis collaboration sync contention", reason="sync_contention"
            )
        except self._redis_errors as exc:
            raise RedisCollaborationUnavailable(
                "Redis collaboration service unavailable"
            ) from exc

    def _refresh_capture_if_advanced(self, revision, entries):
        """Use a fresh same-Store capture only when it extends the full prefix."""
        fresh_revision, fresh_entries = self._capture_events(self._store)
        if fresh_revision <= revision:
            return None
        if not self._entries_preserve_prefix(fresh_entries, entries, revision):
            raise RedisCollaborationUnavailable(
                "SQLite runtime source prefix changed during sync",
                reason="source_diverged",
            )
        return fresh_revision, fresh_entries

    def _sync_attempt(self, revision, entries):
        with self._client.pipeline() as pipe:
            pipe.watch(self.stream_key, self.metadata_key)
            state = self._read_fence(pipe)
            watermark, _prefix_count, relation = self._prove_prefix(
                pipe, entries, state
            )
            if relation == "ahead":
                raise RedisCollaborationUnavailable(
                    "Redis collaboration is ahead of this source snapshot",
                    reason="redis_ahead",
                )
            if relation == "equal":
                pipe.multi()
                pipe.execute()
                return {
                    "authoritative_revision": revision,
                    "watermark": watermark,
                    "lag": 0,
                    "synced": True,
                }
            suffix_count = 0
            byte_count = 0
            for event_id, fields in self._iter_events(entries, watermark):
                suffix_count += 1
                byte_count += self._resp_size(event_id, fields)
            byte_count += (
                self._resp_command_size(
                    [
                        "HSET",
                        self.metadata_key,
                        "watermark",
                        revision,
                        "schema",
                        COLLABORATION_SCHEMA,
                    ]
                )
                + 16
            )
            if suffix_count <= _MAX_APPEND_EVENTS and byte_count <= _MAX_APPEND_BYTES:
                self._append(
                    pipe,
                    state,
                    self._iter_events(entries, watermark),
                    revision,
                )
            else:
                self._stage_and_swap(pipe, state, entries, revision)
            return {
                "authoritative_revision": revision,
                "watermark": revision,
                "lag": 0,
                "synced": True,
            }

    def rebuild(self, fresh_store_factory=None) -> dict[str, int | bool]:
        """Sync safely, or repair from an explicitly supplied routed snapshot factory.

        The caller owns every snapshot returned by ``fresh_store_factory``. The
        service does not read its original Store after asking for a fresh snapshot.
        """
        if fresh_store_factory is None:
            return self.sync()
        self._require_domain()
        source_revision, source_entries = self._capture_events(self._store)
        try:
            for attempt in range(_MAX_WATCH_ATTEMPTS):
                fresh = None
                fresh_entries = None
                try:
                    with self._client.pipeline() as pipe:
                        # Fence live state before opening P; retain this WATCH through EXEC.
                        pipe.watch(self.stream_key, self.metadata_key)
                        state = self._read_fence(pipe)
                        fresh = fresh_store_factory()
                        from aigineering.core.domain import load_genesis

                        if load_genesis(fresh).id != self._require_domain():
                            raise RedisCollaborationUnavailable(
                                "Routed snapshot belongs to a different domain",
                                reason="source_domain_mismatch",
                            )
                        fresh_revision, fresh_entries = self._capture_events(fresh)
                        if not self._entries_preserve_prefix(
                            fresh_entries, source_entries, source_revision
                        ):
                            raise RedisCollaborationUnavailable(
                                "Routed source does not preserve the captured snapshot",
                                reason="source_diverged",
                            )
                        allow_damaged = attempt == 0
                        try:
                            _watermark, _prefix_count, relation = self._prove_prefix(
                                pipe, fresh_entries, state
                            )
                        except RedisCollaborationUnavailable as exc:
                            if not allow_damaged or exc.reason not in (
                                "projection_inconsistent",
                                "source_diverged",
                                "schema_mismatch",
                                "metadata_invalid",
                            ):
                                raise
                            relation = "damaged"
                        if relation == "ahead" and not allow_damaged:
                            raise RedisCollaborationUnavailable(
                                "Redis collaboration is ahead of the routed source",
                                reason="redis_ahead",
                            )
                        self._stage_and_swap(
                            pipe,
                            state,
                            fresh_entries,
                            fresh_revision,
                            allow_damaged=allow_damaged,
                        )
                        return {
                            "authoritative_revision": fresh_revision,
                            "watermark": fresh_revision,
                            "lag": 0,
                            "synced": True,
                        }
                except self._redis_errors as exc:
                    if exc.__class__.__name__ == "WatchError":
                        continue
                    raise
                finally:
                    # Release per-attempt source materialization before factory
                    # opens P again; the caller retains snapshot ownership.
                    fresh_entries = None
                    fresh = None
            raise RedisCollaborationUnavailable(
                "Redis collaboration rebuild contention", reason="sync_contention"
            )
        except self._redis_errors as exc:
            raise RedisCollaborationUnavailable(
                "Redis collaboration service unavailable"
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
        self._require_domain()
        try:
            revision, entries = self._capture_events(self._store)
            response = None
            for _ in range(_MAX_WATCH_ATTEMPTS):
                try:
                    with self._client.pipeline() as pipe:
                        pipe.watch(self.stream_key, self.metadata_key)
                        state = self._read_fence(pipe)
                        _watermark, _prefix_count, relation = self._prove_prefix(
                            pipe, entries, state
                        )
                        if relation in ("ahead", "equal"):
                            # Bound first, then parse only the source-known prefix.
                            response = pipe.xrange(
                                self.stream_key,
                                min=f"({after_revision}-0",
                                max=f"{revision}-0" if revision else "0-0",
                                count=count,
                            )
                            pipe.multi()
                            pipe.ping()
                            pipe.execute()
                    if response is not None:
                        break
                    self.sync()
                except RedisCollaborationUnavailable as exc:
                    if exc.reason != "sync_contention":
                        raise
                    response = None
                    continue
                except self._redis_errors as exc:
                    if exc.__class__.__name__ == "WatchError":
                        response = None
                        continue
                    raise
            else:
                raise RedisCollaborationUnavailable(
                    "Redis collaboration sync contention", reason="sync_contention"
                )
            if response is None:
                raise RedisCollaborationUnavailable(
                    "Redis collaboration sync contention", reason="sync_contention"
                )
            parsed = self._parse_entries(response)
            if parsed or block_ms is None:
                return [event for event in parsed if event["revision"] <= revision]
            cursor = f"{after_revision}-0"
            while True:
                wake = self._client.xread(
                    {self.stream_key: cursor},
                    count=count,
                    block=250 if block_ms == 0 else block_ms,
                )
                for _stream, entries in wake or ():
                    if entries:
                        cursor = _decode_text(entries[-1][0])
                if int(self._store.get_runtime_revision()) > revision:
                    return self.events(
                        after_revision=after_revision, count=count, block_ms=None
                    )
                if block_ms != 0:
                    break
            return [event for event in parsed if event["revision"] <= revision]
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
        source_revision = int(self._store.get_runtime_revision())
        initial_events = self.events(after_revision=after_revision, count=100)
        if initial_events:
            return initial_events
        try:
            tail = self._client.xrevrange(self.stream_key, count=1)
            latest_seen = source_revision
            if tail:
                tail_id = _decode_text(tail[0][0])
                parts = tail_id.split("-")
                if len(parts) != 2 or parts[1] != "0":
                    raise RedisCollaborationUnavailable(
                        "Redis collaboration projection is inconsistent",
                        reason="projection_inconsistent",
                    )
                latest_seen = max(latest_seen, int(parts[0]))
            cursor = f"{max(after_revision, source_revision, latest_seen)}-0"
            current_revision = int(self._store.get_runtime_revision())
            if current_revision != source_revision:
                source_revision = current_revision
                events = self.events(after_revision=after_revision, count=100)
                if events:
                    return events
            while True:
                if time.monotonic() >= deadline:
                    return []
                remaining_ms = max(
                    1, min(250, int((deadline - time.monotonic()) * 1000))
                )
                response = self._client.xread(
                    {self.stream_key: cursor}, count=100, block=remaining_ms
                )
                for _stream, entries in response or ():
                    if entries:
                        cursor = _decode_text(entries[-1][0])
                current_revision = int(self._store.get_runtime_revision())
                if current_revision != source_revision:
                    source_revision = current_revision
                    events = self.events(after_revision=after_revision, count=100)
                    if events:
                        return events
        except self._redis_errors as exc:
            raise RedisCollaborationUnavailable(
                "Redis collaboration service unavailable"
            ) from exc

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
            revision, source_entries = self._capture_events(self._store)
            with self._client.pipeline() as pipe:
                pipe.watch(self.stream_key, self.metadata_key)
                state = self._read_fence(pipe)
                watermark, _prefix_count, relation = self._prove_prefix(
                    pipe, source_entries, state
                )
                schema = state[3]
                if relation == "ahead":
                    suffix = pipe.xrange(
                        self.stream_key,
                        min=f"({revision}-0" if revision else "-",
                        max="+",
                        count=None,
                    )
                    parsed_suffix = self._parse_entries(suffix)
                    suffix_revisions = [event["revision"] for event in parsed_suffix]
                    if suffix_revisions != sorted(set(suffix_revisions)) or (
                        not suffix_revisions or suffix_revisions[-1] != watermark
                    ):
                        raise RedisCollaborationUnavailable(
                            "Redis collaboration projection is inconsistent",
                            reason="projection_inconsistent",
                        )
                pipe.multi()
                pipe.ping()
                pipe.execute()
            current = watermark == revision and relation == "equal"
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
                "reason": "redis_ahead"
                if relation == "ahead"
                else ("revision_lag" if lag else ""),
                "schema": schema or COLLABORATION_SCHEMA,
            }
        except self._redis_errors as exc:
            if exc.__class__.__name__ == "WatchError":
                return {
                    "available": True,
                    "backend": "redis",
                    "configured": True,
                    "current": False,
                    "authoritative_revision": revision,
                    "watermark": 0,
                    "lag": revision,
                    "reason": "sync_contention",
                    "schema": COLLABORATION_SCHEMA,
                }
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
        except RedisCollaborationUnavailable as exc:
            return {
                "available": exc.reason != "redis_unavailable",
                "backend": "redis",
                "configured": True,
                "current": False,
                "authoritative_revision": revision,
                "watermark": 0,
                "lag": revision,
                "reason": exc.reason,
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

    def _parse_entries(self, response) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for item in response or ():
            if len(item) == 2 and isinstance(item[1], dict):
                entries = [item]
            else:
                entries = item[1]
            for event_id, fields in entries:
                try:
                    if set(fields) != {
                        "revision",
                        "record_id",
                        "record_type",
                        "contract_ids",
                        "worker_ids",
                    }:
                        raise ValueError("invalid event fields")
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
                    if (
                        not isinstance(contracts, list)
                        or not all(
                            isinstance(item, str) and item.startswith("task:v")
                            for item in contracts
                        )
                        or contracts != sorted(set(contracts))
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
                        "Redis collaboration event is invalid",
                        reason="projection_inconsistent",
                    ) from exc
        return events

    def _parse_stream_response(self, response) -> list[dict[str, Any]]:
        return self._parse_entries(response)
