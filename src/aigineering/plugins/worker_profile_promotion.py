"""Narrow, auditable profile-only promotion for local Worker registrations."""

from __future__ import annotations

from collections.abc import Mapping

from aigineering.core.effect_projection import EffectHandler
from aigineering.core.effect_projectors import (
    EffectProjection,
    project_worker_registration,
)
from aigineering.core.ids import compute_content_hash
from aigineering.core.projection_context import EffectProjectionContext
from aigineering.core.worker_routing import (
    WorkerRegistration,
    registration_from_record,
    worker_registration_payload,
)
from aigineering.protocol.candidate import CandidateEffect, CandidateProposal
from aigineering.protocol.immutability import deep_thaw
from aigineering.protocol.runtime_record import RuntimeRecord, create_runtime_record
from aigineering.protocol.runtime_record import runtime_record_effective_payload


_REGISTRATION_FIELDS = frozenset(worker_registration_payload(WorkerRegistration("x")))
_EFFECT_FIELDS = frozenset({"previous_registration_id", "registration"})
_PROMOTED_FIELDS = frozenset(
    {
        "worker_id",
        "previous_registration_id",
        "registration_id",
        "previous_version",
        "version",
        "profile_id",
        "actor_id",
        "key_id",
        "candidate_id",
    }
)


def _same_runtime_fact(left: RuntimeRecord, right: RuntimeRecord) -> bool:
    """Compare durable identity and content while ignoring local timestamps."""
    return runtime_record_effective_payload(left) == runtime_record_effective_payload(
        right
    )


def _registration_from_payload(value: object) -> WorkerRegistration:
    if not isinstance(value, Mapping) or set(value) != _REGISTRATION_FIELDS:
        raise ValueError("worker.profile.promote registration has invalid fields")
    data = deep_thaw(value)
    for field in ("worker_id", "profile_id", "version", "actor_id", "key_id"):
        if not isinstance(data[field], str):
            raise ValueError(
                f"worker.profile.promote registration {field} must be text"
            )
    for field in ("capabilities", "pools"):
        values = data[field]
        if not isinstance(values, list) or any(
            not isinstance(item, str) for item in values
        ):
            raise ValueError(
                f"worker.profile.promote registration {field} must be text list"
            )
    if type(data["capacity"]) is not int or type(data["enabled"]) is not bool:
        raise ValueError(
            "worker.profile.promote registration has invalid routing values"
        )
    registration = WorkerRegistration(
        worker_id=data["worker_id"],
        capabilities=tuple(data["capabilities"]),
        pools=tuple(data["pools"]),
        profile_id=data["profile_id"],
        capacity=data["capacity"],
        enabled=data["enabled"],
        version=data["version"],
        actor_id=data["actor_id"],
        key_id=data["key_id"],
    )
    if worker_registration_payload(registration) != data:
        raise ValueError("worker.profile.promote registration is not canonical")
    return registration


def _paired_registration(
    effect: CandidateEffect, candidate: CandidateProposal
) -> tuple[str, WorkerRegistration]:
    if len(candidate.effects) != 2 or candidate.effects[0] != effect:
        raise ValueError(
            "worker.profile.promote must be first in a two-effect Candidate"
        )
    if candidate.effects[1].effect_type != "worker.register":
        raise ValueError("worker.profile.promote must be followed by worker.register")
    if set(effect.payload) != _EFFECT_FIELDS:
        raise ValueError("worker.profile.promote has invalid payload fields")
    previous_id = effect.payload.get("previous_registration_id")
    if not isinstance(previous_id, str) or not previous_id:
        raise ValueError("worker.profile.promote requires a prior registration ID")
    registration = _registration_from_payload(effect.payload.get("registration"))
    paired_payload = deep_thaw(candidate.effects[1].payload)
    if set(paired_payload) != {"registration"}:
        raise ValueError("paired worker.register has invalid payload fields")
    paired_registration = _registration_from_payload(paired_payload["registration"])
    if paired_registration != registration:
        raise ValueError("paired worker.register does not match profile promotion")
    return previous_id, registration


def _promoted_fact(
    *,
    worker_id: str,
    previous_registration_id: str,
    registration_record: RuntimeRecord,
    previous_version: str,
    registration: WorkerRegistration,
    candidate: CandidateProposal,
    receipt_id: str,
) -> RuntimeRecord:
    payload = {
        "worker_id": worker_id,
        "previous_registration_id": previous_registration_id,
        "registration_id": registration_record.id,
        "previous_version": previous_version,
        "version": registration.version,
        "profile_id": registration.profile_id,
        "actor_id": registration.actor_id,
        "key_id": registration.key_id,
        "candidate_id": candidate.id,
    }
    if set(payload) != _PROMOTED_FIELDS:
        raise AssertionError("internal worker profile promotion payload mismatch")
    return create_runtime_record(
        "worker.profile.promoted",
        payload,
        causal_parents=(receipt_id, previous_registration_id, registration_record.id),
    )


def _exact_replay(
    *,
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
    previous_registration_id: str,
    registration: WorkerRegistration,
    registration_record: RuntimeRecord,
) -> RuntimeRecord | None:
    records = context.runtime_records
    heads = [
        record
        for record in records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate.id
    ]
    if not heads:
        return None
    if len(heads) != 1:
        raise ValueError("worker profile promotion replay has ambiguous commit heads")
    receipts = [
        record
        for record in records
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate.id
    ]
    if len(receipts) != 1 or receipts[0].id != receipt_id:
        raise ValueError("worker profile promotion replay has no exact receipt")
    receipt = receipts[0]
    if (
        receipt.payload.get("actor_id") != candidate.actor_id
        or receipt.payload.get("key_id") != candidate.key_id
        or tuple(receipt.payload.get("effect_types", ()))
        != ("worker.profile.promote", "worker.register")
    ):
        raise ValueError("worker profile promotion replay receipt identity changed")
    committed_ids = heads[0].payload.get("committed_record_ids")
    if (
        not isinstance(committed_ids, (tuple, list))
        or len(committed_ids) != len(set(committed_ids))
        or len(
            [
                record
                for record in records
                if record.record_type == "worker.profile.promoted"
                and record.payload.get("candidate_id") == candidate.id
            ]
        )
        != 1
    ):
        raise ValueError("worker profile promotion replay has invalid committed IDs")
    promoted = _promoted_fact(
        worker_id=registration.worker_id,
        previous_registration_id=previous_registration_id,
        registration_record=registration_record,
        previous_version=_registration_version_for(
            context, previous_registration_id, registration.worker_id
        ),
        registration=registration,
        candidate=candidate,
        receipt_id=receipt_id,
    )
    registered = [record for record in records if record.id == registration_record.id]
    promoted_records = [
        record
        for record in records
        if record.record_type == "worker.profile.promoted"
        and record.payload.get("candidate_id") == candidate.id
    ]
    if (
        len(registered) != 1
        or not _same_runtime_fact(registered[0], registration_record)
        or len(promoted_records) != 1
        or not _same_runtime_fact(promoted_records[0], promoted)
        or list(committed_ids) != [promoted.id, registration_record.id]
        or heads[0].causal_parents != (receipt_id, *tuple(committed_ids))
    ):
        raise ValueError("worker profile promotion replay facts do not match Candidate")
    return promoted_records[0]


def _registration_version_for(
    context: EffectProjectionContext, record_id: str, worker_id: str
) -> str:
    matches = [
        record
        for record in context.runtime_records
        if record.id == record_id
        and record.record_type == "worker.registered"
        and record.payload.get("worker_id") == worker_id
    ]
    if len(matches) != 1:
        raise ValueError("worker profile promotion prior registration fact is missing")
    return str(matches[0].payload.get("version", ""))


def validate_worker_profile_promotion(
    context: EffectProjectionContext,
    *,
    previous_registration_id: str,
    registration: WorkerRegistration,
) -> RuntimeRecord:
    """Validate the narrow local profile transition against one fact snapshot."""
    source_records = [
        record
        for record in context.runtime_records
        if record.record_type == "worker.registered"
        and record.payload.get("worker_id") == registration.worker_id
    ]
    if len(source_records) != 1 or source_records[0].id != previous_registration_id:
        raise ValueError("profile promotion requires the exact sole prior registration")
    source_record = source_records[0]
    source = registration_from_record(source_record)
    current = next(
        (
            item
            for item in context.registrations
            if item.worker_id == registration.worker_id
        ),
        None,
    )
    if current is None or worker_registration_payload(
        current
    ) != worker_registration_payload(source):
        raise ValueError(
            "profile promotion source is no longer the current registration"
        )
    if (
        not source.enabled
        or source.capabilities
        or source.pools
        or source.capacity != 1
    ):
        raise ValueError(
            "profile promotion source is not an enabled pull-only registration"
        )
    if not source.actor_id or source.actor_id != source.worker_id or not source.key_id:
        raise ValueError(
            "profile promotion requires the canonical local Worker identity"
        )
    expected_key_id = f"worker-{compute_content_hash(source.worker_id)[:16]}"
    if source.key_id != expected_key_id:
        raise ValueError("profile promotion cannot rebind a non-local Worker key")
    if current.active_claims or _has_active_claim_facts(
        context.runtime_records, source.worker_id
    ):
        raise ValueError(
            "profile promotion is blocked while the Worker has an active claim"
        )
    actor_key = next(
        (
            key
            for key in context.actor_keys
            if key.actor_id == source.actor_id and key.key_id == source.key_id
        ),
        None,
    )
    if (
        actor_key is None
        or actor_key.revoked
        or "worker.submit" not in actor_key.capabilities
    ):
        raise ValueError("profile promotion requires the current Worker signing key")
    if (
        not source.version.isdecimal()
        or str(int(source.version)) != source.version
        or int(source.version) < 1
    ):
        raise ValueError(
            "profile promotion requires a canonical positive registration version"
        )
    expected_version = str(int(source.version) + 1)
    if (
        registration.worker_id != source.worker_id
        or registration.actor_id != source.actor_id
        or registration.key_id != source.key_id
        or registration.capabilities != source.capabilities
        or registration.pools != source.pools
        or registration.capacity != source.capacity
        or registration.enabled != source.enabled
        or registration.profile_id == ""
        or source.profile_id != ""
        or registration.version != expected_version
    ):
        raise ValueError("profile promotion may change only profile and next version")
    return source_record


def _has_active_claim_facts(records: tuple[RuntimeRecord, ...], worker_id: str) -> bool:
    open_claims = {
        str(record.payload.get("claim_id", ""))
        for record in records
        if record.record_type == "claim.granted"
        and record.payload.get("worker_id") == worker_id
    }
    closed_claims = {
        str(record.payload.get("claim_id", ""))
        for record in records
        if record.record_type in {"claim.expired", "claim.released", "claim.submitted"}
    }
    return bool((open_claims - {""}) - closed_claims)


def completed_profile_promotion_matches(
    records: tuple[RuntimeRecord, ...],
    *,
    requested: WorkerRegistration,
    current: WorkerRegistration,
) -> bool:
    """Recognize only the committed local promotion that explains current vN."""
    history = [
        record
        for record in records
        if record.record_type == "worker.registered"
        and record.payload.get("worker_id") == current.worker_id
    ]
    if len(history) != 2:
        return False
    prior_record, current_record = history
    prior = registration_from_record(prior_record)
    promotions = [
        record
        for record in records
        if record.record_type == "worker.profile.promoted"
        and record.payload.get("worker_id") == current.worker_id
        and record.payload.get("registration_id") == current_record.id
    ]
    if len(promotions) != 1:
        return False
    promoted = promotions[0]
    registration_payload = deep_thaw(current_record.payload)
    expected_registration_payload = {
        **worker_registration_payload(current),
        "registered_by": registration_payload.get("registered_by"),
    }
    if (
        set(promoted.payload) != _PROMOTED_FIELDS
        or set(registration_payload) != set(expected_registration_payload)
        or registration_payload != expected_registration_payload
    ):
        return False
    if (
        not prior.version.isdecimal()
        or str(int(prior.version)) != prior.version
        or int(prior.version) < 1
        or not current.version.isdecimal()
        or str(int(current.version)) != current.version
        or int(current.version) < 1
    ):
        return False
    if (
        promoted.payload.get("previous_registration_id") != prior_record.id
        or promoted.payload.get("previous_version") != prior.version
        or promoted.payload.get("version") != current.version
        or promoted.payload.get("profile_id") != current.profile_id
        or promoted.payload.get("actor_id") != current.actor_id
        or promoted.payload.get("key_id") != current.key_id
        or prior.profile_id != ""
        or not prior.enabled
        or prior.capabilities
        or prior.pools
        or prior.capacity != 1
        or current.actor_id != current.worker_id
        or current.key_id != f"worker-{compute_content_hash(current.worker_id)[:16]}"
        or current.capabilities != prior.capabilities
        or current.pools != prior.pools
        or current.capacity != prior.capacity
        or current.enabled != prior.enabled
        or not current.version.isdecimal()
        or str(int(prior.version) + 1) != current.version
        or requested.profile_id != current.profile_id
        or requested.worker_id != current.worker_id
        or requested.actor_id != current.actor_id
        or requested.key_id != current.key_id
        or requested.capabilities != current.capabilities
        or requested.pools != current.pools
        or requested.capacity != current.capacity
        or requested.enabled != current.enabled
    ):
        return False
    candidate_id = promoted.payload.get("candidate_id")
    receipts = [
        record
        for record in records
        if record.record_type == "candidate.received"
        and record.payload.get("candidate_id") == candidate_id
    ]
    commits = [
        record
        for record in records
        if record.record_type == "candidate.committed"
        and record.payload.get("candidate_id") == candidate_id
    ]
    if len(receipts) != 1 or len(commits) != 1:
        return False
    receipt, commit = receipts[0], commits[0]
    committed_ids = commit.payload.get("committed_record_ids")
    if not isinstance(committed_ids, (tuple, list)) or len(committed_ids) != 2:
        return False
    return (
        list(committed_ids) == [promoted.id, current_record.id]
        and registration_payload.get("registered_by") == receipt.payload.get("actor_id")
        and tuple(receipt.payload.get("effect_types", ()))
        == ("worker.profile.promote", "worker.register")
        and current_record.causal_parents == (receipt.id,)
        and promoted.causal_parents == (receipt.id, prior_record.id, current_record.id)
        and commit.causal_parents == (receipt.id, *tuple(committed_ids))
    )


def project_worker_profile_promotion(
    effect: CandidateEffect,
    candidate: CandidateProposal,
    receipt_id: str,
    context: EffectProjectionContext,
) -> EffectProjection:
    """Project a paired local profile promotion without emitting registration."""
    previous_id, registration = _paired_registration(effect, candidate)
    registration_projection = project_worker_registration(
        candidate.effects[1], candidate, receipt_id, context
    )
    registration_record = registration_projection.records[0]
    replay = _exact_replay(
        effect=effect,
        candidate=candidate,
        receipt_id=receipt_id,
        context=context,
        previous_registration_id=previous_id,
        registration=registration,
        registration_record=registration_record,
    )
    if replay is not None:
        return EffectProjection((replay,), registration.worker_id)
    source_record = validate_worker_profile_promotion(
        context,
        previous_registration_id=previous_id,
        registration=registration,
    )
    source = registration_from_record(source_record)
    promoted = _promoted_fact(
        worker_id=registration.worker_id,
        previous_registration_id=previous_id,
        registration_record=registration_record,
        previous_version=source.version,
        registration=registration,
        candidate=candidate,
        receipt_id=receipt_id,
    )
    return EffectProjection((promoted,), registration.worker_id)


WORKER_PROFILE_PROMOTION_EFFECTS: Mapping[str, EffectHandler] = {
    "worker.profile.promote": EffectHandler(
        capability="worker.register",
        projector=project_worker_profile_promotion,
        direct_output_fact_types=frozenset({"worker.profile.promoted"}),
        reserved_fact_types=frozenset(),
    )
}
