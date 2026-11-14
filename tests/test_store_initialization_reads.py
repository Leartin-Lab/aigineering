from __future__ import annotations

from conftest import candidate_runtime

from aigineering.core.sqlite_migrations import SQLiteMigrator
from aigineering.core.sqlite_store import CURRENT_SCHEMA_VERSION, SQLiteStore
from aigineering.protocol.asset_graph import (
    create_content_object,
    create_definition_content_assertion,
    create_signed_definition,
)
from aigineering.protocol.effect_builders import (
    content_publication_effect,
    definition_content_assertion_effect,
    definition_publication_effect,
)


def _publish_signed_graph(store: SQLiteStore):
    runtime = candidate_runtime(store)
    content = create_content_object("current-schema startup fixture")
    definition = create_signed_definition(
        domain_id=runtime.genesis.id,
        name="startup-fixture",
        description="Signed graph materialization for reopen coverage",
        content_type="text/plain",
        source_kind="contract-output",
        source_uri="task:v3:startup-fixture",
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        signer=runtime.signer,
    )
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
        (
            content_publication_effect(content),
            definition_publication_effect(definition),
            definition_content_assertion_effect(assertion),
        ),
        idempotency_key="signed-startup-graph",
    )
    assert decision.accepted
    return content, definition, assertion


def test_current_schema_open_reads_existing_materializations_without_replay(
    tmp_path, monkeypatch
):
    path = tmp_path / "current-schema.sqlite"
    store = SQLiteStore(str(path))
    content, definition, assertion = _publish_signed_graph(store)
    assert store.schema_version == CURRENT_SCHEMA_VERSION

    graph_before = (
        store.get_content_objects(),
        store.get_asset_definitions(),
        store.get_definition_content_assertions(),
    )
    revision_before = store.get_runtime_revision()
    digest_before = store.runtime_materialization_digest()
    store.close()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("current-schema open performed startup rebuild work")

    monkeypatch.setattr(SQLiteStore, "scan_runtime_records", forbidden)
    monkeypatch.setattr(SQLiteStore, "rebuild_asset_graph_projection", forbidden)
    monkeypatch.setattr(SQLiteMigrator, "run_migrations", forbidden)

    reopened = SQLiteStore(str(path))
    # The spies cover construction only. Subsequent assertions may read history
    # as part of semantic digest verification, so restore methods first.
    monkeypatch.undo()
    try:
        assert reopened.schema_version == CURRENT_SCHEMA_VERSION
        assert reopened.get_runtime_revision() == revision_before
        assert reopened.runtime_materialization_digest() == digest_before
        assert (
            reopened.get_content_objects(),
            reopened.get_asset_definitions(),
            reopened.get_definition_content_assertions(),
        ) == graph_before
        assert reopened.get_content_objects()[0]["id"] == content.id
        assert reopened.get_asset_definitions()[0]["id"] == definition.id
        assert reopened.get_definition_content_assertions()[0]["id"] == assertion.id
    finally:
        reopened.close()


def test_explicit_full_rebuild_preserves_signed_graph_and_current_reopen(tmp_path):
    path = tmp_path / "explicit-rebuild.sqlite"
    store = SQLiteStore(str(path))
    _publish_signed_graph(store)
    before = store.runtime_materialization_digest()

    assert store.rebuild_runtime_materializations(strict=True) == before
    graph_after_rebuild = (
        store.get_content_objects(),
        store.get_asset_definitions(),
        store.get_definition_content_assertions(),
    )
    store.close()

    reopened = SQLiteStore(str(path))
    try:
        assert reopened.runtime_materialization_digest() == before
        assert (
            reopened.get_content_objects(),
            reopened.get_asset_definitions(),
            reopened.get_definition_content_assertions(),
        ) == graph_after_rebuild
    finally:
        reopened.close()
