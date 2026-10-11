import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_allowance_returns_are_generic_consequences_and_writer_validated():
    allowance = (ROOT / "src/aigineering/core/causal_allowance.py").read_text()
    returns = (ROOT / "src/aigineering/core/allowance_returns.py").read_text()
    projection = (ROOT / "src/aigineering/core/effect_projection.py").read_text()
    store = (ROOT / "src/aigineering/core/sqlite_store.py").read_text()
    migration = (ROOT / "src/aigineering/core/sqlite_migrations.py").read_text()

    assert "def project_causal_allowance_returns(" in returns
    assert "def validate_causal_allowance_returns(" in returns
    assert "from aigineering.plugins" not in allowance + returns + projection + store
    assert "validate_causal_allowance_returns(" in projection
    assert projection.index("validate_causal_allowance_returns(") < projection.rindex(
        "project_contract_allowance_records("
    )
    assert '"contract.cancel"' in projection and '"contract.publish"' in projection
    for record_type in (
        "allowance.granted",
        "allowance.reserved",
        "allowance.returned",
        "allowance.extinguished",
    ):
        assert f'"{record_type}"' in store
    assert "validate_allowance_grants(" in returns
    assert "validate_allowance_grants(" in allowance
    assert "require_committed_head=True" in allowance
    assert "CURRENT_SCHEMA_VERSION = 21" in migration
    assert "def _migrate_to_v19(self)" in migration
    assert "pre-existing allowance.returned facts" in migration


def test_verifier_preservation_has_one_shared_core_owner():
    predicate = (ROOT / "src/aigineering/core/task_lifecycle.py").read_text()
    reducer = (ROOT / "src/aigineering/core/fact_reducer.py").read_text()
    assert "def serves_pending_independent_acceptance(" in predicate
    assert "return serves_pending_independent_acceptance(" in reducer


def test_only_reviewed_handoff_feature_requests_new_return_consequences():
    callers = set()
    constructors = set()
    for path in (ROOT / "src/aigineering").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id == "project_causal_allowance_returns":
                callers.add(path.relative_to(ROOT).as_posix())
            if (
                node.func.id == "create_runtime_record"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "allowance.returned"
            ):
                constructors.add(path.relative_to(ROOT).as_posix())
    assert callers == {"src/aigineering/plugins/handoff.py"}
    assert constructors == {"src/aigineering/core/allowance_returns.py"}
    core = "\n".join(
        path.read_text() for path in (ROOT / "src/aigineering/core").glob("*.py")
    )
    assert "task.handoff" not in core
    assert "task.handed_off" not in core
