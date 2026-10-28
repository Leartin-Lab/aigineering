"""Signed Candidates for one-Contract Worker assignment transitions."""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

from aigineering.core.claim_assignments import (
    AssignmentAdmission,
    AssignmentIntent,
    AssignmentMode,
    AssignmentTransition,
    assignment_context_from_projection,
    build_assignment_fact,
    validate_assignment_request,
)
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.effect_projectors import EffectProjection
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.wire import contract_to_dict

ASSIGNMENT_FACT = "claim.assignment.changed"
_FACT_FIELDS = frozenset(
    {
        "actor_id",
        "candidate_id",
        "contract_id",
        "key_id",
        "previous_assignment_id",
        "reason",
        "worker_id",
    }
)


def project_task_assignment(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Project a signed direct set, replace, or clear for one Contract."""
    if effect.effect_type != "task.assign":
        raise ValueError("assignment projector requires task.assign")
    if candidate.effects != (effect,):
        raise ValueError("task.assign Candidate must contain exactly one effect")
    payload = effect.payload
    if set(payload) != {
        "contract_id",
        "previous_assignment_id",
        "reason",
        "worker_id",
    }:
        raise ValueError("task.assign payload fields are invalid")
    contract_id = _required_text(payload["contract_id"], "contract_id")
    previous_id = payload["previous_assignment_id"]
    if not isinstance(previous_id, str):
        raise ValueError("task.assign previous_assignment_id must be text")
    reason = _required_text(payload["reason"], "reason")
    worker_id = payload["worker_id"]
    if not isinstance(worker_id, str):
        raise ValueError("task.assign worker_id must be text")
    if worker_id:
        _required_text(worker_id, "worker_id")

    replay = _replay_projection(candidate, contract_id, context)
    if replay is not None:
        return replay

    assignment_context = assignment_context_from_projection(context)
    contract = next(
        (item for item in context.contracts if item.id == contract_id), None
    )
    if contract is None:
        raise ValueError(f"task.assign references unknown Contract {contract_id!r}")
    declaration = _declaration_record(contract, context.runtime_records)
    transition = AssignmentTransition(
        target_contract_id=contract_id,
        worker_id=worker_id,
        previous_assignment_id=previous_id,
        reason=reason,
        mode=AssignmentMode.DIRECT,
    )
    validate_assignment_request(
        assignment_context,
        transition,
        target_contract=contract,
        target_declaration_record=declaration,
    )
    record = build_assignment_fact(
        assignment_context,
        transition,
        candidate=candidate,
        receipt_id=receipt_id,
        target_contract=contract,
        target_declaration_record=declaration,
    )
    return EffectProjection(
        records=(record,),
        relation_target=contract_id,
        assignment_intents=(
            AssignmentIntent(
                assignment_record_id=record.id,
                target_contract_id=contract_id,
                target_declaration_record_id=declaration.id,
                mode=AssignmentMode.DIRECT,
            ),
        ),
    )


def assignment_history(
    runtime_records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return immutable assignment facts in the supplied durable order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type == ASSIGNMENT_FACT
        and (contract_id is None or record.payload.get("contract_id") == contract_id)
    )


def _replay_projection(
    candidate: CandidateProposal,
    contract_id: str,
    context: EffectProjectionContext,
) -> EffectProjection | None:
    heads = tuple(
        record
        for record in context.runtime_records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate.id
    )
    if not heads:
        return None
    if len(heads) != 1:
        raise ValueError("task.assign replay has multiple committed Candidate heads")
    head = heads[0]
    listed = head.payload.get("committed_record_ids")
    if not isinstance(listed, (tuple, list)) or any(
        not isinstance(record_id, str) or not record_id for record_id in listed
    ):
        raise ValueError("task.assign replay has malformed committed record IDs")
    if len(set(listed)) != len(listed):
        raise ValueError("task.assign replay duplicates committed record IDs")
    records_by_id = {record.id: record for record in context.runtime_records}
    if any(record_id not in records_by_id for record_id in listed):
        raise ValueError("task.assign replay is missing committed projection facts")
    records = tuple(records_by_id[record_id] for record_id in listed)
    facts = tuple(
        record
        for record in records
        if record.record_type == ASSIGNMENT_FACT
        and record.payload.get("candidate_id") == candidate.id
    )
    if len(facts) != 1:
        raise ValueError("task.assign replay has no unique assignment fact")
    fact = facts[0]
    if (
        set(fact.payload) != _FACT_FIELDS
        or fact.payload.get("contract_id") != contract_id
        or fact.payload.get("actor_id") != candidate.actor_id
        or fact.payload.get("key_id") != candidate.key_id
        or fact.id not in head.causal_parents
    ):
        raise ValueError("task.assign replay fact provenance is invalid")
    receipts = tuple(
        record
        for record in context.runtime_records
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
        and record.payload.get("actor_id") == candidate.actor_id
        and record.payload.get("key_id") == candidate.key_id
    )
    if len(receipts) != 1 or receipts[0].id not in head.causal_parents:
        raise ValueError("task.assign replay has no matching Candidate receipt")
    if any(record_id not in head.causal_parents for record_id in listed):
        raise ValueError("task.assign replay head does not own its listed facts")
    return EffectProjection(
        records=records,
        relation_target=contract_id,
        assignment_intents=(
            AssignmentIntent(
                assignment_record_id=fact.id,
                target_contract_id=contract_id,
                target_declaration_record_id=_declaration_record(
                    next(
                        (item for item in context.contracts if item.id == contract_id),
                        None,
                    ),
                    context.runtime_records,
                ).id,
                mode=AssignmentMode.DIRECT,
            ),
        ),
    )


def _declaration_record(
    contract, runtime_records: Iterable[RuntimeRecord]
) -> RuntimeRecord:
    expected = deep_thaw(contract_to_dict(contract))
    matches = tuple(
        record
        for record in runtime_records
        if record.record_type == "contract.declared"
        and record.payload.get("contract", {}).get("id") == contract.id
    )
    if (
        len(matches) != 1
        or deep_thaw(matches[0].payload.get("contract", {})) != expected
    ):
        raise ValueError("task.assign target has no exact committed declaration fact")
    return matches[0]


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task.assign {field} must be non-empty text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"task.assign {field} must be valid UTF-8") from exc
    if size > 64 * 1024:
        raise ValueError(f"task.assign {field} exceeds 65536 UTF-8 bytes")
    return value


TASK_ASSIGNMENT_EFFECTS = MappingProxyType(
    {
        "task.assign": EffectHandler(
            capability="task.assignment.manage",
            projector=project_task_assignment,
            emitted_fact_types=frozenset({ASSIGNMENT_FACT}),
            assignment_admission=AssignmentAdmission.DIRECT,
            direct_output_fact_types=frozenset({ASSIGNMENT_FACT}),
            reserved_fact_types=frozenset(),
        )
    }
)


__all__ = [
    "ASSIGNMENT_FACT",
    "TASK_ASSIGNMENT_EFFECTS",
    "assignment_history",
    "project_task_assignment",
]
