"""Focused read-efficiency checks for the public claim path."""

from __future__ import annotations

from aigineering.cli._candidate import commit_local_effects, require_accepted
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.local_identity import ensure_local_domain, ensure_local_worker_host
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.runtime import claim_next_package


class _Worker:
    worker_id = "worker:read-efficiency"

    def registration(self):
        from aigineering.core.worker_routing import WorkerRegistration

        return WorkerRegistration(
            self.worker_id,
            capabilities=("text.extract",),
            pools=("economy",),
        )

    def invoke(self, contract, disclosed_assets):
        del contract, disclosed_assets
        raise AssertionError("claim test must not invoke a Worker")


def _publish_contract(store, *, name: str, capability: str):
    contract = build_control_plane_contract(
        name=name,
        outputs=(f"{name}.result",),
        budget=1,
        worker_capabilities=(capability,),
        worker_pools=("economy",),
    )
    return require_accepted(
        commit_local_effects(
            store,
            (contract_declaration_effect(contract),),
            idempotency_key=f"claim-read:{contract.id}",
        )
    ).contract


def test_claim_routes_before_asset_and_projection_reads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SQLiteStore(str(tmp_path / "claims.db"))
    try:
        ensure_local_domain(store)
        worker = _Worker()
        host = ensure_local_worker_host(store, worker)
        eligible = _publish_contract(store, name="eligible", capability="text.extract")
        ineligible = _publish_contract(
            store, name="ineligible", capability="reasoning.deep"
        )
        assert eligible is not None and ineligible is not None

        counts = {"all_contracts": 0, "all_assets": 0}
        original_all_contracts = store.get_all_contracts
        original_all_assets = store.get_all_assets

        def counted_contracts():
            counts["all_contracts"] += 1
            return original_all_contracts()

        def counted_assets():
            counts["all_assets"] += 1
            return original_all_assets()

        store.get_all_contracts = counted_contracts
        store.get_all_assets = counted_assets
        monkeypatch.setattr(
            "aigineering.runtime.process_expired_claims", lambda *a, **k: []
        )

        assert (
            claim_next_package(
                store,
                worker_id=host.worker_id,
                contract_id=ineligible.id,
            )
            is None
        )
        assert counts == {"all_contracts": 0, "all_assets": 0}

        claimed = claim_next_package(
            store,
            worker_id=host.worker_id,
            contract_id=eligible.id,
        )
        assert claimed is not None
        assert counts["all_contracts"] == 0
        assert counts["all_assets"] == 1
    finally:
        store.close()
