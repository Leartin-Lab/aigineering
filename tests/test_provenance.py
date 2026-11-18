"""Tests for asset provenance metadata."""

from aigineering.core.projection import project_candidate
from dataclasses import replace

from aigineering.core.provenance import (
    compute_provenance_seal,
    sign_asset,
    verify_asset_seal,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Asset, Candidate, Contract


def test_sqlite_store_roundtrips_asset_provenance(tmp_path):
    path = tmp_path / "store.sqlite"
    store = SQLiteStore(str(path))
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
    store.close()
    reopened = SQLiteStore(str(path))

    signed_asset = sign_asset(asset)
    loaded = reopened.get_asset("asset_1")
    assert loaded == signed_asset
    assert loaded.origin == "tool"
    assert loaded.trust_tier == "observed"
    assert loaded.minted_by == "tool_worker"
    assert loaded.source_uri == "tool://read"
    reopened.close()


def test_projection_assets_record_worker_provenance():
    contract = Contract(id="contract_1", outputs=["report"])
    candidate = Candidate(worker_id="mock_worker", raw_output="report: ok")

    result = project_candidate(contract, candidate)

    assert len(result.accepted_assets) == 1
    asset = result.accepted_assets[0]
    assert asset.origin == "mock"
    assert asset.trust_tier == "untrusted"
    assert asset.minted_by == "mock_worker"


def test_sign_asset_adds_deterministic_compute_provenance_seal():
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
    assert signed.provenance_seal.startswith("asig_")
    assert signed.provenance_seal == signed_again.provenance_seal
    assert signed.provenance_seal == compute_provenance_seal(
        asset, signed_by="tool_worker"
    )


def test_sqlite_store_roundtrips_asset_signature(tmp_path):
    path = tmp_path / "store.sqlite"
    store = SQLiteStore(str(path))
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
    store.close()
    reopened = SQLiteStore(str(path))

    loaded = reopened.get_asset("asset_1")
    assert loaded == signed
    assert loaded.provenance_seal == signed.provenance_seal
    reopened.close()


def test_verify_asset_seal_accepts_signed_asset():
    signed = sign_asset(
        Asset(
            id="asset_1",
            name="evidence",
            content="observed",
            minted_by="tool_worker",
        )
    )

    assert verify_asset_seal(signed) is True


def test_verify_asset_seal_rejects_tampered_asset():
    signed = sign_asset(
        Asset(
            id="asset_1",
            name="evidence",
            content="observed",
            minted_by="tool_worker",
        )
    )

    tampered = replace(signed, content="changed")

    assert verify_asset_seal(tampered) is False


def test_verify_asset_seal_rejects_unsigned_asset():
    asset = Asset(id="asset_1", name="evidence", content="observed")

    assert verify_asset_seal(asset) is False
