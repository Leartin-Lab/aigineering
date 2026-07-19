"""Fresh materialized views rebuild from immutable runtime facts."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import candidate_runtime, hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.runtime import claim_next_package, execute_claimed_package
from aigineering.core.asset_versions import create_replacement_claim
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.protocol.types import Asset, Contract


def test_delete_and_rebuild_all_runtime_materializations(tmp_path):
    store = SQLiteStore(str(tmp_path / "rebuild.db"))
    ingress = candidate_runtime(store)
    source = ingress.accept_asset(
        Asset(id="asset:source", name="source", content="evidence"),
        source="test",
    )
    replacement = ingress.accept_asset(
        Asset(id="asset:source-v2", name="source", content="evidence v2"),
        source="test",
    )
    replacement_claim = create_replacement_claim(source.id, replacement.id)
    ingress.accept_replacement_claim(replacement_claim, source="test")
    contract = ingress.accept_contract(
        build_control_plane_contract(
            name="rebuildable",
            inputs=("source",),
            outputs=("report",),
            activation="source",
            budget=1,
        )
    )
    worker = MockWorker(
        {"rebuildable": '/exec {"outputs": {"report": "done"}}'},
        worker_id="worker",
    )
    worker.registration = lambda: WorkerRegistration(
        "worker",
        capabilities=("text",),
        profile_id="profile:test",
        version="1",
    )
    host = hosted_worker(
        store,
        worker,
        genesis=ingress.genesis,
        authority_key=ingress.actor_key,
        authority_signer=ingress.signer,
    )
    claimed = claim_next_package(
        store, worker_id=host.worker_id, contract_id=contract.id
    )
    assert claimed is not None
    execute_claimed_package(claimed, host, store)
    before_digest = store.runtime_materialization_digest()
    before_view = RuntimeProjection(store, store).contract_view(contract)

    rebuilt_digest = store.rebuild_runtime_materializations()
    rebuilt_contract = store.get_contract(contract.id)
    assert rebuilt_contract is not None
    after_view = RuntimeProjection(store, store).contract_view(rebuilt_contract)

    assert rebuilt_digest == before_digest
    assert after_view.projection_hash == before_view.projection_hash
    assert after_view.terminal == "complete"
    assert store.get_assets_by_name("report")[0].content == "done"
    assert store.get_claim(contract.id)["status"] == "submitted"
    assert store.get_worker_registration("worker").profile_id == "profile:test"
    rebuilt_claim = store.get_claims_for_asset(source.id)[0]
    assert rebuilt_claim.id == replacement_claim.id
    assert rebuilt_claim.source_asset_id == replacement_claim.source_asset_id
    assert rebuilt_claim.replacement_asset_id == replacement_claim.replacement_asset_id
    store.close()


def test_rebuild_fails_closed_on_unrecorded_legacy_rows():
    store = SQLiteStore(":memory:")
    store.add_contract(Contract(id="legacy:unrecorded"))

    with pytest.raises(RuntimeError, match="absent from immutable log"):
        store.rebuild_runtime_materializations()
    assert store.get_contract("legacy:unrecorded") is not None
    store.close()


def test_v5_materializations_backfill_into_reconstructable_runtime_facts(tmp_path):
    path = str(tmp_path / "v5-upgrade.db")
    store = SQLiteStore(path)
    ingress = candidate_runtime(store)
    source = ingress.accept_asset(Asset(id="asset:v5", name="source", content="v1"))
    replacement = ingress.accept_asset(
        Asset(id="asset:v5-2", name="source", content="v2")
    )
    replacement_claim = create_replacement_claim(source.id, replacement.id)
    ingress.accept_replacement_claim(replacement_claim)
    contract = ingress.accept_contract(
        build_control_plane_contract(
            name="v5-task",
            inputs=("source",),
            outputs=("report",),
            activation="source",
            budget=1,
        )
    )
    store.register_worker(WorkerRegistration("v5-worker", version="1"))
    claim = store.claim_contract(contract.id, "v5-worker", package_id="pkg:v5")
    assert claim is not None
    with store._conn:
        store.set_idempotency(contract.id, "v5-key", {"status": "legacy"})
    store.close()

    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM runtime_records")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version VALUES (5, 'legacy-v5')")
    conn.commit()
    conn.close()

    migrated = SQLiteStore(path)
    record_types = {record.record_type for _, record in migrated.scan_runtime_records()}
    assert {
        "asset.committed",
        "claim.granted",
        "contract.declared",
        "idempotency.bound",
        "replacement.claimed",
        "trace.recorded",
        "worker.registered",
    } <= record_types
    before = migrated.runtime_materialization_digest()
    assert migrated.rebuild_runtime_materializations() == before
    assert migrated.get_claim(contract.id)["status"] == "active"
    assert migrated.get_idempotency(contract.id, "v5-key") == {"status": "legacy"}
    rebuilt_claim = migrated.get_claims_for_asset(source.id)[0]
    assert rebuilt_claim.id == replacement_claim.id
    assert rebuilt_claim.source_asset_id == replacement_claim.source_asset_id
    assert rebuilt_claim.replacement_asset_id == replacement_claim.replacement_asset_id
    migrated.close()
