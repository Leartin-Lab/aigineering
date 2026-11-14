"""Signed primitive compilation for independent review."""

from __future__ import annotations

from dataclasses import fields

import pytest

from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    publish_effect,
    publish_effects,
)
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset
from aigineering.task_policy_review import (
    publish_task_review_decision,
    publish_task_review_request,
    validate_task_review_request,
)


def _publish_claimed_output(
    store, genesis, signer, actor, contract, output_name, content
):
    store.register_worker(
        WorkerRegistration(actor.actor_id, actor_id=actor.actor_id, key_id=actor.key_id)
    )
    claim = store.claim_contract(
        contract.id, actor.actor_id, package_id=f"pkg:{output_name}"
    )
    assert claim is not None
    proposal = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=(
            asset_proposal_effect(
                Asset(id="unused", name=output_name, content=content)
            ),
        ),
        signer=signer,
        idempotency_key=f"output:{contract.id}:{output_name}",
        claim_binding=CandidateClaimBinding(
            contract_id=contract.id,
            claim_id=claim["claim_id"],
            claim_epoch=claim["epoch"],
            package_id=claim["package_id"],
        ),
    )
    result = CandidateCommitter(store, store).commit(proposal, genesis)
    assert result.accepted, [
        record.payload.get("reason")
        for record in result.runtime_records
        if record.record_type == "candidate.rejected"
    ]
    return result.assets[0]


@pytest.fixture
def world(tmp_path, monkeypatch):
    import aigineering.application
    import aigineering.task_policy_review as service

    store = SQLiteStore(tmp_path / "review.sqlite")
    signer = Ed25519Signer()
    operator = ActorKey(
        "human:operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        ("asset.attest", "asset.publish", "contract.publish", "verify.human"),
    )
    target_actor = ActorKey(
        "worker:target",
        "target-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish", "worker.claim", "worker.submit"),
    )
    report_actor = ActorKey(
        "worker:reviewer",
        "report-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish", "worker.claim", "worker.submit"),
    )
    genesis = create_genesis_manifest(
        "review-test", (operator, target_actor, report_actor), "policy:test"
    )
    initialize_genesis(store, genesis)
    rubric = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(
            Asset(id="unused-rubric", name="rubric", content="Check claims")
        ),
        idempotency_key="rubric",
    ).assets[0]
    evidence = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(
            Asset(id="unused-evidence", name="evidence", content="Evidence")
        ),
        idempotency_key="evidence",
    ).assets[0]
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "required_attestations": 1,
        "rubric_asset_ids": [rubric.id],
        "evidence_asset_ids": [evidence.id],
        "verifier_capabilities": ["verify.human"],
        "output_requirements": {"final": {"producer_actor_ids": ["worker:target"]}},
        "output_shapes": {"final": "nonempty_string"},
    }
    base = build_control_plane_contract(
        name="research report",
        description="Produce the requested result.",
        outputs=("final",),
        budget=8,
        acceptance_policy=policy,
    )
    source = contract_from_fields(
        **{
            **{
                field.name: getattr(base, field.name)
                for field in fields(base)
                if field.name != "id"
            },
            "origin": "recovery",
            "labels": ("method:plan", "plugin:example.owner"),
        }
    )
    assert publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        contract_declaration_effect(source),
        idempotency_key="source",
    ).accepted
    target = _publish_claimed_output(
        store, genesis, signer, target_actor, source, "final", '"Research result"'
    )
    publisher = CandidatePublisher(store, store, genesis, operator, signer)
    monkeypatch.setattr(service, "require_redis", lambda: None)
    monkeypatch.setattr(service, "sync_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service, "ensure_local_plugin_publisher", lambda *_args, **_kwargs: publisher
    )
    monkeypatch.setattr(
        aigineering.application,
        "collaboration_service",
        lambda _store: type("Sync", (), {"sync": lambda self: None})(),
    )
    return {
        "store": store,
        "store_path": tmp_path / "review.sqlite",
        "signer": signer,
        "genesis": genesis,
        "operator": operator,
        "target_actor": target_actor,
        "report_actor": report_actor,
        "source": source,
        "target": target,
        "policy": policy,
    }


def _receipt_with_effects(store, effects):
    return [
        record
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.received"
        )
        if tuple(record.payload.get("effect_types", ())) == effects
    ]


def test_request_and_decision_are_single_signed_ordinary_candidates(world):
    store = world["store"]
    source = world["source"]
    target = world["target"]
    request, _ = publish_task_review_request(
        store,
        source.id,
        "final",
        target.id,
        "Check evidence",
        idempotency_key="request-1",
    )
    request_relation = request["relation"]
    assert request_relation["relation"] == "task-review-request"
    assert request["asset_id"] != target.id
    assert (
        request_relation["reviewer_contract_id"]
        == store.get_contract(request_relation["reviewer_contract_id"]).id
    )
    assert not store.scan_runtime_records(record_type="output.qualified")
    request_receipt_count = len(
        _receipt_with_effects(store, ("contract.declare", "asset.propose"))
    )
    replayed_request, _ = publish_task_review_request(
        store,
        source.id,
        "final",
        target.id,
        "Check evidence",
        idempotency_key="request-1",
    )
    assert replayed_request["asset_id"] == request["asset_id"]
    assert (
        len(_receipt_with_effects(store, ("contract.declare", "asset.propose")))
        == request_receipt_count
    )
    remaining_consumer = contract_from_fields(
        **{
            **{
                field.name: getattr(source, field.name)
                for field in fields(source)
                if field.name != "id"
            },
            "name": "remaining source work",
            "parent_id": source.id,
            "budget": source.budget - 1,
        }
    )
    assert publish_effect(
        store,
        store,
        world["genesis"],
        world["operator"],
        world["signer"],
        contract_declaration_effect(remaining_consumer),
        idempotency_key="remaining-source-work",
    ).accepted
    replayed_after_exhaustion, _ = publish_task_review_request(
        store,
        source.id,
        "final",
        target.id,
        "Check evidence",
        idempotency_key="request-1",
    )
    assert replayed_after_exhaustion["asset_id"] == request["asset_id"]
    assert (
        len(_receipt_with_effects(store, ("contract.declare", "asset.propose")))
        == request_receipt_count
    )
    request_receipt = _receipt_with_effects(
        store, ("contract.declare", "asset.propose")
    )[-1]
    request_head = next(
        record
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.committed"
        )
        if record.payload.get("candidate_id") == request_receipt.payload["candidate_id"]
    )
    assert len(request_head.payload["committed_record_ids"]) >= 5
    request_members = {
        record.id: record
        for _revision, record in store.scan_runtime_records()
        if record.id in request_head.payload["committed_record_ids"]
    }
    assert any(
        record.record_type == "contract.declared" for record in request_members.values()
    )
    assert any(
        record.record_type == "asset.committed" for record in request_members.values()
    )
    assert any(
        record.record_type == "contract.declared"
        and record.payload["contract"]["id"] == request_relation["reviewer_contract_id"]
        for _revision, record in store.scan_runtime_records()
    )

    reviewer = store.get_contract(request_relation["reviewer_contract_id"])
    report = _publish_claimed_output(
        store,
        world["genesis"],
        world["signer"],
        world["report_actor"],
        reviewer,
        request_relation["report_output"],
        "Independent review report",
    )
    decision, _ = publish_task_review_decision(
        store,
        request["asset_id"],
        report.id,
        "accept",
        "Evidence supports result",
        idempotency_key="decision-1",
    )
    assert decision["relation"]["relation"] == "task-review-decision"
    assert decision["relation"]["request_asset_id"] == request["asset_id"]
    decision_receipt = _receipt_with_effects(store, ("asset.attest", "asset.propose"))[
        -1
    ]
    decision_head = next(
        record
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.committed"
        )
        if record.payload.get("candidate_id")
        == decision_receipt.payload["candidate_id"]
    )
    assert len(decision_head.payload["committed_record_ids"]) >= 5
    decision_members = {
        record.id: record
        for _revision, record in store.scan_runtime_records()
        if record.id in decision_head.payload["committed_record_ids"]
    }
    assert any(
        record.record_type == "asset.attested" for record in decision_members.values()
    )
    assert any(
        record.record_type == "asset.committed" for record in decision_members.values()
    )
    qualified = store.scan_runtime_records(record_type="output.qualified")
    assert len(qualified) == 1
    assert qualified[0][1].payload["contract_id"] == source.id
    assert qualified[0][1].payload["asset_id"] == target.id
    decision_receipt_count = len(
        _receipt_with_effects(store, ("asset.attest", "asset.propose"))
    )
    replayed_decision, _ = publish_task_review_decision(
        store,
        request["asset_id"],
        report.id,
        "accept",
        "Evidence supports result",
        idempotency_key="decision-1",
    )
    assert replayed_decision["asset_id"] == decision["asset_id"]
    assert (
        len(_receipt_with_effects(store, ("asset.attest", "asset.propose")))
        == decision_receipt_count
    )
    assert not store.scan_runtime_records(record_type="task.review.requested")
    assert not store.scan_runtime_records(record_type="task.review.decided")


def test_request_rejects_wrong_output_or_exhausted_budget_before_publication(world):
    with pytest.raises(ValueError, match="declared source Contract output"):
        validate_task_review_request(
            world["store"], world["source"].id, "other", world["target"].id, "Review"
        )
    before = len(world["store"].scan_runtime_records(record_type="candidate.received"))
    exhaust = contract_from_fields(
        **{
            **{
                field.name: getattr(world["source"], field.name)
                for field in fields(world["source"])
                if field.name != "id"
            },
            "parent_id": world["source"].id,
            "budget": world["source"].budget,
        }
    )
    reservation = publish_effect(
        world["store"],
        world["store"],
        world["genesis"],
        world["operator"],
        world["signer"],
        contract_declaration_effect(exhaust),
        idempotency_key="exhaust",
    )
    assert reservation.accepted
    rejected_before = len(
        world["store"].scan_runtime_records(record_type="candidate.rejected")
    )
    with pytest.raises(ValueError, match="causal allowance exceeded"):
        publish_task_review_request(
            world["store"],
            world["source"].id,
            "final",
            world["target"].id,
            "Review",
            idempotency_key="unfunded-review",
        )
    assert (
        len(world["store"].scan_runtime_records(record_type="candidate.received"))
        == before + 2
    )
    assert (
        len(world["store"].scan_runtime_records(record_type="candidate.rejected"))
        == rejected_before + 1
    )
    assert not world["store"].get_assets_by_name("task-review/task-review-request")
    assert (
        allowance_available(
            world["source"],
            (record for _revision, record in world["store"].scan_runtime_records()),
        )
        == 0
    )
    from aigineering.plugins.task_review import task_review_asset_records

    assert not task_review_asset_records(world["store"], world["source"].id)


def test_nonclaim_asset_source_and_report_are_eligible_by_conditions(world):
    from aigineering.core.output_satisfaction import output_asset_is_eligible

    nonclaim_target = publish_effect(
        world["store"],
        world["store"],
        world["genesis"],
        world["target_actor"],
        world["signer"],
        asset_proposal_effect(
            Asset(id="unused", name="final", content='"Another source result"')
        ),
        idempotency_key="nonclaim-target",
    ).assets[0]
    validate_task_review_request(
        world["store"], world["source"].id, "final", nonclaim_target.id, "Review"
    )
    wrong_actor_target = publish_effect(
        world["store"],
        world["store"],
        world["genesis"],
        world["report_actor"],
        world["signer"],
        asset_proposal_effect(
            Asset(id="unused", name="final", content="Wrong producer")
        ),
        idempotency_key="wrong-producer-target",
    ).assets[0]
    with pytest.raises(ValueError, match="frozen output requirements"):
        validate_task_review_request(
            world["store"], world["source"].id, "final", wrong_actor_target.id, "Review"
        )
    empty_target = publish_effect(
        world["store"],
        world["store"],
        world["genesis"],
        world["target_actor"],
        world["signer"],
        asset_proposal_effect(Asset(id="unused", name="final", content='""')),
        idempotency_key="wrong-shape-target",
    ).assets[0]
    with pytest.raises(ValueError, match="frozen output requirements"):
        validate_task_review_request(
            world["store"], world["source"].id, "final", empty_target.id, "Review"
        )
    request, _ = publish_task_review_request(
        world["store"],
        world["source"].id,
        "final",
        nonclaim_target.id,
        "Review nonclaim output",
        idempotency_key="nonclaim-request",
    )
    relation_asset = world["store"].get_asset(request["asset_id"])
    assert relation_asset is not None
    ordinary_task = build_control_plane_contract(
        name="consume review descriptor",
        outputs=(relation_asset.name,),
        budget=1,
        acceptance_policy={"mode": "mechanical"},
    )
    assert output_asset_is_eligible(
        ordinary_task, relation_asset.name, relation_asset, world["store"]
    )
    request_relation = request["relation"]
    same_actor_report = publish_effect(
        world["store"],
        world["store"],
        world["genesis"],
        world["target_actor"],
        world["signer"],
        asset_proposal_effect(
            Asset(
                id="unused",
                name=request_relation["report_output"],
                content="Report from the target publisher",
            )
        ),
        idempotency_key="same-actor-report",
    ).assets[0]
    from aigineering.task_policy_review import validate_task_review_decision

    with pytest.raises(ValueError, match="must differ from target producer"):
        validate_task_review_decision(
            world["store"],
            request["asset_id"],
            same_actor_report.id,
            "accept",
            "Reject same producer",
        )


def test_descriptor_without_companion_declaration_is_not_review_history(world):
    from aigineering.plugins.task_review import (
        review_relation_asset,
        task_review_asset_records,
    )

    forged = review_relation_asset(
        "task-review-request",
        {
            "source_contract_id": world["source"].id,
            "output_name": "final",
            "asset_id": world["target"].id,
            "reviewer_contract_id": "forged-reviewer",
            "report_output": "review_report_forged",
            "policy_id": "forged-policy",
            "policy_version": "forged-version",
            "rubric_asset_ids": [],
            "evidence_asset_ids": [],
            "reason": "A descriptor alone is not a request",
            "budget": 1,
        },
    )
    published = publish_effect(
        world["store"],
        world["store"],
        world["genesis"],
        world["operator"],
        world["signer"],
        asset_proposal_effect(forged),
        idempotency_key="forged-descriptor",
    )
    assert published.accepted
    assert not task_review_asset_records(world["store"], world["source"].id)


def test_request_batch_rejection_has_no_partial_contract_or_asset(world):
    child = contract_from_fields(
        **{
            **{
                field.name: getattr(world["source"], field.name)
                for field in fields(world["source"])
                if field.name != "id"
            },
            "name": "rejected child",
            "parent_id": world["source"].id,
            "budget": 1,
        }
    )
    bad_relation = Asset(
        id="unused",
        name="_tool_obs_rejected",
        content="not authorized for this publisher",
    )
    before_contracts = {
        record.payload.get("contract", {}).get("id")
        for _revision, record in world["store"].scan_runtime_records(
            record_type="contract.declared"
        )
    }
    before_assets = {
        record.payload.get("asset", {}).get("id")
        for _revision, record in world["store"].scan_runtime_records(
            record_type="asset.committed"
        )
    }
    rejected = publish_effects(
        world["store"],
        world["store"],
        world["genesis"],
        world["operator"],
        world["signer"],
        (contract_declaration_effect(child), asset_proposal_effect(bad_relation)),
        idempotency_key="atomic-review-reject",
        causal_parents=(world["source"].id, world["target"].id),
    )
    assert not rejected.accepted
    after_contracts = {
        record.payload.get("contract", {}).get("id")
        for _revision, record in world["store"].scan_runtime_records(
            record_type="contract.declared"
        )
    }
    assert after_contracts == before_contracts
    after_assets = {
        record.payload.get("asset", {}).get("id")
        for _revision, record in world["store"].scan_runtime_records(
            record_type="asset.committed"
        )
    }
    assert after_assets == before_assets


def test_relation_assets_are_descriptive_and_history_exposes_asset_ids(world):
    request, _ = publish_task_review_request(
        world["store"],
        world["source"].id,
        "final",
        world["target"].id,
        "Check evidence",
        idempotency_key="request-history",
    )
    from aigineering.task_policy_review import task_review_records

    history, _ = task_review_records(world["store"], world["source"].id)
    assert len(history) == 1
    assert history[0]["asset_id"] == request["asset_id"]
    assert "record_type" not in history[0]
    assert "record_id" not in history[0]
    assert not world["store"].scan_runtime_records(record_type="output.qualified")
    reopened = SQLiteStore(world["store_path"])
    reopened_history, reopened_revision = task_review_records(
        reopened, world["source"].id
    )
    assert reopened_history[0]["asset_id"] == request["asset_id"]
    assert reopened_revision == world["store"].get_runtime_revision()
    reopened.close()


@pytest.mark.parametrize("verdict", ("accept", "changes"))
def test_decision_history_preserves_ordinary_relations(world, verdict):
    from aigineering.core.publication_history import validate_published_member
    from aigineering.task_policy_review import task_review_records

    store = world["store"]
    source = world["source"]
    request, _ = publish_task_review_request(
        store,
        source.id,
        "final",
        world["target"].id,
        "Check frozen evidence",
        idempotency_key="history-request",
    )
    reviewer = store.get_contract(request["relation"]["reviewer_contract_id"])
    report = _publish_claimed_output(
        store,
        world["genesis"],
        world["signer"],
        world["report_actor"],
        reviewer,
        request["relation"]["report_output"],
        "Independent report over frozen evidence",
    )
    decision, _ = publish_task_review_decision(
        store,
        request["asset_id"],
        report.id,
        verdict,
        "Check completed",
        idempotency_key="history-decision",
    )
    records = tuple(record for _revision, record in store.scan_runtime_records())
    before = tuple(
        (revision, record.id) for revision, record in store.scan_runtime_records()
    )
    relation_commit = next(
        record
        for record in records
        if record.record_type == "asset.committed"
        and record.payload["asset"]["id"] == decision["asset_id"]
    )
    request_commit = next(
        record
        for record in records
        if record.record_type == "asset.committed"
        and record.payload["asset"]["id"] == request["asset_id"]
    )
    receipt, head = validate_published_member(records, relation_commit)
    request_receipt, request_head = validate_published_member(records, request_commit)
    attestation = next(
        record for record in records if record.record_type == "asset.attested"
    )
    assert "candidate_id" not in attestation.payload
    assert len(attestation.causal_parents) > 1
    assert validate_published_member(
        records,
        attestation,
        expected_receipt=receipt.id,
        expected_head=head.id,
        allow_additional_causal_parents=True,
    ) == (receipt, head)
    with pytest.raises(ValueError, match="receipt witness changed"):
        validate_published_member(
            records,
            attestation,
            expected_receipt=request_receipt.id,
            allow_additional_causal_parents=True,
        )
    with pytest.raises(ValueError, match="head witness changed"):
        validate_published_member(
            records,
            attestation,
            expected_head=request_head.id,
            allow_additional_causal_parents=True,
        )
    replay, _ = publish_task_review_decision(
        store,
        request["asset_id"],
        report.id,
        verdict,
        "Check completed",
        idempotency_key="history-decision",
    )
    assert replay == decision
    assert (
        tuple((rev, record.id) for rev, record in store.scan_runtime_records())
        == before
    )
    history, revision = task_review_records(store, source.id)
    assert [row["asset_id"] for row in history] == [
        request["asset_id"],
        decision["asset_id"],
    ]
    assert [row["relation"] for row in history] == [
        request["relation"],
        decision["relation"],
    ]
    assert revision == store.get_runtime_revision()
    assert (
        tuple((rev, record.id) for rev, record in store.scan_runtime_records())
        == before
    )

    reopened = SQLiteStore(world["store_path"])
    try:
        assert task_review_records(reopened, source.id) == (history, revision)
        reopened.rebuild_runtime_materializations()
        assert task_review_records(reopened, source.id) == (history, revision)
        assert (
            tuple((rev, record.id) for rev, record in reopened.scan_runtime_records())
            == before
        )
    finally:
        reopened.close()


def test_legacy_review_facts_are_read_only_history(world):
    legacy = create_runtime_record(
        "task.review.requested",
        {
            "contract_id": world["source"].id,
            "asset_id": world["target"].id,
            "output_name": "final",
            "child_contract_id": "legacy-reviewer",
            "budget": 1,
            "reason": "Historical request",
        },
    )
    world["store"].append_runtime_record(legacy)
    revision_before = world["store"].get_runtime_revision()
    from aigineering.task_policy_review import task_review_records

    rows, revision = task_review_records(world["store"], world["source"].id)
    assert revision == revision_before
    assert rows[0]["record_id"] == legacy.id
    assert rows[0]["record_type"] == "task.review.requested"
    assert world["store"].get_runtime_revision() == revision_before
