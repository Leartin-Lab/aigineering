"""Attempt failure consumes its allocation, never the Asset obligation."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from conftest import candidate_runtime, hosted_worker
from aigineering.agent.mock import MockWorker
from aigineering.core.candidate_decision import trace_record
from aigineering.core.causal_allowance import allowance_available
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.lifecycle_facts import create_terminal_record
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry
from aigineering.protocol.candidate import (
    CandidateClaimBinding,
    CandidateEffect,
    create_candidate_proposal,
)
from aigineering.protocol.runtime_record import create_runtime_record
from aigineering.protocol.types import Asset
from aigineering.runtime import claim_next_package, _record_worker_invocation_failure
from aigineering.task_views import project_task_status


def _claimed(store):
    setup = candidate_runtime(store)
    task = setup.accept_contract(
        build_control_plane_contract(
            name="unmet obligation",
            outputs=("required_result",),
            budget=3,
        )
    )
    host = hosted_worker(
        store,
        MockWorker(worker_id="worker:failure-proof"),
        genesis=setup.genesis,
        authority_key=setup.actor_key,
        authority_signer=setup.signer,
    )
    claimed = claim_next_package(store, worker_id=host.worker_id, contract_id=task.id)
    assert claimed is not None
    return setup, task, host, claimed


@pytest.mark.parametrize("kind", ["provider", "rejected", "expired"])
def test_failure_retains_proof_but_later_external_asset_satisfies(
    tmp_path, monkeypatch, kind
):
    import aigineering.runtime_services as services

    monkeypatch.setattr(services, "sync_runtime", lambda *args, **kwargs: None)
    path = tmp_path / f"failure-{kind}.sqlite"
    with SQLiteStore(path) as store:
        setup, task, host, claimed = _claimed(store)
        if kind == "provider":
            _record_worker_invocation_failure(
                claimed,
                store,
                category="provider_error",
                status_code=503,
                retryable=True,
            )
        elif kind == "rejected":
            binding = CandidateClaimBinding(
                task.id,
                claimed.package.claim_id,
                claimed.package.claim_epoch,
                claimed.package.package_id,
            )
            proposal = create_candidate_proposal(
                domain_id=setup.genesis.id,
                actor_id=host.actor_key.actor_id,
                key_id=host.actor_key.key_id,
                signer=host.signer,
                claim_binding=binding,
                effects=(CandidateEffect("unknown.effect", {}),),
                idempotency_key="invalid-output",
            )
            decision = CandidateCommitter(store, store).commit(proposal, setup.genesis)
            assert not decision.accepted
        else:
            grant = next(
                row
                for _, row in store.scan_runtime_records(record_type="claim.granted")
            )
            payload = dict(grant.payload)
            expiration = create_runtime_record(
                "claim.expired", payload, causal_parents=(grant.id,)
            )
            entry = create_entry(task.id, "claim_expired")
            store.commit_claim_expiration(
                trace_entry=entry,
                runtime_records=(
                    expiration,
                    trace_record(entry, causal_parents=(expiration.id,)),
                ),
                claim_id=claimed.package.claim_id,
                claim_epoch=claimed.package.claim_epoch,
                expected_lease_until=payload["lease_until"],
                observed_at=(
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat(),
            )
        assert not store.read_task_facts(task.id, ("lifecycle.terminal",))
        assert (
            allowance_available(task, [row for _, row in store.scan_runtime_records()])
            == 0
        )
        ext = store.read_task_facts(task.id, ("allowance.extinguished",))
        assert len(ext) == 1 and ext[0].payload["amount"] == 3
        assert project_task_status(task, store)["status"] in {"stalled", "submitted"}
        original_claim = store.get_claim(task.id)
        setup.accept_asset(
            Asset(id="unused", name="required_result", content="actual external result")
        )
        assert (
            RuntimeProjection(store, store).contract_view(task).terminal == "complete"
        )
        assert store.get_claim(task.id) == original_claim
        assert store.read_task_facts(task.id, ("allowance.extinguished",)) == ext
        digest = store.runtime_materialization_digest()
        assert store.rebuild_runtime_materializations(strict=True) == digest
    with SQLiteStore(path) as store:
        assert (
            project_task_status(store.get_contract(task.id), store)["status"]
            == "completed"
        )


def test_legacy_failed_records_are_immutable_and_parent_fulfillment_cancels_descendants(
    tmp_path,
):
    with SQLiteStore(tmp_path / "legacy.sqlite") as store:
        setup = candidate_runtime(store)
        parent = setup.accept_contract(
            build_control_plane_contract(name="parent", outputs=("goal",), budget=4)
        )
        child = setup.accept_contract(
            replace(
                build_control_plane_contract(
                    name="child", outputs=("child_goal",), budget=1
                ),
                id="new-child",
                parent_id=parent.id,
            )
        )
        fresh = setup.accept_contract(
            replace(
                build_control_plane_contract(
                    name="grandchild",
                    outputs=("unused_child_goal",),
                    budget=1,
                ),
                id="new-grandchild",
                parent_id=child.id,
            )
        )
        # Trusted historical import seam: never a supported fresh Candidate effect.
        old = tuple(
            create_terminal_record(task.id, "failed", reason="legacy failure")
            for task in (parent, child)
        )
        for row in old:
            store.append_runtime_record(row)
        setup.accept_asset(
            Asset(id="unused", name="goal", content="fulfilled elsewhere")
        )
        assert (
            RuntimeProjection(store, store).contract_view(parent).terminal == "complete"
        )
        assert (
            RuntimeProjection(store, store).contract_view(child).terminal == "cancelled"
        )
        assert (
            RuntimeProjection(store, store).contract_view(fresh).terminal == "cancelled"
        )
        assert store.read_task_facts(parent.id, ("lifecycle.terminal",)) == (old[0],)
        assert store.read_task_facts(child.id, ("lifecycle.terminal",)) == (old[1],)
        assert store.claim_contract(child.id, "any-worker") is None
        assert store.claim_contract(fresh.id, "any-worker") is None
        cancellation = store.read_task_facts(fresh.id, ("lifecycle.terminal",))[0]
        asset_commits = store.scan_runtime_records(record_type="asset.committed")
        assert asset_commits[-1][1].id in cancellation.causal_parents


def test_claim_and_failure_writer_do_not_scan_unrelated_history(tmp_path, monkeypatch):
    import aigineering.runtime_services as services

    monkeypatch.setattr(services, "sync_runtime", lambda *args, **kwargs: None)
    with SQLiteStore(tmp_path / "local-failure.sqlite") as store:
        setup, _task, _host, claimed = _claimed(store)
        target = setup.accept_contract(
            build_control_plane_contract(
                name="other local claim", outputs=("other_result",), budget=1
            )
        )
        scan = store.scan_runtime_records

        def scoped_scan(*args, **kwargs):
            assert kwargs.get("record_type") is not None, "writer scanned all history"
            return scan(*args, **kwargs)

        def forbidden(*args, **kwargs):
            raise AssertionError("writer loaded all unrelated objects")

        monkeypatch.setattr(store, "get_all_assets", forbidden)
        monkeypatch.setattr(store, "get_all_contracts", forbidden)
        monkeypatch.setattr(store, "scan_runtime_records", scoped_scan)
        assert (
            store.claim_contract(target.id, "trusted-internal-claim-test") is not None
        )
        _record_worker_invocation_failure(
            claimed, store, category="provider_error", status_code=503, retryable=True
        )
        assert store.read_task_facts(claimed.contract.id, ("allowance.extinguished",))


def test_legacy_failed_audit_does_not_blacklist_real_remaining_funding(tmp_path):
    with SQLiteStore(tmp_path / "legacy-funding.sqlite") as store:
        setup = candidate_runtime(store)
        parent = setup.accept_contract(
            build_control_plane_contract(
                name="legacy unspent", outputs=("goal",), budget=3
            )
        )
        old = create_terminal_record(parent.id, "failed", reason="legacy attempt audit")
        store.append_runtime_record(old)
        child = setup.accept_contract(
            replace(
                build_control_plane_contract(
                    name="ordinary repair", outputs=("goal",), budget=1
                ),
                id="ordinary-child",
                parent_id=parent.id,
            )
        )
        assert child.parent_id == parent.id
        assert (
            allowance_available(
                parent, [row for _, row in store.scan_runtime_records()]
            )
            == 2
        )
        assert store.read_task_facts(parent.id, ("lifecycle.terminal",)) == (old,)


def test_external_parent_asset_closes_claims_and_diagnostics_prove_cancellation(
    tmp_path, monkeypatch
):
    import aigineering.runtime_services as services
    from aigineering.task_diagnostics import task_runs, task_diagnostics

    monkeypatch.setattr(services, "sync_runtime", lambda *args, **kwargs: None)
    path = tmp_path / "external-cancel.sqlite"
    with SQLiteStore(path) as store:
        setup, parent, _host, parent_claim = _claimed(store)
        child = setup.accept_contract(
            replace(
                build_control_plane_contract(
                    name="active child", outputs=("child_result",), budget=1
                ),
                id="new-child",
                parent_id=parent.id,
            )
        )
        child_host = hosted_worker(
            store,
            MockWorker(worker_id="worker:active-child"),
            genesis=setup.genesis,
            authority_key=setup.actor_key,
            authority_signer=setup.signer,
        )
        child_claim = claim_next_package(
            store, worker_id=child_host.worker_id, contract_id=child.id
        )
        assert child_claim is not None
        setup.accept_asset(
            Asset(
                id="external",
                name="required_result",
                content="fulfilled by external actor",
            )
        )
        for task, claim in ((parent, parent_claim), (child, child_claim)):
            assert store.get_claim(task.id)["status"] == "released"
            runs = task_runs(store, task.id)
            assert runs["ok"], runs["data"]["integrity"]
            report = task_diagnostics(store, task.id, claim.package.claim_id)
            assert report["ok"], report["data"]["integrity"]
        before = tuple(store.scan_runtime_records())
        digest = store.runtime_materialization_digest()
        assert store.rebuild_runtime_materializations(strict=True) == digest
        assert tuple(store.scan_runtime_records()) == before
    with SQLiteStore(path) as store:
        assert task_runs(store, child.id)["ok"]
