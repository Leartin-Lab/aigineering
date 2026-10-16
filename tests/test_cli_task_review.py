"""Behavior tests for signed, exact-Asset task policy review."""

from __future__ import annotations

import json
import os
from pathlib import Path

from click import Group
from click.testing import CliRunner
import pytest

from aigineering.cli.task_policy_review import register_task_policy_review_commands
from aigineering.core.candidate_publisher import publish_effect
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.ids import acceptance_policy_id
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import (
    ensure_local_domain,
    ensure_local_worker_host,
    load_actor_signer,
)
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.task_views import project_task_status


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_policy_review_commands(task)
    root.add_command(task)
    return root


class _OutputWorker:
    def __init__(self, worker_id: str, output_name: str, content: str):
        self.worker_id = worker_id
        self.output_name = output_name
        self.content = content
        self.disclosed = None

    def invoke(self, contract, disclosed_assets):
        self.disclosed = tuple(disclosed_assets)
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps({"outputs": {self.output_name: self.content}}),
        )


def _install_cli_store(monkeypatch, database: Path):
    import aigineering.application
    import aigineering.cli.task_policy_review as cli_review
    import aigineering.task_policy_review as app_review
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
    monkeypatch.setattr(cli_review, "require_redis", lambda: None)
    monkeypatch.setattr(
        cli_review, "collaboration_service", lambda _store: _NoopCollaboration()
    )
    monkeypatch.setattr(app_review, "require_redis", lambda: None)
    monkeypatch.setattr(app_review, "sync_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_review,
        "_persistent_snapshot",
        lambda: SQLiteStore.snapshot_from_path(database),
    )
    monkeypatch.setattr(cli_review, "_persistent_store", lambda: SQLiteStore(database))


def _workspace(
    tmp_path: Path,
    monkeypatch,
    *,
    verifier_capabilities=("verify.human",),
    source_labels=(),
):
    database = tmp_path / ".aig" / "store.db"
    database.parent.mkdir(parents=True)
    root_key_path = tmp_path / ".aig" / "identity" / "root.ed25519"
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(root_key_path))
    store = SQLiteStore(database)
    genesis = ensure_local_domain(store)
    root_key = genesis.root_keys[0]
    root_signer = load_actor_signer(root_key_path)

    for name, content in (
        ("review_rubric", "Check the output against the source evidence."),
        ("review_evidence", "Fixture evidence records a sample size of twelve."),
    ):
        asset = build_control_plane_asset(name=name, content=content)
        published = publish_effect(
            store,
            store,
            genesis,
            root_key,
            root_signer,
            asset_proposal_effect(asset),
            idempotency_key=f"test-asset:{name}",
        )
        assert published.accepted

    rubric_id = store.get_assets_by_name("review_rubric")[0].id
    evidence_id = store.get_assets_by_name("review_evidence")[0].id
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "required_attestations": 1,
        "verifier_capabilities": list(verifier_capabilities),
        "rubric_asset_ids": [rubric_id],
        "evidence_asset_ids": [evidence_id],
    }
    source = build_control_plane_contract(
        name="source-study",
        description="Produce a report for independent review.",
        inputs=(),
        outputs=("research_summary",),
        budget=5,
        labels=("review_evidence", "review_rubric", *source_labels),
        context_asset_ids=(rubric_id, evidence_id),
        acceptance_policy=policy,
    )
    decision = publish_effect(
        store,
        store,
        genesis,
        root_key,
        root_signer,
        contract_declaration_effect(source),
        idempotency_key="test-source",
    )
    assert decision.accepted
    store.close()
    _install_cli_store(monkeypatch, database)
    return database, root_key_path, source, policy


def _run_worker(database: Path, root_key_path: Path, contract, worker: _OutputWorker):
    store = SQLiteStore(database)
    host = ensure_local_worker_host(store, worker)
    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None
    result = execute_claimed_package(claimed, host, store)
    store.close()
    return result


def _request_review(runner, source, target, *, key="review-request-1"):
    result = runner.invoke(
        _app(),
        [
            "task",
            "request-review",
            source.id,
            "--output",
            "research_summary",
            "--asset",
            target,
            "--reason",
            "Check the claims against evidence.",
            "--idempotency-key",
            key,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _plugin_key_path(root_key_path: Path, actor_id: str) -> Path:
    from aigineering.core.ids import compute_content_hash

    return root_key_path.parent / (
        "plugin-" + compute_content_hash(actor_id)[:16] + ".ed25519"
    )


def _create_target(database: Path, root_key_path: Path, source):
    producer = _OutputWorker(
        "worker:source-producer",
        "research_summary",
        "A fixture report with bounded claims and source references.",
    )
    run_result = _run_worker(database, root_key_path, source, producer)
    assert run_result["status"] == "accepted"
    store = SQLiteStore.snapshot_from_path(database)
    target = store.get_assets_by_name("research_summary")[0]
    assert not project_task_status(source, store)["outputs_satisfied"]
    store.close()
    return target, producer


def test_request_review_runs_exact_bounded_report_and_policy_decision(
    tmp_path, monkeypatch
):
    database, root_key_path, source, policy = _workspace(tmp_path, monkeypatch)
    target, producer = _create_target(database, root_key_path, source)
    runner = CliRunner()
    request = _request_review(runner, source, target.id)
    unavailable_decision = runner.invoke(
        _app(),
        [
            "task",
            "review",
            "decide",
            request["record_id"],
            "--report",
            "missing-report",
            "--verdict",
            "accept",
            "--reason",
            "Wait for the reviewer report.",
            "--json",
        ],
    )
    assert unavailable_decision.exit_code == 1
    assert json.loads(unavailable_decision.output)["error"]["code"] == "not_found"
    assert not _plugin_key_path(
        root_key_path, "plugin:task.review.decide.operator.v1"
    ).exists()

    store = SQLiteStore.snapshot_from_path(database)
    reviewer = store.get_contract(request["child_contract_id"])
    assert reviewer is not None
    assert reviewer.parent_id == source.id
    assert reviewer.budget == 1
    assert reviewer.inputs == ()
    expected_context = tuple(
        sorted((target.id, *policy["rubric_asset_ids"], *policy["evidence_asset_ids"]))
    )
    assert reviewer.context_asset_ids == expected_context
    assert reviewer.acceptance_policy is None
    assert request["policy_id"] == acceptance_policy_id(source.acceptance_policy)
    assert request["rubric_asset_ids"] == policy["rubric_asset_ids"]
    assert request["evidence_asset_ids"] == policy["evidence_asset_ids"]
    store.close()

    review_worker = _OutputWorker(
        "worker:independent-reviewer",
        request["report_output"],
        "Report: unsupported causal language should be removed.",
    )
    result = _run_worker(database, root_key_path, reviewer, review_worker)
    assert result["status"] == "accepted"
    assert {asset.id for asset in review_worker.disclosed} == set(expected_context)
    assert producer.worker_id != review_worker.worker_id
    assert any(asset.id == target.id for asset in review_worker.disclosed)

    store = SQLiteStore.snapshot_from_path(database)
    report_asset = store.get_assets_by_name(request["report_output"])[0]
    assert report_asset.created_by == reviewer.id
    assert report_asset.signed_by != target.signed_by
    assert not project_task_status(source, store)["outputs_satisfied"]
    assert project_task_status(reviewer, store)["status"] == "completed"
    store.close()

    decision = runner.invoke(
        _app(),
        [
            "task",
            "review",
            "decide",
            request["record_id"],
            "--report",
            report_asset.id,
            "--verdict",
            "accept",
            "--reason",
            "Report supports a bounded acceptance.",
            "--idempotency-key",
            "review-decision-1",
            "--json",
        ],
    )
    assert decision.exit_code == 0, decision.output
    decided = json.loads(decision.output)["data"]
    assert decided["record_type"] == "task.review.decided"
    assert decided["contract_id"] == source.id
    assert decided["child_contract_id"] == reviewer.id
    assert decided["report_asset_id"] == report_asset.id
    assert decided["verdict"] == "accept"
    assert decided["actor_id"] == "plugin:task.review.decide.operator.v1"

    store = SQLiteStore.snapshot_from_path(database)
    assert project_task_status(source, store)["status"] == "completed"
    assert project_task_status(reviewer, store)["status"] == "completed"
    attested = store.scan_runtime_records(record_type="asset.attested")
    qualified = store.scan_runtime_records(record_type="output.qualified")
    assert len(attested) == len(qualified) == 1
    assert attested[0][1].payload["asset_id"] == target.id
    store.close()

    replay = runner.invoke(
        _app(),
        [
            "task",
            "review",
            "decide",
            request["record_id"],
            "--report",
            report_asset.id,
            "--verdict",
            "accept",
            "--reason",
            "Report supports a bounded acceptance.",
            "--idempotency-key",
            "review-decision-1",
            "--json",
        ],
    )
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["data"] == decided
    history = runner.invoke(_app(), ["task", "reviews", source.id, "--json"])
    assert history.exit_code == 0, history.output
    assert [item["record_type"] for item in json.loads(history.output)["data"]] == [
        "task.review.requested",
        "task.review.decided",
    ]
    human_history = runner.invoke(_app(), ["task", "reviews", source.id])
    assert human_history.exit_code == 0, human_history.output
    assert request["record_id"] in human_history.output
    assert report_asset.id in human_history.output
    reopened = SQLiteStore.snapshot_from_path(database)
    assert project_task_status(source, reopened)["status"] == "completed"
    assert project_task_status(reviewer, reopened)["status"] == "completed"
    assert len(reopened.scan_runtime_records(record_type="task.review.requested")) == 1
    assert len(reopened.scan_runtime_records(record_type="task.review.decided")) == 1
    reopened.close()


def test_invalid_request_and_unsupported_default_policy_do_not_provision_keys(
    tmp_path, monkeypatch
):
    database, root_key_path, source, _policy = _workspace(
        tmp_path, monkeypatch, verifier_capabilities=("verify.compliance",)
    )
    target, _producer = _create_target(database, root_key_path, source)
    runner = CliRunner()

    missing = runner.invoke(
        _app(),
        [
            "task",
            "request-review",
            "missing-contract",
            "--output",
            "research_summary",
            "--asset",
            target.id,
            "--reason",
            "Review this",
            "--json",
        ],
    )
    assert missing.exit_code == 1
    assert json.loads(missing.output)["error"]["code"] == "not_found"
    request_actor_key = _plugin_key_path(
        root_key_path, "plugin:task.review.request.operator.v1"
    )
    assert not request_actor_key.exists()

    request = _request_review(runner, source, target.id, key="unsupported-caps-review")
    reviewer = SQLiteStore.snapshot_from_path(database).get_contract(
        request["child_contract_id"]
    )
    assert reviewer is not None
    worker = _OutputWorker(
        "worker:reviewer-unsupported", request["report_output"], "Review text"
    )
    assert (
        _run_worker(database, root_key_path, reviewer, worker)["status"] == "accepted"
    )
    store = SQLiteStore.snapshot_from_path(database)
    report = store.get_assets_by_name(request["report_output"])[0]
    store.close()

    refused = runner.invoke(
        _app(),
        [
            "task",
            "review",
            "decide",
            request["record_id"],
            "--report",
            report.id,
            "--verdict",
            "accept",
            "--reason",
            "Cannot use default capability.",
            "--json",
        ],
    )
    assert refused.exit_code == 1
    assert json.loads(refused.output)["error"]["code"] == "invalid_input"
    # A failed default-capability preflight must not create the default decision actor.
    default_key = _plugin_key_path(
        root_key_path, "plugin:task.review.decide.operator.v1"
    )
    assert not default_key.exists()


def test_review_changes_decision_leaves_source_unqualified(tmp_path, monkeypatch):
    database, root_key_path, source, _policy = _workspace(tmp_path, monkeypatch)
    target, _producer = _create_target(database, root_key_path, source)
    runner = CliRunner()
    request = _request_review(runner, source, target.id, key="review-changes-request")
    reviewer = SQLiteStore.snapshot_from_path(database).get_contract(
        request["child_contract_id"]
    )
    assert reviewer is not None
    worker = _OutputWorker(
        "worker:reviewer-changes", request["report_output"], "Needs revision"
    )
    assert (
        _run_worker(database, root_key_path, reviewer, worker)["status"] == "accepted"
    )
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
            "changes",
            "--reason",
            "Evidence does not support a claim.",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["verdict"] == "changes"
    store = SQLiteStore.snapshot_from_path(database)
    assert not project_task_status(source, store)["outputs_satisfied"]
    assert not store.scan_runtime_records(record_type="output.qualified")
    store.close()


def test_custom_existing_actor_can_meet_extended_source_policy(tmp_path, monkeypatch):
    database, root_key_path, source, _policy = _workspace(
        tmp_path, monkeypatch, verifier_capabilities=("verify.compliance",)
    )
    target, _producer = _create_target(database, root_key_path, source)
    runner = CliRunner()
    request = _request_review(runner, source, target.id, key="custom-review-request")
    reviewer = SQLiteStore.snapshot_from_path(database).get_contract(
        request["child_contract_id"]
    )
    assert reviewer is not None
    worker = _OutputWorker(
        "worker:custom-reviewer",
        request["report_output"],
        "Evidence matches the report.",
    )
    assert (
        _run_worker(database, root_key_path, reviewer, worker)["status"] == "accepted"
    )
    store = SQLiteStore(database)
    report = store.get_assets_by_name(request["report_output"])[0]

    from aigineering.core.domain import load_genesis
    from aigineering.local_identity import write_actor_key
    from aigineering.protocol.candidate import ActorKey
    from aigineering.protocol.effect_builders import actor_authorization_effect

    genesis = load_genesis(store)
    root_key = genesis.root_keys[0]
    root_signer = load_actor_signer(root_key_path)

    limited_signer = Ed25519Signer()
    limited_key = ActorKey(
        "human:limited-review-board",
        "limited-review-key",
        limited_signer.kind,
        limited_signer.signer_id,
        ("asset.attest", "task.review.decide"),
    )
    limited_auth = publish_effect(
        store,
        store,
        genesis,
        root_key,
        root_signer,
        actor_authorization_effect(limited_key),
        idempotency_key="authorize-limited-review-board",
    )
    assert limited_auth.accepted
    limited_file = tmp_path / "limited-review-board.ed25519"
    write_actor_key(limited_file, limited_signer)
    os.chmod(limited_file, 0o600)

    custom_signer = Ed25519Signer()
    custom_key = ActorKey(
        "human:review-board",
        "review-board-key",
        custom_signer.kind,
        custom_signer.signer_id,
        ("asset.attest", "task.review.decide", "verify.compliance"),
    )
    authorized = publish_effect(
        store,
        store,
        genesis,
        root_key,
        root_signer,
        actor_authorization_effect(custom_key),
        idempotency_key="authorize-custom-review-board",
    )
    assert authorized.accepted
    key_file = tmp_path / "review-board.ed25519"
    write_actor_key(key_file, custom_signer)
    os.chmod(key_file, 0o600)
    store.close()

    rejected = runner.invoke(
        _app(),
        [
            "task",
            "review",
            "decide",
            request["record_id"],
            "--report",
            report.id,
            "--verdict",
            "accept",
            "--reason",
            "Insufficient custom policy caps.",
            "--actor-id",
            limited_key.actor_id,
            "--key-id",
            limited_key.key_id,
            "--key-file",
            str(limited_file),
            "--json",
        ],
    )
    assert rejected.exit_code == 1
    assert json.loads(rejected.output)["error"]["code"] == "invalid_input"
    assert not _plugin_key_path(
        root_key_path, "plugin:task.review.decide.operator.v1"
    ).exists()
    store = SQLiteStore.snapshot_from_path(database)
    assert not store.scan_runtime_records(record_type="task.review.decided")
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
            "accept",
            "--reason",
            "Authorized board review.",
            "--actor-id",
            custom_key.actor_id,
            "--key-id",
            custom_key.key_id,
            "--key-file",
            str(key_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    decision = json.loads(result.output)["data"]
    assert decision["actor_id"] == custom_key.actor_id
    assert decision["key_id"] == custom_key.key_id
    assert decision["verdict"] == "accept"
    store = SQLiteStore.snapshot_from_path(database)
    assert project_task_status(source, store)["status"] == "completed"
    store.close()


def test_postcommit_redis_pending_marks_durable_request_and_replays(
    tmp_path, monkeypatch
):
    database, root_key_path, source, _policy = _workspace(tmp_path, monkeypatch)
    target, _producer = _create_target(database, root_key_path, source)
    from aigineering.cli.main import cli
    from aigineering.runtime_services import RedisSyncPending
    import aigineering.cli.infrastructure as infrastructure
    import aigineering.task_policy_review as app_review

    monkeypatch.setattr(infrastructure, "require_redis", lambda: None)

    def pending_after_commit(_store, *, accepted=None):
        if accepted is True:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr(app_review, "sync_runtime", pending_after_commit)
    args = [
        "task",
        "request-review",
        source.id,
        "--output",
        "research_summary",
        "--asset",
        target.id,
        "--reason",
        "Retry relay after durable publication.",
        "--idempotency-key",
        "pending-task-review",
        "--json",
    ]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 1, result.output
    pending = json.loads(result.output)
    assert pending["error"] == "redis_sync_pending"
    assert pending["facts_retained"] is True
    assert pending["candidate_accepted"] is True

    monkeypatch.setattr(app_review, "sync_runtime", lambda *_args, **_kwargs: None)
    replay = CliRunner().invoke(cli, args)
    assert replay.exit_code == 0, replay.output
    data = json.loads(replay.output)["data"]
    store = SQLiteStore.snapshot_from_path(database)
    links = store.scan_runtime_records(record_type="task.review.requested")
    children = [
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == source.id
        and contract.name.startswith("Review source-study")
    ]
    store.close()
    assert len(links) == 1
    assert len(children) == 1
    assert data["record_id"] == links[0][1].id
    assert data["candidate_id"] == links[0][1].payload["candidate_id"]


def test_rejected_candidate_redis_pending_is_not_reported_as_accepted(
    tmp_path, monkeypatch
):
    database, _root_key_path, source, _policy = _workspace(tmp_path, monkeypatch)
    target, _producer = _create_target(database, _root_key_path, source)
    from aigineering.cli.main import cli
    from aigineering.runtime_services import RedisSyncPending
    import aigineering.cli.infrastructure as infrastructure
    import aigineering.task_policy_review as app_review

    monkeypatch.setattr(infrastructure, "require_redis", lambda: None)

    def pending_after_rejection(_store, *, accepted=None):
        if accepted is False:
            raise RedisSyncPending(accepted=False)

    monkeypatch.setattr(app_review, "sync_runtime", pending_after_rejection)
    args = [
        "task",
        "request-review",
        source.id,
        "--output",
        "research_summary",
        "--asset",
        target.id,
        "--reason",
        "This exceeds remaining allowance.",
        "--budget",
        "99",
        "--idempotency-key",
        "review-rejected-pending",
        "--json",
    ]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 1, result.output
    pending = json.loads(result.output)
    assert pending["error"] == "redis_sync_pending"
    assert pending["facts_retained"] is True
    assert pending["candidate_accepted"] is False

    monkeypatch.setattr(app_review, "sync_runtime", lambda *_args, **_kwargs: None)
    replay = CliRunner().invoke(cli, args)
    assert replay.exit_code == 1, replay.output
    rejected = json.loads(replay.output)
    assert rejected["error"]["code"] == "rejected"
    store = SQLiteStore.snapshot_from_path(database)
    assert len(store.scan_runtime_records(record_type="candidate.rejected")) == 1
    assert not store.scan_runtime_records(record_type="task.review.requested")
    assert not any(
        contract.parent_id == source.id
        and contract.name.startswith("Review source-study")
        for contract in store.get_all_contracts()
    )
    store.close()


def test_request_review_accepts_canonical_retry_source(tmp_path, monkeypatch):
    from aigineering.agent.worker import WorkerExecutionError
    from aigineering import local_publication
    from aigineering.local_identity import ensure_local_worker_host
    from aigineering.runtime import WorkerInvocationError
    from aigineering.task_lineage import task_lineage
    from aigineering.task_publication import retry_local_task_result

    database, root_key_path, source, _policy = _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(local_publication, "require_redis", lambda: None)
    monkeypatch.setattr(
        local_publication, "sync_runtime", lambda *_args, **_kwargs: None
    )
    store = SQLiteStore(database)

    class _FailingWorker:
        worker_id = "worker:retry-source-failure"

        def invoke(self, contract, disclosed_assets):
            del contract, disclosed_assets
            raise WorkerExecutionError("fixture_failure", "fixture failure")

    failed_host = ensure_local_worker_host(store, _FailingWorker())
    failed_claim = claim_next_package(
        store, worker_id=failed_host.worker_id, contract_id=source.id
    )
    assert failed_claim is not None
    try:
        execute_claimed_package(failed_claim, failed_host, store)
    except WorkerInvocationError:
        pass
    else:
        raise AssertionError("source Worker failure did not reach runtime failure path")
    assert len(store.scan_runtime_records(record_type="worker.invocation_failed")) == 1
    retry_result, _revision = retry_local_task_result(store, source.id)
    retry = store.get_contract(retry_result["retry_contract_id"])
    assert retry is not None and retry.origin == "retry"
    store.close()

    target, producer = _create_target(database, root_key_path, retry)
    request = _request_review(CliRunner(), retry, target.id, key="retry-review-request")
    store = SQLiteStore.snapshot_from_path(database)
    reviewer = store.get_contract(request["child_contract_id"])
    assert reviewer is not None
    assert reviewer.parent_id == retry.id
    assert reviewer.origin == "human"
    assert not project_task_status(retry, store)["outputs_satisfied"]
    store.close()

    report_worker = _OutputWorker(
        "worker:retry-independent-reviewer",
        request["report_output"],
        "The report is bounded by the frozen evidence.",
    )
    assert producer.worker_id != report_worker.worker_id
    assert (
        _run_worker(database, root_key_path, reviewer, report_worker)["status"]
        == "accepted"
    )
    store = SQLiteStore.snapshot_from_path(database)
    report = store.get_assets_by_name(request["report_output"])[0]
    assert not project_task_status(retry, store)["outputs_satisfied"]
    store.close()
    runner = CliRunner()
    decision_args = [
        "task",
        "review",
        "decide",
        request["record_id"],
        "--report",
        report.id,
        "--verdict",
        "accept",
        "--reason",
        "The report supports a bounded acceptance.",
        "--idempotency-key",
        "retry-review-decision",
        "--json",
    ]
    decision = runner.invoke(_app(), decision_args)
    assert decision.exit_code == 0, decision.output
    decided = json.loads(decision.output)["data"]
    assert decided["contract_id"] == retry.id

    store = SQLiteStore(database)
    counts_before = {
        record_type: len(store.scan_runtime_records(record_type=record_type))
        for record_type in (
            "task.review.requested",
            "task.review.decided",
            "claim.granted",
            "output.qualified",
            "asset.attested",
        )
    }
    store.rebuild_runtime_materializations()
    store.close()
    replay = _request_review(runner, retry, target.id, key="retry-review-request")
    assert replay == request
    replayed_decision = runner.invoke(_app(), decision_args)
    assert replayed_decision.exit_code == 0, replayed_decision.output
    assert json.loads(replayed_decision.output)["data"] == decided

    store = SQLiteStore.snapshot_from_path(database)
    assert {
        record_type: len(store.scan_runtime_records(record_type=record_type))
        for record_type in counts_before
    } == counts_before
    lineage = task_lineage(store, retry.id)["data"]
    assert not lineage["issues"]
    edges = lineage["edges"]
    assert any(
        edge["kind"] == "retry_publication"
        and edge["source_contract_id"] == source.id
        and edge["target_contract_id"] == retry.id
        for edge in edges
    )
    assert any(
        edge["kind"] == "containment"
        and edge["source_contract_id"] == retry.id
        and edge["target_contract_id"] == reviewer.id
        for edge in edges
    )
    assert not any(
        edge["kind"] == "retry_publication"
        and edge["source_contract_id"] == retry.id
        and edge["target_contract_id"] == reviewer.id
        for edge in edges
    )
    assert store.get_contract(retry.id).origin == "retry"
    assert project_task_status(retry, store)["status"] == "completed"
    store.close()


@pytest.mark.parametrize("source_label", ("method:plan", "plugin:example.owner"))
def test_retry_review_rejects_method_and_plugin_labeled_sources(
    tmp_path, monkeypatch, source_label
):
    from dataclasses import replace

    from aigineering.local_identity import ensure_local_plugin_publisher
    from aigineering.plugins.task_review import TASK_REVIEW_EFFECTS
    from aigineering.protocol.effect_builders import task_review_request_effect
    from aigineering.task_publication import retry_local_task_result

    database, root_key_path, source, _policy = _workspace(
        tmp_path, monkeypatch, source_labels=(source_label,)
    )
    from aigineering import local_publication

    monkeypatch.setattr(local_publication, "require_redis", lambda: None)
    monkeypatch.setattr(
        local_publication, "sync_runtime", lambda *_args, **_kwargs: None
    )
    store = SQLiteStore(database)
    retry_result, _revision = retry_local_task_result(store, source.id)
    retry = store.get_contract(retry_result["retry_contract_id"])
    assert retry is not None and retry.origin == "retry"
    store.close()
    target, _producer = _create_target(database, root_key_path, retry)

    runner = CliRunner()
    app_result = runner.invoke(
        _app(),
        [
            "task",
            "request-review",
            retry.id,
            "--output",
            "research_summary",
            "--asset",
            target.id,
            "--reason",
            "Reject method and plugin work.",
            "--json",
        ],
    )
    assert app_result.exit_code == 1
    assert json.loads(app_result.output)["error"]["message"] == (
        "task review requires an ordinary business Contract"
    )
    assert not _plugin_key_path(
        root_key_path, "plugin:task.review.request.operator.v1"
    ).exists()

    store = SQLiteStore(database)
    publisher = replace(
        ensure_local_plugin_publisher(
            store,
            "task.review.request.operator.v1",
            ("contract.publish", "task.review.request"),
        ),
        additional_effects=TASK_REVIEW_EFFECTS,
    )
    decision = publisher.publish(
        (
            task_review_request_effect(
                retry.id,
                "research_summary",
                target.id,
                "Reject method and plugin work.",
            ),
        ),
        idempotency_key=f"plugin-negative:{source_label}",
    )
    assert not decision.accepted
    rejection = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )
    assert (
        "task.review.request requires an ordinary business Contract"
        in (rejection.payload["reason"])
    )
    assert not store.scan_runtime_records(record_type="task.review.requested")
    store.close()


@pytest.mark.parametrize("origin", ("system", "recovery", "unrecognized-origin"))
def test_task_review_rejects_nonbusiness_origins_after_signed_request_setup(
    tmp_path, monkeypatch, origin
):
    from dataclasses import replace

    from aigineering.core.domain import load_genesis
    from aigineering.local_identity import (
        ensure_local_plugin_publisher,
        load_actor_signer,
    )
    from aigineering.plugins.task_review import TASK_REVIEW_EFFECTS
    from aigineering.protocol.effect_builders import (
        contract_declaration_effect,
        task_review_request_effect,
    )
    from aigineering.protocol.identity import contract_from_fields

    database, root_key_path, _business_source, policy = _workspace(
        tmp_path, monkeypatch
    )
    store = SQLiteStore(database)
    genesis = load_genesis(store)
    root_key = genesis.root_keys[0]
    source = contract_from_fields(
        name=f"unsupported-origin-{origin}",
        description="A signed source used to reach the review origin guard.",
        inputs=(),
        outputs=("research_summary",),
        activation="",
        budget=5,
        tool_scope=(),
        labels=(),
        worker_capabilities=(),
        worker_pools=(),
        delegation_capabilities=(),
        delegation_pools=(),
        origin=origin,
        minting_authority=(),
        sensitive_input_policy=None,
        acceptance_policy=policy,
        context_asset_ids=(),
    )
    publication = publish_effect(
        store,
        store,
        genesis,
        root_key,
        load_actor_signer(root_key_path),
        contract_declaration_effect(source),
        idempotency_key=f"unsupported-origin:{origin}",
    )
    assert publication.accepted

    app_result = CliRunner().invoke(
        _app(),
        [
            "task",
            "request-review",
            source.id,
            "--output",
            "research_summary",
            "--asset",
            "unneeded-before-origin-guard",
            "--reason",
            "Unsupported origins stay excluded.",
            "--json",
        ],
    )
    assert app_result.exit_code == 1
    assert json.loads(app_result.output)["error"]["message"] == (
        "task review requires an ordinary business Contract"
    )

    publisher = replace(
        ensure_local_plugin_publisher(
            store,
            "task.review.request.operator.v1",
            ("contract.publish", "task.review.request"),
        ),
        additional_effects=TASK_REVIEW_EFFECTS,
    )
    decision = publisher.publish(
        (
            task_review_request_effect(
                source.id,
                "research_summary",
                "unneeded-before-origin-guard",
                "Unsupported origins stay excluded.",
            ),
        ),
        idempotency_key=f"unsupported-origin-request:{origin}",
    )
    assert not decision.accepted
    rejection = next(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.rejected"
    )
    assert (
        "task.review.request requires an ordinary business Contract"
        in (rejection.payload["reason"])
    )
    assert not store.scan_runtime_records(record_type="task.review.requested")
    store.close()
