"""Shared task views for submitted Worker claims and pending qualification."""

from __future__ import annotations

import json
from dataclasses import replace

import click
from click.testing import CliRunner

from aigineering.cli import task_board as task_board_cli
from aigineering.cli.run import _finish_target_run
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.task_views import project_task_status
from aigineering.task_view_query import load_task_view_query_context
from conftest import candidate_runtime, hosted_worker


class _OutputWorker:
    def __init__(self, worker_id: str, outputs: dict[str, str]):
        self.worker_id = worker_id
        self.outputs = outputs

    def invoke(self, contract, disclosed_assets):
        del contract, disclosed_assets
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec " + json.dumps({"outputs": self.outputs}),
        )


def _publish_task(
    store, publisher, *, name, outputs, policy=None, context_ids=(), parent_id=None
):
    contract = build_control_plane_contract(
        name=name,
        description="Publish the declared task output for a view regression fixture.",
        outputs=tuple(outputs),
        budget=5,
        acceptance_policy=policy,
        context_asset_ids=tuple(context_ids),
    )
    if parent_id is not None:
        contract = replace(contract, id="", parent_id=parent_id)
    return publisher.accept_contract(contract)


def _run(store, publisher, worker, contract, monkeypatch):
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime", lambda *_a, **_k: None
    )
    host = hosted_worker(
        store,
        worker,
        genesis=publisher.genesis,
        authority_key=publisher.actor_key,
        authority_signer=publisher.signer,
    )
    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None
    result = execute_claimed_package(claimed, host, store)
    return result


def _independent_policy(publisher, *, output_shapes=None):
    rubric = publisher.accept_asset(
        build_control_plane_asset(
            name="view_rubric", content="Check each declared output."
        )
    )
    evidence = publisher.accept_asset(
        build_control_plane_asset(
            name="view_evidence", content="Frozen evidence for the view fixture."
        )
    )
    policy = {
        "mode": "independent",
        "policy_version": "submitted-view-v1",
        "required_attestations": 1,
        "verifier_capabilities": ["verify.human"],
        "rubric_asset_ids": [rubric.id],
        "evidence_asset_ids": [evidence.id],
    }
    if output_shapes is not None:
        policy["output_shapes"] = output_shapes
    return policy, (rubric.id, evidence.id)


def test_signed_submitted_independent_outputs_are_a_qualification_blocker(
    tmp_path, monkeypatch
):
    database = tmp_path / "submitted.sqlite"
    store = SQLiteStore(db_path=str(database))
    publisher = candidate_runtime(store)
    policy, _context_ids = _independent_policy(publisher)
    contract = _publish_task(
        store,
        publisher,
        name="submitted-report",
        outputs=("summary", "appendix"),
        policy=policy,
    )
    worker = _OutputWorker(
        "worker:submitted-output",
        {"summary": "A bounded report.", "appendix": "A cited appendix."},
    )
    result = _run(store, publisher, worker, contract, monkeypatch)
    assert result["status"] == "accepted"
    original_outputs = {
        asset.name: asset.id
        for asset in store.get_all_assets()
        if asset.created_by == contract.id and asset.name in {"summary", "appendix"}
    }
    publisher.accept_asset(
        build_control_plane_asset(name="summary", content="Unrelated same-name noise.")
    )
    before_revision = store.get_runtime_revision()
    before_records = [record.id for _, record in store.scan_runtime_records()]
    store.close()

    reopened = SQLiteStore(db_path=str(database))
    contract = reopened.get_contract(contract.id)
    assert contract is not None
    status = project_task_status(contract, reopened)
    query_context = load_task_view_query_context(reopened)
    aggregate_status = project_task_status(
        contract,
        reopened,
        projection_context=query_context.effect_projection,
        query_context=query_context,
    )
    assert aggregate_status == status
    assert status["status"] == "submitted"
    assert status["outputs_satisfied"] is False
    assert status["blockers"].count("qualification:independent_acceptance") == 1
    assert "submitted_without_recovery" not in {
        risk["code"] for risk in status["silent_failure_risks"]
    }
    output_assets = {
        asset.name: asset.id
        for asset in reopened.get_all_assets()
        if asset.name in {"summary", "appendix"}
    }
    assert set(status["outputs"]) == {"summary", "appendix"}
    assert status["outputs"] == output_assets
    assert status["qualification_pending_outputs"] == original_outputs

    from aigineering.cli import task as task_cli

    class _NoopCollaboration:
        def sync(self):
            return None

    def board_snapshot():
        snapshot = SQLiteStore.snapshot_from_path(database)
        return (
            snapshot,
            None,
            snapshot.get_all_contracts(),
            snapshot.get_runtime_revision(),
        )

    monkeypatch.setattr(task_board_cli, "_snapshot_context", board_snapshot)
    monkeypatch.setattr(
        task_board_cli, "collaboration_service", lambda _store: _NoopCollaboration()
    )
    board_group = click.Group("task")
    task_board_cli.register_task_board_commands(board_group)
    ready = CliRunner().invoke(board_group, ["list", "--status", "ready", "--json"])
    assert ready.exit_code == 0, ready.output
    assert contract.id not in {
        row["contract_id"] for row in json.loads(ready.output)["data"]["tasks"]
    }
    submitted = CliRunner().invoke(
        board_group, ["list", "--status", "submitted", "--json"]
    )
    assert submitted.exit_code == 0, submitted.output
    assert contract.id in {
        row["contract_id"] for row in json.loads(submitted.output)["data"]["tasks"]
    }

    monkeypatch.setattr(
        task_cli,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        task_cli, "collaboration_service", lambda _store: _NoopCollaboration()
    )
    waited = CliRunner().invoke(
        task_cli.task_group,
        ["wait", contract.id, "--timeout", "0", "--json"],
    )
    assert waited.exit_code == 1
    wait_status = json.loads(waited.output)
    assert wait_status["status"] == "submitted"
    assert "qualification:independent_acceptance" in wait_status["blockers"]

    @click.command()
    def target_result():
        _finish_target_run(status, 0, stop_reason="idle", json_output=True)

    target = CliRunner().invoke(target_result)
    assert target.exit_code == 1
    guidance = json.loads(target.output)["next_step"]
    assert "independent qualification" in guidance
    assert "Do not rerun or reset" in guidance

    digest_before = reopened.runtime_materialization_digest()
    rebuilt_before = reopened.get_runtime_revision()
    rebuilt_ids_before = [record.id for _, record in reopened.scan_runtime_records()]
    reopened.rebuild_runtime_materializations()
    assert reopened.runtime_materialization_digest() == digest_before
    assert reopened.get_runtime_revision() == rebuilt_before == before_revision
    assert [
        record.id for _, record in reopened.scan_runtime_records()
    ] == before_records
    assert rebuilt_ids_before == before_records
    rebuilt = project_task_status(contract, reopened)
    assert rebuilt["status"] == "submitted"
    assert rebuilt["blockers"] == status["blockers"]
    reopened.close()


def test_submitted_view_ignores_earlier_unclaimed_graph_asset(tmp_path):
    import test_task_review as review
    from aigineering.core.projection_context import load_effect_projection_context
    from aigineering.core.publication_history import validate_published_member

    env = review._workspace(
        tmp_path,
        graph_target=True,
    )
    store = env["store"]
    try:
        fake = review._publish_unclaimed_graph(
            env,
            content_text='{"answer":"looks shaped but has no claim"}',
        )
        context = load_effect_projection_context(store)
        fake_committed = next(
            record
            for record in context.runtime_records
            if record.record_type == "asset.committed"
            and record.payload.get("asset", {}).get("id") == fake.id
        )
        fake_assertion = next(
            record
            for record in context.runtime_records
            if record.id == fake_committed.causal_parents[0]
        )
        validate_published_member(context.runtime_records, fake_assertion)
        fake_definition_id = fake_assertion.payload["assertion"]["definition_id"]
        fake_definition = next(
            record.payload["definition"]
            for record in context.runtime_records
            if record.record_type == "asset.definition.published"
            and record.payload.get("definition", {}).get("id") == fake_definition_id
        )
        assert fake_definition["source_uri"] == env["source"].id

        canonical = review._publish_unclaimed_graph(
            env,
            claimed=True,
            content_text='{"answer":"claim-bound output"}',
        )
        env["target"] = canonical
        same_name_assets = store.get_assets_by_name("final")
        same_name_ids = [asset.id for asset in same_name_assets]
        assert same_name_ids[0] == fake.id
        assert same_name_ids.index(canonical.id) > 0
        submitted = store.scan_runtime_records(record_type="claim.submitted")
        assert len(submitted) == 1
        assert submitted[0][1].payload["contract_id"] == env["source"].id
        assert canonical.content == '{"answer":"claim-bound output"}'

        status = project_task_status(env["source"], store)
        assert status["status"] == "submitted"
        assert status["outputs_satisfied"] is False
        assert status["qualification_pending_outputs"] == {"final": canonical.id}
        assert fake.id not in status["qualification_pending_outputs"].values()
        assert "qualification:independent_acceptance" in status["blockers"]
    finally:
        store.close()


def test_incomplete_outputs_and_foreign_or_tool_assets_are_not_qualification_pending(
    tmp_path, monkeypatch
):
    store = SQLiteStore(db_path=str(tmp_path / "incomplete.sqlite"))
    publisher = candidate_runtime(store)
    policy, _context_ids = _independent_policy(publisher)
    target = _publish_task(
        store,
        publisher,
        name="incomplete-report",
        outputs=("summary", "appendix"),
        policy=policy,
    )
    incomplete_result = _run(
        store,
        publisher,
        _OutputWorker("worker:incomplete", {"summary": "Only one slot."}),
        target,
        monkeypatch,
    )
    assert incomplete_result["status"] == "rejected", incomplete_result

    foreign = _publish_task(
        store, publisher, name="foreign-producer", outputs=("appendix",)
    )
    assert (
        _run(
            store,
            publisher,
            _OutputWorker("worker:foreign", {"appendix": "Foreign same-name asset."}),
            foreign,
            monkeypatch,
        )["status"]
        == "accepted"
    )
    publisher.accept_asset(
        build_control_plane_asset(
            name="appendix", content="Tool observation with same name.", origin="tool"
        )
    )

    status = project_task_status(target, store)
    assert status["status"] in {"failed", "rejected", "stalled"}
    assert status["outputs_satisfied"] is False
    assert "qualification:independent_acceptance" not in status["blockers"]
    store.close()


def test_descendant_business_outputs_remain_exact_qualification_references(
    tmp_path, monkeypatch
):
    from aigineering.task_views import _submitted_independent_output_assets

    store = SQLiteStore(db_path=str(tmp_path / "descendant.sqlite"))
    publisher = candidate_runtime(store)
    policy, _context_ids = _independent_policy(publisher)
    parent = _publish_task(
        store,
        publisher,
        name="descendant-output-parent",
        outputs=("summary", "appendix"),
        policy=policy,
    )
    child = _publish_task(
        store,
        publisher,
        name="descendant-output-child",
        outputs=("summary", "appendix"),
        parent_id=parent.id,
    )
    assert (
        _run(
            store,
            publisher,
            _OutputWorker(
                "worker:descendant-output",
                {"summary": "Child summary.", "appendix": "Child appendix."},
            ),
            child,
            monkeypatch,
        )["status"]
        == "accepted"
    )
    expected = {
        asset.name: asset.id
        for asset in store.get_assets_by_contract(child.id)
        if asset.name in child.outputs
    }
    assert _submitted_independent_output_assets(parent, store) == expected
    store.close()


def test_aggregate_parent_view_reuses_snapshot_for_submitted_child_risk(
    tmp_path, monkeypatch
):
    store = SQLiteStore(db_path=str(tmp_path / "submitted-child-view.sqlite"))
    publisher = candidate_runtime(store)
    policy, _context_ids = _independent_policy(publisher)
    parent = _publish_task(
        store,
        publisher,
        name="submitted-child-parent",
        outputs=("parent-result",),
    )
    child = _publish_task(
        store,
        publisher,
        name="submitted-child",
        outputs=("summary", "appendix"),
        policy=policy,
        parent_id=parent.id,
    )
    result = _run(
        store,
        publisher,
        _OutputWorker(
            "worker:submitted-child",
            {"summary": "A child summary.", "appendix": "A child appendix."},
        ),
        child,
        monkeypatch,
    )
    assert result["status"] == "accepted"

    query_context = load_task_view_query_context(store)
    status = project_task_status(
        parent,
        store,
        projection_context=query_context.effect_projection,
        query_context=query_context,
    )

    assert "descendant_submitted_without_recovery" not in {
        risk["code"] for risk in status["silent_failure_risks"]
    }
    store.close()


def test_legacy_orphan_projection_keeps_risk_and_state_precedence():
    store = SQLiteStore(":memory:")
    publisher = candidate_runtime(store)
    orphan = _publish_task(store, publisher, name="orphan", outputs=("report",))
    from aigineering.core.trace import create_entry

    store.append(
        create_entry(
            contract_id=orphan.id,
            event_type="projection",
            rejected_fragments=["[parse_error] no output"],
            authority_result="rejected",
        )
    )
    status = project_task_status(orphan, store)
    assert status["status"] == "submitted"
    assert "submitted_without_recovery" in {
        risk["code"] for risk in status["silent_failure_risks"]
    }
    store.close()


def test_terminal_expansion_active_claim_and_unclaimed_ready_precedence():
    from types import SimpleNamespace

    from aigineering.task_views import _status_from_entries

    store = SQLiteStore(":memory:")
    contract = build_control_plane_contract(
        name="precedence", outputs=("result",), budget=1
    )
    base_view = SimpleNamespace(
        terminal=None,
        claim_status=None,
        inputs_satisfied=True,
        activation_satisfied=True,
        budget_remaining=1,
        blockers=(),
    )
    assert _status_from_entries(contract, store, [], "", base_view, "") == "ready"
    assert (
        _status_from_entries(
            contract,
            store,
            [],
            "",
            base_view.__class__(**{**base_view.__dict__, "claim_status": "active"}),
            "",
        )
        == "claimed"
    )
    assert (
        _status_from_entries(contract, store, [], "", base_view, "expanded")
        == "expanded"
    )
    failed_view = base_view.__class__(**{**base_view.__dict__, "terminal": "failed"})
    assert _status_from_entries(contract, store, [], "", failed_view, "") == "failed"
    conflict_view = base_view.__class__(
        **{**base_view.__dict__, "terminal": "conflict"}
    )
    assert _status_from_entries(contract, store, [], "", conflict_view, "") == "stalled"
    store.close()


def test_ordinary_complete_output_stays_completed(tmp_path, monkeypatch):
    store = SQLiteStore(db_path=str(tmp_path / "complete.sqlite"))
    publisher = candidate_runtime(store)
    contract = _publish_task(
        store, publisher, name="ordinary-complete", outputs=("result",)
    )
    result = _run(
        store,
        publisher,
        _OutputWorker("worker:ordinary-complete", {"result": "Done."}),
        contract,
        monkeypatch,
    )
    assert result["status"] == "accepted"
    status = project_task_status(contract, store)
    assert status["status"] == "completed"
    assert "qualification:independent_acceptance" not in status["blockers"]
    store.close()


def test_cancelled_submitted_task_does_not_advertise_pending_qualification(
    tmp_path, monkeypatch
):
    from aigineering.core.candidate_publisher import CandidatePublisher
    from aigineering.protocol.candidate import ActorKey
    from aigineering.protocol.effect_builders import contract_cancellation_effect

    store = SQLiteStore(db_path=str(tmp_path / "cancelled.sqlite"))
    publisher = candidate_runtime(store)
    policy, _context_ids = _independent_policy(publisher)
    contract = _publish_task(
        store,
        publisher,
        name="cancelled-submitted",
        outputs=("summary",),
        policy=policy,
    )
    result = _run(
        store,
        publisher,
        _OutputWorker("worker:cancelled-submitted", {"summary": "Submitted."}),
        contract,
        monkeypatch,
    )
    assert result["status"] == "accepted"

    cancel_key = ActorKey(
        "test:cancel",
        "test-cancel-1",
        publisher.signer.kind,
        publisher.signer.signer_id,
        ("contract.cancel",),
    )
    publisher.authorize_actor(cancel_key)
    canceller = CandidatePublisher(
        store,
        store,
        publisher.genesis,
        cancel_key,
        publisher.signer,
    )
    cancelled = canceller.publish(
        (contract_cancellation_effect(contract.id, "fixture cancellation"),),
        idempotency_key="cancel-submitted-fixture",
    )
    assert cancelled.accepted

    status = project_task_status(contract, store)
    assert status["status"] == "cancelled"
    assert status["qualification_pending_outputs"] == {}
    assert "qualification:independent_acceptance" not in status["blockers"]
    store.close()


def test_accepted_review_removes_only_its_exact_output_from_pending_map(tmp_path):
    from unittest.mock import patch

    import test_task_review as review
    from aigineering.core.commitment import CandidateCommitter
    from aigineering.protocol.candidate import (
        CandidateClaimBinding,
        create_candidate_proposal,
    )
    from aigineering.protocol.effect_builders import asset_proposal_effect
    from aigineering.protocol.types import Asset

    original_builder = review.build_control_plane_contract

    def two_output_contract(*args, **kwargs):
        kwargs["outputs"] = ("final", "appendix")
        return original_builder(*args, **kwargs)

    def publish_both_outputs(store, signer, genesis, actor, contract, name, content):
        del name, content
        store.register_worker(
            review.WorkerRegistration(
                actor.actor_id, actor_id=actor.actor_id, key_id=actor.key_id
            )
        )
        claim = store.claim_contract(
            contract.id, actor.actor_id, package_id="pkg:partial-qualification"
        )
        assert claim is not None
        effects = tuple(
            asset_proposal_effect(
                Asset(id=f"unused-{output}", name=output, content=f"{output} result")
            )
            for output in contract.outputs
        )
        candidate = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=actor.actor_id,
            key_id=actor.key_id,
            effects=effects,
            signer=signer,
            idempotency_key=f"submit-both:{contract.id}",
            claim_binding=CandidateClaimBinding(
                contract.id,
                claim["claim_id"],
                claim["epoch"],
                claim["package_id"],
            ),
        )
        result = CandidateCommitter(store, store).commit(candidate, genesis)
        assert result.accepted, result
        return next(asset for asset in result.assets if asset.name == "final")

    with (
        patch.object(review, "build_control_plane_contract", two_output_contract),
        patch.object(review, "_publish_output", publish_both_outputs),
    ):
        env = review._workspace(tmp_path)
    store = env["store"]
    try:
        _, request_decision = review._request(env, reason="Review final only")
        assert request_decision.accepted, request_decision
        request = review.project_task_reviews(request_decision.runtime_records)[0]
        _, report = review._review_report(env, request)
        _, decision = review._decide(
            env, env["verifier"], request, report, verdict="accept"
        )
        assert decision.accepted, decision

        status = project_task_status(env["source"], store)
        qualified = store.scan_runtime_records(record_type="output.qualified")
        assert len(qualified) == 1
        assert qualified[0][1].payload["contract_id"] == env["source"].id
        assert qualified[0][1].payload["output_name"] == "final"
        assert qualified[0][1].payload["asset_id"] == env["target"].id
        assert status["status"] == "submitted"
        assert status["outputs_satisfied"] is False
        assert status["qualification_pending_outputs"] == {
            "appendix": next(
                asset.id
                for asset in store.get_assets_by_name("appendix")
                if asset.created_by == env["source"].id
            )
        }
        assert "qualification:independent_acceptance" in status["blockers"]
        assert "submitted_without_recovery" not in {
            risk["code"] for risk in status["silent_failure_risks"]
        }
    finally:
        store.close()
