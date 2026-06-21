"""Concurrent worker claim/submit tests for SQLiteStore WAL mode."""

import threading
import time


from aigineering.core.ids import hash_asset_definition, hash_asset_content
from aigineering.core.provenance import sign_asset
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.types import Asset, Candidate, Contract


def _mk_asset(name, content="data"):
    """Create a signed Asset for store operations."""
    return sign_asset(
        Asset(
            id=f"asset:{name}",
            name=name,
            content=content,
            definition_hash=hash_asset_definition(name),
            content_hash=hash_asset_content(name, content),
            origin="test",
        ),
        signed_by="test",
    )


def _mk_candidate(worker_id, raw_output="/exec out\ntest"):
    return Candidate(
        worker_id=worker_id,
        raw_output=raw_output,
    )


class TestConcurrentClaims:
    """Multi-threaded claim operations — each thread gets its own SQLiteStore."""

    def test_two_workers_claim_different_contracts_both_succeed(self, tmp_path):
        """Two workers claiming different contracts concurrently → both succeed."""
        db_path = str(tmp_path / "aig_c1.db")
        # Set up contracts in main thread store
        setup_store = SQLiteStore(db_path)
        setup_store.add_contract(Contract(id="task:a", name="task_a", outputs=("out",), activation=""))
        setup_store.add_contract(Contract(id="task:b", name="task_b", outputs=("out",), activation=""))
        setup_store.close()

        results = {}

        def claim_one(contract_id, worker_id):
            store = SQLiteStore(db_path)
            try:
                claim = store.claim_contract(contract_id, worker_id)
                if claim is not None:
                    results[worker_id] = "claimed"
                else:
                    results[worker_id] = "claim returned None"
            except Exception as e:
                results[worker_id] = str(e)
            finally:
                store.close()

        barrier = threading.Barrier(2, timeout=10)

        def worker(contract_id, worker_id):
            barrier.wait()
            claim_one(contract_id, worker_id)

        t1 = threading.Thread(target=worker, args=("task:a", "worker_a"))
        t2 = threading.Thread(target=worker, args=("task:b", "worker_b"))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert results.get("worker_a") == "claimed", f"Worker A: {results.get('worker_a')}"
        assert results.get("worker_b") == "claimed", f"Worker B: {results.get('worker_b')}"

    def test_two_workers_claim_same_contract_one_fails(self, tmp_path):
        """Two workers claiming the same contract → only one succeeds."""
        db_path = str(tmp_path / "aig_c2.db")
        setup_store = SQLiteStore(db_path)
        setup_store.add_contract(Contract(id="task:shared", name="shared_task", outputs=("out",), activation=""))
        setup_store.close()

        results = {}

        def claim_one(worker_id):
            store = SQLiteStore(db_path)
            try:
                claim = store.claim_contract("task:shared", worker_id)
                if claim is not None:
                    results[worker_id] = "claimed"
                else:
                    results[worker_id] = "failed"
            except Exception as e:
                results[worker_id] = f"exception: {e}"
            finally:
                store.close()

        barrier = threading.Barrier(2, timeout=10)

        def worker(wid):
            barrier.wait()
            claim_one(wid)

        t1 = threading.Thread(target=worker, args=("worker_x",))
        t2 = threading.Thread(target=worker, args=("worker_y",))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert results.get("worker_x") is not None
        assert results.get("worker_y") is not None
        claimed = sum(1 for v in results.values() if v == "claimed")
        failed = sum(1 for v in results.values() if v == "failed")
        assert claimed == 1, f"Exactly one worker should claim: {results}"
        assert failed == 1, f"One worker should fail: {results}"

    def test_ten_workers_submit_to_ten_contracts_all_correct(self, tmp_path):
        """10 workers submit to 10 distinct contracts concurrently — all produce correct traces."""
        db_path = str(tmp_path / "aig_c3.db")
        setup_store = SQLiteStore(db_path)

        asset = _mk_asset("shared_input")
        setup_store.add_asset(asset)

        claim_ids = {}
        for i in range(10):
            cid = f"task:conc_{i}"
            c = Contract(
                id=cid, name=f"task_{i}",
                inputs=("shared_input",), outputs=("out",), activation="shared_input",
            )
            setup_store.add_contract(c)
            claim = setup_store.claim_contract(cid, f"worker_{i}")
            assert claim is not None, f"Main thread failed to claim {cid}"
            claim_ids[cid] = claim["claim_id"]
        setup_store.close()

        results = {}

        def submit_one(i):
            cid = f"task:conc_{i}"
            store = SQLiteStore(db_path)
            try:
                from aigineering.core.trace import create_entry

                entry = create_entry(
                    contract_id=cid, event_type="projection",
                    sequence=0, worker_id=f"worker_{i}",
                )
                store.commit_candidate_submission(
                    accepted_assets=[_mk_asset(f"out_{i}", f"result_{i}")],
                    trace_entries=[entry],
                    idempotency_key=f"key_{i}",
                    idempotency_result={"status": "accepted"},
                    claim_id=claim_ids[cid],
                    worker_id=f"worker_{i}",
                )
                results[i] = "submitted"
            except Exception as e:
                results[i] = str(e)
            finally:
                store.close()

        threads = []
        for i in range(10):
            t = threading.Thread(target=submit_one, args=(i,))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        for i in range(10):
            assert results.get(i) == "submitted", f"Worker {i}: {results.get(i)}"

        # Verify assets exist (open a verification store)
        verify_store = SQLiteStore(db_path)
        for i in range(10):
            assets = verify_store.get_assets_by_name(f"out_{i}")
            assert len(assets) == 1, f"Expected 1 asset out_{i}, got {len(assets)}"
            assert assets[0].content == f"result_{i}"
        verify_store.close()

    def test_no_deadlocks_with_concurrent_reads(self, tmp_path):
        """Concurrent reads while writes are in progress do not deadlock (WAL mode)."""
        db_path = str(tmp_path / "aig_c4.db")
        setup_store = SQLiteStore(db_path)
        setup_store.add_contract(Contract(id="task:r1", name="reader_test", outputs=("out",), activation=""))
        setup_store.add_asset(_mk_asset("reader_asset"))
        setup_store.close()

        stop_flag = threading.Event()
        errors = []

        def reader():
            store = SQLiteStore(db_path)
            try:
                while not stop_flag.is_set():
                    assets = store.get_assets_by_name("reader_asset")
                    assert len(assets) >= 1
                    time.sleep(0.01)
            except Exception as e:
                errors.append(str(e))
            finally:
                store.close()

        def writer():
            store = SQLiteStore(db_path)
            try:
                for i in range(20):
                    store.add_asset(_mk_asset(f"w_{i}", f"write_{i}"))
                    time.sleep(0.02)
            except Exception as e:
                errors.append(str(e))
            finally:
                store.close()

        rt = threading.Thread(target=reader)
        wt = threading.Thread(target=writer)
        rt.start()
        wt.start()
        wt.join(timeout=15)
        stop_flag.set()
        rt.join(timeout=5)

        assert len(errors) == 0, f"Errors during concurrent access: {errors}"
