"""Worker task delegation as a typed Candidate adapter plugin."""

from __future__ import annotations

import json

from aigineering.core.ids import compute_content_hash, hash_contract_current
from aigineering.protocol.immutability import deep_thaw
from aigineering.plugins.task_semantics import method_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Contract


class TaskDelegationPlugin:
    """Propose an ordinary contained task for a claimed action."""

    plugin_id = "task.delegate.v1"
    action_types = frozenset(
        {"fail", "plan", "replan", "retry", "tool", "parallel_tool"}
    )

    def can_handle(self, action_type: str) -> bool:
        return action_type in self.action_types

    def propose_claimed(
        self,
        parent: Contract,
        action: WorkerAction,
        *,
        allowance: int,
    ):
        """Propose an ordinary contained task for one claim-bound action."""
        if action.type not in {"tool", "parallel_tool", "fail", "retry"}:
            raise ValueError(f"unsupported claimed task action {action.type!r}")
        if allowance < 1:
            raise ValueError("claimed task action has no causal allowance")
        from aigineering.plugins.base import PluginProposal

        if action.type == "parallel_tool":
            children = _parallel_tool_contracts(parent, action, allowance)
            return PluginProposal(
                effects=tuple(contract_declaration_effect(child) for child in children)
            )
        child = (
            _claimed_retry_contract(parent, allowance)
            if action.type == "retry"
            else _claimed_method_contract(parent, action)
        )
        return PluginProposal(effects=(contract_declaration_effect(child),))


def _parallel_tool_contracts(
    parent: Contract, action: WorkerAction, allowance: int
) -> tuple[Contract, ...]:
    payload = deep_thaw(action.payload)
    calls = payload.get("calls")
    if not isinstance(calls, list) or not 2 <= len(calls) <= 8:
        raise ValueError("parallel_tool requires between 2 and 8 calls")
    if payload.get("join", "all") != "all":
        raise ValueError("parallel_tool currently supports only join='all'")
    if allowance < len(calls) + 1:
        raise ValueError("parallel_tool lacks allowance for calls and continuation")

    normalized: list[dict[str, object]] = []
    for index, raw in enumerate(calls):
        if not isinstance(raw, dict):
            raise ValueError("parallel_tool calls must be objects")
        name = raw.get("name")
        args = raw.get("args", {})
        call_id = raw.get("id", f"call-{index + 1}")
        if not isinstance(name, str) or not name:
            raise ValueError("parallel_tool call name must be a non-empty string")
        if name not in parent.tool_scope:
            raise ValueError(f"parallel_tool call {name!r} is outside tool scope")
        if not isinstance(args, dict):
            raise ValueError("parallel_tool call args must be an object")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("parallel_tool call id must be a non-empty string")
        normalized.append({"id": call_id, "name": name, "args": args})
    ids = [str(call["id"]) for call in normalized]
    if len(set(ids)) != len(ids):
        raise ValueError("parallel_tool call ids must be unique")

    batch_key = compute_content_hash(
        json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    )[:20]
    tool_tasks = tuple(
        _parallel_tool_item_contract(parent, call, batch_key) for call in normalized
    )
    observations = tuple(task.outputs[0] for task in tool_tasks)
    used_tools = {str(call["name"]) for call in normalized}
    continuation_fields = {
        "name": f"{parent.name or parent.id}.parallel_tool.continue.{batch_key}",
        "description": parent.description,
        "inputs": observations,
        "outputs": parent.outputs,
        "activation": " AND ".join(observations),
        "budget": allowance - len(tool_tasks),
        "tool_scope": tuple(
            tool for tool in parent.tool_scope if tool not in used_tools
        ),
        "labels": tuple(
            dict.fromkeys((*parent.labels, "plugin:parallel_tool.continuation"))
        ),
        "worker_capabilities": parent.worker_capabilities,
        "worker_pools": parent.worker_pools,
        "delegation_capabilities": parent.delegation_capabilities,
        "delegation_pools": parent.delegation_pools,
        "origin": "continuation",
        "parent_id": parent.id,
        "minting_authority": tuple(
            output for output in parent.outputs if output in parent.minting_authority
        ),
        "sensitive_input_policy": (
            dict(parent.sensitive_input_policy)
            if parent.sensitive_input_policy is not None
            else None
        ),
        "context_asset_ids": parent.context_asset_ids,
    }
    continuation = Contract(
        id=hash_contract_current(**continuation_fields), **continuation_fields
    )
    return (*tool_tasks, continuation)


def _parallel_tool_item_contract(
    parent: Contract, call: dict[str, object], batch_key: str
) -> Contract:
    base = _claimed_method_contract(
        parent,
        WorkerAction(type="tool", payload={"name": call["name"], "args": call["args"]}),
    )
    description = json.loads(base.description)
    description["method"] = "parallel_tool_item"
    description["parallel_call_id"] = call["id"]
    description["parallel_batch"] = batch_key
    labels = tuple(
        "plugin:parallel_tool_item" if label == "plugin:tool" else label
        for label in base.labels
    )
    observation_name = (
        f"tool_observation_{batch_key}_{compute_content_hash(str(call['id']))[:12]}"
    )
    fields = {
        "name": f"{parent.name or parent.id}.parallel_tool.{call['id']}",
        "description": json.dumps(description, sort_keys=True, ensure_ascii=False),
        "inputs": base.inputs,
        "outputs": (observation_name,),
        "activation": base.activation,
        "budget": 1,
        "tool_scope": base.tool_scope,
        "labels": labels,
        "worker_capabilities": base.worker_capabilities,
        "worker_pools": (),
        "delegation_capabilities": (),
        "delegation_pools": (),
        "origin": "system",
        "parent_id": parent.id,
        "minting_authority": base.minting_authority,
        "sensitive_input_policy": (
            dict(base.sensitive_input_policy)
            if base.sensitive_input_policy is not None
            else None
        ),
        "context_asset_ids": base.context_asset_ids,
    }
    return Contract(id=hash_contract_current(**fields), **fields)


def _claimed_method_contract(parent: Contract, action: WorkerAction) -> Contract:
    base = method_contract(parent, action)
    labels = tuple(dict.fromkeys((*parent.labels, f"plugin:{action.type}")))
    result_name = (
        f"tool_observation_{compute_content_hash(base.description)[:24]}"
        if action.type == "tool"
        else f"failure_result_{compute_content_hash(base.description)[:24]}"
    )
    fields = {
        "name": base.name,
        "description": base.description,
        "inputs": base.inputs,
        "outputs": (result_name,),
        "activation": parent.activation,
        "budget": 1,
        "tool_scope": base.tool_scope,
        "labels": labels,
        "worker_capabilities": base.worker_capabilities,
        "worker_pools": () if action.type == "tool" else base.worker_pools,
        "delegation_capabilities": base.delegation_capabilities,
        "delegation_pools": base.delegation_pools,
        "origin": "system",
        "parent_id": parent.id,
        "minting_authority": (),
        "sensitive_input_policy": (
            dict(parent.sensitive_input_policy)
            if parent.sensitive_input_policy is not None
            else None
        ),
        "context_asset_ids": parent.context_asset_ids,
    }
    return Contract(id=hash_contract_current(**fields), **fields)


def _claimed_retry_contract(parent: Contract, allowance: int) -> Contract:
    budget = max(1, allowance)
    authority = tuple(
        output for output in parent.outputs if output in parent.minting_authority
    )
    labels = tuple(dict.fromkeys((*parent.labels, "plugin:retry")))
    policy = (
        dict(parent.sensitive_input_policy)
        if parent.sensitive_input_policy is not None
        else None
    )
    acceptance = (
        dict(parent.acceptance_policy) if parent.acceptance_policy is not None else None
    )
    fields = {
        "name": f"{parent.name or parent.id}.retry",
        "description": parent.description,
        "inputs": parent.inputs,
        "outputs": parent.outputs,
        "activation": parent.activation,
        "budget": budget,
        "tool_scope": parent.tool_scope,
        "labels": labels,
        "worker_capabilities": parent.worker_capabilities,
        "worker_pools": parent.worker_pools,
        "delegation_capabilities": parent.delegation_capabilities,
        "delegation_pools": parent.delegation_pools,
        "origin": "retry",
        "parent_id": parent.id,
        "minting_authority": authority,
        "sensitive_input_policy": policy,
        "acceptance_policy": acceptance,
        "context_asset_ids": parent.context_asset_ids,
    }
    return Contract(id=hash_contract_current(**fields), **fields)
