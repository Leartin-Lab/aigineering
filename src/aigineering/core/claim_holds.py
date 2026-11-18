"""Read-only validation of historical claim holds records."""

from __future__ import annotations

from collections.abc import Iterable

from aigineering.protocol.runtime_record import RuntimeRecord, validate_runtime_record


class ClaimHoldConflict(ValueError):
    """A claim-hold transition conflicts with claim or hold history."""


_PLACED = "claim.hold.placed"
_RELEASED = "claim.hold.released"
_PLACED_FIELDS = frozenset(
    {"contract_id", "reason", "actor_id", "key_id", "candidate_id"}
)
_RELEASED_FIELDS = _PLACED_FIELDS | {"hold_id"}
_MAX_TRANSITIONS_PER_CANDIDATE = 64


def _validate_fact(record: RuntimeRecord) -> None:
    if record.record_type not in {_PLACED, _RELEASED}:
        return
    try:
        validate_runtime_record(record)
    except ValueError as exc:
        raise ClaimHoldConflict("claim hold record identity is invalid") from exc
    if record.schema_version != 1:
        raise ClaimHoldConflict("unsupported claim hold schema version")
    expected = _PLACED_FIELDS if record.record_type == _PLACED else _RELEASED_FIELDS
    if set(record.payload) != expected:
        raise ClaimHoldConflict("claim hold record fields are invalid")
    for field in ("contract_id", "reason", "actor_id", "key_id", "candidate_id"):
        value = record.payload[field]
        if not isinstance(value, str) or not value.strip():
            raise ClaimHoldConflict(f"claim hold {field} must be non-empty text")
    try:
        size = len(str(record.payload["reason"]).encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ClaimHoldConflict("claim hold reason is not valid UTF-8") from exc
    if size > 64 * 1024:
        raise ClaimHoldConflict("claim hold reason exceeds 65536 UTF-8 bytes")
    if not record.causal_parents:
        raise ClaimHoldConflict("claim hold fact requires Candidate receipt provenance")
    if record.record_type == _RELEASED:
        hold_id = record.payload["hold_id"]
        if not isinstance(hold_id, str) or not hold_id.strip():
            raise ClaimHoldConflict("claim hold release requires a hold_id")
        if hold_id not in record.causal_parents:
            raise ClaimHoldConflict("claim hold release must depend on its placement")


def open_claim_holds(
    records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return open placement records in durable input order, validating history."""
    opened: dict[str, RuntimeRecord] = {}
    closed: set[str] = set()
    for record in records:
        if record.record_type not in {_PLACED, _RELEASED}:
            continue
        _validate_fact(record)
        target = str(record.payload["contract_id"])
        if record.record_type == _PLACED:
            if record.id in opened or record.id in closed:
                raise ClaimHoldConflict("claim hold placement is duplicated")
            opened[record.id] = record
            continue
        hold_id = str(record.payload["hold_id"])
        placement = opened.get(hold_id)
        if placement is None or hold_id in closed:
            raise ClaimHoldConflict(
                "claim hold release does not reference an open hold"
            )
        if placement.payload["contract_id"] != target:
            raise ClaimHoldConflict("claim hold release targets another Contract")
        if len(record.causal_parents) < 2:
            raise ClaimHoldConflict(
                "claim hold release requires receipt and hold parents"
            )
        closed.add(hold_id)
    return tuple(
        record
        for hold_id, record in opened.items()
        if hold_id not in closed
        and (contract_id is None or record.payload["contract_id"] == contract_id)
    )
