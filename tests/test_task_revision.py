"""Signed task revision creates bounded successor work after review changes."""

from __future__ import annotations

from dataclasses import replace

import pytest

from aigineering.core.commitment import CandidateCommitter
from aigineering.core.acceptance import acceptance_policy_id
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import initialize_genesis
from aigineering.core.disclosure import compute_disclosure
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.task_revision import (
    TASK_REVISION_EFFECTS,
    project_task_revision_effect,
    validate_task_revision_references,
)
from aigineering.plugins.task_review import (
    TASK_REVIEW_EFFECTS,
    project_task_reviews,
)
from aigineering.protocol.candidate import (
    ActorKey,
    CandidateClaimBinding,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    asset_proposal_effect,
    contract_declaration_effect,
    task_review_decision_effect,
    task_review_request_effect,
    task_revision_effect,
    task_dependency_link_effect,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.effect_builders import asset_attestation_effect
from aigineering.protocol.types import Asset


def _env(tmp_path):
    store = SQLiteStore(tmp_path / "revision.sqlite")
    signer = Ed25519Signer()
    authority = ActorKey(
        "test:authority",
        "authority-key",
        signer.kind,
        signer.signer_id,
        (
            "actor.authorize",
            "asset.publish",
            "contract.publish",
            "worker.register",
        ),
    )
    genesis = create_genesis_manifest(
        "task-revision-test", (authority,), "policy:revision-test"
    )
    initialize_genesis(store, genesis)

    def authorize(actor_id, key_id, capabilities):
        actor = ActorKey(
            actor_id,
            key_id,
            signer.kind,
            signer.signer_id,
            tuple(capabilities),
        )
        setup = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=authority.actor_id,
            key_id=authority.key_id,
            effects=(actor_authorization_effect(actor),),
            signer=signer,
            idempotency_key=f"authorize:{actor_id}",
        )
        decision = CandidateCommitter(store, store).commit(setup, genesis)
        assert decision.accepted, decision
        return actor

    operator = authorize(
        "human:operator",
        "operator-key",
        (
            "asset.publish",
            "contract.publish",
            "task.review.request",
            "task.revise",
            "contract.cancel",
        ),
    )
    target_worker = authorize(
        "worker:target", "target-key", ("asset.publish", "worker.submit")
    )
    review_worker = authorize(
        "worker:review", "review-key", ("asset.publish", "worker.submit")
    )
    reviewer = authorize(
        "human:reviewer",
        "reviewer-key",
        ("asset.attest", "policy.review", "task.review.decide"),
    )

    rubric = _publish_asset(
        store, genesis, signer, operator, "rubric", "Check accuracy"
    )
    evidence = _publish_asset(
        store, genesis, signer, operator, "evidence", "Research source"
    )
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "required_attestations": 1,
        "rubric_asset_ids": [rubric.id],
        "evidence_asset_ids": [evidence.id],
        "verifier_capabilities": ["policy.review"],
    }
    source_input = _publish_asset(
        store, genesis, signer, operator, "dataset", "Pinned source material"
    )
    source = build_control_plane_contract(
        name="report",
        inputs=("dataset",),
        description="Create a checked report.",
        outputs=("draft", "final"),
        budget=8,
        sensitive_input_policy={"required_signer": operator.actor_id},
        acceptance_policy=policy,
    )
    declared = _commit(
        store,
        genesis,
        signer,
        operator,
        contract_declaration_effect(source),
        "declare-source",
    )
    assert declared.accepted, declared
    draft, final = _publish_outputs(
        store, genesis, signer, target_worker, source, "final", "Initial final"
    )
    qualification = _commit(
        store,
        genesis,
        signer,
        reviewer,
        asset_attestation_effect(
            source.id,
            "draft",
            draft.id,
            policy_id=acceptance_policy_id(policy),
            policy_version="review-v1",
            rubric_asset_ids=(rubric.id,),
            evidence_asset_ids=(evidence.id,),
        ),
        "qualify-draft",
    )
    assert qualification.accepted, qualification
    request_effect = task_review_request_effect(
        source.id, "final", final.id, "Please check the final output", budget=1
    )
    request_result = _commit(
        store,
        genesis,
        signer,
        operator,
        request_effect,
        "review-request",
        effects=TASK_REVIEW_EFFECTS,
    )
    assert request_result.accepted, request_result
    request = project_task_reviews(request_result.runtime_records)[0]
    reviewer_contract = store.get_contract(request.payload["child_contract_id"])
    assert reviewer_contract is not None
    report = _publish_output(
        store,
        genesis,
        signer,
        review_worker,
        reviewer_contract,
        reviewer_contract.outputs[0],
        "Check the report against the frozen evidence.",
    )
    decision_result = _commit(
        store,
        genesis,
        signer,
        reviewer,
        task_review_decision_effect(request.id, report.id, "changes", "Revise final"),
        "review-changes",
        effects=TASK_REVIEW_EFFECTS,
    )
    assert decision_result.accepted, decision_result
    decision_link = project_task_reviews(decision_result.runtime_records)[-1]
    return {
        "store": store,
        "genesis": genesis,
        "signer": signer,
        "operator": operator,
        "target_worker": target_worker,
        "reviewer": reviewer,
        "source": source,
        "draft": draft,
        "final": final,
        "report": report,
        "decision": decision_link,
        "request": request,
        "rubric": rubric,
        "evidence": evidence,
        "source_input": source_input,
    }


def _publish_asset(store, genesis, signer, actor, name, content):
    result = _commit(
        store,
        genesis,
        signer,
        actor,
        asset_proposal_effect(Asset(id="untrusted", name=name, content=content)),
        f"publish:{name}",
    )
    assert result.accepted, [r.payload for r in result.runtime_records]
    return result.assets[0]


def _publish_outputs(
    store, genesis, signer, actor, contract, final_name, final_content
):
    store.register_worker(
        WorkerRegistration(actor.actor_id, actor_id=actor.actor_id, key_id=actor.key_id)
    )
    claim = store.claim_contract(
        contract.id, actor.actor_id, package_id="pkg:all-outputs"
    )
    assert claim is not None
    candidate = create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=(
            asset_proposal_effect(
                Asset(id="untrusted-draft", name="draft", content="Initial draft")
            ),
            asset_proposal_effect(
                Asset(id="untrusted-final", name=final_name, content=final_content)
            ),
        ),
        signer=signer,
        idempotency_key=f"submit:{contract.id}:all-outputs",
        claim_binding=CandidateClaimBinding(
            contract_id=contract.id,
            claim_id=claim["claim_id"],
            claim_epoch=claim["epoch"],
            package_id=claim["package_id"],
        ),
    )
    result = CandidateCommitter(store, store).commit(candidate, genesis)
    assert result.accepted, [
        (record.record_type, record.payload.get("reason"))
        for record in result.runtime_records
    ]
    assets_by_name = {asset.name: asset for asset in result.assets}
    return assets_by_name["draft"], assets_by_name[final_name]


def _publish_output(store, genesis, signer, actor, contract, name, content):
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
    result = CandidateCommitter(store, store).commit(candidate, genesis)
    assert result.accepted, [
        (record.record_type, record.payload.get("reason"))
        for record in result.runtime_records
    ]
    return result.assets[0]


def _commit(store, genesis, signer, actor, effect, idem, *, effects=None):
    candidate = _candidate(genesis, signer, actor, effect, idem)
    return CandidateCommitter(store, store, additional_effects=effects).commit(
        candidate, genesis
    )


def _candidate(genesis, signer, actor, effect, idem):
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor.actor_id,
        key_id=actor.key_id,
        effects=(effect,),
        signer=signer,
        idempotency_key=idem,
    )


def test_changes_decision_creates_bounded_revision_and_retains_qualified_slots(
    tmp_path,
):
    env = _env(tmp_path)
    store = env["store"]
    # The draft is currently an ordinary output and the final slot is the one
    # under review, so both remain obligations until independently qualified.
    effect = task_revision_effect(env["decision"].id, "Address the review findings")
    result = _commit(
        store,
        env["genesis"],
        env["signer"],
        env["operator"],
        effect,
        "revise-after-changes",
        effects={**TASK_REVIEW_EFFECTS, **TASK_REVISION_EFFECTS},
    )
    assert result.accepted, result
    link = next(
        record
        for record in result.runtime_records
        if record.record_type == "task.revised"
    )
    successor = store.get_contract(link.payload["successor_id"])
    assert successor is not None
    assert successor.parent_id == env["source"].id
    assert successor.outputs == ("final",)
    assert successor.budget == 7  # source 8, review request reserves 1
    assert successor.acceptance_policy == env["source"].acceptance_policy
    assert successor.worker_capabilities == env["source"].worker_capabilities
    assert successor.id.startswith("task:v6:")
    assert successor.sensitive_input_policy["input_asset_bindings"] == {
        "dataset": (env["source_input"].id,)
    }
    assert link.payload["completed_outputs"] == ("draft",)
    assert link.payload["retained_asset_ids"] == (env["draft"].id,)
    assert link.payload["asset_id"] == env["final"].id
    assert link.payload["report_asset_id"] == env["report"].id
    twin = _publish_asset(
        store,
        env["genesis"],
        env["signer"],
        env["operator"],
        "dataset",
        "Same-name data added after the successor was committed.",
    )
    disclosed_ids = {asset.id for asset in compute_disclosure(successor, store)}
    assert env["source_input"].id in disclosed_ids
    assert twin.id not in disclosed_ids
    assert env["report"].id in disclosed_ids
    assert env["report"].id not in successor.acceptance_policy["evidence_asset_ids"]
    assert (
        store.claim_contract(env["source"].id, "worker:target", package_id="late")
        is None
    )
    assert (
        store.claim_contract(successor.id, "worker:target", package_id="new")
        is not None
    )
    original_head = next(
        record
        for record in result.runtime_records
        if record.record_type == "candidate.committed"
    )
    database = store._db_path
    store.close()
    reopened = SQLiteStore(database)
    replay = _commit(
        reopened,
        env["genesis"],
        env["signer"],
        env["operator"],
        effect,
        "revise-after-changes",
        effects={**TASK_REVIEW_EFFECTS, **TASK_REVISION_EFFECTS},
    )
    replayed_head = next(
        record
        for record in replay.runtime_records
        if record.record_type == "candidate.committed"
    )
    assert replay.accepted
    assert replayed_head.id == original_head.id
    assert (
        replayed_head.payload["committed_record_ids"]
        == original_head.payload["committed_record_ids"]
    )
    reopened.close()


def test_revision_rejects_unfinished_external_prerequisite_on_cancelled_source(
    tmp_path,
):
    env = _env(tmp_path)
    source = env["source"]
    external = build_control_plane_contract(
        name="revision external prerequisite",
        description="unfinished prerequisite",
        outputs=("result",),
        budget=1,
    )
    dependency = task_dependency_link_effect(
        source.id, external.id, "wait for prerequisite"
    )
    constraint = create_runtime_record(
        "claim.constraint.placed",
        {
            "actor_id": "human:operator",
            "candidate_id": "candidate:review-dependency",
            "contract_id": source.id,
            "key_id": "operator-key",
            "predicate": "prerequisite_terminal",
            "prerequisite_contract_id": external.id,
            "reason": str(dependency.payload["reason"]),
            "required_terminal": "complete",
        },
        causal_parents=("record:dependency-receipt",),
    )
    context = load_effect_projection_context(env["store"])
    context = replace(
        context,
        contracts=(*context.contracts, external),
        runtime_records=(*context.runtime_records, constraint),
    )
    effect = task_revision_effect(
        env["decision"].id, "Revise only after prerequisite completion"
    )
    candidate = _candidate(
        env["genesis"],
        env["signer"],
        env["operator"],
        effect,
        "blocked-revision-dependency",
    )

    with pytest.raises(ValueError, match=constraint.id):
        project_task_revision_effect(
            effect, candidate, "record:revision-receipt", context
        )


def test_revision_references_are_a_shared_pure_preflight_owner(tmp_path):
    env = _env(tmp_path)
    from aigineering.core.projection_context import load_effect_projection_context

    context = load_effect_projection_context(env["store"])
    refs = validate_task_revision_references(context, env["decision"].id)
    assert refs.source.id == env["source"].id
    assert refs.request.id == env["request"].id
    assert refs.decision.id == env["decision"].id
    assert refs.target.id == env["final"].id
    assert refs.report.id == env["report"].id
    reviewer_id = env["request"].payload["child_contract_id"]
    omissions = (
        tuple(
            record
            for record in context.runtime_records
            if record.record_type != "asset.attested"
        ),
        tuple(
            record
            for record in context.runtime_records
            if not (
                record.record_type == "contract.declared"
                and record.payload.get("contract", {}).get("id") == reviewer_id
            )
        ),
        tuple(
            record
            for record in context.runtime_records
            if not (
                record.record_type == "asset.committed"
                and record.payload.get("asset", {}).get("id") == env["report"].id
            )
        ),
        tuple(
            record
            for record in context.runtime_records
            if not (
                record.record_type == "candidate.committed"
                and record.payload.get("candidate_id")
                == env["decision"].payload.get("candidate_id")
            )
        ),
    )
    for records in omissions:
        with pytest.raises(ValueError):
            validate_task_revision_references(
                replace(context, runtime_records=records), env["decision"].id
            )
    env["store"].close()


def test_fresh_writer_snapshot_rejects_revision_if_exact_output_wins(tmp_path):
    env = _env(tmp_path)
    store = env["store"]
    effects = {**TASK_REVIEW_EFFECTS, **TASK_REVISION_EFFECTS}
    revision_effect = task_revision_effect(env["decision"].id, "Revise the report")
    revision_candidate = _candidate(
        env["genesis"], env["signer"], env["operator"], revision_effect, "race-revision"
    )
    other = SQLiteStore(store._db_path)
    original = store.commit_ingress_batch
    raced = False

    def force_competing_commit(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            competing = _commit(
                other,
                env["genesis"],
                env["signer"],
                env["reviewer"],
                asset_attestation_effect(
                    env["source"].id,
                    "final",
                    env["final"].id,
                    policy_id=acceptance_policy_id(env["source"].acceptance_policy),
                    policy_version="review-v1",
                    rubric_asset_ids=(env["rubric"].id,),
                    evidence_asset_ids=(env["evidence"].id,),
                ),
                "accept-output-before-revision-commit",
            )
            assert competing.accepted, competing
        return original(*args, **kwargs)

    store.commit_ingress_batch = force_competing_commit
    raced_revision = CandidateCommitter(
        store, store, additional_effects=effects
    ).commit(revision_candidate, env["genesis"])
    assert not raced_revision.accepted
    assert any(
        record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == revision_candidate.id
        for record in raced_revision.runtime_records
    )
    assert not store.scan_runtime_records(record_type="task.revised")
    assert not any(
        contract.name and ".revision." in contract.name
        for contract in store.get_all_contracts()
    )
    source = store.get_contract(env["source"].id)
    assert source is not None
    assert RuntimeProjection(store, store).contract_view(source).terminal == "complete"
    store.close()
    other.close()


def test_fresh_writer_snapshot_rejects_same_name_input_added_before_commit(tmp_path):
    env = _env(tmp_path)
    store = env["store"]
    effects = {**TASK_REVIEW_EFFECTS, **TASK_REVISION_EFFECTS}
    candidate = _candidate(
        env["genesis"],
        env["signer"],
        env["operator"],
        task_revision_effect(env["decision"].id, "Pin the source input set"),
        "revision-input-race",
    )
    other = SQLiteStore(store._db_path)
    original = store.commit_ingress_batch
    raced = False

    def add_same_name_input_before_commit(*args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            twin = _commit(
                other,
                env["genesis"],
                env["signer"],
                env["operator"],
                asset_proposal_effect(
                    Asset(
                        id="untrusted-late-dataset",
                        name="dataset",
                        content="A new same-name input added after projection.",
                    )
                ),
                "publish-input-twin-before-revision",
            )
            assert twin.accepted, twin
        return original(*args, **kwargs)

    store.commit_ingress_batch = add_same_name_input_before_commit
    result = CandidateCommitter(store, store, additional_effects=effects).commit(
        candidate, env["genesis"]
    )
    assert not result.accepted
    assert not store.scan_runtime_records(record_type="task.revised")
    assert not any(
        contract.name and ".revision." in contract.name
        for contract in store.get_all_contracts()
    )
    store.close()
    other.close()


def test_revision_fails_closed_for_oversized_budget_and_terminal_source(tmp_path):
    env = _env(tmp_path)
    store = env["store"]
    effects = {**TASK_REVIEW_EFFECTS, **TASK_REVISION_EFFECTS}
    oversized = _commit(
        store,
        env["genesis"],
        env["signer"],
        env["operator"],
        task_revision_effect(env["decision"].id, "Too much budget", budget=99),
        "oversized-revision-budget",
        effects=effects,
    )
    assert not oversized.accepted
    assert not store.scan_runtime_records(record_type="task.revised")
    assert not any(
        ".revision." in (contract.name or "") for contract in store.get_all_contracts()
    )

    accepted = _commit(
        store,
        env["genesis"],
        env["signer"],
        env["reviewer"],
        asset_attestation_effect(
            env["source"].id,
            "final",
            env["final"].id,
            policy_id=acceptance_policy_id(env["source"].acceptance_policy),
            policy_version="review-v1",
            rubric_asset_ids=(env["rubric"].id,),
            evidence_asset_ids=(env["evidence"].id,),
        ),
        "complete-before-revision",
    )
    assert accepted.accepted, accepted
    terminal_source = store.get_contract(env["source"].id)
    assert terminal_source is not None
    assert (
        RuntimeProjection(store, store).contract_view(terminal_source).terminal
        == "complete"
    )
    after_terminal = _commit(
        store,
        env["genesis"],
        env["signer"],
        env["operator"],
        task_revision_effect(env["decision"].id, "Too late"),
        "revision-after-terminal",
        effects=effects,
    )
    assert not after_terminal.accepted
    assert not store.scan_runtime_records(record_type="task.revised")
    store.close()
