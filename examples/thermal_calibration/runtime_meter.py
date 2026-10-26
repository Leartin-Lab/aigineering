"""Process-local provider admission for the ordinary thermal-calibration Fleet."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from aigineering.agent.llm import ProviderError
from aigineering.agent.worker import WorkerExecutionError
from aigineering.task_logs import current_observer

from .deepseek_client import TransportReply, _decode_root, _stdlib_https_transport
from .provider_meter import (
    OutputReservationExceeded,
    RequestAttemptBudgetExceeded,
    RunBudget,
    RunDeadlineExpired,
    RunMeter,
)

_ENV_MAX_ATTEMPTS = "AIGINEERING_G0C_MAX_ATTEMPTS"
_ENV_OUTPUT_CEILING = "AIGINEERING_G0C_OUTPUT_RESERVATION_CEILING"
_ENV_DEADLINE = "AIGINEERING_G0C_RUN_DEADLINE_SECONDS"
_API_KEY_ENV = "AIGINEERING_API_KEY"
_DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
_DEFAULT_MODEL = "deepseek-flash"
_OUTPUT_CAP = 8192
_REQUEST_TIMEOUT_CAP = 60.0
_TOOL_REGISTRY_REF = "examples.thermal_calibration.tools:build_registry"
_DEFAULT_OBSERVER_GETTER = current_observer


@dataclass(frozen=True)
class RuntimeMeterConfig:
    max_attempts: int
    output_reservation_ceiling: int
    deadline_seconds: float
    base_url: str = _DEFAULT_BASE_URL
    model: str = _DEFAULT_MODEL
    max_output_tokens: int = _OUTPUT_CAP
    request_timeout_seconds: float = _REQUEST_TIMEOUT_CAP
    tool_registry_ref: str = _TOOL_REGISTRY_REF
    profile_version: str = "g0c-runtime-v2"
    api_key_env: str = _API_KEY_ENV

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> RuntimeMeterConfig:
        values = os.environ if env is None else env
        attempts = _bounded_integer(values.get(_ENV_MAX_ATTEMPTS))
        ceiling = _bounded_integer(values.get(_ENV_OUTPUT_CEILING))
        deadline = _bounded_decimal(values.get(_ENV_DEADLINE))
        return cls(attempts, ceiling, deadline)


def _bounded_integer(raw: str | None) -> int:
    if not isinstance(raw, str) or re.fullmatch(r"[0-9]+", raw) is None:
        raise ValueError("G0c run meter configuration is invalid")
    try:
        return int(raw)
    except ValueError:
        raise ValueError("G0c run meter configuration is invalid")


def _bounded_decimal(raw: str | None) -> float:
    if not isinstance(raw, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw) is None:
        raise ValueError("G0c run meter configuration is invalid")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("G0c run meter configuration is invalid")
    return value


def _fingerprint(config: RuntimeMeterConfig) -> str:
    encoded = json.dumps(
        {
            "attempts": config.max_attempts,
            "ceiling": config.output_reservation_ceiling,
            "deadline": config.deadline_seconds,
            "url": config.base_url,
            "model": config.model,
            "max_output_tokens": config.max_output_tokens,
            "timeout": config.request_timeout_seconds,
            "tool_registry_ref": config.tool_registry_ref,
            "profile_version": config.profile_version,
            "api_key_env": config.api_key_env,
            "planner_id": "calibration-planner",
            "researcher_id": "calibration-researcher",
            "retry_count": 0,
            "planner_thinking": "disabled",
            "researcher_thinking": "disabled",
            "planner_response_format": "json_object",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class RuntimeMeterSession:
    def __init__(self, config: RuntimeMeterConfig, clock) -> None:
        self.config = config
        self.fingerprint = _fingerprint(config)
        self.epoch = secrets.token_hex(16)
        self.clock = clock
        self.meter = RunMeter(
            RunBudget(
                max_attempts=config.max_attempts,
                max_output_tokens_per_request=config.max_output_tokens,
                output_reservation_ceiling=config.output_reservation_ceiling,
                deadline_seconds=config.deadline_seconds,
            ),
            clock=clock,
        )
        self.lock = threading.RLock()
        self.api_key: str | None = None
        self.key_selected = False
        self.roles: set[str] = set()
        self.constructing_roles: set[str] = set()
        self.phase = "preparing"
        self.poisoned = False
        self.registry = None
        self.tool_definitions: list[dict[str, Any]] = []

    @classmethod
    def from_config(
        cls, config: RuntimeMeterConfig, *, clock: Callable[[], float] = time.monotonic
    ) -> RuntimeMeterSession:
        """Build one process epoch from a validated, nonsecret profile."""
        if not isinstance(config, RuntimeMeterConfig):
            raise TypeError("config must be RuntimeMeterConfig")
        return cls(config, clock)

    def __repr__(self) -> str:
        return f"RuntimeMeterSession(epoch={self.epoch!r}, phase={self.phase!r})"


_lock = threading.RLock()
_session: RuntimeMeterSession | None = None


def _monotonic_clock() -> float:
    return time.monotonic()


def reset_process_session_for_tests() -> None:
    global _session
    with _lock:
        _session = None


def _validated_config() -> tuple[RuntimeMeterConfig, object, list[dict[str, Any]]]:
    config = RuntimeMeterConfig.from_environment()
    if config.base_url != _DEFAULT_BASE_URL or config.model != _DEFAULT_MODEL:
        raise ValueError("G0c run meter configuration is invalid")
    from aigineering.agent.tool_registry_loader import (
        load_tool_registry,
        provider_tool_definitions,
    )

    registry = load_tool_registry(config.tool_registry_ref)
    definitions = provider_tool_definitions(registry)
    names = (
        {
            item.get("function", {}).get("name")
            for item in definitions
            if isinstance(item, dict)
        }
        if isinstance(definitions, list)
        else set()
    )
    if (
        not isinstance(definitions, list)
        or len(definitions) != 2
        or any(not isinstance(item, dict) for item in definitions)
        or names != {"normalize_calibration", "analyze_calibration"}
    ):
        raise ValueError("G0c run meter configuration is invalid")
    return config, registry, definitions


def get_process_session() -> RuntimeMeterSession:
    global _session
    config, registry, definitions = _validated_config()
    fingerprint = _fingerprint(config)
    with _lock:
        if _session is None:
            _session = RuntimeMeterSession.from_config(config, clock=_monotonic_clock)
            _session.registry = registry
            _session.tool_definitions = definitions
        elif _session.fingerprint != fingerprint:
            raise ValueError("G0c run meter configuration changed in this process")
        return _session


def _select_key(session: RuntimeMeterSession) -> str:
    with session.lock:
        if session.poisoned:
            raise ValueError("G0c run meter session is poisoned")
        if not session.key_selected:
            session.key_selected = True
            key = os.environ.get(_API_KEY_ENV)
            if (
                not isinstance(key, str)
                or not key.strip()
                or any(character in key for character in "\r\n")
            ):
                session.poisoned = True
                session.phase = "poisoned"
                raise ValueError("AIGINEERING_API_KEY is missing or invalid")
            session.api_key = key
        assert session.api_key is not None
        return session.api_key


def _reserve_role(session: RuntimeMeterSession, role: str, worker_id: str) -> str:
    expected = {
        "planner": "calibration-planner",
        "researcher": "calibration-researcher",
    }[role]
    with session.lock:
        if (
            session.poisoned
            or worker_id != expected
            or role in session.roles
            or role in session.constructing_roles
        ):
            session.poisoned = True
            session.phase = "poisoned"
            raise ValueError("G0c run meter session is poisoned")
        session.constructing_roles.add(role)
    return _select_key(session)


def _complete_role(session: RuntimeMeterSession, role: str, *, succeeded: bool) -> None:
    with session.lock:
        session.constructing_roles.discard(role)
        if not succeeded:
            session.poisoned = True
            session.phase = "poisoned"
            return
        session.roles.add(role)
        if session.roles == {"planner", "researcher"}:
            session.phase = "ready"


def _safe_emit(
    observer_getter: Callable[[], object], name: str, **fields: object
) -> None:
    writer = None
    try:
        writer = observer_getter()
        if writer is None or getattr(writer, "warning", None) is not None:
            return
        writer.emit(name, **fields)
    except Exception:
        if writer is not None:
            try:
                if getattr(writer, "warning", None) is None:
                    writer.warning = "capture_unavailable"
            except Exception:
                pass


@contextmanager
def factory_construction():
    """Serialize config/key validation and no-HTTP Worker construction."""
    with _lock:
        yield


def _profile_payload(
    role: str, payload: object, definitions: list[dict[str, Any]]
) -> tuple[str, dict[str, str], bytes]:
    invalid = WorkerExecutionError(
        "provider_request_invalid", "provider request profile is invalid"
    )
    if not isinstance(payload, Mapping):
        raise invalid
    payload = dict(payload)
    common = {"model", "temperature", "max_tokens", "messages", "thinking"}
    expected = common | ({"response_format"} if role == "planner" else set())
    if role == "researcher" and "tools" in payload:
        expected.add("tools")
    if set(payload) != expected:
        raise invalid
    if (
        payload.get("model") != _DEFAULT_MODEL
        or type(payload.get("temperature")) is not int
        or payload.get("temperature") != 0
        or type(payload.get("max_tokens")) is not int
        or payload.get("max_tokens") != _OUTPUT_CAP
        or not isinstance(payload.get("messages"), list)
        or len(payload["messages"]) != 2
        or any(
            not isinstance(m, dict)
            or set(m) != {"role", "content"}
            or m.get("role") not in {"system", "user"}
            or not isinstance(m.get("content"), str)
            for m in payload["messages"]
        )
        or [m["role"] for m in payload["messages"]] != ["system", "user"]
        or payload.get("thinking") != {"type": "disabled"}
    ):
        raise invalid
    if role == "planner" and payload.get("response_format") != {"type": "json_object"}:
        raise invalid
    if role == "researcher" and "tools" in payload:
        if (
            not isinstance(payload["tools"], list)
            or not 1 <= len(payload["tools"]) <= 2
        ):
            raise invalid
        known = {item["function"]["name"]: item for item in definitions}
        seen: set[str] = set()
        for item in payload["tools"]:
            if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                raise invalid
            name = item["function"].get("name")
            if name not in known or name in seen or item != known[name]:
                raise invalid
            seen.add(name)
    try:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WorkerExecutionError(
            "provider_request_invalid", "provider request profile is invalid"
        ) from exc
    return (
        f"{_DEFAULT_BASE_URL}/chat/completions",
        {"Authorization": "Bearer", "Content-Type": "application/json"},
        body,
    )


class RuntimeMeteredTransport:
    def __init__(
        self,
        session: RuntimeMeterSession,
        role: str,
        *,
        transport: Callable[..., TransportReply] | None = None,
        observer_getter: Callable[[], object] = current_observer,
    ) -> None:
        if role not in {"planner", "researcher"}:
            raise ValueError("role must be planner or researcher")
        self.session = session
        self.role = role
        self._transport = transport
        self._observer_getter = observer_getter

    def _emit(self, name: str, **fields: object) -> None:
        observer_getter = self._observer_getter
        if observer_getter is _DEFAULT_OBSERVER_GETTER:
            observer_getter = current_observer
        _safe_emit(observer_getter, name, **fields)

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        config = self.session.config
        checked_url, checked_headers, body = _profile_payload(
            self.role, payload, self.session.tool_definitions
        )
        if (
            url != checked_url
            or url != f"{config.base_url}/chat/completions"
            or set(headers) != {"Authorization", "Content-Type"}
            or headers.get("Authorization") != f"Bearer {self.session.api_key}"
            or headers.get("Content-Type") != checked_headers["Content-Type"]
        ):
            raise WorkerExecutionError(
                "provider_request_invalid", "provider request profile is invalid"
            )
        try:
            permit = self.session.meter.acquire_request(
                output_cap=config.max_output_tokens,
                request_timeout_cap=config.request_timeout_seconds,
            )
        except RunDeadlineExpired as exc:
            raise WorkerExecutionError(
                "provider_deadline_exceeded", "provider request deadline exceeded"
            ) from exc
        except (RequestAttemptBudgetExceeded, OutputReservationExceeded) as exc:
            raise WorkerExecutionError(
                "provider_request_budget_exhausted", "provider request budget exhausted"
            ) from exc

        started = self.session.clock()
        self._emit(
            "provider.permit_admitted",
            meter_epoch_id=self.session.epoch,
            permit_id=permit.permit_id,
            output_cap=permit.output_cap,
            reserved_output_tokens=permit.reserved_output_tokens,
            timeout_seconds=permit.timeout_seconds,
        )
        self._emit(
            "provider.transport_entered",
            meter_epoch_id=self.session.epoch,
            permit_id=permit.permit_id,
        )
        finish_attempted = False
        status: int | None = None
        raw_usage: Any = None
        result_eligible = False

        def finalize(
            final_outcome: str, usage: Any, code: str | None, is_late: bool = False
        ):
            nonlocal finish_attempted, result_eligible
            if finish_attempted:
                return None
            finish_attempted = True
            try:
                record = self.session.meter.finish(
                    permit, outcome=final_outcome, usage=usage
                )
            except OSError as exc:
                raise WorkerExecutionError(
                    "provider_meter_finalize_failed",
                    "provider attempt finalization failed",
                ) from exc
            result_eligible = record.result_eligible is True
            event_code = code
            if (
                record.outcome == "response"
                and record.late
                and decoded_root
                and event_code is None
            ):
                event_code = "deadline_exceeded"
            duration = (
                max(0.0, record.completed_monotonic - started)
                if record.completed_monotonic is not None
                else 0.0
            )
            self._emit(
                "provider.attempt_finalized",
                meter_epoch_id=self.session.epoch,
                permit_id=permit.permit_id,
                outcome=record.outcome,
                status_code=status,
                error_code=event_code,
                duration_seconds=duration,
                late=record.late is True,
                result_eligible=result_eligible,
                prompt_tokens=record.prompt_tokens.value,
                prompt_status=record.prompt_tokens.status,
                completion_tokens=record.completion_tokens.value,
                completion_status=record.completion_tokens.status,
                total_tokens=record.total_tokens.value,
                total_status=record.total_tokens.status,
                inconsistent_total=record.inconsistent_total,
                output_cap_violation=record.output_cap_violation,
                cached_reasoning_status=record.cached_reasoning_status,
            )
            return record

        decoded_root = False
        reply: TransportReply | None = None
        try:
            transport_call = (
                _stdlib_https_transport if self._transport is None else self._transport
            )
            reply: TransportReply = transport_call(
                url=url,
                headers={**headers, "Authorization": f"Bearer {self.session.api_key}"},
                body=body,
                timeout_seconds=permit.timeout_seconds,
                max_response_bytes=4 * 1024 * 1024,
            )
            status = reply.status
            if len(reply.body) > 4 * 1024 * 1024:
                self._emit(
                    "provider.response_observed",
                    meter_epoch_id=self.session.epoch,
                    permit_id=permit.permit_id,
                    status_code=status,
                    response_bytes=len(reply.body),
                    decode_status="not_attempted",
                    usage_member_state="not_observed",
                )
                is_2xx = 200 <= status < 300
                finalize("response" if is_2xx else "failed", None, "response_too_large")
                if is_2xx:
                    raise WorkerExecutionError(
                        "provider_response_too_large",
                        "provider response exceeded the byte limit",
                    )
                raise ProviderError(status, "provider returned an HTTP error")
            root, decode_error = _decode_root(reply.body)
            if root is None:
                decode_state = (
                    "invalid_json"
                    if decode_error == "invalid_json"
                    else "invalid_response"
                )
                self._emit(
                    "provider.response_observed",
                    meter_epoch_id=self.session.epoch,
                    permit_id=permit.permit_id,
                    status_code=status,
                    response_bytes=len(reply.body),
                    decode_status=decode_state,
                    usage_member_state="not_observed",
                )
                is_2xx = 200 <= status < 300
                code = (
                    "invalid_json"
                    if decode_state == "invalid_json"
                    else "invalid_response"
                )
                finalize(
                    "response" if is_2xx else "failed",
                    None,
                    code if is_2xx else "http_status",
                )
                if is_2xx:
                    raise WorkerExecutionError(
                        "provider_invalid_json"
                        if code == "invalid_json"
                        else "provider_invalid_response",
                        "provider response could not be decoded"
                        if code == "invalid_json"
                        else "provider response root is invalid",
                    )
                raise ProviderError(status, "provider returned an HTTP error")
            if "usage" not in root:
                usage_state, raw_usage = "absent", None
            elif root["usage"] is None:
                usage_state, raw_usage = "null", None
            else:
                usage_state, raw_usage = "value", root["usage"]
            decoded_root = True
            self._emit(
                "provider.response_observed",
                meter_epoch_id=self.session.epoch,
                permit_id=permit.permit_id,
                status_code=status,
                response_bytes=len(reply.body),
                decode_status="root_object",
                usage_member_state=usage_state,
            )
            is_2xx = 200 <= status < 300
            late = self.session.clock() >= permit.deadline_monotonic
            if is_2xx and late:
                finalize("response", raw_usage, "deadline_exceeded", True)
                raise WorkerExecutionError(
                    "provider_deadline_exceeded", "provider request deadline exceeded"
                )
            if not is_2xx:
                finalize("failed", raw_usage, "http_status")
                raise ProviderError(status, "provider returned an HTTP error")
            finalize("response", raw_usage, None)
            if not result_eligible:
                raise WorkerExecutionError(
                    "provider_deadline_exceeded", "provider request deadline exceeded"
                )
            return root
        except (ProviderError, WorkerExecutionError):
            raise
        except TimeoutError:
            if finish_attempted:
                raise
            if reply is not None:
                is_2xx = status is not None and 200 <= status < 300
                finalize(
                    "response" if is_2xx else "failed",
                    raw_usage if decoded_root else None,
                    "internal_error" if is_2xx else "http_status",
                )
                if not is_2xx:
                    raise ProviderError(
                        status, "provider returned an HTTP error"
                    ) from None
                raise WorkerExecutionError(
                    "provider_internal_error", "provider request failed"
                ) from None
            if not finish_attempted:
                finalize("timeout", None, "transport_timeout")
            raise ProviderError(0, "provider transport timeout") from None
        except OSError:
            if finish_attempted:
                raise
            if reply is not None:
                is_2xx = status is not None and 200 <= status < 300
                finalize(
                    "response" if is_2xx else "failed",
                    raw_usage if decoded_root else None,
                    "internal_error" if is_2xx else "http_status",
                )
                if not is_2xx:
                    raise ProviderError(
                        status, "provider returned an HTTP error"
                    ) from None
                raise WorkerExecutionError(
                    "provider_internal_error", "provider request failed"
                ) from None
            if not finish_attempted:
                finalize("failed", None, "transport_error")
            raise ProviderError(0, "provider transport error") from None
        except http.client.IncompleteRead:
            if finish_attempted:
                raise
            if reply is not None:
                is_2xx = status is not None and 200 <= status < 300
                finalize(
                    "response" if is_2xx else "failed",
                    raw_usage if decoded_root else None,
                    "internal_error" if is_2xx else "http_status",
                )
                if not is_2xx:
                    raise ProviderError(
                        status, "provider returned an HTTP error"
                    ) from None
                raise WorkerExecutionError(
                    "provider_internal_error", "provider request failed"
                ) from None
            if not finish_attempted:
                finalize("failed", None, "transport_error")
            raise ProviderError(0, "provider transport error") from None
        except Exception:
            if not finish_attempted:
                code = (
                    "internal_error"
                    if status is None or 200 <= status < 300
                    else "http_status"
                )
                if status is not None:
                    final_outcome = "response" if 200 <= status < 300 else "failed"
                    try:
                        finalize(
                            final_outcome,
                            raw_usage if decoded_root else None,
                            code,
                        )
                    except Exception:
                        # This is cleanup for the original unknown exception. The
                        # meter may remain pending or may already be committed;
                        # a secondary finish failure must not replace its cause.
                        pass
                else:
                    try:
                        finalize("failed", None, code)
                    except Exception:
                        pass
            raise


def create_transport(role: str, worker_id: str):
    with _lock:
        config, _registry, _definitions = _validated_config()
        session = get_process_session()
        if session.fingerprint != _fingerprint(config):
            raise ValueError("G0c run meter configuration changed in this process")
        key = _reserve_role(session, role, worker_id)
        return session, key, RuntimeMeteredTransport(session, role)
