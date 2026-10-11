"""Pure signed effects for claim-gating task dependencies."""

from __future__ import annotations

from collections.abc import Iterable
from types import MappingProxyType

from aigineering.core.claim_constraints import (
    PLACED,
    PREDICATE,
    RELEASED,
    REQUIRED_TERMINAL,
    open_claim_constraints,
    validate_claim_constraint_transition,
)
from aigineering.core.effect_projectors import EffectProjection
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.task_successors import ProjectionStoreView
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record


def project_task_dependency_link_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Project one exact prerequisite placement or release."""
    if effect.effect_type == "task.dependency.link":
        return _project_link(effect, candidate, receipt_id, context)
    if effect.effect_type == "task.dependency.unlink":
        return _project_unlink(effect, candidate, receipt_id, context)
    raise ValueError("task dependency projector received an unsupported effect")


def _project_link(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    if candidate.effects != (effect,):
        raise ValueError("dependency link Candidate must contain exactly one effect")
    payload = effect.payload
    if set(payload) != {"contract_id", "prerequisite_contract_id", "reason"}:
        raise ValueError("dependency link payload fields are invalid")
    target = _required_text(payload["contract_id"], "contract_id")
    prerequisite = _required_text(
        payload["prerequisite_contract_id"], "prerequisite_contract_id"
    )
    reason = _required_text(payload["reason"], "reason")
    contracts = {contract.id: contract for contract in context.contracts}
    if target not in contracts:
        raise ValueError(
            f"dependency link references unknown target Contract {target!r}"
        )
    if prerequisite not in contracts:
        raise ValueError(
            f"dependency link references unknown prerequisite Contract {prerequisite!r}"
        )

    record = create_runtime_record(
        PLACED,
        {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "contract_id": target,
            "key_id": candidate.key_id,
            "predicate": PREDICATE,
            "prerequisite_contract_id": prerequisite,
            "reason": reason,
            "required_terminal": REQUIRED_TERMINAL,
        },
        causal_parents=(receipt_id,),
    )
    previous = next(
        (item for item in context.runtime_records if item.id == record.id), None
    )
    if previous is not None:
        return EffectProjection((previous,), target)

    view = ProjectionStoreView(context)
    output_satisfied_ids = frozenset(
        contract_id
        for contract_id, contract in contracts.items()
        if contract.outputs and all_outputs_satisfied(contract, view)
    )
    active = validate_claim_constraint_transition(
        context.runtime_records,
        record,
        known_contract_ids=frozenset(contracts),
        claimed_contract_ids=_claimed_contract_ids(context.runtime_records),
        terminal_contract_ids=_terminal_contract_ids(context.runtime_records),
        output_satisfied_contract_ids=output_satisfied_ids,
    )
    del active
    return EffectProjection((record,), target)


def _project_unlink(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    if candidate.effects != (effect,):
        raise ValueError("dependency unlink Candidate must contain exactly one effect")
    payload = effect.payload
    if set(payload) != {"contract_id", "constraint_id", "reason"}:
        raise ValueError("dependency unlink payload fields are invalid")
    target = _required_text(payload["contract_id"], "contract_id")
    constraint_id = _required_text(payload["constraint_id"], "constraint_id")
    reason = _required_text(payload["reason"], "reason")
    contracts = {contract.id: contract for contract in context.contracts}
    if target not in contracts:
        raise ValueError(
            f"dependency unlink references unknown target Contract {target!r}"
        )

    placement = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == PLACED
            and record.id == constraint_id
            and record.payload["contract_id"] == target
        ),
        None,
    )
    if placement is None:
        raise ValueError("dependency unlink requires an open exact link on the target")
    record = create_runtime_record(
        RELEASED,
        {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "constraint_id": constraint_id,
            "contract_id": target,
            "key_id": candidate.key_id,
            "predicate": PREDICATE,
            "prerequisite_contract_id": str(
                placement.payload["prerequisite_contract_id"]
            ),
            "reason": reason,
            "required_terminal": REQUIRED_TERMINAL,
        },
        causal_parents=(receipt_id, constraint_id),
    )
    previous = next(
        (item for item in context.runtime_records if item.id == record.id), None
    )
    if previous is not None:
        return EffectProjection((previous,), target)

    if not any(
        active.id == constraint_id
        for active in open_claim_constraints(
            context.runtime_records, contract_id=target
        )
    ):
        raise ValueError("dependency unlink requires an open exact link on the target")

    validate_claim_constraint_transition(
        context.runtime_records,
        record,
        known_contract_ids=frozenset(contracts),
        claimed_contract_ids=_claimed_contract_ids(context.runtime_records),
        terminal_contract_ids=_terminal_contract_ids(context.runtime_records),
        # Unlink only removes a prior claim gate; it cannot create new work or
        # reverse output-derived completion, so output satisfaction is not a
        # reason to make this cleanup transition impossible.
        output_satisfied_contract_ids=frozenset(),
    )
    return EffectProjection((record,), target)


def project_task_dependencies(
    runtime_records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return immutable dependency placements and releases in supplied order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type in {PLACED, RELEASED}
        and (contract_id is None or record.payload.get("contract_id") == contract_id)
    )


def _claimed_contract_ids(records: Iterable[RuntimeRecord]) -> frozenset[str]:
    return frozenset(
        str(record.payload["contract_id"])
        for record in records
        if record.record_type == "claim.granted"
        and isinstance(record.payload.get("contract_id"), str)
    )


def _terminal_contract_ids(records: Iterable[RuntimeRecord]) -> frozenset[str]:
    return frozenset(
        str(record.payload["contract_id"])
        for record in records
        if record.record_type == "lifecycle.terminal"
        and isinstance(record.payload.get("contract_id"), str)
    )


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task dependency {field} must be non-empty text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"task dependency {field} must be valid UTF-8") from exc
    if size > 64 * 1024:
        raise ValueError(f"task dependency {field} exceeds 65536 UTF-8 bytes")
    return value


TASK_DEPENDENCY_EFFECTS = MappingProxyType(
    {
        "task.dependency.link": EffectHandler(
            capability="task.dependency.manage",
            projector=project_task_dependency_link_effect,
            direct_output_fact_types=frozenset({PLACED}),
            reserved_fact_types=frozenset(),
        ),
        "task.dependency.unlink": EffectHandler(
            capability="task.dependency.manage",
            projector=project_task_dependency_link_effect,
            direct_output_fact_types=frozenset({RELEASED}),
            reserved_fact_types=frozenset(),
        ),
    }
)


__all__ = [
    "TASK_DEPENDENCY_EFFECTS",
    "project_task_dependencies",
    "project_task_dependency_link_effect",
]
