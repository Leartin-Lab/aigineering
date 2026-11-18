"""Signed advisory task priority effects."""

from __future__ import annotations

from collections.abc import Mapping

from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    validate_runtime_record,
)
from aigineering.protocol.wire import contract_from_dict

PRIORITY_FACT = "task.priority.changed"
PRIORITY_EFFECT = "task.priority.set"


class PriorityStateRejected(ValueError):
    """A valid priority request conflicts with the exact current task state."""


def _contract_declaration(
    context: EffectProjectionContext, contract_id: str
) -> RuntimeRecord:
    matches = [
        record
        for record in context.runtime_records
        if record.record_type == "contract.declared"
        and isinstance(record.payload.get("contract"), Mapping)
        and record.payload["contract"].get("id") == contract_id
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Contract {contract_id!r} has no unique canonical declaration"
        )
    return matches[0]


def priority_declaration(
    context: EffectProjectionContext, contract_id: str
) -> RuntimeRecord:
    """Resolve one exact declared Contract fact from a projection snapshot."""
    return _contract_declaration(context, contract_id)


def priority_history(
    context: EffectProjectionContext, contract_id: str
) -> tuple[RuntimeRecord, ...]:
    """Return a fail-closed, provenance-linked history in fact order."""
    return priority_histories(context, (contract_id,))[contract_id]


def priority_histories(
    context: EffectProjectionContext, contract_ids: tuple[str, ...]
) -> dict[str, tuple[RuntimeRecord, ...]]:
    """Validate and return several chains from one indexed fact snapshot."""
    if not isinstance(contract_ids, tuple) or len(contract_ids) != len(
        set(contract_ids)
    ):
        raise ValueError("contract_ids must be a tuple of unique Contract IDs")
    requested = set(contract_ids)
    contracts: dict[str, list] = {contract_id: [] for contract_id in requested}
    for contract in context.contracts:
        if contract.id in requested:
            contracts[contract.id].append(contract)
    declarations: dict[str, list[RuntimeRecord]] = {
        contract_id: [] for contract_id in requested
    }
    grouped: dict[str, list[RuntimeRecord]] = {
        contract_id: [] for contract_id in requested
    }
    by_id: dict[str, list[RuntimeRecord]] = {}
    receipts: dict[str, list[RuntimeRecord]] = {}
    commits: dict[str, list[RuntimeRecord]] = {}
    for record in context.runtime_records:
        by_id.setdefault(record.id, []).append(record)
        if record.record_type == "candidate.received":
            candidate_id = record.payload.get("candidate_id")
            if isinstance(candidate_id, str):
                receipts.setdefault(candidate_id, []).append(record)
        elif record.record_type == "candidate.committed":
            candidate_id = record.payload.get("candidate_id")
            if isinstance(candidate_id, str):
                commits.setdefault(candidate_id, []).append(record)
        if record.record_type == "contract.declared":
            payload = record.payload.get("contract")
            contract_id = payload.get("id") if isinstance(payload, Mapping) else None
            if contract_id in requested:
                declarations[contract_id].append(record)
        elif record.record_type == PRIORITY_FACT:
            contract_id = record.payload.get("contract_id")
            if contract_id in requested:
                grouped[contract_id].append(record)

    result: dict[str, tuple[RuntimeRecord, ...]] = {}
    for contract_id in contract_ids:
        matching_contracts = contracts[contract_id]
        matching_declarations = declarations[contract_id]
        if len(matching_contracts) != 1 or len(matching_declarations) != 1:
            raise ValueError(
                f"Contract {contract_id!r} has no unique canonical declaration"
            )
        contract = matching_contracts[0]
        declaration = matching_declarations[0]
        try:
            validate_runtime_record(declaration)
            declared = contract_from_dict(deep_thaw(declaration.payload["contract"]))
            validate_contract_commitment(declared)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Contract {contract_id!r} declaration is malformed"
            ) from exc
        if declared != contract:
            raise ValueError(
                f"Contract {contract_id!r} declaration does not match its projection"
            )
        records = tuple(grouped[contract_id])
        previous_id = ""
        previous_value: int | None = None
        seen: set[str] = set()
        for record in records:
            _validate_priority_record(
                record,
                contract_id,
                previous_id,
                declaration.id,
                by_id,
                receipts,
                commits,
            )
            if record.id in seen:
                raise ValueError(
                    f"priority history chain is malformed for Contract {contract_id}"
                )
            value = int(record.payload["priority"])
            if previous_value == value or (previous_value is None and value == 0):
                raise ValueError(
                    f"priority history contains a no-op for Contract {contract_id}"
                )
            seen.add(record.id)
            previous_id = record.id
            previous_value = value
        result[contract_id] = records
    return result


def _validate_priority_record(
    record: RuntimeRecord,
    contract_id: str,
    previous: str,
    declaration_id: str,
    by_id: dict[str, list[RuntimeRecord]],
    receipts_by_candidate: dict[str, list[RuntimeRecord]],
    commits_by_candidate: dict[str, list[RuntimeRecord]],
) -> None:
    try:
        validate_runtime_record(record)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"malformed priority fact for Contract {contract_id}") from exc
    payload = record.payload
    if set(payload) != {
        "actor_id",
        "candidate_id",
        "contract_id",
        "key_id",
        "previous_priority_id",
        "priority",
        "reason",
    }:
        raise ValueError(f"malformed priority history for Contract {contract_id}")
    if payload["contract_id"] != contract_id:
        raise ValueError(f"malformed priority history for Contract {contract_id}")
    priority = payload["priority"]
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or not -100 <= priority <= 100
    ):
        raise ValueError(f"malformed priority history for Contract {contract_id}")
    for name in ("actor_id", "candidate_id", "key_id", "reason"):
        if not isinstance(payload[name], str) or not payload[name].strip():
            raise ValueError(
                f"malformed priority provenance for Contract {contract_id}"
            )
    if payload["previous_priority_id"] != previous:
        raise ValueError(f"priority history chain is malformed for {contract_id}")
    expected_parents = (declaration_id, *((previous,) if previous else ()))
    if (
        len(record.causal_parents) != len(expected_parents) + 1
        or tuple(record.causal_parents[1:]) != expected_parents
    ):
        raise ValueError(
            f"priority fact parents are malformed for Contract {contract_id}"
        )
    receipts = (
        [
            item
            for item in by_id.get(record.causal_parents[0], ())
            if item.record_type == "candidate.received"
        ]
        if record.causal_parents
        else []
    )
    if len(receipts) != 1:
        raise ValueError(f"priority fact receipt is missing for Contract {contract_id}")
    receipt = receipts[0]
    try:
        validate_runtime_record(receipt)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"priority fact receipt is malformed for Contract {contract_id}"
        ) from exc
    if (
        receipt.payload.get("candidate_id") != payload["candidate_id"]
        or receipt.payload.get("actor_id") != payload["actor_id"]
        or receipt.payload.get("key_id") != payload["key_id"]
        or tuple(receipt.payload.get("effect_types", ())) != (PRIORITY_EFFECT,)
        or tuple(receipt.causal_parents) != expected_parents
        or not receipt.payload.get("signature")
    ):
        raise ValueError(
            f"priority fact receipt does not bind its provenance for {contract_id}"
        )
    if receipts != receipts_by_candidate.get(payload["candidate_id"], []):
        raise ValueError(
            f"priority receipt index is malformed for Contract {contract_id}"
        )
    committed_heads = commits_by_candidate.get(payload["candidate_id"], [])
    if len(committed_heads) != 1:
        raise ValueError(
            f"priority fact commitment is missing for Contract {contract_id}"
        )
    committed = committed_heads[0]
    try:
        validate_runtime_record(committed)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"priority fact commitment is malformed for Contract {contract_id}"
        ) from exc
    member_ids = tuple(committed.payload.get("committed_record_ids", ()))
    if member_ids != (record.id,) or committed.causal_parents != (
        receipt.id,
        record.id,
    ):
        raise ValueError(
            f"priority fact is not the exact committed member for {contract_id}"
        )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    try:
        if len(value.encode("utf-8", errors="strict")) > 65536:
            raise ValueError(f"{field} exceeds 65536 UTF-8 bytes")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    return value
