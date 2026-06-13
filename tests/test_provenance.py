"""Tests for asset provenance metadata."""

from aigineering.core.projection import project_candidate
from dataclasses import replace

from aigineering.core.provenance import (
    provenance_signature,
    sign_asset,
    verify_asset_signature,
)
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

    store.add_asset(sign_asset(asset))
    reopened = JsonLStore(
        str(tmp_path / "assets.jsonl"),
        str(tmp_path / "contracts.jsonl"),
    )

    signed_asset = sign_asset(asset)
    loaded = reopened.get_asset("asset_1")
    assert loaded == signed_asset
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


def test_sign_asset_adds_deterministic_provenance_signature():
    asset = Asset(
        id="asset_1",
        name="evidence",
        content="observed",
        origin="tool",
        trust_tier="observed",
        minted_by="tool_worker",
    )

    signed = sign_asset(asset)
    signed_again = sign_asset(asset)

    assert signed.signed_by == "tool_worker"
    assert signed.signature.startswith("asig_")
    assert signed.signature == signed_again.signature
    assert signed.signature == provenance_signature(asset, signed_by="tool_worker")


def test_jsonl_store_roundtrips_asset_signature(tmp_path):
    store = JsonLStore(
        str(tmp_path / "assets.jsonl"),
        str(tmp_path / "contracts.jsonl"),
    )
    signed = sign_asset(
        Asset(
            id="asset_1",
            name="evidence",
            content="observed",
            origin="tool",
            trust_tier="observed",
            minted_by="tool_worker",
        )
    )

    store.add_asset(signed)
    reopened = JsonLStore(
        str(tmp_path / "assets.jsonl"),
        str(tmp_path / "contracts.jsonl"),
    )

    loaded = reopened.get_asset("asset_1")
    assert loaded == signed
    assert loaded.signature == signed.signature


def test_verify_asset_signature_accepts_signed_asset():
    signed = sign_asset(
        Asset(
            id="asset_1",
            name="evidence",
            content="observed",
            minted_by="tool_worker",
        )
    )

    assert verify_asset_signature(signed) is True


def test_verify_asset_signature_rejects_tampered_asset():
    signed = sign_asset(
        Asset(
            id="asset_1",
            name="evidence",
            content="observed",
            minted_by="tool_worker",
        )
    )

    tampered = replace(signed, content="changed")

    assert verify_asset_signature(tampered) is False


def test_verify_asset_signature_rejects_unsigned_asset():
    asset = Asset(id="asset_1", name="evidence", content="observed")

    assert verify_asset_signature(asset) is False
