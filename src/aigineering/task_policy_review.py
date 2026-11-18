"""Signed application compilers for independent review Tasks and decisions."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from pathlib import Path

from aigineering.application import require_redis
from aigineering.core.acceptance import (
    acceptance_policy_id,
    validate_asset_publication,
    validate_output_shape,
)
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.domain import load_genesis
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.output_satisfaction import (
    output_asset_meets_conditions,
)
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.projection_store_view import ProjectionStoreView
from aigineering.core.signing import Ed25519Signer
from aigineering.local_identity import (
    ensure_local_plugin_publisher,
    load_actor_signer,
)
from aigineering.plugins.task_review import (
    DECISION_RELATION,
    REQUEST_RELATION,
    parse_review_relation,
    review_relation_asset,
    task_review_asset_records,
)
from aigineering.protocol.effect_builders import (
    asset_attestation_effect,
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Asset
from aigineering.runtime_services import RedisSyncPending, sync_runtime

_REQUEST_PLUGIN = "task.review.request.operator.v2"
_REQUEST_CAPABILITIES = ("asset.publish", "contract.publish")
_DECISION_PLUGIN = "task.review.decide.operator.v2"
_DECISION_BASE_CAPABILITIES = ("asset.attest", "asset.publish", "verify.human")
_MAX_TEXT_BYTES = 64 * 1024


def _validate_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    if size > _MAX_TEXT_BYTES:
        raise ValueError(f"{field} exceeds 65536 UTF-8 bytes")


def _validate_key(idempotency_key: str | None) -> None:
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise ValueError("idempotency_key must be non-blank text")


def _committed_asset(store, asset_id: str) -> Asset:
    asset = store.get_asset(asset_id)
    if asset is None:
        raise LookupError(f"unknown Asset {asset_id!r}")
    return asset


def _policy_assets(store, policy, context) -> tuple[tuple[str, ...], tuple[str, ...]]:
    result = []
    for field in ("rubric_asset_ids", "evidence_asset_ids"):
        values = policy.get(field, ())
        if not isinstance(values, (tuple, list)) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError(f"acceptance policy {field} must contain Asset IDs")
        values = tuple(values)
        if values != tuple(sorted(set(values))):
            raise ValueError(f"acceptance policy {field} must be sorted and unique")
        for asset_id in values:
            asset = _committed_asset(store, asset_id)
            if (
                asset.tombstoned
                or not asset.promptable
                or asset.disclosure_view != "original"
            ):
                raise ValueError(
                    f"acceptance policy Asset {asset_id!r} is not disclosable"
                )
            validate_asset_publication(context, asset)
        result.append(values)
    return result[0], result[1]


def _validate_source_target(store, source_id: str, output_name: str, asset_id: str):
    source = store.get_contract(source_id)
    if source is None:
        raise LookupError(f"unknown Contract {source_id!r}")
    if output_name not in source.outputs:
        raise ValueError("output must be a declared source Contract output")
    policy = source.acceptance_policy
    if not isinstance(policy, Mapping):
        raise ValueError(
            "task review requires an independent-acceptance source Contract"
        )
    if policy.get("mode") != "independent":
        raise ValueError(
            "task review requires an independent-acceptance source Contract"
        )
    policy_version = policy.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError("independent acceptance requires a policy_version")
    required = policy.get("required_attestations", 1)
    if isinstance(required, bool) or not isinstance(required, int) or required < 1:
        raise ValueError(
            "independent acceptance required_attestations must be positive"
        )
    context = load_effect_projection_context(store)
    rubric_ids, evidence_ids = _policy_assets(store, policy, context)
    asset = _committed_asset(store, asset_id)
    if (
        asset.name != output_name
        or asset.tombstoned
        or not asset.promptable
        or asset.disclosure_view != "original"
    ):
        raise ValueError("review target must be the exact disclosable output Asset")
    if not output_asset_meets_conditions(source, output_name, asset):
        raise ValueError("review target does not match the frozen output requirements")
    validate_asset_publication(context, asset)
    validate_output_shape(policy, output_name, asset.content)
    return source, asset, policy, rubric_ids, evidence_ids


def _request_fields(
    store,
    source_contract_id: str,
    output_name: str,
    asset_id: str,
    reason: str,
    *,
    budget: int,
    idempotency_key: str | None,
):
    if not isinstance(source_contract_id, str) or not source_contract_id.strip():
        raise ValueError("source Contract ID must be non-empty text")
    if not isinstance(output_name, str) or not output_name.strip():
        raise ValueError("output name must be non-empty text")
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ValueError("Asset ID must be non-empty text")
    _validate_text(reason, "reason")
    if type(budget) is not int or budget != 1:
        raise ValueError("review request budget is fixed at one allowance unit")
    _validate_key(idempotency_key)
    source, target, policy, rubric_ids, evidence_ids = _validate_source_target(
        store, source_contract_id, output_name, asset_id
    )
    return source, target, policy, rubric_ids, evidence_ids


def validate_task_review_request(
    store,
    source_contract_id: str,
    output_name: str,
    asset_id: str,
    reason: str,
    *,
    budget: int = 1,
    idempotency_key: str | None = None,
) -> None:
    _request_fields(
        store,
        source_contract_id,
        output_name,
        asset_id,
        reason,
        budget=budget,
        idempotency_key=idempotency_key,
    )


def _stable_suffix(idempotency_key: str) -> str:
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]


def _request_publication(
    store, publisher, source, target, policy, rubric_ids, evidence_ids, reason, key
):
    suffix = _stable_suffix(key)
    context_ids = tuple(sorted({target.id, *rubric_ids, *evidence_ids}))
    context_assets = tuple(_committed_asset(store, value) for value in context_ids)
    reviewer = contract_from_fields(
        name=f"Review {source.name or source.id} {suffix}",
        description=(
            f"Independently review source Contract {source.id}, output {target.name!r}, "
            f"exact target Asset {target.id}, and frozen acceptance policy "
            f"{acceptance_policy_id(policy)} version {policy['policy_version']}. "
            f"Rubric Asset IDs: {', '.join(rubric_ids) or '(none)'}. "
            f"Evidence Asset IDs: {', '.join(evidence_ids) or '(none)'}. "
            "Report comparisons, uncertainties, and a recommendation to accept or "
            f"request changes. Request reason: {reason}"
        ),
        parent_id=source.id,
        inputs=(),
        outputs=(f"review_report_{suffix}",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=tuple(sorted({asset.name for asset in context_assets})),
        context_asset_ids=context_ids,
        worker_capabilities=(),
        worker_pools=(),
        delegation_capabilities=(),
        delegation_pools=(),
        origin="human",
        minting_authority=(),
        sensitive_input_policy=None,
        acceptance_policy=None,
    )
    projection_context = load_effect_projection_context(store)
    disclosed = compute_disclosure(reviewer, ProjectionStoreView(projection_context))
    if {asset.id for asset in disclosed} != set(context_ids):
        raise ValueError("reviewer disclosure does not match its exact frozen context")
    relation = review_relation_asset(
        REQUEST_RELATION,
        {
            "source_contract_id": source.id,
            "output_name": target.name,
            "asset_id": target.id,
            "reviewer_contract_id": reviewer.id,
            "report_output": reviewer.outputs[0],
            "policy_id": acceptance_policy_id(policy),
            "policy_version": policy["policy_version"],
            "rubric_asset_ids": list(rubric_ids),
            "evidence_asset_ids": list(evidence_ids),
            "reason": reason,
            "budget": 1,
        },
    )
    return _publish(
        store,
        publisher,
        (contract_declaration_effect(reviewer), asset_proposal_effect(relation)),
        key=key,
        parents=(source.id, target.id),
    )


def _decision_target(store, request_asset_id, report_asset_id, verdict, reason):
    request_asset = _committed_asset(store, request_asset_id)
    request = parse_review_relation(request_asset)
    if not request or request.get("relation") != REQUEST_RELATION:
        raise LookupError(f"unknown task review request Asset {request_asset_id!r}")
    verified_requests = {
        item["asset_id"]
        for item in task_review_asset_records(
            store, str(request.get("source_contract_id", ""))
        )
        if item["relation"].get("relation") == REQUEST_RELATION
    }
    if request_asset_id not in verified_requests:
        raise ValueError("review request Asset lacks exact accepted Candidate history")
    if verdict not in {"accept", "changes"}:
        raise ValueError("verdict must be 'accept' or 'changes'")
    _validate_text(reason, "reason")
    source, target, policy, rubric_ids, evidence_ids = _validate_source_target(
        store,
        str(request["source_contract_id"]),
        str(request["output_name"]),
        str(request["asset_id"]),
    )
    if (
        request.get("policy_id") != acceptance_policy_id(policy)
        or request.get("policy_version") != policy.get("policy_version")
        or tuple(request.get("rubric_asset_ids", ())) != rubric_ids
        or tuple(request.get("evidence_asset_ids", ())) != evidence_ids
        or request.get("reviewer_contract_id") is None
    ):
        raise ValueError("review request does not match the frozen source policy")
    report = _committed_asset(store, report_asset_id)
    if (
        report.tombstoned
        or not report.promptable
        or report.disclosure_view != "original"
    ):
        raise ValueError("review report Asset is not disclosable")
    reviewer = store.get_contract(str(request["reviewer_contract_id"]))
    if reviewer is None:
        raise ValueError("review request references a missing reviewer Contract")
    if (
        report.name != request.get("report_output")
        or report.name not in reviewer.outputs
    ):
        raise ValueError("report Asset must match a declared reviewer output")
    records = load_effect_projection_context(store)
    validate_asset_publication(records, report)
    if report.signed_by == target.signed_by:
        raise ValueError("review report producer must differ from target producer")
    if report.content is None:
        raise ValueError("review report content is unavailable")
    return (
        request_asset,
        request,
        source,
        target,
        policy,
        reviewer,
        report,
        rubric_ids,
        evidence_ids,
    )


def _decision_capabilities(policy_caps: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({*_DECISION_BASE_CAPABILITIES, *policy_caps}))


def _validate_custom_identity(store, actor_id, key_id, key_file, required):
    if not actor_id.strip() or not key_id.strip() or not str(key_file).strip():
        raise ValueError(
            "custom decision identity requires actor_id, key_id, and key_file"
        )
    genesis = load_genesis(store)
    signer: Ed25519Signer = load_actor_signer(Path(key_file))
    from aigineering.core.actor_facts import load_effective_actor_keys

    key = next(
        (
            item
            for item in load_effective_actor_keys(store, genesis)
            if item.actor_id == actor_id and item.key_id == key_id and not item.revoked
        ),
        None,
    )
    if key is None or key.kind != signer.kind or key.public_key != signer.signer_id:
        raise ValueError("custom decision actor/key is not currently authorized")
    missing = set(required) - set(key.capabilities)
    if missing:
        raise ValueError(
            "custom decision actor lacks required capabilities: "
            + ", ".join(sorted(missing))
        )
    return CandidatePublisher(store, store, genesis, key, signer)


def validate_task_review_decision(
    store,
    request_id: str,
    report_asset_id: str,
    verdict: str,
    reason: str,
    *,
    idempotency_key: str | None = None,
    actor_id: str | None = None,
    key_id: str | None = None,
    key_file: str | Path | None = None,
) -> None:
    supplied = (actor_id is not None, key_id is not None, key_file is not None)
    if any(supplied) and not all(supplied):
        raise ValueError(
            "custom decision identity requires actor_id, key_id, and key_file together"
        )
    _validate_key(idempotency_key)
    target = _decision_target(store, request_id, report_asset_id, verdict, reason)
    policy = target[4]
    caps = policy.get("verifier_capabilities", ())
    if not isinstance(caps, (tuple, list)) or not all(
        isinstance(cap, str) for cap in caps
    ):
        raise ValueError("acceptance policy verifier_capabilities must be strings")
    required = _decision_capabilities(tuple(caps))
    if all(supplied):
        _validate_custom_identity(store, str(actor_id), str(key_id), key_file, required)
    else:
        missing = set(required) - set(_DECISION_BASE_CAPABILITIES)
        if missing:
            raise ValueError(
                "default review decision actor cannot satisfy source policy capabilities: "
                + ", ".join(sorted(missing))
                + "; supply an already-authorized custom actor/key"
            )


def _record_view(
    store, asset: Asset, relation: dict, revision: int | None = None
) -> dict:
    if revision is None:
        revision = next(
            (
                rev
                for rev, record in store.scan_runtime_records(
                    record_type="asset.committed"
                )
                if record.payload.get("asset", {}).get("id") == asset.id
            ),
            None,
        )
    return {
        "asset_id": asset.id,
        "revision": revision,
        "relation": deep_thaw(relation),
        "signed_by": asset.signed_by,
    }


def _publish(store, publisher, effects, *, key, parents):
    sync_runtime(store)
    try:
        decision = publisher.publish(
            effects, idempotency_key=key, causal_parents=parents
        )
    except CandidateCommitRejected:
        sync_runtime(store, accepted=False)
        raise
    if not decision.accepted:
        try:
            sync_runtime(store, accepted=False)
        except RedisSyncPending as exc:
            exc.decision = decision
            raise
        rejected = next(
            (
                item
                for item in decision.runtime_records
                if item.record_type == "candidate.rejected"
            ),
            None,
        )
        raise ValueError(
            str(rejected.payload.get("reason", "Candidate was rejected"))
            if rejected
            else "Candidate was not accepted"
        )
    try:
        sync_runtime(store, accepted=True)
    except RedisSyncPending as exc:
        exc.decision = decision
        raise
    return decision


def publish_task_review_request(
    store,
    source_contract_id: str,
    output_name: str,
    asset_id: str,
    reason: str,
    *,
    budget: int = 1,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    require_redis()
    source, target, policy, rubric_ids, evidence_ids = _request_fields(
        store,
        source_contract_id,
        output_name,
        asset_id,
        reason,
        budget=budget,
        idempotency_key=idempotency_key,
    )
    key = idempotency_key or f"task-review-request:{uuid.uuid4().hex}"
    publisher = ensure_local_plugin_publisher(
        store, _REQUEST_PLUGIN, _REQUEST_CAPABILITIES
    )
    decision = _request_publication(
        store, publisher, source, target, policy, rubric_ids, evidence_ids, reason, key
    )
    asset = next(
        (item for item in decision.assets if parse_review_relation(item)), None
    )
    if asset is None:
        raise RuntimeError(
            "accepted review request Candidate produced no relation Asset"
        )
    relation = parse_review_relation(asset)
    return _record_view(store, asset, relation), store.get_runtime_revision()


def publish_task_review_decision(
    store,
    request_id: str,
    report_asset_id: str,
    verdict: str,
    reason: str,
    *,
    idempotency_key: str | None = None,
    actor_id: str | None = None,
    key_id: str | None = None,
    key_file: str | Path | None = None,
) -> tuple[dict, int]:
    require_redis()
    validate_task_review_decision(
        store,
        request_id,
        report_asset_id,
        verdict,
        reason,
        idempotency_key=idempotency_key,
        actor_id=actor_id,
        key_id=key_id,
        key_file=key_file,
    )
    (
        request_asset,
        request,
        source,
        target,
        policy,
        reviewer,
        report,
        rubric_ids,
        evidence_ids,
    ) = _decision_target(store, request_id, report_asset_id, verdict, reason)
    caps = tuple(policy.get("verifier_capabilities", ()))
    required = _decision_capabilities(caps)
    if actor_id is None:
        publisher = ensure_local_plugin_publisher(store, _DECISION_PLUGIN, required)
    else:
        publisher = _validate_custom_identity(
            store, str(actor_id), str(key_id), key_file, required
        )
    link_key = idempotency_key or f"task-review-decision:{uuid.uuid4().hex}"
    attestation = asset_attestation_effect(
        source.id,
        str(request["output_name"]),
        target.id,
        policy_id=acceptance_policy_id(policy),
        policy_version=str(policy["policy_version"]),
        verdict="accepted" if verdict == "accept" else "rejected",
        rubric_asset_ids=rubric_ids,
        evidence_asset_ids=evidence_ids,
    )
    link = review_relation_asset(
        DECISION_RELATION,
        {
            "request_asset_id": request_asset.id,
            "source_contract_id": source.id,
            "output_name": str(request["output_name"]),
            "asset_id": target.id,
            "reviewer_contract_id": reviewer.id,
            "report_asset_id": report.id,
            "verdict": verdict,
            "reason": reason,
            "policy_id": acceptance_policy_id(policy),
            "policy_version": policy["policy_version"],
        },
    )
    sync_runtime(store)
    decision = _publish(
        store,
        publisher,
        (attestation, asset_proposal_effect(link)),
        key=link_key,
        parents=(request_asset.id, report.id, target.id),
    )
    relation = next(
        (
            parse_review_relation(item)
            for item in decision.assets
            if parse_review_relation(item)
        ),
        None,
    )
    if relation is None:
        raise RuntimeError(
            "accepted review decision Candidate produced no relation Asset"
        )
    asset = next(item for item in decision.assets if parse_review_relation(item))
    return _record_view(store, asset, relation), store.get_runtime_revision()


def task_review_records(store, source_contract_id: str) -> tuple[list[dict], int]:
    """Read current Asset relations and immutable legacy review records."""
    require_redis()
    if store.get_contract(source_contract_id) is None:
        raise LookupError(f"unknown Contract {source_contract_id!r}")
    from aigineering.application import collaboration_service

    collaboration_service(store).sync()
    pairs = store.scan_runtime_records()
    asset_commit_info = {
        record.payload.get("asset", {}).get("id"): (revision, record.recorded_at)
        for revision, record in pairs
        if record.record_type == "asset.committed"
        and isinstance(record.payload.get("asset"), Mapping)
    }
    rows = task_review_asset_records(store, source_contract_id)
    data = [
        {
            "asset_id": item["asset_id"],
            "revision": item["revision"],
            "relation": deep_thaw(item["relation"]),
            "recorded_at": asset_commit_info.get(item["asset_id"], (None, None))[1],
        }
        for item in rows
    ]
    for revision, record in pairs:
        if (
            record.record_type in {"task.review.requested", "task.review.decided"}
            and record.payload.get("contract_id") == source_contract_id
        ):
            data.append(
                {
                    "record_id": record.id,
                    "record_type": record.record_type,
                    "revision": revision,
                    "recorded_at": record.recorded_at,
                    **deep_thaw(record.payload),
                }
            )
    data.sort(
        key=lambda item: (
            item.get("revision", 0),
            item.get("asset_id", item.get("record_id", "")),
        )
    )
    return data, store.get_runtime_revision()
