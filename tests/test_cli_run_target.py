"""Target-scoped behavior for ``aig run --task``."""

import json
import sqlite3
from types import SimpleNamespace

from click.testing import CliRunner

from aigineering.cli.main import cli
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.task_views import project_task_status
from aigineering.core.control_plane import build_control_plane_contract
from aigineering.protocol.identity import contract_from_fields


def _create_task(runner, name, output, *extra):
    result = runner.invoke(
        cli,
        ["task", "create", "--name", name, "--output", output, *extra, "--json"],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["contract_id"]


def test_target_run_leaves_unrelated_ready_contract_unclaimed(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(runner, "target", "target_output")
        unrelated_id = _create_task(runner, "unrelated", "unrelated_output")

        result = runner.invoke(
            cli, ["run", "--task", target_id, "--worker", "mock", "--json"]
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert payload["stop_reason"] == "terminal"
        store = SQLiteStore(".aig/store.db")
        try:
            claimed = {
                str(record.payload.get("contract_id", ""))
                for _, record in store.scan_runtime_records(record_type="claim.granted")
            }
            assert claimed == {target_id}
            unrelated = store.get_contract(unrelated_id)
            assert unrelated is not None
            assert project_task_status(unrelated, store)["status"] == "ready"
            assert store.get_claim(unrelated_id) is None
        finally:
            store.close()


def test_terminal_target_is_checked_before_claiming_other_work(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(runner, "target", "target_output")
        first = runner.invoke(
            cli, ["run", "--task", target_id, "--worker", "mock", "--json"]
        )
        assert first.exit_code == 0, first.output
        unrelated_id = _create_task(runner, "unrelated", "unrelated_output")

        result = runner.invoke(
            cli, ["run", "--task", target_id, "--worker", "mock", "--json"]
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["stop_reason"] == "terminal"
        store = SQLiteStore(".aig/store.db")
        try:
            claimed = {
                str(record.payload.get("contract_id", ""))
                for _, record in store.scan_runtime_records(record_type="claim.granted")
            }
            assert claimed == {target_id}
            assert store.get_claim(unrelated_id) is None
        finally:
            store.close()


def test_blocked_idle_reports_status_blockers_and_next_step(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(
            runner,
            "blocked",
            "blocked_output",
            "--activation",
            "missing_input",
        )

        result = runner.invoke(
            cli, ["run", "--task", target_id, "--worker", "mock", "--json"]
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "blocked"
        assert payload["stop_reason"] == "idle"
        assert payload["timed_out"] is False
        assert payload["blockers"]
        assert "activation" in payload["next_step"]


def test_target_timeout_human_output_keeps_blockers_and_guidance(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(
            runner,
            "blocked",
            "blocked_output",
            "--activation",
            "missing_input",
        )

        result = runner.invoke(
            cli,
            [
                "run",
                "--task",
                target_id,
                "--worker",
                "mock",
                "--wait-timeout",
                "0.000001",
            ],
        )

        assert result.exit_code == 1, result.output
        assert "run blocked" in result.output
        assert "stop_reason: timeout" in result.output
        assert "blockers:" in result.output
        assert "next_step:" in result.output


def test_target_pool_passes_response_format_to_worker_builder(monkeypatch, tmp_path):
    from aigineering.agent.mock import MockWorker

    runner = CliRunner()
    options = {}

    def build_worker(*_args, **kwargs):
        options.update(kwargs)
        return MockWorker()

    monkeypatch.setattr("aigineering.cli.run.build_worker", build_worker)
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(
            runner,
            "blocked",
            "blocked_output",
            "--activation",
            "missing_input",
        )

        result = runner.invoke(
            cli,
            [
                "run",
                "--task",
                target_id,
                "--worker",
                "llm",
                "--model",
                "fixture",
                "--response-format",
                "json_object",
                "--json",
            ],
        )

        assert result.exit_code == 1, result.output
        assert options["response_format"] == "json_object"


def test_once_pool_passes_response_format_to_worker_builder(monkeypatch, tmp_path):
    from aigineering.agent.mock import MockWorker

    runner = CliRunner()
    options = {}

    def build_worker(*_args, **kwargs):
        options.update(kwargs)
        return MockWorker()

    monkeypatch.setattr("aigineering.cli.run.build_worker", build_worker)
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output

        result = runner.invoke(
            cli,
            [
                "run",
                "--once",
                "--worker",
                "llm",
                "--model",
                "fixture",
                "--response-format",
                "json_object",
                "--json",
            ],
        )

        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["status"] == "idle"
        assert options["response_format"] == "json_object"


def test_response_format_rejects_tool_registry_before_pool_start():
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--once",
            "--worker",
            "llm",
            "--model",
            "fixture",
            "--response-format",
            "json_object",
            "--tool-registry",
            "example:registry",
        ],
    )

    assert result.exit_code == 2
    assert "cannot be combined with --tool-registry" in result.output


def test_response_format_rejects_mock_worker():
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "--once",
            "--worker",
            "mock",
            "--response-format",
            "json_object",
        ],
    )

    assert result.exit_code == 2
    assert "requires --worker llm" in result.output


def test_recovery_status_does_not_cross_same_name_roots(monkeypatch, tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        failed_id = _create_task(runner, "shared_name", "first_output")
        other_id = _create_task(runner, "shared_name", "second_output")

        failed = runner.invoke(
            cli,
            [
                "run",
                "--task",
                failed_id,
                "--worker",
                "mock",
                "--mock-output",
                "not a valid action",
                "--json",
            ],
        )
        assert failed.exit_code == 1, failed.output
        first_recoveries = json.loads(failed.output)["recoveries"]
        assert first_recoveries
        deeply_nested = "[" * 1500 + "0" + "]" * 1500
        with sqlite3.connect(".aig/store.db") as connection:
            connection.execute(
                "UPDATE contracts SET description=? WHERE id=?",
                (deeply_nested, first_recoveries[0]),
            )

        first_status = runner.invoke(cli, ["task", "status", failed_id, "--json"])
        assert first_status.exit_code == 1, first_status.output
        first_status_payload = json.loads(first_status.output)
        assert first_status_payload["recoveries"] == first_recoveries

        status = runner.invoke(cli, ["task", "status", other_id, "--json"])
        assert status.exit_code == 1, status.output
        assert json.loads(status.output)["recoveries"] == []
        from aigineering.task_views import _has_active_recovery

        store = SQLiteStore(".aig/store.db")
        try:
            failed_contract = store.get_contract(failed_id)
            other_contract = store.get_contract(other_id)
            assert failed_contract is not None
            assert other_contract is not None
            assert _has_active_recovery(failed_contract, store) is True
            assert _has_active_recovery(other_contract, store) is False
        finally:
            store.close()

        from aigineering.agent.worker import WorkerExecutionError

        class FailingWorker:
            worker_id = "failing-worker-for-recovery-attribution"

            def invoke(self, contract, disclosed_assets):
                del contract, disclosed_assets
                raise WorkerExecutionError("fixture_failure", "fixture failure")

        monkeypatch.setattr(
            "aigineering.cli.run.build_worker",
            lambda *_args, **_kwargs: FailingWorker(),
        )
        second_failure = runner.invoke(
            cli,
            [
                "run",
                "--task",
                other_id,
                "--worker",
                "llm",
                "--model",
                "fixture",
                "--json",
            ],
        )

        assert second_failure.exit_code == 1, second_failure.output
        second_recoveries = json.loads(second_failure.output)["recoveries"]
        assert second_recoveries
        assert set(second_recoveries).isdisjoint(
            json.loads(failed.output)["recoveries"]
        )


def test_target_pool_claims_child_recovery_and_tool_descendants(monkeypatch):
    from aigineering.cli.run import _PoolHost, _claim_for_target_pool_host

    root = build_control_plane_contract(name="root", outputs=("root_output",))
    child = contract_from_fields(
        name="child",
        description="child work",
        inputs=(),
        outputs=("root_output",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        origin="planner",
        parent_id=root.id,
    )
    recovery = contract_from_fields(
        name="recovery",
        description="recovery work",
        inputs=(),
        outputs=("root_output",),
        activation="",
        budget=2,
        tool_scope=(),
        labels=(),
        origin="recovery",
        parent_id=child.id,
    )
    tool_child = contract_from_fields(
        name="tool_child",
        description="tool dispatch",
        inputs=(),
        outputs=("root_output",),
        activation="",
        budget=2,
        tool_scope=("example_tool",),
        labels=(),
        origin="planner",
        parent_id=root.id,
        worker_capabilities=("tool-execution",),
    )
    unrelated = build_control_plane_contract(
        name="unrelated", outputs=("unrelated_output",)
    )
    contracts = {
        item.id: item for item in (root, child, recovery, tool_child, unrelated)
    }

    class Store:
        def get_all_contracts(self):
            return list(contracts.values())

        def get_contract(self, contract_id):
            return contracts.get(contract_id)

    store = Store()
    attempts = []
    available = {child.id, recovery.id}

    def claim(
        _store,
        *,
        worker_id,
        contract_id,
        candidate_publishers,
        claim_candidate,
        expected_registration_version,
    ):
        del _store, worker_id, candidate_publishers
        assert claim_candidate is not None
        assert expected_registration_version == "1"
        attempts.append(contract_id)
        if contract_id in available:
            available.remove(contract_id)
            return SimpleNamespace(contract=contracts[contract_id])
        return None

    monkeypatch.setattr("aigineering.runtime.claim_next_package", claim)
    monkeypatch.setattr(
        "aigineering.cli.run.order_application_contract_ids",
        lambda _store, ids: ids,
    )
    host = _PoolHost(
        SimpleNamespace(
            worker_id="reasoner",
            registration_version="1",
            claim_candidate=lambda **_kwargs: object(),
        )
    )
    first = _claim_for_target_pool_host(store, host, None, target_task_id=root.id)
    second = _claim_for_target_pool_host(store, host, None, target_task_id=root.id)
    assert {first.contract.id, second.contract.id} == {child.id, recovery.id}
    assert unrelated.id not in attempts

    tool_attempts = []

    def claim_tool(
        _store,
        *,
        worker_id,
        contract_id,
        candidate_publishers,
        claim_candidate,
        expected_registration_version,
    ):
        del _store, worker_id, candidate_publishers
        assert claim_candidate is not None
        assert expected_registration_version == "1"
        tool_attempts.append(contract_id)
        return (
            SimpleNamespace(contract=tool_child)
            if contract_id == tool_child.id
            else None
        )

    monkeypatch.setattr("aigineering.runtime.claim_next_package", claim_tool)
    tool_host = _PoolHost(
        SimpleNamespace(
            worker_id="tool-worker",
            registration_version="1",
            claim_candidate=lambda **_kwargs: object(),
        ),
        "tool-execution",
    )
    claimed_tool = _claim_for_target_pool_host(
        store, tool_host, None, target_task_id=root.id
    )
    assert claimed_tool.contract.id == tool_child.id
    assert child.id not in tool_attempts
    assert recovery.id not in tool_attempts
    assert unrelated.id not in tool_attempts
