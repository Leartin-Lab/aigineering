"""Recovery helpers for method-result repair tasks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aigineering.core.ids import hash_contract_v2
from aigineering.protocol.types import Candidate, Contract

if TYPE_CHECKING:
    from aigineering.core.method_runtime import MethodRuntime
    from aigineering.protocol.types import Asset


class RecoveryMethodHandler:
    """Method ingress for explicit operator recovery decisions."""

    def handle_cancel(
        self,
        runtime: MethodRuntime,
        contract: Contract,
        candidate: Candidate,
    ) -> bool:
        """Apply an explicit recovery cancellation as one terminal transition."""
        if candidate.parsed_action is not None:
            action = candidate.parsed_action.get("action")
            if action != "cancel":
                return False
        return runtime.cancel_contract(
            contract,
            reason="operator requested cancellation of recovery-required contract",
            relation_target=candidate.worker_id,
        )


def schedule_method_result_recovery(
    runtime: MethodRuntime,
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
    cid = hash_contract_v2(
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
        origin="system",
        parent_id=parent_id,
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
        origin="system",
        minting_authority=(output_name, context_name),
    )
    runtime.add_contract(recovery)
    context_asset = runtime.mint_authorized_system_asset(
        recovery,
        name=context_name,
        content=json.dumps(context, sort_keys=True, ensure_ascii=False),
        created_by=failed_contract.id,
        promptable=True,
    )
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
    runtime: MethodRuntime,
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
    cid = hash_contract_v2(
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
        origin="recovery",
        parent_id=failed_contract.parent_id,
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
        worker_capabilities=failed_contract.worker_capabilities,
        worker_pools=failed_contract.worker_pools,
        origin="recovery",
        minting_authority=(context_name, *failed_contract.minting_authority),
        sensitive_input_policy=failed_contract.sensitive_input_policy,
    )
    runtime.add_contract(recovery)
    context_asset = runtime.mint_authorized_system_asset(
        recovery,
        name=context_name,
        content=json.dumps(context, sort_keys=True, ensure_ascii=False),
        created_by=failed_contract.id,
        promptable=True,
    )
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
