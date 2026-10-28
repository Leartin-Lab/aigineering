"""Operational coordination stays outside the commitment kernel."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_shared_task_views_do_not_import_cli_or_publish_effects():
    source = (ROOT / "src/aigineering/task_views.py").read_text()
    assert "from aigineering.cli" not in source
    assert "commit_local_effect" not in source
    assert "append_runtime_record(" not in source


def test_redis_relay_uses_records_and_never_decides_claims():
    source = (ROOT / "src/aigineering/redis_collaboration.py").read_text()
    assert "scan_runtime_records(" in source
    for forbidden in (
        "claim_next_package(",
        "commit_ingress_batch(",
        "publish_effects(",
        "add_contract(",
    ):
        assert forbidden not in source


def test_execution_entries_sync_after_commit_and_use_redis_wakes():
    source = (ROOT / "src/aigineering/local_fleet.py").read_text()
    assert "sync_runtime(store)" in source
    assert "collaboration_service(store).wait(" in source
    assert "contract_tree_ids(" in source
