"""Pure Candidate-native active task handoff projection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from aigineering.protocol.runtime_record import (
    RuntimeRecord,
)
from aigineering.plugins.successor_publications import (
    validate_successor_publications,
)


@dataclass(frozen=True)
class HandoffPublicationWitness:
    """Exact read-only witnesses for one accepted handoff relation."""

    source_contract_id: str
    target_contract_id: str
    source_declaration_id: str
    source_receipt_id: str
    source_head_id: str
    handoff_link_id: str
    handoff_receipt_id: str
    handoff_head_id: str
    cancelled_terminal_id: str
    target_declaration_id: str
    target_receipt_id: str
    target_head_id: str


@dataclass(frozen=True)
class HandoffPublicationIssue:
    """Safe known endpoints and fact reference for an unverified handoff."""

    code: str
    source_contract_id: str | None = None
    target_contract_id: str | None = None
    record_id: str | None = None


def validate_task_handoff_relations(
    query_context, records: tuple, publication_index
) -> tuple[tuple[HandoffPublicationWitness, ...], tuple[HandoffPublicationIssue, ...]]:
    """Verify accepted handoff links using one immutable query and shared proof index.

    This proves only the published source-to-successor relation. Variant-specific
    budget, tree, hold, assignment, and allowance decisions remain owned by the
    handoff publisher and are not re-evaluated against current state here.
    """
    witnesses, issues = validate_successor_publications(
        query_context,
        records,
        publication_index,
        link_record_type="task.handed_off",
        sole_effect_type="task.handoff",
        source_contract_field="contract_id",
        target_contract_field="successor_id",
    )
    return (
        tuple(
            HandoffPublicationWitness(
                source_contract_id=item.source_contract_id,
                target_contract_id=item.target_contract_id,
                source_declaration_id=item.source_declaration_id,
                source_receipt_id=item.source_receipt_id,
                source_head_id=item.source_head_id,
                handoff_link_id=item.link_record_id,
                handoff_receipt_id=item.link_receipt_id,
                handoff_head_id=item.link_head_id,
                cancelled_terminal_id=item.cancelled_terminal_id,
                target_declaration_id=item.target_declaration_id,
                target_receipt_id=item.target_receipt_id,
                target_head_id=item.target_head_id,
            )
            for item in witnesses
        ),
        tuple(
            HandoffPublicationIssue(
                code="handoff_publication_unverified",
                source_contract_id=item.source_contract_id,
                target_contract_id=item.target_contract_id,
                record_id=item.record_id,
            )
            for item in issues
        ),
    )


def project_task_handoffs(
    runtime_records: Iterable[RuntimeRecord],
    *,
    source_contract_id: str | None = None,
) -> tuple[RuntimeRecord, ...]:
    """Return durable handoff link facts in the supplied revision order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type == "task.handed_off"
        and (
            source_contract_id is None
            or record.payload.get("contract_id") == source_contract_id
        )
    )
