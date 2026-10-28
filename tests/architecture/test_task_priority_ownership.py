import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_priority_semantics_stay_in_plugin_and_application():
    core = ROOT / "src/aigineering/core"
    core_text = "\n".join(
        path.read_text(encoding="utf-8") for path in core.glob("*.py")
    )
    assert "task.priority" not in core_text
    assert "PRIORITY_FACT" not in core_text

    plugin = (ROOT / "src/aigineering/plugins/task_priority.py").read_text(
        encoding="utf-8"
    )
    assert "direct_output_fact_types=frozenset({PRIORITY_FACT})" in plugin
    assert "reserved_fact_types=frozenset({PRIORITY_FACT})" in plugin
    assert '"task.priority.set"' in plugin


def test_absent_priority_target_is_classified_in_shared_owner_before_history_reads():
    source = ROOT / "src/aigineering/task_priority.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    functions = {
        item.name: item
        for item in tree.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_require_materialized_priority_contract" in functions
    for owner_name in ("validate_task_priority", "publish_task_priority"):
        owner = functions[owner_name]
        call_nodes = sorted(
            (
                node
                for node in ast.walk(owner)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            ),
            key=lambda node: node.lineno,
        )
        calls = [node.func.id for node in call_nodes]
        assert "_require_materialized_priority_contract" in calls
        require_index = calls.index("_require_materialized_priority_contract")
        reads = [
            calls.index(name)
            for name in ("priority_declaration", "priority_history")
            if name in calls
        ]
        assert reads and require_index < min(reads)
