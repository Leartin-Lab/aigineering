"""One Candidate head proves its complete atomic causal closure."""

from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Asset
from aigineering.runtime import claim_next_package, execute_claimed_package


def _new_records(store, revision):
    return tuple(row for _, row in store.scan_runtime_records(after_revision=revision))


def _assert_closed_head(records):
    (head,) = (row for row in records if row.record_type == "candidate.committed")
    (receipt,) = (row for row in records if row.record_type == "candidate.received")
    members = tuple(row for row in records if row.id not in {head.id, receipt.id})
    assert head.payload.get("closure_version") == 1
    assert tuple(head.payload["committed_record_ids"]) == tuple(
        row.id for row in members
    )
    assert head.causal_parents == (receipt.id, *(row.id for row in members))
    assert all(head.id not in row.causal_parents for row in members)
    return receipt, head


def test_asset_completion_uses_actual_commit_witness_and_complete_head(tmp_path):
    store = SQLiteStore(tmp_path / "asset-closure.sqlite")
    try:
        setup = candidate_runtime(store)
        task = setup.accept_contract(
            build_control_plane_contract(
                name="required-assets",
                outputs=("report",),
                budget=3,
            )
        )
        before = store.get_runtime_revision()
        asset = setup.accept_asset(
            Asset(
                id="fixture:report",
                name="report",
                content="Exact report.",
                created_by=setup.actor_key.actor_id,
            )
        )
        records = _new_records(store, before)
        (committed,) = (row for row in records if row.record_type == "asset.committed")
        (complete,) = (
            row for row in records if row.record_type == "lifecycle.terminal"
        )
        assert complete.payload["contract_id"] == task.id
        assert complete.payload["terminal"] == "complete"
        assert committed.payload["asset"]["id"] == asset.id
        assert committed.id in complete.causal_parents
        all_ids = {row.id for _, row in store.scan_runtime_records()}
        assert set(complete.causal_parents) <= all_ids
        _assert_closed_head(records)
    finally:
        store.close()


def test_worker_output_claim_and_attempt_belong_to_the_same_head(monkeypatch, tmp_path):
    import aigineering.runtime_services as runtime_services

    monkeypatch.setattr(runtime_services, "sync_runtime", lambda *args, **kwargs: None)
    store = SQLiteStore(tmp_path / "worker-closure.sqlite")
    try:
        setup = candidate_runtime(store)
        task = setup.accept_contract(
            build_control_plane_contract(
                name="worker-output",
                outputs=("result",),
                budget=2,
            )
        )
        worker = MockWorker(worker_id="worker:causal-closure")
        worker.set_output(task.name, '/exec {"outputs":{"result":"Exact result."}}')
        host = hosted_worker(
            store,
            worker,
            genesis=setup.genesis,
            authority_key=setup.actor_key,
            authority_signer=setup.signer,
        )
        claimed = claim_next_package(
            store, worker_id=host.worker_id, contract_id=task.id
        )
        assert claimed is not None
        before = store.get_runtime_revision()
        result = execute_claimed_package(claimed, host, store)
        assert result["status"] == "accepted", result
        records = _new_records(store, before)
        receipt, _head = _assert_closed_head(records)
        (submitted,) = (row for row in records if row.record_type == "claim.submitted")
        (attempt,) = (row for row in records if row.record_type == "attempt.closed")
        assert receipt.id in submitted.causal_parents
        assert attempt.causal_parents == (receipt.id,)
        assert store.get_claim(task.id)["status"] == "submitted"
    finally:
        store.close()


def test_compound_replay_returns_original_closure_without_running_projectors(
    monkeypatch, tmp_path
):
    from aigineering.core.commitment import CandidateCommitter
    from aigineering.core.store import MemoryStore
    from aigineering.protocol.candidate import create_candidate_proposal
    from aigineering.protocol.effect_builders import (
        asset_proposal_effect,
        contract_declaration_effect,
    )
    import aigineering.core.commitment as commitment

    for store in (MemoryStore(), SQLiteStore(tmp_path / "replay-closure.sqlite")):
        try:
            setup = candidate_runtime(store)
            task = build_control_plane_contract(
                name="compound", outputs=("compound_result",), budget=2
            )
            candidate = create_candidate_proposal(
                domain_id=setup.genesis.id,
                actor_id=setup.actor_key.actor_id,
                key_id=setup.actor_key.key_id,
                signer=setup.signer,
                effects=(
                    contract_declaration_effect(task),
                    asset_proposal_effect(
                        Asset(
                            id="fixture:compound",
                            name="compound_result",
                            content="Exact compound result.",
                        )
                    ),
                ),
                idempotency_key="compound-closure",
            )
            original = CandidateCommitter(store, store).commit(candidate, setup.genesis)
            assert original.accepted, original
            before = store.get_runtime_revision()
            with monkeypatch.context() as patch:
                patch.setattr(
                    commitment,
                    "_reduce_received_candidate",
                    lambda *args, **kwargs: (_ for _ in ()).throw(
                        AssertionError("replay ran projector")
                    ),
                )
                replay = CandidateCommitter(store, store).commit(
                    candidate, setup.genesis
                )
            assert replay.accepted
            assert store.get_runtime_revision() == before
            assert tuple(row.id for row in replay.runtime_records) == tuple(
                row.id for row in original.runtime_records
            )
            assert replay.assets == original.assets
            assert replay.contracts == original.contracts
        finally:
            if hasattr(store, "close"):
                store.close()


def test_shared_store_candidates_serialize_one_decision_each(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from aigineering.core.commitment import CandidateCommitter
    from aigineering.protocol.candidate import create_candidate_proposal
    from aigineering.protocol.effect_builders import asset_proposal_effect

    store = SQLiteStore(tmp_path / "shared-connection.sqlite")
    try:
        setup = candidate_runtime(store)
        task = setup.accept_contract(
            build_control_plane_contract(
                name="shared-connection",
                outputs=("left_result", "right_result"),
                budget=3,
            )
        )
        candidates = tuple(
            create_candidate_proposal(
                domain_id=setup.genesis.id,
                actor_id=setup.actor_key.actor_id,
                key_id=setup.actor_key.key_id,
                signer=setup.signer,
                effects=(
                    asset_proposal_effect(
                        Asset(id=f"fixture:{name}", name=name, content=name)
                    ),
                ),
                idempotency_key=f"shared-{name}",
            )
            for name in task.outputs
        )
        ready = Barrier(2)

        def commit(candidate):
            ready.wait(timeout=5)
            return CandidateCommitter(store, store).commit(candidate, setup.genesis)

        with ThreadPoolExecutor(max_workers=2) as pool:
            decisions = tuple(pool.map(commit, candidates))
        assert all(decision.accepted for decision in decisions)
        terminals = tuple(
            row
            for _, row in store.scan_runtime_records(record_type="lifecycle.terminal")
        )
        assert len(terminals) == 1
        assert terminals[0].payload == {"contract_id": task.id, "terminal": "complete"}
        assert all(
            next(
                row
                for row in decision.runtime_records
                if row.record_type == "candidate.committed"
            ).payload["closure_version"]
            == 1
            for decision in decisions
        )
        assert not store._conn.in_transaction
    finally:
        store.close()


def test_observed_failure_cannot_join_another_threads_candidate_transaction(
    monkeypatch, tmp_path
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from aigineering.core.commitment import CandidateCommitter
    from aigineering.protocol.candidate import create_candidate_proposal
    from aigineering.protocol.effect_builders import asset_proposal_effect
    from aigineering.runtime import _record_worker_invocation_failure
    import aigineering.core.commitment as commitment

    store = SQLiteStore(tmp_path / "shared-failure.sqlite")
    try:
        setup = candidate_runtime(store)
        setup.accept_contract(
            build_control_plane_contract(
                name="independent-output",
                outputs=("independent_result",),
                budget=2,
            )
        )
        failure_task = setup.accept_contract(
            build_control_plane_contract(
                name="independent-failure",
                outputs=("never_produced",),
                budget=1,
            )
        )
        host = hosted_worker(
            store,
            MockWorker(worker_id="worker:independent-failure"),
            genesis=setup.genesis,
            authority_key=setup.actor_key,
            authority_signer=setup.signer,
        )
        claimed = claim_next_package(
            store, worker_id=host.worker_id, contract_id=failure_task.id
        )
        assert claimed is not None
        candidate = create_candidate_proposal(
            domain_id=setup.genesis.id,
            actor_id=setup.actor_key.actor_id,
            key_id=setup.actor_key.key_id,
            signer=setup.signer,
            effects=(
                asset_proposal_effect(
                    Asset(
                        id="fixture:independent",
                        name="independent_result",
                        content="Exact result.",
                    )
                ),
            ),
            idempotency_key="independent-output",
        )
        entered, release, observing, observed = (Event() for _ in range(4))
        original = commitment._reduce_received_candidate

        def pause(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(commitment, "_reduce_received_candidate", pause)

        def record_failure():
            observing.set()
            _record_worker_invocation_failure(
                claimed,
                store,
                status_code=503,
                retryable=False,
                category="fixture_failure",
            )
            observed.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            output = pool.submit(
                CandidateCommitter(store, store).commit, candidate, setup.genesis
            )
            assert entered.wait(5)
            failure = pool.submit(record_failure)
            assert observing.wait(5)
            try:
                assert not observed.wait(0.05), (
                    "failure writer joined the Candidate transaction"
                )
            finally:
                release.set()
            decision = output.result(timeout=5)
            failure.result(timeout=5)
        assert decision.accepted
        assert observed.is_set()
        head = next(
            row
            for row in decision.runtime_records
            if row.record_type == "candidate.committed"
        )
        failures = tuple(
            row
            for _, row in store.scan_runtime_records(
                record_type="worker.invocation_failed"
            )
        )
        assert len(failures) == 1
        assert failures[0].payload["contract_id"] == failure_task.id
        assert failures[0].id not in head.payload["committed_record_ids"]
        assert store.get_claim(failure_task.id)["status"] == "released"
        assert not store._conn.in_transaction
    finally:
        store.close()
