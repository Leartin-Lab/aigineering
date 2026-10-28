"""Immutable task-to-Worker assignment facts and claim-time validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.projection_store_view import ProjectionStoreView
from aigineering.core.worker_routing import (
    WorkerRegistration,
    is_compatible,
    registration_from_record,
)
from aigineering.protocol.candidate import CandidateProposal
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
    validate_runtime_record,
)
from aigineering.protocol.types import Contract
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.identity import contract_from_fields


ASSIGNMENT_FACT = "claim.assignment.changed"
_ASSIGNMENT_FIELDS = frozenset(
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
_MAX_TEXT_BYTES = 65536


class AssignmentAdmission(str, Enum):
    NONE = "none"
    DIRECT = "direct"
    SAME_OBLIGATION_SUCCESSOR = "same_obligation_successor"


class AssignmentMode(str, Enum):
    DIRECT = "direct"
    INHERIT = "inherit"
    OVERRIDE = "override"


class AssignmentConflict(ValueError):
    """An assignment fact or transition conflicts with current task state."""


@dataclass(frozen=True)
class AssignmentFactContext:
    projection: EffectProjectionContext


@dataclass(frozen=True)
class AssignmentTransition:
    target_contract_id: str
    worker_id: str
    previous_assignment_id: str
    reason: str
    mode: AssignmentMode = AssignmentMode.DIRECT
    source_contract_id: str = ""
    source_assignment_head_id: str = ""


@dataclass(frozen=True)
class AssignmentIntent:
    assignment_record_id: str
    target_contract_id: str
    target_declaration_record_id: str
    mode: AssignmentMode


@dataclass(frozen=True)
class SuccessorAssignmentWitness:
    source_contract_id: str
    source_assignment_head_id: str
    source_terminal_record_id: str
    successor_contract_id: str
    successor_declaration_record_id: str
    successor_assignment_record_id: str
    successor_link_record_id: str
    assignment_mode: AssignmentMode


@dataclass(frozen=True)
class EffectFactOwner:
    record_id: str
    effect_type: str
    assignment_admission: AssignmentAdmission


def assignment_context_from_projection(
    context: EffectProjectionContext,
) -> AssignmentFactContext:
    if not isinstance(context, EffectProjectionContext):
        raise TypeError("assignment context requires EffectProjectionContext")
    return AssignmentFactContext(context)


def _validate_assignment_record(record: RuntimeRecord) -> None:
    if record.record_type != ASSIGNMENT_FACT:
        return
    try:
        validate_runtime_record(record)
    except ValueError as exc:
        raise AssignmentConflict("assignment fact identity is invalid") from exc
    if record.schema_version != 1:
        raise AssignmentConflict("unsupported assignment fact schema version")
    payload = record.payload
    if set(payload) != _ASSIGNMENT_FIELDS:
        raise AssignmentConflict("assignment fact fields are invalid")
    for field in _ASSIGNMENT_FIELDS:
        value = payload[field]
        if not isinstance(value, str):
            raise AssignmentConflict(f"assignment {field} must be text")
        try:
            size = len(value.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise AssignmentConflict(f"assignment {field} is not valid UTF-8") from exc
        if size > _MAX_TEXT_BYTES:
            raise AssignmentConflict(f"assignment {field} is too long")
    for field in (
        "actor_id",
        "candidate_id",
        "contract_id",
        "key_id",
        "reason",
    ):
        if not payload[field].strip():
            raise AssignmentConflict(f"assignment {field} must not be blank")
    if not record.causal_parents:
        raise AssignmentConflict("assignment fact lacks Candidate provenance")


def assignment_history(
    context: AssignmentFactContext, *, contract_id: str
) -> tuple[RuntimeRecord, ...]:
    records = tuple(
        record
        for record in context.projection.runtime_records
        if record.record_type == ASSIGNMENT_FACT
        and record.payload.get("contract_id") == contract_id
    )
    heads: set[str] = set()
    previous = ""
    for record in records:
        _validate_assignment_record(record)
        if record.id in heads or record.payload["previous_assignment_id"] != previous:
            raise AssignmentConflict("assignment history predecessor is invalid")
        heads.add(record.id)
        previous = record.id
    return records


def assignment_head(
    context: AssignmentFactContext, *, contract_id: str
) -> RuntimeRecord | None:
    history = assignment_history(context, contract_id=contract_id)
    return history[-1] if history else None


def effective_assignment(
    context: AssignmentFactContext, *, contract_id: str
) -> RuntimeRecord | None:
    head = assignment_head(context, contract_id=contract_id)
    return head if head is not None and head.payload["worker_id"] else None


def _contract_declaration(
    context: AssignmentFactContext,
    target_contract: Contract,
    declaration_record: RuntimeRecord,
) -> None:
    if declaration_record.record_type != "contract.declared":
        raise AssignmentConflict("assignment target lacks a Contract declaration")
    _validate_runtime(declaration_record, "Contract declaration")
    declaration = declaration_record.payload.get("contract")
    if not isinstance(declaration, Mapping):
        raise AssignmentConflict("assignment target declaration is malformed")
    if declaration.get("id") != target_contract.id:
        raise AssignmentConflict("assignment declaration targets another Contract")
    try:
        declaration_fields = deep_thaw(declaration)
        declared_id = declaration_fields.pop("id")
        declared_contract = contract_from_fields(**declaration_fields)
    except (TypeError, ValueError) as exc:
        raise AssignmentConflict("assignment target declaration is malformed") from exc
    if declared_id != declared_contract.id or declared_contract != target_contract:
        raise AssignmentConflict("assignment target differs from its declaration")
    if declaration_record not in context.projection.runtime_records:
        # Pending declarations are admitted only through an EffectFactOwner and
        # are checked again by validate_assignment_batch.
        return
    current = next(
        (
            item
            for item in context.projection.contracts
            if item.id == target_contract.id
        ),
        None,
    )
    if current != target_contract:
        raise AssignmentConflict("assignment target differs from its declaration")


def _validate_runtime(record: RuntimeRecord, label: str) -> None:
    try:
        validate_runtime_record(record)
    except ValueError as exc:
        raise AssignmentConflict(f"{label} identity is invalid") from exc


def _registration_record(
    context: AssignmentFactContext, worker_id: str
) -> tuple[WorkerRegistration, RuntimeRecord]:
    registration = next(
        (
            item
            for item in context.projection.registrations
            if item.worker_id == worker_id
        ),
        None,
    )
    if registration is None or not registration.enabled:
        raise AssignmentConflict("assignment Worker is not currently enabled")
    candidates = tuple(
        record
        for record in context.projection.runtime_records
        if record.record_type == "worker.registered"
        and record.payload.get("worker_id") == worker_id
    )
    matching = tuple(
        record
        for record in candidates
        if record.payload.get("version") == registration.version
        and record.payload.get("actor_id", "") == registration.actor_id
        and record.payload.get("key_id", "") == registration.key_id
        and tuple(record.payload.get("capabilities", ())) == registration.capabilities
        and tuple(record.payload.get("pools", ())) == registration.pools
        and record.payload.get("enabled") is registration.enabled
    )
    if len(matching) != 1:
        raise AssignmentConflict("assignment Worker registration fact is ambiguous")
    return registration, matching[0]


def _source_head(
    context: AssignmentFactContext, transition: AssignmentTransition
) -> RuntimeRecord | None:
    if transition.mode is AssignmentMode.DIRECT:
        if transition.source_contract_id or transition.source_assignment_head_id:
            raise AssignmentConflict("direct assignment cannot name a source")
        return None
    if not transition.source_contract_id:
        raise AssignmentConflict("successor assignment requires a source Contract")
    source = next(
        (
            contract
            for contract in context.projection.contracts
            if contract.id == transition.source_contract_id
        ),
        None,
    )
    if source is None:
        raise AssignmentConflict("assignment source Contract is unknown")
    head = assignment_head(context, contract_id=source.id)
    head_id = head.id if head is not None else ""
    if head_id != transition.source_assignment_head_id:
        raise AssignmentConflict("assignment source head changed")
    return head


def validate_assignment_request(
    context: AssignmentFactContext,
    transition: AssignmentTransition,
    *,
    target_contract: Contract,
    target_declaration_record: RuntimeRecord,
    source_terminal_record: RuntimeRecord | None = None,
) -> None:
    """Pure shared preflight for plugin construction and writer validation."""
    if not isinstance(transition.mode, AssignmentMode):
        raise AssignmentConflict("assignment mode is invalid")
    if transition.target_contract_id != target_contract.id:
        raise AssignmentConflict("assignment target Contract does not match")
    _contract_declaration(context, target_contract, target_declaration_record)
    is_pending = target_declaration_record not in context.projection.runtime_records
    if is_pending:
        if (
            target_contract.id not in {item.id for item in context.projection.contracts}
            and transition.mode is AssignmentMode.DIRECT
        ):
            raise AssignmentConflict("direct assignment target is undeclared")
        if transition.mode is not AssignmentMode.DIRECT:
            if target_contract.parent_id != transition.source_contract_id:
                raise AssignmentConflict(
                    "successor is not a direct child of its source"
                )
    else:
        if target_contract not in context.projection.contracts:
            raise AssignmentConflict("assignment target Contract is unknown")
    current_head = assignment_head(context, contract_id=target_contract.id)
    current_head_id = current_head.id if current_head is not None else ""
    if transition.previous_assignment_id != current_head_id:
        if not (
            is_pending and not current_head and not transition.previous_assignment_id
        ):
            raise AssignmentConflict("assignment predecessor is stale")
    current_worker_id = (
        str(current_head.payload.get("worker_id", "")) if current_head else ""
    )
    if (
        transition.mode is AssignmentMode.DIRECT
        and transition.worker_id == current_worker_id
    ):
        raise AssignmentConflict("assignment does not change the current Worker")
    if (
        transition.mode is AssignmentMode.DIRECT
        and not transition.worker_id
        and not current_head
    ):
        raise AssignmentConflict("cannot clear an unassigned Contract")
    if target_contract.id in context.projection.claim_history_contract_ids:
        raise AssignmentConflict("Contract with claim history cannot be assigned")
    if any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == target_contract.id
        for record in context.projection.runtime_records
    ):
        raise AssignmentConflict("terminal Contract cannot be assigned")
    if target_contract.outputs and all_outputs_satisfied(
        target_contract, ProjectionStoreView(context.projection), require_outputs=True
    ):
        raise AssignmentConflict("output-satisfied Contract cannot be assigned")
    source_head = _source_head(context, transition)
    if transition.mode is AssignmentMode.DIRECT:
        if source_terminal_record is not None:
            raise AssignmentConflict("direct assignment cannot carry source terminal")
    else:
        if source_terminal_record is None:
            raise AssignmentConflict(
                "successor assignment requires source cancellation"
            )
        _validate_runtime(source_terminal_record, "source terminal")
        if (
            source_terminal_record.record_type != "lifecycle.terminal"
            or source_terminal_record.payload.get("contract_id")
            != transition.source_contract_id
            or source_terminal_record.payload.get("terminal") != "cancelled"
        ):
            raise AssignmentConflict("successor source is not cancelled in this batch")
        if transition.mode is AssignmentMode.INHERIT:
            source_worker = (
                str(source_head.payload.get("worker_id", "")) if source_head else ""
            )
            if transition.worker_id != source_worker:
                raise AssignmentConflict(
                    "inherited Worker differs from source assignment"
                )
        elif transition.mode is AssignmentMode.OVERRIDE:
            source_worker = (
                str(source_head.payload.get("worker_id", ""))
                if source_head is not None
                else ""
            )
            if not transition.worker_id and not source_worker:
                raise AssignmentConflict("cannot clear an unassigned successor")
            if transition.worker_id and transition.worker_id == source_worker:
                raise AssignmentConflict(
                    "successor override does not change its assigned Worker"
                )
    if transition.worker_id:
        registration, _ = _registration_record(context, transition.worker_id)
        if not is_compatible(target_contract, registration):
            raise AssignmentConflict("assignment Worker is incompatible with Contract")
    if not isinstance(transition.reason, str) or not transition.reason.strip():
        raise AssignmentConflict("assignment reason must be nonblank text")
    try:
        if len(transition.reason.encode("utf-8", errors="strict")) > _MAX_TEXT_BYTES:
            raise AssignmentConflict("assignment reason is too long")
    except UnicodeEncodeError as exc:
        raise AssignmentConflict("assignment reason is not valid UTF-8") from exc


def build_assignment_fact(
    context: AssignmentFactContext,
    transition: AssignmentTransition,
    *,
    candidate: CandidateProposal,
    receipt_id: str,
    target_contract: Contract,
    target_declaration_record: RuntimeRecord,
    source_terminal_record: RuntimeRecord | None = None,
) -> RuntimeRecord:
    validate_assignment_request(
        context,
        transition,
        target_contract=target_contract,
        target_declaration_record=target_declaration_record,
        source_terminal_record=source_terminal_record,
    )
    if not receipt_id:
        raise AssignmentConflict("assignment receipt ID is required")
    current = assignment_head(context, contract_id=target_contract.id)
    parents = [receipt_id, target_declaration_record.id]
    if current is not None:
        parents.append(current.id)
    if transition.worker_id:
        _, registration_record = _registration_record(context, transition.worker_id)
        parents.append(registration_record.id)
    source = _source_head(context, transition)
    if source is not None:
        parents.append(source.id)
    return create_runtime_record(
        ASSIGNMENT_FACT,
        {
            "actor_id": candidate.actor_id,
            "candidate_id": candidate.id,
            "contract_id": target_contract.id,
            "key_id": candidate.key_id,
            "previous_assignment_id": current.id if current else "",
            "reason": transition.reason,
            "worker_id": transition.worker_id,
        },
        causal_parents=tuple(dict.fromkeys(parents)),
    )


def _declarations(
    context: AssignmentFactContext,
    pending_records: Sequence[RuntimeRecord],
    pending_contracts: Sequence[Contract],
) -> dict[str, tuple[Contract, RuntimeRecord]]:
    result: dict[str, tuple[Contract, RuntimeRecord]] = {}
    contracts = {contract.id: contract for contract in context.projection.contracts}
    contracts.update({contract.id: contract for contract in pending_contracts})
    for record in (*context.projection.runtime_records, *pending_records):
        if record.record_type != "contract.declared":
            continue
        value = record.payload.get("contract")
        if not isinstance(value, Mapping):
            continue
        contract_id = str(value.get("id", ""))
        contract = contracts.get(contract_id)
        if contract is not None:
            result[contract_id] = (contract, record)
    return result


def _exact_replay(
    context: AssignmentFactContext,
    pending_records: tuple[RuntimeRecord, ...],
    candidate: CandidateProposal,
    receipt_id: str,
    owners: tuple[EffectFactOwner, ...],
) -> bool:
    heads = tuple(
        record
        for record in context.projection.runtime_records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate.id
    )
    if not heads:
        return False
    if len(heads) != 1:
        raise AssignmentConflict("Candidate has ambiguous committed replay history")
    head = heads[0]
    _validate_runtime(head, "committed Candidate head")
    receipts = tuple(
        record
        for record in context.projection.runtime_records
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
    )
    if len(receipts) != 1 or receipts[0].id != receipt_id:
        raise AssignmentConflict("assignment replay receipt is invalid")
    _validate_runtime(receipts[0], "Candidate receipt")
    if (
        receipts[0].payload.get("actor_id") != candidate.actor_id
        or receipts[0].payload.get("key_id") != candidate.key_id
    ):
        raise AssignmentConflict("assignment replay actor provenance is invalid")
    if receipt_id not in head.causal_parents:
        raise AssignmentConflict("assignment replay head omits its receipt")
    listed = tuple(head.payload.get("committed_record_ids", ()))
    if len(listed) != len(set(listed)):
        raise AssignmentConflict("assignment replay head has duplicate record IDs")
    pending_ids = tuple(record.id for record in pending_records)
    if pending_ids != listed:
        raise AssignmentConflict(
            "assignment replay projection differs from committed head"
        )
    committed_records = {
        record.id: record for record in context.projection.runtime_records
    }
    if any(
        record_id not in head.causal_parents or record_id not in committed_records
        for record_id in listed
    ):
        raise AssignmentConflict("assignment replay head does not own its listed facts")
    owner_ids = tuple(owner.record_id for owner in owners)
    if len(owner_ids) != len(set(owner_ids)) or not set(owner_ids) <= set(listed):
        raise AssignmentConflict("assignment replay fact ownership is invalid")
    return True


def _validate_successor_witness(
    context: AssignmentFactContext,
    records_by_id: Mapping[str, RuntimeRecord],
    owners_by_id: Mapping[str, EffectFactOwner],
    witness: SuccessorAssignmentWitness,
    *,
    replay: bool = False,
) -> None:
    if witness.assignment_mode not in {
        AssignmentMode.INHERIT,
        AssignmentMode.OVERRIDE,
    }:
        raise AssignmentConflict("successor assignment mode is invalid")
    link = records_by_id.get(witness.successor_link_record_id)
    terminal = records_by_id.get(witness.source_terminal_record_id)
    declaration = records_by_id.get(witness.successor_declaration_record_id)
    if link is None or terminal is None or declaration is None:
        raise AssignmentConflict("successor witness facts are missing")
    _validate_runtime(link, "successor link")
    if (
        terminal.record_type != "lifecycle.terminal"
        or terminal.payload.get("contract_id") != witness.source_contract_id
        or terminal.payload.get("terminal") != "cancelled"
    ):
        raise AssignmentConflict("successor source is not cancelled in the batch")
    _validate_runtime(terminal, "source terminal")
    if declaration.record_type != "contract.declared":
        raise AssignmentConflict("successor declaration is invalid")
    declaration_value = declaration.payload.get("contract")
    if (
        not isinstance(declaration_value, Mapping)
        or declaration_value.get("id") != witness.successor_contract_id
        or declaration_value.get("parent_id") != witness.source_contract_id
    ):
        raise AssignmentConflict("successor declaration is not a child of the source")
    owner = owners_by_id.get(link.id)
    if (
        owner is None
        or owner.assignment_admission
        is not AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR
    ):
        raise AssignmentConflict("successor link lacks registered effect ownership")
    related_ids = {
        witness.source_terminal_record_id,
        witness.successor_declaration_record_id,
        witness.successor_link_record_id,
        *(
            (witness.successor_assignment_record_id,)
            if witness.successor_assignment_record_id
            else ()
        ),
    }
    if any(
        item not in owners_by_id
        or owners_by_id[item].effect_type != owner.effect_type
        or owners_by_id[item].assignment_admission
        is not AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR
        for item in related_ids
    ):
        raise AssignmentConflict(
            "successor facts do not share their registered effect owner"
        )
    if (
        not {
            "contract_id",
            "successor_id",
            "assignment_mode",
            "source_assignment_id",
            "assignment_fact_id",
            "worker_id",
        }.issubset(link.payload)
        or link.payload.get("contract_id") != witness.source_contract_id
        or link.payload.get("successor_id") != witness.successor_contract_id
        or link.payload.get("assignment_mode") != witness.assignment_mode.value
        or link.payload.get("source_assignment_id") != witness.source_assignment_head_id
        or link.payload.get("assignment_fact_id")
        != witness.successor_assignment_record_id
    ):
        raise AssignmentConflict("successor link does not bind assignment witness")
    if any(
        parent not in link.causal_parents
        for parent in (
            witness.source_terminal_record_id,
            witness.successor_declaration_record_id,
            *(
                (witness.source_assignment_head_id,)
                if witness.source_assignment_head_id
                else ()
            ),
            *(
                (witness.successor_assignment_record_id,)
                if witness.successor_assignment_record_id
                else ()
            ),
        )
    ):
        raise AssignmentConflict("successor link omits causal witness facts")
    source_head = (
        assignment_head(context, contract_id=witness.source_contract_id)
        if not replay
        else records_by_id.get(witness.source_assignment_head_id)
    )
    actual_head_id = source_head.id if source_head is not None else ""
    if actual_head_id != witness.source_assignment_head_id:
        raise AssignmentConflict("successor source assignment head is stale")
    if source_head is not None:
        _validate_assignment_record(source_head)
        if source_head.payload.get("contract_id") != witness.source_contract_id:
            raise AssignmentConflict("successor source assignment fact is invalid")
    assigned_worker = (
        str(source_head.payload.get("worker_id", "")) if source_head else ""
    )
    assignment_id = witness.successor_assignment_record_id
    if not assignment_id:
        if witness.assignment_mode is AssignmentMode.OVERRIDE or assigned_worker:
            raise AssignmentConflict("successor transition requires an assignment fact")
        if link.payload.get("worker_id", ""):
            raise AssignmentConflict("unassigned successor link names a Worker")
        return
    fact = records_by_id.get(assignment_id)
    if fact is None or fact.record_type != ASSIGNMENT_FACT:
        raise AssignmentConflict("successor assignment fact is missing")
    _validate_assignment_record(fact)
    if (
        fact.payload.get("contract_id") != witness.successor_contract_id
        or (
            witness.source_assignment_head_id
            and witness.source_assignment_head_id not in fact.causal_parents
        )
        or witness.successor_declaration_record_id not in fact.causal_parents
    ):
        raise AssignmentConflict("successor assignment fact lineage is invalid")
    worker_id = str(fact.payload.get("worker_id", ""))
    previous_id = str(fact.payload.get("previous_assignment_id", ""))
    current = assignment_head(context, contract_id=witness.successor_contract_id)
    if not replay and previous_id != (current.id if current else ""):
        raise AssignmentConflict("successor assignment predecessor is invalid")
    if previous_id and previous_id not in fact.causal_parents:
        raise AssignmentConflict("successor assignment omits its predecessor fact")
    if previous_id:
        previous = records_by_id.get(previous_id)
        if (
            previous is None
            or previous.record_type != ASSIGNMENT_FACT
            or previous.payload.get("contract_id") != witness.successor_contract_id
        ):
            raise AssignmentConflict("successor assignment predecessor fact is invalid")
    if worker_id:
        if replay:
            registration_record = next(
                (
                    records_by_id[parent]
                    for parent in fact.causal_parents
                    if parent in records_by_id
                    and records_by_id[parent].record_type == "worker.registered"
                    and records_by_id[parent].payload.get("worker_id") == worker_id
                ),
                None,
            )
            if registration_record is None:
                raise AssignmentConflict(
                    "successor assignment registration fact is missing"
                )
            _validate_runtime(registration_record, "Worker registration")
            historical_registration = registration_from_record(registration_record)
            if not historical_registration.enabled:
                raise AssignmentConflict("successor assignment Worker was disabled")
            contract = next(
                item
                for item in context.projection.contracts
                if item.id == witness.successor_contract_id
            )
            if not is_compatible(contract, historical_registration):
                raise AssignmentConflict("successor assignment Worker was incompatible")
        else:
            _, registration_record = _registration_record(context, worker_id)
        if registration_record.id not in fact.causal_parents:
            raise AssignmentConflict("successor assignment omits Worker registration")
    if link.payload.get("worker_id") != worker_id:
        raise AssignmentConflict("successor link Worker differs from assignment fact")
    if (
        witness.assignment_mode is AssignmentMode.INHERIT
        and worker_id != assigned_worker
    ):
        raise AssignmentConflict("inherited assignment differs from source Worker")
    if (
        witness.assignment_mode is AssignmentMode.OVERRIDE
        and not worker_id
        and not assigned_worker
    ):
        raise AssignmentConflict("cannot clear an unassigned successor")


def validate_assignment_batch(
    context: AssignmentFactContext,
    pending_records: tuple[RuntimeRecord, ...],
    pending_contracts: tuple[Contract, ...],
    *,
    candidate: CandidateProposal,
    receipt_id: str,
    effect_fact_owners: tuple[EffectFactOwner, ...],
    assignment_intents: tuple[AssignmentIntent, ...],
    successor_assignment_witnesses: tuple[SuccessorAssignmentWitness, ...] = (),
) -> None:
    relevant = (
        any(record.record_type == ASSIGNMENT_FACT for record in pending_records)
        or bool(assignment_intents)
        or bool(successor_assignment_witnesses)
    )
    if not relevant:
        return
    owners_by_id: dict[str, EffectFactOwner] = {}
    for owner in effect_fact_owners:
        if owner.record_id in owners_by_id:
            raise AssignmentConflict("projected fact has duplicate effect ownership")
        owners_by_id[owner.record_id] = owner
    intents_by_id: dict[str, AssignmentIntent] = {}
    for intent in assignment_intents:
        if intent.assignment_record_id in intents_by_id:
            raise AssignmentConflict("assignment fact has duplicate intent")
        intents_by_id[intent.assignment_record_id] = intent
    if _exact_replay(
        context, pending_records, candidate, receipt_id, effect_fact_owners
    ):
        assignment_records = tuple(
            record
            for record in pending_records
            if record.record_type == ASSIGNMENT_FACT
        )
        if len(assignment_records) != len(assignment_intents):
            raise AssignmentConflict("replay assignment facts and intents do not match")
        declarations = _declarations(context, pending_records, pending_contracts)
        records_by_id = {
            record.id: record
            for record in (*context.projection.runtime_records, *pending_records)
        }
        for record in pending_records:
            _validate_assignment_record(record)
        for intent in assignment_intents:
            record = next(
                (
                    item
                    for item in pending_records
                    if item.id == intent.assignment_record_id
                ),
                None,
            )
            if record is None or record.record_type != ASSIGNMENT_FACT:
                raise AssignmentConflict("replay assignment fact is missing")
            owner = owners_by_id.get(record.id)
            if owner is None or owner.assignment_admission is AssignmentAdmission.NONE:
                raise AssignmentConflict("replay assignment fact owner is invalid")
            expected_admission = (
                AssignmentAdmission.DIRECT
                if intent.mode is AssignmentMode.DIRECT
                else AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR
            )
            if owner.assignment_admission is not expected_admission:
                raise AssignmentConflict("replay assignment handler permission changed")
            pair = declarations.get(intent.target_contract_id)
            if pair is None or pair[1].id != intent.target_declaration_record_id:
                raise AssignmentConflict("replay assignment declaration is invalid")
            target_contract, declaration = pair
            if (
                record.payload.get("candidate_id") != candidate.id
                or record.payload.get("actor_id") != candidate.actor_id
                or record.payload.get("key_id") != candidate.key_id
                or record.payload.get("contract_id") != intent.target_contract_id
                or receipt_id not in record.causal_parents
                or declaration.id not in record.causal_parents
            ):
                raise AssignmentConflict("replay assignment provenance is invalid")
            previous_id = str(record.payload.get("previous_assignment_id", ""))
            if previous_id and (
                previous_id not in record.causal_parents
                or previous_id not in records_by_id
                or records_by_id[previous_id].record_type != ASSIGNMENT_FACT
                or records_by_id[previous_id].payload.get("contract_id")
                != intent.target_contract_id
            ):
                raise AssignmentConflict("replay assignment predecessor is invalid")
            worker_id = str(record.payload.get("worker_id", ""))
            if worker_id:
                historical_registration = next(
                    (
                        records_by_id[parent]
                        for parent in record.causal_parents
                        if parent in records_by_id
                        and records_by_id[parent].record_type == "worker.registered"
                        and records_by_id[parent].payload.get("worker_id") == worker_id
                    ),
                    None,
                )
                if historical_registration is None:
                    raise AssignmentConflict(
                        "replay Worker registration fact is missing"
                    )
                _validate_runtime(historical_registration, "Worker registration")
                registered = registration_from_record(historical_registration)
                if not registered.enabled or not is_compatible(
                    target_contract, registered
                ):
                    raise AssignmentConflict(
                        "replay Worker registration was incompatible"
                    )
        for witness in successor_assignment_witnesses:
            _validate_successor_witness(
                context, records_by_id, owners_by_id, witness, replay=True
            )
        return

    assignment_records = tuple(
        record for record in pending_records if record.record_type == ASSIGNMENT_FACT
    )
    if len(assignment_records) != len(assignment_intents):
        raise AssignmentConflict("assignment facts and intents do not match")
    if len({intent.target_contract_id for intent in assignment_intents}) != len(
        assignment_intents
    ):
        raise AssignmentConflict(
            "Candidate contains multiple assignments for one Contract"
        )
    declarations = _declarations(context, pending_records, pending_contracts)
    records_by_id = {
        record.id: record
        for record in (*context.projection.runtime_records, *pending_records)
    }
    for record in assignment_records:
        _validate_assignment_record(record)
        owner = owners_by_id.get(record.id)
        if owner is None or owner.assignment_admission is AssignmentAdmission.NONE:
            raise AssignmentConflict(
                "assignment fact was emitted by an unregistered handler"
            )
    seen_witness_successors: set[str] = set()
    for witness in successor_assignment_witnesses:
        if witness.successor_contract_id in seen_witness_successors:
            raise AssignmentConflict("successor has duplicate assignment witnesses")
        seen_witness_successors.add(witness.successor_contract_id)
        _validate_successor_witness(context, records_by_id, owners_by_id, witness)
    for intent in assignment_intents:
        record = records_by_id.get(intent.assignment_record_id)
        if record is None or record.record_type != ASSIGNMENT_FACT:
            raise AssignmentConflict("assignment intent references no assignment fact")
        if record.payload.get("candidate_id") != candidate.id:
            raise AssignmentConflict("assignment fact Candidate attribution is invalid")
        if (
            record.payload.get("actor_id") != candidate.actor_id
            or record.payload.get("key_id") != candidate.key_id
        ):
            raise AssignmentConflict("assignment fact actor provenance is invalid")
        if receipt_id not in record.causal_parents:
            raise AssignmentConflict("assignment fact omits Candidate receipt")
        owner = owners_by_id.get(record.id)
        if owner is None or owner.assignment_admission is AssignmentAdmission.NONE:
            raise AssignmentConflict("assignment fact has no permitted effect owner")
        if intent.mode is AssignmentMode.DIRECT:
            if owner.assignment_admission is not AssignmentAdmission.DIRECT:
                raise AssignmentConflict("direct assignment handler is not permitted")
        elif (
            owner.assignment_admission
            is not AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR
        ):
            raise AssignmentConflict("successor assignment handler is not permitted")
        target_pair = declarations.get(intent.target_contract_id)
        if target_pair is None:
            raise AssignmentConflict("assignment target declaration is missing")
        target_contract, declaration = target_pair
        if declaration.id != intent.target_declaration_record_id:
            raise AssignmentConflict(
                "assignment intent declaration identity is invalid"
            )
        if record.payload.get("contract_id") != intent.target_contract_id:
            raise AssignmentConflict("assignment intent target is invalid")
        transition = AssignmentTransition(
            target_contract_id=intent.target_contract_id,
            worker_id=str(record.payload["worker_id"]),
            previous_assignment_id=str(record.payload["previous_assignment_id"]),
            reason=str(record.payload["reason"]),
            mode=intent.mode,
        )
        witness = next(
            (
                item
                for item in successor_assignment_witnesses
                if item.successor_assignment_record_id == record.id
                or item.successor_contract_id == intent.target_contract_id
            ),
            None,
        )
        terminal = (
            records_by_id.get(witness.source_terminal_record_id)
            if witness is not None
            else None
        )
        if intent.mode is not AssignmentMode.DIRECT:
            if witness is None:
                raise AssignmentConflict("successor assignment witness is missing")
            if (
                witness.successor_contract_id != intent.target_contract_id
                or witness.successor_declaration_record_id != declaration.id
                or witness.assignment_mode is not intent.mode
                or witness.successor_assignment_record_id != record.id
                or witness.source_contract_id != target_contract.parent_id
            ):
                raise AssignmentConflict("successor assignment witness is inconsistent")
            transition = AssignmentTransition(
                target_contract_id=intent.target_contract_id,
                worker_id=str(record.payload["worker_id"]),
                previous_assignment_id=str(record.payload["previous_assignment_id"]),
                reason=str(record.payload["reason"]),
                mode=intent.mode,
                source_contract_id=witness.source_contract_id,
                source_assignment_head_id=witness.source_assignment_head_id,
            )
            if terminal is None:
                raise AssignmentConflict("successor assignment terminal is missing")
        required_parents = {receipt_id, declaration.id}
        previous_id = str(record.payload.get("previous_assignment_id", ""))
        if previous_id:
            required_parents.add(previous_id)
        if transition.worker_id:
            _, registration_record = _registration_record(context, transition.worker_id)
            required_parents.add(registration_record.id)
        if witness is not None:
            if witness.source_assignment_head_id:
                required_parents.add(witness.source_assignment_head_id)
        if not required_parents.issubset(record.causal_parents):
            raise AssignmentConflict("assignment fact omits required causal parents")
        validate_assignment_request(
            context,
            transition,
            target_contract=target_contract,
            target_declaration_record=declaration,
            source_terminal_record=terminal,
        )
    if seen_witness_successors != {
        witness.successor_contract_id for witness in successor_assignment_witnesses
    }:
        raise AssignmentConflict("successor assignment witness set is inconsistent")


def assignment_blocker(
    context: AssignmentFactContext, *, contract_id: str
) -> Mapping[str, str] | None:
    assigned = effective_assignment(context, contract_id=contract_id)
    if assigned is None:
        return None
    worker_id = str(assigned.payload["worker_id"])
    registration = next(
        (
            item
            for item in context.projection.registrations
            if item.worker_id == worker_id
        ),
        None,
    )
    contract = next(
        (item for item in context.projection.contracts if item.id == contract_id), None
    )
    reason = "missing_registration"
    if registration is not None:
        reason = (
            "disabled_worker" if not registration.enabled else "incompatible_worker"
        )
        if contract is not None and is_compatible(contract, registration):
            if registration.active_claims < registration.capacity:
                return None
            reason = "worker_at_capacity"
    return {
        "code": "assignment_unavailable",
        "contract_id": contract_id,
        "worker_id": worker_id,
        "reason": reason,
        "assignment_id": assigned.id,
    }
