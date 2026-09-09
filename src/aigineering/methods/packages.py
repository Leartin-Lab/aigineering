"""Exact method dependency resolution and pure Contract construction."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import re

from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.labels import BEHAVIOR_LABEL_PREFIX, is_behavior_asset_allowed
from aigineering.protocol.identity import canonical_json
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Asset, Contract
from aigineering.methods.schema import MethodPackage, canonical_package, parse_package

GetAsset = Callable[[str], Asset | None]
MEDIA_TYPE = "application/vnd.aigineering.method+json"


def visible_asset(asset_id: str, get_asset: GetAsset) -> Asset:
    asset = get_asset(asset_id)
    if asset is None or asset.id != asset_id:
        raise ValueError(f"missing exact Asset {asset_id!r}")
    if asset.tombstoned or asset.disclosure_view != "original" or not asset.promptable:
        raise ValueError(f"Asset {asset_id!r} is not available for method disclosure")
    if asset.name.startswith(BEHAVIOR_LABEL_PREFIX) and not is_behavior_asset_allowed(
        asset
    ):
        raise ValueError("behavior Asset trust is insufficient for method disclosure")
    return asset


def read_package(asset: Asset) -> MethodPackage:
    if asset.tombstoned or asset.disclosure_view != "original" or not asset.promptable:
        raise ValueError("method package is not available for disclosure")
    if asset.content_type not in {MEDIA_TYPE, "text", "text/plain", "application/json"}:
        raise ValueError("unsupported method package media type")
    return parse_package(asset.content)


def package_proposal(package: MethodPackage, *, name: str | None = None) -> Asset:
    return build_control_plane_asset(
        name=name or f"method.{package.name}.{package.version}",
        content=canonical_package(package),
        content_type=MEDIA_TYPE,
        origin="method-adapter",
        trust_tier="untrusted",
    )


def resolve_package(
    asset_id: str, get_asset: GetAsset
) -> tuple[MethodPackage, tuple[Asset, ...]]:
    """Resolve a bounded dependency closure without name-based substitution."""
    root = read_package(visible_asset(asset_id, get_asset))
    resolved: dict[str, Asset] = {}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(current_id: str) -> None:
        if current_id in visiting:
            raise ValueError("cyclic method dependency")
        if current_id in visited:
            return
        if len(visited) + len(visiting) >= 32:
            raise ValueError("method dependency closure exceeds 32 packages")
        visiting.add(current_id)
        asset = visible_asset(current_id, get_asset)
        package = read_package(asset)
        for field, values in package.requirements.items():
            if not set(values).issubset(root.requirements[field]):
                raise ValueError(f"dependency widens method requirement {field!r}")
        resolved[asset.id] = asset
        for context_id in package.context_asset_ids:
            context = visible_asset(context_id, get_asset)
            resolved[context.id] = context
        for dependency_id in package.dependencies:
            visit(dependency_id)
        visiting.remove(current_id)
        visited.add(current_id)
        if len(resolved) > 256:
            raise ValueError("method context closure exceeds 256 Assets")

    visit(asset_id)
    return root, tuple(resolved[key] for key in sorted(resolved))


def build_invocation(
    method_asset_id: str,
    *,
    inputs: Mapping[str, str],
    outputs: Mapping[str, str],
    get_asset: GetAsset,
    name: str,
    budget: int,
    allowed_tools: tuple[str, ...] = (),
    extra_context: tuple[str, ...] = (),
    evaluation_asset_id: str | None = None,
    provisional: bool = False,
) -> Contract:
    """Compile an explicitly authorized invocation; no proposal is committed here.

    Evaluation eligibility is checked by the lifecycle adapter before calling
    this constructor. The selected evidence and provisional decision are bound
    into Contract identity for audit, never inferred from package metadata.
    """
    package, closure = resolve_package(method_asset_id, get_asset)
    if set(inputs) != set(package.inputs) or set(outputs) != set(package.outputs):
        raise ValueError("method input/output bindings must match every declared slot")
    if len(set(outputs.values())) != len(outputs) or any(
        not isinstance(value, str)
        or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,127}", value)
        for value in outputs.values()
    ):
        raise ValueError("method outputs require unique ordinary names")
    if type(budget) is not int or budget < 1:
        raise ValueError("method budget must be a positive integer")
    tools = tuple(package.requirements["tool_scope"])
    if not set(tools).issubset(allowed_tools):
        raise ValueError("method requires tools outside the explicit invocation grant")
    if tools and budget < 2:
        raise ValueError(
            "tool-bearing method needs allowance for tool and continuation"
        )
    assets = {asset.id: asset for asset in closure}
    for asset_id in (*inputs.values(), *extra_context):
        assets[asset_id] = visible_asset(asset_id, get_asset)
    if evaluation_asset_id:
        assets[evaluation_asset_id] = visible_asset(evaluation_asset_id, get_asset)
    if len(assets) > 256:
        raise ValueError("method invocation context exceeds 256 Assets")
    if set(outputs.values()) & {asset.name for asset in assets.values()}:
        raise ValueError("method output cannot shadow its disclosed inputs")
    shapes = {
        outputs[slot]: deep_thaw(shape)
        for slot, shape in package.outputs.items()
        if shape is not None
    }
    binding = {
        "schema": "method-invocation-v1",
        "method_asset_id": method_asset_id,
        "inputs": dict(inputs),
        "outputs": dict(outputs),
        "evaluation_asset_id": evaluation_asset_id,
        "provisional": provisional,
    }
    return build_control_plane_contract(
        name=name,
        budget=budget,
        description=(
            f"Method: {package.name} {package.version}\n{package.description}\n\n"
            f"{package.instructions}\n\n"
            "Use only the exact input Asset IDs below. Publish each logical output "
            "under its bound output name. Dependencies are disclosed by exact ID.\n"
            + canonical_json(binding)
        ),
        outputs=tuple(outputs.values()),
        labels=tuple(sorted({asset.name for asset in assets.values()})),
        context_asset_ids=tuple(sorted(assets)),
        tool_scope=tools,
        worker_capabilities=tuple(package.requirements["worker_capabilities"]),
        worker_pools=tuple(package.requirements["worker_pools"]),
        delegation_capabilities=tuple(package.requirements["delegation_capabilities"]),
        delegation_pools=tuple(package.requirements["delegation_pools"]),
        acceptance_policy={"mode": "mechanical", "output_shapes": shapes}
        if shapes
        else None,
    )
