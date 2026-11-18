"""Only fixed core primitives can produce new runtime facts."""

import inspect
from pathlib import Path

import pytest
from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.core import candidate_publisher, commitment, effect_projection
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.candidate import CandidateEffect, create_candidate_proposal
from aigineering.protocol.types import Asset
from aigineering.runtime import claim_next_package
from aigineering.runtime_services import order_application_contract_ids


def test_kernel_has_no_injected_fact_producer_api():
    for function in (
        commitment.CandidateCommitter,
        commitment.reduce_candidate,
        candidate_publisher.CandidatePublisher,
        candidate_publisher.publish_effects,
        effect_projection.project_effect_batch,
    ):
        assert "additional_effects" not in inspect.signature(function).parameters
    source = Path(effect_projection.__file__).read_text()
    assert "EffectHandler" not in source
    assert "freeze_additional_effects" not in source
    assert "aigineering.plugins" not in source


@pytest.mark.parametrize(
    "effect_type",
    (
        "contract.complete",
        "task.assign",
        "task.block",
        "task.dependency.link",
        "task.handoff",
        "task.revise",
    ),
)
def test_removed_control_effect_is_rejected_without_task_or_asset_changes(
    tmp_path, effect_type
):
    store = SQLiteStore(tmp_path / "closed-primitives.sqlite")
    try:
        setup = candidate_runtime(store)
        task = setup.accept_contract(
            build_control_plane_contract(
                name="still-open", outputs=("report",), budget=3
            )
        )
        before = RuntimeProjection(store, store).contract_view(task)
        candidate = create_candidate_proposal(
            domain_id=setup.genesis.id,
            actor_id=setup.actor_key.actor_id,
            key_id=setup.actor_key.key_id,
            signer=setup.signer,
            effects=(
                CandidateEffect(
                    effect_type, {"contract_id": task.id, "terminal": "complete"}
                ),
            ),
            idempotency_key=effect_type,
        )
        revision = store.get_runtime_revision()
        decision = commitment.CandidateCommitter(store, store).commit(
            candidate, setup.genesis
        )
        assert not decision.accepted
        rows = tuple(
            row for _, row in store.scan_runtime_records(after_revision=revision)
        )
        assert {row.record_type for row in rows} <= {
            "candidate.received",
            "candidate.rejected",
            "trace.recorded",
        }
        assert any(
            "unsupported effect type" in row.payload.get("reason", "") for row in rows
        )
        assert RuntimeProjection(store, store).contract_view(task) == before
        assert store.get_assets_by_name("report") == []
    finally:
        store.close()


def test_descriptive_asset_has_ordinary_matching_semantics_and_no_control_privilege(
    tmp_path,
):
    store = SQLiteStore(tmp_path / "ordinary-descriptor.sqlite")
    try:
        setup = candidate_runtime(store)
        business = setup.accept_contract(
            build_control_plane_contract(name="business", outputs=("report",), budget=2)
        )
        descriptor_task = setup.accept_contract(
            build_control_plane_contract(
                name="descriptor-consumer", outputs=("relation",), budget=2
            )
        )
        descriptor = setup.accept_asset(
            Asset(
                id="fixture:relation",
                name="relation",
                content='{"terminal":"complete"}',
            )
        )
        assert RuntimeProjection(store, store).contract_view(business).terminal is None
        assert (
            RuntimeProjection(store, store).contract_view(descriptor_task).terminal
            == "complete"
        )
        assert descriptor.signed_by == setup.actor_key.actor_id
        assert order_application_contract_ids(
            store, (business.id, descriptor_task.id)
        ) == (business.id, descriptor_task.id)
        host = hosted_worker(
            store,
            MockWorker(worker_id="worker:ordinary"),
            genesis=setup.genesis,
            authority_key=setup.actor_key,
            authority_signer=setup.signer,
        )
        claimed = claim_next_package(
            store, worker_id=host.worker_id, contract_id=business.id
        )
        assert claimed is not None
    finally:
        store.close()
