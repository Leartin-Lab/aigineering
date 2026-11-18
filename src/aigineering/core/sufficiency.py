"""Readiness and sufficiency diagnostic infrastructure (v0.3.12).

The report is a RECOMMENDATION, not a scheduler decision.
"""

from __future__ import annotations

import json
from typing import Any

from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.input_bindings import (
    InputAssetBindingIssue,
    input_asset_binding_issue,
    input_asset_bindings,
)
from aigineering.core.provenance import verify_asset_seal
from aigineering.core.trust_policy import TrustPolicy
from aigineering.protocol.types import Asset, Contract, TrustTier
from aigineering.core.store import StoreProtocol


def _trust_gap(asset: Asset) -> bool:
    """Return True when *asset* has a trust tier below OBSERVED (i.e. UNTRUSTED)."""
    try:
        tier = TrustTier.from_str(asset.trust_tier)
    except ValueError:
        tier = TrustTier.UNTRUSTED  # unknown tiers treated as untrusted
    return tier.value < TrustTier.OBSERVED.value


def _seal_gap(asset: Asset) -> bool:
    """Return True when *asset* has missing or invalid provenance signature."""
    if not asset.signed_by or not asset.provenance_seal:
        return True
    return not verify_asset_seal(asset)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_sufficiency(
    contract: Contract, store: StoreProtocol, trust_policy: TrustPolicy | None = None
) -> dict[str, Any]:
    """Check contract readiness. Returns a report dict.

    The report is a **recommendation** — it does NOT alter runtime state,
    schedule contracts, or make decisions.  Engines and planners consume
    the report as input to their own decision logic.
    """

    report: dict[str, Any] = {
        "contract_id": contract.id,
        "contract_name": contract.name,
        "missing_inputs": [],
        "stale_assets": [],
        "version_conflicts": [],
        "trust_gaps": [],
        "seal_gaps": [],
        "recommendation": "exec",
        "sufficiency_ok": True,
    }

    bindings = input_asset_bindings(contract.sensitive_input_policy)
    assets_by_definition: dict[str, dict[str, str]] = {}
    policy_gaps = []
    for role in dict.fromkeys(contract.inputs):
        if role in bindings:
            assets = []
            missing = not bindings[role]
            for asset_id in bindings[role]:
                asset = store.get_asset(asset_id)
                issue = input_asset_binding_issue(role, asset)
                if asset is None or issue is InputAssetBindingIssue.WRONG_ROLE:
                    missing = True
                else:
                    assets.append(asset)
        else:
            assets = store.get_assets_by_name(role)
            missing = not assets
            for asset in assets:
                if asset.definition_hash:
                    assets_by_definition.setdefault(asset.definition_hash, {})[
                        asset.id
                    ] = asset.name

        if missing:
            report["missing_inputs"].append(role)
        if any(asset.tombstoned for asset in assets):
            report["stale_assets"].append(role)
        if any(not asset.tombstoned and _trust_gap(asset) for asset in assets):
            report["trust_gaps"].append(role)
        if any(not asset.tombstoned and _seal_gap(asset) for asset in assets):
            report["seal_gaps"].append(role)
        if trust_policy is not None and assets:
            result = trust_policy.evaluate(assets, contract)
            if not result.accepted:
                policy_gaps.extend(
                    {"asset": role, "gap": "trust", "reason": reason}
                    for reason in result.reasons
                )

    report["trust_gaps"].extend(policy_gaps)
    for definition_hash, names_by_id in assets_by_definition.items():
        if len(names_by_id) > 1:
            report["version_conflicts"].append(
                {
                    "definition_hash": definition_hash,
                    "asset_ids": sorted(names_by_id),
                    "names": sorted(set(names_by_id.values())),
                }
            )

    # ── 6. Recommendation ─────────────────────────────────────────────────
    has_missing = len(report["missing_inputs"]) > 0
    has_stale = len(report["stale_assets"]) > 0
    has_conflicts = len(report["version_conflicts"]) > 0
    has_trust = len(report["trust_gaps"]) > 0
    has_sig = len(report["seal_gaps"]) > 0

    if has_missing:
        report["recommendation"] = "plan"
        report["sufficiency_ok"] = False
    elif has_stale or has_conflicts:
        report["recommendation"] = "replan"
        report["sufficiency_ok"] = False
    elif has_trust or has_sig:
        report["recommendation"] = "escalate"
        report["sufficiency_ok"] = False
    else:
        report["recommendation"] = "exec"
        report["sufficiency_ok"] = True

    return report


# ---------------------------------------------------------------------------
# Asset producer — sufficiency result as traceable system asset
# ---------------------------------------------------------------------------


def sufficiency_result_asset(
    contract: Contract,
    store: StoreProtocol,
) -> Asset:
    """Create a _sufficiency_result_ system asset from the report.

    The asset is content-addressed and traceable — it can be stored,
    queried, and audited like any other asset in the system.
    """
    report = check_sufficiency(contract, store)
    name = f"_sufficiency_result_{contract.id}"
    content = json.dumps(report, ensure_ascii=False, sort_keys=True)

    return Asset(
        id=hash_asset_content(name, content),
        name=name,
        content=content,
        content_type="application/json",
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, content),
        created_by=contract.id,
        origin="system",
        trust_tier="system",
        minted_by="engine",
        promptable=True,
        disclosure_view="original",
    )
