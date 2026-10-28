from __future__ import annotations

import ast
import copy
import threading
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from examples.thermal_calibration.provider_meter import (
    InvalidRequestLimit,
    InvalidRunBudget,
    OutputReservationExceeded,
    PermitAlreadyFinalized,
    ProviderMeterError,
    RequestPermit,
    RequestAttemptBudgetExceeded,
    RunBudget,
    RunDeadlineExpired,
    RunMeter,
    UnknownRequestPermit,
)


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.value


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": True},
        {"max_attempts": -1},
        {"max_attempts": 1.5},
        {"output_reservation_ceiling": False},
        {"output_reservation_ceiling": -1},
        {"max_output_tokens_per_request": True},
        {"max_output_tokens_per_request": 0},
        {"deadline_seconds": True},
        {"deadline_seconds": 0},
        {"deadline_seconds": float("inf")},
    ],
)
def test_run_budget_rejects_invalid_values(kwargs: dict[str, object]) -> None:
    values: dict[str, object] = {
        "max_attempts": 2,
        "max_output_tokens_per_request": 8,
        "output_reservation_ceiling": 16,
        "deadline_seconds": 10.0,
    }
    values.update(kwargs)
    with pytest.raises(InvalidRunBudget):
        RunBudget(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("max_attempts", "reservation", "error"),
    [
        (0, 10, RequestAttemptBudgetExceeded),
        (2, 0, OutputReservationExceeded),
    ],
)
def test_zero_global_limits_disable_request_acquisition(
    max_attempts: int, reservation: int, error: type[ProviderMeterError]
) -> None:
    meter = RunMeter(RunBudget(max_attempts, 4, reservation, 10.0), FakeClock())
    with pytest.raises(error):
        meter.acquire_request(output_cap=1, request_timeout_cap=2.0)
    snapshot = meter.snapshot()
    assert snapshot.admitted_attempts == 0
    assert snapshot.output_reserved == 0


def test_concurrent_acquisition_has_one_final_slot_winner() -> None:
    meter = RunMeter(RunBudget(1, 4, 4, 10.0), FakeClock())
    barrier = threading.Barrier(3)
    permits = []
    errors = []

    def acquire() -> None:
        barrier.wait()
        try:
            permits.append(meter.acquire_request(output_cap=4, request_timeout_cap=3.0))
        except ProviderMeterError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=acquire) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2.0)
    assert all(not thread.is_alive() for thread in threads)
    assert len(permits) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], RequestAttemptBudgetExceeded)
    assert meter.snapshot().admitted_attempts == 1
    assert meter.snapshot().output_reserved == 4


def test_output_caps_are_reserved_at_boundaries_and_never_refunded() -> None:
    meter = RunMeter(RunBudget(3, 5, 7, 10.0), FakeClock())
    first = meter.acquire_request(output_cap=4, request_timeout_cap=5.0)
    meter.finish(first, outcome="failed", usage=None)
    second = meter.acquire_request(output_cap=3, request_timeout_cap=5.0)
    assert meter.snapshot().output_reserved == 7
    with pytest.raises(OutputReservationExceeded):
        meter.acquire_request(output_cap=1, request_timeout_cap=5.0)
    with pytest.raises(InvalidRequestLimit):
        meter.acquire_request(output_cap=6, request_timeout_cap=5.0)
    assert meter.snapshot().admitted_attempts == 2
    assert meter.snapshot().output_reserved == 7
    assert second.output_cap == 3


def test_request_timeout_is_capped_by_remaining_run_deadline() -> None:
    clock = FakeClock(4.0)
    meter = RunMeter(RunBudget(2, 5, 8, 6.0), clock)
    permit = meter.acquire_request(output_cap=2, request_timeout_cap=9.0)
    assert permit.deadline_monotonic == 10.0
    assert permit.timeout_seconds == 6.0
    clock.value = 9.25
    later = meter.acquire_request(output_cap=2, request_timeout_cap=8.0)
    assert later.timeout_seconds == 0.75
    clock.value = 10.0
    with pytest.raises(RunDeadlineExpired):
        meter.acquire_request(output_cap=1, request_timeout_cap=1.0)


def test_deadline_equality_is_late_and_preserves_usage() -> None:
    clock = FakeClock(0.0)
    meter = RunMeter(RunBudget(1, 3, 3, 5.0), clock)
    permit = meter.acquire_request(output_cap=3, request_timeout_cap=5.0)
    clock.value = permit.deadline_monotonic
    record = meter.finish(
        permit,
        outcome="response",
        usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    )
    assert record.late is True
    assert record.result_eligible is False
    assert record.prompt_tokens.value == 2
    assert meter.snapshot().prompt.value == 2


def test_usage_is_componentwise_and_unknown_is_not_zero() -> None:
    meter = RunMeter(RunBudget(4, 8, 24, 30.0), FakeClock())
    meter.finish(
        meter.acquire_request(output_cap=4, request_timeout_cap=10.0),
        outcome="response",
        usage={"prompt_tokens": 0, "completion_tokens": 2, "total_tokens": 99},
    )
    meter.finish(
        meter.acquire_request(output_cap=4, request_timeout_cap=10.0),
        outcome="failed",
        usage={"prompt_tokens": 3, "completion_tokens": -1},
    )
    meter.finish(
        meter.acquire_request(output_cap=4, request_timeout_cap=10.0),
        outcome="timeout",
        usage=None,
    )
    snapshot = meter.snapshot()
    assert snapshot.prompt.known_subtotal == 3
    assert snapshot.prompt.reported_count == 2
    assert snapshot.prompt.unknown_count == 1
    assert snapshot.prompt.value is None
    assert snapshot.completion.known_subtotal == 2
    assert snapshot.completion.unknown_count == 2
    assert snapshot.completion.value is None
    assert snapshot.total.known_subtotal == 99
    assert snapshot.total.unknown_count == 2
    assert snapshot.total.value is None
    assert snapshot.inconsistent_total_attempts == 1
    assert snapshot.cached_reasoning_status == "unsupported"


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({}, ("missing", "missing", "missing")),
        ([], ("invalid", "invalid", "invalid")),
        ({"prompt_tokens": None}, ("invalid", "missing", "missing")),
        ({"prompt_tokens": True}, ("invalid", "missing", "missing")),
        ({"prompt_tokens": 1.0}, ("invalid", "missing", "missing")),
        ({"prompt_tokens": "1"}, ("invalid", "missing", "missing")),
        ({"prompt_tokens": -1}, ("invalid", "missing", "missing")),
        ({"prompt_tokens": 0}, ("reported", "missing", "missing")),
    ],
)
def test_usage_shape_and_values_keep_explicit_statuses(
    usage: object, expected: tuple[str, str, str]
) -> None:
    meter = RunMeter(RunBudget(1, 4, 4, 10.0), FakeClock())
    record = meter.finish(
        meter.acquire_request(output_cap=4, request_timeout_cap=2.0),
        outcome="response",
        usage=usage,  # type: ignore[arg-type]
    )
    actual = tuple(
        component.status
        for component in (
            record.prompt_tokens,
            record.completion_tokens,
            record.total_tokens,
        )
    )
    assert actual == expected


def test_pending_snapshot_is_immutable_and_second_finish_cannot_replace_record() -> (
    None
):
    meter = RunMeter(RunBudget(1, 4, 4, 10.0), FakeClock())
    permit = meter.acquire_request(output_cap=4, request_timeout_cap=2.0)
    pending = meter.snapshot()
    assert pending.pending_count == 1
    assert pending.attempts[0].outcome == "pending"
    assert pending.prompt.unknown_count == 1
    meter.finish(permit, outcome="failed", usage=None)
    assert pending.pending_count == 1
    assert pending.attempts[0].outcome == "pending"
    after = meter.snapshot()
    assert after.pending_count == 0
    assert after.attempts[0].prompt_tokens.status == "not_returned"
    with pytest.raises(PermitAlreadyFinalized):
        meter.finish(permit, outcome="response", usage={})
    assert meter.snapshot().attempts[0].outcome == "failed"


def test_foreign_fabricated_and_copied_permits_are_rejected() -> None:
    first_meter = RunMeter(RunBudget(1, 4, 4, 10.0), FakeClock())
    second_meter = RunMeter(RunBudget(1, 4, 4, 10.0), FakeClock())
    permit = first_meter.acquire_request(output_cap=2, request_timeout_cap=2.0)
    forged = RequestPermit(
        permit_id=permit.permit_id,
        attempt_number=permit.attempt_number,
        output_cap=permit.output_cap,
        reserved_output_tokens=permit.reserved_output_tokens,
        timeout_seconds=permit.timeout_seconds,
        deadline_monotonic=permit.deadline_monotonic,
    )
    for foreign in (
        second_meter,
        forged,
        copy.copy(permit),
        copy.deepcopy(permit),
    ):
        with pytest.raises(UnknownRequestPermit):
            first_meter.finish(foreign, outcome="response", usage={})
    with pytest.raises(UnknownRequestPermit):
        second_meter.finish(permit, outcome="response", usage={})
    assert first_meter.snapshot().pending_count == 1
    assert second_meter.snapshot().admitted_attempts == 0


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "2", float("inf")])
def test_output_cap_rejects_invalid_types_without_admitting(value: object) -> None:
    clock = FakeClock()
    meter = RunMeter(RunBudget(2, 4, 8, 10.0), clock)
    calls_before = clock.calls
    with pytest.raises(InvalidRequestLimit):
        meter.acquire_request(output_cap=value, request_timeout_cap=2.0)  # type: ignore[arg-type]

    assert clock.calls == calls_before
    assert meter.snapshot().admitted_attempts == 0
    assert meter.snapshot().output_reserved == 0


@pytest.mark.parametrize("value", [True, 0, -1, "2", float("inf"), float("nan")])
def test_request_timeout_cap_rejects_invalid_types_without_admitting(
    value: object,
) -> None:
    clock = FakeClock()
    meter = RunMeter(RunBudget(2, 4, 8, 10.0), clock)
    calls_before = clock.calls
    with pytest.raises(InvalidRequestLimit):
        meter.acquire_request(output_cap=2, request_timeout_cap=value)  # type: ignore[arg-type]
    assert clock.calls == calls_before
    assert meter.snapshot().admitted_attempts == 0
    assert meter.snapshot().output_reserved == 0


@pytest.mark.parametrize("outcome", ["failed", "timeout"])
def test_failed_and_timed_out_observed_usage_is_retained(outcome: str) -> None:
    meter = RunMeter(RunBudget(1, 4, 4, 10.0), FakeClock())
    record = meter.finish(
        meter.acquire_request(output_cap=4, request_timeout_cap=2.0),
        outcome=outcome,  # type: ignore[arg-type]
        usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    )
    assert record.outcome == outcome
    assert record.result_eligible is False
    assert record.prompt_tokens.status == "reported"
    assert record.prompt_tokens.value == 3
    assert record.completion_tokens.value == 1
    assert record.total_tokens.value == 4
    assert meter.snapshot().prompt.value == 3


class ExplodingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError("mapping read failed")

    def __iter__(self):
        return iter(("prompt_tokens",))

    def __len__(self) -> int:
        return 1


@pytest.mark.parametrize("bad_outcome", [None, [], {}])
def test_bad_outcome_precedes_clock_and_usage_and_mapping_failure_is_atomic(
    bad_outcome: object,
) -> None:
    clock = FakeClock()
    meter = RunMeter(RunBudget(1, 4, 4, 10.0), clock)
    permit = meter.acquire_request(output_cap=3, request_timeout_cap=2.0)
    calls_after_acquire = clock.calls
    with pytest.raises(InvalidRequestLimit):
        meter.finish(
            permit,
            outcome=bad_outcome,  # type: ignore[arg-type]
            usage=ExplodingMapping(),
        )
    assert clock.calls == calls_after_acquire
    before = meter.snapshot()
    with pytest.raises(RuntimeError, match="mapping read failed"):
        meter.finish(permit, outcome="response", usage=ExplodingMapping())
    after = meter.snapshot()
    assert after == before
    assert after.pending_count == 1
    assert after.output_reserved == 3


def test_clock_exception_keeps_permit_pending() -> None:
    class FlakyClock:
        fail = False

        def __call__(self) -> float:
            if self.fail:
                raise RuntimeError("clock failed")
            return 0.0

    clock = FlakyClock()
    meter = RunMeter(RunBudget(1, 4, 4, 10.0), clock)
    permit = meter.acquire_request(output_cap=3, request_timeout_cap=2.0)
    before = meter.snapshot()
    clock.fail = True
    with pytest.raises(RuntimeError, match="clock failed"):
        meter.finish(permit, outcome="response", usage={"prompt_tokens": 1})
    assert meter.snapshot() == before


def test_eligible_response_can_still_have_inconsistency_and_cap_violation() -> None:
    clock = FakeClock()
    meter = RunMeter(RunBudget(1, 4, 4, 10.0), clock)
    permit = meter.acquire_request(output_cap=1, request_timeout_cap=2.0)
    clock.value = 1.0
    record = meter.finish(
        permit,
        outcome="response",
        usage={"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 17},
    )
    assert record.result_eligible is True
    assert record.inconsistent_total is True
    assert record.output_cap_violation is True
    assert record.completion_tokens.value == 2
    assert meter.snapshot().completion.known_subtotal == 2


def test_distinct_meters_have_independent_run_scoped_budgets() -> None:
    budget = RunBudget(1, 4, 4, 10.0)
    first = RunMeter(budget, FakeClock())
    second = RunMeter(budget, FakeClock())
    first.acquire_request(output_cap=4, request_timeout_cap=2.0)
    permit = second.acquire_request(output_cap=4, request_timeout_cap=2.0)
    assert permit.attempt_number == 1
    assert first.snapshot().admitted_attempts == 1
    assert second.snapshot().admitted_attempts == 1


def test_budget_permit_records_and_snapshots_are_frozen() -> None:
    meter = RunMeter(RunBudget(1, 4, 4, 10.0), FakeClock())
    permit = meter.acquire_request(output_cap=3, request_timeout_cap=2.0)
    snapshot = meter.snapshot()
    for target, field, value in (
        (meter._budget, "max_attempts", 2),
        (permit, "output_cap", 1),
        (snapshot, "output_reserved", 0),
        (snapshot.prompt, "known_subtotal", 9),
        (snapshot.attempts[0], "outcome", "response"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(target, field, value)


def test_meter_module_has_no_io_provider_runtime_or_store_dependencies() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "thermal_calibration"
        / "provider_meter.py"
    )
    tree = ast.parse(source.read_text())
    forbidden_roots = {
        "aigineering",
        "httpx",
        "requests",
        "openai",
        "anthropic",
        "redis",
        "sqlite3",
        "os",
        "pathlib",
        "subprocess",
        "logging",
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert imported.isdisjoint(forbidden_roots)
    source_text = source.read_text()
    assert "os.environ" not in source_text
    assert "open(" not in source_text
