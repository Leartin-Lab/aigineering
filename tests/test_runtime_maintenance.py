"""Maintenance replays only facts that represent real unfinished work."""

from aigineering import runtime


def test_runtime_maintenance_only_processes_failures_and_rejections(monkeypatch):
    calls = []
    store = object()
    publishers = object()

    def worker_failures(actual_store, *, candidate_publishers=None):
        calls.append(("worker_failures", actual_store, candidate_publishers))
        return ["failed-contract"]

    def rejected_submissions(actual_store, *, candidate_publishers=None):
        calls.append(("rejected_submissions", actual_store, candidate_publishers))
        return ["rejected-contract"]

    monkeypatch.setattr(runtime, "process_worker_failures", worker_failures)
    monkeypatch.setattr(runtime, "process_rejected_submissions", rejected_submissions)

    result = runtime.run_runtime_maintenance_step(
        store, candidate_publishers=publishers
    )

    assert calls == [
        ("worker_failures", store, publishers),
        ("rejected_submissions", store, publishers),
    ]
    assert result.worker_failures == ("failed-contract",)
    assert result.rejected_submissions == ("rejected-contract",)
    assert not hasattr(result, "task_completions")


def test_maintenance_has_no_postcompletion_dispatch_or_completion_result():
    import ast
    from pathlib import Path

    source = Path("src/aigineering/runtime.py").read_text()
    tree = ast.parse(source)
    maintenance = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "run_runtime_maintenance_step"
    )
    called = {
        node.func.id
        for node in ast.walk(maintenance)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called - {"tuple"} == {
        "process_worker_failures",
        "process_rejected_submissions",
        "RuntimeMaintenanceResult",
    }
    assert "process_task_completions" not in source
    assert "TaskCompletionContext" not in source
    assert "TaskCompletionProjector" not in source
