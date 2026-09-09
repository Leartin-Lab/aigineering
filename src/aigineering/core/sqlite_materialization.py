"""Pure SQLite row-to-protocol materialization helpers.

This module deliberately owns no connection or cursor. Callers retain query and
transaction ownership and pass individual rows here for deterministic decoding.
"""

from __future__ import annotations

import json
from typing import Protocol

from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.protocol.types import Asset, Contract, ReplacementClaim


class SQLiteRow(Protocol):
    """The keyed access surface used from ``sqlite3.Row``."""

    def __getitem__(self, key: str, /): ...


def runtime_record_from_row(row: SQLiteRow) -> RuntimeRecord:
    return RuntimeRecord(
        id=row["record_id"],
        record_type=row["record_type"],
        schema_version=int(row["schema_version"]),
        payload=json.loads(row["payload_json"]),
        causal_parents=tuple(json.loads(row["causal_parents"])),
        recorded_at=row["recorded_at"],
    )


def asset_from_row(row: SQLiteRow) -> Asset:
    return Asset(
        id=row["id"],
        name=row["name"],
        content=row["content"],
        content_type=row["content_type"],
        created_by=row["created_by"],
        origin=row["origin"],
        trust_tier=row["trust_tier"],
        minted_by=row["minted_by"],
        source_uri=row["source_uri"],
        signed_by=row["signed_by"],
        signer_kind=row["signer_kind"],
        provenance_seal=row["provenance_seal"],
        promptable=bool(row["promptable"]),
        disclosure_view=row["disclosure_view"],
        definition_hash=row["definition_hash"],
        content_hash=row["content_hash"],
        keep_flag=bool(row["keep_flag"]),
        tombstoned=bool(row["tombstoned"]),
        tombstoned_at=row["tombstoned_at"],
        lineage_id=row["lineage_id"],
    )


def contract_from_row(row: SQLiteRow) -> Contract:
    return Contract(
        id=row["id"],
        parent_id=row["parent_id"],
        name=row["name"],
        description=row["description"],
        inputs=tuple(json.loads(row["inputs"])),
        outputs=tuple(json.loads(row["outputs"])),
        activation=row["activation"],
        budget=row["budget"],
        tool_scope=tuple(json.loads(row["tool_scope"])),
        labels=tuple(json.loads(row["labels"])),
        context_asset_ids=tuple(json.loads(row["context_asset_ids"])),
        worker_capabilities=tuple(json.loads(row["worker_capabilities"] or "[]")),
        worker_pools=tuple(json.loads(row["worker_pools"] or "[]")),
        delegation_capabilities=tuple(
            json.loads(row["delegation_capabilities"] or "[]")
        ),
        delegation_pools=tuple(json.loads(row["delegation_pools"] or "[]")),
        origin=row["origin"],
        minting_authority=tuple(json.loads(row["minting_authority"] or "[]")),
        sensitive_input_policy=(
            json.loads(row["sensitive_input_policy"])
            if row["sensitive_input_policy"]
            else None
        ),
        acceptance_policy=(
            json.loads(row["acceptance_policy"]) if row["acceptance_policy"] else None
        ),
    )


def replacement_claim_from_row(row: SQLiteRow) -> ReplacementClaim:
    return ReplacementClaim(
        id=row["id"],
        source_asset_id=row["source_asset_id"],
        replacement_asset_id=row["replacement_asset_id"],
        definition_hash=row["definition_hash"],
        claim_type=row["claim_type"],
        signed_by=row["signed_by"],
        provenance_seal=row["provenance_seal"],
        lineage_id=row["lineage_id"],
        derivation_version=row["derivation_version"],
        range_spec=row["range_spec"],
    )
