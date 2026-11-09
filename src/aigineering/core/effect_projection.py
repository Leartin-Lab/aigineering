"""Built-in typed-effect projectors for the commitment reducer."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

from aigineering.core.effect_projectors import (
    BUILTIN_EFFECTS,
    EffectProjection,
)
from aigineering.core.asset_graph_facts import project_new_graph_assets
from aigineering.core.acceptance import (
    validate_output_shape,
    validate_output_qualification_batch,
)
from aigineering.core.authority import (
    matched_reserved_prefix,
    validate_reserved_name_authority,
)
from aigineering.core.causal_allowance import (
    project_contract_allowance_records,
    validate_causal_allowance_returns,
)
from aigineering.core.fact_materialization import asset_committed_record
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.runtime_record import RuntimeRecord, validate_runtime_record
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
        if not isinstance(projection, EffectProjection):
            raise ValueError(
                f"projector for {effect.effect_type!r} returned an invalid projection"
            )
        if not isinstance(projection.records, tuple):
            raise ValueError(
                f"projector for {effect.effect_type!r} records must be a tuple"
            )
        for record in projection.records:
            if not isinstance(record, RuntimeRecord):
                raise ValueError(
                    f"projector for {effect.effect_type!r} returned a non-record fact"
                )
            if not isinstance(record.record_type, str) or not record.record_type:
                raise ValueError("projector returned a record with an invalid type")
            if type(record.schema_version) is not int or record.schema_version < 1:
                raise ValueError("projector returned a record with an invalid schema")
            try:
                validate_runtime_record(record)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "projector returned a malformed RuntimeRecord"
                ) from exc
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
    validate_output_qualification_batch(
        effective_context.runtime_records, projection_records
    )
    graph_assets = project_new_graph_assets(
        effective_context.runtime_records + projection_records,
        projection_records,
    )
    delegated_names = claimed_parent.minting_authority if claimed_parent else ()
    for record in projection_records:
        if record.record_type == "asset.definition.published":
            validate_reserved_name_authority(
                str(record.payload["definition"]["name"]),
                actor_capabilities,
                delegated_names,
            )
    for asset, _assertion in graph_assets:
        validate_reserved_name_authority(
            asset.name,
            actor_capabilities,
            delegated_names,
        )
    assets = assets + tuple(asset for asset, _ in graph_assets)
    if claimed_parent is not None:
        _validate_claim_bound_projection(
            claimed_parent,
            contracts,
            assets,
            candidate=candidate,
            graph_assets=graph_assets,
        )
    pending_records = projection_records + tuple(
        asset_committed_record(asset, causal_parents=(assertion.id,))
        for asset, assertion in graph_assets
    )
    incoming_returns = validate_causal_allowance_returns(
        tuple(effective_context.contracts) + contracts,
        effective_context.runtime_records,
        pending_records,
        candidate_id=candidate.id,
        receipt_id=receipt_id,
    )
    if incoming_returns and not {
        "contract.cancel",
        "contract.publish",
    }.issubset(actor_capabilities):
        raise ValueError(
            "allowance returns require cancellation and Contract publication authority"
        )
    existing_record_ids = {record.id for record in effective_context.runtime_records}
    return_records = tuple(
        record
        for record in pending_records
        if record.record_type == "allowance.returned"
        and record.id not in existing_record_ids
    )
    allowance_records = project_contract_allowance_records(
        contracts,
        effective_context.contracts,
        effective_context.runtime_records + return_records,
        causal_parent=receipt_id,
        balance_inputs=effective_context.allowance_balance_inputs,
    )
    targets = tuple(
        (effect.effect_type, projection.relation_target)
        for effect, projection in projections
    )
    return EffectBatchProjection(
        records=pending_records + allowance_records,
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
    *,
    candidate: CandidateProposal,
    graph_assets,
) -> None:
    if assets:
        requirements = (parent.acceptance_policy or {}).get("output_requirements", {})
        assertions = {
            asset.id: record.payload["assertion"] for asset, record in graph_assets
        }
        for asset in assets:
            required_definition = requirements.get(asset.name, {}).get("definition_id")
            if required_definition:
                assertion = assertions.get(asset.id, {})
                evidence = assertion.get("evidence", {})
                if (
                    asset.definition_hash != required_definition
                    or assertion.get("actor_id") != candidate.actor_id
                    or assertion.get("key_id") != candidate.key_id
                    or not isinstance(evidence, Mapping)
                    or evidence.get("contract_id") != parent.id
                    or evidence.get("output_name") != asset.name
                ):
                    raise ValueError(
                        "shared-definition output lacks exact claim provenance"
                    )
            elif asset.created_by != parent.id:
                raise ValueError(
                    "claim-bound output Asset has invalid Contract provenance"
                )
        if {asset.name for asset in assets} != set(parent.outputs):
            raise ValueError(
                "claim-bound output must satisfy exactly all declared outputs"
            )
        if parent.acceptance_policy is not None:
            from aigineering.core.output_satisfaction import (
                output_asset_meets_conditions,
            )

            for asset in assets:
                validate_output_shape(
                    parent.acceptance_policy, asset.name, asset.content
                )
                if not output_asset_meets_conditions(parent, asset.name, asset):
                    raise ValueError(
                        "claim-bound output does not meet declared Asset conditions"
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
            if parent.id.startswith(("task:v5:", "task:v6:"))
            else set(parent.worker_capabilities)
        )
        parent_pool_scope = (
            set(parent.delegation_pools) | set(parent.worker_pools)
            if parent.id.startswith(("task:v5:", "task:v6:"))
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
