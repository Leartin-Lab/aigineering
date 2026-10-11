"""Fleet failure identity stays a read-only diagnostics and app concern."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_fleet_failure_details_reuse_read_only_exact_claim_owner():
    fleet = (ROOT / "src/aigineering/local_fleet.py").read_text()
    diagnostics = (ROOT / "src/aigineering/task_diagnostics.py").read_text()
    shared = (ROOT / "src/aigineering/run_failure_diagnostics.py").read_text()
    run_cli = (ROOT / "src/aigineering/cli/run.py").read_text()
    fleet_cli = (ROOT / "src/aigineering/cli/fleet.py").read_text()

    assert "invocation_failure_detail as _invocation_failure_detail" in fleet
    assert "format_worker_failure_detail as _format_worker_failure_detail" in fleet
    assert "task_invocation_failure_binding" in shared
    assert "task_invocation_failure_binding" not in fleet
    assert "_snapshot(store, contract_id)" in diagnostics
    assert "_build_runs(contract.id" in diagnostics
    assert "commit_ingress_batch" not in diagnostics
    assert "submit_worker_proposal" not in diagnostics
    assert '"worker_failure_details"' in run_cli
    assert '"worker_failure_details"' in fleet_cli
    assert "str(future.exception())" not in fleet
    assert "str(exc)" not in fleet
