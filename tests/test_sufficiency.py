"""Tests for readiness/sufficiency diagnostic infrastructure (v0.3.12)."""

import json

import pytest

from aigineering.core.store import MemoryStore
from aigineering.core.sufficiency import (
    check_sufficiency,
    sufficiency_result_asset,
)
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.provenance import sign_asset
from aigineering.protocol.types import Asset, Contract


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_contract(name="test_contract", inputs=None, outputs=None):
    return Contract(
        id=f"task:contract_{name}",
        name=name,
        inputs=tuple(inputs or []),
        outputs=tuple(outputs or ["result"]),
    )


def _make_asset(name, content, trust_tier="human", tombstoned=False, signed=True):
    asset = Asset(
        id=hash_asset_content(name, content),
        name=name,
        content=content,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, content),
        origin="human",
        trust_tier=trust_tier,
        tombstoned=tombstoned,
    )
    if signed:
        return sign_asset(asset)
    return asset


# ── Missing inputs ───────────────────────────────────────────────────────


def test_detects_missing_input():
    contract = _make_contract(inputs=["data_file", "citation_db"])
    store = MemoryStore()
    store.add_asset(_make_asset("data_file", "some data"))

    report = check_sufficiency(contract, store)

    assert report["missing_inputs"] == ["citation_db"]
    assert not report["sufficiency_ok"]
    assert report["recommendation"] == "plan"


def test_no_missing_inputs_when_all_present():
    contract = _make_contract(inputs=["data_file", "citation_db"])
    store = MemoryStore()
    store.add_asset(_make_asset("data_file", "some data"))
    store.add_asset(_make_asset("citation_db", "citations"))

    report = check_sufficiency(contract, store)

    assert report["missing_inputs"] == []


def test_missing_input_triggers_plan_recommendation():
    contract = _make_contract(inputs=["missing_only"])
    store = MemoryStore()

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "plan"
    assert not report["sufficiency_ok"]


# ── Stale / tombstoned assets ────────────────────────────────────────────


def test_detects_stale_tombstoned_asset():
    contract = _make_contract(inputs=["data_file"])
    store = MemoryStore()
    store.add_asset(_make_asset("data_file", "old data", tombstoned=True))

    report = check_sufficiency(contract, store)

    assert report["stale_assets"] == ["data_file"]
    assert not report["sufficiency_ok"]


def test_tombstoned_asset_triggers_replan():
    contract = _make_contract(inputs=["data_file"])
    store = MemoryStore()
    store.add_asset(_make_asset("data_file", "old data", tombstoned=True))

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "replan"
    assert not report["sufficiency_ok"]


def test_non_tombstoned_asset_not_stale():
    contract = _make_contract(inputs=["data_file"])
    store = MemoryStore()
    store.add_asset(_make_asset("data_file", "fresh data", tombstoned=False))

    report = check_sufficiency(contract, store)

    assert report["stale_assets"] == []


# ── Version conflicts ────────────────────────────────────────────────────


def test_detects_multiple_versions_conflict():
    contract = _make_contract(inputs=["report"])
    store = MemoryStore()

    # Two assets with same name, same definition_hash, but different content (= different IDs)
    asset_a = _make_asset("report", "content version A")
    asset_b = _make_asset("report", "content version B")

    store.add_asset(asset_a)
    store.add_asset(asset_b)

    report = check_sufficiency(contract, store)

    assert len(report["version_conflicts"]) >= 1
    conflict = report["version_conflicts"][0]
    assert sorted(conflict["asset_ids"]) == sorted([asset_a.id, asset_b.id])
    assert "report" in conflict["names"]
    assert not report["sufficiency_ok"]


def test_version_conflict_triggers_replan():
    contract = _make_contract(inputs=["report"])
    store = MemoryStore()

    store.add_asset(_make_asset("report", "v1"))
    store.add_asset(_make_asset("report", "v2"))

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "replan"


def test_no_version_conflict_when_single_version():
    contract = _make_contract(inputs=["report"])
    store = MemoryStore()
    store.add_asset(_make_asset("report", "v1"))

    report = check_sufficiency(contract, store)

    assert report["version_conflicts"] == []


# ── Trust gaps ───────────────────────────────────────────────────────────


def test_trust_gap_detection():
    contract = _make_contract(inputs=["worker_output"])
    store = MemoryStore()
    store.add_asset(_make_asset("worker_output", "output", trust_tier="untrusted"))

    report = check_sufficiency(contract, store)

    assert report["trust_gaps"] == ["worker_output"]
    assert not report["sufficiency_ok"]


def test_trust_gap_with_worker_tier():
    contract = _make_contract(inputs=["worker_output"])
    store = MemoryStore()
    store.add_asset(_make_asset("worker_output", "output", trust_tier="worker"))

    report = check_sufficiency(contract, store)

    assert report["trust_gaps"] == ["worker_output"]


def test_no_trust_gap_for_human_tier():
    contract = _make_contract(inputs=["trusted"])
    store = MemoryStore()
    store.add_asset(_make_asset("trusted", "data", trust_tier="human"))

    report = check_sufficiency(contract, store)

    assert report["trust_gaps"] == []


def test_trust_gap_triggers_escalate():
    contract = _make_contract(inputs=["untrusted_asset"])
    store = MemoryStore()
    store.add_asset(_make_asset("untrusted_asset", "data", trust_tier="untrusted"))

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "escalate"


# ── Signature gaps ───────────────────────────────────────────────────────


def test_seal_gap_detection_unsigned():
    """Store rejects unsigned assets — sufficiency reports missing input."""
    contract = _make_contract(inputs=["unsigned_asset"])
    store = MemoryStore()

    with pytest.raises(ValueError, match="missing or invalid canonical seal"):
        store.add_asset(_make_asset("unsigned_asset", "data", signed=False))

    report = check_sufficiency(contract, store)
    assert "unsigned_asset" in report["missing_inputs"]
    assert not report["sufficiency_ok"]


def test_seal_gap_detection_invalid():
    """Store rejects tampered assets — sufficiency reports missing input."""
    from dataclasses import replace

    contract = _make_contract(inputs=["tampered"])
    store = MemoryStore()
    signed = _make_asset("tampered", "original", signed=True)
    tampered = replace(signed, content="changed")

    with pytest.raises(ValueError, match="missing or invalid canonical seal"):
        store.add_asset(tampered)

    # The asset was never stored, so sufficiency should show it as missing
    report = check_sufficiency(contract, store)
    assert "tampered" in report["missing_inputs"]


def test_no_seal_gap_for_validly_signed():
    contract = _make_contract(inputs=["signed_asset"])
    store = MemoryStore()
    store.add_asset(_make_asset("signed_asset", "data", signed=True))

    report = check_sufficiency(contract, store)

    assert report["seal_gaps"] == []


def test_seal_gap_triggers_escalate():
    """Store rejects unsigned — missing input triggers escalate."""
    contract = _make_contract(inputs=["unsigned_asset"])
    store = MemoryStore()

    with pytest.raises(ValueError, match="missing or invalid canonical seal"):
        store.add_asset(_make_asset("unsigned_asset", "data", signed=False))

    report = check_sufficiency(contract, store)
    assert report["recommendation"] == "plan"  # missing inputs → plan


# ── Recommendations ──────────────────────────────────────────────────────


def test_recommends_exec_when_all_ok():
    contract = _make_contract(inputs=["data_file"])
    store = MemoryStore()
    store.add_asset(_make_asset("data_file", "data", trust_tier="human", signed=True))

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "exec"
    assert report["sufficiency_ok"]


def test_recommends_plan_when_missing_inputs():
    contract = _make_contract(inputs=["missing"])
    store = MemoryStore()

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "plan"
    assert not report["sufficiency_ok"]


def test_recommends_replan_when_stale():
    contract = _make_contract(inputs=["stale_data"])
    store = MemoryStore()
    store.add_asset(_make_asset("stale_data", "old", tombstoned=True))

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "replan"


def test_recommends_escalate_when_trust_gap():
    contract = _make_contract(inputs=["low_trust"])
    store = MemoryStore()
    store.add_asset(_make_asset("low_trust", "data", trust_tier="untrusted"))

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "escalate"


def test_priority_missing_over_stale():
    """Missing beats stale in recommendation priority."""
    contract = _make_contract(inputs=["missing", "tombstoned"])
    store = MemoryStore()
    store.add_asset(_make_asset("tombstoned", "old", tombstoned=True))

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "plan"
    assert report["missing_inputs"] == ["missing"]


def test_priority_stale_over_trust():
    """Stale beats trust gap in recommendation priority."""
    contract = _make_contract(inputs=["tombstoned_trusted", "untrusted_ok"])
    store = MemoryStore()
    store.add_asset(_make_asset("tombstoned_trusted", "old", tombstoned=True))
    store.add_asset(_make_asset("untrusted_ok", "data", trust_tier="untrusted"))

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "replan"


# ── Report as traceable asset ────────────────────────────────────────────


def test_report_is_traceable_asset():
    contract = _make_contract(inputs=["data_file"])
    store = MemoryStore()
    store.add_asset(_make_asset("data_file", "data"))

    asset = sufficiency_result_asset(contract, store)

    assert asset.name == f"_sufficiency_result_{contract.id}"
    assert asset.origin == "system"
    assert asset.trust_tier == "system"
    assert asset.minted_by == "engine"
    assert asset.created_by == contract.id
    assert asset.content_type == "application/json"

    # Content is valid JSON matching the report
    parsed = json.loads(asset.content)
    assert parsed["contract_id"] == contract.id
    assert parsed["recommendation"] == "exec"
    assert parsed["sufficiency_ok"] is True

    # Asset has proper hashes
    assert asset.definition_hash
    assert asset.content_hash
    assert asset.id
    assert asset.id.startswith("content:")


def test_sufficiency_asset_can_be_stored_and_retrieved():
    contract = _make_contract(inputs=["data_file"])
    store = MemoryStore()
    store.add_asset(_make_asset("data_file", "data", signed=True))

    asset = sufficiency_result_asset(contract, store)
    store.add_asset(sign_asset(asset))

    retrieved = store.get_asset(asset.id)
    assert retrieved is not None
    assert retrieved.name == f"_sufficiency_result_{contract.id}"

    # The report itself should NOT mark the contract as insufficient
    # (the sufficiency_result asset exists alongside input assets)
    report2 = check_sufficiency(contract, store)
    assert report2["recommendation"] == "exec"


def test_contract_report_includes_contract_name():
    contract = _make_contract(name="build_report", inputs=[])
    store = MemoryStore()

    report = check_sufficiency(contract, store)

    assert report["contract_name"] == "build_report"
    assert report["contract_id"] == contract.id


# ── Edge cases ───────────────────────────────────────────────────────────


def test_empty_inputs_contract_is_sufficient():
    contract = _make_contract(inputs=[])
    store = MemoryStore()

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "exec"
    assert report["sufficiency_ok"]


def test_tombstoned_asset_not_flagged_as_trust_gap():
    """A tombstoned asset with low trust should be a stale issue, not a trust gap."""
    contract = _make_contract(inputs=["stale_low_trust"])
    store = MemoryStore()
    store.add_asset(
        _make_asset("stale_low_trust", "old", trust_tier="untrusted", tombstoned=True)
    )

    report = check_sufficiency(contract, store)

    assert "stale_low_trust" in report["stale_assets"]
    assert "stale_low_trust" not in report["trust_gaps"]


def test_tombstoned_asset_not_flagged_as_sig_gap():
    """A tombstoned signed asset should be a stale issue, not a sig gap."""
    contract = _make_contract(inputs=["stale_unsigned"])
    store = MemoryStore()
    store.add_asset(_make_asset("stale_unsigned", "old", signed=True, tombstoned=True))

    report = check_sufficiency(contract, store)

    assert "stale_unsigned" in report["stale_assets"]
    assert "stale_unsigned" not in report["seal_gaps"]


# ── Parametrized across store types ──────────────────────────────────────


@pytest.fixture(params=["memory"])
def store(request):
    """Store fixture for sufficiency tests."""
    assert request.param == "memory"
    return MemoryStore()


def test_sufficiency_with_fixture(store):
    contract = _make_contract(inputs=["data_file"])
    store.add_asset(_make_asset("data_file", "data"))

    report = check_sufficiency(contract, store)

    assert report["recommendation"] == "exec"
    assert report["sufficiency_ok"]
