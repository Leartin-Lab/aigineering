"""Declared-output satisfaction helpers.

This module is the single runtime definition for whether an asset can satisfy
a contract output.  Ready projection, reducers, recovery, and worker
submission must share this logic.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import TYPE_CHECKING

from aigineering.core.authority import matched_reserved_prefix

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.protocol.types import Asset, Contract


# Origins that indicate context/observation assets, not business outputs.
NON_OUTPUT_ORIGINS: frozenset[str] = frozenset({"tool", "mcp", "label_placeholder"})

# Name prefixes that indicate context/observation assets, not business outputs.
# NOTE: _plan_result_, _replan_result_, _fail_result_, _sufficiency_result_
# are intentionally excluded: they are legitimate outputs of system method
# contracts even though ordinary workers should never mint them directly.
NON_OUTPUT_PREFIXES: tuple[str, ...] = (
    "_tool_obs_",
    "_tool_call_",
    "_mcp_obs_",
    "_mcp_call_",
    "_method_ctx_",
    "_fail_context_",
    "_fail_report_",
    "_label_missing_",
)


class CompletionConditionConflict(ValueError):
    """A fresh completion fact has no satisfying declared Asset conditions."""


_OUTPUT_REQUIREMENT_SELECTORS = frozenset(
    {"definition_id", "content_id", "producer_actor_ids"}
)
_GRAPH_ID_PATTERNS = {
    "definition_id": re.compile(r"definition:v1:[0-9a-f]{64}\Z"),
    "content_id": re.compile(r"content:v1:[0-9a-f]{64}\Z"),
}


def validate_output_requirements(contract: Contract) -> None:
    """Validate the small, exact selector map attached to output roles."""
    policy = contract.acceptance_policy
    if policy is None:
        return
    if not isinstance(policy, Mapping):
        raise ValueError("Contract acceptance_policy must be an object")
    if "output_requirements" not in policy:
        return
    requirements = policy["output_requirements"]
    if not isinstance(requirements, Mapping):
        raise ValueError("acceptance_policy.output_requirements must be an object")
    declared = set(contract.outputs)
    for output_name, selectors in requirements.items():
        if not isinstance(output_name, str) or output_name not in declared:
            raise ValueError(
                "acceptance_policy.output_requirements keys must be declared outputs"
            )
        if not isinstance(selectors, Mapping):
            raise ValueError(
                f"output requirement for {output_name!r} must be an object"
            )
        _validate_output_requirement_selectors(output_name, selectors)


def _validate_output_requirement_selectors(
    output_name: str, selectors: Mapping
) -> None:
    unknown = set(selectors) - _OUTPUT_REQUIREMENT_SELECTORS
    if unknown:
        raise ValueError(
            f"output requirement for {output_name!r} has unknown selectors: "
            f"{sorted(map(str, unknown))}"
        )
    if not selectors:
        raise ValueError(
            f"output requirement for {output_name!r} needs at least one selector"
        )
    for field, pattern in _GRAPH_ID_PATTERNS.items():
        if field not in selectors:
            continue
        value = selectors[field]
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ValueError(
                f"output requirement {output_name!r}.{field} must be an exact "
                f"{field.removesuffix('_id')}:v1 identity"
            )
    if "producer_actor_ids" in selectors:
        actor_ids = selectors["producer_actor_ids"]
        if (
            not isinstance(actor_ids, (list, tuple))
            or not actor_ids
            or any(
                not isinstance(actor_id, str)
                or not actor_id
                or actor_id != actor_id.strip()
                for actor_id in actor_ids
            )
            or tuple(actor_ids) != tuple(sorted(set(actor_ids)))
        ):
            raise ValueError(
                f"output requirement {output_name!r}.producer_actor_ids must be "
                "non-empty, sorted, unique Actor ID strings"
            )


def is_business_output(asset: object, declared_output_name: str) -> bool:
    """Return True when *asset* can satisfy a declared business output."""
    name: str = getattr(asset, "name", "")
    origin: str = getattr(asset, "origin", "")

    if name != declared_output_name:
        return False
    if origin in NON_OUTPUT_ORIGINS:
        return False
    return not any(name.startswith(prefix) for prefix in NON_OUTPUT_PREFIXES)


def output_asset_is_eligible(
    contract: Contract,
    output_name: str,
    asset: Asset,
    store: StoreProtocol,
    *,
    qualified_asset_ids: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Return whether one exact Asset can satisfy this Contract output slot."""
    if not is_output_role_asset(asset, output_name):
        return False

    policy = contract.acceptance_policy
    if not output_asset_meets_conditions(contract, output_name, asset):
        return False
    if isinstance(policy, Mapping) and policy.get("mode") == "independent":
        return asset.id in qualified_asset_ids
    return True


def is_output_role_asset(asset: Asset, output_name: str) -> bool:
    """Keep observation roles distinct without giving task origins authority.

    An explicit reserved observation role can be required by an ordinary task.
    Publication still enforces its namespace authority. An observation cannot
    masquerade as a business output just by sharing its name.
    """
    return is_business_output(asset, output_name) or (
        asset.name == output_name
        and asset.origin != "label_placeholder"
        and matched_reserved_prefix(output_name) is not None
    )


def output_asset_meets_conditions(
    contract: Contract, output_name: str, asset: Asset
) -> bool:
    """Match an admitted Asset to the exact conditions declared for one role.

    This pure matcher checks declared shape and identity conditions but reads no
    Store and verifies no publication signatures. Callers supply an Asset
    already admitted by the normal runtime path; producer identity is its
    authenticated signer metadata.
    """
    if getattr(asset, "name", None) != output_name:
        return False
    policy = contract.acceptance_policy
    if not isinstance(policy, Mapping):
        return True
    if "output_shapes" in policy:
        from aigineering.core.acceptance import validate_output_shape

        try:
            validate_output_shape(policy, output_name, asset.content)
        except ValueError:
            return False
    requirements = policy.get("output_requirements", {})
    if not isinstance(requirements, Mapping):
        return False
    selectors = requirements.get(output_name)
    if selectors is None:
        return True
    if not isinstance(selectors, Mapping):
        return False
    try:
        _validate_output_requirement_selectors(output_name, selectors)
    except (TypeError, ValueError):
        return False
    definition_id = selectors.get("definition_id")
    if definition_id is not None and asset.definition_hash != definition_id:
        return False
    content_id = selectors.get("content_id")
    if content_id is not None and asset.content_hash != content_id:
        return False
    producer_actor_ids = selectors.get("producer_actor_ids")
    if producer_actor_ids is not None and asset.signed_by not in producer_actor_ids:
        return False
    return True


def unmet_output_names(
    contract: Contract,
    store: StoreProtocol,
    *,
    pending_assets: tuple[Asset, ...] = (),
    extra_qualified_asset_ids: Mapping[tuple[str, str], set[str]] | None = None,
) -> tuple[str, ...]:
    """Return declared output names without a matching admitted Asset condition."""
    return _unmet_output_names(
        contract,
        store,
        pending_assets=pending_assets,
        extra_qualified_asset_ids=extra_qualified_asset_ids,
    )


def all_outputs_satisfied(
    contract: Contract,
    store: StoreProtocol,
    *,
    extra_output_names: set[str] | None = None,
    pending_assets: tuple[Asset, ...] = (),
    require_outputs: bool = False,
    extra_qualified_asset_ids: Mapping[tuple[str, str], set[str]] | None = None,
) -> bool:
    """Return True when every declared output has a valid satisfying asset.

    ``extra_output_names`` treats just-projected output names as already
    available for callers that compute completion before committing a batch.
    ``pending_assets`` applies the normal output policy to actual Assets that
    have been projected in the current batch but are not yet stored.
    ``require_outputs`` prevents output-less contracts from becoming terminal
    through the asset-satisfaction projection.
    """
    if require_outputs and not contract.outputs:
        return False
    return not _unmet_output_names(
        contract,
        store,
        extra_output_names=extra_output_names or set(),
        pending_assets=pending_assets,
        extra_qualified_asset_ids=extra_qualified_asset_ids,
    )


def _unmet_output_names(
    contract: Contract,
    store: StoreProtocol,
    *,
    extra_output_names: set[str] | None = None,
    pending_assets: tuple[Asset, ...] = (),
    extra_qualified_asset_ids: Mapping[tuple[str, str], set[str]] | None = None,
) -> tuple[str, ...]:
    extra_output_names = extra_output_names or set()
    extra_qualified_asset_ids = extra_qualified_asset_ids or {}
    policy = contract.acceptance_policy
    try:
        validate_output_requirements(contract)
    except (TypeError, ValueError):
        return tuple(contract.outputs)
    requirements = (
        policy.get("output_requirements", {}) if isinstance(policy, Mapping) else {}
    )
    qualified_by_output: dict[str, set[str]] = {}
    independent = isinstance(policy, Mapping) and policy.get("mode") == "independent"
    if independent:
        for _, record in store.scan_runtime_records(record_type="output.qualified"):
            if record.payload.get("contract_id") == contract.id:
                output_name = record.payload.get("output_name")
                if isinstance(output_name, str):
                    qualified_by_output.setdefault(output_name, set()).add(
                        str(record.payload.get("asset_id", ""))
                    )

    unmet: list[str] = []
    for output_name in contract.outputs:
        if independent:
            qualified_ids = set(qualified_by_output.get(output_name, ()))
            qualified_ids.update(
                extra_qualified_asset_ids.get((contract.id, output_name), set())
            )
            if not qualified_ids:
                unmet.append(output_name)
                continue
        else:
            qualified_ids = set()

        selectors = (
            requirements.get(output_name) if isinstance(requirements, Mapping) else None
        )
        definition_id = (
            selectors.get("definition_id") if isinstance(selectors, Mapping) else None
        )
        if definition_id is not None:
            get_by_definition = getattr(store, "get_assets_by_definition", None)
            committed = (
                get_by_definition(definition_id)
                if callable(get_by_definition)
                else store.get_assets_by_name(output_name)
            )
        else:
            committed = store.get_assets_by_name(output_name)
        matching = (
            *committed,
            *(asset for asset in pending_assets if asset.name == output_name),
        )
        if any(
            output_asset_is_eligible(
                contract,
                output_name,
                asset,
                store,
                qualified_asset_ids=qualified_ids,
            )
            for asset in matching
        ):
            continue

        has_explicit_requirement = (
            isinstance(requirements, Mapping) and output_name in requirements
        )
        output_shapes = (
            policy.get("output_shapes", {}) if isinstance(policy, Mapping) else {}
        )
        has_shape_requirement = (
            isinstance(output_shapes, Mapping) and output_name in output_shapes
        )
        if (
            output_name in extra_output_names
            and not has_explicit_requirement
            and not has_shape_requirement
            and not independent
        ):
            continue
        unmet.append(output_name)
    return tuple(unmet)


def _is_contract_or_descendant(producer_id: str, contract_id: str, store) -> bool:
    """Return whether immutable Contract ancestry binds producer to owner."""
    current_id = producer_id
    visited: set[str] = set()
    while current_id and current_id not in visited:
        if current_id == contract_id:
            return True
        visited.add(current_id)
        current = store.get_contract(current_id)
        current_id = str(current.parent_id or "") if current is not None else ""
    return False
