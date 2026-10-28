"""Result collection and input helpers for independent task operations."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import click

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.protocol.identity import canonical_json
from aigineering.runtime_services import RedisSyncPending
from aigineering.application import collaboration_service, require_redis
from aigineering.core.domain import load_genesis

MAX_TASK_BATCH = 64


def validate_task_ids(contract_ids: Sequence[str]) -> tuple[str, ...]:
    """Validate an exact, ordered batch of one through 64 distinct IDs."""
    values = tuple(contract_ids)
    if not 1 <= len(values) <= MAX_TASK_BATCH:
        raise ValueError("one invocation must target between 1 and 64 tasks")
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ValueError("Contract IDs must be non-empty strings")
    for item in values:
        try:
            item.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("Contract IDs must be valid UTF-8") from exc
    if len(values) != len(set(values)):
        raise ValueError("Contract IDs must not contain duplicates")
    return values


def validate_batch_key(value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError("idempotency key must not be blank")
    if value is not None:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("idempotency key must be valid UTF-8") from exc


def new_batch_key_base(action: str) -> str:
    return f"task-bulk-v1:{action}:{uuid.uuid4().hex}"


def target_key(action: str, base: str, contract_id: str) -> str:
    """Derive a bounded per-target Candidate key independent of batch order."""
    payload = canonical_json(("task-bulk-v1", action, base, contract_id))
    digest = hashlib.sha256(payload.encode("utf-8", errors="strict")).hexdigest()
    return f"task-bulk-v1:{action}:{digest}"


def exact_target_mapping(
    values: Sequence[str],
    contract_ids: Sequence[str],
    *,
    multiple_per_target: bool,
) -> dict[str, tuple[str, ...]]:
    """Parse complete ``TASK=RECORD`` mappings without normalizing IDs."""
    targets = set(contract_ids)
    parsed: dict[str, list[str]] = {contract_id: [] for contract_id in contract_ids}
    seen: set[tuple[str, str]] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("target mappings must be text")
        target, separator, record_id = value.partition("=")
        if not separator or not target.strip() or not record_id.strip():
            raise ValueError("target mapping must be TASK_ID=RECORD_ID")
        try:
            target.encode("utf-8", errors="strict")
            record_id.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("target mapping must contain valid UTF-8") from exc
        if target not in targets:
            raise ValueError(f"mapping references unselected Contract {target!r}")
        pair = (target, record_id)
        if pair in seen:
            raise ValueError("target mappings must not contain duplicates")
        seen.add(pair)
        if parsed[target] and not multiple_per_target:
            raise ValueError(f"Contract {target!r} must have exactly one mapping")
        parsed[target].append(record_id)
    missing = [contract_id for contract_id, records in parsed.items() if not records]
    if missing:
        raise ValueError(f"every target needs an exact mapping: {', '.join(missing)}")
    return {
        contract_id: tuple(sorted(records)) for contract_id, records in parsed.items()
    }


def preflight_task_batch(open_snapshot: Callable[[], object]) -> None:
    """Complete required Redis/domain preflight before any target publication."""
    require_redis()
    snapshot = open_snapshot()
    try:
        load_genesis(snapshot)
        collaboration_service(snapshot).sync()
    finally:
        snapshot.close()


@dataclass(frozen=True)
class BatchCollection:
    data: dict
    revision: int
    unexpected: Exception | None = None


def collect_task_batch(
    contract_ids: Sequence[str],
    *,
    action: str,
    batch_key_base: str,
    invoke: Callable[[str, str | None], tuple[dict, int]],
    key_for_target: Callable[[str], str | None] | None = None,
    expected_rejection_types: tuple[type[Exception], ...] = (ValueError,),
    passthrough_exceptions: tuple[type[Exception], ...] = (),
    rejection_result: Callable[[Exception], tuple[dict | None, int | None]]
    | None = None,
    include_operation_revision: bool = False,
    batch_field: str = "batch_idempotency_key",
) -> BatchCollection:
    """Invoke one existing application owner per target and keep every result."""
    if batch_field not in {"batch_idempotency_key", "batch_correlation_id"}:
        raise ValueError("unsupported batch result field")
    results: list[dict] = []
    revision = 0
    unexpected: Exception | None = None

    for index, contract_id in enumerate(contract_ids):
        key: str | None = None
        try:
            key = (
                key_for_target(contract_id)
                if key_for_target is not None
                else target_key(action, batch_key_base, contract_id)
            )
        except Exception as exc:
            unexpected = exc
            results.extend(
                _unknown_tail(
                    contract_ids,
                    index,
                    contract_id,
                    key,
                    exc,
                    action,
                    batch_key_base,
                    key_for_target,
                )
            )
            break
        try:
            item, item_revision = invoke(contract_id, key)
            revision = max(revision, item_revision)
            results.append(
                _result_row(
                    contract_id,
                    item,
                    key,
                    revision=item_revision,
                    include_revision=include_operation_revision,
                )
            )
        except RedisSyncPending as exc:
            if exc.revision is not None:
                revision = max(revision, exc.revision)
            item = exc.data if isinstance(exc.data, dict) else None
            if exc.accepted is True and item is not None:
                row = _result_row(
                    contract_id,
                    item,
                    key,
                    revision=exc.revision,
                    include_revision=include_operation_revision,
                )
                row.update(
                    {
                        "ok": False,
                        "outcome": "redis_sync_pending",
                        "candidate_accepted": True,
                        "facts_retained": True,
                    }
                )
                results.append(row)
            elif exc.accepted is False:
                if item is None:
                    row = {
                        "contract_id": contract_id,
                        "ok": False,
                        "outcome": "outcome_unknown",
                        "candidate_accepted": False,
                        "command_attempted": True,
                        "facts_retained": None,
                        "relay_pending": True,
                        "error": {
                            "code": "missing_rejection_result",
                            "exception_class": "RedisSyncPending",
                        },
                    }
                    _set_optional_key(row, key)
                    if include_operation_revision and exc.revision is not None:
                        row["revision"] = exc.revision
                    results.append(row)
                    results.extend(
                        _not_attempted_tail(
                            contract_ids,
                            index,
                            action,
                            batch_key_base,
                            key_for_target,
                        )
                    )
                    unexpected = exc
                    break
                else:
                    row = _result_row(
                        contract_id,
                        item,
                        key,
                        revision=exc.revision,
                        include_revision=include_operation_revision,
                    )
                    row.update(
                        {
                            "ok": False,
                            "outcome": "rejected",
                            "candidate_accepted": False,
                            "facts_retained": True,
                            "command_attempted": True,
                            "relay_pending": True,
                            "error": {
                                "code": "rejected",
                                "message": str(
                                    item.get("rejection_reason", "Candidate rejected")
                                ),
                            },
                        }
                    )
                    results.append(row)
            elif exc.accepted is True:
                row = {
                    "contract_id": contract_id,
                    "ok": False,
                    "outcome": "outcome_unknown",
                    "candidate_accepted": True,
                    "command_attempted": True,
                    "facts_retained": True,
                    "error": {
                        "code": "missing_result",
                        "exception_class": "RedisSyncPending",
                    },
                }
                _set_optional_key(row, key)
                if include_operation_revision and exc.revision is not None:
                    row["revision"] = exc.revision
                results.append(row)
                results.extend(
                    _not_attempted_tail(
                        contract_ids, index, action, batch_key_base, key_for_target
                    )
                )
                unexpected = exc
                break
            else:
                row = {
                    "contract_id": contract_id,
                    "ok": False,
                    "outcome": "not_attempted",
                    "candidate_accepted": None,
                    "command_attempted": False,
                    "error": {"code": "redis_sync_pending", "message": str(exc)},
                }
                _set_optional_key(row, key)
                results.append(row)
        except QueryProjectionUnavailable as exc:
            row = {
                "contract_id": contract_id,
                "ok": False,
                "outcome": "not_attempted",
                "candidate_accepted": None,
                "command_attempted": False,
                "error": {"code": "redis_unavailable", "message": str(exc)},
            }
            _set_optional_key(row, key)
            results.append(row)
        except LookupError as exc:
            if type(exc) is LookupError:
                results.append(_error_row(contract_id, key, "not_found", str(exc)))
                continue
            unexpected = exc
            results.extend(
                _unknown_tail(
                    contract_ids,
                    index,
                    contract_id,
                    key,
                    exc,
                    action,
                    batch_key_base,
                    key_for_target,
                )
            )
            break
        except ValueError as exc:
            if isinstance(exc, passthrough_exceptions):
                raise
            if isinstance(exc, expected_rejection_types):
                try:
                    row = _expected_rejection_row(
                        contract_id,
                        key,
                        exc,
                        rejection_result=rejection_result,
                        include_revision=include_operation_revision,
                    )
                except Exception as result_exc:
                    unexpected = result_exc
                    results.extend(
                        _unknown_tail(
                            contract_ids,
                            index,
                            contract_id,
                            key,
                            result_exc,
                            action,
                            batch_key_base,
                            key_for_target,
                        )
                    )
                    break
                if row.get("revision") is not None:
                    revision = max(revision, row["revision"])
                results.append(row)
                continue
            unexpected = exc
            results.extend(
                _unknown_tail(
                    contract_ids,
                    index,
                    contract_id,
                    key,
                    exc,
                    action,
                    batch_key_base,
                    key_for_target,
                )
            )
            break
        except Exception as exc:
            if isinstance(exc, passthrough_exceptions):
                raise
            if isinstance(exc, expected_rejection_types):
                try:
                    row = _expected_rejection_row(
                        contract_id,
                        key,
                        exc,
                        rejection_result=rejection_result,
                        include_revision=include_operation_revision,
                    )
                except Exception as result_exc:
                    unexpected = result_exc
                    results.extend(
                        _unknown_tail(
                            contract_ids,
                            index,
                            contract_id,
                            key,
                            result_exc,
                            action,
                            batch_key_base,
                            key_for_target,
                        )
                    )
                    break
                if row.get("revision") is not None:
                    revision = max(revision, row["revision"])
                results.append(row)
                continue
            unexpected = exc
            results.extend(
                _unknown_tail(
                    contract_ids,
                    index,
                    contract_id,
                    key,
                    exc,
                    action,
                    batch_key_base,
                    key_for_target,
                )
            )
            break

    succeeded = sum(row["outcome"] == "accepted" for row in results)
    pending = sum(row["outcome"] == "redis_sync_pending" for row in results)
    unknown = sum(row["outcome"] == "outcome_unknown" for row in results)
    not_attempted = sum(row["outcome"] == "not_attempted" for row in results)
    data = {
        batch_field: batch_key_base,
        "results": results,
        "success_count": succeeded,
        "failed_count": len(results) - succeeded,
        "pending_count": pending,
        "unknown_count": unknown,
        "not_attempted_count": not_attempted,
    }
    return BatchCollection(data, revision, unexpected)


def _result_row(
    contract_id: str,
    item: dict,
    key: str | None,
    *,
    revision: int | None = None,
    include_revision: bool = False,
) -> dict:
    data = dict(item)
    if key is not None:
        data.setdefault("idempotency_key", key)
    candidate_id = data.get("candidate_id")
    fact_ids = data.get("fact_ids")
    if fact_ids is None:
        fact_ids = [str(data[name]) for name in ("record_id",) if data.get(name)]
    row = {
        "contract_id": contract_id,
        "ok": True,
        "outcome": "accepted",
        "fact_ids": list(fact_ids),
        "data": data,
    }
    actual_key = data.get("idempotency_key", key)
    _set_optional_key(row, actual_key if isinstance(actual_key, str) else None)
    if candidate_id is not None:
        row["candidate_id"] = str(candidate_id)
    if include_revision and revision is not None:
        row["revision"] = revision
    return row


def _error_row(contract_id: str, key: str | None, outcome: str, message: str) -> dict:
    row = {
        "contract_id": contract_id,
        "ok": False,
        "outcome": outcome,
        "error": {"code": outcome, "message": message},
    }
    _set_optional_key(row, key)
    return row


def _set_optional_key(row: dict, key: str | None) -> None:
    if key is not None:
        row["idempotency_key"] = key


def _copy_exact_ids(row: dict, data: dict) -> None:
    fact_ids = data.get("fact_ids")
    if isinstance(fact_ids, list):
        row["fact_ids"] = list(fact_ids)
    candidate_id = data.get("candidate_id")
    if isinstance(candidate_id, str):
        row["candidate_id"] = candidate_id


def _expected_rejection_row(
    contract_id: str,
    key: str | None,
    exc: Exception,
    *,
    rejection_result: Callable[[Exception], tuple[dict | None, int | None]] | None,
    include_revision: bool,
) -> dict:
    row = _error_row(contract_id, key, "rejected", str(exc))
    if rejection_result is None:
        return row
    data, revision = rejection_result(exc)
    if data is None:
        row["candidate_accepted"] = None
        row["command_attempted"] = False
        if include_revision and revision is not None:
            row["revision"] = revision
        return row
    if not isinstance(data, dict):
        raise TypeError("typed rejection result data must be a mapping")
    row["data"] = dict(data)
    row["candidate_accepted"] = False
    row["facts_retained"] = True
    row["command_attempted"] = True
    _copy_exact_ids(row, data)
    actual_key = data.get("idempotency_key", key)
    _set_optional_key(row, actual_key if isinstance(actual_key, str) else None)
    if include_revision and revision is not None:
        row["revision"] = revision
    return row


def _unknown_tail(
    contract_ids, index, contract_id, key, exc, action, base, key_for_target
):
    row = _optional_key_row(
        {
            "contract_id": contract_id,
            "ok": False,
            "outcome": "outcome_unknown",
            "candidate_accepted": None,
            "command_attempted": True,
            "error": {"code": "internal_error", "exception_class": type(exc).__name__},
        },
        key,
    )
    return [row] + _not_attempted_tail(
        contract_ids, index, action, base, key_for_target
    )


def _not_attempted_tail(contract_ids, index, action, base, key_for_target):
    rows = []
    for pending_id in contract_ids[index + 1 :]:
        key = None
        try:
            key = (
                key_for_target(pending_id)
                if key_for_target is not None
                else target_key(action, base, pending_id)
            )
        except Exception:
            pass
        row = {
            "contract_id": pending_id,
            "ok": False,
            "outcome": "not_attempted",
            "candidate_accepted": None,
            "command_attempted": False,
            "error": {"code": "not_attempted"},
        }
        _set_optional_key(row, key)
        rows.append(row)
    return rows


def _optional_key_row(row: dict, key: str | None) -> dict:
    _set_optional_key(row, key)
    return row


def emit_batch(
    collection: BatchCollection,
    *,
    as_json: bool,
    emit_json,
    safe_human_base: bool = False,
) -> None:
    """Emit a complete batch outcome, then surface any unexpected cause."""
    data = collection.data
    failed = data["failed_count"] > 0
    if as_json:
        emit_json(
            {
                "ok": not failed,
                "data": data,
                "revision": collection.revision,
            }
        )
    else:
        click.echo("\n\n".join(_human_row(row) for row in data["results"]))
        batch_field = (
            "batch_idempotency_key"
            if "batch_idempotency_key" in data
            else "batch_correlation_id"
        )
        base = data[batch_field]
        if safe_human_base and not re.fullmatch(r"[A-Za-z0-9._:-]+", base):
            click.echo(
                "Batch replay key withheld; use --json to retrieve the exact key."
            )
        else:
            label = (
                "Batch replay key"
                if batch_field == "batch_idempotency_key"
                else "Batch correlation ID"
            )
            click.echo(f"{label}: {base}")
    if collection.unexpected is not None:
        if as_json:
            raise click.exceptions.Exit(1) from collection.unexpected
        raise click.ClickException(
            f"Unexpected task batch failure ({type(collection.unexpected).__name__})"
        ) from collection.unexpected
    if failed:
        raise click.exceptions.Exit(1)


def _human_row(row: dict) -> str:
    target = row["contract_id"]
    outcome = row["outcome"]
    key = row.get("idempotency_key")
    suffix = f"; key {key}" if key is not None else ""
    if outcome == "accepted":
        details = ", ".join(str(value) for value in row.get("fact_ids", ()))
        return f"{target}: accepted; facts {details}{suffix}"
    if outcome == "redis_sync_pending":
        return f"{target}: accepted; Redis sync pending{suffix}"
    error = row.get("error", {})
    detail = error.get("message", error.get("exception_class", outcome))
    return f"{target}: {outcome}: {detail}{suffix}"
