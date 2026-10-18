"""Signed tool continuations preserve ordinary independent-review funding."""

from __future__ import annotations

import json
from click import Group
from click.testing import CliRunner
import pytest

from test_cli_task_review import (
    _install_cli_store,
    _run_worker,
    _workspace,
)

from aigineering.agent.tool_worker import ToolWorker
from aigineering.application import default_completion_registry
from aigineering.cli.task_board import register_task_board_commands
from aigineering.cli.task_policy_review import register_task_policy_review_commands
from aigineering.core.candidate_publisher import (
    CandidatePublisher,
    CandidatePublisherRegistry,
    publish_effect,
)
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.domain import load_genesis
from aigineering.core.signing import Ed25519Signer
from aigineering.core.ids import acceptance_policy_id
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace_manager import TraceManager
from aigineering.core.tools import ToolRegistry
from aigineering.local_identity import (
    ensure_local_runtime_publishers,
    ensure_local_worker_host,
    load_actor_signer,
)
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.effect_builders import (
    actor_authorization_effect,
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Candidate, ToolSpec
from aigineering.runtime import (
    claim_next_package,
    execute_claimed_package,
    process_task_completions,
)
from aigineering.task_views import project_task_status


def _app() -> Group:
    root = Group()
    task = Group("task")
    register_task_board_commands(task)
    register_task_policy_review_commands(task)
    root.add_command(task)
    return root


class _ActionWorker:
    def __init__(self, worker_id: str, action: str):
        self.worker_id = worker_id
        self.action = action

    def invoke(self, contract, disclosed_assets):
        return Candidate(worker_id=self.worker_id, raw_output=self.action)


@pytest.mark.parametrize(
    ("action_kind", "root_budget", "tool_names"),
    (("tool", 3, ("lookup",)), ("parallel_tool", 7, ("lookup", "lookup2"))),
)
def test_signed_tool_continuation_can_request_and_complete_exact_root_review(
    tmp_path, monkeypatch, action_kind, root_budget, tool_names
):
    database, root_key_path, _unused, policy = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    genesis = load_genesis(store)
    root_key = genesis.root_keys[0]
    root_signer = load_actor_signer(root_key_path)
    context_ids = tuple(
        sorted((*policy["rubric_asset_ids"], *policy["evidence_asset_ids"]))
    )
    root = build_control_plane_contract(
        name=f"review-funded-{action_kind}-root",
        description="Use read-only tools and produce one report for review.",
        inputs=(),
        outputs=("research_summary",),
        budget=root_budget,
        tool_scope=tool_names,
        labels=("review_evidence", "review_rubric"),
        context_asset_ids=context_ids,
        acceptance_policy=policy,
    )
    root_publication = publish_effect(
        store,
        store,
        genesis,
        root_key,
        root_signer,
        contract_declaration_effect(root),
        idempotency_key=f"review-funded-{action_kind}-root",
    )
    assert root_publication.accepted
    root = root_publication.contract
    assert root is not None

    output_schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "string"}},
    }
    tools = ToolRegistry()
    for tool_name in tool_names:
        descriptor = create_tool_descriptor(
            tool_name,
            f"Bounded fixture {tool_name}.",
            {"type": "object"},
            output_schema=output_schema,
            source_uri=f"python:{tool_name}",
            version="1",
            max_output_bytes=4096,
        )
        descriptor_publication = publish_effect(
            store,
            store,
            genesis,
            root_key,
            root_signer,
            asset_proposal_effect(descriptor),
            idempotency_key=f"review-funded-tool-descriptor:{tool_name}",
        )
        assert descriptor_publication.accepted
        tools.register(
            ToolSpec(
                name=tool_name,
                input_schema={"type": "object"},
                output_schema=output_schema,
                version="1",
                max_output_bytes=4096,
            ),
            lambda _args: json.dumps({"value": "fixture observation"}),
        )

    action = (
        '/tool {"name":"lookup","args":{}}'
        if action_kind == "tool"
        else '/parallel_tool {"calls":[{"id":"a","name":"lookup","args":{}},{"id":"b","name":"lookup2","args":{}}]}'
    )
    publishers = ensure_local_runtime_publishers(store)
    producer = _ActionWorker(f"worker:review-funded-{action_kind}", action)
    producer_host = ensure_local_worker_host(store, producer)
    root_claim = claim_next_package(
        store, worker_id=producer_host.worker_id, contract_id=root.id
    )
    assert root_claim is not None
    root_result = execute_claimed_package(root_claim, producer_host, store)
    assert root_result["status"] == "task_delegated"

    method_tasks = [
        item
        for item in store.get_all_contracts()
        if item.parent_id == root.id
        and (
            item.origin == "system"
            and any(
                label in item.labels
                for label in ("plugin:tool", "plugin:parallel_tool_item")
            )
        )
    ]
    assert len(method_tasks) == len(tool_names)
    assert all(item.budget == 1 for item in method_tasks)
    tool_host = ensure_local_worker_host(
        store, ToolWorker(tools, worker_id=f"tool_worker:review-funding-{action_kind}")
    )
    for method_task in method_tasks:
        tool_claim = claim_next_package(
            store, worker_id=tool_host.worker_id, contract_id=method_task.id
        )
        assert tool_claim is not None
        tool_result = execute_claimed_package(tool_claim, tool_host, store)
        assert tool_result["status"] == "accepted"

    completion_registry = default_completion_registry()
    completed = process_task_completions(
        store, completion_registry, candidate_publishers=publishers
    )
    assert set(item.id for item in method_tasks).issubset(set(completed))
    continuation = next(
        item
        for item in store.get_all_contracts()
        if item.parent_id == root.id and item.origin == "continuation"
    )
    expected_continuation_budget = 1 if action_kind == "tool" else 2
    assert continuation.budget == expected_continuation_budget
    repeated_completion = process_task_completions(
        store, completion_registry, candidate_publishers=publishers
    )
    assert not repeated_completion
    assert (
        len(
            [
                item
                for item in store.get_all_contracts()
                if item.parent_id == root.id and item.origin == "continuation"
            ]
        )
        == 1
    )
    store.close()

    final_worker = _ActionWorker(
        f"worker:review-funded-{action_kind}",
        '/exec {"outputs":{"research_summary":"report grounded in the committed observation"}}',
    )
    final_result = _run_worker(database, root_key_path, continuation, final_worker)
    assert final_result["status"] == "accepted"

    _install_cli_store(monkeypatch, database)
    runner = CliRunner()
    snapshot = SQLiteStore.snapshot_from_path(database)
    root = snapshot.get_contract(root.id)
    target = snapshot.get_assets_by_name("research_summary")[0]
    assert project_task_status(root, snapshot)["status"] != "completed"
    assert project_task_status(root, snapshot)["outputs_satisfied"] is False
    assert snapshot.get_contract(continuation.id) is not None
    expected_retained = 1 if action_kind == "tool" else 3
    records = tuple(record for _, record in snapshot.scan_runtime_records())
    assert allowance_available(root, records) == expected_retained
    snapshot.close()

    request_args = [
        "task",
        "request-review",
        root.id,
        "--output",
        "research_summary",
        "--asset",
        target.id,
        "--reason",
        "Check the exact report against the committed observation.",
        "--idempotency-key",
        f"review-funded-{action_kind}-request",
        "--json",
    ]
    request_result = runner.invoke(_app(), request_args)
    assert request_result.exit_code == 0, request_result.output
    request = json.loads(request_result.output)["data"]
    assert request["budget"] == 1
    assert request["policy_id"] == acceptance_policy_id(policy)
    after_review_reservation = SQLiteStore.snapshot_from_path(database)
    root_after_review_reservation = after_review_reservation.get_contract(root.id)
    review_records = tuple(
        record for _, record in after_review_reservation.scan_runtime_records()
    )
    assert allowance_available(root_after_review_reservation, review_records) == (
        expected_retained - 1
    )
    after_review_reservation.close()

    replay_result = runner.invoke(_app(), request_args)
    assert replay_result.exit_code == 0, replay_result.output
    assert json.loads(replay_result.output)["data"] == request

    snapshot = SQLiteStore.snapshot_from_path(database)
    reviewer = snapshot.get_contract(request["child_contract_id"])
    assert reviewer is not None
    assert reviewer.parent_id == root.id
    assert reviewer.budget == 1
    snapshot.close()

    review_worker = _ActionWorker(
        f"worker:independent-reviewer-{action_kind}",
        f'/exec {{"outputs":{{"{reviewer.outputs[0]}":"The report is grounded in the fixture observation."}}}}',
    )
    review_result = _run_worker(database, root_key_path, reviewer, review_worker)
    assert review_result["status"] == "accepted"

    snapshot = SQLiteStore.snapshot_from_path(database)
    report = snapshot.get_assets_by_name(reviewer.outputs[0])[0]
    assert report.created_by == reviewer.id
    assert report.signed_by != target.signed_by
    snapshot.close()

    decision = runner.invoke(
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
            "The report supports the exact output against frozen evidence.",
            "--idempotency-key",
            f"review-funded-{action_kind}-decision",
            "--json",
        ],
    )
    assert decision.exit_code == 0, decision.output

    reopened = SQLiteStore.snapshot_from_path(database)
    reopened_root = reopened.get_contract(root.id)
    assert project_task_status(reopened_root, reopened)["status"] == "completed"
    qualified = reopened.scan_runtime_records(record_type="output.qualified")
    assert len(qualified) == 1
    assert qualified[0][1].payload["contract_id"] == root.id
    assert qualified[0][1].payload["asset_id"] == target.id
    assert reopened.get_contract(continuation.id).budget == expected_continuation_budget
    assert len(reopened.scan_runtime_records(record_type="task.review.requested")) == 1
    root_reservations = [
        record
        for _, record in reopened.scan_runtime_records(record_type="allowance.reserved")
        if record.payload.get("source_contract_id") == root.id
    ]
    expected_child_count = 3 if action_kind == "tool" else 4
    assert len(root_reservations) == expected_child_count
    assert sum(int(record.payload["amount"]) for record in root_reservations) == (
        root_budget if action_kind == "tool" else 5
    )
    reopened.close()

    from test_cli_task_board import _attach_cli

    _attach_cli(monkeypatch, database)
    destination = tmp_path / f"{action_kind}-qualified-report.txt"
    exported = runner.invoke(
        _app(),
        [
            "task",
            "result",
            root.id,
            "--output",
            "research_summary",
            "--to",
            str(destination),
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.output
    exported_data = json.loads(exported.output)["data"]
    assert exported_data["outputs"][0]["asset_id"] == target.id
    assert destination.read_text(encoding="utf-8") == (
        "report grounded in the committed observation"
    )


def test_continuation_plugin_retains_only_fundable_default_review_units(
    tmp_path, monkeypatch
):
    from aigineering.plugins.base import PluginRequest
    from aigineering.plugins.continuation import ContinuationTaskPlugin
    from aigineering.plugins.task_semantics import method_contract
    from aigineering.protocol.actions import WorkerAction

    _database, _root_key_path, root, _policy = _workspace(tmp_path, monkeypatch)
    source = method_contract(
        root, WorkerAction(type="tool", payload={"name": "lookup", "args": {}})
    )
    plugin = ContinuationTaskPlugin()
    budgets = [
        plugin.propose(PluginRequest(parent=root, source=source, allowance=allowance))
        .effects[0]
        .payload["contract"]["budget"]
        for allowance in (1, 2, 3)
    ]
    # After the method unit is allocated, one remaining unit cannot be split;
    # with two, keep one for review and one for the continuation.
    assert budgets == [1, 1, 2]


def test_nested_continuation_does_not_repeat_ancestor_review_reserve(
    tmp_path, monkeypatch
):
    from aigineering.plugins.base import PluginRequest
    from aigineering.plugins.continuation import ContinuationTaskPlugin
    from aigineering.plugins.task_semantics import (
        continuation_contract,
        method_contract,
    )
    from aigineering.protocol.actions import WorkerAction

    _database, _root_key_path, root, _policy = _workspace(tmp_path, monkeypatch)
    original_method = method_contract(
        root, WorkerAction(type="tool", payload={"name": "lookup", "args": {}})
    )
    continuation = continuation_contract(root, original_method, method="tool", budget=4)
    nested_method = method_contract(
        continuation,
        WorkerAction(type="tool", payload={"name": "another", "args": {}}),
    )
    proposal = ContinuationTaskPlugin().propose(
        PluginRequest(parent=continuation, source=nested_method, allowance=4)
    )
    assert proposal.effects[0].payload["contract"]["budget"] == 4


def _completed_tool_source(tmp_path, monkeypatch, *, budget=100, outputs=None):
    database, root_key_path, template, policy = _workspace(tmp_path, monkeypatch)
    store = SQLiteStore(database)
    genesis = load_genesis(store)
    root_key = genesis.root_keys[0]
    root_signer = load_actor_signer(root_key_path)
    root = build_control_plane_contract(
        name=f"restart-tool-root-{budget}",
        description="Use a tool and continue the ordinary task.",
        outputs=tuple(outputs) if outputs is not None else template.outputs,
        budget=budget,
        tool_scope=("lookup",),
        labels=template.labels,
        context_asset_ids=template.context_asset_ids,
        acceptance_policy=policy,
    )
    publication = publish_effect(
        store,
        store,
        genesis,
        root_key,
        root_signer,
        contract_declaration_effect(root),
        idempotency_key=f"restart-tool-root-{budget}",
    )
    assert publication.accepted
    root = publication.contract
    assert root is not None

    output_schema = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "string"}},
    }
    descriptor = create_tool_descriptor(
        "lookup",
        "Restart fixture lookup.",
        {"type": "object"},
        output_schema=output_schema,
        source_uri="python:restart-lookup",
        version="1",
        max_output_bytes=4096,
    )
    assert publish_effect(
        store,
        store,
        genesis,
        root_key,
        root_signer,
        asset_proposal_effect(descriptor),
        idempotency_key="restart-tool-descriptor",
    ).accepted
    tools = ToolRegistry()
    tools.register(
        ToolSpec(
            name="lookup",
            input_schema={"type": "object"},
            output_schema=output_schema,
            version="1",
            max_output_bytes=4096,
        ),
        lambda _args: json.dumps({"value": "committed observation"}),
    )
    publishers = ensure_local_runtime_publishers(store)
    producer = _ActionWorker(
        f"worker:restart-root-{budget}", '/tool {"name":"lookup","args":{}}'
    )
    producer_host = ensure_local_worker_host(store, producer)
    root_claim = claim_next_package(
        store, worker_id=producer_host.worker_id, contract_id=root.id
    )
    assert root_claim is not None
    assert (
        execute_claimed_package(
            root_claim, producer_host, store, candidate_publishers=publishers
        )["status"]
        == "task_delegated"
    )
    source = next(
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == root.id
        and contract.origin == "system"
        and contract.labels
        and "plugin:tool" in contract.labels
    )
    tool_host = ensure_local_worker_host(
        store, ToolWorker(tools, worker_id=f"tool_worker:restart-{budget}")
    )
    tool_claim = claim_next_package(
        store, worker_id=tool_host.worker_id, contract_id=source.id
    )
    assert tool_claim is not None
    assert (
        execute_claimed_package(
            tool_claim, tool_host, store, candidate_publishers=publishers
        )["status"]
        == "accepted"
    )
    return database, root_key_path, root, source, store, publishers


def test_restart_after_accepted_continuation_before_marker_reuses_one_child(
    tmp_path, monkeypatch
):
    database, _key_path, root, source, store, publishers = _completed_tool_source(
        tmp_path, monkeypatch, budget=100
    )
    original_record = TraceManager.record
    interrupted = False
    remainder_before_restart = []

    def interrupt_after_publication(trace, contract_id, event_type, **kwargs):
        nonlocal interrupted
        if event_type == "method_continuation_scheduled" and not interrupted:
            children = [
                contract
                for contract in store.get_all_contracts()
                if contract.parent_id == root.id and contract.origin == "continuation"
            ]
            assert len(children) == 1
            assert children[0].budget == 98
            remaining = allowance_available(
                store.get_contract(root.id),
                tuple(record for _, record in store.scan_runtime_records()),
            )
            assert remaining == 1
            remainder_before_restart.append(remaining)
            interrupted = True
            raise RuntimeError("injected post-publication/pre-marker interruption")
        return original_record(trace, contract_id, event_type, **kwargs)

    monkeypatch.setattr(TraceManager, "record", interrupt_after_publication)
    with pytest.raises(RuntimeError, match="post-publication/pre-marker"):
        process_task_completions(
            store, default_completion_registry(), candidate_publishers=publishers
        )
    assert interrupted
    assert not store.scan_runtime_records(record_type="task_completion.projected")
    store.close()

    reopened = SQLiteStore(database)
    reopened_publishers = ensure_local_runtime_publishers(reopened)
    processed = process_task_completions(
        reopened,
        default_completion_registry(),
        candidate_publishers=reopened_publishers,
    )
    assert processed == [source.id]
    children = [
        contract
        for contract in reopened.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "continuation"
    ]
    assert len(children) == 1
    assert children[0].budget == 98
    records = tuple(record for _, record in reopened.scan_runtime_records())
    assert remainder_before_restart == [1]
    assert allowance_available(reopened.get_contract(root.id), records) == 1
    assert not [
        entry
        for entry in reopened.get_by_contract(root.id)
        if entry.event_type == "task_continuation_rejected"
    ]
    assert (
        len(reopened.scan_runtime_records(record_type="task_completion.projected")) == 1
    )
    declaration_rows = [
        record
        for _, record in reopened.scan_runtime_records(record_type="contract.declared")
        if record.payload.get("contract", {}).get("parent_id") == root.id
        and record.payload.get("contract", {}).get("origin") == "continuation"
    ]
    assert len(declaration_rows) == 1
    reopened.close()


def test_restarted_completion_recognizes_accepted_legacy_budget99_child(
    tmp_path, monkeypatch
):
    from aigineering.plugins.task_semantics import continuation_contract

    _database, _key_path, root, source, store, publishers = _completed_tool_source(
        tmp_path, monkeypatch, budget=100
    )
    historical = continuation_contract(root, source, method="tool", budget=99)
    publisher = publishers.get("continuation.publish.v1")
    assert publisher is not None
    accepted = publisher.publish(
        (contract_declaration_effect(historical),),
        idempotency_key=f"continuation:{source.id}:{historical.id}",
        causal_parents=(source.id,),
    )
    assert accepted.accepted

    processed = process_task_completions(
        store, default_completion_registry(), candidate_publishers=publishers
    )
    assert processed == [source.id]
    children = [
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "continuation"
    ]
    assert children == [historical]
    assert children[0].budget == 99
    assert (
        allowance_available(
            store.get_contract(root.id),
            tuple(record for _, record in store.scan_runtime_records()),
        )
        == 0
    )
    declaration_rows = [
        record
        for _, record in store.scan_runtime_records(record_type="contract.declared")
        if record.payload.get("contract", {}).get("parent_id") == root.id
        and record.payload.get("contract", {}).get("origin") == "continuation"
    ]
    assert len(declaration_rows) == 1
    assert len(store.scan_runtime_records(record_type="task_completion.projected")) == 1
    assert not [
        entry
        for entry in store.get_by_contract(root.id)
        if entry.event_type == "task_continuation_rejected"
    ]
    exact_receipts = [
        record
        for _, record in store.scan_runtime_records(record_type="candidate.received")
        if tuple(record.causal_parents) == (source.id,)
        and record.payload.get("effect_types") == ("contract.declare",)
    ]
    assert len(exact_receipts) == 1
    store.close()


def _registry_with_continuation_publisher(publishers, publisher):
    return CandidatePublisherRegistry(
        tuple(
            (plugin_id, publisher if plugin_id == "continuation.publish.v1" else item)
            for plugin_id, item in publishers.publishers
        )
    )


def test_restart_recognizes_prior_child_after_configured_key_rotation(
    tmp_path, monkeypatch
):
    from aigineering.plugins.task_semantics import continuation_contract

    _database, root_key_path, root, source, store, publishers = _completed_tool_source(
        tmp_path, monkeypatch, budget=100
    )
    old_publisher = publishers.get("continuation.publish.v1")
    assert old_publisher is not None
    historical = continuation_contract(root, source, method="tool", budget=99)
    assert old_publisher.publish(
        (contract_declaration_effect(historical),),
        idempotency_key=f"continuation:{source.id}:{historical.id}",
        causal_parents=(source.id,),
    ).accepted

    new_signer = Ed25519Signer()
    old_key = old_publisher.actor_key
    new_key = ActorKey(
        old_key.actor_id,
        f"{old_key.key_id}-rotated",
        new_signer.kind,
        new_signer.signer_id,
        old_key.capabilities,
    )
    genesis = load_genesis(store)
    root_key = genesis.root_keys[0]
    root_authority = CandidatePublisher(
        store, store, genesis, root_key, load_actor_signer(root_key_path)
    )
    assert root_authority.publish(
        (actor_authorization_effect(new_key),),
        idempotency_key=f"test-authorize:{new_key.actor_id}:{new_key.key_id}",
    ).accepted
    rotated = CandidatePublisher(store, store, genesis, new_key, new_signer)
    current_publishers = _registry_with_continuation_publisher(publishers, rotated)

    assert process_task_completions(
        store,
        default_completion_registry(),
        candidate_publishers=current_publishers,
    ) == [source.id]
    children = [
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "continuation"
    ]
    assert children == [historical]
    assert not [
        entry
        for entry in store.get_by_contract(root.id)
        if entry.event_type == "task_continuation_rejected"
    ]
    assert len(store.scan_runtime_records(record_type="task_completion.projected")) == 1


def test_fresh_continuation_requires_current_publisher_authorization(
    tmp_path, monkeypatch
):
    _database, _root_key_path, root, source, store, publishers = _completed_tool_source(
        tmp_path, monkeypatch, budget=100
    )
    configured = publishers.get("continuation.publish.v1")
    assert configured is not None
    signer = Ed25519Signer()
    unauthorized_key = ActorKey(
        configured.actor_key.actor_id,
        f"{configured.actor_key.key_id}-unregistered",
        signer.kind,
        signer.signer_id,
        configured.actor_key.capabilities,
    )
    rogue = CandidatePublisher(
        store, store, configured.genesis, unauthorized_key, signer
    )
    current_publishers = _registry_with_continuation_publisher(publishers, rogue)

    assert process_task_completions(
        store,
        default_completion_registry(),
        candidate_publishers=current_publishers,
    ) == [source.id]
    assert not [
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "continuation"
    ]
    assert [
        entry
        for entry in store.get_by_contract(root.id)
        if entry.event_type == "task_continuation_rejected"
    ]
    assert any(
        record.record_type == "candidate.authentication_rejected"
        for _, record in store.scan_runtime_records()
    )


def test_continuation_history_fails_closed_on_ambiguous_receipt(tmp_path, monkeypatch):
    from dataclasses import replace

    from aigineering.plugins.continuation_history import (
        capture_continuation_history,
        inspect_continuation_history,
    )
    from aigineering.plugins.task_semantics import continuation_contract

    _database, _root_key_path, root, source, store, publishers = _completed_tool_source(
        tmp_path, monkeypatch, budget=100
    )
    publisher = publishers.get("continuation.publish.v1")
    assert publisher is not None
    historical = continuation_contract(root, source, method="tool", budget=99)
    assert publisher.publish(
        (contract_declaration_effect(historical),),
        idempotency_key=f"continuation:{source.id}:{historical.id}",
        causal_parents=(source.id,),
    ).accepted
    snapshot = capture_continuation_history(store, publisher)
    rows = snapshot.context.runtime_records
    receipt = next(
        row
        for row in rows
        if row.record_type == "candidate.received"
        and tuple(row.causal_parents) == (source.id,)
        and row.payload.get("actor_id") == publisher.actor_key.actor_id
        and row.payload.get("candidate_id")
        == next(
            member.payload["candidate_id"]
            for member in rows
            if member.record_type == "contract.declared"
            and member.payload.get("contract", {}).get("id") == historical.id
        )
    )
    duplicate = replace(receipt, id="record:duplicate-continuation-receipt")
    context = replace(snapshot.context, runtime_records=(*rows, duplicate))
    proof = inspect_continuation_history(
        replace(snapshot, context=context),
        parent=root,
        source=source,
        method="tool",
    )
    assert proof.status == "conflict"
    assert proof.reason == "continuation_receipt_missing_or_ambiguous"


def test_two_engines_share_fixed_multioutput_continuation_intent(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    import aigineering.plugins.continuation_history as history

    database, _key_path, root, source, initial_store, _publishers = (
        _completed_tool_source(
            tmp_path,
            monkeypatch,
            budget=100,
            outputs=("research_summary", "second_summary"),
        )
    )
    initial_store.close()
    stores = (SQLiteStore(database), SQLiteStore(database))
    publisher_registries = tuple(
        ensure_local_runtime_publishers(store) for store in stores
    )
    original_capture = history.capture_continuation_history
    capture_barrier = Barrier(2, timeout=10)

    def capture_then_release(store, publisher):
        snapshot = original_capture(store, publisher)
        capture_barrier.wait()
        return snapshot

    monkeypatch.setattr(history, "capture_continuation_history", capture_then_release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(
            pool.submit(
                process_task_completions,
                store,
                default_completion_registry(),
                candidate_publishers=publishers,
            )
            for store, publishers in zip(stores, publisher_registries, strict=True)
        )
        results = tuple(future.result(timeout=20) for future in futures)
    for store in stores:
        store.close()

    snapshot = SQLiteStore.snapshot_from_path(database)
    children = [
        contract
        for contract in snapshot.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "continuation"
    ]
    assert len(children) == 1
    assert children[0].budget == 97
    records = tuple(record for _, record in snapshot.scan_runtime_records())
    assert allowance_available(snapshot.get_contract(root.id), records) == 2
    assert (
        len(snapshot.scan_runtime_records(record_type="task_completion.projected")) == 1
    )
    assert not [
        entry
        for entry in snapshot.get_by_contract(root.id)
        if entry.event_type == "task_continuation_rejected"
    ]
    assert all(source.id in result for result in results)
    snapshot.close()


@pytest.mark.parametrize(
    ("extra_sibling_spends", "continuation_expected"), [(1, True), (2, False)]
)
def test_fixed_continuation_intent_obeys_current_writer_allowance(
    tmp_path, monkeypatch, extra_sibling_spends, continuation_expected
):
    from aigineering.plugins.delegation import TaskDelegationPlugin
    from aigineering.protocol.actions import WorkerAction

    _database, root_key_path, root, source, store, publishers = _completed_tool_source(
        tmp_path, monkeypatch, budget=100
    )
    genesis = load_genesis(store)
    root_authority = CandidatePublisher(
        store,
        store,
        genesis,
        genesis.root_keys[0],
        load_actor_signer(root_key_path),
    )
    plugin = TaskDelegationPlugin()
    for index in range(extra_sibling_spends):
        available = allowance_available(
            store.get_contract(root.id),
            tuple(record for _, record in store.scan_runtime_records()),
        )
        proposal = plugin.propose_claimed(
            root,
            WorkerAction(
                type="tool",
                payload={"name": "lookup", "args": {"legal_sibling": index}},
            ),
            allowance=available,
        )
        decision = root_authority.publish(
            proposal.effects,
            idempotency_key=f"signed-legal-sibling-spend:{index}",
            causal_parents=(root.id,),
        )
        assert decision.accepted

    assert process_task_completions(
        store,
        default_completion_registry(),
        candidate_publishers=publishers,
    ) == [source.id]
    children = [
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "continuation"
    ]
    if continuation_expected:
        assert len(children) == 1
        assert children[0].budget == 98
        assert not [
            entry
            for entry in store.get_by_contract(root.id)
            if entry.event_type == "task_continuation_rejected"
        ]
        assert (
            allowance_available(
                store.get_contract(root.id),
                tuple(record for _, record in store.scan_runtime_records()),
            )
            == 0
        )
    else:
        assert children == []
        assert [
            entry
            for entry in store.get_by_contract(root.id)
            if entry.event_type == "task_continuation_rejected"
        ]
        assert any(
            record.record_type == "candidate.rejected"
            for _, record in store.scan_runtime_records()
        )


@pytest.mark.parametrize(
    ("missing", "expected_reason"),
    [
        ("contract", "continuation_materialized_contract_missing_or_ambiguous"),
        ("receipt", "continuation_receipt_missing_or_ambiguous"),
    ],
)
def test_selected_continuation_history_fails_closed_when_incomplete(
    tmp_path, monkeypatch, missing, expected_reason
):
    from dataclasses import replace

    from aigineering.plugins.continuation_history import (
        capture_continuation_history,
        inspect_continuation_history,
    )
    from aigineering.plugins.task_semantics import continuation_contract

    _database, _root_key_path, root, source, store, publishers = _completed_tool_source(
        tmp_path, monkeypatch, budget=100
    )
    publisher = publishers.get("continuation.publish.v1")
    assert publisher is not None
    historical = continuation_contract(root, source, method="tool", budget=99)
    assert publisher.publish(
        (contract_declaration_effect(historical),),
        idempotency_key=f"continuation:{source.id}:{historical.id}",
        causal_parents=(source.id,),
    ).accepted
    snapshot = capture_continuation_history(store, publisher)
    if missing == "contract":
        context = replace(
            snapshot.context,
            contracts=tuple(
                contract
                for contract in snapshot.context.contracts
                if contract.id != historical.id
            ),
        )
    else:
        member = next(
            row
            for row in snapshot.context.runtime_records
            if row.record_type == "contract.declared"
            and row.payload.get("contract", {}).get("id") == historical.id
        )
        receipt_id = member.causal_parents[0]
        context = replace(
            snapshot.context,
            runtime_records=tuple(
                row for row in snapshot.context.runtime_records if row.id != receipt_id
            ),
        )
    proof = inspect_continuation_history(
        replace(snapshot, context=context),
        parent=root,
        source=source,
        method="tool",
    )
    assert proof.status == "conflict"
    assert proof.reason == expected_reason


def test_prepublication_restart_reuses_source_head_intent(tmp_path, monkeypatch):
    database, _key_path, root, source, store, publishers = _completed_tool_source(
        tmp_path, monkeypatch, budget=100
    )
    original_publish = CandidatePublisher.publish
    interrupted = False

    def interrupt_before_publication(
        publisher, effects, *, idempotency_key, causal_parents=()
    ):
        nonlocal interrupted
        if idempotency_key.startswith("continuation:") and not interrupted:
            interrupted = True
            raise RuntimeError("injected pre-publication interruption")
        return original_publish(
            publisher,
            effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )

    monkeypatch.setattr(CandidatePublisher, "publish", interrupt_before_publication)
    with pytest.raises(RuntimeError, match="pre-publication"):
        process_task_completions(
            store, default_completion_registry(), candidate_publishers=publishers
        )
    assert interrupted
    assert not [
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "continuation"
    ]
    assert [
        entry
        for entry in store.get_by_contract(root.id)
        if entry.event_type == "method_continuation_context_prepared"
    ]
    store.close()

    monkeypatch.setattr(CandidatePublisher, "publish", original_publish)
    reopened = SQLiteStore(database)
    reopened_publishers = ensure_local_runtime_publishers(reopened)
    assert process_task_completions(
        reopened,
        default_completion_registry(),
        candidate_publishers=reopened_publishers,
    ) == [source.id]
    children = [
        contract
        for contract in reopened.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "continuation"
    ]
    assert len(children) == 1
    assert children[0].budget == 98
    assert (
        allowance_available(
            reopened.get_contract(root.id),
            tuple(record for _, record in reopened.scan_runtime_records()),
        )
        == 1
    )
    reopened.close()


@pytest.mark.parametrize(
    ("sibling_spends", "continuation_expected"), [(1, True), (3, False)]
)
def test_two_engines_publish_fixed_multioutput_intent_after_signed_spend(
    tmp_path, monkeypatch, sibling_spends, continuation_expected
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event, Lock

    import aigineering.plugins.continuation_history as history
    from aigineering.protocol.actions import WorkerAction
    from aigineering.plugins.base import PluginRequest
    from aigineering.plugins.continuation import ContinuationTaskPlugin
    from aigineering.plugins.continuation_history import inspect_continuation_history
    from aigineering.plugins.delegation import TaskDelegationPlugin
    from aigineering.protocol.candidate import create_candidate_proposal

    database, root_key_path, root, source, initial_store, _publishers = (
        _completed_tool_source(
            tmp_path,
            monkeypatch,
            budget=100,
            outputs=("research_summary", "second_summary"),
        )
    )
    initial_store.close()
    stores = (SQLiteStore(database), SQLiteStore(database))
    publisher_registries = tuple(
        ensure_local_runtime_publishers(store) for store in stores
    )
    capture_barrier = Barrier(3, timeout=10)
    release_publishers = Event()
    snapshot_proofs = []
    publication_rows = []
    capture_lock = Lock()
    publication_lock = Lock()
    original_capture = history.capture_continuation_history
    original_publish = CandidatePublisher.publish

    def capture_absent_then_pause(store, publisher):
        snapshot = original_capture(store, publisher)
        proof = inspect_continuation_history(
            snapshot, parent=root, source=source, method="tool"
        )
        assert proof.status == "absent"
        with capture_lock:
            snapshot_proofs.append(proof)
        capture_barrier.wait()
        assert release_publishers.wait(timeout=10)
        return snapshot

    def capture_continuation_publish(
        publisher, effects, *, idempotency_key, causal_parents=()
    ):
        if idempotency_key.startswith(f"continuation:{source.id}:"):
            candidate = create_candidate_proposal(
                domain_id=publisher.genesis.id,
                actor_id=publisher.actor_key.actor_id,
                key_id=publisher.actor_key.key_id,
                effects=effects,
                signer=publisher.signer,
                causal_parents=causal_parents,
                idempotency_key=idempotency_key,
            )
            contract_wire = effects[0].payload["contract"]
            decision = original_publish(
                publisher,
                effects,
                idempotency_key=idempotency_key,
                causal_parents=causal_parents,
            )
            with publication_lock:
                publication_rows.append(
                    {
                        "candidate_id": candidate.id,
                        "contract_id": contract_wire["id"],
                        "budget": contract_wire["budget"],
                        "idempotency_key": idempotency_key,
                        "causal_parents": tuple(causal_parents),
                        "accepted": decision.accepted,
                    }
                )
            return decision
        return original_publish(
            publisher,
            effects,
            idempotency_key=idempotency_key,
            causal_parents=causal_parents,
        )

    monkeypatch.setattr(
        history, "capture_continuation_history", capture_absent_then_pause
    )
    monkeypatch.setattr(CandidatePublisher, "publish", capture_continuation_publish)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = tuple(
                pool.submit(
                    process_task_completions,
                    store,
                    default_completion_registry(),
                    candidate_publishers=publishers,
                )
                for store, publishers in zip(stores, publisher_registries, strict=True)
            )
            capture_barrier.wait()
            assert len(snapshot_proofs) == 2
            assert all(proof.status == "absent" for proof in snapshot_proofs)

            spend_store = SQLiteStore(database)
            genesis = load_genesis(spend_store)
            root_authority = CandidatePublisher(
                spend_store,
                spend_store,
                genesis,
                genesis.root_keys[0],
                load_actor_signer(root_key_path),
            )
            delegation = TaskDelegationPlugin()
            for index in range(sibling_spends):
                available = allowance_available(
                    spend_store.get_contract(root.id),
                    tuple(record for _, record in spend_store.scan_runtime_records()),
                )
                proposal = delegation.propose_claimed(
                    root,
                    WorkerAction(
                        type="tool",
                        payload={"name": "lookup", "args": {"race_spend": index}},
                    ),
                    allowance=available,
                )
                decision = root_authority.publish(
                    proposal.effects,
                    idempotency_key=f"signed-concurrent-race-spend:{index}",
                    causal_parents=(root.id,),
                )
                assert decision.accepted

            records = tuple(record for _, record in spend_store.scan_runtime_records())
            available_after_spend = allowance_available(
                spend_store.get_contract(root.id), records
            )
            original_intent = (
                ContinuationTaskPlugin()
                .propose(
                    PluginRequest(
                        parent=root,
                        source=source,
                        allowance=max(
                            1,
                            allowance_available(root, snapshot_proofs[0].source_prefix),
                        ),
                    )
                )
                .effects[0]
                .payload["contract"]
            )
            original_intent_budget = original_intent["budget"]
            original_intent_id = original_intent["id"]
            assert (
                available_after_spend >= original_intent_budget
                if continuation_expected
                else available_after_spend < original_intent_budget
            )
            spend_store.close()
            release_publishers.set()
            results = tuple(future.result(timeout=20) for future in futures)
    finally:
        release_publishers.set()
        for store in stores:
            store.close()

    assert all(source.id in result for result in results)
    assert len(publication_rows) == 2
    assert len({row["candidate_id"] for row in publication_rows}) == 1
    assert len({row["contract_id"] for row in publication_rows}) == 1
    assert {row["contract_id"] for row in publication_rows} == {original_intent_id}
    assert len({row["budget"] for row in publication_rows}) == 1
    assert {row["budget"] for row in publication_rows} == {original_intent_budget}
    assert {row["idempotency_key"] for row in publication_rows} == {
        f"continuation:{source.id}:{publication_rows[0]['contract_id']}"
    }
    assert all(row["causal_parents"] == (source.id,) for row in publication_rows)

    snapshot = SQLiteStore.snapshot_from_path(database)
    children = [
        contract
        for contract in snapshot.get_all_contracts()
        if contract.parent_id == root.id and contract.origin == "continuation"
    ]
    marker_rows = [
        record
        for _, record in snapshot.scan_runtime_records(
            record_type="task_completion.projected"
        )
        if record.payload.get("source_contract_id") == source.id
    ]
    rejection_entries = [
        entry
        for entry in snapshot.get_by_contract(root.id)
        if entry.event_type == "task_continuation_rejected"
    ]
    root_terminals = [
        record
        for _, record in snapshot.scan_runtime_records(record_type="lifecycle.terminal")
        if record.payload.get("contract_id") == root.id
    ]
    continuation_grants = [
        record
        for _, record in snapshot.scan_runtime_records(record_type="allowance.granted")
        if record.payload.get("contract_id") == original_intent_id
    ]
    if continuation_expected:
        assert len(children) == 1
        assert children[0].id == publication_rows[0]["contract_id"]
        assert children[0].budget == original_intent_budget
        assert len(continuation_grants) == 1
        assert continuation_grants[0].payload["amount"] == original_intent_budget
        assert len(marker_rows) == 1
        assert not rejection_entries
        assert not root_terminals
        assert all(row["accepted"] for row in publication_rows)
    else:
        assert children == []
        assert not continuation_grants
        rejected_candidates = [
            record
            for _, record in snapshot.scan_runtime_records(
                record_type="candidate.rejected"
            )
            if record.payload.get("candidate_id") == publication_rows[0]["candidate_id"]
        ]
        assert rejected_candidates
        assert len(marker_rows) == 1
        assert rejection_entries
        assert len(root_terminals) == 1
        assert root_terminals[0].payload["terminal"] == "failed"
        assert not any(row["accepted"] for row in publication_rows)
    snapshot.close()
