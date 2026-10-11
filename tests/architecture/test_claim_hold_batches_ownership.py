from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_hold_batch_owner_is_generic_and_keeps_single_fact_validation():
    source = (ROOT / "src/aigineering/core/claim_holds.py").read_text()
    store = (ROOT / "src/aigineering/core/sqlite_store.py").read_text()

    assert "_MAX_TRANSITIONS_PER_CANDIDATE = 64" in source
    assert "claim-hold Candidate batch cannot mix" in source
    assert "claim-hold Candidate batch must target one Contract" in source
    assert "claim-hold Candidate batch repeats a record ID" in source
    assert "claim-hold Candidate batch repeats a released hold ID" in source
    assert "validate_claim_hold_batch(" in store
    assert "task.promote" not in source
    assert "task.edit" not in source
    assert "from aigineering.plugins" not in source


def test_hold_batch_behavior_uses_signed_candidate_and_store_transaction():
    test_source = (ROOT / "tests/test_claim_hold_batches.py").read_text()
    assert "CandidateCommitter" in test_source
    assert "EffectHandler" in test_source
    assert "CandidateCommitRejected" in test_source
    assert "ThreadPoolExecutor" in test_source
    assert "release-valid-and-unknown" in test_source
