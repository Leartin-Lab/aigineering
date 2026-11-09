"""Read-only readers for ordinary Asset based task-review history."""

from __future__ import annotations

import json
from collections.abc import Mapping

from aigineering.core.acceptance import (
    acceptance_policy_id,
    validate_asset_publication,
)
from aigineering.core.output_satisfaction import output_asset_meets_conditions
from aigineering.core.publication_history import (
    index_published_records,
    validate_published_member,
)
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.protocol.identity import canonical_json
from aigineering.protocol.types import Asset

REQUEST_RELATION = "task-review-request"
DECISION_RELATION = "task-review-decision"
_RELATION_PREFIX = "task-review/"


def review_relation_asset(kind: str, values: Mapping[str, object]) -> Asset:
    """Build an ordinary descriptive Asset; qualification remains a core effect."""
    if kind not in {REQUEST_RELATION, DECISION_RELATION}:
        raise ValueError("unknown task-review relation kind")
    payload = {"relation": kind, "version": 1, **dict(values)}
    name = f"{_RELATION_PREFIX}{kind}"
    return Asset(
        id="unused-task-review-relation",
        name=name,
        content=canonical_json(payload),
        content_type="application/json",
        origin="task-review",
        trust_tier="human",
    )


def parse_review_relation(asset: Asset) -> dict | None:
    """Parse a relation descriptor without assigning it special Asset authority."""
    if not isinstance(asset, Asset) or not asset.name.startswith(_RELATION_PREFIX):
        return None
    try:
        value = json.loads(asset.content)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    try:
        if canonical_json(value) != asset.content:
            return None
    except (TypeError, ValueError):
        return None
    kind = value.get("relation")
    expected_name = f"{_RELATION_PREFIX}{kind}"
    if kind not in {REQUEST_RELATION, DECISION_RELATION} or asset.name != expected_name:
        return None
    common = {"relation", "version"}
    fields = (
        {
            "source_contract_id",
            "output_name",
            "asset_id",
            "reviewer_contract_id",
            "report_output",
            "policy_id",
            "policy_version",
            "rubric_asset_ids",
            "evidence_asset_ids",
            "reason",
            "budget",
        }
        if kind == REQUEST_RELATION
        else {
            "request_asset_id",
            "source_contract_id",
            "output_name",
            "asset_id",
            "reviewer_contract_id",
            "report_asset_id",
            "verdict",
            "reason",
            "policy_id",
            "policy_version",
        }
    )
    if set(value) != common | fields:
        return None
    text_fields = fields - {
        "rubric_asset_ids",
        "evidence_asset_ids",
        "budget",
        "verdict",
    }
    if any(
        not isinstance(value[field], str) or not value[field].strip()
        for field in text_fields
    ):
        return None
    if kind == REQUEST_RELATION:
        if type(value["budget"]) is not int or value["budget"] != 1:
            return None
        for field in ("rubric_asset_ids", "evidence_asset_ids"):
            ids = value[field]
            if not isinstance(ids, list) or not all(
                isinstance(item, str) and item for item in ids
            ):
                return None
            if ids != sorted(set(ids)):
                return None
    elif value["verdict"] not in {"accept", "changes"}:
        return None
    return value


def task_review_asset_records(store, source_contract_id: str) -> list[dict]:
    """Read new relation Assets only when their exact receipt/head proves them."""
    pairs = store.scan_runtime_records()
    records = tuple(record for _revision, record in pairs)
    publication_index = index_published_records(records)
    commit_rows = tuple(
        (revision, record)
        for revision, record in pairs
        if record.record_type == "asset.committed"
    )
    results = []
    valid_requests: dict[str, dict] = {}
    projection_context = None
    for revision, committed in commit_rows:
        asset_data = committed.payload.get("asset")
        if not isinstance(asset_data, Mapping):
            continue
        from aigineering.protocol.wire import asset_from_dict

        asset = asset_from_dict(asset_data)
        relation = parse_review_relation(asset)
        if relation is None or relation.get("source_contract_id") != source_contract_id:
            continue
        try:
            receipt, _head = validate_published_member(
                records, committed, record_index=publication_index
            )
        except ValueError:
            continue
        candidate_id = receipt.payload.get("candidate_id")
        expected_effects = (
            ("contract.declare", "asset.propose")
            if relation["relation"] == REQUEST_RELATION
            else ("asset.attest", "asset.propose")
        )
        if tuple(receipt.payload.get("effect_types", ())) != expected_effects:
            continue
        if relation["relation"] == REQUEST_RELATION:
            if not {
                source_contract_id,
                str(relation.get("asset_id", "")),
            } <= set(receipt.causal_parents):
                continue
        elif not {
            str(relation.get("request_asset_id", "")),
            str(relation.get("report_asset_id", "")),
            str(relation.get("asset_id", "")),
        } <= set(receipt.causal_parents):
            continue
        members = tuple(
            record
            for record in records
            if record.payload.get("candidate_id") == candidate_id
        )
        if relation["relation"] == REQUEST_RELATION:
            reviewer_id = relation.get("reviewer_contract_id")
            source = store.get_contract(source_contract_id)
            target = store.get_asset(str(relation.get("asset_id", "")))
            policy = source.acceptance_policy if source is not None else None
            context_ids = (
                tuple(
                    sorted(
                        {
                            target.id,
                            *relation["rubric_asset_ids"],
                            *relation["evidence_asset_ids"],
                        }
                    )
                )
                if target is not None
                else ()
            )
            context_assets = tuple(
                store.get_asset(asset_id) for asset_id in context_ids
            )
            declaration = next(
                (
                    item
                    for item in members
                    if item.record_type == "contract.declared"
                    and item.payload.get("contract", {}).get("id") == reviewer_id
                ),
                None,
            )
            if declaration is None:
                continue
            reviewer = store.get_contract(str(reviewer_id))
            if (
                source is None
                or target is None
                or reviewer is None
                or not isinstance(policy, Mapping)
                or policy.get("mode") != "independent"
                or relation.get("output_name") not in source.outputs
                or not output_asset_meets_conditions(
                    source, relation["output_name"], target
                )
                or relation.get("policy_id") != acceptance_policy_id(policy)
                or relation.get("policy_version") != policy.get("policy_version")
                or reviewer.parent_id != source.id
                or reviewer.budget != 1
                or reviewer.context_asset_ids != context_ids
                or any(asset is None for asset in context_assets)
                or reviewer.labels
                != tuple(
                    sorted(
                        {asset.name for asset in context_assets if asset is not None}
                    )
                )
                or reviewer.outputs != (relation["report_output"],)
                or reviewer.inputs
                or reviewer.activation
                or reviewer.tool_scope
                or reviewer.worker_capabilities
                or reviewer.worker_pools
                or reviewer.delegation_capabilities
                or reviewer.delegation_pools
                or reviewer.minting_authority
                or reviewer.sensitive_input_policy is not None
                or reviewer.acceptance_policy is not None
                or tuple(policy.get("rubric_asset_ids", ()))
                != tuple(relation["rubric_asset_ids"])
                or tuple(policy.get("evidence_asset_ids", ()))
                != tuple(relation["evidence_asset_ids"])
            ):
                continue
            if projection_context is None:
                projection_context = load_effect_projection_context(store)
            try:
                validate_asset_publication(projection_context, target)
            except ValueError:
                continue
            try:
                validate_published_member(
                    records,
                    declaration,
                    expected_receipt=receipt.id,
                    record_index=publication_index,
                )
            except ValueError:
                continue
            valid_requests[asset.id] = relation
        else:
            request = valid_requests.get(relation.get("request_asset_id"))
            if request is None:
                continue
            target_id = relation.get("asset_id")
            output_name = relation.get("output_name")
            report = store.get_asset(str(relation.get("report_asset_id", "")))
            target = store.get_asset(str(target_id))
            attestation = next(
                (
                    item
                    for item in members
                    if item.record_type == "asset.attested"
                    and item.payload.get("contract_id") == source_contract_id
                    and item.payload.get("asset_id") == target_id
                    and item.payload.get("output_name") == output_name
                ),
                None,
            )
            if attestation is None:
                continue
            source = store.get_contract(source_contract_id)
            policy = source.acceptance_policy if source is not None else None
            if (
                source is None
                or not isinstance(policy, Mapping)
                or attestation.payload.get("policy_id") != acceptance_policy_id(policy)
                or attestation.payload.get("policy_version")
                != policy.get("policy_version")
                or attestation.payload.get("verdict")
                != ("accepted" if relation.get("verdict") == "accept" else "rejected")
                or relation.get("policy_id") != acceptance_policy_id(policy)
                or relation.get("policy_version") != policy.get("policy_version")
                or request.get("asset_id") != relation.get("asset_id")
                or request.get("output_name") != relation.get("output_name")
                or request.get("reviewer_contract_id")
                != relation.get("reviewer_contract_id")
                or request.get("policy_id") != relation.get("policy_id")
                or request.get("policy_version") != relation.get("policy_version")
                or attestation.payload.get("rubric_asset_ids")
                != request.get("rubric_asset_ids")
                or attestation.payload.get("evidence_asset_ids")
                != request.get("evidence_asset_ids")
                or report is None
                or target is None
                or report.name != request.get("report_output")
                or report.signed_by == target.signed_by
            ):
                continue
            try:
                if projection_context is None:
                    projection_context = load_effect_projection_context(store)
                validate_asset_publication(projection_context, report)
            except ValueError:
                continue
            try:
                validate_published_member(
                    records,
                    attestation,
                    expected_receipt=receipt.id,
                    record_index=publication_index,
                )
            except ValueError:
                continue
        results.append(
            {
                "asset_id": asset.id,
                "revision": revision,
                "relation": relation,
                "signed_by": asset.signed_by,
            }
        )
    return results
