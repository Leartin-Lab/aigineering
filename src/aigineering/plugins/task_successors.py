"""Shared pure helpers for immutable task successor projections."""

from __future__ import annotations

from dataclasses import replace

from aigineering.core.disclosure import compute_disclosure
from aigineering.core.input_bindings import (
    input_asset_bindings,
    read_contract_input_assets,
)
from aigineering.core.output_satisfaction import (
    _is_contract_or_descendant,
    all_outputs_satisfied,
    is_business_output,
)
from aigineering.core.projection_store_view import ProjectionStoreView
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Contract


def satisfied_asset_ids(
    contract: Contract,
    output_name: str,
    context: EffectProjectionContext,
) -> tuple[str, ...]:
    """Return exact eligible Asset IDs satisfying one output slot."""
    view = ProjectionStoreView(context)
    slot = replace(contract, outputs=(output_name,))
    if not all_outputs_satisfied(slot, view):
        return ()
    qualified = {
        str(record.payload.get("asset_id", ""))
        for record in context.runtime_records
        if record.record_type == "output.qualified"
        and record.payload.get("contract_id") == contract.id
        and record.payload.get("output_name") == output_name
    }
    selected: set[str] = set()
    for asset in context.assets:
        if asset.name != output_name:
            continue
        if (
            contract.acceptance_policy is not None
            and contract.acceptance_policy.get("mode") == "independent"
        ):
            if (
                not is_business_output(asset, output_name)
                or asset.id not in qualified
                or not _is_contract_or_descendant(asset.created_by, contract.id, view)
            ):
                continue
        elif contract.origin in {"system", "recovery"}:
            if asset.created_by != contract.id:
                continue
        elif not is_business_output(asset, output_name):
            continue
        selected.add(asset.id)
    return tuple(sorted(selected))


def satisfied_output_slots(
    contract: Contract, context: EffectProjectionContext
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return satisfied declared slots and their exact qualifying Asset IDs."""
    return tuple(
        (name, ids)
        for name in contract.outputs
        if (ids := satisfied_asset_ids(contract, name, context))
    )


def remaining_output_slots(
    contract: Contract, context: EffectProjectionContext
) -> tuple[str, ...]:
    """Return declared slots without a qualifying exact business output."""
    satisfied = {name for name, _ in satisfied_output_slots(contract, context)}
    return tuple(name for name in contract.outputs if name not in satisfied)


def has_unfinished_descendants(
    source: Contract, context: EffectProjectionContext
) -> bool:
    """Whether any descendant lacks a durable terminal fact."""
    children_by_parent: dict[str, list[Contract]] = {}
    for contract in context.contracts:
        if contract.parent_id:
            children_by_parent.setdefault(contract.parent_id, []).append(contract)
    terminal_ids = {
        str(record.payload.get("contract_id", ""))
        for record in context.runtime_records
        if record.record_type == "lifecycle.terminal"
    }
    pending = list(children_by_parent.get(source.id, ()))
    visited: set[str] = set()
    while pending:
        child = pending.pop()
        if child.id in visited:
            continue
        visited.add(child.id)
        if child.id not in terminal_ids:
            return True
        pending.extend(children_by_parent.get(child.id, ()))
    return False


def build_successor_contract(
    source: Contract,
    outputs: tuple[str, ...],
    budget: int,
    reason: str,
    *,
    name_suffix: str,
    reason_label: str = "Task handoff reason",
    extra_context_asset_ids: tuple[str, ...] = (),
    extra_label_names: tuple[str, ...] = (),
    frozen_context_asset_ids: tuple[str, ...] | None = None,
    sensitive_input_policy: dict | None = None,
    use_source_sensitive_policy: bool = True,
    name_override: str | None = None,
    description_override: str | None = None,
) -> Contract:
    """Build a successor that copies immutable execution and acceptance scope.

    ``frozen_context_asset_ids`` is an opt-in for operations that deliberately
    snapshot name-resolved legacy labels. Omitting it preserves handoff's
    historical successor identity and disclosure fields.
    """
    context_ids = (
        tuple(source.context_asset_ids)
        if frozen_context_asset_ids is None
        else tuple(frozen_context_asset_ids)
    )
    context_ids = tuple(dict.fromkeys((*context_ids, *extra_context_asset_ids)))
    labels = tuple(dict.fromkeys((*source.labels, *extra_label_names)))
    name = f"{source.name or source.id}{name_suffix}"
    description = source.description + f"\n\n{reason_label}: {reason}"
    if name_override is not None:
        name = name_override
    if description_override is not None:
        description = description_override
    return contract_from_fields(
        name=name,
        description=description,
        parent_id=source.id,
        inputs=source.inputs,
        outputs=outputs,
        activation=source.activation,
        budget=budget,
        tool_scope=source.tool_scope,
        labels=labels,
        context_asset_ids=context_ids,
        worker_capabilities=source.worker_capabilities,
        worker_pools=source.worker_pools,
        delegation_capabilities=source.delegation_capabilities,
        delegation_pools=source.delegation_pools,
        origin=source.origin,
        minting_authority=source.minting_authority,
        sensitive_input_policy=(
            dict(source.sensitive_input_policy)
            if use_source_sensitive_policy and source.sensitive_input_policy is not None
            else sensitive_input_policy
        ),
        acceptance_policy=(
            dict(source.acceptance_policy)
            if source.acceptance_policy is not None
            else None
        ),
    )


def freeze_successor_disclosure(
    source: Contract,
    context: EffectProjectionContext,
) -> tuple[tuple[str, ...], tuple[str, ...], dict]:
    """Snapshot successor disclosure and bind each current input role by ID."""
    from aigineering.protocol.immutability import deep_thaw

    view = ProjectionStoreView(context)
    source_disclosure = compute_disclosure(source, view)
    source_disclosure_ids = tuple(sorted({asset.id for asset in source_disclosure}))
    frozen_inputs = input_asset_bindings(source.sensitive_input_policy)
    for role in source.inputs:
        role_contract = replace(source, inputs=(role,))
        role_assets = read_contract_input_assets(role_contract, view)
        frozen_inputs[role] = tuple(sorted({asset.id for asset in role_assets}))

    policy = (
        deep_thaw(source.sensitive_input_policy)
        if source.sensitive_input_policy is not None
        else {}
    )
    if not isinstance(policy, dict):
        raise ValueError("source sensitive input policy must be an object")
    policy["input_asset_bindings"] = {
        role: list(asset_ids) for role, asset_ids in frozen_inputs.items()
    }

    if source.id.startswith("task:v3:"):
        labels = set(source.labels)
        frozen_context = tuple(
            sorted(asset.id for asset in source_disclosure if asset.name in labels)
        )
    else:
        frozen_context = tuple(source.context_asset_ids)
    return frozen_context, source_disclosure_ids, policy


__all__ = [
    "ProjectionStoreView",
    "build_successor_contract",
    "freeze_successor_disclosure",
    "has_unfinished_descendants",
    "remaining_output_slots",
    "satisfied_asset_ids",
    "satisfied_output_slots",
]
