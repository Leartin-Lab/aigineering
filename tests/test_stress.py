"""Candidate-native stress tests for larger SQLite task sets."""

from __future__ import annotations

import pytest
from conftest import hosted_worker

from aigineering.agent.mock import MockWorker
from aigineering.core.ids import hash_asset_content, hash_asset_definition
from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Asset, Contract
from aigineering.runtime import claim_next_package, execute_claimed_package


def _trigger(name: str) -> Asset:
    content = "ready"
    return sign_asset(
        Asset(
            id=f"asset:{name}",
            name=name,
            content=content,
            definition_hash=hash_asset_definition(name),
            content_hash=hash_asset_content(name, content),
        ),
        signed_by="test",
    )


@pytest.mark.stress
def test_hundred_chained_contracts_commit_through_worker_host(tmp_path):
    store = SQLiteStore(str(tmp_path / "chain.db"))
    host = hosted_worker(
        store,
        MockWorker(
            {
                f"stress_{index}": (f'/exec {{"out_{index}":"result_{index:03d}"}}')
                for index in range(100)
            },
            worker_id="worker:stress-chain",
        ),
    )
    store.add_asset(_trigger("trigger"))
    for index in range(100):
        dependency = "trigger" if index == 0 else f"out_{index - 1}"
        store.add_contract(
            Contract(
                id=f"task:stress_{index}",
                name=f"stress_{index}",
                inputs=(dependency,),
                outputs=(f"out_{index}",),
                activation=dependency,
                budget=1,
            )
        )

    for index in range(100):
        claimed = claim_next_package(store, worker_id=host.worker_id)
        assert claimed is not None
        assert claimed.contract.name == f"stress_{index}"
        assert execute_claimed_package(claimed, host, store)["status"] == "accepted"

    assert len(store.scan_runtime_records(record_type="candidate.committed")) >= 100
    for index in range(100):
        assets = store.get_assets_by_name(f"out_{index}")
        assert [asset.content for asset in assets] == [f"result_{index:03d}"]
    store.close()


@pytest.mark.stress
def test_hundred_independent_contracts_commit_without_collision(tmp_path):
    store = SQLiteStore(str(tmp_path / "independent.db"))
    host = hosted_worker(
        store,
        MockWorker(
            {
                f"parallel_{index}": (
                    f'/exec {{"parallel_out_{index}":"parallel_result_{index:03d}"}}'
                )
                for index in range(100)
            },
            worker_id="worker:stress-independent",
        ),
    )
    store.add_asset(_trigger("shared_trigger"))
    for index in range(100):
        store.add_contract(
            Contract(
                id=f"task:parallel_{index:03d}",
                name=f"parallel_{index}",
                inputs=("shared_trigger",),
                outputs=(f"parallel_out_{index}",),
                activation="shared_trigger",
                budget=1,
            )
        )

    for _ in range(100):
        claimed = claim_next_package(store, worker_id=host.worker_id)
        assert claimed is not None
        assert execute_claimed_package(claimed, host, store)["status"] == "accepted"

    for index in range(100):
        assets = store.get_assets_by_name(f"parallel_out_{index}")
        assert [asset.content for asset in assets] == [f"parallel_result_{index:03d}"]
    store.close()
