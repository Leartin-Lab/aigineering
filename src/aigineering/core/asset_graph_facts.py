"""Store-bound validation for signed definition/content graph facts."""

from __future__ import annotations

from collections.abc import Mapping

from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.domain import load_genesis
from aigineering.core.ids import canonical_json, compute_content_hash
from aigineering.core.provenance import sign_asset
from aigineering.protocol.asset_graph import (
    content_object_from_dict,
    definition_content_assertion_from_dict,
    signed_definition_from_dict,
    validate_content_object,
    verify_definition_content_assertion,
    verify_signed_definition,
)
from aigineering.protocol.runtime_record import RuntimeRecord
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.types import Asset

ASSET_GRAPH_RECORD_TYPES = frozenset(
    {
        "asset.content.published",
        "asset.definition.published",
        "asset.definition-content.asserted",
    }
)

LEGACY_GRAPH_RECORD_TYPE = "asset.legacy-graph.migrated"


def _require_candidate_receipt(record: RuntimeRecord, store) -> None:
    if not any(
        (parent := store.get_runtime_record(parent_id)) is not None
        and parent.record_type == "candidate.received"
        for parent_id in record.causal_parents
    ):
        raise ValueError(
            f"{record.record_type} must be causally rooted in a Candidate receipt"
        )


def _actor_key(record_actor: str, record_key: str, store):
    genesis = load_genesis(store)
    matches = tuple(
        key
        for key in load_effective_actor_keys(store, genesis)
        if key.actor_id == record_actor and key.key_id == record_key
    )
    if len(matches) != 1:
        raise ValueError("asset graph fact references an unauthorized actor key")
    return genesis, matches[0]


def validate_asset_graph_record(record: RuntimeRecord, store) -> None:
    """Fail closed for graph facts even when a Store adapter is called directly."""
    if record.record_type not in ASSET_GRAPH_RECORD_TYPES:
        return
    _require_candidate_receipt(record, store)
    if record.record_type == "asset.content.published":
        content = content_object_from_dict(record.payload.get("content", {}))
        validate_content_object(content)
        return
    if record.record_type == "asset.definition.published":
        definition = signed_definition_from_dict(record.payload.get("definition", {}))
        genesis, key = _actor_key(definition.actor_id, definition.key_id, store)
        if definition.domain_id != genesis.id:
            raise ValueError("definition fact domain does not match Store Genesis")
        verify_signed_definition(definition, key)
        return

    assertion = definition_content_assertion_from_dict(
        record.payload.get("assertion", {})
    )
    genesis, key = _actor_key(assertion.actor_id, assertion.key_id, store)
    if assertion.domain_id != genesis.id:
        raise ValueError("assertion fact domain does not match Store Genesis")
    verify_definition_content_assertion(assertion, key)
    definitions = {
        str(item.payload["definition"]["id"])
        for _, item in store.scan_runtime_records(
            record_type="asset.definition.published"
        )
    }
    contents = {
        str(item.payload["content"]["id"])
        for _, item in store.scan_runtime_records(record_type="asset.content.published")
    }
    if assertion.definition_id not in definitions:
        raise ValueError("assertion fact references an unknown definition")
    if assertion.content_id not in contents:
        raise ValueError("assertion fact references unknown content")


def legacy_asset_graph_record(
    asset: Asset, *, domain_id: str, causal_parent: str = ""
) -> RuntimeRecord:
    """Describe one historical Asset without pretending it had a v1 signature."""
    content = {
        "content": asset.content,
        "id": f"content:v1:{compute_content_hash(asset.content)}",
        "schema_version": 1,
    }
    definition_payload = {
        "content_type": asset.content_type,
        "created_by": asset.created_by,
        "domain_id": domain_id,
        "legacy_asset_id": asset.id,
        "legacy_definition_hash": asset.definition_hash,
        "minted_by": asset.minted_by,
        "name": asset.name,
        "origin": asset.origin,
        "provenance_seal": asset.provenance_seal,
        "signed_by": asset.signed_by,
        "signer_kind": asset.signer_kind,
        "source_uri": asset.source_uri,
        "trust_tier": asset.trust_tier,
    }
    definition = {
        **definition_payload,
        "id": "definition:legacy:"
        + compute_content_hash(canonical_json(definition_payload)),
        "schema_version": 0,
    }
    assertion_payload = {
        "content_id": content["id"],
        "definition_id": definition["id"],
        "domain_id": domain_id,
        "legacy_asset_id": asset.id,
        "legacy_content_hash": asset.content_hash,
        "provenance_seal": asset.provenance_seal,
        "relation_type": "legacy-materialization",
        "signed_by": asset.signed_by,
    }
    assertion = {
        **assertion_payload,
        "id": "assertion:legacy:"
        + compute_content_hash(canonical_json(assertion_payload)),
        "schema_version": 0,
    }
    return create_runtime_record(
        LEGACY_GRAPH_RECORD_TYPE,
        {
            "assertion": assertion,
            "content": content,
            "definition": definition,
        },
        causal_parents=((causal_parent,) if causal_parent else ()),
    )


def graph_rows_from_record(
    record: RuntimeRecord,
) -> tuple[dict | None, dict | None, dict | None]:
    """Return content, definition, and assertion rows carried by one fact."""
    if record.record_type == "asset.content.published":
        return deep_thaw(record.payload["content"]), None, None
    if record.record_type == "asset.definition.published":
        return None, deep_thaw(record.payload["definition"]), None
    if record.record_type == "asset.definition-content.asserted":
        return None, None, deep_thaw(record.payload["assertion"])
    if record.record_type == LEGACY_GRAPH_RECORD_TYPE:
        return (
            deep_thaw(record.payload["content"]),
            deep_thaw(record.payload["definition"]),
            deep_thaw(record.payload["assertion"]),
        )
    return None, None, None


def asset_from_graph_values(
    content: Mapping[str, object],
    definition: Mapping[str, object],
    assertion: Mapping[str, object],
) -> Asset:
    """Project one accepted graph assertion to the compatibility Asset view."""
    return sign_asset(
        Asset(
            id="asset:v1:" + compute_content_hash(str(assertion["id"])),
            name=str(definition["name"]),
            content=str(content["content"]),
            content_type=str(definition["content_type"]),
            created_by=str(definition["source_uri"]),
            origin=(
                "worker"
                if str(definition["source_kind"]) == "contract-output"
                else "definition-content-assertion"
            ),
            trust_tier="observed",
            source_uri=str(definition["source_uri"]),
            signed_by=str(assertion["actor_id"]),
            signer_kind=str(assertion["signature_kind"]),
            definition_hash=str(definition["id"]),
            content_hash=str(content["id"]),
        ),
        signed_by=str(assertion["actor_id"]),
    )


def project_new_graph_assets(
    all_records: tuple[RuntimeRecord, ...],
    projected_records: tuple[RuntimeRecord, ...],
) -> tuple[tuple[Asset, RuntimeRecord], ...]:
    """Project compatibility Assets only for assertions in the current batch."""
    definitions = {
        str(record.payload["definition"]["id"]): record.payload["definition"]
        for record in all_records
        if record.record_type == "asset.definition.published"
    }
    contents = {
        str(record.payload["content"]["id"]): record.payload["content"]
        for record in all_records
        if record.record_type == "asset.content.published"
    }
    projected: list[tuple[Asset, RuntimeRecord]] = []
    for record in projected_records:
        if record.record_type != "asset.definition-content.asserted":
            continue
        assertion = record.payload["assertion"]
        projected.append(
            (
                asset_from_graph_values(
                    contents[str(assertion["content_id"])],
                    definitions[str(assertion["definition_id"])],
                    assertion,
                ),
                record,
            )
        )
    return tuple(projected)


def project_graph_assets(store) -> tuple[Asset, ...]:
    """Build legacy-shaped read views from accepted v1 assertions."""
    contents = {
        str(value["id"]): value
        for value in store.get_content_objects()
        if value.get("schema_version") == 1
    }
    definitions = {
        str(value["id"]): value
        for value in store.get_asset_definitions()
        if value.get("schema_version") == 1
    }
    assets: list[Asset] = []
    for assertion in store.get_definition_content_assertions():
        if assertion.get("schema_version") != 1:
            continue
        definition = definitions.get(str(assertion["definition_id"]))
        content = contents.get(str(assertion["content_id"]))
        if definition is None or content is None:
            continue
        assets.append(asset_from_graph_values(content, definition, assertion))
    return tuple(sorted(assets, key=lambda asset: asset.id))
