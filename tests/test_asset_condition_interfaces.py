"""Stable interfaces must use the canonical exact Asset conditions."""

from __future__ import annotations

from aigineering.cli.task_board import _qualified_outputs
from aigineering.core.acceptance import acceptance_policy_id
from aigineering.protocol.effect_builders import (
    asset_attestation_effect,
    asset_proposal_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Asset
from aigineering.task_views import project_task_status

from test_shared_definition_output import _graph_output_effects, _world


def test_system_and_recovery_export_exact_cross_source_asset_and_skip_name_twins(
    tmp_path,
):
    for origin in ("system", "recovery"):
        store, runtime, actors = _world(tmp_path / origin)
        correct = actors["worker:external"]
        wrong = actors["worker:imposter"]
        definition, content, _assertion, good_effects = _graph_output_effects(
            runtime,
            correct,
            name="report",
            content_text="the exact report",
            source_uri="external-producer",
        )
        wrong_definition, _wrong_content, _wrong_assertion, wrong_effects = (
            _graph_output_effects(
                runtime,
                wrong,
                name="report",
                content_text="the exact report",
                source_uri="wrong-actor-and-definition",
            )
        )
        contract = contract_from_fields(
            name=f"{origin}-exact-output",
            description="Export the exact matching output.",
            inputs=(),
            outputs=("report",),
            activation="",
            budget=2,
            tool_scope=(),
            labels=(),
            origin=origin,
            acceptance_policy={
                "mode": "mechanical",
                "output_requirements": {
                    "report": {
                        "definition_id": definition.id,
                        "content_id": content.id,
                        "producer_actor_ids": [correct[1].actor_id],
                    }
                },
            },
        )
        runtime.accept_contract(contract)
        runtime.publisher.publish(good_effects, idempotency_key=f"good-{origin}")
        runtime.publisher.publish(wrong_effects, idempotency_key=f"wrong-{origin}")

        exported = _qualified_outputs(contract, store)

        assert len(exported) == 1
        assert exported[0]["content_hash"] == content.id
        assert exported[0]["content_bytes"] == len("the exact report")
        assert exported[0]["created_by"] != contract.id
        assert (
            project_task_status(contract, store)["outputs"]["report"]
            == exported[0]["asset_id"]
        )
        assert wrong_definition.id != definition.id
        store.close()


def test_independent_export_uses_exact_cross_source_qualified_asset(tmp_path):
    store, runtime, actors = _world(tmp_path)
    external = actors["worker:external"]
    definition, content, _assertion, effects = _graph_output_effects(
        runtime,
        external,
        name="report",
        content_text="qualified external report",
        source_uri="external-producer",
    )
    rubric = runtime.publisher.publish(
        (asset_proposal_effect(Asset(id="rubric", name="rubric", content="criteria")),),
        idempotency_key="publish-rubric-for-export",
    )
    evidence = runtime.publisher.publish(
        (
            asset_proposal_effect(
                Asset(id="evidence", name="evidence", content="facts")
            ),
        ),
        idempotency_key="publish-evidence-for-export",
    )
    assert rubric.accepted and evidence.accepted
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "required_attestations": 1,
        "rubric_asset_ids": [rubric.assets[0].id],
        "evidence_asset_ids": [evidence.assets[0].id],
        "verifier_capabilities": ["policy.review"],
        "output_requirements": {
            "report": {
                "definition_id": definition.id,
                "content_id": content.id,
                "producer_actor_ids": [external[1].actor_id],
            }
        },
    }
    contract = contract_from_fields(
        name="independent-cross-source-export",
        description="Require independent acceptance of exact output.",
        inputs=(),
        outputs=("report",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        origin="recovery",
        acceptance_policy=policy,
    )
    runtime.accept_contract(contract)
    published = runtime.publisher.publish(
        effects, idempotency_key="publish-independent-output"
    )
    assert published.accepted
    target = published.assets[0]
    pending = project_task_status(contract, store)
    assert store.get_claim(contract.id) is None
    assert "qualification:independent_acceptance" in pending["blockers"]
    attestation = asset_attestation_effect(
        contract.id,
        "report",
        target.id,
        policy_id=acceptance_policy_id(policy),
        policy_version="review-v1",
        verdict="accepted",
        rubric_asset_ids=(rubric.assets[0].id,),
        evidence_asset_ids=(evidence.assets[0].id,),
    )
    decision = runtime.publisher.publish(
        (attestation,), idempotency_key="attest-independent-external-output"
    )
    assert decision.accepted

    exported = _qualified_outputs(contract, store)

    assert len(exported) == 1
    assert exported[0]["asset_id"] == target.id
    assert exported[0]["created_by"] != contract.id
    assert project_task_status(contract, store)["outputs"]["report"] == target.id
    store.close()
