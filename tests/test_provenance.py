"""Tests for asset provenance metadata."""

from aigineering.core.projection import project_candidate
from aigineering.core.store import JsonLStore
from aigineering.protocol.types import Asset, Candidate, Contract


def test_jsonl_store_roundtrips_asset_provenance(tmp_path):
    store = JsonLStore(
        str(tmp_path / "assets.jsonl"),
        str(tmp_path / "contracts.jsonl"),
    )
    asset = Asset(
        id="asset_1",
        name="evidence",
        content="observed",
        origin="tool",
        trust_tier="observed",
        minted_by="tool_worker",
        source_uri="tool://read",
    )

    store.add_asset(asset)
    reopened = JsonLStore(
        str(tmp_path / "assets.jsonl"),
        str(tmp_path / "contracts.jsonl"),
    )

    loaded = reopened.get_asset("asset_1")
    assert loaded == asset
    assert loaded.origin == "tool"
    assert loaded.trust_tier == "observed"
    assert loaded.minted_by == "tool_worker"
    assert loaded.source_uri == "tool://read"


def test_projection_assets_record_worker_provenance():
    contract = Contract(id="contract_1", outputs=["report"])
    candidate = Candidate(worker_id="mock_worker", raw_output="report: ok")

    result = project_candidate(contract, candidate)

    assert len(result.accepted_assets) == 1
    asset = result.accepted_assets[0]
    assert asset.origin == "mock"
    assert asset.trust_tier == "untrusted"
    assert asset.minted_by == "mock_worker"
