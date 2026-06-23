"""Declared-output satisfaction helpers.

This module is the single runtime definition for whether an asset can satisfy
a contract output.  Engine scheduling, reducer projection, recovery, and worker
submission must share this logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.protocol.types import Contract


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


def is_business_output(asset: object, declared_output_name: str) -> bool:
    """Return True when *asset* can satisfy a declared business output."""
    name: str = getattr(asset, "name", "")
    origin: str = getattr(asset, "origin", "")

    if name != declared_output_name:
        return False
    if origin in NON_OUTPUT_ORIGINS:
        return False
    return not any(name.startswith(prefix) for prefix in NON_OUTPUT_PREFIXES)


def all_outputs_satisfied(
    contract: Contract,
    store: StoreProtocol,
    *,
    extra_output_names: set[str] | None = None,
    require_outputs: bool = False,
) -> bool:
    """Return True when every declared output has a valid satisfying asset.

    ``extra_output_names`` treats just-projected output names as already
    available for callers that compute completion before committing a batch.
    ``require_outputs`` prevents output-less contracts from becoming terminal
    through the asset-satisfaction projection.
    """
    if require_outputs and not contract.outputs:
        return False

    extra_output_names = extra_output_names or set()
    for output_name in contract.outputs:
        if output_name in extra_output_names:
            continue
        matching = store.get_assets_by_name(output_name)
        if not matching:
            return False
        if contract.origin == "system":
            continue
        if not any(is_business_output(asset, output_name) for asset in matching):
            return False
    return True
