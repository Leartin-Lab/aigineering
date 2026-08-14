"""Built-in typed-effect projectors for the commitment reducer."""

from __future__ import annotations

from dataclasses import dataclass

from aigineering.core.effect_projectors import BUILTIN_EFFECTS, EffectProjection
from aigineering.core.asset_graph_facts import project_new_graph_assets
from aigineering.core.acceptance import validate_output_shape
from aigineering.core.authority import matched_reserved_prefix
from aigineering.core.causal_allowance import project_contract_allowance_records
from aigineering.core.fact_materialization import asset_committed_record
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.protocol.types import Asset, Contract


@dataclass(frozen=True)
class EffectBatchProjection:
    records: tuple[RuntimeRecord, ...]
    relation_target: str
    projected_effects: tuple[tuple[str, str], ...]
    contracts: tuple[Contract, ...] = ()
    assets: tuple[Asset, ...] = ()
    accepted_asset_names: tuple[str, ...] = ()

    @property
    def contract(self) -> Contract | None:
        """Compatibility view for callers expecting a single declaration."""
        return self.contracts[0] if len(self.contracts) == 1 else None


def project_effect_batch(
    candidate: CandidateProposal,
    receipt_id: str,
    actor_capabilities: tuple[str, ...],
    context: EffectProjectionContext | None = None,
) -> EffectBatchProjection:
    """Project one Candidate-wide atomic effect group without Store access."""
    groups = {effect.atomic_group for effect in candidate.effects}
    if len(groups) > 1:
        raise ValueError("one Candidate cannot mix different atomic_group values")
    effective_context = context or EffectProjectionContext()
    claimed_parent = _validate_claim_bound_effects(
        candidate, actor_capabilities, effective_context
    )
    projections: list[tuple[CandidateEffect, EffectProjection]] = []
    for effect in candidate.effects:
        handler = BUILTIN_EFFECTS.get(effect.effect_type)
        if handler is None:
            raise ValueError(f"unsupported effect type {effect.effect_type!r}")
        required_capability, projector = handler
        claim_delegated = claimed_parent is not None and effect.effect_type in {
            "asset.assert",
            "asset.content.publish",
            "asset.definition.publish",
            "asset.propose",
            "contract.declare",
        }
        if not claim_delegated and required_capability not in actor_capabilities:
            raise ValueError(f"actor lacks required capability {required_capability!r}")
        projection = projector(effect, candidate, receipt_id, effective_context)
        missing = tuple(
            capability
            for capability in projection.additional_capabilities
            if capability not in actor_capabilities
            and not _claim_delegates_protected_capability(
                claimed_parent, projection, capability
            )
        )
        if missing:
            raise ValueError(f"actor lacks required capabilities {missing!r}")
        projections.append((effect, projection))

    _validate_graph_batch(projections, effective_context)

    contracts = tuple(
        projection.contract
        for _, projection in projections
        if projection.contract is not None
    )
    assets = tuple(
        asset for _, projection in projections for asset in projection.assets
    )
    projection_records = _ordered_projection_records(projections)
    graph_assets = project_new_graph_assets(
        effective_context.runtime_records + projection_records,
        projection_records,
    )
    assets = assets + tuple(asset for asset, _ in graph_assets)
    if claimed_parent is not None:
        _validate_claim_bound_projection(claimed_parent, contracts, assets)
    allowance_records = project_contract_allowance_records(
        contracts,
        effective_context.contracts,
        effective_context.runtime_records,
        causal_parent=receipt_id,
    )
    targets = tuple(
        (effect.effect_type, projection.relation_target)
        for effect, projection in projections
    )
    return EffectBatchProjection(
        records=projection_records
        + tuple(
            asset_committed_record(asset, causal_parents=(assertion.id,))
            for asset, assertion in graph_assets
        )
        + allowance_records,
        relation_target=(targets[0][1] if len(targets) == 1 else candidate.id),
        projected_effects=targets,
        contracts=contracts,
        assets=assets,
        accepted_asset_names=tuple(
            name
            for _, projection in projections
            for name in projection.accepted_asset_names
        )
        + tuple(asset.name for asset, _ in graph_assets),
    )


def _validate_graph_batch(
    projections: list[tuple[CandidateEffect, EffectProjection]],
    context: EffectProjectionContext,
) -> None:
    records = tuple(
        record for _, projection in projections for record in projection.records
    )
    definition_ids = {
        str(record.payload["definition"]["id"])
        for record in (*context.runtime_records, *records)
        if record.record_type == "asset.definition.published"
    }
    content_ids = {
        str(record.payload["content"]["id"])
        for record in (*context.runtime_records, *records)
        if record.record_type == "asset.content.published"
    }
    for record in records:
        if record.record_type != "asset.definition-content.asserted":
            continue
        assertion = record.payload["assertion"]
        if assertion["definition_id"] not in definition_ids:
            raise ValueError("assertion references an unknown signed definition")
        if assertion["content_id"] not in content_ids:
            raise ValueError("assertion references an unknown content object")


def _ordered_projection_records(
    projections: list[tuple[CandidateEffect, EffectProjection]],
) -> tuple[RuntimeRecord, ...]:
    records = tuple(
        record for _, projection in projections for record in projection.records
    )
    graph_order = {
        "asset.content.published": 0,
        "asset.definition.published": 1,
        "asset.definition-content.asserted": 2,
    }
    graph = tuple(record for record in records if record.record_type in graph_order)
    non_graph = tuple(
        record for record in records if record.record_type not in graph_order
    )
    return non_graph + tuple(
        sorted(graph, key=lambda record: graph_order[record.record_type])
    )


def _validate_claim_bound_effects(
    candidate: CandidateProposal,
    actor_capabilities: tuple[str, ...],
    context: EffectProjectionContext,
) -> Contract | None:
    binding = candidate.claim_binding
    if binding is None:
        return None
    if "worker.submit" not in actor_capabilities:
        raise ValueError("claim-bound Candidate actor lacks 'worker.submit'")
    parent = next(
        (
            contract
            for contract in context.contracts
            if contract.id == binding.contract_id
        ),
        None,
    )
    if parent is None:
        raise ValueError("claim-bound Candidate references an unknown Contract")
    effect_types = {effect.effect_type for effect in candidate.effects}
    graph_effects = {
        "asset.assert",
        "asset.content.publish",
        "asset.definition.publish",
    }
    output_effect_types = effect_types - {"asset.attest"}
    valid_output = output_effect_types == {"asset.propose"} or (
        output_effect_types <= graph_effects and "asset.assert" in output_effect_types
    )
    if parent.origin == "recovery" and parent.tool_scope and valid_output:
        raise ValueError(
            "tool recovery cannot publish outputs before its remaining tool scope "
            "is compiled into tool observations"
        )
    if "asset.attest" in effect_types and "asset.attest" not in actor_capabilities:
        raise ValueError("claim-bound attestation actor lacks 'asset.attest'")
    if not valid_output and effect_types != {"contract.declare"}:
        raise ValueError("claim-bound Candidate contains an unsupported effect")
    return parent


def _claim_delegates_protected_capability(
    parent: Contract | None,
    projection: EffectProjection,
    capability: str,
) -> bool:
    if parent is None:
        return False
    if capability == "asset.publish.protected":
        return all(
            asset.name in parent.minting_authority for asset in projection.assets
        )
    if capability == "contract.publish.protected" and projection.contract is not None:
        inherited = set(parent.minting_authority)
        return (
            all(
                output in inherited and output in projection.contract.minting_authority
                for output in projection.contract.outputs
                if matched_reserved_prefix(output) is not None
            )
            and set(projection.contract.minting_authority) <= inherited
        )
    return False


def _validate_claim_bound_projection(
    parent: Contract,
    contracts: tuple[Contract, ...],
    assets: tuple[Asset, ...],
) -> None:
    if assets:
        if any(asset.created_by != parent.id for asset in assets):
            raise ValueError("claim-bound output Asset has invalid Contract provenance")
        if {asset.name for asset in assets} != set(parent.outputs):
            raise ValueError(
                "claim-bound output must satisfy exactly all declared outputs"
            )
        if parent.acceptance_policy is not None:
            for asset in assets:
                validate_output_shape(
                    parent.acceptance_policy, asset.name, asset.content
                )
        return
    if not contracts:
        raise ValueError("claim-bound expansion produced no Contracts")
    available_inputs = set(parent.inputs)
    available_inputs.update(
        f"_tool_capability_{name}"
        for name in parent.tool_scope
        if not name.startswith("mcp:")
    )
    available_inputs.update(
        f"_mcp_{name[4:].split('.', 1)[0]}"
        for name in parent.tool_scope
        if name.startswith("mcp:")
    )
    for contract in contracts:
        available_inputs.update(contract.outputs)
    if sum(contract.budget for contract in contracts) > parent.budget:
        raise ValueError("claim-bound expansion exceeds parent causal allowance")
    for contract in contracts:
        if contract.parent_id != parent.id:
            raise ValueError("claim-bound child must reference the claimed parent")
        if not set(contract.inputs) <= available_inputs:
            raise ValueError("claim-bound child widens disclosed input scope")
        if not set(contract.tool_scope) <= set(parent.tool_scope):
            raise ValueError("claim-bound child widens tool scope")
        parent_capability_scope = (
            set(parent.delegation_capabilities) | set(parent.worker_capabilities)
            if parent.id.startswith("task:v5:")
            else set(parent.worker_capabilities)
        )
        parent_pool_scope = (
            set(parent.delegation_pools) | set(parent.worker_pools)
            if parent.id.startswith("task:v5:")
            else set(parent.worker_pools)
        )
        if not set(contract.worker_pools) <= parent_pool_scope:
            raise ValueError("claim-bound child widens worker pools")
        extra_capabilities = set(contract.worker_capabilities) - set(
            parent_capability_scope
        )
        allowed_capabilities = (
            {
                "tool-execution",
                "mcp-execution",
                *(f"tool:{name}" for name in contract.tool_scope),
            }
            if contract.tool_scope
            else set()
        )
        if not extra_capabilities <= allowed_capabilities:
            raise ValueError("claim-bound child widens worker capabilities")
        if not set(contract.delegation_capabilities) <= parent_capability_scope:
            raise ValueError("claim-bound child widens delegation capabilities")
        if not set(contract.delegation_pools) <= parent_pool_scope:
            raise ValueError("claim-bound child widens delegation pools")
        if not set(contract.minting_authority) <= set(parent.minting_authority):
            raise ValueError("claim-bound child widens minting authority")
        non_plugin_labels = {
            label for label in contract.labels if not label.startswith("plugin:")
        }
        if not non_plugin_labels <= set(parent.labels):
            raise ValueError("claim-bound child widens parent labels")
        if not set(contract.context_asset_ids) <= set(parent.context_asset_ids):
            raise ValueError("claim-bound child widens exact context Asset references")
