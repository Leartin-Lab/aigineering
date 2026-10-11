"""Best-effort local Worker invocation event capture and read projection.

These files are operational observations, not Runtime facts or task outputs.
"""

from __future__ import annotations

import base64
from contextvars import ContextVar
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - platform-specific safe fallback
    fcntl = None

from aigineering.core.domain import load_genesis

_SCHEMA = "aig-worker-log-v1"
_MAX_CAPTURE_BYTES = 256 * 1024
_MAX_CAPTURE_EVENTS = 512
_MAX_WORKSPACE_BYTES = 64 * 1024 * 1024
_PAGE_MAX = 100
_FAILURE_CATEGORIES = {"provider_error", "worker_execution_error", "other"}
_EVENT_FIELDS = {
    "invocation.started": set(),
    "candidate.returned": set(),
    "invocation.failed": {"category", "status_code", "retryable"},
    "invocation.finished": {"outcome", "duration_ms"},
    "provider.retry": {"status_code", "attempt", "max_retries", "backoff_seconds"},
    "provider.failed": {"status_code", "retryable"},
    "capture.truncated": {"reason"},
}
_OBSERVER: ContextVar["CaptureWriter | None"] = ContextVar(
    "aig_worker_log_observer", default=None
)


def store_log_root(db_path: str | os.PathLike[str]) -> Path:
    """Resolve the app-owned log root adjacent to the selected SQLite Store."""
    if os.fspath(db_path) == ":memory:":
        raise TaskLogError("capture_unavailable")
    path = Path(db_path).expanduser().absolute()
    return path.parent / "worker-logs"


def capture_path(root: Path, domain_id: str, claim_id: str) -> Path:
    """Return the deterministic private filename for one domain/claim pair."""
    claim_key = _digest((domain_id + "\0" + claim_id).encode())[:32]
    return root / f"{claim_key}.jsonl"


class TaskLogError(ValueError):
    """Safe public log inspection failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _valid_utc_stamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def _valid_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        remaining = len(data) - offset
        written = os.write(fd, data[offset:])
        if type(written) is not int or written <= 0 or written > remaining:
            raise OSError("capture write made no valid progress")
        offset += written


def _validate_event(event: Any, fields: Any) -> None:
    if not isinstance(event, str) or event not in _EVENT_FIELDS:
        raise ValueError
    if not isinstance(fields, dict) or set(fields) != _EVENT_FIELDS[event]:
        raise ValueError
    int_fields = {"status_code", "attempt", "max_retries", "duration_ms"}
    for key, value in fields.items():
        if key in int_fields and (type(value) is not int or value < 0):
            raise ValueError
        if key == "retryable" and type(value) is not bool:
            raise ValueError
        if key == "category" and value not in _FAILURE_CATEGORIES:
            raise ValueError
        if key == "outcome" and value not in {"returned", "failed", "error"}:
            raise ValueError
        if key == "reason" and value not in {"byte_limit", "event_limit"}:
            raise ValueError
        if key == "backoff_seconds" and (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or value < 0
            or value > 3600
        ):
            raise ValueError


def _open_quota_lock(root: Path) -> int:
    lock_path = root / ".quota.lock"
    try:
        prior = lock_path.lstat()
    except FileNotFoundError:
        prior = None
    if prior is not None and (
        not stat.S_ISREG(prior.st_mode) or stat.S_ISLNK(prior.st_mode)
    ):
        raise OSError("unsafe quota lock")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (
        prior is not None and (prior.st_dev, prior.st_ino) != (info.st_dev, info.st_ino)
    ):
        os.close(fd)
        raise OSError("unsafe quota lock")
    os.fchmod(fd, 0o600)
    return fd


def _hash_token(payload: dict[str, Any]) -> str:
    body = _canonical(payload)
    return (
        base64.urlsafe_b64encode(body + b"." + _digest(body).encode())
        .decode()
        .rstrip("=")
    )


def _read_token(token: str) -> dict[str, Any]:
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(raw) > 2048:
            raise ValueError
        body, check = raw.rsplit(b".", 1)
        if _digest(body).encode() != check:
            raise ValueError
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        raise TaskLogError("cursor_invalid") from None


def _secure_dir(path: Path, *, create: bool) -> None:
    try:
        parent_info = path.parent.lstat()
    except FileNotFoundError:
        raise TaskLogError("capture_unsafe_path") from None
    if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
        raise TaskLogError("capture_unsafe_path")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise TaskLogError("capture_missing") from None
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise TaskLogError("capture_unsafe_path")
    if create:
        os.chmod(path, 0o700)


class CaptureWriter:
    """Non-blocking best-effort writer bound to one claimed package."""

    def __init__(self, db_path: str | os.PathLike[str], binding: dict[str, Any]):
        self.root = store_log_root(db_path)
        self.binding = dict(binding)
        self.capture_id = uuid4().hex
        self.warning: str | None = None
        self.count = 0
        self.started = time.monotonic()
        self.previous = "0" * 64
        self.path: Path | None = None
        self._fd: int | None = None
        try:
            _secure_dir(self.root, create=True)
            self.path = capture_path(
                self.root, binding["domain_id"], binding["claim_id"]
            )
            header = {
                "schema": _SCHEMA,
                "kind": "header",
                "capture_id": self.capture_id,
                "created_at": _stamp(),
                "store_root": _digest(str(self.root.resolve()).encode()),
                **{
                    key: binding[key]
                    for key in (
                        "domain_id",
                        "contract_id",
                        "claim_id",
                        "epoch",
                        "package_id",
                        "worker_id",
                    )
                },
            }
            header["hash"] = _digest(_canonical(header))
            line = _canonical(header) + b"\n"
            self._fd = self._create_with_quota(line)
            self.previous = header["hash"]
        except TaskLogError as error:
            self.warning = (
                "capture_unavailable"
                if error.code == "capture_unsafe_path"
                else error.code
            )
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        except FileExistsError:
            self.warning = "capture_conflict"
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        except Exception:
            self.warning = "capture_unavailable"
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def _create_with_quota(self, line: bytes) -> int:
        if fcntl is None:
            raise TaskLogError("capture_unavailable")
        lock_fd = _open_quota_lock(self.root)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            size = sum(
                path.stat().st_size
                for path in self.root.glob("*.jsonl")
                if path.is_file()
            )
            if size + len(line) > _MAX_WORKSPACE_BYTES:
                raise TaskLogError("storage_limit")
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                _write_all(fd, line)
                os.fchmod(fd, 0o600)
                return fd
            except BaseException:
                os.close(fd)
                raise
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def emit(self, event: str, **fields: Any) -> None:
        """Append one allowlisted event; event failures never escape the Worker."""
        if self._fd is None or self.warning is not None:
            return
        try:
            _validate_event(event, fields)
            if self.count >= _MAX_CAPTURE_EVENTS - 1 and event != "invocation.finished":
                self._write_truncation_marker("event_limit")
                self.warning = "capture_truncated"
                return
            self.count += 1
            if self.count > _MAX_CAPTURE_EVENTS:
                self.warning = "capture_truncated"
                return
            row = {
                "schema": _SCHEMA,
                "kind": "event",
                "capture_id": self.capture_id,
                "sequence": self.count,
                "recorded_at": _stamp(),
                "event": event,
                "fields": fields,
                "previous_hash": self.previous,
            }
            row["hash"] = _digest(_canonical(row))
            line = _canonical(row) + b"\n"
            if os.fstat(self._fd).st_size + len(line) > _MAX_CAPTURE_BYTES:
                self.count -= 1
                self._write_truncation_marker("byte_limit")
                self.warning = "capture_truncated"
                return
            failure = self._append_with_quota(line)
            if failure is not None:
                self.warning = failure
                return
            self.previous = row["hash"]
        except Exception:
            self.warning = "capture_unavailable"

    def _write_truncation_marker(self, reason: str) -> None:
        if self._fd is None or self.count >= _MAX_CAPTURE_EVENTS:
            return
        self.count += 1
        row = {
            "schema": _SCHEMA,
            "kind": "event",
            "capture_id": self.capture_id,
            "sequence": self.count,
            "recorded_at": _stamp(),
            "event": "capture.truncated",
            "fields": {"reason": reason},
            "previous_hash": self.previous,
        }
        row["hash"] = _digest(_canonical(row))
        line = _canonical(row) + b"\n"
        if os.fstat(self._fd).st_size + len(line) > _MAX_CAPTURE_BYTES:
            self.count -= 1
            return
        failure = self._append_with_quota(line)
        if failure is not None:
            self.count -= 1
            self.warning = failure
            return
        self.previous = row["hash"]

    def _append_with_quota(self, line: bytes) -> str | None:
        if fcntl is None:
            return "capture_unavailable"
        lock_fd = None
        try:
            lock_fd = _open_quota_lock(self.root)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            size = sum(
                path.stat().st_size
                for path in self.root.glob("*.jsonl")
                if path.is_file()
            )
            if size + len(line) > _MAX_WORKSPACE_BYTES:
                return "storage_limit"
            _write_all(self._fd, line)
            return None
        except (OSError, ImportError):
            return "capture_unavailable"
        finally:
            if lock_fd is not None:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_fd)

    def close(self, outcome: str = "error") -> str | None:
        """Close capture without affecting execution; return a safe warning code."""
        if self._fd is not None:
            try:
                os.fsync(self._fd)
            except OSError:
                self.warning = self.warning or "capture_unavailable"
            try:
                os.close(self._fd)
            except OSError:
                self.warning = self.warning or "capture_unavailable"
            self._fd = None
        return self.warning


def current_observer() -> CaptureWriter | None:
    return _OBSERVER.get()


def observer_scope(observer: CaptureWriter | None):
    """Context manager installing and restoring an invocation-local observer."""
    from contextlib import contextmanager

    @contextmanager
    def scope():
        token = _OBSERVER.set(observer)
        try:
            yield
        finally:
            _OBSERVER.reset(token)

    return scope()


def open_capture(db_path: str | os.PathLike[str], store, claimed) -> CaptureWriter:
    """Create a capture after claim; failures become a warning, never a gate."""
    try:
        genesis = load_genesis(store)
        binding = {
            "domain_id": genesis.id,
            "contract_id": claimed.contract.id,
            "claim_id": claimed.package.claim_id,
            "epoch": claimed.package.claim_epoch,
            "package_id": claimed.package.package_id,
            "worker_id": claimed.worker_id,
        }
        return CaptureWriter(db_path, binding)
    except Exception:
        writer = object.__new__(CaptureWriter)
        writer.root = Path(".")
        writer.binding = {}
        writer.capture_id = uuid4().hex
        writer.warning = "capture_unavailable"
        writer.count = 0
        writer.started = time.monotonic()
        writer.previous = "0" * 64
        writer.path = None
        writer._fd = None
        return writer


def execute_captured_package(
    claimed,
    worker,
    store,
    *,
    db_path: str | os.PathLike[str],
    trace_store=None,
    candidate_publishers=None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Run the canonical runtime path with one best-effort local capture."""
    from aigineering.runtime import execute_claimed_package

    writer = open_capture(db_path, store, claimed)
    outcome = "error"
    try:
        result = execute_claimed_package(
            claimed,
            worker,
            store,
            trace_store,
            candidate_publishers=candidate_publishers,
            event_observer=writer,
        )
        outcome = "returned"
        return result
    except BaseException:
        raise
    finally:
        warning = writer.close(outcome)
        if warning and warnings is not None:
            warnings.append(warning)


def read_capture(
    root: Path, binding: dict[str, Any], *, limit: int = 20, cursor: str | None = None
) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= _PAGE_MAX:
        raise TaskLogError("invalid_limit")
    try:
        _secure_dir(root, create=False)
    except PermissionError:
        raise TaskLogError("capture_unavailable") from None
    path = capture_path(root, binding["domain_id"], binding["claim_id"])
    try:
        path_info = path.lstat()
    except FileNotFoundError:
        raise TaskLogError("capture_missing") from None
    except PermissionError:
        raise TaskLogError("capture_unavailable") from None
    if not stat.S_ISREG(path_info.st_mode) or stat.S_ISLNK(path_info.st_mode):
        raise TaskLogError("capture_unsafe_path")
    if path_info.st_size > _MAX_CAPTURE_BYTES:
        raise TaskLogError("capture_corrupt")
    read_fd = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        read_fd = os.open(path, flags)
        opened_info = os.fstat(read_fd)
        if not stat.S_ISREG(opened_info.st_mode) or (
            opened_info.st_dev,
            opened_info.st_ino,
        ) != (path_info.st_dev, path_info.st_ino):
            raise TaskLogError("capture_unsafe_path")
        chunks = []
        remaining = _MAX_CAPTURE_BYTES + 1
        while remaining > 0:
            chunk = os.read(read_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_CAPTURE_BYTES:
            raise TaskLogError("capture_corrupt")
        if not raw.endswith(b"\n"):
            raise TaskLogError("capture_incomplete")
        raw_lines = raw.splitlines(keepends=True)
        rows = [json.loads(line) for line in raw_lines]
        header = rows[0]
        if not isinstance(header, dict) or set(header) != {
            "schema",
            "kind",
            "capture_id",
            "created_at",
            "store_root",
            "domain_id",
            "contract_id",
            "claim_id",
            "epoch",
            "package_id",
            "worker_id",
            "hash",
        }:
            raise TaskLogError("capture_corrupt")
        saved_hash = header.pop("hash")
        if (
            not _valid_digest(saved_hash)
            or saved_hash != _digest(_canonical(header))
            or header.get("schema") != _SCHEMA
            or header.get("kind") != "header"
            or not isinstance(header.get("capture_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", header["capture_id"]) is None
            or not _valid_utc_stamp(header.get("created_at"))
            or not _valid_digest(header.get("store_root"))
            or any(
                not isinstance(header.get(key), str) or not header[key]
                for key in (
                    "domain_id",
                    "contract_id",
                    "claim_id",
                    "package_id",
                    "worker_id",
                )
            )
            or type(header.get("epoch")) is not int
            or header["epoch"] < 0
        ):
            raise TaskLogError("capture_corrupt")
        header["hash"] = saved_hash
        if header.get("schema") != _SCHEMA or header.get("kind") != "header":
            raise TaskLogError("capture_corrupt")
        for key in (
            "domain_id",
            "contract_id",
            "claim_id",
            "epoch",
            "package_id",
            "worker_id",
        ):
            if header.get(key) != binding.get(key):
                raise TaskLogError("capture_binding_mismatch")
        if header.get("store_root") != _digest(str(root.resolve()).encode()):
            raise TaskLogError("capture_binding_mismatch")
        previous = saved_hash
        events = []
        event_end_offsets = []
        byte_offset = len(raw_lines[0])
        for sequence, row in enumerate(rows[1:], start=1):
            expected_keys = {
                "schema",
                "kind",
                "capture_id",
                "sequence",
                "recorded_at",
                "event",
                "fields",
                "previous_hash",
                "hash",
            }
            if not isinstance(row, dict) or set(row) != expected_keys:
                raise TaskLogError("capture_corrupt")
            expected_hash = row.pop("hash")
            if (
                not _valid_digest(expected_hash)
                or not _valid_digest(row.get("previous_hash"))
                or type(row.get("sequence")) is not int
                or row.get("previous_hash") != previous
                or row.get("sequence") != sequence
                or row.get("capture_id") != header.get("capture_id")
                or expected_hash != _digest(_canonical(row))
            ):
                raise TaskLogError("capture_corrupt")
            row["hash"] = expected_hash
            previous = expected_hash
            fields = row.get("fields")
            if (
                row.get("schema") != _SCHEMA
                or row.get("kind") != "event"
                or not isinstance(fields, dict)
            ):
                raise TaskLogError("capture_corrupt")
            try:
                _validate_event(row.get("event"), fields)
                if not _valid_utc_stamp(row.get("recorded_at")):
                    raise ValueError
            except Exception:
                raise TaskLogError("capture_corrupt") from None
            events.append(row)
            byte_offset += len(raw_lines[sequence])
            event_end_offsets.append(byte_offset)
    except TaskLogError:
        raise
    except PermissionError:
        raise TaskLogError("capture_unavailable") from None
    except Exception:
        raise TaskLogError("capture_corrupt") from None
    finally:
        if read_fd is not None:
            os.close(read_fd)
    offset = 0
    if cursor:
        value = _read_token(cursor)
        if set(value) != {
            "v",
            "store_root",
            "domain_id",
            "contract_id",
            "claim_id",
            "capture_id",
            "sequence",
            "byte_offset",
            "prefix_hash",
        }:
            raise TaskLogError("cursor_invalid")
        if type(value.get("v")) is not int or type(value.get("byte_offset")) is not int:
            raise TaskLogError("cursor_invalid")
        expected = {
            "v": 1,
            "store_root": header["store_root"],
            "domain_id": header["domain_id"],
            "contract_id": header["contract_id"],
            "claim_id": header["claim_id"],
            "capture_id": header["capture_id"],
        }
        if any(value.get(key) != item for key, item in expected.items()):
            raise TaskLogError("cursor_scope_mismatch")
        offset = value.get("sequence")
        if type(offset) is not int or not 0 <= offset <= len(events):
            raise TaskLogError("cursor_invalid")
        prefix_hash = header["hash"] if offset == 0 else events[offset - 1]["hash"]
        byte_boundary = (
            len(raw_lines[0]) if offset == 0 else event_end_offsets[offset - 1]
        )
        if not _valid_digest(value.get("prefix_hash")):
            raise TaskLogError("cursor_invalid")
        if (
            value.get("prefix_hash") != prefix_hash
            or value.get("byte_offset") != byte_boundary
        ):
            raise TaskLogError("cursor_prefix_changed")
    page = events[offset : offset + limit]
    end = offset + len(page)
    more = end < len(events)
    byte_boundary = len(raw_lines[0]) if end == 0 else event_end_offsets[end - 1]
    token_data = {
        "v": 1,
        "store_root": header["store_root"],
        "domain_id": header["domain_id"],
        "contract_id": header["contract_id"],
        "claim_id": header["claim_id"],
        "capture_id": header["capture_id"],
        "sequence": end,
        "byte_offset": byte_boundary,
        "prefix_hash": header["hash"] if end == 0 else events[end - 1]["hash"],
    }
    next_cursor = _hash_token(token_data)
    complete = any(row.get("event") == "invocation.finished" for row in events)
    truncated_capture = (
        any(row.get("event") == "capture.truncated" for row in events)
        or (len(events) >= _MAX_CAPTURE_EVENTS and not complete)
        or (len(raw) >= _MAX_CAPTURE_BYTES and not complete)
    )
    capture_status = (
        "truncated" if truncated_capture else "complete" if complete else "incomplete"
    )
    warnings = ["capture_truncated"] if truncated_capture else []
    return {
        "ok": True,
        "data": {
            "contract_id": header["contract_id"],
            "domain_id": header["domain_id"],
            "claim_id": header["claim_id"],
            "capture_id": header["capture_id"],
            "events": [
                {
                    key: value
                    for key, value in row.items()
                    if key != "hash" and key != "previous_hash"
                }
                for row in page
            ],
            "next_cursor": next_cursor,
            "truncated": more,
            "follow_status": capture_status,
            "available": True,
            "warnings": warnings,
        },
        "revision": binding.get("watermark"),
    }
