"""Pure task revision projection following an explicit review change decision."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from aigineering.core.acceptance import acceptance_policy_id
from aigineering.core.claim_assignments import (
    ASSIGNMENT_FACT,
    AssignmentAdmission,
    AssignmentIntent,
    AssignmentMode,
    AssignmentTransition,
    SuccessorAssignmentWitness,
    assignment_context_from_projection,
    assignment_head,
    build_assignment_fact,
)
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.claim_holds import open_claim_holds
from aigineering.core.claim_constraints import external_prerequisite_blockers
from aigineering.core.disclosure import REDACTED_CONTENT, compute_disclosure
from aigineering.core.effect_projectors import (
    EffectProjection,
    project_contract_cancellation,
    project_contract_declaration,
)
from aigineering.core.effect_projection import EffectHandler
from aigineering.core.output_satisfaction import (
    _is_contract_or_descendant,
    is_business_output,
)
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.plugins.task_successors import (
    ProjectionStoreView,
    build_successor_contract,
    freeze_successor_disclosure,
    has_unfinished_descendants,
    remaining_output_slots,
    satisfied_output_slots,
)
from aigineering.plugins.successor_publications import (
    validate_successor_publications,
)
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.effect_builders import contract_cancellation_effect
from aigineering.protocol.runtime_record import (
    RuntimeRecord,
    create_runtime_record,
    validate_runtime_record,
)
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import asset_to_dict


@dataclass(frozen=True)
class TaskRevisionReferences:
    """Immutable objects referenced by a signed review-change decision."""

    source: Contract
    request: RuntimeRecord
    decision: RuntimeRecord
    reviewer: Contract
    target: Asset
    report: Asset
    policy_id: str
    policy_version: str


@dataclass(frozen=True)
class TaskRevisionPublicationWitness:
    """Exact read-only witnesses for one accepted revision relation."""

    source_contract_id: str
    target_contract_id: str
    source_declaration_id: str
    source_receipt_id: str
    source_head_id: str
    revision_link_id: str
    revision_receipt_id: str
    revision_head_id: str
    cancelled_terminal_id: str
    target_declaration_id: str
    target_receipt_id: str
    target_head_id: str


@dataclass(frozen=True)
class TaskRevisionPublicationIssue:
    """Safe endpoint and record references for an unverified revision."""

    code: str
    source_contract_id: str | None = None
    target_contract_id: str | None = None
    record_id: str | None = None


def validate_task_revision_relations(
    query_context, records: tuple, publication_index
) -> tuple[
    tuple[TaskRevisionPublicationWitness, ...],
    tuple[TaskRevisionPublicationIssue, ...],
]:
    """Verify accepted revision publications without re-auditing review policy."""
    witnesses, issues = validate_successor_publications(
        query_context,
        records,
        publication_index,
        link_record_type="task.revised",
        sole_effect_type="task.revise",
        source_contract_field="contract_id",
        target_contract_field="successor_id",
    )
    return (
        tuple(
            TaskRevisionPublicationWitness(
                source_contract_id=item.source_contract_id,
                target_contract_id=item.target_contract_id,
                source_declaration_id=item.source_declaration_id,
                source_receipt_id=item.source_receipt_id,
                source_head_id=item.source_head_id,
                revision_link_id=item.link_record_id,
                revision_receipt_id=item.link_receipt_id,
                revision_head_id=item.link_head_id,
                cancelled_terminal_id=item.cancelled_terminal_id,
                target_declaration_id=item.target_declaration_id,
                target_receipt_id=item.target_receipt_id,
                target_head_id=item.target_head_id,
            )
            for item in witnesses
        ),
        tuple(
            TaskRevisionPublicationIssue(
                code="revision_publication_unverified",
                source_contract_id=item.source_contract_id,
                target_contract_id=item.target_contract_id,
                record_id=item.record_id,
            )
            for item in issues
        ),
    )


def validate_task_revision_references(
    context: EffectProjectionContext, decision_id: str
) -> TaskRevisionReferences:
    """Resolve and validate immutable request/decision/assets for revision.

    Applications may call this same pure boundary before signing a revision
    Candidate; mutable source eligibility remains the projector's job.
    """
    decision = _record_by_id(
        context.runtime_records, decision_id, "task.review.decided"
    )
    validate_runtime_record(decision)
    if decision.payload.get("verdict") != "changes":
        raise ValueError("task.revise requires a review decision requesting changes")
    _validate_committed_decision(decision, context)
    request = _record_by_id(
        context.runtime_records,
        str(decision.payload.get("request_id", "")),
        "task.review.requested",
    )
    validate_runtime_record(request)
    if any(
        decision.payload.get(key) != request.payload.get(key)
        for key in (
            "contract_id",
            "child_contract_id",
            "output_name",
            "asset_id",
            "policy_id",
            "policy_version",
        )
    ):
        raise ValueError("review decision does not match its exact request scope")
    source = _contract(context.contracts, str(request.payload.get("contract_id", "")))
    reviewer = _contract(
        context.contracts, str(request.payload.get("child_contract_id", ""))
    )
    if reviewer.parent_id != source.id:
        raise ValueError("reviewer is outside the revision source scope")
    policy = source.acceptance_policy
    if not isinstance(policy, Mapping) or policy.get("mode") != "independent":
        raise ValueError("task.revise requires the source independent policy")
    policy_id = acceptance_policy_id(policy)
    policy_version = policy.get("policy_version")
    if (
        request.payload.get("policy_id") != policy_id
        or request.payload.get("policy_version") != policy_version
        or decision.payload.get("policy_id") != policy_id
        or decision.payload.get("policy_version") != policy_version
    ):
        raise ValueError("review decision does not match the source policy")
    if request.payload.get("rubric_asset_ids") != tuple(
        policy.get("rubric_asset_ids", ())
    ) or request.payload.get("evidence_asset_ids") != tuple(
        policy.get("evidence_asset_ids", ())
    ):
        raise ValueError("review request context does not match the frozen policy")
    output_name = str(request.payload.get("output_name", ""))
    target = _committed_asset(str(request.payload.get("asset_id", "")), context)
    if target.name != output_name or output_name not in source.outputs:
        raise ValueError("review target is not a declared source output")
    view = ProjectionStoreView(context)
    if not is_business_output(target, output_name) or not _is_contract_or_descendant(
        target.created_by, source.id, view
    ):
        raise ValueError("review target is outside source output ancestry")
    report = _committed_asset(str(decision.payload.get("report_asset_id", "")), context)
    report_output = str(request.payload.get("report_output", ""))
    if (
        not report_output
        or report.name != report_output
        or not is_business_output(report, report_output)
        or not _is_contract_or_descendant(report.created_by, reviewer.id, view)
    ):
        raise ValueError("review decision report is not the exact reviewer output")
    if not report.signed_by or report.signed_by == target.signed_by:
        raise ValueError("review report producer must differ from target producer")
    report_commit = _asset_commit(report.id, context.runtime_records)
    if report_commit.id not in set(decision.causal_parents):
        raise ValueError("review decision is not causally bound to its exact report")
    _validate_review_attestation(decision, request, source, target, context)
    _require_promptable_original(target, "review target")
    _require_promptable_original(report, "review report")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError("source policy version must be non-empty text")
    return TaskRevisionReferences(
        source, request, decision, reviewer, target, report, policy_id, policy_version
    )


def project_task_revision_effect(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Cancel an unqualified source and declare its bounded revised successor."""
    if effect.effect_type != "task.revise":
        raise ValueError("revision projector requires task.revise")
    if candidate.effects != (effect,):
        raise ValueError("task.revise Candidate must contain exactly one effect")
    payload = effect.payload
    expected_fields = {"decision_id", "reason", "budget"}
    if "assignment_override" in payload:
        expected_fields.add("assignment_override")
    if set(payload) != expected_fields:
        raise ValueError("task.revise payload fields are invalid")
    decision_id = _required_text(payload["decision_id"], "decision_id")
    reason = _required_text(payload["reason"], "reason")
    requested_budget = payload["budget"]
    if requested_budget is not None and (
        type(requested_budget) is not int or requested_budget <= 0
    ):
        raise ValueError("task.revise budget must be null or a positive integer")
    assignment_override = payload.get("assignment_override")
    if assignment_override is not None:
        if not isinstance(assignment_override, Mapping) or set(assignment_override) != {
            "source_assignment_id",
            "worker_id",
        }:
            raise ValueError("task.revise assignment override fields are invalid")
        if not isinstance(
            assignment_override["source_assignment_id"], str
        ) or not isinstance(assignment_override["worker_id"], str):
            raise ValueError("task.revise assignment override values must be text")
        if assignment_override["worker_id"]:
            _required_text(assignment_override["worker_id"], "assignment Worker ID")

    replay = _replay_projection(candidate, context)
    if replay is not None:
        return replay

    references = validate_task_revision_references(context, decision_id)
    decision = references.decision
    request = references.request
    source = references.source
    target = references.target
    report = references.report
    policy_id = references.policy_id
    policy_version = references.policy_version
    dependency_blockers = external_prerequisite_blockers(
        context.runtime_records,
        cancelled_contract_ids=frozenset({source.id}),
    )
    if dependency_blockers:
        details = "; ".join(
            f"constraint {item['constraint_id']} targets Contract "
            f"{item['contract_id']} and requires Contract "
            f"{item['prerequisite_contract_id']} (observed "
            f"{item['observed_terminal']})"
            for item in dependency_blockers
        )
        raise ValueError("task.revise blocked by external prerequisites: " + details)
    if any(
        record.record_type == "task.revised"
        and record.payload.get("decision_id") == decision.id
        for record in context.runtime_records
    ):
        raise ValueError("task.review.decide already has a revision successor")
    if source.origin not in {"human", "plan"} or any(
        label.startswith(("plugin:", "method:")) for label in source.labels
    ):
        raise ValueError("task.revise requires an ordinary business Contract")
    if source.minting_authority:
        raise ValueError("task.revise does not support protected minting")
    if _is_terminal(source.id, context.runtime_records):
        raise ValueError("task.revise source Contract is already terminal")
    if has_unfinished_descendants(source, context):
        raise ValueError("task.revise source has unfinished descendants")
    if open_claim_holds(context.runtime_records, contract_id=source.id):
        raise ValueError("task.revise source has an open claim hold")

    request_id = request.id
    output_name = str(request.payload["output_name"])
    if _is_qualified(source.id, output_name, context.runtime_records):
        raise ValueError("review target output is already qualified")
    _require_promptable_original(target, "review target")
    _require_promptable_original(report, "review report")

    completed_outputs = satisfied_output_slots(source, context)
    retained_names = {name for name, _ in completed_outputs}
    remaining = remaining_output_slots(source, context)
    if output_name not in remaining:
        raise ValueError("review target slot is not an unqualified remaining output")
    if not remaining:
        raise ValueError("task.revise source has no remaining output slots")

    available = allowance_available(source, context.runtime_records)
    budget = available if requested_budget is None else requested_budget
    if budget <= 0:
        raise ValueError("task.revise has no remaining causal allowance")
    if budget > available:
        raise ValueError("task.revise budget exceeds current causal allowance")

    extra_context_ids = (target.id, report.id)
    frozen_context_ids, source_disclosure_ids, frozen_sensitive_policy = (
        freeze_successor_disclosure(source, context)
    )
    suffix = candidate.id.rsplit(":", 1)[-1]
    successor = build_successor_contract(
        source,
        remaining,
        budget,
        reason,
        name_suffix=f".revision.{suffix}",
        reason_label="Task revision reason",
        extra_context_asset_ids=extra_context_ids,
        extra_label_names=(target.name, report.name),
        frozen_context_asset_ids=frozen_context_ids,
        sensitive_input_policy=frozen_sensitive_policy,
        use_source_sensitive_policy=False,
    )
    _validate_successor_disclosure(
        successor,
        context,
        source_disclosure_ids,
        (target, report),
    )

    target_commit = _asset_commit(target.id, context.runtime_records)
    report_commit = _asset_commit(report.id, context.runtime_records)
    cancellation_effect = contract_cancellation_effect(source.id, reason)
    cancellation = project_contract_cancellation(
        cancellation_effect, candidate, receipt_id, context
    )
    declaration = project_contract_declaration(
        _declaration_effect(successor), candidate, receipt_id, context
    )
    if declaration.contract is None:
        raise ValueError("revision successor declaration failed")
    assignment_context = assignment_context_from_projection(context)
    source_assignment = assignment_head(assignment_context, contract_id=source.id)
    source_assignment_id = source_assignment.id if source_assignment else ""
    terminal = cancellation.records[0]
    assignment_mode: AssignmentMode | None = None
    assignment_worker_id = ""
    successor_assignment_record: RuntimeRecord | None = None
    if assignment_override is None:
        assignment_mode = AssignmentMode.INHERIT
        if source_assignment is not None:
            assignment_worker_id = str(source_assignment.payload.get("worker_id", ""))
            if assignment_worker_id:
                transition = AssignmentTransition(
                    target_contract_id=successor.id,
                    worker_id=assignment_worker_id,
                    previous_assignment_id="",
                    reason=reason,
                    mode=AssignmentMode.INHERIT,
                    source_contract_id=source.id,
                    source_assignment_head_id=source_assignment_id,
                )
                successor_assignment_record = build_assignment_fact(
                    assignment_context,
                    transition,
                    candidate=candidate,
                    receipt_id=receipt_id,
                    target_contract=successor,
                    target_declaration_record=declaration.records[0],
                    source_terminal_record=terminal,
                )
    else:
        assignment_mode = AssignmentMode.OVERRIDE
        if str(assignment_override["source_assignment_id"]) != source_assignment_id:
            raise ValueError("revision source assignment head changed")
        assignment_worker_id = str(assignment_override["worker_id"])
        transition = AssignmentTransition(
            target_contract_id=successor.id,
            worker_id=assignment_worker_id,
            previous_assignment_id="",
            reason=reason,
            mode=AssignmentMode.OVERRIDE,
            source_contract_id=source.id,
            source_assignment_head_id=source_assignment_id,
        )
        successor_assignment_record = build_assignment_fact(
            assignment_context,
            transition,
            candidate=candidate,
            receipt_id=receipt_id,
            target_contract=successor,
            target_declaration_record=declaration.records[0],
            source_terminal_record=terminal,
        )
    retained_asset_ids = tuple(
        sorted(
            {
                asset_id
                for _name, asset_ids in completed_outputs
                for asset_id in asset_ids
            }
        )
    )
    link_payload = {
        "actor_id": candidate.actor_id,
        "asset_id": target.id,
        "budget": budget,
        "candidate_id": candidate.id,
        "completed_outputs": tuple(sorted(retained_names)),
        "contract_id": source.id,
        "decision_id": decision.id,
        "key_id": candidate.key_id,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "reason": reason,
        "report_asset_id": report.id,
        "request_id": request_id,
        "retained_asset_ids": retained_asset_ids,
        "successor_id": successor.id,
    }
    if assignment_mode is not None and (
        source_assignment is not None or assignment_override is not None
    ):
        link_payload.update(
            {
                "assignment_mode": assignment_mode.value,
                "source_assignment_id": source_assignment_id,
                "assignment_fact_id": (
                    successor_assignment_record.id
                    if successor_assignment_record is not None
                    else ""
                ),
                "worker_id": assignment_worker_id,
            }
        )
    link_parents = tuple(
        dict.fromkeys(
            (
                receipt_id,
                decision.id,
                request.id,
                target_commit.id,
                report_commit.id,
                *(record.id for record in cancellation.records),
                *(record.id for record in declaration.records),
                *((source_assignment_id,) if source_assignment_id else ()),
                *(
                    (successor_assignment_record.id,)
                    if successor_assignment_record
                    else ()
                ),
            )
        )
    )
    link = create_runtime_record(
        "task.revised",
        link_payload,
        causal_parents=link_parents,
    )
    intents = (
        (
            AssignmentIntent(
                successor_assignment_record.id,
                successor.id,
                declaration.records[0].id,
                assignment_mode,
            ),
        )
        if successor_assignment_record is not None
        else ()
    )
    witnesses = (
        (
            SuccessorAssignmentWitness(
                source_contract_id=source.id,
                source_assignment_head_id=source_assignment_id,
                source_terminal_record_id=terminal.id,
                successor_contract_id=successor.id,
                successor_declaration_record_id=declaration.records[0].id,
                successor_assignment_record_id=(
                    successor_assignment_record.id
                    if successor_assignment_record is not None
                    else ""
                ),
                successor_link_record_id=link.id,
                assignment_mode=assignment_mode,
            ),
        )
        if assignment_mode is not None
        and (source_assignment is not None or assignment_override is not None)
        else ()
    )
    result_records = (*cancellation.records, *declaration.records)
    if successor_assignment_record is not None:
        result_records += (successor_assignment_record,)
    result_records += (link,)
    return EffectProjection(
        records=result_records,
        relation_target=source.id,
        contract=successor,
        additional_capabilities=(
            "contract.cancel",
            "contract.publish",
            *(("task.assignment.manage",) if assignment_override is not None else ()),
        ),
        assignment_intents=intents,
        successor_assignment_witnesses=witnesses,
    )


def project_task_revisions(
    runtime_records: Iterable[RuntimeRecord],
    *,
    source_contract_id: str | None = None,
) -> tuple[RuntimeRecord, ...]:
    """Return immutable task revision links in supplied durable order."""
    return tuple(
        record
        for record in runtime_records
        if record.record_type == "task.revised"
        and (
            source_contract_id is None
            or record.payload.get("contract_id") == source_contract_id
        )
    )


def _replay_projection(
    candidate: CandidateProposal, context: EffectProjectionContext
) -> EffectProjection | None:
    link = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == "task.revised"
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
        raise ValueError("revision replay has no committed Candidate head")
    index = {record.id: record for record in context.runtime_records}
    ids = head.payload.get("committed_record_ids")
    if not isinstance(ids, (tuple, list)) or any(
        record_id not in index for record_id in ids
    ):
        raise ValueError("revision replay is missing committed projection facts")
    records = tuple(index[record_id] for record_id in ids)
    if link not in records:
        raise ValueError("revision replay link is not owned by its Candidate head")
    successor = next(
        (
            contract
            for contract in context.contracts
            if contract.id == link.payload.get("successor_id")
        ),
        None,
    )
    if successor is None:
        raise ValueError("revision replay successor Contract is missing")
    intents = ()
    witnesses = ()
    assignment_fact_id = str(link.payload.get("assignment_fact_id", ""))
    assignment_mode_value = link.payload.get("assignment_mode")
    if assignment_mode_value is not None:
        try:
            mode = AssignmentMode(str(assignment_mode_value))
        except ValueError as exc:
            raise ValueError("revision replay assignment mode is invalid") from exc
        declaration = next(
            (
                record
                for record in records
                if record.record_type == "contract.declared"
                and record.payload.get("contract", {}).get("id") == successor.id
            ),
            None,
        )
        source_terminal = next(
            (
                record
                for record in records
                if record.record_type == "lifecycle.terminal"
                and record.payload.get("contract_id") == link.payload.get("contract_id")
            ),
            None,
        )
        if declaration is None or source_terminal is None:
            raise ValueError("revision replay assignment witness facts are missing")
        if assignment_fact_id:
            assignment_fact = index.get(assignment_fact_id)
            if (
                assignment_fact is None
                or assignment_fact.record_type != ASSIGNMENT_FACT
            ):
                raise ValueError("revision replay assignment fact is missing")
            intents = (
                AssignmentIntent(
                    assignment_fact.id, successor.id, declaration.id, mode
                ),
            )
        witnesses = (
            SuccessorAssignmentWitness(
                source_contract_id=str(link.payload["contract_id"]),
                source_assignment_head_id=str(
                    link.payload.get("source_assignment_id", "")
                ),
                source_terminal_record_id=source_terminal.id,
                successor_contract_id=successor.id,
                successor_declaration_record_id=declaration.id,
                successor_assignment_record_id=assignment_fact_id,
                successor_link_record_id=link.id,
                assignment_mode=mode,
            ),
        )
    return EffectProjection(
        records=records,
        relation_target=str(link.payload["contract_id"]),
        contract=successor,
        additional_capabilities=(
            "contract.cancel",
            "contract.publish",
            *(
                ("task.assignment.manage",)
                if assignment_mode_value == AssignmentMode.OVERRIDE.value
                else ()
            ),
        ),
        assignment_intents=intents,
        successor_assignment_witnesses=witnesses,
    )


def _validate_successor_disclosure(
    successor: Contract,
    context: EffectProjectionContext,
    source_ids: tuple[str, ...],
    additional_assets: tuple[Asset, ...],
) -> None:
    view = ProjectionStoreView(context)
    source_assets = compute_disclosure(
        _contract(context.contracts, str(successor.parent_id)), view
    )
    successor_assets = compute_disclosure(successor, view)
    source_by_id = {asset.id: asset for asset in source_assets}
    successor_by_id = {asset.id: asset for asset in successor_assets}
    expected_ids = set(source_ids) | {asset.id for asset in additional_assets}
    if set(successor_by_id) != expected_ids:
        raise ValueError(
            "revision successor disclosure does not match exact frozen context"
        )
    for asset_id in source_ids:
        if asset_id not in source_by_id or asset_id not in successor_by_id:
            raise ValueError("revision source disclosure snapshot is incomplete")
        if asset_to_dict(source_by_id[asset_id]) != asset_to_dict(
            successor_by_id[asset_id]
        ):
            raise ValueError("revision successor changed a source disclosure view")
    for asset in additional_assets:
        if asset_to_dict(successor_by_id[asset.id]) != asset_to_dict(asset):
            raise ValueError(
                "revision target context is not disclosed in its original view"
            )


def _require_promptable_original(asset: Asset, label: str) -> None:
    if (
        not asset.promptable
        or asset.tombstoned
        or asset.disclosure_view != "original"
        or asset.content == REDACTED_CONTENT
        or asset.origin.lower() in {"sealed", "redacted"}
    ):
        raise ValueError(f"{label} must be original, promptable, and non-tombstoned")


def _committed_asset(asset_id: str, context: EffectProjectionContext) -> Asset:
    asset = next((item for item in context.assets if item.id == asset_id), None)
    if asset is None or _asset_commit(asset_id, context.runtime_records) is None:
        raise ValueError("review decision references an unknown committed Asset")
    return asset


def _asset_commit(asset_id: str, records: tuple[RuntimeRecord, ...]) -> RuntimeRecord:
    for record in records:
        asset_data = record.payload.get("asset")
        if (
            record.record_type == "asset.committed"
            and isinstance(asset_data, Mapping)
            and asset_data.get("id") == asset_id
        ):
            return record
    raise ValueError("review target or report lacks a committed Asset fact")


def _validate_committed_decision(
    decision: RuntimeRecord, context: EffectProjectionContext
) -> None:
    """Require a decision link to belong to its signed committed Candidate."""
    candidate_id = decision.payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("review decision has no committed Candidate identity")
    head = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == candidate_id
        ),
        None,
    )
    if head is None:
        raise ValueError("review decision Candidate has no committed head")
    committed_ids = head.payload.get("committed_record_ids")
    if not isinstance(committed_ids, (tuple, list)) or decision.id not in committed_ids:
        raise ValueError("review decision is not included in its committed Candidate")
    parents = set(decision.causal_parents)
    receipt = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == candidate_id
        ),
        None,
    )
    if (
        receipt is None
        or receipt.id not in parents
        or decision.payload.get("request_id") not in parents
        or receipt.payload.get("actor_id") != decision.payload.get("actor_id")
        or receipt.payload.get("key_id") != decision.payload.get("key_id")
    ):
        raise ValueError("review decision is missing its signed causal provenance")


def _validate_review_attestation(
    decision: RuntimeRecord,
    request: RuntimeRecord,
    source: Contract,
    target: Asset,
    context: EffectProjectionContext,
) -> None:
    """Bind the rejected review decision to its canonical attestation fact."""
    decision_head = _candidate_head(decision.payload.get("candidate_id"), context)
    decision_members = decision_head.payload.get("committed_record_ids", ())
    receipt = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id")
            == decision.payload.get("candidate_id")
        ),
        None,
    )
    attestation = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == "asset.attested"
            and record.id in decision.causal_parents
            and record.id in decision_members
            and record.payload.get("contract_id") == source.id
            and record.payload.get("output_name") == request.payload.get("output_name")
            and record.payload.get("asset_id") == target.id
            and record.payload.get("policy_id") == request.payload.get("policy_id")
            and record.payload.get("policy_version")
            == request.payload.get("policy_version")
            and record.payload.get("verdict") == "rejected"
            and tuple(record.payload.get("rubric_asset_ids", ()))
            == tuple(request.payload.get("rubric_asset_ids", ()))
            and tuple(record.payload.get("evidence_asset_ids", ()))
            == tuple(request.payload.get("evidence_asset_ids", ()))
            and record.payload.get("verifier_actor_id")
            == decision.payload.get("actor_id")
            and record.payload.get("verifier_key_id") == decision.payload.get("key_id")
        ),
        None,
    )
    if (
        receipt is None
        or attestation is None
        or receipt.id not in attestation.causal_parents
    ):
        raise ValueError(
            "review decision lacks its exact canonical rejected attestation"
        )
    request_head = _candidate_head(request.payload.get("candidate_id"), context)
    request_members = request_head.payload.get("committed_record_ids", ())
    request_receipt = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id")
            == request.payload.get("candidate_id")
        ),
        None,
    )
    declaration = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == "contract.declared"
            and record.id in request_members
            and isinstance(record.payload.get("contract"), Mapping)
            and record.payload["contract"].get("id")
            == request.payload.get("child_contract_id")
        ),
        None,
    )
    target_commit = _asset_commit(target.id, context.runtime_records)
    if (
        request_receipt is None
        or request_receipt.payload.get("actor_id") != request.payload.get("actor_id")
        or request_receipt.payload.get("key_id") != request.payload.get("key_id")
        or request.id not in request_members
        or declaration is None
        or request_receipt.id not in request.causal_parents
        or declaration.id not in request.causal_parents
        or target_commit.id not in request.causal_parents
        or declaration.id not in request_members
    ):
        raise ValueError("review request lacks its signed declaration provenance")
    validate_runtime_record(attestation)


def _candidate_head(
    candidate_id: object, context: EffectProjectionContext
) -> RuntimeRecord:
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("review record has no Candidate identity")
    head = next(
        (
            record
            for record in context.runtime_records
            if record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == candidate_id
        ),
        None,
    )
    if head is None:
        raise ValueError("review record Candidate has no committed head")
    return head


def _record_by_id(
    records: tuple[RuntimeRecord, ...], record_id: str, record_type: str
) -> RuntimeRecord:
    record = next(
        (
            item
            for item in records
            if item.id == record_id and item.record_type == record_type
        ),
        None,
    )
    if record is None:
        raise ValueError(f"task.revise references an unknown {record_type} record")
    return record


def _contract(contracts: tuple[Contract, ...], contract_id: str) -> Contract:
    contract = next((item for item in contracts if item.id == contract_id), None)
    if contract is None:
        raise ValueError("task.revise references an unknown Contract")
    return contract


def _is_terminal(contract_id: str, records: tuple[RuntimeRecord, ...]) -> bool:
    return any(
        record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == contract_id
        for record in records
    )


def _is_qualified(
    contract_id: str, output_name: str, records: tuple[RuntimeRecord, ...]
) -> bool:
    return any(
        record.record_type == "output.qualified"
        and record.payload.get("contract_id") == contract_id
        and record.payload.get("output_name") == output_name
        for record in records
    )


def _declaration_effect(contract: Contract) -> CandidateEffect:
    from aigineering.protocol.effect_builders import contract_declaration_effect

    return contract_declaration_effect(contract)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task.revise {field} must be non-empty text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"task.revise {field} must be valid UTF-8") from exc
    if size > 65536:
        raise ValueError(f"task.revise {field} exceeds 65536 UTF-8 bytes")
    return value


TASK_REVISION_EFFECTS = MappingProxyType(
    {
        "task.revise": EffectHandler(
            capability="task.revise",
            projector=project_task_revision_effect,
            emitted_fact_types=frozenset({ASSIGNMENT_FACT}),
            assignment_admission=AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR,
            direct_output_fact_types=frozenset(
                {
                    "lifecycle.terminal",
                    "contract.declared",
                    ASSIGNMENT_FACT,
                    "task.revised",
                }
            ),
            reserved_fact_types=frozenset(),
        )
    }
)
REVISION_EFFECTS = TASK_REVISION_EFFECTS


__all__ = [
    "REVISION_EFFECTS",
    "TASK_REVISION_EFFECTS",
    "project_task_revision_effect",
    "project_task_revisions",
    "TaskRevisionReferences",
    "validate_task_revision_references",
]
