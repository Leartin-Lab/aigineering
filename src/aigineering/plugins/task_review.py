"""Pure request and decision projections for independent task-output review."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from aigineering.core.acceptance import (
    acceptance_policy_id,
    project_asset_attestation_records,
    validate_claim_bound_output,
)
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.disclosure import REDACTED_CONTENT, compute_disclosure
from aigineering.core.effect_projectors import (
    EffectProjection,
    project_contract_declaration,
)
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.output_satisfaction import (
    _is_contract_or_descendant,
    is_business_output,
)
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.effect_builders import asset_attestation_effect
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.types import Asset, Contract


class _ContextAssetReader:
    """Read-only exact-ID adapter used to verify reviewer disclosure."""

    def __init__(self, assets: tuple[Asset, ...]) -> None:
        self._assets = assets

    def get_asset(self, asset_id: str) -> Asset | None:
        return next((asset for asset in self._assets if asset.id == asset_id), None)

    def get_assets_by_ids(self, asset_ids: tuple[str, ...]) -> list[Asset]:
        wanted = set(asset_ids)
        return [asset for asset in self._assets if asset.id in wanted]

    def get_assets_by_name(self, name: str) -> list[Asset]:
        return [asset for asset in self._assets if asset.name == name]


class _ContextContractReader:
    """Read-only ancestry adapter over loaded contracts."""

    def __init__(self, contracts: tuple[Contract, ...]) -> None:
        self._contracts = {contract.id: contract for contract in contracts}

    def get_contract(self, contract_id: str) -> Contract | None:
        return self._contracts.get(contract_id)


def is_task_review_source_eligible(source: Contract) -> bool:
    """Return whether a business or retry Contract may request policy review."""
    return source.origin in {"human", "plan", "retry"} and not any(
        label.startswith(("plugin:", "method:")) for label in source.labels
    )


def project_task_review_request_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Create a bounded reviewer Contract and an immutable request link."""
    if effect.effect_type != "task.review.request":
        raise ValueError("review request projector requires task.review.request")
    if candidate.effects != (effect,):
        raise ValueError(
            "task.review.request Candidate must contain exactly one effect"
        )
    payload = effect.payload
    expected_fields = {
        "source_contract_id",
        "output_name",
        "asset_id",
        "reason",
        "budget",
    }
    if set(payload) != expected_fields:
        raise ValueError("task.review.request payload fields are invalid")
    source_id = _required_text(payload["source_contract_id"], "source_contract_id")
    output_name = _required_text(payload["output_name"], "output_name")
    asset_id = _required_text(payload["asset_id"], "asset_id")
    reason = _required_text(payload["reason"], "reason")
    budget = payload["budget"]
    if type(budget) is not int or budget <= 0:
        raise ValueError("task.review.request budget must be a positive integer")

    replay = _replay_projection(candidate, context, "task.review.requested")
    if replay is not None:
        link = next(
            record
            for record in replay
            if record.record_type == "task.review.requested"
            and record.payload.get("candidate_id") == candidate.id
        )
        child_id = str(link.payload["child_contract_id"])
        child = next((item for item in context.contracts if item.id == child_id), None)
        if child is None:
            raise ValueError("review request replay is missing its reviewer Contract")
        return EffectProjection(
            records=replay,
            relation_target=source_id,
            contract=child,
            additional_capabilities=("contract.publish",),
        )

    source = _contract(context.contracts, source_id)
    if not is_task_review_source_eligible(source):
        raise ValueError("task.review.request requires an ordinary business Contract")
    if source.minting_authority:
        raise ValueError("task.review.request does not support protected minting")
    if _is_terminal(source.id, context.runtime_records):
        raise ValueError("task.review.request source Contract is already terminal")
    if output_name not in source.outputs:
        raise ValueError("task.review.request output is not declared by the source")
    policy = source.acceptance_policy
    if not isinstance(policy, Mapping) or policy.get("mode") != "independent":
        raise ValueError("task.review.request requires independent acceptance policy")
    policy_version = policy.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError("independent acceptance policy requires policy_version")
    policy_id = acceptance_policy_id(policy)
    rubric_ids = _policy_ids(policy, "rubric_asset_ids")
    evidence_ids = _policy_ids(policy, "evidence_asset_ids")
    if allowance_available(source, context.runtime_records) < budget:
        raise ValueError("task.review.request budget exceeds current source allowance")

    target = next((asset for asset in context.assets if asset.id == asset_id), None)
    if target is None:
        raise ValueError("task.review.request target Asset is unknown")
    if (
        target.name != output_name
        or target.tombstoned
        or not is_business_output(target, output_name)
        or not _is_contract_or_descendant(
            target.created_by, source.id, _ContextContractReader(context.contracts)
        )
    ):
        raise ValueError("task.review.request target is not an eligible source output")
    validate_claim_bound_output(
        context, target, _contract(context.contracts, target.created_by)
    )
    target_commit = _asset_commit_record(target.id, context.runtime_records)
    if target_commit is None:
        raise ValueError("task.review.request target has no committed Asset fact")
    if _is_qualified(source.id, output_name, context.runtime_records):
        raise ValueError("task.review.request output is already qualified")

    context_ids = tuple(sorted({target.id, *rubric_ids, *evidence_ids}))
    visible_assets = _validated_context_assets(context_ids, context.assets)
    suffix = candidate.id.rsplit(":", 1)[-1]
    report_output = f"review_report_{suffix}"
    if any(
        not asset.tombstoned and is_business_output(asset, report_output)
        for asset in context.assets
    ):
        raise ValueError("task.review.request report output name is already in use")
    reviewer = contract_from_fields(
        name=f"Review {source.name or source.id} {suffix}",
        description=(
            f"Independently review source Contract {source.id}, output "
            f"{output_name!r}, exact target Asset {target.id}, and frozen "
            f"acceptance policy {policy_id} version {policy_version}. "
            f"Rubric Asset IDs: {', '.join(rubric_ids) or '(none)'}. "
            f"Evidence Asset IDs: {', '.join(evidence_ids) or '(none)'}. "
            "Compare the output with the rubric and evidence, report comparisons, "
            "uncertainties, and a recommendation to accept or request changes. "
            "Return an ordinary /exec review report. Do not attest or change the "
            f"source task. Request reason: {reason}"
        ),
        parent_id=source.id,
        inputs=(),
        outputs=(report_output,),
        activation="",
        budget=budget,
        tool_scope=(),
        labels=tuple(sorted({asset.name for asset in visible_assets})),
        context_asset_ids=context_ids,
        worker_capabilities=(),
        worker_pools=(),
        delegation_capabilities=(),
        delegation_pools=(),
        origin="human" if source.origin == "retry" else source.origin,
        minting_authority=(),
        sensitive_input_policy=None,
        acceptance_policy=None,
    )
    disclosed = compute_disclosure(reviewer, _ContextAssetReader(context.assets))
    if {asset.id for asset in disclosed} != set(context_ids):
        raise ValueError("reviewer disclosure does not match its exact frozen context")

    declaration = project_contract_declaration(
        _declaration_effect(reviewer), candidate, receipt_id, context
    )
    if declaration.contract is None:
        raise ValueError("task.review.request reviewer declaration failed")
    context_commit_ids = tuple(
        _asset_commit_record(asset_id, context.runtime_records).id
        for asset_id in context_ids
        if _asset_commit_record(asset_id, context.runtime_records) is not None
    )
    if len(context_commit_ids) != len(context_ids):
        raise ValueError("review context Asset has no immutable committed fact")
    request = create_runtime_record(
        "task.review.requested",
        {
            "actor_id": candidate.actor_id,
            "asset_id": target.id,
            "budget": budget,
            "candidate_id": candidate.id,
            "child_contract_id": reviewer.id,
            "contract_id": source.id,
            "evidence_asset_ids": evidence_ids,
            "key_id": candidate.key_id,
            "output_name": output_name,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "reason": reason,
            "report_output": report_output,
            "rubric_asset_ids": rubric_ids,
        },
        causal_parents=tuple(
            dict.fromkeys(
                (
                    receipt_id,
                    target_commit.id,
                    *context_commit_ids,
                    declaration.records[0].id,
                )
            )
        ),
    )
    return EffectProjection(
        records=(*declaration.records, request),
        relation_target=source.id,
        contract=reviewer,
        additional_capabilities=("contract.publish",),
    )


def project_task_review_decision_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Attest exactly the requested output using canonical acceptance rules."""
    if effect.effect_type != "task.review.decide":
        raise ValueError("review decision projector requires task.review.decide")
    if candidate.effects != (effect,):
        raise ValueError("task.review.decide Candidate must contain exactly one effect")
    payload = effect.payload
    if set(payload) != {"request_id", "report_asset_id", "verdict", "reason"}:
        raise ValueError("task.review.decide payload fields are invalid")
    request_id = _required_text(payload["request_id"], "request_id")
    report_asset_id = _required_text(payload["report_asset_id"], "report_asset_id")
    verdict = payload["verdict"]
    if not isinstance(verdict, str) or verdict not in {"accept", "changes"}:
        raise ValueError("task.review.decide verdict must be accept or changes")
    reason = _required_text(payload["reason"], "reason")

    replay = _replay_projection(candidate, context, "task.review.decided")
    if replay is not None:
        link = next(
            record
            for record in replay
            if record.record_type == "task.review.decided"
            and record.payload.get("candidate_id") == candidate.id
        )
        policy = _contract(
            context.contracts, str(link.payload["contract_id"])
        ).acceptance_policy
        if not isinstance(policy, Mapping):
            raise ValueError("review replay source has no acceptance policy")
        return EffectProjection(
            records=replay,
            relation_target=str(link.payload["contract_id"]),
            additional_capabilities=(
                "asset.attest",
                *_policy_ids(policy, "verifier_capabilities"),
            ),
        )

    request = next(
        (
            record
            for record in context.runtime_records
            if record.id == request_id and record.record_type == "task.review.requested"
        ),
        None,
    )
    if request is None:
        raise ValueError("task.review.decide references an unknown review request")
    if any(
        record.record_type == "task.review.decided"
        and record.payload.get("request_id") == request.id
        for record in context.runtime_records
    ):
        raise ValueError("task.review.decide request already has a decision")
    source_id = str(request.payload["contract_id"])
    reviewer_id = str(request.payload["child_contract_id"])
    source = _contract(context.contracts, source_id)
    reviewer = _contract(context.contracts, reviewer_id)
    if reviewer.parent_id != source.id:
        raise ValueError("task.review.decide reviewer is outside source ancestry")
    if _is_terminal(source.id, context.runtime_records):
        raise ValueError("task.review.decide source Contract is already terminal")
    output_name = str(request.payload["output_name"])
    target_id = str(request.payload["asset_id"])
    target = next((asset for asset in context.assets if asset.id == target_id), None)
    if target is None or _is_qualified(source.id, output_name, context.runtime_records):
        raise ValueError("task.review.decide source output is no longer eligible")
    if not is_business_output(target, output_name) or not _is_contract_or_descendant(
        target.created_by, source.id, _ContextContractReader(context.contracts)
    ):
        raise ValueError("task.review.decide target is no longer source-owned")
    validate_claim_bound_output(
        context, target, _contract(context.contracts, target.created_by)
    )

    report = next(
        (asset for asset in context.assets if asset.id == report_asset_id), None
    )
    report_name = str(request.payload["report_output"])
    if (
        report is None
        or report.name != report_name
        or report.tombstoned
        or not is_business_output(report, report_name)
        or not _is_contract_or_descendant(
            report.created_by,
            reviewer.id,
            _ContextContractReader(context.contracts),
        )
    ):
        raise ValueError("task.review.decide requires the exact reviewer report output")
    validate_claim_bound_output(
        context, report, _contract(context.contracts, report.created_by)
    )
    _validated_context_assets(
        (
            target.id,
            report.id,
            *tuple(request.payload["rubric_asset_ids"]),
            *tuple(request.payload["evidence_asset_ids"]),
        ),
        context.assets,
    )
    if not report.signed_by or report.signed_by == target.signed_by:
        raise ValueError("review report producer must differ from target producer")
    request_policy_id = str(request.payload["policy_id"])
    policy = source.acceptance_policy
    if (
        not isinstance(policy, Mapping)
        or acceptance_policy_id(policy) != request_policy_id
    ):
        raise ValueError("task.review.decide acceptance policy identity changed")
    policy_version = str(request.payload["policy_version"])
    if policy.get("policy_version") != policy_version:
        raise ValueError("task.review.decide acceptance policy version changed")
    report_commit = _asset_commit_record(report.id, context.runtime_records)
    if report_commit is None:
        raise ValueError("task.review.decide report has no committed Asset fact")

    canonical_effect = asset_attestation_effect(
        source.id,
        output_name,
        target.id,
        policy_id=request_policy_id,
        policy_version=policy_version,
        verdict="accepted" if verdict == "accept" else "rejected",
        rubric_asset_ids=tuple(request.payload["rubric_asset_ids"]),
        evidence_asset_ids=tuple(request.payload["evidence_asset_ids"]),
    )
    attestation = project_asset_attestation_records(
        canonical_effect, candidate, receipt_id, context
    )
    decision_link = create_runtime_record(
        "task.review.decided",
        {
            "actor_id": candidate.actor_id,
            "asset_id": target.id,
            "candidate_id": candidate.id,
            "child_contract_id": reviewer.id,
            "contract_id": source.id,
            "key_id": candidate.key_id,
            "output_name": output_name,
            "policy_id": request_policy_id,
            "policy_version": policy_version,
            "reason": reason,
            "report_asset_id": report.id,
            "request_id": request.id,
            "verdict": verdict,
        },
        causal_parents=tuple(
            dict.fromkeys(
                (
                    receipt_id,
                    request.id,
                    report_commit.id,
                    *(record.id for record in attestation.records),
                )
            )
        ),
    )
    return EffectProjection(
        records=(*attestation.records, decision_link),
        relation_target=source.id,
        additional_capabilities=(
            "asset.attest",
            *attestation.additional_capabilities,
        ),
    )


def project_task_reviews(
    runtime_records: Iterable[RuntimeRecord],
    *,
    source_contract_id: str | None = None,
) -> tuple[RuntimeRecord, ...]:
    """Return immutable task-review links in supplied durable order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type in {"task.review.requested", "task.review.decided"}
        and (
            source_contract_id is None
            or record.payload.get("contract_id") == source_contract_id
        )
    )


def _replay_projection(candidate, context, record_type):
    link = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == record_type
            and record.payload.get("candidate_id") == candidate.id
        ),
        None,
    )
    if link is None:
        return None
    head = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == candidate.id
        ),
        None,
    )
    if head is None:
        raise ValueError("review replay is missing its committed Candidate head")
    index = {record.id: record for record in context.runtime_records}
    committed_ids = head.payload.get("committed_record_ids")
    if not isinstance(committed_ids, (tuple, list)) or any(
        record_id not in index for record_id in committed_ids
    ):
        raise ValueError("review replay is missing committed projection facts")
    records = tuple(index[record_id] for record_id in committed_ids)
    if link not in records:
        raise ValueError("review replay link is not owned by its Candidate head")
    return records


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task review {field} must be non-empty text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"task review {field} must be valid UTF-8") from exc
    if size > 64 * 1024:
        raise ValueError(f"task review {field} exceeds 65536 UTF-8 bytes")
    return value


def _policy_ids(policy: Mapping, name: str) -> tuple[str, ...]:
    values = policy.get(name, ())
    if not isinstance(values, (tuple, list)) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ValueError(f"acceptance policy {name} must be Asset ID strings")
    if tuple(values) != tuple(sorted(set(values))):
        raise ValueError(f"acceptance policy {name} must be sorted and unique")
    return tuple(values)


def _contract(contracts: Iterable[Contract], contract_id: str) -> Contract:
    result = next((item for item in contracts if item.id == contract_id), None)
    if result is None:
        raise ValueError("task review references an unknown Contract")
    return result


def _is_terminal(contract_id: str, records: Iterable[RuntimeRecord]) -> bool:
    return any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == contract_id
        for record in records
    )


def _is_qualified(contract_id: str, output_name: str, records) -> bool:
    return any(
        record.record_type == "output.qualified"
        and record.payload.get("contract_id") == contract_id
        and record.payload.get("output_name") == output_name
        for record in records
    )


def _asset_commit_record(asset_id: str, records) -> RuntimeRecord | None:
    for record in records:
        asset_data = record.payload.get("asset")
        if (
            record.record_type == "asset.committed"
            and isinstance(asset_data, Mapping)
            and asset_data.get("id") == asset_id
        ):
            return record
    return None


def _validated_context_assets(asset_ids, assets) -> tuple[Asset, ...]:
    index = {asset.id: asset for asset in assets}
    selected = tuple(index[asset_id] for asset_id in asset_ids if asset_id in index)
    if len(selected) != len(asset_ids):
        raise ValueError("review context references a missing Asset")
    for asset in selected:
        if (
            not asset.promptable
            or asset.tombstoned
            or asset.disclosure_view != "original"
            or asset.content == REDACTED_CONTENT
            or asset.origin.lower() in {"sealed", "redacted"}
        ):
            raise ValueError("review context includes a non-disclosable Asset")
    return selected


def _declaration_effect(contract: Contract) -> CandidateEffect:
    from aigineering.protocol.effect_builders import contract_declaration_effect

    return contract_declaration_effect(contract)


TASK_REVIEW_EFFECTS = MappingProxyType(
    {
        "task.review.request": EffectHandler(
            capability="task.review.request",
            projector=project_task_review_request_effect,
            direct_output_fact_types=frozenset(
                {"contract.declared", "task.review.requested"}
            ),
            reserved_fact_types=frozenset(),
        ),
        "task.review.decide": EffectHandler(
            capability="task.review.decide",
            projector=project_task_review_decision_effect,
            direct_output_fact_types=frozenset(
                {"asset.attested", "output.qualified", "task.review.decided"}
            ),
            reserved_fact_types=frozenset(),
        ),
    }
)
