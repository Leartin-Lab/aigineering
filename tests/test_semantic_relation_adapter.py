from __future__ import annotations

import pytest
from conftest import candidate_runtime

from aigineering.adapters.semantic_relation import (
    SemanticMatch,
    publish_semantic_relation,
)
from aigineering.core.query_projection import StoreQueryProjection
from aigineering.protocol.asset_graph import (
    create_content_object,
    create_signed_definition,
)
from aigineering.protocol.effect_builders import (
    content_publication_effect,
    definition_publication_effect,
)


def _endpoints(runtime):
    content = create_content_object("same")
    definition = create_signed_definition(
        domain_id=runtime.genesis.id,
        name="report",
        description="Report",
        content_type="text",
        source_kind="contract-output",
        source_uri="task:v4:one",
        actor_id=runtime.actor_key.actor_id,
        key_id=runtime.actor_key.key_id,
        signer=runtime.signer,
    )
    runtime._publish(content_publication_effect(content))
    runtime._publish(definition_publication_effect(definition))
    return definition, content


def test_semantic_match_is_only_a_signed_relation_candidate(temp_sqlite_store) -> None:
    runtime = candidate_runtime(temp_sqlite_store)
    definition, content = _endpoints(runtime)
    assert temp_sqlite_store.get_definition_content_assertions() == []

    decision = publish_semantic_relation(
        runtime.publisher,
        definition_id=definition.id,
        content_id=content.id,
        match=SemanticMatch(
            model="embedding-v1",
            model_version="2026-07",
            score="0.981",
            threshold="0.950",
            evidence_uri="asset:evidence",
        ),
        idempotency_key="semantic-one",
    )

    assert decision.accepted
    assertions = temp_sqlite_store.get_definition_content_assertions()
    assert len(assertions) == 1
    assert assertions[0]["relation_type"] == "semantic-equivalent"
    assert assertions[0]["evidence"]["model"] == "embedding-v1"
    assert (
        StoreQueryProjection(temp_sqlite_store)
        .get_assets_by_definition(definition.id)[0]
        .content
        == content.content
    )
    committed = temp_sqlite_store.scan_runtime_records(
        record_type="candidate.committed"
    )
    assert committed


@pytest.mark.parametrize(
    ("score", "threshold", "message"),
    [
        ("not-a-number", "0.9", "decimal strings"),
        ("1.1", "0.9", "between 0 and 1"),
        ("0.8", "0.9", "does not meet"),
    ],
)
def test_invalid_or_below_threshold_match_creates_no_fact(
    temp_sqlite_store, score, threshold, message
) -> None:
    runtime = candidate_runtime(temp_sqlite_store)
    definition, content = _endpoints(runtime)
    with pytest.raises(ValueError, match=message):
        match = SemanticMatch(
            model="embedding-v1",
            model_version="1",
            score=score,
            threshold=threshold,
        )
        publish_semantic_relation(
            runtime.publisher,
            definition_id=definition.id,
            content_id=content.id,
            match=match,
            idempotency_key="semantic-rejected",
        )
    assert temp_sqlite_store.get_definition_content_assertions() == []
