"""Local heterogeneous fleet tests over independent SQLite connections."""

from __future__ import annotations

import json
from threading import Barrier, Event

import pytest

from aigineering.cli._candidate import commit_local_effects, require_accepted
from aigineering.core.control_plane import (
    bind_contract_label_assets,
    build_control_plane_asset,
    build_control_plane_contract,
)
from aigineering.core.capability_descriptors import create_tool_descriptor
from aigineering.core.ids import hash_contract_current
from aigineering.core.runtime_projection import RuntimeProjection
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.core.tools import ToolRegistry
from aigineering.core.worker_routing import WorkerRegistration
from aigineering.fleet_config import (
    FleetWorkerSpec,
    build_fleet_worker,
    load_fleet_config,
)
from aigineering.local_fleet import FleetHost, run_local_cycle, run_local_fleet
import aigineering.local_fleet as local_fleet_module
from aigineering.local_identity import ensure_local_domain, ensure_local_worker_host
from aigineering.protocol.effect_builders import (
    asset_proposal_effect,
    contract_declaration_effect,
)
from aigineering.protocol.types import Candidate, Contract, ToolSpec
from aigineering.agent.tool_worker import ToolWorker
from aigineering.runtime_services import RedisSyncPending


def test_fleet_result_includes_capture_warning_completed_during_executor_join(
    monkeypatch,
):
    class FakeFuture:
        def cancelled(self):
            return False

        def done(self):
            return False

        def exception(self):
            return None

    class DelayedExecutor:
        def __init__(self, max_workers):
            self.pending = []

        def __enter__(self):
            return self

        def submit(self, function, *args):
            self.pending.append((function, args))
            return FakeFuture()

        def __exit__(self, *_exc):
            # Model a slot that finishes while the real executor joins after stop.
            for function, args in self.pending:
                function(*args)

    class FakeStore:
        def __init__(self, _path):
            pass

        def get_contract(self, contract_id):
            return type("Contract", (), {"id": contract_id})()

        def close(self):
            pass

        def get_runtime_revision(self):
            return 1

    class FakeProjection:
        def __init__(self, *_args):
            pass

        def contract_view(self, _contract):
            return type("View", (), {"terminal": "complete"})()

    monkeypatch.setattr(local_fleet_module, "ThreadPoolExecutor", DelayedExecutor)
    monkeypatch.setattr(local_fleet_module, "SQLiteStore", FakeStore)
    monkeypatch.setattr(local_fleet_module, "RuntimeProjection", FakeProjection)
    monkeypatch.setattr(local_fleet_module, "require_redis", lambda: None)
    monkeypatch.setattr(
        local_fleet_module, "ensure_local_runtime_publishers", lambda _s: {}
    )
    monkeypatch.setattr(local_fleet_module, "default_completion_registry", lambda: {})
    monkeypatch.setattr(
        local_fleet_module, "run_runtime_maintenance_step", lambda *_a, **_k: None
    )
    monkeypatch.setattr(local_fleet_module, "sync_runtime", lambda _s: None)
    monkeypatch.setattr(
        local_fleet_module,
        "collaboration_service",
        lambda _s: type("Collaboration", (), {"wait": lambda *_a, **_k: None})(),
    )

    def finish_slot(
        _db, _host, _stop, _errors, _interval, _target, warnings, _failure_details
    ):
        warnings.append("capture_unavailable")

    monkeypatch.setattr(local_fleet_module, "_run_slot", finish_slot)
    result = run_local_fleet(
        "unused.sqlite",
        (FleetHost(object()),),
        target_contract_id="task:fleet-test",
    )
    assert result.completed is True
    assert result.capture_warnings == ("capture_unavailable",)


@pytest.mark.parametrize(
    ("mode", "terminal", "timeout", "expected_status"),
    [
        ("terminal", "complete", 2, "complete"),
        ("timeout", None, 0, "timed_out"),
        ("first_error", None, 2, "worker_error"),
    ],
)
def test_late_unexpected_future_is_reported_safely_after_executor_join(
    monkeypatch, mode, terminal, timeout, expected_status
):
    late_secret = "PRIVATE_LATE_EXCEPTION_SENTINEL"
    first_secret = "PRIVATE_FIRST_EXCEPTION_SENTINEL"

    class Future:
        def __init__(self, *, done=False, error=None):
            self._done = done
            self._error = error

        def done(self):
            return self._done

        def cancelled(self):
            return False

        def exception(self):
            return self._error

        def finish(self, error=None):
            self._done = True
            self._error = error

    class JoiningExecutor:
        def __init__(self, max_workers):
            self.pending = []

        def __enter__(self):
            return self

        def submit(self, function, *args):
            host = args[1]
            if mode == "first_error" and host.kind == "first":
                return Future(done=True, error=RuntimeError(first_secret))
            future = Future()
            self.pending.append((function, args, future))
            return future

        def __exit__(self, *_exc):
            for function, args, future in self.pending:
                try:
                    function(*args)
                except BaseException as error:
                    future.finish(error)
                else:
                    future.finish()

    class FakeStore:
        def __init__(self, _path):
            pass

        def get_contract(self, contract_id):
            return type("Contract", (), {"id": contract_id})()

        def get_runtime_revision(self):
            return 1

        def close(self):
            pass

    class FakeProjection:
        def __init__(self, *_args):
            pass

        def contract_view(self, _contract):
            return type("View", (), {"terminal": terminal})()

    class Waiter:
        def wait(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(local_fleet_module, "ThreadPoolExecutor", JoiningExecutor)
    monkeypatch.setattr(local_fleet_module, "SQLiteStore", FakeStore)
    monkeypatch.setattr(local_fleet_module, "RuntimeProjection", FakeProjection)
    monkeypatch.setattr(local_fleet_module, "require_redis", lambda: None)
    monkeypatch.setattr(
        local_fleet_module, "ensure_local_runtime_publishers", lambda _store: {}
    )
    monkeypatch.setattr(local_fleet_module, "default_completion_registry", lambda: {})
    monkeypatch.setattr(
        local_fleet_module, "run_runtime_maintenance_step", lambda *_a, **_k: None
    )
    monkeypatch.setattr(local_fleet_module, "sync_runtime", lambda _store: None)
    monkeypatch.setattr(
        local_fleet_module, "collaboration_service", lambda _store: Waiter()
    )

    def late_slot(*_args):
        raise RuntimeError(late_secret)

    monkeypatch.setattr(local_fleet_module, "_run_slot", late_slot)
    hosts = tuple(
        FleetHost(type("Host", (), {"kind": kind})())
        for kind in (("first", "late") if mode == "first_error" else ("late",))
    )
    result = run_local_fleet(
        "unused.sqlite",
        hosts,
        target_contract_id="task:late-future",
        timeout=timeout,
        poll_interval=0.001,
    )

    assert result.status == expected_status
    assert hasattr(result, "worker_failure_details")
    assert any(
        detail.get("kind") == "slot_error"
        and detail.get("category") == "unexpected_exception"
        for detail in result.worker_failure_details
    )
    assert sum(
        detail.get("kind") == "slot_error" for detail in result.worker_failure_details
    ) == (2 if mode == "first_error" else 1)
    assert late_secret not in repr(result)
    assert first_secret not in repr(result)


def test_invocation_diagnostic_failure_does_not_echo_unproven_claim_ids(monkeypatch):
    class Package:
        claim_id = "lease:" + "a" * 64
        package_id = "pkg:" + "b" * 64
        claim_epoch = 1

    claimed = type(
        "Claimed",
        (),
        {
            "contract": type("Contract", (), {"id": "task:unavailable"})(),
            "package": Package(),
            "worker_id": "worker:unavailable",
        },
    )()

    def unavailable(*_args):
        raise ValueError("PRIVATE_DIAGNOSTIC_EXCEPTION")

    monkeypatch.setattr(
        "aigineering.run_failure_diagnostics.task_invocation_failure_binding",
        unavailable,
    )
    detail = local_fleet_module._invocation_failure_detail(object(), claimed)
    assert detail == {
        "kind": "invocation_failure",
        "status": "diagnostic_unavailable",
        "category": "diagnostic_unavailable",
    }
    assert "task:unavailable" not in repr(detail)
    assert "lease:" not in repr(detail)
    assert "PRIVATE_DIAGNOSTIC_EXCEPTION" not in repr(detail)


def test_persistent_fleet_keeps_tuple_and_uses_shared_safe_failure_formatter(
    monkeypatch,
):
    failed = Event()
    stop_event = Event()
    claim_id = "lease:" + "c" * 64
    contract_id = "task:persistent-failure"
    details = {
        "kind": "invocation_failure",
        "status": "invocation_failed",
        "contract_id": contract_id,
        "claim_id": claim_id,
        "package_id": "pkg:" + "d" * 64,
        "worker_id": "worker:persistent",
        "category": "provider_error",
        "status_code": 503,
    }
    claimed = type(
        "Claimed",
        (),
        {
            "contract": type("Contract", (), {"id": contract_id})(),
            "package": object(),
            "worker_id": "worker:persistent",
        },
    )()

    class FakeStore:
        def __init__(self, _path):
            pass

        def get_runtime_revision(self):
            return 0

        def close(self):
            pass

    class Permit:
        def release(self):
            pass

    class Gate:
        closed = False

        def admit(self):
            return None if self.closed else Permit()

        def close(self):
            self.closed = True

    class Waiter:
        def wait(self, *_args, **_kwargs):
            return None

    attempts = 0

    def claim_one(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return claimed if attempts == 1 else None

    def fail_invocation(*_args, **_kwargs):
        failed.set()
        raise local_fleet_module.WorkerInvocationError("PRIVATE_EXCEPTION_BODY")

    monkeypatch.setattr(local_fleet_module, "SQLiteStore", FakeStore)
    monkeypatch.setattr(
        local_fleet_module, "ensure_local_runtime_publishers", lambda _store: {}
    )
    monkeypatch.setattr(local_fleet_module, "require_redis", lambda: None)
    monkeypatch.setattr(local_fleet_module, "_claim_one", claim_one)
    monkeypatch.setattr(local_fleet_module, "execute_captured_package", fail_invocation)
    monkeypatch.setattr(
        local_fleet_module,
        "_invocation_failure_detail",
        lambda _store, _claimed: details,
    )
    monkeypatch.setattr(local_fleet_module, "sync_runtime", lambda _store: None)
    monkeypatch.setattr(
        local_fleet_module, "collaboration_service", lambda _store: Waiter()
    )
    errors = local_fleet_module.run_persistent_local_fleet(
        "unused.sqlite",
        (FleetHost(object()),),
        gate=Gate(),
        stop_event=stop_event,
        stop_requested=failed.is_set,
        poll_interval=0.001,
    )
    assert isinstance(errors, tuple)
    assert len(errors) == 1
    assert contract_id in errors[0] and claim_id in errors[0]
    assert "provider_error" in errors[0]
    assert "PRIVATE_EXCEPTION_BODY" not in errors[0]


def test_persistent_fleet_surfaces_failure_when_future_is_done_before_loop(
    monkeypatch,
):
    failure = RuntimeError("PRIVATE_PERSISTENT_SLOT_SENTINEL")

    class AlreadyFailedFuture:
        def done(self):
            return True

        def cancelled(self):
            return False

        def exception(self):
            return failure

    class AlreadyDoneExecutor:
        def __init__(self, max_workers):
            assert max_workers == 1

        def __enter__(self):
            return self

        def submit(self, *_args):
            return AlreadyFailedFuture()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(local_fleet_module, "ThreadPoolExecutor", AlreadyDoneExecutor)
    with pytest.raises(RuntimeError) as raised:
        local_fleet_module.run_persistent_local_fleet(
            "unused.sqlite",
            (FleetHost(object()),),
            gate=local_fleet_module.PullAdmissionGate(),
            stop_event=Event(),
            stop_requested=lambda: False,
        )
    assert raised.value is failure


class _ConcurrentWorker:
    def __init__(self, worker_id: str, capability: str, barrier: Barrier) -> None:
        self.worker_id = worker_id
        self.capability = capability
        self.barrier = barrier

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(
            self.worker_id,
            capabilities=() if self.capability is None else (self.capability,),
            capacity=1,
            profile_id=f"test:{self.capability}",
        )

    def invoke(self, contract: Contract, disclosed_assets: list) -> Candidate:
        del disclosed_assets
        self.barrier.wait(timeout=2)
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps(
                {
                    "outputs": {
                        name: f"{self.worker_id}:{name}" for name in contract.outputs
                    }
                }
            ),
        )


class _RepairingWorker:
    def __init__(self) -> None:
        self.worker_id = "worker:repairing"
        self.disclosures: list[tuple[str, ...]] = []

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(self.worker_id, capabilities=("text.extract",))

    def invoke(self, contract: Contract, disclosed_assets: list) -> Candidate:
        self.disclosures.append(tuple(asset.name for asset in disclosed_assets))
        if contract.origin != "recovery":
            return Candidate(
                worker_id=self.worker_id,
                raw_output='/exec {"outputs":{"wrong_name":"malformed"}}',
            )
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps(
                {"outputs": {name: "repaired with skill" for name in contract.outputs}}
            ),
        )


class _CycleWorker:
    def __init__(self, worker_id="worker:cycle", capability="text.extract", *, delay=0):
        self.worker_id = worker_id
        self.capability = capability
        self.delay = delay
        self.invocations = []

    def registration(self):
        return WorkerRegistration(
            self.worker_id,
            capabilities=() if self.capability is None else (self.capability,),
            profile_id=f"test:{self.capability}",
        )

    def invoke(self, contract, disclosed_assets):
        del disclosed_assets
        self.invocations.append(contract.id)
        if self.delay:
            import time

            time.sleep(self.delay)
        return Candidate(
            worker_id=self.worker_id,
            raw_output="/exec "
            + json.dumps(
                {"outputs": {name: f"accepted:{name}" for name in contract.outputs}}
            ),
        )


class _PlanCycleWorker(_CycleWorker):
    def invoke(self, contract, disclosed_assets):
        del disclosed_assets
        self.invocations.append(contract.id)
        if contract.origin != "plan":
            return Candidate(
                worker_id=self.worker_id,
                raw_output='/plan {"reason":"decompose the research task"}',
            )
        return super().invoke(contract, ())


def _prepare_local_cycle_case(tmp_path, monkeypatch, worker, contracts=()):
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIG_ACTOR_KEY_FILE", str(tmp_path / "root.ed25519"))
    monkeypatch.setattr(local_fleet_module, "require_redis", lambda: None)
    monkeypatch.setattr(local_fleet_module, "sync_runtime", lambda _store: None)
    monkeypatch.setattr("aigineering.local_publication.require_redis", lambda: None)
    monkeypatch.setattr(
        "aigineering.local_publication.sync_runtime", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr("aigineering.application.require_redis", lambda *_args: None)
    monkeypatch.setattr(
        "aigineering.runtime_services.sync_runtime", lambda *_args, **_kwargs: None
    )
    db_path = str(tmp_path / "cycle.sqlite")
    store = SQLiteStore(db_path)
    ensure_local_domain(store)
    if contracts:
        require_accepted(
            commit_local_effects(
                store,
                tuple(contract_declaration_effect(item) for item in contracts),
                idempotency_key="cycle-test-contracts:" + worker.worker_id,
            )
        )
    host = ensure_local_worker_host(store, worker)
    return db_path, store, host


def _child_contract(parent, *, name, output, capability):
    fields = {
        "name": name,
        "description": f"Produce {output}.",
        "inputs": (),
        "outputs": (output,),
        "activation": "",
        "budget": 1,
        "tool_scope": (),
        "labels": (),
        "worker_capabilities": (capability,),
        "worker_pools": (),
        "origin": "plan",
        "parent_id": parent.id,
    }
    return Contract(id=hash_contract_current(**fields), **fields)


class _ParallelToolOrchestrator:
    worker_id = "worker:parallel-orchestrator"

    def __init__(self) -> None:
        self.disclosures: list[tuple[str, ...]] = []

    def registration(self) -> WorkerRegistration:
        return WorkerRegistration(self.worker_id, capabilities=("orchestrate",))

    def invoke(self, contract: Contract, disclosed_assets: list) -> Candidate:
        names = tuple(asset.name for asset in disclosed_assets)
        self.disclosures.append(names)
        if contract.origin != "continuation":
            return Candidate(
                worker_id=self.worker_id,
                raw_output="/parallel_tool "
                + json.dumps(
                    {
                        "calls": [
                            {"id": "search", "name": "search", "args": {"q": "a"}},
                            {"id": "lookup", "name": "lookup", "args": {"q": "b"}},
                        ],
                        "join": "all",
                    }
                ),
            )
        return Candidate(
            worker_id=self.worker_id,
            raw_output='/exec {"outputs":{"report":"joined observations"}}',
        )


def test_fleet_config_parses_explicit_worker_profiles(tmp_path):
    config = tmp_path / "workers.toml"
    config.write_text(
        """
[fleet]
db_path = ".aig/test.db"
poll_interval = 0.05

[[workers]]
id = "cheap"
kind = "llm"
model = "cheap-model"
capabilities = ["text.extract"]
pools = ["economy"]
capacity = 4
max_output_tokens = 3072
thinking_mode = "disabled"
effect_capabilities = ["asset.attest"]

[[workers]]
id = "tools"
kind = "tool"
tool_registry = "examples.ai4s.tools:build_registry"
capacity = 2
""".strip(),
        encoding="utf-8",
    )

    parsed = load_fleet_config(config)

    assert parsed.poll_interval == 0.05
    assert parsed.workers[0].worker_id == "cheap"
    assert parsed.workers[0].capabilities == ("text.extract",)
    assert parsed.workers[0].capacity == 4
    assert parsed.workers[0].max_output_tokens == 3072
    assert parsed.workers[0].thinking_mode == "disabled"
    assert parsed.workers[0].effect_capabilities == ("asset.attest",)
    assert parsed.workers[1].kind == "tool"


@pytest.mark.parametrize(
    ("field", "value"),
    (("capacity", '"2"'), ("max_retries", "true"), ("timeout", '"2"')),
)
def test_fleet_config_rejects_coerced_scalar_types(tmp_path, field, value):
    config = tmp_path / "invalid-workers.toml"
    config.write_text(
        f'[[workers]]\nid = "worker"\nkind = "llm"\nmodel = "model"\n{field} = {value}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=field):
        load_fleet_config(config)


@pytest.mark.parametrize(
    "field_value",
    (("timeout", "0.0"), ("timeout", "nan"), ("timeout", "inf"), ("max_retries", "-1")),
)
def test_fleet_config_rejects_invalid_retry_and_timeout(field_value, tmp_path):
    field, value = field_value
    config = tmp_path / "invalid-workers.toml"
    config.write_text(
        f'[[workers]]\nid = "worker"\nkind = "llm"\nmodel = "model"\n{field} = {value}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_fleet_config(config)


def test_fleet_worker_preserves_fractional_timeout():
    worker = build_fleet_worker(
        FleetWorkerSpec(worker_id="worker", kind="llm", model="model", timeout=0.5)
    )

    assert worker._timeout == 0.5


def test_local_fleet_executes_specialized_tasks_concurrently(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_path = str(tmp_path / "fleet.db")
    store = SQLiteStore(db_path)
    ensure_local_domain(store)
    unrelated = build_control_plane_contract(
        name="unrelated-workspace-root",
        description="Must not be claimed by a fleet assigned to another root.",
        outputs=("unrelated_result",),
        worker_capabilities=("text.extract",),
    )
    require_accepted(
        commit_local_effects(
            store,
            (contract_declaration_effect(unrelated),),
            idempotency_key="fleet-unrelated-root",
        )
    )
    root = build_control_plane_contract(
        name="root",
        outputs=("cheap_result", "deep_result"),
        budget=2,
        worker_capabilities=("root.orchestrator",),
        delegation_capabilities=("text.extract", "reasoning.deep"),
    )

    def child(name: str, output: str, capability: str) -> Contract:
        fields = {
            "name": name,
            "description": f"Produce {output}",
            "inputs": (),
            "outputs": (output,),
            "activation": "",
            "budget": 1,
            "tool_scope": (),
            "labels": (),
            "worker_capabilities": (capability,),
            "worker_pools": (),
            "origin": "plan",
            "parent_id": root.id,
        }
        return Contract(id=hash_contract_current(**fields), **fields)

    cheap_task = child("cheap", "cheap_result", "text.extract")
    deep_task = child("deep", "deep_result", "reasoning.deep")
    require_accepted(
        commit_local_effects(
            store,
            tuple(
                contract_declaration_effect(item)
                for item in (root, cheap_task, deep_task)
            ),
            idempotency_key="fleet-concurrency-setup",
        )
    )
    barrier = Barrier(2)
    cheap = _ConcurrentWorker("worker:cheap", "text.extract", barrier)
    deep = _ConcurrentWorker("worker:deep", "reasoning.deep", barrier)
    hosts = tuple(
        FleetHost(ensure_local_worker_host(store, worker)) for worker in (cheap, deep)
    )
    store.close()

    result = run_local_fleet(
        db_path,
        hosts,
        target_contract_id=root.id,
        timeout=5,
        poll_interval=0.01,
    )

    assert result.completed is True
    assert result.status == "complete"
    reopened = SQLiteStore(db_path)
    try:
        assert reopened.get_assets_by_name("cheap_result")
        assert reopened.get_assets_by_name("deep_result")
        assert not reopened.get_assets_by_name("unrelated_result")
        assert not any(
            record.payload.get("contract_id") == unrelated.id
            for _, record in reopened.scan_runtime_records(record_type="claim.granted")
        )
    finally:
        reopened.close()


@pytest.mark.parametrize("concurrent_recovery", [False, True])
def test_local_fleet_recovers_bad_output_with_frozen_skill_context(
    tmp_path, monkeypatch, concurrent_recovery
):
    if concurrent_recovery:
        recovery_barrier = Barrier(2)
        original_commit = SQLiteStore.commit_ingress_batch

        def synchronized_recovery(store, *args, **kwargs):
            if any(
                record.record_type == "contract.declared"
                and record.payload["contract"].get("origin") == "recovery"
                for record in kwargs.get("runtime_records", ())
            ):
                recovery_barrier.wait(timeout=5)
            return original_commit(store, *args, **kwargs)

        monkeypatch.setattr(SQLiteStore, "commit_ingress_batch", synchronized_recovery)
    monkeypatch.chdir(tmp_path)
    db_path = str(tmp_path / "recovery-fleet.db")
    store = SQLiteStore(db_path)
    ensure_local_domain(store)
    skill = build_control_plane_asset(
        name="_skill_content_extract_evidence",
        content="Always emit exactly the declared evidence output.",
        origin="skill",
        trust_tier="configured",
        allow_protected=True,
    )
    require_accepted(
        commit_local_effects(
            store,
            (asset_proposal_effect(skill),),
            idempotency_key="fleet-recovery-skill",
        )
    )
    stored_skill = store.get_assets_by_name(skill.name)[0]
    root = build_control_plane_contract(
        name="recoverable_root",
        outputs=("evidence",),
        budget=2,
        labels=(skill.name,),
        context_asset_ids=(stored_skill.id,),
        worker_capabilities=("root.orchestrator",),
        delegation_capabilities=("text.extract",),
    )
    fields = {
        "name": "extract_evidence",
        "description": "Extract one evidence fact using the bound Skill.",
        "inputs": (),
        "outputs": ("evidence",),
        "activation": "",
        "budget": 1,
        "tool_scope": (),
        "labels": (skill.name,),
        "context_asset_ids": (stored_skill.id,),
        "worker_capabilities": ("text.extract",),
        "worker_pools": (),
        "origin": "plan",
        "parent_id": root.id,
    }
    task = Contract(id=hash_contract_current(**fields), **fields)
    task = bind_contract_label_assets(task, store)
    require_accepted(
        commit_local_effects(
            store,
            tuple(contract_declaration_effect(item) for item in (root, task)),
            idempotency_key="fleet-recovery-tasks",
        )
    )
    worker = _RepairingWorker()
    host = FleetHost(ensure_local_worker_host(store, worker))
    store.close()

    result = run_local_fleet(
        db_path,
        (host,),
        target_contract_id=root.id,
        timeout=5,
        poll_interval=0.01,
    )

    assert result.completed is True
    reopened = SQLiteStore(db_path)
    try:
        recovery = next(
            contract
            for contract in reopened.get_all_contracts()
            if contract.origin == "recovery"
        )
        assert recovery.labels == task.labels
        assert recovery.context_asset_ids == task.context_asset_ids
        assert reopened.get_assets_by_name("evidence")[0].created_by == recovery.id
        assert reopened.scan_runtime_records(record_type="candidate.rejected")
        assert reopened.scan_runtime_records(
            record_type="candidate_rejection.recovery_scheduled"
        )
        before_traces = {entry.id: entry for entry in reopened.get_all()}
        if concurrent_recovery:
            recovery_records = [
                record
                for _, record in reopened.scan_runtime_records(
                    record_type="trace.recorded"
                )
                if record.payload["trace"]["event_type"] == "recovery_scheduled"
            ]
            assert len(recovery_records) == 2
            assert (
                len({record.payload["trace"]["id"] for record in recovery_records}) == 1
            )
        before_digest = reopened.runtime_materialization_digest()
        rebuilt_digest = reopened.rebuild_runtime_materializations()
        assert rebuilt_digest == before_digest
        assert {entry.id: entry for entry in reopened.get_all()} == before_traces
        rebuilt_recovery = reopened.get_contract(recovery.id)
        assert rebuilt_recovery is not None
        assert rebuilt_recovery.labels == task.labels
        assert reopened.get_assets_by_name("evidence")[0].created_by == recovery.id
    finally:
        reopened.close()
    assert any(skill.name in names for names in worker.disclosures)
    assert any(
        skill.name in names and any(name.startswith("_fail_context_") for name in names)
        for names in worker.disclosures
    )


def test_parallel_tool_method_runs_calls_concurrently_then_joins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_path = str(tmp_path / "parallel-tools.db")
    store = SQLiteStore(db_path)
    ensure_local_domain(store)
    registry = ToolRegistry()
    barrier = Barrier(2)

    def tool_result(args):
        barrier.wait(timeout=2)
        return f"result:{args['q']}"

    for name in ("search", "lookup"):
        registry.register(
            ToolSpec(name=name, description=name, input_schema={"type": "object"}),
            tool_result,
        )
    descriptors = tuple(
        create_tool_descriptor(name, name, {"type": "object"})
        for name in ("search", "lookup")
    )
    require_accepted(
        commit_local_effects(
            store,
            tuple(asset_proposal_effect(asset) for asset in descriptors),
            idempotency_key="parallel-tool-descriptors",
        )
    )
    root = build_control_plane_contract(
        name="parallel_research",
        outputs=("report",),
        budget=4,
        tool_scope=("search", "lookup"),
        worker_capabilities=("orchestrate",),
    )
    require_accepted(
        commit_local_effects(
            store,
            (contract_declaration_effect(root),),
            idempotency_key="parallel-tool-root",
        )
    )
    orchestrator = _ParallelToolOrchestrator()
    tool_worker = ToolWorker(registry, worker_id="worker:parallel-tools", capacity=2)
    hosts = (
        FleetHost(ensure_local_worker_host(store, orchestrator)),
        FleetHost(ensure_local_worker_host(store, tool_worker), capacity=2),
    )
    store.close()

    result = run_local_fleet(
        db_path,
        hosts,
        target_contract_id=root.id,
        timeout=5,
        poll_interval=0.01,
    )

    assert result.completed is True
    reopened = SQLiteStore(db_path)
    try:
        tool_items = [
            contract
            for contract in reopened.get_all_contracts()
            if "plugin:parallel_tool_item" in contract.labels
        ]
        assert len(tool_items) == 2
        assert all(
            RuntimeProjection(reopened, reopened).contract_view(item).terminal
            == "complete"
            for item in tool_items
        )
        continuation = next(
            contract
            for contract in reopened.get_all_contracts()
            if "plugin:parallel_tool.continuation" in contract.labels
        )
        assert continuation.activation == " AND ".join(continuation.inputs)
        assert reopened.get_assets_by_name("report")
    finally:
        reopened.close()
    assert any(
        len([name for name in names if name.startswith("tool_observation_")]) == 2
        for names in orchestrator.disclosures
    )


def test_fleet_json_action_mode_rejects_tool_registry_before_loading():
    with pytest.raises(ValueError, match="cannot be combined with tool_registry"):
        FleetWorkerSpec(
            worker_id="json-with-tools",
            kind="llm",
            model="unused",
            response_format="json_object",
            tool_registry="unimportable:factory",
        )


def test_local_cycle_skips_ineligible_slot_then_stops_after_one_claim(monkeypatch):
    class Store:
        def get_runtime_revision(self):
            return 4

        def scan_runtime_records(self, *, after_revision, through_revision):
            return ()

        def get_by_contract(self, _contract_id):
            return ()

        def get_contract(self, contract_id):
            return type("ContractRef", (), {"id": contract_id})()

    class Host:
        worker_id = "cycle-worker"
        registration_version = "registration-v1"

        def claim_candidate(self, *, request_id, contract_id=None):
            return {"request_id": request_id, "contract_id": contract_id}

    store = Store()
    claimed = []
    executed = []
    monkeypatch.setattr(local_fleet_module, "require_redis", lambda: None)
    monkeypatch.setattr(
        local_fleet_module, "ensure_local_runtime_publishers", lambda _store: object()
    )
    monkeypatch.setattr(
        local_fleet_module, "default_completion_registry", lambda: object()
    )
    monkeypatch.setattr(local_fleet_module, "sync_runtime", lambda _store: None)
    maintenance = type(
        "Maintenance",
        (),
        {
            "worker_failures": (),
            "rejected_submissions": (),
            "task_completions": (),
        },
    )()
    monkeypatch.setattr(
        local_fleet_module,
        "run_runtime_maintenance_step",
        lambda *_a, **_kw: maintenance,
    )

    def claim(_store, _host, _publishers, **kwargs):
        claimed.append(kwargs)
        if len(claimed) == 1:
            return None
        return type(
            "Claim",
            (),
            {"contract": type("ContractRef", (), {"id": "task-eligible"})()},
        )()

    def execute(package, host, _store, **_kwargs):
        executed.append((package.contract.id, host.worker_id))
        return {"status": "submitted"}

    monkeypatch.setattr(local_fleet_module, "_claim_cycle_slot", claim)
    monkeypatch.setattr(local_fleet_module, "execute_captured_package", execute)
    monkeypatch.setattr(
        local_fleet_module,
        "project_task_status",
        lambda contract, _store: {"status": "running", "terminal": False},
    )

    result = run_local_cycle(
        store,
        (FleetHost(Host(), capacity=3),),
        db_path="fixture.db",
    )

    assert len(claimed) == 2
    assert len(executed) == 1
    assert executed == [("task-eligible", "cycle-worker")]
    assert result.stop_reason == "cycle_boundary"
    assert result.contract_id == "task-eligible"


def test_local_cycle_uses_signed_host_and_exact_target_tree(tmp_path, monkeypatch):
    worker = _CycleWorker()
    root = build_control_plane_contract(
        name="cycle-root",
        outputs=("final",),
        budget=2,
        worker_capabilities=("root.orchestrator",),
        delegation_capabilities=("text.extract",),
    )
    child = _child_contract(
        root, name="cycle-child", output="draft", capability="text.extract"
    )
    unrelated = build_control_plane_contract(
        name="cycle-unrelated",
        outputs=("outside",),
        budget=1,
        worker_capabilities=("text.extract",),
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, worker, (root, child, unrelated)
    )
    try:
        result = run_local_cycle(
            store,
            (FleetHost(host, capacity=2, required_contract_capability="text.extract"),),
            db_path=db_path,
            target_contract_id=root.id,
        )
        assert worker.invocations == [child.id]
        granted = [
            record.payload["contract_id"]
            for _revision, record in store.scan_runtime_records(
                record_type="claim.granted"
            )
        ]
        assert granted == [child.id]
        assert unrelated.id not in granted
        assert result.contract_id == root.id
        assert result.status == "blocked_capability"
        store.rebuild_runtime_materializations()
        assert (
            store.get_worker_registration(host.worker_id).version
            == host.registration_version
        )
    finally:
        store.close()
    reopened = SQLiteStore(db_path)
    try:
        assert reopened.get_claim(child.id) is not None
        assert (
            reopened.get_worker_registration(host.worker_id).version
            == host.registration_version
        )
    finally:
        reopened.close()


def test_local_cycle_capacity_claims_only_one_real_task(tmp_path, monkeypatch):
    worker = _CycleWorker()
    tasks = (
        build_control_plane_contract(
            name="cycle-capacity-one",
            outputs=("result_one",),
            worker_capabilities=("text.extract",),
        ),
        build_control_plane_contract(
            name="cycle-capacity-two",
            outputs=("result_two",),
            worker_capabilities=("text.extract",),
        ),
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, worker, tasks
    )
    try:
        result = run_local_cycle(store, (FleetHost(host, capacity=2),), db_path=db_path)
        granted = [
            record.payload["contract_id"]
            for _revision, record in store.scan_runtime_records(
                record_type="claim.granted"
            )
        ]
        assert len(granted) == len(worker.invocations) == 1
        assert granted == worker.invocations
        assert granted[0] in {task.id for task in tasks}
        unclaimed = next(task for task in tasks if task.id != granted[0])
        assert store.get_claim(unclaimed.id) is None
        assert result.stop_reason == "terminal"
    finally:
        store.close()


def test_local_cycle_distinct_hosts_skip_ineligible_then_claim_once(
    tmp_path, monkeypatch
):
    first_worker = _CycleWorker("worker:cycle-first", "text.reason")
    eligible_worker = _CycleWorker("worker:cycle-second", "text.extract")
    later_worker = _CycleWorker("worker:cycle-third", "text.extract")
    tasks = (
        build_control_plane_contract(
            name="cycle-heterogeneous-one",
            outputs=("result_one",),
            worker_capabilities=("text.extract",),
        ),
        build_control_plane_contract(
            name="cycle-heterogeneous-two",
            outputs=("result_two",),
            worker_capabilities=("text.extract",),
        ),
    )
    db_path, store, first_host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, first_worker, tasks
    )
    eligible_host = ensure_local_worker_host(store, eligible_worker)
    later_host = ensure_local_worker_host(store, later_worker)
    try:
        result = run_local_cycle(
            store,
            (
                FleetHost(
                    first_host,
                    required_contract_capability="text.extract",
                ),
                FleetHost(
                    eligible_host,
                    required_contract_capability="text.extract",
                ),
                FleetHost(
                    later_host,
                    required_contract_capability="text.extract",
                ),
            ),
            db_path=db_path,
        )
        grants = [
            record.payload
            for _revision, record in store.scan_runtime_records(
                record_type="claim.granted"
            )
        ]
        assert first_worker.invocations == []
        assert len(eligible_worker.invocations) == 1
        assert later_worker.invocations == []
        assert len(grants) == 1
        assert grants[0]["contract_id"] == eligible_worker.invocations[0]
        assert grants[0]["worker_id"] == eligible_worker.worker_id
        assert grants[0]["contract_id"] in {task.id for task in tasks}
        unclaimed = next(task for task in tasks if task.id != grants[0]["contract_id"])
        assert store.get_claim(unclaimed.id) is None
        assert result.stop_reason == "terminal"
    finally:
        store.close()


def test_local_cycle_invocation_failure_does_not_claim_later_slot(
    tmp_path, monkeypatch
):
    worker = _CycleWorker()
    tasks = (
        build_control_plane_contract(
            name="cycle-failed-first",
            outputs=("failed_result",),
            worker_capabilities=("text.extract",),
        ),
        build_control_plane_contract(
            name="cycle-after-failure",
            outputs=("later_result",),
            worker_capabilities=("text.extract",),
        ),
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, worker, tasks
    )
    executions = []

    def fail_first(package, *_args, **_kwargs):
        executions.append(package.contract.id)
        raise local_fleet_module.WorkerInvocationError("fixture invocation failure")

    monkeypatch.setattr(local_fleet_module, "execute_captured_package", fail_first)
    try:
        result = run_local_cycle(store, (FleetHost(host, capacity=2),), db_path=db_path)
        grants = [
            record.payload["contract_id"]
            for _revision, record in store.scan_runtime_records(
                record_type="claim.granted"
            )
        ]
        assert len(grants) == len(executions) == 1
        assert grants == executions
        assert result.stop_reason == "worker_failure"
        unclaimed = next(task for task in tasks if task.id != grants[0])
        assert store.get_claim(unclaimed.id) is None
    finally:
        store.close()


def test_local_cycle_planner_children_are_not_claimed_in_same_cycle(
    tmp_path, monkeypatch
):
    worker = _PlanCycleWorker()
    root = build_control_plane_contract(
        name="cycle-plan-root", outputs=("final_report",), budget=5
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, worker, (root,)
    )
    try:
        result = run_local_cycle(
            store,
            (FleetHost(host, capacity=1),),
            db_path=db_path,
            target_contract_id=root.id,
        )
        children = [
            contract
            for contract in store.get_all_contracts()
            if contract.parent_id == root.id
        ]
        # Planning is the ordinary runtime continuation path: the /plan
        # Candidate publishes child Contracts, but this cycle admits only the
        # one root claim and does not recycle its capacity slot for a child.
        assert len(children) == 3
        assert all(child is not None for child in children)
        assert worker.invocations == [root.id]
        grants = [
            record.payload["contract_id"]
            for _revision, record in store.scan_runtime_records(
                record_type="claim.granted"
            )
        ]
        assert grants == [root.id]
        assert result.stop_reason == "cycle_boundary"
        assert not result.completed
        assert result.projection["status"] == "expanded"
    finally:
        store.close()


def test_local_cycle_terminal_and_idle_are_observable_without_extra_claims(
    tmp_path, monkeypatch
):
    worker = _CycleWorker()
    task = build_control_plane_contract(
        name="cycle-terminal",
        outputs=("result",),
        worker_capabilities=("text.extract",),
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path / "terminal", monkeypatch, worker, (task,)
    )
    try:
        first = run_local_cycle(
            store, (FleetHost(host),), db_path=db_path, target_contract_id=task.id
        )
        second = run_local_cycle(
            store, (FleetHost(host),), db_path=db_path, target_contract_id=task.id
        )
        assert first.completed
        assert second.stop_reason == "terminal"
        assert worker.invocations == [task.id]
    finally:
        store.close()

    idle_worker = _CycleWorker(worker_id="worker:idle")
    idle_path, idle_store, idle_host = _prepare_local_cycle_case(
        tmp_path / "idle", monkeypatch, idle_worker
    )
    try:
        idle = run_local_cycle(idle_store, (FleetHost(idle_host),), db_path=idle_path)
        assert idle.status == "idle"
        assert idle.stop_reason == "idle"
        assert idle.projection is None
        assert idle_worker.invocations == []
    finally:
        idle_store.close()


def test_local_cycle_submitted_output_stays_unqualified(tmp_path, monkeypatch):
    worker = _CycleWorker()
    policy = {
        "mode": "independent",
        "policy_version": "cycle-review-v1",
        "required_attestations": 1,
        "verifier_capabilities": ["policy.review"],
    }
    task = build_control_plane_contract(
        name="cycle-submitted",
        outputs=("report",),
        worker_capabilities=("text.extract",),
        acceptance_policy=policy,
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, worker, (task,)
    )
    try:
        result = run_local_cycle(
            store, (FleetHost(host),), db_path=db_path, target_contract_id=task.id
        )
        assert result.status == "submitted"
        assert result.completed is False
        assert "qualification:independent_acceptance" in result.projection["blockers"]
    finally:
        store.close()


def test_local_cycle_joins_one_admitted_package_without_later_slot(
    tmp_path, monkeypatch
):
    worker = _CycleWorker()
    first = build_control_plane_contract(
        name="cycle-deadline-one",
        outputs=("one",),
        worker_capabilities=("text.extract",),
    )
    second = build_control_plane_contract(
        name="cycle-deadline-two",
        outputs=("two",),
        worker_capabilities=("text.extract",),
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, worker, (first, second)
    )
    clock = [100.0]
    monkeypatch.setattr(
        local_fleet_module,
        "time",
        type("FakeTime", (), {"monotonic": staticmethod(lambda: clock[0])})(),
    )
    invoke = worker.invoke

    def invoke_and_consume_remaining_time(contract, disclosed_assets):
        result = invoke(contract, disclosed_assets)
        clock[0] += 2.0
        return result

    worker.invoke = invoke_and_consume_remaining_time
    try:
        result = run_local_cycle(
            store, (FleetHost(host, capacity=2),), db_path=db_path, timeout=1.0
        )
        assert len(worker.invocations) == 1
        grants = [
            record.payload["contract_id"]
            for _revision, record in store.scan_runtime_records(
                record_type="claim.granted"
            )
        ]
        assert len(grants) == 1
        other = second if grants[0] == first.id else first
        assert store.get_claim(other.id) is None
        assert result.timed_out is False
        assert result.completed is True
        assert result.stop_reason == "terminal"
    finally:
        store.close()


def test_local_cycle_deadline_includes_premaintenance_and_prevents_claim(
    tmp_path, monkeypatch
):
    worker = _CycleWorker()
    task = build_control_plane_contract(
        name="cycle-deadline-premaintenance",
        outputs=("result",),
        worker_capabilities=("text.extract",),
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, worker, (task,)
    )
    clock = [100.0]
    original_maintenance = local_fleet_module.run_runtime_maintenance_step

    def maintenance_consumes_deadline(*args, **kwargs):
        result = original_maintenance(*args, **kwargs)
        clock[0] += 2.0
        return result

    monkeypatch.setattr(
        local_fleet_module,
        "time",
        type("FakeTime", (), {"monotonic": staticmethod(lambda: clock[0])})(),
    )
    monkeypatch.setattr(
        local_fleet_module,
        "run_runtime_maintenance_step",
        maintenance_consumes_deadline,
    )
    try:
        result = run_local_cycle(
            store,
            (FleetHost(host),),
            db_path=db_path,
            target_contract_id=task.id,
            timeout=1.0,
        )
        assert result.timed_out is True
        assert result.stop_reason == "deadline"
        assert worker.invocations == []
        assert store.get_claim(task.id) is None
        assert not store.scan_runtime_records(record_type="claim.granted")
    finally:
        store.close()


def test_local_cycle_known_failure_publishes_recovery_without_reset(
    tmp_path, monkeypatch
):
    worker = _RepairingWorker()
    root = build_control_plane_contract(
        name="cycle-recovery-root",
        outputs=("evidence",),
        budget=2,
        worker_capabilities=("root.orchestrator",),
        delegation_capabilities=("text.extract",),
    )
    child = _child_contract(
        root, name="cycle-recovery-child", output="evidence", capability="text.extract"
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, worker, (root, child)
    )
    try:
        result = run_local_cycle(
            store,
            (FleetHost(host, required_contract_capability="text.extract"),),
            db_path=db_path,
            target_contract_id=root.id,
        )
        contracts = store.get_all_contracts()
        recoveries = [item for item in contracts if item.origin == "recovery"]
        assert recoveries
        assert store.get_claim(child.id)["status"] == "submitted"
        assert result.status == "stalled"
    finally:
        store.close()


def test_local_cycle_redis_pending_and_unknown_failure_stop(tmp_path, monkeypatch):
    worker = _CycleWorker()
    task = build_control_plane_contract(
        name="cycle-relay", outputs=("result",), worker_capabilities=("text.extract",)
    )
    db_path, store, host = _prepare_local_cycle_case(
        tmp_path / "pending", monkeypatch, worker, (task,)
    )
    calls = 0

    monkeypatch.setattr(
        local_fleet_module,
        "sync_runtime",
        lambda _store: (_ for _ in ()).throw(RedisSyncPending(accepted=None)),
    )
    with pytest.raises(RedisSyncPending):
        run_local_cycle(
            store, (FleetHost(host),), db_path=db_path, target_contract_id=task.id
        )
    assert worker.invocations == []

    def pending_after_commit(_store):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RedisSyncPending(accepted=True)

    monkeypatch.setattr(local_fleet_module, "sync_runtime", pending_after_commit)
    try:
        with pytest.raises(RedisSyncPending):
            run_local_cycle(
                store, (FleetHost(host),), db_path=db_path, target_contract_id=task.id
            )
        assert store.get_assets_by_name("result")
    finally:
        store.close()

    second_worker = _CycleWorker(worker_id="worker:unknown")
    second_task = build_control_plane_contract(
        name="cycle-unknown", outputs=("result",), worker_capabilities=("text.extract",)
    )
    second_path, second_store, second_host = _prepare_local_cycle_case(
        tmp_path / "unknown", monkeypatch, second_worker, (second_task,)
    )
    monkeypatch.setattr(local_fleet_module, "sync_runtime", lambda _store: None)
    monkeypatch.setattr(
        local_fleet_module,
        "execute_captured_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("unknown execution failure")
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="unknown execution failure"):
            run_local_cycle(
                second_store,
                (FleetHost(second_host),),
                db_path=second_path,
                target_contract_id=second_task.id,
            )
        assert second_store.get_claim(second_task.id)["status"] == "active"
    finally:
        second_store.close()


def test_local_cycle_rejects_stale_signed_host_registration(tmp_path, monkeypatch):
    worker = _CycleWorker(worker_id="worker:stale-registration", capability=None)
    worker.registration = lambda: WorkerRegistration(worker.worker_id)
    task = build_control_plane_contract(
        name="cycle-stale-registration",
        outputs=("result",),
        worker_capabilities=("text.extract",),
    )
    db_path, store, stale_host = _prepare_local_cycle_case(
        tmp_path, monkeypatch, worker, (task,)
    )
    try:
        updated_worker = _CycleWorker(
            worker_id=worker.worker_id,
            capability=None,
        )
        updated_worker.registration = lambda: WorkerRegistration(
            worker.worker_id,
            profile_id="test:updated-profile",
        )
        current_host = ensure_local_worker_host(store, updated_worker)
        assert current_host.registration_version != stale_host.registration_version
        with pytest.raises(ValueError, match="stale"):
            run_local_cycle(
                store,
                (FleetHost(stale_host),),
                db_path=db_path,
                target_contract_id=task.id,
            )
        assert worker.invocations == []
    finally:
        store.close()
