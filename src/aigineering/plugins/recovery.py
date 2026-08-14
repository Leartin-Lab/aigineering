"""Recovery task projection shared by runtime and completion plugins."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.core.ids import hash_contract_current
from aigineering.plugins.task_semantics import system_asset
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Contract

if TYPE_CHECKING:
    from aigineering.plugins.completion_projection import TaskCompletionContext
    from aigineering.protocol.types import Asset


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

    if _recovery_depth(failed_contract.name) >= 1:
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
    cid = hash_contract_current(
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
        context_asset_ids=failed_contract.context_asset_ids,
    )
    if runtime.get_contract(cid) is not None:
        return None

    recovery = Contract(
        id=cid,
        parent_id=parent_id,
        name=name,
        description=description,
        inputs=[context_name],
        outputs=[output_name],
        activation=context_name,
        budget=1,
        tool_scope=[],
        labels=[f"method:{method_type}"],
        context_asset_ids=failed_contract.context_asset_ids,
        worker_capabilities=failed_contract.worker_capabilities,
        worker_pools=failed_contract.worker_pools,
        delegation_capabilities=failed_contract.delegation_capabilities,
        delegation_pools=failed_contract.delegation_pools,
        origin="system",
        minting_authority=authority,
        sensitive_input_policy=failed_contract.sensitive_input_policy,
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
) -> Contract | None:
    """Create a normal recovery task for rejected Worker output."""

    if _recovery_depth(failed_contract.name) >= 1:
        return None

    context_name = f"_fail_context_{failed_contract.id}"
    context = {
        "trigger": "projection_rejected",
        "failed_contract_id": failed_contract.id,
        "failed_contract_name": failed_contract.name,
        "bad_worker_output": candidate_raw[:4000],
        "rejections": rejections,
        "required_outputs": list(failed_contract.outputs),
        "instructions": (
            "Return exactly /exec with only the failed contract's declared "
            "outputs. Do not invent undeclared asset names. Use the rejection "
            "reasons to correct the output format and names."
        ),
    }
    name = f"{failed_contract.name or failed_contract.id}.recover"
    description = json.dumps(
        {
            "recovery": "projection_repair",
            "failed_contract_id": failed_contract.id,
            "instructions": (
                f"Read {context_name}. Repair the rejected Worker output and "
                f"produce exactly these declared outputs: "
                f"{list(failed_contract.outputs)}."
            ),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    inputs = _dedupe([context_name, *failed_contract.inputs])
    activation = _append_activation(failed_contract.activation, context_name)
    output_authority = tuple(
        output
        for output in failed_contract.outputs
        if output in failed_contract.minting_authority
    )
    authority = (context_name, *output_authority)
    policy = (
        dict(failed_contract.sensitive_input_policy)
        if failed_contract.sensitive_input_policy is not None
        else None
    )
    cid = hash_contract_current(
        name=name,
        description=description,
        inputs=inputs,
        outputs=list(failed_contract.outputs),
        activation=activation,
        budget=1,
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
        context_asset_ids=failed_contract.context_asset_ids,
    )
    if runtime.get_contract(cid) is not None:
        return None

    recovery = Contract(
        id=cid,
        parent_id=failed_contract.parent_id,
        name=name,
        description=description,
        inputs=inputs,
        outputs=failed_contract.outputs,
        activation=activation,
        budget=1,
        tool_scope=failed_contract.tool_scope,
        labels=failed_contract.labels,
        context_asset_ids=failed_contract.context_asset_ids,
        worker_capabilities=failed_contract.worker_capabilities,
        worker_pools=failed_contract.worker_pools,
        delegation_capabilities=failed_contract.delegation_capabilities,
        delegation_pools=failed_contract.delegation_pools,
        origin="recovery",
        minting_authority=authority,
        sensitive_input_policy=failed_contract.sensitive_input_policy,
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
    return recovery


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
) -> tuple[Contract, Asset] | None:
    reason = f"{relation_type} recovery Candidate publisher unavailable"
    if runtime.can_publish_candidates("recovery.publish.v1"):
        decision = runtime.publish_task_effects(
            "recovery.publish.v1",
            (
                contract_declaration_effect(recovery),
                asset_proposal_effect(context_template),
            ),
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )
        if decision is not None and decision.accepted:
            return decision.contracts[0], decision.assets[0]
        reason = f"{relation_type} recovery Candidate publication was rejected"
    runtime.record_rejection(
        rejection_contract_id,
        reason,
        relation_type=relation_type,
        relation_target=recovery.id,
        authority_result="rejected",
    )
    return None


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
