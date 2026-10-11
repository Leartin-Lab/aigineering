"""Signed, immutable task-review requests and decisions."""

from __future__ import annotations

import pytest
from dataclasses import replace
from threading import Event, Thread

from aigineering.core.acceptance import acceptance_policy_id
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.commitment import CandidateCommitRejected, CandidateCommitter
from aigineering.core.projection_context import (
    EffectProjectionContext,
    load_effect_projection_context,
)
from aigineering.core.control_plane import (
    build_control_plane_contract,
)
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.task_review import (
    TASK_REVIEW_EFFECTS,
    project_task_review_request_effect,
    project_task_reviews,
)
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    CandidateEffect,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_attestation_effect,
    asset_proposal_effect,
    contract_declaration_effect,
    task_review_decision_effect,
    task_review_request_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Asset
from aigineering.core.disclosure import compute_disclosure


def _workspace(tmp_path, *, self_verifier=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(tmp_path / "review.sqlite")
    signer = Ed25519Signer()
    operator = ActorKey(
        "human:operator",
        "operator-key",
        signer.kind,
        signer.signer_id,
        (
            "asset.attest",
            "asset.publish",
            "contract.publish",
            "policy.review",
            "task.review.request",
        ),
    )
    limited_requester = ActorKey(
        "human:limited-requester",
        "limited-requester-key",
        signer.kind,
        signer.signer_id,
        ("contract.publish",),
    )
    target_caps = ["asset.publish", "worker.submit"]
    if self_verifier:
        target_caps.extend(("asset.attest", "task.review.decide", "policy.review"))
    target_worker = ActorKey(
        "worker:target",
        "target-key",
        signer.kind,
        signer.signer_id,
        tuple(target_caps),
    )
    review_worker = ActorKey(
        "worker:review",
        "review-worker-key",
        signer.kind,
        signer.signer_id,
        ("asset.publish", "worker.submit"),
    )
    verifier = ActorKey(
        "human:reviewer",
        "reviewer-key",
        signer.kind,
        signer.signer_id,
        ("asset.attest", "policy.review", "task.review.decide"),
    )
    limited_verifier = ActorKey(
        "human:limited-reviewer",
        "limited-reviewer-key",
        signer.kind,
        signer.signer_id,
        ("asset.attest", "task.review.decide"),
    )
    genesis = create_genesis_manifest(
        "task-review-test",
        (
            operator,
            limited_requester,
            target_worker,
            review_worker,
            verifier,
            limited_verifier,
        ),
        "policy:test",
    )
    initialize_genesis(store, genesis)
    rubric_result = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(
            Asset(id="unused-rubric", name="rubric", content="Review criteria")
        ),
        idempotency_key="publish-rubric",
    )
    twin_result = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(
            Asset(id="unused-rubric-twin", name="rubric", content="Unselected twin")
        ),
        idempotency_key="publish-rubric-twin",
    )
    evidence_result = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        asset_proposal_effect(
            Asset(id="unused-evidence", name="evidence", content="Source evidence")
        ),
        idempotency_key="publish-evidence",
    )
    rubric, rubric_twin, evidence = (
        rubric_result.assets[0],
        twin_result.assets[0],
        evidence_result.assets[0],
    )
    assert rubric_result.accepted and twin_result.accepted and evidence_result.accepted
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "required_attestations": 1,
        "rubric_asset_ids": [rubric.id],
        "evidence_asset_ids": [evidence.id],
        "verifier_capabilities": ["policy.review"],
    }
    source = build_control_plane_contract(
        name="research report",
        description="Produce the requested result.",
        outputs=("final",),
        budget=8,
        acceptance_policy=policy,
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        operator,
        signer,
        contract_declaration_effect(source),
        idempotency_key="declare-review-source",
    )
    assert declared.accepted
    target_asset = _publish_output(
        store, signer, genesis, target_worker, source, "final", "Research result"
    )
    return {
        "store": store,
        "signer": signer,
        "genesis": genesis,
        "operator": operator,
        "limited_requester": limited_requester,
        "target_worker": target_worker,
        "review_worker": review_worker,
        "verifier": verifier,
        "limited_verifier": limited_verifier,
        "source": source,
        "target": target_asset,
        "rubric": rubric,
        "rubric_twin": rubric_twin,
        "evidence": evidence,
        "policy": policy,
    }


def _publish_output(store, signer, genesis, actor, contract, name, content):
    store.register_worker(
        WorkerRegistration(actor.actor_id, actor_id=actor.actor_id, key_id=actor.key_id)
    )
    claim = store.claim_contract(contract.id, actor.actor_id, package_id=f"pkg:{name}")
    assert claim is not None
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=(
            asset_proposal_effect(Asset(id="untrusted", name=name, content=content)),
        ),
        signer=signer,
        idempotency_key=f"submit:{contract.id}:{name}",
        claim_binding=CandidateClaimBinding(
            contract_id=contract.id,
            claim_id=claim["claim_id"],
            claim_epoch=claim["epoch"],
            package_id=claim["package_id"],
        ),
    )
    decision = CandidateCommitter(store, store).commit(candidate, genesis)
    assert decision.accepted, decision
    return decision.assets[0]


def _request_candidate(
    env,
    *,
    actor=None,
    reason="Need independent review",
    budget=1,
    extra=None,
    asset_id=None,
):
    actor = actor or env["operator"]
    effect = task_review_request_effect(
        env["source"].id,
        "final",
        asset_id or env["target"].id,
        reason,
        budget=budget,
    )
    if extra:
        effect = CandidateEffect(effect.effect_type, {**effect.payload, **extra})
    return create_candidate_proposal(
        domain_id=env["genesis"].id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=(effect,),
        signer=env["signer"],
        idempotency_key=f"request:{reason}:{budget}:{asset_id or env['target'].id}",
    )


def _request(env, *, reason="Need independent review", budget=1, extra=None):
    candidate = _request_candidate(env, reason=reason, budget=budget, extra=extra)
    decision = CandidateCommitter(
        env["store"], env["store"], additional_effects=TASK_REVIEW_EFFECTS
    ).commit(candidate, env["genesis"])
    return candidate, decision


def _review_report(env, request_record):
    child = env["store"].get_contract(str(request_record.payload["child_contract_id"]))
    assert child is not None
    return child, _publish_output(
        env["store"],
        env["signer"],
        env["genesis"],
        env["review_worker"],
        child,
        child.outputs[0],
        '{"verdict":"reviewed"}',
    )


def _decision_candidate(
    env, actor, request_record, report, *, verdict="accept", reason="Reviewed"
):
    return create_candidate_proposal(
        domain_id=env["genesis"].id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=(
            task_review_decision_effect(request_record.id, report.id, verdict, reason),
        ),
        signer=env["signer"],
        idempotency_key=f"decision:{request_record.id}:{actor.actor_id}:{verdict}:{report.id}:{reason}",
    )


def _decide(env, actor, request_record, report, *, verdict="accept", reason="Reviewed"):
    candidate = _decision_candidate(
        env, actor, request_record, report, verdict=verdict, reason=reason
    )
    return candidate, CandidateCommitter(
        env["store"], env["store"], additional_effects=TASK_REVIEW_EFFECTS
    ).commit(candidate, env["genesis"])


def test_request_freezes_exact_promptable_context_and_reserves_child(tmp_path):
    env = _workspace(tmp_path)
    candidate, request_decision = _request(env)
    assert request_decision.accepted, request_decision
    request = project_task_reviews(request_decision.runtime_records)[0]
    child = env["store"].get_contract(request.payload["child_contract_id"])
    assert child is not None
    expected_context = {
        env["target"].id,
        env["rubric"].id,
        env["evidence"].id,
    }
    assert set(child.context_asset_ids) == expected_context
    assert child.labels == ("evidence", "final", "rubric")
    assert child.inputs == () and child.tool_scope == ()
    assert child.acceptance_policy is None
    assert child.origin == env["source"].origin
    assert child.parent_id == env["source"].id
    assert child.budget == 1
    for asset_id in expected_context:
        assert asset_id in child.description
    assert "ordinary /exec review report" in child.description
    assert "Do not attest" in child.description
    assert {
        asset.id for asset in compute_disclosure(child, env["store"])
    } == expected_context
    assert env["rubric_twin"].id not in child.context_asset_ids
    assert request.payload["candidate_id"] == candidate.id
    assert request.payload["policy_id"] == acceptance_policy_id(env["policy"])
    assert request.payload["report_output"] == child.outputs[0]
    assert request.payload["asset_id"] == env["target"].id
    assert not any(
        env["target"].content in str(record.payload)
        for record in request_decision.runtime_records
    )
    allowance_records = tuple(
        record for _, record in env["store"].scan_runtime_records()
    )
    assert allowance_available(env["source"], allowance_records) == 7
    env["store"].close()


@pytest.mark.parametrize("verdict", ["accept", "changes"])
def test_decision_reuses_canonical_attestation_and_replay_after_reopen(
    tmp_path, verdict
):
    env = _workspace(tmp_path)
    request_candidate, request_decision = _request(env)
    request = project_task_reviews(request_decision.runtime_records)[0]
    _child, report = _review_report(env, request)
    decision_candidate, decision = _decide(
        env, env["verifier"], request, report, verdict=verdict
    )
    assert decision.accepted, decision
    link = next(
        record
        for record in project_task_reviews(decision.runtime_records)
        if record.record_type == "task.review.decided"
    )
    assert link.payload["verdict"] == verdict
    assert link.payload["request_id"] == request.id
    attestations = env["store"].scan_runtime_records(record_type="asset.attested")
    assert len(attestations) == 1
    qualifications = env["store"].scan_runtime_records(record_type="output.qualified")
    assert len(qualifications) == (1 if verdict == "accept" else 0)
    original_request_records = tuple(
        record.id for record in request_decision.runtime_records
    )
    original_head = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.committed"
    )
    database = env["store"]._db_path
    env["store"].close()

    reopened = SQLiteStore(database)
    env["store"] = reopened
    repeated_request = CandidateCommitter(
        reopened, reopened, additional_effects=TASK_REVIEW_EFFECTS
    ).commit(request_candidate, env["genesis"])
    repeated_decision = CandidateCommitter(
        reopened, reopened, additional_effects=TASK_REVIEW_EFFECTS
    ).commit(decision_candidate, env["genesis"])
    assert repeated_request.accepted
    assert (
        tuple(record.id for record in repeated_request.runtime_records)
        == original_request_records
    )
    assert repeated_decision.accepted
    replayed_head = next(
        record
        for record in repeated_decision.runtime_records
        if record.record_type == "candidate.committed"
    )
    assert replayed_head.id == original_head.id
    assert (
        replayed_head.payload["committed_record_ids"]
        == original_head.payload["committed_record_ids"]
    )
    assert len(reopened.scan_runtime_records(record_type="asset.attested")) == 1
    assert len(reopened.scan_runtime_records(record_type="output.qualified")) == len(
        qualifications
    )
    if verdict == "accept":
        post_terminal_candidate = _request_candidate(
            env, reason="request after source completion"
        )
        post_terminal = CandidateCommitter(
            reopened, reopened, additional_effects=TASK_REVIEW_EFFECTS
        ).commit(post_terminal_candidate, env["genesis"])
        assert not post_terminal.accepted
        assert not project_task_reviews(post_terminal.runtime_records)
    if verdict == "changes":
        second_candidate, second_decision = _decide(
            env,
            env["verifier"],
            request,
            report,
            verdict="accept",
            reason="second decision must be rejected",
        )
        assert not second_decision.accepted
        assert any(
            record.record_type == "candidate.rejected"
            and record.payload.get("candidate_id") == second_candidate.id
            for record in second_decision.runtime_records
        )
        assert (
            len(reopened.scan_runtime_records(record_type="task.review.decided")) == 1
        )
        assert reopened.scan_runtime_records(record_type="output.qualified") == []
    reopened.close()


def test_foreign_report_rejected_but_exact_report_survives_same_name_collision(
    tmp_path,
):
    env = _workspace(tmp_path)
    _candidate, request_decision = _request(env)
    request = project_task_reviews(request_decision.runtime_records)[0]
    child, report = _review_report(env, request)
    foreign = publish_effect(
        env["store"],
        env["store"],
        env["genesis"],
        env["operator"],
        env["signer"],
        asset_proposal_effect(
            Asset(
                id="untrusted-foreign",
                name=child.outputs[0],
                content="same name, wrong ancestry",
            )
        ),
        idempotency_key="foreign-review-report",
    )
    assert foreign.accepted
    candidate, rejected = _decide(
        env, env["verifier"], request, foreign.assets[0], reason="wrong ancestry"
    )
    assert not rejected.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == candidate.id
        for record in rejected.runtime_records
    )
    assert env["store"].scan_runtime_records(record_type="output.qualified") == []
    assert not any(
        record.payload.get("contract_id") == env["source"].id
        for _, record in env["store"].scan_runtime_records(
            record_type="lifecycle.terminal"
        )
    )
    assert env["store"].get_contract(child.id) is not None
    _candidate, accepted = _decide(
        env, env["verifier"], request, report, reason="exact reviewer output"
    )
    assert accepted.accepted, accepted
    assert len(env["store"].scan_runtime_records(record_type="output.qualified")) == 1
    env["store"].close()


def test_target_producer_cannot_decide_its_own_review(tmp_path):
    env = _workspace(tmp_path, self_verifier=True)
    _candidate, request_decision = _request(env)
    request = project_task_reviews(request_decision.runtime_records)[0]
    _child, report = _review_report(env, request)
    candidate, rejected = _decide(
        env, env["target_worker"], request, report, verdict="accept"
    )
    assert not rejected.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == candidate.id
        for record in rejected.runtime_records
    )
    assert env["store"].scan_runtime_records(record_type="output.qualified") == []
    env["store"].close()


def test_report_from_another_review_request_cannot_be_substituted(tmp_path):
    env = _workspace(tmp_path)
    _first_candidate, first_decision = _request(env, reason="first review")
    _second_candidate, second_decision = _request(env, reason="second review")
    first_request = project_task_reviews(first_decision.runtime_records)[0]
    second_request = project_task_reviews(second_decision.runtime_records)[0]
    _first_child, first_report = _review_report(env, first_request)
    _second_child, second_report = _review_report(env, second_request)

    wrong_candidate, wrong_decision = _decide(
        env,
        env["verifier"],
        first_request,
        second_report,
        verdict="changes",
        reason="report belongs to second request",
    )
    assert not wrong_decision.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == wrong_candidate.id
        for record in wrong_decision.runtime_records
    )
    assert env["store"].scan_runtime_records(record_type="task.review.decided") == []
    correct_candidate, correct_decision = _decide(
        env, env["verifier"], first_request, first_report, verdict="changes"
    )
    assert correct_decision.accepted, correct_decision
    links = env["store"].scan_runtime_records(record_type="task.review.decided")
    assert len(links) == 1
    assert links[0][1].payload["request_id"] == first_request.id
    assert links[0][1].payload["report_asset_id"] == first_report.id
    assert correct_candidate.id == links[0][1].payload["candidate_id"]
    env["store"].close()


def test_reviewer_capability_must_include_policy_verifier_capability(tmp_path):
    env = _workspace(tmp_path)
    _candidate, request_decision = _request(env)
    request = project_task_reviews(request_decision.runtime_records)[0]
    _child, report = _review_report(env, request)
    candidate, rejected = _decide(
        env, env["limited_verifier"], request, report, verdict="accept"
    )
    assert not rejected.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == candidate.id
        and "policy.review" in record.payload.get("reason", "")
        for record in rejected.runtime_records
    )
    assert env["store"].scan_runtime_records(record_type="output.qualified") == []
    env["store"].close()


@pytest.mark.parametrize("verdict", [None, True, ["accept"], "accepted"])
def test_decision_rejects_noncanonical_verdict_values(tmp_path, verdict):
    env = _workspace(tmp_path)
    _candidate, request_decision = _request(env)
    request = project_task_reviews(request_decision.runtime_records)[0]
    _child, report = _review_report(env, request)
    candidate = create_candidate_proposal(
        domain_id=env["genesis"].id,
        actor_id=env["verifier"].actor_id,
        key_id=env["verifier"].key_id,
        effects=(
            CandidateEffect(
                "task.review.decide",
                {
                    "request_id": request.id,
                    "report_asset_id": report.id,
                    "verdict": verdict,
                    "reason": "must use the public verdict vocabulary",
                },
            ),
        ),
        signer=env["signer"],
        idempotency_key=f"bad-verdict:{verdict!r}",
    )
    rejected = CandidateCommitter(
        env["store"], env["store"], additional_effects=TASK_REVIEW_EFFECTS
    ).commit(candidate, env["genesis"])
    assert not rejected.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == candidate.id
        for record in rejected.runtime_records
    )
    assert env["store"].scan_runtime_records(record_type="task.review.decided") == []
    env["store"].close()


def test_request_rejects_non_disclosable_or_missing_frozen_context(tmp_path):
    for mode in ("missing", "redacted", "nonpromptable", "sealed"):
        env = _workspace(tmp_path / mode)
        candidate = _request_candidate(env, reason=f"preflight {mode}")
        context = load_effect_projection_context(env["store"])
        if mode == "missing":
            assets = tuple(
                asset for asset in context.assets if asset.id != env["evidence"].id
            )
        else:
            changed = replace(
                env["evidence"],
                promptable=False if mode == "nonpromptable" else True,
                disclosure_view="redacted" if mode == "redacted" else "original",
                origin="sealed" if mode == "sealed" else env["evidence"].origin,
            )
            assets = tuple(
                changed if asset.id == changed.id else asset for asset in context.assets
            )
        context = EffectProjectionContext(
            contracts=context.contracts,
            assets=assets,
            runtime_records=context.runtime_records,
            actor_keys=context.actor_keys,
        )
        with pytest.raises(ValueError, match="context|disclosable"):
            project_task_review_request_effect(
                candidate.effects[0], candidate, "record:receipt", context
            )
        env["store"].close()


def test_request_rejects_external_same_name_asset_and_missing_capability(tmp_path):
    env = _workspace(tmp_path)
    external = publish_effect(
        env["store"],
        env["store"],
        env["genesis"],
        env["operator"],
        env["signer"],
        asset_proposal_effect(
            Asset(id="external", name="final", content="unrelated asset")
        ),
        idempotency_key="external-same-output-name",
    )
    assert external.accepted
    wrong_target = _request_candidate(
        env, reason="wrong target", asset_id=external.assets[0].id
    )
    rejected_target = CandidateCommitter(
        env["store"], env["store"], additional_effects=TASK_REVIEW_EFFECTS
    ).commit(wrong_target, env["genesis"])
    assert not rejected_target.accepted
    assert not project_task_reviews(rejected_target.runtime_records)

    no_cap = _request_candidate(
        env, actor=env["limited_requester"], reason="no capability"
    )
    rejected_capability = CandidateCommitter(
        env["store"], env["store"], additional_effects=TASK_REVIEW_EFFECTS
    ).commit(no_cap, env["genesis"])
    assert not rejected_capability.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == no_cap.id
        and "task.review.request" in record.payload.get("reason", "")
        for record in rejected_capability.runtime_records
    )
    env["store"].close()


def test_request_snapshot_recheck_arbitrates_source_reservation_race(tmp_path):
    env = _workspace(tmp_path)
    competing_store = SQLiteStore(env["store"]._db_path)
    request_candidate = _request_candidate(env, reason="race against reservation")
    reached = Event()
    release = Event()
    original_commit = env["store"].commit_ingress_batch

    def pause_before_write(*args, **kwargs):
        if kwargs.get("validate_snapshot") is not None and not reached.is_set():
            reached.set()
            if not release.wait(10):
                raise TimeoutError("review request race release timed out")
        return original_commit(*args, **kwargs)

    env["store"].commit_ingress_batch = pause_before_write
    outcomes = []
    errors = []

    def request_review():
        try:
            outcomes.append(
                CandidateCommitter(
                    env["store"],
                    env["store"],
                    additional_effects=TASK_REVIEW_EFFECTS,
                ).commit(request_candidate, env["genesis"])
            )
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=request_review)
    thread.start()
    assert reached.wait(10)
    try:
        sibling = contract_from_fields(
            name="allowance competitor",
            description="Consume the remaining source allowance.",
            parent_id=env["source"].id,
            inputs=(),
            outputs=("other_result",),
            activation="",
            budget=8,
            tool_scope=(),
            labels=(),
            origin="human",
        )
        competing = publish_effect(
            competing_store,
            competing_store,
            env["genesis"],
            env["operator"],
            env["signer"],
            contract_declaration_effect(sibling),
            idempotency_key="consume-source-allowance",
        )
        assert competing.accepted, competing
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors, errors
    assert len(outcomes) == 1 and not outcomes[0].accepted
    assert not project_task_reviews(outcomes[0].runtime_records)
    assert not any(
        contract.parent_id == env["source"].id and contract.name.startswith("Review ")
        for contract in env["store"].get_all_contracts()
    )
    competing_store.close()
    env["store"].close()


def test_request_snapshot_rejects_source_completed_before_write(tmp_path):
    env = _workspace(tmp_path)
    competing_store = SQLiteStore(env["store"]._db_path)
    request_candidate = _request_candidate(
        env, reason="race against source qualification"
    )
    reached = Event()
    release = Event()
    original_commit = env["store"].commit_ingress_batch

    def pause_before_write(*args, **kwargs):
        if kwargs.get("validate_snapshot") is not None and not reached.is_set():
            reached.set()
            if not release.wait(10):
                raise TimeoutError("review request race release timed out")
        return original_commit(*args, **kwargs)

    env["store"].commit_ingress_batch = pause_before_write
    outcomes = []
    errors = []

    def request_review():
        try:
            outcomes.append(
                CandidateCommitter(
                    env["store"],
                    env["store"],
                    additional_effects=TASK_REVIEW_EFFECTS,
                ).commit(request_candidate, env["genesis"])
            )
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=request_review)
    thread.start()
    assert reached.wait(10)
    try:
        qualification = publish_effect(
            competing_store,
            competing_store,
            env["genesis"],
            env["operator"],
            env["signer"],
            asset_attestation_effect(
                env["source"].id,
                "final",
                env["target"].id,
                policy_id=acceptance_policy_id(env["policy"]),
                policy_version="review-v1",
                verdict="accepted",
                rubric_asset_ids=(env["rubric"].id,),
                evidence_asset_ids=(env["evidence"].id,),
            ),
            idempotency_key="qualify-source-before-review-request",
        )
        assert qualification.accepted, qualification
        terminal_source = [
            record
            for _, record in competing_store.scan_runtime_records(
                record_type="lifecycle.terminal"
            )
            if record.payload.get("contract_id") == env["source"].id
        ]
        assert len(terminal_source) == 1
        assert terminal_source[0].payload["terminal"] == "complete"
    finally:
        release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert not errors, errors
    assert len(outcomes) == 1 and not outcomes[0].accepted
    assert not any(
        record.record_type == "task.review.requested"
        and record.payload.get("candidate_id") == request_candidate.id
        for _, record in env["store"].scan_runtime_records()
    )
    assert not any(
        contract.parent_id == env["source"].id and contract.name.startswith("Review ")
        for contract in env["store"].get_all_contracts()
    )
    assert not any(
        record.record_type == "allowance.reserved" and "Review " in str(record.payload)
        for _, record in env["store"].scan_runtime_records()
    )
    assert len(env["store"].scan_runtime_records(record_type="output.qualified")) == 1
    competing_store.close()
    env["store"].close()


def test_acceptance_cancels_and_fences_active_direct_child(tmp_path):
    env = _workspace(tmp_path)
    child = contract_from_fields(
        name="unfinished source child",
        description="Work that is no longer needed after source completion.",
        parent_id=env["source"].id,
        inputs=(),
        outputs=("subtask_result",),
        activation="",
        budget=1,
        tool_scope=(),
        labels=(),
        origin="human",
    )
    declared_child = publish_effect(
        env["store"],
        env["store"],
        env["genesis"],
        env["operator"],
        env["signer"],
        contract_declaration_effect(child),
        idempotency_key="declare-unfinished-review-source-child",
    )
    assert declared_child.accepted, declared_child
    env["store"].register_worker(
        WorkerRegistration(
            env["target_worker"].actor_id,
            actor_id=env["target_worker"].actor_id,
            key_id=env["target_worker"].key_id,
        )
    )
    claim = env["store"].claim_contract(
        child.id, env["target_worker"].actor_id, package_id="pkg:unfinished-child"
    )
    assert claim is not None
    late_submission = create_candidate_proposal(
        domain_id=env["genesis"].id,
        actor_id=env["target_worker"].actor_id,
        key_id=env["target_worker"].key_id,
        effects=(
            asset_proposal_effect(
                Asset(
                    id="asset:late-child-output",
                    name="subtask_result",
                    content="late child result",
                )
            ),
        ),
        signer=env["signer"],
        idempotency_key="late-unfinished-child-submit",
        claim_binding=CandidateClaimBinding(
            contract_id=child.id,
            claim_id=claim["claim_id"],
            claim_epoch=claim["epoch"],
            package_id=claim["package_id"],
        ),
    )

    _request_candidate_value, request_decision = _request(env)
    assert request_decision.accepted, request_decision
    request = project_task_reviews(request_decision.runtime_records)[0]
    _reviewer, report = _review_report(env, request)
    _decision_candidate_value, decision = _decide(
        env, env["verifier"], request, report, verdict="accept"
    )
    assert decision.accepted, decision

    source_terminal = [
        record
        for _, record in env["store"].scan_runtime_records(
            record_type="lifecycle.terminal"
        )
        if record.payload.get("contract_id") == env["source"].id
    ]
    child_terminal = [
        record
        for _, record in env["store"].scan_runtime_records(
            record_type="lifecycle.terminal"
        )
        if record.payload.get("contract_id") == child.id
    ]
    assert len(source_terminal) == 1
    assert source_terminal[0].payload["terminal"] == "complete"
    assert len(child_terminal) == 1
    assert child_terminal[0].payload["terminal"] == "cancelled"
    assert env["store"].get_claim(child.id)["status"] == "released"
    assert any(
        record.payload.get("claim_id") == claim["claim_id"]
        for _, record in env["store"].scan_runtime_records(record_type="claim.released")
    )
    with pytest.raises(CandidateCommitRejected):
        CandidateCommitter(env["store"], env["store"]).commit(
            late_submission, env["genesis"]
        )
    assert env["store"].get_assets_by_name("subtask_result") == []
    env["store"].close()


@pytest.mark.parametrize("winner_verdict", ["accept", "changes"])
def test_competing_decisions_are_fenced_to_one_per_request(tmp_path, winner_verdict):
    env = _workspace(tmp_path)
    _request_candidate_value, request_decision = _request(env)
    request = project_task_reviews(request_decision.runtime_records)[0]
    _child, report = _review_report(env, request)
    losing_verdict = "changes" if winner_verdict == "accept" else "accept"
    winning_candidate = _decision_candidate(
        env,
        env["verifier"],
        request,
        report,
        verdict=winner_verdict,
        reason=f"winning {winner_verdict}",
    )
    losing_candidate = _decision_candidate(
        env,
        env["verifier"],
        request,
        report,
        verdict=losing_verdict,
        reason="stale competing accept",
    )
    competing_store = SQLiteStore(env["store"]._db_path)
    reached = Event()
    release = Event()
    original_commit = env["store"].commit_ingress_batch

    def pause_before_write(*args, **kwargs):
        if kwargs.get("validate_snapshot") is not None and not reached.is_set():
            reached.set()
            if not release.wait(10):
                raise TimeoutError("review decision race release timed out")
        return original_commit(*args, **kwargs)

    env["store"].commit_ingress_batch = pause_before_write
    outcomes = []
    errors = []

    def commit_stale_accept():
        try:
            outcomes.append(
                CandidateCommitter(
                    env["store"],
                    env["store"],
                    additional_effects=TASK_REVIEW_EFFECTS,
                ).commit(losing_candidate, env["genesis"])
            )
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=commit_stale_accept)
    thread.start()
    assert reached.wait(10)
    try:
        winner = CandidateCommitter(
            competing_store,
            competing_store,
            additional_effects=TASK_REVIEW_EFFECTS,
        ).commit(winning_candidate, env["genesis"])
        assert winner.accepted, winner
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors, errors
    assert len(outcomes) == 1 and not outcomes[0].accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == losing_candidate.id
        for record in outcomes[0].runtime_records
    )
    links = env["store"].scan_runtime_records(record_type="task.review.decided")
    assert len(links) == 1 and links[0][1].payload["verdict"] == winner_verdict
    qualifications = env["store"].scan_runtime_records(record_type="output.qualified")
    assert len(qualifications) == (1 if winner_verdict == "accept" else 0)
    terminal_sources = [
        record
        for _, record in env["store"].scan_runtime_records(
            record_type="lifecycle.terminal"
        )
        if record.payload.get("contract_id") == env["source"].id
    ]
    assert len(terminal_sources) == (1 if winner_verdict == "accept" else 0)
    if terminal_sources:
        assert terminal_sources[0].payload["terminal"] == "complete"
    competing_store.close()
    env["store"].close()


@pytest.mark.parametrize(
    "budget,extra",
    [(True, None), (0, None), (1, {"actor_id": "human:spoof"}), (1, {"unknown": "x"})],
)
def test_request_rejects_malformed_bounds_and_caller_identity(tmp_path, budget, extra):
    env = _workspace(tmp_path)
    candidate, rejected = _request(env, budget=budget, extra=extra)
    assert not rejected.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == candidate.id
        for record in rejected.runtime_records
    )
    env["store"].close()


def test_request_requires_exact_budget_and_review_capability(tmp_path):
    env = _workspace(tmp_path)
    candidate, rejected = _request(env, budget=99)
    assert not rejected.accepted
    assert any(
        record.record_type == "candidate.rejected"
        for record in rejected.runtime_records
    )
    env["store"].close()
