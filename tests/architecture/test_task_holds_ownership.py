from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_claim_holds_use_plugin_semantics_and_generic_sqlite_transactions():
    plugin = (ROOT / "src/aigineering/plugins/collaboration.py").read_text()
    generic = (ROOT / "src/aigineering/core/claim_holds.py").read_text()
    store = (ROOT / "src/aigineering/core/sqlite_store.py").read_text()
    projection = (ROOT / "src/aigineering/core/runtime_projection.py").read_text()
    core_projectors = (ROOT / "src/aigineering/core/effect_projectors.py").read_text()

    assert "COLLABORATION_EFFECTS" in plugin
    assert "task.block" not in generic
    assert "task.unblock" not in generic
    assert "plugins." not in store
    assert "plugins." not in core_projectors
    assert "open_claim_holds" in store
    assert "validate_claim_hold_batch" in store
    claim_body = store.split("def claim_contract", 1)[1].split("def renew_claim", 1)[0]
    assert "open_claim_holds" in claim_body
    assert "claim_hold:" in projection
    assert "claim.hold.placed" in store
    assert "claim.hold.released" in store


def test_hold_facts_are_not_mutable_store_authority():
    store = (ROOT / "src/aigineering/core/sqlite_store.py").read_text()
    claim_holds = (ROOT / "src/aigineering/core/claim_holds.py").read_text()
    migrations = (ROOT / "src/aigineering/core/sqlite_migrations.py").read_text()

    assert "CREATE TABLE claim_holds" not in migrations
    assert "UPDATE claim_holds" not in store
    assert "worker_claims" not in claim_holds
