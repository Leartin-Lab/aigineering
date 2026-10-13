from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest
from conftest import candidate_runtime

from aigineering.core.sqlite_store import CURRENT_SCHEMA_VERSION, SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.core.trace import MemoryTraceStore
from aigineering.core.verification import batch_verify_definition
from aigineering.core.query_projection import StoreQueryProjection
from aigineering.protocol.candidate import CandidateEffect
from aigineering.protocol.effect_builders import (
    content_publication_effect,
    definition_content_assertion_effect,
    definition_publication_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset
from aigineering.core.authority import RESERVED_PREFIXES
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
from aigineering.protocol.candidate import (
    CandidateClaimBinding,
    create_candidate_proposal,
)
from aigineering.protocol.types import Contract
from aigineering.runtime import claim_next_package, submit_worker_proposal
from conftest import hosted_worker


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
    first = create_content_object("café")
    second = create_content_object("café")
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
    asset_view = StoreQueryProjection(temp_sqlite_store).get_assets_by_definition(
        definition.id
    )
    assert len(asset_view) == 1
    assert asset_view[0].name == definition.name
    assert asset_view[0].content == content.content
    assert asset_view[0].content_hash == content.id
    verification = batch_verify_definition(temp_sqlite_store, definition.id)
    assert verification["pass_count"] == 1
    assert verification["fail_count"] == 0


@pytest.mark.parametrize(
    "reserved_name",
    sorted(
        {
            name
            for prefix in RESERVED_PREFIXES
            for name in (f"{prefix}graph_payload", prefix.rstrip("_"))
        }
    ),
)
def test_graph_materialization_rejects_reserved_name_without_protected_capability(
    temp_sqlite_store, reserved_name
) -> None:
    from aigineering.core.candidate_publisher import CandidatePublisher

    runtime = candidate_runtime(temp_sqlite_store)
    actor_signer, actor_key = _identity("worker:graph-writer")
    actor_key = replace(actor_key, capabilities=("asset.publish", "asset.relate"))
    runtime.authorize_actor(actor_key)
    publisher = CandidatePublisher(
        temp_sqlite_store,
        temp_sqlite_store,
        runtime.genesis,
        actor_key,
        actor_signer,
    )
    content = create_content_object("reserved graph value")
    definition = _definition(
        actor_signer,
        actor_id=actor_key.actor_id,
        domain_id=runtime.genesis.id,
        key_id=actor_key.key_id,
        name=reserved_name,
    )
    assertion = create_definition_content_assertion(
        domain_id=runtime.genesis.id,
        definition_id=definition.id,
        content_id=content.id,
        relation_type="satisfies",
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        signer=actor_signer,
    )

    decision = publisher.publish(
        (
            content_publication_effect(content),
            definition_publication_effect(definition),
            definition_content_assertion_effect(assertion),
        ),
        idempotency_key=f"reserved-graph-without-protected-capability-{reserved_name}",
    )

    assert not decision.accepted
    assert any(
        "asset.publish.protected" in str(record.payload.get("reason", ""))
        for record in decision.runtime_records
        if record.record_type.endswith("rejected")
    )
    if reserved_name == "_sys_graph_payload":
        assert temp_sqlite_store.get_content_objects() == []
        assert temp_sqlite_store.get_asset_definitions() == []
        assert temp_sqlite_store.get_definition_content_assertions() == []
        assert temp_sqlite_store.get_all_assets() == []
        rejected = temp_sqlite_store.scan_runtime_records(
            record_type="candidate.rejected"
        )
        assert any(
            record.payload["candidate_id"] == decision.candidate_id
            for _, record in rejected
        )
        traces = temp_sqlite_store.scan_runtime_records(record_type="trace.recorded")
        assert any(
            record.payload["trace"]["parent_id"] == decision.candidate_id
            and record.payload["trace"]["event_type"] == "candidate_rejected"
            for _, record in traces
        )


def test_graph_assertion_reusing_historical_reserved_definition_is_rejected(
    temp_sqlite_store,
) -> None:
    from aigineering.core.candidate_publisher import CandidatePublisher

    runtime = candidate_runtime(temp_sqlite_store)
    content = create_content_object("historical reserved content")
    definition = _definition(
        runtime.signer,
        actor_id=runtime.actor_key.actor_id,
        domain_id=runtime.genesis.id,
        key_id=runtime.actor_key.key_id,
        name="_sys_historical_graph",
    )
    assert runtime._publish(content_publication_effect(content)).accepted
    assert runtime._publish(definition_publication_effect(definition)).accepted

    actor_signer, actor_key = _identity("worker:historical-graph-writer")
    actor_key = replace(actor_key, capabilities=("asset.relate",))
    runtime.authorize_actor(actor_key)
    assertion = create_definition_content_assertion(
        domain_id=runtime.genesis.id,
        definition_id=definition.id,
        content_id=content.id,
        relation_type="satisfies",
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
        signer=actor_signer,
    )
    decision = CandidatePublisher(
        temp_sqlite_store,
        temp_sqlite_store,
        runtime.genesis,
        actor_key,
        actor_signer,
    ).publish(
        (definition_content_assertion_effect(assertion),),
        idempotency_key="historical-reserved-graph-assertion",
    )

    assert not decision.accepted
    assert any(
        "asset.publish.protected" in str(record.payload.get("reason", ""))
        for record in decision.runtime_records
        if record.record_type.endswith("rejected")
    )
    assert temp_sqlite_store.get_content_objects() == [content_object_to_dict(content)]
    assert temp_sqlite_store.get_asset_definitions() == [
        signed_definition_to_dict(definition)
    ]
    assert temp_sqlite_store.get_definition_content_assertions() == []
    assert temp_sqlite_store.get_all_assets() == []
    rejected = temp_sqlite_store.scan_runtime_records(record_type="candidate.rejected")
    assert any(
        record.payload["candidate_id"] == decision.candidate_id
        for _, record in rejected
    )
    traces = temp_sqlite_store.scan_runtime_records(record_type="trace.recorded")
    assert any(
        record.payload["trace"]["parent_id"] == decision.candidate_id
        and record.payload["trace"]["event_type"] == "candidate_rejected"
        for _, record in traces
    )


class _ClaimGraphWorker:
    worker_id = "worker:claim-graph-writer"

    def registration(self):
        from aigineering.core.worker_routing import WorkerRegistration

        return WorkerRegistration(self.worker_id)

    def invoke(self, contract, disclosed_assets):
        del contract, disclosed_assets
        raise AssertionError("the test submits a signed Candidate directly")


def _submit_claimed_graph(
    monkeypatch,
    *,
    output_name: str,
    minting_authority: tuple[str, ...],
    graph_name: str,
):
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime", lambda store, **kwargs: None
    )
    runtime_store = SQLiteStore(":memory:")
    ingress = candidate_runtime(runtime_store)
    contract = ingress.accept_contract(
        Contract(
            id=f"task:graph-claim-{output_name.replace('_', '-')}",
            name="graph claim output",
            outputs=(output_name,),
            budget=1,
            minting_authority=minting_authority,
        )
    )
    host = hosted_worker(
        runtime_store,
        _ClaimGraphWorker(),
        genesis=ingress.genesis,
        authority_key=ingress.actor_key,
        authority_signer=ingress.signer,
    )
    claimed = claim_next_package(
        runtime_store,
        worker_id=host.worker_id,
        contract_id=contract.id,
        claim_candidate=host.claim_candidate(
            request_id=f"claim-{output_name}", contract_id=contract.id
        ),
    )
    assert claimed is not None
    content = create_content_object("claimed graph output")
    definition = _definition(
        host.signer,
        actor_id=host.actor_key.actor_id,
        domain_id=host.genesis.id,
        key_id=host.actor_key.key_id,
        name=graph_name,
        source_uri=contract.id,
    )
    assertion = create_definition_content_assertion(
        domain_id=host.genesis.id,
        definition_id=definition.id,
        content_id=content.id,
        relation_type="satisfies",
        actor_id=host.actor_key.actor_id,
        key_id=host.actor_key.key_id,
        signer=host.signer,
    )
    package = claimed.package
    proposal = create_candidate_proposal(
        domain_id=host.genesis.id,
        actor_id=host.actor_key.actor_id,
        key_id=host.actor_key.key_id,
        effects=(
            content_publication_effect(content),
            definition_publication_effect(definition),
            definition_content_assertion_effect(assertion),
        ),
        signer=host.signer,
        idempotency_key=f"submit-{output_name}",
        claim_binding=CandidateClaimBinding(
            contract.id,
            package.claim_id,
            package.claim_epoch,
            package.package_id,
        ),
    )
    result = submit_worker_proposal(proposal, runtime_store)
    return runtime_store, result


def test_claim_minting_authority_allows_exact_reserved_graph_output(monkeypatch):
    name = "_sys_claimed_graph"
    store, result = _submit_claimed_graph(
        monkeypatch, output_name=name, minting_authority=(name,), graph_name=name
    )
    try:
        assert result["status"] == "accepted", result
        assert store.get_assets_by_name(name)
    finally:
        store.close()


def test_claim_without_exact_minting_authority_rejects_reserved_graph_asset(
    monkeypatch,
):
    store, result = _submit_claimed_graph(
        monkeypatch,
        output_name="ordinary_result",
        minting_authority=(),
        graph_name="_sys_unminted_graph",
    )
    try:
        assert result["status"] == "rejected", result
        assert any("asset.publish.protected" in reason for reason in result["rejected"])
        assert store.get_assets_by_name("_sys_unminted_graph") == []
    finally:
        store.close()


def test_graph_materialization_rebuilds_strictly_and_reopens(tmp_path):
    path = str(tmp_path / "graph-materialization-rebuild.db")
    store = SQLiteStore(path)
    runtime = candidate_runtime(store)
    definition = _definition(
        runtime.signer,
        actor_id=runtime.actor_key.actor_id,
        domain_id=runtime.genesis.id,
        key_id=runtime.actor_key.key_id,
        name="_sys_graph_materialized",
    )
    content = create_content_object("rebuild graph value")
    assertion = create_definition_content_assertion(
        domain_id=runtime.genesis.id,
        definition_id=definition.id,
        content_id=content.id,
        relation_type="satisfies",
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        signer=runtime.signer,
    )
    effects = (
        content_publication_effect(content),
        definition_publication_effect(definition),
        definition_content_assertion_effect(assertion),
    )
    decision = runtime.publisher.publish(
        effects, idempotency_key="graph-materialization-rebuild-control"
    )
    assert decision.accepted
    replay = runtime.publisher.publish(
        effects, idempotency_key="graph-materialization-rebuild-control"
    )
    assert replay.accepted
    assert (
        len(store.scan_runtime_records(record_type="asset.definition-content.asserted"))
        == 1
    )
    before = store.runtime_materialization_digest()
    assert store.rebuild_runtime_materializations(strict=True) == before
    store.close()

    reopened = SQLiteStore(path)
    try:
        assert reopened.runtime_materialization_digest() == before
        assert reopened.rebuild_runtime_materializations(strict=True) == before
        assert reopened.get_assets_by_name("_sys_graph_materialized")[0].content == (
            content.content
        )
    finally:
        reopened.close()


def test_graph_assertion_materializes_same_compatibility_asset_in_memory() -> None:
    store = MemoryStore()
    runtime = candidate_runtime(store, MemoryTraceStore())
    content = create_content_object("portable")
    definition = _definition(
        runtime.signer,
        actor_id=runtime.actor_key.actor_id,
        domain_id=runtime.genesis.id,
        key_id=runtime.actor_key.key_id,
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
        idempotency_key="memory-graph-materialization",
    )

    assert decision.accepted
    assert len(decision.assets) == 1
    assert store.get_asset(decision.assets[0].id) == decision.assets[0]
    assert (
        StoreQueryProjection(store).get_asset(decision.assets[0].id)
        == decision.assets[0]
    )


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
    rejected = temp_sqlite_store.scan_runtime_records(record_type="candidate.rejected")
    assert len(rejected) == 1
    assert "unknown signed definition" in rejected[0][1].payload["reason"]

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


def test_graph_endpoints_and_edge_commit_atomically_in_any_effect_order(
    temp_sqlite_store,
) -> None:
    runtime = candidate_runtime(temp_sqlite_store)
    content = create_content_object("atomic")
    definition = _definition(
        runtime.signer,
        actor_id=runtime.actor_key.actor_id,
        domain_id=runtime.genesis.id,
        key_id=runtime.actor_key.key_id,
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
            definition_content_assertion_effect(assertion),
            definition_publication_effect(definition),
            content_publication_effect(content),
        ),
        idempotency_key="atomic-graph",
    )
    assert decision.accepted
    assert (
        temp_sqlite_store.get_definition_content_assertions()[0]["id"] == assertion.id
    )


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
        assert reopened.schema_version == CURRENT_SCHEMA_VERSION
        assert reopened.get_asset(legacy.id) == legacy
        contents = reopened.get_content_objects()
        definitions = reopened.get_asset_definitions()
        assertions = reopened.get_definition_content_assertions()
        assert contents[0]["id"].startswith("content:v1:")
        assert definitions[0]["id"].startswith("definition:legacy:")
        assert definitions[0]["legacy_asset_id"] == legacy.id
        assert assertions[0]["id"].startswith("assertion:legacy:")
        assert assertions[0]["legacy_asset_id"] == legacy.id
        before = reopened.runtime_materialization_digest()
        assert reopened.rebuild_runtime_materializations() == before
    finally:
        reopened.close()
