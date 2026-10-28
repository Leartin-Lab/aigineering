"""Deterministic task-board CLI coverage over signed committed fixtures."""

from __future__ import annotations

import json
from copy import copy
from inspect import unwrap
from types import SimpleNamespace

from click.testing import CliRunner

from aigineering.cli import task_board as board
from aigineering.cli.run import _finish_target_run
from aigineering.core.candidate_publisher import CandidatePublisher, publish_effect
from aigineering.core.control_plane import (
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.acceptance import acceptance_policy_id
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.domain import initialize_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.plugins.task_assignment import TASK_ASSIGNMENT_EFFECTS
from aigineering.protocol.candidate import (
    ActorKey,
    create_candidate_proposal,
    create_genesis_manifest,
)
from aigineering.protocol.effect_builders import (
    asset_attestation_effect,
    asset_proposal_effect,
    contract_declaration_effect,
    task_assign_effect,
    worker_registration_effect,
)
from aigineering.protocol.types import Candidate
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.agent.worker import WorkerHost
from aigineering.runtime_services import RedisSyncPending
from conftest import candidate_runtime


class _Projection:
    def __init__(self, store):
        self.store = store

    def get_all_contracts(self):
        return self.store.get_all_contracts()

    def get_contract(self, contract_id):
        return self.store.get_contract(contract_id)

    def memoize_json(self, _name, _revision, builder):
        return builder()


class _Collaboration:
    def __init__(self):
        self.sync_count = 0

    def sync(self):
        self.sync_count += 1


def _candidate(genesis, actor_id, key_id, signer, effect, nonce):
    return create_candidate_proposal(
        domain_id=genesis.id,
        actor_id=actor_id,
        key_id=key_id,
        effects=(effect,),
        signer=signer,
        idempotency_key=nonce,
    )


def _cli_group():
    import click

    group = click.Group("task")
    board.register_task_board_commands(group)
    # ``protect_commands`` may wrap the module-level Commands when the main CLI
    # is imported. These tests exercise the leaf callback contract directly,
    # so use local shallow copies and unwrap only the copies.
    group.commands = {
        name: _unwrapped_command(command) for name, command in group.commands.items()
    }
    return group


def _unwrapped_command(command):
    local = copy(command)
    local.callback = unwrap(local.callback)
    return local


def _signed_fixture(tmp_path, monkeypatch, *, output_content=None, promptable=True):
    db_path = tmp_path / "board.sqlite"
    setup = SQLiteStore(db_path=str(db_path))
    publisher = candidate_runtime(setup)
    parent = publisher.accept_contract(
        build_control_plane_contract(name="research", description="Find evidence.")
    )
    child = publisher.accept_contract(
        build_control_plane_contract(
            name="write report",
            description="Write a report.",
            outputs=("report",),
            budget=3,
        )
    )
    if output_content is not None:
        publisher.accept_asset(
            build_control_plane_asset(
                name="report", content=output_content, promptable=promptable
            )
        )
    setup.close()

    services = _attach_cli(monkeypatch, db_path)

    def open_store():
        return SQLiteStore(db_path=str(db_path))

    return parent, child, services, open_store


def _disabled_assignee_fixture(tmp_path, monkeypatch):
    db_path = tmp_path / "assigned.sqlite"
    store = SQLiteStore(db_path=str(db_path))
    signer = Ed25519Signer()
    actor_key = ActorKey(
        "worker:assigned",
        "assignment-1",
        signer.kind,
        signer.signer_id,
        ("contract.publish", "task.assignment.manage", "worker.register"),
    )
    genesis = create_genesis_manifest(
        "task-board-assignment", (actor_key,), "policy:task-board"
    )
    initialize_genesis(store, genesis)
    contract = build_control_plane_contract(
        name="assigned target",
        description="Blocked because its exact assignee is disabled.",
        outputs=("assigned-result",),
        budget=3,
    )
    declared = publish_effect(
        store,
        store,
        genesis,
        actor_key,
        signer,
        contract_declaration_effect(contract),
        idempotency_key="board-assigned-contract",
    )
    assert declared.accepted
    registration = WorkerRegistration(
        "worker:assigned",
        capacity=1,
        actor_id=actor_key.actor_id,
        key_id=actor_key.key_id,
    )
    enabled = publish_effect(
        store,
        store,
        genesis,
        actor_key,
        signer,
        worker_registration_effect(registration),
        idempotency_key="board-assigned-worker-enabled",
    )
    assert enabled.accepted, [
        (record.record_type, dict(record.payload)) for record in enabled.runtime_records
    ]
    assigned = publish_effect(
        store,
        store,
        genesis,
        actor_key,
        signer,
        task_assign_effect(contract.id, registration.worker_id, "", "initial owner"),
        idempotency_key="board-assignment",
        additional_effects=TASK_ASSIGNMENT_EFFECTS,
    )
    assert assigned.accepted
    disabled = publish_effect(
        store,
        store,
        genesis,
        actor_key,
        signer,
        worker_registration_effect(
            WorkerRegistration(
                registration.worker_id,
                capacity=1,
                enabled=False,
                version="2",
                actor_id=actor_key.actor_id,
                key_id=actor_key.key_id,
            )
        ),
        idempotency_key="board-assigned-worker-disabled",
    )
    assert disabled.accepted, disabled.trace_entries
    store.close()

    services = _attach_cli(monkeypatch, db_path)

    def open_store():
        return SQLiteStore(db_path=str(db_path))

    return contract, services, open_store


def _attach_cli(monkeypatch, db_path):
    services = []

    def open_store():
        return SQLiteStore(db_path=str(db_path))

    def collaboration(store):
        service = _Collaboration()
        services.append(service)
        return service

    monkeypatch.setattr(board, "_persistent_snapshot", open_store)
    monkeypatch.setattr(board, "persistent_store", open_store)
    monkeypatch.setattr(board, "require_redis", lambda: None)
    monkeypatch.setattr(board, "collaboration_service", collaboration)
    monkeypatch.setattr(board, "query_projection", lambda store: _Projection(store))
    return services


def _invoke(args):
    return CliRunner().invoke(_cli_group(), args)


def test_list_board_stats_and_show_use_signed_committed_facts(tmp_path, monkeypatch):
    root, child, services, _open_store = _signed_fixture(tmp_path, monkeypatch)

    listed = _invoke(["list", "--root", root.id, "--json"])
    assert listed.exit_code == 0, listed.output
    envelope = json.loads(listed.output)
    assert envelope["ok"] is True
    assert [item["contract_id"] for item in envelope["data"]["tasks"]] == [root.id]
    assert envelope["data"]["tasks"][0]["priority"] == 0
    assert envelope["data"]["tasks"][0]["priority_fact_id"] is None
    assert envelope["revision"] > 0

    board_result = _invoke(["board", "--json"])
    assert board_result.exit_code == 0, board_result.output
    assert json.loads(board_result.output)["data"]["total"] == 2
    assert all(
        row["priority"] == 0 and row["priority_fact_id"] is None
        for rows in json.loads(board_result.output)["data"]["columns"].values()
        for row in rows
    )
    sorted_board = _invoke(["board", "--sort", "priority", "--json"])
    assert sorted_board.exit_code == 0, sorted_board.output
    assert json.loads(sorted_board.output)["data"]["total"] == 2

    stats = _invoke(["stats", "--json"])
    assert stats.exit_code == 0, stats.output
    assert json.loads(stats.output)["data"]["total"] == 2

    shown = _invoke(["show", child.id, "--json"])
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.output)["data"]
    assert data["contract"]["contract_id"] == child.id
    assert data["task"]["output_semantics"].startswith(
        "task-view asset-name assertions"
    )
    assert "children" in data and "recovery_contract_ids" in data
    assert all(service.sync_count == 1 for service in services)


def test_aggregate_query_uses_one_revision_scan_and_matches_legacy_cards(
    tmp_path, monkeypatch
):
    _root, _child, _services, open_store = _signed_fixture(tmp_path, monkeypatch)
    store = open_store()
    try:
        contracts = store.get_all_contracts()
        revision = store.get_runtime_revision()
        scans = []
        original_scan = store.scan_runtime_records
        fact_fingerprint = tuple(
            (record_revision, record.record_type, record.id)
            for record_revision, record in original_scan()
        )

        def counted_scan(**kwargs):
            scans.append(kwargs)
            return original_scan(**kwargs)

        store.scan_runtime_records = counted_scan
        effect_context = load_effect_projection_context(store)
        histories = board.task_priority_histories(
            effect_context, tuple(item.id for item in contracts)
        )
        legacy_cards = []
        by_id = {item.id: item for item in contracts}
        for contract in sorted(contracts, key=lambda item: item.id):
            status = board.project_task_status(
                contract,
                store,
                projection_context=effect_context,
                priority_facts=histories[contract.id],
            )
            current = contract
            seen = set()
            while (
                current.parent_id
                and current.parent_id in by_id
                and current.id not in seen
            ):
                seen.add(current.id)
                current = by_id[current.parent_id]
            legacy_cards.append(
                {
                    "contract_id": contract.id,
                    "root_contract_id": current.id,
                    "parent_id": contract.parent_id,
                    "name": contract.name,
                    "status": status["status"],
                    "terminal": status["terminal"],
                    "blockers": status["blockers"],
                    "budget_remaining": status["budget_remaining"],
                    "outputs_satisfied": status["outputs_satisfied"],
                    "recoveries": status["recoveries"],
                    "assignment": status["assignment"],
                    "priority": status["priority"],
                    "priority_fact_id": status["priority_fact_id"],
                }
            )

        scans.clear()
        actual = board._fetch_cards(store, None, list(reversed(contracts)), revision)
        assert actual == legacy_cards
        assert scans == [{}]
        assert store.get_runtime_revision() == revision
        assert (
            tuple(
                (record_revision, record.record_type, record.id)
                for record_revision, record in original_scan()
            )
            == fact_fingerprint
        )
    finally:
        store.close()


def test_priority_values_and_sort_use_signed_nonzero_history(tmp_path, monkeypatch):
    from aigineering.plugins.task_priority import TASK_PRIORITY_EFFECTS
    from aigineering.protocol.effect_builders import task_priority_effect

    db_path = tmp_path / "priority-board.sqlite"
    store = SQLiteStore(db_path=str(db_path))
    runtime = candidate_runtime(store)
    tasks = [
        runtime.accept_contract(
            build_control_plane_contract(name=f"priority {index}", description="")
        )
        for index in range(2)
    ]
    signer = Ed25519Signer()
    priority_actor = ActorKey(
        "operator:priority-board",
        "priority-board-key",
        signer.kind,
        signer.signer_id,
        ("task.priority.manage",),
    )
    runtime.authorize_actor(priority_actor)
    priority_publisher = CandidatePublisher(
        store,
        store,
        runtime.genesis,
        priority_actor,
        signer,
        additional_effects=TASK_PRIORITY_EFFECTS,
    )
    declarations = {
        record.payload["contract"]["id"]: record.id
        for _revision, record in store.scan_runtime_records(
            record_type="contract.declared"
        )
    }
    default_ids = [item.id for item in sorted(tasks, key=lambda item: item.id)]
    low_id, high_id = default_ids
    for contract_id, value, reason in (
        (low_id, -6, "lower board priority"),
        (high_id, 9, "higher board priority"),
    ):
        decision = priority_publisher.publish(
            (task_priority_effect(contract_id, value, "", reason),),
            idempotency_key=f"board-priority-{value}",
            causal_parents=(declarations[contract_id],),
        )
        assert decision.accepted, decision
    store.close()

    _attach_cli(monkeypatch, db_path)
    default_list = _invoke(["list", "--json"])
    assert default_list.exit_code == 0, default_list.output
    default_rows = json.loads(default_list.output)["data"]["tasks"]
    assert [item["contract_id"] for item in default_rows] == default_ids
    assert [item["priority"] for item in default_rows] == [-6, 9]
    assert all(item["priority_fact_id"].startswith("record:") for item in default_rows)

    sorted_list = _invoke(["list", "--sort", "priority", "--json"])
    assert sorted_list.exit_code == 0, sorted_list.output
    sorted_rows = json.loads(sorted_list.output)["data"]["tasks"]
    assert [item["contract_id"] for item in sorted_rows] == list(reversed(default_ids))
    assert [item["priority"] for item in sorted_rows] == [9, -6]

    default_board = _invoke(["board", "--json"])
    priority_board = _invoke(["board", "--sort", "priority", "--json"])
    assert default_board.exit_code == priority_board.exit_code == 0
    board_rows = [
        row
        for rows in json.loads(priority_board.output)["data"]["columns"].values()
        for row in rows
    ]
    assert [item["contract_id"] for item in board_rows] == list(reversed(default_ids))
    show = _invoke(["show", high_id, "--json"])
    assert show.exit_code == 0, show.output
    shown = json.loads(show.output)["data"]["task"]
    assert shown["priority"] == 9
    assert shown["priority_fact_id"] == next(
        item["priority_fact_id"]
        for item in sorted_rows
        if item["contract_id"] == high_id
    )


def test_legacy_materialized_task_views_default_priority_without_fact_history(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-priority-board.sqlite"
    store = SQLiteStore(db_path=str(db_path))
    contract = build_control_plane_contract(name="legacy task", description="")
    store.add_contract(contract)
    store.close()
    _attach_cli(monkeypatch, db_path)

    listed = _invoke(["list", "--json"])
    assert listed.exit_code == 0, listed.output
    row = json.loads(listed.output)["data"]["tasks"][0]
    assert row["contract_id"] == contract.id
    assert row["priority"] == 0
    assert row["priority_fact_id"] is None
    shown = _invoke(["show", contract.id, "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["data"]["task"]["priority"] == 0


def test_list_uses_exact_root_id_and_json_not_found_is_nonzero(tmp_path, monkeypatch):
    root, child, _services, _open_store = _signed_fixture(tmp_path, monkeypatch)
    result = _invoke(["list", "--root", root.id, "--json"])
    assert result.exit_code == 0, result.output
    ids = {item["contract_id"] for item in json.loads(result.output)["data"]["tasks"]}
    assert ids == {root.id}
    child_only = _invoke(["list", "--root", child.id, "--json"])
    assert child_only.exit_code == 0, child_only.output
    assert {
        item["contract_id"] for item in json.loads(child_only.output)["data"]["tasks"]
    } == {child.id}
    missing = _invoke(["show", "not-a-contract", "--json"])
    assert missing.exit_code == 1
    assert json.loads(missing.output)["error"]["code"] == "not_found"


def test_list_filters_derived_disabled_assignment_and_run_guidance_is_actionable(
    tmp_path, monkeypatch
):
    contract, _services, open_store = _disabled_assignee_fixture(tmp_path, monkeypatch)

    filtered = _invoke(["list", "--status", "blocked_assignment", "--json"])
    assert filtered.exit_code == 0, filtered.output
    tasks = json.loads(filtered.output)["data"]["tasks"]
    assert [item["contract_id"] for item in tasks] == [contract.id]
    assert tasks[0]["status"] == "blocked_assignment"
    assert any("assignment:" in item for item in tasks[0]["blockers"])

    ready = _invoke(["list", "--status", "ready", "--json"])
    assert ready.exit_code == 0, ready.output
    assert all(
        item["contract_id"] != contract.id
        for item in json.loads(ready.output)["data"]["tasks"]
    )

    store = open_store()
    try:
        status = board._safe_status(contract, store)
    finally:
        store.close()
    assert status["status"] == "blocked_assignment"
    import click

    @click.command()
    def target_run_result():
        _finish_target_run(status, 0, stop_reason="idle", json_output=True)

    result = CliRunner().invoke(target_run_result)
    assert result.exit_code == 1
    run_payload = json.loads(result.output)
    assert run_payload["status"] == "blocked_assignment"
    guidance = run_payload["next_step"]
    assert "worker:assigned" in guidance
    assert "disabled" in guidance
    assert "another Worker cannot take over" in guidance
    assert "never been claimed" in guidance
    assert "aig task assignments" in guidance


def test_exact_root_filter_includes_only_immutable_descendants():
    root = SimpleNamespace(id="task:root", parent_id=None)
    child = SimpleNamespace(id="task:child", parent_id=root.id)
    unrelated = SimpleNamespace(id="task:other", parent_id=None)
    contracts = [root, child, unrelated]
    cards = [{"contract_id": contract.id, "status": "ready"} for contract in contracts]
    selected = board._select_cards(cards, contracts, statuses=(), root_id=root.id)
    assert {item["contract_id"] for item in selected} == {root.id, child.id}


def test_result_refuses_incomplete_task_and_cancel_requires_stable_reason(
    tmp_path, monkeypatch
):
    _root, child, _services, _open_store = _signed_fixture(tmp_path, monkeypatch)

    result = _invoke(["result", child.id, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "not_completed"

    invalid = _invoke(["cancel", child.id, "--reason", "   ", "--json"])
    assert invalid.exit_code == 2
    assert json.loads(invalid.output)["error"]["code"] == "invalid_reason"


def test_retry_and_cancel_delegate_signed_effect_publication(tmp_path, monkeypatch):
    _root, child, _services, _open_store = _signed_fixture(tmp_path, monkeypatch)
    calls = []

    class Decision:
        accepted = True
        contract = None

    def cancel(_store, contract_id, reason, *, idempotency_key=None):
        calls.append(("cancel", contract_id, reason, idempotency_key))
        return (
            {
                "contract_id": contract_id,
                "status": "ready",
                "accepted": True,
                "reason": reason.strip(),
            },
            10,
        )

    def retry(_store, contract_id):
        calls.append(("retry_local_task_result", contract_id))
        original = _store.get_contract(contract_id)
        return (
            {
                "original_contract_id": original.id,
                "retry_contract_id": "task:v6:retry-result",
                "budget": original.budget,
                "status": "declared",
                "record_id": "record:retry-result",
                "record_revision": 10,
                "candidate_id": "candidate:v1:retry-result",
                "idempotency_key": f"retry:{original.id}:task:v6:retry-result",
                "fact_ids": ["record:retry-result"],
            },
            10,
        )

    monkeypatch.setattr("aigineering.task_publication.publish_task_cancel", cancel)
    monkeypatch.setattr(board, "retry_local_task_result", retry)

    cancelled = _invoke(
        ["cancel", child.id, "--reason", "operator requested", "--json"]
    )
    assert cancelled.exit_code == 0, cancelled.output
    assert calls[-1] == ("cancel", child.id, "operator requested", None)
    assert json.loads(cancelled.output)["data"]["status"] == "ready"

    retried = _invoke(["retry", child.id, "--json"])
    assert retried.exit_code == 0, retried.output
    assert calls[-1] == ("retry_local_task_result", child.id)
    assert json.loads(retried.output)["data"]["original_contract_id"] == child.id


def test_list_reports_truncation_and_rejects_unknown_status(tmp_path, monkeypatch):
    _root, _child, _services, _open_store = _signed_fixture(tmp_path, monkeypatch)
    limited = _invoke(["list", "--limit", "1", "--json"])
    assert limited.exit_code == 0, limited.output
    data = json.loads(limited.output)["data"]
    assert data["total"] == 2
    assert data["matching_total"] == 2
    assert data["limit"] == 1
    assert data["has_more"] is True

    invalid = _invoke(["list", "--status", "mystery", "--json"])
    assert invalid.exit_code == 2


def test_query_error_does_not_echo_store_exception_text(tmp_path, monkeypatch):
    _signed_fixture(tmp_path, monkeypatch)

    def unavailable(_store):
        raise RuntimeError("sealed-provider-secret")

    monkeypatch.setattr(board, "query_projection", unavailable)
    result = _invoke(["list", "--json"])
    assert result.exit_code == 1
    assert "sealed-provider-secret" not in result.output
    assert "RuntimeError" in result.output


def test_postcommit_redis_sync_pending_is_left_for_outer_cli_handler(
    tmp_path, monkeypatch
):
    _root, child, _services, _open_store = _signed_fixture(tmp_path, monkeypatch)

    def pending(*_args, **_kwargs):
        raise RedisSyncPending(
            accepted=True, data={"candidate_id": "candidate:x"}, revision=8
        )

    monkeypatch.setattr("aigineering.task_publication.publish_task_cancel", pending)
    result = _invoke(["cancel", child.id, "--reason", "operator requested", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "redis_sync_pending"
    assert json.loads(result.output)["error"]["candidate_accepted"] is True


def test_result_exports_exact_utf8_bytes_and_never_overwrites(tmp_path, monkeypatch):
    content = "first\r\nsecond\n終"
    _root, contract, _services, _open_store = _signed_fixture(
        tmp_path, monkeypatch, output_content=content
    )

    destination = tmp_path / "report.txt"
    exported = _invoke(["result", contract.id, "--to", str(destination), "--json"])
    assert exported.exit_code == 0, exported.output
    envelope = json.loads(exported.output)
    metadata = envelope["data"]["outputs"][0]
    assert destination.read_bytes() == content.encode("utf-8")
    assert metadata["content_bytes"] == len(content.encode("utf-8"))

    destination.write_bytes(b"keep me")
    overwrite = _invoke(["result", contract.id, "--to", str(destination), "--json"])
    assert overwrite.exit_code == 1
    assert json.loads(overwrite.output)["error"]["code"] == "output_exists"
    assert destination.read_bytes() == b"keep me"


def test_result_checks_fact_and_view_integrity(tmp_path, monkeypatch):
    _root, contract, _services, _open_store = _signed_fixture(
        tmp_path, monkeypatch, output_content="grounded report"
    )
    real_project = board.project_task_status
    checks = []

    def project(contract, store, *, check_integrity=False):
        checks.append(check_integrity)
        return real_project(contract, store, check_integrity=check_integrity)

    monkeypatch.setattr(board, "project_task_status", project)
    result = _invoke(["result", contract.id, "--json"])
    assert result.exit_code == 0, result.output
    assert checks == [True]


def test_result_refuses_nonpromptable_assets(tmp_path, monkeypatch):
    _root, contract, _services, _open_store = _signed_fixture(
        tmp_path,
        monkeypatch,
        output_content="sealed provider configuration",
        promptable=False,
    )
    path = tmp_path / "must-not-exist.txt"
    result = _invoke(["result", contract.id, "--to", str(path), "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "result_unavailable"
    assert not path.exists()


def test_result_exports_only_exact_independently_qualified_asset(tmp_path, monkeypatch):
    db_path = tmp_path / "independent.sqlite"
    store = SQLiteStore(db_path=str(db_path))
    owner = Ed25519Signer()
    producer = Ed25519Signer()
    verifier = Ed25519Signer()
    owner_key = ActorKey(
        "human:owner",
        "owner-key",
        owner.kind,
        owner.signer_id,
        ("asset.publish", "contract.publish", "worker.register"),
    )
    producer_key = ActorKey(
        "worker:producer",
        "producer-key",
        producer.kind,
        producer.signer_id,
        ("worker.submit", "asset.attest", "verify.compliance"),
    )
    verifier_key = ActorKey(
        "worker:verifier",
        "verifier-key",
        verifier.kind,
        verifier.signer_id,
        ("asset.attest", "verify.compliance"),
    )
    genesis = create_genesis_manifest(
        "task-board-independent",
        (owner_key, producer_key, verifier_key),
        "policy:task-board-independent",
    )
    initialize_genesis(store, genesis)
    committer = CandidateCommitter(store, store)

    for name, content in (("review_rubric", "be exact"), ("review_evidence", "source")):
        proposal = build_control_plane_asset(name=name, content=content)
        decision = committer.commit(
            _candidate(
                genesis,
                owner_key.actor_id,
                owner_key.key_id,
                owner,
                asset_proposal_effect(proposal),
                f"asset:{name}",
            ),
            genesis,
        )
        assert decision.accepted
    policy = {
        "mode": "independent",
        "policy_version": "review-v1",
        "rubric_asset_ids": [store.get_assets_by_name("review_rubric")[0].id],
        "evidence_asset_ids": [store.get_assets_by_name("review_evidence")[0].id],
        "required_attestations": 1,
        "verifier_capabilities": ["verify.compliance"],
    }
    contract = build_control_plane_contract(
        name="independent report", outputs=("report",), acceptance_policy=policy
    )
    assert committer.commit(
        _candidate(
            genesis,
            owner_key.actor_id,
            owner_key.key_id,
            owner,
            contract_declaration_effect(contract),
            "contract:report",
        ),
        genesis,
    ).accepted
    registration = WorkerRegistration(
        "worker:producer", actor_id="worker:producer", key_id="producer-key"
    )
    assert committer.commit(
        _candidate(
            genesis,
            owner_key.actor_id,
            owner_key.key_id,
            owner,
            worker_registration_effect(registration),
            "register:producer",
        ),
        genesis,
    ).accepted
    claimed = claim_next_package(store, worker_id="worker:producer")
    assert claimed is not None

    class Producer:
        worker_id = "worker:producer"

        def invoke(self, _contract, _assets):
            return Candidate(
                worker_id=self.worker_id,
                raw_output=r'/exec {"outputs":{"report":"qualified bytes\r\n"}}',
            )

    host = WorkerHost(Producer(), genesis, producer_key, producer)
    outcome = execute_claimed_package(claimed, host, store)
    assert outcome["status"] == "accepted"
    output = store.get_assets_by_name("report")[0]

    # A name match alone cannot export the unqualified Candidate output.
    _attach_cli(monkeypatch, db_path)
    rejected = _invoke(["result", contract.id, "--json"])
    assert rejected.exit_code == 1
    assert json.loads(rejected.output)["error"]["code"] == "not_completed"

    attestation = asset_attestation_effect(
        contract.id,
        "report",
        output.id,
        policy_id=acceptance_policy_id(policy),
        policy_version=policy["policy_version"],
        verdict="accepted",
        rubric_asset_ids=tuple(policy["rubric_asset_ids"]),
        evidence_asset_ids=tuple(policy["evidence_asset_ids"]),
    )
    qualified = committer.commit(
        _candidate(
            genesis,
            verifier_key.actor_id,
            verifier_key.key_id,
            verifier,
            attestation,
            "attest:report",
        ),
        genesis,
    )
    assert qualified.accepted

    # Add a higher-sorting unqualified same-name asset; only the exact
    # output.qualified asset may be shown or written.
    outsider = next(
        proposal
        for index in range(10000)
        if (
            proposal := build_control_plane_asset(
                name="report", content=f"unqualified-{index}"
            )
        ).id
        > output.id
    )
    assert committer.commit(
        _candidate(
            genesis,
            owner_key.actor_id,
            owner_key.key_id,
            owner,
            asset_proposal_effect(outsider),
            "asset:unqualified-duplicate",
        ),
        genesis,
    ).accepted
    store.close()

    exported_path = tmp_path / "qualified.txt"
    exported = _invoke(["result", contract.id, "--to", str(exported_path), "--json"])
    assert exported.exit_code == 0, exported.output
    record = json.loads(exported.output)["data"]["outputs"][0]
    assert record["asset_id"] == output.id
    assert exported_path.read_bytes() == output.content.encode("utf-8")
