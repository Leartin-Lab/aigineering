"""Worker task delegation as a typed Candidate adapter plugin."""

from __future__ import annotations

from aigineering.core.ids import compute_content_hash, hash_contract_v3
from aigineering.plugins.task_semantics import method_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Contract


class TaskDelegationPlugin:
    """Propose an ordinary contained task for a claimed action."""

    plugin_id = "task.delegate.v1"
    action_types = frozenset({"fail", "plan", "replan", "retry", "tool"})

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
        if action.type not in {"tool", "fail", "retry"}:
            raise ValueError(f"unsupported claimed task action {action.type!r}")
        if allowance < 1:
            raise ValueError("claimed task action has no causal allowance")
        child = (
            _claimed_retry_contract(parent, allowance)
            if action.type == "retry"
            else _claimed_method_contract(parent, action)
        )
        from aigineering.plugins.base import PluginProposal

        return PluginProposal(effects=(contract_declaration_effect(child),))


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
        "worker_pools": base.worker_pools,
        "origin": "system",
        "parent_id": parent.id,
        "minting_authority": (),
        "sensitive_input_policy": (
            dict(parent.sensitive_input_policy)
            if parent.sensitive_input_policy is not None
            else None
        ),
    }
    return Contract(id=hash_contract_v3(**fields), **fields)


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
        "origin": "retry",
        "parent_id": parent.id,
        "minting_authority": authority,
        "sensitive_input_policy": policy,
        "acceptance_policy": acceptance,
    }
    return Contract(id=hash_contract_v3(**fields), **fields)
