"""Signed, append-only task-to-attachment association effects."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from aigineering.business import artifacts
from aigineering.core.publication_history import (
    validate_published_member,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord

LINKED = "task.attachment.linked"


def validate_link_history(
    records: Iterable[RuntimeRecord], contract_id: str
) -> tuple[RuntimeRecord, ...]:
    """Validate every association fact for one task against immutable witnesses."""
    rows = tuple(records)
    selected = tuple(
        record
        for record in rows
        if record.record_type == LINKED
        and record.payload.get("contract_id") == contract_id
    )
    by_id: dict[str, list[RuntimeRecord]] = {}
    for record in rows:
        by_id.setdefault(record.id, []).append(record)
    domain_id = _domain_id(rows)
    seen_keys: set[str] = set()
    for link in selected:
        _valid_record(link)
        payload = link.payload
        if set(payload) != {
            "actor_id",
            "asset_candidate_id",
            "asset_head_id",
            "asset_id",
            "asset_receipt_id",
            "asset_record_id",
            "candidate_id",
            "contract_id",
            "contract_record_id",
            "key_id",
            "idempotency_key",
        }:
            raise ValueError("attachment link fields are invalid")
        asset_id = _text(payload.get("asset_id"), "asset_id")
        key = _text(payload.get("idempotency_key"), "idempotency_key")
        if key in seen_keys:
            raise ValueError("attachment history reuses an idempotency key")
        seen_keys.add(key)
        if len(link.causal_parents) != 5 or link.causal_parents[0] not in by_id:
            raise ValueError("attachment link causal witnesses are incomplete")
        receipt = _one(by_id, link.causal_parents[0], "candidate.received")
        expected_witnesses = (
            payload.get("contract_record_id"),
            payload.get("asset_record_id"),
            payload.get("asset_receipt_id"),
            payload.get("asset_head_id"),
        )
        if tuple(link.causal_parents[1:]) != expected_witnesses:
            raise ValueError("attachment link witness parents changed")
        _validate_link_member(rows, link)
        if receipt.payload.get("candidate_id") != payload.get("candidate_id"):
            raise ValueError("attachment link receipt does not match its Candidate")
        declaration = _one(
            by_id, str(payload.get("contract_record_id")), "contract.declared"
        )
        asset_fact = _one(by_id, str(payload.get("asset_record_id")), "asset.committed")
        declarations_for_task = [
            row
            for row in rows
            if row.record_type == "contract.declared"
            and isinstance(row.payload.get("contract"), Mapping)
            and row.payload["contract"].get("id") == contract_id
        ]
        assets_for_id = [
            row
            for row in rows
            if row.record_type == "asset.committed"
            and isinstance(row.payload.get("asset"), Mapping)
            and row.payload["asset"].get("id") == asset_id
        ]
        if (
            len(declarations_for_task) != 1
            or declarations_for_task[0].id != declaration.id
        ):
            raise ValueError("attachment task declaration is ambiguous")
        if len(assets_for_id) != 1 or assets_for_id[0].id != asset_fact.id:
            raise ValueError("attachment Asset publication is ambiguous")
        if declaration.payload.get("contract", {}).get("id") != contract_id:
            raise ValueError("attachment link task declaration is mismatched")
        from aigineering.core.contract_admission import validate_contract_commitment
        from aigineering.protocol.wire import contract_from_dict

        try:
            declared_contract = contract_from_dict(
                deep_thaw(declaration.payload["contract"])
            )
            validate_contract_commitment(declared_contract)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("attachment task declaration is invalid") from exc
        validate_contract_declaration_member(
            rows,
            declaration,
            contract_id=contract_id,
            domain_id=domain_id,
        )
        raw_asset = asset_fact.payload.get("asset")
        if not isinstance(raw_asset, Mapping) or raw_asset.get("id") != asset_id:
            raise ValueError("attachment link Asset witness is mismatched")
        from aigineering.protocol.wire import asset_from_dict

        try:
            historical_asset = asset_from_dict(dict(raw_asset))
            if artifacts.envelope(historical_asset)["kind"] != "attachment":
                raise ValueError("historical Asset is not an attachment")
            artifacts.attachment_bytes(historical_asset)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("historical attachment bytes are unavailable") from exc
        source_receipt, source_head = validate_published_member(
            rows,
            asset_fact,
            expected_candidate=str(payload.get("asset_candidate_id")),
            expected_receipt=str(payload.get("asset_receipt_id")),
            expected_head=str(payload.get("asset_head_id")),
            domain_id=domain_id,
        )
        source_effect_types = source_receipt.payload.get("effect_types", ())
        if not isinstance(source_effect_types, (tuple, list)) or (
            "asset.propose" not in source_effect_types
        ):
            raise ValueError("attachment source publication is unsupported")
    return selected


def validate_contract_declaration_member(
    records, declaration, *, contract_id, domain_id=None
):
    """Validate a task declaration and its funding inside its exact publication."""
    rows = tuple(records)
    receipt, head = validate_published_member(rows, declaration, domain_id=domain_id)
    if declaration.payload.get("contract", {}).get("id") != contract_id:
        raise ValueError("task declaration identifies another Contract")
    members = tuple(head.payload.get("committed_record_ids", ()))
    by_id = {record.id: record for record in rows}
    committed = tuple(by_id[record_id] for record_id in members)
    declarations = tuple(
        record for record in committed if record.record_type == "contract.declared"
    )
    if declaration not in declarations:
        raise ValueError("task declaration is absent from its committed head")

    # The head may contain other effects from the same Candidate. Validate all
    # declarations and their exact funding witnesses without imposing a
    # constructor or publisher-effect shape on the task.
    _validate_declaration_allowance_bundle(rows, receipt, head, committed, declarations)
    from aigineering.core.contract_admission import validate_contract_commitment
    from aigineering.protocol.wire import contract_from_dict

    declared_ids = set()
    try:
        for record in declarations:
            contract = contract_from_dict(deep_thaw(record.payload["contract"]))
            validate_contract_commitment(contract)
            if contract.id in declared_ids:
                raise ValueError("declaration head repeats a Contract")
            declared_ids.add(contract.id)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("task declaration head contains an invalid Contract") from exc
    return receipt, head


def _validate_declaration_allowance_bundle(
    rows, receipt, head, committed, declarations
):
    from aigineering.core.causal_allowance import validate_allowance_grants
    from aigineering.protocol.wire import contract_from_dict

    needed = {record.id: record for record in declarations}
    for record in declarations:
        payload = record.payload.get("contract")
        if not isinstance(payload, Mapping):
            raise ValueError("declaration has no Contract payload")
        parent_id = payload.get("parent_id")
        if parent_id is not None:
            parents = [
                item
                for item in rows
                if item.record_type == "contract.declared"
                and isinstance(item.payload.get("contract"), Mapping)
                and item.payload["contract"].get("id") == parent_id
            ]
            if len(parents) != 1:
                raise ValueError("child declaration has no unique parent fact")
            needed[parents[0].id] = parents[0]
    contracts = tuple(
        contract_from_dict(deep_thaw(record.payload["contract"]))
        for record in needed.values()
    )
    declared_contracts = tuple(
        contract_from_dict(deep_thaw(record.payload["contract"]))
        for record in declarations
    )
    expected_reservations = sum(
        contract.parent_id is not None for contract in declared_contracts
    )
    grants = tuple(
        record for record in committed if record.record_type == "allowance.granted"
    )
    reservations = tuple(
        record for record in committed if record.record_type == "allowance.reserved"
    )
    if len(grants) != len(declarations) or len(reservations) != expected_reservations:
        raise ValueError("declaration head is missing allowance funding facts")
    pending_records = (receipt, *committed, head)
    pending_ids = {record.id for record in pending_records}
    existing = tuple(record for record in rows if record.id not in pending_ids)
    validate_allowance_grants(contracts, existing, pending_records)


def _validate_link_member(records, member):
    rows = tuple(records)
    _valid_record(member)
    parents = tuple(member.causal_parents)
    if len(parents) != 5:
        raise ValueError("attachment link has incomplete causal witnesses")
    receipt = next(
        (
            row
            for row in rows
            if row.id == parents[0] and row.record_type == "candidate.received"
        ),
        None,
    )
    if receipt is None:
        raise ValueError("attachment link has no unique receipt")
    candidate_id = member.payload.get("candidate_id")
    receipts = [
        row
        for row in rows
        if row.record_type == "candidate.received"
        and row.payload.get("candidate_id") == candidate_id
    ]
    heads = [
        row
        for row in rows
        if row.record_type == "candidate.committed"
        and row.payload.get("candidate_id") == candidate_id
    ]
    if len(receipts) != 1 or receipts[0].id != receipt.id or len(heads) != 1:
        raise ValueError("attachment link has no unique Candidate proof")
    head = heads[0]
    members = head.payload.get("committed_record_ids")
    if (
        not isinstance(members, (tuple, list))
        or len(members) != len(set(members))
        or tuple(members) != (member.id,)
        or tuple(head.causal_parents) != (receipt.id, *tuple(members))
        or receipt.id not in head.causal_parents
        or member.id not in head.causal_parents
        or tuple(receipt.payload.get("effect_types", ())) != ("task.attachment.link",)
        or receipt.payload.get("actor_id") != member.payload.get("actor_id")
        or receipt.payload.get("key_id") != member.payload.get("key_id")
        or receipt.payload.get("domain_id") != _domain_id(rows)
        or not isinstance(member.payload.get("idempotency_key"), str)
        or not member.payload.get("idempotency_key")
        or tuple(receipt.causal_parents)
        != (
            member.payload.get("contract_record_id"),
            member.payload.get("asset_record_id"),
            member.payload.get("asset_receipt_id"),
            member.payload.get("asset_head_id"),
        )
    ):
        raise ValueError("attachment link is not a member of its exact committed head")
    _valid_record(receipt)
    _valid_record(head)
    return receipt, head


def _one(by_id, record_id, record_type):
    rows = [row for row in by_id.get(record_id, ()) if row.record_type == record_type]
    if len(rows) != 1:
        raise ValueError("attachment publication history is unavailable")
    return rows[0]


def _valid_record(record):
    from aigineering.protocol.runtime_record import validate_runtime_record

    validate_runtime_record(record)


def _domain_id(records):
    genesis = [record for record in records if record.record_type == "domain.genesis"]
    if len(genesis) != 1 or not isinstance(genesis[0].payload.get("manifest"), Mapping):
        raise ValueError("attachment domain Genesis is unavailable")
    _valid_record(genesis[0])
    domain_id = genesis[0].payload["manifest"].get("id")
    if not isinstance(domain_id, str) or not domain_id:
        raise ValueError("attachment domain Genesis is invalid")
    return domain_id


def _asset_payload(asset):
    from aigineering.protocol.wire import asset_to_dict

    return asset_to_dict(asset)


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value


__all__ = [
    "LINKED",
    "validate_contract_declaration_member",
    "validate_link_history",
    "validate_published_member",
]
