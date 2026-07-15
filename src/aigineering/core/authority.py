"""Authority checker — the commitment boundary."""

from __future__ import annotations

from aigineering.protocol.types import Contract

from aigineering.core.trust_policy import TrustPolicy

RESERVED_PREFIXES: frozenset[str] = frozenset(
    {
        "_sys_",
        "_skill_",
        "_memory_",
        "_soul_",
        "_mcp_",
        "_tool_obs_",
        "_tool_call_",
        "_tool_capability_",
        "_plan_result_",
        "_replan_result_",
        "_fail_result_",
        "_fail_context_",
        "_sufficiency_result_",
        "_file_content_",
        "_method_ctx_",
        "_context_overflow_report_",
        "_label_missing_",
        "_provider_config_",
        "_replan_report_",
        "_fail_report_",
        "_retry_",
        "_persona_",
        "_north_star_",
    }
)


def matched_reserved_prefix(
    name: str, prefixes: frozenset[str] = RESERVED_PREFIXES
) -> str | None:
    """Return the canonical reserved prefix matched by *name*, if any."""
    for prefix in prefixes:
        if name.startswith(prefix):
            return prefix
        if prefix.endswith("_") and name == prefix.rstrip("_"):
            return prefix
    return None


def _is_protected_name(name: str) -> bool:
    return matched_reserved_prefix(name) is not None


class ReservedNamespaceError(ValueError):
    """Raised when an asset name collides with a reserved runtime prefix."""

    def __init__(self, name: str, prefix: str) -> None:
        self.name = name
        self.prefix = prefix
        super().__init__(
            f"Asset name {name!r} collides with reserved prefix {prefix!r}"
        )


def check_authority(
    contract: Contract,
    candidate_assets: list[dict],
    trust_policy: TrustPolicy | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Check whether each candidate asset is within the contract's declared
    outputs and does not collide with a reserved name prefix.

    Returns
    -------
    (accepted, rejected, authority_policy)

    accepted : list[dict]
        Each dict has ``name`` and ``content``.
    rejected : list[dict]
        Each dict has ``name``, ``content``, ``reject_reason``, and
        ``category`` (one of ``"protected_name_rejection"`` or
        ``"authority_rejection"``).
    authority_policy : dict
        ``{"declared_outputs": contract.outputs,
        "reserved_prefixes": sorted(RESERVED_PREFIXES)}``.
    """
    accepted: list[dict] = []
    rejected: list[dict] = []

    if trust_policy is not None and trust_policy.reserved_prefixes is not None:
        prefixes = trust_policy.reserved_prefixes | RESERVED_PREFIXES
    else:
        prefixes = RESERVED_PREFIXES

    for candidate in candidate_assets:
        name: str = candidate["name"]
        reasons: list[str] = []
        category: str | None = None

        if name not in contract.outputs:
            reasons.append(
                f"asset '{name}' is not in contract.outputs ({contract.outputs!r})"
            )
            category = "authority_rejection"

        for prefix in prefixes:
            if name.startswith(prefix) and name not in contract.minting_authority:
                reasons.append(
                    f"asset '{name}' starts with reserved prefix '{prefix}'"
                    f" and is not in contract.minting_authority"
                )
                category = "protected_name_rejection"
                break

        if reasons:
            rejected.append(
                {
                    "name": name,
                    "content": candidate["content"],
                    "reject_reason": "; ".join(reasons),
                    "category": category,
                }
            )
        else:
            accepted.append({"name": name, "content": candidate["content"]})

    authority_policy: dict = {
        "declared_outputs": contract.outputs,
        "reserved_prefixes": sorted(prefixes),
    }

    return accepted, rejected, authority_policy
