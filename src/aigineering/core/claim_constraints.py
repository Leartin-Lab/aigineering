"""Generic immutable constraints that gate acquisition of new claims.

The first supported predicate is deliberately fixed: a task may be claimed only
after one exact prerequisite Contract has a typed successful terminal fact.
These records do not alter Contract identity, disclosure, or output semantics.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from aigineering.protocol.runtime_record import RuntimeRecord, validate_runtime_record


PLACED = "claim.constraint.placed"
RELEASED = "claim.constraint.released"
PREDICATE = "prerequisite_terminal"
REQUIRED_TERMINAL = "complete"
_PLACED_FIELDS = frozenset(
    {
        "actor_id",
        "key_id",
        "candidate_id",
        "contract_id",
        "prerequisite_contract_id",
        "predicate",
        "required_terminal",
        "reason",
    }
)
_RELEASED_FIELDS = _PLACED_FIELDS | {"constraint_id"}
_MAX_FIELD_BYTES = 65536


class ClaimConstraintConflict(ValueError):
    """A prerequisite constraint is malformed or conflicts with claim history."""


@dataclass(frozen=True)
class PrerequisiteBlocker:
    """One active edge that currently prevents a task from being claimed."""

    contract_id: str
    constraint_id: str
    prerequisite_contract_id: str
    predicate: str
    required_terminal: str
    observed_terminal: str

    def as_dict(self) -> dict[str, str]:
        return {
            "contract_id": self.contract_id,
            "constraint_id": self.constraint_id,
            "prerequisite_contract_id": self.prerequisite_contract_id,
            "predicate": self.predicate,
            "required_terminal": self.required_terminal,
            "observed_terminal": self.observed_terminal,
        }


def _validate_fact(record: RuntimeRecord) -> None:
    if record.record_type not in {PLACED, RELEASED}:
        return
    try:
        validate_runtime_record(record)
    except ValueError as exc:
        raise ClaimConstraintConflict(
            "claim constraint record identity is invalid"
        ) from exc
    if record.schema_version != 1:
        raise ClaimConstraintConflict("unsupported claim constraint schema version")
    expected = _PLACED_FIELDS if record.record_type == PLACED else _RELEASED_FIELDS
    if set(record.payload) != expected:
        raise ClaimConstraintConflict("claim constraint fields are invalid")
    for field in (
        "actor_id",
        "key_id",
        "candidate_id",
        "contract_id",
        "prerequisite_contract_id",
        "predicate",
        "required_terminal",
        "reason",
    ):
        value = record.payload[field]
        if not isinstance(value, str) or not value.strip():
            raise ClaimConstraintConflict(
                f"claim constraint {field} must be non-empty text"
            )
        try:
            size = len(value.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise ClaimConstraintConflict(
                f"claim constraint {field} is not valid UTF-8"
            ) from exc
        if size > _MAX_FIELD_BYTES:
            raise ClaimConstraintConflict(f"claim constraint {field} is too long")
    if record.payload["predicate"] != PREDICATE:
        raise ClaimConstraintConflict("unsupported claim constraint predicate")
    if record.payload["required_terminal"] != REQUIRED_TERMINAL:
        raise ClaimConstraintConflict("unsupported prerequisite terminal requirement")
    target = record.payload["contract_id"]
    prerequisite = record.payload["prerequisite_contract_id"]
    if target == prerequisite:
        raise ClaimConstraintConflict("a Contract cannot depend on itself")
    if not record.causal_parents:
        raise ClaimConstraintConflict("claim constraint requires Candidate provenance")
    if record.record_type == RELEASED:
        constraint_id = record.payload["constraint_id"]
        if not isinstance(constraint_id, str) or not constraint_id.strip():
            raise ClaimConstraintConflict(
                "claim constraint release requires an exact ID"
            )
        if constraint_id not in record.causal_parents:
            raise ClaimConstraintConflict(
                "claim constraint release must depend on its placement"
            )


def open_claim_constraints(
    records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[RuntimeRecord, ...]:
    """Return open placement facts in record order, validating typed history."""
    opened: dict[str, RuntimeRecord] = {}
    closed: set[str] = set()
    for record in records:
        if record.record_type not in {PLACED, RELEASED}:
            continue
        _validate_fact(record)
        payload = record.payload
        if record.record_type == PLACED:
            if record.id in opened or record.id in closed:
                raise ClaimConstraintConflict(
                    "claim constraint placement is duplicated"
                )
            opened[record.id] = record
            continue
        constraint_id = str(payload["constraint_id"])
        placement = opened.get(constraint_id)
        if placement is None or constraint_id in closed:
            raise ClaimConstraintConflict(
                "claim constraint release has no open placement"
            )
        if any(
            placement.payload[field] != payload[field]
            for field in (
                "contract_id",
                "prerequisite_contract_id",
                "predicate",
                "required_terminal",
            )
        ):
            raise ClaimConstraintConflict(
                "claim constraint release changes its exact pair"
            )
        if len(record.causal_parents) < 2:
            raise ClaimConstraintConflict(
                "claim constraint release lacks receipt provenance"
            )
        closed.add(constraint_id)
    effective = tuple(
        record
        for constraint_id, record in opened.items()
        if constraint_id not in closed
        and (contract_id is None or record.payload["contract_id"] == contract_id)
    )
    _validate_acyclic(effective)
    return effective


def claim_constraint_history(
    records: Iterable[RuntimeRecord], *, contract_id: str | None = None
) -> tuple[dict[str, object], ...]:
    """Return validated placement/release history without duplicating reduction."""
    records = tuple(records)
    open_ids = {record.id for record in open_claim_constraints(records)}
    selected_placements = tuple(
        record
        for record in records
        if record.record_type == PLACED
        and (contract_id is None or record.payload.get("contract_id") == contract_id)
    )
    prerequisite_ids = frozenset(
        str(record.payload["prerequisite_contract_id"])
        for record in selected_placements
    )
    terminal_status = _typed_terminal_status(records, contract_ids=prerequisite_ids)
    releases = {
        str(record.payload["constraint_id"]): record
        for record in records
        if record.record_type == RELEASED
    }
    history = []
    for record in records:
        if record.record_type != PLACED:
            continue
        payload = record.payload
        if contract_id is not None and payload["contract_id"] != contract_id:
            continue
        release = releases.get(record.id)
        history.append(
            {
                "constraint_id": record.id,
                "contract_id": payload["contract_id"],
                "prerequisite_contract_id": payload["prerequisite_contract_id"],
                "predicate": payload["predicate"],
                "required_terminal": payload["required_terminal"],
                "observed_terminal": terminal_status.get(
                    str(payload["prerequisite_contract_id"]), "missing"
                ),
                "reason": payload["reason"],
                "actor_id": payload["actor_id"],
                "key_id": payload["key_id"],
                "candidate_id": payload["candidate_id"],
                "released_record_id": release.id if release else "",
                "release_reason": release.payload["reason"] if release else "",
                "release_actor_id": release.payload["actor_id"] if release else "",
                "release_key_id": release.payload["key_id"] if release else "",
                "release_candidate_id": (
                    release.payload["candidate_id"] if release else ""
                ),
                "active": record.id in open_ids,
            }
        )
    return tuple(history)


def claim_constraint_blockers(
    records: Iterable[RuntimeRecord], *, contract_id: str
) -> tuple[dict[str, str], ...]:
    """Return exact unresolved prerequisite blockers for a task."""
    records = tuple(records)
    placements = open_claim_constraints(records, contract_id=contract_id)
    if not placements:
        return ()
    prerequisite_ids = frozenset(
        str(record.payload["prerequisite_contract_id"]) for record in placements
    )
    terminals = _typed_terminal_status(records, contract_ids=prerequisite_ids)
    blockers = []
    for placement in placements:
        prerequisite = str(placement.payload["prerequisite_contract_id"])
        observed = terminals.get(prerequisite, "missing")
        if observed == REQUIRED_TERMINAL:
            continue
        blockers.append(
            PrerequisiteBlocker(
                contract_id=contract_id,
                constraint_id=placement.id,
                prerequisite_contract_id=prerequisite,
                predicate=PREDICATE,
                required_terminal=REQUIRED_TERMINAL,
                observed_terminal=observed,
            ).as_dict()
        )
    return tuple(sorted(blockers, key=lambda item: item["constraint_id"]))


def _typed_terminal_status(
    records: Iterable[RuntimeRecord], *, contract_ids: frozenset[str] | None = None
) -> dict[str, str]:
    status: dict[str, str] = {}
    for record in records:
        if record.record_type != "lifecycle.terminal":
            continue
        contract_id = record.payload.get("contract_id")
        if contract_ids is not None:
            if not isinstance(contract_id, str) or contract_id not in contract_ids:
                continue
        try:
            validate_runtime_record(record)
        except ValueError as exc:
            raise ClaimConstraintConflict(
                "typed prerequisite terminal is corrupt"
            ) from exc
        terminal = record.payload.get("terminal")
        if (
            record.schema_version != 1
            or not isinstance(contract_id, str)
            or not contract_id
            or not isinstance(terminal, str)
            or terminal not in {"complete", "failed", "cancelled", "unreachable"}
        ):
            raise ClaimConstraintConflict("typed prerequisite terminal is malformed")
        if isinstance(contract_id, str) and isinstance(terminal, str):
            if contract_id in status and status[contract_id] != terminal:
                status[contract_id] = "untyped"
            else:
                status[contract_id] = terminal
    return status


def validate_claim_constraint_batch(
    existing: Iterable[RuntimeRecord],
    pending: Iterable[RuntimeRecord],
    *,
    candidate_id: str,
    actor_id: str,
    key_id: str,
    known_contract_ids: frozenset[str],
    claimed_contract_ids: frozenset[str],
    terminal_contract_ids: frozenset[str],
    output_satisfied_contract_ids: frozenset[str] = frozenset(),
) -> tuple[RuntimeRecord, ...]:
    """Validate a single signed transition and all effective graph invariants."""
    history = tuple(existing)
    pending = tuple(pending)
    transitions = tuple(
        record for record in pending if record.record_type in {PLACED, RELEASED}
    )
    if not transitions:
        return ()
    if len(transitions) != 1:
        raise ClaimConstraintConflict("one Candidate may change one claim constraint")

    receipt_records = tuple(
        record
        for record in pending
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate_id
        and record.payload.get("actor_id") == actor_id
        and record.payload.get("key_id") == key_id
    )
    heads = tuple(
        record
        for record in pending
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate_id
    )
    if len(receipt_records) != 1 or len(heads) != 1:
        raise ClaimConstraintConflict(
            "constraint lacks authenticated Candidate receipt and head"
        )
    receipt, head = receipt_records[0], heads[0]
    try:
        validate_runtime_record(receipt)
        validate_runtime_record(head)
    except ValueError as exc:
        raise ClaimConstraintConflict(
            "constraint Candidate provenance is invalid"
        ) from exc
    if receipt.schema_version != 1 or head.schema_version != 1:
        raise ClaimConstraintConflict("unsupported Candidate provenance schema")
    if set(head.payload) != {"candidate_id", "committed_record_ids"}:
        raise ClaimConstraintConflict("committed Candidate head fields are invalid")
    if head.payload.get("candidate_id") != candidate_id:
        raise ClaimConstraintConflict("committed Candidate head identity is invalid")
    if receipt.id not in head.causal_parents:
        raise ClaimConstraintConflict("committed head omits Candidate receipt")
    committed_ids = head.payload.get("committed_record_ids")
    if not isinstance(committed_ids, (tuple, list)):
        raise ClaimConstraintConflict("committed head record list is invalid")

    transition = transitions[0]
    _validate_fact(transition)
    payload = transition.payload
    if (
        payload["candidate_id"] != candidate_id
        or payload["actor_id"] != actor_id
        or payload["key_id"] != key_id
        or receipt.id not in transition.causal_parents
        or transition.id not in committed_ids
        or transition.id not in head.causal_parents
    ):
        raise ClaimConstraintConflict(
            "constraint provenance does not match Candidate head"
        )

    validate_claim_constraint_transition(
        history,
        transition,
        known_contract_ids=known_contract_ids,
        claimed_contract_ids=claimed_contract_ids,
        terminal_contract_ids=terminal_contract_ids,
        output_satisfied_contract_ids=output_satisfied_contract_ids,
    )
    return (transition,) if transition.record_type == PLACED else ()


def validate_claim_constraint_transition(
    existing: Iterable[RuntimeRecord],
    transition: RuntimeRecord,
    *,
    known_contract_ids: frozenset[str],
    claimed_contract_ids: frozenset[str],
    terminal_contract_ids: frozenset[str],
    output_satisfied_contract_ids: frozenset[str] = frozenset(),
) -> tuple[RuntimeRecord, ...]:
    """Validate one pure transition and return the resulting active edge set.

    This form is suitable for authenticated pure projection, where the receipt
    ID is known but the committed Candidate head does not exist yet. The Store
    uses the batch form above to add receipt/head verification in its writer.
    """
    _validate_fact(transition)
    if transition.record_type not in {PLACED, RELEASED}:
        raise ClaimConstraintConflict("expected one claim constraint transition")
    target = str(transition.payload["contract_id"])
    prerequisite = str(transition.payload["prerequisite_contract_id"])
    if target not in known_contract_ids or prerequisite not in known_contract_ids:
        raise ClaimConstraintConflict("claim constraint references an unknown Contract")
    if target in terminal_contract_ids:
        raise ClaimConstraintConflict("claim constraint target is terminal")
    if target in claimed_contract_ids:
        raise ClaimConstraintConflict("claim constraint target has claim history")
    if transition.record_type == PLACED and target in output_satisfied_contract_ids:
        raise ClaimConstraintConflict(
            "claim constraint target outputs are already satisfied"
        )

    active = list(open_claim_constraints(existing))
    if transition.record_type == PLACED:
        if any(
            record.payload["contract_id"] == target
            and record.payload["prerequisite_contract_id"] == prerequisite
            for record in active
        ):
            raise ClaimConstraintConflict("duplicate open prerequisite constraint")
        active.append(transition)
    else:
        constraint_id = str(transition.payload["constraint_id"])
        placement = next(
            (record for record in active if record.id == constraint_id), None
        )
        if placement is None:
            raise ClaimConstraintConflict(
                "constraint release does not identify an open link"
            )
        if any(
            placement.payload[field] != transition.payload[field]
            for field in (
                "contract_id",
                "prerequisite_contract_id",
                "predicate",
                "required_terminal",
            )
        ):
            raise ClaimConstraintConflict("constraint release changes its exact link")
        active.remove(placement)
    _validate_acyclic(active)
    return tuple(active)


def _validate_acyclic(placements: Iterable[RuntimeRecord]) -> None:
    graph: dict[str, set[str]] = {}
    for record in placements:
        graph.setdefault(str(record.payload["contract_id"]), set()).add(
            str(record.payload["prerequisite_contract_id"])
        )
    state: dict[str, int] = {}
    for root in graph:
        if state.get(root) == 2:
            continue
        state[root] = 1
        stack: list[tuple[str, Iterator[str]]] = [(root, iter(graph.get(root, ())))]
        while stack:
            node, children = stack[-1]
            try:
                child = next(children)
            except StopIteration:
                state[node] = 2
                stack.pop()
                continue
            child_state = state.get(child, 0)
            if child_state == 1:
                raise ClaimConstraintConflict(
                    "prerequisite constraints contain a cycle"
                )
            if child_state == 0:
                state[child] = 1
                stack.append((child, iter(graph.get(child, ()))))


def external_prerequisite_blockers(
    records: Iterable[RuntimeRecord],
    *,
    cancelled_contract_ids: frozenset[str],
    repair_complete_contract_ids: frozenset[str] = frozenset(),
    repair_only_target_ids: frozenset[str] = frozenset(),
) -> tuple[dict[str, str], ...]:
    """Find open edges that prevent transferring cancelled targets.

    Edges internal to the cancellation set and targets explicitly receiving a
    same-batch canonical completion repair are excluded. No successor IDs are
    inferred or followed here.
    """
    records = tuple(records)
    placements = open_claim_constraints(records)
    blockers: list[dict[str, str]] = []
    relevant: list[RuntimeRecord] = []
    for placement in placements:
        target = str(placement.payload["contract_id"])
        prerequisite = str(placement.payload["prerequisite_contract_id"])
        if target not in cancelled_contract_ids:
            continue
        if target in repair_only_target_ids:
            continue
        if prerequisite in cancelled_contract_ids:
            continue
        if prerequisite in repair_complete_contract_ids:
            continue
        relevant.append(placement)
    if not relevant:
        return ()
    prerequisite_ids = frozenset(
        str(record.payload["prerequisite_contract_id"]) for record in relevant
    )
    terminals = _typed_terminal_status(records, contract_ids=prerequisite_ids)
    for placement in relevant:
        target = str(placement.payload["contract_id"])
        prerequisite = str(placement.payload["prerequisite_contract_id"])
        observed = terminals.get(prerequisite, "missing")
        if observed == REQUIRED_TERMINAL:
            continue
        blockers.append(
            PrerequisiteBlocker(
                contract_id=target,
                constraint_id=placement.id,
                prerequisite_contract_id=prerequisite,
                predicate=PREDICATE,
                required_terminal=REQUIRED_TERMINAL,
                observed_terminal=observed,
            ).as_dict()
        )
    return tuple(
        sorted(blockers, key=lambda item: (item["contract_id"], item["constraint_id"]))
    )


__all__ = [
    "ClaimConstraintConflict",
    "PLACED",
    "PREDICATE",
    "RELEASED",
    "REQUIRED_TERMINAL",
    "PrerequisiteBlocker",
    "claim_constraint_blockers",
    "claim_constraint_history",
    "external_prerequisite_blockers",
    "open_claim_constraints",
    "validate_claim_constraint_transition",
    "validate_claim_constraint_batch",
]
