"""Private immutable CLI inputs for exact assignment-request recovery.

These files preserve operator intent only. They are not signed facts,
authorization, completion state, or a queue.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any

from aigineering.core.claim_assignments import (
    AssignmentConflict,
    assignment_context_from_projection,
    assignment_head,
)
from aigineering.core.domain import load_genesis
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.protocol.identity import canonical_json

REQUEST_SCHEMA = "aig-task-reassignment-request-v1"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 65536
MAX_TARGETS = 64
_REQUEST_FIELDS = frozenset(
    {
        "schema",
        "store_path",
        "domain_id",
        "observed_revision",
        "targets",
        "previous_by_target",
        "action",
        "worker_id",
        "reason",
        "batch_base",
        "request_id",
    }
)
_REQUEST_ID = re.compile(r"[0-9a-f]{64}\Z")


class AssignmentRequestError(ValueError):
    """A saved reassignment request is invalid or bound to another workspace."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _text(value: Any, field: str, *, maximum: int = MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssignmentRequestError(
            "request_invalid", f"{field} must be non-empty text"
        )
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise AssignmentRequestError(
            "request_invalid", f"{field} must be valid UTF-8"
        ) from exc
    if size > maximum:
        raise AssignmentRequestError(
            "request_invalid", f"{field} exceeds {maximum} UTF-8 bytes"
        )
    return value


def resolved_store_path(value: str | os.PathLike[str]) -> str:
    """Return the existing Store's canonical absolute path."""
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AssignmentRequestError(
            "request_workspace_mismatch", "The assignment request Store is unavailable"
        ) from exc
    if not path.is_file():
        raise AssignmentRequestError(
            "request_workspace_mismatch", "The assignment request Store is unavailable"
        )
    return str(path)


def _request_directory(store_path: str | os.PathLike[str]) -> Path:
    return Path(store_path).parent / "requests" / "reassign"


def _request_body(request: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if key != "request_id"}


def _request_digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json(body).encode("utf-8", errors="strict")
    ).hexdigest()


def validate_assignment_request_inputs(
    *,
    targets: tuple[str, ...] | list[str],
    worker_id: str | None,
    reason: str,
    batch_base: str | None,
) -> str:
    """Validate fresh-request shape and bounds without opening a Store."""
    if not isinstance(targets, (tuple, list)) or not 1 <= len(targets) <= MAX_TARGETS:
        raise AssignmentRequestError("request_invalid", "Target count must be 1 to 64")
    target_list = list(targets)
    for target in target_list:
        _text(target, "Contract ID", maximum=4096)
    if len(set(target_list)) != len(target_list):
        raise AssignmentRequestError("request_invalid", "Contract IDs must be distinct")
    _text(reason, "reason")
    _text(batch_base, "idempotency key base")
    if worker_id is None:
        return "clear"
    _text(worker_id, "Worker ID")
    return "set"


def _validate_request(request: Any, *, verify_id: bool = True) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise AssignmentRequestError(
            "request_invalid", "Saved request fields are invalid"
        )
    if request["schema"] != REQUEST_SCHEMA:
        raise AssignmentRequestError(
            "request_invalid", "Saved request schema is unsupported"
        )

    path = request["store_path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise AssignmentRequestError("request_invalid", "Saved Store path is invalid")
    _text(path, "Store path", maximum=4096)
    _text(request["domain_id"], "domain_id", maximum=4096)
    revision = request["observed_revision"]
    if type(revision) is not int or revision < 0:
        raise AssignmentRequestError("request_invalid", "Saved revision is invalid")

    targets = request["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise AssignmentRequestError("request_invalid", "Saved target list is invalid")
    for target in targets:
        _text(target, "Contract ID", maximum=4096)
    if len(set(targets)) != len(targets):
        raise AssignmentRequestError("request_invalid", "Saved targets are duplicated")

    previous = request["previous_by_target"]
    if not isinstance(previous, dict) or set(previous) != set(targets):
        raise AssignmentRequestError("request_invalid", "Saved head map is incomplete")
    for target, head in previous.items():
        _text(target, "Contract ID", maximum=4096)
        _text(head, "assignment head", maximum=4096)

    action = request["action"]
    worker_id = request["worker_id"]
    if action == "clear":
        if worker_id is not None:
            raise AssignmentRequestError(
                "request_invalid", "Saved clear action is invalid"
            )
    elif action == "set":
        _text(worker_id, "Worker ID")
    else:
        raise AssignmentRequestError("request_invalid", "Saved action is invalid")
    _text(request["reason"], "reason")
    _text(request["batch_base"], "idempotency key base")

    request_id = request["request_id"]
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
        raise AssignmentRequestError("request_invalid", "Request ID is invalid")
    if verify_id and _request_digest(_request_body(request)) != request_id:
        raise AssignmentRequestError(
            "request_invalid", "Saved request digest does not match"
        )
    return request


def prepare_assignment_request(
    store,
    *,
    store_path: str | os.PathLike[str],
    targets: tuple[str, ...] | list[str],
    worker_id: str | None,
    reason: str,
    batch_base: str,
) -> dict[str, Any]:
    """Freeze all current heads from one immutable Store snapshot."""
    action = validate_assignment_request_inputs(
        targets=targets,
        worker_id=worker_id,
        reason=reason,
        batch_base=batch_base,
    )
    target_list = list(targets)

    canonical_path = resolved_store_path(store_path)
    try:
        genesis = load_genesis(store)
    except LookupError as exc:
        raise AssignmentRequestError(
            "workspace_not_initialized", "Initialize this workspace first"
        ) from exc
    try:
        context = assignment_context_from_projection(
            load_effect_projection_context(store)
        )
    except AssignmentConflict as exc:
        raise AssignmentRequestError(
            "assignment_integrity_error", "Assignment history is inconsistent"
        ) from exc

    previous_by_target: dict[str, str] = {}
    for contract_id in target_list:
        if store.get_contract(contract_id) is None:
            raise AssignmentRequestError(
                "task_not_found", "A selected Contract does not exist"
            )
        try:
            head = assignment_head(context, contract_id=contract_id)
        except AssignmentConflict as exc:
            raise AssignmentRequestError(
                "assignment_integrity_error", "Assignment history is inconsistent"
            ) from exc
        if head is None:
            raise AssignmentRequestError(
                "assignment_head_missing",
                f"Contract {contract_id!r} has no assignment history; use task assign",
            )
        previous_by_target[contract_id] = head.id

    revision = store.get_runtime_revision()
    if type(revision) is not int or revision < 0:
        raise AssignmentRequestError("request_invalid", "Store revision is invalid")
    body: dict[str, Any] = {
        "schema": REQUEST_SCHEMA,
        "store_path": canonical_path,
        "domain_id": genesis.id,
        "observed_revision": revision,
        "targets": target_list,
        "previous_by_target": previous_by_target,
        "action": action,
        "worker_id": worker_id,
        "reason": reason,
        "batch_base": batch_base,
    }
    request = {**body, "request_id": _request_digest(body)}
    return _validate_request(request)


def _check_private_directory(path: Path, *, create: bool) -> None:
    directories = (path.parent, path)
    for directory in directories:
        try:
            if create:
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    pass
                else:
                    os.chmod(directory, 0o700)
                    _fsync_directory(directory.parent)
            info = directory.lstat()
        except FileNotFoundError as exc:
            if not create:
                raise AssignmentRequestError(
                    "request_not_found", "Saved reassignment request was not found"
                ) from exc
            raise AssignmentRequestError(
                "request_storage_failed",
                "Could not create the private request directory",
            ) from exc
        except OSError as exc:
            raise AssignmentRequestError(
                "request_storage_failed",
                "Could not access the private request directory",
            ) from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise AssignmentRequestError(
                "request_storage_failed",
                "The request directory is not a regular directory",
            )
        if os.name != "nt" and (
            stat.S_IMODE(info.st_mode) & 0o077
            or stat.S_IMODE(info.st_mode) & 0o700 != 0o700
        ):
            raise AssignmentRequestError(
                "request_storage_failed",
                "The request directory permissions are not private",
            )


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_request_file(path: Path, *, expected_id: str) -> tuple[bytes, dict[str, Any]]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise AssignmentRequestError(
            "request_not_found", "Saved reassignment request was not found"
        ) from exc
    except OSError as exc:
        raise AssignmentRequestError(
            "request_invalid", "Saved reassignment request cannot be opened safely"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
            raise AssignmentRequestError(
                "request_invalid",
                "Saved reassignment request is not a bounded regular file",
            )
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            raise AssignmentRequestError(
                "request_invalid", "Saved reassignment request permissions are invalid"
            )
        chunks = bytearray()
        while len(chunks) <= MAX_REQUEST_BYTES:
            block = os.read(fd, min(65536, MAX_REQUEST_BYTES + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        raw = bytes(chunks)
        os.fsync(fd)
    except OSError as exc:
        raise AssignmentRequestError(
            "request_storage_failed", "Could not confirm saved request durability"
        ) from exc
    finally:
        os.close(fd)
    if len(raw) > MAX_REQUEST_BYTES:
        raise AssignmentRequestError("request_invalid", "Saved request is too large")
    try:
        text = raw.decode("utf-8", errors="strict")
        request = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AssignmentRequestError(
            "request_invalid", "Saved reassignment request is malformed"
        ) from exc
    request = _validate_request(request)
    if canonical_json(request).encode("utf-8", errors="strict") != raw:
        raise AssignmentRequestError(
            "request_invalid", "Saved reassignment request is not canonical"
        )
    if request["request_id"] != expected_id:
        raise AssignmentRequestError(
            "request_invalid", "Saved request filename does not match its identity"
        )
    try:
        _fsync_directory(path.parent)
    except OSError as exc:
        raise AssignmentRequestError(
            "request_storage_failed", "Could not confirm saved request durability"
        ) from exc
    return raw, request


def save_assignment_request(request: dict[str, Any]) -> Path:
    """Persist one complete request without replacing any prior request."""
    request = _validate_request(request)
    if resolved_store_path(request["store_path"]) != request["store_path"]:
        raise AssignmentRequestError(
            "request_workspace_mismatch", "Saved Store path is not canonical"
        )
    payload = canonical_json(request).encode("utf-8", errors="strict")
    if len(payload) > MAX_REQUEST_BYTES:
        raise AssignmentRequestError("request_invalid", "Request is too large to save")
    directory = _request_directory(request["store_path"])
    _check_private_directory(directory, create=True)
    final_path = directory / f"request-{request['request_id']}.json"

    try:
        existing_raw, _existing = _read_request_file(
            final_path, expected_id=request["request_id"]
        )
    except AssignmentRequestError as exc:
        if exc.code != "request_not_found":
            raise
    else:
        if existing_raw != payload:
            raise AssignmentRequestError(
                "request_invalid", "A different request already occupies this ID"
            )
        return final_path

    temporary_path = directory / f".tmp-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    temporary_identity: tuple[int, int] | None = None
    try:
        fd = os.open(temporary_path, flags, 0o600)
        try:
            created = os.fstat(fd)
            temporary_identity = (created.st_dev, created.st_ino)
            os.chmod(temporary_path, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write while saving reassignment request")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temporary_path, final_path)
        except FileExistsError:
            existing_raw, _existing = _read_request_file(
                final_path, expected_id=request["request_id"]
            )
            if existing_raw != payload:
                raise AssignmentRequestError(
                    "request_invalid", "A different request already occupies this ID"
                )
        _fsync_directory(directory)
    except AssignmentRequestError:
        raise
    except OSError as exc:
        raise AssignmentRequestError(
            "request_storage_failed", "Could not durably save reassignment request"
        ) from exc
    finally:
        if temporary_identity is not None:
            try:
                current = temporary_path.lstat()
                if (
                    stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == temporary_identity
                ):
                    temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return final_path


def load_assignment_request(
    store_path: str | os.PathLike[str], request_id: str
) -> dict[str, Any]:
    """Load an immutable request by its lowercase content digest."""
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
        raise AssignmentRequestError("request_invalid", "Request ID is invalid")
    canonical_path = resolved_store_path(store_path)
    directory = _request_directory(canonical_path)
    _check_private_directory(directory, create=False)
    path = directory / f"request-{request_id}.json"
    _raw, request = _read_request_file(path, expected_id=request_id)
    if request["store_path"] != canonical_path:
        raise AssignmentRequestError(
            "request_workspace_mismatch", "Saved request belongs to another Store path"
        )
    return request


def validate_assignment_request_domain(request: dict[str, Any], store) -> None:
    """Require the saved request's original Genesis identity."""
    request = _validate_request(request)
    try:
        genesis = load_genesis(store)
    except LookupError as exc:
        raise AssignmentRequestError(
            "workspace_not_initialized", "Initialize this workspace first"
        ) from exc
    if genesis.id != request["domain_id"]:
        raise AssignmentRequestError(
            "request_domain_mismatch", "Saved request belongs to another runtime domain"
        )
