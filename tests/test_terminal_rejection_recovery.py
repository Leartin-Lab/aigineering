"""Late signed submissions preserve an explicit cancellation and its history."""

import pytest

from conftest import candidate_runtime, hosted_worker
from aigineering.agent.mock import MockWorker
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.signing import Ed25519Signer
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.effect_builders import contract_cancellation_effect
from aigineering.runtime import (
    claim_next_package,
    execute_claimed_package,
    process_rejected_submissions,
)


def test_late_signed_submit_is_audited_without_recovery_or_terminal_rewrite(
    tmp_path, monkeypatch
):
    import aigineering.runtime_services as services

    monkeypatch.setattr(services, "sync_runtime", lambda *args, **kwargs: None)
    path = tmp_path / "late.sqlite"
    with SQLiteStore(path) as store:
        setup = candidate_runtime(store)
        task = setup.accept_contract(
            build_control_plane_contract(
                name="cancelled source", outputs=("report",), budget=2
            )
        )
        worker = MockWorker(worker_id="worker:late")
        worker.set_output(task.name, '/exec {"outputs":{"report":"stale"}}')
        host = hosted_worker(
            store,
            worker,
            genesis=setup.genesis,
            authority_key=setup.actor_key,
            authority_signer=setup.signer,
        )
        claim = claim_next_package(store, worker_id=host.worker_id, contract_id=task.id)
        assert claim is not None
        signer = Ed25519Signer()
        key = ActorKey(
            "operator:cancel",
            "cancel-key",
            signer.kind,
            signer.signer_id,
            ("contract.cancel",),
        )
        setup.authorize_actor(key)
        decision = CandidatePublisher(store, store, setup.genesis, key, signer).publish(
            (contract_cancellation_effect(task.id, "explicit operator cancellation"),),
            idempotency_key="cancel",
        )
        assert decision.accepted
        terminal = store.read_task_facts(task.id, ("lifecycle.terminal",))
        with pytest.raises(CandidateCommitRejected):
            execute_claimed_package(claim, host, store)
        assert store.scan_runtime_records(record_type="candidate.rejected")
        assert store.read_task_facts(task.id, ("lifecycle.terminal",)) == terminal
        before = store.get_runtime_revision()
        assert process_rejected_submissions(store) == []
        assert process_rejected_submissions(store) == []
        assert store.get_runtime_revision() == before
        digest = store.runtime_materialization_digest()
        assert store.rebuild_runtime_materializations(strict=True) == digest
    with SQLiteStore(path) as store:
        assert store.read_task_facts(task.id, ("lifecycle.terminal",)) == terminal
