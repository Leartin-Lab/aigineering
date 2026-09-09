import json

from aigineering.core.sqlite_materialization import (
    replacement_claim_from_row,
    runtime_record_from_row,
)
from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.protocol.types import ReplacementClaim


def test_runtime_record_materialization_decodes_json_fields():
    row = {
        "record_id": "record-1",
        "record_type": "example.recorded",
        "schema_version": 1,
        "payload_json": json.dumps({"answer": 42}),
        "causal_parents": json.dumps(["parent-1"]),
        "recorded_at": "2026-09-08T00:00:00+00:00",
    }

    assert runtime_record_from_row(row) == RuntimeRecord(
        id="record-1",
        record_type="example.recorded",
        schema_version=1,
        payload={"answer": 42},
        causal_parents=("parent-1",),
        recorded_at="2026-09-08T00:00:00+00:00",
    )


def test_replacement_claim_materialization_preserves_optional_fields():
    row = {
        "id": "claim-1",
        "source_asset_id": "source-1",
        "replacement_asset_id": "replacement-1",
        "definition_hash": "definition-hash",
        "claim_type": "replacement",
        "signed_by": "key-1",
        "provenance_seal": "seal",
        "lineage_id": "lineage-1",
        "derivation_version": None,
        "range_spec": None,
    }

    assert replacement_claim_from_row(row) == ReplacementClaim(
        id="claim-1",
        source_asset_id="source-1",
        replacement_asset_id="replacement-1",
        definition_hash="definition-hash",
        claim_type="replacement",
        signed_by="key-1",
        provenance_seal="seal",
        lineage_id="lineage-1",
        derivation_version=None,
        range_spec=None,
    )
