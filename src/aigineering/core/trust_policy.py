"""Unified trust policy engine for the Aigineering agent runtime.

Consolidates trust checks scattered across verification, sufficiency,
capability descriptors, and authority modules into a single declarative policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from aigineering.protocol.types import TrustTier

if TYPE_CHECKING:
    from aigineering.protocol.types import Asset, Contract


@dataclass(frozen=True)
class TrustDecision:
    """Result of a TrustPolicy evaluation.

    Attributes:
        accepted: True if all evaluated dimensions pass.
        reasons: Human-readable reasons for rejection (empty if accepted).
    """
    accepted: bool
    reasons: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def accept(cls) -> "TrustDecision":
        return cls(accepted=True)

    @classmethod
    def reject(cls, *reasons: str) -> "TrustDecision":
        return cls(accepted=False, reasons=frozenset(reasons))


@dataclass(frozen=True)
class TrustPolicy:
    """Declarative trust policy for evaluating assets against trust criteria.

    All fields are optional (None means "don't enforce this dimension").
    An empty policy (all None) accepts everything.

    Fields:
        minimum_trust_tier: Assets must be at or above this tier (TrustTier).
        allowed_signers: Only assets signed by these signers are accepted.
        allowed_origins: Only assets with these origins are accepted.
        required_labels: Assets must carry ALL these labels.
        allowed_tool_scope: Only tools in this scope are allowed.
        reserved_prefixes: Asset names starting with these prefixes are rejected.
    """
    minimum_trust_tier: Optional[TrustTier] = None
    allowed_signers: Optional[frozenset[str]] = None
    allowed_origins: Optional[frozenset[str]] = None
    required_labels: Optional[frozenset[str]] = None
    allowed_tool_scope: Optional[frozenset[str]] = None
    reserved_prefixes: Optional[frozenset[str]] = None

    def evaluate(self, assets: list[Asset], contract: Optional[Contract] = None) -> TrustDecision:
        """Evaluate a list of assets against this policy.

        Returns TrustDecision.accept() if all checks pass, or
        TrustDecision.reject(...) with reasons for each failure.
        """
        reasons: list[str] = []

        for asset in assets:
            # --- Trust tier check ---
            if self.minimum_trust_tier is not None:
                try:
                    asset_tier = TrustTier.from_str(asset.trust_tier)
                except ValueError:
                    reasons.append(
                        f"asset '{asset.name}' has unrecognized trust_tier "
                        f"'{asset.trust_tier}'"
                    )
                else:
                    if asset_tier.value < self.minimum_trust_tier.value:
                        reasons.append(
                            f"asset '{asset.name}' trust_tier '{asset.trust_tier}' "
                            f"(rank {asset_tier.value}) is below minimum "
                            f"'{self.minimum_trust_tier}' "
                            f"(rank {self.minimum_trust_tier.value})"
                        )

            # --- Signer check ---
            if self.allowed_signers is not None:
                if asset.signed_by not in self.allowed_signers:
                    reasons.append(
                        f"asset '{asset.name}' signed_by '{asset.signed_by}' "
                        f"not in allowed signers"
                    )

            # --- Origin check ---
            if self.allowed_origins is not None:
                if asset.origin not in self.allowed_origins:
                    reasons.append(
                        f"asset '{asset.name}' origin '{asset.origin}' "
                        f"not in allowed origins"
                    )

            # --- Label check (contract-scoped) ---
            # Labels live on contracts, not assets.  This check is
            # contract-level and runs once after the asset loop.
            pass  # see contract-level check below

            # --- Reserved prefix check ---
            if self.reserved_prefixes is not None:
                for prefix in self.reserved_prefixes:
                    if asset.name.startswith(prefix):
                        reasons.append(
                            f"asset '{asset.name}' uses reserved prefix '{prefix}'"
                        )
                        break

        # --- Label check (contract-level) ---
        if self.required_labels is not None and contract is not None:
            contract_labels = set(getattr(contract, 'labels', ()))
            missing = self.required_labels - contract_labels
            if missing:
                reasons.append(
                    f"contract '{contract.name}' missing required labels: "
                    f"{sorted(missing)}"
                )

        # --- Tool scope check (contract-level) ---
        if self.allowed_tool_scope is not None and contract is not None:
            contract_scope = getattr(contract, 'tool_scope', None)
            if contract_scope:
                if not set(contract_scope).issubset(self.allowed_tool_scope):
                    extra = set(contract_scope) - self.allowed_tool_scope
                    reasons.append(
                        f"contract '{contract.name}' tool_scope contains "
                        f"disallowed tools: {sorted(extra)}"
                    )

        if reasons:
            return TrustDecision.reject(*reasons)
        return TrustDecision.accept()

    @classmethod
    def from_config(cls, config: dict) -> "TrustPolicy":
        """Create a TrustPolicy from a configuration dictionary.

        All keys are optional. Missing keys → None (don't enforce).

        Supported keys:
            minimum_trust_tier: str — resolved via TrustTier.from_str()
            allowed_signers: list[str]
            allowed_origins: list[str]
            required_labels: list[str]
            allowed_tool_scope: list[str]
            reserved_prefixes: list[str]

        Legacy keys (backward compat with sensitive_input_policy):
            required_trust_tier: str — mapped to minimum_trust_tier
            required_signer: str — mapped to single-element allowed_signers
        """
        # Normalize legacy keys to canonical keys
        normalized: dict = dict(config)
        if "required_trust_tier" in normalized and "minimum_trust_tier" not in normalized:
            normalized["minimum_trust_tier"] = normalized.pop("required_trust_tier")
        if "required_signer" in normalized and "allowed_signers" not in normalized:
            normalized["allowed_signers"] = [normalized.pop("required_signer")]

        kwargs: dict = {}

        if "minimum_trust_tier" in normalized:
            kwargs["minimum_trust_tier"] = TrustTier.from_str(normalized["minimum_trust_tier"])

        for field_name in ("allowed_signers", "allowed_origins", "required_labels",
                           "allowed_tool_scope", "reserved_prefixes"):
            if field_name in normalized:
                value = normalized[field_name]
                if isinstance(value, str):
                    kwargs[field_name] = frozenset([value])
                else:
                    kwargs[field_name] = frozenset(value)

        return cls(**kwargs)
