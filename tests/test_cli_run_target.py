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
        "aigineering.local_fleet.order_application_contract_ids",
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


def _local_authority(store):
    from aigineering.core.domain import load_genesis
    from aigineering.local_identity import load_actor_signer

    genesis = load_genesis(store)
    signer = load_actor_signer()
    key = next(
        item
        for item in genesis.root_keys
        if item.public_key == signer.signer_id and not item.revoked
    )
    return genesis, key, signer


def _publish_signed_child(store, parent_id, name, *, budget=2):
    from aigineering.core.candidate_publisher import CandidatePublisher
    from aigineering.protocol.effect_builders import contract_declaration_effect

    genesis, key, signer = _local_authority(store)
    publisher = CandidatePublisher(store, store, genesis, key, signer)
    child = contract_from_fields(
        name=name,
        description="Signed child used by bounded target wait tests.",
        inputs=(),
        outputs=(f"{name}_output",),
        activation="",
        budget=budget,
        tool_scope=(),
        labels=(),
        origin="human",
        parent_id=parent_id,
    )
    decision = publisher.publish(
        (contract_declaration_effect(child),),
        idempotency_key=f"target-wait-child:{child.id}",
    )
    assert decision.accepted, decision.runtime_records
    return child


def _claim_signed_task(store, contract, *, worker_id, raw_output=None, lease=600):
    from conftest import hosted_worker

    from aigineering.agent.mock import MockWorker
    from aigineering.local_identity import ensure_local_runtime_publishers
    from aigineering.runtime_services import claim_next_application_package

    genesis, key, signer = _local_authority(store)
    output = raw_output or f'/exec {{"outputs":{{"{contract.outputs[0]}":"done"}}}}'
    worker = MockWorker({contract.name: output}, worker_id=worker_id)
    host = hosted_worker(
        store,
        worker,
        genesis=genesis,
        authority_key=key,
        authority_signer=signer,
    )
    claimed = claim_next_application_package(
        store,
        worker_id=host.worker_id,
        lease_seconds=lease,
        contract_id=contract.id,
        expected_registration_version=host.registration_version,
        candidate_publishers=ensure_local_runtime_publishers(store),
        claim_candidate=host.claim_candidate(
            request_id=f"target-wait-claim:{worker_id}:{contract.id}",
            contract_id=contract.id,
            lease_seconds=lease,
        ),
    )
    assert claimed is not None
    return claimed, host


def _install_controlled_wait(monkeypatch, *, on_wait=None, step=0.5):
    class Clock:
        value = 0.0

        def monotonic(self):
            return self.value

    class Waiter:
        calls = 0

        def wait(self, _revision, *, timeout):
            self.calls += 1
            if on_wait is not None:
                on_wait()
            clock.value += min(step, timeout)

    clock = Clock()
    waiter = Waiter()
    monkeypatch.setattr("aigineering.cli.run.time", clock)
    monkeypatch.setattr(
        "aigineering.cli.run.collaboration_service",
        lambda _store: waiter,
    )
    return clock, waiter


def _run_target_args(contract_id, *, wait_timeout="1", interval="0.5"):
    return [
        "run",
        "--task",
        contract_id,
        "--worker",
        "mock",
        "--wait-timeout",
        wait_timeout,
        "--interval",
        interval,
        "--json",
    ]


def test_target_run_waits_for_signed_active_descendant_until_timeout(
    monkeypatch, tmp_path
):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(
            runner,
            "active-child-parent",
            "parent_output",
            "--budget",
            "20",
            "--input",
            "missing-parent-input",
            "--activation",
            "missing-parent-input",
        )
        store = SQLiteStore(".aig/store.db")
        target = store.get_contract(target_id)
        assert target is not None
        child = _publish_signed_child(store, target_id, "active-child")
        claimed, _holder = _claim_signed_task(
            store, child, worker_id="active-child-holder"
        )
        before_parent = project_task_status(target, store)
        before_claim = store.get_claim(child.id)
        assert before_claim is not None and before_claim["status"] == "active"
        store.close()

        _clock, waiter = _install_controlled_wait(monkeypatch, step=0.5)
        result = runner.invoke(cli, _run_target_args(target_id))

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["stop_reason"] == "timeout"
        assert payload["timed_out"] is True
        assert payload["status"] == before_parent["status"]
        assert waiter.calls == 2
        persisted = SQLiteStore(".aig/store.db")
        try:
            after_parent = project_task_status(
                persisted.get_contract(target_id), persisted
            )
            assert after_parent == before_parent
            assert persisted.get_claim(child.id) == before_claim
            assert persisted.get_claim(child.id)["claim_id"] == claimed.package.claim_id
        finally:
            persisted.close()


def test_target_run_observes_external_completion_of_claimed_target(
    monkeypatch, tmp_path
):
    from aigineering.local_identity import ensure_local_runtime_publishers
    from aigineering.runtime import execute_claimed_package

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(runner, "external-completion", "report")
        store = SQLiteStore(".aig/store.db")
        target = store.get_contract(target_id)
        assert target is not None
        claimed, host = _claim_signed_task(
            store,
            target,
            worker_id="external-completion-holder",
            raw_output='/exec {"outputs":{"report":"completed elsewhere"}}',
        )
        before_claim = store.get_claim(target_id)
        assert before_claim is not None and before_claim["status"] == "active"
        store.close()

        def finish_claimed_package():
            external_store = SQLiteStore(".aig/store.db")
            try:
                result = execute_claimed_package(
                    claimed,
                    host,
                    external_store,
                    candidate_publishers=ensure_local_runtime_publishers(
                        external_store
                    ),
                )
                assert result["status"] == "accepted", result
            finally:
                external_store.close()

        _clock, waiter = _install_controlled_wait(
            monkeypatch, on_wait=finish_claimed_package, step=0.1
        )
        result = runner.invoke(cli, _run_target_args(target_id, interval="0.1"))

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert payload["stop_reason"] == "terminal"
        assert waiter.calls == 1
        persisted = SQLiteStore(".aig/store.db")
        try:
            assert (
                project_task_status(persisted.get_contract(target_id), persisted)[
                    "terminal"
                ]
                is True
            )
            assert persisted.get_claim(target_id)["status"] == "submitted"
        finally:
            persisted.close()


def test_unrelated_active_claim_does_not_suppress_target_idle(monkeypatch, tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(
            runner,
            "blocked-target",
            "blocked_output",
            "--input",
            "missing_input",
            "--activation",
            "missing_input",
        )
        unrelated_id = _create_task(runner, "unrelated-active", "other_output")
        store = SQLiteStore(".aig/store.db")
        unrelated = store.get_contract(unrelated_id)
        assert unrelated is not None
        _claimed, _holder = _claim_signed_task(
            store, unrelated, worker_id="unrelated-active-holder"
        )
        unrelated_claim = store.get_claim(unrelated_id)
        assert unrelated_claim is not None and unrelated_claim["status"] == "active"
        store.close()

        _clock, waiter = _install_controlled_wait(monkeypatch)
        result = runner.invoke(cli, _run_target_args(target_id))

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["stop_reason"] == "idle"
        assert payload["status"] == "blocked"
        assert waiter.calls == 0
        persisted = SQLiteStore(".aig/store.db")
        try:
            assert persisted.get_claim(unrelated_id) == unrelated_claim
            assert persisted.get_claim(target_id) is None
        finally:
            persisted.close()


def test_submitted_claim_does_not_suppress_target_idle(monkeypatch, tmp_path):
    from conftest import candidate_runtime

    from aigineering.core.control_plane import build_control_plane_asset
    from aigineering.local_identity import ensure_local_runtime_publishers
    from aigineering.runtime import execute_claimed_package

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        store = SQLiteStore(".aig/store.db")
        genesis, key, signer = _local_authority(store)
        ingress = candidate_runtime(
            store, genesis=genesis, actor_key=key, signer=signer
        )
        rubric = ingress.accept_asset(
            build_control_plane_asset(name="rubric", content="Review the report.")
        )
        evidence = ingress.accept_asset(
            build_control_plane_asset(name="evidence", content="Source material.")
        )
        policy = {
            "mode": "independent",
            "policy_version": "target-wait-submitted-v1",
            "required_attestations": 1,
            "verifier_capabilities": ["verify.human"],
            "rubric_asset_ids": [rubric.id],
            "evidence_asset_ids": [evidence.id],
        }
        target = contract_from_fields(
            name="submitted-target",
            description="A submitted output awaits independent review.",
            inputs=(),
            outputs=("review_report",),
            activation="",
            budget=5,
            tool_scope=(),
            labels=(),
            origin="human",
            acceptance_policy=policy,
        )
        ingress.accept_contract(target)
        claimed, host = _claim_signed_task(
            store,
            target,
            worker_id="submitted-output-holder",
            raw_output='/exec {"outputs":{"review_report":"Pending review."}}',
        )
        execution = execute_claimed_package(
            claimed,
            host,
            store,
            candidate_publishers=ensure_local_runtime_publishers(store),
        )
        assert execution["status"] == "accepted", execution
        assert project_task_status(target, store)["status"] == "submitted"
        submitted_claim = store.get_claim(target.id)
        assert submitted_claim is not None and submitted_claim["status"] == "submitted"
        store.close()

        _clock, waiter = _install_controlled_wait(monkeypatch)
        result = runner.invoke(cli, _run_target_args(target.id))

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "submitted"
        assert payload["stop_reason"] == "idle"
        assert waiter.calls == 0
        persisted = SQLiteStore(".aig/store.db")
        try:
            assert persisted.get_claim(target.id) == submitted_claim
        finally:
            persisted.close()


def test_expired_selected_claim_does_not_suppress_idle_and_lease_equality_is_live(
    monkeypatch, tmp_path
):
    from datetime import datetime, timedelta

    from aigineering.cli import run as run_cli_module

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(
            runner,
            "expired-child-parent",
            "parent_output",
            "--budget",
            "20",
            "--input",
            "missing-parent-input",
            "--activation",
            "missing-parent-input",
        )
        store = SQLiteStore(".aig/store.db")
        target = store.get_contract(target_id)
        assert target is not None
        child = _publish_signed_child(store, target_id, "expired-child")
        _claimed, _holder = _claim_signed_task(
            store, child, worker_id="expired-child-holder"
        )
        claim = store.get_claim(child.id)
        assert claim is not None and claim["status"] == "active"
        lease_until = datetime.fromisoformat(claim["lease_until"])
        assert lease_until.tzinfo is not None

        class AtLease(datetime):
            @classmethod
            def now(cls, tz=None):
                return (
                    lease_until if tz is not None else lease_until.replace(tzinfo=None)
                )

        class AfterLease(datetime):
            @classmethod
            def now(cls, tz=None):
                observed = lease_until + timedelta(microseconds=1)
                return observed if tz is not None else observed.replace(tzinfo=None)

        monkeypatch.setattr(run_cli_module, "datetime", AtLease)
        assert run_cli_module._has_active_unfinished_tree_claim(store, target_id)
        monkeypatch.setattr(run_cli_module, "datetime", AfterLease)
        assert not run_cli_module._has_active_unfinished_tree_claim(store, target_id)
        store.close()

        _clock, waiter = _install_controlled_wait(monkeypatch)
        result = runner.invoke(cli, _run_target_args(target_id))

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["stop_reason"] == "idle"
        assert waiter.calls == 0
        persisted = SQLiteStore(".aig/store.db")
        try:
            assert persisted.get_claim(child.id) == claim
        finally:
            persisted.close()


def test_target_run_joins_admitted_worker_after_deadline(monkeypatch, tmp_path):
    from aigineering.agent.mock import MockWorker

    class Clock:
        value = 0.0

        def monotonic(self):
            return self.value

    clock = Clock()

    class SlowMockWorker(MockWorker):
        def invoke(self, contract, disclosed_assets):
            clock.value = 2.0
            return super().invoke(contract, disclosed_assets)

    worker = SlowMockWorker(worker_id="slow-target-worker")
    monkeypatch.setattr("aigineering.cli.run.time", clock)
    monkeypatch.setattr("aigineering.cli.run.build_worker", lambda *_a, **_kw: worker)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=str(tmp_path)):
        initialized = runner.invoke(cli, ["domain", "init"])
        assert initialized.exit_code == 0, initialized.output
        target_id = _create_task(runner, "slow-target", "slow_output")
        result = runner.invoke(cli, _run_target_args(target_id, wait_timeout="1"))

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert payload["stop_reason"] == "terminal"
        assert payload.get("timed_out") is None
