"""Control-plane ingress API for asset and contract injection.

Control-plane operations are human/administrator actions that create
runtime facts outside the worker candidate flow.  Every injection must
be traceable, signed, and subject to authority checks.

Protected runtime namespaces (``_sys_``, ``_tool_obs_``, ``_mcp_``,
``_skill_``, etc.) are rejected by default.  An explicit
``allow_protected=True`` flag (traced) is required to override.
"""

from __future__ import annotations

from dataclasses import replace
from aigineering.core.authority import _is_protected_name
from aigineering.core.ids import (
    hash_asset_definition,
    hash_asset_content,
    hash_contract_current,
)
from aigineering.protocol.types import Asset, Contract

# ---------------------------------------------------------------------------
# Asset injection
# ---------------------------------------------------------------------------


def build_control_plane_asset(
    *,
    name: str,
    content: str,
    origin: str = "human",
    trust_tier: str = "human",
    source_uri: str = "",
    promptable: bool = True,
    content_type: str = "text",
    allow_protected: bool = False,
) -> Asset:
    """Build a human-origin Asset proposal without committing it.

    Parameters
    ----------
    name : str
        Asset name (must not start with a protected prefix unless
        *allow_protected* is ``True``).
    content : str
        Asset content.
    origin : str
        Provenance origin (default ``"human"``).
    trust_tier : str
        Trust tier (default ``"human"`` for control-plane injection).
    source_uri : str
        Optional source reference.
    promptable : bool
        Whether the asset may be disclosed to workers.
    content_type : str
        Asset content type.
    allow_protected : bool
        Explicit override for protected-prefix names.  When ``True``
        the injection trace records that the override was used.
    This compatibility builder retains legacy metadata options while the
    Candidate reducer owns identity, provenance, and protected-name checks.
    """
    if _is_protected_name(name) and not allow_protected:
        raise ValueError(
            f"Asset name '{name}' uses a protected prefix. "
            f"Use allow_protected=True if intentional."
        )

    definition_hash = hash_asset_definition(name)
    content_hash = hash_asset_content(name, content)

    from aigineering.protocol.types import Asset

    asset = Asset(
        id=content_hash,
        name=name,
        content=content,
        content_type=content_type,
        origin=origin,
        trust_tier=trust_tier,
        source_uri=source_uri,
        promptable=promptable,
        definition_hash=definition_hash,
        content_hash=content_hash,
    )

    return asset


# ---------------------------------------------------------------------------
# Contract injection
# ---------------------------------------------------------------------------


def build_control_plane_contract(
    *,
    name: str,
    inputs: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (),
    activation: str = "",
    budget: int = 5,
    description: str = "",
    labels: tuple[str, ...] = (),
    context_asset_ids: tuple[str, ...] = (),
    tool_scope: tuple[str, ...] = (),
    worker_capabilities: tuple[str, ...] = (),
    worker_pools: tuple[str, ...] = (),
    delegation_capabilities: tuple[str, ...] = (),
    delegation_pools: tuple[str, ...] = (),
    sensitive_input_policy: dict | None = None,
    acceptance_policy: dict | None = None,
    allow_protected_outputs: bool = False,
) -> Contract:
    """Build and validate a human-origin Contract without committing it.

    Control-plane contracts are human/administrator-created work items.
    They are hashed, persisted, traced, and subject to authority checks
    — just like injected assets.

    Parameters
    ----------
    allow_protected_outputs : bool
        Deprecated fail-closed compatibility parameter. Control-plane work
        contracts never receive runtime minting authority; true is rejected.
    """
    from aigineering.protocol.types import Contract

    if not allow_protected_outputs:
        for output_name in outputs:
            if _is_protected_name(output_name):
                raise ValueError(
                    f"Contract output '{output_name}' uses a protected prefix; "
                    "control-plane work contracts cannot mint runtime names."
                )

    if allow_protected_outputs:
        raise ValueError(
            "control-plane work contracts cannot receive runtime minting authority"
        )

    policy = sensitive_input_policy if sensitive_input_policy is not None else None
    identity_fields = dict(
        name=name,
        description=description,
        inputs=inputs,
        outputs=outputs,
        activation=activation,
        budget=budget,
        tool_scope=tool_scope,
        labels=labels,
        origin="human",
        sensitive_input_policy=policy,
        acceptance_policy=acceptance_policy,
        worker_capabilities=worker_capabilities,
        worker_pools=worker_pools,
        delegation_capabilities=delegation_capabilities,
        delegation_pools=delegation_pools,
        context_asset_ids=context_asset_ids,
    )
    identity = hash_contract_current(**identity_fields)

    return Contract(
        id=identity,
        name=name,
        description=description,
        inputs=inputs,
        outputs=outputs,
        activation=activation,
        budget=budget,
        labels=labels,
        context_asset_ids=context_asset_ids,
        tool_scope=tool_scope,
        worker_capabilities=worker_capabilities,
        worker_pools=worker_pools,
        delegation_capabilities=delegation_capabilities,
        delegation_pools=delegation_pools,
        sensitive_input_policy=policy,
        acceptance_policy=acceptance_policy,
    )


def bind_contract_label_assets(contract: Contract, store) -> Contract:
    """Resolve label syntax once and bind exact Asset IDs into Contract identity."""
    asset_ids = tuple(
        sorted(
            {
                asset.id
                for label in contract.labels
                if not label.startswith("plugin:")
                for asset in store.get_assets_by_name(label)
                if asset.promptable
            }
        )
    )
    if not asset_ids:
        return contract
    identity = hash_contract_current(
        name=contract.name,
        description=contract.description,
        inputs=contract.inputs,
        outputs=contract.outputs,
        activation=contract.activation,
        budget=contract.budget,
        tool_scope=contract.tool_scope,
        labels=contract.labels,
        origin=contract.origin,
        parent_id=contract.parent_id,
        worker_capabilities=contract.worker_capabilities,
        worker_pools=contract.worker_pools,
        delegation_capabilities=contract.delegation_capabilities,
        delegation_pools=contract.delegation_pools,
        minting_authority=contract.minting_authority,
        sensitive_input_policy=contract.sensitive_input_policy,
        acceptance_policy=contract.acceptance_policy,
        context_asset_ids=asset_ids,
    )
    return replace(contract, id=identity, context_asset_ids=asset_ids)
