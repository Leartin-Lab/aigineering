"""Only matching admitted Assets fulfill requirements; reads stay local."""

import pytest

from conftest import candidate_runtime
from aigineering.core.commitment import CandidateCommitter
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.fact_reducer import FactReducer
from aigineering.core.output_satisfaction import all_outputs_satisfied
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.store import MemoryStore
from aigineering.protocol.candidate import CandidateEffect, create_candidate_proposal
from aigineering.protocol.types import Asset


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_new_task_observes_preexisting_assets_without_domain_task_scan(
    tmp_path, monkeypatch, kind
):
    store = (
        MemoryStore() if kind == "memory" else SQLiteStore(tmp_path / "local.sqlite")
    )
    setup = candidate_runtime(store)
    asset = setup.accept_asset(
        Asset(id="unused", name="existing_result", content="actual")
    )
    task = build_control_plane_contract(
        name="new consumer", outputs=(asset.name,), budget=1
    )
    reducer = FactReducer.on_assets_created

    def reduce_without_scans(self, *args, **kwargs):
        class NarrowStore:
            def __getattr__(self, name):
                if name in {
                    "get_all_assets",
                    "get_all_contracts",
                    "scan_runtime_records",
                }:
                    raise AssertionError(f"Reducer attempted {name}")
                return getattr(store, name)

        self._store = NarrowStore()
        return reducer(self, *args, **kwargs)

    monkeypatch.setattr(FactReducer, "on_assets_created", reduce_without_scans)
    if kind == "sqlite":

        def forbidden_domain_read(*args, **kwargs):
            raise AssertionError("new Task publication loaded unrelated objects")

        monkeypatch.setattr(store, "get_all_contracts", forbidden_domain_read)
        monkeypatch.setattr(store, "get_all_assets", forbidden_domain_read)
    setup.accept_contract(task)
    assert all_outputs_satisfied(task, store)
    (complete,) = store.read_task_facts(task.id, ("lifecycle.terminal",))
    assert complete.payload["terminal"] == "complete"
    assert store.read_asset_commit_facts(asset.id)[0].id in complete.causal_parents
    if kind == "sqlite":
        store.close()


def test_unknown_signed_effect_cannot_invent_completion(tmp_path):
    with SQLiteStore(tmp_path / "closed.sqlite") as store:
        setup = candidate_runtime(store)
        task = setup.accept_contract(
            build_control_plane_contract(name="unmet", outputs=("report",), budget=2)
        )
        proposal = create_candidate_proposal(
            domain_id=setup.genesis.id,
            actor_id=setup.actor_key.actor_id,
            key_id=setup.actor_key.key_id,
            signer=setup.signer,
            effects=(CandidateEffect("task.complete", {"contract_id": task.id}),),
            idempotency_key="no-controller",
        )
        decision = CandidateCommitter(store, store).commit(proposal, setup.genesis)
        assert not decision.accepted
        assert not store.read_task_facts(task.id, ("lifecycle.terminal",))
        assert not all_outputs_satisfied(task, store)


def test_all_selected_output_commits_are_exact_completion_causes(tmp_path):
    with SQLiteStore(tmp_path / "multi-output.sqlite") as store:
        setup = candidate_runtime(store)
        task = setup.accept_contract(
            build_control_plane_contract(
                name="two required Assets", outputs=("a", "b"), budget=2
            )
        )
        first = setup.accept_asset(Asset(id="unused-a", name="a", content="first"))
        assert not store.read_task_facts(task.id, ("lifecycle.terminal",))
        second = setup.accept_asset(Asset(id="unused-b", name="b", content="second"))
        (complete,) = store.read_task_facts(task.id, ("lifecycle.terminal",))
        required = {
            store.read_asset_commit_facts(item.id)[0].id for item in (first, second)
        }
        required.add(store.read_task_facts(task.id, ("contract.declared",))[0].id)
        assert set(complete.causal_parents) == required
        before = tuple(store.scan_runtime_records())
        digest = store.runtime_materialization_digest()
        assert store.rebuild_runtime_materializations(strict=True) == digest
        assert tuple(store.scan_runtime_records()) == before


def test_completion_cancellation_traverses_deep_closed_intermediates(tmp_path):
    from dataclasses import replace
    from aigineering.core.lifecycle_facts import create_terminal_record

    depth = 132
    with SQLiteStore(tmp_path / "deep.sqlite") as store:
        setup = candidate_runtime(store)
        root = setup.accept_contract(
            build_control_plane_contract(
                name="root", outputs=("final_goal",), budget=depth + 2
            )
        )
        parent = root
        descendants = []
        for index in range(depth):
            child = setup.accept_contract(
                replace(
                    build_control_plane_contract(
                        name=f"task {index}",
                        outputs=(f"result_{index}",),
                        budget=depth - index,
                    ),
                    id=f"pending-{index}",
                    parent_id=parent.id,
                )
            )
            descendants.append(child)
            parent = child
        closed = create_terminal_record(
            descendants[42].id, "cancelled", reason="immutable prior cancellation"
        )
        store.append_runtime_record(closed)
        setup.accept_asset(Asset(id="unused", name="final_goal", content="achieved"))
        for child in descendants:
            terminal = store.read_task_facts(child.id, ("lifecycle.terminal",))
            assert len(terminal) == 1 and terminal[0].payload["terminal"] == "cancelled"
        assert store.read_task_facts(descendants[42].id, ("lifecycle.terminal",)) == (
            closed,
        )
