"""Versioned immutable envelope for append-only runtime records."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from aigineering.core.ids import canonical_json, compute_content_hash, now_iso


RUNTIME_RECORD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RuntimeRecord:
    """Canonical typed runtime fact independent of its physical store table."""

    id: str
    record_type: str
    payload: Mapping[str, Any]
    causal_parents: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = RUNTIME_RECORD_SCHEMA_VERSION
    recorded_at: str = ""

    def __post_init__(self) -> None:
        if not self.record_type:
            raise ValueError("runtime record_type must not be empty")
        if self.schema_version < 1:
            raise ValueError("runtime record schema_version must be positive")
        object.__setattr__(self, "payload", _deep_freeze(dict(self.payload)))
        object.__setattr__(self, "causal_parents", tuple(self.causal_parents))


def create_runtime_record(
    record_type: str,
    payload: Mapping[str, Any],
    *,
    causal_parents: tuple[str, ...] | list[str] = (),
    schema_version: int = RUNTIME_RECORD_SCHEMA_VERSION,
    recorded_at: str | None = None,
) -> RuntimeRecord:
    effective = {
        "causal_parents": list(causal_parents),
        "payload": _deep_thaw(_deep_freeze(dict(payload))),
        "record_type": record_type,
        "schema_version": schema_version,
    }
    record_id = f"record:{compute_content_hash(canonical_json(effective))}"
    return RuntimeRecord(
        id=record_id,
        record_type=record_type,
        payload=payload,
        causal_parents=tuple(causal_parents),
        schema_version=schema_version,
        recorded_at=recorded_at or now_iso(),
    )


def runtime_record_effective_payload(record: RuntimeRecord) -> dict[str, Any]:
    return {
        "causal_parents": list(record.causal_parents),
        "payload": _deep_thaw(record.payload),
        "record_type": record.record_type,
        "schema_version": record.schema_version,
    }


def validate_runtime_record(record: RuntimeRecord) -> None:
    expected = create_runtime_record(
        record.record_type,
        record.payload,
        causal_parents=record.causal_parents,
        schema_version=record.schema_version,
        recorded_at=record.recorded_at,
    ).id
    if record.id != expected:
        raise ValueError(
            f"runtime record id mismatch: supplied {record.id!r}, expected {expected!r}"
        )


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_deep_freeze(item) for item in value), key=repr))
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _deep_thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value
