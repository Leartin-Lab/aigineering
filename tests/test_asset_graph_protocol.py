from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest
from conftest import candidate_runtime

from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import CandidateEffect
from aigineering.protocol.effect_builders import (
    content_publication_effect,
    definition_content_assertion_effect,
    definition_publication_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset
from aigineering.core.signing import Ed25519Signer
from aigineering.protocol.asset_graph import (
    assertion_signing_bytes,
    content_object_from_dict,
    content_object_to_dict,
    create_content_object,
    create_definition_content_assertion,
    create_signed_definition,
    definition_content_assertion_from_dict,
    definition_content_assertion_to_dict,
    definition_signing_bytes,
    signed_definition_from_dict,
    signed_definition_to_dict,
    verify_definition_content_assertion,
    verify_signed_definition,
)
from aigineering.protocol.candidate import ActorKey


def _identity(actor_id: str = "human:owner"):
    signer = Ed25519Signer()
    key = ActorKey(
        actor_id=actor_id,
        key_id="root-1",
        kind=signer.kind,
        public_key=signer.signer_id,
    )
    return signer, key


def _definition(signer, actor_id: str = "human:owner", **overrides):
    values = {
        "domain_id": "genesis:test",
        "name": "report",
        "description": "Audited report",
        "content_type": "text/markdown",
        "source_kind": "contract-output",
        "source_uri": "task:v3:one",
        "actor_id": actor_id,
        "key_id": "root-1",
        "signer": signer,
    }
    values.update(overrides)
    return create_signed_definition(**values)


def test_content_identity_uses_normalized_content_only() -> None:
    first = create_content_object("café", content_type="text/plain")
    second = create_content_object("café", content_type="text/markdown")
    assert first.id == second.id
    assert first.content == second.content == "café"
    assert content_object_from_dict(content_object_to_dict(first)) == first


def test_definition_identity_binds_source_actor_and_signature() -> None:
    signer, key = _identity()
    definition = _definition(signer)
    verify_signed_definition(definition, key)
    assert (
        signed_definition_from_dict(signed_definition_to_dict(definition)) == definition
    )

    changed_source = _definition(signer, source_uri="task:v3:two")
    assert changed_source.id != definition.id
    other_signer, _ = _identity()
    changed_signer = _definition(other_signer)
    assert changed_signer.id != definition.id


def test_invalid_definition_signature_fails_closed() -> None:
    signer, key = _identity()
    definition = _definition(signer)
    tampered = replace(definition, description="Different")
    with pytest.raises(ValueError, match="id does not match"):
        verify_signed_definition(tampered, key)
    invalid = replace(
        definition,
        signature=definition.signature[:-1]
        + ("A" if definition.signature[-1] != "A" else "B"),
    )
    with pytest.raises(ValueError, match="id does not match"):
        verify_signed_definition(invalid, key)


def test_definition_content_graph_is_many_to_many() -> None:
    signer, key = _identity()
    first_definition = _definition(signer)
    second_definition = _definition(signer, name="executive_report")
    first_content = create_content_object("one")
    second_content = create_content_object("two")

    assertions = (
        create_definition_content_assertion(
            domain_id="genesis:test",
            definition_id=first_definition.id,
            content_id=first_content.id,
            relation_type="satisfies",
            actor_id=key.actor_id,
            key_id=key.key_id,
            signer=signer,
        ),
        create_definition_content_assertion(
            domain_id="genesis:test",
            definition_id=first_definition.id,
            content_id=second_content.id,
            relation_type="satisfies",
            actor_id=key.actor_id,
            key_id=key.key_id,
            signer=signer,
        ),
        create_definition_content_assertion(
            domain_id="genesis:test",
            definition_id=second_definition.id,
            content_id=first_content.id,
            relation_type="satisfies",
            actor_id=key.actor_id,
            key_id=key.key_id,
            signer=signer,
        ),
    )
    assert len({item.id for item in assertions}) == 3
    for assertion in assertions:
        verify_definition_content_assertion(assertion, key)
        assert (
            definition_content_assertion_from_dict(
                definition_content_assertion_to_dict(assertion)
            )
            == assertion
        )


def test_assertion_evidence_is_signed_and_language_neutral() -> None:
    signer, key = _identity()
    definition = _definition(signer)
    content = create_content_object("same")
    assertion = create_definition_content_assertion(
        domain_id="genesis:test",
        definition_id=definition.id,
        content_id=content.id,
        relation_type="semantic-equivalent",
        actor_id=key.actor_id,
        key_id=key.key_id,
        signer=signer,
        evidence={
            "model": "embedding-v1",
            "score": "0.981",
            "threshold": "0.950",
        },
    )
    verify_definition_content_assertion(assertion, key)
    tampered = replace(
        assertion,
        evidence={
            "model": "embedding-v1",
            "score": "0.981",
            "threshold": "0.990",
        },
    )
    with pytest.raises(ValueError, match="id does not match"):
        verify_definition_content_assertion(tampered, key)

    with pytest.raises(ValueError, match="floating-point"):
        create_definition_content_assertion(
            domain_id="genesis:test",
            definition_id=definition.id,
            content_id=content.id,
            relation_type="semantic-equivalent",
            actor_id=key.actor_id,
            key_id=key.key_id,
            signer=signer,
            evidence={"score": 0.981},
        )


def test_signatures_cover_unsigned_payload_not_identity_or_signature() -> None:
    signer, key = _identity()
    definition = _definition(signer)
    assert b'"id"' not in definition_signing_bytes(definition)
    assert b'"signature"' not in definition_signing_bytes(definition)

    content = create_content_object("content")
    assertion = create_definition_content_assertion(
        domain_id="genesis:test",
        definition_id=definition.id,
        content_id=content.id,
        relation_type="satisfies",
        actor_id=key.actor_id,
        key_id=key.key_id,
        signer=signer,
    )
    assert b'"id"' not in assertion_signing_bytes(assertion)
    assert b'"signature"' not in assertion_signing_bytes(assertion)


def test_graph_facts_enter_through_candidate_commitment(temp_sqlite_store) -> None:
    runtime = candidate_runtime(temp_sqlite_store)
    definition = _definition(
        runtime.signer,
        actor_id=runtime.actor_key.actor_id,
        domain_id=runtime.genesis.id,
        key_id=runtime.actor_key.key_id,
    )
    content = create_content_object("audited")
    assertion = create_definition_content_assertion(
        domain_id=runtime.genesis.id,
        definition_id=definition.id,
        content_id=content.id,
        relation_type="satisfies",
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        signer=runtime.signer,
    )

    assert runtime._publish(content_publication_effect(content)).accepted
    assert runtime._publish(definition_publication_effect(definition)).accepted
    assert runtime._publish(definition_content_assertion_effect(assertion)).accepted
    assert (
        len(
            temp_sqlite_store.scan_runtime_records(
                record_type="asset.definition-content.asserted"
            )
        )
        == 1
    )
    assert [item["id"] for item in temp_sqlite_store.get_content_objects()] == [
        content.id
    ]
    assert [item["id"] for item in temp_sqlite_store.get_asset_definitions()] == [
        definition.id
    ]
    assert [
        item["id"]
        for item in temp_sqlite_store.get_definition_content_assertions(
            definition_id=definition.id
        )
    ] == [assertion.id]


def test_unknown_or_unsigned_graph_edges_are_rejected(temp_sqlite_store) -> None:
    runtime = candidate_runtime(temp_sqlite_store)
    definition = _definition(
        runtime.signer,
        actor_id=runtime.actor_key.actor_id,
        domain_id=runtime.genesis.id,
        key_id=runtime.actor_key.key_id,
    )
    content = create_content_object("missing")
    assertion = create_definition_content_assertion(
        domain_id=runtime.genesis.id,
        definition_id=definition.id,
        content_id=content.id,
        relation_type="satisfies",
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        signer=runtime.signer,
    )
    decision = runtime.publisher.publish(
        (definition_content_assertion_effect(assertion),),
        idempotency_key="unknown-endpoints",
    )
    assert not decision.accepted
    assert not temp_sqlite_store.scan_runtime_records(
        record_type="asset.definition-content.asserted"
    )

    forged = create_runtime_record(
        "asset.content.published",
        {"content": content_object_to_dict(content)},
    )
    with pytest.raises(ValueError, match="Candidate receipt"):
        temp_sqlite_store.append_runtime_record(forged)

    malformed = runtime.publisher.publish(
        (
            CandidateEffect(
                "asset.definition.publish",
                {
                    "definition": {
                        **signed_definition_to_dict(definition),
                        "signature": "ed25519:invalid",
                    }
                },
            ),
        ),
        idempotency_key="invalid-definition-signature",
    )
    assert not malformed.accepted


def test_graph_projection_rebuilds_on_reopen(tmp_path) -> None:
    db_path = tmp_path / "graph.db"
    store = SQLiteStore(str(db_path))
    runtime = candidate_runtime(store)
    content = create_content_object("rebuild")
    runtime._publish(content_publication_effect(content))
    store.close()

    connection = sqlite3.connect(db_path)
    with connection:
        connection.execute("DELETE FROM asset_contents")
    connection.close()

    reopened = SQLiteStore(str(db_path))
    try:
        assert [item["id"] for item in reopened.get_content_objects()] == [content.id]
    finally:
        reopened.close()


def test_v13_legacy_assets_migrate_without_changing_asset_identity(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    store = SQLiteStore(str(db_path))
    runtime = candidate_runtime(store)
    legacy = runtime.accept_asset(
        Asset(
            id="ignored",
            name="legacy-report",
            content="same bytes",
            source_uri="file:///legacy",
        )
    )
    store.close()

    connection = sqlite3.connect(db_path)
    with connection:
        connection.execute(
            "DELETE FROM runtime_records "
            "WHERE record_type = 'asset.legacy-graph.migrated'"
        )
        connection.execute("DROP TABLE asset_definition_content_assertions")
        connection.execute("DROP TABLE asset_definitions")
        connection.execute("DROP TABLE asset_contents")
        connection.execute("DELETE FROM schema_version")
        connection.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (13, 'legacy')"
        )
    connection.close()

    reopened = SQLiteStore(str(db_path))
    try:
        assert reopened.schema_version == 14
        assert reopened.get_asset(legacy.id) == legacy
        contents = reopened.get_content_objects()
        definitions = reopened.get_asset_definitions()
        assertions = reopened.get_definition_content_assertions()
        assert contents[0]["id"].startswith("content:v1:")
        assert definitions[0]["id"].startswith("definition:legacy:")
        assert definitions[0]["legacy_asset_id"] == legacy.id
        assert assertions[0]["id"].startswith("assertion:legacy:")
        assert assertions[0]["legacy_asset_id"] == legacy.id
    finally:
        reopened.close()
