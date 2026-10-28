"""Shared application owner for signed task Contract publication."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.contract_admission import validate_contract_commitment
from aigineering.core.control_plane import (
    bind_contract_label_assets,
    build_control_plane_contract,
)
from aigineering.protocol.effect_builders import contract_declaration_effect
from aigineering.protocol.types import Contract
from aigineering.local_publication import (
    commit_local_effect,
    commit_local_effects,
    require_accepted,
    verify_durable_candidate_member,
    verify_durable_rejection_member,
)
from aigineering.protocol.effect_builders import contract_cancellation_effect
from aigineering.protocol.immutability import deep_thaw
from aigineering.runtime_services import RedisSyncPending, sync_runtime
from aigineering.task_views import project_task_status
from aigineering.plugins.task_semantics import retry_contract
from aigineering.protocol.wire import contract_to_dict
from aigineering.protocol.runtime_record import (
    runtime_record_effective_payload,
    validate_runtime_record,
)
from aigineering.core.publication_history import (
    read_effect_types,
    validate_published_member,
)


def prepare_task_contract(fields: Mapping[str, object]) -> Contract:
    """Build and validate an operator-declared Contract before opening a Store."""
    values = dict(fields)
    budget = values.get("budget", 5)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError("Contract budget must be a positive integer")
    for field in ("sensitive_input_policy", "acceptance_policy"):
        value = values.get(field)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"Contract {field} must be a JSON object")

    contract = build_control_plane_contract(**values)
    validate_contract_commitment(contract)
    return contract


def publish_task_contract(
    store,
    contract: Contract,
    *,
    idempotency_key: str | None = None,
    causal_parents: tuple[str, ...] = (),
    freeze_labels: bool = True,
) -> Contract:
    """Freeze label-selected Assets and publish a Contract through a signed Candidate."""
    bound = bind_contract_label_assets(contract, store) if freeze_labels else contract
    validate_contract_commitment(bound)
    require_accepted(
        commit_local_effect(
            store,
            contract_declaration_effect(bound),
            idempotency_key=idempotency_key or f"contract:{bound.id}",
            causal_parents=causal_parents,
        )
    )
    return bound


class RetryPublicationRejected(ValueError):
    """A retry Candidate was rejected and its exact audit result is durable."""

    def __init__(self, data: dict, revision: int) -> None:
        self.data = data
        self.revision = revision
        super().__init__("retry Candidate was rejected")


class CancelPublicationRejected(ValueError):
    """A cancellation Candidate was rejected and its exact audit result is durable."""

    def __init__(self, data: dict, revision: int) -> None:
        self.data = data
        self.revision = revision
        super().__init__(str(data.get("rejection_reason", "Candidate rejected")))


def retry_local_task_result(store, contract_id: str) -> tuple[dict, int]:
    """Publish a deterministic retry and return the exact signed result view."""
    _original, _proposed, data, revision = _retry_local_task_operation(
        store, contract_id
    )
    return data, revision


def retry_local_task(store, contract_id: str) -> tuple[Contract, Contract]:
    """Compatibility tuple seam for publishing a security-equivalent retry."""
    original, proposed, _data, _revision = _retry_local_task_operation(
        store, contract_id
    )
    return original, proposed


def _retry_local_task_operation(store, contract_id: str):
    original = store.get_contract(contract_id)
    if original is None:
        raise LookupError(f"Contract '{contract_id}' not found")
    proposed = retry_contract(original)
    key = f"retry:{original.id}:{proposed.id}"
    effect = contract_declaration_effect(proposed)
    try:
        decision = commit_local_effect(
            store,
            effect,
            idempotency_key=key,
            causal_parents=(original.id,),
        )
    except CandidateCommitRejected as exc:
        decision = exc.decision
        data, revision = _retry_rejection_result(store, decision, key)
        try:
            sync_runtime(store, accepted=False)
        except RedisSyncPending as pending:
            _attach_retry_pending(pending, decision, data, revision, accepted=False)
            raise
        raise RetryPublicationRejected(data, revision) from exc
    except RedisSyncPending as pending:
        decision = pending.decision
        if decision is None:
            raise
        if decision.accepted is True:
            data, revision = _retry_accepted_result(
                store, decision, original, proposed, key
            )
            _attach_retry_pending(pending, decision, data, revision, accepted=True)
        elif decision.accepted is False:
            data, revision = _retry_rejection_result(store, decision, key)
            _attach_retry_pending(pending, decision, data, revision, accepted=False)
        raise

    if not decision.accepted:
        data, revision = _retry_rejection_result(store, decision, key)
        raise RetryPublicationRejected(data, revision)
    data, revision = _retry_accepted_result(store, decision, original, proposed, key)
    return original, proposed, data, revision


@dataclass(frozen=True)
class RetryPublicationWitness:
    """Exact read-only publication witnesses for one canonical retry edge."""

    source_contract_id: str
    target_contract_id: str
    source_declaration_id: str
    source_receipt_id: str
    source_head_id: str
    target_declaration_id: str
    target_receipt_id: str
    target_head_id: str
    candidate_id: str


@dataclass(frozen=True)
class RetryPublicationIssue:
    """Safe references for a retry-shaped publication that did not verify."""

    code: str
    target_contract_id: str | None = None
    source_contract_id: str | None = None
    record_id: str | None = None


def validate_retry_publication_relations(
    query_context, records: tuple, publication_index
) -> tuple[tuple[RetryPublicationWitness, ...], tuple[RetryPublicationIssue, ...]]:
    """Validate retry-shaped Contracts against one immutable query and index.

    This is an application-level read-only composition seam. Generic receipt,
    head and member proofs remain owned by ``core.publication_history``.
    """
    captured_records = tuple(
        record for _revision, record in query_context.runtime_records
    )
    if len(records) != len(captured_records) or any(
        supplied is not captured
        for supplied, captured in zip(records, captured_records)
    ):
        raise ValueError("retry history does not match its captured query")

    contracts_by_id: dict[str, list[Contract]] = {}
    for contract in query_context.contracts:
        contracts_by_id.setdefault(contract.id, []).append(contract)
    declarations_by_target: dict[str, list] = {}
    for record in records:
        if record.record_type != "contract.declared" or not isinstance(
            record.payload, Mapping
        ):
            continue
        declared = record.payload.get("contract")
        if isinstance(declared, Mapping):
            target_id = declared.get("id")
            if isinstance(target_id, str) and target_id:
                declarations_by_target.setdefault(target_id, []).append(record)

    candidate_ids = {
        contract.id
        for contract in query_context.contracts
        if contract.origin == "retry"
    }
    candidate_ids.update(
        target_id
        for target_id, declarations in declarations_by_target.items()
        if any(
            isinstance(record.payload.get("contract"), Mapping)
            and record.payload["contract"].get("origin") == "retry"
            for record in declarations
        )
    )

    witnesses: list[RetryPublicationWitness] = []
    issues: list[RetryPublicationIssue] = []

    def issue(
        target_id, source_id=None, record_id=None, code="retry_publication_unverified"
    ):
        if source_id is not None and len(contracts_by_id.get(source_id, ())) != 1:
            source_id = None
        issues.append(
            RetryPublicationIssue(
                code=code,
                target_contract_id=target_id,
                source_contract_id=source_id,
                record_id=record_id,
            )
        )

    publication_cache: dict[tuple[str, bool], tuple] = {}

    def published_declaration(contract, *, require_sole_declaration=False):
        cache_key = (contract.id, require_sole_declaration)
        cached = publication_cache.get(cache_key)
        if cached is not None:
            return cached
        matches = declarations_by_target.get(contract.id, ())
        if len(matches) != 1:
            raise ValueError("declaration missing or ambiguous")
        declaration = matches[0]
        if deep_thaw(declaration.payload.get("contract")) != deep_thaw(
            contract_to_dict(contract)
        ):
            raise ValueError("declaration differs from materialized Contract")
        receipt, head = validate_published_member(
            records,
            declaration,
            domain_id=query_context.domain_id,
            record_index=publication_index,
        )
        effect_types = read_effect_types(receipt.payload)
        if require_sole_declaration and effect_types != ("contract.declare",):
            raise ValueError("publication receipt has an unexpected effect")
        cached = (declaration, receipt, head)
        publication_cache[cache_key] = cached
        return cached

    def exact_known_retry_source(target_id, declaration):
        """Read only a valid target receipt's causal source for safe diagnostics."""
        try:
            validate_runtime_record(declaration)
            declared = declaration.payload.get("contract")
            if (
                not isinstance(declared, Mapping)
                or declared.get("id") != target_id
                or declared.get("origin") != "retry"
            ):
                return None
            declaration_candidate = declaration.payload.get("candidate_id")
            receipt_ids = tuple(declaration.causal_parents)
            if not isinstance(declaration_candidate, str) or len(receipt_ids) != 1:
                return None
            receipt_rows = publication_index.by_id.get(receipt_ids[0], ())
            if len(receipt_rows) != 1:
                return None
            receipt = receipt_rows[0]
            validate_runtime_record(receipt)
            if (
                receipt.record_type != "candidate.received"
                or not isinstance(receipt.payload, Mapping)
                or receipt.payload.get("domain_id") != query_context.domain_id
                or receipt.payload.get("candidate_id") != declaration_candidate
            ):
                return None
            source_ids = tuple(receipt.causal_parents)
            if len(source_ids) == 1 and isinstance(source_ids[0], str):
                return source_ids[0]
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            return None
        return None

    for target_id in sorted(candidate_ids):
        target_matches = contracts_by_id.get(target_id, ())
        if len(target_matches) != 1:
            declarations = declarations_by_target.get(target_id, ())
            issue(target_id, record_id=declarations[0].id if declarations else None)
            continue
        target = target_matches[0]
        declarations = declarations_by_target.get(target_id, ())
        if len(declarations) != 1:
            issue(target_id, record_id=declarations[0].id if declarations else None)
            continue
        target_declaration = declarations[0]
        source_id = exact_known_retry_source(target_id, target_declaration)
        try:
            if target.origin != "retry":
                raise ValueError("materialized Contract is not a retry")
            target_declaration, target_receipt, target_head = published_declaration(
                target, require_sole_declaration=True
            )
            source_parents = tuple(target_receipt.causal_parents)
            if len(source_parents) != 1 or not isinstance(source_parents[0], str):
                raise ValueError("retry Candidate has no exact source parent")
            source_id = source_parents[0]
            source_matches = contracts_by_id.get(source_id, ())
            if len(source_matches) != 1:
                raise ValueError("retry source Contract is missing or ambiguous")
            source = source_matches[0]
            source_declaration, source_receipt, source_head = published_declaration(
                source
            )
            expected = retry_contract(source)
            expected_fields = deep_thaw(contract_to_dict(expected))
            if (
                expected.id != target.id
                or deep_thaw(contract_to_dict(target)) != expected_fields
                or deep_thaw(target_declaration.payload.get("contract"))
                != expected_fields
            ):
                raise ValueError("retry Contract differs from canonical successor")
            candidate_id = target_receipt.payload.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError("retry receipt has no Candidate identity")
            witnesses.append(
                RetryPublicationWitness(
                    source_contract_id=source.id,
                    target_contract_id=target.id,
                    source_declaration_id=source_declaration.id,
                    source_receipt_id=source_receipt.id,
                    source_head_id=source_head.id,
                    target_declaration_id=target_declaration.id,
                    target_receipt_id=target_receipt.id,
                    target_head_id=target_head.id,
                    candidate_id=candidate_id,
                )
            )
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            issue(
                target_id,
                source_id,
                target_declaration.id if target_declaration is not None else None,
            )

    # A rejected declaration Candidate has no accepted target fact to prove.
    # Report only its exact source and rejection record, never a proposed edge.
    record_by_id: dict[str, list] = {}
    for record in records:
        if isinstance(record.id, str):
            record_by_id.setdefault(record.id, []).append(record)
    for rejection in records:
        if rejection.record_type != "candidate.rejected" or not isinstance(
            rejection.payload, Mapping
        ):
            continue
        try:
            validate_runtime_record(rejection)
            if read_effect_types(rejection.payload) != ("contract.declare",):
                continue
            receipt_ids = tuple(rejection.causal_parents)
            if len(receipt_ids) != 1:
                continue
            receipts = record_by_id.get(receipt_ids[0], ())
            if len(receipts) != 1:
                continue
            receipt = receipts[0]
            validate_runtime_record(receipt)
            if (
                receipt.record_type != "candidate.received"
                or not isinstance(receipt.payload, Mapping)
                or receipt.payload.get("domain_id") != query_context.domain_id
                or read_effect_types(receipt.payload) != ("contract.declare",)
                or tuple(rejection.causal_parents) != (receipt.id,)
                or receipt.payload.get("candidate_id")
                != rejection.payload.get("candidate_id")
            ):
                continue
            source_parents = tuple(receipt.causal_parents)
            if len(source_parents) != 1 or source_parents[0] not in contracts_by_id:
                continue
            source_matches = contracts_by_id[source_parents[0]]
            if len(source_matches) != 1:
                continue
            published_declaration(source_matches[0])
        except (KeyError, TypeError, ValueError, RuntimeError, AttributeError):
            continue
        issues.append(
            RetryPublicationIssue(
                code="declaration_candidate_rejected_unverified",
                source_contract_id=source_parents[0],
                record_id=rejection.id,
            )
        )

    return tuple(witnesses), tuple(issues)


def _retry_accepted_result(store, decision, original, proposed, key: str):
    if not decision.accepted:
        raise RuntimeError("retry Candidate did not accept its declaration")
    contract_fields = deep_thaw(contract_to_dict(proposed))
    matches = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == "contract.declared"
        and record.payload.get("candidate_id") == decision.candidate_id
        and deep_thaw(record.payload.get("contract", {})) == contract_fields
    )
    receipts = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == decision.candidate_id
    )
    if len(matches) != 1 or len(receipts) != 1:
        raise RuntimeError("accepted retry has no unique declaration and receipt")
    if original.id not in receipts[0].causal_parents:
        raise RuntimeError("retry Candidate does not name its original Contract")
    durable = _verify_retry_member(store, decision, receipts[0], matches[0])
    record_revision = next(
        (
            revision
            for revision, record in store.scan_runtime_records(
                record_type="contract.declared"
            )
            if record.id == durable.id
        ),
        None,
    )
    if record_revision is None:
        raise RuntimeError("durable retry declaration has no Store revision")
    data = {
        "original_contract_id": original.id,
        "retry_contract_id": proposed.id,
        "budget": proposed.budget,
        "status": "declared",
        "record_id": durable.id,
        "record_revision": record_revision,
        "candidate_id": decision.candidate_id,
        "idempotency_key": key,
        "fact_ids": [durable.id],
    }
    return data, store.get_runtime_revision()


def _verify_retry_member(store, decision, receipt, member):
    """Accept the original exact durable head on deterministic Candidate replay.

    CandidateCommitter can return a newly projected head during replay even
    though its candidate ID already has a durable head. The returned head is
    not itself evidence. Bind the replay to the durable receipt, member, and
    unique original head for this exact candidate instead.
    """
    current_heads = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == decision.candidate_id
    )
    if len(current_heads) != 1:
        raise RuntimeError("accepted retry has no unique Candidate head")
    current_head = current_heads[0]
    current_members = tuple(current_head.payload.get("committed_record_ids", ()))
    if (
        receipt.id not in current_head.causal_parents
        or member.id not in current_members
        or member.id not in current_head.causal_parents
    ):
        raise RuntimeError("retry declaration is outside its Candidate head")

    durable_current_head = store.get_runtime_record(current_head.id)
    if durable_current_head is not None:
        return verify_durable_candidate_member(store, decision, member)

    durable_receipt = store.get_runtime_record(receipt.id)
    durable_member = store.get_runtime_record(member.id)
    if (
        durable_receipt is None
        or durable_receipt.record_type != "candidate.received"
        or durable_receipt.schema_version != receipt.schema_version
        or runtime_record_effective_payload(durable_receipt)
        != runtime_record_effective_payload(receipt)
        or durable_member is None
        or durable_member.record_type != member.record_type
        or durable_member.schema_version != member.schema_version
        or runtime_record_effective_payload(durable_member)
        != runtime_record_effective_payload(member)
    ):
        raise RuntimeError("replayed retry has no exact durable receipt and output")
    original_heads = tuple(
        record
        for _revision, record in store.scan_runtime_records(
            record_type="candidate.committed"
        )
        if record.payload.get("candidate_id") == decision.candidate_id
    )
    if len(original_heads) != 1:
        raise RuntimeError("replayed retry has no unique original committed head")
    original_head = original_heads[0]
    original_members = tuple(original_head.payload.get("committed_record_ids", ()))
    if (
        durable_receipt.id not in original_head.causal_parents
        or durable_member.id not in original_members
        or durable_member.id not in original_head.causal_parents
    ):
        raise RuntimeError("durable retry is not in its original committed head")
    return durable_member


def _retry_rejection_result(store, decision, key: str):
    rejection = verify_durable_rejection_member(store, decision)
    data = {
        "rejection_fact_id": rejection.id,
        "fact_ids": [rejection.id],
        "idempotency_key": key,
    }
    return data, store.get_runtime_revision()


def _attach_retry_pending(pending, decision, data, revision, *, accepted: bool):
    pending.accepted = accepted
    pending.decision = decision
    pending.data = data
    pending.revision = revision


def publish_task_cancel(
    store,
    contract_id: str,
    reason: str,
    *,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Publish one cancellation through the shared signed Candidate boundary."""
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be a non-empty string")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must not be empty or whitespace")
    normalized_reason = reason.strip()
    try:
        normalized_reason.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("reason must be valid UTF-8") from exc
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise ValueError("idempotency key must not be blank")
    key = (
        idempotency_key
        if idempotency_key is not None
        else (
            f"task-cancel:{contract_id}:"
            f"{hashlib.sha256(normalized_reason.encode()).hexdigest()}"
        )
    )
    contract = store.get_contract(contract_id)
    if contract is None:
        raise LookupError(f"Contract {contract_id!r} does not exist")

    from aigineering.runtime_services import sync_runtime

    sync_runtime(store)
    try:
        decision = commit_local_effects(
            store,
            (contract_cancellation_effect(contract_id, normalized_reason),),
            idempotency_key=key,
            causal_parents=(contract_id,),
        )
    except CandidateCommitRejected as exc:
        from aigineering.runtime_services import sync_runtime

        decision = exc.decision
        rejection = _cancel_rejection_result(
            store, decision, contract_id, key, normalized_reason
        )
        try:
            sync_runtime(store, accepted=False)
        except RedisSyncPending as pending:
            pending.decision = decision
            pending.data = rejection
            pending.revision = store.get_runtime_revision()
            raise
        raise CancelPublicationRejected(
            rejection, store.get_runtime_revision()
        ) from exc
    except RedisSyncPending as exc:
        decision = exc.decision
        if decision is None:
            raise
        if not decision.accepted:
            exc.data = _cancel_rejection_result(
                store, decision, contract_id, key, normalized_reason
            )
            exc.revision = store.get_runtime_revision()
            raise
        data = _cancel_accepted_result(
            store, decision, contract, key, normalized_reason
        )
        exc.data = data
        exc.revision = store.get_runtime_revision()
        raise

    if not decision.accepted:
        rejected = _cancel_rejection_result(
            store, decision, contract_id, key, normalized_reason
        )
        raise CancelPublicationRejected(rejected, store.get_runtime_revision())
    data = _cancel_accepted_result(store, decision, contract, key, normalized_reason)
    return data, store.get_runtime_revision()


def _cancel_accepted_result(store, decision, contract, key: str, reason: str) -> dict:
    members = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == "lifecycle.terminal"
        and record.payload.get("contract_id") == contract.id
        and record.payload.get("terminal") == "cancelled"
    )
    if len(members) != 1:
        raise RuntimeError("accepted cancellation has no unique terminal fact")
    durable = verify_durable_candidate_member(store, decision, members[0])
    return {
        "contract_id": contract.id,
        "status": project_task_status(contract, store)["status"],
        "accepted": True,
        "reason": reason,
        "record_id": durable.id,
        "candidate_id": decision.candidate_id,
        "idempotency_key": key,
        "fact_ids": [durable.id],
    }


def _cancel_rejection_result(
    store, decision, contract_id: str, key: str, reason: str
) -> dict:
    durable = verify_durable_rejection_member(store, decision)
    data = {
        "contract_id": contract_id,
        "accepted": False,
        "reason": reason,
        "idempotency_key": key,
        "fact_ids": [durable.id],
        "rejection_record_id": durable.id,
        "rejection_reason": str(durable.payload.get("reason", "Candidate rejected")),
    }
    if durable.record_type == "candidate.authentication_rejected":
        data["claimed_candidate_id"] = str(
            durable.payload.get("claimed_candidate_id", "")
        )
    else:
        data["candidate_id"] = decision.candidate_id
    return data
