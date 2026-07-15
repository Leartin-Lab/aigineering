"""Durable Genesis bootstrap and reconstruction.

Genesis is the sole direct bootstrap fact.  Once present it is immutable and
all ordinary changes must enter as Candidates authorized by that manifest.
"""

from __future__ import annotations

from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.protocol.candidate import (
    GenesisManifest,
    genesis_manifest_from_dict,
    genesis_manifest_to_dict,
    validate_genesis_manifest,
)
from aigineering.protocol.runtime_record import create_runtime_record


GENESIS_RECORD_TYPE = "domain.genesis"


def load_genesis(store) -> GenesisManifest:
    records = store.scan_runtime_records(record_type=GENESIS_RECORD_TYPE)
    if not records:
        raise LookupError("runtime domain has not been initialized")
    if len(records) != 1:
        raise RuntimeError("runtime domain contains more than one Genesis record")
    return genesis_manifest_from_dict(records[0][1].payload["manifest"])


def initialize_genesis(store, manifest: GenesisManifest) -> GenesisManifest:
    """Append the domain's one immutable bootstrap record, idempotently."""
    validate_genesis_manifest(manifest)
    try:
        existing = load_genesis(store)
    except LookupError:
        existing = None
    if existing is not None:
        if existing == manifest:
            return existing
        raise ImmutableRecordConflict("domain genesis", existing.id)

    record = create_runtime_record(
        GENESIS_RECORD_TYPE, {"manifest": genesis_manifest_to_dict(manifest)}
    )
    try:
        store.append_runtime_record(record)
    except ImmutableRecordConflict:
        # A concurrent initializer may have won the Store's uniqueness race.
        existing = load_genesis(store)
        if existing == manifest:
            return existing
        raise ImmutableRecordConflict("domain genesis", existing.id) from None
    return manifest
