from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_handoff_semantics_live_in_plugin_and_core_has_only_generic_snapshot_guard():
    plugin = (ROOT / "src/aigineering/plugins/handoff.py").read_text()
    core = "\n".join(
        (ROOT / f"src/aigineering/core/{name}").read_text()
        for name in ("commitment.py", "commit_snapshot.py", "sqlite_store.py")
    )
    assert '"task.handoff": EffectHandler(' in plugin
    assert 'capability="task.handoff"' in plugin
    assert "projector=project_task_handoff_effect" in plugin
    assert "emitted_fact_types=frozenset({ASSIGNMENT_FACT})" in plugin
    assert (
        "assignment_admission=AssignmentAdmission.SAME_OBLIGATION_SUCCESSOR" in plugin
    )
    assert "contract.cancel" in plugin and "contract.publish" in plugin
    assert "task.handoff" not in core
    assert "from aigineering.plugins" not in core
    assert "validate_snapshot" in core
    assert "class ProjectionSnapshotConflict" in core


def test_link_is_an_immutable_runtime_fact_and_does_not_add_mutable_handoff_state():
    store = (ROOT / "src/aigineering/core/sqlite_store.py").read_text()
    migrations = (ROOT / "src/aigineering/core/sqlite_migrations.py").read_text()
    assert '"task.handed_off"' not in store
    assert "CREATE TABLE handoffs" not in migrations
