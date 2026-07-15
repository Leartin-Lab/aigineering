"""Actor-key facts derived from authenticated Candidate authorization."""

from __future__ import annotations

from collections.abc import Iterable

from aigineering.core.record_conflict import ImmutableRecordConflict
from aigineering.protocol.candidate import ActorKey
from aigineering.protocol.runtime_record import RuntimeRecord


ACTOR_AUTHORIZED = "actor.authorized"


def actor_key_payload(key: ActorKey) -> dict[str, object]:
    return {
        "actor_id": key.actor_id,
        "capabilities": list(key.capabilities),
        "key_id": key.key_id,
        "kind": key.kind,
        "public_key": key.public_key,
    }


def actor_key_from_record(record: RuntimeRecord) -> ActorKey:
    if record.record_type != ACTOR_AUTHORIZED:
        raise ValueError(f"expected {ACTOR_AUTHORIZED}, got {record.record_type}")
    return ActorKey(
        actor_id=str(record.payload.get("actor_id", "")),
        key_id=str(record.payload.get("key_id", "")),
        kind=str(record.payload.get("kind", "")),
        public_key=str(record.payload.get("public_key", "")),
        capabilities=tuple(record.payload.get("capabilities", ())),
    )


def load_authorized_actor_keys(store) -> tuple[ActorKey, ...]:
    return tuple(
        actor_key_from_record(record)
        for _, record in store.scan_runtime_records(record_type=ACTOR_AUTHORIZED)
    )


def genesis_actor_identities(store) -> tuple[tuple[str, str], ...]:
    records = store.scan_runtime_records(record_type="domain.genesis")
    if not records:
        return ()
    keys = records[0][1].payload["manifest"]["root_keys"]
    return tuple((str(key["actor_id"]), str(key["key_id"])) for key in keys)


def validate_actor_authorization_record(
    record: RuntimeRecord,
    existing: list[tuple[int, RuntimeRecord]],
    *,
    reserved_identities: Iterable[tuple[str, str]] = (),
) -> None:
    if record.record_type != ACTOR_AUTHORIZED:
        return
    key = actor_key_from_record(record)
    identity = (key.actor_id, key.key_id)
    if key.kind in {"deterministic", "asig_"}:
        raise ValueError("actor authorization requires an authenticating key kind")
    if identity in set(reserved_identities):
        raise ImmutableRecordConflict("actor key", f"{key.actor_id}/{key.key_id}")
    for _, current in existing:
        current_key = actor_key_from_record(current)
        if (current_key.actor_id, current_key.key_id) != identity:
            continue
        if current.id == record.id:
            return
        raise ImmutableRecordConflict("actor key", f"{key.actor_id}/{key.key_id}")
