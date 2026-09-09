from aigineering import runtime


def test_runtime_maintenance_step_has_canonical_order_and_structured_result(
    monkeypatch,
):
    calls = []
    store = object()
    registry = object()
    publishers = object()

    def worker_failures(actual_store, *, candidate_publishers=None):
        calls.append(("worker_failures", actual_store, candidate_publishers))
        return ["worker-contract"]

    def rejected_submissions(actual_store, *, candidate_publishers=None):
        calls.append(("rejected_submissions", actual_store, candidate_publishers))
        return ["rejected-contract"]

    def task_completions(actual_store, actual_registry, *, candidate_publishers=None):
        calls.append(
            (
                "task_completions",
                actual_store,
                actual_registry,
                candidate_publishers,
            )
        )
        return ["completed-contract"]

    monkeypatch.setattr(runtime, "process_worker_failures", worker_failures)
    monkeypatch.setattr(runtime, "process_rejected_submissions", rejected_submissions)
    monkeypatch.setattr(runtime, "process_task_completions", task_completions)

    result = runtime.run_runtime_maintenance_step(
        store,
        registry,
        candidate_publishers=publishers,
    )

    assert calls == [
        ("worker_failures", store, publishers),
        ("rejected_submissions", store, publishers),
        ("task_completions", store, registry, publishers),
    ]
    assert result == runtime.RuntimeMaintenanceResult(
        worker_failures=("worker-contract",),
        rejected_submissions=("rejected-contract",),
        task_completions=("completed-contract",),
    )
