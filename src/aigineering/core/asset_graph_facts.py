"""Store-bound validation for signed definition/content graph facts."""

from __future__ import annotations

from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.domain import load_genesis
from aigineering.protocol.asset_graph import (
    content_object_from_dict,
    definition_content_assertion_from_dict,
    signed_definition_from_dict,
    validate_content_object,
    verify_definition_content_assertion,
    verify_signed_definition,
)
from aigineering.protocol.runtime_record import RuntimeRecord

ASSET_GRAPH_RECORD_TYPES = frozenset(
    {
        "asset.content.published",
        "asset.definition.published",
        "asset.definition-content.asserted",
    }
)


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
