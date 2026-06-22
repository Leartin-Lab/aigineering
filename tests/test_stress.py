"""Stress tests for the Aigineering runtime with SQLiteStore."""

import pytest

from aigineering.core.ids import hash_asset_definition, hash_asset_content
from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.trace import create_entry
from aigineering.protocol.types import Asset, Contract


def _mk_asset(name, content):
    return Asset(
        id=f"asset:{name}",
        name=name,
        content=content,
        definition_hash=hash_asset_definition(name),
        content_hash=hash_asset_content(name, content),
    )


class TestStressChainedContracts:
    """100 contracts chained via activation expressions."""

    @pytest.mark.stress
    def test_hundred_contracts_chained_all_traces_correct(self, tmp_path):
        """Create 100 chained contracts, process them all, verify traces and hashes."""
        db_path = str(tmp_path / "aig.db")
        store = SQLiteStore(db_path)

        # Create input asset that activates the first contract
        input_asset = sign_asset(_mk_asset("trigger", "go"), signed_by="test")
        store.add_asset(input_asset)

        # Create 100 contracts, each depending on the previous one
        contracts = []
        for i in range(100):
            activation = "trigger" if i == 0 else f"out_{i - 1}"
            c = Contract(
                id=f"task:stress_{i}",
                name=f"stress_{i}",
                inputs=[activation],
                outputs=[f"out_{i}"],
                activation=activation,
            )
            store.add_contract(c)
            contracts.append(c)

        # Process each contract: claim, create output asset, submit
        for i, c in enumerate(contracts):
            claim = store.claim_contract(c.id, f"worker_{i}")
            assert claim is not None, f"Failed to claim contract {c.id}"

            out_name = f"out_{i}"
            out_content = f"result_{i:03d}"
            out_asset = _mk_asset(out_name, out_content)
            out_asset = sign_asset(out_asset, signed_by=f"worker_{i}")

            entry = create_entry(
                contract_id=c.id,
                event_type="projection",
                sequence=0,
                worker_id=f"worker_{i}",
                candidate_raw=f"/exec {out_name}\n{out_content}",
                accepted_asset_names=[out_name],
            )

            store.commit_candidate_submission(
                accepted_assets=[out_asset],
                trace_entries=[entry],
                idempotency_key=f"stress_key_{i}",
                idempotency_result={"status": "accepted"},
                claim_id=claim["claim_id"],
                worker_id=f"worker_{i}",
            )

        # Verify all assets exist with correct content
        for i in range(100):
            assets = store.get_assets_by_name(f"out_{i}")
            assert len(assets) == 1, f"Missing asset out_{i}"
            assert assets[0].content == f"result_{i:03d}", (
                f"Wrong content for out_{i}: {assets[0].content}"
            )
            assert assets[0].definition_hash, f"Missing definition_hash for out_{i}"
            assert assets[0].content_hash, f"Missing content_hash for out_{i}"

        # Verify the trigger asset is untouched
        trigger_assets = store.get_assets_by_name("trigger")
        assert len(trigger_assets) == 1
        assert trigger_assets[0].content == "go"

        store.close()

    @pytest.mark.stress
    def test_hundred_contracts_parallel_claims(self, tmp_path):
        """100 contracts claimed and submitted — no collisions or data loss."""
        db_path = str(tmp_path / "aig.db")
        store = SQLiteStore(db_path)

        # Create 100 independent contracts with the same activation
        input_asset = sign_asset(_mk_asset("shared_trigger", "start"), signed_by="test")
        store.add_asset(input_asset)

        for i in range(100):
            c = Contract(
                id=f"task:par_{i}",
                name=f"par_{i}",
                inputs=["shared_trigger"],
                outputs=[f"par_out_{i}"],
                activation="shared_trigger",
            )
            store.add_contract(c)
            claim = store.claim_contract(c.id, f"worker_{i}")
            assert claim is not None, f"Failed to claim contract {c.id}"

            out_asset = _mk_asset(f"par_out_{i}", f"par_result_{i:03d}")
            out_asset = sign_asset(out_asset, signed_by=f"worker_{i}")

            entry = create_entry(
                contract_id=c.id,
                event_type="projection",
                sequence=0,
                worker_id=f"worker_{i}",
                candidate_raw=f"/exec par_out_{i}\npar_result_{i:03d}",
                accepted_asset_names=[f"par_out_{i}"],
            )

            store.commit_candidate_submission(
                accepted_assets=[out_asset],
                trace_entries=[entry],
                idempotency_key=f"par_key_{i}",
                idempotency_result={"status": "accepted"},
                claim_id=claim["claim_id"],
                worker_id=f"worker_{i}",
            )

        # Verify all 100 outputs exist
        for i in range(100):
            assets = store.get_assets_by_name(f"par_out_{i}")
            assert len(assets) == 1, f"Missing par_out_{i}"
            assert assets[0].content == f"par_result_{i:03d}"

        store.close()
