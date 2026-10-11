"""Run command reports exact, proven invocation failures without raw errors."""

import json

import pytest
from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.protocol.identity import contract_from_fields
from aigineering.run_failure_diagnostics import (
    format_worker_failure_detail,
    invocation_failure_detail,
    safe_task_rejections,
    safe_worker_error_messages,
)
from aigineering.cli._cycle_result import configured_cycle_payload
from aigineering.task_diagnostics import task_runs
from aigineering.task_publication import publish_task_contract


def _create_task(runner, name, output, *extra):
    result = runner.invoke(
        cli, ["task", "create", "--name", name, "--output", output, *extra, "--json"]
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["contract_id"]


class _FailingWorker:
    worker_id = "fixture-failing-worker"

    def invoke(self, contract, disclosed_assets):
        from aigineering.agent.worker import WorkerExecutionError

        del contract, disclosed_assets
        raise WorkerExecutionError(
            "fixture_provider_failure", "PRIVATE_RUN_FAILURE_MARKER"
        )


@pytest.mark.parametrize("target_mode", [False, True], ids=["pool", "target-child"])
def test_run_failure_exposes_only_exact_proven_claim_details(
    monkeypatch, tmp_path, target_mode
):
    runner = CliRunner()
    monkeypatch.setattr(
        "aigineering.cli.run.build_worker",
        lambda *_args, **_kwargs: _FailingWorker(),
    )
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output

        if target_mode:
            requested_id = _create_task(
                runner,
                "blocked-root",
                "root-output",
                "--activation",
                "missing-input",
            )
            child = contract_from_fields(
                name="eligible-child",
                description="eligible descendant for failure diagnostics",
                inputs=(),
                outputs=("child-output",),
                activation="",
                budget=2,
                tool_scope=(),
                labels=(),
                origin="planner",
                parent_id=requested_id,
            )
            store = SQLiteStore(".aig/store.db")
            try:
                publish_task_contract(store, child)
            finally:
                store.close()
            arguments = ["run", "--task", requested_id]
        else:
            requested_id = _create_task(runner, "pool-task", "pool-output")
            child = None
            arguments = ["run", "--once"]

        result = runner.invoke(
            cli, [*arguments, "--worker", "llm", "--model", "fixture", "--json"]
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        expected_status = "blocked" if target_mode else "failed"
        assert payload["status"] == expected_status
        assert payload["ok"] is False
        details = payload["worker_failure_details"]
        assert len(details) == 1
        detail = details[0]
        assert detail["kind"] == "invocation_failure"
        assert detail["status"] == "invocation_failed"
        assert detail["contract_id"] == (
            child.id if child is not None else requested_id
        )
        assert detail["claim_id"].startswith("lease:")
        assert detail["package_id"].startswith("pkg:")
        assert detail["worker_id"] == _FailingWorker.worker_id
        assert detail["failure_record_id"]
        assert detail["release_record_id"]
        assert payload["cycles"] == []
        assert "PRIVATE_RUN_FAILURE_MARKER" not in result.output
        assert "PRIVATE_RUN_FAILURE_MARKER" not in payload["error"]
        store = SQLiteStore(".aig/store.db")
        try:
            runs = task_runs(store, detail["contract_id"])["data"]["runs"]
            proven = next(row for row in runs if row["claim_id"] == detail["claim_id"])
            assert proven["status"] == "invocation_failed"
            assert proven["package_id"] == detail["package_id"]
            assert proven["worker_id"] == detail["worker_id"]
            assert proven["epoch"] == detail["epoch"]
            assert proven["grant_record_id"] == detail["grant_record_id"]
            assert proven["failure"]["record_id"] == detail["failure_record_id"]
            release = next(
                store.get_runtime_record(record_id)
                for record_id in proven["fact_ids"]
                if store.get_runtime_record(record_id).record_type == "claim.released"
            )
            assert release.id == detail["release_record_id"]
        finally:
            store.close()


def test_human_run_failure_includes_safe_diagnostic_not_raw_exception(
    monkeypatch, tmp_path
):
    runner = CliRunner()
    monkeypatch.setattr(
        "aigineering.cli.run.build_worker",
        lambda *_args, **_kwargs: _FailingWorker(),
    )
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        task_id = _create_task(runner, "human-failure", "human-output")

        result = runner.invoke(
            cli, ["run", "--once", "--worker", "llm", "--model", "fixture"]
        )

        assert result.exit_code == 1, result.output
        assert "run failed" in result.output
        assert f'task "{task_id}" claim "lease:' in result.output
        assert "inspect task runs/diagnostics" in result.output
        assert "PRIVATE_RUN_FAILURE_MARKER" not in result.output


def test_unavailable_claim_proof_does_not_echo_supplied_ids_or_exception(
    monkeypatch,
):
    class Claimed:
        contract = type("Contract", (), {"id": "task:unproven-marker"})()
        package = type(
            "Package",
            (),
            {
                "claim_id": "lease:" + "a" * 64,
                "package_id": "pkg:" + "b" * 64,
                "claim_epoch": 1,
            },
        )()
        worker_id = "worker:unproven-marker"

    def invalid_proof(*_args):
        raise ValueError("PRIVATE_PROOF_EXCEPTION_MARKER")

    monkeypatch.setattr(
        "aigineering.run_failure_diagnostics.task_invocation_failure_binding",
        invalid_proof,
    )

    detail = invocation_failure_detail(object(), Claimed())

    assert detail == {
        "kind": "invocation_failure",
        "status": "diagnostic_unavailable",
        "category": "diagnostic_unavailable",
    }
    assert "task:unproven-marker" not in repr(detail)
    assert "lease:" not in repr(detail)
    assert "worker:unproven-marker" not in repr(detail)
    assert "PRIVATE_PROOF_EXCEPTION_MARKER" not in repr(detail)
    assert "claim was released" not in format_worker_failure_detail(detail)


def test_run_failure_with_unavailable_proof_returns_no_claim_identifiers(
    monkeypatch, tmp_path
):
    runner = CliRunner()
    monkeypatch.setattr(
        "aigineering.cli.run.build_worker",
        lambda *_args, **_kwargs: _FailingWorker(),
    )

    def invalid_proof(*_args):
        raise ValueError("PRIVATE_DIAGNOSTIC_READ_MARKER")

    monkeypatch.setattr(
        "aigineering.run_failure_diagnostics.task_invocation_failure_binding",
        invalid_proof,
    )
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        _create_task(runner, "unavailable-proof", "unavailable-output")

        result = runner.invoke(
            cli,
            ["run", "--once", "--worker", "llm", "--model", "fixture", "--json"],
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        detail = payload["worker_failure_details"][0]
        assert detail == {
            "kind": "invocation_failure",
            "status": "diagnostic_unavailable",
            "category": "diagnostic_unavailable",
        }
        assert "PRIVATE_DIAGNOSTIC_READ_MARKER" not in result.output
        assert "claim_id" not in detail
        assert "contract_id" not in detail


def test_configured_failure_rendering_uses_exact_safe_details_only():
    details = [
        {
            "kind": "invocation_failure",
            "status": "invocation_failed",
            "contract_id": "task:v3:" + "a" * 64,
            "claim_id": "lease:" + "b" * 64,
            "category": "worker_error:fixture_provider_failure",
            "status_code": 0,
        }
    ]
    projection = {
        "status": "failed",
        "blockers": ["terminal:failed"],
        "rejections": [
            "[recovery] worker_error:fixture_provider_failure (provider): "
            "PRIVATE_RUN_FAILURE_MARKER"
        ],
    }
    result = type(
        "Result",
        (),
        {
            "contract_id": "task:v3:" + "a" * 64,
            "status": "failed",
            "completed": False,
            "timed_out": False,
            "worker_errors": ("PRIVATE_RUN_FAILURE_MARKER",),
            "worker_failure_details": tuple(details),
            "capture_warnings": (),
            "stop_reason": "worker_failure",
            "cycles": (),
            "submission_status": None,
            "projection": projection,
        },
    )()

    payload = configured_cycle_payload(result, resume_command="aig run --resume exact")
    rendered = json.dumps(payload, sort_keys=True)

    assert "PRIVATE_RUN_FAILURE_MARKER" not in rendered
    assert payload["worker_errors"] == [
        'worker invocation failed for task "task:v3:'
        + "a" * 64
        + '" claim "lease:'
        + "b" * 64
        + '"; claim was released; inspect task runs/diagnostics '
        "(category=worker_error:fixture_provider_failure, status_code=0)"
    ]
    assert payload["projection"]["rejections"] == ["[recovery] details omitted"]
    assert (
        safe_task_rejections(projection["rejections"], details)
        == payload["projection"]["rejections"]
    )
    assert safe_worker_error_messages(("PRIVATE_RUN_FAILURE_MARKER",), ()) == [
        "worker invocation failed; exact claim diagnostics are unavailable"
    ]
    assert safe_worker_error_messages((), details) == []


def test_terminal_resume_redacts_historical_recovery_text_without_current_proof():
    marker = "PRIVATE_RUN_FAILURE_MARKER"
    result = type(
        "Result",
        (),
        {
            "contract_id": "task:v3:" + "c" * 64,
            "status": "failed",
            "completed": False,
            "timed_out": False,
            "worker_errors": (),
            "worker_failure_details": (),
            "capture_warnings": (),
            "stop_reason": "terminal",
            "cycles": (),
            "submission_status": None,
            "projection": {
                "status": "failed",
                "blockers": ["terminal:failed"],
                "rejections": ["[recovery] provider diagnostic: " + marker],
            },
        },
    )()

    payload = configured_cycle_payload(result, resume_command="aig run --resume exact")

    assert marker not in json.dumps(payload, sort_keys=True)
    assert payload["projection"]["rejections"] == ["[recovery] details omitted"]
    assert "resume_command" not in payload
