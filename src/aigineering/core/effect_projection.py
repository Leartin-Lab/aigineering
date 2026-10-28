"""Built-in typed-effect projectors for the commitment reducer."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from collections.abc import Mapping

from aigineering.core.effect_projectors import (
    BUILTIN_EFFECTS,
    EffectProjection,
    EffectProjector,
)
from aigineering.core.asset_graph_facts import project_new_graph_assets
from aigineering.core.acceptance import validate_output_shape
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
from aigineering.core.claim_assignments import (
    AssignmentAdmission,
    AssignmentConflict,
    AssignmentIntent,
    EffectFactOwner,
    SuccessorAssignmentWitness,
    assignment_context_from_projection,
    validate_assignment_batch,
)
from aigineering.core.injected_fact_replay import (
    CORE_GENERATED_FACT_TYPES,
    exact_single_effect_head_replay,
)
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
    effect_fact_owners: tuple[EffectFactOwner, ...] = ()
    assignment_intents: tuple[AssignmentIntent, ...] = ()
    successor_assignment_witnesses: tuple[SuccessorAssignmentWitness, ...] = ()

    @property
    def contract(self) -> Contract | None:
        """Compatibility view for callers expecting a single declaration."""
        return self.contracts[0] if len(self.contracts) == 1 else None


@dataclass(frozen=True)
class EffectHandler:
    capability: str
    projector: EffectProjector
    emitted_fact_types: frozenset[str] = frozenset()
    assignment_admission: AssignmentAdmission = AssignmentAdmission.NONE
    direct_output_fact_types: frozenset[str] | None = field(default=None, kw_only=True)
    reserved_fact_types: frozenset[str] = field(default_factory=frozenset, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.capability, str) or not self.capability:
            raise ValueError("effect handler capability must be non-empty")
        if not callable(self.projector):
            raise ValueError("effect handler projector must be callable")
        fact_types = frozenset(self.emitted_fact_types)
        if any(not isinstance(item, str) or not item for item in fact_types):
            raise ValueError("effect handler fact types must be non-empty strings")
        object.__setattr__(self, "emitted_fact_types", fact_types)
        if not isinstance(self.assignment_admission, AssignmentAdmission):
            raise ValueError("effect handler assignment admission is invalid")
        if (
            "claim.assignment.changed" in fact_types
            and self.assignment_admission is AssignmentAdmission.NONE
        ):
            raise ValueError("assignment fact requires an assignment admission")
        direct_types = _freeze_fact_type_set(
            self.direct_output_fact_types,
            "direct_output_fact_types",
            allow_none=True,
        )
        reserved_types = _freeze_fact_type_set(
            self.reserved_fact_types,
            "reserved_fact_types",
            allow_none=False,
        )
        if direct_types is None and reserved_types:
            raise ValueError(
                "reserved_fact_types requires strict direct_output_fact_types"
            )
        if direct_types is not None and not reserved_types <= direct_types:
            raise ValueError(
                "reserved_fact_types must be a subset of direct_output_fact_types"
            )
        object.__setattr__(self, "direct_output_fact_types", direct_types)
        object.__setattr__(self, "reserved_fact_types", reserved_types)


def _freeze_fact_type_set(
    value, name: str, *, allow_none: bool
) -> frozenset[str] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, (set, frozenset)):
        expected = "a set or frozenset" + (" or None" if allow_none else "")
        raise ValueError(f"{name} must be {expected}")
    result = frozenset(value)
    for fact_type in result:
        if not isinstance(fact_type, str) or not fact_type.strip():
            raise ValueError(f"{name} entries must be non-empty strings")
        try:
            fact_type.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError(f"{name} entries must be valid UTF-8") from exc
    return result


def freeze_additional_effects(
    effects: Mapping[str, tuple[str, EffectProjector] | EffectHandler] | None,
) -> Mapping[str, EffectHandler] | None:
    """Copy and validate trusted host projectors without changing core defaults."""
    if effects is None:
        return None
    if not isinstance(effects, Mapping):
        raise TypeError("additional_effects must be a mapping")
    copied: dict[str, EffectHandler] = {}
    for effect_type, handler in effects.items():
        if not isinstance(effect_type, str) or not effect_type:
            raise ValueError("additional effect type must be a non-empty string")
        if effect_type in BUILTIN_EFFECTS:
            raise ValueError(
                f"additional effect cannot replace built-in {effect_type!r}"
            )
        if isinstance(handler, EffectHandler):
            copied[effect_type] = EffectHandler(
                handler.capability,
                handler.projector,
                handler.emitted_fact_types,
                handler.assignment_admission,
                direct_output_fact_types=handler.direct_output_fact_types,
                reserved_fact_types=handler.reserved_fact_types,
            )
        elif isinstance(handler, tuple) and len(handler) == 2:
            capability, projector = handler
            copied[effect_type] = EffectHandler(capability, projector)
        else:
            raise ValueError(
                "additional effect handler must be EffectHandler or legacy pair"
            )
    reserved_owners: dict[str, str] = {}
    for effect_type, handler in copied.items():
        for fact_type in handler.reserved_fact_types:
            previous_owner = reserved_owners.get(fact_type)
            if previous_owner is not None:
                raise ValueError(
                    f"reserved fact type {fact_type!r} has multiple effect owners"
                )
            reserved_owners[fact_type] = effect_type
    return MappingProxyType(copied)


def project_effect_batch(
    candidate: CandidateProposal,
    receipt_id: str,
    actor_capabilities: tuple[str, ...],
    context: EffectProjectionContext | None = None,
    *,
    additional_effects: Mapping[str, tuple[str, EffectProjector] | EffectHandler]
    | None = None,
) -> EffectBatchProjection:
    """Project one Candidate-wide atomic effect group without Store access."""
    groups = {effect.atomic_group for effect in candidate.effects}
    if len(groups) > 1:
        raise ValueError("one Candidate cannot mix different atomic_group values")
    effective_context = context or EffectProjectionContext()
    handlers = dict(BUILTIN_EFFECTS)
    handlers.update(freeze_additional_effects(additional_effects) or {})
    normalized_handlers = {
        effect_type: handler
        if isinstance(handler, EffectHandler)
        else EffectHandler(*handler)
        for effect_type, handler in handlers.items()
    }
    reserved_owners: dict[str, str] = {}
    for effect_type, handler in normalized_handlers.items():
        for fact_type in handler.reserved_fact_types:
            previous_owner = reserved_owners.get(fact_type)
            if previous_owner is not None:
                raise ValueError(
                    f"reserved fact type {fact_type!r} has multiple effect owners"
                )
            reserved_owners[fact_type] = effect_type
    claimed_parent = _validate_claim_bound_effects(
        candidate, actor_capabilities, effective_context
    )
    projections: list[tuple[CandidateEffect, EffectProjection]] = []
    handlers_for_effect: dict[str, EffectHandler] = {}
    for effect in candidate.effects:
        handler = normalized_handlers.get(effect.effect_type)
        if handler is None:
            raise ValueError(f"unsupported effect type {effect.effect_type!r}")
        required_capability, projector = handler.capability, handler.projector
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
        if any(
            record.record_type == "claim.assignment.changed"
            and record.record_type not in handler.emitted_fact_types
            for record in projection.records
        ):
            raise ValueError("effect handler emitted an undeclared assignment fact")
        if (
            projection.assignment_intents
            and handler.assignment_admission is AssignmentAdmission.NONE
        ):
            raise ValueError("effect handler cannot emit assignment intents")
        if (
            projection.successor_assignment_witnesses
            and handler.assignment_admission
            is not AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR
        ):
            raise ValueError(
                "effect handler cannot emit successor assignment witnesses"
            )
        handlers_for_effect[effect.effect_type] = handler
        projections.append((effect, projection))

    exact_replay = False
    if len(projections) == 1:
        only_effect, only_projection = projections[0]
        exact_replay = exact_single_effect_head_replay(
            candidate,
            receipt_id,
            only_projection.records,
            effective_context.runtime_records,
        )
    for effect, projection in projections:
        handler = handlers_for_effect[effect.effect_type]
        for record in projection.records:
            owner = reserved_owners.get(record.record_type)
            if owner is not None and owner != effect.effect_type:
                raise ValueError(
                    f"effect handler {effect.effect_type!r} emitted reserved fact "
                    f"type {record.record_type!r} owned by {owner!r}"
                )
            is_generated_coeffect = (
                exact_replay
                and handler.direct_output_fact_types is not None
                and record.record_type in CORE_GENERATED_FACT_TYPES
                and record.record_type not in handler.direct_output_fact_types
            )
            if (
                handler.direct_output_fact_types is not None
                and record.record_type not in handler.direct_output_fact_types
                and not is_generated_coeffect
            ):
                raise ValueError(
                    f"effect handler {effect.effect_type!r} emitted undeclared "
                    f"direct fact type(s): {record.record_type}"
                )

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
        _validate_claim_bound_projection(claimed_parent, contracts, assets)
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
    owners = tuple(
        EffectFactOwner(
            record.id,
            effect.effect_type,
            handlers_for_effect[effect.effect_type].assignment_admission,
        )
        for effect, projection in projections
        for record in projection.records
        if (
            handlers_for_effect[effect.effect_type].direct_output_fact_types is None
            or record.record_type
            in handlers_for_effect[effect.effect_type].direct_output_fact_types
        )
    )
    intents = tuple(
        intent
        for _, projection in projections
        for intent in projection.assignment_intents
    )
    witnesses = tuple(
        witness
        for _, projection in projections
        for witness in projection.successor_assignment_witnesses
    )
    if (
        any(
            record.record_type == "claim.assignment.changed"
            for record in pending_records
        )
        or intents
        or witnesses
    ):
        try:
            validate_assignment_batch(
                assignment_context_from_projection(effective_context),
                pending_records,
                contracts,
                candidate=candidate,
                receipt_id=receipt_id,
                effect_fact_owners=owners,
                assignment_intents=intents,
                successor_assignment_witnesses=witnesses,
            )
        except AssignmentConflict as exc:
            raise ValueError(str(exc)) from exc
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
        effect_fact_owners=owners,
        assignment_intents=intents,
        successor_assignment_witnesses=witnesses,
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
