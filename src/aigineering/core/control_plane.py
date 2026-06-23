"""Control-plane ingress API for asset and contract injection.

Control-plane operations are human/administrator actions that create
runtime facts outside the worker candidate flow.  Every injection must
be traceable, signed, and subject to authority checks.

Protected runtime namespaces (``_sys_``, ``_tool_obs_``, ``_mcp_``,
``_skill_``, etc.) are rejected by default.  An explicit
``allow_protected=True`` flag (traced) is required to override.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aigineering.core.authority import RESERVED_PREFIXES
from aigineering.core.ids import (
    hash_asset_definition,
    hash_asset_content,
    hash_contract,
)

if TYPE_CHECKING:
    from aigineering.core.runtime_ingress import RuntimeIngress
    from aigineering.protocol.types import Asset, Contract

# ---------------------------------------------------------------------------
# Protected prefix enforcement
# ---------------------------------------------------------------------------

# Single source of truth: authority.py RESERVED_PREFIXES.
# The control plane enforces the same reserved prefix set as the authority
# gate so there is one canonical list of protected runtime namespaces.


def _is_protected_name(name: str) -> bool:
    """Return True when *name* starts with a protected prefix."""
    for prefix in RESERVED_PREFIXES:
        if name.startswith(prefix):
            return True
        # Also match the bare prefix form: "_sys_" should match "_sys"
        if prefix.endswith("_") and name == prefix.rstrip("_"):
            return True
    return False


# ---------------------------------------------------------------------------
# Asset injection
# ---------------------------------------------------------------------------


def inject_asset(
    store,
    trace_store,
    *,
    name: str,
    content: str,
    origin: str = "human",
    trust_tier: str = "human",
    source_uri: str = "",
    promptable: bool = True,
    content_type: str = "text",
    allow_protected: bool = False,
    ingress: RuntimeIngress,
) -> Asset:
    """Inject an asset through the control plane.

    Parameters
    ----------
    store : StoreProtocol
        Asset store for persistence (passed through to ingress).
    trace_store : TraceStoreProtocol
        Trace store for the injection audit record (passed through to ingress).
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
    ingress : RuntimeIngress
        Required ingress for asset persistence and tracing.

    Returns
    -------
    Asset
        The signed, persisted asset.

    Raises
    ------
    ValueError
        If *name* uses a protected prefix and *allow_protected* is
        ``False``.
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

    return ingress.accept_asset(
        asset, source="control_plane", allow_protected=allow_protected
    )


# ---------------------------------------------------------------------------
# Contract injection
# ---------------------------------------------------------------------------


def inject_contract(
    store,
    trace_store,
    *,
    name: str,
    inputs: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (),
    activation: str = "",
    budget: int = 5,
    description: str = "",
    labels: tuple[str, ...] = (),
    tool_scope: tuple[str, ...] = (),
    sensitive_input_policy: dict | None = None,
    allow_protected_outputs: bool = False,
    ingress: RuntimeIngress,
) -> Contract:
    """Inject a contract through the control plane.

    Control-plane contracts are human/administrator-created work items.
    They are hashed, persisted, traced, and subject to authority checks
    — just like injected assets.

    Parameters
    ----------
    allow_protected_outputs : bool
        When False (default), outputs starting with protected prefixes
        cause ValueError.  Set True only for system/admin use.
    ingress : RuntimeIngress
        Required ingress for contract persistence and tracing.

    Returns
    -------
    Contract
        The hashed, persisted contract.
    """
    from aigineering.protocol.types import Contract

    if not allow_protected_outputs:
        for output_name in outputs:
            if _is_protected_name(output_name):
                raise ValueError(
                    f"Contract output '{output_name}' uses a protected prefix. "
                    f"Use allow_protected_outputs=True if intentional."
                )

    # Build minting_authority for protected outputs when explicitly allowed
    minting_auth: tuple[str, ...] = ()
    if allow_protected_outputs:
        minting_auth = tuple(o for o in outputs if _is_protected_name(o))

    contract = Contract(
        id="",
        name=name,
        description=description,
        inputs=inputs,
        outputs=outputs,
        activation=activation,
        budget=budget,
        labels=labels,
        tool_scope=tool_scope,
        sensitive_input_policy=sensitive_input_policy or {},
        minting_authority=minting_auth,
    )
    hashed = Contract(
        id=hash_contract(
            name=contract.name,
            description=contract.description,
            inputs=list(contract.inputs),
            outputs=list(contract.outputs),
            activation=contract.activation,
            budget=contract.budget,
            tool_scope=list(contract.tool_scope),
            labels=list(contract.labels),
            origin=contract.origin,
        ),
        **{k: v for k, v in contract.__dict__.items() if k != "id"},
    )

    ingress.accept_contract(hashed)
    return hashed
