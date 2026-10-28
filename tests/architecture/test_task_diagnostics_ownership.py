"""Task history reads remain fact-derived and outside writer ownership."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_task_diagnostics_is_a_read_owner_and_cli_is_a_thin_adapter():
    owner = (ROOT / "src/aigineering/task_diagnostics.py").read_text()
    cli = (ROOT / "src/aigineering/cli/task_diagnostics.py").read_text()

    assert "require_operational_store" in owner
    assert "scan_runtime_records(through_revision=watermark)" in owner
    assert "candidate.authentication_rejected" not in owner
    assert "CandidatePublisher" not in owner
    assert "commit_ingress_batch" not in owner
    assert "claim_contract(" not in owner
    assert 'payload.get("raw_output")' not in owner
    assert 'payload.get("diagnostic")' not in owner
    assert "publish_effect" not in cli
    assert "submit_worker_proposal" not in cli
    assert "collaboration_service(store).sync()" in cli
    assert "_persistent_snapshot()" in cli


def test_task_diagnostics_exports_only_closed_safe_event_categories():
    owner = (ROOT / "src/aigineering/task_diagnostics.py").read_text()

    assert "_FAILURE_CODES = frozenset(" in owner
    assert 'return "unknown"' in owner
    assert "_EVENT_RECORD_TYPES = frozenset(" in owner
    assert 'row["record_type"] = record.record_type' not in owner
    assert 'record.payload.get("raw_output")' not in owner
