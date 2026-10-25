"""Pure typed requests and shape rules for allowance publication relations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeAlias

from aigineering.core.ids import validate_contract_identity
from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.protocol.types import Contract
from aigineering.protocol.wire import contract_from_dict

if TYPE_CHECKING:
    from aigineering.core.causal_allowance import RawAllowanceTotals


def safe_committed_head_relations(
    record: RuntimeRecord,
) -> tuple[frozenset[str], frozenset[str]] | None:
    """Return indexable members and parents, without validating the head."""
    if record.record_type != "candidate.committed":
        return None
    members = record.payload.get("committed_record_ids", ())
    parents = record.causal_parents
    if (
        not isinstance(members, (list, tuple))
        or not all(isinstance(item, str) and item for item in members)
        or not isinstance(parents, tuple)
        or not all(isinstance(item, str) and item for item in parents)
    ):
        return None
    return frozenset(members), frozenset(parents)


@dataclass(frozen=True)
class PublicationHeadsRequest:
    member_ids: tuple[str, ...]


@dataclass(frozen=True)
class PublicationClosureRequest:
    record_ids: tuple[str, ...]


@dataclass(frozen=True)
class DeclarationPairsRequest:
    pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PublicationSelection:
    records: tuple[RuntimeRecord, ...]
    complete: bool


@dataclass(frozen=True)
class CanonicalAllowanceDeclarationBatch:
    candidate_id: str
    incoming_contracts: tuple[Contract, ...]
    receipt: RuntimeRecord
    head: RuntimeRecord
    declarations: tuple[RuntimeRecord, ...]
    funding_facts: tuple[RuntimeRecord, ...]


def build_canonical_allowance_declaration_batch(
    runtime_records: tuple[RuntimeRecord, ...],
    candidate_id: str,
    *,
    candidate_proposal,
    effect_fact_owners=(),
) -> CanonicalAllowanceDeclarationBatch | None:
    """Build a safe hint for a pure, built-in declaration Candidate.

    Failure means that the SQLite adapter must use the reference reader; it is
    never a business rejection or a replacement for the original validator.
    """
    allowed = {
        "candidate.received",
        "candidate.committed",
        "contract.declared",
        "allowance.granted",
        "allowance.reserved",
        "trace.recorded",
        "attempt.closed",
    }
    effects = getattr(candidate_proposal, "effects", ())
    if (
        not runtime_records
        or candidate_proposal is None
        or getattr(candidate_proposal, "id", None) != candidate_id
        or not effects
        or any(effect.effect_type != "contract.declare" for effect in effects)
        or any(record.record_type not in allowed for record in runtime_records)
        or not any(
            record.record_type == "contract.declared" for record in runtime_records
        )
        or any(
            record.record_type == "attempt.closed"
            and record.payload.get("outcome") != "expanded"
            for record in runtime_records
        )
    ):
        return None

    receipts = tuple(
        record
        for record in runtime_records
        if record.record_type == "candidate.received"
    )
    heads = tuple(
        record
        for record in runtime_records
        if record.record_type == "candidate.committed"
    )
    if (
        len(receipts) != 1
        or len(heads) != 1
        or receipts[0].payload.get("candidate_id") != candidate_id
        or heads[0].payload.get("candidate_id") != candidate_id
    ):
        return None
    declarations = tuple(
        record
        for record in runtime_records
        if record.record_type == "contract.declared"
    )
    if len(effects) != len(declarations) or len(effect_fact_owners) != len(
        declarations
    ):
        return None
    declaration_ids = {record.id for record in declarations}
    if (
        any(
            getattr(owner, "effect_type", None) != "contract.declare"
            or getattr(owner, "record_id", None) not in declaration_ids
            or getattr(getattr(owner, "assignment_admission", None), "value", None)
            not in {"none", None}
            for owner in effect_fact_owners
        )
        or {owner.record_id for owner in effect_fact_owners} != declaration_ids
    ):
        return None
    try:
        incoming = tuple(
            contract_from_dict(record.payload["contract"]) for record in declarations
        )
        if not incoming:
            return None
        for contract in incoming:
            if not contract.id.startswith(
                ("task:v3:", "task:v4:", "task:v5:", "task:v6:")
            ):
                return None
            validate_contract_identity(contract)
    except (KeyError, TypeError, ValueError):
        return None
    source_ids = {
        contract.parent_id for contract in incoming if contract.parent_id is not None
    }
    if any(
        not source_id.startswith(("task:v3:", "task:v4:", "task:v5:", "task:v6:"))
        for source_id in source_ids
    ):
        return None
    return CanonicalAllowanceDeclarationBatch(
        candidate_id=candidate_id,
        incoming_contracts=incoming,
        receipt=receipts[0],
        head=heads[0],
        declarations=declarations,
        funding_facts=tuple(
            record
            for record in runtime_records
            if record.record_type in {"allowance.granted", "allowance.reserved"}
        ),
    )


def publication_heads_for_funding_facts(
    batch: CanonicalAllowanceDeclarationBatch,
) -> PublicationHeadsRequest:
    return PublicationHeadsRequest(tuple(record.id for record in batch.funding_facts))


def publication_closure_for_funding_batch(
    batch: CanonicalAllowanceDeclarationBatch,
) -> PublicationClosureRequest | None:
    relations = safe_committed_head_relations(batch.head)
    raw_members = batch.head.payload.get("committed_record_ids")
    if (
        relations is None
        or len(raw_members) != len(set(raw_members))
        or len(batch.head.causal_parents) != len(set(batch.head.causal_parents))
        or any(
            not isinstance(fact.causal_parents, tuple) for fact in batch.funding_facts
        )
        or any(
            not isinstance(parent, str) or not parent
            for fact in batch.funding_facts
            for parent in fact.causal_parents
        )
    ):
        return None
    members, parents = relations
    first_ids = {record.id for record in batch.funding_facts}
    first_ids.update(members)
    first_ids.update(parents)
    for fact in batch.funding_facts:
        first_ids.update(fact.causal_parents)
    if (
        batch.head.causal_parents.count(batch.receipt.id) != 1
        or batch.receipt.payload.get("candidate_id") != batch.candidate_id
    ):
        return None
    if any(
        fact.id not in members or fact.id not in parents for fact in batch.funding_facts
    ):
        return None
    return PublicationClosureRequest(tuple(sorted(first_ids)))


def publication_declaration_pairs(
    batch: CanonicalAllowanceDeclarationBatch,
) -> tuple[tuple[str, str], ...] | None:
    pairs: list[tuple[str, str]] = []
    for fact in batch.funding_facts:
        target_id = (
            fact.payload.get("contract_id")
            if fact.record_type == "allowance.granted"
            else fact.payload.get("child_contract_id")
        )
        if not isinstance(target_id, str) or not target_id:
            return None
        pairs.append((batch.candidate_id, target_id))
    return tuple(dict.fromkeys(pairs))


def publication_heads_for_declarations(
    declaration_ids: tuple[str, ...],
) -> PublicationHeadsRequest:
    return PublicationHeadsRequest(tuple(dict.fromkeys(declaration_ids)))


def publication_closure_for_heads(
    declaration_ids: tuple[str, ...],
    heads: tuple[RuntimeRecord, ...],
) -> PublicationClosureRequest | None:
    selected_ids = set(declaration_ids)
    for head in heads:
        relations = safe_committed_head_relations(head)
        raw_members = head.payload.get("committed_record_ids")
        if (
            relations is None
            or len(raw_members) != len(set(raw_members))
            or len(head.causal_parents) != len(set(head.causal_parents))
        ):
            return None
        members, parents = relations
        selected_ids.add(head.id)
        selected_ids.update(members)
        selected_ids.update(parents)
    return PublicationClosureRequest(tuple(sorted(selected_ids)))


@dataclass(frozen=True)
class AllowanceBalanceInput:
    contract_id: str
    totals: RawAllowanceTotals | None
    reference_records: tuple[RuntimeRecord, ...] = ()


PublicationRequest: TypeAlias = (
    PublicationHeadsRequest | PublicationClosureRequest | DeclarationPairsRequest
)
