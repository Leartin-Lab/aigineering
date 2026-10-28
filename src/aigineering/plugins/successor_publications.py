"""Shared pure proof for accepted task-successor publication relations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aigineering.core.publication_history import (
    read_effect_types,
    validate_published_member,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import validate_runtime_record
from aigineering.protocol.wire import contract_to_dict


@dataclass(frozen=True)
class SuccessorPublicationWitness:
    """Neutral exact witnesses for one accepted source-to-successor link."""

    source_contract_id: str
    target_contract_id: str
    source_declaration_id: str
    source_receipt_id: str
    source_head_id: str
    link_record_id: str
    link_receipt_id: str
    link_head_id: str
    cancelled_terminal_id: str
    target_declaration_id: str
    target_receipt_id: str
    target_head_id: str


@dataclass(frozen=True)
class SuccessorPublicationIssue:
    """Safe known endpoint and exact-link IDs for unverified history."""

    source_contract_id: str | None = None
    target_contract_id: str | None = None
    record_id: str | None = None


def validate_successor_publications(
    query_context,
    records: tuple,
    publication_index,
    *,
    link_record_type: str,
    sole_effect_type: str,
    source_contract_field: str,
    target_contract_field: str,
) -> tuple[
    tuple[SuccessorPublicationWitness, ...], tuple[SuccessorPublicationIssue, ...]
]:
    """Prove common endpoint, cancellation and unique-link publication shape.

    Feature wrappers supply fixed record/effect/field constants. This helper
    owns no feature policy and leaves receipt/head/member validation to the
    generic publication-history owner.
    """
    captured = tuple(record for _revision, record in query_context.runtime_records)
    if len(records) != len(captured) or any(
        supplied is not original for supplied, original in zip(records, captured)
    ):
        raise ValueError("successor history does not match its captured query")

    contracts_by_id: dict[str, list] = {}
    for contract in query_context.contracts:
        contracts_by_id.setdefault(contract.id, []).append(contract)
    declarations_by_id: dict[str, list] = {}
    for record in records:
        if record.record_type != "contract.declared" or not isinstance(
            record.payload, Mapping
        ):
            continue
        raw = record.payload.get("contract")
        if isinstance(raw, Mapping) and isinstance(raw.get("id"), str):
            declarations_by_id.setdefault(raw["id"], []).append(record)

    witnesses: list[SuccessorPublicationWitness] = []
    issues: list[SuccessorPublicationIssue] = []
    endpoint_cache: dict[str, tuple] = {}
    for link in records:
        if link.record_type != link_record_type:
            continue
        safe_record_id = None
        payload = link.payload if isinstance(link.payload, Mapping) else {}
        raw_source = payload.get(source_contract_field)
        raw_target = payload.get(target_contract_field)
        source_id = (
            raw_source
            if isinstance(raw_source, str)
            and len(contracts_by_id.get(raw_source, ())) == 1
            else None
        )
        target_id = (
            raw_target
            if isinstance(raw_target, str)
            and len(contracts_by_id.get(raw_target, ())) == 1
            else None
        )
        try:
            validate_runtime_record(link)
            safe_record_id = link.id
            candidate_id = payload.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or source_id is None
                or target_id is None
                or source_id == target_id
            ):
                raise ValueError("successor endpoints are not uniquely materialized")
            source, target = (
                contracts_by_id[source_id][0],
                contracts_by_id[target_id][0],
            )

            def exact_declaration(contract):
                cached = endpoint_cache.get(contract.id)
                if cached is not None:
                    return cached
                declarations = declarations_by_id.get(contract.id, ())
                if len(declarations) != 1:
                    raise ValueError(
                        "successor endpoint declaration is missing or ambiguous"
                    )
                declaration = declarations[0]
                if deep_thaw(declaration.payload.get("contract")) != deep_thaw(
                    contract_to_dict(contract)
                ):
                    raise ValueError(
                        "successor declaration differs from materialized Contract"
                    )
                receipt, head = validate_published_member(
                    records,
                    declaration,
                    domain_id=query_context.domain_id,
                    record_index=publication_index,
                )
                cached = (declaration, receipt, head)
                endpoint_cache[contract.id] = cached
                return cached

            source_declaration, source_receipt, source_head = exact_declaration(source)
            target_declaration, target_receipt, target_head = exact_declaration(target)
            receipt, head = validate_published_member(
                records,
                link,
                expected_candidate=candidate_id,
                domain_id=query_context.domain_id,
                allow_additional_causal_parents=True,
                record_index=publication_index,
            )
            if read_effect_types(receipt.payload) != (sole_effect_type,):
                raise ValueError("successor receipt has an unexpected effect")
            candidate_links = publication_index.by_type_candidate.get(
                (link_record_type, candidate_id), ()
            )
            if len(candidate_links) != 1 or candidate_links[0] is not link:
                raise ValueError("successor Candidate has multiple typed links")
            member_ids = tuple(head.payload.get("committed_record_ids", ()))
            head_links = tuple(
                member
                for member_id in member_ids
                for member_rows in (publication_index.by_id.get(member_id, ()),)
                if len(member_rows) == 1
                for member in member_rows
                if member.record_type == link_record_type
            )
            if len(head_links) != 1 or head_links[0] is not link:
                raise ValueError("successor head has multiple typed links")
            if (
                target_receipt.id != receipt.id
                or target_head.id != head.id
                or target_declaration.id not in member_ids
                or target_declaration.id not in link.causal_parents
            ):
                raise ValueError("successor declaration is detached")
            source_terminals = []
            for member_id in member_ids:
                member_rows = publication_index.by_id.get(member_id, ())
                if len(member_rows) != 1:
                    raise ValueError("successor head member is missing or ambiguous")
                member = member_rows[0]
                if (
                    member.record_type == "lifecycle.terminal"
                    and member.payload.get("contract_id") == source_id
                ):
                    source_terminals.append(member)
            if (
                len(source_terminals) != 1
                or source_terminals[0].payload.get("terminal") != "cancelled"
                or source_terminals[0].id not in link.causal_parents
                or link.id not in member_ids
            ):
                raise ValueError("successor source cancellation is detached")
            witnesses.append(
                SuccessorPublicationWitness(
                    source_contract_id=source_id,
                    target_contract_id=target_id,
                    source_declaration_id=source_declaration.id,
                    source_receipt_id=source_receipt.id,
                    source_head_id=source_head.id,
                    link_record_id=link.id,
                    link_receipt_id=receipt.id,
                    link_head_id=head.id,
                    cancelled_terminal_id=source_terminals[0].id,
                    target_declaration_id=target_declaration.id,
                    target_receipt_id=target_receipt.id,
                    target_head_id=target_head.id,
                )
            )
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            issues.append(
                SuccessorPublicationIssue(
                    source_contract_id=source_id,
                    target_contract_id=target_id,
                    record_id=safe_record_id,
                )
            )
    return tuple(witnesses), tuple(issues)
