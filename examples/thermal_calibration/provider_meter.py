"""In-process request permits and explicit provider-usage uncertainty."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Callable, Literal


class ProviderMeterError(Exception):
    """Base class for declared request-meter boundary errors."""


class InvalidRunBudget(ProviderMeterError):
    pass


class InvalidRequestLimit(ProviderMeterError):
    pass


class RequestAttemptBudgetExceeded(ProviderMeterError):
    pass


class OutputReservationExceeded(ProviderMeterError):
    pass


class RunDeadlineExpired(ProviderMeterError):
    pass


class UnknownRequestPermit(ProviderMeterError):
    pass


class PermitAlreadyFinalized(ProviderMeterError):
    pass


def _nonnegative_integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value) and value > 0
    except (OverflowError, TypeError, ValueError):
        return False


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _clock_value(clock: Callable[[], float]) -> float:
    value = clock()
    if not _finite_number(value):
        raise RuntimeError("monotonic clock returned a non-finite or non-numeric value")
    return float(value)


@dataclass(frozen=True, slots=True)
class RunBudget:
    max_attempts: int
    max_output_tokens_per_request: int
    output_reservation_ceiling: int
    deadline_seconds: float

    def __post_init__(self) -> None:
        if not _nonnegative_integer(self.max_attempts):
            raise InvalidRunBudget("max_attempts must be a nonnegative integer")
        if not _nonnegative_integer(self.output_reservation_ceiling):
            raise InvalidRunBudget(
                "output_reservation_ceiling must be a nonnegative integer"
            )
        if (
            not _nonnegative_integer(self.max_output_tokens_per_request)
            or self.max_output_tokens_per_request == 0
        ):
            raise InvalidRunBudget(
                "max_output_tokens_per_request must be a positive integer"
            )
        if not _positive_finite_number(self.deadline_seconds):
            raise InvalidRunBudget("deadline_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class RequestPermit:
    permit_id: int
    attempt_number: int
    output_cap: int
    reserved_output_tokens: int
    timeout_seconds: float
    deadline_monotonic: float


UsageStatus = Literal["reported", "pending", "not_returned", "missing", "invalid"]


@dataclass(frozen=True, slots=True)
class UsageComponent:
    value: int | None
    status: UsageStatus


AttemptOutcome = Literal["pending", "response", "failed", "timeout"]


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    permit_id: int
    outcome: AttemptOutcome
    prompt_tokens: UsageComponent
    completion_tokens: UsageComponent
    total_tokens: UsageComponent
    completed_monotonic: float | None
    late: bool | None
    result_eligible: bool | None
    inconsistent_total: bool | None
    output_cap_violation: bool | None
    cached_reasoning_status: Literal["unsupported"]


@dataclass(frozen=True, slots=True)
class ComponentAggregate:
    known_subtotal: int
    unknown_count: int
    reported_count: int
    value: int | None


@dataclass(frozen=True, slots=True)
class RunUsageSummary:
    admitted_attempts: int
    output_reserved: int
    pending_count: int
    prompt: ComponentAggregate
    completion: ComponentAggregate
    total: ComponentAggregate
    inconsistent_total_attempts: int
    cached_reasoning_status: Literal["unsupported"]
    attempts: tuple[AttemptRecord, ...]


class RunMeter:
    """Thread-safe in-process owner for one run's request permits and usage."""

    def __init__(
        self, budget: RunBudget, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._budget = budget
        self._clock = clock
        now = _clock_value(clock)
        deadline = now + budget.deadline_seconds
        if not math.isfinite(deadline):
            raise InvalidRunBudget("deadline overflows the monotonic clock range")
        self._deadline = deadline
        self._lock = threading.Lock()
        self._next_permit_id = 1
        self._reserved_output = 0
        self._permits_by_identity: dict[int, RequestPermit] = {}
        self._records: dict[int, AttemptRecord] = {}

    def acquire_request(
        self, *, output_cap: int, request_timeout_cap: float
    ) -> RequestPermit:
        if (
            not _nonnegative_integer(output_cap)
            or output_cap == 0
            or output_cap > self._budget.max_output_tokens_per_request
        ):
            raise InvalidRequestLimit(
                "output_cap must be a positive integer within the per-request limit"
            )
        if not _positive_finite_number(request_timeout_cap):
            raise InvalidRequestLimit("request_timeout_cap must be finite and positive")
        with self._lock:
            now = _clock_value(self._clock)
            if now >= self._deadline:
                raise RunDeadlineExpired("run deadline has expired")
            if len(self._records) >= self._budget.max_attempts:
                raise RequestAttemptBudgetExceeded("run attempt budget is exhausted")
            reserved = self._reserved_output + output_cap
            if reserved > self._budget.output_reservation_ceiling:
                raise OutputReservationExceeded("run output reservation is exhausted")
            timeout = min(float(request_timeout_cap), self._deadline - now)
            permit_id = self._next_permit_id
            permit = RequestPermit(
                permit_id=permit_id,
                attempt_number=permit_id,
                output_cap=output_cap,
                reserved_output_tokens=output_cap,
                timeout_seconds=timeout,
                deadline_monotonic=self._deadline,
            )
            pending = UsageComponent(value=None, status="pending")
            record = AttemptRecord(
                permit_id=permit_id,
                outcome="pending",
                prompt_tokens=pending,
                completion_tokens=pending,
                total_tokens=pending,
                completed_monotonic=None,
                late=None,
                result_eligible=None,
                inconsistent_total=None,
                output_cap_violation=None,
                cached_reasoning_status="unsupported",
            )
            self._next_permit_id += 1
            self._reserved_output = reserved
            self._permits_by_identity[id(permit)] = permit
            self._records[permit_id] = record
            return permit

    def finish(
        self,
        permit: RequestPermit,
        *,
        outcome: Literal["response", "failed", "timeout"],
        usage: Mapping[str, object] | None,
    ) -> AttemptRecord:
        with self._lock:
            registered = self._permits_by_identity.get(id(permit))
            if registered is not permit:
                raise UnknownRequestPermit("permit was not issued by this meter")
            prior = self._records[registered.permit_id]
            if prior.outcome != "pending":
                raise PermitAlreadyFinalized("permit has already been finalized")
            if type(outcome) is not str or outcome not in {
                "response",
                "failed",
                "timeout",
            }:
                raise InvalidRequestLimit(
                    "outcome must be response, failed, or timeout"
                )

            completed = _clock_value(self._clock)
            late = completed >= self._deadline
            components = self._parse_usage(usage)
            prompt, completion, total = components
            if all(item.status == "reported" for item in components):
                inconsistent = total.value != prompt.value + completion.value  # type: ignore[operator]
            else:
                inconsistent = None
            if completion.status == "reported":
                cap_violation = completion.value > registered.output_cap  # type: ignore[operator]
            else:
                cap_violation = None
            record = AttemptRecord(
                permit_id=registered.permit_id,
                outcome=outcome,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                completed_monotonic=completed,
                late=late,
                result_eligible=outcome == "response" and not late,
                inconsistent_total=inconsistent,
                output_cap_violation=cap_violation,
                cached_reasoning_status="unsupported",
            )
            self._records[registered.permit_id] = record
            return record

    @staticmethod
    def _parse_usage(
        usage: Mapping[str, object] | None,
    ) -> tuple[UsageComponent, UsageComponent, UsageComponent]:
        keys = ("prompt_tokens", "completion_tokens", "total_tokens")
        if usage is None:
            return tuple(UsageComponent(None, "not_returned") for _ in keys)  # type: ignore[return-value]
        if not isinstance(usage, Mapping):
            return tuple(UsageComponent(None, "invalid") for _ in keys)  # type: ignore[return-value]
        parsed = []
        for key in keys:
            if key not in usage:
                parsed.append(UsageComponent(None, "missing"))
                continue
            value = usage[key]
            if type(value) is int and value >= 0:
                parsed.append(UsageComponent(value, "reported"))
            else:
                parsed.append(UsageComponent(None, "invalid"))
        return tuple(parsed)  # type: ignore[return-value]

    def snapshot(self) -> RunUsageSummary:
        with self._lock:
            attempts = tuple(self._records.values())

            def aggregate(attribute: str) -> ComponentAggregate:
                components = [getattr(item, attribute) for item in attempts]
                reported = [
                    item.value for item in components if item.status == "reported"
                ]
                unknown = len(components) - len(reported)
                subtotal = sum(reported)
                return ComponentAggregate(
                    known_subtotal=subtotal,
                    unknown_count=unknown,
                    reported_count=len(reported),
                    value=subtotal if unknown == 0 else None,
                )

            return RunUsageSummary(
                admitted_attempts=len(attempts),
                output_reserved=self._reserved_output,
                pending_count=sum(item.outcome == "pending" for item in attempts),
                prompt=aggregate("prompt_tokens"),
                completion=aggregate("completion_tokens"),
                total=aggregate("total_tokens"),
                inconsistent_total_attempts=sum(
                    item.inconsistent_total is True for item in attempts
                ),
                cached_reasoning_status="unsupported",
                attempts=attempts,
            )
