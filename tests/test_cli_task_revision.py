"""Behavior tests for signed, review-linked bounded task revisions."""

from __future__ import annotations

import json

from click import Group
from click.testing import CliRunner

from aigineering.cli.task_policy_review import register_task_policy_review_commands
from aigineering.cli.task_revision import register_task_revision_commands
from aigineering.cli.task_assignment import register_task_assignment_commands
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain, load_actor_signer
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.task_views import project_task_status

from test_cli_task_review import (
    _OutputWorker,
    _create_target,
    _plugin_key_path,
    _request_review,
    _workspace,
)


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_policy_review_commands(task)
    register_task_assignment_commands(task)
    register_task_revision_commands(task)
    root.add_command(task)
    return root


class _MultiOutputWorker:
    def __init__(self, worker_id, outputs):
        self.worker_id = worker_id
        self.outputs = outputs

    def invoke(self, _contract, _disclosed_assets):
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec " + json.dumps({"outputs": self.outputs}),
        )


def _multi_output_workspace(tmp_path, monkeypatch):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key_path))
    store = SQLiteStore(database)
    genesis = ensure_local_domain(store)
    root_key = genesis.root_keys[0]
    root_signer = load_actor_signer(root_key_path)
    assets = []
    for name, content in (
        ("review_rubric", "Check claims against frozen evidence."),
        ("review_evidence", "Fixture evidence contains bounded facts."),
    ):
        asset = build_control_plane_asset(name=name, content=content)
        result = publish_effect(
            store,
            store,
            genesis,
            root_key,
            root_signer,
            asset_proposal_effect(asset),
            idempotency_key=f"revision-test-asset:{name}",
        )
        assert result.accepted
        assets.append(store.get_assets_by_name(name)[0])
    policy = {
        "mode": "independent",
        "policy_version": "revision-multi-output-v1",
        "required_attestations": 1,
        "verifier_capabilities": ["verify.human"],
        "rubric_asset_ids": [assets[0].id],
        "evidence_asset_ids": [assets[1].id],
    }
    source = build_control_plane_contract(
        name="source-multi-output",
        description="Produce two output slots for independent review.",
        inputs=(),
        outputs=("research_summary", "risk_register"),
        budget=8,
        labels=(assets[0].name, assets[1].name),
        context_asset_ids=tuple(asset.id for asset in assets),
        acceptance_policy=policy,
    )
    result = publish_effect(
        store,
        store,
        genesis,
        root_key,
        root_signer,
        contract_declaration_effect(source),
        idempotency_key="revision-test-source-multi-output",
    )
    assert result.accepted
    store.close()
    import test_cli_task_review as review_helpers

    review_helpers._install_cli_store(monkeypatch, database)
    _install_revision_store(monkeypatch, database)
    return database, root_key_path, source


def _install_revision_store(monkeypatch, database):
    import aigineering.application
    import aigineering.cli.task_revision as cli_revision
    import aigineering.task_revision as app_revision
    import aigineering.runtime_services as runtime_services

    class _NoopCollaboration:
        def sync(self):
            return None

    monkeypatch.setattr(
        aigineering.application, "require_redis", lambda _redis_url=None: None
    )
    monkeypatch.setattr(
        aigineering.application,
        "collaboration_service",
        lambda _store: _NoopCollaboration(),
    )
    monkeypatch.setattr(
        runtime_services,
        "collaboration_service",
        lambda _store: _NoopCollaboration(),
    )
    monkeypatch.setattr(cli_revision, "require_redis", lambda: None)
    monkeypatch.setattr(
        cli_revision, "collaboration_service", lambda _store: _NoopCollaboration()
    )
    monkeypatch.setattr(app_revision, "require_redis", lambda: None)
    monkeypatch.setattr(
        app_revision, "collaboration_service", lambda _store: _NoopCollaboration()
    )
    monkeypatch.setattr(app_revision, "sync_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_revision,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        cli_revision, "_persistent_store", lambda: SQLiteStore(database)
    )


def _run_worker(database, contract, worker):
    from aigineering.local_identity import ensure_local_worker_host

    store = SQLiteStore(database)
    host = ensure_local_worker_host(store, worker)
    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None
    result = execute_claimed_package(claimed, host, store)
    store.close()
    return result


def _review_and_decide(runner, database, root_key_path, source, target, *, verdict):
    request = _request_review(
        runner,
        source,
        target.id,
        key=f"review-request-{source.id[-8:]}-{target.id[-8:]}",
    )
    store = SQLiteStore.snapshot_from_path(database)
    reviewer = store.get_contract(request["child_contract_id"])
    store.close()
    assert reviewer is not None
    worker = _OutputWorker(
        f"worker:reviewer-{source.id[-8:]}",
        request["report_output"],
        "The exact target is checked against the frozen evidence.",
    )
    assert _run_worker(database, reviewer, worker)["status"] == "accepted"
    store = SQLiteStore.snapshot_from_path(database)
    report = store.get_assets_by_name(request["report_output"])[0]
    store.close()
    result = runner.invoke(
        _app(),
        [
            "task",
            "review",
            "decide",
            request["record_id"],
            "--report",
            report.id,
            "--verdict",
            verdict,
            "--reason",
            f"Independent policy decision: {verdict}.",
            "--idempotency-key",
            f"decision-{source.id[-8:]}-{verdict}",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return request, report, json.loads(result.output)["data"]


def _review_named_output(runner, database, source, output_name, target, verdict):
    request_result = runner.invoke(
        _app(),
        [
            "task",
            "request-review",
            source.id,
            "--output",
            output_name,
            "--asset",
            target.id,
            "--reason",
            f"Check the exact {output_name} output.",
            "--idempotency-key",
            f"review-{source.id[-7:]}-{output_name}",
            "--json",
        ],
    )
    assert request_result.exit_code == 0, request_result.output
    request = json.loads(request_result.output)["data"]
    store = SQLiteStore.snapshot_from_path(database)
    reviewer = store.get_contract(request["child_contract_id"])
    store.close()
    assert reviewer is not None
    worker = _OutputWorker(
        f"worker:reviewer-{source.id[-7:]}-{output_name}",
        request["report_output"],
        f"Review of {output_name}: revision matches the frozen policy inputs.",
    )
    assert _run_worker(database, reviewer, worker)["status"] == "accepted"
    store = SQLiteStore.snapshot_from_path(database)
    report = store.get_assets_by_name(request["report_output"])[0]
    store.close()
    decision_result = runner.invoke(
        _app(),
        [
            "task",
            "review",
            "decide",
            request["record_id"],
            "--report",
            report.id,
            "--verdict",
            verdict,
            "--reason",
            f"Policy decision for {output_name}: {verdict}.",
            "--idempotency-key",
            f"decision-{source.id[-7:]}-{output_name}-{verdict}",
            "--json",
        ],
    )
    assert decision_result.exit_code == 0, decision_result.output
    return report, json.loads(decision_result.output)["data"]


def test_request_changes_publishes_bounded_successor_then_fresh_acceptance(
    tmp_path, monkeypatch
):
    database, root_key_path, source, _policy = _workspace(tmp_path, monkeypatch)
    _install_revision_store(monkeypatch, database)
    from aigineering.local_identity import ensure_local_worker_host
    from test_cli_task_review import _OutputWorker

    store = SQLiteStore(database)
    source_worker = _OutputWorker(
        "worker:source-producer",
        "research_summary",
        "A fixture report with bounded claims and source references.",
    )
    ensure_local_worker_host(store, source_worker)
    store.close()
    import aigineering.application
    import aigineering.cli.task_assignment as cli_assignment
    import aigineering.task_assignment as app_assignment

    class _NoopCollaboration:
        def sync(self):
            return None

    monkeypatch.setattr(aigineering.application, "require_redis", lambda: None)
    monkeypatch.setattr(
        aigineering.application,
        "collaboration_service",
        lambda _s: _NoopCollaboration(),
    )
    monkeypatch.setattr(cli_assignment, "require_redis", lambda: None)
    monkeypatch.setattr(
        cli_assignment, "collaboration_service", lambda _s: _NoopCollaboration()
    )
    monkeypatch.setattr(app_assignment, "require_redis", lambda: None)
    monkeypatch.setattr(app_assignment, "sync_runtime", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        app_assignment, "collaboration_service", lambda _s: _NoopCollaboration()
    )
    monkeypatch.setattr(
        cli_assignment,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(
        cli_assignment, "_persistent_store", lambda: SQLiteStore(database)
    )
    runner = CliRunner()
    assignment_result = runner.invoke(
        _app(),
        [
            "task",
            "assign",
            source.id,
            "worker:source-producer",
            "--reason",
            "root routing",
            "--idempotency-key",
            "revision-source-assignment",
            "--json",
        ],
    )
    assert assignment_result.exit_code == 0, assignment_result.output
    source_assignment = json.loads(assignment_result.output)["data"]
    target, _producer = _create_target(database, root_key_path, source)
    _request, report, changed = _review_and_decide(
        runner, database, root_key_path, source, target, verdict="changes"
    )

    result = runner.invoke(
        _app(),
        [
            "task",
            "request-changes",
            changed["record_id"],
            "--reason",
            "Revise the unsupported causal claim.",
            "--budget",
            "1",
            "--idempotency-key",
            "revision-exact-replay",
            "--clear-assignment",
            "--source-assignment-id",
            source_assignment["record_id"],
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    revision = json.loads(result.output)["data"]
    assert revision["record_type"] == "task.revised"
    assert revision["contract_id"] == source.id
    assert revision["decision_id"] == changed["record_id"]
    assert revision["report_asset_id"] == report.id
    assert revision["asset_id"] == target.id
    assert revision["budget"] == 1
    assert revision["actor_id"] == "plugin:task.revise.assignment-override.operator.v1"
    assert revision["assignment_mode"] == "override"
    assert revision["source_assignment_id"] == source_assignment["record_id"]
    assert revision["assignment_fact_id"]
    assert revision["worker_id"] == ""
    successor_id = revision["successor_id"]

    store = SQLiteStore.snapshot_from_path(database)
    successor = store.get_contract(successor_id)
    assert successor is not None
    assert successor.parent_id == source.id
    assert successor.outputs == source.outputs
    assert successor.acceptance_policy == source.acceptance_policy
    assert project_task_status(source, store)["status"] == "cancelled"
    assert not store.scan_runtime_records(record_type="output.qualified")
    store.close()

    replay = runner.invoke(
        _app(),
        [
            "task",
            "request-changes",
            changed["record_id"],
            "--reason",
            "Revise the unsupported causal claim.",
            "--budget",
            "1",
            "--idempotency-key",
            "revision-exact-replay",
            "--clear-assignment",
            "--source-assignment-id",
            source_assignment["record_id"],
            "--json",
        ],
    )
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["data"] == revision

    stale_request = runner.invoke(
        _app(),
        [
            "task",
            "request-changes",
            changed["record_id"],
            "--reason",
            "A different reason cannot reopen a terminal source.",
            "--budget",
            "1",
            "--idempotency-key",
            "different-reason-after-terminal",
            "--json",
        ],
    )
    assert stale_request.exit_code == 1, stale_request.output
    assert json.loads(stale_request.output)["error"]["code"] == "rejected"
    store = SQLiteStore.snapshot_from_path(database)
    assert len(store.scan_runtime_records(record_type="task.revised")) == 1
    store.close()

    revision_worker = _OutputWorker(
        "worker:revision-producer",
        "research_summary",
        "Revised report with bounded claims and evidence links.",
    )
    assert _run_worker(database, successor, revision_worker)["status"] == "accepted"
    store = SQLiteStore.snapshot_from_path(database)
    revised_target = store.get_assets_by_name("research_summary")[-1]
    assert revised_target.id != target.id
    assert project_task_status(successor, store)["status"] != "completed"
    store.close()

    _review_and_decide(
        runner, database, root_key_path, successor, revised_target, verdict="accept"
    )
    store = SQLiteStore.snapshot_from_path(database)
    successor = store.get_contract(successor_id)
    assert successor is not None
    assert project_task_status(successor, store)["status"] == "completed"
    assert project_task_status(source, store)["status"] == "cancelled"
    qualified = store.scan_runtime_records(record_type="output.qualified")
    assert len(qualified) == 1
    assert qualified[0][1].payload["asset_id"] == revised_target.id
    store.close()

    history = runner.invoke(_app(), ["task", "revisions", source.id, "--json"])
    assert history.exit_code == 0, history.output
    assert json.loads(history.output)["data"] == [revision]
    human = runner.invoke(_app(), ["task", "revisions", source.id])
    assert human.exit_code == 0, human.output
    assert revision["successor_id"] in human.output
    assert f"aig run --task {revision['successor_id']}" in human.output
    reopened = SQLiteStore.snapshot_from_path(database)
    assert len(reopened.scan_runtime_records(record_type="task.revised")) == 1
    reopened.close()


def test_revision_preflight_rejects_non_changes_and_blank_without_provisioning(
    tmp_path, monkeypatch
):
    database, root_key_path, source, _policy = _workspace(tmp_path, monkeypatch)
    _install_revision_store(monkeypatch, database)
    target, _producer = _create_target(database, root_key_path, source)
    runner = CliRunner()
    _request, report, accepted = _review_and_decide(
        runner, database, root_key_path, source, target, verdict="accept"
    )
    assert report.id == accepted["report_asset_id"]

    actor_id = "plugin:task.revise.operator.v1"
    key_file = _plugin_key_path(root_key_path, actor_id)
    for args in (
        ["task", "request-changes", accepted["record_id"], "--reason", " ", "--json"],
        [
            "task",
            "request-changes",
            accepted["record_id"],
            "--reason",
            "Not a changes decision.",
            "--json",
        ],
    ):
        failed = runner.invoke(_app(), args)
        assert failed.exit_code == 1, failed.output
    unknown = runner.invoke(
        _app(),
        [
            "task",
            "request-changes",
            "missing-decision",
            "--reason",
            "Decision lookup must happen without provisioning.",
            "--json",
        ],
    )
    assert unknown.exit_code == 1, unknown.output
    assert json.loads(unknown.output)["error"]["code"] == "not_found"
    assert not key_file.exists()
    reopened = SQLiteStore.snapshot_from_path(database)
    assert not reopened.scan_runtime_records(record_type="task.revised")
    reopened.close()


def test_revision_preserves_every_unqualified_slot_and_replay_stable_default_budget(
    tmp_path, monkeypatch
):
    database, root_key_path, source = _multi_output_workspace(tmp_path, monkeypatch)
    producer = _MultiOutputWorker(
        "worker:multi-source-producer",
        {
            "research_summary": "Initial bounded report requiring changes.",
            "risk_register": "Initial risk register requiring changes.",
        },
    )
    producer_result = _run_worker(database, source, producer)
    assert producer_result["status"] == "accepted", json.dumps(producer_result)
    store = SQLiteStore.snapshot_from_path(database)
    initial = store.get_assets_by_name("research_summary")[-1]
    store.close()
    runner = CliRunner()
    _request, _review_report, changed = _review_and_decide(
        runner, database, root_key_path, source, initial, verdict="changes"
    )
    result = runner.invoke(
        _app(),
        [
            "task",
            "request-changes",
            changed["record_id"],
            "--reason",
            "Revise the analysis and add the separate risk register.",
            "--idempotency-key",
            "multi-output-null-budget",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    revision = json.loads(result.output)["data"]
    store = SQLiteStore.snapshot_from_path(database)
    successor = store.get_contract(revision["successor_id"])
    assert successor is not None
    assert successor.parent_id == source.id
    assert successor.outputs == ("research_summary", "risk_register")
    assert successor.acceptance_policy == source.acceptance_policy
    assert successor.budget > 0
    assert revision["retained_asset_ids"] == []
    store.close()

    revision_worker = _MultiOutputWorker(
        "worker:multi-revision-producer",
        {
            "research_summary": "Revised summary with bounded evidence.",
            "risk_register": "Separate risk register with explicit uncertainty.",
        },
    )
    assert _run_worker(database, successor, revision_worker)["status"] == "accepted"
    store = SQLiteStore.snapshot_from_path(database)
    summary = store.get_assets_by_name("research_summary")[-1]
    risk_register = store.get_assets_by_name("risk_register")[-1]
    assert project_task_status(successor, store)["status"] != "completed"
    store.close()

    _summary_report, summary_decision = _review_named_output(
        runner, database, successor, "research_summary", summary, "accept"
    )
    store = SQLiteStore.snapshot_from_path(database)
    assert not project_task_status(successor, store)["outputs_satisfied"]
    assert len(store.scan_runtime_records(record_type="output.qualified")) == 1
    store.close()
    _risk_report, risk_decision = _review_named_output(
        runner, database, successor, "risk_register", risk_register, "accept"
    )

    replay = runner.invoke(
        _app(),
        [
            "task",
            "request-changes",
            changed["record_id"],
            "--reason",
            "Revise the analysis and add the separate risk register.",
            "--idempotency-key",
            "multi-output-null-budget",
            "--json",
        ],
    )
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["data"] == revision
    store = SQLiteStore.snapshot_from_path(database)
    successor = store.get_contract(revision["successor_id"])
    assert successor is not None
    assert project_task_status(successor, store)["status"] == "completed"
    assert project_task_status(source, store)["status"] == "cancelled"
    qualified = {
        record.payload["output_name"]: record.payload["asset_id"]
        for _revision, record in store.scan_runtime_records(
            record_type="output.qualified"
        )
    }
    assert qualified == {
        "research_summary": summary.id,
        "risk_register": risk_register.id,
    }
    assert summary_decision["verdict"] == risk_decision["verdict"] == "accept"
    store.close()


def test_postcommit_redis_pending_retains_revision_and_exact_replay(
    tmp_path, monkeypatch
):
    database, root_key_path, source, _policy = _workspace(tmp_path, monkeypatch)
    _install_revision_store(monkeypatch, database)
    target, _producer = _create_target(database, root_key_path, source)
    runner = CliRunner()
    _request, _report, changed = _review_and_decide(
        runner, database, root_key_path, source, target, verdict="changes"
    )

    from aigineering.cli.main import cli
    from aigineering.local_identity import ensure_local_plugin_publisher
    from aigineering.runtime_services import RedisSyncPending

    monkeypatch.setattr("aigineering.cli.infrastructure.require_redis", lambda: None)
    store = SQLiteStore(database)
    ensure_local_plugin_publisher(
        store,
        "task.revise.operator.v1",
        ("contract.cancel", "contract.publish", "task.revise"),
    )
    store.close()

    def pending_after_commit(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr("aigineering.task_revision.sync_runtime", pending_after_commit)
    args = [
        "task",
        "request-changes",
        changed["record_id"],
        "--reason",
        "Retain the signed revision while Redis is unavailable.",
        "--idempotency-key",
        "revision-postcommit-pending",
        "--json",
    ]
    pending_result = CliRunner().invoke(cli, args)
    assert pending_result.exit_code == 1, pending_result.output
    pending = json.loads(pending_result.output)
    assert pending["error"] == "redis_sync_pending"
    assert pending["facts_retained"] is True
    assert pending["candidate_accepted"] is True

    monkeypatch.setattr(
        "aigineering.task_revision.sync_runtime", lambda *_a, **_k: None
    )
    replay = CliRunner().invoke(cli, args)
    assert replay.exit_code == 0, replay.output
    replayed_link = json.loads(replay.output)["data"]
    store = SQLiteStore.snapshot_from_path(database)
    links = store.scan_runtime_records(record_type="task.revised")
    revision_successors = [
        item
        for item in store.get_all_contracts()
        if item.id == replayed_link["successor_id"] and item.parent_id == source.id
    ]
    assert len(links) == len(revision_successors) == 1
    assert replayed_link["successor_id"] == revision_successors[0].id
    store.close()
