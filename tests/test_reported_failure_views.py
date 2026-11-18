"""Read-only views for linked, published method failure assertions."""

from __future__ import annotations

import json

from conftest import candidate_runtime, hosted_worker
import pytest

from aigineering.agent.mock import MockWorker
from aigineering.core.candidate_publisher import CandidatePublisherRegistry
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.acceptance import validate_asset_publication
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.publication_history import (
    index_published_records,
    validate_published_member,
)
from aigineering.plugins.task_semantics import method_contract
from aigineering.protocol.actions import WorkerAction
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.identity import contract_from_fields
from aigineering.protocol.types import Asset, Contract
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.task_views import project_task_status


def test_published_fail_report_is_visible_without_closing_source(tmp_path, monkeypatch):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    store_path = tmp_path / "reported-failure.sqlite3"
    store = SQLiteStore(store_path)
    setup = candidate_runtime(store)
    parent = setup.accept_contract(
        Contract(id="", name="root", outputs=("report",), budget=4)
    )
    setup.accept_asset(Asset(id="asset:source", name="source", content="input"))
    worker = MockWorker(worker_id="worker:fail-view")
    worker.set_output(parent.name, '/fail {"reason":"missing source data"}')
    host = hosted_worker(
        store,
        worker,
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    publishers = CandidatePublisherRegistry(())
    claim = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=parent.id,
        candidate_publishers=publishers,
        claim_candidate=host.claim_candidate(
            request_id="claim:root", contract_id=parent.id
        ),
    )
    assert claim is not None
    assert (
        execute_claimed_package(claim, host, store, candidate_publishers=publishers)[
            "status"
        ]
        == "task_delegated"
    )
    method = next(
        item
        for item in store.get_all_contracts()
        if item.parent_id == parent.id and item.name.endswith(".fail")
    )
    worker.set_output(
        method.name,
        "/exec "
        + json.dumps(
            {
                "outputs": {
                    method.outputs[0]: json.dumps({"reason": "missing source data"})
                }
            }
        ),
    )
    method_claim = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=method.id,
        candidate_publishers=publishers,
        claim_candidate=host.claim_candidate(
            request_id="claim:method", contract_id=method.id
        ),
    )
    assert method_claim is not None
    assert (
        execute_claimed_package(
            method_claim, host, store, candidate_publishers=publishers
        )["status"]
        == "accepted"
    )

    store.close()
    store = SQLiteStore(store_path)
    parent = store.get_contract(parent.id)
    method = store.get_contract(method.id)
    before = store.get_runtime_revision()
    projection = project_task_status(parent, store)
    details = [
        item
        for item in projection["failure_details"]
        if item.get("kind") == "reported_method_failure"
    ]
    assert len(details) == 1
    detail = details[0]
    assert detail["source_contract_id"] == parent.id
    assert detail["method_contract_id"] == method.id
    assert detail["method_declaration_record_id"]
    assert detail["method_receipt_id"]
    assert detail["method_head_id"]
    assert detail["report_asset_ids"] == [
        asset.id for asset in store.get_assets_by_name(method.outputs[0])
    ]
    assert detail["report_receipt_id"]
    assert detail["report_head_id"]
    assert detail["report_actor_id"] == worker.worker_id
    assert "untrusted" in detail["assertion_label"]
    assert detail["reason"] == "missing source data"
    assert projection["terminal"] is False
    assert projection["outputs_satisfied"] is False
    assert store.get_runtime_revision() == before
    from aigineering.cli.task_board import _cards
    from aigineering.task_view_query import load_task_view_query_context

    query = load_task_view_query_context(store)
    card = next(
        item
        for item in _cards(store, store.get_all_contracts(), query)
        if item["contract_id"] == parent.id
    )
    assert card["failure_details"] == projection["failure_details"]
    assert (
        card["reported_failure_candidates_truncated"]
        == projection["reported_failure_candidates_truncated"]
    )

    setup = candidate_runtime(
        store, genesis=setup.genesis, actor_key=setup.actor_key, signer=setup.signer
    )
    setup.accept_asset(
        Asset(
            id="asset:other-producer-report",
            name="report",
            content="matching output from another source",
            created_by="task:independent-producer",
            origin="worker",
        )
    )
    cleared = project_task_status(parent, store)
    assert not any(
        item.get("kind") == "reported_method_failure"
        for item in cleared["failure_details"]
    )
    assert cleared["outputs_satisfied"] is True
    store.close()


def test_composite_fail_declaration_batch_is_observable(tmp_path):
    from aigineering.core.candidate_publisher import publish_effects

    store = SQLiteStore(tmp_path / "composite-failure.sqlite3")
    setup = candidate_runtime(store)
    source = setup.accept_contract(
        Contract(id="", name="composite-source", outputs=("answer",), budget=2)
    )
    method = method_contract(
        source, WorkerAction(type="fail", payload={"reason": "remote source is absent"})
    )
    report = Asset(
        id="asset:composite-failure-report",
        name=method.outputs[0],
        content='{"reason":"remote source is absent"}',
        created_by=method.id,
        origin="worker",
    )
    published = publish_effects(
        store,
        store,
        setup.genesis,
        setup.actor_key,
        setup.signer,
        (
            contract_declaration_effect(method),
            asset_proposal_effect(report),
        ),
        idempotency_key="composite-fail-method-and-report",
    )
    assert published.accepted, published

    projection = project_task_status(source, store)
    details = [
        item
        for item in projection["failure_details"]
        if item.get("kind") == "reported_method_failure"
    ]
    assert len(details) == 1
    assert details[0]["method_contract_id"] == method.id
    assert details[0]["report_asset_ids"] == [published.assets[0].id]
    assert details[0]["reason"] == "remote source is absent"
    assert details[0]["method_actor_id"] == setup.actor_key.actor_id

    from aigineering.core.candidate_publisher import CandidatePublisher
    from aigineering.core.signing import Ed25519Signer
    from aigineering.protocol.candidate import ActorKey
    from aigineering.protocol.effect_builders import contract_cancellation_effect

    cancel_signer = Ed25519Signer()
    cancel_key = ActorKey(
        "operator:reported-failure-test",
        "key:reported-failure-test",
        cancel_signer.kind,
        cancel_signer.signer_id,
        ("contract.cancel",),
    )
    setup.authorize_actor(cancel_key)
    cancelled = CandidatePublisher(
        store, store, setup.genesis, cancel_key, cancel_signer
    ).publish(
        (contract_cancellation_effect(source.id, "terminal-state fixture"),),
        idempotency_key="cancel-source-after-failure-report",
    )
    assert cancelled.accepted, cancelled
    terminal_projection = project_task_status(source, store)
    assert not any(
        item.get("kind") == "reported_method_failure"
        for item in terminal_projection["failure_details"]
    )
    store.close()


@pytest.mark.parametrize(
    ("report_kind", "expected_details"),
    [("missing", 0), ("mismatch", 0), ("sealed", 0), ("other_producer", 1)],
)
def test_report_requires_matching_disclosable_published_output(
    tmp_path, report_kind, expected_details
):
    store = SQLiteStore(tmp_path / f"reported-failure-{report_kind}.sqlite3")
    setup = candidate_runtime(store)
    source = setup.accept_contract(
        Contract(id="", name="report-source", outputs=("answer",), budget=2)
    )
    method = method_contract(
        source, WorkerAction(type="fail", payload={"reason": "source is unavailable"})
    )
    committed_method = setup.accept_contract(method)
    declarations = tuple(
        record
        for record in store.scan_runtime_records()
        if record[1].record_type == "contract.declared"
        and record[1].payload.get("contract", {}).get("id") == method.id
    )
    assert len(declarations) == 1
    records = tuple(record for _, record in store.scan_runtime_records())
    receipt, head = validate_published_member(
        records,
        declarations[0][1],
        record_index=index_published_records(records),
    )
    assert receipt.payload["effect_types"] == ("contract.declare",)
    assert head.id

    if report_kind != "missing":
        asset = Asset(
            id=f"asset:{report_kind}-failure-report",
            name=(
                "different-output-name"
                if report_kind == "mismatch"
                else method.outputs[0]
            ),
            content='{"reason":"source is unavailable"}',
            created_by=(
                "task:other-reporter" if report_kind == "other_producer" else method.id
            ),
            origin="worker",
            promptable=report_kind != "sealed",
        )
        admitted = setup.accept_asset(asset)
        validate_asset_publication(load_effect_projection_context(store), admitted)

    projection = project_task_status(source, store)
    details = [
        item
        for item in projection["failure_details"]
        if item.get("kind") == "reported_method_failure"
    ]
    assert len(details) == expected_details
    if expected_details:
        assert details[0]["method_contract_id"] == committed_method.id
        assert details[0]["report_actor_id"] == setup.actor_key.actor_id
    store.close()


def test_same_name_report_asset_must_match_frozen_content_requirement(tmp_path):
    store = SQLiteStore(tmp_path / "reported-failure-content-mismatch.sqlite3")
    setup = candidate_runtime(store)
    source = setup.accept_contract(
        Contract(id="", name="content-source", outputs=("answer",), budget=2)
    )
    base_method = method_contract(
        source,
        WorkerAction(type="fail", payload={"reason": "source is unavailable"}),
    )
    output_name = base_method.outputs[0]
    method = contract_from_fields(
        name=base_method.name,
        description=base_method.description,
        inputs=base_method.inputs,
        outputs=base_method.outputs,
        activation=base_method.activation,
        budget=base_method.budget,
        tool_scope=base_method.tool_scope,
        labels=base_method.labels,
        origin=base_method.origin,
        parent_id=base_method.parent_id,
        worker_capabilities=base_method.worker_capabilities,
        worker_pools=base_method.worker_pools,
        minting_authority=base_method.minting_authority,
        sensitive_input_policy=base_method.sensitive_input_policy,
        acceptance_policy={
            "mode": "mechanical",
            "output_requirements": {
                output_name: {"content_id": "content:v1:" + "0" * 64}
            },
        },
    )
    method = setup.accept_contract(method)
    report = setup.accept_asset(
        Asset(
            id="asset:same-name-wrong-content",
            name=output_name,
            content="validly published but wrong frozen content",
            created_by="task:unclaimed-authorized-publisher",
            origin="worker",
        )
    )
    context = load_effect_projection_context(store)
    declaration = next(
        row
        for row in context.runtime_records
        if row.record_type == "contract.declared"
        and row.payload.get("contract", {}).get("id") == method.id
    )
    validate_published_member(
        context.runtime_records,
        declaration,
        record_index=index_published_records(context.runtime_records),
    )
    validate_asset_publication(context, report)

    projection = project_task_status(source, store)
    assert not any(
        item.get("kind") == "reported_method_failure"
        for item in projection["failure_details"]
    )
    store.close()


def test_wrong_parent_binding_and_unlinked_tree_are_ignored(tmp_path):
    store = SQLiteStore(tmp_path / "reported-failure-parent-binding.sqlite3")
    setup = candidate_runtime(store)
    source = setup.accept_contract(
        Contract(id="", name="bound-source", outputs=("answer",), budget=2)
    )
    method = method_contract(
        source, WorkerAction(type="fail", payload={"reason": "misbound assertion"})
    )
    method_payload = json.loads(method.description)
    method_payload["parent_contract_id"] = "task:wrong-parent"
    misbound = contract_from_fields(
        name=method.name,
        description=json.dumps(method_payload, sort_keys=True),
        inputs=method.inputs,
        outputs=method.outputs,
        activation=method.activation,
        budget=method.budget,
        tool_scope=method.tool_scope,
        labels=method.labels,
        origin=method.origin,
        parent_id=method.parent_id,
        worker_capabilities=method.worker_capabilities,
        worker_pools=method.worker_pools,
        delegation_capabilities=method.delegation_capabilities,
        delegation_pools=method.delegation_pools,
        context_asset_ids=method.context_asset_ids,
        minting_authority=method.minting_authority,
        sensitive_input_policy=method.sensitive_input_policy,
        acceptance_policy=method.acceptance_policy,
    )
    setup.accept_contract(misbound)
    setup.accept_asset(
        Asset(
            id="asset:misbound-report",
            name=misbound.outputs[0],
            content="misbound report",
            created_by=misbound.id,
            origin="worker",
        )
    )

    other_source = setup.accept_contract(
        Contract(id="", name="other-source", outputs=("answer",), budget=2)
    )
    unlinked = method_contract(
        other_source, WorkerAction(type="fail", payload={"reason": "other tree"})
    )
    setup.accept_contract(unlinked)
    setup.accept_asset(
        Asset(
            id="asset:unlinked-report",
            name=unlinked.outputs[0],
            content="other tree report",
            created_by=unlinked.id,
            origin="worker",
        )
    )

    projection = project_task_status(source, store)
    assert not any(
        item.get("kind") == "reported_method_failure"
        for item in projection["failure_details"]
    )
    store.close()


def test_missing_report_publication_member_fails_closed(tmp_path):
    from dataclasses import replace

    store = SQLiteStore(tmp_path / "reported-failure-damaged-report-proof.sqlite3")
    setup = candidate_runtime(store)
    source = setup.accept_contract(
        Contract(id="", name="damaged-proof-source", outputs=("answer",), budget=2)
    )
    method = method_contract(
        source,
        WorkerAction(type="fail", payload={"reason": "source evidence absent"}),
    )
    method = setup.accept_contract(method)
    report = setup.accept_asset(
        Asset(
            id="asset:valid-before-snapshot-damage",
            name=method.outputs[0],
            content="report body",
            created_by=method.id,
            origin="worker",
        )
    )

    valid_context = load_effect_projection_context(store)
    declaration = next(
        row
        for row in valid_context.runtime_records
        if row.record_type == "contract.declared"
        and row.payload.get("contract", {}).get("id") == method.id
    )
    _receipt, declaration_head = validate_published_member(
        valid_context.runtime_records,
        declaration,
        record_index=index_published_records(valid_context.runtime_records),
    )
    validate_asset_publication(valid_context, report)
    report_member = next(
        row
        for row in valid_context.runtime_records
        if row.record_type == "asset.committed"
        and row.payload.get("asset", {}).get("id") == report.id
    )
    for missing_member_id in (declaration_head.id, report_member.id):
        damaged_context = replace(
            valid_context,
            runtime_records=tuple(
                row
                for row in valid_context.runtime_records
                if row.id != missing_member_id
            ),
        )
        projection = project_task_status(
            source, store, projection_context=damaged_context
        )
        assert not any(
            item.get("kind") == "reported_method_failure"
            for item in projection["failure_details"]
        )
    store.close()


def test_reported_failure_projection_caps_candidates_and_asset_proofs(
    tmp_path, monkeypatch
):
    from aigineering.core.candidate_publisher import publish_effects
    import aigineering.task_views as task_views

    store = SQLiteStore(tmp_path / "reported-failure-cap.sqlite3")
    setup = candidate_runtime(store)
    source = setup.accept_contract(
        Contract(id="", name="many-failures", outputs=("answer",), budget=30)
    )
    for number in range(9):
        method = method_contract(
            source,
            WorkerAction(type="fail", payload={"reason": f"failure {number}"}),
        )
        report = Asset(
            id=f"asset:failure-report-{number}",
            name=method.outputs[0],
            content=json.dumps({"reason": f"failure {number}"}),
            created_by=method.id,
            origin="worker",
        )
        decision = publish_effects(
            store,
            store,
            setup.genesis,
            setup.actor_key,
            setup.signer,
            (contract_declaration_effect(method), asset_proposal_effect(report)),
            idempotency_key=f"failure-report-{number}",
        )
        assert decision.accepted, decision

    proof_calls = 0
    validate = task_views.validate_asset_publication

    def count_proofs(*args, **kwargs):
        nonlocal proof_calls
        proof_calls += 1
        return validate(*args, **kwargs)

    monkeypatch.setattr(task_views, "validate_asset_publication", count_proofs)
    projection = project_task_status(source, store)
    details = [
        item
        for item in projection["failure_details"]
        if item.get("kind") == "reported_method_failure"
    ]
    assert len(details) == 8
    assert proof_calls <= 8
    assert projection["reported_failure_candidates_truncated"] is True
    store.close()
