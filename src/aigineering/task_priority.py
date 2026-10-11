"""Signed application services for advisory task priority facts."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace

from aigineering.application import collaboration_service, require_redis
from aigineering.core.actor_facts import load_effective_actor_keys
from aigineering.core.candidate_publisher import CandidatePublisher
from aigineering.core.commitment import CandidateCommitRejected
from aigineering.core.domain import load_genesis
from aigineering.core.ids import compute_content_hash
from aigineering.core.projection_context import load_effect_projection_context
from aigineering.local_identity import (
    actor_key_path,
    ensure_local_plugin_publisher,
    load_actor_signer,
)
from aigineering.local_publication import (
    verify_durable_candidate_member,
    verify_durable_rejection_member,
)
from aigineering.plugins.task_priority import (
    PRIORITY_EFFECT,
    PRIORITY_FACT,
    TASK_PRIORITY_EFFECTS,
    priority_declaration,
    priority_history,
    priority_histories,
    validate_task_priority_request,
)
from aigineering.protocol.candidate import (
    CandidateProposal,
    candidate_content_id,
    candidate_received_record,
    verify_candidate_proposal,
)
from aigineering.protocol.effect_builders import task_priority_effect
from aigineering.protocol.immutability import deep_thaw
from aigineering.runtime_services import RedisSyncPending, sync_runtime

_PLUGIN_ID = "task.priority.operator.v1"
_CAPABILITIES = ("task.priority.manage",)


class PriorityPublicationRejected(CandidateCommitRejected):
    """A durable priority rejection with its exact audit result and key."""

    def __init__(self, decision, data: dict, revision: int) -> None:
        self.data = data
        self.revision = revision
        reason = data.get("rejection_reason")
        if not isinstance(reason, str) or not reason:
            reason = "Priority Candidate was rejected"
        super().__init__(reason, decision)


def _validate_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    try:
        if len(value.encode("utf-8", errors="strict")) > 65536:
            raise ValueError(f"{name} exceeds 65536 UTF-8 bytes")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc


def _validate_priority(priority: int) -> None:
    if (
        isinstance(priority, bool)
        or not isinstance(priority, int)
        or not -100 <= priority <= 100
    ):
        raise ValueError("priority must be an integer from -100 through 100")


def _validate_previous(previous_priority_id: str | None) -> None:
    if previous_priority_id is not None and not isinstance(previous_priority_id, str):
        raise ValueError("previous_priority_id must be text when supplied")


def _require_materialized_priority_contract(context, contract_id: str) -> None:
    """Separate an absent target from an orphaned or ambiguous projection."""
    matches = [contract for contract in context.contracts if contract.id == contract_id]
    if not matches:
        has_selected_history = any(
            (
                record.record_type == "contract.declared"
                and isinstance(record.payload.get("contract"), Mapping)
                and record.payload["contract"].get("id") == contract_id
            )
            or (
                record.record_type == PRIORITY_FACT
                and record.payload.get("contract_id") == contract_id
            )
            for record in context.runtime_records
        )
        if has_selected_history:
            raise ValueError(
                f"Contract {contract_id!r} has selected history but no materialized Contract"
            )
        raise LookupError(f"unknown Contract {contract_id!r}")
    if len(matches) != 1:
        raise ValueError(
            f"Contract {contract_id!r} is ambiguous in the current projection"
        )


def validate_task_priority(
    store,
    contract_id: str,
    priority: int,
    reason: str,
    *,
    previous_priority_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    _validate_text(contract_id, "contract_id")
    _validate_text(reason, "reason")
    _validate_priority(priority)
    _validate_previous(previous_priority_id)
    if idempotency_key is not None:
        _validate_text(idempotency_key, "idempotency_key")
    context = load_effect_projection_context(store)
    _require_materialized_priority_contract(context, contract_id)
    declaration = priority_declaration(context, contract_id)
    history = priority_history(context, contract_id)
    previous = previous_priority_id or ""
    if idempotency_key is not None:
        effect = task_priority_effect(contract_id, priority, previous, reason)
        parents = (declaration.id, *((previous,) if previous else ()))
        if _exact_replay_publisher(store, effect, key=idempotency_key, parents=parents):
            return
    if history and previous_priority_id is None:
        raise ValueError("--previous must name the exact current priority head")
    validate_task_priority_request(context, contract_id, priority, previous)


def _identity() -> tuple[str, str, str]:
    actor_id = f"plugin:{_PLUGIN_ID}"
    key_id = f"plugin-{compute_content_hash(actor_id)[:16]}"
    return actor_id, key_id, str(actor_key_path().parent / f"{key_id}.ed25519")


def _exact_replay_publisher(
    store,
    effect,
    *,
    key: str,
    parents: tuple[str, ...],
):
    """Return the existing authorized publisher only for a signed exact replay."""
    actor_id, key_id, key_path = _identity()
    try:
        genesis = load_genesis(store)
        actor_key = next(
            item
            for item in load_effective_actor_keys(store, genesis)
            if item.actor_id == actor_id and item.key_id == key_id
        )
        signer = load_actor_signer(actor_key_path().parent / f"{key_id}.ed25519")
    except (LookupError, StopIteration, ValueError, OSError):
        return None
    if actor_key.revoked or not set(_CAPABILITIES).issubset(actor_key.capabilities):
        return None
    if signer.kind != actor_key.kind or signer.signer_id != actor_key.public_key:
        return None

    facts = [
        record
        for _revision, record in store.scan_runtime_records(record_type=PRIORITY_FACT)
        if record.payload.get("actor_id") == actor_id
        and record.payload.get("key_id") == key_id
        and record.payload.get("contract_id") == effect.payload["contract_id"]
        and record.payload.get("priority") == effect.payload["priority"]
        and record.payload.get("previous_priority_id")
        == effect.payload["previous_priority_id"]
        and record.payload.get("reason") == effect.payload["reason"]
    ]
    for fact in facts:
        candidate_id = str(fact.payload.get("candidate_id", ""))
        receipts = [
            record
            for _revision, record in store.scan_runtime_records(
                record_type="candidate.received"
            )
            if record.payload.get("candidate_id") == candidate_id
            and record.payload.get("actor_id") == actor_id
            and record.payload.get("key_id") == key_id
            and tuple(record.payload.get("effect_types", ())) == (PRIORITY_EFFECT,)
        ]
        heads = [
            record
            for _revision, record in store.scan_runtime_records(
                record_type="candidate.committed"
            )
            if record.payload.get("candidate_id") == candidate_id
        ]
        if len(receipts) != 1 or len(heads) != 1:
            continue
        receipt, committed = receipts[0], heads[0]
        if tuple(receipt.causal_parents) != parents or fact.causal_parents != (
            receipt.id,
            *parents,
        ):
            continue
        if tuple(committed.payload.get("committed_record_ids", ())) != (fact.id,):
            continue
        if committed.causal_parents != (receipt.id, fact.id):
            continue
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
            verify_candidate_proposal(
                proposal,
                genesis,
                actor_keys=tuple(load_effective_actor_keys(store, genesis)),
            )
            rebuilt_receipt = candidate_received_record(
                proposal,
                genesis,
                actor_keys=tuple(load_effective_actor_keys(store, genesis)),
            )
        except (ValueError, StopIteration):
            continue
        if rebuilt_receipt.id != receipt.id or fact.id not in committed.payload.get(
            "committed_record_ids", ()
        ):
            continue
        return CandidatePublisher(
            store,
            store,
            genesis,
            actor_key,
            signer,
            additional_effects=TASK_PRIORITY_EFFECTS,
        )
    return None


def _publisher(store):
    return replace(
        ensure_local_plugin_publisher(store, _PLUGIN_ID, _CAPABILITIES),
        additional_effects=TASK_PRIORITY_EFFECTS,
    )


def _record_view(store, record):
    durable = store.get_runtime_record(record.id)
    if durable is None or durable.record_type != PRIORITY_FACT:
        raise RuntimeError("priority fact is missing from the durable Store")
    revision = next(
        (
            rev
            for rev, item in store.scan_runtime_records(record_type=PRIORITY_FACT)
            if item.id == durable.id
        ),
        None,
    )
    if revision is None:
        raise RuntimeError("priority fact has no Store revision")
    return {
        "record_id": durable.id,
        "record_type": durable.record_type,
        "revision": revision,
        "recorded_at": durable.recorded_at,
        **deep_thaw(durable.payload),
    }


def _publish(store, effect, *, key: str, parents: tuple[str, ...], publisher):
    sync_runtime(store)
    try:
        decision = publisher.publish(
            (effect,), idempotency_key=key, causal_parents=parents
        )
    except CandidateCommitRejected as exc:
        try:
            sync_runtime(store, accepted=False)
        except RedisSyncPending as pending:
            _attach_rejection_pending(store, exc.decision, pending, key)
            raise
        data, revision = _rejection_result(store, exc.decision, key)
        raise PriorityPublicationRejected(exc.decision, data, revision) from exc
    if not decision.accepted:
        try:
            sync_runtime(store, accepted=False)
        except RedisSyncPending as pending:
            _attach_rejection_pending(store, decision, pending, key)
            raise
        data, revision = _rejection_result(store, decision, key)
        raise PriorityPublicationRejected(decision, data, revision)
    matches = tuple(
        record
        for record in decision.runtime_records
        if record.record_type == PRIORITY_FACT
        and record.payload.get("candidate_id") == decision.candidate_id
        and record.payload.get("actor_id") == f"plugin:{_PLUGIN_ID}"
        and record.payload.get("contract_id") == effect.payload["contract_id"]
        and record.payload.get("priority") == effect.payload["priority"]
        and record.payload.get("previous_priority_id")
        == effect.payload["previous_priority_id"]
        and record.payload.get("reason") == effect.payload["reason"]
    )
    if len(matches) != 1:
        raise RuntimeError("accepted priority Candidate has no unique exact fact")
    fact = verify_durable_candidate_member(store, decision, matches[0])
    item = _record_view(store, fact)
    if item.get("candidate_id") != decision.candidate_id:
        raise RuntimeError("durable priority fact has a mismatched Candidate ID")
    item["idempotency_key"] = key
    item["fact_ids"] = [fact.id]
    revision = store.get_runtime_revision()
    try:
        sync_runtime(store, accepted=True)
    except RedisSyncPending as pending:
        pending.accepted = True
        pending.decision = decision
        pending.data = item
        pending.revision = revision
        raise
    return item, revision


def _rejection_result(store, decision, key: str) -> tuple[dict, int]:
    rejection = verify_durable_rejection_member(store, decision)
    reason = rejection.payload.get("reason")
    if not isinstance(reason, str) or not reason:
        reason = "Priority Candidate was rejected"
    return (
        {
            "rejection_fact_id": rejection.id,
            "fact_ids": [rejection.id],
            "idempotency_key": key,
            "rejection_reason": reason,
        },
        store.get_runtime_revision(),
    )


def _attach_rejection_pending(
    store, decision, pending: RedisSyncPending, key: str
) -> None:
    data, revision = _rejection_result(store, decision, key)
    pending.accepted = False
    pending.decision = decision
    pending.data = data
    pending.revision = revision


def publish_task_priority(
    store,
    contract_id: str,
    priority: int,
    reason: str,
    *,
    previous_priority_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[dict, int]:
    require_redis()
    _validate_text(contract_id, "contract_id")
    _validate_text(reason, "reason")
    _validate_priority(priority)
    _validate_previous(previous_priority_id)
    if idempotency_key is not None:
        _validate_text(idempotency_key, "idempotency_key")
    previous = previous_priority_id or ""
    effect = task_priority_effect(contract_id, priority, previous, reason)
    key = idempotency_key or f"task-priority:{uuid.uuid4().hex}"
    context = load_effect_projection_context(store)
    _require_materialized_priority_contract(context, contract_id)
    priority_history(context, contract_id)
    declaration = priority_declaration(context, contract_id)
    parents = (declaration.id, *((previous,) if previous else ()))

    # Only an explicitly keyed, fully authenticated receipt can bypass mutable
    # head/terminal checks. No identity or authorization is provisioned here.
    if idempotency_key is not None:
        publisher = _exact_replay_publisher(store, effect, key=key, parents=parents)
        if publisher is not None:
            return _publish(
                store, effect, key=key, parents=parents, publisher=publisher
            )

    validate_task_priority(
        store,
        contract_id,
        priority,
        reason,
        previous_priority_id=previous_priority_id,
        idempotency_key=idempotency_key,
    )
    return _publish(
        store,
        effect,
        key=key,
        parents=parents,
        publisher=_publisher(store),
    )


def task_priority_records(store, *, contract_id: str) -> tuple[dict, int]:
    require_redis()
    _validate_text(contract_id, "contract_id")
    if store.get_contract(contract_id) is None:
        raise LookupError(f"unknown Contract {contract_id!r}")
    collaboration_service(store).sync()
    context = load_effect_projection_context(store)
    has_history = any(
        record.record_type == PRIORITY_FACT
        and record.payload.get("contract_id") == contract_id
        for record in context.runtime_records
    )
    records = priority_history(context, contract_id) if has_history else ()
    history = [_record_view(store, record) for record in records]
    # The revision in this envelope is the snapshot revision, while each row
    # retains its own immutable Store revision.
    return {
        "contract_id": contract_id,
        "priority": int(records[-1].payload["priority"]) if records else 0,
        "head_priority_id": records[-1].id if records else None,
        "history": history,
    }, store.get_runtime_revision()


def order_task_contract_ids(store, contract_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Order an existing eligible ID set by advisory priority, stably.

    This read-only helper intentionally does not query Redis or change the set
    of IDs supplied by the caller. Equal values retain declaration order, then
    use the full Contract ID as a deterministic final key.
    """
    if not isinstance(contract_ids, tuple) or any(
        not isinstance(item, str) or not item.strip() for item in contract_ids
    ):
        raise ValueError("contract_ids must be a tuple of non-empty full IDs")
    if len(contract_ids) != len(set(contract_ids)):
        raise ValueError("contract_ids must not contain duplicates")
    context = load_effect_projection_context(store)
    contracts = {item.id: item for item in context.contracts}
    declarations: dict[str, int] = {}
    for index, record in enumerate(context.runtime_records):
        if record.record_type != "contract.declared":
            continue
        payload = record.payload.get("contract")
        if isinstance(payload, Mapping):
            contract_id = payload.get("id")
            if isinstance(contract_id, str):
                declarations[contract_id] = index
    for contract_id in contract_ids:
        if contract_id not in contracts:
            raise LookupError(f"unknown Contract {contract_id!r}")
    histories = priority_histories(context, contract_ids)
    priorities: dict[str, int] = {}
    for contract_id in contract_ids:
        history = histories[contract_id]
        priorities[contract_id] = int(history[-1].payload["priority"]) if history else 0
        if contract_id not in declarations:
            raise ValueError(f"Contract {contract_id!r} has no declaration fact")
    return tuple(
        sorted(
            contract_ids,
            key=lambda contract_id: (
                -priorities[contract_id],
                declarations[contract_id],
                contract_id,
            ),
        )
    )
