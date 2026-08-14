"""Disclosure function — determines what assets to reveal to a worker."""

from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping
from typing import Protocol

from aigineering.core.labels import BEHAVIOR_LABEL_PREFIX, is_behavior_asset_allowed
from aigineering.core.trust_policy import TrustPolicy
from aigineering.protocol.types import Asset, Contract

REDACTED_CONTENT = "[redacted]"


class StoreLike(Protocol):
    def get_all_assets(self) -> list[Asset]: ...
    def get_assets_by_name(self, name: str) -> list[Asset]: ...
    def get_asset(self, asset_id: str) -> Asset | None: ...
    def get_claims_for_replacement_asset(self, asset_id: str) -> list: ...


class DisclosurePolicyError(ValueError):
    """Raised before claim when sensitive inputs are not worker-disclosable."""

    def __init__(self, contract_id: str, reasons: list[str]) -> None:
        self.contract_id = contract_id
        self.reasons = tuple(reasons)
        super().__init__(
            f"contract {contract_id!r} disclosure policy rejected: "
            + "; ".join(reasons)
        )


def redact_for_disclosure(asset: Asset) -> Asset:
    """Return a redacted copy of *asset* when its disclosure_view is not 'original'.

    The returned copy has ``content == REDACTED_CONTENT`` but preserves
    the original ``id``, ``content_hash``, and ``signature`` — they identify
    the stored asset, not the disclosure view.
    """
    if asset.disclosure_view == "original":
        return asset
    return replace(asset, content=REDACTED_CONTENT)


def compute_disclosure(contract: Contract, store: StoreLike) -> list[Asset]:
    seen: set[str] = set()
    result: list[Asset] = []
    input_assets: list[Asset] = []

    for input_name in contract.inputs:
        for asset in store.get_assets_by_name(input_name):
            if not asset.promptable:
                continue
            if asset.id not in seen:
                seen.add(asset.id)
                input_assets.append(asset)
                result.append(asset)

    _enforce_sensitive_input_policy(contract, input_assets, store)

    if contract.id.startswith(("task:v4:", "task:v5:")):
        label_assets = tuple(
            asset
            for asset_id in contract.context_asset_ids
            if (asset := store.get_asset(asset_id)) is not None
        )
    else:
        # Historical v3 Contracts retain their original name-resolved behavior.
        label_assets = tuple(
            asset
            for label_name in contract.labels
            for asset in store.get_assets_by_name(label_name)
        )
    for asset in label_assets:
        if asset.name.startswith(BEHAVIOR_LABEL_PREFIX):
            if not is_behavior_asset_allowed(asset):
                continue
        if not asset.promptable:
            continue
        if asset.id not in seen:
            seen.add(asset.id)
            result.append(asset)

    return [redact_for_disclosure(asset) for asset in result]


def _enforce_sensitive_input_policy(
    contract: Contract, input_assets: list[Asset], store: StoreLike
) -> None:
    policy = contract.sensitive_input_policy
    if not isinstance(policy, Mapping) or not policy:
        return

    reasons: list[str] = []
    try:
        decision = TrustPolicy.from_config(dict(policy)).evaluate(
            input_assets, contract
        )
    except ValueError as exc:
        reasons.append(str(exc))
    else:
        reasons.extend(sorted(decision.reasons))

    required_defs = set(policy.get("required_definition_hashes", ()))
    observed_defs = {asset.definition_hash for asset in input_assets}
    for definition_hash in sorted(required_defs - observed_defs):
        reasons.append(
            f"required definition hash {definition_hash!r} is not among disclosed inputs"
        )

    accepted_claim_types = set(policy.get("accepted_claim_types", ()))
    if accepted_claim_types:
        from aigineering.core.verification import verify_replacement_claims
        from aigineering.protocol.types import ReplacementClaim

        unknown = accepted_claim_types - ReplacementClaim._VALID_CLAIM_TYPES
        for claim_type in sorted(unknown):
            reasons.append(f"accepted claim type {claim_type!r} is invalid")
        for asset in input_assets:
            claims = [
                claim
                for claim in store.get_claims_for_replacement_asset(asset.id)
                if claim.claim_type in accepted_claim_types
            ]
            verification = verify_replacement_claims(store, claims)
            if not any(item["valid"] for item in verification["results"]):
                reasons.append(
                    f"input asset {asset.id!r} has no verified incoming claim "
                    f"of type {sorted(accepted_claim_types)}"
                )

    if input_assets == [] and any(
        key in policy
        for key in (
            "minimum_trust_tier",
            "required_trust_tier",
            "allowed_signers",
            "required_signer",
            "required_definition_hashes",
            "accepted_claim_types",
        )
    ):
        reasons.append("sensitive input policy has no matching input assets")

    if reasons:
        raise DisclosurePolicyError(contract.id, reasons)
