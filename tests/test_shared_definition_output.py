"""Signed shared-Definition publication satisfies exact Contract outputs."""

from __future__ import annotations

import json

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.worker import WorkerHost
from aigineering.core.domain import initialize_genesis
from aigineering.core.acceptance import acceptance_policy_id
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.asset_graph import (
    create_content_object,
    create_definition_content_assertion,
    create_signed_definition,
)
from aigineering.protocol.candidate import (
    ActorKey,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_attestation_effect,
    content_publication_effect,
    contract_declaration_effect,
    definition_content_assertion_effect,
    definition_publication_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package


def _world(tmp_path):
    store = SQLiteStore(tmp_path / "shared-definition.sqlite")
    named = {}
    for actor_id, capabilities in (
        (
            "human:operator",
            (
                "actor.authorize",
                "asset.attest",
                "asset.publish",
                "asset.publish.protected",
                "asset.relate",
                "contract.publish",
                "policy.review",
                "worker.register",
            ),
        ),
        ("worker:external", ("asset.publish", "asset.relate", "worker.submit")),
        ("worker:imposter", ("asset.publish", "asset.relate", "worker.submit")),
        ("worker:verifier", ("asset.attest", "policy.review")),
    ):
        signer = Ed25519Signer()
        named[actor_id] = (
            signer,
            ActorKey(
                actor_id, f"key:{actor_id}", signer.kind, signer.signer_id, capabilities
            ),
        )
    keys = tuple(key for _, key in named.values())
    genesis = create_genesis_manifest("shared-definition-output", keys, "policy:test")
    initialize_genesis(store, genesis)
    operator_signer, operator_key = named["human:operator"]
    runtime = candidate_runtime(
        store,
        genesis=genesis,
        actor_key=operator_key,
        signer=operator_signer,
    )
    return store, runtime, named


def _contract(*, name, outputs, parent_id=None, acceptance_policy=None, budget=3):
    return contract_from_fields(
        name=name,
        description=f"Contract for {name}.",
        parent_id=parent_id,
        inputs=(),
        outputs=tuple(outputs),
        activation="",
        budget=budget,
        tool_scope=(),
        labels=(),
        worker_capabilities=(),
        worker_pools=(),
        delegation_capabilities=(),
        delegation_pools=(),
        minting_authority=(),
        sensitive_input_policy=None,
        acceptance_policy=acceptance_policy,
        origin="human",
    )


def _definition(runtime, actor, *, name, source_uri):
    signer, key = actor
    return create_signed_definition(
        domain_id=runtime.genesis.id,
        name=name,
        description=f"Shared semantic definition for {name}.",
        content_type="text/plain",
        source_kind="contract-output",
        source_uri=source_uri,
        actor_id=key.actor_id,
        key_id=key.key_id,
        signer=signer,
    )


def _graph_output_effects(runtime, actor, *, name, content_text, source_uri):
    signer, key = actor
    definition = _definition(runtime, actor, name=name, source_uri=source_uri)
    content = create_content_object(content_text)
    assertion = create_definition_content_assertion(
        domain_id=runtime.genesis.id,
        definition_id=definition.id,
        content_id=content.id,
        relation_type="satisfies",
        actor_id=key.actor_id,
        key_id=key.key_id,
        signer=signer,
        evidence={"source": source_uri},
    )
    return (
        definition,
        content,
        assertion,
        (
            content_publication_effect(content),
            definition_publication_effect(definition),
            definition_content_assertion_effect(assertion),
        ),
    )


def _publish(runtime, effects, *, key):
    decision = runtime.publisher.publish(effects, idempotency_key=key)
    reasons = [
        str(record.payload.get("reason", ""))
        for record in decision.runtime_records
        if record.record_type.endswith("rejected")
    ]
    assert decision.accepted, reasons or decision.runtime_records
    return decision


def _requirements(definition, content, actor_id):
    return {
        "mode": "mechanical",
        "output_requirements": {
            "report": {
                "definition_id": definition.id,
                "content_id": content.id,
                "producer_actor_ids": [actor_id],
            }
        },
    }


def _terminal_map(store):
    return {
        str(record.payload["contract_id"]): str(record.payload["terminal"])
        for _, record in store.scan_runtime_records(record_type="lifecycle.terminal")
    }


def test_unclaimed_signed_output_satisfies_exact_declared_conditions(tmp_path):
    store, runtime, actors = _world(tmp_path)
    external = actors["worker:external"]
    definition, content, assertion, effects = _graph_output_effects(
        runtime,
        external,
        name="report",
        content_text="shared result",
        source_uri="unrelated-source-task",
    )
    contract = runtime.accept_contract(
        _contract(
            name="consumer-with-exact-output",
            outputs=("report",),
            acceptance_policy=_requirements(
                definition, content, actors["worker:external"][1].actor_id
            ),
        )
    )
    _publish(runtime, effects, key="publish-unclaimed-output")

    assert store.get_claim(contract.id) is None
    assert _terminal_map(store)[contract.id] == "complete"
    materialized = store.get_assets_by_definition(definition.id)
    assert len(materialized) == 1
    assert materialized[0].content_hash == content.id
    assert materialized[0].signed_by == assertion.actor_id
    store.close()


def test_preexisting_asset_completes_new_parent_and_cancels_new_child(tmp_path):
    store, runtime, actors = _world(tmp_path)
    external = actors["worker:external"]
    definition, content, _assertion, effects = _graph_output_effects(
        runtime,
        external,
        name="report",
        content_text="already committed",
        source_uri="a-different-source",
    )
    _publish(runtime, effects, key="publish-before-declaration")
    parent = _contract(
        name="late-declared-consumer",
        outputs=("report",),
        acceptance_policy=_requirements(
            definition, content, actors["worker:external"][1].actor_id
        ),
    )
    child = _contract(
        name="unfinished-child",
        parent_id=parent.id,
        outputs=("child_report",),
        budget=2,
    )

    _publish(
        runtime,
        (contract_declaration_effect(parent), contract_declaration_effect(child)),
        key="declare-parent-and-child-after-output",
    )

    terminals = _terminal_map(store)
    assert terminals[parent.id] == "complete"
    assert terminals[child.id] == "cancelled"
    assert store.get_claim(parent.id) is None
    store.close()


class _FixedOutputWorker:
    worker_id = "worker:shared-definition-host"

    def __init__(self, content):
        self.content = content

    def invoke(self, contract, disclosed_assets):
        del disclosed_assets
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps(
                {"outputs": {name: self.content for name in contract.outputs}}
            ),
        )


def test_worker_host_exec_reuses_declared_definition_with_exact_claim_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime", lambda store, **kwargs: None
    )
    store, runtime, actors = _world(tmp_path)
    host = hosted_worker(
        store,
        _FixedOutputWorker("worker result"),
        genesis=runtime.genesis,
        authority_key=runtime.actor_key,
        authority_signer=runtime.signer,
    )
    assert isinstance(host, WorkerHost)
    definition = _definition(
        runtime,
        (runtime.signer, runtime.actor_key),
        name="report",
        source_uri="frozen-definition-source",
    )
    _publish(
        runtime,
        (definition_publication_effect(definition),),
        key="publish-frozen-definition",
    )
    content = create_content_object("worker result")
    contract = runtime.accept_contract(
        _contract(
            name="worker-reuses-definition",
            outputs=("report",),
            acceptance_policy=_requirements(
                definition, content, host.actor_key.actor_id
            ),
        )
    )
    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None

    result = execute_claimed_package(claimed, host, store)

    assert result["status"] == "accepted", result
    assert _terminal_map(store)[contract.id] == "complete"
    persisted_claim = store.get_claim(contract.id)
    assert persisted_claim is not None
    assert persisted_claim["claim_id"] == claimed.package.claim_id
    assert persisted_claim["epoch"] == claimed.package.claim_epoch
    assert persisted_claim["package_id"] == claimed.package.package_id
    assets = store.get_assets_by_name("report")
    assert len(assets) == 1
    assert assets[0].definition_hash == definition.id
    assert assets[0].content_hash == content.id
    assert assets[0].signed_by == host.actor_key.actor_id
    definitions = store.get_asset_definitions()
    assert [item["id"] for item in definitions] == [definition.id]
    assertions = store.get_definition_content_assertions(definition_id=definition.id)
    assert len(assertions) == 1
    assertion = assertions[0]
    assert assertion["actor_id"] == host.actor_key.actor_id
    assert assertion["key_id"] == host.actor_key.key_id
    assert assertion["evidence"] == {
        "contract_id": contract.id,
        "output_name": "report",
    }
    from aigineering.core.acceptance import validate_claim_bound_output
    from aigineering.core.projection_context import load_effect_projection_context

    assert assets[0].created_by != contract.id
    assert (
        validate_claim_bound_output(load_effect_projection_context(store), assets[0])
        == contract
    )
    store.close()


def test_name_match_does_not_hide_wrong_definition_content_or_issuer(tmp_path):
    store, runtime, actors = _world(tmp_path)
    expected_definition = _definition(
        runtime,
        actors["human:operator"],
        name="report",
        source_uri="expected-definition",
    )
    wrong_definition = _definition(
        runtime,
        actors["human:operator"],
        name="report",
        source_uri="wrong-definition",
    )
    expected_content = create_content_object("expected bytes")
    wrong_content = create_content_object("wrong bytes")
    external_signer, external_key = actors["worker:external"]
    imposter_signer, imposter_key = actors["worker:imposter"]

    def assertion_effect(definition, content, actor_key, signer, label):
        assertion = create_definition_content_assertion(
            domain_id=runtime.genesis.id,
            definition_id=definition.id,
            content_id=content.id,
            relation_type="satisfies",
            actor_id=actor_key.actor_id,
            key_id=actor_key.key_id,
            signer=signer,
            evidence={"case": label},
        )
        return definition_content_assertion_effect(assertion)

    effects = (
        content_publication_effect(expected_content),
        content_publication_effect(wrong_content),
        definition_publication_effect(expected_definition),
        definition_publication_effect(wrong_definition),
        assertion_effect(
            wrong_definition,
            expected_content,
            external_key,
            external_signer,
            "wrong-def",
        ),
        assertion_effect(
            expected_definition,
            wrong_content,
            external_key,
            external_signer,
            "wrong-content",
        ),
        assertion_effect(
            expected_definition,
            expected_content,
            imposter_key,
            imposter_signer,
            "wrong-issuer",
        ),
    )
    _publish(runtime, effects, key="publish-three-nonmatching-assets")
    contract = runtime.accept_contract(
        _contract(
            name="exact-output-requirements",
            outputs=("report",),
            acceptance_policy=_requirements(
                expected_definition, expected_content, external_key.actor_id
            ),
        )
    )

    assert contract.id not in _terminal_map(store)
    assert len(store.get_assets_by_name("report")) == 3
    store.close()


def test_independent_target_can_accept_unclaimed_matching_graph_asset(tmp_path):
    store, runtime, actors = _world(tmp_path)
    external = actors["worker:external"]
    definition, content, _assertion, effects = _graph_output_effects(
        runtime,
        external,
        name="report",
        content_text="independently reviewed result",
        source_uri="external-publication",
    )
    _publish(runtime, effects, key="publish-independent-source")
    policy = {
        "mode": "independent",
        "policy_version": "shared-definition-review-v1",
        "required_attestations": 1,
        "verifier_capabilities": ["policy.review"],
        "output_requirements": {
            "report": {
                "definition_id": definition.id,
                "content_id": content.id,
                "producer_actor_ids": [actors["worker:external"][1].actor_id],
            }
        },
    }
    target = runtime.accept_contract(
        _contract(
            name="independent-review-target",
            outputs=("report",),
            acceptance_policy=policy,
        )
    )
    asset = store.get_assets_by_definition(definition.id)[0]
    verifier_signer, verifier_key = actors["worker:verifier"]
    from aigineering.core.candidate_publisher import CandidatePublisher

    decision = CandidatePublisher(
        store, store, runtime.genesis, verifier_key, verifier_signer
    ).publish(
        (
            asset_attestation_effect(
                target.id,
                "report",
                asset.id,
                policy_id=acceptance_policy_id(policy),
                policy_version="shared-definition-review-v1",
                verdict="accepted",
            ),
        ),
        idempotency_key="accept-unclaimed-shared-output",
    )
    assert decision.accepted, decision.runtime_records

    assert store.get_claim(target.id) is None
    assert _terminal_map(store)[target.id] == "complete"
    store.close()
