"""Signed application services for task-scoped independent review."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from aigineering.application import require_redis
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.acceptance import (
    acceptance_policy_id,
    validate_claim_bound_output,
    validate_output_shape,
)
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.domain import load_genesis
from aigineering.core.output_satisfaction import (
    _is_contract_or_descendant,
    is_business_output,
)
from aigineering.core.signing import Ed25519Signer
from aigineering.local_identity import (
    ensure_local_plugin_publisher,
    load_actor_signer,
)
from aigineering.plugins.task_review import (
    TASK_REVIEW_EFFECTS,
    is_task_review_source_eligible,
    project_task_reviews,
)
from aigineering.protocol.effect_builders import (
    task_review_decision_effect,
    task_review_request_effect,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.runtime_services import sync_runtime

_REQUEST_PLUGIN = "task.review.request.operator.v1"
_REQUEST_CAPABILITIES = ("contract.publish", "task.review.request")
_DECISION_PLUGIN = "task.review.decide.operator.v1"
_DECISION_CAPABILITIES = (
    "asset.attest",
    "task.review.decide",
    "verify.human",
)
_MAX_TEXT_BYTES = 64 * 1024


def _asset_has_committed_fact(store, asset_id: str) -> bool:
    return any(
        record.record_type == "asset.committed"
        and record.payload.get("asset", {}).get("id") == asset_id
        for _revision, record in store.scan_runtime_records(
            record_type="asset.committed"
        )
    )


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


def _source_asset_scope(store, source, output_name: str, asset_id: str):
    if (
        source.acceptance_policy is None
        or source.acceptance_policy.get("mode") != "independent"
    ):
        raise ValueError(
            "task review requires an independent-acceptance source Contract"
        )
    if output_name not in source.outputs:
        raise ValueError("output must be a declared source Contract output")
    if not is_task_review_source_eligible(source):
        raise ValueError("task review requires an ordinary business Contract")
    if source.minting_authority:
        raise ValueError("task review does not support protected minting")
    asset = store.get_asset(asset_id)
    if asset is None:
        raise LookupError(f"unknown Asset {asset_id!r}")
    if asset.name != output_name:
        raise ValueError("review target Asset must match the exact output slot name")
    if (
        asset.tombstoned
        or not asset.promptable
        or asset.disclosure_view != "original"
        or not is_business_output(asset, output_name)
        or not _is_contract_or_descendant(asset.created_by, source.id, store)
    ):
        raise ValueError(
            "review target must be produced for the source or its descendant"
        )
    if not _asset_has_committed_fact(store, asset.id):
        raise ValueError("review target Asset has no immutable committed fact")
    records = load_effect_projection_context(store)
    producer = next(
        (item for item in records.contracts if item.id == asset.created_by), None
    )
    if producer is None:
        raise ValueError("review target producer Contract is missing")
    validate_claim_bound_output(records, asset, producer)
    policy = source.acceptance_policy
    validate_output_shape(policy, output_name, asset.content)
    policy_version = policy.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError("independent acceptance requires a policy_version")
    attestations = policy.get("required_attestations", 1)
    if (
        isinstance(attestations, bool)
        or not isinstance(attestations, int)
        or attestations < 1
    ):
        raise ValueError(
            "independent acceptance required_attestations must be a positive integer"
        )
    for field in ("rubric_asset_ids", "evidence_asset_ids"):
        values = policy.get(field, ())
        if not isinstance(values, (tuple, list)) or not all(
            isinstance(value, str) and value for value in values
        ):
            raise ValueError(f"acceptance policy {field} must contain Asset IDs")
        if tuple(values) != tuple(sorted(set(values))):
            raise ValueError(f"acceptance policy {field} must be sorted and unique")
        for value in values:
            context_asset = store.get_asset(value)
            if context_asset is None:
                raise LookupError(f"unknown acceptance policy Asset {value!r}")
            if (
                context_asset.tombstoned
                or not context_asset.promptable
                or context_asset.disclosure_view != "original"
            ):
                raise ValueError(
                    f"acceptance policy Asset {value!r} is not disclosable"
                )
            if not _asset_has_committed_fact(store, value):
                raise ValueError(
                    f"acceptance policy Asset {value!r} has no committed fact"
                )
    capabilities = policy.get("verifier_capabilities", ())
    if not isinstance(capabilities, (tuple, list)) or not all(
        isinstance(value, str) and value for value in capabilities
    ):
        raise ValueError("acceptance policy verifier_capabilities must be strings")
    return asset, policy, tuple(capabilities)


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
    if not isinstance(source_contract_id, str) or not source_contract_id.strip():
        raise ValueError("source Contract ID must be non-empty text")
    if not isinstance(output_name, str) or not output_name.strip():
        raise ValueError("output name must be non-empty text")
    if not isinstance(asset_id, str) or not asset_id.strip():
        raise ValueError("Asset ID must be non-empty text")
    _validate_text(reason, "reason")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError("review budget must be a positive integer")
    _validate_key(idempotency_key)
    source = store.get_contract(source_contract_id)
    if source is None:
        raise LookupError(f"unknown Contract {source_contract_id!r}")
    _source_asset_scope(store, source, output_name, asset_id)


def _request_payload(store, request_id: str) -> tuple[object, object, object]:
    record = store.get_runtime_record(request_id)
    if record is None or record.record_type != "task.review.requested":
        raise LookupError(f"unknown task review request {request_id!r}")
    payload = record.payload
    source = store.get_contract(str(payload.get("contract_id", "")))
    child = store.get_contract(str(payload.get("child_contract_id", "")))
    if source is None or child is None:
        raise RuntimeError(
            "review request references a missing source or reviewer Contract"
        )
    return record, source, child


def _validate_decision_target(
    store,
    request_id: str,
    report_asset_id: str,
    verdict: str,
    reason: str,
) -> tuple[object, object, object, object, tuple[str, ...]]:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("review request ID must be non-empty text")
    if not isinstance(report_asset_id, str) or not report_asset_id.strip():
        raise ValueError("report Asset ID must be non-empty text")
    if verdict not in {"accept", "changes"}:
        raise ValueError("verdict must be 'accept' or 'changes'")
    _validate_text(reason, "reason")
    request, source, child = _request_payload(store, request_id)
    payload = request.payload
    asset_id = str(payload.get("asset_id", ""))
    output_name = str(payload.get("output_name", ""))
    target = store.get_asset(asset_id)
    report = store.get_asset(report_asset_id)
    if target is None or report is None:
        raise LookupError("review request target or report Asset does not exist")
    if (
        target.name != output_name
        or target.tombstoned
        or not target.promptable
        or target.disclosure_view != "original"
        or not is_business_output(target, output_name)
        or not _is_contract_or_descendant(target.created_by, source.id, store)
    ):
        raise ValueError("review request no longer identifies its exact source output")
    report_output = str(payload.get("report_output", ""))
    if (
        report.name != report_output
        or report.tombstoned
        or not report.promptable
        or report.disclosure_view != "original"
        or not is_business_output(report, report_output)
        or not _is_contract_or_descendant(report.created_by, child.id, store)
    ):
        raise ValueError(
            "report Asset must be the exact declared output of the reviewer Contract"
        )
    if report.signed_by == target.signed_by:
        raise ValueError(
            "review report must be produced by an actor distinct from the source output producer"
        )
    if not _asset_has_committed_fact(store, target.id) or not _asset_has_committed_fact(
        store, report.id
    ):
        raise ValueError(
            "review source or report Asset has no immutable committed fact"
        )
    records = load_effect_projection_context(store)
    target_producer = next(
        (item for item in records.contracts if item.id == target.created_by), None
    )
    report_producer = next(
        (item for item in records.contracts if item.id == report.created_by), None
    )
    if target_producer is None or report_producer is None:
        raise ValueError("review source or report producer Contract is missing")
    validate_claim_bound_output(records, target, target_producer)
    validate_claim_bound_output(records, report, report_producer)
    if child.acceptance_policy is not None:
        raise ValueError("reviewer Contract must remain an ordinary report task")
    policy = source.acceptance_policy
    if policy is None or policy.get("mode") != "independent":
        raise ValueError("review source no longer has independent acceptance")
    if str(payload.get("policy_id", "")) != acceptance_policy_id(policy):
        raise ValueError("review request policy identity does not match the source")
    if str(payload.get("policy_version", "")) != str(policy.get("policy_version", "")):
        raise ValueError("review request policy version does not match the source")
    caps = policy.get("verifier_capabilities", ())
    if not isinstance(caps, (tuple, list)) or not all(
        isinstance(cap, str) for cap in caps
    ):
        raise ValueError("acceptance policy verifier_capabilities must be strings")
    return request, source, child, report, tuple(caps)


def _decision_capabilities(policy_caps: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({"task.review.decide", "asset.attest", *policy_caps}))


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
    """Validate exact immutable targets and signer authority without writes."""
    supplied = (actor_id is not None, key_id is not None, key_file is not None)
    if any(supplied) and not all(supplied):
        raise ValueError(
            "custom decision identity requires actor_id, key_id, and key_file together"
        )
    _validate_key(idempotency_key)
    _request, _source, _child, _report, policy_caps = _validate_decision_target(
        store, request_id, report_asset_id, verdict, reason
    )
    required = _decision_capabilities(policy_caps)
    if not any(supplied):
        unsupported = set(required) - set(_DECISION_CAPABILITIES)
        if unsupported:
            raise ValueError(
                "default review decision actor cannot satisfy source policy capabilities: "
                + ", ".join(sorted(unsupported))
                + "; supply an already-authorized custom actor/key"
            )
        return
    _custom_decision_publisher(
        store, str(actor_id), str(key_id), str(key_file), required
    )


def _publisher_with_effects(store, plugin_id: str, capabilities: tuple[str, ...]):
    publisher = ensure_local_plugin_publisher(store, plugin_id, capabilities)
    return replace(publisher, additional_effects=TASK_REVIEW_EFFECTS)


def _custom_decision_publisher(
    store,
    actor_id: str,
    key_id: str,
    key_file: str | Path,
    required_capabilities: tuple[str, ...],
):
    if not actor_id.strip() or not key_id.strip():
        raise ValueError("custom decision actor ID and key ID must be non-empty")
    if not str(key_file).strip():
        raise ValueError("custom decision key file must be supplied")
    genesis = load_genesis(store)
    signer: Ed25519Signer = load_actor_signer(Path(key_file))
    key = next(
        (
            item
            for item in load_effective_actor_keys(store, genesis)
            if item.actor_id == actor_id and item.key_id == key_id and not item.revoked
        ),
        None,
    )
    if key is None:
        raise ValueError("custom decision actor/key is not authorized in this domain")
    if key.kind != signer.kind or key.public_key != signer.signer_id:
        raise ValueError(
            "custom decision key file does not match the authorized actor key"
        )
    missing = set(required_capabilities) - set(key.capabilities)
    if missing:
        raise ValueError(
            "custom decision actor lacks required capabilities: "
            + ", ".join(sorted(missing))
        )
    return replace(
        CandidatePublisher(store, store, genesis, key, signer),
        additional_effects=TASK_REVIEW_EFFECTS,
    )


def _record_view(
    store, proposed, record_type: str, revision: int | None = None
) -> dict:
    if proposed is None:
        raise RuntimeError(f"accepted Candidate produced no {record_type} fact")
    durable = store.get_runtime_record(proposed.id)
    if durable is None or durable.record_type != record_type:
        raise RuntimeError(
            f"accepted {record_type} fact is missing from the durable Store"
        )
    if revision is None:
        revision = next(
            (
                rev
                for rev, record in store.scan_runtime_records(record_type=record_type)
                if record.id == durable.id
            ),
            None,
        )
    if revision is None:
        raise RuntimeError(f"durable {record_type} fact has no Store revision")
    return {
        "record_id": durable.id,
        "record_type": durable.record_type,
        "revision": revision,
        "recorded_at": durable.recorded_at,
        **deep_thaw(durable.payload),
    }


def _publish(store, publisher, effect, *, key: str, parents: tuple[str, ...]):
    # The authorizer and any previous facts must be relayed before provisioning
    # or publication. That keeps a Redis outage from creating a local-only key.
    sync_runtime(store)
    try:
        decision = publisher.publish(
            (effect,), idempotency_key=key, causal_parents=parents
        )
    except CandidateCommitRejected:
        sync_runtime(store, accepted=False)
        raise
    if not decision.accepted:
        sync_runtime(store, accepted=False)
        rejected = next(
            (
                record
                for record in decision.runtime_records
                if record.record_type == "candidate.rejected"
            ),
            None,
        )
        raise ValueError(
            str(rejected.payload.get("reason", "Candidate was rejected"))
            if rejected is not None
            else "Candidate was not accepted"
        )
    sync_runtime(store, accepted=True)
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
    validate_task_review_request(
        store,
        source_contract_id,
        output_name,
        asset_id,
        reason,
        budget=budget,
        idempotency_key=idempotency_key,
    )
    sync_runtime(store)
    publisher = _publisher_with_effects(store, _REQUEST_PLUGIN, _REQUEST_CAPABILITIES)
    effect = task_review_request_effect(
        source_contract_id, output_name, asset_id, reason, budget=budget
    )
    decision = _publish(
        store,
        publisher,
        effect,
        key=idempotency_key or f"task-review-request:{uuid.uuid4().hex}",
        parents=(source_contract_id, asset_id),
    )
    proposed = next(
        (
            record
            for record in decision.runtime_records
            if record.record_type == "task.review.requested"
            and record.payload.get("contract_id") == source_contract_id
            and record.payload.get("asset_id") == asset_id
        ),
        None,
    )
    view = _record_view(store, proposed, "task.review.requested")
    return view, store.get_runtime_revision()


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
    if (actor_id, key_id, key_file) not in ((None, None, None),) and not all(
        (actor_id, key_id, key_file)
    ):
        raise ValueError(
            "custom decision identity requires --actor-id, --key-id, and --key-file together"
        )
    _validate_key(idempotency_key)
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
    request, _source, _child, _report, policy_caps = _validate_decision_target(
        store, request_id, report_asset_id, verdict, reason
    )
    required = _decision_capabilities(policy_caps)
    custom = actor_id is not None
    sync_runtime(store)
    # Validate an explicitly selected key before any actor provisioning or write.
    if custom:
        publisher = _custom_decision_publisher(
            store, str(actor_id), str(key_id), str(key_file), required
        )
    else:
        publisher = _publisher_with_effects(
            store, _DECISION_PLUGIN, _DECISION_CAPABILITIES
        )
    effect = task_review_decision_effect(
        request_id,
        report_asset_id,
        verdict,
        reason,
    )
    decision = _publish(
        store,
        publisher,
        effect,
        key=idempotency_key or f"task-review-decision:{uuid.uuid4().hex}",
        parents=(request_id, report_asset_id),
    )
    proposed = next(
        (
            record
            for record in decision.runtime_records
            if record.record_type == "task.review.decided"
            and record.payload.get("request_id") == request_id
            and record.payload.get("report_asset_id") == report_asset_id
        ),
        None,
    )
    view = _record_view(store, proposed, "task.review.decided")
    return view, store.get_runtime_revision()


def task_review_records(store, source_contract_id: str) -> tuple[list[dict], int]:
    """Return immutable review request and decision links for one source task."""
    require_redis()
    if store.get_contract(source_contract_id) is None:
        raise LookupError(f"unknown Contract {source_contract_id!r}")
    from aigineering.application import collaboration_service

    collaboration_service(store).sync()
    pairs = store.scan_runtime_records()
    projected_ids = {
        record.id
        for record in project_task_reviews(
            (record for _revision, record in pairs),
            source_contract_id=source_contract_id,
        )
    }
    data = [
        {
            "record_id": record.id,
            "record_type": record.record_type,
            "revision": revision,
            "recorded_at": record.recorded_at,
            **deep_thaw(record.payload),
        }
        for revision, record in pairs
        if record.id in projected_ids
    ]
    return data, store.get_runtime_revision()
