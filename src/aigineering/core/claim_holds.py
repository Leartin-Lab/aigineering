"""Generic immutable claim-hold facts and their deterministic reducer."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

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


def validate_claim_hold_batch(
    existing: Iterable[RuntimeRecord],
    pending: Iterable[RuntimeRecord],
    *,
    candidate_id: str = "",
    actor_id: str = "",
    key_id: str = "",
) -> tuple[RuntimeRecord, ...]:
    """Validate hold transitions in a transaction and return new placements."""
    history = tuple(existing)
    pending = tuple(pending)
    transitions = tuple(
        record for record in pending if record.record_type in {_PLACED, _RELEASED}
    )
    if len(transitions) > _MAX_TRANSITIONS_PER_CANDIDATE:
        raise ClaimHoldConflict("claim-hold Candidate batch exceeds 64 transitions")
    if not transitions:
        return ()
    for record in transitions:
        _validate_fact(record)
    transition_types = {record.record_type for record in transitions}
    transition_contracts = {record.payload["contract_id"] for record in transitions}
    if len(transition_types) != 1:
        raise ClaimHoldConflict(
            "claim-hold Candidate batch cannot mix placements and releases"
        )
    if len(transition_contracts) != 1 or not all(
        isinstance(contract, str) and contract.strip()
        for contract in transition_contracts
    ):
        raise ClaimHoldConflict("claim-hold Candidate batch must target one Contract")
    transition_ids = [record.id for record in transitions]
    if len(transition_ids) != len(set(transition_ids)):
        raise ClaimHoldConflict("claim-hold Candidate batch repeats a record ID")
    if next(iter(transition_types)) == _RELEASED:
        hold_ids = [record.payload["hold_id"] for record in transitions]
        if len(hold_ids) != len(set(hold_ids)):
            raise ClaimHoldConflict(
                "claim-hold Candidate batch repeats a released hold ID"
            )
    receipts = tuple(
        record
        for record in pending
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate_id
        and record.payload.get("actor_id") == actor_id
        and record.payload.get("key_id") == key_id
    )
    committed_heads = tuple(
        record
        for record in pending
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate_id
    )
    if len(receipts) != 1 or len(committed_heads) != 1:
        raise ClaimHoldConflict(
            "claim hold transition lacks its authenticated Candidate head"
        )
    receipt = receipts[0]
    committed = committed_heads[0]
    try:
        validate_runtime_record(receipt)
        validate_runtime_record(committed)
    except ValueError as exc:
        raise ClaimHoldConflict("claim hold Candidate head is invalid") from exc
    if receipt.schema_version != 1 or committed.schema_version != 1:
        raise ClaimHoldConflict("unsupported claim hold Candidate head schema")
    if not isinstance(receipt.payload, Mapping) or not {
        "candidate_id",
        "actor_id",
        "key_id",
    }.issubset(receipt.payload):
        raise ClaimHoldConflict("claim hold Candidate receipt fields are invalid")
    if not isinstance(committed.payload, Mapping) or set(committed.payload) != {
        "candidate_id",
        "committed_record_ids",
    }:
        raise ClaimHoldConflict("claim hold committed head fields are invalid")
    if receipt.id not in committed.causal_parents:
        raise ClaimHoldConflict("committed head does not depend on Candidate receipt")
    open_holds = {record.id: record for record in open_claim_holds(history)}
    placements: list[RuntimeRecord] = []
    seen_ids = {record.id for record in history}
    for record in pending:
        if record.record_type not in {_PLACED, _RELEASED}:
            continue
        _validate_fact(record)
        if (
            not candidate_id
            or record.payload["candidate_id"] != candidate_id
            or record.payload["actor_id"] != actor_id
            or record.payload["key_id"] != key_id
        ):
            raise ClaimHoldConflict(
                "claim hold provenance does not match its authenticated Candidate"
            )
        required_parents = {receipt.id}
        if record.record_type == _RELEASED:
            required_parents.add(str(record.payload["hold_id"]))
        if not required_parents.issubset(record.causal_parents):
            raise ClaimHoldConflict(
                "claim hold fact lacks required Candidate or hold parents"
            )
        committed_ids = committed_heads[0].payload.get("committed_record_ids")
        if (
            not isinstance(committed_ids, (list, tuple))
            or record.id not in committed_ids
            or record.id not in committed.causal_parents
        ):
            raise ClaimHoldConflict(
                "Candidate committed head does not reference its claim hold fact"
            )
        if record.id in seen_ids:
            # The candidate-level idempotency check normally handles this;
            # identical facts are harmless if another record in the batch repeats.
            continue
        seen_ids.add(record.id)
        if record.record_type == _PLACED:
            if record.id in open_holds:
                raise ClaimHoldConflict("claim hold placement is duplicated")
            open_holds[record.id] = record
            placements.append(record)
        else:
            hold_id = str(record.payload["hold_id"])
            placement = open_holds.get(hold_id)
            if placement is None:
                raise ClaimHoldConflict(
                    "claim hold release does not reference an open hold"
                )
            if placement.payload["contract_id"] != record.payload["contract_id"]:
                raise ClaimHoldConflict("claim hold release targets another Contract")
            del open_holds[hold_id]
    return tuple(placements)
