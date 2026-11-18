"""Read-only compatibility decoding for pre-runtime JSONL records."""

import json

import pytest

from aigineering.adapters.legacy_jsonl import load_legacy_jsonl_assets
from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.protocol.types import Asset, Contract
from aigineering.protocol.wire import asset_to_dict, contract_to_dict


def _write_records(path, records):
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )


def test_decoder_reads_both_files_with_legacy_defaults_and_signature_alias(tmp_path):
    assets = tmp_path / "assets.jsonl"
    contracts = tmp_path / "contracts.jsonl"
    _write_records(
        assets,
        [
            {
                "id": "asset-legacy",
                "name": "evidence",
                "content": "observed",
                "signature": "legacy-seal",
                "unknown_historical_field": "ignored",
            }
        ],
    )
    assets.write_text(
        "\n" + assets.read_text(encoding="utf-8") + " \n", encoding="utf-8"
    )
    _write_records(contracts, [contract_to_dict(Contract(id="contract-legacy"))])
    before = {
        assets: assets.read_bytes(),
        contracts: contracts.read_bytes(),
    }

    decoded = load_legacy_jsonl_assets(str(assets), str(contracts))

    assert decoded == {
        "asset-legacy": Asset(
            id="asset-legacy",
            name="evidence",
            content="observed",
            provenance_seal="legacy-seal",
        )
    }
    assert {path: path.read_bytes() for path in before} == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "assets.jsonl",
        "contracts.jsonl",
    ]


def test_decoder_accepts_exact_duplicate_ids_and_rejects_conflicting_duplicates(
    tmp_path,
):
    assets = tmp_path / "assets.jsonl"
    contracts = tmp_path / "contracts.jsonl"
    original = Asset(id="asset-1", name="evidence", content="same")
    _write_records(assets, [asset_to_dict(original), asset_to_dict(original)])
    _write_records(contracts, [])
    assert load_legacy_jsonl_assets(str(assets), str(contracts)) == {
        "asset-1": original
    }

    changed = Asset(id="asset-1", name="evidence", content="changed")
    _write_records(assets, [asset_to_dict(original), asset_to_dict(changed)])
    with pytest.raises(ImmutableRecordConflict, match="asset"):
        load_legacy_jsonl_assets(str(assets), str(contracts))

    original_contract = Contract(id="contract-1", name="same")
    _write_records(assets, [])
    _write_records(
        contracts,
        [contract_to_dict(original_contract), contract_to_dict(original_contract)],
    )
    assert load_legacy_jsonl_assets(str(assets), str(contracts)) == {}
    changed_contract = Contract(id="contract-1", name="changed")
    _write_records(
        contracts,
        [contract_to_dict(original_contract), contract_to_dict(changed_contract)],
    )
    with pytest.raises(ImmutableRecordConflict, match="contract"):
        load_legacy_jsonl_assets(str(assets), str(contracts))


def test_decoder_parses_assets_before_contracts_and_validates_contract_file(tmp_path):
    assets = tmp_path / "assets.jsonl"
    contracts = tmp_path / "contracts.jsonl"
    assets.write_text("{broken asset\n", encoding="utf-8")
    contracts.write_text("{broken contract\n", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        load_legacy_jsonl_assets(str(assets), str(contracts))

    _write_records(assets, [])
    with pytest.raises(json.JSONDecodeError):
        load_legacy_jsonl_assets(str(assets), str(contracts))

    contracts.write_text("{}\n", encoding="utf-8")
    with pytest.raises(KeyError, match="id"):
        load_legacy_jsonl_assets(str(assets), str(contracts))


def test_decoder_preserves_post_decode_index_checks_and_error_order(tmp_path):
    assets = tmp_path / "assets.jsonl"
    contracts = tmp_path / "contracts.jsonl"
    _write_records(assets, [])
    too_many_tokens = Contract(
        id="legacy-no-identity-validation",
        activation=" ".join(["input"] * 201),
    )
    _write_records(contracts, [contract_to_dict(too_many_tokens)])

    with pytest.raises(ValueError, match="Activation expression too long"):
        load_legacy_jsonl_assets(str(assets), str(contracts))

    # The old loader decoded every Contract before it rebuilt any indexes.
    # A later record's decode failure therefore precedes the first record's
    # activation-index failure.
    contracts.write_text(
        json.dumps(contract_to_dict(too_many_tokens)) + "\n{broken contract\n",
        encoding="utf-8",
    )
    with pytest.raises(json.JSONDecodeError):
        load_legacy_jsonl_assets(str(assets), str(contracts))

    # Index rebuilding also hashed outputs, but did not validate Contract
    # identity or output schema beyond that operation.
    invalid_outputs = Contract(id="still-legacy", outputs=(["unhashable"],))
    _write_records(contracts, [contract_to_dict(invalid_outputs)])
    with pytest.raises(TypeError, match="unhashable"):
        load_legacy_jsonl_assets(str(assets), str(contracts))


@pytest.mark.parametrize(
    "asset_record",
    [
        {"id": "bad-name", "name": ["unhashable"], "content": "x"},
        {
            "id": "bad-creator",
            "name": "valid",
            "content": "x",
            "created_by": ["unhashable"],
        },
    ],
)
def test_asset_index_key_failures_precede_contract_decode_errors(
    tmp_path, asset_record
):
    assets = tmp_path / "assets.jsonl"
    contracts = tmp_path / "contracts.jsonl"
    _write_records(assets, [asset_record])
    contracts.write_text("{broken contract\n", encoding="utf-8")

    with pytest.raises(TypeError, match="unhashable"):
        load_legacy_jsonl_assets(str(assets), str(contracts))


def test_decoder_treats_missing_files_as_empty_without_creating_them(tmp_path):
    assets = tmp_path / "not-created" / "assets.jsonl"
    contracts = tmp_path / "not-created" / "contracts.jsonl"

    assert load_legacy_jsonl_assets(str(assets), str(contracts)) == {}
    assert not assets.parent.exists()

    contracts.parent.mkdir()
    _write_records(contracts, [contract_to_dict(Contract(id="contract-only"))])
    assert load_legacy_jsonl_assets(str(assets), str(contracts)) == {}

    assets.write_text(
        json.dumps(asset_to_dict(Asset(id="asset-only", name="n", content="c"))) + "\n",
        encoding="utf-8",
    )
    contracts.unlink()
    assert "asset-only" in load_legacy_jsonl_assets(str(assets), str(contracts))
