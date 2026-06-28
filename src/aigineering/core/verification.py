"""Batch verification and sensitive-input policy for v0.3.13."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.trust_policy import TrustPolicy
from aigineering.protocol.types import ReplacementClaim, TrustTier

if TYPE_CHECKING:
    from aigineering.core.store import StoreProtocol
    from aigineering.protocol.types import Contract


# ---------------------------------------------------------------------------
# Batch verification over asset definitions
# ---------------------------------------------------------------------------


def batch_verify_definition(store: StoreProtocol, def_hash: str) -> dict[str, Any]:
    """Verify all content hashes under a definition hash.

    Returns
    -------
    dict with keys:
        pass_count : int
        fail_count : int
        results    : list[dict] — each has asset_id, content_hash, valid
    """
    assets = store.get_assets_by_definition(def_hash)
    results: list[dict[str, Any]] = []
    pass_count = 0
    fail_count = 0

    for asset in assets:
        expected_content = hash_asset_content(asset.name, asset.content)
        expected_def = hash_asset_definition(asset.name)

        content_ok = asset.content_hash == expected_content
        def_ok = asset.definition_hash == expected_def
        valid = content_ok and def_ok

        if valid:
            pass_count += 1
        else:
            fail_count += 1

        results.append(
            {
                "asset_id": asset.id,
                "content_hash": asset.content_hash,
                "expected_content_hash": expected_content,
                "definition_hash": asset.definition_hash,
                "expected_definition_hash": expected_def,
                "valid": valid,
            }
        )

    return {
        "pass_count": pass_count,
        "fail_count": fail_count,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Replacement claim verification
# ---------------------------------------------------------------------------


def verify_replacement_claims(
    store: StoreProtocol, claims: list[ReplacementClaim]
) -> dict[str, Any]:
    """Verify replacement/equivalence claims.

    For each claim, checks that the source and replacement assets exist in the
    store and that they share the same definition hash.

    Returns
    -------
    dict with keys:
        pass_count : int
        fail_count : int
        results    : list[dict]
    """
    results: list[dict[str, Any]] = []
    pass_count = 0
    fail_count = 0

    for claim in claims:
        source = store.get_asset(claim.source_asset_id)
        replacement = store.get_asset(claim.replacement_asset_id)

        issues: list[str] = []

        if source is None:
            issues.append(f"source asset {claim.source_asset_id} not found")
        if replacement is None:
            issues.append(f"replacement asset {claim.replacement_asset_id} not found")

        if source is not None and replacement is not None:
            if source.definition_hash != replacement.definition_hash:
                issues.append(
                    f"definition hash mismatch: "
                    f"source={source.definition_hash} vs replacement={replacement.definition_hash}"
                )
            if source.definition_hash != claim.definition_hash:
                issues.append(
                    f"claim definition_hash {claim.definition_hash} does not match "
                    f"assets' definition_hash {source.definition_hash}"
                )

            # Verify content hashes on both assets
            for label, asset in [("source", source), ("replacement", replacement)]:
                expected = hash_asset_content(asset.name, asset.content)
                if asset.content_hash != expected:
                    issues.append(
                        f"{label} asset {asset.id} content_hash mismatch: "
                        f"stored={asset.content_hash} expected={expected}"
                    )

        valid = len(issues) == 0
        if valid:
            pass_count += 1
        else:
            fail_count += 1

        results.append(
            {
                "claim_id": claim.id,
                "claim_type": claim.claim_type,
                "source_asset_id": claim.source_asset_id,
                "replacement_asset_id": claim.replacement_asset_id,
                "valid": valid,
                "issues": issues,
            }
        )

    return {
        "pass_count": pass_count,
        "fail_count": fail_count,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Sensitive input policy
# ---------------------------------------------------------------------------


def check_sensitive_input_policy(
    contract: Contract,
    store: StoreProtocol,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check if contract's sensitive inputs meet policy requirements.

    The policy dict may contain:
      - required_definition_hashes: list of ``def:<hash>`` that must be present
      - accepted_claim_types: list of claim types (replacement, slice, etc.)
      - required_signer: signer that must have signed
      - required_trust_tier: minimum trust tier

    When *policy* is None, the contract's ``sensitive_input_policy`` attribute
    (if any) is used.

    Returns
    -------
    dict with keys:
        compliant  : bool
        violations : list[str]
    """
    effective_policy = policy
    if effective_policy is None:
        effective_policy = getattr(contract, "sensitive_input_policy", None)
    if effective_policy is None or not isinstance(effective_policy, (dict, Mapping)):
        return {"compliant": True, "violations": []}

    violations: list[str] = []

    # --- Required definition hashes ---
    required_defs: list[str] = effective_policy.get("required_definition_hashes", [])
    for def_hash in required_defs:
        assets = store.get_assets_by_definition(def_hash)
        if not assets:
            violations.append(
                f"required definition hash {def_hash} has no assets in store"
            )

    # --- Accepted claim types ---
    accepted_types: list[str] = effective_policy.get("accepted_claim_types", [])
    if accepted_types:
        valid_types = frozenset(ReplacementClaim._VALID_CLAIM_TYPES)  # type: ignore[attr-defined]
        for ct in accepted_types:
            if ct not in valid_types:
                violations.append(
                    f"accepted_claim_type '{ct}' is not a valid claim type; "
                    f"valid types: {sorted(valid_types)}"
                )

    # --- Delegate tier and signer checks to TrustPolicy ---
    try:
        trust_policy = TrustPolicy.from_config(effective_policy)
    except ValueError:
        # Invalid tier name → flag and skip tier/signer enforcement
        violations.append(
            f"required_trust_tier '{effective_policy.get('required_trust_tier', '')}' "
            f"is not a recognized tier"
        )
    else:
        input_assets = _collect_input_assets(contract, store) if contract.inputs else []
        if not input_assets:
            # Policy requires signer/tier but contract has no input assets
            if effective_policy.get("required_signer"):
                violations.append(
                    f"required_signer '{effective_policy['required_signer']}' "
                    f"has not signed any input asset of contract '{contract.id}'"
                )
            if effective_policy.get("required_trust_tier"):
                violations.append(
                    f"no input asset of contract '{contract.id}' meets "
                    f"required_trust_tier '{effective_policy['required_trust_tier']}'"
                )
        else:
            # Existential semantics: at least ONE input asset must satisfy
            # signer and tier requirements (matching pre-TrustPolicy behaviour).
            if trust_policy.allowed_signers is not None:
                if not any(
                    a.signed_by in trust_policy.allowed_signers for a in input_assets
                ):
                    violations.append(
                        f"required_signer '{effective_policy.get('required_signer', '')}' "
                        f"has not signed any input asset of contract '{contract.id}'"
                    )
            if trust_policy.minimum_trust_tier is not None:
                sufficient = False
                for a in input_assets:
                    try:
                        t = TrustTier.from_str(a.trust_tier)
                    except ValueError:
                        continue
                    if t.value >= trust_policy.minimum_trust_tier.value:
                        sufficient = True
                        break
                if not sufficient:
                    violations.append(
                        f"no input asset of contract '{contract.id}' meets required_trust_tier "
                        f"'{effective_policy.get('required_trust_tier', '')}'"
                        f" (minimum rank {trust_policy.minimum_trust_tier.value})"
                    )

    return {
        "compliant": len(violations) == 0,
        "violations": violations,
    }


def _collect_input_assets(contract: Contract, store: Any) -> list[Any]:
    """Collect all assets that are contract inputs (not all store assets)."""
    result: list[Any] = []
    for input_name in contract.inputs:
        result.extend(store.get_assets_by_name(input_name))
    return result
