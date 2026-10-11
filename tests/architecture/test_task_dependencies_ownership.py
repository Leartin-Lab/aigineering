from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dependencies_use_one_plugin_fact_family_and_generic_claim_gate():
    plugin = (ROOT / "src/aigineering/plugins/task_dependencies.py").read_text()
    generic = (ROOT / "src/aigineering/core/claim_constraints.py").read_text()
    store = (ROOT / "src/aigineering/core/sqlite_store.py").read_text()
    projection = (ROOT / "src/aigineering/core/runtime_projection.py").read_text()
    effect_projectors = (ROOT / "src/aigineering/core/effect_projectors.py").read_text()

    assert '"task.dependency.link"' in plugin
    assert '"task.dependency.unlink"' in plugin
    assert "open_claim_constraints" in plugin
    assert '"claim.constraint.placed"' in generic
    assert '"claim.constraint.released"' in generic
    assert "task.dependency" not in generic
    assert "from aigineering.plugins" not in store
    assert "from aigineering.plugins" not in effect_projectors
    assert "validate_claim_constraint_batch" in store
    claim_body = store.split("def claim_contract", 1)[1].split("def renew_claim", 1)[0]
    assert "claim_constraint_blockers" in claim_body
    assert "prerequisite:" in projection


def test_dependencies_are_not_contract_inputs_or_mutable_edge_tables():
    store = (ROOT / "src/aigineering/core/sqlite_store.py").read_text()
    migrations = (ROOT / "src/aigineering/core/sqlite_migrations.py").read_text()
    plugin = (ROOT / "src/aigineering/plugins/task_dependencies.py").read_text()

    assert '"inputs"' not in plugin
    assert "CREATE TABLE task_dependencies" not in migrations
    assert "UPDATE task_dependencies" not in store
    assert "claim.constraint.placed" in store
    assert "claim.constraint.released" in store
