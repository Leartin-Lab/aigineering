"""Application services for signed, task-scoped collaboration comments."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace

from aigineering.application import require_redis
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.domain import load_genesis
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.core.ids import compute_content_hash
from aigineering.local_identity import ensure_local_plugin_publisher
from aigineering.protocol.effect_builders import (
    task_block_effect,
    task_comment_effect,
    task_unblock_effect,
    task_promote_effect,
)
from aigineering.protocol.immutability import deep_thaw
from aigineering.plugins.collaboration import (
    COLLABORATION_EFFECTS,
    project_task_holds,
    project_task_comments,
    validate_task_promote_request,
)
from aigineering.runtime_services import RedisSyncPending, sync_runtime
from aigineering.protocol.candidate import (
    CandidateProposal,
    candidate_content_id,
    candidate_received_record,
    verify_candidate_proposal,
)
from aigineering.protocol.runtime_record import runtime_record_effective_payload
from aigineering.local_publication import (
    verify_durable_candidate_member,
    verify_durable_rejection_member,
)
from aigineering.plugins.collaboration import project_task_promote_effect

_COMMENT_PLUGIN_ID = "task.collaboration.operator.v1"
_COMMENT_CAPABILITIES = ("task.comment",)
_HOLDS_PLUGIN_ID = "task.collaboration.holds.operator.v1"
_HOLDS_CAPABILITIES = ("task.block", "task.unblock")


def validate_comment_text(text: str) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("comment text must be a non-empty string")
    try:
        size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise ValueError("comment text must be valid UTF-8") from exc
    if size > 64 * 1024:
        raise ValueError("comment text exceeds 65536 UTF-8 bytes")


def task_comment_records(store, *, contract_id: str | None = None):
    """Read projected comments with their immutable Store revisions."""
    require_redis()
    from aigineering.application import collaboration_service

    collaboration_service(store).sync()
    pairs = store.scan_runtime_records(record_type="task.commented")
    records = project_task_comments(
        (record for _revision, record in pairs), contract_id=contract_id
    )
    revisions = {record.id: revision for revision, record in pairs}
    current_revision = store.get_runtime_revision()
    data = [
        {
            "record_id": record.id,
            "revision": revisions[record.id],
            "recorded_at": record.recorded_at,
            "contract_id": str(record.payload["contract_id"]),
            "text": str(record.payload["text"]),
            "reply_to": str(record.payload.get("reply_to", "")),
            "actor_id": str(record.payload["actor_id"]),
            "key_id": str(record.payload["key_id"]),
            "candidate_id": str(record.payload["candidate_id"]),
        }
        for record in records
    ]
    return data, current_revision


def validate_comment_target(store, contract_id: str, *, reply_to: str = "") -> None:
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be a non-empty string")
    if not isinstance(reply_to, str):
        raise ValueError("reply_to must be a string")
    if reply_to and not reply_to.strip():
        raise ValueError("reply_to must be a non-empty record ID when supplied")
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    if reply_to:
        records = project_task_comments(
            (record for _revision, record in store.scan_runtime_records()),
            contract_id=contract_id,
        )
        if not any(record.id == reply_to for record in records):
            raise LookupError(
                "reply_to must reference a committed comment on the same Contract"
            )


def _validate_idempotency_key(idempotency_key: str | None) -> None:
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise ValueError("idempotency_key must be a non-blank string")
    if idempotency_key is not None:
        try:
            idempotency_key.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("idempotency_key must be valid UTF-8") from exc


def _validate_hold_target(store, contract_id: str) -> None:
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("contract_id must be a non-empty string")
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")


def validate_task_block(store, contract_id: str, reason: str) -> None:
    validate_comment_text(reason)
    _validate_hold_target(store, contract_id)


def validate_task_unblock(store, contract_id: str, hold_id: str, reason: str) -> None:
    validate_comment_text(reason)
    _validate_hold_target(store, contract_id)
    if not isinstance(hold_id, str) or not hold_id.strip():
        raise ValueError("hold_id must be a non-empty string")
    placed = store.get_runtime_record(hold_id)
    if (
        placed is None
        or placed.record_type != "claim.hold.placed"
        or placed.payload.get("contract_id") != contract_id
    ):
        raise LookupError("hold ID must identify a hold placement for this Contract")


def _comment_actor_publisher(store):
    return replace(
        ensure_local_plugin_publisher(store, _COMMENT_PLUGIN_ID, _COMMENT_CAPABILITIES),
        additional_effects=COLLABORATION_EFFECTS,
    )


def _holds_actor_publisher(store):
    return replace(
        ensure_local_plugin_publisher(store, _HOLDS_PLUGIN_ID, _HOLDS_CAPABILITIES),
        additional_effects=COLLABORATION_EFFECTS,
    )


def _hold_declaration_record(store, contract_id: str):
    matches = []
    for _revision, record in store.scan_runtime_records(
        record_type="contract.declared"
    ):
        payload = record.payload.get("contract")
        if isinstance(payload, Mapping) and payload.get("id") == contract_id:
            matches.append(record)
    if len(matches) != 1:
        raise ValueError("task promotion requires one canonical Contract declaration")
    return matches[0]


def _existing_task_promote_publisher(
    store, contract_id: str, hold_ids: tuple[str, ...], reason: str, key: str
):
    """Load an already-authorized publisher only for a proven exact replay."""
    actor_id = f"plugin:{_HOLDS_PLUGIN_ID}"
    key_id = f"plugin-{compute_content_hash(actor_id)[:16]}"
    genesis = load_genesis(store)
    effective_keys = tuple(load_effective_actor_keys(store, genesis))
    actor_key = next(
        (
            item
            for item in effective_keys
            if item.actor_id == actor_id and item.key_id == key_id
        ),
        None,
    )
    if (
        actor_key is None
        or actor_key.revoked
        or not set(_HOLDS_CAPABILITIES).issubset(actor_key.capabilities)
    ):
        return None
    try:
        from aigineering.local_identity import actor_key_path, load_actor_signer

        signer = load_actor_signer(actor_key_path().parent / f"{key_id}.ed25519")
    except (OSError, ValueError):
        return None
    if signer.kind != actor_key.kind or signer.signer_id != actor_key.public_key:
        return None

    effect = task_promote_effect(contract_id, hold_ids, reason)
    declaration = _hold_declaration_record(store, contract_id)
    parents = (declaration.id, *hold_ids)
    context = load_effect_projection_context(store)
    records = tuple(record for _revision, record in store.scan_runtime_records())
    matches = []
    for audit in records:
        if (
            audit.record_type != "task.promoted"
            or audit.payload.get("contract_id") != contract_id
            or audit.payload.get("reason") != reason
            or tuple(audit.payload.get("hold_ids", ())) != hold_ids
        ):
            continue
        candidate_id = str(audit.payload.get("candidate_id", ""))
        receipts = [
            record
            for record in records
            if record.record_type == "candidate.received"
            and record.payload.get("candidate_id") == candidate_id
            and record.payload.get("actor_id") == actor_id
            and record.payload.get("key_id") == key_id
            and tuple(record.payload.get("effect_types", ())) == ("task.promote",)
        ]
        heads = [
            record
            for record in records
            if record.record_type == "candidate.committed"
            and record.payload.get("candidate_id") == candidate_id
        ]
        if len(receipts) != 1 or len(heads) != 1:
            continue
        receipt = receipts[0]
        proposal = CandidateProposal(
            id=candidate_id,
            domain_id=genesis.id,
            actor_id=actor_id,
            key_id=key_id,
            signature_kind=str(receipt.payload.get("signature_kind", "")),
            signature=str(receipt.payload.get("signature", "")),
            effects=(effect,),
            causal_parents=parents,
            idempotency_key=key,
        )
        if candidate_content_id(proposal) != candidate_id:
            continue
        try:
            verify_candidate_proposal(proposal, genesis, actor_keys=effective_keys)
            rebuilt_receipt = candidate_received_record(
                proposal, genesis, actor_keys=effective_keys
            )
        except (TypeError, ValueError):
            continue
        if rebuilt_receipt.id != receipt.id or receipt.causal_parents != parents:
            continue
        if runtime_record_effective_payload(
            receipt
        ) != runtime_record_effective_payload(rebuilt_receipt):
            continue
        try:
            projection = project_task_promote_effect(
                effect, proposal, receipt.id, context
            )
        except ValueError:
            continue
        projected_ids = tuple(record.id for record in projection.records)
        head = heads[0]
        if (
            tuple(head.payload.get("committed_record_ids", ())) != projected_ids
            or head.causal_parents != (receipt.id, *projected_ids)
            or audit.id not in projected_ids
        ):
            continue
        matches.append((proposal, receipt, projection))
    if len(matches) != 1:
        return None
    return CandidatePublisher(
        store,
        store,
        genesis,
        actor_key,
        signer,
        additional_effects=COLLABORATION_EFFECTS,
    )


def _persisted_record_view(store, proposed, record_type: str) -> tuple[dict, int]:
    if proposed is None:
        raise RuntimeError(f"accepted Candidate produced no {record_type} record")
    durable = store.get_runtime_record(proposed.id)
    if durable is None:
        raise RuntimeError(f"accepted {record_type} is missing from the durable Store")
    revision = next(
        (
            item_revision
            for item_revision, item in store.scan_runtime_records(
                record_type=record_type
            )
            if item.id == durable.id
        ),
        None,
    )
    if revision is None:
        raise RuntimeError(f"durable {record_type} has no Store revision")
    return _record_view(durable, revision), store.get_runtime_revision()


def _candidate_rejection_data(store, decision, contract_id: str, key: str) -> dict:
    if decision is None or decision.accepted:
        raise RuntimeError("rejection result has no rejected Candidate decision")
    rejection = verify_durable_rejection_member(store, decision)
    data = {
        "contract_id": contract_id,
        "accepted": False,
        "idempotency_key": key,
        "fact_ids": [rejection.id],
        "rejection_record_id": rejection.id,
        "rejection_reason": str(rejection.payload.get("reason", "Candidate rejected")),
    }
    if rejection.record_type == "candidate.authentication_rejected":
        data["claimed_candidate_id"] = str(
            rejection.payload.get("claimed_candidate_id", "")
        )
    else:
        data["candidate_id"] = decision.candidate_id
    return data


def _sync_rejection(store, decision, contract_id: str, key: str) -> None:
    try:
        sync_runtime(store, accepted=False)
    except RedisSyncPending as exc:
        exc.decision = decision
        exc.data = _candidate_rejection_data(store, decision, contract_id, key)
        exc.revision = store.get_runtime_revision()
        raise


def _record_view(record, revision: int) -> dict:
    data = {
        "record_id": record.id,
        "record_type": record.record_type,
        "revision": revision,
        "recorded_at": record.recorded_at,
        **deep_thaw(record.payload),
    }
    if record.record_type == "claim.hold.placed":
        data["hold_id"] = record.id
    return data


def _publish_hold_effect(
    store,
    *,
    contract_id: str,
    effect,
    effect_type: str,
    record_type: str,
    idempotency_key: str | None,
    causal_parents: tuple[str, ...] = (),
) -> tuple[dict, int]:
    require_redis()
    _validate_idempotency_key(idempotency_key)
    key = idempotency_key or f"task-hold:{uuid.uuid4().hex}"
    publisher = _holds_actor_publisher(store)
    # The plugin authorization is a separate durable Candidate publication.
    sync_runtime(store)
    try:
        decision = publisher.publish(
            (effect,),
            idempotency_key=key,
            causal_parents=causal_parents,
        )
    except CandidateCommitRejected as exc:
        # BEGIN-guard races persist a rejection before raising. Relay that
        # durable outcome just as we do for a returned rejected decision.
        _sync_rejection(store, exc.decision, contract_id, key)
        raise
    if not decision.accepted:
        rejection = next(
            (
                record
                for record in decision.runtime_records
                if record.record_type.endswith("rejected")
            ),
            None,
        )
        reason = (
            str(rejection.payload.get("reason", "Candidate was rejected"))
            if rejection is not None
            else "Candidate was not accepted"
        )
        _sync_rejection(store, decision, contract_id, key)
        raise ValueError(reason)
    proposed_records = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == record_type
        and record.payload.get("contract_id") == contract_id
        and (
            effect_type != "task.unblock"
            or record.payload.get("hold_id") == effect.payload.get("hold_id")
        )
    )
    if len(proposed_records) != 1:
        raise RuntimeError(
            f"accepted Candidate produced no unique {record_type} record"
        )
    proposed = verify_durable_candidate_member(store, decision, proposed_records[0])
    data, revision = _persisted_record_view(store, proposed, record_type)
    data.update(
        {
            "candidate_id": decision.candidate_id,
            "idempotency_key": key,
            "fact_ids": [proposed.id],
        }
    )
    try:
        sync_runtime(store, accepted=True)
    except RedisSyncPending as exc:
        exc.decision = decision
        exc.data = data
        exc.revision = revision
        raise
    return data, revision


def publish_task_block(
    store,
    contract_id: str,
    reason: str,
    *,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Publish an authenticated hold placement for one task."""
    require_redis()
    validate_task_block(store, contract_id, reason)
    _validate_idempotency_key(idempotency_key)
    key = idempotency_key or f"task-hold:{uuid.uuid4().hex}"
    return _publish_hold_effect(
        store,
        contract_id=contract_id,
        effect=task_block_effect(contract_id, reason),
        effect_type="task.block",
        record_type="claim.hold.placed",
        idempotency_key=key,
    )


def publish_task_unblock(
    store,
    contract_id: str,
    hold_id: str,
    reason: str,
    *,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Publish a release Candidate for one exact hold placement."""
    require_redis()
    validate_task_unblock(store, contract_id, hold_id, reason)
    _validate_idempotency_key(idempotency_key)
    key = idempotency_key or f"task-hold:{uuid.uuid4().hex}"
    return _publish_hold_effect(
        store,
        contract_id=contract_id,
        effect=task_unblock_effect(contract_id, hold_id, reason),
        effect_type="task.unblock",
        record_type="claim.hold.released",
        idempotency_key=key,
        causal_parents=(hold_id,),
    )


def _promotion_item_view(store, audit, key: str, decision) -> tuple[dict, int]:
    audit = verify_durable_candidate_member(store, decision, audit)
    durable = store.get_runtime_record(audit.id)
    if durable is None or durable.record_type != "task.promoted":
        raise RuntimeError("accepted promotion audit is missing from the durable Store")
    revision = next(
        (
            item_revision
            for item_revision, item in store.scan_runtime_records(
                record_type="task.promoted"
            )
            if item.id == durable.id
        ),
        None,
    )
    if revision is None:
        raise RuntimeError("promotion audit has no Store revision")
    from aigineering.task_views import project_task_status

    contract = store.get_contract(str(durable.payload["contract_id"]))
    if contract is None:
        raise RuntimeError("promoted Contract is missing from the durable Store")
    state = project_task_status(contract, store)
    active_holds = project_task_holds(
        (record for _rev, record in store.scan_runtime_records()),
        contract_id=contract.id,
    )
    release_ids = tuple(str(item) for item in durable.payload["release_record_ids"])
    release_records = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == "claim.hold.released"
        and record.id in release_ids
        and record.payload.get("contract_id") == contract.id
        and record.payload.get("candidate_id") == decision.candidate_id
    )
    if len(release_records) != len(release_ids):
        raise RuntimeError("promotion release facts do not match its exact Candidate")
    for released in release_records:
        verify_durable_candidate_member(store, decision, released)
    data = _record_view(durable, revision)
    data.update(
        {
            "candidate_id": decision.candidate_id,
            "idempotency_key": key,
            "fact_ids": [*release_ids, durable.id],
            "status": state["status"],
            "blockers": deep_thaw(state["blockers"]),
            "remaining_hold_ids": [record.id for record in active_holds],
        }
    )
    return data, store.get_runtime_revision()


def publish_task_promote(
    store,
    contract_id: str,
    reason: str,
    *,
    hold_ids: tuple[str, ...] | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Release all current exact holds in one signed task.promote Candidate."""
    require_redis()
    validate_comment_text(reason)
    _validate_hold_target(store, contract_id)
    _validate_idempotency_key(idempotency_key)
    if hold_ids is not None:
        if not isinstance(hold_ids, (tuple, list)):
            raise ValueError("hold_ids must be a sequence of exact hold IDs")
        if any(not isinstance(item, str) or not item.strip() for item in hold_ids):
            raise ValueError("hold IDs must be non-empty strings")
        if len(hold_ids) != len(set(hold_ids)):
            raise ValueError("hold IDs must not contain duplicates")
        hold_ids = tuple(sorted(hold_ids))
        if not 1 <= len(hold_ids) <= 64:
            raise ValueError("promotion requires between 1 and 64 holds")

    context = load_effect_projection_context(store)
    declaration = _hold_declaration_record(store, contract_id)
    if declaration.id not in {record.id for record in context.runtime_records}:
        raise ValueError("Contract declaration is missing from projection context")
    if hold_ids is not None and idempotency_key is not None:
        replay_publisher = _existing_task_promote_publisher(
            store, contract_id, hold_ids, reason, idempotency_key
        )
        if replay_publisher is not None:
            key = idempotency_key
            effect = task_promote_effect(contract_id, hold_ids, reason)
            parents = (declaration.id, *hold_ids)
            sync_runtime(store)
            try:
                decision = replay_publisher.publish(
                    (effect,), idempotency_key=key, causal_parents=parents
                )
            except CandidateCommitRejected as exc:
                _sync_rejection(store, exc.decision, contract_id, key)
                raise
            if not decision.accepted:
                _sync_rejection(store, decision, contract_id, key)
                raise ValueError("exact promotion replay was rejected")
            audit = next(
                record
                for record in decision.runtime_records
                if record.record_type == "task.promoted"
            )
            data, revision = _promotion_item_view(store, audit, key, decision)
            try:
                sync_runtime(store, accepted=True)
            except RedisSyncPending as exc:
                exc.decision = decision
                exc.data = data
                exc.revision = revision
                raise
            return data, revision

    if hold_ids is None:
        open_holds = project_task_holds(
            context.runtime_records, contract_id=contract_id
        )
        hold_ids = tuple(sorted(record.id for record in open_holds))
    if not 1 <= len(hold_ids) <= 64:
        if len(hold_ids) > 64:
            raise ValueError(
                "promotion supports at most 64 holds; release some holds with "
                "task unblock, keeping at least one open, then promote the remainder"
            )
        raise ValueError("task has no open holds to promote")
    effect = task_promote_effect(contract_id, hold_ids, reason)
    validate_task_promote_request(effect, context)
    parents = (declaration.id, *hold_ids)
    key = idempotency_key or f"task-promote:{uuid.uuid4().hex}"

    publisher = _holds_actor_publisher(store)
    # Actor authorization is a separate durable publication; relay it before
    # attempting the signed promotion Candidate.
    sync_runtime(store)
    try:
        decision = publisher.publish(
            (effect,), idempotency_key=key, causal_parents=parents
        )
    except CandidateCommitRejected as exc:
        _sync_rejection(store, exc.decision, contract_id, key)
        raise
    if not decision.accepted:
        _sync_rejection(store, decision, contract_id, key)
        rejection = next(
            (
                record
                for record in decision.runtime_records
                if record.record_type == "candidate.rejected"
            ),
            None,
        )
        raise ValueError(
            str(rejection.payload.get("reason", "Candidate was rejected"))
            if rejection
            else "Candidate was rejected"
        )
    audit = next(
        (
            record
            for record in decision.runtime_records
            if record.record_type == "task.promoted"
        ),
        None,
    )
    if audit is None:
        raise RuntimeError("accepted task.promote produced no audit record")
    data, revision = _promotion_item_view(store, audit, key, decision)
    try:
        sync_runtime(store, accepted=True)
    except RedisSyncPending as exc:
        exc.decision = decision
        exc.data = data
        exc.revision = revision
        raise
    return data, revision


def task_promotion_records(store, *, contract_id: str) -> tuple[list[dict], int]:
    """Return immutable task.promoted audit history in durable order."""
    require_redis()
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    from aigineering.application import collaboration_service

    collaboration_service(store).sync()
    pairs = store.scan_runtime_records(record_type="task.promoted")
    records = [
        record
        for _revision, record in pairs
        if record.payload.get("contract_id") == contract_id
    ]
    revisions = {record.id: revision for revision, record in pairs}
    return [
        _record_view(record, revisions[record.id]) for record in records
    ], store.get_runtime_revision()


def task_hold_records(store, *, contract_id: str):
    """Return every hold placement, including active state and release record refs."""
    require_redis()
    from aigineering.application import collaboration_service

    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    collaboration_service(store).sync()
    pairs = store.scan_runtime_records()
    records = tuple(record for _revision, record in pairs)
    active_ids = {
        record.id for record in project_task_holds(records, contract_id=contract_id)
    }
    revisions = {record.id: revision for revision, record in pairs}
    releases: dict[str, list] = {}
    for _revision, record in pairs:
        if (
            record.record_type == "claim.hold.released"
            and record.payload.get("contract_id") == contract_id
        ):
            releases.setdefault(str(record.payload.get("hold_id", "")), []).append(
                record
            )
    data = []
    for _record_revision, record in pairs:
        if (
            record.record_type != "claim.hold.placed"
            or record.payload.get("contract_id") != contract_id
        ):
            continue
        linked_releases = releases.get(record.id, [])
        item = _record_view(record, revisions[record.id])
        item["active"] = record.id in active_ids
        item["release_refs"] = [
            {
                "record_id": released.id,
                "revision": revisions[released.id],
                "recorded_at": released.recorded_at,
                "reason": str(released.payload.get("reason", "")),
                "actor_id": str(released.payload.get("actor_id", "")),
                "key_id": str(released.payload.get("key_id", "")),
                "candidate_id": str(released.payload.get("candidate_id", "")),
            }
            for released in linked_releases
        ]
        data.append(item)
    return data, store.get_runtime_revision()


def publish_task_comment(
    store,
    contract_id: str,
    text: str,
    *,
    reply_to: str = "",
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    """Publish one signed comment and return its persisted projection and revision."""
    require_redis()
    validate_comment_text(text)
    validate_comment_target(store, contract_id, reply_to=reply_to)
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise ValueError("idempotency_key must be a non-blank string")

    publisher = replace(
        ensure_local_plugin_publisher(store, _COMMENT_PLUGIN_ID, _COMMENT_CAPABILITIES),
        additional_effects=COLLABORATION_EFFECTS,
    )
    # Actor authorization may have committed independently before the comment.
    sync_runtime(store)

    key = idempotency_key or f"task-comment:{uuid.uuid4().hex}"
    decision = publisher.publish(
        (task_comment_effect(contract_id, text, reply_to=reply_to),),
        idempotency_key=key,
        causal_parents=(reply_to,) if reply_to else (),
    )
    if not decision.accepted:
        sync_runtime(store, accepted=False)
        rejection = next(
            (
                record
                for record in decision.runtime_records
                if record.record_type.endswith("rejected")
            ),
            None,
        )
        reason = (
            str(rejection.payload.get("reason", "Candidate was rejected"))
            if rejection is not None
            else "Candidate was not accepted"
        )
        raise ValueError(reason)

    sync_runtime(store, accepted=True)
    proposed = next(
        (
            record
            for record in decision.runtime_records
            if record.record_type == "task.commented"
        ),
        None,
    )
    if proposed is None:
        raise RuntimeError("accepted task comment produced no comment record")
    durable = store.get_runtime_record(proposed.id)
    if durable is None:
        raise RuntimeError("accepted task comment is missing from the durable Store")
    revision_pairs = store.scan_runtime_records(record_type="task.commented")
    revision = next(
        (
            item_revision
            for item_revision, item in revision_pairs
            if item.id == durable.id
        ),
        None,
    )
    if revision is None:
        raise RuntimeError("durable task comment has no Store revision")
    data = {
        "record_id": durable.id,
        "revision": revision,
        "recorded_at": durable.recorded_at,
        "contract_id": str(durable.payload["contract_id"]),
        "text": str(durable.payload["text"]),
        "reply_to": str(durable.payload.get("reply_to", "")),
        "actor_id": str(durable.payload["actor_id"]),
        "key_id": str(durable.payload["key_id"]),
        "candidate_id": str(durable.payload["candidate_id"]),
    }
    return data, store.get_runtime_revision()
