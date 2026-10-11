"""Signed advisory task priority effects."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from aigineering.core.effect_projectors import EffectProjection
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.task_successors import ProjectionStoreView
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
    runtime_record_effective_payload,
    validate_runtime_record,
)
from aigineering.protocol.wire import contract_from_dict

PRIORITY_FACT = "task.priority.changed"
PRIORITY_EFFECT = "task.priority.set"


class PriorityStateRejected(ValueError):
    """A valid priority request conflicts with the exact current task state."""


def task_priority_projector(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    if candidate.effects != (effect,):
        raise ValueError("task priority Candidate must contain exactly one effect")
    payload = effect.payload
    if set(payload) != {
        "contract_id",
        "priority",
        "previous_priority_id",
        "reason",
    }:
        raise ValueError("task priority effect payload fields are invalid")
    contract_id = _text(payload["contract_id"], "contract_id")
    reason = _text(payload["reason"], "reason")
    priority = payload["priority"]
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or not -100 <= priority <= 100
    ):
        raise ValueError("priority must be an integer from -100 through 100")
    previous_id = payload["previous_priority_id"]
    if not isinstance(previous_id, str):
        raise ValueError("previous_priority_id must be text")

    contract = next(
        (item for item in context.contracts if item.id == contract_id), None
    )
    if contract is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    declaration = _contract_declaration(context, contract_id)
    expected_candidate_parents = (
        declaration.id,
        *((previous_id,) if previous_id else ()),
    )
    if candidate.causal_parents != expected_candidate_parents:
        raise ValueError("task priority Candidate has incorrect causal parents")
    proposed = create_runtime_record(
        PRIORITY_FACT,
        {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "contract_id": contract_id,
            "key_id": candidate.key_id,
            "previous_priority_id": previous_id,
            "priority": priority,
            "reason": reason,
        },
        causal_parents=(
            receipt_id,
            declaration.id,
            *((previous_id,) if previous_id else ()),
        ),
    )
    existing = next(
        (record for record in context.runtime_records if record.id == proposed.id),
        None,
    )
    if existing is not None:
        if priority_history(context, contract_id).count(existing) != 1:
            raise ValueError("priority replay is not a committed history member")
        if runtime_record_effective_payload(
            existing
        ) != runtime_record_effective_payload(proposed):
            raise ValueError("priority fact ID conflicts with existing history")
        return EffectProjection((existing,), contract_id)

    validate_task_priority_request(context, contract_id, priority, previous_id)

    return EffectProjection((proposed,), contract_id)


def validate_task_priority_request(
    context: EffectProjectionContext,
    contract_id: str,
    priority: int,
    previous_priority_id: str,
):
    """Validate mutable-independent priority intent against one fact snapshot."""
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be non-empty text")
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or not -100 <= priority <= 100
    ):
        raise ValueError("priority must be an integer from -100 through 100")
    if not isinstance(previous_priority_id, str):
        raise ValueError("previous_priority_id must be text")
    contracts = [item for item in context.contracts if item.id == contract_id]
    if len(contracts) != 1:
        raise LookupError(f"unknown or ambiguous Contract {contract_id!r}")
    contract = contracts[0]
    declaration = _contract_declaration(context, contract_id)
    if contract is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    history = priority_history(context, contract_id)
    head = history[-1] if history else None
    head_id = head.id if head is not None else ""
    if previous_priority_id != head_id:
        raise PriorityStateRejected(
            "previous_priority_id does not match the exact current head"
        )
    if head is not None and head.payload["priority"] == priority:
        raise PriorityStateRejected(
            "priority is unchanged; no priority fact was created"
        )
    if head is None and priority == 0:
        raise PriorityStateRejected(
            "priority zero is already the default; no fact was created"
        )
    if any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == contract_id
        for record in context.runtime_records
    ):
        raise PriorityStateRejected("terminal task priority cannot be changed")
    if contract.outputs and all_outputs_satisfied(
        contract, ProjectionStoreView(context), require_outputs=True
    ):
        raise PriorityStateRejected("completed task priority cannot be changed")
    return contract, declaration, head


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


TASK_PRIORITY_EFFECTS = MappingProxyType(
    {
        PRIORITY_EFFECT: EffectHandler(
            capability="task.priority.manage",
            projector=task_priority_projector,
            direct_output_fact_types=frozenset({PRIORITY_FACT}),
            reserved_fact_types=frozenset({PRIORITY_FACT}),
        )
    }
)
