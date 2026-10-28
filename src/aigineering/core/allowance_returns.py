"""Generic construction and validation for conserved allowance returns."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from aigineering.core.ids import canonical_json, compute_content_hash
from aigineering.core.allowance_publication_index import safe_committed_head_relations
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
    runtime_record_effective_payload,
    validate_runtime_record,
)
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import contract_from_dict


class CausalAllowanceConflict(ValueError):
    """A projected reservation no longer fits the committed causal balance."""


class _IndexedCommittedHead:
    __slots__ = ("record", "members", "causal_parents")

    def __init__(
        self,
        record: RuntimeRecord,
        members: frozenset[str],
        causal_parents: frozenset[str],
    ) -> None:
        self.record = record
        self.members = members
        self.causal_parents = causal_parents


class _AllowanceGrantProofContext:
    """Call-scoped relation index and memoized immutable-record proofs."""

    __slots__ = (
        "records_by_id",
        "heads_by_member",
        "heads_by_id",
        "declarations_by_candidate_contract",
        "new_reservations_by_edge",
        "existing_grant_contract_ids",
        "has_opaque_heads",
        "_validated_heads",
        "_validated_receipts",
    )

    def __init__(
        self,
        records_by_id: dict[str, RuntimeRecord],
        new_reservations: Sequence[RuntimeRecord],
        existing_records: Sequence[RuntimeRecord],
    ) -> None:
        heads_by_member: dict[str, list[_IndexedCommittedHead]] = defaultdict(list)
        heads_by_id: dict[str, _IndexedCommittedHead] = {}
        declarations: dict[tuple[str, str], list[RuntimeRecord]] = defaultdict(list)
        has_opaque_heads = False

        for record in records_by_id.values():
            if record.record_type == "candidate.committed":
                relations = safe_committed_head_relations(record)
                if relations is None:
                    has_opaque_heads = True
                    continue
                # The historical predicate treated a repeated member within
                # one head as one matching head; _validate_candidate_head
                # separately rejects duplicate members after selection.
                indexed_head = _IndexedCommittedHead(record, *relations)
                heads_by_id[record.id] = indexed_head
                for member_id in indexed_head.members:
                    heads_by_member[member_id].append(indexed_head)
            elif record.record_type == "contract.declared":
                candidate_id = record.payload.get("candidate_id")
                contract = record.payload.get("contract")
                if (
                    isinstance(candidate_id, str)
                    and candidate_id
                    and isinstance(contract, Mapping)
                    and isinstance(contract.get("id"), str)
                    and contract.get("id")
                ):
                    declarations[(candidate_id, contract["id"])].append(record)

        reservations: dict[tuple[str, str, str], list[RuntimeRecord]] = defaultdict(
            list
        )
        for record in new_reservations:
            child_id = record.payload.get("child_contract_id")
            source_id = record.payload.get("source_contract_id")
            reservation_id = record.payload.get("reservation_id")
            if all(
                isinstance(value, str) and value
                for value in (child_id, source_id, reservation_id)
            ):
                # Preserve repeated facts and repeated IDs.  The caller owns
                # the exact-one check; an index must not collapse ambiguity.
                reservations[(child_id, source_id, reservation_id)].append(record)

        self.records_by_id = MappingProxyType(records_by_id)
        self.heads_by_member = MappingProxyType(
            {key: tuple(value) for key, value in heads_by_member.items()}
        )
        self.heads_by_id = MappingProxyType(heads_by_id)
        self.declarations_by_candidate_contract = MappingProxyType(
            {key: tuple(value) for key, value in declarations.items()}
        )
        self.new_reservations_by_edge = MappingProxyType(
            {key: tuple(value) for key, value in reservations.items()}
        )
        self.existing_grant_contract_ids = frozenset(
            record.payload.get("contract_id")
            for record in existing_records
            if record.record_type == "allowance.granted"
            and isinstance(record.payload.get("contract_id"), str)
            and record.payload.get("contract_id")
        )
        self.has_opaque_heads = has_opaque_heads
        self._validated_heads: dict[str, RuntimeRecord] = {}
        self._validated_receipts: set[str] = set()

    def matching_heads(self, fact_id: str) -> tuple[RuntimeRecord, ...]:
        if self.has_opaque_heads:
            # Preserve the exact historical predicate and its failure timing
            # whenever an opaque head could make indexed selection lossy.
            return tuple(
                head
                for head in self.records_by_id.values()
                if head.record_type == "candidate.committed"
                and fact_id in head.payload.get("committed_record_ids", ())
                and fact_id in head.causal_parents
            )
        return tuple(
            indexed_head.record
            for indexed_head in self.heads_by_member.get(fact_id, ())
            if fact_id in indexed_head.causal_parents
        )

    def head_contains_member(self, head: RuntimeRecord, member_id: str) -> bool:
        indexed_head = self.heads_by_id.get(head.id)
        if indexed_head is not None and not self.has_opaque_heads:
            return member_id in indexed_head.members
        return member_id in head.payload.get("committed_record_ids", ())

    def head_contains_parent(self, head: RuntimeRecord, parent_id: str) -> bool:
        indexed_head = self.heads_by_id.get(head.id)
        if indexed_head is not None and not self.has_opaque_heads:
            return parent_id in indexed_head.causal_parents
        return parent_id in head.causal_parents

    def declarations_for(
        self, candidate_id: str, contract_id: str
    ) -> tuple[RuntimeRecord, ...]:
        return self.declarations_by_candidate_contract.get(
            (candidate_id, contract_id), ()
        )

    def reservations_for(
        self, child_id: str, source_id: str, reservation_id: str
    ) -> tuple[RuntimeRecord, ...]:
        return self.new_reservations_by_edge.get(
            (child_id, source_id, reservation_id), ()
        )

    def validate_receipt(self, receipt: RuntimeRecord) -> None:
        if receipt.id not in self._validated_receipts:
            validate_runtime_record(receipt)
            self._validated_receipts.add(receipt.id)

    def validate_head(
        self, head: RuntimeRecord, records_by_id: dict[str, RuntimeRecord]
    ) -> RuntimeRecord:
        if head.id not in self._validated_heads:
            validate_runtime_record(head)
            receipt = _validate_candidate_head(head, records_by_id, proof_context=self)
            self._validated_heads[head.id] = receipt
        return self._validated_heads[head.id]


def project_causal_allowance_returns(
    recipient: Contract,
    donors: Sequence[Contract],
    contracts: Sequence[Contract],
    existing_records: Sequence[RuntimeRecord],
    pending_records: Sequence[RuntimeRecord],
    *,
    candidate_id: str,
    receipt_id: str,
) -> tuple[RuntimeRecord, ...]:
    """Project bounded residual balances from cancelled descendants upward.

    The caller selects donors as part of its domain-specific pure projection
    and supplies their canonical cancellation facts. This generic owner checks
    ancestry and the immutable grant/reservation path before creating returns.
    """
    from aigineering.core.causal_allowance import allowance_available

    if not candidate_id or not receipt_id:
        raise ValueError("allowance return requires authenticated Candidate IDs")
    by_id = {contract.id: contract for contract in contracts}
    by_id.setdefault(recipient.id, recipient)
    terminals = _pending_cancelled_donors(pending_records)
    projected: list[RuntimeRecord] = []
    seen: set[str] = set()
    for donor in donors:
        if donor.id in seen:
            raise ValueError("allowance return contains a duplicate donor")
        seen.add(donor.id)
        terminal = terminals.get(donor.id)
        if terminal is None:
            raise ValueError("allowance return donor requires pending cancellation")
        if any(
            record.record_type == "lifecycle.terminal"
            and record.payload.get("contract_id") == donor.id
            for record in existing_records
        ):
            raise ValueError("allowance return donor was already terminal")
        amount = allowance_available(donor, existing_records)
        if amount <= 0:
            continue
        funding_path = _allowance_funding_path(
            donor, recipient, by_id, existing_records
        )
        return_id = allowance_return_id(candidate_id, recipient.id, donor.id)
        projected.append(
            create_runtime_record(
                "allowance.returned",
                {
                    "amount": amount,
                    "candidate_id": candidate_id,
                    "cancellation_record_id": terminal.id,
                    "from_contract_id": donor.id,
                    "funding_path_record_ids": [record.id for record in funding_path],
                    "receipt_id": receipt_id,
                    "return_id": return_id,
                    "source_contract_id": recipient.id,
                },
                causal_parents=(
                    receipt_id,
                    terminal.id,
                    *(record.id for record in funding_path),
                ),
            )
        )
    validate_causal_allowance_returns(
        tuple(by_id.values()),
        existing_records,
        tuple(pending_records) + tuple(projected),
        candidate_id=candidate_id,
        receipt_id=receipt_id,
    )
    return tuple(projected)


def validate_causal_allowance_returns(
    contracts: Sequence[Contract],
    existing_records: Sequence[RuntimeRecord],
    pending_records: Sequence[RuntimeRecord],
    *,
    candidate_id: str | None = None,
    receipt_id: str | None = None,
    require_committed_head: bool = False,
) -> dict[str, int]:
    """Validate pending returns and return incoming credits by recipient.

    The pure projection phase binds authenticated IDs supplied by the reducer
    but does not require a not-yet-created receipt/head. The SQLite writer gate
    requires both immutable records and their exact Candidate commitment.
    """
    from aigineering.core.causal_allowance import allowance_available

    records = tuple(pending_records)
    if not any(record.record_type == "allowance.returned" for record in records):
        return {}
    existing = tuple(existing_records)
    by_id = {contract.id: contract for contract in contracts}
    pending_terminals = _pending_cancelled_donors(records)
    pending_by_id = {record.id: record for record in records}
    existing_by_id = {record.id: record for record in existing}
    all_records_by_id = {**existing_by_id, **pending_by_id}
    heads = tuple(
        {
            record.id: record
            for record in (*existing, *records)
            if record.record_type == "candidate.committed"
        }.values()
    )
    receipt_records = {
        record.id: record
        for record in (*existing, *records)
        if record.record_type == "candidate.received"
    }
    incoming: dict[str, int] = defaultdict(int)
    donor_totals: dict[str, int] = defaultdict(int)
    seen_returns: set[str] = set()
    returned_donors = {
        str(record.payload.get("from_contract_id", ""))
        for record in existing
        if record.record_type == "allowance.returned"
    }
    for record in records:
        if record.record_type != "allowance.returned":
            continue
        try:
            validate_runtime_record(record)
        except ValueError as exc:
            raise CausalAllowanceConflict("allowance return record is invalid") from exc
        payload = record.payload
        source_id = payload.get("source_contract_id")
        donor_id = payload.get("from_contract_id")
        record_candidate_id = payload.get("candidate_id")
        record_receipt_id = payload.get("receipt_id")
        return_id = payload.get("return_id")
        cancellation_id = payload.get("cancellation_record_id")
        funding_ids = payload.get("funding_path_record_ids")
        amount = payload.get("amount")
        if (
            not isinstance(source_id, str)
            or not source_id
            or not isinstance(donor_id, str)
            or not donor_id
            or not isinstance(record_candidate_id, str)
            or not record_candidate_id
            or not isinstance(record_receipt_id, str)
            or not record_receipt_id
            or not isinstance(return_id, str)
            or not return_id
            or not isinstance(cancellation_id, str)
            or not cancellation_id
            or not isinstance(funding_ids, (list, tuple))
            or not funding_ids
            or not all(isinstance(item, str) and item for item in funding_ids)
            or type(amount) is not int
            or amount <= 0
        ):
            raise CausalAllowanceConflict("allowance return fields are malformed")
        if set(payload) != {
            "amount",
            "candidate_id",
            "cancellation_record_id",
            "from_contract_id",
            "funding_path_record_ids",
            "receipt_id",
            "return_id",
            "source_contract_id",
        }:
            raise CausalAllowanceConflict("allowance return fields are not canonical")
        if candidate_id is not None and record_candidate_id != candidate_id:
            raise CausalAllowanceConflict(
                "allowance return Candidate provenance differs"
            )
        if receipt_id is not None and record_receipt_id != receipt_id:
            raise CausalAllowanceConflict("allowance return receipt provenance differs")
        if return_id != allowance_return_id(record_candidate_id, source_id, donor_id):
            raise CausalAllowanceConflict("allowance return identity is invalid")
        if return_id in seen_returns:
            raise CausalAllowanceConflict("allowance return is duplicated")
        seen_returns.add(return_id)
        prior = existing_by_id.get(record.id)
        if prior is not None:
            if runtime_record_effective_payload(
                prior
            ) != runtime_record_effective_payload(
                record
            ) or not _is_exact_committed_return_replay(
                prior,
                record_candidate_id,
                record_receipt_id,
                cancellation_id,
                heads,
                receipt_records,
                all_records_by_id,
            ):
                raise CausalAllowanceConflict("allowance return replay differs")
            continue
        if any(
            current.record_type == "allowance.returned"
            and current.payload.get("return_id") == return_id
            for current in existing
        ):
            raise CausalAllowanceConflict("allowance return is duplicated")
        if donor_id in returned_donors:
            raise CausalAllowanceConflict("allowance donor was already returned")
        donor = by_id.get(donor_id)
        recipient = by_id.get(source_id)
        if donor is None or recipient is None:
            raise CausalAllowanceConflict(
                "allowance return references unknown Contract"
            )
        if _has_terminal(existing, donor_id):
            raise CausalAllowanceConflict("allowance return donor was already terminal")
        if _has_terminal(existing, source_id):
            raise CausalAllowanceConflict(
                "allowance return recipient was already terminal"
            )
        cancellation = pending_terminals.get(donor_id)
        if cancellation is None or cancellation.id != cancellation_id:
            raise CausalAllowanceConflict("allowance return lacks donor cancellation")
        if cancellation.payload.get("terminal") != "cancelled":
            raise CausalAllowanceConflict("allowance return donor was not cancelled")
        if record_receipt_id not in record.causal_parents:
            raise CausalAllowanceConflict("allowance return lacks receipt causality")
        if record_receipt_id not in cancellation.causal_parents:
            raise CausalAllowanceConflict(
                "allowance cancellation lacks receipt causality"
            )
        if cancellation.id not in record.causal_parents:
            raise CausalAllowanceConflict(
                "allowance return lacks cancellation causality"
            )
        if record.causal_parents != (
            record_receipt_id,
            cancellation.id,
            *tuple(funding_ids),
        ):
            raise CausalAllowanceConflict("allowance return causal parents differ")
        funding_path = _allowance_funding_path(
            donor,
            recipient,
            by_id,
            existing,
        )
        if tuple(funding_ids) != tuple(item.id for item in funding_path):
            raise CausalAllowanceConflict("allowance return funding path is invalid")
        current_available = allowance_available(donor, existing)
        donor_totals[donor_id] += amount
        if donor_totals[donor_id] > current_available:
            raise CausalAllowanceConflict("allowance return exceeds donor residual")
        if require_committed_head:
            _validate_return_commitment_head(
                record,
                cancellation,
                record_candidate_id,
                record_receipt_id,
                pending_by_id,
                all_records_by_id,
                heads,
                receipt_records,
            )
        incoming[source_id] += amount
    return dict(incoming)


def validate_allowance_grants(
    contracts: Sequence[Contract],
    existing_records: Sequence[RuntimeRecord],
    pending_records: Sequence[RuntimeRecord],
) -> None:
    """Require new grant/reservation facts to match a committed declaration.

    Contract budgets are migration inputs; a runtime grant can affect future
    authority only when the canonical Candidate commitment contains its exact
    declaration and funding facts.
    """
    from aigineering.core.causal_allowance import (
        allowance_purpose as _allowance_purpose,
        allowance_reservation_id as _allowance_reservation_id,
    )

    by_contract = {contract.id: contract for contract in contracts}
    existing_by_id = {record.id: record for record in existing_records}
    pending_by_id = {record.id: record for record in pending_records}
    all_by_id = {**existing_by_id, **pending_by_id}
    new_grants = tuple(
        record
        for record in pending_records
        if record.record_type == "allowance.granted" and record.id not in existing_by_id
    )
    new_reservations = tuple(
        record
        for record in pending_records
        if record.record_type == "allowance.reserved"
        and record.id not in existing_by_id
    )
    if not new_grants and not new_reservations:
        return

    proof_context = _AllowanceGrantProofContext(
        all_by_id,
        new_reservations,
        existing_records,
    )
    validated_reservations: set[str] = set()
    seen_grants: set[str] = set()
    for grant in new_grants:
        try:
            validate_runtime_record(grant)
        except ValueError as exc:
            raise CausalAllowanceConflict("allowance grant record is invalid") from exc
        contract_id = grant.payload.get("contract_id")
        contract = (
            by_contract.get(contract_id) if isinstance(contract_id, str) else None
        )
        amount = grant.payload.get("amount")
        if (
            contract is None
            or type(amount) is not int
            or amount != contract.budget
            or grant.payload.get("grant_id") != f"grant:{contract.id}"
        ):
            raise CausalAllowanceConflict(
                "allowance grant must match its declared Contract budget"
            )
        if contract.id in seen_grants:
            raise CausalAllowanceConflict("Contract has duplicate allowance grants")
        seen_grants.add(contract.id)
        if contract.id in proof_context.existing_grant_contract_ids:
            raise CausalAllowanceConflict("Contract already has an allowance grant")

        if contract.parent_id is None:
            if (
                set(grant.payload)
                != {
                    "amount",
                    "contract_id",
                    "grant_id",
                    "purpose",
                    "source_contract_id",
                }
                or grant.payload.get("purpose") != "root"
                or grant.payload.get("source_contract_id") != ""
            ):
                raise CausalAllowanceConflict("root allowance grant is not canonical")
            _validate_committed_fact_bundle(
                (grant,), all_by_id, proof_context=proof_context
            )
            receipt = _committed_fact_receipt(
                grant, all_by_id, proof_context=proof_context
            )
            declaration = _matching_contract_declaration(
                contract,
                grant,
                receipt,
                all_by_id,
                proof_context=proof_context,
            )
            _validate_committed_fact_bundle(
                (declaration, grant), all_by_id, proof_context=proof_context
            )
            continue

        parent = by_contract.get(contract.parent_id)
        purpose = _allowance_purpose(contract)
        reservation_id = (
            _allowance_reservation_id(parent.id, contract.id, purpose) if parent else ""
        )
        if (
            parent is None
            or set(grant.payload)
            != {
                "amount",
                "contract_id",
                "grant_id",
                "purpose",
                "reservation_id",
                "source_contract_id",
            }
            or grant.payload.get("purpose") != purpose
            or grant.payload.get("source_contract_id") != parent.id
            or grant.payload.get("reservation_id") != reservation_id
        ):
            raise CausalAllowanceConflict("child allowance grant is not canonical")
        reservations = proof_context.reservations_for(
            contract.id, parent.id, reservation_id
        )
        if len(reservations) != 1:
            raise CausalAllowanceConflict(
                "child allowance grant lacks its new committed reservation"
            )
        reservation = reservations[0]
        _validate_funding_edge(
            parent,
            contract,
            reservation,
            grant,
            all_by_id,
            proof_context=proof_context,
        )
        receipt = _committed_fact_receipt(
            reservation, all_by_id, proof_context=proof_context
        )
        declaration = _matching_contract_declaration(
            contract,
            grant,
            receipt,
            all_by_id,
            proof_context=proof_context,
        )
        _validate_committed_fact_bundle(
            (declaration, reservation, grant),
            all_by_id,
            proof_context=proof_context,
        )
        validated_reservations.add(reservation.id)

    if {record.id for record in new_reservations} != validated_reservations:
        raise CausalAllowanceConflict(
            "allowance reservation lacks its canonical child grant"
        )


def _matching_contract_declaration(
    contract: Contract,
    grant: RuntimeRecord,
    receipt: RuntimeRecord,
    records_by_id: dict[str, RuntimeRecord],
    *,
    proof_context: _AllowanceGrantProofContext | None = None,
) -> RuntimeRecord:
    heads = (
        proof_context.matching_heads(grant.id)
        if proof_context is not None
        else tuple(
            head
            for head in records_by_id.values()
            if head.record_type == "candidate.committed"
            and grant.id in head.payload.get("committed_record_ids", ())
            and grant.id in head.causal_parents
        )
    )
    if len(heads) != 1:
        raise CausalAllowanceConflict("allowance grant lacks one Candidate head")
    candidate_id = heads[0].payload.get("candidate_id")
    declarations = (
        proof_context.declarations_for(candidate_id, contract.id)
        if proof_context is not None and isinstance(candidate_id, str)
        else tuple(
            record
            for record in records_by_id.values()
            if record.record_type == "contract.declared"
            and record.payload.get("candidate_id") == candidate_id
            and isinstance(record.payload.get("contract"), Mapping)
            and record.payload["contract"].get("id") == contract.id
        )
    )
    if len(declarations) != 1:
        raise CausalAllowanceConflict(
            "allowance grant lacks its exact committed Contract declaration"
        )
    declaration = declarations[0]
    try:
        validate_runtime_record(declaration)
        declared_contract = contract_from_dict(declaration.payload["contract"])
    except (TypeError, ValueError, KeyError) as exc:
        raise CausalAllowanceConflict(
            "allowance grant Contract declaration is invalid"
        ) from exc
    if (
        declared_contract != contract
        or declaration.causal_parents != (receipt.id,)
        or (
            declaration.id not in heads[0].payload.get("committed_record_ids", ())
            if proof_context is None or proof_context.has_opaque_heads
            else not proof_context.head_contains_member(heads[0], declaration.id)
        )
        or (
            declaration.id not in heads[0].causal_parents
            if proof_context is None or proof_context.has_opaque_heads
            else not proof_context.head_contains_parent(heads[0], declaration.id)
        )
    ):
        raise CausalAllowanceConflict(
            "allowance grant Contract declaration provenance differs"
        )
    return declaration


def allowance_return_id(
    candidate_id: str, recipient_contract_id: str, donor_contract_id: str
) -> str:
    """Return identity bound to one Candidate, donor, and recipient."""
    digest = compute_content_hash(
        canonical_json(
            {
                "candidate_id": candidate_id,
                "donor_contract_id": donor_contract_id,
                "recipient_contract_id": recipient_contract_id,
            }
        )
    )
    return f"return:{digest}"


def _pending_cancelled_donors(
    records: Sequence[RuntimeRecord],
) -> dict[str, RuntimeRecord]:
    terminals: dict[str, RuntimeRecord] = {}
    for record in records:
        if record.record_type != "lifecycle.terminal":
            continue
        contract_id = record.payload.get("contract_id")
        if not isinstance(contract_id, str) or not contract_id:
            continue
        try:
            validate_runtime_record(record)
        except ValueError as exc:
            raise CausalAllowanceConflict(
                "allowance cancellation fact is invalid"
            ) from exc
        if record.payload.get("terminal") not in {
            "complete",
            "failed",
            "cancelled",
            "unreachable",
        }:
            raise CausalAllowanceConflict("allowance terminal fact is malformed")
        if contract_id in terminals:
            raise CausalAllowanceConflict("multiple terminal facts for allowance donor")
        terminals[contract_id] = record
    return terminals


def _has_terminal(records: Sequence[RuntimeRecord], contract_id: str) -> bool:
    return any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == contract_id
        for record in records
    )


def _allowance_funding_path(
    donor: Contract,
    recipient: Contract,
    contracts_by_id: dict[str, Contract],
    records: Sequence[RuntimeRecord],
) -> tuple[RuntimeRecord, ...]:
    """Return committed reservation/grant facts along donor→recipient ancestry."""
    if donor.id == recipient.id or not _is_strict_ancestor(
        recipient.id, donor, contracts_by_id
    ):
        raise ValueError("allowance return recipient must be a strict ancestor")
    known_records = {record.id: record for record in records}
    recipient_grants = [
        record
        for record in records
        if record.record_type == "allowance.granted"
        and record.payload.get("contract_id") == recipient.id
    ]
    if len(recipient_grants) != 1:
        raise ValueError("allowance return recipient lacks one committed grant")
    recipient_grant = recipient_grants[0]
    _validate_committed_fact_bundle((recipient_grant,), known_records)
    path: list[RuntimeRecord] = []
    if recipient.parent_id is None:
        if (
            recipient_grant.payload.get("purpose") != "root"
            or recipient_grant.payload.get("source_contract_id") != ""
            or recipient_grant.payload.get("grant_id") != f"grant:{recipient.id}"
            or recipient_grant.payload.get("amount") != recipient.budget
            or type(recipient_grant.payload.get("amount")) is not int
            or set(recipient_grant.payload)
            != {"amount", "contract_id", "grant_id", "purpose", "source_contract_id"}
        ):
            raise ValueError("allowance return recipient root grant is invalid")
        receipt = _committed_fact_receipt(recipient_grant, known_records)
        if recipient_grant.causal_parents != (receipt.id,):
            raise ValueError(
                "allowance return recipient root grant causality is invalid"
            )
        path.append(recipient_grant)
    else:
        parent = contracts_by_id.get(recipient.parent_id)
        reservations = [
            record
            for record in records
            if record.record_type == "allowance.reserved"
            and record.payload.get("child_contract_id") == recipient.id
            and record.payload.get("source_contract_id") == recipient.parent_id
        ]
        if parent is None or len(reservations) != 1:
            raise ValueError("allowance return recipient grant path is incomplete")
        reservation = reservations[0]
        _validate_funding_edge(
            parent, recipient, reservation, recipient_grant, known_records
        )
        if reservation.id not in recipient_grant.causal_parents:
            raise ValueError("allowance return recipient grant is not funded")
        path.extend((reservation, recipient_grant))
    child = donor
    visited: set[str] = set()
    while child.id not in visited:
        visited.add(child.id)
        parent_id = child.parent_id
        if parent_id is None:
            break
        parent = contracts_by_id.get(parent_id)
        if parent is None:
            break
        reservations = [
            record
            for record in records
            if record.record_type == "allowance.reserved"
            and record.payload.get("child_contract_id") == child.id
            and record.payload.get("source_contract_id") == parent.id
        ]
        if len(reservations) != 1:
            break
        reservation = reservations[0]
        reservation_id = reservation.payload.get("reservation_id")
        grants = [
            record
            for record in records
            if record.record_type == "allowance.granted"
            and record.payload.get("contract_id") == child.id
            and record.payload.get("source_contract_id") == parent.id
            and record.payload.get("reservation_id") == reservation_id
        ]
        if len(grants) != 1:
            break
        grant = grants[0]
        try:
            _validate_funding_edge(parent, child, reservation, grant, known_records)
        except ValueError:
            break
        path.extend((reservation, grant))
        if parent.id == recipient.id:
            return tuple(path)
        child = parent
    raise ValueError("allowance return lacks a complete committed funding path")


def _is_strict_ancestor(
    ancestor_id: str, descendant: Contract, contracts_by_id: dict[str, Contract]
) -> bool:
    current = descendant
    visited: set[str] = set()
    while current.parent_id and current.id not in visited:
        visited.add(current.id)
        if current.parent_id == ancestor_id:
            return True
        parent = contracts_by_id.get(current.parent_id)
        if parent is None:
            return False
        current = parent
    return False


def _validate_funding_edge(
    parent: Contract,
    child: Contract,
    reservation: RuntimeRecord,
    grant: RuntimeRecord,
    records_by_id: dict[str, RuntimeRecord],
    *,
    proof_context: _AllowanceGrantProofContext | None = None,
) -> None:
    from aigineering.core.causal_allowance import (
        allowance_purpose,
        allowance_reservation_id,
    )

    expected_purpose = allowance_purpose(child)
    expected_reservation = allowance_reservation_id(
        parent.id, child.id, expected_purpose
    )
    amount = reservation.payload.get("amount")
    if (
        reservation.record_type != "allowance.reserved"
        or grant.record_type != "allowance.granted"
        or reservation.schema_version != 1
        or grant.schema_version != 1
        or type(amount) is not int
        or amount <= 0
        or amount != child.budget
        or grant.payload.get("amount") != amount
        or type(grant.payload.get("amount")) is not int
        or reservation.payload.get("child_contract_id") != child.id
        or reservation.payload.get("source_contract_id") != parent.id
        or reservation.payload.get("purpose") != expected_purpose
        or reservation.payload.get("reservation_id") != expected_reservation
        or set(reservation.payload)
        != {
            "amount",
            "child_contract_id",
            "purpose",
            "reservation_id",
            "source_contract_id",
        }
        or grant.payload.get("contract_id") != child.id
        or grant.payload.get("source_contract_id") != parent.id
        or grant.payload.get("purpose") != expected_purpose
        or grant.payload.get("reservation_id") != expected_reservation
        or grant.payload.get("grant_id") != f"grant:{child.id}"
        or set(grant.payload)
        != {
            "amount",
            "contract_id",
            "grant_id",
            "purpose",
            "reservation_id",
            "source_contract_id",
        }
        or reservation.id not in grant.causal_parents
        or len(reservation.causal_parents) != 1
        or grant.causal_parents != (reservation.id,)
    ):
        raise ValueError("allowance funding edge is malformed")
    _validate_committed_fact_bundle(
        (reservation, grant), records_by_id, proof_context=proof_context
    )


def _validate_committed_fact_bundle(
    facts: Sequence[RuntimeRecord],
    records_by_id: dict[str, RuntimeRecord],
    *,
    proof_context: _AllowanceGrantProofContext | None = None,
) -> None:
    head_candidates: set[str] | None = None
    for fact in facts:
        validate_runtime_record(fact)
        heads = (
            proof_context.matching_heads(fact.id)
            if proof_context is not None
            else tuple(
                head
                for head in records_by_id.values()
                if head.record_type == "candidate.committed"
                and fact.id in head.payload.get("committed_record_ids", ())
                and fact.id in head.causal_parents
            )
        )
        if len(heads) != 1:
            raise ValueError("allowance funding fact lacks one committed head")
        head = heads[0]
        if proof_context is None:
            validate_runtime_record(head)
            _validate_candidate_head(head, records_by_id)
            candidate_id = head.payload.get("candidate_id")
            receipt_ids = [
                parent_id
                for parent_id in head.causal_parents
                if parent_id in records_by_id
                and records_by_id[parent_id].record_type == "candidate.received"
                and records_by_id[parent_id].payload.get("candidate_id") == candidate_id
            ]
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or len(receipt_ids) != 1
            ):
                raise ValueError(
                    "allowance funding head lacks its authenticated receipt"
                )
            receipt = records_by_id[receipt_ids[0]]
            validate_runtime_record(receipt)
        else:
            receipt = proof_context.validate_head(head, records_by_id)
            candidate_id = head.payload.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError(
                    "allowance funding head lacks its authenticated receipt"
                )
        if proof_context is None or proof_context.has_opaque_heads:
            fact_is_causal_parent = fact.id in head.causal_parents
            receipt_is_causal_parent = receipt.id in head.causal_parents
        else:
            fact_is_causal_parent = proof_context.head_contains_parent(head, fact.id)
            receipt_is_causal_parent = proof_context.head_contains_parent(
                head, receipt.id
            )
        if not fact_is_causal_parent or not receipt_is_causal_parent:
            raise ValueError("allowance funding head causal closure is incomplete")
        if head_candidates is None:
            head_candidates = {head.id}
        else:
            head_candidates.intersection_update({head.id})
    if len(facts) > 1 and not head_candidates:
        raise ValueError("allowance funding edge facts lack a shared Candidate head")


def _committed_fact_receipt(
    fact: RuntimeRecord,
    records_by_id: dict[str, RuntimeRecord],
    *,
    proof_context: _AllowanceGrantProofContext | None = None,
) -> RuntimeRecord:
    receipts = [
        records_by_id[parent_id]
        for parent_id in fact.causal_parents
        if parent_id in records_by_id
        and records_by_id[parent_id].record_type == "candidate.received"
    ]
    if len(receipts) != 1:
        raise ValueError("allowance fact lacks its direct Candidate receipt")
    receipt = receipts[0]
    if proof_context is None:
        validate_runtime_record(receipt)
    else:
        proof_context.validate_receipt(receipt)
    return receipt


def _validate_candidate_head(
    head: RuntimeRecord,
    records_by_id: dict[str, RuntimeRecord],
    *,
    proof_context: _AllowanceGrantProofContext | None = None,
) -> RuntimeRecord:
    if set(head.payload) != {"candidate_id", "committed_record_ids"}:
        raise ValueError("Candidate committed head fields are invalid")
    candidate_id = head.payload.get("candidate_id")
    committed_ids = head.payload.get("committed_record_ids")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or not isinstance(committed_ids, (tuple, list))
        or not all(isinstance(item, str) and item for item in committed_ids)
    ):
        raise ValueError("Candidate committed head is malformed")
    if len(set(committed_ids)) != len(committed_ids):
        raise ValueError("Candidate committed head has duplicate record IDs")
    receipt_ids = [
        parent_id
        for parent_id in head.causal_parents
        if parent_id in records_by_id
        and records_by_id[parent_id].record_type == "candidate.received"
        and records_by_id[parent_id].payload.get("candidate_id") == candidate_id
    ]
    if len(receipt_ids) != 1 or head.causal_parents != (
        receipt_ids[0],
        *tuple(committed_ids),
    ):
        raise ValueError("Candidate committed head causal closure is malformed")
    receipt = records_by_id[receipt_ids[0]]
    if proof_context is None:
        validate_runtime_record(receipt)
    else:
        proof_context.validate_receipt(receipt)
    if receipt.payload.get("candidate_id") != candidate_id:
        raise ValueError("Candidate committed head receipt is mismatched")
    return receipt


def _validate_return_commitment_head(
    returned: RuntimeRecord,
    cancellation: RuntimeRecord,
    candidate_id: str,
    receipt_id: str,
    pending_by_id: dict[str, RuntimeRecord],
    all_records_by_id: dict[str, RuntimeRecord],
    heads: Sequence[RuntimeRecord],
    receipts: dict[str, RuntimeRecord],
) -> None:
    receipt = receipts.get(receipt_id)
    if (
        receipt is None
        or receipt.payload.get("candidate_id") != candidate_id
        or receipt.id not in returned.causal_parents
        or receipt.id not in cancellation.causal_parents
    ):
        raise CausalAllowanceConflict("allowance return receipt is not committed")
    matching = [
        head
        for head in heads
        if head.payload.get("candidate_id") == candidate_id
        and returned.id in head.payload.get("committed_record_ids", ())
        and cancellation.id in head.payload.get("committed_record_ids", ())
        and receipt_id in head.causal_parents
        and returned.id in head.causal_parents
        and cancellation.id in head.causal_parents
    ]
    if len(matching) != 1 or matching[0].id not in pending_by_id:
        raise CausalAllowanceConflict("allowance return lacks its Candidate head")
    head = matching[0]
    if returned.id not in all_records_by_id or cancellation.id not in all_records_by_id:
        raise CausalAllowanceConflict("allowance return commitment is incomplete")
    try:
        validate_runtime_record(head)
        validate_runtime_record(receipt)
        validate_runtime_record(cancellation)
        _validate_candidate_head(head, all_records_by_id)
    except ValueError as exc:
        raise CausalAllowanceConflict(
            "allowance return commitment records are invalid"
        ) from exc


def _is_exact_committed_return_replay(
    returned: RuntimeRecord,
    candidate_id: str,
    receipt_id: str,
    cancellation_id: str,
    heads: Sequence[RuntimeRecord],
    receipts: dict[str, RuntimeRecord],
    records_by_id: dict[str, RuntimeRecord],
) -> bool:
    receipt = receipts.get(receipt_id)
    cancellation = records_by_id.get(cancellation_id)
    if (
        receipt is None
        or receipt.payload.get("candidate_id") != candidate_id
        or cancellation is None
        or cancellation.record_type != "lifecycle.terminal"
        or cancellation.payload.get("terminal") != "cancelled"
        or cancellation.payload.get("contract_id")
        != returned.payload.get("from_contract_id")
        or receipt_id not in cancellation.causal_parents
    ):
        return False
    return any(
        head.payload.get("candidate_id") == candidate_id
        and returned.id in head.payload.get("committed_record_ids", ())
        and cancellation_id in head.payload.get("committed_record_ids", ())
        and receipt_id in head.causal_parents
        and returned.id in head.causal_parents
        and cancellation_id in head.causal_parents
        for head in heads
    )
