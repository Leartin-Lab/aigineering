"""Application services for signed task handoff Candidates."""

from __future__ import annotations

import uuid
from dataclasses import replace
from collections.abc import Mapping, Sequence

from aigineering.application import collaboration_service, require_redis
from aigineering.core.claim_assignments import (
    assignment_context_from_projection,
    assignment_head,
)
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.domain import load_genesis
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.worker_routing import is_compatible
from aigineering.local_identity import (
    actor_key_path,
    ensure_local_plugin_publisher,
    load_actor_signer,
)
from aigineering.plugins.handoff import (
    HANDOFF_EFFECTS,
    project_task_handoffs,
    task_edit_snapshot,
    validate_task_edit_request,
    validate_task_edit_snapshot,
)
from aigineering.protocol.effect_builders import task_handoff_effect
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.candidate import (
    candidate_received_record,
    create_candidate_proposal,
    verify_candidate_proposal,
)
from aigineering.protocol.identity import compute_content_hash
from aigineering.protocol.runtime_record import runtime_record_effective_payload
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.runtime_services import RedisSyncPending, sync_runtime

_PLUGIN_ID = "task.handoff.operator.v1"
_CAPABILITIES = (
    "contract.cancel",
    "contract.publish",
    "task.block",
    "task.handoff",
)
_EDIT_PLUGIN_ID = "task.edit.operator.v1"
_EDIT_CAPABILITIES = (
    "contract.cancel",
    "contract.publish",
    "task.block",
    "task.edit",
    "task.handoff",
)


def _validate_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8") from exc
    if size > 64 * 1024:
        raise ValueError(f"{field} exceeds 65536 UTF-8 bytes")


def validate_task_handoff(
    store,
    contract_id: str,
    reason: str,
    *,
    budget: int | None = None,
    hold_reason: str = "",
    assignment_worker_id: str | None = None,
    clear_assignment: bool = False,
    source_assignment_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    """Validate request shape and immutable target existence before provisioning."""
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be non-empty text")
    _validate_text(reason, "reason")
    if budget is not None and (
        isinstance(budget, bool) or not isinstance(budget, int) or budget < 1
    ):
        raise ValueError("budget must be a positive integer when supplied")
    if not isinstance(hold_reason, str):
        raise ValueError("hold_reason must be text")
    if hold_reason:
        _validate_text(hold_reason, "hold_reason")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise ValueError("idempotency_key must be non-blank text")
    source = store.get_contract(contract_id)
    if source is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    override_present = assignment_worker_id is not None or clear_assignment
    if override_present != (source_assignment_id is not None):
        raise ValueError("assignment override requires an exact source assignment ID")
    if clear_assignment == (assignment_worker_id is not None):
        if override_present:
            raise ValueError("choose exactly one assignment Worker or clear")
    if assignment_worker_id is not None and not assignment_worker_id.strip():
        raise ValueError("assignment Worker ID must be non-empty")
    if override_present:
        assignment_context = assignment_context_from_projection(
            load_effect_projection_context(store)
        )
        head = assignment_head(assignment_context, contract_id=contract_id)
        head_id = head.id if head is not None else ""
        if source_assignment_id != head_id:
            raise ValueError(
                "source assignment ID does not match the exact current head"
            )
        if clear_assignment and (head is None or not head.payload.get("worker_id")):
            raise ValueError(
                "cannot clear an unassigned source; omit the override to inherit"
            )
        if assignment_worker_id is not None:
            worker = next(
                (
                    item
                    for item in assignment_context.projection.registrations
                    if item.worker_id == assignment_worker_id
                ),
                None,
            )
            if worker is None or not is_compatible(source, worker):
                raise ValueError(
                    "assignment override Worker is not currently compatible"
                )


def _publisher(store, *, assignment_override: bool = False):
    plugin_id = (
        "task.handoff.assignment-override.operator.v1"
        if assignment_override
        else _PLUGIN_ID
    )
    capabilities = (
        (*_CAPABILITIES, "task.assignment.manage")
        if assignment_override
        else _CAPABILITIES
    )
    return replace(
        ensure_local_plugin_publisher(store, plugin_id, capabilities),
        additional_effects=HANDOFF_EFFECTS,
    )


def _durable_link_view(store, proposed, revision: int | None = None) -> dict:
    if proposed is None:
        raise RuntimeError("accepted task handoff produced no task.handed_off link")
    durable = store.get_runtime_record(proposed.id)
    if durable is None or durable.record_type != "task.handed_off":
        raise RuntimeError(
            "accepted task handoff link is missing from the durable Store"
        )
    # RuntimeRecord payloads are recursively frozen. Thaw the durable wire
    # value before merging it into an application/CLI JSON view so nested
    # mappings and tuples remain JSON objects and arrays.
    payload = deep_thaw(durable.payload)
    if revision is None:
        revision = next(
            (
                item_revision
                for item_revision, item in store.scan_runtime_records(
                    record_type="task.handed_off"
                )
                if item.id == durable.id
            ),
            None,
        )
    if revision is None:
        raise RuntimeError("durable task handoff link has no Store revision")
    successor_id = str(payload["successor_id"])
    hold_id = str(payload.get("hold_id", ""))
    return {
        "record_id": durable.id,
        "record_type": durable.record_type,
        "revision": revision,
        "recorded_at": durable.recorded_at,
        **payload,
        "unblock_command": (
            f"aig task unblock {successor_id} --hold {hold_id} --reason 'ready to continue'"
            if hold_id
            else ""
        ),
        "run_command": f"aig run --task {successor_id}",
    }


def publish_task_handoff(
    store,
    contract_id: str,
    reason: str,
    *,
    budget: int | None = None,
    hold_reason: str = "",
    assignment_worker_id: str | None = None,
    clear_assignment: bool = False,
    source_assignment_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Publish a handoff Candidate and return its original durable link fact."""
    require_redis()
    validate_task_handoff(
        store,
        contract_id,
        reason,
        budget=budget,
        hold_reason=hold_reason,
        assignment_worker_id=assignment_worker_id,
        clear_assignment=clear_assignment,
        source_assignment_id=source_assignment_id,
        idempotency_key=idempotency_key,
    )
    override = assignment_worker_id is not None or clear_assignment
    publisher = _publisher(store, assignment_override=override)
    # Plugin authorization can be committed separately from the handoff.
    sync_runtime(store)
    effect = task_handoff_effect(
        contract_id,
        reason,
        budget=budget,
        hold_reason=hold_reason,
        assignment_override=(
            {
                "source_assignment_id": str(source_assignment_id),
                "worker_id": "" if clear_assignment else str(assignment_worker_id),
            }
            if override
            else None
        ),
    )
    try:
        decision = publisher.publish(
            (effect,),
            idempotency_key=idempotency_key or f"task-handoff:{uuid.uuid4().hex}",
            causal_parents=(
                contract_id,
                *((source_assignment_id,) if override and source_assignment_id else ()),
            ),
        )
    except CandidateCommitRejected:
        sync_runtime(store, accepted=False)
        raise
    if not decision.accepted:
        sync_runtime(store, accepted=False)
        rejected = next(
            (
                item
                for item in decision.runtime_records
                if item.record_type == "candidate.rejected"
            ),
            None,
        )
        raise ValueError(
            str(rejected.payload.get("reason", "Candidate was rejected"))
            if rejected is not None
            else "Candidate was not accepted"
        )
    sync_runtime(store, accepted=True)
    proposed = next(
        (
            item
            for item in decision.runtime_records
            if item.record_type == "task.handed_off"
            and item.payload.get("contract_id") == contract_id
        ),
        None,
    )
    view = _durable_link_view(store, proposed)
    return view, store.get_runtime_revision()


def task_handoff_records(store, *, contract_id: str) -> tuple[list[dict], int]:
    """Read immutable handoff links and their successor/hold references."""
    require_redis()
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    collaboration_service(store).sync()
    pairs = store.scan_runtime_records(record_type="task.handed_off")
    current_revision = store.get_runtime_revision()
    projected = {
        record.id
        for record in project_task_handoffs(
            (record for _revision, record in pairs), source_contract_id=contract_id
        )
    }
    data = []
    for revision, record in pairs:
        if record.id in projected:
            data.append(_durable_link_view(store, record, revision))
            data[-1]["revision"] = revision
    return data, current_revision


def _task_declaration_record(store, contract_id: str):
    matches = []
    for _revision, record in store.scan_runtime_records(
        record_type="contract.declared"
    ):
        payload = record.payload.get("contract")
        if isinstance(payload, Mapping) and payload.get("id") == contract_id:
            matches.append(record)
    if len(matches) != 1:
        raise ValueError("task edit requires one canonical Contract declaration")
    return matches[0]


def _edit_publisher(store):
    return replace(
        ensure_local_plugin_publisher(store, _EDIT_PLUGIN_ID, _EDIT_CAPABILITIES),
        additional_effects=HANDOFF_EFFECTS,
    )


def _existing_edit_publisher(
    store,
    *,
    source,
    title: str | None,
    body: str | None,
    reason: str,
    idempotency_key: str,
    source_assignment_id: str | None,
    source_hold_ids: tuple[str, ...] | None,
) -> tuple[CandidatePublisher, str, tuple[str, ...]] | None:
    """Find an exact committed edit Candidate without provisioning an identity."""
    actor_id = f"plugin:{_EDIT_PLUGIN_ID}"
    key_id = f"plugin-{compute_content_hash(actor_id)[:16]}"
    try:
        genesis = load_genesis(store)
        actor_key = next(
            item
            for item in load_effective_actor_keys(store, genesis)
            if item.actor_id == actor_id and item.key_id == key_id
        )
        signer = load_actor_signer(actor_key_path().parent / f"{key_id}.ed25519")
    except (LookupError, StopIteration, OSError, ValueError):
        return None
    if actor_key.revoked or not set(_EDIT_CAPABILITIES).issubset(
        actor_key.capabilities
    ):
        return None
    if signer.kind != actor_key.kind or signer.signer_id != actor_key.public_key:
        return None

    source_declaration = _task_declaration_record(store, source.id)
    context_records = tuple(
        record for _revision, record in store.scan_runtime_records()
    )
    links = project_task_handoffs(context_records, source_contract_id=source.id)
    for link in links:
        edit_meta = link.payload.get("edit")
        if not isinstance(edit_meta, Mapping):
            continue
        changed = tuple(edit_meta.get("changed_fields", ()))
        if changed != tuple(
            field
            for field in ("title", "body")
            if (title if field == "title" else body) is not None
        ):
            continue
        if (
            source_assignment_id is not None
            and edit_meta.get("source_assignment_id") != source_assignment_id
        ):
            continue
        linked_holds = tuple(edit_meta.get("source_hold_ids", ()))
        if source_hold_ids is not None and linked_holds != source_hold_ids:
            continue
        successor = store.get_contract(str(link.payload.get("successor_id", "")))
        if successor is None:
            continue
        if title is not None and successor.name != title:
            continue
        if body is not None and successor.description != body:
            continue
        exact_assignment = str(edit_meta.get("source_assignment_id", ""))
        exact_holds = tuple(linked_holds)
        edit_payload: dict[str, object] = {
            "source_assignment_id": exact_assignment,
            "source_hold_ids": list(exact_holds),
        }
        if title is not None:
            edit_payload["title"] = title
        if body is not None:
            edit_payload["body"] = body
        effect = task_handoff_effect(source.id, reason, edit=edit_payload)
        parents = (
            source.id,
            source_declaration.id,
            *((exact_assignment,) if exact_assignment else ()),
            *exact_holds,
        )
        proposal = create_candidate_proposal(
            domain_id=genesis.id,
            actor_id=actor_id,
            key_id=key_id,
            effects=(effect,),
            signer=signer,
            causal_parents=parents,
            idempotency_key=idempotency_key,
        )
        if proposal.id != link.payload.get("candidate_id"):
            continue
        receipts = [
            record
            for record in context_records
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == proposal.id
        ]
        heads = [
            record
            for record in context_records
            if record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == proposal.id
        ]
        if len(receipts) != 1 or len(heads) != 1:
            continue
        receipt, head = receipts[0], heads[0]
        member_ids = tuple(head.payload.get("committed_record_ids", ()))
        if (
            receipt.causal_parents != parents
            or tuple(receipt.payload.get("effect_types", ())) != ("task.handoff",)
            or link.id not in member_ids
            or head.causal_parents != (receipt.id, *member_ids)
            or len(member_ids) != len(set(member_ids))
            or any(
                not any(item.id == record_id for item in context_records)
                for record_id in member_ids
            )
        ):
            continue
        try:
            effective_keys = tuple(load_effective_actor_keys(store, genesis))
            verify_candidate_proposal(proposal, genesis, actor_keys=effective_keys)
            rebuilt_receipt = candidate_received_record(
                proposal, genesis, actor_keys=effective_keys
            )
        except (TypeError, ValueError):
            continue
        if rebuilt_receipt.id != receipt.id or runtime_record_effective_payload(
            rebuilt_receipt
        ) != runtime_record_effective_payload(receipt):
            continue
        return (
            CandidatePublisher(
                store,
                store,
                genesis,
                actor_key,
                signer,
                additional_effects=HANDOFF_EFFECTS,
            ),
            exact_assignment,
            exact_holds,
        )
    return None


def validate_task_edit(
    store,
    contract_id: str,
    reason: str,
    *,
    title: str | None = None,
    body: str | None = None,
    source_assignment_id: str | None = None,
    source_hold_ids: Sequence[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, object]:
    """Validate edit shape and fresh source references before identity setup."""
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be non-empty text")
    _validate_text(reason, "reason")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise ValueError("idempotency_key must be non-blank text")
    source = store.get_contract(contract_id)
    if source is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    edit: dict[str, object] = {}
    if title is not None:
        edit["title"] = title
    if body is not None:
        edit["body"] = body
    if source_assignment_id is not None:
        edit["source_assignment_id"] = source_assignment_id
    if source_hold_ids is not None:
        edit["source_hold_ids"] = list(source_hold_ids)
    # Parse and normalize the exact signed shape. For keyed replay, historical
    # snapshot references are recovered from the durable link and authenticated
    # below before skipping current-state equality.
    if source_assignment_id is None or source_hold_ids is None:
        exact_replay = None
        if idempotency_key is not None:
            exact_replay = _existing_edit_publisher(
                store,
                source=source,
                title=title,
                body=body,
                reason=reason,
                idempotency_key=idempotency_key,
                source_assignment_id=source_assignment_id,
                source_hold_ids=(
                    tuple(source_hold_ids) if source_hold_ids is not None else None
                ),
            )
        if exact_replay is not None:
            _publisher, exact_assignment, exact_holds = exact_replay
            edit["source_assignment_id"] = exact_assignment
            edit["source_hold_ids"] = list(exact_holds)
    if "source_assignment_id" not in edit or "source_hold_ids" not in edit:
        context = load_effect_projection_context(store)
        assignment_id, hold_ids = task_edit_snapshot(source, context)
        if source_assignment_id is not None:
            assignment_id = source_assignment_id
        if source_hold_ids is not None:
            hold_ids = tuple(source_hold_ids)
        edit["source_assignment_id"] = assignment_id
        edit["source_hold_ids"] = list(hold_ids)
    intent = validate_task_edit_request(edit, source)
    # Exact committed replay is detected again at publish time. Here the same
    # proof permits old snapshot references; all fresh requests compare current.
    replay = False
    if idempotency_key is not None:
        replay = (
            _existing_edit_publisher(
                store,
                source=source,
                title=title,
                body=body,
                reason=reason,
                idempotency_key=idempotency_key,
                source_assignment_id=intent.source_assignment_id,
                source_hold_ids=intent.source_hold_ids,
            )
            is not None
        )
    if not replay:
        context = load_effect_projection_context(store)
        actual_assignment, actual_holds = task_edit_snapshot(source, context)
        validate_task_edit_snapshot(
            intent,
            source_assignment_id=actual_assignment,
            source_hold_ids=actual_holds,
        )
    return edit


def publish_task_edit(
    store,
    contract_id: str,
    reason: str,
    *,
    title: str | None = None,
    body: str | None = None,
    source_assignment_id: str | None = None,
    source_hold_ids: Sequence[str] | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Publish a bounded edit as a handoff successor."""
    require_redis()
    key = idempotency_key or f"task-edit:{uuid.uuid4().hex}"
    edit = validate_task_edit(
        store,
        contract_id,
        reason,
        title=title,
        body=body,
        source_assignment_id=source_assignment_id,
        source_hold_ids=source_hold_ids,
        idempotency_key=key,
    )
    source = store.get_contract(contract_id)
    if source is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    declaration = _task_declaration_record(store, contract_id)
    parents = (
        contract_id,
        declaration.id,
        *((str(edit["source_assignment_id"]),) if edit["source_assignment_id"] else ()),
        *tuple(edit["source_hold_ids"]),
    )
    effect = task_handoff_effect(contract_id, reason, edit=edit)
    publisher = None
    if idempotency_key is not None:
        replay = _existing_edit_publisher(
            store,
            source=source,
            title=title,
            body=body,
            reason=reason,
            idempotency_key=key,
            source_assignment_id=str(edit["source_assignment_id"]),
            source_hold_ids=tuple(edit["source_hold_ids"]),
        )
        if replay is not None:
            publisher = replay[0]
    if publisher is None:
        publisher = _edit_publisher(store)
    sync_runtime(store)
    try:
        decision = publisher.publish(
            (effect,), idempotency_key=key, causal_parents=parents
        )
    except CandidateCommitRejected:
        sync_runtime(store, accepted=False)
        raise
    if not decision.accepted:
        sync_runtime(store, accepted=False)
        rejected = next(
            (
                item
                for item in decision.runtime_records
                if item.record_type == "candidate.rejected"
            ),
            None,
        )
        raise ValueError(
            str(rejected.payload.get("reason", "Candidate was rejected"))
            if rejected is not None
            else "Candidate was not accepted"
        )
    proposed = next(
        (
            item
            for item in decision.runtime_records
            if item.record_type == "task.handed_off"
            and item.payload.get("contract_id") == contract_id
            and item.payload.get("edit") is not None
        ),
        None,
    )
    data = _durable_link_view(store, proposed)
    from aigineering.task_views import project_task_status
    from aigineering.core.claim_assignments import assignment_context_from_projection

    data["source_status"] = project_task_status(source, store)["status"]
    successor_id = str(data["successor_id"])
    successor = store.get_contract(successor_id)
    if successor is None:
        raise RuntimeError("accepted edit successor is missing")
    assignment = assignment_head(
        assignment_context_from_projection(load_effect_projection_context(store)),
        contract_id=successor_id,
    )
    data["successor_assignment_id"] = assignment.id if assignment else ""
    data["idempotency_key"] = key
    data["source_assignment_id"] = str(edit["source_assignment_id"])
    data["source_hold_ids"] = list(edit["source_hold_ids"])
    try:
        sync_runtime(store, accepted=True)
    except RedisSyncPending as exc:
        exc.result = data
        raise
    return data, store.get_runtime_revision()


def task_edit_records(store, *, contract_id: str) -> tuple[list[dict], int]:
    """List edit links through the shared immutable handoff history owner."""
    rows, revision = task_handoff_records(store, contract_id=contract_id)
    edits = [item for item in rows if isinstance(item.get("edit"), Mapping)]
    for item in edits:
        item["source_assignment_id"] = str(item["edit"].get("source_assignment_id", ""))
        item["source_hold_ids"] = list(item["edit"].get("source_hold_ids", ()))
    return edits, revision
