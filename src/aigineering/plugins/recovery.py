"""Recovery task projection shared by runtime and completion plugins."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aigineering.core.ids import contract_from_fields
from aigineering.plugins.task_semantics import system_asset
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Contract

if TYPE_CHECKING:
    from aigineering.plugins.completion_projection import TaskCompletionContext
    from aigineering.plugins.recovery_history import RecoveryPublicationProof
    from aigineering.protocol.candidate import CandidateEffect
    from aigineering.protocol.types import Asset


@dataclass(frozen=True)
class RecoveryDisposition:
    contract: Contract | None
    reason: str = ""
    recovery_contract_id: str | None = None


@dataclass(frozen=True)
class MethodRecoveryIntent:
    recovery: Contract
    context_asset: Asset
    legacy_recovery: Contract
    legacy_context: Asset
    source_record_ids: tuple[str, ...]
    idempotency_key: str
    causal_parents: tuple[str, ...]


def _history_status_disposition(legacy_status: str, current_status: str) -> str:
    """Resolve dual history proofs with conflicts taking precedence."""
    if "conflict" in {legacy_status, current_status}:
        return "recovery_history_conflict"
    if legacy_status == current_status == "matched":
        return "recovery_history_ambiguous"
    if legacy_status == "matched":
        return "legacy_method_intent_missing"
    return ""


def schedule_method_result_recovery(
    runtime: TaskCompletionContext,
    *,
    method_type: str,
    parent_id: str,
    failed_contract: Contract,
    result_asset: Asset,
    rejections: list[dict],
) -> Contract | None:
    """Create a normal Worker task that repairs malformed method output."""

    if _recovery_depth(failed_contract.name) >= 3:
        return None

    output_name = _method_result_output_name(method_type, parent_id)
    context_name = f"_fail_context_{failed_contract.id}"
    context = {
        "trigger": "method_result_rejected",
        "method": method_type,
        "parent_contract_id": parent_id,
        "failed_contract_id": failed_contract.id,
        "bad_asset_name": result_asset.name,
        "bad_asset_content": result_asset.content[:4000],
        "rejections": rejections,
        "required_output": output_name,
        "expected_format": _expected_format(method_type),
    }
    name = f"{failed_contract.name}.recover"
    description = json.dumps(
        {
            "method": method_type,
            "recovery": "method_result_repair",
            "parent_contract_id": parent_id,
            "failed_contract_id": failed_contract.id,
            "instructions": (
                f"Read {context_name}. The previous {method_type} result was "
                f"rejected. Return exactly /exec with output {output_name}, "
                "using the expected JSON schema in the failure context. Do not "
                "produce the parent business output directly."
            ),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    authority = (output_name, context_name)
    policy = (
        dict(failed_contract.sensitive_input_policy)
        if failed_contract.sensitive_input_policy is not None
        else None
    )
    acceptance_policy = (
        dict(failed_contract.acceptance_policy)
        if failed_contract.acceptance_policy is not None
        else None
    )
    recovery = contract_from_fields(
        name=name,
        description=description,
        inputs=[context_name],
        outputs=[output_name],
        activation=context_name,
        budget=1,
        tool_scope=[],
        labels=[f"method:{method_type}"],
        worker_capabilities=failed_contract.worker_capabilities,
        worker_pools=failed_contract.worker_pools,
        delegation_capabilities=failed_contract.delegation_capabilities,
        delegation_pools=failed_contract.delegation_pools,
        origin="system",
        parent_id=parent_id,
        minting_authority=authority,
        sensitive_input_policy=policy,
        acceptance_policy=acceptance_policy,
        context_asset_ids=failed_contract.context_asset_ids,
    )
    context_template = system_asset(
        name=context_name,
        content=json.dumps(context, sort_keys=True, ensure_ascii=False),
        created_by=failed_contract.id,
        promptable=True,
    )
    published = _publish_recovery(
        runtime,
        recovery=recovery,
        context_template=context_template,
        idempotency_key=f"method-recovery:{failed_contract.id}:{result_asset.id}",
        causal_parents=(result_asset.id,),
        rejection_contract_id=parent_id,
        relation_type=method_type,
    )
    if published is None:
        return None
    recovery, context_asset = published
    runtime.append_trace(
        parent_id,
        "method_recovery_scheduled",
        relation_type=method_type,
        relation_target=recovery.id,
        disclosed_assets=[context_asset.id],
        rejected_fragments=[
            f"[recovery] {entry.get('field', '?')}: {entry.get('reason', '')}"
            for entry in rejections
        ],
        authority_result="recovery_scheduled",
        budget_remaining=runtime.resolve_budget(parent_id),
    )
    return recovery


def schedule_projection_recovery(
    runtime: TaskCompletionContext,
    *,
    failed_contract: Contract,
    candidate_raw: str,
    rejections: list[dict],
    source_binding: dict | None = None,
    ancestor_source_bindings: dict[str, dict] | None = None,
    recovery_snapshot=None,
    include_disposition: bool = False,
) -> Contract | RecoveryDisposition | None:
    """Create a normal recovery task for rejected Worker output."""

    if _recovery_depth(failed_contract.name) >= 3:
        result = RecoveryDisposition(None, "recovery_depth_exceeded")
        return result if include_disposition else None

    source_method, source_error = _recoverable_tool_method(failed_contract)
    if source_error:
        result = RecoveryDisposition(None, source_error)
        return result if include_disposition else None
    if source_method is not None:
        if source_binding is None:
            result = RecoveryDisposition(None, "source_proof_missing")
            return result if include_disposition else None
        from aigineering.plugins.recovery_history import RecoveryHistoryCaptureUnstable

        try:
            snapshot = recovery_snapshot or runtime.capture_recovery_history()
        except RecoveryHistoryCaptureUnstable:
            result = RecoveryDisposition(None, "recovery_history_conflict")
            return result if include_disposition else None
        if snapshot is None:
            result = RecoveryDisposition(None, "recovery_history_unavailable")
            return result if include_disposition else None
        prepared = prepare_method_recovery_intent(
            snapshot,
            failed_contract=failed_contract,
            source_binding=source_binding,
            ancestor_source_bindings=ancestor_source_bindings,
            candidate_raw=candidate_raw,
            rejections=rejections,
        )
        if isinstance(prepared, RecoveryDisposition):
            return prepared if include_disposition else None
        if prepared is None:
            result = RecoveryDisposition(None, "unsupported_method_intent")
            return result if include_disposition else None
        result = _schedule_tool_method_recovery(
            runtime, intent=prepared, recovery_snapshot=snapshot
        )
        return result if include_disposition else result.contract

    recovery, context_template = _legacy_projection_intent(
        failed_contract, candidate_raw, rejections
    )
    published = _publish_recovery(
        runtime,
        recovery=recovery,
        context_template=context_template,
        idempotency_key=f"projection-recovery:{failed_contract.id}",
        causal_parents=(failed_contract.id,),
        rejection_contract_id=failed_contract.id,
        relation_type="projection",
    )
    if published is None:
        return None
    recovery, context_asset = published
    runtime.append_trace(
        failed_contract.id,
        "recovery_scheduled",
        relation_type="projection",
        relation_target=recovery.id,
        disclosed_assets=[context_asset.id],
        rejected_fragments=[
            f"[recovery] {entry.get('category', '?')} "
            f"{entry.get('name', '?')}: {entry.get('reject_reason', '')}"
            for entry in rejections
        ],
        authority_result="recovery_scheduled",
        budget_remaining=runtime.resolve_budget(failed_contract.id),
    )
    disposition = RecoveryDisposition(recovery)
    return disposition if include_disposition else recovery


def _recoverable_tool_method(contract: Contract) -> tuple[dict | None, str]:
    """Classify method recovery metadata without treating markers as authority."""
    payload = _method_payload(contract)
    method = payload.get("method")
    if method not in {"tool", "parallel_tool_item"}:
        return None, ""
    if contract.origin != "system":
        return None, "unsupported_method_intent"
    version = payload.get("recovery_intent_version")
    source_id = payload.get("recovery_source_contract_id")
    if version is None:
        if source_id is not None:
            return None, "unsupported_method_intent"
    elif type(version) is not int or version != 1:
        return None, "unsupported_method_intent"
    elif not isinstance(source_id, str) or not source_id:
        return None, "unsupported_method_intent"
    allowed = {
        "method",
        "parent_contract_id",
        "parent_contract_name",
        "parent_inputs",
        "parent_outputs",
        "parent_tool_scope",
        "payload",
    }
    if method == "parallel_tool_item":
        allowed.update({"parallel_call_id", "parallel_batch"})
    if version == 1:
        allowed.update({"recovery_intent_version", "recovery_source_contract_id"})
    if set(payload) != allowed:
        return None, "unsupported_method_intent"
    tool = payload.get("payload")
    if (
        not isinstance(payload.get("parent_contract_id"), str)
        or payload.get("parent_contract_id") != contract.parent_id
        or not isinstance(tool, dict)
        or not isinstance(tool.get("name"), str)
        or not tool["name"]
        or not isinstance(tool.get("args"), dict)
    ):
        return None, "unsupported_method_intent"
    if method == "parallel_tool_item" and (
        not isinstance(payload.get("parallel_call_id"), str)
        or not payload["parallel_call_id"]
        or not isinstance(payload.get("parallel_batch"), str)
        or not payload["parallel_batch"]
    ):
        return None, "unsupported_method_intent"
    return payload, ""


def prepare_method_recovery_intent(
    snapshot,
    *,
    failed_contract: Contract,
    source_binding: dict,
    candidate_raw: str = "",
    rejections: list[dict] | None = None,
    ancestor_source_bindings: dict[str, dict] | None = None,
    _proof_depth: int = 0,
) -> MethodRecoveryIntent | RecoveryDisposition | None:
    """Prepare the one canonical current and legacy intent pair from a snapshot."""
    source_method, unsupported_reason = _recoverable_tool_method(failed_contract)
    if unsupported_reason:
        return RecoveryDisposition(None, unsupported_reason)
    if source_method is None:
        return None
    if _proof_depth >= 3:
        return RecoveryDisposition(None, "recovery_depth_exceeded")
    if (
        source_binding.get("contract_id") != failed_contract.id
        or source_binding.get("kind")
        not in {
            "worker.invocation_failed",
            "claim.expired",
            "candidate.rejected",
            "projection.decided",
        }
        or not isinstance(source_binding.get("grant_record_id"), str)
        or not isinstance(source_binding.get("witness_record_ids"), (tuple, list))
    ):
        return RecoveryDisposition(None, "source_proof_missing")
    original_source = failed_contract
    original_method = source_method
    if source_method.get("recovery_intent_version") == 1:
        source_id = source_method["recovery_source_contract_id"]
        source_contract = next(
            (
                contract
                for contract in snapshot.context.contracts
                if contract.id == source_id
            ),
            None,
        )
        source_binding_for_predecessor = (ancestor_source_bindings or {}).get(source_id)
        if source_contract is None or source_binding_for_predecessor is None:
            return RecoveryDisposition(None, "method_source_history_unverified")
        prior = prepare_method_recovery_intent(
            snapshot,
            failed_contract=source_contract,
            source_binding=source_binding_for_predecessor,
            ancestor_source_bindings=ancestor_source_bindings,
            _proof_depth=_proof_depth + 1,
        )
        if not isinstance(prior, MethodRecoveryIntent):
            return RecoveryDisposition(None, "method_source_history_unverified")
        from aigineering.plugins.recovery_history import prove_recovery_publication

        prior_legacy = prove_recovery_publication(
            snapshot,
            recovery=prior.legacy_recovery,
            context_template=prior.legacy_context,
            effects=_recovery_effects(prior.legacy_recovery, prior.legacy_context),
            idempotency_key=prior.idempotency_key,
            causal_parents=prior.causal_parents,
            source_record_ids=prior.source_record_ids,
        )
        prior_current = prove_recovery_publication(
            snapshot,
            recovery=prior.recovery,
            context_template=prior.context_asset,
            effects=_recovery_effects(prior.recovery, prior.context_asset),
            idempotency_key=prior.idempotency_key,
            causal_parents=prior.causal_parents,
            source_record_ids=prior.source_record_ids,
        )
        if (
            prior_legacy.status != "absent"
            or prior_current.status != "matched"
            or prior_current.contract != failed_contract
        ):
            return RecoveryDisposition(None, "method_source_history_unverified")
        original_source = source_contract
        original_method, source_error = _recoverable_tool_method(source_contract)
        if source_error or original_method is None:
            return RecoveryDisposition(None, "method_source_history_unverified")
    source_parent = next(
        (
            contract
            for contract in snapshot.context.contracts
            if contract.id == original_source.parent_id
        ),
        None,
    )
    if source_parent is None:
        return RecoveryDisposition(None, "method_parent_missing")
    canonical_error = (
        ""
        if source_method.get("recovery_intent_version") == 1
        else _canonical_method_source(
            snapshot, original_source, source_parent, original_method
        )
    )
    if canonical_error:
        return RecoveryDisposition(None, canonical_error)
    payload = dict(source_method)
    payload["recovery_intent_version"] = 1
    payload["recovery_source_contract_id"] = failed_contract.id
    recovery = contract_from_fields(
        name=f"{failed_contract.name}.recover",
        description=json.dumps(payload, sort_keys=True, ensure_ascii=False),
        inputs=failed_contract.inputs,
        outputs=failed_contract.outputs,
        activation=failed_contract.activation,
        budget=max(1, failed_contract.budget),
        tool_scope=failed_contract.tool_scope,
        labels=failed_contract.labels,
        worker_capabilities=failed_contract.worker_capabilities,
        worker_pools=failed_contract.worker_pools,
        delegation_capabilities=failed_contract.delegation_capabilities,
        delegation_pools=failed_contract.delegation_pools,
        origin="system",
        parent_id=failed_contract.parent_id,
        minting_authority=tuple(
            output
            for output in failed_contract.outputs
            if output in failed_contract.minting_authority
        ),
        sensitive_input_policy=(
            dict(failed_contract.sensitive_input_policy)
            if failed_contract.sensitive_input_policy is not None
            else None
        ),
        acceptance_policy=(
            dict(failed_contract.acceptance_policy)
            if failed_contract.acceptance_policy is not None
            else None
        ),
        context_asset_ids=failed_contract.context_asset_ids,
    )
    legacy_recovery, legacy_context = _legacy_projection_intent(
        failed_contract,
        candidate_raw,
        rejections or [],
        bind_failure_context=False,
    )
    context_name = f"_fail_context_{failed_contract.id}.recovery"
    stable_binding = {
        key: source_binding[key]
        for key in (
            "kind",
            "contract_id",
            "claim_id",
            "grant_record_id",
            "failure_record_id",
            "release_record_id",
            "source_record_id",
            "witness_record_ids",
        )
        if key in source_binding
    }
    context_asset = system_asset(
        name=context_name,
        content=json.dumps(
            {
                "trigger": "verified_task_failure",
                "failed_contract_id": failed_contract.id,
                "source_binding": stable_binding,
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
        created_by=failed_contract.id,
        promptable=True,
    )
    return MethodRecoveryIntent(
        recovery=recovery,
        context_asset=context_asset,
        legacy_recovery=legacy_recovery,
        legacy_context=legacy_context,
        source_record_ids=tuple(source_binding["witness_record_ids"]),
        idempotency_key=f"projection-recovery:{failed_contract.id}",
        causal_parents=(failed_contract.id,),
    )


def _canonical_method_source(
    snapshot, source: Contract, parent: Contract, payload: dict
) -> str:
    """Match original method source to its existing pure method/delegation builders."""
    from aigineering.core.publication_history import (
        index_published_records,
        read_effect_types,
        validate_published_member,
    )
    from aigineering.plugins.delegation import (
        _claimed_method_contract,
        _parallel_tool_contracts,
    )
    from aigineering.plugins.task_semantics import method_contract
    from aigineering.protocol.actions import WorkerAction

    rows = snapshot.context.runtime_records
    declaration_rows = tuple(
        row
        for row in rows
        if row.record_type == "contract.declared"
        and isinstance(row.payload.get("contract"), Mapping)
        and row.payload["contract"].get("id") == source.id
    )
    if len(declaration_rows) != 1:
        return "method_source_declaration_unavailable"
    try:
        source_receipt, _source_head = validate_published_member(
            rows,
            declaration_rows[0],
            expected_candidate=declaration_rows[0].payload.get("candidate_id"),
            domain_id=snapshot.store_domain_id,
            record_index=index_published_records(rows),
        )
    except (TypeError, ValueError):
        return "method_source_publication_unverified"
    action = WorkerAction(type="tool", payload=payload["payload"])
    if payload["method"] == "tool":
        try:
            source_effect_types = read_effect_types(source_receipt.payload)
        except ValueError:
            return "method_source_publication_unverified"
        if source_effect_types not in {
            ("contract.declare",),
            ("contract.declare", "asset.propose"),
        }:
            return "method_source_noncanonical"
        expected = [method_contract(parent, action)]
        expected.append(_claimed_method_contract(parent, action))
        return "" if source in expected else "method_source_noncanonical"
    source_row = declaration_rows[0]
    candidate_id = source_row.payload.get("candidate_id")
    if not isinstance(candidate_id, str):
        return "parallel_method_source_batch_unverified"
    ordered_declarations = tuple(
        row
        for row in rows
        if row.record_type == "contract.declared"
        and row.payload.get("candidate_id") == candidate_id
    )
    normalized = []
    for row in ordered_declarations:
        item_id = row.payload["contract"]["id"]
        item = next(
            (
                contract
                for contract in snapshot.context.contracts
                if contract.id == item_id
            ),
            None,
        )
        item_payload = _method_payload(item) if item is not None else {}
        if item_payload.get("method") != "parallel_tool_item":
            continue
        call = item_payload.get("payload")
        if not isinstance(call, Mapping) or not isinstance(call.get("args"), Mapping):
            return "parallel_method_source_noncanonical"
        normalized.append(
            {
                "id": item_payload.get("parallel_call_id"),
                "name": call.get("name"),
                "args": call.get("args"),
            }
        )
    if not 2 <= len(normalized) <= 8:
        return "parallel_method_source_batch_unverified"
    try:
        source_effect_types = read_effect_types(source_receipt.payload)
    except ValueError:
        return "parallel_method_source_publication_unverified"
    if source_effect_types != ("contract.declare",) * (len(normalized) + 1):
        return "parallel_method_source_noncanonical"
    continuation_ids = tuple(
        row.payload["contract"]["id"]
        for row in ordered_declarations
        if row.payload["contract"].get("origin") == "continuation"
    )
    continuations = tuple(
        contract
        for contract in snapshot.context.contracts
        if contract.id in continuation_ids
    )
    if len(continuations) != 1 or continuations[0].budget < 1:
        return "parallel_method_source_batch_unverified"
    try:
        expected = _parallel_tool_contracts(
            parent,
            WorkerAction(type="parallel_tool", payload={"calls": normalized}),
            allowance=len(normalized) + continuations[0].budget,
        )
    except (TypeError, ValueError):
        return "parallel_method_source_noncanonical"
    expected_ids = tuple(contract.id for contract in expected)
    if (
        tuple(row.payload["contract"]["id"] for row in ordered_declarations)
        != expected_ids
    ):
        return "parallel_method_source_batch_noncanonical"
    return "" if source in expected else "parallel_method_source_noncanonical"


def _schedule_tool_method_recovery(
    runtime: TaskCompletionContext,
    *,
    intent: MethodRecoveryIntent,
    recovery_snapshot=None,
) -> RecoveryDisposition:
    """Resolve both deterministic histories and publish only the current intent."""
    from aigineering.plugins.recovery_history import RecoveryHistoryCaptureUnstable

    def prove_both(snapshot):
        from aigineering.plugins.recovery_history import prove_recovery_publication

        legacy = prove_recovery_publication(
            snapshot,
            recovery=intent.legacy_recovery,
            context_template=intent.legacy_context,
            effects=_recovery_effects(intent.legacy_recovery, intent.legacy_context),
            idempotency_key=intent.idempotency_key,
            causal_parents=intent.causal_parents,
            source_record_ids=intent.source_record_ids,
        )
        current = prove_recovery_publication(
            snapshot,
            recovery=intent.recovery,
            context_template=intent.context_asset,
            effects=_recovery_effects(intent.recovery, intent.context_asset),
            idempotency_key=intent.idempotency_key,
            causal_parents=intent.causal_parents,
            source_record_ids=intent.source_record_ids,
        )
        return legacy, current

    def resolve(legacy, current):
        disposition = _history_status_disposition(legacy.status, current.status)
        if disposition:
            target = (
                intent.legacy_recovery.id
                if disposition == "legacy_method_intent_missing"
                else None
            )
            reason = disposition
            if disposition == "recovery_history_conflict":
                reason = (
                    legacy.reason if legacy.status == "conflict" else current.reason
                ) or disposition
            return RecoveryDisposition(None, reason, target)
        if current.status == "matched":
            if current.contract is None or current.context_asset is None:
                return RecoveryDisposition(None, "recovery_history_conflict")
            return RecoveryDisposition(current.contract)
        return None

    try:
        snapshot = recovery_snapshot or runtime.capture_recovery_history()
    except RecoveryHistoryCaptureUnstable:
        return RecoveryDisposition(None, "recovery_history_conflict")
    if snapshot is None:
        return RecoveryDisposition(None, "recovery_history_unavailable")
    legacy, current = prove_both(snapshot)
    resolved = resolve(legacy, current)
    if resolved is not None:
        return resolved
    if not runtime.can_publish_candidates("recovery.publish.v1"):
        return RecoveryDisposition(None, "recovery_publisher_unavailable")
    decision = runtime.publish_task_effects(
        "recovery.publish.v1",
        _recovery_effects(intent.recovery, intent.context_asset),
        idempotency_key=intent.idempotency_key,
        causal_parents=intent.causal_parents,
    )
    if decision is not None and decision.accepted:
        recovery, context_asset = decision.contracts[0], decision.assets[0]
    else:
        try:
            recaptured = runtime.capture_recovery_history()
        except RecoveryHistoryCaptureUnstable:
            return RecoveryDisposition(None, "recovery_history_conflict")
        if recaptured is None:
            return RecoveryDisposition(None, "recovery_history_unavailable")
        legacy, current = prove_both(recaptured)
        resolved = resolve(legacy, current)
        if resolved is not None:
            return resolved
        return RecoveryDisposition(None, "recovery_publication_rejected")
    runtime.append_trace(
        intent.causal_parents[0],
        "recovery_scheduled",
        relation_type="projection",
        relation_target=recovery.id,
        disclosed_assets=[context_asset.id],
        rejected_fragments=["[recovery] verified tool method retry"],
        authority_result="recovery_scheduled",
        budget_remaining=runtime.resolve_budget(intent.causal_parents[0]),
    )
    return RecoveryDisposition(recovery)


def _legacy_projection_intent(
    failed_contract: Contract,
    candidate_raw: str,
    rejections: list[dict],
    *,
    bind_failure_context: bool = True,
) -> tuple[Contract, Asset]:
    """Build the legacy generic intent used by projection repair and history reads."""
    context_name = f"_fail_context_{failed_contract.id}"
    repair_instructions = (
        "The failed task has allowed tools. If no successful tool observation "
        "is disclosed, request an allowed tool before producing its declared "
        "outputs. Never invent tool-derived evidence."
        if failed_contract.tool_scope
        else (
            "Return exactly /exec with only the failed contract's declared "
            "outputs. Do not invent undeclared asset names. Use the rejection "
            "reasons to correct the output format and names."
        )
    )
    context = {
        "trigger": "projection_rejected",
        "failed_contract_id": failed_contract.id,
        "failed_contract_name": failed_contract.name,
        "bad_worker_output": candidate_raw[:4000],
        "rejections": rejections,
        "required_outputs": list(failed_contract.outputs),
        "instructions": repair_instructions,
    }
    description = json.dumps(
        {
            "recovery": "projection_repair",
            "failed_contract_id": failed_contract.id,
            "instructions": (
                f"Read {context_name}. {repair_instructions} Required outputs: "
                f"{list(failed_contract.outputs)}."
            ),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    authority = (
        context_name,
        *(
            output
            for output in failed_contract.outputs
            if output in failed_contract.minting_authority
        ),
    )
    asset = system_asset(
        name=context_name,
        content=json.dumps(context, sort_keys=True, ensure_ascii=False),
        created_by=failed_contract.id,
        promptable=True,
    )
    policy = (
        dict(failed_contract.sensitive_input_policy)
        if failed_contract.sensitive_input_policy is not None
        else None
    )
    recovery = contract_from_fields(
        name=f"{failed_contract.name or failed_contract.id}.recover",
        description=description,
        inputs=_dedupe([context_name, *failed_contract.inputs]),
        outputs=list(failed_contract.outputs),
        activation=_append_activation(failed_contract.activation, context_name),
        budget=max(1, failed_contract.budget),
        tool_scope=list(failed_contract.tool_scope),
        labels=list(failed_contract.labels),
        worker_capabilities=list(failed_contract.worker_capabilities),
        worker_pools=list(failed_contract.worker_pools),
        delegation_capabilities=list(failed_contract.delegation_capabilities),
        delegation_pools=list(failed_contract.delegation_pools),
        origin="recovery",
        parent_id=failed_contract.parent_id,
        minting_authority=authority,
        sensitive_input_policy=policy,
        acceptance_policy=(
            dict(failed_contract.acceptance_policy)
            if failed_contract.acceptance_policy is not None
            else None
        ),
        context_asset_ids=failed_contract.context_asset_ids,
    )
    return recovery, asset


def _method_payload(contract: Contract) -> dict:
    try:
        value = json.loads(contract.description)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def has_recoverable_method_result_rejection(rejections: list[dict]) -> bool:
    return any(bool(entry.get("recoverable")) for entry in rejections)


def _publish_recovery(
    runtime: TaskCompletionContext,
    *,
    recovery: Contract,
    context_template: Asset,
    idempotency_key: str,
    causal_parents: tuple[str, ...],
    rejection_contract_id: str,
    relation_type: str,
    source_record_ids: tuple[str, ...] = (),
    recovery_snapshot=None,
) -> tuple[Contract, Asset] | None:
    effects = _recovery_effects(recovery, context_template)
    proof = _prove_existing_recovery(
        runtime,
        recovery=recovery,
        context_template=context_template,
        effects=effects,
        idempotency_key=idempotency_key,
        causal_parents=causal_parents,
        source_record_ids=source_record_ids,
        recovery_snapshot=recovery_snapshot,
    )
    if proof is not None:
        if proof.status == "matched":
            assert proof.contract is not None and proof.context_asset is not None
            return proof.contract, proof.context_asset
        if proof.status == "conflict":
            reason = (
                f"{relation_type} recovery publication history conflict: {proof.reason}"
            )
            runtime.record_rejection(
                rejection_contract_id,
                reason,
                relation_type=relation_type,
                relation_target=recovery.id,
                authority_result="rejected",
            )
            return None
    reason = f"{relation_type} recovery Candidate publisher unavailable"
    if runtime.can_publish_candidates("recovery.publish.v1"):
        decision = runtime.publish_task_effects(
            "recovery.publish.v1",
            effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )
        if decision is not None and decision.accepted:
            return decision.contracts[0], decision.assets[0]
        reason = f"{relation_type} recovery Candidate publication was rejected"
        proof = _prove_existing_recovery(
            runtime,
            recovery=recovery,
            context_template=context_template,
            effects=effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
            source_record_ids=source_record_ids,
        )
        if proof is not None and proof.status == "matched":
            assert proof.contract is not None and proof.context_asset is not None
            return proof.contract, proof.context_asset
        if proof is not None and proof.status == "conflict":
            reason = (
                f"{relation_type} recovery publication history conflict: {proof.reason}"
            )
    runtime.record_rejection(
        rejection_contract_id,
        reason,
        relation_type=relation_type,
        relation_target=recovery.id,
        authority_result="rejected",
    )
    return None


def _recovery_effects(recovery: Contract, context_template: Asset):
    declaration = contract_declaration_effect(recovery)
    asset = asset_proposal_effect(context_template)
    return (declaration, asset)


def _prove_existing_recovery(
    runtime: TaskCompletionContext,
    *,
    recovery: Contract,
    context_template: Asset,
    effects: tuple[CandidateEffect, ...],
    idempotency_key: str,
    causal_parents: tuple[str, ...],
    source_record_ids: tuple[str, ...] = (),
    recovery_snapshot=None,
) -> RecoveryPublicationProof | None:
    from aigineering.plugins.recovery_history import (
        RecoveryHistoryCaptureUnstable,
        RecoveryPublicationProof,
        prove_recovery_publication,
    )

    try:
        snapshot = recovery_snapshot or runtime.capture_recovery_history()
    except RecoveryHistoryCaptureUnstable:
        return RecoveryPublicationProof("conflict", reason="history_capture_unstable")
    if snapshot is None:
        return None
    return prove_recovery_publication(
        snapshot,
        recovery=recovery,
        context_template=context_template,
        effects=effects,
        idempotency_key=idempotency_key,
        causal_parents=causal_parents,
        source_record_ids=source_record_ids,
    )


def _method_result_output_name(method_type: str, parent_id: str) -> str:
    if method_type == "replan":
        return f"_replan_result_{parent_id}"
    return f"_plan_result_{parent_id}"


def _expected_format(method_type: str) -> dict:
    return {
        "contracts": [
            {
                "name": "short_child_task_name",
                "description": f"Concrete {method_type} child task.",
                "inputs": ["visible_input_or_promised_sibling_output"],
                "outputs": ["declared_child_output"],
                "activation": "visible_input_or_promised_sibling_output",
                "budget": 1,
                "tool_scope": [],
                "labels": [],
            }
        ]
    }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _append_activation(existing: str, context_name: str) -> str:
    if "_fail_context_" in existing:
        return context_name
    if existing.strip():
        return f"({existing}) AND {context_name}"
    return context_name


def _recovery_depth(name: str) -> int:
    return name.count(".recover")
