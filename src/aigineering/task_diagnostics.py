"""Read-only, fact-derived task claim-attempt history and diagnostics."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any

from aigineering.core.domain import load_genesis
from aigineering.core.store import require_operational_store
from aigineering.protocol.identity import canonical_json
from aigineering.protocol.runtime_record import RuntimeRecord, validate_runtime_record
from aigineering.planning_diagnostics import (
    is_compile_failure,
    valid_compile_diagnostics,
)

_CURSOR_VERSION = 1
_MAX_LIMIT = 100
_FAILURE_CODES = frozenset(
    {
        "provider_error",
        "identity_mismatch",
        "candidate_encoding_rejected",
        "planning_request_rejected",
        "task_delegation_rejected",
        "attestation_request_rejected",
        "tool_observation_required",
        "tool_action_invalid",
        "tool_arguments_invalid",
        "tool_unavailable",
        "tool_timeout",
        "tool_schema_invalid",
        "tool_result_too_large",
        "tool_execution_failed",
        "tool_contract_invalid",
        "tool_scope_denied",
        "tool_descriptor_invalid",
        "tool_descriptor_mismatch",
        "mcp_name_invalid",
        "mcp_arguments_invalid",
        "mcp_server_unavailable",
        "mcp_call_failed",
        "mcp_result_invalid",
        "mcp_contract_invalid",
        "mcp_scope_denied",
        "mcp_descriptor_invalid",
        "invalid_action",
        "response_missing_content",
        "response_missing_choices",
        "response_choice_not_object",
        "response_too_large",
        "response_not_object",
        "empty_tool_calls",
        "empty_response",
        "malformed_json_action",
        "invalid_tool_definition",
        "duplicate_tool_definition",
        "response_invalid_encoding",
        "response_invalid_json",
        "missing_api_key",
        "output_truncated",
        "json_object_tool_call_response",
        "json_object_with_tools_unsupported",
        "tool_call_missing_function",
        "tool_call_missing_name",
        "tool_call_invalid_arguments",
        "planning_compile_rejected",
        "invocation_failure",
        "envelope_failure",
        "candidate_encoding_failure",
        "claim_renewal_failed",
        "unsigned_adapter",
        "claim_expired",
    }
)
_ATTEMPT_OUTCOMES = frozenset({"output_asserted", "expanded", "failed"})
_TERMINAL_TYPES = frozenset({"lifecycle.terminal"})
_EVENT_RECORD_TYPES = frozenset(
    {
        "claim.granted",
        "claim.renewed",
        "claim.submitted",
        "claim.released",
        "claim.expired",
        "worker.claim.requested",
        "worker.claim.renew.requested",
        "candidate.received",
        "candidate.committed",
        "candidate.rejected",
        "attempt.closed",
        "worker.invocation_failed",
        "lifecycle.terminal",
        "worker_failure.recovery_scheduled",
        "worker_failure.recovery_unavailable",
        "candidate_rejection.recovery_scheduled",
        "candidate_rejection.recovery_unavailable",
        "claim_expiration.recovery_scheduled",
        "claim_expiration.recovery_unavailable",
    }
)
_RECOVERY_REASON_CODES = frozenset(
    {
        "legacy_method_intent_missing",
        "recovery_history_ambiguous",
        "method_parent_missing",
        "method_source_context_unverified",
        "method_source_declaration_unavailable",
        "method_source_history_unverified",
        "method_source_noncanonical",
        "method_source_publication_unverified",
        "parallel_method_source_batch_unverified",
        "parallel_method_source_batch_noncanonical",
        "parallel_method_source_noncanonical",
        "parallel_method_source_publication_unverified",
        "recovery_depth_exceeded",
        "recovery_history_conflict",
        "recovery_history_unavailable",
        "recovery_publication_rejected",
        "recovery_publisher_unavailable",
        "source_proof_missing",
        "terminal_preserved",
        "unsupported_method_intent",
    }
)


def safe_recovery_reason(value: Any) -> str | None:
    """Return only fixed recovery disposition codes suitable for read views."""
    return value if isinstance(value, str) and value in _RECOVERY_REASON_CODES else None


_CLAIM_ID_PATTERN = re.compile(r"lease:[0-9a-f]{64}\Z")
_CANDIDATE_ID_PATTERN = re.compile(r"candidate:v1:[0-9a-f]{64}\Z")
_CLAIM_TRANSITIONS = frozenset(
    {"claim.renewed", "claim.submitted", "claim.released", "claim.expired"}
)


class TaskDiagnosticError(ValueError):
    """A safe, caller-displayable task diagnostic selection error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise TaskDiagnosticError("invalid_cursor")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskDiagnosticError("invalid_cursor") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise TaskDiagnosticError("invalid_cursor")
    return parsed.astimezone(timezone.utc)


def _safe_utc_timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        _parse_utc(value)
    except TaskDiagnosticError:
        return None
    return value


def _cursor_encode(payload: dict[str, Any]) -> str:
    encoded = canonical_json(payload).encode("utf-8")
    body = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"v{_CURSOR_VERSION}.{body}.{digest}"


def _cursor_decode(value: str) -> dict[str, Any]:
    try:
        version, body, digest = value.split(".", 2)
        if version != f"v{_CURSOR_VERSION}" or not body:
            raise ValueError
        encoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError
        payload = json.loads(encoded)
        if not isinstance(payload, dict) or canonical_json(payload).encode() != encoded:
            raise ValueError
        return payload
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise TaskDiagnosticError("invalid_cursor") from exc


def _valid_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
        raise TaskDiagnosticError("invalid_limit")


def _issue(code: str, *record_ids: str) -> dict[str, Any]:
    return {"code": code, "record_ids": sorted(set(record_ids))}


def _record_is_valid(record: RuntimeRecord) -> bool:
    try:
        validate_runtime_record(record)
    except (TypeError, ValueError):
        return False
    return True


def _same_fields(record: RuntimeRecord, expected: dict[str, Any]) -> bool:
    return all(
        type(record.payload.get(key)) is type(value)
        and record.payload.get(key) == value
        for key, value in expected.items()
    )


def _safe_category(category: object) -> str:
    if not isinstance(category, str):
        return "unknown"
    if category in _FAILURE_CODES:
        return category
    prefix = "worker_error:"
    if category.startswith(prefix):
        code = category[len(prefix) :]
        if code in _FAILURE_CODES:
            return code
        if code.startswith("planning_compile_rejected:"):
            return "planning_compile_rejected"
    return "unknown"


def _is_one_of(value: object, choices: frozenset[str] | set[str]) -> bool:
    return type(value) is str and value in choices


def _is_claim_id(value: object) -> bool:
    return isinstance(value, str) and _CLAIM_ID_PATTERN.fullmatch(value) is not None


def _safe_candidate_id(value: object) -> str | None:
    if isinstance(value, str) and _CANDIDATE_ID_PATTERN.fullmatch(value) is not None:
        return value
    return None


def _is_package_id(value: object) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"pkg:[0-9a-f]{64}", value) is not None
    )


def _safe_usage(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        item = value.get(name)
        if type(item) is int and item >= 0:
            result[name] = item
    return result or None


def _validate_cursor(
    cursor: str | None,
    *,
    command: str,
    domain_id: str,
    contract_id: str,
    claim_id: str | None,
    limit: int,
    revision: int,
) -> tuple[str, tuple[int, str] | None, int]:
    if cursor is None:
        return _utc_now(), None, revision
    payload = _cursor_decode(cursor)
    expected = {
        "version": _CURSOR_VERSION,
        "command": command,
        "domain_id": domain_id,
        "contract_id": contract_id,
        "claim_id": claim_id,
        "limit": limit,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise TaskDiagnosticError("cursor_scope_mismatch")
    watermark = payload.get("watermark")
    boundary = payload.get("boundary")
    observed_at = payload.get("observed_at")
    if type(watermark) is not int or watermark < 0 or watermark > revision:
        raise TaskDiagnosticError("invalid_cursor")
    if (
        not isinstance(boundary, list)
        or len(boundary) != 2
        or type(boundary[0]) is not int
        or not isinstance(boundary[1], str)
    ):
        raise TaskDiagnosticError("invalid_cursor")
    _parse_utc(observed_at)
    return str(observed_at), (boundary[0], boundary[1]), watermark


def _snapshot(store, contract_id: str, *, query_context=None):
    store = require_operational_store(store)
    if query_context is not None:
        query_context.require_match(store)
        contract = query_context.read_store.get_contract(contract_id)
        if contract is None:
            raise TaskDiagnosticError("task_not_found")
        indexed = list(query_context.runtime_records)
        by_id = {record.id: (revision, record) for revision, record in indexed}
        if len(by_id) != len(indexed):
            raise TaskDiagnosticError("duplicate_record_identity")
        return (
            contract,
            query_context.domain_id,
            query_context.revision,
            indexed,
            by_id,
        )
    contract = store.get_contract(contract_id)
    if contract is None:
        raise TaskDiagnosticError("task_not_found")
    genesis = load_genesis(store)
    watermark = store.get_runtime_revision()
    rows = store.scan_runtime_records(through_revision=watermark)
    indexed = [(revision, record) for revision, record in rows if revision <= watermark]
    by_id = {record.id: (revision, record) for revision, record in indexed}
    if len(by_id) != len(indexed):
        raise TaskDiagnosticError("duplicate_record_identity")
    return contract, genesis.id, watermark, indexed, by_id


def _grant_fields(record: RuntimeRecord) -> dict[str, Any] | None:
    payload = record.payload
    claim_id = payload.get("claim_id")
    contract_id = payload.get("contract_id")
    worker_id = payload.get("worker_id")
    package_id = payload.get("package_id")
    epoch = payload.get("epoch")
    lease_until = payload.get("lease_until")
    if (
        not _is_claim_id(claim_id)
        or not all(
            isinstance(item, str) and item
            for item in (contract_id, worker_id, lease_until)
        )
        or not isinstance(package_id, str)
        or type(epoch) is not int
        or epoch < 1
    ):
        return None
    try:
        _parse_utc(lease_until)
    except TaskDiagnosticError:
        return None
    return {
        "claim_id": claim_id,
        "contract_id": contract_id,
        "worker_id": worker_id,
        "package_id": package_id,
        "epoch": epoch,
        "lease_until": lease_until,
    }


def _worker_was_declared(
    worker_id: str,
    grant_revision: int,
    indexed: list[tuple[int, RuntimeRecord]],
) -> bool:
    for revision, record in indexed:
        if revision >= grant_revision or record.record_type != "worker.registered":
            continue
        payload = record.payload
        if not _record_is_valid(record) or payload.get("worker_id") != worker_id:
            continue
        actor_id = payload.get("actor_id", "")
        key_id = payload.get("key_id", "")
        if (actor_id == worker_id and isinstance(key_id, str) and key_id) or (
            actor_id == "" and key_id == ""
        ):
            return True
    return False


def _validate_grant_provenance(
    grant: RuntimeRecord,
    by_id: dict[str, tuple[int, RuntimeRecord]],
    evidence_ids: list[str],
) -> str | None:
    if not grant.causal_parents:
        return "legacy_recordless"
    if len(grant.causal_parents) != 1:
        return None
    parent_pair = by_id.get(grant.causal_parents[0])
    if parent_pair is None:
        return None
    request_revision, request = parent_pair
    if request.record_type != "worker.claim.requested":
        return None
    receipt_pair = (
        by_id.get(request.causal_parents[0])
        if len(request.causal_parents) == 1
        else None
    )
    receipt = receipt_pair[1] if receipt_pair is not None else None
    grant_pair = by_id.get(grant.id)
    grant_revision = grant_pair[0] if grant_pair is not None else None
    fields = _grant_fields(grant)
    request_payload = request.payload
    request_target = (
        request_payload.get("contract_id")
        if isinstance(request_payload, Mapping)
        else object()
    )
    request_target_matches = (
        isinstance(request_payload, Mapping)
        and "contract_id" in request_payload
        and (
            request_target is None
            or (
                type(request_target) is str
                and fields is not None
                and request_target == fields["contract_id"]
            )
        )
    )
    request_fields = {
        "worker_id": fields["worker_id"] if fields else None,
    }
    receipt_effect_types = (
        receipt.payload.get("effect_types")
        if receipt is not None and isinstance(receipt.payload, Mapping)
        else None
    )
    receipt_is_claim_request = (
        isinstance(receipt_effect_types, (list, tuple))
        and len(receipt_effect_types) == 1
        and type(receipt_effect_types[0]) is str
        and receipt_effect_types[0] == "worker.claim"
    )
    request_shape_valid = (
        isinstance(request_payload, Mapping)
        and set(request_payload)
        == {
            "candidate_id",
            "contract_id",
            "idempotency_key",
            "key_id",
            "lease_seconds",
            "worker_id",
        }
        and _safe_candidate_id(request_payload.get("candidate_id")) is not None
        and isinstance(request_payload.get("idempotency_key"), str)
        and bool(request_payload.get("idempotency_key", "").strip())
        and isinstance(request_payload.get("key_id"), str)
        and bool(request_payload.get("key_id", "").strip())
        and type(request_payload.get("lease_seconds")) is int
        and request_payload.get("lease_seconds", 0) > 0
    )
    if (
        fields is None
        or grant_revision is None
        or request is None
        or receipt is None
        or not request_target_matches
        or not request_shape_valid
        or not _record_is_valid(request)
        or not _record_is_valid(receipt)
        or not _same_fields(request, request_fields)
        or len(request.causal_parents) != 1
        or not isinstance(request.payload.get("candidate_id"), str)
        or not request.payload.get("candidate_id")
        or receipt.record_type != "candidate.received"
        or receipt.payload.get("candidate_id") != request.payload.get("candidate_id")
        or receipt.payload.get("actor_id") != fields["worker_id"]
        or receipt.payload.get("key_id") != request.payload.get("key_id")
        or not receipt_is_claim_request
        or receipt_pair[0] >= request_revision
        or request_revision >= grant_revision
    ):
        return None
    evidence_ids.extend((request.id, receipt.id))
    return "signed"


def _validate_renewal_provenance(
    renewal: RuntimeRecord,
    grant_record: RuntimeRecord,
    grant: dict[str, Any],
    by_id: dict[str, tuple[int, RuntimeRecord]],
    evidence_ids: list[str],
) -> bool:
    if not grant_record.causal_parents and renewal.causal_parents == (grant_record.id,):
        return True
    if (
        grant_record.id not in renewal.causal_parents
        or len(renewal.causal_parents) != 2
    ):
        return False
    request_id = next(
        (parent for parent in renewal.causal_parents if parent != grant_record.id), None
    )
    request_pair = by_id.get(request_id) if request_id else None
    if request_pair is None:
        return False
    request = request_pair[1]
    if request.record_type != "worker.claim.renew.requested":
        return False
    if (
        not _same_fields(
            request,
            {
                "claim_id": grant["claim_id"],
                "claim_epoch": grant["epoch"],
                "worker_id": grant["worker_id"],
            },
        )
        or len(request.causal_parents) != 1
        or not isinstance(request.payload.get("candidate_id"), str)
        or not request.payload.get("candidate_id")
    ):
        return False
    receipt_pair = by_id.get(request.causal_parents[0])
    if receipt_pair is None:
        return False
    receipt = receipt_pair[1]
    valid = (
        receipt.record_type == "candidate.received"
        and receipt.payload.get("candidate_id") == request.payload.get("candidate_id")
        and receipt.payload.get("actor_id") == grant["worker_id"]
        and _record_is_valid(renewal)
        and _record_is_valid(request)
        and _record_is_valid(receipt)
    )
    if valid:
        evidence_ids.extend((request.id, receipt.id))
    return valid


def _matches_transition(record: RuntimeRecord, grant: dict[str, Any]) -> bool:
    if record.record_type == "claim.renewed":
        return _same_fields(
            record,
            {
                "claim_id": grant["claim_id"],
                "epoch": grant["epoch"],
                "worker_id": grant["worker_id"],
            },
        )
    return _same_fields(
        record,
        {
            "claim_id": grant["claim_id"],
            "contract_id": grant["contract_id"],
            "epoch": grant["epoch"],
            "package_id": grant["package_id"],
            "worker_id": grant["worker_id"],
        },
    )


def _release_terminal_is_valid(
    terminal: RuntimeRecord,
    contract_id: str,
    by_id: dict[str, tuple[int, RuntimeRecord]],
    indexed: list[tuple[int, RuntimeRecord]],
) -> bool:
    if (
        terminal.record_type != "lifecycle.terminal"
        or terminal.payload.get("contract_id") != contract_id
        or not _is_one_of(
            terminal.payload.get("terminal"),
            {"complete", "failed", "cancelled", "unreachable", "stalled"},
        )
        or not _record_is_valid(terminal)
    ):
        return False
    parents = terminal.causal_parents
    payload_keys = set(terminal.payload)
    if not parents:
        return payload_keys == {"contract_id", "terminal"}
    if len(parents) != 1:
        return False
    source_pair = by_id.get(parents[0])
    if source_pair is None:
        return False
    source = source_pair[1]
    if source.record_type == "asset.committed":
        return (
            payload_keys == {"contract_id", "terminal"}
            and set(source.payload) == {"asset", "contract_id"}
            and isinstance(source.payload.get("asset"), Mapping)
            and isinstance(source.payload.get("contract_id"), str)
            and _record_is_valid(source)
        )
    if source.record_type != "candidate.received":
        return False
    candidate_id = source.payload.get("candidate_id")
    effect_types = source.payload.get("effect_types")
    if (
        payload_keys != {"contract_id", "terminal", "reason", "actor_id"}
        or not _safe_candidate_id(candidate_id)
        or not isinstance(effect_types, (list, tuple))
        or "contract.cancel" not in effect_types
        or not isinstance(terminal.payload.get("reason"), str)
        or not terminal.payload.get("reason")
        or terminal.payload.get("actor_id") != source.payload.get("actor_id")
        or not _record_is_valid(source)
    ):
        return False
    heads = []
    for _, head in indexed:
        if (
            head.record_type != "candidate.committed"
            or head.payload.get("candidate_id") != candidate_id
        ):
            continue
        committed_ids = head.payload.get("committed_record_ids")
        if not isinstance(committed_ids, (list, tuple)) or any(
            not isinstance(item, str) for item in committed_ids
        ):
            continue
        if terminal.id in committed_ids:
            heads.append((head, tuple(committed_ids)))
    return (
        len(heads) == 1
        and heads[0][0].causal_parents == (source.id, *heads[0][1])
        and _record_is_valid(heads[0][0])
    )


def _invocation_failure_matches_grant(
    failure: RuntimeRecord, grant: dict[str, Any]
) -> bool:
    expected = {
        "claim_id": grant["claim_id"],
        "contract_id": grant["contract_id"],
        "package_id": grant["package_id"],
        "worker_id": grant["worker_id"],
    }
    allowed = expected.keys() | {
        "category",
        "retryable",
        "status_code",
        "raw_output",
        "diagnostic",
        "usage_metadata",
        "compile_diagnostics",
    }
    return (
        failure.record_type == "worker.invocation_failed"
        and _record_is_valid(failure)
        and not failure.causal_parents
        and _same_fields(failure, expected)
        and set(failure.payload) <= allowed
        and isinstance(failure.payload.get("category"), str)
        and type(failure.payload.get("retryable")) is bool
        and type(failure.payload.get("status_code")) is int
        and (
            "compile_diagnostics" not in failure.payload
            or (
                is_compile_failure(failure.payload.get("category"))
                and valid_compile_diagnostics(failure.payload["compile_diagnostics"])
            )
        )
    )


def _receipt_for_candidate(
    candidate_id: str,
    grant: dict[str, Any],
    indexed: list[tuple[int, RuntimeRecord]],
    issues: list[dict[str, Any]],
    evidence_ids: list[str],
) -> RuntimeRecord | None:
    receipts = [
        record
        for _revision, record in indexed
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate_id
        and record.payload.get("claim_id") == grant["claim_id"]
        and record.payload.get("contract_id") == grant["contract_id"]
    ]
    if len(receipts) != 1:
        issues.append(
            _issue(
                "candidate_receipt_missing_or_ambiguous",
                *(item.id for item in receipts),
            )
        )
        return None
    receipt = receipts[0]
    evidence_ids.append(receipt.id)
    if (
        not _record_is_valid(receipt)
        or receipt.payload.get("actor_id") != grant["worker_id"]
    ):
        issues.append(_issue("candidate_receipt_actor_mismatch", receipt.id))
        return None
    return receipt


def _submission_proof(
    transition: RuntimeRecord,
    grant_record: RuntimeRecord,
    grant: dict[str, Any],
    indexed: list[tuple[int, RuntimeRecord]],
    by_id: dict[str, tuple[int, RuntimeRecord]],
    issues: list[dict[str, Any]],
    evidence_ids: list[str],
) -> tuple[str | None, str | None]:
    candidate_id = transition.payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        issues.append(_issue("submitted_candidate_id_missing", transition.id))
        return None, None
    committed_heads = [
        by_id[parent][1]
        for parent in transition.causal_parents
        if parent in by_id
        and by_id[parent][1].record_type == "candidate.committed"
        and by_id[parent][1].payload.get("candidate_id") == candidate_id
    ]
    if len(committed_heads) == 1:
        head = committed_heads[0]
        if not _record_is_valid(head):
            issues.append(_issue("runtime_record_identity_invalid", head.id))
        receipt = _receipt_for_candidate(
            candidate_id, grant, indexed, issues, evidence_ids
        )
        if (
            receipt is None
            or grant_record.id not in transition.causal_parents
            or receipt.id not in head.causal_parents
            or set(head.payload) != {"candidate_id", "committed_record_ids"}
            or not isinstance(head.payload.get("committed_record_ids"), (list, tuple))
            or any(
                not isinstance(member, str)
                for member in head.payload.get("committed_record_ids", ())
            )
            or len(set(head.payload.get("committed_record_ids", ())))
            != len(head.payload.get("committed_record_ids", ()))
            or tuple(head.causal_parents)
            != (receipt.id, *tuple(head.payload.get("committed_record_ids", ())))
        ):
            issues.append(
                _issue("accepted_submission_proof_invalid", transition.id, head.id)
            )
            return candidate_id, None
        if any(
            not isinstance(member, str)
            or member not in by_id
            or member not in head.causal_parents
            for member in head.payload["committed_record_ids"]
        ):
            issues.append(_issue("committed_head_member_missing", head.id))
            return candidate_id, None
        invalid_members = [
            str(member)
            for member in head.payload["committed_record_ids"]
            if not _record_is_valid(by_id[str(member)][1])
        ]
        if invalid_members:
            issues.append(_issue("runtime_record_identity_invalid", *invalid_members))
            return candidate_id, None
        evidence_ids.append(head.id)
        evidence_ids.extend(
            str(member) for member in head.payload["committed_record_ids"]
        )
        closures = [
            record
            for _revision, record in indexed
            if record.record_type == "attempt.closed"
            and record.payload.get("candidate_id") == candidate_id
            and record.payload.get("claim_id") == grant["claim_id"]
            and record.payload.get("contract_id") == grant["contract_id"]
            and record.causal_parents == (head.id,)
        ]
        if len(closures) != 1 or not _is_one_of(
            closures[0].payload.get("outcome"), {"output_asserted", "expanded"}
        ):
            issues.append(_issue("accepted_attempt_closure_invalid", head.id))
            return candidate_id, None
        evidence_ids.extend((head.id, closures[0].id))
        if not _record_is_valid(closures[0]):
            issues.append(_issue("runtime_record_identity_invalid", closures[0].id))
            return candidate_id, None
        return candidate_id, str(closures[0].payload["outcome"])

    if committed_heads or transition.causal_parents != (grant_record.id,):
        issues.append(_issue("submission_causal_shape_invalid", transition.id))
        return candidate_id, None
    rejected = [
        record
        for _revision, record in indexed
        if record.record_type == "candidate.rejected"
        and record.payload.get("candidate_id") == candidate_id
        and record.payload.get("claim_id") == grant["claim_id"]
        and record.payload.get("contract_id") == grant["contract_id"]
    ]
    receipt = _receipt_for_candidate(candidate_id, grant, indexed, issues, evidence_ids)
    if (
        len(rejected) != 1
        or receipt is None
        or rejected[0].causal_parents != (receipt.id,)
    ):
        issues.append(
            _issue(
                "rejected_submission_proof_invalid",
                transition.id,
                *(item.id for item in rejected),
            )
        )
        return candidate_id, None
    rejection = rejected[0]
    if not _record_is_valid(rejection):
        issues.append(_issue("runtime_record_identity_invalid", rejection.id))
        return candidate_id, None
    closures = [
        record
        for _revision, record in indexed
        if record.record_type == "attempt.closed"
        and record.payload.get("candidate_id") == candidate_id
        and record.payload.get("claim_id") == grant["claim_id"]
        and record.payload.get("contract_id") == grant["contract_id"]
        and record.causal_parents == (rejection.id,)
    ]
    if len(closures) != 1 or closures[0].payload.get("outcome") != "failed":
        issues.append(_issue("rejected_attempt_closure_invalid", rejection.id))
        return candidate_id, None
    evidence_ids.extend((rejection.id, closures[0].id))
    if not _record_is_valid(closures[0]):
        issues.append(_issue("runtime_record_identity_invalid", closures[0].id))
        return candidate_id, None
    return candidate_id, "rejected"


def _build_runs(
    contract_id: str,
    watermark: int,
    observed_at: str,
    indexed: list[tuple[int, RuntimeRecord]],
    by_id: dict[str, tuple[int, RuntimeRecord]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_issues: list[dict[str, Any]] = []
    grants = [
        (revision, record)
        for revision, record in indexed
        if record.record_type == "claim.granted"
        and record.payload.get("contract_id") == contract_id
    ]
    contract_grant_ids = [record.id for _, record in grants]
    grants_by_claim: dict[str, list[tuple[int, RuntimeRecord]]] = {}
    for revision, record in indexed:
        if record.record_type != "claim.granted":
            continue
        claim_id = record.payload.get("claim_id")
        if not _is_claim_id(claim_id):
            if record.payload.get("contract_id") == contract_id:
                all_issues.append(_issue("grant_claim_id_invalid", record.id))
            continue
        grants_by_claim.setdefault(claim_id, []).append((revision, record))

    rows: list[dict[str, Any]] = []
    for revision, record in grants:
        grant = _grant_fields(record)
        local_issues: list[dict[str, Any]] = []
        if not _record_is_valid(record):
            local_issues.append(_issue("runtime_record_identity_invalid", record.id))
        if grant is None:
            local_issues.append(_issue("grant_payload_invalid", record.id))
            if _safe_utc_timestamp(record.recorded_at) is None:
                local_issues.append(_issue("recorded_at_invalid", record.id))
            try:
                _parse_utc(record.payload.get("lease_until"))
            except TaskDiagnosticError:
                local_issues.append(_issue("grant_lease_time_invalid", record.id))
            all_issues.extend(local_issues)
            raw_claim_id = record.payload.get("claim_id")
            rows.append(
                {
                    "claim_id": raw_claim_id if _is_claim_id(raw_claim_id) else None,
                    "grant_record_id": record.id,
                    "grant_revision": revision,
                    "status": "integrity_error",
                    "fact_ids": [record.id],
                    "integrity": {"consistent": False, "issues": local_issues},
                }
            )
            continue
        claim_id = grant["claim_id"]
        evidence_ids = [record.id]
        if len(contract_grant_ids) > 1:
            local_issues.append(
                _issue("multiple_contract_claim_grants", *contract_grant_ids)
            )
        if len(grants_by_claim.get(claim_id, ())) != 1:
            local_issues.append(
                _issue(
                    "duplicate_claim_grant",
                    *(item.id for _, item in grants_by_claim[claim_id]),
                )
            )
        provenance = _validate_grant_provenance(record, by_id, evidence_ids)
        if provenance is None:
            local_issues.append(_issue("grant_provenance_invalid", record.id))
        transitions = []
        renewals = []
        for transition_revision, transition in indexed:
            if transition.record_type not in _CLAIM_TRANSITIONS:
                continue
            if transition.payload.get("claim_id") != claim_id:
                continue
            evidence_ids.append(transition.id)
            if not _matches_transition(transition, grant):
                local_issues.append(
                    _issue("claim_transition_payload_mismatch", transition.id)
                )
                continue
            if not _record_is_valid(transition):
                local_issues.append(
                    _issue("runtime_record_identity_invalid", transition.id)
                )
                continue
            expected_parents = (record.id,)
            if transition.record_type == "claim.submitted":
                expected_parents = tuple(transition.causal_parents)
                if record.id not in expected_parents or len(expected_parents) not in {
                    1,
                    2,
                }:
                    local_issues.append(
                        _issue("claim_submitted_parent_invalid", transition.id)
                    )
                    continue
            elif transition.record_type == "claim.renewed":
                if not _validate_renewal_provenance(
                    transition, record, grant, by_id, evidence_ids
                ):
                    local_issues.append(
                        _issue("claim_renewal_provenance_invalid", transition.id)
                    )
                    continue
            elif transition.record_type == "claim.released":
                parents = transition.causal_parents
                parent_valid = True
                if len(parents) == 2 and parents[1] in by_id:
                    source = by_id[parents[1]][1]
                    if source.record_type == "worker.invocation_failed":
                        parent_valid = _invocation_failure_matches_grant(source, grant)
                    elif source.record_type == "lifecycle.terminal":
                        parent_valid = _release_terminal_is_valid(
                            source, contract_id, by_id, indexed
                        )
                    else:
                        parent_valid = False
                if (
                    parents[:1] != (record.id,)
                    or len(parents) > 2
                    or not parent_valid
                    or set(transition.payload)
                    != {"claim_id", "contract_id", "epoch", "package_id", "worker_id"}
                ):
                    local_issues.append(
                        _issue("claim_transition_parent_invalid", transition.id)
                    )
                    continue
            elif transition.causal_parents != expected_parents:
                local_issues.append(
                    _issue("claim_transition_parent_invalid", transition.id)
                )
                continue
            if (
                transition.record_type == "claim.released"
                and len(transition.causal_parents) == 2
            ):
                evidence_ids.append(transition.causal_parents[1])
            if transition.record_type == "claim.renewed":
                lease = transition.payload.get("lease_until")
                if not isinstance(lease, str) or not lease:
                    local_issues.append(
                        _issue("claim_renewal_payload_invalid", transition.id)
                    )
                else:
                    try:
                        _parse_utc(lease)
                    except TaskDiagnosticError:
                        local_issues.append(
                            _issue("claim_renewal_payload_invalid", transition.id)
                        )
                    else:
                        renewals.append((transition_revision, lease, transition.id))
            else:
                transitions.append((transition_revision, transition))
        terminal_transitions = transitions
        if len(terminal_transitions) > 1:
            local_issues.append(
                _issue(
                    "conflicting_claim_transitions",
                    *(item[1].id for item in terminal_transitions),
                )
            )
        terminal = (
            terminal_transitions[0][1] if len(terminal_transitions) == 1 else None
        )
        effective_lease_until = (
            sorted(renewals, key=lambda item: (item[0], item[2]))[-1][1]
            if renewals
            else grant["lease_until"]
        )
        candidate_id = None
        attempt_outcome = None
        if terminal is not None and terminal.record_type == "claim.submitted":
            candidate_id, attempt_outcome = _submission_proof(
                terminal, record, grant, indexed, by_id, local_issues, evidence_ids
            )
        failures = [
            (failure_revision, failure)
            for failure_revision, failure in indexed
            if failure.record_type == "worker.invocation_failed"
            and failure.payload.get("claim_id") == claim_id
        ]
        if len(failures) > 1:
            local_issues.append(
                _issue(
                    "duplicate_invocation_failure", *(item.id for _, item in failures)
                )
            )
        scoped_closures = [
            closure
            for _, closure in indexed
            if closure.record_type == "attempt.closed"
            and closure.payload.get("claim_id") == claim_id
            and closure.payload.get("contract_id") == contract_id
        ]
        linked_closure_ids = {
            fact_id
            for fact_id in evidence_ids
            if fact_id in by_id and by_id[fact_id][1].record_type == "attempt.closed"
        }
        orphan_closures = [
            closure
            for closure in scoped_closures
            if closure.id not in linked_closure_ids
        ]
        if orphan_closures:
            local_issues.append(
                _issue("orphan_attempt_closure", *(item.id for item in orphan_closures))
            )
            evidence_ids.extend(item.id for item in orphan_closures)
        if (
            terminal is not None
            and terminal.record_type == "claim.submitted"
            and failures
        ):
            local_issues.append(
                _issue(
                    "submitted_claim_has_invocation_failure",
                    terminal.id,
                    failures[0][1].id,
                )
            )
        if (
            terminal is not None
            and terminal.record_type == "claim.expired"
            and terminal.payload.get("lease_until") != effective_lease_until
        ):
            local_issues.append(_issue("expiration_lease_mismatch", terminal.id))
        if failures:
            failure_revision, failure = failures[0]
            expected = {
                "claim_id": claim_id,
                "contract_id": contract_id,
                "package_id": grant["package_id"],
                "worker_id": grant["worker_id"],
                "retryable": failure.payload.get("retryable"),
                "status_code": failure.payload.get("status_code"),
            }
            if (
                not _same_fields(failure, expected)
                or not _record_is_valid(failure)
                or type(expected["retryable"]) is not bool
                or type(expected["status_code"]) is not int
            ):
                local_issues.append(
                    _issue("invocation_failure_payload_invalid", failure.id)
                )
            evidence_ids.append(failure.id)
            linked_releases = [
                transition
                for _, transition in transitions
                if transition.record_type == "claim.released"
                and failure.id in transition.causal_parents
            ]
            if (
                len(linked_releases) != 1
                or record.id not in linked_releases[0].causal_parents
            ):
                local_issues.append(
                    _issue("invocation_failure_release_invalid", failure.id)
                )
            category = _safe_category(failure.payload.get("category"))
            status = "invocation_failed"
            failure_summary = {
                "record_id": failure.id,
                "revision": failure_revision,
                "category": category,
                "retryable": bool(expected["retryable"])
                if type(expected["retryable"]) is bool
                else None,
                "status_code": expected["status_code"]
                if type(expected["status_code"]) is int
                else None,
                "usage": _safe_usage(failure.payload.get("usage_metadata")),
            }
        else:
            failure_summary = None
            if terminal is not None:
                status = {
                    "claim.submitted": "submitted"
                    if _is_one_of(attempt_outcome, {"output_asserted", "expanded"})
                    else "rejected"
                    if attempt_outcome == "rejected"
                    else "integrity_error",
                    "claim.expired": "expired",
                    "claim.released": "released",
                }.get(terminal.record_type, "integrity_error")
            else:
                lease = effective_lease_until
                try:
                    elapsed = _parse_utc(lease) <= _parse_utc(observed_at)
                except TaskDiagnosticError:
                    elapsed = False
                    local_issues.append(_issue("grant_lease_time_invalid", record.id))
                status = "lease_elapsed" if elapsed else "active"
        if len(transitions) > 1:
            local_issues.append(
                _issue(
                    "multiple_claim_terminal_facts",
                    *(item[1].id for item in transitions),
                )
            )
        for fact_id in set(evidence_ids):
            pair = by_id.get(fact_id)
            if pair is not None and _safe_utc_timestamp(pair[1].recorded_at) is None:
                local_issues.append(_issue("recorded_at_invalid", fact_id))
        if local_issues:
            status = "integrity_error"
            all_issues.extend(local_issues)
        rows.append(
            {
                "claim_id": claim_id,
                "grant_record_id": record.id,
                "grant_revision": revision,
                "grant_recorded_at": _safe_utc_timestamp(record.recorded_at),
                "epoch": grant["epoch"],
                "package_id": (
                    grant["package_id"] if _is_package_id(grant["package_id"]) else None
                ),
                "worker_id": (
                    grant["worker_id"]
                    if _worker_was_declared(grant["worker_id"], revision, indexed)
                    else None
                ),
                "lease_until": _safe_utc_timestamp(effective_lease_until),
                "grant_provenance": provenance or "unknown",
                "status": status,
                "candidate_id": candidate_id,
                "attempt_outcome": attempt_outcome,
                "failure": failure_summary,
                "fact_ids": sorted(set(evidence_ids)),
                "integrity": {"consistent": not local_issues, "issues": local_issues},
            }
        )
    return rows, all_issues


def _paginate(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    boundary: tuple[int, str] | None,
    reverse: bool,
    cursor_base: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | None, bool]:
    if reverse:
        ordered = sorted(
            rows, key=lambda row: (-row["grant_revision"], row["grant_record_id"])
        )
        if boundary is not None and not any(
            (row["grant_revision"], row["grant_record_id"]) == boundary for row in rows
        ):
            raise TaskDiagnosticError("cursor_boundary_missing")
        remaining = [
            row
            for row in ordered
            if boundary is None
            or (-row["grant_revision"], row["grant_record_id"])
            > (-boundary[0], boundary[1])
        ]
    else:
        ordered = sorted(rows, key=lambda row: (row["revision"], row["record_id"]))
        if boundary is not None and not any(
            (row["revision"], row["record_id"]) == boundary for row in rows
        ):
            raise TaskDiagnosticError("cursor_boundary_missing")
        remaining = [
            row
            for row in ordered
            if boundary is None or (row["revision"], row["record_id"]) > boundary
        ]
    page = remaining[:limit]
    more = len(remaining) > len(page)
    next_cursor = None
    if more and page:
        last = page[-1]
        boundary_value = (
            [last["grant_revision"], last["grant_record_id"]]
            if reverse
            else [last["revision"], last["record_id"]]
        )
        next_cursor = _cursor_encode({**cursor_base, "boundary": boundary_value})
    return page, next_cursor, more


def task_runs(
    store,
    contract_id: str,
    *,
    limit: int = 20,
    cursor: str | None = None,
    query_context=None,
) -> dict[str, Any]:
    """Return a stable page of exact historical claims for one Contract."""
    _valid_limit(limit)
    contract, domain_id, revision, indexed, by_id = _snapshot(
        store, contract_id, query_context=query_context
    )
    observed_at, boundary, watermark = _validate_cursor(
        cursor,
        command="runs",
        domain_id=domain_id,
        contract_id=contract.id,
        claim_id=None,
        limit=limit,
        revision=revision,
    )
    revision = watermark
    indexed = [
        (item_revision, record)
        for item_revision, record in indexed
        if item_revision <= watermark
    ]
    by_id = {record.id: (item_revision, record) for item_revision, record in indexed}
    rows, issues = _build_runs(contract.id, revision, observed_at, indexed, by_id)
    cursor_base = {
        "version": _CURSOR_VERSION,
        "command": "runs",
        "domain_id": domain_id,
        "contract_id": contract.id,
        "claim_id": None,
        "limit": limit,
        "watermark": revision,
        "observed_at": observed_at,
    }
    page, next_cursor, more = _paginate(
        rows, limit=limit, boundary=boundary, reverse=True, cursor_base=cursor_base
    )
    for row in page:
        row["candidate_id"] = _safe_candidate_id(row.get("candidate_id"))
    consistent = not issues
    return {
        "ok": consistent,
        "data": {
            "contract_id": contract.id,
            "domain_id": domain_id,
            "watermark": revision,
            "observed_at": observed_at,
            "runs": page,
            "next_cursor": next_cursor,
            "truncated": more,
            "integrity": {"consistent": consistent, "issues": issues},
        },
        "revision": revision,
    }


def task_log_binding(store, contract_id: str, claim_id: str) -> dict[str, Any]:
    """Bind a local operational-log request to one proven historical claim.

    This narrow bridge intentionally reuses the diagnostics snapshot and claim
    proof builder. It does not derive an invocation record or change task state.
    """
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise TaskDiagnosticError("invalid_claim_id")
    contract, domain_id, revision, run, _indexed, _by_id = _select_proven_claim_run(
        store, contract_id, claim_id
    )
    required = ("claim_id", "package_id", "worker_id", "grant_record_id")
    if not all(isinstance(run.get(key), str) and run[key] for key in required):
        raise TaskDiagnosticError("claim_binding_unavailable")
    return {
        "contract_id": contract.id,
        "domain_id": domain_id,
        "claim_id": claim_id,
        "epoch": run.get("epoch"),
        "package_id": run["package_id"],
        "worker_id": run["worker_id"],
        "grant_record_id": run["grant_record_id"],
        "watermark": revision,
    }


def _select_proven_claim_run(
    store,
    contract_id: str,
    claim_id: str,
    *,
    query_context=None,
    _proof_cache=None,
    _observed_at=None,
):
    """Select one internally consistent claim from a fixed diagnostic snapshot."""
    if query_context is not None:
        query_context.require_match(store)
    proof_observed_at = _observed_at or datetime.now(timezone.utc).isoformat()
    cache_key = None
    cached = None
    if query_context is not None and _proof_cache is not None:
        cache_key = (
            id(store),
            contract_id,
            id(query_context),
            query_context.revision,
            proof_observed_at,
        )
        cached = _proof_cache.get(cache_key)
        if cached is not None and cached[0] is not query_context:
            cached = None
    if cached is None:
        contract, domain_id, revision, indexed, by_id = _snapshot(
            store, contract_id, query_context=query_context
        )
        runs, issues = _build_runs(
            contract.id, revision, proof_observed_at, indexed, by_id
        )
        cached = (
            query_context,
            contract,
            domain_id,
            revision,
            runs,
            issues,
            indexed,
            by_id,
        )
        if cache_key is not None:
            _proof_cache[cache_key] = cached
    _, contract, domain_id, revision, runs, issues, indexed, by_id = cached
    if issues:
        raise TaskDiagnosticError("claim_integrity_error")
    matches = [run for run in runs if run.get("claim_id") == claim_id]
    if not matches:
        raise TaskDiagnosticError("claim_not_found")
    if len(matches) != 1 or not matches[0].get("integrity", {}).get("consistent"):
        raise TaskDiagnosticError("claim_integrity_error")
    return contract, domain_id, revision, matches[0], indexed, by_id


def _task_invocation_failure_binding(
    store,
    contract_id: str,
    claim_id: str,
    package_id: str,
    worker_id: str,
    epoch: int,
    *,
    query_context=None,
    _proof_cache=None,
    _observed_at=None,
    _include_witness_ids=False,
) -> dict[str, Any]:
    """Return safe details for one exact, proven invocation-failed claim.

    Selectors supplied by a caller are only a request. Every returned identity
    and failure field must match the same fixed-revision claim proof used by
    task runs and operational-log binding.
    """
    if (
        not isinstance(contract_id, str)
        or not contract_id
        or not _is_claim_id(claim_id)
        or not _is_package_id(package_id)
        or not isinstance(worker_id, str)
        or not worker_id
        or type(epoch) is not int
        or epoch < 1
    ):
        raise TaskDiagnosticError("invalid_claim_binding")
    contract, domain_id, watermark, run, indexed, by_id = _select_proven_claim_run(
        store,
        contract_id,
        claim_id,
        query_context=query_context,
        _proof_cache=_proof_cache,
        _observed_at=_observed_at,
    )
    if (
        run.get("package_id") != package_id
        or run.get("worker_id") != worker_id
        or type(run.get("epoch")) is not int
        or run.get("epoch") != epoch
    ):
        raise TaskDiagnosticError("claim_binding_mismatch")
    failure = run.get("failure")
    failure_id = failure.get("record_id") if isinstance(failure, dict) else None
    if run.get("status") != "invocation_failed" or not isinstance(failure_id, str):
        raise TaskDiagnosticError("invocation_failure_not_found")
    if failure_id not in run.get("fact_ids", ()) or failure_id not in by_id:
        raise TaskDiagnosticError("claim_integrity_error")
    failure_revision, failure_record = by_id[failure_id]
    if (
        failure_record.record_type != "worker.invocation_failed"
        or failure_revision != failure.get("revision")
        or not _record_is_valid(failure_record)
    ):
        raise TaskDiagnosticError("claim_integrity_error")
    releases = [
        by_id[fact_id][1]
        for fact_id in run.get("fact_ids", ())
        if fact_id in by_id
        and by_id[fact_id][1].record_type == "claim.released"
        and failure_id in by_id[fact_id][1].causal_parents
    ]
    if len(releases) != 1:
        raise TaskDiagnosticError("claim_integrity_error")
    binding = {
        "kind": "invocation_failure",
        "status": "invocation_failed",
        "contract_id": contract.id,
        "domain_id": domain_id,
        "claim_id": claim_id,
        "package_id": package_id,
        "worker_id": worker_id,
        "epoch": epoch,
        "grant_record_id": run["grant_record_id"],
        "failure_record_id": failure_id,
        "release_record_id": releases[0].id,
        "watermark": watermark,
        "category": failure.get("category", "unknown"),
        "retryable": failure.get("retryable"),
        "status_code": failure.get("status_code"),
        "usage": failure.get("usage"),
    }
    if _include_witness_ids:
        binding["witness_record_ids"] = tuple(run.get("fact_ids", ()))
    return binding


def task_invocation_failure_binding(
    store,
    contract_id: str,
    claim_id: str,
    package_id: str,
    worker_id: str,
    epoch: int,
    *,
    query_context=None,
    _proof_cache=None,
    _observed_at=None,
) -> dict[str, Any]:
    """Return the original exact invocation-failure diagnostic binding."""
    return _task_invocation_failure_binding(
        store,
        contract_id,
        claim_id,
        package_id,
        worker_id,
        epoch,
        query_context=query_context,
        _proof_cache=_proof_cache,
        _observed_at=_observed_at,
    )


def task_recovery_source_binding(
    store,
    contract_id: str,
    claim_id: str,
    source_record_id: str,
    source_kind: str,
    *,
    query_context=None,
    _proof_cache=None,
    _observed_at=None,
) -> dict[str, Any]:
    """Bind every recovery source to one proven claim run."""
    if (
        not isinstance(source_record_id, str)
        or not source_record_id
        or source_kind
        not in {
            "worker.invocation_failed",
            "claim.expired",
            "candidate.rejected",
            "projection.decided",
        }
    ):
        raise TaskDiagnosticError("invalid_recovery_source")
    contract, domain_id, watermark, run, _indexed, by_id = _select_proven_claim_run(
        store,
        contract_id,
        claim_id,
        query_context=query_context,
        _proof_cache=_proof_cache,
        _observed_at=_observed_at,
    )
    if source_kind == "worker.invocation_failed":
        if source_record_id not in by_id:
            raise TaskDiagnosticError("invocation_failure_not_found")
        source_revision, source = by_id[source_record_id]
        if (
            source.record_type != source_kind
            or not _record_is_valid(source)
            or source.payload.get("contract_id") != contract_id
            or source.payload.get("claim_id") != claim_id
            or source.payload.get("package_id") != run.get("package_id")
            or source.payload.get("worker_id") != run.get("worker_id")
        ):
            raise TaskDiagnosticError("recovery_source_integrity_error")
        invocation = _task_invocation_failure_binding(
            store,
            contract_id,
            claim_id,
            run.get("package_id"),
            run.get("worker_id"),
            run.get("epoch"),
            query_context=query_context,
            _proof_cache=_proof_cache,
            _observed_at=_observed_at,
            _include_witness_ids=True,
        )
        if invocation.get("failure_record_id") != source_record_id:
            raise TaskDiagnosticError("recovery_source_not_in_claim_proof")
        return {
            "kind": source_kind,
            "contract_id": invocation["contract_id"],
            "domain_id": invocation["domain_id"],
            "claim_id": invocation["claim_id"],
            "grant_record_id": invocation["grant_record_id"],
            "failure_record_id": source_record_id,
            "release_record_id": invocation["release_record_id"],
            "witness_record_ids": invocation["witness_record_ids"],
            "source_record_id": source_record_id,
            "source_revision": source_revision,
            "watermark": watermark,
        }
    expected_status = "expired" if source_kind == "claim.expired" else "rejected"
    if run.get("status") != expected_status:
        raise TaskDiagnosticError("recovery_source_status_mismatch")
    if source_kind == "projection.decided":
        if (
            source_record_id not in by_id
            or by_id[source_record_id][1].record_type != "projection.decided"
        ):
            raise TaskDiagnosticError("recovery_projection_source_missing")
        source = by_id[source_record_id][1]
        if (
            source.payload.get("candidate_id") != run.get("candidate_id")
            or source.payload.get("contract_id") != contract.id
            or source.payload.get("status") != "rejected"
            or not _record_is_valid(source)
            or not any(
                fact_id in by_id
                and by_id[fact_id][1].record_type == "candidate.rejected"
                and by_id[fact_id][1].payload.get("candidate_id")
                == run.get("candidate_id")
                for fact_id in run.get("fact_ids", ())
            )
        ):
            raise TaskDiagnosticError("recovery_projection_source_unproven")
        revision = by_id[source_record_id][0]
    else:
        if (
            source_record_id not in run.get("fact_ids", ())
            or source_record_id not in by_id
        ):
            raise TaskDiagnosticError("recovery_source_not_in_claim_proof")
        revision, source = by_id[source_record_id]
        if (
            source.record_type != source_kind
            or not _record_is_valid(source)
            or source.payload.get("contract_id") != contract.id
        ):
            raise TaskDiagnosticError("recovery_source_integrity_error")
    if source_kind == "claim.expired":
        if source.id not in run.get("fact_ids", ()):
            raise TaskDiagnosticError("recovery_expiration_not_proven")
    elif source_kind == "candidate.rejected":
        if (
            run.get("candidate_id") != source.payload.get("candidate_id")
            or run.get("attempt_outcome") != "rejected"
            or source.payload.get("claim_id") != claim_id
        ):
            raise TaskDiagnosticError("recovery_rejection_not_proven")
    return {
        "kind": source_kind,
        "status": expected_status,
        "contract_id": contract.id,
        "domain_id": domain_id,
        "claim_id": claim_id,
        "grant_record_id": run["grant_record_id"],
        "source_record_id": source.id,
        "source_revision": revision,
        "witness_record_ids": tuple(
            sorted(set(run.get("fact_ids", ())) | {source_record_id})
        ),
        "watermark": watermark,
    }


def task_recovery_ancestor_source_bindings(
    store, contract_id: str, *, query_context
) -> dict[str, dict[str, Any]]:
    """Re-derive source bindings carried by exact accepted recovery members.

    Context IDs and binding fields are only selectors. Each selected source is
    authenticated again by ``task_recovery_source_binding`` against the same
    immutable task query. The recovery feature later proves each declaration
    against its canonical intent.
    """
    import json

    from aigineering.core.publication_history import (
        index_published_records,
        read_effect_types,
        validate_published_member,
    )
    from aigineering.plugins.task_semantics import method_payload

    query_context.require_match(store)
    records = tuple(record for _revision, record in query_context.runtime_records)
    index = index_published_records(records)
    bindings: dict[str, dict[str, Any]] = {}
    target_id = contract_id
    visited: set[str] = set()
    for _depth in range(3):
        if target_id in visited:
            raise TaskDiagnosticError("recovery_source_history_cycle")
        visited.add(target_id)
        target = query_context.read_store.get_contract(target_id)
        if target is None:
            raise TaskDiagnosticError("recovery_source_contract_missing")
        payload = method_payload(target)
        if payload.get("recovery_intent_version") != 1:
            return bindings
        source_id = payload.get("recovery_source_contract_id")
        if not isinstance(source_id, str) or not source_id:
            raise TaskDiagnosticError("recovery_source_history_unverified")
        declaration_rows = tuple(
            record
            for record in records
            if record.record_type == "contract.declared"
            and isinstance(record.payload.get("contract"), Mapping)
            and record.payload["contract"].get("id") == target.id
        )
        if len(declaration_rows) != 1:
            raise TaskDiagnosticError("recovery_source_publication_unverified")
        declaration = declaration_rows[0]
        candidate_id = declaration.payload.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise TaskDiagnosticError("recovery_source_publication_unverified")
        try:
            receipt, head = validate_published_member(
                records,
                declaration,
                expected_candidate=candidate_id,
                domain_id=query_context.domain_id,
                record_index=index,
            )
            effect_types = read_effect_types(receipt.payload)
        except (TypeError, ValueError) as exc:
            raise TaskDiagnosticError("recovery_source_publication_unverified") from exc
        if effect_types != ("contract.declare", "asset.propose"):
            raise TaskDiagnosticError("recovery_source_publication_unverified")
        member_ids = head.payload.get("committed_record_ids")
        if (
            not isinstance(member_ids, (list, tuple))
            or not member_ids
            or len(member_ids) != len(set(member_ids))
        ):
            raise TaskDiagnosticError("recovery_source_publication_unverified")
        members = tuple(
            record
            for member_id in member_ids
            for record in index.by_id.get(member_id, ())
        )
        asset_members = tuple(
            record for record in members if record.record_type == "asset.committed"
        )
        if len(asset_members) != 1:
            raise TaskDiagnosticError("recovery_source_context_unverified")
        try:
            asset_receipt, asset_head = validate_published_member(
                records,
                asset_members[0],
                expected_candidate=candidate_id,
                expected_receipt=receipt.id,
                expected_head=head.id,
                domain_id=query_context.domain_id,
                record_index=index,
            )
        except (TypeError, ValueError) as exc:
            raise TaskDiagnosticError("recovery_source_context_unverified") from exc
        del asset_receipt, asset_head
        asset_id = asset_members[0].payload.get("asset", {}).get("id")
        contexts = tuple(
            asset for asset in query_context.assets if asset.id == asset_id
        )
        if len(contexts) != 1:
            raise TaskDiagnosticError("recovery_source_context_unverified")
        try:
            context = json.loads(contexts[0].content)
            hint = context["source_binding"]
        except (TypeError, ValueError, KeyError) as exc:
            raise TaskDiagnosticError("recovery_source_context_unverified") from exc
        if (
            not isinstance(hint, dict)
            or context.get("failed_contract_id") != source_id
            or hint.get("contract_id") != source_id
            or not isinstance(hint.get("claim_id"), str)
            or not isinstance(hint.get("source_record_id"), str)
            or not isinstance(hint.get("kind"), str)
        ):
            raise TaskDiagnosticError("recovery_source_context_unverified")
        binding = task_recovery_source_binding(
            store,
            source_id,
            hint["claim_id"],
            hint["source_record_id"],
            hint["kind"],
            query_context=query_context,
        )
        for key in (
            "kind",
            "contract_id",
            "claim_id",
            "grant_record_id",
            "failure_record_id",
            "release_record_id",
            "source_record_id",
            "witness_record_ids",
        ):
            observed = hint.get(key)
            if key == "witness_record_ids" and isinstance(observed, list):
                observed = tuple(observed)
            if observed != binding.get(key):
                raise TaskDiagnosticError("recovery_source_context_unverified")
        bindings[source_id] = binding
        target_id = source_id
    target = query_context.read_store.get_contract(target_id)
    if (
        target is not None
        and method_payload(target).get("recovery_intent_version") == 1
    ):
        raise TaskDiagnosticError("recovery_source_history_depth_exceeded")
    return bindings


def _diagnostic_events(
    contract_id: str,
    claim_id: str,
    runs: list[dict[str, Any]],
    indexed: list[tuple[int, RuntimeRecord]],
    by_id: dict[str, tuple[int, RuntimeRecord]],
    issues: list[dict[str, Any]],
    store,
) -> list[dict[str, Any]]:
    matching = [run for run in runs if run.get("claim_id") == claim_id]
    if not matching:
        claims = [
            record
            for _, record in indexed
            if record.record_type == "claim.granted"
            and record.payload.get("claim_id") == claim_id
        ]
        raise TaskDiagnosticError(
            "claim_not_found" if not claims else "claim_scope_mismatch"
        )
    if len(matching) != 1 or not matching[0]["integrity"]["consistent"]:
        issues.append(
            _issue(
                "selected_claim_integrity_invalid",
                *(matching[0].get("fact_ids", ()) if matching else ()),
            )
        )
    selected = matching[0]
    allowed_ids = set(selected.get("fact_ids", ()))
    # Exact claim-related terminal and recovery consequences. The payload field,
    # parent tuple, source fact type, and Contract must agree with the owning writer.
    source_ids = set(allowed_ids)
    for _revision, record in indexed:
        if record.record_type == "lifecycle.terminal":
            if record.id in allowed_ids:
                release_witness = any(
                    fact_id in by_id
                    and by_id[fact_id][1].record_type == "claim.released"
                    and by_id[fact_id][1].causal_parents
                    == (selected.get("grant_record_id"), record.id)
                    for fact_id in allowed_ids
                )
                if release_witness and _release_terminal_is_valid(
                    record, contract_id, by_id, indexed
                ):
                    continue
                candidate_id = selected.get("candidate_id")
                receipts = [
                    by_id[fact_id][1]
                    for fact_id in allowed_ids
                    if fact_id in by_id
                    and by_id[fact_id][1].record_type == "candidate.received"
                    and by_id[fact_id][1].payload.get("candidate_id") == candidate_id
                ]
                if (
                    record.payload.get("contract_id") != contract_id
                    or not _is_one_of(
                        record.payload.get("terminal"),
                        {"complete", "failed", "cancelled", "unreachable", "stalled"},
                    )
                    or set(record.payload)
                    - {"contract_id", "terminal", "reason", "actor_id"}
                    or len(receipts) != 1
                    or record.causal_parents != (receipts[0].id,)
                    or not _record_is_valid(record)
                ):
                    issues.append(
                        _issue("committed_terminal_causality_invalid", record.id)
                    )
                continue
            matching_parents = [
                parent for parent in record.causal_parents if parent in source_ids
            ]
            if not matching_parents:
                continue
            source_pair = by_id.get(matching_parents[0])
            source_type = source_pair[1].record_type if source_pair else None
            if (
                record.payload.get("contract_id") != contract_id
                or len(matching_parents) != 1
                or record.causal_parents != (matching_parents[0],)
                or source_type
                not in {
                    "worker.invocation_failed",
                    "candidate.rejected",
                    "claim.expired",
                }
                or not _is_one_of(
                    record.payload.get("terminal"),
                    {"complete", "failed", "cancelled", "unreachable", "stalled"},
                )
                or set(record.payload)
                - {"contract_id", "terminal", "reason", "actor_id"}
                or not _record_is_valid(record)
            ):
                issues.append(_issue("terminal_causality_invalid", record.id))
                continue
            allowed_ids.add(record.id)
            continue
        recovery_shape = {
            "worker_failure.recovery_scheduled": (
                "failure_id",
                "worker.invocation_failed",
            ),
            "worker_failure.recovery_unavailable": (
                "failure_id",
                "worker.invocation_failed",
            ),
            "candidate_rejection.recovery_scheduled": (
                "rejection_id",
                "candidate.rejected",
            ),
            "candidate_rejection.recovery_unavailable": (
                "rejection_id",
                "candidate.rejected",
            ),
            "claim_expiration.recovery_scheduled": (
                "expiration_id",
                "claim.expired",
            ),
            "claim_expiration.recovery_unavailable": (
                "expiration_id",
                "claim.expired",
            ),
        }.get(record.record_type)
        if recovery_shape is None:
            continue
        source_field, expected_source_type = recovery_shape
        source_id = record.payload.get(source_field)
        linked_by_parent = bool(set(record.causal_parents) & source_ids)
        linked_by_field = source_id in source_ids
        if not linked_by_parent and not linked_by_field:
            continue
        source_pair = by_id.get(source_id) if isinstance(source_id, str) else None
        expected_payload_keys = {"contract_id", source_field}
        if record.payload.get("reason") == "terminal_preserved":
            expected_payload_keys.update({"reason", "terminal"})
        else:
            if "reason" in record.payload:
                expected_payload_keys.add("reason")
            if "recovery_contract_id" in record.payload:
                expected_payload_keys.add("recovery_contract_id")
        if (
            record.payload.get("contract_id") != contract_id
            or record.causal_parents != (source_id,)
            or source_id not in source_ids
            or source_pair is None
            or source_pair[1].record_type != expected_source_type
            or set(record.payload) != expected_payload_keys
            or (
                "reason" in record.payload
                and not isinstance(record.payload.get("reason"), str)
            )
            or (
                "recovery_contract_id" in record.payload
                and (
                    not isinstance(record.payload.get("recovery_contract_id"), str)
                    or not record.payload.get("recovery_contract_id")
                )
            )
            or (
                "terminal" in record.payload
                and not _is_one_of(
                    record.payload.get("terminal"),
                    {"complete", "failed", "cancelled", "unreachable", "stalled"},
                )
            )
            or not _record_is_valid(record)
        ):
            issues.append(_issue("recovery_causality_invalid", record.id))
            continue
        allowed_ids.add(record.id)
    events: list[dict[str, Any]] = []
    for revision, record in indexed:
        if record.id not in allowed_ids:
            continue
        safe_recorded_at = _safe_utc_timestamp(record.recorded_at)
        if safe_recorded_at is None:
            issues.append(_issue("recorded_at_invalid", record.id))
        row: dict[str, Any] = {
            "record_id": record.id,
            "record_type": record.record_type
            if record.record_type in _EVENT_RECORD_TYPES
            else "runtime_fact",
            "revision": revision,
            "recorded_at": safe_recorded_at,
            "causal_parents": [
                parent for parent in record.causal_parents if parent in allowed_ids
            ],
        }
        payload = record.payload
        if record.record_type == "worker.invocation_failed":
            row.update(
                {
                    "category": _safe_category(payload.get("category")),
                    "retryable": payload.get("retryable")
                    if type(payload.get("retryable")) is bool
                    else None,
                    "status_code": payload.get("status_code")
                    if type(payload.get("status_code")) is int
                    else None,
                    "usage": _safe_usage(payload.get("usage_metadata")),
                }
            )
        elif record.record_type == "attempt.closed":
            outcome = payload.get("outcome")
            row["outcome"] = (
                outcome if _is_one_of(outcome, _ATTEMPT_OUTCOMES) else "unknown"
            )
        elif record.record_type == "lifecycle.terminal":
            terminal = payload.get("terminal")
            row["terminal"] = (
                terminal
                if _is_one_of(
                    terminal,
                    {"complete", "failed", "cancelled", "unreachable", "stalled"},
                )
                else "unknown"
            )
        elif record.record_type.endswith(".recovery_unavailable"):
            reason = safe_recovery_reason(payload.get("reason"))
            if reason is not None:
                row["reason"] = reason
            elif "reason" in payload:
                row["reason_status"] = "unsupported_observation"
            target_id = payload.get("recovery_contract_id")
            target = (
                store.get_contract(target_id) if isinstance(target_id, str) else None
            )
            if target is not None:
                row["reported_recovery_contract_id"] = target.id
                row["target_verification"] = "unverified_observation"
            elif "recovery_contract_id" in payload:
                row["target_verification"] = "missing_or_invalid_reference"
        elif record.record_type in {
            "claim.granted",
            "claim.renewed",
            "claim.submitted",
            "claim.released",
            "claim.expired",
        }:
            row["claim_state"] = record.record_type.removeprefix("claim.")
        elif record.record_type in _EVENT_RECORD_TYPES:
            row["event"] = record.record_type
        else:
            row["event"] = "runtime_fact"
        events.append(row)
    return events


def task_diagnostics(
    store,
    contract_id: str,
    claim_id: str,
    *,
    limit: int = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return safe event summaries for one exact historical claim attempt."""
    _valid_limit(limit)
    if not _is_claim_id(claim_id):
        raise TaskDiagnosticError("invalid_claim_id")
    contract, domain_id, revision, indexed, by_id = _snapshot(store, contract_id)
    observed_at, boundary, watermark = _validate_cursor(
        cursor,
        command="diagnostics",
        domain_id=domain_id,
        contract_id=contract.id,
        claim_id=claim_id,
        limit=limit,
        revision=revision,
    )
    revision = watermark
    indexed = [
        (item_revision, record)
        for item_revision, record in indexed
        if item_revision <= watermark
    ]
    by_id = {record.id: (item_revision, record) for item_revision, record in indexed}
    runs, run_issues = _build_runs(contract.id, revision, observed_at, indexed, by_id)
    issues = [
        issue
        for run in runs
        if run.get("claim_id") == claim_id
        for issue in run["integrity"]["issues"]
    ]
    events = _diagnostic_events(
        contract.id, claim_id, runs, indexed, by_id, issues, store
    )
    cursor_base = {
        "version": _CURSOR_VERSION,
        "command": "diagnostics",
        "domain_id": domain_id,
        "contract_id": contract.id,
        "claim_id": claim_id,
        "limit": limit,
        "watermark": revision,
        "observed_at": observed_at,
    }
    page, next_cursor, more = _paginate(
        events, limit=limit, boundary=boundary, reverse=False, cursor_base=cursor_base
    )
    consistent = not issues
    return {
        "ok": consistent,
        "data": {
            "contract_id": contract.id,
            "claim_id": claim_id,
            "domain_id": domain_id,
            "watermark": revision,
            "observed_at": observed_at,
            "events": page,
            "next_cursor": next_cursor,
            "truncated": more,
            "integrity": {"consistent": consistent, "issues": issues},
        },
        "revision": revision,
    }
