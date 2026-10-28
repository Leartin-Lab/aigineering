"""Private local process owner for persistent canonical Fleet pull loops."""

from __future__ import annotations

from dataclasses import dataclass, replace

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows control is unsupported
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4

from aigineering.adapters.redis_query import QueryProjectionUnavailable
from aigineering.application import require_redis
from aigineering.core.domain import load_genesis
from aigineering.core.sqlite_store import SQLiteStore
from aigineering.plugins import default_completion_registry
from aigineering.local_fleet import (
    FleetHost,
    PullAdmissionGate,
    run_persistent_local_fleet,
)
from aigineering.local_identity import (
    ensure_local_runtime_publishers,
    ensure_local_worker_host,
)
from aigineering.run_config import prepare_fleet_resources
from aigineering.runtime_services import sync_runtime
from aigineering.runtime import run_runtime_maintenance_step
from aigineering.fleet_config import load_fleet_config

_SCHEMA = "aig-fleet-service-v1"
_STATE_NAME = "state.json"
_LOCK_NAME = "owner.lock"
_STOP_NAME = "stop.json"
_READINESS_TIMEOUT = 20.0
_STATE_WRITE_LOCK = threading.RLock()
_TERMINAL_SERVICE_STATES = frozenset({"stopped", "failed", "degraded"})
_SERVICE_STATE_TRANSITIONS = {
    "starting": frozenset({"starting", "ready", "failed", "degraded"}),
    "ready": frozenset({"ready", "stopping", "stopped", "failed", "degraded"}),
    "stopping": frozenset({"stopping", "stopped", "failed"}),
    "stopped": frozenset({"stopped"}),
    "failed": frozenset({"failed"}),
    "degraded": frozenset({"degraded"}),
}


class FleetServiceError(RuntimeError):
    """Safe, stable error exposed by the local Fleet lifecycle commands."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ServiceResult:
    state: str
    generation: str | None
    domain_id: str | None
    active_slots: int = 0
    worker_ids: tuple[str, ...] = ()
    registration_versions: tuple[str, ...] = ()
    config_sha256: str | None = None
    config_drift: bool | None = None
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "generation": self.generation,
            "domain_id": self.domain_id,
            "active_slots": self.active_slots,
            "worker_ids": list(self.worker_ids),
            "registration_versions": list(self.registration_versions),
            "config_sha256": self.config_sha256,
            "config_drift": self.config_drift,
            "error_code": self.error_code,
        }


def canonical_service_store_path(workspace: str | Path, configured_path: str) -> Path:
    """Require the service to scan the same Store as the selected CLI."""
    root = Path(workspace).resolve()
    if not isinstance(configured_path, str) or not configured_path.strip():
        raise FleetServiceError("invalid_store_path", "Fleet db_path must be non-empty")
    if configured_path == ":memory:":
        raise FleetServiceError(
            "invalid_store_path",
            "persistent Fleet requires the selected workspace Store",
        )
    selected = Path(configured_path)
    if not selected.is_absolute():
        selected = root / selected
    selected = selected.resolve(strict=False)
    expected = (root / ".aig" / "store.db").resolve(strict=False)
    if selected != expected:
        raise FleetServiceError(
            "invalid_store_path",
            "persistent Fleet must use the selected workspace Store (.aig/store.db)",
        )
    return expected


def write_service_metadata(path: str | Path, payload: dict[str, object]) -> None:
    """Atomically replace private process metadata without following symlinks."""
    target = Path(path)
    _ensure_private_directory(target.parent)
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.chmod(target, 0o600)


def read_service_metadata(path: str | Path) -> dict[str, object] | None:
    target = Path(path)
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FleetServiceError(
            "unsafe_state", "Fleet service metadata is unavailable"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 64 * 1024
            ):
                raise FleetServiceError(
                    "unsafe_state", "Fleet service metadata is invalid"
                )
            raw = stream.read(64 * 1024 + 1)
        value = json.loads(raw.decode("utf-8"))
    except FleetServiceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FleetServiceError(
            "unsafe_state", "Fleet service metadata is invalid"
        ) from exc
    if not isinstance(value, dict) or value.get("schema") != _SCHEMA:
        raise FleetServiceError("unsafe_state", "Fleet service metadata is invalid")
    return value


def start_service(
    workspace: str | Path,
    config_path: str | Path,
    *,
    readiness_timeout: float = _READINESS_TIMEOUT,
) -> ServiceResult:
    _require_process_lock_support()
    root = Path(workspace).resolve()
    try:
        config = Path(config_path)
        if not config.is_absolute():
            config = root / config
        config = config.resolve(strict=True)
        if not config.is_file():
            raise OSError("configuration is not a regular file")
        config_digest = _sha256_file(config)
        fleet = load_fleet_config(config)
    except (OSError, ValueError) as exc:
        raise FleetServiceError(
            "invalid_config", "Fleet configuration is invalid or unavailable"
        ) from exc
    _require_llm_credentials(fleet)
    db_path = canonical_service_store_path(root, fleet.db_path)
    if not db_path.is_file():
        raise FleetServiceError(
            "workspace_uninitialized",
            "Workspace Store is missing; initialize its domain first",
        )
    _prevalidate_fleet_adapters(fleet, config.parent)

    # Read the exact existing domain and test operational Redis before creating
    # a generation or publishing local Worker setup.
    try:
        require_redis()
        with SQLiteStore(db_path) as store:
            genesis = load_genesis(store)
            domain_id = genesis.id
    except QueryProjectionUnavailable as exc:
        raise FleetServiceError(
            "redis_unavailable", "Redis collaboration is unavailable"
        ) from exc
    except FleetServiceError:
        raise
    except (OSError, LookupError, sqlite3.Error, ValueError) as exc:
        raise FleetServiceError(
            "workspace_uninitialized", "Workspace domain is unavailable"
        ) from exc
    except Exception as exc:
        raise FleetServiceError(
            "service_internal_error", "Fleet service preflight failed internally"
        ) from exc

    service_dir = _service_dir(root)
    _validated_prior_state(service_dir, root)
    lock_path = service_dir / _LOCK_NAME
    lock_fd = _open_lock(lock_path)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            current = _validated_prior_state(service_dir, root)
            if current is None:
                raise FleetServiceError(
                    "unsafe_state", "Fleet service lock is live but metadata is missing"
                )
            return _result_from_state(current, fallback="running")

        # A valid stale generation may be replaced only after exclusive lock
        # acquisition and a fresh validation of the metadata it left behind.
        _validated_prior_state(service_dir, root)

        generation = uuid4().hex
        state = {
            "schema": _SCHEMA,
            "state": "starting",
            "generation": generation,
            "pid": None,
            "workspace": str(root),
            "domain_id": domain_id,
            "config_path": str(config),
            "config_sha256": config_digest,
            "worker_ids": [],
            "registration_versions": [],
            "active_slots": 0,
            "error_code": None,
        }
        write_service_metadata(service_dir / _STATE_NAME, state)
        child_env = os.environ.copy()
        child_env["AIG_FLEET_SERVICE_STATE"] = str(service_dir / _STATE_NAME)
        child_env["AIG_FLEET_SERVICE_LOCK_FD"] = str(lock_fd)
        child_env["AIG_FLEET_SERVICE_WORKSPACE"] = str(root)
        child_env.pop("AIG_FLEET_SERVICE_GENERATION", None)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "aigineering.fleet_service"],
                cwd=root,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=(lock_fd,),
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            current = _validated_prior_state(service_dir, root)
            if current is not None and current.get("generation") == generation:
                if current.get("state") == "starting":
                    current.update(state="failed", error_code="service_start_failed")
                    write_service_metadata(service_dir / _STATE_NAME, current)
            raise FleetServiceError(
                "service_start_failed", "Could not start Fleet service"
            ) from exc
        # The child owns state after launch. It may already have written ready
        # or a specific startup failure before Popen returns; never replay the
        # parent's stale starting snapshot over that result.
        deadline = time.monotonic() + readiness_timeout
        while time.monotonic() < deadline:
            latest = read_service_metadata(service_dir / _STATE_NAME)
            if latest is None or latest.get("generation") != generation:
                raise FleetServiceError(
                    "unsafe_state", "Fleet service generation changed during startup"
                )
            if latest.get("state") == "ready":
                return _result_from_state(latest, fallback="ready")
            if latest.get("state") in {"failed", "stopped", "degraded"}:
                raise FleetServiceError(
                    str(latest.get("error_code") or "service_start_failed"),
                    "Fleet service could not become ready",
                )
            if process.poll() is not None:
                raise FleetServiceError(
                    "service_start_failed", "Fleet service exited before readiness"
                )
            time.sleep(0.05)
        raise FleetServiceError(
            "service_start_timeout",
            "Fleet service readiness timed out; inspect fleet status",
        )
    finally:
        os.close(lock_fd)


def status_service(workspace: str | Path) -> ServiceResult:
    root = Path(workspace).resolve()
    service_dir = _existing_service_dir(root)
    if service_dir is None:
        return ServiceResult("stopped", None, None)
    _require_process_lock_support()
    state = _validated_prior_state(service_dir, root)
    if state is None:
        if _probe_lock(service_dir / _LOCK_NAME):
            raise FleetServiceError(
                "unsafe_state", "Fleet service lock is live but metadata is missing"
            )
        return ServiceResult("stopped", None, None)
    active_result = _result_from_state(state, fallback="running")
    lock_fd = _open_lock(service_dir / _LOCK_NAME, create=False)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return active_result
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
    recorded = state.get("state")
    result = _result_from_state(
        state,
        fallback="stale" if recorded not in {"stopped", "failed"} else str(recorded),
    )
    return (
        replace(result, state="stale")
        if recorded not in {"stopped", "failed"}
        else result
    )


def stop_service(workspace: str | Path, *, wait_timeout: float = 30.0) -> ServiceResult:
    if wait_timeout <= 0:
        raise FleetServiceError("invalid_timeout", "wait timeout must be positive")
    root = Path(workspace).resolve()
    service_dir = _existing_service_dir(root)
    if service_dir is None:
        return ServiceResult("stopped", None, None)
    _require_process_lock_support()
    state_path = service_dir / _STATE_NAME
    state = _validated_prior_state(service_dir, root)
    if state is None:
        if _probe_lock(service_dir / _LOCK_NAME):
            raise FleetServiceError(
                "unsafe_state", "Fleet service lock is live but metadata is missing"
            )
        return ServiceResult("stopped", None, None)
    generation = state.get("generation")
    if not isinstance(generation, str):
        raise FleetServiceError("unsafe_state", "Fleet service generation is invalid")
    lock_fd = _open_lock(service_dir / _LOCK_NAME, create=False)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _write_control(
                service_dir / _STOP_NAME,
                {"schema": _SCHEMA, "generation": generation, "action": "stop"},
            )
        else:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            return replace(_result_from_state(state, fallback="stale"), state="stale")
    finally:
        os.close(lock_fd)

    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        latest = read_service_metadata(state_path)
        if latest is None or latest.get("generation") != generation:
            if latest is None:
                return ServiceResult("stopped", generation, state.get("domain_id"))
            # A successor generation owns a new control lifetime. This stop
            # request never signals it and must not report it as stopped.
            return status_service(root)
        if latest.get("state") in {"stopped", "failed"}:
            # Metadata may lead lock release by a few instructions; prove the
            # generation no longer owns the process lock before acknowledging.
            probe = _open_lock(service_dir / _LOCK_NAME, create=False)
            try:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe, fcntl.LOCK_UN)
                    return _result_from_state(latest, fallback="stopped")
                except BlockingIOError:
                    pass
            finally:
                os.close(probe)
        time.sleep(0.05)
    latest = read_service_metadata(state_path) or state
    result = _result_from_state(latest, fallback="stopping")
    return replace(result, state="stopping")


def restart_service(
    workspace: str | Path,
    config_path: str | Path,
    *,
    wait_timeout: float = 30.0,
) -> ServiceResult:
    prior = stop_service(workspace, wait_timeout=wait_timeout)
    if prior.state not in {"stopped", "failed", "stale"}:
        raise FleetServiceError(
            "service_draining", "Prior Fleet generation is still draining"
        )
    return start_service(workspace, config_path)


def _serve_from_environment() -> int:
    state_path_raw = os.environ.get("AIG_FLEET_SERVICE_STATE")
    workspace_raw = os.environ.get("AIG_FLEET_SERVICE_WORKSPACE")
    lock_fd_raw = os.environ.get("AIG_FLEET_SERVICE_LOCK_FD")
    if not state_path_raw or not workspace_raw or not lock_fd_raw:
        return 2
    state_path = Path(state_path_raw)
    root = Path(workspace_raw).resolve()
    try:
        lock_fd = int(lock_fd_raw)
        os.fstat(lock_fd)
        initial = read_service_metadata(state_path)
        if initial is None or initial.get("state") != "starting":
            return 2
        generation = initial.get("generation")
        if not isinstance(generation, str):
            return 2
        config = Path(str(initial["config_path"]))
        if _sha256_file(config) != initial.get("config_sha256"):
            _update_state(
                state_path, initial, state="failed", error_code="config_changed"
            )
            return 1
        fleet = load_fleet_config(config)
        _require_llm_credentials(fleet)
        db_path = canonical_service_store_path(root, fleet.db_path)
        if db_path != (root / ".aig" / "store.db").resolve(strict=False):
            _update_state(
                state_path, initial, state="failed", error_code="invalid_store_path"
            )
            return 1
        resources = prepare_fleet_resources(fleet, base_dir=config.parent)
        with SQLiteStore(db_path) as store:
            genesis = load_genesis(store)
            if genesis.id != initial.get("domain_id"):
                raise FleetServiceError(
                    "domain_changed", "Workspace Genesis changed during startup"
                )
            require_redis()
            hosts: list[FleetHost] = []
            versions: list[str] = []
            for spec, worker in resources.workers:
                host = ensure_local_worker_host(
                    store,
                    worker,
                    effect_capabilities=spec.effect_capabilities,
                )
                hosts.append(FleetHost(host, capacity=spec.capacity))
                versions.append(host.registration_version)
            sync_runtime(store)
        maintenance_store = SQLiteStore(db_path)
        maintenance_publishers = ensure_local_runtime_publishers(maintenance_store)
        completion_registry = default_completion_registry()

        def maintenance_tick() -> None:
            require_redis()
            run_runtime_maintenance_step(
                maintenance_store,
                completion_registry,
                candidate_publishers=maintenance_publishers,
            )
            sync_runtime(maintenance_store)

        maintenance_tick()
        _update_state(
            state_path,
            initial,
            state="ready",
            pid=os.getpid(),
            worker_ids=[item.host.worker_id for item in hosts],
            registration_versions=versions,
            error_code=None,
            active_slots=0,
        )
        gate = PullAdmissionGate()
        stop_event = threading.Event()
        stop_file = state_path.parent / _STOP_NAME

        def requested() -> bool:
            request = _read_control(stop_file)
            return bool(
                request
                and request.get("schema") == _SCHEMA
                and request.get("action") == "stop"
                and request.get("generation") == generation
            )

        def update(active: int) -> None:
            _update_state(
                state_path,
                initial,
                state="stopping" if gate.closed else "ready",
                active_slots=active,
            )

        stop_watcher = threading.Thread(
            target=lambda: _watch_stop(
                requested, gate, stop_event, state_path, initial
            ),
            name="aig-fleet-stop-gate",
            daemon=True,
        )
        stop_watcher.start()
        try:
            run_persistent_local_fleet(
                str(db_path),
                tuple(hosts),
                gate=gate,
                stop_event=stop_event,
                stop_requested=requested,
                on_update=update,
                on_tick=maintenance_tick,
                poll_interval=fleet.poll_interval,
            )
        finally:
            gate.close()
            stop_event.set()
            # A timed join is not proof that an older watcher cannot write.
            stop_watcher.join()
            maintenance_store.close()
        # Publish successful termination only after the slot runner has joined,
        # the stop watcher has exited, and the maintenance Store has closed.
        _update_state(state_path, initial, state="stopped", active_slots=0)
        return 0
    except QueryProjectionUnavailable:
        _update_state(
            state_path,
            initial if "initial" in locals() else {},
            state="failed",
            error_code="redis_unavailable",
        )
        return 1
    except FleetServiceError as exc:
        _update_state(
            state_path,
            initial if "initial" in locals() else {},
            state="failed",
            error_code=exc.code,
        )
        return 1
    except Exception:
        _update_state(
            state_path,
            initial if "initial" in locals() else {},
            state="failed",
            error_code="service_internal_error",
        )
        return 1


def _watch_stop(requested, gate, stop_event, state_path, initial) -> None:
    while not stop_event.wait(0.05):
        if requested():
            gate.close()
            _update_state(state_path, initial, state="stopping")
            stop_event.set()
            return


def _require_llm_credentials(fleet) -> None:
    """Refuse persistent admission when a configured LLM has no key in env."""
    fallback_names = ("AIGINEERING_API_KEY", "OPENAI_API_KEY")
    missing = []
    for spec in fleet.workers:
        if spec.kind != "llm":
            continue
        configured = os.environ.get(spec.api_key_env, "") if spec.api_key_env else ""
        fallback = any(os.environ.get(name, "").strip() for name in fallback_names)
        if not configured.strip() and not fallback:
            missing.append(spec.worker_id)
    if missing:
        raise FleetServiceError(
            "worker_credentials_missing",
            "LLM credentials are required for configured persistent Fleet workers",
        )


def _prevalidate_fleet_adapters(fleet, base_dir: Path) -> None:
    """Use the shared resource owner to validate constructibility before Popen.

    The child constructs its own process-local adapters again after launch.
    Worker factories retain their strict existing module:callable semantics.
    """
    try:
        prepare_fleet_resources(fleet, base_dir=base_dir)
    except Exception as exc:
        raise FleetServiceError(
            "worker_adapter_invalid", "Configured Fleet adapters are invalid"
        ) from exc


def _validated_prior_state(
    service_dir: Path, workspace: Path
) -> dict[str, object] | None:
    state = read_service_metadata(service_dir / _STATE_NAME)
    if state is None:
        return None
    if state.get("workspace") != str(workspace):
        raise FleetServiceError(
            "unsafe_state", "Fleet service metadata belongs to another workspace"
        )
    config_path = state.get("config_path")
    config_digest = state.get("config_sha256")
    if (
        not isinstance(config_path, str)
        or not Path(config_path).is_absolute()
        or not isinstance(config_digest, str)
        or len(config_digest) != 64
        or any(char not in "0123456789abcdef" for char in config_digest)
    ):
        raise FleetServiceError("unsafe_state", "Fleet service metadata is invalid")
    try:
        config_info = Path(config_path).lstat()
    except FileNotFoundError:
        config_info = None
    except OSError as exc:
        raise FleetServiceError(
            "unsafe_state", "Fleet service metadata is invalid"
        ) from exc
    if config_info is not None and (
        stat.S_ISLNK(config_info.st_mode) or not stat.S_ISREG(config_info.st_mode)
    ):
        raise FleetServiceError("unsafe_state", "Fleet service metadata is invalid")
    _result_from_state(state, fallback="stale")
    return state


def _result_from_state(
    state: dict[str, object] | None, *, fallback: str
) -> ServiceResult:
    if state is None:
        return ServiceResult(fallback, None, None)
    state_name = state.get("state")
    generation = state.get("generation")
    domain_id = state.get("domain_id")
    active_slots = state.get("active_slots", 0)
    worker_ids = state.get("worker_ids", [])
    versions = state.get("registration_versions", [])
    if (
        not isinstance(state_name, str)
        or state_name
        not in {"starting", "ready", "stopping", "stopped", "failed", "degraded"}
        or not isinstance(generation, str)
        or len(generation) != 32
        or any(char not in "0123456789abcdef" for char in generation)
        or (domain_id is not None and not isinstance(domain_id, str))
        or type(active_slots) is not int
        or active_slots < 0
        or not isinstance(worker_ids, list)
        or not all(isinstance(item, str) for item in worker_ids)
        or not isinstance(versions, list)
        or not all(isinstance(item, str) for item in versions)
        or len(worker_ids) != len(versions)
    ):
        raise FleetServiceError("unsafe_state", "Fleet service metadata is invalid")
    config_path = state.get("config_path")
    config_digest = state.get("config_sha256")
    config_drift = None
    if isinstance(config_path, str) and isinstance(config_digest, str):
        try:
            config_drift = _sha256_file(Path(config_path)) != config_digest
        except FleetServiceError:
            config_drift = True
    return ServiceResult(
        state=state_name or fallback,
        generation=generation,
        domain_id=domain_id,
        active_slots=active_slots,
        worker_ids=tuple(worker_ids),
        registration_versions=tuple(versions),
        config_sha256=config_digest if isinstance(config_digest, str) else None,
        config_drift=config_drift,
        error_code=state.get("error_code")
        if isinstance(state.get("error_code"), str)
        else None,
    )


def _update_state(path, base, **updates) -> None:
    # State is private process metadata, not a cross-process task/claim fence.
    # Serialize the whole read/merge/replace among this generation's writers.
    with _STATE_WRITE_LOCK:
        current = read_service_metadata(path) or dict(base)
        generation = base.get("generation")
        if current.get("generation") != generation:
            return

        current_state = current.get("state")
        requested_state = updates.get("state")
        if current_state in _TERMINAL_SERVICE_STATES:
            # Freeze the complete terminal snapshot, including active_slots and
            # error_code; late callbacks cannot replay stale observations.
            return
        if requested_state is not None:
            allowed = _SERVICE_STATE_TRANSITIONS.get(current_state)
            if allowed is None or requested_state not in allowed:
                # A late ready callback may still report the current slot count,
                # but cannot reopen admission after stopping was observed.
                updates = dict(updates)
                updates.pop("state", None)
                if not updates:
                    return
        current.update(updates)
        current["updated_at"] = time.time()
        write_service_metadata(path, current)


def _write_control(path: Path, payload: dict[str, object]) -> None:
    write_service_metadata(path, payload)


def _read_control(path: Path) -> dict[str, object] | None:
    try:
        return read_service_metadata(path)
    except FleetServiceError:
        return None


def _service_dir(workspace: Path, *, create: bool = True) -> Path:
    aig_dir = workspace / ".aig"
    if create:
        _ensure_workspace_aig_directory(aig_dir)
    else:
        _validate_directory(aig_dir, allow_broad=True)
    service_dir = aig_dir / "fleet-service"
    if create:
        _ensure_private_directory(service_dir)
    else:
        _validate_directory(service_dir)
    return service_dir


def _existing_service_dir(workspace: Path) -> Path | None:
    aig_dir = workspace / ".aig"
    try:
        aig_info = aig_dir.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(aig_info.st_mode) or stat.S_ISLNK(aig_info.st_mode):
        raise FleetServiceError("unsafe_state", "Workspace state directory is unsafe")
    service_dir = aig_dir / "fleet-service"
    try:
        service_info = service_dir.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(service_info.st_mode) or stat.S_ISLNK(service_info.st_mode):
        raise FleetServiceError(
            "unsafe_state", "Fleet service state directory is unsafe"
        )
    if stat.S_IMODE(service_info.st_mode) & 0o077:
        raise FleetServiceError(
            "unsafe_state", "Fleet service state directory permissions are too broad"
        )
    return service_dir


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    except FileNotFoundError:
        if path.parent != path:
            _ensure_private_directory(path.parent)
            path.mkdir(mode=0o700, exist_ok=True)
    _validate_directory(path)
    os.chmod(path, 0o700)


def _ensure_workspace_aig_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise FleetServiceError(
            "service_state_unavailable", "Fleet service state directory is unavailable"
        ) from exc
    _validate_directory(path, allow_broad=True)


def _validate_directory(path: Path, *, allow_broad: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise FleetServiceError(
            "service_state_unavailable", "Fleet service state directory is unavailable"
        ) from exc
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise FleetServiceError(
            "unsafe_state", "Fleet service state directory is unsafe"
        )
    if not allow_broad and stat.S_IMODE(info.st_mode) & 0o077:
        raise FleetServiceError(
            "unsafe_state", "Fleet service state directory permissions are too broad"
        )


def _open_lock(path: Path, *, create: bool = True) -> int:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise FleetServiceError(
            "service_lock_unavailable", "Fleet service lock is unavailable"
        ) from exc
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise FleetServiceError("unsafe_state", "Fleet service lock is unsafe")
    os.fchmod(descriptor, 0o600)
    return descriptor


def _probe_lock(path: Path) -> bool:
    try:
        descriptor = _open_lock(path, create=False)
    except FleetServiceError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise FleetServiceError(
            "invalid_config", "Fleet configuration is unavailable"
        ) from exc
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_process_lock_support() -> None:
    if fcntl is None or os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise FleetServiceError(
            "unsupported_platform",
            "persistent Fleet process control requires POSIX file locks and no-follow file access",
        )


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(_serve_from_environment())
