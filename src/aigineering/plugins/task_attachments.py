"""Signed, append-only task-to-attachment association effects."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from aigineering.business import artifacts
from aigineering.core.effect_projectors import EffectProjection
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.publication_history import (
    validate_published_member,
)
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.wire import contract_to_dict

LINKED = "task.attachment.linked"


def task_attachment_link_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    if candidate.effects != (effect,):
        raise ValueError("attachment link Candidate must contain exactly one effect")
    payload = effect.payload
    required = {
        "contract_id",
        "asset_id",
        "contract_record_id",
        "asset_record_id",
        "asset_candidate_id",
        "asset_receipt_id",
        "asset_head_id",
        "idempotency_key",
    }
    if set(payload) != required:
        raise ValueError("attachment link effect fields are invalid")
    contract_id = _text(payload["contract_id"], "contract_id")
    asset_id = _text(payload["asset_id"], "asset_id")
    contract_fact_id = _text(payload["contract_record_id"], "contract_record_id")
    asset_fact_id = _text(payload["asset_record_id"], "asset_record_id")
    source_candidate = _text(payload["asset_candidate_id"], "asset_candidate_id")
    source_receipt = _text(payload["asset_receipt_id"], "asset_receipt_id")
    source_head = _text(payload["asset_head_id"], "asset_head_id")
    idempotency_key = _text(payload["idempotency_key"], "idempotency_key")
    if candidate.idempotency_key != idempotency_key:
        raise ValueError("attachment link idempotency key does not match Candidate")

    contracts = [
        contract for contract in context.contracts if contract.id == contract_id
    ]
    assets = [asset for asset in context.assets if asset.id == asset_id]
    declarations = [
        record
        for record in context.runtime_records
        if record.id == contract_fact_id
        and record.record_type == "contract.declared"
        and isinstance(record.payload.get("contract"), Mapping)
        and record.payload["contract"].get("id") == contract_id
    ]
    committed_assets = [
        record
        for record in context.runtime_records
        if record.id == asset_fact_id
        and record.record_type == "asset.committed"
        and isinstance(record.payload.get("asset"), Mapping)
        and record.payload["asset"].get("id") == asset_id
    ]
    if (
        len(contracts) != 1
        or len(assets) != 1
        or len(declarations) != 1
        or len(committed_assets) != 1
    ):
        raise ValueError(
            f"attachment publication is unavailable ({len(contracts)},{len(assets)},{len(declarations)},{len(committed_assets)})"
        )
    declaration = declarations[0]
    if deep_thaw(declaration.payload["contract"]) != deep_thaw(
        contract_to_dict(contracts[0])
    ):
        raise ValueError("task declaration does not match its current Contract")
    records = tuple(context.runtime_records)
    domain_id = _domain_id(records)
    if candidate.domain_id != domain_id:
        raise ValueError("attachment Candidate belongs to another domain")
    validate_contract_declaration_member(
        records, declaration, contract_id=contract_id, domain_id=domain_id
    )
    committed = committed_assets[0]
    source_receipt_record, source_head_record = validate_published_member(
        records,
        committed,
        expected_candidate=source_candidate,
        expected_receipt=source_receipt,
        expected_head=source_head,
        domain_id=domain_id,
    )
    if tuple(source_receipt_record.payload.get("effect_types", ())) != (
        "asset.propose",
    ):
        raise ValueError("attachment was not published by the supported import effect")
    expected_parents = (
        declaration.id,
        committed.id,
        source_receipt,
        source_head,
    )
    if candidate.causal_parents != expected_parents:
        raise ValueError("attachment Candidate has incorrect causal witnesses")
    fact = create_runtime_record(
        LINKED,
        {
            "actor_id": candidate.actor_id,
            "asset_candidate_id": source_candidate,
            "asset_head_id": source_head,
            "asset_id": asset_id,
            "asset_receipt_id": source_receipt,
            "asset_record_id": committed.id,
            "candidate_id": candidate.id,
            "contract_id": contract_id,
            "contract_record_id": declaration.id,
            "key_id": candidate.key_id,
            "idempotency_key": idempotency_key,
        },
        causal_parents=(receipt_id, *expected_parents),
    )
    existing = [record for record in records if record.id == fact.id]
    if existing:
        if len(existing) != 1:
            raise ValueError("attachment link history is ambiguous")
        _validate_link_member(records, existing[0])
        if deep_thaw(existing[0].payload) != deep_thaw(fact.payload):
            raise ValueError("attachment link replay conflicts with its original fact")
        return EffectProjection((existing[0],), contract_id)
    if any(
        record.record_type == LINKED
        and record.payload.get("contract_id") == contract_id
        and record.payload.get("idempotency_key") == idempotency_key
        for record in records
    ):
        raise ValueError("attachment idempotency key is already bound to another link")
    asset = assets[0]
    if deep_thaw(committed.payload["asset"]) != _asset_payload(asset):
        raise ValueError("attachment Asset does not match its committed fact")
    try:
        if artifacts.envelope(asset)["kind"] != "attachment":
            raise ValueError("asset is not an attachment")
        artifacts.attachment_bytes(asset)
    except (TypeError, ValueError) as exc:
        raise ValueError("attachment bytes are unavailable") from exc
    return EffectProjection((fact,), contract_id)


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
        if tuple(source_receipt.payload.get("effect_types", ())) != ("asset.propose",):
            raise ValueError("attachment source publication is unsupported")
    return selected


def validate_contract_declaration_member(
    records, declaration, *, contract_id, domain_id=None
):
    """Validate a declaration under a known publisher's complete head shape."""
    rows = tuple(records)
    receipt, head = validate_published_member(rows, declaration, domain_id=domain_id)
    if declaration.payload.get("contract", {}).get("id") != contract_id:
        raise ValueError("task declaration identifies another Contract")
    effects = receipt.payload.get("effect_types")
    members = tuple(head.payload.get("committed_record_ids", ()))
    by_id = {record.id: record for record in rows}
    committed = tuple(by_id[record_id] for record_id in members)
    declarations = tuple(
        record for record in committed if record.record_type == "contract.declared"
    )
    if declaration not in declarations:
        raise ValueError("task declaration is absent from its committed head")
    _validate_declaration_allowance_bundle(rows, receipt, head, committed, declarations)

    # Direct fan-out publishers (planning, delegation, staged planning) may
    # publish several declarations in one Candidate. Their entire head must
    # consist of exactly that many canonical declaration facts.
    if (
        isinstance(effects, (tuple, list))
        and effects
        and all(effect == "contract.declare" for effect in effects)
    ):
        if len(declarations) != len(effects) or any(
            record.record_type
            not in {
                "contract.declared",
                "allowance.granted",
                "allowance.reserved",
            }
            for record in committed
        ):
            raise ValueError("multi-task declaration head is incomplete")
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
        except (TypeError, KeyError) as exc:
            raise ValueError("declaration head contains an invalid Contract") from exc
        return receipt, head

    # These three existing compound publishers emit a declaration together
    # with a typed relation proving how the ordinary task was produced.
    compound = {
        ("task.handoff",): (
            "task.handed_off",
            "successor_id",
            {
                "lifecycle.terminal",
                "contract.declared",
                "claim.assignment.changed",
                "claim.hold.placed",
                "allowance.returned",
                "task.handed_off",
                "allowance.granted",
                "allowance.reserved",
            },
        ),
        ("task.revise",): (
            "task.revised",
            "successor_id",
            {
                "lifecycle.terminal",
                "contract.declared",
                "claim.assignment.changed",
                "task.revised",
                "allowance.granted",
                "allowance.reserved",
            },
        ),
        ("task.review.request",): (
            "task.review.requested",
            "child_contract_id",
            {
                "contract.declared",
                "task.review.requested",
                "allowance.granted",
                "allowance.reserved",
            },
        ),
    }
    if not isinstance(effects, (tuple, list)):
        raise ValueError("task declaration publisher metadata is invalid")
    shape = compound.get(tuple(effects))
    if shape is None:
        raise ValueError("task declaration publication is unsupported")
    relation_type, target_field, allowed_types = shape
    relations = tuple(
        record for record in committed if record.record_type == relation_type
    )
    target = next(
        (
            record
            for record in declarations
            if record.payload.get("contract", {}).get("id") == contract_id
        ),
        None,
    )
    try:
        from aigineering.protocol.wire import contract_from_dict

        target_contract = (
            contract_from_dict(deep_thaw(target.payload["contract"]))
            if target is not None
            else None
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("compound task declaration is invalid") from exc
    if (
        len(declarations) != 1
        or len(relations) != 1
        or target is None
        or target_contract is None
        or target_contract.parent_id != relations[0].payload.get("contract_id")
        or any(record.record_type not in allowed_types for record in committed)
        or relations[0].payload.get(target_field) != contract_id
        or relations[0].payload.get("candidate_id")
        != receipt.payload.get("candidate_id")
        or relations[0].payload.get("actor_id") != receipt.payload.get("actor_id")
        or relations[0].payload.get("key_id") != receipt.payload.get("key_id")
        or target.id not in relations[0].causal_parents
    ):
        raise ValueError("compound task declaration head is incomplete")
    source_id = relations[0].payload.get("contract_id")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("compound task declaration source is invalid")
    source_terminals = tuple(
        record
        for record in committed
        if record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == source_id
        and record.payload.get("terminal") == "cancelled"
        and record.id in relations[0].causal_parents
        and receipt.id in record.causal_parents
    )
    if effects in {("task.handoff",), ("task.revise",)} and len(source_terminals) != 1:
        raise ValueError("successor declaration lacks its exact source terminal")
    if effects == ("task.review.request",):
        targets = tuple(
            record
            for record in rows
            if record.record_type == "asset.committed"
            and record.payload.get("asset", {}).get("id")
            == relations[0].payload.get("asset_id")
        )
        if len(targets) != 1 or targets[0].id not in relations[0].causal_parents:
            raise ValueError("review declaration lacks its exact target Asset")
        request_receipt, _request_head = validate_published_member(
            rows,
            relations[0],
            domain_id=domain_id or _domain_id(rows),
            allow_additional_causal_parents=True,
        )
        if (
            tuple(request_receipt.payload.get("effect_types", ()))
            != ("task.review.request",)
            or target.id not in relations[0].causal_parents
            or declaration.id not in (relations[0].causal_parents)
        ):
            raise ValueError("review declaration publisher metadata is unsupported")
    if effects == ("task.revise",):
        decision_id = relations[0].payload.get("decision_id")
        decisions = tuple(
            record
            for record in rows
            if record.record_type == "task.review.decided"
            and record.id == decision_id
            and record.payload.get("verdict") == "changes"
            and record.payload.get("contract_id") == source_id
        )
        if len(decisions) != 1 or decisions[0].id not in relations[0].causal_parents:
            raise ValueError("revision declaration lacks its exact review decision")
        decision_receipt, _decision_head = validate_published_member(
            rows,
            decisions[0],
            domain_id=domain_id or _domain_id(rows),
            allow_additional_causal_parents=True,
        )
        if tuple(decision_receipt.payload.get("effect_types", ())) != (
            "task.review.decide",
        ):
            raise ValueError("revision review-decision publisher is unsupported")
        report_commits = tuple(
            record
            for record in rows
            if record.record_type == "asset.committed"
            and record.payload.get("asset", {}).get("id")
            == decisions[0].payload.get("report_asset_id")
        )
        if (
            decisions[0].payload.get("request_id") not in decisions[0].causal_parents
            or len(report_commits) != 1
            or report_commits[0].id not in decisions[0].causal_parents
        ):
            raise ValueError("revision decision causal witnesses are incomplete")
        requests = tuple(
            record
            for record in rows
            if record.record_type == "task.review.requested"
            and record.id == decisions[0].payload.get("request_id")
            and record.payload.get("contract_id") == source_id
        )
        if len(requests) != 1:
            raise ValueError("revision decision lacks its exact source request")
        request_receipt, _request_head = validate_published_member(
            rows,
            requests[0],
            domain_id=domain_id or _domain_id(rows),
            allow_additional_causal_parents=True,
        )
        if tuple(request_receipt.payload.get("effect_types", ())) != (
            "task.review.request",
        ):
            raise ValueError("revision source request publisher is unsupported")
    return receipt, head


def _validate_declaration_allowance_bundle(
    rows, receipt, head, committed, declarations
):
    from aigineering.core.allowance_returns import validate_allowance_grants
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


TASK_ATTACHMENT_EFFECTS = MappingProxyType(
    {
        "task.attachment.link": EffectHandler(
            capability="task.attach",
            projector=task_attachment_link_effect,
            direct_output_fact_types=frozenset({LINKED}),
            reserved_fact_types=frozenset({LINKED}),
        )
    }
)


__all__ = [
    "LINKED",
    "TASK_ATTACHMENT_EFFECTS",
    "task_attachment_link_effect",
    "validate_contract_declaration_member",
    "validate_link_history",
    "validate_published_member",
]
