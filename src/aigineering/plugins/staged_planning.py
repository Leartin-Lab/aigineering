"""Pure three-stage planning task construction.

The plugin does not execute planning or mutate runtime state.  It proposes
three ordinary Contracts whose facts form the stage boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from aigineering.core.ids import (
    canonical_json,
    compute_content_hash,
    hash_contract_current,
)
from aigineering.plugins.base import PluginProposal, PluginRequest
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Contract


@dataclass(frozen=True)
class PlanningStages:
    draft: Contract
    dependencies: Contract
    compile: Contract

    @property
    def contracts(self) -> tuple[Contract, Contract, Contract]:
        return (self.draft, self.dependencies, self.compile)


class StagedPlanningPlugin:
    """Propose draft, dependency-analysis and compile tasks atomically."""

    plugin_id = "planning.staged.v1"
    mode = "plan"

    def propose(self, request: PluginRequest) -> PluginProposal:
        stages = self.stages(request)
        group = f"{self.mode}:{_invocation_id(request, self.mode)}"
        return PluginProposal(
            effects=tuple(
                contract_declaration_effect(contract, atomic_group=group)
                for contract in stages.contracts
            )
        )

    def stages(self, request: PluginRequest) -> PlanningStages:
        if request.allowance < 3:
            raise ValueError("staged planning requires at least 3 allowance units")
        parent = request.parent
        invocation = _invocation_id(request, self.mode)
        draft_output = f"{self.mode}_draft_{invocation}"
        dependencies_output = f"{self.mode}_dependencies_{invocation}"
        policy = {"mode": "mechanical", "required_attestations": 1}

        draft = _stage_contract(
            parent,
            mode=self.mode,
            stage="draft",
            invocation=invocation,
            description={
                "goal": parent.description,
                "invocation": deep_thaw(request.parameters),
                "required_outputs": list(parent.outputs),
                "stage": "draft",
                "task": (
                    "Describe goals, evidence needs, uncertainty and candidate steps. "
                    "Do not emit Contract wire objects."
                ),
            },
            inputs=parent.inputs,
            outputs=(draft_output,),
            activation=parent.activation,
            acceptance_policy=policy,
            budget=1,
            minting_authority=(),
        )
        dependencies = _stage_contract(
            parent,
            mode=self.mode,
            stage="dependencies",
            invocation=invocation,
            description={
                "required_outputs": list(parent.outputs),
                "stage": "dependencies",
                "task": (
                    "Map producers and consumers; detect missing inputs, cycles, "
                    "invalid activation, capability, authority and allowance needs."
                ),
            },
            inputs=(draft_output,),
            outputs=(dependencies_output,),
            activation=draft_output,
            acceptance_policy=policy,
            budget=1,
            minting_authority=(),
        )
        compile_contract = _stage_contract(
            parent,
            mode=self.mode,
            stage="compile",
            invocation=invocation,
            description={
                "allowed_inputs": sorted(
                    request.allowed_input_names or frozenset(parent.inputs)
                ),
                "required_outputs": list(parent.outputs),
                "stage": "compile",
                "task": (
                    "Compile the accepted draft and dependency analysis into the "
                    "exact structured planning schema without inventing evidence or "
                    "weakening parent constraints."
                ),
            },
            inputs=tuple(
                dict.fromkeys((*parent.inputs, draft_output, dependencies_output))
            ),
            outputs=parent.outputs,
            activation=f"{draft_output} AND {dependencies_output}",
            acceptance_policy=policy,
            budget=request.allowance - 2,
            minting_authority=tuple(
                output
                for output in parent.outputs
                if output in parent.minting_authority
            ),
        )
        return PlanningStages(draft, dependencies, compile_contract)


class StagedReplanningPlugin(StagedPlanningPlugin):
    """Use the same stage protocol while binding invalidation evidence."""

    plugin_id = "replanning.staged.v1"
    mode = "replan"


def _invocation_id(request: PluginRequest, mode: str) -> str:
    return compute_content_hash(
        canonical_json(
            {
                "asset_ids": sorted(asset.id for asset in request.assets),
                "mode": mode,
                "parent_id": request.parent.id,
                "parameters": deep_thaw(request.parameters),
            }
        )
    )[:24]


def _stage_contract(
    parent: Contract,
    *,
    mode: str,
    stage: str,
    invocation: str,
    description: dict[str, object],
    inputs: tuple[str, ...],
    outputs: tuple[str, ...],
    activation: str,
    acceptance_policy: dict[str, object],
    budget: int,
    minting_authority: tuple[str, ...] | None = None,
) -> Contract:
    name = f"{parent.name or parent.id}.{mode}.{stage}.{invocation}"
    labels = tuple(dict.fromkeys((*parent.labels, f"plugin:{mode}.{stage}")))
    authority_templates = outputs if minting_authority is None else minting_authority
    fields = {
        "name": name,
        "description": json.dumps(description, sort_keys=True, ensure_ascii=False),
        "inputs": inputs,
        "outputs": outputs,
        "activation": activation,
        "budget": budget,
        "tool_scope": parent.tool_scope,
        "labels": labels,
        "worker_capabilities": (),
        "worker_pools": parent.worker_pools,
        "origin": "plugin",
        "parent_id": parent.id,
        "minting_authority": authority_templates,
        "sensitive_input_policy": (
            dict(parent.sensitive_input_policy)
            if parent.sensitive_input_policy is not None
            else None
        ),
        "acceptance_policy": acceptance_policy,
        "context_asset_ids": parent.context_asset_ids,
    }
    contract_id = hash_contract_current(**fields)
    return Contract(id=contract_id, **fields)
