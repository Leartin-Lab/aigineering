"""Readiness and sufficiency diagnostic infrastructure (v0.3.12).

The report is a RECOMMENDATION, not a scheduler decision.
"""

from __future__ import annotations

import json
from typing import Any

from aigineering.core.ids import hash_asset_content, hash_asset_definition
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


def check_sufficiency(contract: Contract, store: StoreProtocol, trust_policy: TrustPolicy | None = None) -> dict[str, Any]:
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

    # ── 1. Missing inputs ─────────────────────────────────────────────────
    for input_name in contract.inputs:
        if not store.has_asset_named(input_name):
            report["missing_inputs"].append(input_name)

    # ── 2. Stale / tombstoned assets ──────────────────────────────────────
    for input_name in contract.inputs:
        if store.has_asset_named(input_name):
            for asset in store.get_assets_by_name(input_name):
                if asset.tombstoned:
                    if input_name not in report["stale_assets"]:
                        report["stale_assets"].append(input_name)

    # ── 3. Version conflicts (multiple versions under same def_hash) ──────
    seen_def_hashes: dict[str, list[str]] = {}
    for input_name in contract.inputs:
        for asset in store.get_assets_by_name(input_name):
            if asset.definition_hash:
                seen_def_hashes.setdefault(asset.definition_hash, []).append(asset.id)
    for def_hash, asset_ids in seen_def_hashes.items():
        unique_ids = list(set(asset_ids))
        if len(unique_ids) > 1:
            # Find the names that contributed to this conflict
            conflicting_names: list[str] = []
            for aid in unique_ids:
                a = store.get_asset(aid)
                if a is not None and a.name not in conflicting_names:
                    conflicting_names.append(a.name)
            report["version_conflicts"].append(
                {
                    "definition_hash": def_hash,
                    "asset_ids": sorted(unique_ids),
                    "names": sorted(conflicting_names),
                }
            )

    # ── 4. Trust gaps ─────────────────────────────────────────────────────
    for input_name in contract.inputs:
        for asset in store.get_assets_by_name(input_name):
            if _trust_gap(asset) and not asset.tombstoned:
                if input_name not in report["trust_gaps"]:
                    report["trust_gaps"].append(input_name)

    # ── 4b. Trust policy enrichment (when policy provided) ────────────────
    if trust_policy is not None:
        for input_name in contract.inputs:
            assets = store.get_assets_by_name(input_name)
            if not assets:
                continue
            trust_result = trust_policy.evaluate(assets, contract)
            if not trust_result.accepted:
                for reason in trust_result.reasons:
                    report["trust_gaps"].append(
                        {"asset": input_name, "gap": "trust", "reason": reason}
                    )

    # ── 5. Signature gaps ─────────────────────────────────────────────────
    for input_name in contract.inputs:
        for asset in store.get_assets_by_name(input_name):
            if _seal_gap(asset) and not asset.tombstoned:
                if input_name not in report["seal_gaps"]:
                    report["seal_gaps"].append(input_name)

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
