"""Immutable causal-work allowance projection.

``Contract.budget`` is the migration input for a grant. Runtime authority is
represented by append-only allowance records and never credited to a Worker.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from aigineering.core.ids import canonical_json, compute_content_hash
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.types import Contract

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol


class CausalAllowanceConflict(ValueError):
    """A projected reservation no longer fits the committed causal balance."""


def project_contract_allowance_records(
    contracts: Sequence[Contract],
    existing_contracts: Sequence[Contract],
    records: Sequence[RuntimeRecord],
    *,
    causal_parent: str,
) -> tuple[RuntimeRecord, ...]:
    """Grant roots and atomically reserve contained child publication."""
    if not contracts:
        return ()
    existing_ids = {contract.id for contract in existing_contracts}
    new_contracts = tuple(
        contract for contract in contracts if contract.id not in existing_ids
    )
    known = {contract.id: contract for contract in (*existing_contracts, *contracts)}
    requested: dict[str, int] = defaultdict(int)
    for contract in new_contracts:
        if contract.parent_id is not None:
            if contract.parent_id not in known:
                raise ValueError(
                    "child allowance references an unknown parent Contract"
                )
            requested[contract.parent_id] += contract.budget
    for source_id, amount in requested.items():
        available = allowance_available(known[source_id], records)
        if amount > available:
            raise ValueError(
                f"causal allowance exceeded for {source_id!r}: "
                f"requested {amount}, available {available}"
            )

    projected: list[RuntimeRecord] = []
    for contract in new_contracts:
        if contract.parent_id is None:
            projected.append(
                create_runtime_record(
                    "allowance.granted",
                    {
                        "amount": contract.budget,
                        "contract_id": contract.id,
                        "grant_id": f"grant:{contract.id}",
                        "purpose": "root",
                        "source_contract_id": "",
                    },
                    causal_parents=(causal_parent,),
                )
            )
            continue
        purpose = allowance_purpose(contract)
        reservation_id = allowance_reservation_id(
            contract.parent_id, contract.id, purpose
        )
        reservation = create_runtime_record(
            "allowance.reserved",
            {
                "amount": contract.budget,
                "child_contract_id": contract.id,
                "purpose": purpose,
                "reservation_id": reservation_id,
                "source_contract_id": contract.parent_id,
            },
            causal_parents=(causal_parent,),
        )
        grant = create_runtime_record(
            "allowance.granted",
            {
                "amount": contract.budget,
                "contract_id": contract.id,
                "grant_id": f"grant:{contract.id}",
                "purpose": purpose,
                "reservation_id": reservation_id,
                "source_contract_id": contract.parent_id,
            },
            causal_parents=(reservation.id,),
        )
        projected.extend((reservation, grant))
    return tuple(projected)


def allowance_available(contract: Contract, records: Iterable[RuntimeRecord]) -> int:
    relevant = tuple(records)
    grants = [
        int(record.payload.get("amount", 0))
        for record in relevant
        if record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == contract.id
    ]
    granted = sum(grants) if grants else contract.budget
    reserved = sum(
        int(record.payload.get("amount", 0))
        for record in relevant
        if record.record_type == "allowance.reserved"
        and record.payload.get("source_contract_id") == contract.id
    )
    returned = sum(
        int(record.payload.get("amount", 0))
        for record in relevant
        if record.record_type == "allowance.returned"
        and record.payload.get("source_contract_id") == contract.id
    )
    extinguished = sum(
        int(record.payload.get("amount", 0))
        for record in relevant
        if record.record_type == "allowance.extinguished"
        and record.payload.get("contract_id") == contract.id
    )
    return max(0, granted - reserved + returned - extinguished)


def allowance_purpose(contract: Contract) -> str:
    if any(
        label.startswith(("plugin:plan.", "plugin:replan."))
        for label in contract.labels
    ):
        return "planning"
    policy = contract.acceptance_policy
    if policy is not None and policy.get("mode") == "independent":
        return "verification"
    return "execution"


def allowance_reservation_id(
    source_contract_id: str, child_contract_id: str, purpose: str
) -> str:
    return "reservation:" + compute_content_hash(
        canonical_json(
            {
                "child_contract_id": child_contract_id,
                "purpose": purpose,
                "source_contract_id": source_contract_id,
            }
        )
    )


def terminal_allowance_records(
    contracts: Sequence[Contract],
    existing_records: Sequence[RuntimeRecord],
    pending_records: Sequence[RuntimeRecord],
) -> tuple[RuntimeRecord, ...]:
    """Extinguish the remaining allowance of newly terminal Contracts."""
    by_id = {contract.id: contract for contract in contracts}
    all_records = tuple(existing_records) + tuple(pending_records)
    terminals = {
        str(record.payload.get("contract_id", "")): record
        for record in pending_records
        if record.record_type == "lifecycle.terminal"
    }
    consequences: list[RuntimeRecord] = []
    for contract_id, terminal in sorted(terminals.items()):
        contract = by_id.get(contract_id)
        if contract is None:
            continue
        amount = allowance_available(contract, all_records)
        if amount <= 0:
            continue
        if any(
            record.record_type == "allowance.extinguished"
            and record.payload.get("contract_id") == contract_id
            for record in existing_records
        ):
            continue
        consequences.append(
            create_runtime_record(
                "allowance.extinguished",
                {
                    "amount": amount,
                    "contract_id": contract_id,
                    "reason": str(terminal.payload.get("terminal", "terminal")),
                },
                causal_parents=(terminal.id,),
            )
        )
    return tuple(consequences)


def materialize_terminal_allowance(
    store: StoreProtocol,
    contracts: Sequence[Contract],
    pending_records: Sequence[RuntimeRecord],
) -> tuple[RuntimeRecord, ...]:
    """Load the append-only snapshot needed by terminal allowance projection."""
    existing_records = tuple(record for _, record in store.scan_runtime_records())
    return terminal_allowance_records(
        tuple(store.get_all_contracts()) + tuple(contracts),
        existing_records,
        pending_records,
    )


def validate_allowance_commit(
    contracts: Sequence[Contract],
    existing_records: Sequence[RuntimeRecord],
    pending_records: Sequence[RuntimeRecord],
) -> None:
    """Recheck allowance consequences against the commit-time fact snapshot."""
    by_id = {contract.id: contract for contract in contracts}
    requested: dict[str, int] = defaultdict(int)
    for record in pending_records:
        if record.record_type != "allowance.reserved":
            continue
        source_id = str(record.payload.get("source_contract_id", ""))
        requested[source_id] += int(record.payload.get("amount", 0))
    for source_id, amount in requested.items():
        source = by_id.get(source_id)
        if source is None:
            raise ValueError("allowance reservation references an unknown Contract")
        available = allowance_available(source, existing_records)
        if amount > available:
            raise CausalAllowanceConflict(
                f"causal allowance changed before commit for {source_id!r}: "
                f"requested {amount}, available {available}"
            )
    without_extinguishment = tuple(existing_records) + tuple(
        record
        for record in pending_records
        if record.record_type != "allowance.extinguished"
    )
    for record in pending_records:
        if record.record_type != "allowance.extinguished":
            continue
        contract_id = str(record.payload.get("contract_id", ""))
        contract = by_id.get(contract_id)
        if contract is None:
            raise ValueError("allowance extinguishment references an unknown Contract")
        projected = int(record.payload.get("amount", 0))
        available = allowance_available(contract, without_extinguishment)
        if projected != available:
            raise CausalAllowanceConflict(
                f"causal allowance changed before terminal commit for {contract_id!r}: "
                f"projected {projected}, available {available}"
            )


def allowance_is_recorded(contract_id: str, records: Iterable[RuntimeRecord]) -> bool:
    return any(
        record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == contract_id
        for record in records
    )


def resolve_causal_allowance(
    store: StoreProtocol, contract: Contract, *, fallback: int
) -> int:
    """Prefer immutable allowance facts while legacy callers are removed."""
    records = tuple(record for _, record in store.scan_runtime_records())
    if allowance_is_recorded(contract.id, records):
        return allowance_available(contract, records)
    return fallback
