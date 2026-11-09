"""Read-only validation of historical claim constraints records."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

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
