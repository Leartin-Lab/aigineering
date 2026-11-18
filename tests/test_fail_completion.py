"""A failure-method result is ordinary work, not parent completion authority."""

from __future__ import annotations

import json

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.core.candidate_publisher import CandidatePublisherRegistry
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins.task_semantics import method_payload
from aigineering.protocol.types import Asset, Contract
from aigineering.runtime import claim_next_package, execute_claimed_package


def test_signed_fail_result_does_not_terminally_close_parent(tmp_path, monkeypatch):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    store = SQLiteStore(tmp_path / "failure-is-ordinary.sqlite3")
    setup = candidate_runtime(store)
    parent = setup.accept_contract(
        Contract(
            id="",
            name="root",
            outputs=("report",),
            budget=4,
            tool_scope=("lookup",),
        )
    )
    setup.accept_asset(Asset(id="asset:input", name="source", content="input"))
    worker = MockWorker(worker_id="worker:fail-ordinary")
    worker.set_output(parent.name, '/fail {"reason":"source unavailable"}')
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
            request_id="claim:fail-root", contract_id=parent.id
        ),
    )
    assert claim is not None
    delegated = execute_claimed_package(
        claim, host, store, candidate_publishers=publishers
    )
    assert delegated["status"] == "task_delegated", delegated

    failure_method = next(
        contract
        for contract in store.get_all_contracts()
        if contract.parent_id == parent.id
        and method_payload(contract).get("method") == "fail"
    )
    worker.set_output(
        failure_method.name,
        "/exec "
        + json.dumps(
            {
                "outputs": {
                    failure_method.outputs[0]: json.dumps(
                        {"reason": "source unavailable"}, sort_keys=True
                    )
                }
            }
        ),
    )
    method_claim = claim_next_package(
        store,
        worker_id=host.worker_id,
        contract_id=failure_method.id,
        candidate_publishers=publishers,
        claim_candidate=host.claim_candidate(
            request_id="claim:fail-method", contract_id=failure_method.id
        ),
    )
    assert method_claim is not None
    result = execute_claimed_package(
        method_claim, host, store, candidate_publishers=publishers
    )

    assert result["status"] == "accepted", result
    root_view = RuntimeProjection(store, store).contract_view(parent)
    assert root_view.terminal is None
    assert store.get_assets_by_name(failure_method.outputs[0])
    assert not store.get_assets_by_name("report")
    store.close()
